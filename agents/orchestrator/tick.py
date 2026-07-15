"""
Intraday Tick — Single Cron Entrypoint
------------------------------------------
Fired every trading minute (~9:25-15:59 ET) by cron. Stateless between
invocations: everything needed survives in SQLite
(tools/trading/position_store.py). Same execution model as the sibling
ORB/EMA projects — cron IS the scheduler, no daemon, no --schedule flag.

Steps 1-6 and 10 are IDENTICAL to the sibling projects' tick.py (trading-
day gate, virtual-equity computation, monitor_hard_stops, halt/terminate
check, reconcile/check_bracket_exits/audit, and _flatten_all at the
flatten deadline — including all of the ORB sibling's settle-delay/
retry/never-falsely-close/stale-prior-day-position fixes, ported
unchanged). Steps 7-9 are new, replacing ORB's one-shot screener +
opening-range capture + single breakout-scan with continuous VWAP-bias
tracking:

  7. EVERY tick (not once per N bars like the EMA sibling's 5-min
     cadence -- VWAP is naturally a 1-min-granularity cumulative
     construct, and 1-min bars match the cron cadence directly): update
     each ticker's VWAP-bias phase (no_bias -> clean_bias ->
     watching_resumption -> position_open -> back to clean_bias/no_bias),
     detect pullbacks, spawn/refresh vwap_setups rows. Fetches today's
     1-min bars for the whole universe ONCE; step 8 reuses the same fetch.
  8. Every tick, cheap: scan only the (typically small) set of tickers
     currently watching_resumption for a resumption breakout on the
     latest completed 1-minute close.
  9. After any position closes (via reconcile.check_bracket_exits or
     _flatten_all — both ported verbatim, never modified), release the
     linked vwap_state row so the same ticker can generate a new setup
     later the same day.

Usage:
  python agents/orchestrator/tick.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import time
from datetime import datetime, timedelta, date
import pytz
import pandas as pd
import pandas_market_calendars as mcal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus
from alpaca.data.timeframe import TimeFrame

from config.settings import (
    TIER_INITIAL_BALANCE, TIER_INTRADAY_BUYING_POWER,
    DAILY_LOSS_LIMIT_DOLLARS, MAX_DRAWDOWN_DOLLARS, DRAWDOWN_RATCHET_MULTIPLE,
    ATR_PERIOD, CLEAN_BIAS_MIN_BARS_SINCE_CROSS, CLEAN_BIAS_MIN_DISTANCE_ATR_MULT,
    SWING_LOOKBACK_BARS, FIXED_R_MULTIPLE, MIN_TARGET_RISK_REWARD,
    STOP_BUFFER_PCT, MAX_CONCURRENT_SETUPS,
    HALT_PROXY_MOVE_PCT, HALT_PROXY_WINDOW_MINUTES,
    ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE, FLATTEN_MINUTES_BEFORE_CLOSE,
)
from config.universe import UNIVERSE

from tools.trading import position_store
from tools.trading import rules_engine
from tools.trading import order_manager
from tools.market_data.intraday_bars import get_bars, get_latest_prices

from vwap_trend.signal import (
    compute_vwap, compute_atr, classify_bias, compute_clean_bias,
    detect_pullback_touch, detect_bias_failure, compute_swing_reference,
    compute_day_extreme, compute_stop_target, detect_breakout, detect_halt_proxy,
)

from agents.orchestrator import reconcile

ET = pytz.timezone("America/New_York")


def _client() -> TradingClient:
    return TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        paper=True,
    )


def is_market_open_today() -> bool:
    nyse = mcal.get_calendar('NYSE')
    today_str = date.today().isoformat()
    schedule = nyse.schedule(start_date=today_str, end_date=today_str)
    return not schedule.empty


def _market_close_et(now_et: datetime) -> datetime:
    """NYSE-calendar-aware close time — handles early closes (half days)."""
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=now_et.date().isoformat(), end_date=now_et.date().isoformat())
    close_utc = schedule.iloc[0]['market_close']
    return close_utc.tz_convert(ET)


# ── Step 1-6, 10: ported verbatim from the ORB/EMA siblings' tick.py ────

def _flatten_all(client: TradingClient, trading_date: str, reason: str) -> None:
    """
    Cancel every resting bracket, then market-flatten whatever Alpaca
    actually still holds. Ported unchanged from the sibling projects'
    tick.py, including the settle-delay, retry-vs-still-pending
    distinction, and never-falsely-close fixes found live in the ORB
    project on 2026-07-14 — nothing here is strategy-specific.
    """
    open_positions = position_store.list_open_positions()

    for pos in open_positions:
        if pos.get("alpaca_order_id"):
            order_manager.cancel_bracket(pos["alpaca_order_id"], client)

    if open_positions:
        time.sleep(3)  # let Alpaca release the qty hold before attempting to flatten

    alpaca_positions = {p.symbol: p for p in client.get_all_positions()}

    for pos in open_positions:
        ticker = pos["ticker"]
        alpaca_pos = alpaca_positions.get(ticker)
        if alpaca_pos is None or abs(float(alpaca_pos.qty)) <= 0:
            continue

        held_qty = abs(float(alpaca_pos.qty))
        exit_price = None
        confirmed_closed = False
        still_pending_order_id = None

        for attempt in range(3):
            close_client_order_id = f"vwap-flatten-{ticker}-{trading_date}-{pos['id']}-{attempt}"
            try:
                result = order_manager.close_position_market(ticker, held_qty, pos["direction"], close_client_order_id, client)
                order = reconcile.poll_for_fill(result["alpaca_order_id"], client, timeout_seconds=30)
            except Exception as e:
                print(f"  ⚠️  flatten order failed for {ticker} (attempt {attempt + 1}/3): {e}")
                time.sleep(3)
                continue

            if order.status == OrderStatus.FILLED and order.filled_avg_price:
                exit_price = float(order.filled_avg_price)
                confirmed_closed = True
                break
            elif order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                print(f"  ⚠️  flatten order for {ticker} was {order.status}, retrying ({attempt + 1}/3)")
                time.sleep(3)
                continue
            else:
                still_pending_order_id = result["alpaca_order_id"]
                print(f"  ⏳  flatten order for {ticker} still {order.status} after poll — "
                      f"leaving it live (order {still_pending_order_id}), not retrying")
                break

        if not confirmed_closed:
            detail = (f"order {still_pending_order_id} still live/pending" if still_pending_order_id
                       else "order rejected/cancelled after 3 attempts")
            rules_engine.record_breach(
                trading_date, "flatten_failed",
                f"{ticker}: {detail} (reason={reason})",
                "left position OPEN in DB for retry/monitoring -- STILL HELD AT ALPACA",
            )
            continue

        pnl_dollar = None
        if exit_price is not None and pos.get("fill_price") is not None:
            direction_sign = 1.0 if pos["direction"] == "long" else -1.0
            pnl_dollar = (exit_price - pos["fill_price"]) * pos["qty"] * direction_sign

        held_seconds = None
        if pos.get("filled_at"):
            try:
                held_seconds = (datetime.now() - datetime.fromisoformat(pos["filled_at"])).total_seconds()
            except Exception:
                pass

        valid = rules_engine.is_valid_trade(pnl_dollar, held_seconds)
        position_store.close_position(
            pos["id"], exit_reason=reason, exit_price=exit_price, exit_time=datetime.now().isoformat(),
            pnl_dollar=pnl_dollar, held_seconds=held_seconds, is_valid_trade=valid,
            close_client_order_id=close_client_order_id,
        )
        rules_engine.record_realized_pnl(trading_date, pnl_dollar)

    for pos in position_store.list_pending_positions(older_than_minutes=0):
        if pos.get("alpaca_order_id"):
            order_manager.cancel_bracket(pos["alpaca_order_id"], client)
        position_store.mark_position_failed(pos["id"], f"cancelled — {reason}", alpaca_order_id=pos.get("alpaca_order_id"))


# ── Step 7: continuous VWAP-bias state tracking, every tick ─────────────

def _build_setup_levels(bars_today: pd.DataFrame, latest_bar, direction: str, ema_stop_price: float,
                         bias_started_at) -> dict:
    entry_trigger = float(latest_bar["high"]) if direction == "long" else float(latest_bar["low"])
    floor_ts = pd.Timestamp(bias_started_at) if bias_started_at else None

    swing_kind = "swing_low" if direction == "long" else "swing_high"
    swing_stop = compute_swing_reference(bars_today, swing_kind, SWING_LOOKBACK_BARS, floor_ts)

    day_extreme_kind = "day_high" if direction == "long" else "day_low"
    day_extreme = compute_day_extreme(bars_today, day_extreme_kind)

    st = compute_stop_target(
        entry_trigger=entry_trigger, vwap_stop_price=ema_stop_price,
        swing_stop_price=swing_stop["price"], day_extreme_price=day_extreme["price"],
        direction=direction, stop_buffer_pct=STOP_BUFFER_PCT,
        fixed_r_multiple=FIXED_R_MULTIPLE, min_target_risk_reward=MIN_TARGET_RISK_REWARD,
    )
    st["day_extreme_price"] = day_extreme["price"]
    st["day_extreme_ts"] = str(day_extreme["ts"])
    st["swing_ref_price"] = swing_stop["price"]
    st["swing_ref_ts"] = str(swing_stop["ts"])
    return st


def _update_vwap_states(trading_date: str, now_et: datetime) -> dict:
    """Fetches today's 1-min bars for the whole universe once (returned so
    step 8 can reuse it), then updates each ticker's VWAP-bias phase."""
    market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et <= market_open_et:
        return {}

    try:
        bars_by_ticker = get_bars(UNIVERSE, market_open_et.astimezone(pytz.utc), now_et.astimezone(pytz.utc),
                                   timeframe=TimeFrame.Minute)
    except Exception as e:
        print(f"  ⚠️  failed to fetch 1-min bars for VWAP update: {e}")
        return {}

    for ticker, bars in bars_by_ticker.items():
        if bars.empty or len(bars) < ATR_PERIOD + 1:
            continue

        latest_bar_ts = str(bars.index[-1])
        vs = position_store.get_or_create_vwap_state(trading_date, ticker)
        if vs["last_bar_ts"] == latest_bar_ts:
            continue  # already processed this exact bar

        vwap_series = compute_vwap(bars)
        atr_series = compute_atr(bars, ATR_PERIOD)
        vwap = float(vwap_series.iloc[-1])
        price = float(bars["close"].iloc[-1])
        direction = classify_bias(price, vwap)
        latest_bar = bars.iloc[-1]

        update = {"vwap": vwap, "last_bar_ts": latest_bar_ts}

        flipped = vs["direction"] not in (None, "none") and direction not in (None, "none") and direction != vs["direction"]
        if flipped:
            if vs.get("active_setup_id"):
                position_store.update_setup(vs["active_setup_id"], status="abandoned_bias_flip")
            update.update({
                "phase": "no_bias", "direction": direction, "bias_started_at": latest_bar_ts,
                "clean_since_bar_ts": None, "pullback_bar_high": None, "pullback_bar_low": None,
                "pullback_bar_ts": None, "active_setup_id": None,
            })
            position_store.update_vwap_state(trading_date, ticker, **update)
            continue

        if vs["bias_started_at"] is None or direction != vs.get("direction"):
            update["bias_started_at"] = latest_bar_ts
        update["direction"] = direction

        phase = vs["phase"]

        if phase in ("no_bias", "clean_bias") and direction in ("long", "short"):
            clean = compute_clean_bias(
                bars["close"].iloc[:len(bars)], vwap_series, atr_series,
                CLEAN_BIAS_MIN_BARS_SINCE_CROSS, CLEAN_BIAS_MIN_DISTANCE_ATR_MULT,
            )
            if clean["is_clean"]:
                if phase == "no_bias":
                    update["clean_since_bar_ts"] = latest_bar_ts
                phase = "clean_bias"
            else:
                phase = "no_bias"
            update["phase"] = phase
        elif direction not in ("long", "short"):
            phase = "no_bias"
            update["phase"] = phase

        bias_started_at = update.get("bias_started_at") or vs.get("bias_started_at")

        if phase == "clean_bias" and vs["cycles_today"] < MAX_CONCURRENT_SETUPS:
            touch = detect_pullback_touch(latest_bar, vwap, direction)
            if touch["touched"]:
                cycle_number = vs["cycles_today"] + 1
                setup_id = position_store.create_setup(trading_date, ticker, cycle_number, direction)
                try:
                    st = _build_setup_levels(bars, latest_bar, direction, vwap, bias_started_at)
                    position_store.update_setup(
                        setup_id,
                        pullback_bar_high=float(latest_bar["high"]), pullback_bar_low=float(latest_bar["low"]),
                        pullback_bar_ts=latest_bar_ts, swing_ref_price=st["swing_ref_price"], swing_ref_ts=st["swing_ref_ts"],
                        day_extreme_price=st["day_extreme_price"], day_extreme_ts=st["day_extreme_ts"],
                        entry_trigger=st["entry_trigger"], stop_price=st["stop_price"],
                        target_price=st["target_price"], risk_per_share=st["risk_per_share"],
                        stop_basis=st["stop_basis"], target_basis=st["target_basis"],
                    )
                    update.update({
                        "phase": "watching_resumption", "active_setup_id": setup_id, "cycles_today": cycle_number,
                        "pullback_bar_high": float(latest_bar["high"]), "pullback_bar_low": float(latest_bar["low"]),
                        "pullback_bar_ts": latest_bar_ts,
                    })
                except ValueError as e:
                    position_store.update_setup(setup_id, status="skipped_stop_error")
                    print(f"  ⚠️  {ticker}: pullback setup skipped, no valid stop: {e}")

        elif phase == "watching_resumption":
            active_id = vs.get("active_setup_id")
            setup = position_store.get_setup(active_id) if active_id else None
            if setup is None:
                update["phase"] = "clean_bias"
            else:
                bar_close = float(latest_bar["close"])
                if detect_bias_failure(bar_close, vwap, direction):
                    position_store.update_setup(active_id, status="abandoned_bias_failure")
                    update.update({"phase": "no_bias", "active_setup_id": None})
                elif setup["status"] == "watching":
                    touch = detect_pullback_touch(latest_bar, vwap, direction)
                    if touch["touched"]:
                        try:
                            st = _build_setup_levels(bars, latest_bar, direction, vwap, bias_started_at)
                            position_store.update_setup(
                                active_id,
                                pullback_bar_high=float(latest_bar["high"]), pullback_bar_low=float(latest_bar["low"]),
                                pullback_bar_ts=latest_bar_ts, swing_ref_price=st["swing_ref_price"], swing_ref_ts=st["swing_ref_ts"],
                                day_extreme_price=st["day_extreme_price"], day_extreme_ts=st["day_extreme_ts"],
                                entry_trigger=st["entry_trigger"], stop_price=st["stop_price"],
                                target_price=st["target_price"], risk_per_share=st["risk_per_share"],
                                stop_basis=st["stop_basis"], target_basis=st["target_basis"],
                            )
                            update.update({
                                "pullback_bar_high": float(latest_bar["high"]), "pullback_bar_low": float(latest_bar["low"]),
                                "pullback_bar_ts": latest_bar_ts,
                            })
                        except ValueError:
                            pass  # keep the existing setup fields if a fresh recompute fails this bar

        position_store.update_vwap_state(trading_date, ticker, **update)

    return bars_by_ticker


# ── Step 8: every tick, only for tickers currently watching_resumption ──

def _scan_resumption_breakouts(
    client: TradingClient, trading_date: str, bars_by_ticker: dict,
    account_state: dict, daily_state: dict, current_equity: float, now_et: datetime,
) -> None:
    watching = position_store.list_watching_setups(trading_date)
    if not watching:
        return

    remaining_intraday_risk_committed = 0.0
    buying_power = TIER_INTRADAY_BUYING_POWER  # simulated tier BP -- NEVER Alpaca's real (larger) account BP

    for setup in watching:
        ticker = setup["ticker"]
        bars_today = bars_by_ticker.get(ticker)
        if bars_today is None or bars_today.empty:
            continue

        trailing = bars_today.tail(HALT_PROXY_WINDOW_MINUTES)
        if not trailing.empty:
            halt = detect_halt_proxy(trailing, HALT_PROXY_MOVE_PCT)
            if halt["is_halt_proxy"]:
                position_store.update_setup(setup["id"], status="invalidated_halt_proxy")
                continue

        latest_bar = bars_today.iloc[-1]
        latest_close = float(latest_bar["close"])

        if not detect_breakout(latest_close, setup["entry_trigger"], setup["direction"]):
            continue

        if position_store.get_open_position_db(ticker) is not None:
            position_store.update_setup(setup["id"], status="triggered")
            continue

        if not order_manager.is_tradable(ticker, setup["direction"], client):
            position_store.update_setup(setup["id"], status="skipped_not_tradable")
            continue

        prior_minute_volume = float(latest_bar["volume"]) if "volume" in bars_today.columns else 0.0

        check = rules_engine.pre_trade_check(
            setup, account_state, daily_state, current_equity,
            remaining_intraday_risk_committed, prior_minute_volume, buying_power,
        )
        if not check["approved"]:
            reason_tag = (check["reasons"][0][:50] if check["reasons"] else "not_approved").replace(" ", "_")
            position_store.update_setup(setup["id"], status=f"skipped_{reason_tag}"[:60])
            continue

        client_order_id = f"vwap-open-{ticker}-{trading_date}-{setup['id']}"
        try:
            position_id = position_store.create_pending_position(
                setup["id"], trading_date, ticker, setup["direction"], check["final_qty"],
                setup["stop_price"], setup["target_price"], setup["risk_per_share"], check["risk_dollars"],
                client_order_id,
            )
        except Exception:
            continue  # already attempted this ticker today (idempotency guard)

        try:
            result = order_manager.open_bracket_position(
                ticker, setup["direction"], check["final_qty"], setup["stop_price"], setup["target_price"],
                latest_close, client_order_id, client,
            )
        except Exception as e:
            position_store.mark_position_failed(position_id, f"submission error: {e}")
            position_store.update_setup(setup["id"], status="skipped_order_error")
            continue

        order = reconcile.poll_for_fill(result["alpaca_order_id"], client, timeout_seconds=30)

        if order.status == OrderStatus.PARTIALLY_FILLED:
            order_manager.cancel_bracket(result["alpaca_order_id"], client)
            time.sleep(2)

        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            confirmed = reconcile.confirm_fill_via_position(ticker, client)
            if confirmed:
                filled_at = str(order.filled_at) if order.filled_at else datetime.now().isoformat()
                position_store.mark_position_open(
                    position_id, result["alpaca_order_id"], str(order.status),
                    fill_price=confirmed["avg_entry_price"], filled_qty=confirmed["qty"], filled_at=filled_at,
                )
                position_store.update_setup(setup["id"], status="triggered")
                position_store.update_vwap_state(
                    trading_date, ticker, phase="position_open", active_setup_id=setup["id"],
                )
                remaining_intraday_risk_committed += check["risk_dollars"]
                print(f"  ✅ Opened {setup['direction']} {ticker} x{confirmed['qty']} @ {confirmed['avg_entry_price']:.2f} "
                      f"(cycle {setup['cycle_number']}, stop={setup['stop_basis']}, target={setup['target_basis']})")
            else:
                position_store.mark_position_failed(position_id, "fill unconfirmed", alpaca_order_id=result["alpaca_order_id"])
                position_store.update_setup(setup["id"], status="skipped_fill_unconfirmed")
        elif order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            position_store.mark_position_failed(position_id, f"order {order.status}", alpaca_order_id=result["alpaca_order_id"])
            position_store.update_setup(setup["id"], status=f"skipped_order_{str(order.status).lower()}"[:60])
        else:
            order_manager.cancel_bracket(result["alpaca_order_id"], client)
            position_store.mark_position_failed(position_id, "poll timeout -- order cancelled", alpaca_order_id=result["alpaca_order_id"])
            position_store.update_setup(setup["id"], status="skipped_timeout")


# ── Step 9: release vwap_state after a position closes ───────────────────

def _release_closed_setups(trading_date: str) -> None:
    """
    Runs every tick, after reconcile.check_bracket_exits()/_flatten_all()
    (both ported verbatim, never modified here) may have just closed a
    position. Idempotent scan over today's positions: any vwap_state row
    still pointing at a now-closed setup gets released so the same ticker
    can be watched for a fresh setup later the same day.
    """
    for pos in position_store.list_positions_for_date(trading_date):
        if pos["status"] != "closed":
            continue
        vs = position_store.get_vwap_state(trading_date, pos["ticker"])
        if vs is None or vs.get("active_setup_id") != pos["candidate_id"]:
            continue

        vwap = vs.get("vwap")
        direction = vs.get("direction")
        still_directional = vwap is not None and direction in ("long", "short")
        new_phase = "clean_bias" if still_directional else "no_bias"
        position_store.update_vwap_state(
            trading_date, pos["ticker"], active_setup_id=None, phase=new_phase,
        )


def run_tick() -> None:
    now_et = datetime.now(ET)
    trading_date = now_et.date().isoformat()

    if not is_market_open_today():
        print(f"[{now_et}] Market closed today (holiday/weekend) — no-op.")
        return

    client = _client()

    position_store.init_account_state(
        initial_balance=TIER_INITIAL_BALANCE,
        daily_loss_limit_dollars=DAILY_LOSS_LIMIT_DOLLARS,
        max_drawdown_dollars=MAX_DRAWDOWN_DOLLARS,
        drawdown_ratchet_multiple=DRAWDOWN_RATCHET_MULTIPLE,
        evaluation_start_date=trading_date,
    )
    account_state = position_store.get_account_state()

    if account_state["terminated"]:
        print(f"[{now_et}] Account TERMINATED (max drawdown breach) — no further trading.")
        return

    open_positions_for_equity = position_store.list_open_positions()
    latest_prices_for_equity = {}
    if open_positions_for_equity:
        try:
            latest_prices_for_equity = get_latest_prices([p["ticker"] for p in open_positions_for_equity])
        except Exception:
            latest_prices_for_equity = {}
    current_equity = rules_engine.compute_virtual_equity(account_state, open_positions_for_equity, latest_prices_for_equity)

    daily_state = rules_engine.get_or_create_daily_state(trading_date, current_equity)

    stale_positions = [p for p in position_store.list_open_positions() if p["trading_date"] != trading_date]
    if stale_positions:
        print(f"  ⚠️  {len(stale_positions)} position(s) carried over from a prior trading day — "
              f"flattening immediately: {[p['ticker'] for p in stale_positions]}")
        for p in stale_positions:
            rules_engine.record_breach(
                trading_date, "unexpected_overnight_hold",
                f"{p['ticker']} (opened {p['trading_date']}) still open at start of {trading_date}",
                "force-flattening immediately",
            )
        _flatten_all(client, trading_date, reason="unexpected_overnight_hold")

    hard_stops = rules_engine.monitor_hard_stops(account_state, daily_state, current_equity)
    account_state = hard_stops["account_state"]

    if not daily_state["trading_halted"] and hard_stops["daily_loss_breached"]:
        _flatten_all(client, trading_date, reason="daily_loss_limit_breach")
        rules_engine.record_breach(
            trading_date, "daily_loss_limit",
            f"daily loss ${hard_stops['daily_loss_detail']['daily_loss']:.2f} breached limit "
            f"${daily_state['daily_loss_limit_snapshot']:.2f}",
            "closed all positions/orders, halted trading for the day",
        )
        position_store.update_daily_state(
            trading_date, trading_halted=1, halt_reason="daily_loss_limit", halted_at=now_et.isoformat()
        )
        daily_state = position_store.get_daily_state(trading_date)

    if not account_state["terminated"] and hard_stops["drawdown_breached"]:
        _flatten_all(client, trading_date, reason="max_drawdown_breach")
        rules_engine.record_breach(
            trading_date, "max_drawdown",
            f"equity ${current_equity:.2f} breached drawdown floor ${hard_stops['drawdown_detail']['floor']:.2f}",
            "closed all positions/orders, PERMANENT account termination",
        )
        position_store.update_account_state(
            terminated=1, terminated_at=now_et.isoformat(), terminated_reason="max_drawdown_breach"
        )
        account_state = position_store.get_account_state()

    if daily_state["trading_halted"] or account_state["terminated"]:
        print(f"[{now_et}] Trading halted/terminated — no further action this tick.")
        return

    # Step 6: reconcile + detect bracket-leg exits + audit.
    reconcile.reconcile_pending_positions(client)
    reconcile.check_bracket_exits(client)
    reconcile.audit_open_positions(client)

    # Step 9 (runs right after exits are detected, before the next scan).
    _release_closed_setups(trading_date)

    # Step 7: VWAP-bias state update, every tick.
    bars_by_ticker = _update_vwap_states(trading_date, now_et)

    # Step 8: resumption-breakout scan + entry, every tick.
    close_et = _market_close_et(now_et)
    entry_cutoff = close_et - timedelta(minutes=ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE)
    if now_et < entry_cutoff:
        _scan_resumption_breakouts(client, trading_date, bars_by_ticker, account_state, daily_state, current_equity, now_et)
    else:
        # Past the entry cutoff -- sweep any still-watching setups so the
        # EOD report doesn't show stale "watching" rows for the day.
        for setup in position_store.list_watching_setups(trading_date):
            position_store.update_setup(setup["id"], status="expired_eod")

    # Step 10: flatten deadline.
    flatten_deadline = close_et - timedelta(minutes=FLATTEN_MINUTES_BEFORE_CLOSE)
    if now_et >= flatten_deadline:
        _flatten_all(client, trading_date, reason="eod_flatten")

    print(f"[{now_et}] Tick complete.")


if __name__ == "__main__":
    run_tick()

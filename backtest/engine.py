"""
Backtest Engine
------------------
Replays vwap_trend/signal.py's exact pure functions bar-by-bar against
historical 1-min data, with no look-ahead. Reuses
tools/trading/rules_engine.py's position-sizing/loss-limit/drawdown
functions directly — the same functions the live system calls — so
there's no risk of backtest logic silently drifting from live logic.

Unlike the EMA sibling's backtest (a continuous multi-day EMA/ATR
calculation), VWAP and ATR here are recomputed FRESH each trading day
from that day's bars only, exactly matching tick.py's
`_update_vwap_states` (which only ever fetches bars from that day's
market open onward) — cross-day continuity would not be a real intraday
VWAP. This means each new day has its own ~14-bar ATR warm-up, same as
live.

Primary purpose: validate CLEAN_BIAS_MIN_BARS_SINCE_CROSS and
CLEAN_BIAS_MIN_DISTANCE_ATR_MULT (the two genuinely arbitrary defaults
in config/settings.py) against real historical data before risking paper
capital on unvalidated numbers — not to produce a headline return figure.

Simplifications, stated plainly rather than hidden:
- Fill price on a triggered breakout is assumed to be exactly the
  entry_trigger price (no slippage modeled).
- Bracket-fill resolution: if a single bar's [low, high] range contains
  BOTH the stop and target, the STOP is assumed to hit first (conservative).
- Volume cap uses the 1-min bar's own volume directly (no /5 scaling
  needed here, unlike the EMA sibling's 5-min bars — this project's bars
  already ARE 1-minute, the live system's literal "prior one-minute
  volume" input).
"""

from typing import Optional
import pandas as pd

from vwap_trend.signal import (
    compute_vwap, compute_atr, classify_bias, compute_clean_bias,
    detect_pullback_touch, detect_bias_failure, compute_swing_reference,
    compute_day_extreme, compute_stop_target, detect_breakout,
)
from tools.trading.rules_engine import (
    compute_position_size, compute_final_qty, check_daily_loss_limit, check_max_drawdown,
)
from config.settings import (
    ATR_PERIOD, CLEAN_BIAS_MIN_BARS_SINCE_CROSS, CLEAN_BIAS_MIN_DISTANCE_ATR_MULT,
    SWING_LOOKBACK_BARS, FIXED_R_MULTIPLE, MIN_TARGET_RISK_REWARD, STOP_BUFFER_PCT,
    VOLUME_CAP_PCT, RISK_PER_TRADE_PCT, TIER_INITIAL_BALANCE, TIER_INTRADAY_BUYING_POWER,
    DAILY_LOSS_LIMIT_DOLLARS, MAX_DRAWDOWN_DOLLARS, DRAWDOWN_RATCHET_MULTIPLE,
)


class _TickerDayState:
    """In-memory equivalent of a vwap_state row for one (ticker, day) — no DB I/O."""
    __slots__ = ("phase", "direction", "bias_started_at", "active_setup", "cycles_today")

    def __init__(self):
        self.phase = "no_bias"
        self.direction = None
        self.bias_started_at = None
        self.active_setup = None
        self.cycles_today = 0


def _build_setup(bars_so_far, latest_bar, direction, vwap, bias_started_at,
                  swing_lookback_n, stop_buffer_pct, fixed_r_multiple, min_target_risk_reward):
    entry_trigger = float(latest_bar["high"]) if direction == "long" else float(latest_bar["low"])
    floor_ts = pd.Timestamp(bias_started_at) if bias_started_at is not None else None

    swing_kind = "swing_low" if direction == "long" else "swing_high"
    swing_stop = compute_swing_reference(bars_so_far, swing_kind, swing_lookback_n, floor_ts)

    day_extreme_kind = "day_high" if direction == "long" else "day_low"
    day_extreme = compute_day_extreme(bars_so_far, day_extreme_kind)

    return compute_stop_target(
        entry_trigger=entry_trigger, vwap_stop_price=vwap, swing_stop_price=swing_stop["price"],
        day_extreme_price=day_extreme["price"], direction=direction, stop_buffer_pct=stop_buffer_pct,
        fixed_r_multiple=fixed_r_multiple, min_target_risk_reward=min_target_risk_reward,
    )


def _simulate_day(
    ticker: str, day_bars: pd.DataFrame, account_state: dict, daily_states: dict,
    min_bars_since_cross: int, min_distance_atr_mult: float, swing_lookback_n: int,
    stop_buffer_pct: float, fixed_r_multiple: float, min_target_risk_reward: float,
) -> list:
    trades = []
    if day_bars.empty or len(day_bars) < ATR_PERIOD + 2:
        return trades

    trading_date = day_bars.index[0].strftime("%Y-%m-%d")
    state = _TickerDayState()
    open_position = None

    vwap_series = compute_vwap(day_bars)
    atr_series = compute_atr(day_bars, ATR_PERIOD)
    warmup = ATR_PERIOD + 1

    for i in range(warmup, len(day_bars)):
        bar = day_bars.iloc[i]
        bar_ts = day_bars.index[i]

        vwap = float(vwap_series.iloc[i])
        price = float(bar["close"])
        direction = classify_bias(price, vwap)

        # ---- manage an already-open position first ----
        if open_position is not None:
            low, high = float(bar["low"]), float(bar["high"])
            stop, target = open_position["stop"], open_position["target"]
            exit_price, exit_reason = None, None

            if open_position["direction"] == "long":
                stop_hit, target_hit = low <= stop, high >= target
            else:
                stop_hit, target_hit = high >= stop, low <= target

            if stop_hit:
                exit_price, exit_reason = stop, "stop_hit"
            elif target_hit:
                exit_price, exit_reason = target, "target_hit"

            is_last_bar_of_day = (i + 1 >= len(day_bars))
            if exit_price is None and is_last_bar_of_day:
                exit_price, exit_reason = float(bar["close"]), "eod_flatten"

            if exit_price is not None:
                direction_sign = 1.0 if open_position["direction"] == "long" else -1.0
                pnl_dollar = (exit_price - open_position["entry_price"]) * open_position["qty"] * direction_sign
                trades.append({
                    "ticker": ticker, "trading_date": trading_date, "direction": open_position["direction"],
                    "entry_price": open_position["entry_price"], "exit_price": exit_price, "exit_reason": exit_reason,
                    "qty": open_position["qty"], "pnl_dollar": pnl_dollar, "cycle": open_position["cycle"],
                })
                ds = daily_states[trading_date]
                ds["realized_pnl_running"] += pnl_dollar
                account_state["cumulative_realized_pnl"] += pnl_dollar
                open_position = None
                state.phase = "clean_bias" if direction in ("long", "short") else "no_bias"
                state.active_setup = None
            else:
                continue

        # ---- VWAP-bias state machine, mirrors tick.py's _update_vwap_states ----
        flipped = state.direction not in (None, "none") and direction not in (None, "none") and direction != state.direction
        if flipped:
            state.phase = "no_bias"
            state.bias_started_at = bar_ts
            state.active_setup = None
        if state.bias_started_at is None or direction != state.direction:
            state.bias_started_at = bar_ts
        state.direction = direction

        if state.phase in ("no_bias", "clean_bias") and direction in ("long", "short"):
            clean = compute_clean_bias(
                day_bars["close"].iloc[:i + 1], vwap_series.iloc[:i + 1], atr_series.iloc[:i + 1],
                min_bars_since_cross, min_distance_atr_mult,
            )
            state.phase = "clean_bias" if clean["is_clean"] else "no_bias"
        elif direction not in ("long", "short"):
            state.phase = "no_bias"

        bars_so_far = day_bars.iloc[:i + 1]

        if state.phase == "clean_bias":
            touch = detect_pullback_touch(bar, vwap, direction)
            if touch["touched"]:
                try:
                    st = _build_setup(bars_so_far, bar, direction, vwap, state.bias_started_at,
                                       swing_lookback_n, stop_buffer_pct, fixed_r_multiple, min_target_risk_reward)
                    state.cycles_today += 1
                    state.active_setup = {**st, "direction": direction, "cycle": state.cycles_today}
                    state.phase = "watching_resumption"
                except ValueError:
                    pass

        elif state.phase == "watching_resumption" and state.active_setup is not None:
            bar_close = float(bar["close"])
            if detect_bias_failure(bar_close, vwap, direction):
                state.phase = "no_bias"
                state.active_setup = None
            else:
                touch = detect_pullback_touch(bar, vwap, direction)
                if touch["touched"]:
                    try:
                        st = _build_setup(bars_so_far, bar, direction, vwap, state.bias_started_at,
                                           swing_lookback_n, stop_buffer_pct, fixed_r_multiple, min_target_risk_reward)
                        state.active_setup.update(st)
                    except ValueError:
                        pass

                if detect_breakout(bar_close, state.active_setup["entry_trigger"], direction):
                    daily_states.setdefault(trading_date, {
                        "realized_pnl_running": 0.0,
                        "start_of_day_equity": account_state["initial_balance"] + account_state["cumulative_realized_pnl"],
                    })
                    ds = daily_states[trading_date]
                    current_equity = account_state["initial_balance"] + account_state["cumulative_realized_pnl"]
                    daily_check = check_daily_loss_limit(ds, current_equity, DAILY_LOSS_LIMIT_DOLLARS)
                    drawdown_check = check_max_drawdown(account_state, current_equity)

                    stop_distance = abs(state.active_setup["entry_trigger"] - state.active_setup["stop_price"])
                    risk_qty = compute_position_size(DAILY_LOSS_LIMIT_DOLLARS, RISK_PER_TRADE_PCT, stop_distance)
                    prior_minute_volume = float(bar["volume"])
                    sizing = compute_final_qty(risk_qty, prior_minute_volume, VOLUME_CAP_PCT, TIER_INTRADAY_BUYING_POWER, bar_close)
                    qty = sizing["final_qty"]
                    risk_dollars = qty * stop_distance

                    approved = (
                        qty > 0 and not daily_check["breached"] and not drawdown_check["breached"]
                        and risk_dollars <= daily_check["remaining_budget"]
                        and risk_dollars <= drawdown_check["buffer_remaining"]
                    )
                    if approved:
                        open_position = {
                            "entry_price": state.active_setup["entry_trigger"], "stop": state.active_setup["stop_price"],
                            "target": state.active_setup["target_price"], "direction": direction, "qty": qty,
                            "cycle": state.active_setup["cycle"],
                        }
                        state.phase = "position_open"
                    else:
                        state.phase = "clean_bias"
                        state.active_setup = None

    return trades


def run_backtest_for_ticker(
    ticker: str,
    bars: pd.DataFrame,
    account_state: dict,
    daily_states: dict,
    min_bars_since_cross: int = CLEAN_BIAS_MIN_BARS_SINCE_CROSS,
    min_distance_atr_mult: float = CLEAN_BIAS_MIN_DISTANCE_ATR_MULT,
    swing_lookback_n: int = SWING_LOOKBACK_BARS,
    stop_buffer_pct: float = STOP_BUFFER_PCT,
    fixed_r_multiple: float = FIXED_R_MULTIPLE,
    min_target_risk_reward: float = MIN_TARGET_RISK_REWARD,
) -> list:
    """Splits bars by trading day (VWAP/ATR reset daily, matching live) and
    simulates each day independently, sharing one running account ledger."""
    if bars.empty:
        return []

    trades = []
    day_keys = bars.index.strftime("%Y-%m-%d")
    for trading_date in day_keys.unique():
        day_bars = bars[day_keys == trading_date]
        trades.extend(_simulate_day(
            ticker, day_bars, account_state, daily_states,
            min_bars_since_cross, min_distance_atr_mult, swing_lookback_n,
            stop_buffer_pct, fixed_r_multiple, min_target_risk_reward,
        ))
    return trades


def run_backtest(bars_by_ticker: dict, initial_balance: float = TIER_INITIAL_BALANCE, **kwargs) -> dict:
    """Runs all tickers against ONE shared account_state/daily_states ledger
    (same compliance budget contention as live), returns {trades, final_pnl, account_state}."""
    account_state = {
        "initial_balance": initial_balance,
        "cumulative_realized_pnl": 0.0,
        "max_drawdown_dollars": MAX_DRAWDOWN_DOLLARS,
        "drawdown_ratchet_multiple": DRAWDOWN_RATCHET_MULTIPLE,
        "is_ratcheted": False,
    }
    daily_states: dict = {}
    all_trades = []
    for ticker, bars in bars_by_ticker.items():
        trades = run_backtest_for_ticker(ticker, bars, account_state, daily_states, **kwargs)
        all_trades.extend(trades)
    all_trades.sort(key=lambda t: (t["trading_date"], t["ticker"]))
    return {"trades": all_trades, "final_pnl": account_state["cumulative_realized_pnl"], "account_state": account_state}

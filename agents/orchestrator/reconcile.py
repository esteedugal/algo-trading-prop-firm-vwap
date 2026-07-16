"""
Reconciliation & Fill Polling
--------------------------------
Keeps the local position_store honest against Alpaca's real order/
position state across 1-minute-granularity, stateless tick invocations.

Four related concerns:

1. poll_for_fill() — bounded wait for a just-submitted bracket ENTRY
   order to reach a terminal status, within the current tick.

2. confirm_fill_via_position() — after a terminal fill, read the actual
   Alpaca position as source of truth rather than trusting the order
   object's filled_qty/filled_avg_price, which can be stale even when
   status reads FILLED for fast fills on liquid names (the exact race
   condition found and fixed live in the momentum project this session —
   see project memory). Applied identically here since it's the same SDK
   and the same fill-latency behavior.

3. reconcile_pending_positions() — defense-in-depth for a mid-tick
   process crash (poll_for_fill above doesn't help if the process itself
   died). Same deterministic-client_order_id lookup pattern as the
   sibling projects.

4. check_bracket_exits() — THIS project's genuinely new concern, no
   sibling analog: a bracket's take-profit or stop-loss leg can fill on
   its own, silently, between ticks (that's the whole point of a bracket
   order — no polling loop watches it fill). Every tick must check every
   DB-open position's bracket parent order for a resolved exit leg and
   close the position_store row accordingly — this is how exits actually
   get recorded, not by tick.py deciding to close anything itself.

5. audit_open_positions() — final sanity check: DB 'open' rows against
   Alpaca's actual current equity positions. Logs mismatches loudly
   rather than silently trusting the DB.
"""

from dotenv import load_dotenv
import os
load_dotenv('config/.env')

import time
from datetime import datetime
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus, OrderType

from tools.trading import position_store
from tools.trading.rules_engine import is_valid_trade, record_realized_pnl


def _client() -> TradingClient:
    return TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        paper=True,
    )


def poll_for_fill(order_id: str, trading_client: Optional[TradingClient] = None, timeout_seconds: int = 45, interval_seconds: int = 3):
    """
    Bounded poll for an order to reach a terminal status. A marketable-
    limit entry on a name the screener already confirmed has abnormal
    volume, during market hours, should resolve almost immediately --
    this is a safety bound within the 1-minute tick budget, not the
    expected common case.

    PARTIALLY_FILLED is deliberately NOT treated as terminal -- found live
    2026-07-15 (VWAP sibling) closing out a full-size EOD flatten: a
    market order momentarily read PARTIALLY_FILLED at the instant of a
    poll, the caller treated that as "still resting, leave it alone,"
    and the order went on to reach FILLED a few seconds later with
    nothing left watching it -- the DB never learned the position had
    actually closed. Keep polling through PARTIALLY_FILLED until it
    resolves to FILLED or a genuine stop (CANCELED/REJECTED/EXPIRED/
    DONE_FOR_DAY) or the timeout budget runs out.
    """
    trading_client = trading_client or _client()
    terminal = {
        OrderStatus.FILLED, OrderStatus.CANCELED,
        OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.DONE_FOR_DAY,
    }
    elapsed = 0
    order = trading_client.get_order_by_id(order_id)
    while order.status not in terminal and elapsed < timeout_seconds:
        time.sleep(interval_seconds)
        elapsed += interval_seconds
        order = trading_client.get_order_by_id(order_id)
    return order


def confirm_fill_via_position(ticker: str, trading_client: Optional[TradingClient] = None) -> Optional[dict]:
    """
    Source-of-truth fill verification. Returns None if no position
    exists yet — caller must treat that as "not actually open," never
    assume success purely from the order object's own status field.
    """
    trading_client = trading_client or _client()
    try:
        pos = trading_client.get_open_position(ticker)
    except Exception:
        return None
    return {"qty": abs(float(pos.qty)), "avg_entry_price": float(pos.avg_entry_price)}


def reconcile_pending_positions(trading_client: Optional[TradingClient] = None, older_than_minutes: int = 3) -> list:
    """Defense-in-depth for a mid-tick process crash. older_than_minutes
    is short (3, not the sibling projects' 5) since this project's next
    tick is only 60 seconds away, not a week."""
    trading_client = trading_client or _client()
    results = []

    for pos in position_store.list_pending_positions(older_than_minutes=older_than_minutes):
        client_order_id = pos["client_order_id"]
        try:
            order = trading_client.get_order_by_client_id(client_order_id)
        except Exception:
            order = None

        if order is None:
            position_store.mark_position_failed(
                pos["id"], "order not found at Alpaca — submission likely never completed"
            )
            results.append({"position_id": pos["id"], "ticker": pos["ticker"], "resolution": "failed_not_found"})
        elif order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            results_reason = f"order {order.status}"
            position_store.mark_position_failed(pos["id"], results_reason, alpaca_order_id=str(order.id))
            results.append({"position_id": pos["id"], "ticker": pos["ticker"], "resolution": f"failed_{order.status}"})
        elif order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            confirmed = confirm_fill_via_position(pos["ticker"], trading_client)
            if confirmed is None:
                position_store.mark_position_failed(
                    pos["id"], "order status FILLED but no matching Alpaca position found",
                    alpaca_order_id=str(order.id),
                )
                results.append({"position_id": pos["id"], "ticker": pos["ticker"], "resolution": "failed_fill_unconfirmed"})
            else:
                filled_at = str(order.filled_at) if order.filled_at else datetime.now().isoformat()
                position_store.mark_position_open(
                    pos["id"], str(order.id), str(order.status),
                    fill_price=confirmed["avg_entry_price"], filled_qty=confirmed["qty"],
                    filled_at=filled_at,
                )
                results.append({"position_id": pos["id"], "ticker": pos["ticker"], "resolution": "recovered_open"})
        else:
            results.append({"position_id": pos["id"], "ticker": pos["ticker"], "resolution": "still_pending"})

    return results


def check_bracket_exits(trading_client: Optional[TradingClient] = None) -> list:
    """
    The mechanism by which exits actually get recorded for this project:
    a bracket's take-profit or stop-loss leg can fill on its own between
    ticks, with nothing else watching. Every tick must check every DB-open
    position's bracket parent order for a resolved leg and close the
    position_store row accordingly.
    """
    trading_client = trading_client or _client()
    closed = []

    for pos in position_store.list_open_positions():
        if not pos.get("alpaca_order_id"):
            continue
        try:
            order = trading_client.get_order_by_id(pos["alpaca_order_id"])
        except Exception:
            continue

        filled_leg = None
        for leg in (order.legs or []):
            if leg.status == OrderStatus.FILLED:
                filled_leg = leg
                break
        if filled_leg is None:
            continue

        exit_price = float(filled_leg.filled_avg_price) if filled_leg.filled_avg_price else None
        exit_time_raw = filled_leg.filled_at or datetime.now()
        exit_time = str(exit_time_raw)

        exit_reason = "target_hit" if filled_leg.order_type == OrderType.LIMIT else "stop_hit"

        direction_sign = 1.0 if pos["direction"] == "long" else -1.0
        pnl_dollar = (
            (exit_price - pos["fill_price"]) * pos["qty"] * direction_sign
            if exit_price is not None and pos.get("fill_price") is not None
            else None
        )

        held_seconds = None
        if pos.get("filled_at"):
            try:
                filled_at_dt = datetime.fromisoformat(pos["filled_at"])
                exit_time_dt = exit_time_raw if isinstance(exit_time_raw, datetime) else datetime.fromisoformat(exit_time)
                if exit_time_dt.tzinfo is not None and filled_at_dt.tzinfo is None:
                    exit_time_dt = exit_time_dt.replace(tzinfo=None)
                held_seconds = (exit_time_dt - filled_at_dt).total_seconds()
            except Exception:
                held_seconds = None

        valid = is_valid_trade(pnl_dollar, held_seconds)

        position_store.close_position(
            pos["id"], exit_reason=exit_reason, exit_price=exit_price, exit_time=exit_time,
            pnl_dollar=pnl_dollar, held_seconds=held_seconds, is_valid_trade=valid,
        )
        record_realized_pnl(pos["trading_date"], pnl_dollar)

        closed.append({
            "position_id": pos["id"], "ticker": pos["ticker"], "exit_reason": exit_reason,
            "pnl_dollar": pnl_dollar, "is_valid_trade": valid,
        })

    return closed


def audit_open_positions(trading_client: Optional[TradingClient] = None) -> list:
    """
    Final sanity check: every DB row still marked 'open' after
    check_bracket_exits() above should have a matching live Alpaca
    position. A DB row that's STILL 'open' with no matching Alpaca
    position (and no bracket leg fill detected) means something outside
    this system's normal exit paths happened — logged loudly, never
    silently corrected.
    """
    trading_client = trading_client or _client()
    alpaca_positions = {p.symbol: p for p in trading_client.get_all_positions()}

    mismatches = []
    for pos in position_store.list_open_positions():
        ticker = pos["ticker"]
        if ticker not in alpaca_positions:
            msg = f"DB shows OPEN position in {ticker} (id={pos['id']}) but Alpaca has no matching position. NEEDS MANUAL REVIEW."
            print(f"  ⚠️  {msg}")
            mismatches.append({"ticker": ticker, "position_id": pos["id"], "issue": "missing_at_alpaca"})
        else:
            alpaca_qty = abs(float(alpaca_positions[ticker].qty))
            if abs(alpaca_qty - pos["qty"]) > 0.01:
                msg = f"DB shows {pos['qty']} shares of {ticker}, Alpaca shows {alpaca_qty}. NEEDS MANUAL REVIEW."
                print(f"  ⚠️  {msg}")
                mismatches.append({"ticker": ticker, "position_id": pos["id"], "issue": "qty_mismatch",
                                     "db_qty": pos["qty"], "alpaca_qty": alpaca_qty})
    return mismatches

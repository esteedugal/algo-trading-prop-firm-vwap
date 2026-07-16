"""
Order Manager — Bracket Orders
---------------------------------
Places real equity orders on Alpaca for the ORB strategy, using
OrderClass.BRACKET (entry + take-profit + stop-loss submitted as one
atomic order, confirmed live to exist on Alpaca's order request models)
rather than managing three separate orders.

Entry uses a marketable limit order (same ~0.5% buffer pattern as the
momentum project's order_manager.py) to bound slippage on an unattended
job. The take-profit leg is a precise LIMIT at the target price (we want
the exact target if achievable). The stop-loss leg intentionally omits
its optional limit_price, making it a stop-MARKET once triggered — the
entire purpose of a stop is bounding risk, not squeezing exact price, so
guaranteed execution matters more than fill price there.

cancel_bracket() explicitly cancels every leg of a bracket group rather
than relying solely on Alpaca's documented single-cancel auto-behavior
("if any one of the orders is canceled, any remaining open order in the
group is canceled" — confirmed both via Alpaca's docs and a live
structural test in this project) — cheap defense-in-depth against
Alpaca's own documented fast-market edge case where both legs could fill
before a cancel propagates.
"""

from dotenv import load_dotenv
import os
load_dotenv('config/.env')

from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass, AssetStatus

from config.settings import ENTRY_LIMIT_BUFFER


def _client() -> TradingClient:
    return TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        paper=True,
    )


def is_tradable(ticker: str, direction: str, trading_client: Optional[TradingClient] = None) -> bool:
    """Fail-fast check before submitting. Short candidates additionally
    require the asset to be shortable — skip a single bad ticker rather
    than letting it crash the tick."""
    trading_client = trading_client or _client()
    try:
        asset = trading_client.get_asset(ticker)
        if not (asset.status == AssetStatus.ACTIVE and asset.tradable):
            return False
        if direction == "short" and not asset.shortable:
            return False
        return True
    except Exception:
        return False


def open_bracket_position(
    ticker: str,
    direction: str,
    qty: int,
    stop_price: float,
    target_price: float,
    last_price: float,
    client_order_id: str,
    trading_client: Optional[TradingClient] = None,
) -> dict:
    """
    Submit entry + take-profit + stop-loss as one atomic bracket order.
    direction: "long" -> BUY entry (SELL exit legs, inferred by Alpaca);
               "short" -> SELL entry (BUY exit legs, inferred by Alpaca).
    Returns {client_order_id, alpaca_order_id, status, raw}. Raises on
    submission failure — caller treats that as "skip this candidate."
    """
    trading_client = trading_client or _client()

    if direction == "long":
        side = OrderSide.BUY
        limit_price = round(last_price * ENTRY_LIMIT_BUFFER, 2)
    elif direction == "short":
        side = OrderSide.SELL
        limit_price = round(last_price * (2 - ENTRY_LIMIT_BUFFER), 2)
    else:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    # Signal-time target validation checks target_price against the raw
    # entry_trigger, not this marketable-limit price -- if price keeps
    # running between signal detection and submission (routine on a
    # breakout/resumption setup), the buffered limit_price can end up at
    # or past a target that looked valid when the setup was frozen. Alpaca
    # rejects that outright ("take_profit.limit_price must be >= base_price
    # + 0.01" / "<= base_price - 0.01") -- found live 2026-07-15, ~1/3 of
    # one day's setups on this project alone. Re-validate against the
    # ACTUAL price about to be submitted and skip cleanly rather than
    # burning a submission on a target price has already run past.
    take_profit_price = round(target_price, 2)
    if direction == "long" and take_profit_price < round(limit_price + 0.01, 2):
        raise ValueError(
            f"{ticker}: target {take_profit_price} no longer clears buffered entry "
            f"{limit_price} + \/bin/bash.01 -- price ran past the target before submission"
        )
    if direction == "short" and take_profit_price > round(limit_price - 0.01, 2):
        raise ValueError(
            f"{ticker}: target {take_profit_price} no longer clears buffered entry "
            f"{limit_price} - \/bin/bash.01 -- price ran past the target before submission"
        )

    order_request = LimitOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=take_profit_price),
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        client_order_id=client_order_id,
    )
    order = trading_client.submit_order(order_request)

    return {
        "client_order_id": client_order_id,
        "alpaca_order_id": str(order.id),
        "status": str(order.status),
        "raw": order,
    }


def close_position_market(
    ticker: str,
    qty: float,
    direction: str,
    client_order_id: str,
    trading_client: Optional[TradingClient] = None,
) -> dict:
    """
    Market order to flatten a held position — used for the EOD flatten
    deadline and hard-stop-breach force-closes, where guaranteed
    execution matters more than price (unlike entries, which use a
    marketable limit to bound slippage on a non-urgent order).
    """
    trading_client = trading_client or _client()
    side = OrderSide.SELL if direction == "long" else OrderSide.BUY

    order_request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )
    order = trading_client.submit_order(order_request)

    return {
        "client_order_id": client_order_id,
        "alpaca_order_id": str(order.id),
        "status": str(order.status),
        "raw": order,
    }


def cancel_bracket(alpaca_order_id: str, trading_client: Optional[TradingClient] = None) -> None:
    """Best-effort cancel of every leg in a bracket order group (parent +
    both exit legs). Swallows "already filled/canceled/not found" errors
    per order id — the goal is cleanup, not a hard failure if a leg
    already resolved on its own."""
    trading_client = trading_client or _client()
    try:
        order = trading_client.get_order_by_id(alpaca_order_id)
    except Exception:
        return

    all_ids = [str(order.id)] + [str(leg.id) for leg in (order.legs or [])]
    for oid in all_ids:
        try:
            trading_client.cancel_order_by_id(oid)
        except Exception:
            pass


def get_order_status(order_id: str, trading_client: Optional[TradingClient] = None):
    """Poll helper — returns the live Order object (status, legs, filled_qty, etc.)."""
    trading_client = trading_client or _client()
    return trading_client.get_order_by_id(order_id)

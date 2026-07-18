"""
VWAP Pullback / Reversion Signal Math
------------------------------------------
Pure functions, no I/O — mirrors the sibling ORB/EMA projects' signal
modules in shape and naming conventions (strict-inequality breakout
convention, stop/target frozen once computed, halt-proxy logic
unchanged since it was already strategy-agnostic).

Runs on 1-minute bars throughout (unlike the EMA sibling's 5-minute
bars) — VWAP is naturally a fine-granularity cumulative construct, and
1-min bars also match the cron tick cadence directly.

Target design note: agent.md offers "the prior high or low of the day,
OR a fixed R-multiple" as alternatives. Rather than picking one and
rejecting a setup where it doesn't work (the EMA sibling's approach),
compute_stop_target here tries the day-extreme target first and falls
back to a fixed R-multiple whenever the day-extreme is invalid or too
close — it never rejects a setup for target reasons, only for a
degenerate (non-positive) stop distance.
"""

from typing import Literal, Optional
import pandas as pd

Direction = Literal["long", "short", "none"]


def compute_vwap(bars: pd.DataFrame) -> pd.Series:
    """
    Standard intraday VWAP: cumulative(typical_price * volume) / cumulative(volume).
    CALLER MUST pass only today's bars, from market open onward, in
    ascending order — this is an expanding calculation with no explicit
    day-reset logic of its own; passing a multi-day window would blend
    VWAP across days, which is not a real intraday VWAP.
    """
    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    cum_pv = (typical_price * bars["volume"]).cumsum()
    cum_vol = bars["volume"].cumsum()
    return cum_pv / cum_vol


def compute_atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range: rolling mean of the true range."""
    high = bars["high"]
    low = bars["low"]
    prev_close = bars["close"].shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def classify_bias(price: float, vwap: float) -> Direction:
    if price > vwap:
        return "long"
    if price < vwap:
        return "short"
    return "none"


def bars_since_cross(price_series: pd.Series, vwap_series: pd.Series) -> int:
    """
    Number of consecutive trailing bars price has stayed on the same side
    of VWAP. Returns the full series length if consistent for the entire
    available window (no cross found in the data given).
    """
    diff = price_series - vwap_series
    sign = diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    n = len(sign)
    if n == 0:
        return 0
    current = sign.iloc[-1]
    count = 0
    for i in range(n - 1, -1, -1):
        if sign.iloc[i] != current:
            break
        count += 1
    return count


def compute_clean_bias(
    price_series: pd.Series,
    vwap_series: pd.Series,
    atr_series: pd.Series,
    min_bars_since_cross: int = 20,
    min_distance_atr_mult: float = 0.3,
) -> dict:
    """
    agent.md: "if price is chopping across VWAP repeatedly with no clear
    bias, stand aside." Two conditions, both required: (1) price has held
    its current side of VWAP for at least min_bars_since_cross bars, and
    (2) the distance between price and VWAP, scaled by ATR(14), exceeds
    min_distance_atr_mult (self-adjusts per name's own volatility). Both
    threshold defaults are genuinely arbitrary and flagged for the
    backtest to validate before going live.
    """
    price = price_series.iloc[-1]
    vwap = vwap_series.iloc[-1]
    atr = atr_series.iloc[-1]
    distance = abs(price - vwap)
    bsc = bars_since_cross(price_series, vwap_series)

    if atr is None or pd.isna(atr) or atr <= 0:
        return {"is_clean": False, "bars_since_cross": bsc, "distance": distance, "atr": atr, "distance_atr_ratio": None}

    distance_atr_ratio = distance / atr
    is_clean = bool(bsc >= min_bars_since_cross and distance_atr_ratio >= min_distance_atr_mult)
    return {
        "is_clean": is_clean,
        "bars_since_cross": bsc,
        "distance": distance,
        "atr": atr,
        "distance_atr_ratio": distance_atr_ratio,
    }


def detect_pullback_touch(bar: dict, vwap: float, direction: Direction) -> dict:
    """
    True if the bar's [low, high] range traded through VWAP (price
    pulled back into it) but the bar's CLOSE is still on the correct side
    of the prevailing bias — a legitimate "test and hold" pullback, not a
    break of bias (that's detect_bias_failure's job).
    """
    if direction not in ("long", "short"):
        return {"touched": False}
    low, high = bar["low"], bar["high"]
    close = bar.get("close", (low + high) / 2)
    range_touched = low <= vwap <= high
    held = (close >= vwap) if direction == "long" else (close <= vwap)
    return {"touched": bool(range_touched and held)}


def detect_bias_failure(bar_close: float, vwap: float, direction: Direction) -> bool:
    """
    True if the bar's CLOSE ends up on the WRONG side of VWAP — a genuine
    break of the prevailing bias, not just a touch/test. This is itself a
    signal to abandon the setup, not a non-event.
    """
    if direction == "long":
        return bar_close < vwap
    if direction == "short":
        return bar_close > vwap
    return False


def compute_swing_reference(
    bars: pd.DataFrame,
    kind: Literal["swing_low", "swing_high"],
    lookback_n: int = 20,
    floor_ts: Optional[pd.Timestamp] = None,
) -> dict:
    """
    Lowest low (kind='swing_low') or highest high (kind='swing_high') over
    a bounded window: min(lookback_n bars, bars since floor_ts). floor_ts
    should be the current bias's own start — a swing point from before
    the bias began isn't a real reference level for a stop.
    """
    window = bars
    if floor_ts is not None:
        window = window[window.index >= floor_ts]
    window = window.tail(lookback_n)

    if window.empty:
        raise ValueError("compute_swing_reference: no bars available in the bounded lookback window")

    if kind == "swing_low":
        idx = window["low"].idxmin()
        price = window.loc[idx, "low"]
    elif kind == "swing_high":
        idx = window["high"].idxmax()
        price = window.loc[idx, "high"]
    else:
        raise ValueError(f"kind must be 'swing_low' or 'swing_high', got {kind!r}")

    return {"price": float(price), "ts": idx}


def compute_day_extreme(bars_today: pd.DataFrame, kind: Literal["day_high", "day_low"]) -> dict:
    """Highest high / lowest low across ALL of today's bars so far — no
    lookback bound, the whole session. Used as the primary target
    reference (agent.md: "the prior high or low of the day")."""
    if bars_today.empty:
        raise ValueError("compute_day_extreme: no bars available for today")

    if kind == "day_high":
        idx = bars_today["high"].idxmax()
        price = bars_today.loc[idx, "high"]
    elif kind == "day_low":
        idx = bars_today["low"].idxmin()
        price = bars_today.loc[idx, "low"]
    else:
        raise ValueError(f"kind must be 'day_high' or 'day_low', got {kind!r}")

    return {"price": float(price), "ts": idx}


def compute_stop_target(
    entry_trigger: float,
    vwap_stop_price: float,
    swing_stop_price: float,
    day_extreme_price: float,
    direction: Direction,
    stop_buffer_pct: float = 0.0030,
    fixed_r_multiple: float = 2.0,
    min_target_risk_reward: float = 1.5,
) -> dict:
    """
    Stop = tighter-of(VWAP-based, swing-based) by distance from entry,
    pushed stop_buffer_pct further out. Target: try the day's prior
    high/low first (must be on the profitable side of entry AND clear
    min_target_risk_reward x the stop distance); if not, fall back to a
    fixed fixed_r_multiple x risk target — never rejects a setup outright
    for target reasons, unlike the EMA sibling's stricter reject-only
    approach (agent.md explicitly offers both as valid alternatives here).
    """
    if direction not in ("long", "short"):
        raise ValueError(f"compute_stop_target requires direction in ('long','short'), got {direction!r}")

    vwap_distance = abs(entry_trigger - vwap_stop_price)
    swing_distance = abs(entry_trigger - swing_stop_price)

    if swing_distance < vwap_distance:
        raw_stop, stop_basis = swing_stop_price, "swing"
    else:
        raw_stop, stop_basis = vwap_stop_price, "vwap"

    stop_price = raw_stop * (1 - stop_buffer_pct) if direction == "long" else raw_stop * (1 + stop_buffer_pct)

    risk_per_share = abs(entry_trigger - stop_price)
    if risk_per_share <= 0:
        raise ValueError(f"Non-positive risk_per_share (entry={entry_trigger}, stop={stop_price})")

    day_extreme_valid = (
        (direction == "long" and day_extreme_price > entry_trigger)
        or (direction == "short" and day_extreme_price < entry_trigger)
    )
    day_extreme_reward = abs(day_extreme_price - entry_trigger) if day_extreme_valid else 0.0

    if day_extreme_valid and day_extreme_reward >= min_target_risk_reward * risk_per_share:
        target_price, target_basis = day_extreme_price, "day_extreme"
    else:
        target_price = (
            entry_trigger + fixed_r_multiple * risk_per_share if direction == "long"
            else entry_trigger - fixed_r_multiple * risk_per_share
        )
        target_basis = "fixed_r"

    return {
        "entry_trigger": entry_trigger,
        "stop_price": stop_price,
        "stop_basis": stop_basis,
        "target_price": target_price,
        "target_basis": target_basis,
        "risk_per_share": risk_per_share,
    }


def detect_breakout(close_price: float, entry_trigger: float, direction: Direction) -> bool:
    """Strict inequality — same convention as the ORB/EMA siblings'
    detect_breakout. Fed a 1-minute bar's CLOSE, matching this project's
    single-granularity (1-min throughout) design."""
    if direction == "long":
        return close_price > entry_trigger
    if direction == "short":
        return close_price < entry_trigger
    return False


def detect_halt_proxy(bars_trailing, move_pct_threshold: float = 0.10) -> dict:
    """Copy-pasted unchanged from the sibling projects' signal modules —
    no strategy-specific logic in it. No halt-status API exists anywhere
    in Alpaca's SDK; this is a self-computed >=10%-move-in-5-minutes
    proxy, with actual exchange-level order rejection as the real backstop."""
    first_open = float(bars_trailing["open"].iloc[0])
    last_close = float(bars_trailing["close"].iloc[-1])
    move_pct = (last_close - first_open) / first_open
    return {
        "is_halt_proxy": abs(move_pct) >= move_pct_threshold,
        "move_pct": move_pct,
    }

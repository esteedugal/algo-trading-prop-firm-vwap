"""
Close-Confirmation Stop — Shadow Analysis
--------------------------------------------
Not a live trading filter. This is a standing research tool: it replays every
stop_hit trade against an alternative stop-exit rule -- exit only when a bar
CLOSES beyond the stop level, instead of the instant price touches it
intrabar (the live behavior, a broker-side stop-market order) -- and compares
the two.

The idea comes from Kristjan Qullamaggie's moving-average trailing-stop
discipline (allow intraday violations, only exit on a confirmed close) --
see the 2026-07-18 conversation. Vwap's own decision timeframe is already
1-minute bars, so "close-confirm" here means a 1-min bar close, matching the
strategy's own native granularity (unlike Ema, whose 5-min trend calc and
1-min stop-checking are different granularities -- tested there too and it
did NOT help, in either the 5-min or 1-min close-confirm variant).

Why this is safe to run purely retroactively: the exit rule only depends on
the historical bar sequence strictly after entry (no data at or before entry
is used, and nothing at or after the actual exit point is used) -- there is
no lookahead, so replaying it after the fact is equivalent to computing it
live.

First run (2026-07-15 to 2026-07-17, 52 stop_hit trades):
  touch (current live):  win_rate=3.8%  total_pnl=-$1,757.12
  close-confirm:         win_rate=13.5% total_pnl=-$1,333.52
Day-by-day: better or roughly flat on all 3 days, no inversions (7/15 a wash
at -$58, 7/16 and 7/17 both clearly better) -- a materially more robust
pattern than the anchored-VWAP confluence result, which fully inverted on
one of its three days. Promising, but 3 days is still a small sample --
re-run this periodically as more real trades accumulate before considering
it for a live change.

Usage:
  venv/bin/python3 backtest/close_confirm_stop_shadow.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import sqlite3
from datetime import datetime
import pytz

from tools.market_data.intraday_bars import get_bars
from tools.trading.position_store import DB_PATH
from config.settings import FLATTEN_MINUTES_BEFORE_CLOSE

ET = pytz.timezone("America/New_York")


def fetch_stop_hit_trades(db_path: str = DB_PATH) -> list:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        select trading_date, ticker, direction, qty, fill_price, stop_price, target_price, filled_at
        from positions where exit_reason='stop_hit' order by trading_date, id
    """)
    rows = cur.fetchall()
    con.close()
    return rows


def fetch_bars_cache(trades: list) -> dict:
    unique_days = sorted(set((t[0], t[1]) for t in trades))
    cache = {}
    for date_str, ticker in unique_days:
        day = datetime.strptime(date_str, "%Y-%m-%d")
        start_et = ET.localize(day.replace(hour=9, minute=25))
        end_et = ET.localize(day.replace(hour=16, minute=0))
        result = get_bars([ticker], start_et.astimezone(pytz.utc), end_et.astimezone(pytz.utc))
        cache[(date_str, ticker)] = result.get(ticker)
    return cache


def simulate(trade: tuple, bars_cache: dict, mode: str) -> dict | None:
    trading_date, ticker, direction, qty, fill_price, stop_price, target_price, filled_at = trade
    bars = bars_cache.get((trading_date, ticker))
    if bars is None or bars.empty:
        return None

    filled_dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
    bars_after = bars[bars.index > filled_dt]
    flatten_hour_min = _flatten_cutoff_hm()
    flatten_cutoff = ET.localize(
        datetime.strptime(trading_date, "%Y-%m-%d").replace(hour=flatten_hour_min[0], minute=flatten_hour_min[1])
    ).astimezone(pytz.utc)

    direction_sign = 1.0 if direction == "long" else -1.0
    exit_price, reason = None, None
    for ts, row in bars_after.iterrows():
        if ts >= flatten_cutoff:
            exit_price, reason = row["close"], "eod_flatten"
            break
        if direction == "long":
            if row["high"] >= target_price:
                exit_price, reason = target_price, "target_hit"
                break
            triggered = row["low"] <= stop_price if mode == "touch" else row["close"] <= stop_price
            if triggered:
                exit_price, reason = (stop_price if mode == "touch" else row["close"]), "stop_hit"
                break
        else:
            if row["low"] <= target_price:
                exit_price, reason = target_price, "target_hit"
                break
            triggered = row["high"] >= stop_price if mode == "touch" else row["close"] >= stop_price
            if triggered:
                exit_price, reason = (stop_price if mode == "touch" else row["close"]), "stop_hit"
                break

    if exit_price is None:
        exit_price = bars_after["close"].iloc[-1] if not bars_after.empty else fill_price
        reason = "eod_flatten_nodata"

    pnl = (exit_price - fill_price) * qty * direction_sign
    return {"date": trading_date, "ticker": ticker, "reason": reason, "pnl": pnl, "win": pnl > 0}


def _flatten_cutoff_hm() -> tuple:
    # 4:00pm ET close, minus FLATTEN_MINUTES_BEFORE_CLOSE
    total_minutes = 16 * 60 - FLATTEN_MINUTES_BEFORE_CLOSE
    return (total_minutes // 60, total_minutes % 60)


def summarize(group: list, label: str) -> None:
    if not group:
        print(f"  {label}: n=0")
        return
    wins = sum(r["win"] for r in group)
    total_pnl = sum(r["pnl"] for r in group)
    print(f"  {label}: n={len(group)}  win_rate={wins/len(group)*100:.1f}%  total_pnl=${total_pnl:.2f}")


def main():
    trades = fetch_stop_hit_trades()
    print(f"Loaded {len(trades)} stop_hit trades from {DB_PATH}\n")

    bars_cache = fetch_bars_cache(trades)

    all_results = {}
    for mode in ("touch", "close"):
        results = [simulate(t, bars_cache, mode) for t in trades]
        all_results[mode] = [r for r in results if r is not None]

    print("=== Aggregate ===")
    summarize(all_results["touch"], "touch (current live)")
    summarize(all_results["close"], "close-confirm    ")

    print("\n=== Day-by-day ===")
    for date in sorted(set(r["date"] for r in all_results["touch"])):
        print(f"{date}:")
        summarize([r for r in all_results["touch"] if r["date"] == date], "touch")
        summarize([r for r in all_results["close"] if r["date"] == date], "close")


if __name__ == "__main__":
    main()

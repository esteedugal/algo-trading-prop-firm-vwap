"""
Anchored VWAP Confluence — Shadow Analysis
--------------------------------------------
Not a live trading filter. This is a standing research tool: it replays every
closed trade against a rolling multi-day ("anchored") VWAP and checks whether
that anchored VWAP agreed with the trade's actual direction at entry time.

Why this is safe to run purely retroactively (no real-time hook needed): VWAP
is a function of historical price/volume only, computed strictly up through
the entry timestamp (no bars at or after entry are used) -- there is no
lookahead, so replaying it after the fact is equivalent to computing it live.

Origin: 2026-07-18 conversation. First run (3 days, 72 trades) found a large
aggregate edge (confluence trades: 33% win rate, +$192; disagreement trades:
13% win rate, -$375) that was robust across anchor window lengths (2-15 days)
but NOT robust day-to-day -- the effect fully inverted on 2026-07-17, the one
day that was broadly bad for every intraday trader in the family. Not enough
evidence yet to gate live entries on this. Re-run this script periodically
(e.g. weekly) as more real trading days accumulate, and watch whether 7/17-style
inversions keep recurring or turn out to have been a one-off.

Usage:
  venv/bin/python3 backtest/anchored_vwap_shadow.py                 # default 10-day anchor
  venv/bin/python3 backtest/anchored_vwap_shadow.py --anchor-days 6  # override anchor window
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import argparse
import sqlite3
from datetime import datetime, timedelta
import pytz

from tools.market_data.intraday_bars import get_bars
from tools.trading.position_store import DB_PATH

ET = pytz.timezone("America/New_York")


def fetch_closed_trades(db_path: str = DB_PATH) -> list:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        select trading_date, ticker, direction, fill_price, filled_at, pnl_dollar
        from positions where status='closed' order by trading_date, filled_at
    """)
    rows = cur.fetchall()
    con.close()
    return rows


def fetch_bars_cache(trades: list, anchor_days: int) -> dict:
    unique_days = sorted(set((t[0], t[1]) for t in trades))
    cache = {}
    for date_str, ticker in unique_days:
        day = datetime.strptime(date_str, "%Y-%m-%d")
        start = (ET.localize(day.replace(hour=9, minute=25)) - timedelta(days=anchor_days + 1)).astimezone(pytz.utc)
        end = ET.localize(day.replace(hour=16, minute=0)).astimezone(pytz.utc)
        result = get_bars([ticker], start, end)
        cache[(date_str, ticker)] = result.get(ticker)
    return cache


def analyze_trade(trade: tuple, bars_cache: dict, anchor_days: int) -> dict | None:
    trading_date, ticker, direction, fill_price, filled_at, pnl = trade
    bars = bars_cache.get((trading_date, ticker))
    if bars is None or bars.empty:
        return None

    filled_dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
    anchor_start = (
        ET.localize(datetime.strptime(trading_date, "%Y-%m-%d").replace(hour=9, minute=30))
        - timedelta(days=anchor_days)
    ).astimezone(pytz.utc)

    # strictly prior bars only -- no lookahead
    window = bars[(bars.index < filled_dt) & (bars.index >= anchor_start)]
    if window.empty:
        return None

    anchored_vwap = (window["close"] * window["volume"]).sum() / window["volume"].sum()
    anchored_bias = "long" if fill_price > anchored_vwap else "short"

    return {
        "date": trading_date,
        "ticker": ticker,
        "direction": direction,
        "anchored_vwap": anchored_vwap,
        "confluence": anchored_bias == direction,
        "pnl": pnl,
        "win": pnl > 0,
    }


def summarize(group: list, label: str) -> None:
    if not group:
        print(f"  {label}: n=0")
        return
    wins = sum(r["win"] for r in group)
    total_pnl = sum(r["pnl"] for r in group)
    print(f"  {label}: n={len(group)}  win_rate={wins/len(group)*100:.1f}%  "
          f"total_pnl=${total_pnl:.2f}  avg_pnl=${total_pnl/len(group):.2f}")


def main():
    parser = argparse.ArgumentParser(description="Anchored VWAP confluence shadow analysis")
    parser.add_argument("--anchor-days", type=int, default=10,
                         help="Calendar days back the anchored VWAP window covers (default 10)")
    args = parser.parse_args()

    trades = fetch_closed_trades()
    print(f"Loaded {len(trades)} closed trades from {DB_PATH}\n")

    bars_cache = fetch_bars_cache(trades, args.anchor_days)
    results = [analyze_trade(t, bars_cache, args.anchor_days) for t in trades]
    results = [r for r in results if r is not None]
    print(f"Analyzed {len(results)} trades (anchor window = {args.anchor_days} calendar days)\n")

    print("=== Day-by-day breakdown ===")
    for date in sorted(set(r["date"] for r in results)):
        day_results = [r for r in results if r["date"] == date]
        print(f"{date}:")
        summarize([r for r in day_results if r["confluence"]], "confluence")
        summarize([r for r in day_results if not r["confluence"]], "disagree  ")

    print("\n=== Aggregate ===")
    summarize([r for r in results if r["confluence"]], "confluence")
    summarize([r for r in results if not r["confluence"]], "disagree  ")


if __name__ == "__main__":
    main()

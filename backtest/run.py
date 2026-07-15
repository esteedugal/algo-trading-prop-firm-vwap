"""
Backtest Runner
------------------
Fetches historical 1-min bars for the universe over a date range and
runs backtest/engine.py, reporting results. Primary purpose: validate
CLEAN_BIAS_MIN_BARS_SINCE_CROSS and CLEAN_BIAS_MIN_DISTANCE_ATR_MULT
(the two genuinely arbitrary defaults in config/settings.py) before
going live — not to produce a headline return figure.

Usage:
  python backtest/run.py --days 20
  python backtest/run.py --days 20 --sweep   # also compares alternate threshold values
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import argparse
from datetime import datetime, timedelta
import pytz
from alpaca.data.timeframe import TimeFrame

from config.universe import UNIVERSE
from config.settings import CLEAN_BIAS_MIN_BARS_SINCE_CROSS, CLEAN_BIAS_MIN_DISTANCE_ATR_MULT
from tools.market_data.intraday_bars import get_bars
from backtest.engine import run_backtest

ET = pytz.timezone("America/New_York")


def fetch_universe_bars(days: int) -> dict:
    end = datetime.now(ET)
    start = end - timedelta(days=days)
    print(f"Fetching {days} days of 1-min bars for {len(UNIVERSE)} tickers...")
    bars_by_ticker = get_bars(UNIVERSE, start.astimezone(pytz.utc), end.astimezone(pytz.utc), timeframe=TimeFrame.Minute)
    print(f"Got data for {len(bars_by_ticker)} tickers\n")
    return bars_by_ticker


def summarize(result: dict, label: str) -> None:
    trades = result["trades"]
    total_pnl = result["final_pnl"]
    wins = [t for t in trades if t["pnl_dollar"] > 0]
    losses = [t for t in trades if t["pnl_dollar"] <= 0]

    print(f"=== {label} ===")
    if trades:
        print(f"Trades: {len(trades)}  Wins: {len(wins)}  Losses: {len(losses)}  "
              f"Win rate: {len(wins) / len(trades) * 100:.1f}%")
        avg_win = sum(t["pnl_dollar"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t["pnl_dollar"] for t in losses) / len(losses) if losses else 0.0
        print(f"Avg win: ${avg_win:,.2f}  Avg loss: ${avg_loss:,.2f}")
        reasons: dict = {}
        for t in trades:
            reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        print(f"Exit reasons: {reasons}")
    else:
        print("Trades: 0")
    print(f"Total P&L: ${total_pnl:,.2f}\n")


def main():
    parser = argparse.ArgumentParser(description="VWAP Pullback/Reversion Backtest")
    parser.add_argument("--days", type=int, default=20, help="Calendar days of history to backtest")
    parser.add_argument("--sweep", action="store_true", help="Also compare alternate clean-bias thresholds")
    args = parser.parse_args()

    bars_by_ticker = fetch_universe_bars(args.days)

    baseline = run_backtest(bars_by_ticker)
    summarize(baseline, f"BASELINE (min_bars_since_cross={CLEAN_BIAS_MIN_BARS_SINCE_CROSS}, "
                         f"min_distance_atr_mult={CLEAN_BIAS_MIN_DISTANCE_ATR_MULT})")

    if args.sweep:
        looser = run_backtest(bars_by_ticker, min_bars_since_cross=10, min_distance_atr_mult=0.15)
        summarize(looser, "LOOSER (min_bars_since_cross=10, min_distance_atr_mult=0.15)")

        stricter = run_backtest(bars_by_ticker, min_bars_since_cross=30, min_distance_atr_mult=0.5)
        summarize(stricter, "STRICTER (min_bars_since_cross=30, min_distance_atr_mult=0.5)")


if __name__ == "__main__":
    main()

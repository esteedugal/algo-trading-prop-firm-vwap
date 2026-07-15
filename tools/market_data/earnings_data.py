"""
Earnings Data Tool
-------------------
Ported near-verbatim from algo-trading/tools/market_data/earnings_data.py
(the original options-trading project). Here it's used ONLY as a tie-
breaker annotation on the screener's already-volume-qualified shortlist
(screen_stocks_in_play's top N) — never a hard gate, and never run
against the full ~100-ticker base universe (yfinance latency makes that
impractical inside the 9:31-9:34 ET screening window).
"""

import pandas as pd
import yfinance as yf
import logging

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

KNOWN_ETFS = {
    'SPY', 'QQQ', 'IWM', 'DIA', 'GLD', 'SLV', 'TLT', 'HYG', 'LQD',
    'XLF', 'XLK', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB',
    'VTI', 'VOO', 'VEA', 'VWO', 'EFA', 'EEM', 'AGG', 'BND',
}


def get_earnings_proximity(ticker: str) -> dict:
    """Returns dict with days_to_earnings, has_earnings_soon, earnings_date."""
    if ticker.upper() in KNOWN_ETFS:
        return {"ticker": ticker, "earnings_date": None, "days_to_earnings": None,
                "has_earnings_soon": False, "note": "N/A - ETF"}

    try:
        stock = yf.Ticker(ticker)
        calendar = stock.calendar

        if calendar is None or not isinstance(calendar, dict) or not calendar.get('Earnings Date'):
            return {"ticker": ticker, "earnings_date": None, "days_to_earnings": None,
                     "has_earnings_soon": False, "note": "No earnings date available"}

        earnings_date = pd.Timestamp(calendar['Earnings Date'][0])
        today = pd.Timestamp.now()
        days_to = (earnings_date - today).days

        return {
            "ticker": ticker,
            "earnings_date": earnings_date.date(),
            "days_to_earnings": days_to,
            "has_earnings_soon": 0 <= days_to <= 21,
            "note": f"Earnings in {days_to} days",
        }
    except Exception as e:
        return {"ticker": ticker, "earnings_date": None, "days_to_earnings": None,
                 "has_earnings_soon": False, "note": f"Error: {str(e)}"}


def is_earnings_today(ticker: str) -> bool:
    info = get_earnings_proximity(ticker)
    return info.get("days_to_earnings") == 0

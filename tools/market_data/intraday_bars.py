"""
Intraday Bars
---------------
Thin wrapper around Alpaca's bar/latest-trade endpoints. Adapted from the
momentum project's price_data.py's hard-won lesson:

  feed=DataFeed.IEX — this account only has free-tier IEX data access,
  not the paid SIP consolidated feed. Omitting this causes a 403 on
  every request. (No split-adjustment param here, unlike momentum —
  positions in this project never span a trading day, so a mid-day split
  is a non-issue this project doesn't need to defend against.)

Two timeframes are used deliberately for different purposes:
  - 1-minute bars for TODAY's live opening-range capture and breakout
    monitoring (fine granularity needed).
  - 5-minute bars for the screener's HISTORICAL relative-volume baseline
    (see screener.py) — one batched request per universe slice returns
    the whole trailing lookback window's bars in one call; the screener
    then picks out just the bar whose start time is exactly 9:30 ET each
    day (the opening 5-minute bar), rather than pulling full days of
    1-minute historical bars just to reconstruct that same number at 5x
    the data cost.

Requests are batched across a symbol list (not one call per ticker) —
this is what keeps a ~100-ticker base universe scan fast enough for the
9:31-9:34 ET screening window.
"""

from dotenv import load_dotenv
import os
load_dotenv('config/.env')

from datetime import datetime
from typing import Optional
import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed


def _client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
    )


def _split_multi_index(df: pd.DataFrame, tickers) -> dict:
    if df.empty:
        return {}
    result = {}
    if isinstance(df.index, pd.MultiIndex):
        for ticker in df.index.get_level_values('symbol').unique():
            sub = df.xs(ticker, level='symbol')[['open', 'high', 'low', 'close', 'volume']].copy()
            sub.index.name = 'timestamp'
            result[ticker] = sub
    else:
        ticker = tickers[0] if isinstance(tickers, list) else tickers
        sub = df[['open', 'high', 'low', 'close', 'volume']].copy()
        sub.index.name = 'timestamp'
        result[ticker] = sub
    return result


def get_bars(
    tickers: list,
    start: datetime,
    end: datetime,
    timeframe: TimeFrame = TimeFrame.Minute,
    data_client: Optional[StockHistoricalDataClient] = None,
) -> dict:
    """
    Returns {ticker: DataFrame[open, high, low, close, volume]}, indexed
    by UTC timestamp. Tickers with zero bars in the window are simply
    absent from the dict, not an error (e.g. a name that didn't trade
    yet, or a bad/delisted symbol).
    """
    data_client = data_client or _client()
    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(request)
    return _split_multi_index(bars.df, tickers)


def get_latest_price(ticker: str, data_client: Optional[StockHistoricalDataClient] = None) -> float:
    data_client = data_client or _client()
    trade = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
    return float(trade[ticker].price)


def get_latest_prices(tickers: list, data_client: Optional[StockHistoricalDataClient] = None) -> dict:
    """Batched latest-trade lookup — used by the breakout scan each tick
    to avoid one request per watched candidate."""
    data_client = data_client or _client()
    trades = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=tickers))
    return {ticker: float(t.price) for ticker, t in trades.items()}


FIVE_MIN_BAR = TimeFrame(5, TimeFrameUnit.Minute)

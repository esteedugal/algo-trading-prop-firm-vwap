"""
ORB Base Universe
-------------------
Hardcoded, git-versioned candidate list that the daily screener
(tools/market_data/screener.py) ranks by relative volume each morning to
find "stocks in play." Deliberately NOT the momentum project's blue-chip
list — ORB's edge specifically needs names that can plausibly have an
abnormal-activity day (earnings surprise, news, retail-flow spikes), so
this skews toward higher-beta, higher-optionable-volume names rather than
low-volatility mega-caps that rarely move enough to feed a 10:1 R:R
breakout. Capped at a size a handful of batched StockBarsRequest calls
can scan well inside the 9:30-9:35 screening window — this is NOT meant
to approximate the paper's full 7,000+ stock universe, just a liquid,
volatility-prone shortlist to draw daily candidates from.

Needs occasional manual review (delistings, M&A, IPO additions) — same
discipline as the momentum project's universe, not automated.
"""

UNIVERSE = [
    # Mega-cap tech — high options volume, earnings-day movers
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "AMD", "CRM",
    "NFLX", "ORCL", "ADBE", "CSCO", "QCOM", "INTC", "MU", "PLTR", "SMCI", "PANW",

    # High-beta / retail-favorite names — frequent large intraday ranges
    "COIN", "MSTR", "RIVN", "LCID", "SOFI", "AFRM", "UPST", "HOOD", "DKNG", "RBLX",
    "SNAP", "PINS", "ROKU", "SHOP", "NET", "CRWD", "DDOG", "ZS", "MDB", "SNOW",

    # Financials — earnings/rate-sensitive movers
    "JPM", "BAC", "GS", "MS", "C", "WFC", "SCHW", "COF", "AXP", "PYPL",

    # Healthcare / biotech — binary-event volatility (trial readouts, FDA)
    "UNH", "LLY", "PFE", "MRNA", "BNTX", "GILD", "REGN", "VRTX", "BIIB", "ISRG",

    # Energy — macro/commodity-driven swings
    "XOM", "CVX", "OXY", "SLB", "DVN", "FANG", "MRO", "HAL",

    # Consumer discretionary / industrials — earnings movers
    "HD", "NKE", "MCD", "SBUX", "DIS", "UBER", "ABNB", "CAT", "BA", "GE", "DE", "F", "GM",

    # Semiconductors / hardware — cyclical, news-sensitive
    "TSM", "ASML", "LRCX", "KLAC", "ON", "MRVL", "ARM",

    # Chinese ADRs — frequently among the most volatile large-caps
    "BABA", "JD", "PDD", "NIO", "XPEV", "LI", "BIDU",

    # Media / telecom
    "T", "VZ", "TMUS", "WBD", "PARA",

    # Materials / industrials with commodity exposure
    "FCX", "NEM", "AA", "X",

    # Broad index / sector ETFs — liquid, sometimes still show relative-volume
    # spikes on macro days (CPI, FOMC); kept small since ETFs rarely have the
    # single-name "news gap" character the paper's edge depends on
    "SPY", "QQQ", "IWM",
]

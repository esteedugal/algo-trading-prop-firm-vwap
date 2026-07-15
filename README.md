# algo-trading-prop-firm-vwap

A VWAP Pullback/Reversion day-trading strategy, operated under the same simulated prop-firm compliance rulebook as [`algo-trading-prop-firm`](https://github.com/esteedugal/algo-trading-prop-firm) (ORB) and [`algo-trading-prop-firm-ema`](https://github.com/esteedugal/algo-trading-prop-firm-ema) (9/20 EMA) — same $25k tier, same daily-loss/drawdown/consistency/volume rules, run against its own separate Alpaca paper account so all three intraday signals can be compared head-to-head under identical constraints. The third and closest-yet variant of an already-built sibling: the entire compliance layer is ported byte-for-byte from the EMA project (which itself ported it from ORB), and the architecture (continuous intraday state tracking, multiple setups per ticker per day) directly reuses the pattern validated there.

## Strategy

1. **Bias**: price relative to the day's VWAP (volume-weighted average price, recomputed fresh each trading day from that day's 1-minute bars). Above VWAP favors longs, below favors shorts.
2. **Clean-bias filter**: price must have held the same side of VWAP for at least 20 consecutive 1-min bars AND the distance from VWAP must be at least 0.3× ATR(14) — self-scales per name's own volatility. Stand aside on choppy tape where price whipsaws across VWAP.
3. **Entry**: wait for a pullback where price trades through VWAP but the bar's close holds the correct side (a "test and hold," not a break), then enter on a close beyond the high (long) or low (short) of that pullback bar.
4. **Stop**: the tighter of (just beyond VWAP) or (the nearest swing low/high within the current bias's own lifetime).
5. **Target**: tries the prior high/low of the day first (agent.md's first-listed option); if that's invalid (wrong side, already exceeded) or doesn't clear a 1.5× minimum reward:risk, falls back to a fixed 2R target (agent.md's stated alternative) rather than skipping the setup outright — this project's target logic never rejects a setup for target reasons the way the EMA sibling's stricter approach does, since a real fallback is always available here.
6. **Multiple setups per ticker per day**: same continuous-tracking architecture as the EMA sibling — a ticker can establish bias, pull back, resume, close, and set up again later the same session.
7. **Exit**: target or stop, whichever hits first; flatten 10 minutes before close regardless.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config/.env.example config/.env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY / ANTHROPIC_API_KEY
```

## Usage

```bash
venv/bin/python agents/orchestrator/tick.py                 # one tick (also what cron invokes every trading minute)
venv/bin/python agents/orchestrator/eod_report.py            # today's EOD report
venv/bin/python backtest/run.py --days 20                     # backtest, validates the clean-bias thresholds
venv/bin/python backtest/run.py --days 20 --sweep             # + compares looser/stricter threshold alternatives

venv/bin/python tests/test_vwap_trend_signal.py   # signal math regression tests
venv/bin/python tests/test_rules_engine.py        # compliance engine tests (ported verbatim, should pass unchanged)
```

## What's ported verbatim from the sibling projects (zero changes)

`tools/trading/rules_engine.py`, `order_manager.py`, `agents/orchestrator/reconcile.py`, `tools/market_data/intraday_bars.py`, `earnings_data.py`, `config/universe.py`, and the `account_state`/`daily_state`/`positions`/`breaches`/`eod_reports` tables (plus all their CRUD) in `position_store.py`. Same $25k tier, same $500 daily loss limit / $1,500 max drawdown with the same one-way 3× ratchet.

## Design notes

- **1-minute bars throughout** — unlike the EMA sibling's 5-minute bars, this project runs on 1-min bars for everything (VWAP calculation, bias tracking, pullback/breakout detection). VWAP is naturally a fine-granularity cumulative construct (most platforms compute it from 1-min or finer), and 1-min bars match the cron tick cadence directly — there's no separate "once per N minutes" update step like the EMA sibling's step 7; VWAP-bias state updates every tick from the same bar fetch used for breakout scanning.
- **VWAP and ATR reset fresh each trading day** — both are recomputed from only that day's bars (matching how `tick.py` only ever fetches bars from that day's market open onward), never blended across days. This gives each new day a ~14-bar ATR warm-up, same as live; the backtest engine replicates this exactly by segmenting bars per trading day rather than running one continuous multi-day calculation (unlike the EMA sibling's continuous EMA/ATR series).
- **Target logic learned directly from a real bug found in the EMA sibling**: that project's first backtest run produced an implausible 68.9% win rate because its target lookback was bounded the same way as its stop's (correct for a stop, wrong for a target — collapses to near-entry early in a fresh trend). This project's target was designed correctly from the start: `compute_day_extreme` always searches the WHOLE trading day so far, never bounded by when the current bias began, and the fixed-R fallback (rather than a hard reject) means a setup is never discarded just because the day's prior high/low happens to be too close. The first backtest run here came back with a believable 30.9% win rate and a realistic 4.4:1 win/loss ratio on the first try — no equivalent bug found.
- **Entry trigger and stop/target are frozen once computed per pullback bar**, refreshed only while still actively pulling back (mirrors both siblings' "measure, refresh while forming, freeze on trigger" discipline) — no race with an eventual fill price.
- **`_flatten_all()` and the whole force-close path are ported unchanged**, including every fix found live in the ORB sibling on its first trading day: the settle-delay before flattening, the retry-vs-still-pending distinction, and never marking a position closed without a confirmed fill.

## Backtest — built for v1, with real caveats

Same posture as the EMA sibling: this strategy has no daily selection step with a hindsight-bias problem (fixed universe, same as live), and every signal function is pure over bar data, so `backtest/engine.py` replays them bar-by-bar with no look-ahead, reusing `rules_engine.py`'s sizing/loss-limit/drawdown functions directly.

**Known simplifications**: fill price is assumed to be exactly the entry trigger (no slippage); a same-bar stop-and-target overlap assumes the stop hit first (conservative); the 5% volume cap uses the 1-min bar's own volume directly (no scaling needed here, since bars already are 1-minute).

**A 20-day run produced**: 340 trades, 30.9% win rate, 4.4:1 average win/loss ratio ($84.05 avg win vs $18.91 avg loss) — a believable trend-continuation profile (cut losses fast, let winners run to distant day-extreme targets), not the "too good to be true" profile the EMA sibling's first (buggy) run showed. Day-to-day results were realistically mixed: 10 of 13 trading days profitable, 3 losing days — a healthier signal than a suspiciously perfect record would be. **However, P&L was notably concentrated**: the top 10 of 104 tickers accounted for 89.4% of total profit (AMD alone was ~21%), a meaningfully higher concentration than the EMA sibling's ~40%. This suggests the edge in this window may be more idiosyncratic to a handful of momentum/tech names (AMD, AMZN, NKE, MU, TSLA, ADBE, AAPL, UBER, MSFT, RIVN) than a broad, reliable cross-sectional edge — worth watching in live paper trading rather than assuming it generalizes. Treat this backtest as confirmation that the mechanics behave sensibly, not as a forecast — the real test is forward paper-trading, same as every sibling project's actual validation method.

## Known limitations

- **IEX-only volume** (same free-tier limitation as every sibling project) — the 5%-of-volume cap is conservative relative to true market liquidity.
- **No halt-status API** — the same self-computed >10%-move-in-5-minutes proxy as the sibling projects, with exchange-level order rejection as the real backstop.
- **~60-second breakout-detection lag**, inherent to 1-minute cron cadence.
- **Consistency rule has no pre-trade lever** — same as both siblings, a fixed target means there's no mechanism to cap a single winner's share of total profit before the fact; only monitored and self-reported after.
- **Two genuinely arbitrary thresholds** (`CLEAN_BIAS_MIN_BARS_SINCE_CROSS=20`, `CLEAN_BIAS_MIN_DISTANCE_ATR_MULT=0.3`) — validated by the backtest above only in the loose sense of "producing a sane trade profile," not tuned/optimized. `backtest/run.py --sweep` compares looser and stricter alternatives if that comparison becomes useful later.
- **Backtest profit concentration** (see above) — the 20-day sample's returns leaned heavily on a handful of tickers; worth re-checking with a longer window or after some live paper-trading days accumulate.

"""
VWAP Pullback / Reversion Strategy Parameters
----------------------------------------------------
Pullback-to-VWAP-and-resumption entries on a fixed universe, operated
under the SAME simulated prop-firm compliance rulebook as the sibling
`algo-trading-prop-firm` (ORB) and `algo-trading-prop-firm-ema` (9/20
EMA) projects — same $25k tier, same daily-loss/drawdown/consistency/
volume rules. Only the signal mechanics below differ from those siblings.

Every $ threshold below is a literal module constant, not read from env
with a fallback default — there is deliberately no silent-default path.
These numbers are the entire basis of position sizing and the hard-stop
compliance checks; guessing here is the one mistake this file can't afford.

IMPORTANT: the Alpaca paper account backing this project has real equity/
buying power far larger than the tier below (standard Alpaca paper
default) — that is infrastructure only, so a real order is never blocked
by Alpaca's own buying power. The account this strategy is actually
managed against is a SIMULATED $25k prop-firm tier, tracked entirely in
our own `rules_engine` state. Every compliance check uses TIER_* below,
never the real Alpaca account's actual equity or buying power.
"""

# ── Simulated prop-firm account tier (same as the ORB/EMA siblings) ────
TIER_INITIAL_BALANCE = 25_000.0
TIER_INTRADAY_BUYING_POWER = 25_000.0
TIER_OVERNIGHT_BUYING_POWER = 4_000.0

# ── Daily Loss Limit / Max Drawdown (same $25k-tier figures as the ORB/
#    EMA siblings -- agent.md gives rule structure only, not $ figures) ─
DAILY_LOSS_LIMIT_DOLLARS = 500.0
MAX_DRAWDOWN_DOLLARS = 1_500.0
DRAWDOWN_RATCHET_MULTIPLE = 3.0   # equity >= initial + 3x daily loss limit -> floor locks at initial

# ── Position sizing ─────────────────────────────────────────────────────
RISK_PER_TRADE_PCT = 0.30      # of DAILY_LOSS_LIMIT_DOLLARS, per agent.md's literal formula
VOLUME_CAP_PCT = 0.05          # new/added position <= this fraction of prior 1-min volume

# ── Trade validity / consistency rule ───────────────────────────────────
CONSISTENCY_MAX_PROFIT_SHARE = 0.30   # no single position > this share of total profit
MIN_VALID_PROFIT_CENTS = 10.0
MIN_VALID_HOLD_SECONDS = 60
MIN_VALID_TRADES = 20                 # over the evaluation period (monitored, never forced)

# ── VWAP mechanics ────────────────────────────────────────────────────────
# Unlike the EMA sibling (5-min bars), this strategy runs on 1-MINUTE bars
# throughout -- VWAP is naturally a fine-granularity cumulative construct
# (most platforms compute it from 1-min or finer), and 1-min bars also
# match the cron tick cadence directly, so there's no separate "once per
# N minutes" update step like the EMA sibling's step 7 -- VWAP/bias state
# updates every tick from the same bar fetch used for breakout scanning.
ATR_PERIOD = 14

# "Clean bias" filter (agent.md: stand aside if price is chopping across
# VWAP repeatedly with no clear bias). Both of these are genuinely
# arbitrary defaults, flagged for the backtest to validate before going
# live -- not shipped as unverified assumptions. Values are smaller than
# the EMA sibling's equivalents because they're expressed in 1-min bars
# instead of 5-min bars, not because the persistence requirement itself
# is meant to be looser.
CLEAN_BIAS_MIN_BARS_SINCE_CROSS = 20     # 20 x 1-min bars = 20 minutes of sustained same-side-of-VWAP
CLEAN_BIAS_MIN_DISTANCE_ATR_MULT = 0.3   # |price-vwap| >= 0.3x ATR(14) -- self-scales per name's own volatility

SWING_LOOKBACK_BARS = 20                 # STOP reference only -- bounded further by bars-since-bias-start
                                          # (same trend-start-bounding logic as the EMA sibling's stop, and
                                          # for the same reason: a stop referencing a level from before the
                                          # current bias began isn't a real reference point for it)

# TARGET: agent.md offers "the prior high or low of the day, OR a fixed
# R-multiple (e.g., 2R)" as alternatives -- rather than picking one and
# rejecting setups where it doesn't work (the EMA sibling's approach,
# which needed a hard MIN_TARGET_RISK_REWARD reject), this strategy tries
# the day-extreme target first and falls back to the fixed R-multiple
# whenever the day-extreme doesn't clear the minimum reward:risk. This
# uses agent.md's own stated flexibility productively instead of
# discarding a setup that still has a legitimate fallback available.
FIXED_R_MULTIPLE = 2.0                   # fallback target when the day's prior high/low is too close
MIN_TARGET_RISK_REWARD = 1.5             # day-extreme target must clear this x the stop distance, else fallback
STOP_BUFFER_PCT = 0.0005                 # push stop just beyond the level, not exactly on it

# Operational cap only (NOT a risk control -- pre_trade_check's own
# budget/volume/buying-power gating already bounds real exposure) --
# bounds the per-tick "fetch latest close for watched tickers" batch size.
MAX_CONCURRENT_SETUPS = 15

HALT_PROXY_MOVE_PCT = 0.10
HALT_PROXY_WINDOW_MINUTES = 5

# ── Session timing (all wall-clock ET) ──────────────────────────────────
ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE = 20   # stop looking for NEW entries this far before close
FLATTEN_MINUTES_BEFORE_CLOSE = 10        # force-close everything this far before close

# ── Order execution ──────────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 1.005   # marketable-limit buffer on bracket entry (0.5%), bounds slippage

# ── EOD reporting ──────────────────────────────────────────────────────
EOD_REFLECTION_MODEL = "claude-haiku-4-5-20251001"

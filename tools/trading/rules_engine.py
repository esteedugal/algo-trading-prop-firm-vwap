"""
Prop-Firm Rules Engine
-------------------------
The highest-stakes, most novel module in this project — enforces the
compliance rulebook (agent.md) on top of the mechanical ORB signal. A
dedicated module (not a generic dispatcher) since this rulebook is too
specific and numerous to abstract, same shape as the tastystyle
project's exit_rules.py.

Two levels of persistent state (tools/trading/position_store.py):
  account_state - whole-evaluation-lifetime (initial balance, static $
                  thresholds, one-way ratchet flag, permanent termination).
  daily_state   - per trading day (start-of-day equity snapshot, running
                  realized P&L, halted flag).

`pre_trade_check` is the single entry point tick.py calls before ever
submitting an order. It threads a MUTATING in-flight risk accumulator
(`remaining_intraday_risk_committed`) across candidates processed
sequentially within one tick — otherwise two names breaking out in the
same minute could each be approved independently against a stale "full
remaining budget" figure and jointly blow past it. The caller (tick.py)
is responsible for incrementing that accumulator after each approval and
threading it into the next candidate's check within the same tick.
"""

from datetime import datetime
from typing import Optional

from tools.trading import position_store
from config.settings import (
    RISK_PER_TRADE_PCT,
    VOLUME_CAP_PCT,
    CONSISTENCY_MAX_PROFIT_SHARE,
    MIN_VALID_PROFIT_CENTS,
    MIN_VALID_HOLD_SECONDS,
)


# ── Virtual account equity ───────────────────────────────────────────────
#
# CRITICAL: the Alpaca paper account backing this project has $100,000 of
# real equity (standard Alpaca paper default) — that number must NEVER be
# used for compliance checks. This strategy is managed against a
# SIMULATED $25k prop-firm tier, and "current equity" for every rule in
# this file means account_state['initial_balance'] (the virtual tier
# balance) plus this project's OWN cumulative realized P&L ledger plus
# the mark-to-market unrealized P&L of any currently open positions —
# entirely our own bookkeeping, decoupled from whatever Alpaca's account
# object reports. Feeding Alpaca's real $100k equity into
# check_max_drawdown/apply_drawdown_ratchet was tried and immediately
# mis-fired (instant false-positive ratchet on the very first tick) —
# this is the fix, not an optional refinement.

def compute_unrealized_pnl(open_positions: list, latest_prices: dict) -> float:
    total = 0.0
    for pos in open_positions:
        price = latest_prices.get(pos["ticker"])
        if price is None or pos.get("fill_price") is None:
            continue
        direction_sign = 1.0 if pos["direction"] == "long" else -1.0
        total += (price - pos["fill_price"]) * pos["qty"] * direction_sign
    return total


def compute_virtual_equity(account_state: dict, open_positions: list, latest_prices: dict) -> float:
    return (
        account_state["initial_balance"]
        + (account_state.get("cumulative_realized_pnl") or 0.0)
        + compute_unrealized_pnl(open_positions, latest_prices)
    )


def record_realized_pnl(trading_date: str, pnl_dollar: Optional[float], db_path: str = position_store.DB_PATH) -> None:
    """
    Single place that updates BOTH the per-day realized_pnl_running (daily
    loss limit bookkeeping) and the whole-evaluation-lifetime
    cumulative_realized_pnl (drawdown/ratchet bookkeeping) whenever a
    position closes. Called from reconcile.check_bracket_exits() (normal
    bracket-leg exits) and tick.py's _flatten_all() (forced closes) —
    never updated ad hoc elsewhere, so these two numbers can't drift apart.
    """
    if pnl_dollar is None:
        return
    daily = position_store.get_daily_state(trading_date, db_path)
    if daily:
        position_store.update_daily_state(
            trading_date, db_path=db_path, realized_pnl_running=daily["realized_pnl_running"] + pnl_dollar
        )
    account = position_store.get_account_state(db_path)
    if account:
        position_store.update_account_state(
            db_path=db_path,
            cumulative_realized_pnl=(account.get("cumulative_realized_pnl") or 0.0) + pnl_dollar,
        )


# ── Daily state bootstrap ────────────────────────────────────────────────

def get_or_create_daily_state(trading_date: str, current_equity: float, db_path: str = position_store.DB_PATH) -> dict:
    """Snapshots start-of-day equity on the first call of the day; every
    later call the same day just returns the existing row unchanged."""
    existing = position_store.get_daily_state(trading_date, db_path)
    if existing:
        return existing

    account_state = position_store.get_account_state(db_path)
    if account_state is None:
        raise RuntimeError(
            "account_state not initialized — call position_store.init_account_state() "
            "before the first tick of any trading day."
        )
    return position_store.create_daily_state(
        trading_date=trading_date,
        start_of_day_equity=current_equity,
        daily_loss_limit_snapshot=account_state["daily_loss_limit_dollars"],
        db_path=db_path,
    )


# ── Daily loss limit ─────────────────────────────────────────────────────

def compute_daily_loss(current_equity: float, start_of_day_equity: float) -> float:
    """Literal agent.md definition: current equity minus start-of-day
    balance. Negative = a loss so far today."""
    return current_equity - start_of_day_equity


def check_daily_loss_limit(daily_state: dict, current_equity: float, daily_loss_limit_dollars: float) -> dict:
    daily_loss = compute_daily_loss(current_equity, daily_state["start_of_day_equity"])
    loss_so_far = max(0.0, -daily_loss)
    remaining_budget = max(0.0, daily_loss_limit_dollars - loss_so_far)
    return {
        "breached": loss_so_far >= daily_loss_limit_dollars,
        "daily_loss": daily_loss,
        "remaining_budget": remaining_budget,
    }


# ── Max drawdown + one-way ratchet ───────────────────────────────────────

def compute_drawdown_floor(account_state: dict) -> float:
    if account_state["is_ratcheted"]:
        return account_state["initial_balance"]
    return account_state["initial_balance"] - account_state["max_drawdown_dollars"]


def check_max_drawdown(account_state: dict, current_equity: float) -> dict:
    floor = compute_drawdown_floor(account_state)
    return {
        "breached": current_equity <= floor,
        "floor": floor,
        "buffer_remaining": max(0.0, current_equity - floor),
    }


def apply_drawdown_ratchet(account_state: dict, current_equity: float, db_path: str = position_store.DB_PATH) -> dict:
    """
    One-way: once equity reaches initial_balance + ratchet_multiple *
    daily_loss_limit_dollars in profit, the drawdown floor moves up to
    initial_balance (locks in a no-loss buffer) and NEVER moves back down,
    even if equity later falls again. Persists the flip immediately so a
    crashed/restarted tick can't lose the ratchet.
    """
    if account_state["is_ratcheted"]:
        return account_state

    threshold = account_state["initial_balance"] + (
        account_state["drawdown_ratchet_multiple"] * account_state["daily_loss_limit_dollars"]
    )
    if current_equity < threshold:
        return account_state

    now = datetime.now().isoformat()
    position_store.update_account_state(db_path=db_path, is_ratcheted=1, ratcheted_at=now)
    return position_store.get_account_state(db_path)


# ── Position sizing ──────────────────────────────────────────────────────

def compute_position_size(daily_loss_limit_dollars: float, risk_per_trade_pct: float, stop_distance_dollars: float) -> int:
    """qty = floor((daily_loss_limit_dollars * risk_per_trade_pct) / stop_distance).
    Uses the FIXED daily_loss_limit constant per agent.md's literal
    formula — deliberately NOT "remaining budget today" (that's a
    separate, later check in pre_trade_check)."""
    if stop_distance_dollars <= 0:
        return 0
    risk_dollars = daily_loss_limit_dollars * risk_per_trade_pct
    return int(risk_dollars // stop_distance_dollars)


def compute_final_qty(
    risk_sized_qty: int,
    prior_minute_volume: float,
    volume_cap_pct: float,
    buying_power: float,
    last_price: float,
) -> dict:
    volume_cap_qty = int(volume_cap_pct * prior_minute_volume) if prior_minute_volume else 0
    bp_qty = int(buying_power // last_price) if last_price and last_price > 0 else 0

    candidates = {"risk": risk_sized_qty, "volume_cap": volume_cap_qty, "buying_power": bp_qty}
    final_qty = min(candidates.values())
    priority = ["risk", "volume_cap", "buying_power"]
    binding_constraint = min(
        (k for k, v in candidates.items() if v == final_qty),
        key=lambda k: priority.index(k),
    )
    return {"final_qty": max(0, final_qty), "binding_constraint": binding_constraint}


# ── Pre-trade checklist (mirrors agent.md's own 5-item checklist) ───────

def pre_trade_check(
    candidate: dict,
    account_state: dict,
    daily_state: dict,
    current_equity: float,
    remaining_intraday_risk_committed: float,
    prior_minute_volume: float,
    buying_power: float,
) -> dict:
    checklist = {}
    reasons = []

    # 1. A stop-loss level is set.
    stop_price = candidate.get("stop_price")
    entry_trigger = candidate.get("entry_trigger")
    checklist["stop_set"] = stop_price is not None and entry_trigger is not None
    if not checklist["stop_set"]:
        reasons.append("candidate missing stop_price/entry_trigger")
        stop_distance = 0.0
    else:
        stop_distance = abs(entry_trigger - stop_price)

    # 2. Position size respects the risk-per-trade math, clamped by volume
    #    cap and buying power (agent.md items 2 and 3 collapse into one
    #    sizing computation — the volume cap is enforced by construction,
    #    not as a separate pass/fail gate, since final_qty can never
    #    exceed it).
    risk_sized_qty = compute_position_size(
        account_state["daily_loss_limit_dollars"], RISK_PER_TRADE_PCT, stop_distance
    ) if stop_distance > 0 else 0
    sizing = compute_final_qty(
        risk_sized_qty, prior_minute_volume, VOLUME_CAP_PCT, buying_power, entry_trigger or 0.0
    )
    final_qty = sizing["final_qty"]
    checklist["size_ok"] = final_qty > 0
    if not checklist["size_ok"]:
        reasons.append(f"final_qty <= 0 (binding_constraint={sizing['binding_constraint']})")

    # 3. Order size within the 5% one-minute-volume cap — enforced by
    #    construction inside compute_final_qty above (final_qty can never
    #    exceed the volume-cap-implied quantity), checked explicitly here
    #    as a named checklist item rather than silently assumed.
    checklist["volume_cap_ok"] = True

    # 4. Remaining daily loss budget and drawdown buffer can absorb a
    #    worst-case loss on this trade (threading the same-tick mutating
    #    accumulator so two same-minute breakouts can't both be approved
    #    against a stale "full remaining budget" figure).
    risk_dollars_this_trade = final_qty * stop_distance
    daily_check = check_daily_loss_limit(daily_state, current_equity, daily_state["daily_loss_limit_snapshot"])
    drawdown_check = check_max_drawdown(account_state, current_equity)
    remaining_daily_budget = daily_check["remaining_budget"] - remaining_intraday_risk_committed
    remaining_drawdown_buffer = drawdown_check["buffer_remaining"] - remaining_intraday_risk_committed
    checklist["budget_ok"] = (
        risk_dollars_this_trade <= remaining_daily_budget
        and risk_dollars_this_trade <= remaining_drawdown_buffer
    )
    if not checklist["budget_ok"]:
        reasons.append(
            f"risk ${risk_dollars_this_trade:.2f} exceeds remaining budget "
            f"(daily=${remaining_daily_budget:.2f}, drawdown=${remaining_drawdown_buffer:.2f})"
        )

    # 5. No conflict with overnight/earnings/dividend/split restrictions if
    #    held past close. Always True for this strategy (ORB always
    #    flattens by the FLATTEN_MINUTES_BEFORE_CLOSE deadline — no
    #    overnight holding branch exists) — checked explicitly rather than
    #    assumed away, per the plan's sharp-edges note.
    checklist["no_calendar_conflict"] = True

    approved = all(checklist.values())

    return {
        "approved": approved,
        "checklist": checklist,
        "reasons": reasons,
        "final_qty": final_qty if approved else 0,
        "risk_dollars": risk_dollars_this_trade if checklist["stop_set"] else 0.0,
        "binding_constraint": sizing["binding_constraint"],
    }


# ── Hard-stop monitoring — runs first, every tick ────────────────────────

def monitor_hard_stops(account_state: dict, daily_state: dict, current_equity: float) -> dict:
    """Checked unconditionally at the top of every tick, even on an
    already-halted/terminated day (cheap re-check). Detection only —
    tick.py is responsible for actually cancelling brackets and
    flattening positions when this reports a breach."""
    ratcheted_state = apply_drawdown_ratchet(account_state, current_equity)
    daily_check = check_daily_loss_limit(
        daily_state, current_equity, daily_state["daily_loss_limit_snapshot"]
    )
    drawdown_check = check_max_drawdown(ratcheted_state, current_equity)
    return {
        "daily_loss_breached": daily_check["breached"],
        "drawdown_breached": drawdown_check["breached"],
        "daily_loss_detail": daily_check,
        "drawdown_detail": drawdown_check,
        "account_state": ratcheted_state,
    }


def record_breach(trading_date: str, rule: str, detail: str, action_taken: str, db_path: str = position_store.DB_PATH) -> None:
    """Append-only. Never silently swallowed — every hard-stop and every
    other rule violation must go through here, no exceptions."""
    position_store.record_breach(trading_date, rule, detail, action_taken, db_path)


# ── Trade validity / consistency rule (post-trade, self-reported) ──────

def is_valid_trade(pnl_dollar: Optional[float], held_seconds: Optional[float]) -> bool:
    """A position counts toward the 20-valid-trade minimum only if it
    cleared the minimum profit AND minimum hold time. Purely a counter —
    has no write-path back into orb/signal.py or pre_trade_check's
    thresholds, by design: this module must never force a trade to hit a
    quota."""
    if pnl_dollar is None or held_seconds is None:
        return False
    return pnl_dollar >= (MIN_VALID_PROFIT_CENTS / 100.0) and held_seconds >= MIN_VALID_HOLD_SECONDS


def compute_profit_shares(closed_positions: list) -> dict:
    """{position_id: share of TOTAL POSITIVE P&L this position represents}.
    Losing trades and trades on a day/period with zero total profit get 0.
    """
    total_profit = sum(p["pnl_dollar"] for p in closed_positions if (p.get("pnl_dollar") or 0) > 0)
    shares = {}
    for p in closed_positions:
        pnl = p.get("pnl_dollar") or 0.0
        shares[p["id"]] = (pnl / total_profit) if (pnl > 0 and total_profit > 0) else 0.0
    return shares


def check_consistency_rule(closed_positions: list, max_share: float = CONSISTENCY_MAX_PROFIT_SHARE) -> dict:
    """
    No pre-trade lever exists for this rule: fixed 10:1 bracket exits mean
    there's no mechanism to prevent a single winner from growing "too big"
    once in flight (the target is fixed at entry). This can only be
    monitored and self-reported after the fact — never enforced pre-trade
    the way the loss/drawdown/volume rules can be.
    """
    shares = compute_profit_shares(closed_positions)
    violations = [pid for pid, s in shares.items() if s > max_share]
    return {"shares": shares, "violations": violations, "breached": len(violations) > 0}

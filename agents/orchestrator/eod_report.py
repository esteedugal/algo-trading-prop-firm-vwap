"""
End-of-Day Report — Trade Log + LLM Reflection
----------------------------------------------------
Fires once via a separate cron entry (~16:20 ET, after tick.py's own
flatten deadline has had time to settle). Ported from the sibling ORB/
EMA projects' eod_report.py with targeted edits: candidates -> vwap_setups,
and the reflection prompt retitled for this strategy. Sequence, reconcile/
save/file-write logic, and the LLM-touchpoint scope boundary are unchanged.

This is the ONLY place an LLM touches this project. The entry/exit/
sizing path (vwap_trend/signal.py + tools/trading/rules_engine.py) is
100% mechanical and never calls Claude. Always runs, even on a zero-
trade day — a quiet or genuinely choppy day is a valid, reportable
outcome, not a skip condition.

Usage:
  python agents/orchestrator/eod_report.py [YYYY-MM-DD]
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import argparse
from datetime import datetime
from typing import Optional
import pytz

from alpaca.trading.client import TradingClient
import anthropic

from config.settings import EOD_REFLECTION_MODEL, CONSISTENCY_MAX_PROFIT_SHARE, MIN_VALID_TRADES
from tools.trading import position_store
from tools.trading.rules_engine import check_consistency_rule
from agents.orchestrator import reconcile

ET = pytz.timezone("America/New_York")


def _client() -> TradingClient:
    return TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        paper=True,
    )


def build_trade_table(positions: list, setups_by_id: dict) -> str:
    if not positions:
        return "_No trades were opened today._"

    lines = ["| Entry | Exit | Size | Stop | P&L | Rationale |", "|---|---|---|---|---|---|"]
    for p in positions:
        setup = setups_by_id.get(p["candidate_id"], {})

        if p.get("fill_price") is not None:
            entry_str = f"{p['direction'].upper()} {p['ticker']} @ {p['fill_price']:.2f}"
        else:
            entry_str = f"{p['direction'].upper()} {p['ticker']} (never filled — {p.get('exit_reason') or p.get('status')})"

        if p["status"] == "closed":
            exit_str = f"{p['exit_price']:.2f} ({p['exit_reason']})" if p.get("exit_price") is not None else f"({p['exit_reason']})"
            pnl_str = f"${p['pnl_dollar']:+.2f}" if p.get("pnl_dollar") is not None else "n/a"
        elif p["status"] == "open":
            exit_str = "STILL OPEN (unexpected — EOD flatten should have closed this)"
            pnl_str = "n/a"
        else:
            exit_str = "n/a (order never filled)"
            pnl_str = "n/a"

        rationale_parts = []
        if setup.get("stop_basis"):
            rationale_parts.append(f"stop={setup['stop_basis']}")
        if setup.get("target_basis"):
            rationale_parts.append(f"target={setup['target_basis']}")
        if setup.get("cycle_number"):
            rationale_parts.append(f"cycle {setup['cycle_number']} of day")
        rationale_parts.append(f"{p['direction']} VWAP pullback resumption")
        rationale = ", ".join(rationale_parts)

        stop_str = f"{p['stop_price']:.2f}" if p.get("stop_price") is not None else "n/a"
        lines.append(f"| {entry_str} | {exit_str} | {p.get('qty', 'n/a')} | {stop_str} | {pnl_str} | {rationale} |")

    return "\n".join(lines)


def build_context(trading_date: str, positions: list, setups: list, account_state: dict, daily_state: dict, breaches: list) -> str:
    setups_by_id = {s["id"]: s for s in setups}
    trade_table = build_trade_table(positions, setups_by_id)

    closed = [p for p in positions if p["status"] == "closed"]
    consistency = check_consistency_rule(closed) if closed else {"shares": {}, "violations": [], "breached": False}
    valid_count_today = sum(1 for p in closed if p.get("is_valid_trade"))
    lifetime_valid = position_store.count_valid_trades()

    status_counts: dict = {}
    for s in setups:
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1

    target_basis_counts: dict = {}
    for s in setups:
        if s.get("target_basis"):
            target_basis_counts[s["target_basis"]] = target_basis_counts.get(s["target_basis"], 0) + 1

    breach_lines = "\n".join(f"- {b['rule']}: {b['detail']} (action: {b['action_taken']})" for b in breaches) or "None"

    parts = [
        f"TRADING DATE: {trading_date}",
        f"\nTRADE LOG:\n{trade_table}",
        "\nDAILY SUMMARY:",
        f"  Start-of-day equity: ${daily_state['start_of_day_equity']:.2f}",
        f"  Realized P&L today: ${daily_state['realized_pnl_running']:.2f}",
        f"  Daily loss limit: ${daily_state['daily_loss_limit_snapshot']:.2f}",
        f"  Trading halted today: {'YES — ' + str(daily_state.get('halt_reason')) if daily_state['trading_halted'] else 'No'}",
        "\nACCOUNT STATUS:",
        f"  Cumulative P&L since evaluation start: ${account_state.get('cumulative_realized_pnl', 0):.2f}",
        f"  Drawdown ratchet locked in: {'YES' if account_state['is_ratcheted'] else 'No'}",
        f"  Account terminated: {'YES — ' + str(account_state.get('terminated_reason')) if account_state['terminated'] else 'No'}",
        f"\nCONSISTENCY RULE (max {int(CONSISTENCY_MAX_PROFIT_SHARE * 100)}% of profit from one trade):",
        f"  Breached today: {consistency['breached']}",
        f"  Per-trade profit shares: {consistency['shares']}",
        f"\nVALID-TRADE PROGRESS (min {MIN_VALID_TRADES} required over the evaluation period, never forced):",
        f"  Valid trades today: {valid_count_today}",
        f"  Valid trades lifetime: {lifetime_valid} / {MIN_VALID_TRADES}",
        "\nVWAP-SETUP OUTCOMES TODAY:",
        f"  {status_counts if status_counts else 'No VWAP setups found (no ticker qualified as a clean bias today)'}",
        f"  Target basis used (day_extreme vs fixed_r fallback): {target_basis_counts if target_basis_counts else 'n/a'}",
        f"\nRULE BREACHES TODAY:\n{breach_lines}",
    ]
    return "\n".join(parts)


def generate_reflection(context: str) -> dict:
    system_prompt = """You are the trading agent's own end-of-day reviewer for a
VWAP pullback/reversion day-trading strategy operated under a prop-firm
compliance rulebook. Given today's actual trade log and compliance state
below, write a short reflection with exactly these three headers:

**What worked:**
**What didn't:**
**Lesson learned:**

Ground every claim in the actual data provided — name real tickers, real
sizes, real timing. Be specific enough to change tomorrow's behavior
(e.g. "AAPL's pullback to VWAP at 10:42 resumed cleanly and hit its
day-high target" is useful; "risk management is important" is not). Do
not restate the rulebook's rules in general terms. If there were zero
trades today, say so plainly and comment on whether that reflects a
genuinely choppy/directionless day across the universe versus the
clean-bias filter being too strict — but do not speculate beyond what
the data shows. If many setups were abandoned via bias failure or bias
flip, comment on whether that's a sign the clean-bias filter's
thresholds need revisiting. If the fixed R-multiple fallback target was
used far more often than the day-extreme target, comment on whether that
suggests entries are happening too early in the session (before a
meaningful prior high/low has formed) rather than a filter problem."""

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model=EOD_REFLECTION_MODEL,
        max_tokens=800,
        system=system_prompt,
        messages=[{"role": "user", "content": context}],
    )
    return {
        "text": message.content[0].text,
        "model_used": EOD_REFLECTION_MODEL,
        "tokens_in": message.usage.input_tokens,
        "tokens_out": message.usage.output_tokens,
    }


def run_eod_report(trading_date: Optional[str] = None) -> None:
    now_et = datetime.now(ET)
    trading_date = trading_date or now_et.date().isoformat()

    client = _client()
    reconcile.reconcile_pending_positions(client)
    reconcile.check_bracket_exits(client)
    reconcile.audit_open_positions(client)

    daily_state = position_store.get_daily_state(trading_date)
    if daily_state is None:
        print(f"[{now_et}] No daily_state for {trading_date} — market likely didn't open, "
              f"or tick.py never ran today. Skipping EOD report.")
        return

    positions = position_store.list_positions_for_date(trading_date)
    setups = position_store.list_setups_for_date(trading_date)
    account_state = position_store.get_account_state()
    breaches = position_store.list_breaches_for_date(trading_date)

    setups_by_id = {s["id"]: s for s in setups}
    trade_table = build_trade_table(positions, setups_by_id)
    context = build_context(trading_date, positions, setups, account_state, daily_state, breaches)

    try:
        reflection = generate_reflection(context)
    except Exception as e:
        print(f"  ⚠️  LLM reflection generation failed: {e}")
        reflection = {"text": f"(reflection generation failed: {e})", "model_used": None, "tokens_in": None, "tokens_out": None}

    position_store.save_eod_report(
        trading_date, trade_table, reflection["text"], reflection["model_used"],
        reflection["tokens_in"], reflection["tokens_out"],
    )

    report_text = f"# EOD Report — {trading_date}\n\n{trade_table}\n\n{reflection['text']}\n"
    os.makedirs("logs", exist_ok=True)
    with open(f"logs/eod_report_{trading_date.replace('-', '')}.txt", "w") as f:
        f.write(report_text)

    print(report_text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VWAP Pullback/Reversion EOD Report")
    parser.add_argument("date", nargs="?", default=None, help="Trading date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    run_eod_report(args.date)

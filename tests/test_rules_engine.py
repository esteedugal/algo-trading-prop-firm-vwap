"""Unit tests for tools/trading/rules_engine.py — synthetic account/daily
states, temp SQLite DB, no live API needed."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from tools.trading import position_store
from tools.trading import rules_engine as re


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let init_db create it fresh
    return path


def _account_state(db_path, initial_balance=25_000.0, daily_loss_limit=500.0, max_drawdown=1500.0, ratchet_multiple=3.0):
    return position_store.init_account_state(
        initial_balance=initial_balance,
        daily_loss_limit_dollars=daily_loss_limit,
        max_drawdown_dollars=max_drawdown,
        drawdown_ratchet_multiple=ratchet_multiple,
        evaluation_start_date="2026-07-13",
        db_path=db_path,
    )


def test_compute_daily_loss_basic():
    assert re.compute_daily_loss(current_equity=24_800.0, start_of_day_equity=25_000.0) == -200.0
    assert re.compute_daily_loss(current_equity=25_300.0, start_of_day_equity=25_000.0) == 300.0


def test_check_daily_loss_limit_not_breached():
    daily_state = {"start_of_day_equity": 25_000.0}
    result = re.check_daily_loss_limit(daily_state, current_equity=24_800.0, daily_loss_limit_dollars=500.0)
    assert result["breached"] is False
    assert result["remaining_budget"] == 300.0


def test_check_daily_loss_limit_breached_at_exact_threshold():
    daily_state = {"start_of_day_equity": 25_000.0}
    result = re.check_daily_loss_limit(daily_state, current_equity=24_500.0, daily_loss_limit_dollars=500.0)
    assert result["breached"] is True
    assert result["remaining_budget"] == 0.0


def test_check_daily_loss_limit_profit_day_gives_full_budget():
    daily_state = {"start_of_day_equity": 25_000.0}
    result = re.check_daily_loss_limit(daily_state, current_equity=25_500.0, daily_loss_limit_dollars=500.0)
    assert result["breached"] is False
    assert result["remaining_budget"] == 500.0


def test_compute_drawdown_floor_not_ratcheted():
    account_state = {"initial_balance": 25_000.0, "max_drawdown_dollars": 1500.0, "is_ratcheted": 0}
    assert re.compute_drawdown_floor(account_state) == 23_500.0


def test_compute_drawdown_floor_ratcheted_locks_at_initial():
    account_state = {"initial_balance": 25_000.0, "max_drawdown_dollars": 1500.0, "is_ratcheted": 1}
    assert re.compute_drawdown_floor(account_state) == 25_000.0


def test_check_max_drawdown_breach():
    account_state = {"initial_balance": 25_000.0, "max_drawdown_dollars": 1500.0, "is_ratcheted": 0}
    result = re.check_max_drawdown(account_state, current_equity=23_400.0)
    assert result["breached"] is True
    assert result["floor"] == 23_500.0


def test_apply_drawdown_ratchet_is_one_way_and_persists():
    db_path = _fresh_db()
    try:
        account_state = _account_state(db_path)
        assert account_state["is_ratcheted"] == 0

        # Not yet at 3x daily loss limit in profit (25000 + 3*500 = 26500) -> no ratchet
        result = re.apply_drawdown_ratchet(account_state, current_equity=26_000.0, db_path=db_path)
        assert result["is_ratcheted"] == 0

        # Crosses the threshold -> ratchets, persists
        result = re.apply_drawdown_ratchet(account_state, current_equity=26_500.0, db_path=db_path)
        assert result["is_ratcheted"] == 1
        assert result["ratcheted_at"] is not None

        # Re-fetch fresh state, confirm persisted
        fresh = position_store.get_account_state(db_path)
        assert fresh["is_ratcheted"] == 1

        # Equity falling back below the threshold does NOT un-ratchet
        result2 = re.apply_drawdown_ratchet(fresh, current_equity=24_000.0, db_path=db_path)
        assert result2["is_ratcheted"] == 1
        assert re.compute_drawdown_floor(result2) == 25_000.0
    finally:
        os.remove(db_path)


def test_compute_position_size_basic():
    # 500 * 0.30 = 150 risk dollars; stop distance 0.50 -> 300 shares
    qty = re.compute_position_size(daily_loss_limit_dollars=500.0, risk_per_trade_pct=0.30, stop_distance_dollars=0.50)
    assert qty == 300


def test_compute_position_size_zero_stop_distance_returns_zero():
    assert re.compute_position_size(500.0, 0.30, 0.0) == 0


def test_compute_final_qty_risk_binds():
    result = re.compute_final_qty(risk_sized_qty=300, prior_minute_volume=1_000_000, volume_cap_pct=0.05, buying_power=25_000, last_price=10.0)
    # volume_cap_qty = 50000, bp_qty = 2500 -> risk(300) is smallest
    assert result["final_qty"] == 300
    assert result["binding_constraint"] == "risk"


def test_compute_final_qty_volume_cap_binds():
    result = re.compute_final_qty(risk_sized_qty=300, prior_minute_volume=1000, volume_cap_pct=0.05, buying_power=25_000, last_price=10.0)
    # volume_cap_qty = 50 -> smallest
    assert result["final_qty"] == 50
    assert result["binding_constraint"] == "volume_cap"


def test_compute_final_qty_buying_power_binds():
    result = re.compute_final_qty(risk_sized_qty=300, prior_minute_volume=1_000_000, volume_cap_pct=0.05, buying_power=500, last_price=10.0)
    # bp_qty = 50 -> smallest
    assert result["final_qty"] == 50
    assert result["binding_constraint"] == "buying_power"


def test_pre_trade_check_approves_clean_candidate():
    db_path = _fresh_db()
    try:
        account_state = _account_state(db_path)
        daily_state = {"start_of_day_equity": 25_000.0, "daily_loss_limit_snapshot": 500.0}
        candidate = {"entry_trigger": 100.0, "stop_price": 99.0}  # stop distance 1.0
        result = re.pre_trade_check(
            candidate, account_state, daily_state,
            current_equity=25_000.0, remaining_intraday_risk_committed=0.0,
            prior_minute_volume=1_000_000, buying_power=25_000.0,
        )
        assert result["approved"] is True
        assert result["final_qty"] > 0
        assert all(result["checklist"].values())
    finally:
        os.remove(db_path)


def test_pre_trade_check_rejects_missing_stop():
    db_path = _fresh_db()
    try:
        account_state = _account_state(db_path)
        daily_state = {"start_of_day_equity": 25_000.0, "daily_loss_limit_snapshot": 500.0}
        candidate = {"entry_trigger": 100.0, "stop_price": None}
        result = re.pre_trade_check(
            candidate, account_state, daily_state,
            current_equity=25_000.0, remaining_intraday_risk_committed=0.0,
            prior_minute_volume=1_000_000, buying_power=25_000.0,
        )
        assert result["approved"] is False
        assert result["checklist"]["stop_set"] is False
    finally:
        os.remove(db_path)


def test_pre_trade_check_rejects_when_budget_exhausted_by_same_tick_accumulator():
    db_path = _fresh_db()
    try:
        account_state = _account_state(db_path)
        daily_state = {"start_of_day_equity": 25_000.0, "daily_loss_limit_snapshot": 500.0}
        candidate = {"entry_trigger": 100.0, "stop_price": 99.0}
        # Simulate the full daily budget already committed earlier in this same tick.
        result = re.pre_trade_check(
            candidate, account_state, daily_state,
            current_equity=25_000.0, remaining_intraday_risk_committed=500.0,
            prior_minute_volume=1_000_000, buying_power=25_000.0,
        )
        assert result["approved"] is False
        assert result["checklist"]["budget_ok"] is False
    finally:
        os.remove(db_path)


def test_compute_unrealized_pnl_long_and_short():
    open_positions = [
        {"ticker": "AAPL", "direction": "long", "fill_price": 100.0, "qty": 10},
        {"ticker": "TSLA", "direction": "short", "fill_price": 200.0, "qty": 5},
    ]
    latest_prices = {"AAPL": 102.0, "TSLA": 195.0}
    # AAPL: (102-100)*10 = +20 ; TSLA short: (195-200)*5*(-1) = +25
    pnl = re.compute_unrealized_pnl(open_positions, latest_prices)
    assert abs(pnl - 45.0) < 1e-9


def test_compute_unrealized_pnl_missing_price_skipped():
    open_positions = [{"ticker": "AAPL", "direction": "long", "fill_price": 100.0, "qty": 10}]
    assert re.compute_unrealized_pnl(open_positions, {}) == 0.0


def test_compute_virtual_equity_ignores_alpaca_real_account_value():
    # This is the exact bug found live: Alpaca's real paper account sits
    # at $100k while the simulated tier is $25k -- virtual equity must be
    # computed ENTIRELY from our own ledger (initial_balance +
    # cumulative_realized_pnl + unrealized), never from Alpaca's account.
    account_state = {"initial_balance": 25_000.0, "cumulative_realized_pnl": 150.0}
    equity = re.compute_virtual_equity(account_state, open_positions=[], latest_prices={})
    assert equity == 25_150.0


def test_record_realized_pnl_updates_both_daily_and_lifetime_ledgers():
    db_path = _fresh_db()
    try:
        _account_state(db_path)
        position_store.create_daily_state("2026-07-13", start_of_day_equity=25_000.0, daily_loss_limit_snapshot=500.0, db_path=db_path)

        re.record_realized_pnl("2026-07-13", 80.0, db_path=db_path)
        re.record_realized_pnl("2026-07-13", -20.0, db_path=db_path)

        daily = position_store.get_daily_state("2026-07-13", db_path)
        account = position_store.get_account_state(db_path)
        assert daily["realized_pnl_running"] == 60.0
        assert account["cumulative_realized_pnl"] == 60.0
    finally:
        os.remove(db_path)


def test_is_valid_trade():
    assert re.is_valid_trade(pnl_dollar=0.15, held_seconds=90) is True
    assert re.is_valid_trade(pnl_dollar=0.05, held_seconds=90) is False, "below 10-cent minimum"
    assert re.is_valid_trade(pnl_dollar=0.15, held_seconds=30) is False, "below 60-second minimum"
    assert re.is_valid_trade(pnl_dollar=None, held_seconds=90) is False


def test_compute_profit_shares_and_consistency_rule():
    positions = [
        {"id": 1, "pnl_dollar": 900.0},
        {"id": 2, "pnl_dollar": 100.0},
        {"id": 3, "pnl_dollar": -200.0},
    ]
    shares = re.compute_profit_shares(positions)
    assert abs(shares[1] - 0.9) < 1e-9
    assert abs(shares[2] - 0.1) < 1e-9
    assert shares[3] == 0.0

    result = re.check_consistency_rule(positions, max_share=0.30)
    assert result["breached"] is True
    assert result["violations"] == [1]


def test_check_consistency_rule_no_violation_when_balanced():
    # 4 equal winners -> 25% share each, under the 30% threshold.
    positions = [{"id": i, "pnl_dollar": 25.0} for i in range(4)]
    result = re.check_consistency_rule(positions, max_share=0.30)
    assert result["breached"] is False


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

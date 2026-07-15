"""Unit tests for vwap_trend/signal.py — pure functions, synthetic data, no live API."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from vwap_trend.signal import (
    compute_vwap,
    compute_atr,
    classify_bias,
    bars_since_cross,
    compute_clean_bias,
    detect_pullback_touch,
    detect_bias_failure,
    compute_swing_reference,
    compute_day_extreme,
    compute_stop_target,
    detect_breakout,
    detect_halt_proxy,
)


def _bars(rows, start="2026-07-14 09:30", freq="1min"):
    """rows: list of (open, high, low, close, volume) tuples."""
    idx = pd.date_range(start=start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    return df


def test_compute_vwap_constant_price_equals_price():
    bars = _bars([(50.0, 50.0, 50.0, 50.0, 1000)] * 10)
    vwap = compute_vwap(bars)
    assert abs(vwap.iloc[-1] - 50.0) < 1e-9


def test_compute_vwap_weights_by_volume():
    # Two bars: first at 100 with huge volume, second at 110 with tiny volume.
    # VWAP should sit much closer to 100 than a simple average of (100+110)/2=105.
    bars = _bars([
        (100.0, 100.0, 100.0, 100.0, 1_000_000),
        (110.0, 110.0, 110.0, 110.0, 1_000),
    ])
    vwap = compute_vwap(bars)
    assert vwap.iloc[-1] < 101.0  # heavily weighted toward the high-volume bar's price


def test_compute_atr_constant_range_bars():
    rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000) for i in range(20)]
    bars = _bars(rows)
    atr = compute_atr(bars, period=14)
    assert abs(atr.iloc[-1] - 2.0) < 0.5


def test_classify_bias_long_short_none():
    assert classify_bias(price=101.0, vwap=100.0) == "long"
    assert classify_bias(price=99.0, vwap=100.0) == "short"
    assert classify_bias(price=100.0, vwap=100.0) == "none"


def test_bars_since_cross_counts_consistent_run():
    price = pd.Series([110, 110, 110, 95, 96, 97, 97.5])
    vwap = pd.Series([100, 100, 100, 100, 100, 100, 100])
    assert bars_since_cross(price, vwap) == 4


def test_compute_clean_bias_true_for_sustained_distance():
    # Price steadily rising, VWAP lagging behind (weighted toward earlier, lower prices).
    rows = []
    price = 100.0
    for i in range(40):
        price += 0.5
        rows.append((price, price + 0.1, price - 0.1, price, 1000))
    bars = _bars(rows)
    vwap = compute_vwap(bars)
    atr = compute_atr(bars, period=14)
    result = compute_clean_bias(bars["close"], vwap, atr, min_bars_since_cross=20, min_distance_atr_mult=0.3)
    assert result["is_clean"] is True


def test_compute_clean_bias_false_for_choppy_series():
    rows = []
    price = 100.0
    for i in range(40):
        price += 0.3 if i % 2 == 0 else -0.3
        rows.append((price, price + 0.1, price - 0.1, price, 1000))
    bars = _bars(rows)
    vwap = compute_vwap(bars)
    atr = compute_atr(bars, period=14)
    result = compute_clean_bias(bars["close"], vwap, atr, min_bars_since_cross=20, min_distance_atr_mult=0.3)
    assert result["is_clean"] is False


def test_detect_pullback_touch_true_when_range_touches_and_close_holds():
    bar = {"low": 99.0, "high": 101.0, "close": 100.5}
    result = detect_pullback_touch(bar, vwap=100.0, direction="long")
    assert result["touched"] is True


def test_detect_pullback_touch_false_when_close_breaks_through():
    # range touches vwap but close ends up BELOW it for a long -- that's a
    # failure, not a held pullback.
    bar = {"low": 99.0, "high": 100.5, "close": 99.5}
    result = detect_pullback_touch(bar, vwap=100.0, direction="long")
    assert result["touched"] is False


def test_detect_pullback_touch_false_when_no_touch():
    bar = {"low": 105.0, "high": 106.0, "close": 105.5}
    result = detect_pullback_touch(bar, vwap=100.0, direction="long")
    assert result["touched"] is False


def test_detect_bias_failure_long_and_short():
    assert detect_bias_failure(bar_close=99.0, vwap=100.0, direction="long") is True
    assert detect_bias_failure(bar_close=100.5, vwap=100.0, direction="long") is False
    assert detect_bias_failure(bar_close=101.0, vwap=100.0, direction="short") is True
    assert detect_bias_failure(bar_close=99.5, vwap=100.0, direction="short") is False


def test_compute_swing_reference_bounded_by_floor_ts():
    bars = _bars([
        (100.0, 101.0, 90.0, 100.5, 1000),   # very low here, before the bias started
        (100.5, 102.0, 100.0, 101.5, 1000),
        (101.5, 102.5, 97.0, 98.0, 1000),    # swing low within the bias window
        (98.0, 99.0, 97.5, 98.5, 1000),
    ])
    floor_ts = bars.index[1]
    result = compute_swing_reference(bars, kind="swing_low", lookback_n=20, floor_ts=floor_ts)
    assert result["price"] == 97.0


def test_compute_day_extreme_high_and_low():
    bars = _bars([
        (100.0, 105.0, 99.0, 101.0, 1000),
        (101.0, 103.0, 95.0, 96.0, 1000),   # day low here
        (96.0, 108.0, 95.5, 107.0, 1000),   # day high here
    ])
    high = compute_day_extreme(bars, kind="day_high")
    low = compute_day_extreme(bars, kind="day_low")
    assert high["price"] == 108.0
    assert low["price"] == 95.0


def test_compute_stop_target_uses_day_extreme_when_it_clears_min_rr():
    result = compute_stop_target(
        entry_trigger=100.0, vwap_stop_price=99.0, swing_stop_price=98.5,
        day_extreme_price=110.0, direction="long", stop_buffer_pct=0.0,
        fixed_r_multiple=2.0, min_target_risk_reward=1.5,
    )
    # risk = 1.0 (tighter of 99.0/98.5 -> swing 98.5 is farther, vwap 99.0 is tighter)
    assert result["stop_basis"] == "vwap"
    assert result["stop_price"] == 99.0
    assert result["target_basis"] == "day_extreme"
    assert result["target_price"] == 110.0


def test_compute_stop_target_falls_back_to_fixed_r_when_day_extreme_too_close():
    result = compute_stop_target(
        entry_trigger=100.0, vwap_stop_price=99.0, swing_stop_price=98.5,
        day_extreme_price=100.5, direction="long", stop_buffer_pct=0.0,
        fixed_r_multiple=2.0, min_target_risk_reward=1.5,
    )
    assert result["stop_price"] == 99.0
    assert result["risk_per_share"] == 1.0
    assert result["target_basis"] == "fixed_r"
    assert result["target_price"] == 100.0 + 2.0 * 1.0  # 102.0


def test_compute_stop_target_falls_back_to_fixed_r_when_day_extreme_wrong_side():
    # day_extreme_price is BELOW entry for a long -- invalid, must fall back
    result = compute_stop_target(
        entry_trigger=100.0, vwap_stop_price=99.0, swing_stop_price=98.5,
        day_extreme_price=99.5, direction="long", stop_buffer_pct=0.0,
        fixed_r_multiple=2.0, min_target_risk_reward=1.5,
    )
    assert result["target_basis"] == "fixed_r"


def test_compute_stop_target_short_direction():
    result = compute_stop_target(
        entry_trigger=100.0, vwap_stop_price=101.0, swing_stop_price=101.5,
        day_extreme_price=90.0, direction="short", stop_buffer_pct=0.0,
        fixed_r_multiple=2.0, min_target_risk_reward=1.5,
    )
    assert result["stop_basis"] == "vwap"
    assert result["stop_price"] == 101.0
    assert result["target_basis"] == "day_extreme"
    assert result["target_price"] == 90.0


def test_compute_stop_target_applies_stop_buffer():
    result = compute_stop_target(
        entry_trigger=100.0, vwap_stop_price=99.0, swing_stop_price=98.5,
        day_extreme_price=110.0, direction="long", stop_buffer_pct=0.01,
        fixed_r_multiple=2.0, min_target_risk_reward=1.5,
    )
    assert result["stop_price"] == 99.0 * 0.99


def test_compute_stop_target_rejects_none_direction():
    try:
        compute_stop_target(100.0, 99.0, 98.5, 110.0, direction="none")
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_detect_breakout_boundary_values():
    assert detect_breakout(close_price=100.0, entry_trigger=100.0, direction="long") is False
    assert detect_breakout(close_price=100.01, entry_trigger=100.0, direction="long") is True
    assert detect_breakout(close_price=99.99, entry_trigger=100.0, direction="short") is True
    assert detect_breakout(close_price=100.0, entry_trigger=100.0, direction="short") is False


def test_detect_halt_proxy_trips_above_threshold():
    bars = _bars([(100.0, 101.0, 99.5, 100.5, 1000), (100.5, 112.0, 100.0, 111.0, 1000)])
    result = detect_halt_proxy(bars, move_pct_threshold=0.10)
    assert result["is_halt_proxy"] is True


def test_detect_halt_proxy_does_not_trip_below_threshold():
    bars = _bars([(100.0, 101.0, 99.5, 100.5, 1000), (100.5, 105.0, 100.0, 104.0, 1000)])
    result = detect_halt_proxy(bars, move_pct_threshold=0.10)
    assert result["is_halt_proxy"] is False


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

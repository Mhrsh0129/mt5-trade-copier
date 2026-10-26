import pytest

from copier.engine.lots import (
    apply_allowed_lots,
    compute_base_lot,
    compute_open_lot,
    compute_partial_close_lot,
    snap_to_step,
)


def test_allowed_lots_exact_and_snap_nearest():
    allowed = [0.01, 0.05, 0.10]
    assert apply_allowed_lots(0.05, allowed, "snap").volume == 0.05
    # 0.04 is closer to 0.05 than 0.01
    r = apply_allowed_lots(0.04, allowed, "snap")
    assert r.ok and r.volume == 0.05
    # 0.02 is closer to 0.01 than 0.05
    assert apply_allowed_lots(0.02, allowed, "snap").volume == 0.01
    # Exact tie (0.03 between 0.01 and 0.05) -> resolves to lower value (0.01)
    assert apply_allowed_lots(0.03, allowed, "snap").volume == 0.01


def test_allowed_lots_skip_mode():
    allowed = [0.01, 0.05, 0.10]
    assert apply_allowed_lots(0.05, allowed, "skip").ok
    r = apply_allowed_lots(0.03, allowed, "skip")
    assert not r.ok and "not in allowed list" in r.reason


def test_snap_to_broker_step_and_bounds():
    assert snap_to_step(0.127, 0.01, 0.01, 50.0).volume == 0.12
    assert snap_to_step(100.0, 0.01, 0.01, 20.0).volume == 20.0
    below = snap_to_step(0.004, 0.01, 0.01, 50.0)
    assert not below.ok and "below broker min" in below.reason


def test_lot_modes_fixed_multiplier_balance():
    assert compute_base_lot(0.5, lot_mode="fixed", lot_value=0.05) == 0.05
    assert compute_base_lot(0.2, lot_mode="multiplier", lot_value=1.5) == pytest.approx(0.3)
    # Balance proportional: slave has 5,000 vs master 10,000, 100% scale -> 0.5x of master lot
    assert compute_base_lot(
        0.4, lot_mode="balance_pct", lot_value=100.0, master_balance=10_000, slave_balance=5_000
    ) == pytest.approx(0.2)


def test_full_open_lot_pipeline():
    # Multiplier gives 0.03, clamped to min_lot=0.04 -> snapped to allowed [0.01, 0.05, 0.10] -> 0.05
    res = compute_open_lot(
        0.03,
        lot_mode="multiplier",
        lot_value=1.0,
        min_lot=0.04,
        max_lot=0.20,
        allowed_lots=[0.01, 0.05, 0.10],
        not_allowed_action="snap",
    )
    assert res.ok and res.volume == 0.05


def test_partial_close_lot_and_min_remainder():
    # 0.10 lot, 50% close -> 0.05
    assert compute_partial_close_lot(0.10, 0.5, broker_min=0.01, broker_step=0.01) == 0.05
    # 0.01 lot (already at broker min), 50% close -> closes full 0.01 so remainder isn't < min
    assert compute_partial_close_lot(0.01, 0.5, broker_min=0.01, broker_step=0.01) == 0.01
    # 0.03 lot with broker_min=0.02, 50% close would leave 0.01 (< 0.02) -> closes full 0.03
    assert compute_partial_close_lot(0.03, 0.5, broker_min=0.02, broker_step=0.01) == 0.03


def test_risk_pct_lot_calculation_and_fallback():
    # Slave balance = $10,000, risk = 1% ($100), SL = 500 points, tick_value_per_lot_per_point = $1.0 -> 0.20 lots
    res = compute_open_lot(
        0.5,
        lot_mode="risk_pct",
        lot_value=1.0,
        slave_balance=10_000.0,
        sl_points=500.0,
        tick_value_per_lot_per_point=1.0,
    )
    assert res.ok and res.volume == 0.20
    # When SL is 0 (no stop-loss), falls back to master_lot
    fallback = compute_open_lot(
        0.15,
        lot_mode="risk_pct",
        lot_value=1.0,
        slave_balance=10_000.0,
        sl_points=0.0,
        tick_value_per_lot_per_point=1.0,
    )
    assert fallback.ok and fallback.volume == 0.15

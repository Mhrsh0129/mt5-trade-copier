"""Lot calculation, allowed-lot snapping, and broker step validation (architecture §5.2).

Pure functions, thoroughly unit-tested in ``tests/test_lots.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_DOWN


@dataclass(frozen=True)
class LotResult:
    ok: bool
    volume: float = 0.0
    reason: str = "ok"


def _decimals(step: float) -> int:
    s = f"{step:.8f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def snap_to_step(volume: float, step: float, vmin: float, vmax: float) -> LotResult:
    """Snap ``volume`` down/nearest to broker ``step`` and clamp to ``[vmin, vmax]``.

    If ``volume`` is below ``vmin`` (by more than floating-point epsilon), skip.
    """
    if step <= 0:
        step = 0.01
    if volume < vmin - 1e-9:
        return LotResult(False, 0.0, f"computed lot {volume:.4f} is below broker min lot {vmin}")
    clamped = min(volume, vmax)
    steps = math.floor((clamped + 1e-9) / step)
    snapped = round(steps * step, _decimals(step))
    if snapped < vmin - 1e-9:
        return LotResult(False, 0.0, f"snapped lot {snapped:.4f} is below broker min lot {vmin}")
    return LotResult(True, snapped, "ok")


def apply_allowed_lots(
    volume: float,
    allowed_lots: list[float] | None,
    not_allowed_action: str = "snap",
) -> LotResult:
    """Validate or snap ``volume`` against an explicit ``allowed_lots`` list.

    * If ``allowed_lots`` is empty/None: pass through unchanged.
    * Exact match (within 1e-7): pass through.
    * ``not_allowed_action == 'skip'``: reject with reason.
    * ``not_allowed_action == 'snap'``: snap to nearest allowed value; ties resolve
      to the **lower** allowed lot for conservative risk.
    """
    if not allowed_lots:
        return LotResult(True, volume, "ok")
    cleaned = sorted({round(float(x), 8) for x in allowed_lots if float(x) > 0})
    if not cleaned:
        return LotResult(True, volume, "ok")

    for a in cleaned:
        if abs(volume - a) <= 1e-7:
            return LotResult(True, a, "ok")

    if not_allowed_action == "skip":
        return LotResult(False, 0.0, f"lot {volume:g} not in allowed list {cleaned}")

    # Snap to nearest; on exact tie choose the smaller lot
    best = min(cleaned, key=lambda a: (round(abs(volume - a), 9), a))
    return LotResult(True, best, "snapped_to_allowed")


def compute_base_lot(
    master_lot: float,
    *,
    lot_mode: str,
    lot_value: float,
    master_balance: float = 0.0,
    slave_balance: float = 0.0,
    sl_points: float = 0.0,
    tick_value_per_lot_per_point: float = 0.0,
) -> float:
    """Compute raw unrounded slave lot before clamps/allowed list/broker step.

    Modes:
    - ``fixed``: always ``lot_value``
    - ``multiplier``: ``master_lot * lot_value``
    - ``balance_pct`` / ``balance_ratio``:
        ``master_lot * (slave_balance / master_balance) * (lot_value / 100.0 if lot_mode == 'balance_pct' else lot_value)``
        (if ``master_balance <= 0``, falls back to ``slave_balance * (lot_value / 100.0) / 1000.0``)
    - ``risk_pct``: risks ``lot_value``% of ``slave_balance`` over ``sl_points``; falls back to ``master_lot`` if no SL.
    """
    mode = (lot_mode or "multiplier").lower()
    if mode == "fixed":
        return max(0.0, float(lot_value))
    if mode == "multiplier":
        return max(0.0, float(master_lot) * float(lot_value))
    if mode in ("balance_pct", "balance_ratio"):
        scale = (float(lot_value) / 100.0) if mode == "balance_pct" else float(lot_value)
        if master_balance > 0 and slave_balance > 0:
            return max(0.0, float(master_lot) * (slave_balance / master_balance) * scale)
        if slave_balance > 0:
            return max(0.0, (slave_balance / 10_000.0) * scale)
        return max(0.0, float(master_lot) * scale)
    if mode == "risk_pct":
        if slave_balance > 0 and sl_points > 0 and tick_value_per_lot_per_point > 0:
            risk_money = slave_balance * (float(lot_value) / 100.0)
            return max(0.0, risk_money / (sl_points * tick_value_per_lot_per_point))
        return max(0.0, float(master_lot))
    return max(0.0, float(master_lot) * float(lot_value))


def compute_open_lot(
    master_lot: float,
    *,
    lot_mode: str = "multiplier",
    lot_value: float = 1.0,
    master_balance: float = 0.0,
    slave_balance: float = 0.0,
    sl_points: float = 0.0,
    tick_value_per_lot_per_point: float = 0.0,
    min_lot: float | None = None,
    max_lot: float | None = None,
    allowed_lots: list[float] | None = None,
    not_allowed_action: str = "snap",
    broker_min: float = 0.01,
    broker_max: float = 100.0,
    broker_step: float = 0.01,
    contract_size_ratio: float = 1.0,
) -> LotResult:
    """Full open-lot pipeline (architecture §5.2):

    1. Base lot from mode (fixed / multiplier / balance_pct / balance_ratio / risk_pct)
    2. Optional contract-size normalization
    3. Clamp to slave ``min_lot`` / ``max_lot`` (if configured)
    4. Apply ``allowed_lots`` list (snap to nearest or skip)
    5. Snap to broker ``volume_step`` and validate ``[broker_min, broker_max]``
    """
    lot = compute_base_lot(
        master_lot,
        lot_mode=lot_mode,
        lot_value=lot_value,
        master_balance=master_balance,
        slave_balance=slave_balance,
        sl_points=sl_points,
        tick_value_per_lot_per_point=tick_value_per_lot_per_point,
    )
    if contract_size_ratio > 0 and abs(contract_size_ratio - 1.0) > 1e-6:
        lot = lot * contract_size_ratio

    # Round to 8 decimal places before comparisons to avoid binary float artifacts like 0.030000000000000006
    lot = float(Decimal(str(round(lot, 8))).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_DOWN))

    if min_lot is not None and min_lot > 0 and lot < min_lot:
        lot = float(min_lot)
    if max_lot is not None and max_lot > 0 and lot > max_lot:
        lot = float(max_lot)

    allowed_res = apply_allowed_lots(lot, allowed_lots, not_allowed_action=not_allowed_action)
    if not allowed_res.ok:
        return allowed_res
    lot = allowed_res.volume

    return snap_to_step(lot, broker_step, broker_min, broker_max)


def compute_partial_close_lot(
    slave_current_volume: float,
    close_ratio: float,
    *,
    broker_min: float = 0.01,
    broker_step: float = 0.01,
) -> float:
    """Compute volume to close on a slave when master partially closes ``close_ratio``.

    As specified in architecture §5.2:
    - The allowed-lots list applies to opens only, never blocking a close.
    - If the remaining volume would fall below ``broker_min``, close the entire slave position.
    """
    if close_ratio >= 1.0 - 1e-7 or slave_current_volume <= broker_min + 1e-9:
        return slave_current_volume
    raw_close = slave_current_volume * max(0.0, min(1.0, close_ratio))
    decs = _decimals(broker_step)
    steps = max(1, round(raw_close / broker_step))
    close_vol = round(steps * broker_step, decs)
    remaining = round(slave_current_volume - close_vol, decs)
    if remaining < broker_min - 1e-9:
        return slave_current_volume
    return min(close_vol, slave_current_volume)

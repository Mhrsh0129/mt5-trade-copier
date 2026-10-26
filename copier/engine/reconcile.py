"""Snapshot reconciliation engine (architecture §6).

Runs on startup, reconnect, internet recovery, or manual "Reconcile now".
Idempotent: running it twice yields the exact same result without duplicates.

Cases handled:
1. Recover any ``pending_send`` write-ahead rows by inspecting live slave positions/pendings by magic.
2. Mapped slave trade whose master position is no longer in master's open positions:
   - If ``master_known_open`` is authoritative (master is connected), close the slave trade ("no missed closes").
3. Mapped slave trade still in master's open positions:
   - Sync SL/TP if drifted.
   - If master partially closed while slave was offline (master volume < mapped master_volume), partially close slave proportionally.
4. Master open position not mapped on slave:
   - Late-copy only if position age <= ``late_copy_max_age_s`` and price drift <= ``late_copy_max_points``; otherwise skip + record reason.
5. Unmapped copier-magic trade on slave whose master position is not open on master (orphan):
   - Always flag in report + alert; auto-close only if ``close_orphans`` is enabled on the slave (off by default).
6. Pending orders: place missing pendings and cancel stale slave pendings whose master order vanished.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..adapters.base import BrokerAdapter, PendingOrder, Position, Side
from .events import EventType, TradeEvent
from .executor import SlaveExecutor
from .ticket_map import (
    decode_magic,
    get_ticket_map,
    is_copier_magic,
    list_open_maps,
    mark_closed_or_failed,
    mark_open,
    resolve_pending_sends,
)

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    slave_id: int
    recovered_pending_sends: list[int] = field(default_factory=list)
    closed_missed: list[int] = field(default_factory=list)
    partial_adjusted: list[int] = field(default_factory=list)
    sltp_synced: list[int] = field(default_factory=list)
    late_copied: list[int] = field(default_factory=list)
    late_skipped: list[tuple[int, str]] = field(default_factory=list)
    orphans_flagged: list[int] = field(default_factory=list)
    orphans_closed: list[int] = field(default_factory=list)
    pendings_placed: list[int] = field(default_factory=list)
    pendings_cancelled: list[int] = field(default_factory=list)


def reconcile_slave(
    executor: SlaveExecutor,
    master_positions: list[Position],
    master_pendings: list[PendingOrder],
    *,
    master_balance: float = 0.0,
    master_connected: bool = True,
    now_ms: int | None = None,
) -> ReconcileReport:
    slave_id = executor.slave_id
    adapter = executor.adapter
    cfg = executor.config
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    report = ReconcileReport(slave_id=slave_id)

    # 1. Recover any pending_send rows by checking live slave positions/pendings by magic
    report.recovered_pending_sends = resolve_pending_sends(slave_id, adapter)

    m_pos_by_id = {p.position_id: p for p in master_positions}
    m_ord_by_id = {o.order_id: o for o in master_pendings}

    slave_positions = adapter.positions()
    slave_pos_by_id = {p.position_id: p for p in slave_positions}

    # Also link any live slave copier position that has a valid copier magic for an active master position
    for sp in slave_positions:
        m_id = decode_magic(sp.magic)
        if m_id is not None and m_id in m_pos_by_id:
            tmap = get_ticket_map(slave_id, m_id, kind="position")
            if tmap is None or tmap.state != "open":
                mp = m_pos_by_id[m_id]
                mark_open(
                    slave_id,
                    m_id,
                    kind="position",
                    slave_position_id=sp.position_id,
                    slave_volume=sp.volume,
                    master_volume=mp.volume,
                    sl=sp.sl,
                    tp=sp.tp,
                )

    open_maps = list_open_maps(slave_id, kind="position")
    mapped_slave_pos_ids: set[int] = set()

    # 2 & 3. Check all mapped positions
    for tmap in open_maps:
        m_id = int(tmap.master_position_id)
        if tmap.slave_position_id:
            mapped_slave_pos_ids.add(int(tmap.slave_position_id))

        # If the slave position itself is already gone on the broker, mark closed
        if tmap.slave_position_id and int(tmap.slave_position_id) not in slave_pos_by_id:
            mark_closed_or_failed(slave_id, m_id, kind="position", state="closed")
            continue

        if m_id not in m_pos_by_id:
            if master_connected:
                # Master closed this position while slave was offline -> close slave immediately
                ev = TradeEvent(
                    seq=0,
                    event_type=EventType.POSITION_CLOSED,
                    symbol=tmap.slave_symbol or "",
                    side="buy",
                    master_position_id=m_id,
                    volume=tmap.master_volume or 0.0,
                    detected_at_ms=now_ms,
                )
                audit = executor._on_position_closed(ev)
                if audit.result in ("ok", "already_closed"):
                    report.closed_missed.append(m_id)
            continue

        # Both master and slave have the position open -> check volume ratio and SL/TP
        mp = m_pos_by_id[m_id]
        sp = slave_pos_by_id.get(int(tmap.slave_position_id)) if tmap.slave_position_id else None
        if sp is None:
            continue

        if tmap.master_volume and mp.volume < tmap.master_volume - 1e-7:
            ratio = (tmap.master_volume - mp.volume) / tmap.master_volume
            ev = TradeEvent(
                seq=0,
                event_type=EventType.POSITION_PARTIAL_CLOSE,
                symbol=mp.symbol,
                side=mp.side.value,
                master_position_id=m_id,
                volume=mp.volume,
                prev_volume=tmap.master_volume,
                close_ratio=ratio,
                sl=mp.sl,
                tp=mp.tp,
                detected_at_ms=now_ms,
            )
            audit = executor._on_partial_close(ev)
            if audit.result == "ok":
                report.partial_adjusted.append(m_id)

        _, expected_sl, expected_tp = executor._effective_side_and_stops(mp.side.value, mp.sl, mp.tp)
        if cfg.copy_sltp and (abs(sp.sl - expected_sl) > 1e-6 or abs(sp.tp - expected_tp) > 1e-6):
            ev = TradeEvent(
                seq=0,
                event_type=EventType.POSITION_SLTP_MODIFIED,
                symbol=mp.symbol,
                side=mp.side.value,
                master_position_id=m_id,
                volume=mp.volume,
                sl=mp.sl,
                tp=mp.tp,
                detected_at_ms=now_ms,
            )
            audit = executor._on_sltp_modified(ev)
            if audit.result == "ok":
                report.sltp_synced.append(m_id)

    # 4. Master open positions that were never copied -> check late-copy window
    for m_id, mp in m_pos_by_id.items():
        tmap = get_ticket_map(slave_id, m_id, kind="position")
        if tmap is not None and tmap.state in ("open", "closed"):
            continue
        age_s = max(0.0, (now_ms - mp.time_msc) / 1000.0) if mp.time_msc else 0.0
        if cfg.late_copy_max_age_s >= 0 and age_s > cfg.late_copy_max_age_s:
            report.late_skipped.append((m_id, f"age {age_s:.0f}s > max {cfg.late_copy_max_age_s}s"))
            continue

        # Check price drift in points on slave symbol if available
        spec, err = executor._resolve_and_check_symbol(mp.symbol)
        if spec is None:
            report.late_skipped.append((m_id, err or "symbol unavailable"))
            continue
        tick = adapter.tick(spec.name)
        if tick is not None and spec.point > 0 and mp.price_open > 0:
            cur_price = tick.ask if mp.side is Side.BUY else tick.bid
            drift_pts = abs(cur_price - mp.price_open) / spec.point
            if cfg.late_copy_max_points >= 0 and drift_pts > cfg.late_copy_max_points:
                report.late_skipped.append(
                    (m_id, f"price drift {drift_pts:.1f} pts > max {cfg.late_copy_max_points} pts")
                )
                continue

        ev = TradeEvent(
            seq=0,
            event_type=EventType.POSITION_OPENED,
            symbol=mp.symbol,
            side=mp.side.value,
            master_position_id=m_id,
            volume=mp.volume,
            price=mp.price_open,
            sl=mp.sl,
            tp=mp.tp,
            master_balance=master_balance,
            master_time_msc=mp.time_msc,
            detected_at_ms=now_ms,
        )
        audit = executor._on_position_opened(ev)
        if audit.result == "ok":
            report.late_copied.append(m_id)
        else:
            report.late_skipped.append((m_id, audit.detail or audit.result))

    # 5. Detect orphan copier positions on slave (have copier magic, but no matching open master position)
    refreshed_positions = adapter.positions()
    for sp in refreshed_positions:
        if not is_copier_magic(sp.magic):
            continue
        m_id = decode_magic(sp.magic)
        if m_id is not None and m_id in m_pos_by_id:
            continue
        report.orphans_flagged.append(sp.position_id)
        executor._alert(
            f"orphan:{slave_id}:{sp.position_id}",
            f"Slave #{slave_id}: orphaned trade #{sp.position_id} ({sp.symbol} {sp.volume} lots, master #{m_id})",
        )
        if cfg.close_orphans:
            res = adapter.close(sp.position_id, deviation=cfg.deviation_points)
            if res.ok:
                report.orphans_closed.append(sp.position_id)

    # 6. Reconcile pending orders
    open_pend_maps = list_open_maps(slave_id, kind="pending")
    for pmap in open_pend_maps:
        m_oid = int(pmap.master_position_id)
        if m_oid not in m_ord_by_id and master_connected:
            ev = TradeEvent(
                seq=0,
                event_type=EventType.PENDING_DELETED,
                symbol=pmap.slave_symbol or "",
                side="buy",
                master_order_id=m_oid,
                detected_at_ms=now_ms,
            )
            audit = executor._on_pending_deleted(ev)
            if audit.result in ("ok", "already_closed"):
                report.pendings_cancelled.append(m_oid)

    if cfg.copy_pending:
        for m_oid, mo in m_ord_by_id.items():
            pmap = get_ticket_map(slave_id, m_oid, kind="pending")
            if pmap is not None and pmap.state == "open":
                continue
            ev = TradeEvent(
                seq=0,
                event_type=EventType.PENDING_PLACED,
                symbol=mo.symbol,
                side=mo.type.side.value,
                master_order_id=m_oid,
                pending_type=mo.type.value,
                volume=mo.volume,
                price=mo.price,
                stop_limit=mo.stop_limit,
                sl=mo.sl,
                tp=mo.tp,
                expiration=mo.expiration,
                master_balance=master_balance,
                master_time_msc=mo.time_setup_msc,
                detected_at_ms=now_ms,
            )
            audit = executor._on_pending_placed(ev)
            if audit.result == "ok":
                report.pendings_placed.append(m_oid)

    return report

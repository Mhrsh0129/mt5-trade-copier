"""Master snapshot differ (architecture §4).

Compares two consecutive snapshots of master positions and pending orders and
emits normalized :class:`TradeEvent` records with monotonically increasing
sequence numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..adapters.base import PendingOrder, Position
from .events import EventType, TradeEvent

_VOL_EPS = 1e-7
_PRICE_EPS = 1e-7


@dataclass
class SnapshotState:
    positions: dict[int, Position] = field(default_factory=dict)
    pendings: dict[int, PendingOrder] = field(default_factory=dict)


class SnapshotDiffer:
    def __init__(self, initial_seq: int = 0) -> None:
        self.seq = initial_seq
        self.state: SnapshotState | None = None

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def seed(self, positions: list[Position], pendings: list[PendingOrder]) -> None:
        """Seed baseline state without emitting open events (used on startup before reconcile)."""
        self.state = SnapshotState(
            positions={p.position_id: p for p in positions},
            pendings={o.order_id: o for o in pendings},
        )

    def diff(
        self,
        positions: list[Position],
        pendings: list[PendingOrder],
        *,
        master_balance: float = 0.0,
        master_equity: float = 0.0,
        detected_at_ms: int | None = None,
    ) -> list[TradeEvent]:
        now_ms = detected_at_ms if detected_at_ms is not None else int(time.time() * 1000)
        cur_pos = {p.position_id: p for p in positions}
        cur_ord = {o.order_id: o for o in pendings}

        if self.state is None:
            self.seed(positions, pendings)
            return []

        prev_pos = self.state.positions
        prev_ord = self.state.pendings
        events: list[TradeEvent] = []

        # 1. Detect vanished pending orders before new positions so we can tell if a pending filled.
        vanished_orders: dict[int, PendingOrder] = {
            oid: o for oid, o in prev_ord.items() if oid not in cur_ord
        }
        filled_order_ids: set[int] = set()

        # 2. New positions
        for pid, p in cur_pos.items():
            if pid not in prev_pos:
                # In MT5 hedging, a filled pending order creates a position with position_id == order_id
                from_pending = vanished_orders.get(pid)
                if from_pending is not None:
                    filled_order_ids.add(pid)
                    events.append(TradeEvent(
                        seq=self._next_seq(),
                        event_type=EventType.PENDING_FILLED,
                        symbol=p.symbol,
                        side=p.side.value,
                        master_position_id=p.position_id,
                        master_order_id=from_pending.order_id,
                        pending_type=from_pending.type.value,
                        volume=p.volume,
                        price=p.price_open,
                        sl=p.sl,
                        tp=p.tp,
                        master_balance=master_balance,
                        master_equity=master_equity,
                        master_time_msc=p.time_msc,
                        detected_at_ms=now_ms,
                    ))
                else:
                    events.append(TradeEvent(
                        seq=self._next_seq(),
                        event_type=EventType.POSITION_OPENED,
                        symbol=p.symbol,
                        side=p.side.value,
                        master_position_id=p.position_id,
                        volume=p.volume,
                        price=p.price_open,
                        sl=p.sl,
                        tp=p.tp,
                        master_balance=master_balance,
                        master_equity=master_equity,
                        master_time_msc=p.time_msc,
                        detected_at_ms=now_ms,
                    ))

        # 3. Existing positions: volume changes or SL/TP modifications
        for pid, p in cur_pos.items():
            old = prev_pos.get(pid)
            if old is None:
                continue
            if p.volume < old.volume - _VOL_EPS:
                closed_vol = round(old.volume - p.volume, 8)
                ratio = closed_vol / old.volume if old.volume > 0 else 1.0
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.POSITION_PARTIAL_CLOSE,
                    symbol=p.symbol,
                    side=p.side.value,
                    master_position_id=p.position_id,
                    volume=p.volume,
                    prev_volume=old.volume,
                    close_ratio=ratio,
                    price=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=p.time_msc,
                    detected_at_ms=now_ms,
                ))
            elif p.volume > old.volume + _VOL_EPS:
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.POSITION_INCREASED,
                    symbol=p.symbol,
                    side=p.side.value,
                    master_position_id=p.position_id,
                    volume=round(p.volume - old.volume, 8),
                    prev_volume=old.volume,
                    price=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=p.time_msc,
                    detected_at_ms=now_ms,
                ))

            if abs(p.sl - old.sl) > _PRICE_EPS or abs(p.tp - old.tp) > _PRICE_EPS:
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.POSITION_SLTP_MODIFIED,
                    symbol=p.symbol,
                    side=p.side.value,
                    master_position_id=p.position_id,
                    volume=p.volume,
                    price=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=p.time_msc,
                    detected_at_ms=now_ms,
                ))

        # 4. Closed positions
        for pid, old in prev_pos.items():
            if pid not in cur_pos:
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.POSITION_CLOSED,
                    symbol=old.symbol,
                    side=old.side.value,
                    master_position_id=old.position_id,
                    volume=old.volume,
                    prev_volume=old.volume,
                    close_ratio=1.0,
                    price=old.price_open,
                    sl=old.sl,
                    tp=old.tp,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=old.time_msc,
                    detected_at_ms=now_ms,
                ))

        # 5. Pending orders: new, modified, deleted (not filled)
        for oid, o in cur_ord.items():
            old_o = prev_ord.get(oid)
            if old_o is None:
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.PENDING_PLACED,
                    symbol=o.symbol,
                    side=o.type.side.value,
                    master_order_id=o.order_id,
                    pending_type=o.type.value,
                    volume=o.volume,
                    price=o.price,
                    stop_limit=o.stop_limit,
                    sl=o.sl,
                    tp=o.tp,
                    expiration=o.expiration,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=o.time_setup_msc,
                    detected_at_ms=now_ms,
                ))
            elif (
                abs(o.price - old_o.price) > _PRICE_EPS
                or abs(o.sl - old_o.sl) > _PRICE_EPS
                or abs(o.tp - old_o.tp) > _PRICE_EPS
                or abs(o.stop_limit - old_o.stop_limit) > _PRICE_EPS
                or o.expiration != old_o.expiration
            ):
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.PENDING_MODIFIED,
                    symbol=o.symbol,
                    side=o.type.side.value,
                    master_order_id=o.order_id,
                    pending_type=o.type.value,
                    volume=o.volume,
                    price=o.price,
                    stop_limit=o.stop_limit,
                    sl=o.sl,
                    tp=o.tp,
                    expiration=o.expiration,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=o.time_setup_msc,
                    detected_at_ms=now_ms,
                ))

        for oid, old_o in vanished_orders.items():
            if oid not in filled_order_ids:
                events.append(TradeEvent(
                    seq=self._next_seq(),
                    event_type=EventType.PENDING_DELETED,
                    symbol=old_o.symbol,
                    side=old_o.type.side.value,
                    master_order_id=old_o.order_id,
                    pending_type=old_o.type.value,
                    volume=old_o.volume,
                    price=old_o.price,
                    sl=old_o.sl,
                    tp=old_o.tp,
                    master_balance=master_balance,
                    master_equity=master_equity,
                    master_time_msc=old_o.time_setup_msc,
                    detected_at_ms=now_ms,
                ))

        self.state = SnapshotState(positions=cur_pos, pendings=cur_ord)
        return events

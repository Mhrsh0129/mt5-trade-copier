"""Master-ticket to slave-ticket persistence, magic-number encoding, and write-ahead recovery (§5.1).

Every copied order/position carries:
- ``magic``: ``MAGIC_PREFIX + master_position_id`` (or ``master_order_id`` for pendings)
- ``comment``: ``TC|<master_id>``

Because brokers can truncate or overwrite ``comment`` on partial close or server
side operations, ``magic`` is the primary on-broker link back to the master
ticket. The ``ticket_map`` table is written in state ``pending_send`` *before*
``order_send`` so a crash between broker execution and DB commit can be
reconciled idempotently without ever sending a duplicate trade.
"""

from __future__ import annotations

from sqlalchemy import select

from ..adapters.base import BrokerAdapter
from ..db import TicketMap, session_scope
from ..db.models import utcnow

# High namespace prefix so copier magic numbers never collide with manual trades (magic=0)
# or typical retail EAs. Fits comfortably inside signed 64-bit integer.
MAGIC_PREFIX = 700_000_000_000


def encode_magic(master_ticket: int) -> int:
    return MAGIC_PREFIX + int(master_ticket)


def is_copier_magic(magic: int | None) -> bool:
    return bool(magic is not None and magic > MAGIC_PREFIX)


def decode_magic(magic: int | None) -> int | None:
    if not is_copier_magic(magic):
        return None
    return int(magic) - MAGIC_PREFIX


def format_comment(master_ticket: int) -> str:
    return f"TC|{int(master_ticket)}"[:31]


# ------------------------------------------------------------------ DB operations
def get_ticket_map(slave_id: int, master_id: int, kind: str = "position") -> TicketMap | None:
    with session_scope() as s:
        return s.scalar(
            select(TicketMap).where(
                TicketMap.slave_id == slave_id,
                TicketMap.master_position_id == master_id,
                TicketMap.kind == kind,
            )
        )


def list_open_maps(slave_id: int, kind: str = "position") -> list[TicketMap]:
    with session_scope() as s:
        return list(
            s.scalars(
                select(TicketMap).where(
                    TicketMap.slave_id == slave_id,
                    TicketMap.kind == kind,
                    TicketMap.state.in_(("open", "pending_send")),
                )
            )
        )


def begin_send(
    slave_id: int,
    master_id: int,
    *,
    kind: str = "position",
    slave_symbol: str,
    master_volume: float,
    slave_volume: float,
    sl: float = 0.0,
    tp: float = 0.0,
    master_order_id: int | None = None,
) -> TicketMap:
    """Write-ahead reservation before calling ``order_send``."""
    with session_scope() as s:
        row = s.scalar(
            select(TicketMap).where(
                TicketMap.slave_id == slave_id,
                TicketMap.master_position_id == master_id,
                TicketMap.kind == kind,
            )
        )
        if row is None:
            row = TicketMap(
                slave_id=slave_id,
                kind=kind,
                master_position_id=master_id,
                master_order_id=master_order_id,
                slave_symbol=slave_symbol,
                state="pending_send",
                master_volume=master_volume,
                slave_volume=slave_volume,
                sl=sl,
                tp=tp,
            )
            s.add(row)
        else:
            row.slave_symbol = slave_symbol
            row.state = "pending_send"
            row.master_volume = master_volume
            row.slave_volume = slave_volume
            row.sl = sl
            row.tp = tp
            row.updated_at = utcnow()
        s.flush()
        return row


def mark_open(
    slave_id: int,
    master_id: int,
    *,
    kind: str = "position",
    slave_position_id: int | None = None,
    slave_order_id: int | None = None,
    slave_volume: float | None = None,
    master_volume: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
) -> None:
    with session_scope() as s:
        row = s.scalar(
            select(TicketMap).where(
                TicketMap.slave_id == slave_id,
                TicketMap.master_position_id == master_id,
                TicketMap.kind == kind,
            )
        )
        if row is None:
            row = TicketMap(
                slave_id=slave_id,
                kind=kind,
                master_position_id=master_id,
                state="open",
                slave_position_id=slave_position_id,
                slave_order_id=slave_order_id,
                slave_volume=slave_volume,
                master_volume=master_volume,
                sl=sl,
                tp=tp,
            )
            s.add(row)
        else:
            row.state = "open"
            if slave_position_id is not None:
                row.slave_position_id = slave_position_id
            if slave_order_id is not None:
                row.slave_order_id = slave_order_id
            if slave_volume is not None:
                row.slave_volume = slave_volume
            if master_volume is not None:
                row.master_volume = master_volume
            if sl is not None:
                row.sl = sl
            if tp is not None:
                row.tp = tp
            row.updated_at = utcnow()


def mark_closed_or_failed(
    slave_id: int,
    master_id: int,
    *,
    kind: str = "position",
    state: str = "closed",
) -> None:
    with session_scope() as s:
        row = s.scalar(
            select(TicketMap).where(
                TicketMap.slave_id == slave_id,
                TicketMap.master_position_id == master_id,
                TicketMap.kind == kind,
            )
        )
        if row is not None:
            row.state = state
            row.updated_at = utcnow()


def resolve_pending_sends(slave_id: int, adapter: BrokerAdapter) -> list[int]:
    """Recover any ``pending_send`` rows after a crash by inspecting live slave positions/pendings by magic.

    Returns the list of ``master_position_id`` values that were found already open on the broker
    and promoted to ``open`` (preventing duplicate resend). Rows not found on the broker are
    marked ``failed`` so reconciliation can decide whether to late-copy.
    """
    recovered: list[int] = []
    positions_by_magic = {
        decode_magic(p.magic): p for p in adapter.positions() if is_copier_magic(p.magic)
    }
    orders_by_magic = {
        decode_magic(o.magic): o for o in adapter.pending_orders() if is_copier_magic(o.magic)
    }
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(TicketMap).where(
                    TicketMap.slave_id == slave_id,
                    TicketMap.state == "pending_send",
                )
            )
        )
        for row in rows:
            if row.kind == "position" and row.master_position_id in positions_by_magic:
                p = positions_by_magic[row.master_position_id]
                row.state = "open"
                row.slave_position_id = p.position_id
                row.slave_volume = p.volume
                row.sl = p.sl
                row.tp = p.tp
                row.updated_at = utcnow()
                recovered.append(row.master_position_id)
            elif row.kind == "pending" and row.master_position_id in orders_by_magic:
                o = orders_by_magic[row.master_position_id]
                row.state = "open"
                row.slave_order_id = o.order_id
                row.slave_volume = o.volume
                row.sl = o.sl
                row.tp = o.tp
                row.updated_at = utcnow()
                recovered.append(row.master_position_id)
            else:
                row.state = "failed"
                row.updated_at = utcnow()
    return recovered

"""Master and Slave runtime loops (architecture §4, §5)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Callable

from ..adapters.base import BrokerAdapter, PendingOrder, PendingType, Position, Side
from ..bus import Publisher, Subscriber
from ..config import MASTER_POLL_MS, SNAPSHOT_EVERY_S
from ..db import AppSetting, session_scope
from .differ import SnapshotDiffer
from .events import EventType, TradeEvent
from .executor import SlaveExecutor
from .reconcile import ReconcileReport, reconcile_slave

log = logging.getLogger(__name__)


def _pos_to_dict(p: Position) -> dict:
    d = asdict(p)
    d["side"] = p.side.value
    return d


def _pos_from_dict(d: dict) -> Position:
    data = dict(d)
    data["side"] = Side(data["side"])
    return Position(**data)


def _ord_to_dict(o: PendingOrder) -> dict:
    d = asdict(o)
    d["type"] = o.type.value
    return d


def _ord_from_dict(d: dict) -> PendingOrder:
    data = dict(d)
    data["type"] = PendingType(data["type"])
    return PendingOrder(**data)


def save_master_snapshot_db(positions: list[Position], pendings: list[PendingOrder],
                            balance: float = 0.0, equity: float = 0.0) -> None:
    payload = json.dumps({
        "ts_ms": int(time.time() * 1000),
        "balance": balance,
        "equity": equity,
        "positions": [_pos_to_dict(p) for p in positions],
        "pendings": [_ord_to_dict(o) for o in pendings],
    })
    with session_scope() as s:
        s.merge(AppSetting(key="last_master_snapshot", value=payload))


def load_master_snapshot_db() -> tuple[list[Position], list[PendingOrder], float, int] | None:
    with session_scope() as s:
        row = s.get(AppSetting, "last_master_snapshot")
        if row is None or not row.value:
            return None
        try:
            data = json.loads(row.value)
            pos = [_pos_from_dict(x) for x in data.get("positions", [])]
            ords = [_ord_from_dict(x) for x in data.get("pendings", [])]
            return pos, ords, float(data.get("balance", 0.0)), int(data.get("ts_ms", 0))
        except Exception:  # noqa: BLE001
            return None


class MasterPoller:
    """Polls master positions & pending orders every ``poll_ms`` and publishes events + periodic snapshots."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        publisher: Publisher,
        *,
        poll_ms: int = MASTER_POLL_MS,
        snapshot_every_s: float = SNAPSHOT_EVERY_S,
    ) -> None:
        self.adapter = adapter
        self.publisher = publisher
        self.poll_ms = poll_ms
        self.snapshot_every_s = snapshot_every_s
        self.differ = SnapshotDiffer()
        self._last_snapshot_ts = 0.0

    def seed_from_db_or_live(self) -> None:
        saved = load_master_snapshot_db()
        if saved is not None:
            pos, ords, _, _ = saved
            self.differ.seed(pos, ords)
        else:
            self.differ.seed(self.adapter.positions(), self.adapter.pending_orders())

    async def poll_once(self) -> list[TradeEvent]:
        info = self.adapter.account_info()
        positions = self.adapter.positions()
        pendings = self.adapter.pending_orders()
        events = self.differ.diff(
            positions,
            pendings,
            master_balance=info.balance,
            master_equity=info.equity,
        )
        for ev in events:
            await self.publisher.publish("trade_event", ev.to_dict())

        now = time.monotonic()
        if events or (now - self._last_snapshot_ts) >= self.snapshot_every_s:
            self._last_snapshot_ts = now
            save_master_snapshot_db(positions, pendings, info.balance, info.equity)
            await self.publisher.publish(
                "snapshot",
                {
                    "seq": self.differ.seq,
                    "balance": info.balance,
                    "equity": info.equity,
                    "positions": [_pos_to_dict(p) for p in positions],
                    "pendings": [_ord_to_dict(o) for o in pendings],
                },
            )
        return events

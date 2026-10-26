"""Slave subscriber loop: consumes trade events, snapshots, and control messages."""

from __future__ import annotations

import logging
from typing import Any

from ..bus import BusMessage
from .events import TradeEvent
from .executor import SlaveExecutor
from .master import _ord_from_dict, _pos_from_dict, load_master_snapshot_db
from .reconcile import ReconcileReport, reconcile_slave

log = logging.getLogger(__name__)


class SlaveSubscriber:
    """Wraps :class:`SlaveExecutor` with sequence-gap detection and snapshot reconciliation."""

    def __init__(self, executor: SlaveExecutor) -> None:
        self.executor = executor
        self.last_seq: int | None = None
        self.needs_reconcile: bool = True

    def on_bus_message(self, msg: BusMessage) -> Any:
        topic = msg.topic
        payload = msg.payload

        if topic == "control":
            return self._handle_control(payload)

        if topic == "trade_event":
            ev = TradeEvent.from_dict(payload)
            if self.last_seq is not None and ev.seq > self.last_seq + 1:
                log.warning(
                    "Slave %s detected sequence gap (%s -> %s); flagging for reconcile on next snapshot",
                    self.executor.slave_id,
                    self.last_seq,
                    ev.seq,
                )
                self.needs_reconcile = True
            self.last_seq = ev.seq
            return self.executor.handle_event(ev)

        if topic == "snapshot":
            return self._handle_snapshot(payload)

        return None

    def _handle_control(self, payload: dict[str, Any]) -> Any:
        cmd = payload.get("cmd")
        target = payload.get("slave_id")
        if target is not None and int(target) != self.executor.slave_id:
            return None
        if cmd == "config_changed":
            self.executor.reload_config()
        elif cmd == "global_pause":
            was_paused = self.executor.global_paused
            self.executor.global_paused = bool(payload.get("paused", True))
            if was_paused and not self.executor.global_paused:
                self.needs_reconcile = True
                return self._reconcile_from_saved_snapshot()
        elif cmd == "pause":
            self.executor.reload_config()
        elif cmd == "resume":
            self.executor.reload_config()
            self.needs_reconcile = True
        elif cmd == "reconcile_now":
            self.needs_reconcile = True
            return self._reconcile_from_saved_snapshot()
        return None

    def _reconcile_from_saved_snapshot(self) -> ReconcileReport | None:
        if self.executor.global_paused or self.executor.config.paused:
            return None
        saved = load_master_snapshot_db()
        if saved is None:
            return None
        pos, ords, bal, _ = saved
        self.needs_reconcile = False
        return reconcile_slave(
            self.executor,
            pos,
            ords,
            master_balance=bal,
            master_connected=True,
        )

    def _handle_snapshot(self, payload: dict[str, Any]) -> ReconcileReport | None:
        if self.executor.global_paused or self.executor.config.paused:
            return None
        seq = int(payload.get("seq", 0))
        positions = [_pos_from_dict(x) for x in payload.get("positions", [])]
        pendings = [_ord_from_dict(x) for x in payload.get("pendings", [])]
        balance = float(payload.get("balance", 0.0))
        force = bool(payload.get("force_reconcile", False))

        if force or self.needs_reconcile or self.last_seq is None or seq != self.last_seq:
            self.last_seq = seq
            self.needs_reconcile = False
            return reconcile_slave(
                self.executor,
                positions,
                pendings,
                master_balance=balance,
                master_connected=True,
            )
        return None

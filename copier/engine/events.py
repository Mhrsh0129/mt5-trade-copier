"""Engine events and snapshots (architecture §4)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_PARTIAL_CLOSE = "position_partial_close"
    POSITION_INCREASED = "position_increased"
    POSITION_SLTP_MODIFIED = "position_sltp_modified"
    PENDING_PLACED = "pending_placed"
    PENDING_MODIFIED = "pending_modified"
    PENDING_DELETED = "pending_deleted"
    PENDING_FILLED = "pending_filled"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class TradeEvent:
    seq: int
    event_type: EventType
    symbol: str
    side: str  # "buy" | "sell"
    master_position_id: int | None = None
    master_order_id: int | None = None
    pending_type: str | None = None
    volume: float = 0.0
    prev_volume: float | None = None
    close_ratio: float | None = None
    price: float = 0.0
    stop_limit: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    expiration: int = 0
    close_reason: str | None = None
    master_balance: float = 0.0
    master_equity: float = 0.0
    master_time_msc: int = 0
    detected_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TradeEvent":
        data = dict(d)
        data["event_type"] = EventType(data["event_type"])
        return cls(**data)

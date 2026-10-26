from copier.adapters.base import PendingOrder, PendingType, Position, Side
from copier.engine.differ import SnapshotDiffer
from copier.engine.events import EventType


def _pos(pid: int, vol: float = 0.2, sl: float = 0.0, tp: float = 0.0) -> Position:
    return Position(
        position_id=pid,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=vol,
        price_open=2300.0,
        sl=sl,
        tp=tp,
        magic=0,
        comment="",
        time_msc=1000,
    )


def _ord(oid: int, price: float = 2290.0, sl: float = 0.0) -> PendingOrder:
    return PendingOrder(
        order_id=oid,
        symbol="XAUUSD",
        type=PendingType.BUY_LIMIT,
        volume=0.2,
        price=price,
        stop_limit=0.0,
        sl=sl,
        tp=0.0,
        expiration=0,
        magic=0,
        comment="",
        time_setup_msc=1000,
    )


def test_differ_open_modify_partial_close():
    d = SnapshotDiffer()
    assert d.diff([], []) == []  # initial seed

    evs = d.diff([_pos(10, vol=0.2)], [])
    assert len(evs) == 1 and evs[0].event_type is EventType.POSITION_OPENED and evs[0].seq == 1

    # Modify SL/TP
    evs = d.diff([_pos(10, vol=0.2, sl=2280.0, tp=2350.0)], [])
    assert len(evs) == 1 and evs[0].event_type is EventType.POSITION_SLTP_MODIFIED

    # Partial close 0.2 -> 0.05 (75% closed)
    evs = d.diff([_pos(10, vol=0.05, sl=2280.0, tp=2350.0)], [])
    assert len(evs) == 1 and evs[0].event_type is EventType.POSITION_PARTIAL_CLOSE
    assert abs(evs[0].close_ratio - 0.75) < 1e-6

    # Full close
    evs = d.diff([], [])
    assert len(evs) == 1 and evs[0].event_type is EventType.POSITION_CLOSED


def test_differ_pending_lifecycle_and_fill():
    d = SnapshotDiffer()
    d.seed([], [])

    # Place two pendings (#50 and #51)
    evs = d.diff([], [_ord(50, price=2290.0), _ord(51, price=2285.0)])
    assert [e.event_type for e in evs] == [EventType.PENDING_PLACED, EventType.PENDING_PLACED]

    # Modify #50 price
    evs = d.diff([], [_ord(50, price=2291.0), _ord(51, price=2285.0)])
    assert len(evs) == 1 and evs[0].event_type is EventType.PENDING_MODIFIED

    # #50 fills (becomes position #50) and #51 is deleted
    evs = d.diff([_pos(50, vol=0.2)], [])
    by_type = {e.event_type: e for e in evs}
    assert EventType.PENDING_FILLED in by_type
    assert by_type[EventType.PENDING_FILLED].master_position_id == 50
    assert EventType.PENDING_DELETED in by_type
    assert by_type[EventType.PENDING_DELETED].master_order_id == 51

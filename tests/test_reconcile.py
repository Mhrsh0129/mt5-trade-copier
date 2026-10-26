import time

from copier import accounts
from copier.adapters.base import ExecCode, MarginMode, OpenRequest, PendingType, Position, Side
from copier.adapters.fake_adapter import FakeAdapter
from copier.engine.events import EventType, TradeEvent
from copier.engine.executor import SlaveExecutor
from copier.engine.reconcile import reconcile_slave
from copier.engine.rules import save_slave_settings, upsert_symbol_map, upsert_symbol_rule
from copier.engine.ticket_map import encode_magic, format_comment, get_ticket_map


def _make_slave(fake_template, *, login: int = 201, mode: MarginMode = MarginMode.HEDGING):
    accounts.add_template("BrokerX", str(fake_template))
    sid = accounts.add_account(
        role="slave", broker="BrokerX", server="S", login=login, password="pw", provision=False
    )
    adapter = FakeAdapter(login=login, server="S", margin_mode=mode)
    adapter.add_symbol("EURUSD.m", 1.1000, 1.1002)
    adapter.add_symbol("GOLD", 2300.00, 2300.40, digits=2, contract=100.0)
    return sid, adapter


def test_executor_open_partial_modify_close_with_rules(db, fake_template):
    sid, adapter = _make_slave(fake_template)
    upsert_symbol_map(sid, "XAUUSD", "GOLD")
    upsert_symbol_rule(
        sid, "XAUUSD", enabled=True, lot_mode="multiplier", lot_value=1.0,
        allowed_lots=[0.01, 0.05, 0.10], not_allowed_action="snap"
    )
    ex = SlaveExecutor(sid, adapter, retry_delay_s=0)

    # Open 0.04 on master -> snaps to 0.05 on slave GOLD
    ev_open = TradeEvent(
        seq=1, event_type=EventType.POSITION_OPENED, symbol="XAUUSD", side="buy",
        master_position_id=701, volume=0.04, price=2300.0, sl=2290.0, tp=2320.0
    )
    audit = ex.handle_event(ev_open)
    assert audit.result == "ok" and audit.filled_lot == 0.05
    assert len(adapter.positions()) == 1
    pos = adapter.positions()[0]
    assert pos.symbol == "GOLD" and pos.volume == 0.05 and pos.sl == 2290.0

    # Duplicate event for same master position is skipped (idempotent)
    dup = ex.handle_event(ev_open)
    assert dup.result == "skipped" and len(adapter.positions()) == 1

    # Modify SL/TP
    ev_mod = TradeEvent(
        seq=2, event_type=EventType.POSITION_SLTP_MODIFIED, symbol="XAUUSD", side="buy",
        master_position_id=701, volume=0.04, sl=2295.0, tp=2330.0
    )
    assert ex.handle_event(ev_mod).result == "ok"
    assert adapter.positions()[0].sl == 2295.0

    # Partial close 40% -> 0.05 * 0.4 = 0.02 closed, 0.03 remaining
    ev_part = TradeEvent(
        seq=3, event_type=EventType.POSITION_PARTIAL_CLOSE, symbol="XAUUSD", side="buy",
        master_position_id=701, volume=0.024, prev_volume=0.04, close_ratio=0.4
    )
    assert ex.handle_event(ev_part).result == "ok"
    assert adapter.positions()[0].volume == 0.03

    # Full close
    ev_close = TradeEvent(
        seq=4, event_type=EventType.POSITION_CLOSED, symbol="XAUUSD", side="buy",
        master_position_id=701, volume=0.024
    )
    assert ex.handle_event(ev_close).result == "ok"
    assert adapter.positions() == []
    assert get_ticket_map(sid, 701).state == "closed"


def test_executor_reverse_and_retry_on_requote(db, fake_template):
    sid, adapter = _make_slave(fake_template, login=202)
    save_slave_settings(sid, reverse=True)
    ex = SlaveExecutor(sid, adapter, retry_delay_s=0)

    # Script one REQUOTE before succeeding
    adapter.fail_next(ExecCode.REQUOTE)
    ev = TradeEvent(
        seq=1, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=801, volume=0.10, price=1.1002, sl=1.0950, tp=1.1100
    )
    audit = ex.handle_event(ev)
    assert audit.result == "ok"
    pos = adapter.positions()[0]
    # Reversed: BUY became SELL, SL and TP swapped
    assert pos.symbol == "EURUSD.m" and pos.side is Side.SELL
    assert pos.sl == 1.1100 and pos.tp == 1.0950


def test_reconcile_catches_missed_close_late_copy_and_orphans(db, fake_template):
    sid, adapter = _make_slave(fake_template, login=203)
    ex = SlaveExecutor(sid, adapter, retry_delay_s=0)
    now_ms = int(time.time() * 1000)

    # 1. Open master #901 via executor
    ex.handle_event(TradeEvent(
        seq=1, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=901, volume=0.10, price=1.1002, detected_at_ms=now_ms
    ))
    # 2. Place an orphan trade on the slave (copier magic for master #999 which is not on master)
    orphan_res = adapter.market_open(
        OpenRequest("EURUSD.m", Side.BUY, 0.05, magic=encode_magic(999), comment=format_comment(999))
    )

    # Now master snapshot has:
    # - #901 is GONE (closed while slave was offline)
    # - #902 is a brand-new fresh position (opened 5s ago at 1.1001) -> should late-copy
    # - #903 is an old position (opened 20 min ago) -> should skip late-copy
    m_positions = [
        Position(
            position_id=902, symbol="EURUSD", side=Side.BUY, volume=0.10, price_open=1.1001,
            sl=0.0, tp=0.0, magic=0, comment="", time_msc=now_ms - 5_000
        ),
        Position(
            position_id=903, symbol="EURUSD", side=Side.BUY, volume=0.10, price_open=1.1001,
            sl=0.0, tp=0.0, magic=0, comment="", time_msc=now_ms - 1_200_000
        ),
    ]

    rep = reconcile_slave(ex, m_positions, [], master_connected=True, now_ms=now_ms)
    assert rep.closed_missed == [901]
    assert rep.late_copied == [902]
    assert [x[0] for x in rep.late_skipped] == [903]
    assert rep.orphans_flagged == [orphan_res.position_id]
    assert rep.orphans_closed == []  # close_orphans is off by default

    # Running reconcile a second time is a no-op (idempotent: no duplicates)
    rep2 = reconcile_slave(ex, m_positions, [], master_connected=True, now_ms=now_ms)
    assert rep2.closed_missed == [] and rep2.late_copied == []
    assert len(adapter.positions()) == 2  # #902 copy + the untouched orphan

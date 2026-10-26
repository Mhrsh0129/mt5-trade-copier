from copier import accounts
from copier.adapters.base import OpenRequest, Side
from copier.adapters.fake_adapter import FakeAdapter
from copier.engine.ticket_map import (
    begin_send,
    decode_magic,
    encode_magic,
    format_comment,
    get_ticket_map,
    is_copier_magic,
    mark_open,
    resolve_pending_sends,
)


def test_magic_and_comment_encoding():
    m = encode_magic(987654321)
    assert is_copier_magic(m)
    assert decode_magic(m) == 987654321
    assert not is_copier_magic(0)
    assert not is_copier_magic(12345)
    assert decode_magic(12345) is None
    assert format_comment(987654321) == "TC|987654321"


def test_write_ahead_recovery_prevents_duplicates(db, fake_template):
    accounts.add_template("BrokerX", str(fake_template))
    slave_id = accounts.add_account(
        role="slave", broker="BrokerX", server="S", login=101, password="pw", provision=False
    )

    # Simulate two write-ahead reservations before a crash:
    # - Master #5001 actually reached the broker before the crash
    # - Master #5002 never reached the broker
    begin_send(slave_id, 5001, slave_symbol="EURUSD", master_volume=0.1, slave_volume=0.1)
    begin_send(slave_id, 5002, slave_symbol="EURUSD", master_volume=0.2, slave_volume=0.2)

    adapter = FakeAdapter()
    adapter.add_symbol("EURUSD", 1.1000, 1.1002)
    res = adapter.market_open(
        OpenRequest("EURUSD", Side.BUY, 0.1, magic=encode_magic(5001), comment=format_comment(5001))
    )

    recovered = resolve_pending_sends(slave_id, adapter)
    assert recovered == [5001]

    m1 = get_ticket_map(slave_id, 5001)
    assert m1.state == "open" and m1.slave_position_id == res.position_id
    m2 = get_ticket_map(slave_id, 5002)
    assert m2.state == "failed"

    # Also test normal mark_open update
    mark_open(slave_id, 5001, sl=1.0950, tp=1.1100)
    assert get_ticket_map(slave_id, 5001).sl == 1.0950

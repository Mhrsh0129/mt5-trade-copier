from copier.adapters.base import ExecCode, MarginMode, OpenRequest, PendingRequest, PendingType, Side
from copier.adapters.fake_adapter import FakeAdapter
from copier.adapters.mt5_adapter import MT5Adapter


def _fx(mode=MarginMode.HEDGING) -> FakeAdapter:
    f = FakeAdapter(margin_mode=mode)
    f.add_symbol("EURUSD", 1.1000, 1.1002)
    return f


def test_hedging_open_partial_close():
    f = _fx()
    r = f.market_open(OpenRequest("EURUSD", Side.BUY, 0.30, magic=9))
    assert r.ok and r.position_id
    f.close(r.position_id, 0.10)
    assert f.positions()[0].volume == 0.20
    f.close(r.position_id)
    assert f.positions() == []
    assert [d.entry for d in f.deals_for_position(r.position_id)] == ["in", "out", "out"]


def test_netting_aggregates():
    f = _fx(MarginMode.NETTING)
    a = f.market_open(OpenRequest("EURUSD", Side.BUY, 0.30))
    b = f.market_open(OpenRequest("EURUSD", Side.BUY, 0.20))
    assert a.position_id == b.position_id and f.positions()[0].volume == 0.5
    f.market_open(OpenRequest("EURUSD", Side.SELL, 0.7))
    assert f.positions()[0].side is Side.SELL and abs(f.positions()[0].volume - 0.2) < 1e-9


def test_scripted_failures_and_pending_fill():
    f = _fx()
    f.fail_next(ExecCode.REQUOTE)
    assert f.market_open(OpenRequest("EURUSD", Side.BUY, 0.1)).code is ExecCode.REQUOTE
    o = f.place_pending(PendingRequest("EURUSD", PendingType.BUY_LIMIT, 0.1, 1.0950))
    pid = f.fill_pending(o.order_id)
    assert f.pending_orders() == [] and f.positions()[-1].position_id == pid


def test_pending_type_flip():
    assert PendingType.BUY_LIMIT.flipped() is PendingType.SELL_LIMIT
    assert PendingType.SELL_STOP_LIMIT.flipped() is PendingType.BUY_STOP_LIMIT
    assert PendingType.SELL_STOP.side is Side.SELL


def test_mt5_init_error_messages():
    e = MT5Adapter._init_error(-6, "Terminal: Authorization failed")
    assert e.code == -6 and "Authorization failed" in e.reason and "password" in e.hint
    e = MT5Adapter._init_error(-10005, "IPC timeout")
    assert "IPC timeout" in e.reason
    e = MT5Adapter._init_error(-999, "weird")
    assert e.reason == "weird"


def test_retryable_classification():
    assert ExecCode.REQUOTE.retryable and ExecCode.OFF_QUOTES.retryable
    assert not ExecCode.NO_MONEY.retryable and not ExecCode.INVALID_VOLUME.retryable

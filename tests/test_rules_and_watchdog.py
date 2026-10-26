import time
import pytest

from copier import accounts
from copier.adapters.base import Credentials, ExecCode, Side
from copier.adapters.fake_adapter import FakeAdapter
from copier.engine.events import EventType, TradeEvent
from copier.engine.executor import SlaveExecutor
from copier.engine.rules import load_slave_config, save_slave_settings, upsert_symbol_rule
from copier.terminals.watchdog import TerminalHangError, TerminalWatchdog


def _setup_slave(fake_template, login: int = 301):
    accounts.add_template("BrokerX", str(fake_template))
    sid = accounts.add_account(
        role="slave", broker="BrokerX", server="S", login=login, password="pw", provision=False
    )
    adapter = FakeAdapter(login=login, server="S", balance=10_000.0, leverage=100)
    adapter.add_symbol("EURUSD", 1.1000, 1.1002, stops_level=50)  # 50 points min stop distance
    return sid, adapter


def test_case_insensitive_symbol_rules_and_filters(db, fake_template):
    sid, _ = _setup_slave(fake_template, 301)
    save_slave_settings(sid, symbol_filter_mode="blacklist", symbol_filter=["gbpusd"])
    upsert_symbol_rule(sid, "xauusd", enabled=False)
    upsert_symbol_rule(sid, "eurusd", enabled=True, lot_mode="fixed", lot_value=0.07)

    cfg = load_slave_config(sid)
    assert not cfg.check_symbol_allowed("XAUUSD")[0]
    assert not cfg.check_symbol_allowed("xauusd")[0]
    assert not cfg.check_symbol_allowed("GBPUSD")[0]
    assert cfg.check_symbol_allowed("EURUSD")[0]
    mode, val, _, _ = cfg.effective_lot_params("eurusd")
    assert mode == "fixed" and val == 0.07


def test_executor_max_open_trades_and_margin_safety(db, fake_template):
    sid, adapter = _setup_slave(fake_template, 302)
    save_slave_settings(sid, max_open_trades=1)
    alerts = []
    ex = SlaveExecutor(sid, adapter, retry_delay_s=0, alert_cb=lambda k, t, l="warn": alerts.append((k, t)))

    # 1st trade succeeds
    a1 = ex.handle_event(TradeEvent(
        seq=1, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=1001, volume=0.10, price=1.1002
    ))
    assert a1.result == "ok"

    # 2nd trade blocked by max_open_trades=1
    a2 = ex.handle_event(TradeEvent(
        seq=2, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=1002, volume=0.10, price=1.1002
    ))
    assert a2.result == "skipped" and "max_open_trades" in a2.detail

    # Raise max_open_trades, but request a huge lot that exceeds free margin ($10,000)
    save_slave_settings(sid, max_open_trades=10)
    ex.reload_config()
    a3 = ex.handle_event(TradeEvent(
        seq=3, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=1003, volume=50.0, price=1.1002
    ))
    assert a3.result == "skipped" and "insufficient free margin" in a3.detail
    assert any("margin:" in k for k, _ in alerts)


def test_executor_defers_stops_when_inside_stops_level(db, fake_template):
    sid, adapter = _setup_slave(fake_template, 303)
    ex = SlaveExecutor(sid, adapter, retry_delay_s=0)

    # stops_level is 50 points (0.00050 on 5-digit EURUSD).
    # Ask is 1.10020; setting SL at 1.10000 is only 20 points away (< 50), so executor must:
    # 1) open with sl=0, tp=0
    # 2) call modify_position immediately after
    a = ex.handle_event(TradeEvent(
        seq=1, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=1004, volume=0.10, price=1.10020, sl=1.10000, tp=1.10040
    ))
    assert a.result == "ok"
    actions = [kind for kind, _ in adapter.sent]
    assert actions == ["open", "modify"]
    open_req = adapter.sent[0][1]
    assert open_req.sl == 0.0 and open_req.tp == 0.0
    assert adapter.positions()[0].sl == 1.10000 and adapter.positions()[0].tp == 1.10040


def test_executor_non_retryable_error_fails_immediately(db, fake_template):
    sid, adapter = _setup_slave(fake_template, 304)
    ex = SlaveExecutor(sid, adapter, retry_delay_s=0)
    # Queue NO_MONEY (non-retryable) followed by OK -> should stop on first attempt and NOT consume the 2nd
    adapter.fail_next(ExecCode.NO_MONEY)
    a = ex.handle_event(TradeEvent(
        seq=1, event_type=EventType.POSITION_OPENED, symbol="EURUSD", side="buy",
        master_position_id=1005, volume=0.10, price=1.1002
    ))
    assert a.result == "failed" and a.retcode == "no_money"
    assert len(adapter.sent) == 1


def test_watchdog_hang_timeout_and_backoff():
    adapter = FakeAdapter(login=401, server="S", password="pw")
    wd = TerminalWatchdog(
        401,
        adapter,
        lambda: Credentials(login=401, password="pw", server="S"),
        terminal_dir=None,
        hang_timeout_s=0.1,
    )
    ok, info, err = wd.ensure_connected()
    assert ok and info.login == 401 and err is None

    # Simulate a hung C-extension call (> 0.1s)
    with pytest.raises(TerminalHangError):
        wd.call_with_timeout(lambda: time.sleep(0.4), timeout_s=0.08)

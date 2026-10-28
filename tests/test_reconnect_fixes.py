"""Regression tests for the slave 'waiting for reconnect backoff' issue and related fixes."""

import json
import time
from datetime import datetime, timedelta, timezone

from copier import accounts
from copier.adapters.base import ConnectError, Credentials
from copier.adapters.fake_adapter import FakeAdapter
from copier.db import Account, AppSetting, EventLog, session_scope
from copier.terminals.watchdog import TerminalWatchdog


def _wd(adapter, creds, **kw):
    return TerminalWatchdog(401, adapter, lambda: creds, terminal_dir=None, hang_timeout_s=1.0, **kw)


# ------------------------------------------------------------------ watchdog
def test_backoff_reports_real_error_not_placeholder():
    adapter = FakeAdapter(login=401, server="S", password="right")
    attempts = []
    wd = _wd(adapter, Credentials(login=401, password="wrong", server="S"),
             on_attempt=lambda ok, err: attempts.append((ok, err)))

    ok, info, err = wd.ensure_connected()
    assert not ok and info is None
    assert "Authorization failed" in err
    assert attempts == [(False, err)]

    # Second call lands inside the backoff window: must repeat the REAL reason.
    ok, _, err2 = wd.ensure_connected()
    assert not ok
    assert "Authorization failed" in err2 and "retrying in" in err2
    assert "waiting for reconnect backoff" not in err2
    assert len(attempts) == 1, "no new attempt during backoff"


def test_request_reconnect_skips_backoff():
    adapter = FakeAdapter(login=401, server="S", password="pw")
    creds = Credentials(login=401, password="bad", server="S")
    wd = _wd(adapter, creds)
    wd.ensure_connected()
    assert wd.next_retry_at > time.monotonic()

    wd.creds_provider = lambda: Credentials(login=401, password="pw", server="S")  # user fixed password
    ok, _, _ = wd.ensure_connected()
    assert not ok, "still in backoff"
    wd.request_reconnect()
    ok, info, err = wd.ensure_connected()
    assert ok and info.login == 401 and err is None and wd.last_error is None


def test_broker_link_drop_waits_for_terminal_instead_of_reinit():
    adapter = FakeAdapter(login=401, server="S", password="pw")
    wd = _wd(adapter, Credentials(login=401, password="pw", server="S"), link_grace_s=0.3)
    assert wd.ensure_connected()[0]
    assert adapter.connect_calls == 1

    adapter.simulate_broker_link_drop()
    ok, _, err = wd.ensure_connected()
    assert not ok and "reconnecting to broker" in err
    assert adapter.connect_calls == 1, "healthy terminal must not be re-initialised"

    # Terminal recovers on its own inside the grace period.
    adapter.simulate_broker_link_drop(False)
    assert wd.ensure_connected()[0]
    assert adapter.connect_calls == 1


def test_broker_link_drop_reinitialises_after_grace():
    adapter = FakeAdapter(login=401, server="S", password="pw")
    wd = _wd(adapter, Credentials(login=401, password="pw", server="S"), link_grace_s=0.1)
    wd.ensure_connected()
    adapter.simulate_broker_link_drop()
    assert not wd.ensure_connected()[0]
    time.sleep(0.15)
    ok, info, _ = wd.ensure_connected()
    assert ok and adapter.connect_calls == 2


def test_connect_error_hint_is_included():
    class Failing(FakeAdapter):
        def connect(self, creds, terminal_path=None):
            raise ConnectError("IPC timeout", code=-10005, hint="First launch can be slow")

    wd = _wd(Failing(login=1, server="S", password="p"), Credentials(login=1, password="p", server="S"))
    _, _, err = wd.ensure_connected()
    assert "IPC timeout" in err and "First launch can be slow" in err


# ------------------------------------------------------------------ worker attempt record
def test_attempt_recorder_persists_outcome(db):
    from copier.worker import _attempt_recorder, attempt_key
    _attempt_recorder(7)(False, "Authorization failed")
    with session_scope() as s:
        rec = json.loads(s.get(AppSetting, attempt_key(7)).value)
    assert rec["ok"] is False and rec["error"] == "Authorization failed" and rec["ts"] > 0


# ------------------------------------------------------------------ delete
def test_delete_account_removes_terminal_and_reused_id_gets_clean_folder(db, fake_template):
    accounts.add_template("BrokerX", str(fake_template))
    acc_id = accounts.add_account(role="slave", broker="BrokerX", server="BrokerX-Demo", login=501,
                                  password="pw")
    tdir = accounts.get_account(acc_id).terminal_dir
    from pathlib import Path
    (Path(tdir) / "old_login_marker").write_text("x")

    accounts.delete_account(acc_id)
    assert not Path(tdir).exists()

    # Simulate a leftover folder from before this fix, then re-add with the same id.
    Path(tdir).mkdir(parents=True)
    (Path(tdir) / "terminal64.exe").write_bytes(b"MZ old")
    (Path(tdir) / "old_login_marker").write_text("x")
    new_id = accounts.add_account(role="slave", broker="BrokerX", server="BrokerX-Demo", login=502,
                                  password="pw")
    assert new_id == acc_id
    assert not (Path(tdir) / "old_login_marker").exists()
    assert (Path(tdir) / "terminal64.exe").read_bytes() == b"MZ fake"


def test_delete_refuses_paths_outside_terminals_dir(db, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    accounts._remove_terminal_dir(outside)
    assert outside.exists()


# ------------------------------------------------------------------ dashboard connect
def _add_raw_account(status, last_sync):
    from copier import crypto
    with session_scope() as s:
        a = Account(role="slave", broker="B", server="S", login=9, password_enc=crypto.encrypt("pw"),
                    status=status, account_name="X", balance=1.0, currency="USD", leverage=100,
                    last_sync_at=last_sync)
        s.add(a)
        s.flush()
        return a.id


def test_connect_ignores_stale_connected_status(db, monkeypatch):
    import importlib
    webapp = importlib.import_module("copier.web.app")
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    acc_id = _add_raw_account("connected", old)
    called = {}
    monkeypatch.setattr(webapp, "_supervisor_running", lambda port=0: True)
    monkeypatch.setattr(webapp, "_connect_via_worker",
                        lambda aid, timeout_s=75.0: called.setdefault("aid", aid) and
                        accounts.ConnectResult(False, "worker said no"))
    res = webapp._run_connect(acc_id)
    assert called.get("aid") == acc_id, "stale 'connected' must not short-circuit"
    assert not res.success


def test_connect_uses_fresh_connected_status(db, monkeypatch):
    import importlib
    webapp = importlib.import_module("copier.web.app")
    acc_id = _add_raw_account("connected", datetime.now(timezone.utc))
    monkeypatch.setattr(webapp, "_supervisor_running", lambda port=0: (_ for _ in ()).throw(AssertionError))
    res = webapp._run_connect(acc_id)
    assert res.success and "Connected" in res.message


def test_connect_via_worker_returns_worker_error(db, monkeypatch):
    import importlib
    webapp = importlib.import_module("copier.web.app")
    from copier.worker import _attempt_recorder
    acc_id = _add_raw_account("error", None)
    monkeypatch.setattr(webapp._bus_pub, "publish_sync",
                        lambda topic, payload, timeout=0.5: _attempt_recorder(acc_id)(False, "Authorization failed") or True)
    res = webapp._connect_via_worker(acc_id, timeout_s=5)
    assert not res.success and res.message == "Authorization failed"


def test_delete_is_audit_logged(db):
    from fastapi.testclient import TestClient

    from copier import auth
    from copier.web.app import app
    auth.set_admin_password("Admin@123456")
    acc_id = _add_raw_account("disconnected", None)
    c = TestClient(app)
    r = c.post("/login", data={"password": "Admin@123456"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/accounts").text
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post(f"/accounts/{acc_id}/delete", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        msgs = [e.message for e in s.query(EventLog).all()]
    assert any(m.startswith(f"Deleted slave account #{acc_id}") for m in msgs)

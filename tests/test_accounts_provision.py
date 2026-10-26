import pytest

from copier import accounts
from copier.adapters.fake_adapter import FakeAdapter
from copier.db import Account, SlaveSettings, session_scope
from copier.terminals.provision import (_read_text_any, apply_common_ini, provision_terminal,
                                        validate_template)


# ------------------------------------------------------------------ provisioning
def test_validate_template_rejects_installer_folder(tmp_path):
    (tmp_path / "brokerx5setup.exe").write_bytes(b"MZ")
    chk = validate_template(tmp_path)
    assert not chk.ok and "installer folder" in chk.reason


def test_provision_copies_and_skips_private_files(fake_template, tmp_path):
    dest = provision_terminal(1, fake_template, dest=tmp_path / "acc_1")
    assert (dest / "terminal64.exe").exists()
    assert (dest / "MQL5" / "Experts" / "x.ex5").exists()
    assert (dest / "config" / "servers.dat").exists()
    assert not (dest / "config" / "accounts.dat").exists(), "saved logins must not be copied"
    assert not (dest / "logs").exists()
    assert not (dest / "Bases").exists()
    ini = _read_text_any(dest / "config" / "common.ini")
    assert "[Experts]" in ini and "Enabled=1" in ini and "AllowLiveTrading=1" in ini


def test_provision_is_idempotent(fake_template, tmp_path):
    dest = provision_terminal(1, fake_template, dest=tmp_path / "acc_1")
    (dest / "marker").write_text("keep")
    provision_terminal(1, fake_template, dest=dest)
    assert (dest / "marker").exists()
    provision_terminal(1, fake_template, dest=dest, force=True)
    assert not (dest / "marker").exists()


def test_common_ini_merge_preserves_existing_keys(tmp_path):
    p = tmp_path / "common.ini"
    p.write_text("[Common]\nLogin=123\nNewsEnable=1\n[Experts]\nAllowDllImport=1\n", encoding="utf-16")
    apply_common_ini(p)
    text = _read_text_any(p)
    assert p.read_bytes()[:2] == b"\xff\xfe"  # UTF-16 LE BOM, as MT5 expects
    assert "Login=123" in text
    assert "NewsEnable=0" in text
    assert "AllowDllImport=0" in text


# ------------------------------------------------------------------ accounts
def _setup_template(fake_template):
    accounts.add_template("BrokerX", str(fake_template))


def test_add_account_encrypts_password_and_provisions(db, fake_template):
    _setup_template(fake_template)
    acc_id = accounts.add_account(role="master", broker="BrokerX", server="BrokerX-Demo", login=111,
                                  password="PlainPw#42")
    acc = accounts.get_account(acc_id)
    assert acc.password_enc and b"PlainPw#42" not in acc.password_enc
    assert accounts.credentials_for(acc).password == "PlainPw#42"
    assert acc.terminal_dir and acc.terminal_dir.endswith(f"acc_{acc_id}")
    raw_db = (db / "copier.db").read_bytes()
    wal = (db / "copier.db-wal")
    raw_db += wal.read_bytes() if wal.exists() else b""
    assert b"PlainPw#42" not in raw_db


def test_only_one_master_and_max_ten_slaves(db, fake_template):
    _setup_template(fake_template)
    accounts.add_account(role="master", broker="BrokerX", server="S", login=1, password="p", provision=False)
    with pytest.raises(accounts.ValidationError, match="master account already exists"):
        accounts.add_account(role="master", broker="BrokerX", server="S", login=2, password="p", provision=False)
    for i in range(10):
        accounts.add_account(role="slave", broker="BrokerX", server="S", login=100 + i, password="p",
                             provision=False)
    with pytest.raises(accounts.ValidationError, match="Maximum of 10"):
        accounts.add_account(role="slave", broker="BrokerX", server="S", login=200, password="p", provision=False)
    with session_scope() as s:
        assert s.query(SlaveSettings).count() == 10


def test_duplicate_and_unknown_broker(db, fake_template):
    _setup_template(fake_template)
    accounts.add_account(role="slave", broker="BrokerX", server="S", login=1, password="p", provision=False)
    with pytest.raises(accounts.ValidationError, match="already exists"):
        accounts.add_account(role="slave", broker="BrokerX", server="S", login=1, password="p", provision=False)
    with pytest.raises(accounts.ValidationError, match="No terminal template"):
        accounts.add_account(role="slave", broker="Nope", server="S", login=2, password="p", provision=False)


def test_connect_success_message_and_status(db, fake_template):
    _setup_template(fake_template)
    acc_id = accounts.add_account(role="slave", broker="BrokerX", server="Fake-Demo", login=1000,
                                  password="pw", provision=False)
    res = accounts.connect_account(acc_id, adapter=FakeAdapter())
    assert res.success
    assert res.message == ("Connected: Fake Trader · Fake-Demo · Balance 10,000.00 USD · Leverage 1:100")
    with session_scope() as s:
        acc = s.get(Account, acc_id)
        assert acc.status == "connected" and acc.balance == 10_000 and acc.leverage == 100
        assert acc.last_sync_at is not None and acc.margin_mode == "hedging"


def test_connect_failure_records_exact_reason(db, fake_template):
    _setup_template(fake_template)
    acc_id = accounts.add_account(role="slave", broker="BrokerX", server="Fake-Demo", login=1000,
                                  password="WRONG", provision=False)
    res = accounts.connect_account(acc_id, adapter=FakeAdapter())
    assert not res.success and res.error_code == -6
    assert "Authorization failed" in res.message and "WRONG" not in res.message
    acc = accounts.get_account(acc_id)
    assert acc.status == "error" and "Authorization failed" in acc.last_error

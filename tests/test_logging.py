import json
import logging

from copier.logging_setup import REDACTED, account_logger, redact, register_secret, setup_logging


def test_redacts_key_value_pairs():
    assert "hunter2" not in redact("login ok password=hunter2 server=X")
    assert "hunter2" not in redact('{"password": "hunter2"}')
    assert "abc" not in redact("api_token: abc")
    assert redact("server=ICMarkets-Demo") == "server=ICMarkets-Demo"


def test_redacts_telegram_token():
    s = "url https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0/sendMessage"
    assert "AAHdqTcv" not in redact(s)


def test_redacts_registered_secret_anywhere():
    register_secret("Zq9!xY7#pass")
    assert "Zq9!xY7#pass" not in redact("initialize(login=1, 'Zq9!xY7#pass')")


def test_log_file_never_contains_password(tmp_path):
    register_secret("TopSecret123")
    setup_logging("t", console=False, logs_dir=tmp_path)
    log = account_logger(7)
    log.info("connecting with TopSecret123")
    log.info("dict", extra={"ctx": {"password": "TopSecret123", "login": 5}})
    logging.getLogger("x").warning("password=%s", "TopSecret123")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "TopSecret123" not in content
    lines = [json.loads(line) for line in content.splitlines()]
    assert lines[0]["account_id"] == 7
    assert lines[1]["ctx"]["password"] == REDACTED
    assert lines[1]["ctx"]["login"] == 5

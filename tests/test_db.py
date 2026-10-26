from sqlalchemy import text

from copier.db import get_engine, init_db
from copier.db.session import SCHEMA_VERSION


def test_init_db_idempotent(data_dir):
    assert init_db() == SCHEMA_VERSION
    assert init_db() == SCHEMA_VERSION
    with get_engine().connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        mode = c.execute(text("PRAGMA journal_mode")).scalar()
    assert {"accounts", "slave_settings", "symbol_rules", "symbol_map", "ticket_map", "trade_audit",
            "alert_outbox", "events_log", "terminal_templates", "admin_user", "app_settings"} <= tables
    assert mode.lower() == "wal"

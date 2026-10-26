"""Engine/session factory and lightweight schema migrations.

SQLite is opened in WAL mode with a busy timeout so the API process and up to
11 worker processes can read/write concurrently.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_paths
from .models import AppSetting, Base

log = logging.getLogger(__name__)

_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker] = {}


def get_engine(db_file: Path | None = None) -> Engine:
    db_file = db_file or get_paths().db_file
    key = str(db_file)
    if key not in _engines:
        engine = create_engine(f"sqlite:///{db_file}", future=True,
                               connect_args={"timeout": 15, "check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()

        _engines[key] = engine
        _factories[key] = sessionmaker(engine, expire_on_commit=False, future=True)
    return _engines[key]


@contextmanager
def session_scope(db_file: Path | None = None) -> Iterator[Session]:
    get_engine(db_file)
    factory = _factories[str(db_file or get_paths().db_file)]
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def dispose_all() -> None:
    for e in _engines.values():
        e.dispose()
    _engines.clear()
    _factories.clear()


# ---------------------------------------------------------------- migrations
def _m1_initial(engine: Engine) -> None:
    Base.metadata.create_all(engine)


MIGRATIONS: list[Callable[[Engine], None]] = [_m1_initial]
SCHEMA_VERSION = len(MIGRATIONS)


def _current_version(engine: Engine) -> int:
    with engine.connect() as c:
        exists = c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'")).first()
        if not exists:
            return 0
        row = c.execute(text("SELECT value FROM app_settings WHERE key='schema_version'")).first()
        return int(row[0]) if row else 0


def init_db(db_file: Path | None = None) -> int:
    """Create / migrate the DB. Returns the resulting schema version."""
    engine = get_engine(db_file)
    current = _current_version(engine)
    for i in range(current, SCHEMA_VERSION):
        log.info("Applying DB migration %d", i + 1)
        MIGRATIONS[i](engine)
        with session_scope(db_file) as s:
            s.merge(AppSetting(key="schema_version", value=str(i + 1)))
    return SCHEMA_VERSION

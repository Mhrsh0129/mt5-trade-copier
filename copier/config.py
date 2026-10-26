"""Filesystem layout and runtime settings.

Everything mutable lives under one data directory:

    <data_dir>/
        copier.db              SQLite database (WAL mode)
        secrets/fernet.key.dpapi   Fernet key, DPAPI-protected (never in the DB)
        terminals/<account_id>/    portable MT5 terminal per account
        logs/                  structured JSON logs

The data directory is ``%TC_DATA_DIR%`` if set, otherwise
``%ProgramData%\\TradeCopier`` (``./data`` on non-Windows dev machines).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _default_data_dir() -> Path:
    env = os.environ.get("TC_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if os.environ.get("VERCEL"):
        return Path("/tmp/TradeCopier")
    if sys.platform == "win32" and os.environ.get("ProgramData"):
        return Path(os.environ["ProgramData"]) / "TradeCopier"
    return Path.cwd() / "data"


@dataclass(frozen=True)
class Paths:
    data_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "copier.db"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def key_file(self) -> Path:
        return self.secrets_dir / "fernet.key.dpapi"

    @property
    def terminals_dir(self) -> Path:
        return self.data_dir / "terminals"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure(self) -> "Paths":
        for p in (self.data_dir, self.secrets_dir, self.terminals_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache(maxsize=1)
def get_paths() -> Paths:
    return Paths(_default_data_dir()).ensure()


def reset_paths_cache() -> None:
    """Used by tests after changing TC_DATA_DIR."""
    get_paths.cache_clear()


# --- Tunables (defaults; most become editable in the dashboard later) ---
MASTER_POLL_MS = 50
SNAPSHOT_EVERY_S = 5
CONNECT_TIMEOUT_MS = 60_000
TERMINAL_HANG_TIMEOUT_S = 10

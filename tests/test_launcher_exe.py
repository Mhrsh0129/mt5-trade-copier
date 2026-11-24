"""Tests for standalone .exe launcher, frozen child commands, and data_dir fallback."""

from __future__ import annotations

import sys
from pathlib import Path

from copier import config, launcher, supervisor


def test_build_cmds_when_unfrozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not supervisor.is_frozen()
    w_cmd = supervisor.build_web_cmd("127.0.0.1", 8000)
    assert "-m" in w_cmd and "uvicorn" in w_cmd
    k_cmd = supervisor.build_worker_cmd(5, 5555, connect_only=True)
    assert "-m" in k_cmd and "copier.worker" in k_cmd and "--connect-only" in k_cmd


def test_build_cmds_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert supervisor.is_frozen()
    w_cmd = supervisor.build_web_cmd("127.0.0.1", 8000)
    assert w_cmd == [sys.executable, "--role", "web", "--host", "127.0.0.1", "--port", "8000"]
    k_cmd = supervisor.build_worker_cmd(7, 5555, connect_only=True)
    assert k_cmd == [
        sys.executable, "--role", "worker",
        "--account-id", "7", "--bus-port", "5555", "--connect-only",
    ]


def test_data_dir_falls_back_to_localappdata_when_programdata_unwritable(monkeypatch, tmp_path):
    monkeypatch.delenv("TC_DATA_DIR", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramData", str(tmp_path / "progdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    monkeypatch.setattr(config, "_is_writable_dir", lambda p: False)

    d = config._default_data_dir()
    assert d == Path(tmp_path / "localapp") / "TradeCopier"


def test_launcher_dispatches_worker_role(monkeypatch):
    called = {}
    import copier.worker as wmod
    monkeypatch.setattr(wmod, "main", lambda argv: called.setdefault("argv", argv) and 0)
    rc = launcher.main(["--role", "worker", "--account-id", "3", "--bus-port", "5599", "--connect-only"])
    assert rc == 0
    assert called["argv"] == ["--account-id", "3", "--bus-port", "5599", "--connect-only"]

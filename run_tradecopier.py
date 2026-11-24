"""Top-level entry point for PyInstaller packaging (TradeCopier.exe)."""

from __future__ import annotations

from copier.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())

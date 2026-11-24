"""Unified entry point for the standalone Windows executable (TradeCopier.exe).

Dispatches based on ``--role``:
- ``supervisor`` (default): Initializes the key & SQLite DB, starts the IPC Hub,
  spawns the FastAPI web child and per-account MT5 worker children, and opens
  ``http://127.0.0.1:8000`` in the user's default web browser.
- ``web``: Runs the FastAPI dashboard directly via ``uvicorn.run(app, ...)``.
- ``worker``: Runs a single isolated MT5 account worker process.
"""

from __future__ import annotations

import argparse
import multiprocessing
import socket
import sys
import threading
import time
import webbrowser

from .bus import DEFAULT_EVENT_PORT


def _open_browser_when_ready(host: str, port: int, timeout_s: float = 25.0) -> None:
    target_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{target_host}:{port}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target_host, port), timeout=0.5) as sock:
                sock.sendall(b"GET /login HTTP/1.0\r\nHost: localhost\r\n\r\n")
                sock.recv(64)
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(0.35)


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    ap = argparse.ArgumentParser(description="MT5 Trade Copier Standalone Application")
    ap.add_argument("--role", choices=("supervisor", "web", "worker"), default="supervisor")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bus-port", type=int, default=DEFAULT_EVENT_PORT)
    ap.add_argument("--no-workers", action="store_true", help="start hub + web UI only")
    ap.add_argument("--no-browser", action="store_true", help="do not auto-open browser on startup")
    ap.add_argument("--account-id", type=int, default=None, help="account id (for --role worker)")
    ap.add_argument("--connect-only", action="store_true", help="test connection and exit (for --role worker)")
    args = ap.parse_args(argv)

    if args.role == "worker":
        if args.account_id is None:
            ap.error("--account-id is required when --role=worker")
        from .worker import main as worker_main
        w_argv = ["--account-id", str(args.account_id), "--bus-port", str(args.bus_port)]
        if args.connect_only:
            w_argv.append("--connect-only")
        return worker_main(w_argv)

    if args.role == "web":
        import uvicorn

        from . import crypto
        from .db import init_db
        from .web.app import app
        crypto.ensure_key()
        init_db()
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    # Default role: supervisor
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(args.host, args.port),
            daemon=True,
        ).start()

    from .supervisor import main as supervisor_main
    s_argv = ["--host", args.host, "--port", str(args.port), "--bus-port", str(args.bus_port)]
    if args.no_workers:
        s_argv.append("--no-workers")
    return supervisor_main(s_argv)


if __name__ == "__main__":
    raise SystemExit(main())

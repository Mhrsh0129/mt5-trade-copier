"""Phase 1 spike: verifies the platform assumptions in the architecture doc
against a REAL demo account. Run it on the target laptop.

    .venv\\Scripts\\python scripts\\spike_phase1.py --template "C:\\Program Files\\<Broker> MetaTrader 5" ^
        --server "<Broker>-Demo" --login 12345678 [--trade --symbol EURUSD] [--report out.json]

Checks
  1. Template validation + portable copy into a scratch folder
  2. Login via mt5.initialize(portable=True) and time-to-connect
  3. Portable mode really isolated (data_path == install path)
  4. Algo trading enabled by our common.ini (terminal_info().trade_allowed), maxbars applied
  5. Terminal RAM usage
  6. positions_get() call latency (poll budget for 50 ms master loop)
  7. Windows session id (0 = non-interactive / service context)
  8. --trade: large magic numbers survive the round-trip (opens + closes 0.01 lots, DEMO ONLY)

Run it twice for the session test: once from a normal console, once from a
Scheduled Task set to "Run whether user is logged on or not" with --report.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copier.adapters.base import Credentials, OpenRequest, Side  # noqa: E402
from copier.adapters.mt5_adapter import MT5Adapter, mt5  # noqa: E402
from copier.terminals.provision import (kill_terminal_processes, provision_terminal,  # noqa: E402
                                        terminal_memory_mb, validate_template)

MAGIC_CANDIDATES = [2**31 - 1, 2**32 + 12345, 2**53 + 1, 7_000_000_000_123_456_789]


def session_id() -> int | None:
    if sys.platform != "win32":
        return None
    sid = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return sid.value if ok else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--login", required=True, type=int)
    ap.add_argument("--password", help="omit to be prompted (required when run from a scheduled task)")
    ap.add_argument("--dest", help="scratch terminal folder (default: temp)")
    ap.add_argument("--trade", action="store_true", help="DEMO ONLY: open/close 0.01 lots to test magic numbers")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--report", help="write JSON results to this file")
    ap.add_argument("--keep", action="store_true", help="leave the terminal running")
    a = ap.parse_args()

    R: dict = {"session_id": session_id(), "python": sys.version.split()[0], "checks": {}}
    C = R["checks"]

    def out(name: str, ok: bool, detail) -> None:
        C[name] = {"ok": ok, "detail": detail}
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    print(f"Windows session id: {R['session_id']} ({'NON-interactive' if R['session_id'] == 0 else 'interactive'})")

    chk = validate_template(a.template)
    out("template", chk.ok, chk.reason if not chk.ok else f"exe={chk.exe}, servers.dat={chk.servers_dat}")
    if not chk.ok:
        return _finish(R, a)

    dest = Path(a.dest) if a.dest else Path(tempfile.gettempdir()) / "tc_spike_terminal"
    t0 = time.perf_counter()
    dest = provision_terminal(0, a.template, dest=dest)
    out("provision", True, f"{dest} in {time.perf_counter() - t0:.1f}s")

    pw = a.password or getpass.getpass("Demo password: ")
    adapter = MT5Adapter()
    t0 = time.perf_counter()
    try:
        info = adapter.connect(Credentials(a.login, pw, a.server), dest)
    except Exception as e:  # noqa: BLE001
        out("connect", False, str(e))
        return _finish(R, a)
    out("connect", True, f"{info.name} {info.server} bal={info.balance} lev=1:{info.leverage} "
                         f"mode={info.margin_mode.value} in {time.perf_counter() - t0:.1f}s")

    ti = mt5.terminal_info()
    portable = Path(ti.data_path).resolve() == dest.resolve()
    out("portable_isolated", portable, f"path={ti.path} data_path={ti.data_path}")
    out("algo_trading_enabled", bool(ti.trade_allowed),
        "terminal_info().trade_allowed=%s, account trade_expert=%s" % (ti.trade_allowed, info.algo_allowed))
    out("maxbars_applied", ti.maxbars <= 10000, f"maxbars={ti.maxbars}")
    out("trade_allowed_account", info.trade_allowed, "False => investor password or disabled account")

    time.sleep(2)
    out("terminal_ram_mb", True, terminal_memory_mb(dest))

    samples = []
    for _ in range(50):
        s = time.perf_counter()
        adapter.positions()
        adapter.pending_orders()
        samples.append((time.perf_counter() - s) * 1000)
    out("poll_latency_ms", statistics.median(samples) < 20,
        f"median={statistics.median(samples):.2f} p95={sorted(samples)[47]:.2f} max={max(samples):.2f}")

    if a.trade:
        results = {}
        for magic in MAGIC_CANDIDATES:
            r = adapter.market_open(OpenRequest(symbol=a.symbol, side=Side.BUY, volume=0.01, magic=magic,
                                                comment="TC|spike"))
            if not r.ok:
                results[str(magic)] = f"open failed: {r.code.value} {r.retcode} {r.message}"
                continue
            time.sleep(0.3)
            pos = next((p for p in adapter.positions() if p.position_id == r.position_id), None)
            got = pos.magic if pos else None
            results[str(magic)] = {"read_back": got, "match": got == magic,
                                   "comment": pos.comment if pos else None}
            c = adapter.close(r.position_id)
            if not c.ok:
                results[str(magic)]["close"] = f"FAILED {c.code.value} {c.retcode} - close manually!"
        out("magic_roundtrip", all(isinstance(v, dict) and v["match"] for v in results.values()), results)

    if not a.keep:
        adapter.disconnect()
        kill_terminal_processes(dest)
    return _finish(R, a)


def _finish(R: dict, a: argparse.Namespace) -> int:
    if a.report:
        Path(a.report).write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {a.report}")
    return 0 if all(c["ok"] for c in R["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Supervisor process (architecture §1, §8.2, §8.3, §8.4).

Single entry point started by Windows Scheduled Task (or ``python -m copier.supervisor``):
- Hosts the localhost IPC HubServer so master, slaves, and API can exchange events & control messages.
- Spawns & supervises the FastAPI web dashboard and one ``copier.worker`` process per enabled account,
  restarting crashed children with exponential backoff.
- Runs the internet connectivity monitor: probes independent hosts, broadcasts ``global_pause``
  when offline, queues a recovery summary alert when back online, and triggers full slave reconciliation.
- Flushes the Telegram alert outbox and pings the external healthchecks.io heartbeat every 60 s.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Sequence

from . import accounts, crypto
from .alerts.outbox import enqueue_alert, flush_outbox, record_event_log
from .alerts.telegram import ping_heartbeat
from .bus import DEFAULT_EVENT_PORT, HubServer, Publisher
from .db import AppSetting, init_db, session_scope
from .logging_setup import setup_logging

log = logging.getLogger(__name__)

PROBE_TARGETS: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
    ("9.9.9.9", 53),
)


def check_internet_once(targets: Sequence[tuple[str, int]] = PROBE_TARGETS,
                        timeout: float = 1.5) -> bool:
    """Return True if at least one independent public IP accepts a TCP handshake."""
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class ConnectivityMonitor:
    """Tracks online/offline transitions after ``fail_threshold`` consecutive failed probe rounds."""

    def __init__(
        self,
        probe_fn=check_internet_once,
        *,
        fail_threshold: int = 2,
    ) -> None:
        self.probe_fn = probe_fn
        self.fail_threshold = fail_threshold
        self.online = True
        self.failed_rounds = 0
        self.offline_since: float | None = None

    def step(self) -> tuple[str | None, float]:
        """Run one probe round.

        Returns ``(transition, outage_duration_s)`` where ``transition`` is:
        - ``'lost'`` when transitioning ONLINE -> OFFLINE
        - ``'restored'`` when transitioning OFFLINE -> ONLINE
        - ``None`` when state is unchanged
        """
        ok = bool(self.probe_fn())
        if ok:
            self.failed_rounds = 0
            if not self.online:
                self.online = True
                dur = (time.monotonic() - self.offline_since) if self.offline_since else 0.0
                self.offline_since = None
                return "restored", dur
            return None, 0.0

        self.failed_rounds += 1
        if self.online and self.failed_rounds >= self.fail_threshold:
            self.online = False
            self.offline_since = time.monotonic()
            return "lost", 0.0
        return None, 0.0


@dataclass
class SupervisedChild:
    name: str
    cmd: list[str]
    proc: subprocess.Popen | None = None
    restarts: int = 0
    next_start_at: float = 0.0
    crash_times: list[float] = field(default_factory=list)


class Supervisor:
    def __init__(self, *, web_host: str = "127.0.0.1", web_port: int = 8000,
                 bus_port: int = DEFAULT_EVENT_PORT, spawn_workers: bool = True) -> None:
        self.web_host = web_host
        self.web_port = web_port
        self.bus_port = bus_port
        self.spawn_workers = spawn_workers
        self.hub = HubServer(port=bus_port)
        self.pub = Publisher(port=bus_port)
        self.net = ConnectivityMonitor()
        self.children: dict[str, SupervisedChild] = {}
        self._stopping = False

    def _set_app_state(self, key: str, value: str) -> None:
        with session_scope() as s:
            s.merge(AppSetting(key=key, value=value))

    def _sync_worker_children(self) -> None:
        if not self.spawn_workers:
            return
        active_accounts = [a for a in accounts.list_accounts() if a.enabled]
        desired_keys = {f"account_{a.id}": a for a in active_accounts}

        # Stop removed/disabled account workers
        for key in list(self.children.keys()):
            if key.startswith("account_") and key not in desired_keys:
                self._stop_child(self.children.pop(key))

        # Add new account workers
        for key, acc in desired_keys.items():
            if key not in self.children:
                self.children[key] = SupervisedChild(
                    name=key,
                    cmd=[
                        sys.executable, "-m", "copier.worker",
                        "--account-id", str(acc.id),
                        "--bus-port", str(self.bus_port),
                    ],
                )

    def _ensure_children_running(self) -> None:
        now = time.monotonic()
        for child in self.children.values():
            if child.proc is not None and child.proc.poll() is None:
                continue
            if child.proc is not None:
                rc = child.proc.returncode
                child.proc = None
                child.restarts += 1
                child.crash_times = [t for t in child.crash_times if (now - t) < 600.0] + [now]
                if len(child.crash_times) >= 5:
                    backoff = 300.0
                    enqueue_alert(
                        f"crashloop:{child.name}",
                        f"Process {child.name} crashed {len(child.crash_times)} times in 10m (exit {rc}); backing off 5m",
                        level="error",
                    )
                else:
                    backoff = min(60.0, float(2 ** min(child.restarts, 6)))
                child.next_start_at = now + backoff
                log.warning("Child %s exited (code %s); restart in %.0fs", child.name, rc, backoff)
            if now >= child.next_start_at:
                child.proc = subprocess.Popen(child.cmd)
                log.info("Started child %s (pid=%s)", child.name, child.proc.pid)

    @staticmethod
    def _stop_child(child: SupervisedChild) -> None:
        if child.proc is not None and child.proc.poll() is None:
            try:
                child.proc.terminate()
                child.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    child.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        child.proc = None

    def stop_all(self) -> None:
        self._stopping = True
        for child in list(self.children.values()):
            self._stop_child(child)

    async def run(self) -> None:
        os.environ["TC_BUS_PORT"] = str(self.bus_port)
        await self.hub.start()
        self.children["web"] = SupervisedChild(
            name="web",
            cmd=[
                sys.executable, "-m", "uvicorn", "copier.web.app:app",
                "--host", self.web_host, "--port", str(self.web_port),
                "--log-level", "warning",
            ],
        )
        self._set_app_state("internet_online", "1")
        self._set_app_state("global_paused", "0")
        record_event_log(f"Supervisor started (dashboard http://{self.web_host}:{self.web_port})", source="supervisor")

        last_heartbeat = 0.0
        try:
            while not self._stopping:
                self._sync_worker_children()
                self._ensure_children_running()

                transition, outage_s = self.net.step()
                if transition == "lost":
                    log.warning("Internet connection LOST; pausing copying globally")
                    self._set_app_state("internet_online", "0")
                    self._set_app_state("global_paused", "1")
                    await self.pub.publish("control", {"cmd": "global_pause", "paused": True})
                    enqueue_alert("net:lost", "Internet connection lost on laptop. Copying paused.")
                elif transition == "restored":
                    log.info("Internet connection RESTORED after %.1fs; unpausing & reconciling", outage_s)
                    self._set_app_state("internet_online", "1")
                    self._set_app_state("global_paused", "0")
                    await self.pub.publish("control", {"cmd": "global_pause", "paused": False})
                    enqueue_alert(
                        "net:restored",
                        f"Internet restored after {outage_s:.0f}s offline. Reconciling all slaves against master.",
                    )

                flush_outbox(online=self.net.online)

                now = time.monotonic()
                if self.net.online and (now - last_heartbeat) >= 60.0:
                    last_heartbeat = now
                    ok, detail = ping_heartbeat(healthy=True)
                    if ok:
                        self._set_app_state("last_heartbeat_at", str(int(time.time())))

                await asyncio.sleep(3.0)
        finally:
            self.stop_all()
            await self.pub.close()
            await self.hub.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TradeCopier supervisor")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bus-port", type=int, default=DEFAULT_EVENT_PORT)
    ap.add_argument("--no-workers", action="store_true", help="start hub + web UI only")
    args = ap.parse_args(argv)

    crypto.ensure_key()
    init_db()
    setup_logging("supervisor", console=True)
    sup = Supervisor(web_host=args.host, web_port=args.port, bus_port=args.bus_port,
                     spawn_workers=not args.no_workers)
    try:
        asyncio.run(sup.run())
    except KeyboardInterrupt:
        sup.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

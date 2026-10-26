"""Per-account worker process (architecture §1, §4, §5).

Usage:
    python -m copier.worker --account-id <id> [--connect-only]

Each worker owns a single portable MT5 terminal in its own OS process.
- If ``--connect-only`` is passed (used by the dashboard's "Connect" button),
  it launches/logs into the account's terminal, writes the result to SQLite,
  prints JSON to stdout, and exits.
- Otherwise it runs continuously:
  * Master worker polls every 50 ms, diffs snapshots, and publishes events to the IPC bus.
  * Slave worker subscribes to the IPC bus, executes trade events, and runs reconciliation on reconnect/snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from . import accounts
from .adapters import get_adapter
from .alerts.outbox import enqueue_alert, record_event_log
from .bus import DEFAULT_EVENT_PORT, Publisher, Subscriber
from .config import MASTER_POLL_MS
from .db import Account, init_db, session_scope
from .db.models import utcnow
from .engine.executor import SlaveExecutor
from .engine.master import MasterPoller, load_master_snapshot_db
from .engine.reconcile import reconcile_slave
from .engine.slave import SlaveSubscriber
from .logging_setup import account_logger, setup_logging
from .terminals.watchdog import TerminalWatchdog


def _update_account_status(account_id: int, **fields) -> None:
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is not None:
            for k, v in fields.items():
                setattr(acc, k, v)


async def run_master_worker(account_id: int, event_port: int = DEFAULT_EVENT_PORT) -> None:
    log = account_logger(account_id)
    acc = accounts.get_account(account_id)
    adapter = get_adapter("mt5")
    watchdog = TerminalWatchdog(
        account_id,
        adapter,
        lambda: accounts.credentials_for(accounts.get_account(account_id)),
        Path(acc.terminal_dir) if acc.terminal_dir else None,
    )
    pub = Publisher(port=event_port)
    poller = MasterPoller(adapter, pub, poll_ms=MASTER_POLL_MS)
    seeded = False
    was_connected = False
    last_status_write = 0.0

    log.info("Master worker started for login=%s server=%s", acc.login, acc.server)
    record_event_log(f"Master worker started ({acc.login}@{acc.server})", account_id=account_id, source="worker")

    while True:
        ok, info, err = watchdog.ensure_connected()
        if not ok or info is None:
            if was_connected:
                was_connected = False
                enqueue_alert(
                    f"disc:{account_id}",
                    f"Master account #{account_id} ({acc.login}@{acc.server}) disconnected: {err}",
                    account_id=account_id,
                )
            _update_account_status(account_id, status="error" if err else "disconnected", last_error=err)
            await asyncio.sleep(1.0)
            continue

        if not was_connected:
            was_connected = True
            if not seeded:
                poller.seed_from_db_or_live()
                seeded = True
            record_event_log(f"Master connected: {info.name} ({info.balance:,.2f} {info.currency})",
                             account_id=account_id, source="worker")

        try:
            await poller.poll_once()
        except Exception as e:  # noqa: BLE001
            log.warning("Master poll error: %s", e)
            await asyncio.sleep(0.25)
            continue

        now = time.monotonic()
        if (now - last_status_write) >= 2.0:
            last_status_write = now
            _update_account_status(
                account_id,
                status="connected",
                last_error=None,
                account_name=info.name,
                currency=info.currency,
                balance=info.balance,
                equity=info.equity,
                leverage=info.leverage,
                margin_mode=info.margin_mode.value,
                last_sync_at=utcnow(),
            )

        await asyncio.sleep(MASTER_POLL_MS / 1000.0)


async def run_slave_worker(account_id: int, event_port: int = DEFAULT_EVENT_PORT) -> None:
    log = account_logger(account_id)
    acc = accounts.get_account(account_id)
    adapter = get_adapter("mt5")
    watchdog = TerminalWatchdog(
        account_id,
        adapter,
        lambda: accounts.credentials_for(accounts.get_account(account_id)),
        Path(acc.terminal_dir) if acc.terminal_dir else None,
    )
    executor = SlaveExecutor(
        account_id,
        adapter,
        alert_cb=lambda key, txt, lvl="warn": enqueue_alert(key, txt, level=lvl, account_id=account_id),
    )
    subscriber = SlaveSubscriber(executor)
    bus_sub = Subscriber(topics={"trade_event", "snapshot", "control"}, port=event_port)

    log.info("Slave worker started for login=%s server=%s", acc.login, acc.server)
    record_event_log(f"Slave worker started ({acc.login}@{acc.server})", account_id=account_id, source="worker")

    async def _bus_loop() -> None:
        async for msg in bus_sub.listen():
            if not adapter.is_connected() and msg.topic != "control":
                continue
            try:
                subscriber.on_bus_message(msg)
            except Exception as e:  # noqa: BLE001
                log.exception("Slave %s error handling bus message %s: %s", account_id, msg.topic, e)

    bus_task = asyncio.create_task(_bus_loop())
    was_connected = False

    try:
        while True:
            ok, info, err = watchdog.ensure_connected()
            if not ok or info is None:
                if was_connected:
                    was_connected = False
                    enqueue_alert(
                        f"disc:{account_id}",
                        f"Slave account #{account_id} ({acc.login}@{acc.server}) disconnected: {err}",
                        account_id=account_id,
                    )
                _update_account_status(account_id, status="error" if err else "disconnected", last_error=err)
                await asyncio.sleep(1.5)
                continue

            if not was_connected:
                was_connected = True
                executor.reload_config()
                saved = load_master_snapshot_db()
                if saved is not None:
                    pos, ords, m_bal, _ = saved
                    reconcile_slave(executor, pos, ords, master_balance=m_bal, master_connected=True)
                record_event_log(
                    f"Slave connected & reconciled: {info.name} ({info.balance:,.2f} {info.currency})",
                    account_id=account_id,
                    source="worker",
                )

            _update_account_status(
                account_id,
                status="connected",
                last_error=None,
                account_name=info.name,
                currency=info.currency,
                balance=info.balance,
                equity=info.equity,
                leverage=info.leverage,
                margin_mode=info.margin_mode.value,
                last_sync_at=utcnow(),
            )
            await asyncio.sleep(2.0)
    finally:
        bus_task.cancel()
        await bus_sub.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MT5 account worker process")
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--connect-only", action="store_true", help="test connection, update DB, print JSON, exit")
    ap.add_argument("--bus-port", type=int, default=DEFAULT_EVENT_PORT)
    args = ap.parse_args(argv)

    init_db()
    if args.connect_only:
        setup_logging(f"account_{args.account_id}", console=False)
        res = accounts.connect_account(args.account_id, keep_connected=False)
        print(json.dumps({
            "success": res.success,
            "message": res.message,
            "warnings": res.warnings,
            "error_code": res.error_code,
        }))
        return 0 if res.success else 2

    setup_logging(f"account_{args.account_id}", console=True)
    acc = accounts.get_account(args.account_id)
    if acc.role == "master":
        asyncio.run(run_master_worker(args.account_id, event_port=args.bus_port))
    else:
        asyncio.run(run_slave_worker(args.account_id, event_port=args.bus_port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

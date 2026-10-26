"""Terminal health watchdog (architecture §8.2).

Runs inside each worker process. Checks terminal responsiveness and connection
status every 2 seconds, wraps calls with a hang timeout, and if the terminal
hangs (> ``TERMINAL_HANG_TIMEOUT_S``) or crashes, kills ``terminal64.exe`` for
that account's folder only and relaunches with exponential backoff.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Callable, TypeVar

from ..adapters.base import AccountInfo, BrokerAdapter, Credentials
from ..config import TERMINAL_HANG_TIMEOUT_S
from .provision import TERMINAL_EXE, kill_terminal_processes

log = logging.getLogger(__name__)
T = TypeVar("T")


class TerminalHangError(TimeoutError):
    pass


class TerminalWatchdog:
    def __init__(
        self,
        account_id: int,
        adapter: BrokerAdapter,
        creds_provider: Callable[[], Credentials],
        terminal_dir: Path | None,
        *,
        hang_timeout_s: float = TERMINAL_HANG_TIMEOUT_S,
        max_backoff_s: float = 60.0,
    ) -> None:
        self.account_id = account_id
        self.adapter = adapter
        self.creds_provider = creds_provider
        self.terminal_dir = Path(terminal_dir) if terminal_dir else None
        self.hang_timeout_s = hang_timeout_s
        self.max_backoff_s = max_backoff_s
        self.consecutive_failures = 0
        self.next_retry_at = 0.0
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"wd-{account_id}")

    def call_with_timeout(self, fn: Callable[[], T], timeout_s: float | None = None) -> T:
        t = timeout_s if timeout_s is not None else self.hang_timeout_s
        fut = self._executor.submit(fn)
        try:
            return fut.result(timeout=t)
        except concurrent.futures.TimeoutError as e:
            log.error("Account %s terminal call hung (> %.1fs); killing terminal process", self.account_id, t)
            self.force_kill_terminal()
            # Replace executor because the hung thread is blocked inside the C extension
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"wd-{self.account_id}"
            )
            raise TerminalHangError(f"MT5 terminal hung for >{t}s") from e

    def force_kill_terminal(self) -> int:
        try:
            self.adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if self.terminal_dir and self.terminal_dir.exists():
            return kill_terminal_processes(self.terminal_dir)
        return 0

    def ensure_connected(self) -> tuple[bool, AccountInfo | None, str | None]:
        """Verify connection or attempt reconnect if backoff timer has elapsed."""
        try:
            if self.call_with_timeout(self.adapter.is_connected):
                info = self.call_with_timeout(self.adapter.account_info)
                self.consecutive_failures = 0
                return True, info, None
        except Exception as e:  # noqa: BLE001
            log.warning("Account %s health check failed: %s", self.account_id, e)

        now = time.monotonic()
        if now < self.next_retry_at:
            return False, None, "waiting for reconnect backoff"

        try:
            creds = self.creds_provider()
            exe_path = (self.terminal_dir / TERMINAL_EXE) if self.terminal_dir else None
            info = self.call_with_timeout(
                lambda: self.adapter.connect(creds, exe_path),
                timeout_s=max(30.0, self.hang_timeout_s * 3),
            )
            self.consecutive_failures = 0
            self.next_retry_at = 0.0
            return True, info, None
        except Exception as e:  # noqa: BLE001
            self.consecutive_failures += 1
            backoff = min(self.max_backoff_s, 2.0 ** min(self.consecutive_failures, 6))
            self.next_retry_at = time.monotonic() + backoff
            return False, None, str(e)

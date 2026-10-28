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
        link_grace_s: float = 45.0,
        on_attempt: Callable[[bool, str | None], None] | None = None,
    ) -> None:
        self.account_id = account_id
        self.adapter = adapter
        self.creds_provider = creds_provider
        self.terminal_dir = Path(terminal_dir) if terminal_dir else None
        self.hang_timeout_s = hang_timeout_s
        self.max_backoff_s = max_backoff_s
        self.link_grace_s = link_grace_s
        self.on_attempt = on_attempt
        self.consecutive_failures = 0
        self.next_retry_at = 0.0
        self.last_error: str | None = None
        self.link_down_since: float | None = None
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

    def request_reconnect(self) -> None:
        """Skip any remaining backoff so the next ``ensure_connected`` retries immediately."""
        self.next_retry_at = 0.0
        self.link_down_since = None

    def ensure_connected(self) -> tuple[bool, AccountInfo | None, str | None]:
        """Verify connection or attempt reconnect if backoff timer has elapsed.

        The returned error string is always the *real* reason of the last failed
        attempt (never a placeholder), so the dashboard shows something actionable.
        """
        ipc_alive = False
        try:
            if self.call_with_timeout(self.adapter.is_connected):
                info = self.call_with_timeout(self.adapter.account_info)
                self.consecutive_failures = 0
                self.link_down_since = None
                self.last_error = None
                return True, info, None
            ipc_alive = bool(self.call_with_timeout(self.adapter.terminal_alive))
        except Exception as e:  # noqa: BLE001
            log.warning("Account %s health check failed: %s", self.account_id, e)

        now = time.monotonic()

        # Terminal process + IPC are fine but its link to the broker dropped (server
        # restart, Wi-Fi blip). MT5 reconnects on its own; re-initialising would only
        # tear down a healthy terminal, so give it a grace period first.
        if ipc_alive:
            if self.link_down_since is None:
                self.link_down_since = now
                log.warning("Account %s: terminal lost broker connection; waiting for it to reconnect",
                            self.account_id)
            waited = now - self.link_down_since
            if waited < self.link_grace_s:
                return False, None, f"Terminal reconnecting to broker server ({waited:.0f}s)"
            log.warning("Account %s: broker link down for %.0fs; re-initialising terminal",
                        self.account_id, waited)
            self.link_down_since = None
            self.next_retry_at = 0.0

        if now < self.next_retry_at:
            reason = self.last_error or "connect failed"
            return False, None, f"{reason} (retrying in {self.next_retry_at - now:.0f}s)"

        try:
            creds = self.creds_provider()
            exe_path = (self.terminal_dir / TERMINAL_EXE) if self.terminal_dir else None
            info = self.call_with_timeout(
                lambda: self.adapter.connect(creds, exe_path),
                timeout_s=max(30.0, self.hang_timeout_s * 3),
            )
            self.consecutive_failures = 0
            self.next_retry_at = 0.0
            self.last_error = None
            self._notify_attempt(True, None)
            log.info("Account %s connected (%s)", self.account_id, getattr(info, "name", ""))
            return True, info, None
        except Exception as e:  # noqa: BLE001
            self.consecutive_failures += 1
            backoff = min(self.max_backoff_s, 2.0 ** min(self.consecutive_failures, 6))
            self.next_retry_at = time.monotonic() + backoff
            msg = str(e)
            hint = getattr(e, "hint", None)
            if hint and hint not in msg:
                msg = f"{msg}. {hint}"
            self.last_error = msg
            log.warning("Account %s connect attempt %d failed: %s (next retry in %.0fs)",
                        self.account_id, self.consecutive_failures, msg, backoff)
            self._notify_attempt(False, msg)
            return False, None, msg

    def _notify_attempt(self, ok: bool, err: str | None) -> None:
        if self.on_attempt is None:
            return
        try:
            self.on_attempt(ok, err)
        except Exception:  # noqa: BLE001
            log.exception("on_attempt callback failed")

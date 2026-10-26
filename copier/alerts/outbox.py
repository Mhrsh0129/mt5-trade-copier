"""Persistent alert outbox with 5-minute deduplication and offline queueing (§8.1, §8.3).

Alerts queued while the laptop has no internet remain in SQLite with
``sent_at IS NULL`` and are flushed in order as soon as connectivity returns.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

from sqlalchemy import select

from ..db import AlertOutbox, EventLog, session_scope
from ..db.models import utcnow
from .telegram import get_telegram_config, send_telegram_message

log = logging.getLogger(__name__)

DEDUPE_WINDOW_S = 300
MAX_SEND_ATTEMPTS = 20


def enqueue_alert(dedupe_key: str | None, text: str, level: str = "warn",
                  account_id: int | None = None) -> int | None:
    """Record an event in ``events_log`` and enqueue a Telegram alert (deduped over 5 min)."""
    now = utcnow()
    with session_scope() as s:
        s.add(EventLog(ts=now, account_id=account_id, level=level, source="alert", message=text))
        if dedupe_key:
            cutoff = now - timedelta(seconds=DEDUPE_WINDOW_S)
            recent = s.scalar(
                select(AlertOutbox).where(
                    AlertOutbox.dedupe_key == dedupe_key,
                    AlertOutbox.created_at >= cutoff,
                )
            )
            if recent is not None:
                return None
        row = AlertOutbox(created_at=now, dedupe_key=dedupe_key, level=level, text=text)
        s.add(row)
        s.flush()
        return row.id


def record_event_log(message: str, *, level: str = "info", source: str = "system",
                     account_id: int | None = None) -> None:
    with session_scope() as s:
        s.add(EventLog(ts=utcnow(), account_id=account_id, level=level, source=source, message=message))


def flush_outbox(
    sender: Callable[[str], tuple[bool, str]] | None = None,
    *,
    online: bool = True,
) -> int:
    """Send all pending alerts if ``online`` and Telegram is configured. Returns count sent."""
    if not online:
        return 0
    if sender is None:
        tok, cid = get_telegram_config()
        if not tok or not cid:
            return 0
        sender = lambda txt: send_telegram_message(txt, bot_token=tok, chat_id=cid)  # noqa: E731

    sent_count = 0
    with session_scope() as s:
        pending = list(
            s.scalars(
                select(AlertOutbox)
                .where(AlertOutbox.sent_at.is_(None), AlertOutbox.attempts < MAX_SEND_ATTEMPTS)
                .order_by(AlertOutbox.id)
            )
        )
        for row in pending:
            row.attempts = int(row.attempts or 0) + 1
            ok, _ = sender(row.text)
            if ok:
                row.sent_at = utcnow()
                sent_count += 1
            else:
                # Stop trying further messages this tick if network/API failed
                break
    return sent_count

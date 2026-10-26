"""Telegram Bot HTTP sender and external heartbeat ping (architecture §8.1, §8.4)."""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from typing import Tuple

from .. import crypto
from ..db import AppSetting, session_scope
from ..logging_setup import register_secret

log = logging.getLogger(__name__)


def set_telegram_config(bot_token: str, chat_id: str) -> None:
    bot_token = bot_token.strip()
    chat_id = chat_id.strip()
    with session_scope() as s:
        if bot_token:
            register_secret(bot_token)
            s.merge(AppSetting(key="telegram_bot_token", value=None, value_enc=crypto.encrypt(bot_token)))
        else:
            s.merge(AppSetting(key="telegram_bot_token", value=None, value_enc=None))
        s.merge(AppSetting(key="telegram_chat_id", value=chat_id or None))


def get_telegram_config() -> Tuple[str | None, str | None]:
    """Returns ``(bot_token, chat_id)``."""
    with session_scope() as s:
        tok_row = s.get(AppSetting, "telegram_bot_token")
        chat_row = s.get(AppSetting, "telegram_chat_id")
        token = None
        if tok_row and tok_row.value_enc:
            try:
                token = crypto.decrypt(tok_row.value_enc)
                register_secret(token)
            except Exception:  # noqa: BLE001
                token = None
        chat_id = chat_row.value if chat_row else None
        return token, chat_id


def set_heartbeat_url(url: str) -> None:
    with session_scope() as s:
        s.merge(AppSetting(key="heartbeat_url", value=url.strip() or None))


def get_heartbeat_url() -> str | None:
    with session_scope() as s:
        row = s.get(AppSetting, "heartbeat_url")
        return row.value if row else None


def send_telegram_message(text: str, *, bot_token: str | None = None,
                          chat_id: str | None = None, timeout: float = 8.0) -> tuple[bool, str]:
    if bot_token is None or chat_id is None:
        tok, cid = get_telegram_config()
        bot_token = bot_token or tok
        chat_id = chat_id or cid
    if not bot_token or not chat_id:
        return False, "Telegram bot_token or chat_id not configured"
    register_secret(bot_token)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"[TradeCopier] {text}"}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if 200 <= resp.status < 300:
                return True, "sent"
            return False, f"HTTP {resp.status}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def ping_heartbeat(*, healthy: bool = True, reason: str = "", url: str | None = None,
                   timeout: float = 5.0) -> tuple[bool, str]:
    """Ping healthchecks.io (or compatible URL). Sends ``/fail`` when unhealthy."""
    base_url = (url if url is not None else get_heartbeat_url()) or ""
    base_url = base_url.strip().rstrip("/")
    if not base_url:
        return False, "Heartbeat URL not configured"
    target = base_url if healthy else f"{base_url}/fail"
    body = reason.encode("utf-8")[:1000] if reason else None
    req = urllib.request.Request(target, data=body, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)

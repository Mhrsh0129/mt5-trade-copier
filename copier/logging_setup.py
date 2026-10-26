"""Structured JSON logging with secret redaction.

Every process calls :func:`setup_logging` once. Records go to
``logs/<name>.jsonl`` (rotated daily, 14 days kept) and to stderr.

Redaction is defense in depth: code must never pass passwords to the logger,
but if it does, :class:`RedactingFilter` scrubs them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import get_paths

_SENSITIVE_KEYS = ("password", "passwd", "pwd", "token", "secret", "api_key", "apikey")
_KV_RE = re.compile(
    r"(?i)(\w*(?:" + "|".join(_SENSITIVE_KEYS) + r")\w*)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}&]+)"
)
_TG_TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
REDACTED = "***"

# Exact secret values registered at runtime (e.g. a decrypted password) are scrubbed too.
_known_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 3:
        with _lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    text = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _TG_TOKEN_RE.sub(REDACTED, text)
    with _lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, REDACTED)
    return text


def _redact_obj(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else _redact_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_redact_obj(v) for v in obj)
    if isinstance(obj, str):
        return redact(obj)
    return obj


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        extra = getattr(record, "ctx", None)
        if extra is not None:
            record.ctx = _redact_obj(extra)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for attr in ("account_id", "ctx"):
            val = getattr(record, attr, None)
            if val is not None:
                data[attr] = val
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, default=str, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")


def setup_logging(name: str, level: int = logging.INFO, console: bool = True,
                  logs_dir: Path | None = None) -> logging.Logger:
    """Configure root logging for this process. ``name`` -> logs/<name>.jsonl."""
    logs_dir = logs_dir or get_paths().logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    redactor = RedactingFilter()
    fh = TimedRotatingFileHandler(logs_dir / f"{name}.jsonl", when="midnight", backupCount=14,
                                  encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.addFilter(redactor)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(_ConsoleFormatter())
        ch.addFilter(redactor)
        root.addHandler(ch)
    return logging.getLogger(name)


class _AccountAdapter(logging.LoggerAdapter):
    """Stamps account_id while keeping per-call ``extra`` (e.g. ``ctx``)."""

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **(kwargs.get("extra") or {})}
        return msg, kwargs


def account_logger(account_id: int, base: str = "copier.account") -> logging.LoggerAdapter:
    """Logger that stamps ``account_id`` on every record."""
    return _AccountAdapter(logging.getLogger(f"{base}.{account_id}"), {"account_id": account_id})

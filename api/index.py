"""Vercel serverless entry point for the FastAPI dashboard."""

from __future__ import annotations

from copier import crypto
from copier.db import init_db

crypto.ensure_key()
init_db()

from copier.web.app import app  # noqa: E402, F401

"""Vercel serverless entry point for the FastAPI dashboard."""

from __future__ import annotations

import os

from copier import crypto
from copier.auth import admin_exists, set_admin_password
from copier.db import init_db

crypto.ensure_key()
init_db()
if not admin_exists():
    set_admin_password(os.environ.get("TC_ADMIN_PASSWORD", "Admin@123456"))

from copier.web.app import app  # noqa: E402, F401

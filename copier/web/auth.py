"""Session cookies, CSRF protection, and login rate limiting for the dashboard (§8.6)."""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import auth as admin_auth
from .. import crypto
from ..db import AppSetting, session_scope

SESSION_COOKIE = "tc_session"
SESSION_MAX_AGE = 86_400 * 7  # 7 days
_RATE_WINDOW_S = 300
_MAX_FAILED_LOGINS = 8
_failed_logins: dict[str, list[float]] = defaultdict(list)


def _session_secret() -> str:
    """Derive/store an encrypted session signing secret in AppSetting."""
    crypto.ensure_key()
    with session_scope() as s:
        row = s.get(AppSetting, "web_session_secret")
        if row and row.value_enc:
            return crypto.decrypt(row.value_enc)
        sec = secrets.token_hex(32)
        s.merge(AppSetting(key="web_session_secret", value_enc=crypto.encrypt(sec)))
        return sec


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_session_secret(), salt="tc-web-session")


def create_session_token(username: str = "admin") -> tuple[str, str]:
    """Returns ``(cookie_value, csrf_token)``."""
    csrf = secrets.token_hex(16)
    token = _serializer().dumps({"u": username, "csrf": csrf})
    return token, csrf


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=SESSION_MAX_AGE)
        return data if isinstance(data, dict) and data.get("u") else None
    except BadSignature:
        return None


def require_user(request: Request) -> dict:
    sess = read_session(request)
    if not sess:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return sess


def verify_csrf(request: Request, form_csrf: str | None) -> None:
    sess = require_user(request)
    expected = sess.get("csrf", "")
    if not expected or not form_csrf or not hmac.compare_digest(str(expected), str(form_csrf)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def check_login_rate_limit(client_ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failed_logins[client_ip] if (now - t) < _RATE_WINDOW_S]
    _failed_logins[client_ip] = recent
    return len(recent) < _MAX_FAILED_LOGINS


def record_login_result(client_ip: str, ok: bool) -> None:
    if ok:
        _failed_logins.pop(client_ip, None)
    else:
        _failed_logins[client_ip].append(time.monotonic())


def authenticate(client_ip: str, password: str) -> tuple[bool, str]:
    if not check_login_rate_limit(client_ip):
        return False, "Too many failed login attempts. Try again in 5 minutes."
    if not admin_auth.admin_exists():
        # First-time bootstrap if admin wasn't set during CLI init
        if len(password) >= admin_auth.MIN_PASSWORD_LEN:
            admin_auth.set_admin_password(password)
            record_login_result(client_ip, True)
            return True, "ok"
        return False, f"No admin password set yet. Enter a password of at least {admin_auth.MIN_PASSWORD_LEN} characters to initialize."
    ok = admin_auth.verify_admin_password(password)
    record_login_result(client_ip, ok)
    return (True, "ok") if ok else (False, "Invalid admin password.")

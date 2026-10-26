"""Admin password hashing (argon2 if installed, PBKDF2-HMAC-SHA256 fallback)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from sqlalchemy import select

from .db import AdminUser, session_scope

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    _ph = PasswordHasher()
except ImportError:  # pragma: no cover
    _ph = None

MIN_PASSWORD_LEN = 8
_PBKDF2_ITERS = 600_000


def _hash_password(password: str) -> str:
    if _ph is not None:
        return _ph.hash(password)
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt}${dk.hex()}"


def _verify_hash(password_hash: str, password: str) -> tuple[bool, bool]:
    """Returns (valid, needs_rehash)."""
    if password_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iters_s, salt, digest_hex = password_hash.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(iters_s))
            ok = hmac.compare_digest(dk.hex(), digest_hex)
            return ok, (ok and _ph is not None)
        except Exception:  # noqa: BLE001
            return False, False
    if _ph is not None:
        try:
            _ph.verify(password_hash, password)
            return True, _ph.check_needs_rehash(password_hash)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False
    return False, False


def set_admin_password(password: str, username: str = "admin") -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Admin password must be at least {MIN_PASSWORD_LEN} characters")
    hashed = _hash_password(password)
    with session_scope() as s:
        user = s.scalar(select(AdminUser).where(AdminUser.username == username))
        if user is None:
            s.add(AdminUser(username=username, password_hash=hashed))
        else:
            user.password_hash = hashed


def verify_admin_password(password: str, username: str = "admin") -> bool:
    with session_scope() as s:
        user = s.scalar(select(AdminUser).where(AdminUser.username == username))
        if user is None:
            return False
        ok, rehash = _verify_hash(user.password_hash, password)
        if not ok:
            return False
        if rehash:
            user.password_hash = _hash_password(password)
        return True


def admin_exists() -> bool:
    with session_scope() as s:
        return s.scalar(select(AdminUser)) is not None

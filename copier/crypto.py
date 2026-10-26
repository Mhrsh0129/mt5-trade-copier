"""Credential encryption at rest.

* Broker passwords (and the Telegram token) are encrypted with Fernet and stored
  in the DB as ciphertext.
* The Fernet key lives in a separate file, outside the DB. On Windows the key
  file itself is wrapped with DPAPI (machine scope), so copying the file to
  another machine is useless. On other platforms (tests/dev) it is stored raw
  with restrictive permissions.
"""

from __future__ import annotations

import ctypes
import os
import sys
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import get_paths

_DPAPI_MAGIC = b"TCDPAPI1"
_RAW_MAGIC = b"TCRAWKEY"


# ---------------------------------------------------------------- DPAPI (Windows)
if sys.platform == "win32":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01
    _CRYPTPROTECT_LOCAL_MACHINE = 0x04

    def _blob(data: bytes) -> _DataBlob:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _dpapi_protect(data: bytes) -> bytes:
        src, out = _blob(data), _DataBlob()
        flags = _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE
        if not _crypt32.CryptProtectData(ctypes.byref(src), "TradeCopier", None, None, None,
                                         flags, ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _dpapi_unprotect(data: bytes) -> bytes:
        src, out = _blob(data), _DataBlob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                           _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    HAS_DPAPI = True
else:  # pragma: no cover - non-Windows dev only
    HAS_DPAPI = False


# ---------------------------------------------------------------- key file
def _write_key_file(path: Path, key: bytes, use_dpapi: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _DPAPI_MAGIC + _dpapi_protect(key) if use_dpapi else _RAW_MAGIC + key
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_key_file(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(_DPAPI_MAGIC):
        if not HAS_DPAPI:
            raise RuntimeError("Key file is DPAPI-protected but DPAPI is unavailable")
        return _dpapi_unprotect(raw[len(_DPAPI_MAGIC):])
    if raw.startswith(_RAW_MAGIC):
        return raw[len(_RAW_MAGIC):]
    raise RuntimeError(f"Unrecognised key file format: {path}")


def ensure_key(path: Path | None = None, use_dpapi: bool | None = None) -> bool:
    """Create the key file if missing. Returns True if a new key was created."""
    path = path or get_paths().key_file
    if path.exists():
        return False
    _write_key_file(path, Fernet.generate_key(), HAS_DPAPI if use_dpapi is None else use_dpapi)
    return True


@lru_cache(maxsize=4)
def _fernet(path_str: str) -> Fernet:
    path = Path(path_str)
    if not path.exists():
        raise RuntimeError(f"Encryption key not found at {path}. Run setup.ps1 or `tc init`.")
    return Fernet(_read_key_file(path))


def get_fernet(path: Path | None = None) -> Fernet:
    return _fernet(str(path or get_paths().key_file))


def encrypt(plaintext: str, key_path: Path | None = None) -> bytes:
    return get_fernet(key_path).encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes, key_path: Path | None = None) -> str:
    try:
        return get_fernet(key_path).decrypt(token).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError("Could not decrypt credential: wrong or rotated key") from e


def clear_cache() -> None:
    _fernet.cache_clear()

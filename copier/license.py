"""Single-device hardware-bound (HWID) + expiry license key engine.

How it works:
1. Every Windows PC running ``TradeCopier.exe`` has a deterministic hardware
   **Device ID** (``TC-HWID-XXXX-XXXX-XXXX``) derived from the OS MachineGuid.
2. You (the vendor) hold the private Ed25519 signing key in
   ``C:\\ProgramData\\TradeCopier\\secrets\\license_vendor_private.pem``
   (never shipped to clients and never bundled in ``TradeCopier.exe``).
3. ``TradeCopier.exe`` embeds only the 32-byte Ed25519 **Public Key**, so it can
   verify license keys offline while making it impossible for anyone to forge or
   modify a key's expiry date or Device ID.
4. Each license key (``TCLK-<payload>.<signature>``) encodes:
   - ``hwid``: the single Device ID it is locked to
   - ``exp``: Unix UTC expiry timestamp
   - ``client``: client name / label
   - ``max_slaves``: maximum slave accounts allowed (default 10)
5. When a client's subscription expires, you generate a new key for their
   Device ID with the new expiry date. They simply paste the new key into the
   **/license** page in the dashboard and click **Activate / Renew License** —
   no reinstall or `.exe` rebuild needed, and all their accounts/rules stay intact.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import get_paths
from .db import AppSetting, session_scope

# Embedded vendor Ed25519 public key (32 bytes hex). Private key stays only on vendor machine.
VENDOR_PUBLIC_KEY_HEX = "2baa4174998cf9a7f9ca57850d68cb3ecfcb088db59173d24072af2595a17a31"
LICENSE_PREFIX = "TCLK-"
CLOCK_ROLLBACK_TOLERANCE_S = 3600  # 1 hour tolerance for DST/NTP drift


@dataclass
class LicenseStatus:
    valid: bool
    reason: str
    device_id: str
    bound_device_id: str | None = None
    client_name: str | None = None
    expires_at: datetime | None = None
    days_left: float = 0.0
    expired: bool = False
    max_slaves: int = 10
    raw_key: str | None = None


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def normalize_device_id(hwid: str) -> str:
    clean = "".join(ch for ch in hwid.upper().strip() if ch.isalnum())
    if clean.startswith("TCHWID"):
        clean = clean[6:]
    if len(clean) >= 12:
        clean = clean[:12]
        return f"TC-HWID-{clean[0:4]}-{clean[4:8]}-{clean[8:12]}"
    return hwid.upper().strip()


def _read_windows_machine_guid() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
            if val:
                return str(val).strip()
    except Exception:  # noqa: BLE001
        return None
    return None


def get_device_id() -> str:
    """Return this machine's deterministic hardware Device ID (``TC-HWID-XXXX-XXXX-XXXX``)."""
    override = os.environ.get("TC_DEVICE_ID")
    if override:
        return normalize_device_id(override)
    raw_guid = _read_windows_machine_guid()
    if not raw_guid:
        raw_guid = f"{platform.node()}-{uuid.getnode()}"
    digest = hashlib.sha256(f"TradeCopier-HWID-v1:{raw_guid}".encode("utf-8")).hexdigest().upper()
    return f"TC-HWID-{digest[0:4]}-{digest[4:8]}-{digest[8:12]}"


def vendor_private_key_path() -> Path:
    """Return the path to the vendor's private signing key (only exists on the admin's machine)."""
    env_path = os.environ.get("TC_VENDOR_PRIVATE_KEY_FILE")
    if env_path:
        return Path(env_path)
    # Check both current data_dir/secrets and standard ProgramData\TradeCopier\secrets
    p1 = get_paths().secrets_dir / "license_vendor_private.pem"
    if p1.exists():
        return p1
    prog = os.environ.get("ProgramData")
    if prog:
        p2 = Path(prog) / "TradeCopier" / "secrets" / "license_vendor_private.pem"
        if p2.exists():
            return p2
    return p1


def has_vendor_private_key() -> bool:
    """True only on the vendor's admin machine (never in client .exe distributions)."""
    if getattr(sys, "frozen", False) and not os.environ.get("TC_ALLOW_FROZEN_KEYGEN"):
        return False
    return vendor_private_key_path().exists()


def _load_private_key(private_key_pem: bytes | None = None) -> Ed25519PrivateKey:
    pem = private_key_pem
    if pem is None:
        path = vendor_private_key_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Vendor private signing key not found at {path}. "
                "License keys can only be generated on the vendor's admin machine."
            )
        pem = path.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Invalid vendor private key type (expected Ed25519)")
    return key


def _load_public_key(public_key_hex: str | None = None) -> Ed25519PublicKey:
    hex_str = public_key_hex or os.environ.get("TC_VENDOR_PUBLIC_KEY_HEX") or VENDOR_PUBLIC_KEY_HEX
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_str))


def issue_license_key(
    *,
    device_id: str,
    days: int | float | None = None,
    expires_at: datetime | str | None = None,
    client_name: str = "Client",
    max_slaves: int = 10,
    private_key_pem: bytes | None = None,
) -> str:
    """Sign and return a single-device license key (``TCLK-...``)."""
    if not device_id or not device_id.strip():
        raise ValueError("Device ID (HWID) is required")
    norm_hwid = normalize_device_id(device_id)

    now = datetime.now(timezone.utc)
    if expires_at is not None:
        if isinstance(expires_at, str):
            s = expires_at.strip()
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                exp_dt = datetime.fromisoformat(s + "T23:59:59+00:00")
            else:
                exp_dt = datetime.fromisoformat(s)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        else:
            exp_dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    elif days is not None:
        exp_dt = now + timedelta(days=float(days))
    else:
        exp_dt = now + timedelta(days=30)

    payload = {
        "v": 1,
        "hwid": norm_hwid,
        "exp": int(exp_dt.timestamp()),
        "iat": int(now.timestamp()),
        "client": (client_name or "Client").strip()[:64],
        "max_slaves": max(1, min(int(max_slaves), 50)),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    priv = _load_private_key(private_key_pem)
    sig = priv.sign(payload_bytes)
    return f"{LICENSE_PREFIX}{_b64u_encode(payload_bytes)}.{_b64u_encode(sig)}"


def verify_license_key(
    key_str: str | None,
    *,
    current_device_id: str | None = None,
    now_ts: float | None = None,
    last_seen_ts: float | None = None,
    public_key_hex: str | None = None,
) -> LicenseStatus:
    """Cryptographically verify a license key against this machine's Device ID and current UTC time."""
    dev_id = normalize_device_id(current_device_id or get_device_id())
    if not key_str or not key_str.strip():
        return LicenseStatus(
            valid=False,
            reason="No license key activated on this device. Enter your license key to activate.",
            device_id=dev_id,
        )

    token = key_str.strip()
    if token.upper().startswith(LICENSE_PREFIX):
        token = token[len(LICENSE_PREFIX):]
    if "." not in token:
        return LicenseStatus(
            valid=False,
            reason="Malformed license key format (expected TCLK-<payload>.<signature>).",
            device_id=dev_id,
        )

    part_payload, part_sig = token.split(".", 1)
    try:
        payload_bytes = _b64u_decode(part_payload)
        sig_bytes = _b64u_decode(part_sig)
        pub = _load_public_key(public_key_hex)
        pub.verify(sig_bytes, payload_bytes)
        data = json.loads(payload_bytes.decode("utf-8"))
    except InvalidSignature:
        return LicenseStatus(
            valid=False,
            reason="Invalid license key signature (key was tampered with or not issued by vendor).",
            device_id=dev_id,
        )
    except Exception:  # noqa: BLE001
        return LicenseStatus(
            valid=False,
            reason="Corrupted license key.",
            device_id=dev_id,
        )

    bound_hwid = normalize_device_id(str(data.get("hwid", "")))
    client_name = str(data.get("client", "Client"))
    exp_ts = int(data.get("exp", 0))
    max_slaves = int(data.get("max_slaves", 10))
    exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc)

    # 1. Single-device enforcement
    if bound_hwid != dev_id:
        return LicenseStatus(
            valid=False,
            reason=(
                f"This license key is locked to another device ({bound_hwid}) "
                f"and cannot be used on this device ({dev_id})."
            ),
            device_id=dev_id,
            bound_device_id=bound_hwid,
            client_name=client_name,
            expires_at=exp_dt,
            max_slaves=max_slaves,
            raw_key=key_str.strip(),
        )

    # 2. Clock rollback protection
    now = float(now_ts if now_ts is not None else time.time())
    if last_seen_ts is not None and now + CLOCK_ROLLBACK_TOLERANCE_S < float(last_seen_ts):
        return LicenseStatus(
            valid=False,
            reason="System clock rollback detected. Restore the correct Windows date & time.",
            device_id=dev_id,
            bound_device_id=bound_hwid,
            client_name=client_name,
            expires_at=exp_dt,
            max_slaves=max_slaves,
            raw_key=key_str.strip(),
        )

    # 3. Expiry date check
    remaining_s = exp_ts - now
    if remaining_s <= 0:
        return LicenseStatus(
            valid=False,
            expired=True,
            days_left=0.0,
            reason=(
                f"Subscription expired on {exp_dt.strftime('%Y-%m-%d %H:%M UTC')}. "
                "Enter a renewed license key below to continue copying."
            ),
            device_id=dev_id,
            bound_device_id=bound_hwid,
            client_name=client_name,
            expires_at=exp_dt,
            max_slaves=max_slaves,
            raw_key=key_str.strip(),
        )

    days_left = round(remaining_s / 86400.0, 1)
    return LicenseStatus(
        valid=True,
        reason=f"Active ({days_left:.1f} days remaining, expires {exp_dt.strftime('%Y-%m-%d')})",
        device_id=dev_id,
        bound_device_id=bound_hwid,
        client_name=client_name,
        expires_at=exp_dt,
        days_left=days_left,
        expired=False,
        max_slaves=max_slaves,
        raw_key=key_str.strip(),
    )


def activate_license_key(key_str: str) -> LicenseStatus:
    """Validate ``key_str`` for this device; if valid, save it in SQLite so the subscription activates/renews."""
    with session_scope() as s:
        row_seen = s.get(AppSetting, "license_last_seen_ts")
        last_seen = float(row_seen.value) if (row_seen and row_seen.value) else None

    status = verify_license_key(key_str, last_seen_ts=last_seen)
    if not status.valid:
        return status

    now = time.time()
    with session_scope() as s:
        s.merge(AppSetting(key="license_key", value=key_str.strip()))
        high_water = max(now, last_seen or 0.0)
        s.merge(AppSetting(key="license_last_seen_ts", value=str(int(high_water))))
    return status


def get_installed_license_status() -> LicenseStatus:
    """Check the currently stored license key in SQLite and update the anti-rollback timestamp."""
    if os.environ.get("TC_SKIP_LICENSE_CHECK") == "1":
        dev_id = get_device_id()
        return LicenseStatus(
            valid=True,
            reason="Test mode (license check bypassed)",
            device_id=dev_id,
            bound_device_id=dev_id,
            client_name="TestUser",
            expires_at=datetime.now(timezone.utc) + timedelta(days=365),
            days_left=365.0,
            max_slaves=10,
        )

    with session_scope() as s:
        row_key = s.get(AppSetting, "license_key")
        row_seen = s.get(AppSetting, "license_last_seen_ts")
        saved_key = row_key.value if row_key else None
        last_seen = float(row_seen.value) if (row_seen and row_seen.value) else None

    now = time.time()
    status = verify_license_key(saved_key, now_ts=now, last_seen_ts=last_seen)
    if status.valid and (last_seen is None or now > last_seen + 60):
        with session_scope() as s:
            s.merge(AppSetting(key="license_last_seen_ts", value=str(int(now))))
    return status

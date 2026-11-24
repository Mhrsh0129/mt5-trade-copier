"""Tests for single-device (HWID) + expiry license key engine and web endpoints."""

from __future__ import annotations

import re
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from copier import auth
from copier.license import (
    activate_license_key,
    get_installed_license_status,
    issue_license_key,
    verify_license_key,
)
from copier.web.app import app


@pytest.fixture
def vendor_keypair(monkeypatch, tmp_path):
    """Temporary Ed25519 keypair for isolated license tests."""
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    priv_file = tmp_path / "vendor_priv.pem"
    priv_file.write_bytes(pem)

    monkeypatch.setenv("TC_SKIP_LICENSE_CHECK", "0")
    monkeypatch.setenv("TC_VENDOR_PRIVATE_KEY_FILE", str(priv_file))
    monkeypatch.setenv("TC_VENDOR_PUBLIC_KEY_HEX", pub_hex)
    monkeypatch.setenv("TC_DEVICE_ID", "TC-HWID-AAAA-BBBB-CCCC")
    return pem, pub_hex


def test_single_device_lock_rejects_other_device(vendor_keypair):
    key = issue_license_key(
        device_id="TC-HWID-AAAA-BBBB-CCCC",
        days=30,
        client_name="Rahul",
    )
    # Same device -> valid
    st_ok = verify_license_key(key, current_device_id="TC-HWID-AAAA-BBBB-CCCC")
    assert st_ok.valid is True
    assert st_ok.client_name == "Rahul"
    assert st_ok.days_left >= 29.9

    # Different device -> rejected
    st_other = verify_license_key(key, current_device_id="TC-HWID-1111-2222-3333")
    assert st_other.valid is False
    assert "locked to another device" in st_other.reason


def test_license_expiry_and_seamless_renewal(db, vendor_keypair):
    # 1. Expired key -> rejected
    expired_key = issue_license_key(
        device_id="TC-HWID-AAAA-BBBB-CCCC",
        days=-1,
        client_name="Rahul",
    )
    st_exp = verify_license_key(expired_key)
    assert st_exp.valid is False and st_exp.expired is True
    assert "expired" in st_exp.reason.lower()

    # 2. Activate 10-day key -> valid
    key_10d = issue_license_key(
        device_id="TC-HWID-AAAA-BBBB-CCCC",
        days=10,
        client_name="Rahul",
    )
    st_act = activate_license_key(key_10d)
    assert st_act.valid is True and 9.5 <= st_act.days_left <= 10.1
    assert get_installed_license_status().valid is True

    # 3. Paste a renewed 90-day key -> subscription extends immediately
    key_90d = issue_license_key(
        device_id="TC-HWID-AAAA-BBBB-CCCC",
        days=90,
        client_name="Rahul",
    )
    st_renew = activate_license_key(key_90d)
    assert st_renew.valid is True and st_renew.days_left >= 89.5
    assert get_installed_license_status().days_left >= 89.5


def test_tampered_key_and_clock_rollback_rejected(vendor_keypair):
    key = issue_license_key(device_id="TC-HWID-AAAA-BBBB-CCCC", days=30, client_name="A")
    # Flip a character in the signature
    tampered = key[:-2] + ("AA" if not key.endswith("AA") else "BB")
    st_tamp = verify_license_key(tampered)
    assert st_tamp.valid is False and "signature" in st_tamp.reason.lower()

    # Clock rolled back by 3 hours compared to last_seen_ts
    now = time.time()
    st_clock = verify_license_key(key, now_ts=now - 3 * 3600, last_seen_ts=now)
    assert st_clock.valid is False and "rollback" in st_clock.reason.lower()


def test_web_license_activation_and_generator(db, vendor_keypair):
    auth.set_admin_password("Admin@123456")
    c = TestClient(app)
    r = c.post("/login", data={"password": "Admin@123456"}, follow_redirects=False)
    assert r.status_code == 303

    # Before activating license, /health reports license_valid=False
    h0 = c.get("/health").json()
    assert h0["license_valid"] is False
    assert h0["device_id"] == "TC-HWID-AAAA-BBBB-CCCC"

    # Visit /license and generate a key for this device
    page = c.get("/license").text
    assert "TC-HWID-AAAA-BBBB-CCCC" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    gen_resp = c.post(
        "/license/generate",
        data={
            "csrf": csrf,
            "target_device_id": "TC-HWID-AAAA-BBBB-CCCC",
            "client_name": "VIP Client",
            "days": "45",
            "max_slaves": "10",
        },
    )
    assert gen_resp.status_code == 200
    m_key = re.search(r"(TCLK-[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", gen_resp.text)
    assert m_key, "Expected generated TCLK-... key in response"
    generated_key = m_key.group(1)

    # Activate the generated key
    act_resp = c.post(
        "/license/activate",
        data={"csrf": csrf, "license_key": generated_key},
        follow_redirects=False,
    )
    assert act_resp.status_code == 303 and "ok=" in act_resp.headers["location"]

    # Now /health reports license_valid=True
    h1 = c.get("/health").json()
    assert h1["license_valid"] is True and h1["license_days_left"] >= 44.5

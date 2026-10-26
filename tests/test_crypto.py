import sys

import pytest
from cryptography.fernet import Fernet

from copier import config, crypto


def test_key_created_once_and_roundtrip(data_dir):
    assert crypto.ensure_key() is True
    assert crypto.ensure_key() is False
    token = crypto.encrypt("s3cret-Pass!")
    assert b"s3cret" not in token
    assert crypto.decrypt(token) == "s3cret-Pass!"


def test_key_lives_outside_db(data_dir):
    crypto.ensure_key()
    paths = config.get_paths()
    assert paths.key_file.exists()
    assert paths.key_file.parent != paths.db_file.parent or paths.key_file.name != paths.db_file.name
    assert paths.key_file.parent.name == "secrets"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_key_file_is_dpapi_wrapped(data_dir):
    crypto.ensure_key(use_dpapi=True)
    raw = config.get_paths().key_file.read_bytes()
    assert raw.startswith(b"TCDPAPI1")
    key = crypto._read_key_file(config.get_paths().key_file)
    Fernet(key)  # valid Fernet key
    assert key not in raw  # raw key bytes never on disk


def test_raw_key_mode(tmp_path):
    p = tmp_path / "k.key"
    crypto.ensure_key(p, use_dpapi=False)
    assert p.read_bytes().startswith(b"TCRAWKEY")
    assert crypto.decrypt(crypto.encrypt("x", p), p) == "x"


def test_wrong_key_fails_cleanly(tmp_path):
    a, b = tmp_path / "a.key", tmp_path / "b.key"
    crypto.ensure_key(a, use_dpapi=False)
    crypto.ensure_key(b, use_dpapi=False)
    token = crypto.encrypt("pw", a)
    with pytest.raises(RuntimeError, match="wrong or rotated key"):
        crypto.decrypt(token, b)


def test_missing_key_message(data_dir):
    with pytest.raises(RuntimeError, match="Encryption key not found"):
        crypto.encrypt("x")

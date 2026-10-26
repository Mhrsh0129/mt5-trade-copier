import pytest

from copier import auth


def test_admin_password(db):
    assert not auth.admin_exists()
    with pytest.raises(ValueError):
        auth.set_admin_password("short")
    auth.set_admin_password("correct horse battery")
    assert auth.admin_exists()
    assert auth.verify_admin_password("correct horse battery")
    assert not auth.verify_admin_password("wrong password")
    auth.set_admin_password("another long one")
    assert auth.verify_admin_password("another long one")

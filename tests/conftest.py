import pytest

from copier import config, crypto
from copier.db import dispose_all, init_db


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets an isolated data dir, key and DB."""
    monkeypatch.setenv("TC_DATA_DIR", str(tmp_path / "data"))
    config.reset_paths_cache()
    crypto.clear_cache()
    yield tmp_path / "data"
    dispose_all()
    crypto.clear_cache()
    config.reset_paths_cache()


@pytest.fixture
def db(data_dir):
    crypto.ensure_key()
    init_db()
    return data_dir


@pytest.fixture
def fake_template(tmp_path):
    """A folder that looks like an installed MT5 terminal."""
    root = tmp_path / "BrokerX MetaTrader 5"
    (root / "config").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "Bases" / "BrokerX-Demo").mkdir(parents=True)
    (root / "MQL5" / "Experts").mkdir(parents=True)
    (root / "terminal64.exe").write_bytes(b"MZ fake")
    (root / "metaeditor64.exe").write_bytes(b"MZ fake")
    (root / "config" / "servers.dat").write_bytes(b"servers")
    (root / "config" / "accounts.dat").write_bytes(b"someone else's saved logins")
    (root / "logs" / "20260101.log").write_text("old log")
    (root / "Bases" / "BrokerX-Demo" / "big.hcc").write_bytes(b"0" * 1024)
    (root / "MQL5" / "Experts" / "x.ex5").write_bytes(b"ea")
    return root

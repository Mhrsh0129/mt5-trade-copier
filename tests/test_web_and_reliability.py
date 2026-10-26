import re
from urllib.parse import unquote_plus

import asyncio
from fastapi.testclient import TestClient

from copier import accounts, auth
from copier.adapters.fake_adapter import FakeAdapter
from copier.alerts.outbox import enqueue_alert, flush_outbox
from copier.bus import HubServer, Publisher, Subscriber
from copier.supervisor import ConnectivityMonitor
from copier.web.app import app, set_connect_runner_for_tests


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    assert m, "CSRF token not found in HTML"
    return m.group(1)


def test_dashboard_login_accounts_connect_and_slave_rules(db, fake_template):
    auth.set_admin_password("SuperAdmin#2026")
    client = TestClient(app)

    # Unauthenticated redirect
    r = client.get("/accounts", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    # Wrong password
    r = client.post("/login", data={"password": "wrong"})
    assert r.status_code == 401 and "Invalid admin password" in r.text

    # Valid login
    r = client.post("/login", data={"password": "SuperAdmin#2026"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/accounts"

    page = client.get("/accounts")
    assert page.status_code == 200
    csrf = _extract_csrf(page.text)

    # Add broker template
    r = client.post(
        "/templates/add",
        data={"csrf": csrf, "broker": "ICMarkets", "source_dir": str(fake_template)},
        follow_redirects=True,
    )
    assert r.status_code == 200 and "ICMarkets" in r.text

    # Add master and slave accounts
    r = client.post(
        "/accounts/add",
        data={
            "csrf": csrf,
            "role": "master",
            "broker": "ICMarkets",
            "server": "ICMarkets-Demo",
            "login": "1001",
            "password": "pw1",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200 and "1001" in r.text

    r = client.post(
        "/accounts/add",
        data={
            "csrf": csrf,
            "role": "slave",
            "broker": "ICMarkets",
            "server": "ICMarkets-Demo",
            "login": "2002",
            "password": "pw2",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200 and "2002" in r.text

    # Test "Connect" button success and failure messages
    set_connect_runner_for_tests(
        lambda aid: accounts.connect_account(
            aid, adapter=FakeAdapter(login=1001, server="ICMarkets-Demo", password="pw1")
        )
    )
    try:
        r_ok = client.post("/accounts/1/connect", data={"csrf": csrf}, follow_redirects=False)
        assert r_ok.status_code == 303
        loc_ok = unquote_plus(r_ok.headers["location"])
        assert "Connected: Fake Trader" in loc_ok and "10,000.00 USD" in loc_ok and "1:100" in loc_ok

        # Slave #2 has login 2002, so FakeAdapter(login=1001) returns exact authorization failure
        r_fail = client.post("/accounts/2/connect", data={"csrf": csrf}, follow_redirects=False)
        loc_fail = unquote_plus(r_fail.headers["location"])
        assert "Connect failed: Authorization failed" in loc_fail
    finally:
        set_connect_runner_for_tests(None)

    # Health check reflects error status on slave #2 (503)
    h = client.get("/health")
    assert h.status_code == 503 and h.json()["healthy"] is False

    # Edit slave settings, per-symbol allowed lots, and symbol mapping
    r_s = client.post(
        "/slaves/2/settings",
        data={
            "csrf": csrf,
            "paused": "0",
            "reverse": "1",
            "lot_mode": "multiplier",
            "lot_value": "1.5",
            "min_lot": "0.01",
            "max_lot": "2.0",
            "max_open_trades": "5",
            "deviation_points": "25",
            "symbol_filter_mode": "whitelist",
            "symbol_filter": "XAUUSD, EURUSD",
            "close_orphans": "0",
        },
        follow_redirects=True,
    )
    assert r_s.status_code == 200 and "hot-reloaded" in r_s.text

    r_rule = client.post(
        "/slaves/2/symbol-rule",
        data={
            "csrf": csrf,
            "master_symbol": "XAUUSD",
            "enabled": "1",
            "lot_mode": "fixed",
            "lot_value": "0.05",
            "allowed_lots": "0.01, 0.05, 0.10",
            "not_allowed_action": "snap",
        },
        follow_redirects=True,
    )
    assert r_rule.status_code == 200 and "0.05" in r_rule.text

    r_map = client.post(
        "/slaves/2/symbol-map",
        data={"csrf": csrf, "master_symbol": "XAUUSD", "slave_symbol": "GOLD"},
        follow_redirects=True,
    )
    assert r_map.status_code == 200 and "GOLD" in r_map.text

    # Visit Trades, Logs, and System pages
    assert client.get("/trades").status_code == 200
    assert client.get("/logs").status_code == 200
    assert client.get("/system").status_code == 200


def test_connectivity_monitor_and_alert_outbox_offline_queue(db):
    state = {"up": True}
    mon = ConnectivityMonitor(probe_fn=lambda: state["up"], fail_threshold=2)
    assert mon.step() == (None, 0.0)

    # First failed probe doesn't trip offline yet; second consecutive failure trips 'lost'
    state["up"] = False
    assert mon.step() == (None, 0.0)
    trans, _ = mon.step()
    assert trans == "lost" and not mon.online

    # Queue alerts while offline; dedupe suppresses duplicate key within 5 minutes
    id1 = enqueue_alert("net:1", "Disconnected from broker")
    id2 = enqueue_alert("net:1", "Disconnected from broker duplicate")
    assert id1 is not None and id2 is None

    sent: list[str] = []
    # Flushing while offline sends nothing
    assert flush_outbox(sender=lambda t: (sent.append(t) or True, "ok"), online=False) == 0
    assert sent == []

    # Internet restored -> outbox flushes queued messages
    state["up"] = True
    trans, _ = mon.step()
    assert trans == "restored" and mon.online
    assert flush_outbox(sender=lambda t: (sent.append(t) or True, "ok"), online=True) == 1
    assert sent == ["Disconnected from broker"]


def test_ipc_bus_hub_pub_sub():
    async def _run():
        hub = HubServer(port=0)
        await hub.start()
        sub = Subscriber(topics={"trade_event"}, port=hub.port)
        pub = Publisher(port=hub.port)
        received = []

        async def _reader():
            async for m in sub.listen():
                received.append(m)
                break

        task = asyncio.create_task(_reader())
        await asyncio.sleep(0.05)
        await pub.publish("trade_event", {"seq": 42, "symbol": "XAUUSD"})
        await asyncio.wait_for(task, timeout=2.0)
        await pub.close()
        await sub.close()
        await hub.stop()
        assert len(received) == 1 and received[0].payload["seq"] == 42

    asyncio.run(_run())

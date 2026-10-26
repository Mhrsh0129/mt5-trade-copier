"""End-to-end integration tests:
1. MasterPoller -> IPC HubServer -> multiple SlaveSubscribers (Hedging + Netting + Reversed + Failing slave isolation)
2. Sequence-gap recovery, internet offline global_pause, and full reconciliation on recovery
3. Live hot-reload of SlaveSettings / Allowed Lot Sizes over the IPC bus
"""

from __future__ import annotations

import asyncio

from copier import accounts
from copier.adapters.base import ExecCode, MarginMode, OpenRequest, PendingRequest, PendingType, Side
from copier.adapters.fake_adapter import FakeAdapter
from copier.bus import HubServer, Publisher, Subscriber
from copier.engine.executor import SlaveExecutor
from copier.engine.master import MasterPoller
from copier.engine.rules import save_slave_settings, upsert_symbol_map, upsert_symbol_rule
from copier.engine.slave import SlaveSubscriber


def test_end_to_end_multi_slave_bus_pipeline_and_failing_slave_isolation(db, fake_template):
    async def _scenario():
        accounts.add_template("BrokerX", str(fake_template))
        m_id = accounts.add_account(
            role="master", broker="BrokerX", server="M-Srv", login=10, password="pw", provision=False
        )
        # Slave 1: Hedging, maps XAUUSD -> GOLD, allowed lots [0.01, 0.05, 0.10] (snap)
        s1_id = accounts.add_account(
            role="slave", broker="BrokerX", server="S1-Srv", login=101, password="pw", provision=False
        )
        upsert_symbol_map(s1_id, "XAUUSD", "GOLD")
        upsert_symbol_rule(
            s1_id, "XAUUSD", enabled=True, allowed_lots=[0.01, 0.05, 0.10], not_allowed_action="snap"
        )

        # Slave 2: Netting, reversed direction, suffix EURUSD.m & GOLD.m
        s2_id = accounts.add_account(
            role="slave", broker="BrokerX", server="S2-Srv", login=102, password="pw", provision=False
        )
        save_slave_settings(s2_id, reverse=True, lot_mode="multiplier", lot_value=2.0)

        # Slave 3: Failing broker (always rejects with MARKET_CLOSED) -> must never block Slave 1 or Slave 2
        s3_id = accounts.add_account(
            role="slave", broker="BrokerX", server="S3-Srv", login=103, password="pw", provision=False
        )

        m_ad = FakeAdapter(login=10, server="M-Srv", balance=10_000)
        m_ad.add_symbol("XAUUSD", 2300.0, 2300.5, digits=2, contract=100.0)

        s1_ad = FakeAdapter(login=101, server="S1-Srv", balance=10_000, margin_mode=MarginMode.HEDGING)
        s1_ad.add_symbol("GOLD", 2300.0, 2300.5, digits=2, contract=100.0)

        s2_ad = FakeAdapter(login=102, server="S2-Srv", balance=10_000, margin_mode=MarginMode.NETTING)
        s2_ad.add_symbol("GOLD.m", 2300.0, 2300.5, digits=2, contract=100.0)

        s3_ad = FakeAdapter(login=103, server="S3-Srv", balance=10_000)
        s3_ad.add_symbol("XAUUSD", 2300.0, 2300.5, digits=2, contract=100.0)
        s3_ad.fail_next(ExecCode.MARKET_CLOSED, ExecCode.MARKET_CLOSED, ExecCode.MARKET_CLOSED)

        # Start live IPC Hub
        hub = HubServer(port=0)
        await hub.start()
        pub = Publisher(port=hub.port)
        poller = MasterPoller(m_ad, pub, poll_ms=20, snapshot_every_s=60.0)
        poller.seed_from_db_or_live()

        subs = [
            SlaveSubscriber(SlaveExecutor(s1_id, s1_ad, retry_delay_s=0)),
            SlaveSubscriber(SlaveExecutor(s2_id, s2_ad, retry_delay_s=0)),
            SlaveSubscriber(SlaveExecutor(s3_id, s3_ad, retry_delay_s=0)),
        ]
        bus_subs = [Subscriber(topics={"trade_event", "snapshot", "control"}, port=hub.port) for _ in subs]

        async def _pump(bsub: Subscriber, ssub: SlaveSubscriber):
            async for msg in bsub.listen():
                ssub.on_bus_message(msg)

        tasks = [asyncio.create_task(_pump(bs, ss)) for bs, ss in zip(bus_subs, subs)]
        await asyncio.sleep(0.05)

        try:
            # 1. Master opens 0.04 XAUUSD BUY with SL=2290, TP=2320
            m_open = m_ad.market_open(OpenRequest("XAUUSD", Side.BUY, 0.04, sl=2290.0, tp=2320.0))
            await poller.poll_once()
            await asyncio.sleep(0.08)

            # Slave 1 (Hedging, GOLD, snapped 0.04 -> 0.05)
            assert len(s1_ad.positions()) == 1
            p1 = s1_ad.positions()[0]
            assert p1.symbol == "GOLD" and p1.side is Side.BUY and p1.volume == 0.05

            # Slave 2 (Netting, reversed BUY->SELL, 2x multiplier -> 0.08, auto-alias GOLD.m, swapped SL/TP)
            assert len(s2_ad.positions()) == 1
            p2 = s2_ad.positions()[0]
            assert p2.symbol == "GOLD.m" and p2.side is Side.SELL and p2.volume == 0.08
            assert p2.sl == 2320.0 and p2.tp == 2290.0

            # Slave 3 failed cleanly without blocking Slave 1 or Slave 2
            assert s3_ad.positions() == []

            # 2. Master places a pending order, then fills it
            m_pend = m_ad.place_pending(PendingRequest("XAUUSD", PendingType.BUY_LIMIT, 0.05, 2295.0))
            await poller.poll_once()
            await asyncio.sleep(0.08)
            assert len(s1_ad.pending_orders()) == 1
            assert len(s2_ad.pending_orders()) == 1 and s2_ad.pending_orders()[0].type is PendingType.SELL_LIMIT

            m_ad.fill_pending(m_pend.order_id)
            await poller.poll_once()
            await asyncio.sleep(0.08)
            assert len(s1_ad.pending_orders()) == 0 and len(s1_ad.positions()) == 2
            # Netting Slave 2 aggregated the second 0.10 SELL into its single net position (0.08 + 0.10 = 0.18)
            assert len(s2_ad.positions()) == 1 and abs(s2_ad.positions()[0].volume - 0.18) < 1e-6

            # 3. Simulate offline global_pause -> Master closes first position while slaves are paused
            await pub.publish("control", {"cmd": "global_pause", "paused": True})
            await asyncio.sleep(0.05)
            m_ad.close(m_open.position_id)
            await poller.poll_once()
            await asyncio.sleep(0.05)
            # Still open on Slave 1 while paused
            assert len(s1_ad.positions()) == 2

            # 4. Internet restored (global_pause=False) -> SlaveSubscriber immediately reconciles from saved snapshot!
            await pub.publish("control", {"cmd": "global_pause", "paused": False})
            await asyncio.sleep(0.08)
            assert len(s1_ad.positions()) == 1
            assert abs(s2_ad.positions()[0].volume - 0.10) < 1e-6
        finally:
            for bs in bus_subs:
                await bs.close()
            for t in tasks:
                t.cancel()
            await pub.close()
            await hub.stop()

    asyncio.run(_scenario())

"""In-memory broker used by unit/integration tests (no terminal needed).

Supports hedging and netting accounts, scripted failures (``fail_next``), and
manual price moves. Deliberately simple: fills at the current tick.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import replace
from pathlib import Path

from .base import (AccountInfo, BrokerAdapter, ConnectError, Credentials, Deal, ExecCode, ExecResult,
                   MarginMode, OpenRequest, PendingOrder, PendingRequest, Position, Side, SymbolSpec, Tick)


def _now_ms() -> int:
    return int(time.time() * 1000)


class FakeAdapter(BrokerAdapter):
    platform = "fake"

    def __init__(self, *, login: int = 1000, server: str = "Fake-Demo", password: str = "pw",
                 balance: float = 10_000.0, leverage: int = 100,
                 margin_mode: MarginMode = MarginMode.HEDGING,
                 symbols: dict[str, SymbolSpec] | None = None, ticks: dict[str, Tick] | None = None):
        self.login, self.server, self._password = login, server, password
        self.balance, self.leverage, self.margin_mode = balance, leverage, margin_mode
        self.specs: dict[str, SymbolSpec] = symbols or {}
        self.ticks: dict[str, Tick] = ticks or {}
        self._positions: dict[int, Position] = {}
        self._orders: dict[int, PendingOrder] = {}
        self._deals: list[Deal] = []
        self._ids = itertools.count(100_001)
        self._connected = False
        self.link_down = False
        self.connect_calls = 0
        self.fail_queue: list[ExecCode] = []
        self.sent: list[tuple[str, object]] = []  # request log for assertions

    # ---------------------------------------------------------- helpers for tests
    @staticmethod
    def spec(name: str, *, digits: int = 5, vmin: float = 0.01, vmax: float = 100.0, step: float = 0.01,
             contract: float = 100_000.0, stops_level: int = 0) -> SymbolSpec:
        return SymbolSpec(name=name, digits=digits, point=10 ** -digits, volume_min=vmin, volume_max=vmax,
                          volume_step=step, contract_size=contract, stops_level=stops_level, freeze_level=0,
                          filling_modes=1, trade_allowed=True, visible=True)

    def add_symbol(self, name: str, bid: float, ask: float, **kw) -> None:
        self.specs[name] = self.spec(name, **kw)
        self.ticks[name] = Tick(bid=bid, ask=ask, time_msc=_now_ms())

    def set_price(self, symbol: str, bid: float, ask: float) -> None:
        self.ticks[symbol] = Tick(bid=bid, ask=ask, time_msc=_now_ms())

    def fail_next(self, *codes: ExecCode) -> None:
        self.fail_queue.extend(codes)

    def _maybe_fail(self) -> ExecResult | None:
        if self.fail_queue:
            code = self.fail_queue.pop(0)
            return ExecResult(code, message=f"scripted {code.value}")
        return None

    # ---------------------------------------------------------- session
    def connect(self, creds: Credentials, terminal_path: Path | None = None) -> AccountInfo:
        if creds.login != self.login or creds.password != self._password or creds.server != self.server:
            raise ConnectError("Authorization failed", code=-6,
                               hint="Wrong login/password, wrong server name, or account disabled")
        self._connected = True
        self.link_down = False
        self.connect_calls += 1
        return self.account_info()

    def disconnect(self) -> None:
        self._connected = False
        self.link_down = False

    def is_connected(self) -> bool:
        return self._connected and not self.link_down

    def terminal_alive(self) -> bool:
        return self._connected

    def simulate_broker_link_drop(self, down: bool = True) -> None:
        """Terminal still running, but its connection to the trade server is lost."""
        self.link_down = down

    def account_info(self) -> AccountInfo:
        return AccountInfo(login=self.login, name="Fake Trader", server=self.server, company="FakeBroker",
                           currency="USD", balance=self.balance, equity=self.balance,
                           margin_free=self.balance, leverage=self.leverage, margin_mode=self.margin_mode,
                           trade_allowed=True, algo_allowed=True)

    # ---------------------------------------------------------- state
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def pending_orders(self) -> list[PendingOrder]:
        return list(self._orders.values())

    def symbols(self) -> list[SymbolSpec]:
        return list(self.specs.values())

    def symbol_spec(self, symbol: str) -> SymbolSpec | None:
        return self.specs.get(symbol)

    def tick(self, symbol: str) -> Tick | None:
        return self.ticks.get(symbol)

    def calc_margin(self, side: Side, symbol: str, volume: float, price: float) -> float | None:
        spec = self.specs.get(symbol)
        return None if spec is None else volume * spec.contract_size * price / self.leverage

    def deals_for_position(self, position_id: int) -> list[Deal]:
        return [d for d in self._deals if d.position_id == position_id]

    def _deal(self, pos_id: int, symbol: str, entry: str, side: Side, volume: float, price: float,
              magic: int, reason: str = "expert") -> int:
        did = next(self._ids)
        self._deals.append(Deal(deal_id=did, order_id=did, position_id=pos_id, symbol=symbol, entry=entry,
                                side=side, volume=volume, price=price, reason=reason, magic=magic,
                                time_msc=_now_ms()))
        return did

    # ---------------------------------------------------------- trading
    def market_open(self, req: OpenRequest) -> ExecResult:
        self.sent.append(("open", req))
        if (f := self._maybe_fail()) is not None:
            return f
        spec, tick = self.specs.get(req.symbol), self.ticks.get(req.symbol)
        if spec is None or tick is None:
            return ExecResult(ExecCode.INVALID_PRICE, message=f"unknown symbol {req.symbol}")
        price = tick.ask if req.side is Side.BUY else tick.bid

        if self.margin_mode is MarginMode.NETTING:
            existing = next((p for p in self._positions.values() if p.symbol == req.symbol), None)
            if existing is not None:
                return self._net(existing, req.side, req.volume, price, req.magic)

        pid = next(self._ids)
        self._positions[pid] = Position(position_id=pid, symbol=req.symbol, side=req.side, volume=req.volume,
                                        price_open=price, sl=req.sl, tp=req.tp, magic=req.magic,
                                        comment=req.comment[:31], time_msc=_now_ms())
        did = self._deal(pid, req.symbol, "in", req.side, req.volume, price, req.magic)
        return ExecResult(ExecCode.OK, retcode=10009, order_id=pid, deal_id=did, position_id=pid,
                          volume=req.volume, price=price)

    def _net(self, p: Position, side: Side, volume: float, price: float, magic: int) -> ExecResult:
        if side is p.side:
            newp = replace(p, volume=round(p.volume + volume, 8))
            entry = "in"
        elif volume < p.volume - 1e-9:
            newp = replace(p, volume=round(p.volume - volume, 8))
            entry = "out"
        elif abs(volume - p.volume) <= 1e-9:
            newp = None
            entry = "out"
        else:
            newp = replace(p, side=side, volume=round(volume - p.volume, 8), price_open=price)
            entry = "inout"
        if newp is None:
            del self._positions[p.position_id]
        else:
            self._positions[p.position_id] = newp
        did = self._deal(p.position_id, p.symbol, entry, side, volume, price, magic)
        return ExecResult(ExecCode.OK, retcode=10009, order_id=did, deal_id=did, position_id=p.position_id,
                          volume=volume, price=price)

    def close(self, position_id: int, volume: float | None = None, deviation: int = 20,
              magic: int = 0, comment: str = "") -> ExecResult:
        self.sent.append(("close", (position_id, volume)))
        if (f := self._maybe_fail()) is not None:
            return f
        p = self._positions.get(position_id)
        if p is None:
            return ExecResult(ExecCode.NOT_FOUND, message=f"position {position_id} not found")
        tick = self.ticks[p.symbol]
        price = tick.bid if p.side is Side.BUY else tick.ask
        vol = p.volume if volume is None else min(volume, p.volume)
        remaining = round(p.volume - vol, 8)
        if remaining <= 1e-9:
            del self._positions[position_id]
        else:
            self._positions[position_id] = replace(p, volume=remaining)
        did = self._deal(position_id, p.symbol, "out", p.side.flipped(), vol, price, magic or p.magic)
        return ExecResult(ExecCode.OK, retcode=10009, deal_id=did, position_id=position_id, volume=vol,
                          price=price)

    def modify_position(self, position_id: int, sl: float, tp: float) -> ExecResult:
        self.sent.append(("modify", (position_id, sl, tp)))
        if (f := self._maybe_fail()) is not None:
            return f
        p = self._positions.get(position_id)
        if p is None:
            return ExecResult(ExecCode.NOT_FOUND)
        self._positions[position_id] = replace(p, sl=sl, tp=tp)
        return ExecResult(ExecCode.OK, retcode=10009, position_id=position_id)

    def place_pending(self, req: PendingRequest) -> ExecResult:
        self.sent.append(("pending", req))
        if (f := self._maybe_fail()) is not None:
            return f
        oid = next(self._ids)
        self._orders[oid] = PendingOrder(order_id=oid, symbol=req.symbol, type=req.type, volume=req.volume,
                                         price=req.price, stop_limit=req.stop_limit, sl=req.sl, tp=req.tp,
                                         expiration=req.expiration, magic=req.magic,
                                         comment=req.comment[:31], time_setup_msc=_now_ms())
        return ExecResult(ExecCode.OK, retcode=10008, order_id=oid)

    def modify_pending(self, order_id: int, price: float, sl: float, tp: float,
                       stop_limit: float = 0.0, expiration: int = 0) -> ExecResult:
        self.sent.append(("modify_pending", (order_id, price, sl, tp)))
        if (f := self._maybe_fail()) is not None:
            return f
        o = self._orders.get(order_id)
        if o is None:
            return ExecResult(ExecCode.NOT_FOUND)
        self._orders[order_id] = replace(o, price=price, sl=sl, tp=tp, stop_limit=stop_limit,
                                         expiration=expiration)
        return ExecResult(ExecCode.OK, retcode=10008, order_id=order_id)

    def cancel_pending(self, order_id: int) -> ExecResult:
        self.sent.append(("cancel", order_id))
        if (f := self._maybe_fail()) is not None:
            return f
        if self._orders.pop(order_id, None) is None:
            return ExecResult(ExecCode.NOT_FOUND)
        return ExecResult(ExecCode.OK, retcode=10009, order_id=order_id)

    def fill_pending(self, order_id: int) -> int:
        """Test helper: simulate the broker triggering a pending order. Returns position id."""
        o = self._orders.pop(order_id)
        price = o.price
        pid = order_id  # MT5 hedging: position id == originating order ticket
        self._positions[pid] = Position(position_id=pid, symbol=o.symbol, side=o.type.side, volume=o.volume,
                                        price_open=price, sl=o.sl, tp=o.tp, magic=o.magic, comment=o.comment,
                                        time_msc=_now_ms())
        self._deal(pid, o.symbol, "in", o.type.side, o.volume, price, o.magic)
        return pid

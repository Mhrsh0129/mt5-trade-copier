"""MetaTrader 5 implementation of :class:`BrokerAdapter`.

One instance per process: the ``MetaTrader5`` package binds to a single
terminal per Python process. All calls are serialized with a lock because the
package is not thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .base import (AccountInfo, BrokerAdapter, ConnectError, Credentials, Deal, ExecCode, ExecResult,
                   MarginMode, OpenRequest, PendingOrder, PendingRequest, PendingType, Position, Side,
                   SymbolSpec, Tick)
from ..config import CONNECT_TIMEOUT_MS
from ..logging_setup import register_secret

log = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # pragma: no cover
    mt5 = None


# ------------------------------------------------------------------ mappings
_ORDER_TYPE_TO_PENDING = {2: PendingType.BUY_LIMIT, 3: PendingType.SELL_LIMIT, 4: PendingType.BUY_STOP,
                          5: PendingType.SELL_STOP, 6: PendingType.BUY_STOP_LIMIT,
                          7: PendingType.SELL_STOP_LIMIT}
_PENDING_TO_ORDER_TYPE = {v: k for k, v in _ORDER_TYPE_TO_PENDING.items()}

_ACTION_DEAL, _ACTION_PENDING, _ACTION_SLTP, _ACTION_MODIFY, _ACTION_REMOVE = 1, 5, 6, 7, 8
_FILL_FOK, _FILL_IOC, _FILL_RETURN = 0, 1, 2
_TIME_GTC, _TIME_SPECIFIED = 0, 2

_RETCODE_MAP: dict[int, ExecCode] = {
    10008: ExecCode.OK, 10009: ExecCode.OK, 10010: ExecCode.PARTIAL, 10025: ExecCode.OK,  # no changes
    10004: ExecCode.REQUOTE, 10020: ExecCode.PRICE_CHANGED, 10021: ExecCode.OFF_QUOTES,
    10012: ExecCode.TIMEOUT, 10031: ExecCode.NO_CONNECTION, 10024: ExecCode.TIMEOUT,  # too many requests
    10006: ExecCode.REJECTED, 10007: ExecCode.REJECTED, 10011: ExecCode.REJECTED, 10028: ExecCode.REJECTED,
    10014: ExecCode.INVALID_VOLUME, 10038: ExecCode.INVALID_VOLUME, 10034: ExecCode.LIMIT_REACHED,
    10015: ExecCode.INVALID_PRICE, 10016: ExecCode.INVALID_STOPS, 10029: ExecCode.INVALID_STOPS,
    10017: ExecCode.TRADE_DISABLED, 10032: ExecCode.TRADE_DISABLED, 10042: ExecCode.TRADE_DISABLED,
    10043: ExecCode.TRADE_DISABLED, 10044: ExecCode.TRADE_DISABLED,
    10018: ExecCode.MARKET_CLOSED, 10019: ExecCode.NO_MONEY,
    10026: ExecCode.ALGO_DISABLED, 10027: ExecCode.ALGO_DISABLED,
    10033: ExecCode.LIMIT_REACHED, 10040: ExecCode.LIMIT_REACHED,
    10013: ExecCode.REJECTED, 10035: ExecCode.NOT_FOUND, 10036: ExecCode.NOT_FOUND,
}
_RETCODE_INVALID_FILL = 10030

_DEAL_ENTRY = {0: "in", 1: "out", 2: "inout", 3: "out_by"}
_DEAL_REASON = {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "sl", 5: "tp", 6: "so",
                7: "rollover", 8: "vmargin", 9: "split"}
_MARGIN_MODE = {0: MarginMode.NETTING, 1: MarginMode.EXCHANGE, 2: MarginMode.HEDGING}

# MT5 last_error() codes -> (reason, hint). Shown verbatim in the dashboard.
_INIT_ERRORS: dict[int, tuple[str, str]] = {
    -1: ("Generic failure", "Check the terminal log in <terminal_dir>/logs"),
    -2: ("Invalid parameters", "Check login is numeric and server name is exact"),
    -4: ("Terminal or item not found", "Check the terminal path exists and contains terminal64.exe"),
    -5: ("Invalid terminal version", "Update the broker template (run its installer again)"),
    -6: ("Authorization failed", "Wrong login/password, wrong server name, or account disabled"),
    -7: ("Unsupported method", ""),
    -8: ("Algo trading disabled", "Enable Algo Trading in the terminal"),
    -10000: ("Internal terminal error", "Restart the terminal"),
    -10003: ("IPC initialization failed", "Terminal could not start: check RAM, antivirus, or a "
                                          "stuck terminal64.exe for this folder"),
    -10004: ("No IPC connection to terminal", "Terminal closed during login"),
    -10005: ("IPC timeout - terminal did not respond in time",
             "First launch can be slow; retry. If it persists, open the terminal manually once"),
}


def _require_mt5() -> Any:
    if mt5 is None:
        raise ConnectError("MetaTrader5 Python package is not installed",
                           hint="pip install MetaTrader5 (Windows, Python 3.11-3.13)")
    return mt5


class MT5Adapter(BrokerAdapter):
    platform = "mt5"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connected_login: int | None = None
        self._spec_cache: dict[str, SymbolSpec] = {}
        self._fill_cache: dict[str, list[int]] = {}

    # ================================================================ session
    def connect(self, creds: Credentials, terminal_path: Path | None = None,
                timeout_ms: int = CONNECT_TIMEOUT_MS) -> AccountInfo:
        m = _require_mt5()
        register_secret(creds.password)
        kwargs: dict[str, Any] = dict(login=int(creds.login), password=creds.password,
                                      server=creds.server, timeout=timeout_ms)
        if terminal_path is not None:
            exe = Path(terminal_path)
            if exe.is_dir():
                exe = exe / "terminal64.exe"
            if not exe.exists():
                raise ConnectError(f"Terminal executable not found: {exe}", code=-4,
                                   hint="Re-provision the account's terminal from its broker template")
            kwargs.update(path=str(exe), portable=True)

        with self._lock:
            try:
                m.shutdown()
            except Exception:  # noqa: BLE001
                pass
            ok = m.initialize(**kwargs)
            if not ok:
                code, text = m.last_error()
                raise self._init_error(code, text)

            # initialize() can return before the server session is fully established.
            deadline = time.monotonic() + timeout_ms / 1000
            info = None
            while time.monotonic() < deadline:
                ti, ai = m.terminal_info(), m.account_info()
                if ti is not None and ti.connected and ai is not None and ai.login == int(creds.login):
                    info = ai
                    break
                time.sleep(0.25)
            if info is None:
                code, text = m.last_error()
                ti = m.terminal_info()
                m.shutdown()
                if ti is not None and not ti.connected:
                    raise ConnectError("Terminal started but could not connect to the trade server",
                                       code=code, hint=f"Check server name '{creds.server}' and internet. "
                                                       f"Terminal says: {text}")
                raise ConnectError("Logged-in account does not match requested login", code=code,
                                   hint="Another account may be saved in this terminal; retry Connect")
            self._connected_login = info.login
            self._spec_cache.clear()
            self._fill_cache.clear()
            return self._account_info_locked()

    @staticmethod
    def _init_error(code: int, text: str) -> ConnectError:
        reason, hint = _INIT_ERRORS.get(code, (text or "Unknown error", ""))
        if text and text not in reason:
            reason = f"{reason}: {text}"
        return ConnectError(reason, code=code, hint=hint or None)

    def disconnect(self) -> None:
        if mt5 is None:
            return
        with self._lock:
            mt5.shutdown()
            self._connected_login = None

    def is_connected(self) -> bool:
        if mt5 is None or self._connected_login is None:
            return False
        with self._lock:
            ti = mt5.terminal_info()
            return bool(ti and ti.connected)

    def account_info(self) -> AccountInfo:
        with self._lock:
            return self._account_info_locked()

    def _account_info_locked(self) -> AccountInfo:
        ai, ti = mt5.account_info(), mt5.terminal_info()
        if ai is None:
            raise ConnectError("account_info() unavailable", code=mt5.last_error()[0])
        return AccountInfo(
            login=ai.login, name=ai.name, server=ai.server, company=ai.company, currency=ai.currency,
            balance=ai.balance, equity=ai.equity, margin_free=ai.margin_free, leverage=ai.leverage,
            margin_mode=_MARGIN_MODE.get(ai.margin_mode, MarginMode.HEDGING),
            trade_allowed=bool(ai.trade_allowed),
            algo_allowed=bool(ti.trade_allowed if ti else False) and bool(ai.trade_expert),
        )

    # ================================================================ state
    def positions(self) -> list[Position]:
        with self._lock:
            raw = mt5.positions_get() or ()
        return [Position(position_id=p.ticket, symbol=p.symbol, side=Side.BUY if p.type == 0 else Side.SELL,
                         volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp, magic=p.magic,
                         comment=p.comment, time_msc=p.time_msc, profit=p.profit) for p in raw]

    def pending_orders(self) -> list[PendingOrder]:
        with self._lock:
            raw = mt5.orders_get() or ()
        out = []
        for o in raw:
            pt = _ORDER_TYPE_TO_PENDING.get(o.type)
            if pt is None:  # market order in flight / close-by
                continue
            out.append(PendingOrder(order_id=o.ticket, symbol=o.symbol, type=pt, volume=o.volume_current,
                                    price=o.price_open, stop_limit=o.price_stoplimit, sl=o.sl, tp=o.tp,
                                    expiration=o.time_expiration, magic=o.magic, comment=o.comment,
                                    time_setup_msc=o.time_setup_msc))
        return out

    @staticmethod
    def _spec(s: Any) -> SymbolSpec:
        return SymbolSpec(name=s.name, digits=s.digits, point=s.point, volume_min=s.volume_min,
                          volume_max=s.volume_max, volume_step=s.volume_step,
                          contract_size=s.trade_contract_size, stops_level=s.trade_stops_level,
                          freeze_level=s.trade_freeze_level, filling_modes=s.filling_mode,
                          trade_allowed=s.trade_mode != 0, visible=bool(s.visible))

    def symbols(self) -> list[SymbolSpec]:
        with self._lock:
            raw = mt5.symbols_get() or ()
        return [self._spec(s) for s in raw]

    def symbol_spec(self, symbol: str) -> SymbolSpec | None:
        if symbol in self._spec_cache:
            return self._spec_cache[symbol]
        with self._lock:
            s = mt5.symbol_info(symbol)
            if s is None:
                return None
            if not s.visible:
                mt5.symbol_select(symbol, True)
        spec = self._spec(s)
        self._spec_cache[symbol] = spec
        return spec

    def tick(self, symbol: str) -> Tick | None:
        with self._lock:
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                mt5.symbol_select(symbol, True)
                t = mt5.symbol_info_tick(symbol)
        return Tick(bid=t.bid, ask=t.ask, time_msc=t.time_msc) if t else None

    def calc_margin(self, side: Side, symbol: str, volume: float, price: float) -> float | None:
        with self._lock:
            return mt5.order_calc_margin(0 if side is Side.BUY else 1, symbol, volume, price)

    def deals_for_position(self, position_id: int) -> list[Deal]:
        with self._lock:
            raw = mt5.history_deals_get(position=position_id) or ()
        return [Deal(deal_id=d.ticket, order_id=d.order, position_id=d.position_id, symbol=d.symbol,
                     entry=_DEAL_ENTRY.get(d.entry, str(d.entry)), side=Side.BUY if d.type == 0 else Side.SELL,
                     volume=d.volume, price=d.price, reason=_DEAL_REASON.get(d.reason, str(d.reason)),
                     magic=d.magic, time_msc=d.time_msc) for d in raw if d.type in (0, 1)]

    # ================================================================ trading
    def _filling_candidates(self, symbol: str) -> list[int]:
        if symbol in self._fill_cache:
            return self._fill_cache[symbol]
        spec = self.symbol_spec(symbol)
        flags = spec.filling_modes if spec else 0
        cands = ([_FILL_FOK] if flags & 1 else []) + ([_FILL_IOC] if flags & 2 else []) + [_FILL_RETURN]
        self._fill_cache[symbol] = cands
        return cands

    def _send(self, request: dict[str, Any], try_fillings: bool = True) -> ExecResult:
        fills = self._filling_candidates(request["symbol"]) if try_fillings and "symbol" in request else [None]
        last: ExecResult | None = None
        for fill in fills:
            req = dict(request)
            if fill is not None:
                req["type_filling"] = fill
            with self._lock:
                r = mt5.order_send(req)
                if r is None:
                    code, text = mt5.last_error()
                    return ExecResult(ExecCode.NO_CONNECTION if code in (-10004, -10005) else ExecCode.UNKNOWN,
                                      retcode=code, message=f"order_send returned None: {text}")
            if r.retcode == _RETCODE_INVALID_FILL and fill is not None:
                last = ExecResult(ExecCode.REJECTED, retcode=r.retcode, message=r.comment)
                continue
            if fill is not None and r.retcode in (10008, 10009, 10010):
                # remember the working filling mode first
                self._fill_cache[request["symbol"]] = [fill] + [f for f in fills if f != fill]
            return ExecResult(code=_RETCODE_MAP.get(r.retcode, ExecCode.UNKNOWN), retcode=r.retcode,
                              message=r.comment, order_id=r.order or None, deal_id=r.deal or None,
                              volume=r.volume or None, price=r.price or None)
        return last or ExecResult(ExecCode.UNKNOWN, message="no filling mode accepted")

    def _price(self, symbol: str, side: Side) -> float | None:
        t = self.tick(symbol)
        return None if t is None else (t.ask if side is Side.BUY else t.bid)

    def market_open(self, req: OpenRequest) -> ExecResult:
        price = self._price(req.symbol, req.side)
        if price is None:
            return ExecResult(ExecCode.OFF_QUOTES, message=f"no tick for {req.symbol}")
        request: dict[str, Any] = {
            "action": _ACTION_DEAL, "symbol": req.symbol, "volume": float(req.volume),
            "type": 0 if req.side is Side.BUY else 1, "price": price, "sl": float(req.sl or 0.0),
            "tp": float(req.tp or 0.0), "deviation": int(req.deviation), "magic": int(req.magic),
            "comment": req.comment[:31], "type_time": _TIME_GTC,
        }
        if req.position_id:
            request["position"] = int(req.position_id)
        res = self._send(request)
        if res.ok and res.deal_id:
            res = self._with_position_id(res)
        return res

    def _with_position_id(self, res: ExecResult) -> ExecResult:
        with self._lock:
            deals = mt5.history_deals_get(ticket=res.deal_id) or ()
        if deals:
            return ExecResult(**{**res.__dict__, "position_id": deals[0].position_id})
        # history may lag a few ms; for hedging the position id equals the order ticket
        return ExecResult(**{**res.__dict__, "position_id": res.order_id})

    def close(self, position_id: int, volume: float | None = None, deviation: int = 20,
              magic: int = 0, comment: str = "") -> ExecResult:
        with self._lock:
            ps = mt5.positions_get(ticket=position_id)
        if not ps:
            return ExecResult(ExecCode.NOT_FOUND, message=f"position {position_id} not found")
        p = ps[0]
        side = Side.SELL if p.type == 0 else Side.BUY
        price = self._price(p.symbol, side)
        if price is None:
            return ExecResult(ExecCode.OFF_QUOTES, message=f"no tick for {p.symbol}")
        vol = p.volume if volume is None else min(float(volume), p.volume)
        return self._send({
            "action": _ACTION_DEAL, "symbol": p.symbol, "volume": vol, "position": position_id,
            "type": 0 if side is Side.BUY else 1, "price": price, "deviation": int(deviation),
            "magic": int(magic or p.magic), "comment": comment[:31], "type_time": _TIME_GTC,
        })

    def modify_position(self, position_id: int, sl: float, tp: float) -> ExecResult:
        with self._lock:
            ps = mt5.positions_get(ticket=position_id)
        if not ps:
            return ExecResult(ExecCode.NOT_FOUND, message=f"position {position_id} not found")
        return self._send({"action": _ACTION_SLTP, "symbol": ps[0].symbol, "position": position_id,
                           "sl": float(sl or 0.0), "tp": float(tp or 0.0)}, try_fillings=False)

    def place_pending(self, req: PendingRequest) -> ExecResult:
        request: dict[str, Any] = {
            "action": _ACTION_PENDING, "symbol": req.symbol, "volume": float(req.volume),
            "type": _PENDING_TO_ORDER_TYPE[req.type], "price": float(req.price),
            "stoplimit": float(req.stop_limit or 0.0), "sl": float(req.sl or 0.0), "tp": float(req.tp or 0.0),
            "magic": int(req.magic), "comment": req.comment[:31],
            "type_time": _TIME_SPECIFIED if req.expiration else _TIME_GTC,
            "expiration": int(req.expiration or 0),
        }
        return self._send(request)

    def modify_pending(self, order_id: int, price: float, sl: float, tp: float,
                       stop_limit: float = 0.0, expiration: int = 0) -> ExecResult:
        with self._lock:
            os_ = mt5.orders_get(ticket=order_id)
        if not os_:
            return ExecResult(ExecCode.NOT_FOUND, message=f"order {order_id} not found")
        return self._send({
            "action": _ACTION_MODIFY, "order": order_id, "symbol": os_[0].symbol, "price": float(price),
            "stoplimit": float(stop_limit or 0.0), "sl": float(sl or 0.0), "tp": float(tp or 0.0),
            "type_time": _TIME_SPECIFIED if expiration else _TIME_GTC, "expiration": int(expiration or 0),
        }, try_fillings=False)

    def cancel_pending(self, order_id: int) -> ExecResult:
        return self._send({"action": _ACTION_REMOVE, "order": order_id}, try_fillings=False)

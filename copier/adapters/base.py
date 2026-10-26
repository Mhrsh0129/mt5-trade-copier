"""Platform-neutral broker adapter interface (architecture §2).

The copy engine only talks to :class:`BrokerAdapter`; it never imports a
platform SDK. Add new platforms by implementing this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    def flipped(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class PendingType(str, Enum):
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"
    BUY_STOP_LIMIT = "buy_stop_limit"
    SELL_STOP_LIMIT = "sell_stop_limit"

    @property
    def side(self) -> Side:
        return Side.BUY if self.value.startswith("buy") else Side.SELL

    def flipped(self) -> "PendingType":
        """Reverse-direction counterpart (BUY_LIMIT -> SELL_LIMIT, etc.)."""
        v = self.value
        return PendingType(("sell" + v[3:]) if v.startswith("buy") else ("buy" + v[4:]))


class MarginMode(str, Enum):
    HEDGING = "hedging"
    NETTING = "netting"
    EXCHANGE = "exchange"


class ExecCode(str, Enum):
    """Normalized execution result codes (platform retcodes map onto these)."""
    OK = "ok"
    PARTIAL = "partial"
    REQUOTE = "requote"
    PRICE_CHANGED = "price_changed"
    OFF_QUOTES = "off_quotes"
    TIMEOUT = "timeout"
    NO_CONNECTION = "no_connection"
    REJECTED = "rejected"
    NO_MONEY = "no_money"
    INVALID_VOLUME = "invalid_volume"
    INVALID_STOPS = "invalid_stops"
    INVALID_PRICE = "invalid_price"
    MARKET_CLOSED = "market_closed"
    TRADE_DISABLED = "trade_disabled"
    ALGO_DISABLED = "algo_disabled"
    LIMIT_REACHED = "limit_reached"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return self in _RETRYABLE


_RETRYABLE = {ExecCode.REQUOTE, ExecCode.PRICE_CHANGED, ExecCode.OFF_QUOTES, ExecCode.TIMEOUT,
              ExecCode.NO_CONNECTION, ExecCode.REJECTED}


@dataclass(frozen=True)
class Credentials:
    login: int
    password: str = field(repr=False)  # never printed
    server: str


@dataclass(frozen=True)
class AccountInfo:
    login: int
    name: str
    server: str
    company: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    margin_mode: MarginMode
    trade_allowed: bool
    algo_allowed: bool


@dataclass(frozen=True)
class Position:
    position_id: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_msc: int
    profit: float = 0.0


@dataclass(frozen=True)
class PendingOrder:
    order_id: int
    symbol: str
    type: PendingType
    volume: float
    price: float
    stop_limit: float
    sl: float
    tp: float
    expiration: int
    magic: int
    comment: str
    time_setup_msc: int


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float
    stops_level: int
    freeze_level: int
    filling_modes: int  # bitmask: 1=FOK, 2=IOC (RETURN always allowed for non-market execution)
    trade_allowed: bool
    visible: bool


@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float
    time_msc: int


@dataclass(frozen=True)
class Deal:
    deal_id: int
    order_id: int
    position_id: int
    symbol: str
    entry: str  # in|out|inout|out_by
    side: Side
    volume: float
    price: float
    reason: str  # client|expert|sl|tp|so|...
    magic: int
    time_msc: int


@dataclass(frozen=True)
class OpenRequest:
    symbol: str
    side: Side
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 20
    magic: int = 0
    comment: str = ""
    position_id: int | None = None  # netting: add to / reduce a specific position


@dataclass(frozen=True)
class PendingRequest:
    symbol: str
    type: PendingType
    volume: float
    price: float
    stop_limit: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    expiration: int = 0
    magic: int = 0
    comment: str = ""


@dataclass(frozen=True)
class ExecResult:
    code: ExecCode
    retcode: int | None = None
    message: str = ""
    order_id: int | None = None
    deal_id: int | None = None
    position_id: int | None = None
    volume: float | None = None
    price: float | None = None

    @property
    def ok(self) -> bool:
        return self.code in (ExecCode.OK, ExecCode.PARTIAL)


class ConnectError(Exception):
    """Raised by :meth:`BrokerAdapter.connect` with a precise, user-facing reason."""

    def __init__(self, reason: str, code: int | None = None, hint: str | None = None):
        self.reason, self.code, self.hint = reason, code, hint
        msg = reason if code is None else f"{reason} (code {code})"
        super().__init__(msg + (f" - {hint}" if hint else ""))


class BrokerAdapter(ABC):
    platform: str = "abstract"

    # ---- session
    @abstractmethod
    def connect(self, creds: Credentials, terminal_path: Path | None = None) -> AccountInfo: ...
    @abstractmethod
    def disconnect(self) -> None: ...
    @abstractmethod
    def is_connected(self) -> bool: ...
    @abstractmethod
    def account_info(self) -> AccountInfo: ...

    # ---- state
    @abstractmethod
    def positions(self) -> list[Position]: ...
    @abstractmethod
    def pending_orders(self) -> list[PendingOrder]: ...
    @abstractmethod
    def symbols(self) -> list[SymbolSpec]: ...
    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec | None: ...
    @abstractmethod
    def tick(self, symbol: str) -> Tick | None: ...
    @abstractmethod
    def calc_margin(self, side: Side, symbol: str, volume: float, price: float) -> float | None: ...
    @abstractmethod
    def deals_for_position(self, position_id: int) -> list[Deal]: ...

    # ---- trading
    @abstractmethod
    def market_open(self, req: OpenRequest) -> ExecResult: ...
    @abstractmethod
    def close(self, position_id: int, volume: float | None = None, deviation: int = 20,
              magic: int = 0, comment: str = "") -> ExecResult: ...
    @abstractmethod
    def modify_position(self, position_id: int, sl: float, tp: float) -> ExecResult: ...
    @abstractmethod
    def place_pending(self, req: PendingRequest) -> ExecResult: ...
    @abstractmethod
    def modify_pending(self, order_id: int, price: float, sl: float, tp: float,
                       stop_limit: float = 0.0, expiration: int = 0) -> ExecResult: ...
    @abstractmethod
    def cancel_pending(self, order_id: int) -> ExecResult: ...

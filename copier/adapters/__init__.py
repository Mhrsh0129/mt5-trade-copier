from .base import (AccountInfo, BrokerAdapter, ConnectError, Credentials, Deal, ExecCode,  # noqa: F401
                   ExecResult, MarginMode, OpenRequest, PendingOrder, PendingRequest, PendingType, Position,
                   Side, SymbolSpec, Tick)


def get_adapter(platform: str = "mt5") -> BrokerAdapter:
    """Factory: one place to plug in new platforms."""
    if platform == "mt5":
        from .mt5_adapter import MT5Adapter
        return MT5Adapter()
    if platform == "fake":
        from .fake_adapter import FakeAdapter
        return FakeAdapter()
    raise ValueError(f"Unknown platform: {platform}")

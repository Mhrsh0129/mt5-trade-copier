"""Per-slave rule configuration loader and evaluator (architecture §5.4).

Rules are loaded from SQLite into an immutable :class:`SlaveConfig` snapshot.
When the dashboard edits settings, it increments ``config_version`` and
publishes ``config_changed`` on the control bus so workers reload without a
restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sqlalchemy import select

from ..db import SlaveSettings, SymbolMap, SymbolRule, session_scope


@dataclass(frozen=True)
class PerSymbolRule:
    master_symbol: str
    enabled: bool = True
    lot_mode: str | None = None
    lot_value: float | None = None
    allowed_lots: list[float] = field(default_factory=list)
    not_allowed_action: str = "snap"  # "snap" | "skip"


@dataclass(frozen=True)
class SlaveConfig:
    account_id: int
    paused: bool = False
    reverse: bool = False
    lot_mode: str = "multiplier"
    lot_value: float = 1.0
    min_lot: float | None = None
    max_lot: float | None = None
    max_open_trades: int | None = None
    symbol_filter_mode: str = "none"  # "none" | "whitelist" | "blacklist"
    symbol_filter: list[str] = field(default_factory=list)
    deviation_points: int = 20
    copy_pending: bool = True
    copy_sltp: bool = True
    late_copy_max_age_s: int = 300
    late_copy_max_points: int = 30
    close_orphans: bool = False
    config_version: int = 1
    symbol_rules: dict[str, PerSymbolRule] = field(default_factory=dict)
    symbol_map: dict[str, str] = field(default_factory=dict)

    def check_symbol_allowed(self, master_symbol: str) -> tuple[bool, str]:
        sym_u = master_symbol.strip().upper()
        rule = self.symbol_rules.get(sym_u) or self.symbol_rules.get(master_symbol)
        if rule is not None and not rule.enabled:
            return False, f"symbol {master_symbol} is disabled in per-symbol rules"
        mode = (self.symbol_filter_mode or "none").lower()
        filt = {s.upper() for s in (self.symbol_filter or []) if s}
        if mode == "whitelist" and filt and sym_u not in filt:
            return False, f"symbol {master_symbol} not in whitelist"
        if mode == "blacklist" and sym_u in filt:
            return False, f"symbol {master_symbol} is in blacklist"
        return True, "ok"

    def effective_lot_params(self, master_symbol: str) -> tuple[str, float, list[float], str]:
        """Returns ``(lot_mode, lot_value, allowed_lots, not_allowed_action)`` for ``master_symbol``."""
        sym_u = master_symbol.strip().upper()
        rule = self.symbol_rules.get(sym_u) or self.symbol_rules.get(master_symbol)
        if rule is None:
            return self.lot_mode, self.lot_value, [], "snap"
        mode = rule.lot_mode if rule.lot_mode else self.lot_mode
        val = rule.lot_value if rule.lot_value is not None else self.lot_value
        return mode, float(val), list(rule.allowed_lots or []), (rule.not_allowed_action or "snap")


def load_slave_config(account_id: int) -> SlaveConfig:
    with session_scope() as s:
        st = s.get(SlaveSettings, account_id)
        if st is None:
            st = SlaveSettings(account_id=account_id)
            s.add(st)
            s.flush()
        rules_rows = list(s.scalars(select(SymbolRule).where(SymbolRule.account_id == account_id)))
        map_rows = list(s.scalars(select(SymbolMap).where(SymbolMap.account_id == account_id)))

        sym_rules = {
            r.master_symbol: PerSymbolRule(
                master_symbol=r.master_symbol,
                enabled=bool(r.enabled),
                lot_mode=r.lot_mode,
                lot_value=r.lot_value,
                allowed_lots=[float(x) for x in (r.allowed_lots or [])],
                not_allowed_action=r.not_allowed_action or "snap",
            )
            for r in rules_rows
        }
        sym_map = {m.master_symbol: m.slave_symbol for m in map_rows if m.slave_symbol}
        return SlaveConfig(
            account_id=account_id,
            paused=bool(st.paused),
            reverse=bool(st.reverse),
            lot_mode=st.lot_mode or "multiplier",
            lot_value=float(st.lot_value if st.lot_value is not None else 1.0),
            min_lot=st.min_lot,
            max_lot=st.max_lot,
            max_open_trades=st.max_open_trades,
            symbol_filter_mode=st.symbol_filter_mode or "none",
            symbol_filter=list(st.symbol_filter or []),
            deviation_points=int(st.deviation_points if st.deviation_points is not None else 20),
            copy_pending=bool(st.copy_pending),
            copy_sltp=bool(st.copy_sltp),
            late_copy_max_age_s=int(st.late_copy_max_age_s if st.late_copy_max_age_s is not None else 300),
            late_copy_max_points=int(st.late_copy_max_points if st.late_copy_max_points is not None else 30),
            close_orphans=bool(st.close_orphans),
            config_version=int(st.config_version or 1),
            symbol_rules=sym_rules,
            symbol_map=sym_map,
        )


def save_slave_settings(account_id: int, **updates) -> int:
    """Update slave settings and bump ``config_version``. Returns the new version."""
    with session_scope() as s:
        st = s.get(SlaveSettings, account_id)
        if st is None:
            st = SlaveSettings(account_id=account_id)
            s.add(st)
        for k, v in updates.items():
            if hasattr(st, k):
                setattr(st, k, v)
        st.config_version = int(st.config_version or 0) + 1
        s.flush()
        return st.config_version


def upsert_symbol_rule(
    account_id: int,
    master_symbol: str,
    *,
    enabled: bool = True,
    lot_mode: str | None = None,
    lot_value: float | None = None,
    allowed_lots: list[float] | None = None,
    not_allowed_action: str = "snap",
) -> int:
    master_symbol = master_symbol.strip().upper()
    with session_scope() as s:
        row = s.scalar(
            select(SymbolRule).where(
                SymbolRule.account_id == account_id,
                SymbolRule.master_symbol == master_symbol,
            )
        )
        if row is None:
            row = SymbolRule(account_id=account_id, master_symbol=master_symbol)
            s.add(row)
        row.enabled = enabled
        row.lot_mode = lot_mode or None
        row.lot_value = lot_value
        row.allowed_lots = [float(x) for x in (allowed_lots or [])]
        row.not_allowed_action = not_allowed_action
    return save_slave_settings(account_id)


def delete_symbol_rule(account_id: int, rule_id: int) -> int:
    with session_scope() as s:
        row = s.get(SymbolRule, rule_id)
        if row and row.account_id == account_id:
            s.delete(row)
    return save_slave_settings(account_id)


def upsert_symbol_map(account_id: int, master_symbol: str, slave_symbol: str,
                      auto_detected: bool = False) -> int:
    master_symbol = master_symbol.strip()
    slave_symbol = slave_symbol.strip()
    with session_scope() as s:
        row = s.scalar(
            select(SymbolMap).where(
                SymbolMap.account_id == account_id,
                SymbolMap.master_symbol == master_symbol,
            )
        )
        if row is None:
            row = SymbolMap(account_id=account_id, master_symbol=master_symbol,
                            slave_symbol=slave_symbol, auto_detected=auto_detected)
            s.add(row)
        else:
            row.slave_symbol = slave_symbol
            row.auto_detected = auto_detected
    return save_slave_settings(account_id)

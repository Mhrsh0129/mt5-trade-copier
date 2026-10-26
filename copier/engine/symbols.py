"""Per-broker symbol mapping and suffix/alias auto-detection (architecture §5.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..adapters.base import SymbolSpec

# Bidirectional alias groups: any symbol in a group can match any other in the same group.
_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("XAUUSD", "GOLD"),
    ("XAGUSD", "SILVER"),
    ("US30", "DJ30", "WS30", "US30CASH", "DJI30"),
    ("NAS100", "USTEC", "US100", "NDX100", "USTECH", "NAS100CASH"),
    ("US500", "SPX500", "SP500", "US500CASH"),
    ("GER40", "DE40", "DAX40", "GER30", "DE30"),
    ("UK100", "FTSE100"),
    ("JP225", "JPN225", "NIKKEI225"),
    ("USOIL", "WTI", "XTIUSD", "CL"),
    ("UKOIL", "BRENT", "XBRUSD"),
    ("BTCUSD", "BTCUSDT", "XBTUSD"),
    ("ETHUSD", "ETHUSDT"),
)

_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    sym: grp for grp in _ALIAS_GROUPS for sym in grp
}

# Common broker suffix/prefix patterns: EURUSD.m, EURUSDm, EURUSD.pro, EURUSD-ECN, EURUSD_i, #AAPL, .US30Cash
_PREFIX_RE = re.compile(r"^[#._]+")
_SEP_SUFFIX_RE = re.compile(r"([._-][A-Za-z0-9]{1,8})+$")
# Single-letter trailing suffix on standard 6-char FX/metal pairs, e.g. EURUSDm, XAUUSDc, GBPUSDz
_BARE_FX_SUFFIX_RE = re.compile(r"^([A-Z]{6})([a-z]|[A-Z]{1,2})$")


def normalize_symbol(symbol: str) -> str:
    """Strip common broker prefixes and suffixes to get the canonical base symbol."""
    s = _PREFIX_RE.sub("", symbol.strip())
    # Strip separator-delimited suffixes like .m, .pro, -ECN, _i, #
    s = _SEP_SUFFIX_RE.sub("", s)
    s = s.rstrip("#+!")
    # Check 6-letter FX/metal root with bare trailing micro/cent suffix like EURUSDm or XAUUSD.a
    m = _BARE_FX_SUFFIX_RE.match(s)
    if m and m.group(1) in _KNOWN_6CHAR_ROOTS:
        return m.group(1).upper()
    return s.upper()


_KNOWN_6CHAR_ROOTS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF", "CADJPY", "CADCHF", "CHFJPY", "NZDJPY", "NZDCAD", "NZDCHF",
    "XAUUSD", "XAGUSD", "XTIUSD", "XBRUSD", "BTCUSD", "ETHUSD",
}


def canonical_candidates(master_symbol: str) -> list[str]:
    """Return canonical base names that represent ``master_symbol`` (itself + aliases)."""
    base = normalize_symbol(master_symbol)
    aliases = _ALIAS_MAP.get(base, (base,))
    out = [base]
    for a in aliases:
        if a not in out:
            out.append(a)
    return out


@dataclass(frozen=True)
class SymbolResolution:
    master_symbol: str
    slave_symbol: str | None
    source: str  # "explicit" | "exact" | "auto_suffix" | "auto_alias" | "unmapped"
    contract_size_warning: str | None = None


def resolve_symbol(
    master_symbol: str,
    *,
    explicit_map: dict[str, str] | None = None,
    slave_symbols: list[str] | set[str] | dict[str, SymbolSpec] | None = None,
    master_contract_size: float | None = None,
) -> SymbolResolution:
    """Resolve ``master_symbol`` to a slave broker symbol.

    1. Explicit per-slave mapping wins (``XAUUSD -> GOLD``).
    2. Exact match in ``slave_symbols``.
    3. Suffix-stripped match (``EURUSD -> EURUSD.m``, ``XAUUSD -> XAUUSD.pro``).
    4. Alias match with or without suffix (``XAUUSD -> GOLD``, ``XAUUSD -> GOLD.m``, ``US30 -> WS30.cash``).
    """
    explicit = explicit_map or {}
    explicit_ci = {k.upper(): v for k, v in explicit.items() if v}
    if master_symbol in explicit and explicit[master_symbol]:
        target = explicit[master_symbol]
        warn = _check_contract_size(master_symbol, target, slave_symbols, master_contract_size)
        return SymbolResolution(master_symbol, target, "explicit", warn)
    if master_symbol.upper() in explicit_ci:
        target = explicit_ci[master_symbol.upper()]
        warn = _check_contract_size(master_symbol, target, slave_symbols, master_contract_size)
        return SymbolResolution(master_symbol, target, "explicit", warn)

    if not slave_symbols:
        return SymbolResolution(master_symbol, master_symbol, "exact")

    spec_map: dict[str, SymbolSpec] = slave_symbols if isinstance(slave_symbols, dict) else {}
    names = list(slave_symbols.keys()) if isinstance(slave_symbols, dict) else list(slave_symbols)
    name_set = set(names)

    # 2. Exact match
    if master_symbol in name_set:
        warn = _check_contract_size(master_symbol, master_symbol, spec_map, master_contract_size)
        return SymbolResolution(master_symbol, master_symbol, "exact", warn)

    # Build normalized index of slave symbols (prefer trade_allowed + shorter suffix on ties)
    by_norm: dict[str, list[str]] = {}
    for s in names:
        norm = normalize_symbol(s)
        by_norm.setdefault(norm, []).append(s)
    for norm_key, items in by_norm.items():
        items.sort(key=lambda x: (
            0 if (not spec_map or spec_map[x].trade_allowed) else 1,
            len(x),
            x,
        ))

    base = normalize_symbol(master_symbol)
    if base in by_norm:
        chosen = by_norm[base][0]
        warn = _check_contract_size(master_symbol, chosen, spec_map, master_contract_size)
        return SymbolResolution(master_symbol, chosen, "auto_suffix", warn)

    for alias in canonical_candidates(master_symbol):
        if alias in name_set:
            warn = _check_contract_size(master_symbol, alias, spec_map, master_contract_size)
            return SymbolResolution(master_symbol, alias, "auto_alias", warn)
        if alias in by_norm:
            chosen = by_norm[alias][0]
            warn = _check_contract_size(master_symbol, chosen, spec_map, master_contract_size)
            return SymbolResolution(master_symbol, chosen, "auto_alias", warn)

    return SymbolResolution(master_symbol, None, "unmapped")


def auto_detect_symbol_map(
    master_symbols: list[str],
    slave_symbols: list[str] | dict[str, SymbolSpec],
    explicit_map: dict[str, str] | None = None,
    master_specs: dict[str, SymbolSpec] | None = None,
) -> list[SymbolResolution]:
    """Batch-resolve a list of master symbols against a slave broker's symbol catalog."""
    results: list[SymbolResolution] = []
    for m_sym in master_symbols:
        m_cs = master_specs[m_sym].contract_size if (master_specs and m_sym in master_specs) else None
        results.append(resolve_symbol(
            m_sym,
            explicit_map=explicit_map,
            slave_symbols=slave_symbols,
            master_contract_size=m_cs,
        ))
    return results


def _check_contract_size(
    master_symbol: str,
    slave_symbol: str,
    slave_specs: dict[str, SymbolSpec] | list[str] | set[str] | None,
    master_contract_size: float | None,
) -> str | None:
    if not isinstance(slave_specs, dict) or not master_contract_size or master_contract_size <= 0:
        return None
    s_spec = slave_specs.get(slave_symbol)
    if s_spec is None or s_spec.contract_size <= 0:
        return None
    if abs(s_spec.contract_size - master_contract_size) > 1e-6:
        return (f"Contract size mismatch: master {master_symbol}={master_contract_size:g} vs "
                f"slave {slave_symbol}={s_spec.contract_size:g}")
    return None

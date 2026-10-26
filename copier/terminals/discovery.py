"""Auto-discovery of installed MT5 terminals, broker servers, and catalog suggestions.

Makes adding accounts in the dashboard as easy and mistake-free as inside MT5:
1. Auto-discovers installed MT5 folders on the machine (from ``%APPDATA%\\MetaQuotes\\Terminal\\*\\origin.txt``,
   ``C:\\Program Files\\*MetaTrader*``, and ``D:\\Applications\\*MT5*``).
2. Extracts all trade servers already known on this machine from every terminal's
   ``bases/`` directory and ``logs/*.log`` files (including ``login -> server`` pairs
   so selecting a known login auto-fills its server!).
3. Provides a curated catalog of common MT5 broker server names (Exness, XM,
   ICMarkets, Vantage, FTMO, Pepperstone, Tickmill, EC Markets, MajestyFX,
   VPFX, Eightcap, FP Markets, RoboForex, FxPro, etc.) for instant autocomplete.
4. Automatically copies the largest local ``servers.dat`` when provisioning a
   portable terminal so the new terminal recognizes every broker server immediately.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .provision import TERMINAL_EXE, _read_text_any

_SKIP_BASE_DIRS = {"chats", "custom", "default", "signals", "subscriptions", "temp", "tester"}
_AUTH_LOG_RE = re.compile(r"'(\d+)':\s+authorized on\s+([^\r\n\t(]+?)(?:\s+through\s+|\s*\(|\r|\n|$)")

# Curated catalog of popular MT5 broker servers so users get autocomplete even for new servers
CURATED_BROKER_SERVERS: dict[str, list[str]] = {
    "Exness": [
        "Exness-MT5Real",
        *[f"Exness-MT5Real{i}" for i in range(1, 55)],
        "Exness-MT5Trial",
        *[f"Exness-MT5Trial{i}" for i in range(1, 25)],
    ],
    "XMGlobal": [
        "XMGlobal-MT5",
        *[f"XMGlobal-MT5 {i}" for i in range(2, 16)],
        "XMGlobal-MT5 Demo",
    ],
    "ICMarkets": [
        "ICMarketsSC-Demo",
        *[f"ICMarketsSC-MT5-{i}" for i in range(1, 10)],
        "ICMarketsEU-Demo",
        "ICMarketsEU-MT5-1",
        "ICMarketsGlobal-Demo",
    ],
    "Vantage": [
        "VantageInternational-Demo",
        *[f"VantageInternational-Live {i}" for i in range(1, 15)],
        "VantageMarkets-Demo",
        "VantageMarkets-Live",
    ],
    "ECMarkets": [
        "ECMarketsLtd-MT5-Demo",
        *[f"ECMarketsLtd-MT5-Live0{i}" for i in range(1, 10)],
    ],
    "MajestyFX": [
        "MajestyFX-Trade",
        "MajestyFX-Demo",
    ],
    "VPFX": [
        "VPFXCapitalMarkets-Server",
        "VPFXCapitalMarkets-Demo",
    ],
    "Pepperstone": [
        "Pepperstone-Demo",
        "Pepperstone-MT5-Live01",
        "PepperstoneUK-Demo",
        "PepperstoneUK-Live",
    ],
    "FTMO": [
        "FTMO-Demo",
        "FTMO-Demo2",
        "FTMO-Server",
        "FTMO-Server2",
        "FTMO-Server3",
        "FTMO-Server4",
    ],
    "Tickmill": [
        "Tickmill-Demo",
        "Tickmill-Live",
        "TickmillUK-Demo",
        "TickmillUK-Live",
    ],
    "FP Markets": [
        "FPMarketsLLC-Demo",
        "FPMarketsLLC-Live",
        "FPMarkets-Demo",
        "FPMarkets-Live",
    ],
    "Eightcap": [
        "Eightcap-Demo",
        "Eightcap-Real",
    ],
    "MetaQuotes": [
        "MetaQuotes-Demo",
    ],
}


@dataclass
class DiscoveredTerminal:
    label: str
    install_dir: str
    data_dir: str | None = None
    servers_dat: str | None = None
    servers: list[str] = field(default_factory=list)


@dataclass
class DiscoveryCatalog:
    terminals: list[DiscoveredTerminal] = field(default_factory=list)
    local_servers: list[str] = field(default_factory=list)
    all_servers: list[str] = field(default_factory=list)
    brokers: list[str] = field(default_factory=list)
    login_to_server: dict[str, tuple[str, str]] = field(default_factory=dict)  # login -> (server, install_dir)


def _appdata_terminal_root() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "MetaQuotes" / "Terminal"
    return root if root.is_dir() else None


def find_best_servers_dat() -> Path | None:
    """Return the largest ``servers.dat`` found across all local MT5 installations.

    Copying the richest ``servers.dat`` into every provisioned portable terminal
    means that any server known to *any* MT5 terminal on the laptop works out of
    the box in every worker terminal.
    """
    root = _appdata_terminal_root()
    candidates: list[Path] = []
    if root is not None:
        candidates.extend(root.glob("*/config/servers.dat"))
    best: Path | None = None
    best_size = -1
    for c in candidates:
        try:
            sz = c.stat().st_size
            if sz > best_size:
                best = c
                best_size = sz
        except OSError:
            continue
    return best


def find_best_appdata_config_dir() -> Path | None:
    """Return the AppData ``config`` directory belonging to the richest MT5 installation."""
    best = find_best_servers_dat()
    return best.parent if best is not None else None


def _servers_in_bases(bases_dir: Path) -> set[str]:
    out: set[str] = set()
    if not bases_dir.is_dir():
        return out
    for child in bases_dir.iterdir():
        if child.is_dir() and child.name.lower() not in _SKIP_BASE_DIRS:
            out.add(child.name)
    return out


def _scan_logs_for_logins(logs_dir: Path) -> dict[str, str]:
    """Return ``{login: server_name}`` discovered from MT5 terminal log files."""
    found: dict[str, str] = {}
    if not logs_dir.is_dir():
        return found
    for lf in sorted(logs_dir.glob("*.log")):
        try:
            text = _read_text_any(lf)
        except OSError:
            continue
        for m in _AUTH_LOG_RE.finditer(text):
            login, srv = m.group(1).strip(), m.group(2).strip()
            if login and srv:
                found[login] = srv
    return found


def discover_mt5_environment() -> DiscoveryCatalog:
    terminals_by_path: dict[str, DiscoveredTerminal] = {}
    local_servers: set[str] = set()
    login_to_server: dict[str, tuple[str, str]] = {}

    # 1. Scan %APPDATA%\MetaQuotes\Terminal\*\origin.txt
    root = _appdata_terminal_root()
    if root is not None:
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            origin_file = d / "origin.txt"
            if not origin_file.exists():
                continue
            try:
                install_path = Path(_read_text_any(origin_file).strip())
            except OSError:
                continue
            if not (install_path / TERMINAL_EXE).exists():
                continue

            srvs = _servers_in_bases(d / "bases") | _servers_in_bases(install_path / "Bases")
            log_pairs = _scan_logs_for_logins(d / "logs") | _scan_logs_for_logins(install_path / "logs")
            for lg, sv in log_pairs.items():
                srvs.add(sv)
                login_to_server[lg] = (sv, str(install_path))

            local_servers.update(srvs)
            sdat = d / "config" / "servers.dat"
            key = str(install_path.resolve()).lower()
            label = install_path.name
            terminals_by_path[key] = DiscoveredTerminal(
                label=label,
                install_dir=str(install_path.resolve()),
                data_dir=str(d),
                servers_dat=str(sdat) if sdat.exists() else None,
                servers=sorted(srvs),
            )

    # 2. Scan common install roots (Program Files, D:\Applications) for any additional MT5 folders
    for scan_root in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(r"D:\Applications"),
    ):
        if not scan_root.is_dir():
            continue
        for child in scan_root.iterdir():
            try:
                if not child.is_dir() or not (child / TERMINAL_EXE).exists():
                    continue
            except OSError:
                continue
            key = str(child.resolve()).lower()
            if key not in terminals_by_path:
                srvs = _servers_in_bases(child / "Bases")
                local_servers.update(srvs)
                terminals_by_path[key] = DiscoveredTerminal(
                    label=child.name,
                    install_dir=str(child.resolve()),
                    servers=sorted(srvs),
                )

    # 3. Combine local discovered servers with the curated broker server catalog
    all_servers_set = set(local_servers)
    for srv_list in CURATED_BROKER_SERVERS.values():
        all_servers_set.update(srv_list)

    # Order: local discovered servers first (alphabetical), then curated servers
    ordered_local = sorted(local_servers)
    ordered_rest = sorted(all_servers_set - local_servers)
    all_servers = ordered_local + ordered_rest

    brokers = sorted(
        set(CURATED_BROKER_SERVERS.keys())
        | {s.split("-")[0].strip() for s in ordered_local if "-" in s}
    )

    return DiscoveryCatalog(
        terminals=list(terminals_by_path.values()),
        local_servers=ordered_local,
        all_servers=all_servers,
        brokers=brokers,
        login_to_server=login_to_server,
    )

"""Portable terminal provisioning (architecture §3).

A *template* is an installed broker MT5 folder (contains ``terminal64.exe``).
For each account it is copied to ``<data>/terminals/acc_<id>/`` and always
launched with ``/portable`` (done by ``mt5.initialize(portable=True)``), so
each account has an isolated data folder and accounts from different brokers
run side by side.
"""

from __future__ import annotations

import configparser
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import get_paths

log = logging.getLogger(__name__)

TERMINAL_EXE = "terminal64.exe"

# Heavy or per-user paths (relative to the template root, lower-case posix) never copied.
_IGNORE_DIRS = {"logs", "tester", "bases", "mql5/logs", "mql5/files", "profiles/tester"}
_IGNORE_FILES = {"accounts.dat"}  # never copy saved logins from the template owner

# Slim/safe defaults written to config/common.ini. Algo trading must be ON for
# the Python API's order_send(). Values are verified at runtime by the spike
# (terminal_info().trade_allowed / maxbars) - see scripts/spike_phase1.py.
COMMON_INI_DEFAULTS: dict[str, dict[str, str]] = {
    "Experts": {"AllowLiveTrading": "1", "AllowDllImport": "0", "Enabled": "1", "Account": "0",
                "Profile": "0"},
    "Common": {"NewsEnable": "0"},
    "Charts": {"MaxBars": "10000"},
}


@dataclass(frozen=True)
class TemplateCheck:
    ok: bool
    reason: str
    exe: Path | None = None
    servers_dat: Path | None = None


def validate_template(source_dir: str | Path) -> TemplateCheck:
    src = Path(source_dir)
    if not src.is_dir():
        return TemplateCheck(False, f"Folder does not exist: {src}")
    exe = src / TERMINAL_EXE
    if not exe.exists():
        hint = ""
        if any(p.suffix.lower() == ".exe" and "setup" in p.name.lower() for p in src.glob("*.exe")):
            hint = " This looks like an installer folder: run the broker's setup once, then point to " \
                   "the installed folder (e.g. C:\\Program Files\\<Broker> MetaTrader 5)."
        return TemplateCheck(False, f"{TERMINAL_EXE} not found in {src}.{hint}")
    servers = find_servers_dat(src)
    return TemplateCheck(True, "ok", exe=exe, servers_dat=servers)


def find_servers_dat(install_dir: Path) -> Path | None:
    """Find the broker's servers.dat: in the install folder, or in the AppData data
    folder whose origin.txt points to that install folder."""
    local = install_dir / "config" / "servers.dat"
    if local.exists():
        return local
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "MetaQuotes" / "Terminal"
    if not root.is_dir():
        return None
    target = str(install_dir.resolve()).rstrip("\\/").lower()
    for d in root.iterdir():
        origin = d / "origin.txt"
        if not origin.exists():
            continue
        try:
            text = _read_text_any(origin).strip().rstrip("\\/").lower()
        except OSError:
            continue
        if text == target and (d / "config" / "servers.dat").exists():
            return d / "config" / "servers.dat"
    return None


def _read_text_any(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig", errors="replace")


def _ignore(src_root: Path):
    def _fn(dirpath: str, names: list[str]) -> set[str]:
        rel = Path(dirpath).relative_to(src_root)
        skipped = set()
        for n in names:
            relname = (rel / n).as_posix().lower()
            if n.lower() in _IGNORE_FILES or relname in _IGNORE_DIRS:
                skipped.add(n)
        return skipped
    return _fn


def account_terminal_dir(account_id: int) -> Path:
    return get_paths().terminals_dir / f"acc_{account_id}"


def provision_terminal(account_id: int, source_dir: str | Path, *, force: bool = False,
                       dest: Path | None = None, bootstrap_from_appdata: bool | None = None) -> Path:
    """Copy the template to the account's folder and write slim config. Returns the folder.

    Idempotent: if the folder already has terminal64.exe and ``force`` is False,
    only the config is (re)applied.
    """
    check = validate_template(source_dir)
    if not check.ok:
        raise FileNotFoundError(check.reason)
    src = Path(source_dir)
    use_default_dest = dest is None
    dest = dest or account_terminal_dir(account_id)
    if bootstrap_from_appdata is None:
        bootstrap_from_appdata = use_default_dest

    if force and dest.exists():
        kill_terminal_processes(dest)
        shutil.rmtree(dest)
    if not (dest / TERMINAL_EXE).exists():
        log.info("Provisioning terminal for account %s: %s -> %s", account_id, src, dest)
        shutil.copytree(src, dest, ignore=_ignore(src), dirs_exist_ok=True)

    cfg = dest / "config"
    cfg.mkdir(exist_ok=True)
    if bootstrap_from_appdata:
        # Copy the richest servers.dat and bootstrap accounts.dat so portable terminals never block
        # on the first-run "Open an Account" wizard and know all broker servers.
        from .discovery import find_best_appdata_config_dir, find_best_servers_dat
        sdat = find_best_servers_dat() or check.servers_dat
        if sdat and Path(sdat).exists():
            shutil.copy2(sdat, cfg / "servers.dat")
        best_cfg = find_best_appdata_config_dir()
        if best_cfg is not None:
            for fname in ("accounts.dat", "common.ini", "terminal.ini"):
                src_f = best_cfg / fname
                if src_f.exists() and not (cfg / fname).exists():
                    shutil.copy2(src_f, cfg / fname)
    elif check.servers_dat and not (cfg / "servers.dat").exists():
        shutil.copy2(check.servers_dat, cfg / "servers.dat")
    apply_common_ini(cfg / "common.ini")
    return dest


def apply_common_ini(path: Path, overrides: dict[str, dict[str, str]] | None = None) -> None:
    """Merge our defaults into common.ini, preserving existing keys. MT5 ini files are UTF-16."""
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # preserve key case
    if path.exists():
        cp.read_string(_read_text_any(path))
    for section, values in {**COMMON_INI_DEFAULTS, **(overrides or {})}.items():
        if not cp.has_section(section):
            cp.add_section(section)
        for k, v in values.items():
            cp.set(section, k, v)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-16") as f:
        cp.write(f, space_around_delimiters=False)
    os.replace(tmp, path)


# ------------------------------------------------------------------ processes
def _win_find_terminal_pids(terminal_dir: Path) -> list[tuple[int, int]]:
    """Fallback when psutil is unavailable: returns [(pid, rss_bytes)] for terminal64.exe
    processes running from ``terminal_dir`` using Win32 Toolhelp + QueryFullProcessImageNameW."""
    if os.name != "nt":  # pragma: no cover
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return []
    target = str(Path(terminal_dir).resolve()).lower()
    matches: list[tuple[int, int]] = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == TERMINAL_EXE:
                hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
                if hproc:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        size = wintypes.DWORD(1024)
                        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                            parent = str(Path(buf.value).parent.resolve()).lower()
                            if parent == target:
                                pmc = PROCESS_MEMORY_COUNTERS()
                                pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                                rss = pmc.WorkingSetSize if psapi.GetProcessMemoryInfo(
                                    hproc, ctypes.byref(pmc), pmc.cb) else 0
                                matches.append((int(pe.th32ProcessID), int(rss)))
                    finally:
                        kernel32.CloseHandle(hproc)
            ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return matches


def terminal_processes(terminal_dir: Path) -> list:
    """terminal64.exe processes running from this account's folder only."""
    try:
        import psutil
    except ImportError:
        return [pid for pid, _ in _win_find_terminal_pids(terminal_dir)]
    target = str(Path(terminal_dir).resolve()).lower()
    out = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info["name"] or "").lower() == TERMINAL_EXE and p.info["exe"] and \
                    str(Path(p.info["exe"]).parent.resolve()).lower() == target:
                out.append(p)
        except Exception:  # noqa: BLE001 - access denied / vanished
            continue
    return out


def kill_terminal_processes(terminal_dir: Path, timeout: float = 10.0) -> int:
    try:
        import psutil
        procs = terminal_processes(terminal_dir)
        for p in procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        if procs:
            _, alive = psutil.wait_procs(procs, timeout=timeout)
            for p in alive:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        return len(procs)
    except ImportError:
        pids = _win_find_terminal_pids(terminal_dir)
        if not pids or os.name != "nt":
            return 0
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        for pid, _ in pids:
            h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h:
                kernel32.TerminateProcess(h, 1)
                kernel32.CloseHandle(h)
        return len(pids)


def terminal_memory_mb(terminal_dir: Path) -> float | None:
    try:
        import psutil  # noqa: F401
        procs = terminal_processes(terminal_dir)
        if not procs:
            return None
        total = 0
        for p in procs:
            try:
                total += p.memory_info().rss
            except Exception:  # noqa: BLE001
                pass
        return round(total / 1_048_576, 1)
    except ImportError:
        pids = _win_find_terminal_pids(terminal_dir)
        if not pids:
            return None
        return round(sum(rss for _, rss in pids) / 1_048_576, 1)

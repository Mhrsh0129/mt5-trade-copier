"""FastAPI web dashboard & health check endpoint (architecture §8.2, §9)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from .. import accounts
from .. import auth as admin_auth
from ..alerts.outbox import record_event_log
from ..alerts.telegram import (
    get_heartbeat_url,
    get_telegram_config,
    set_heartbeat_url,
    set_telegram_config,
)
from ..bus import DEFAULT_EVENT_PORT, Publisher
from ..db import AppSetting, EventLog, TicketMap, TradeAudit, init_db, session_scope
from ..engine.rules import (
    load_slave_config,
    save_slave_settings,
    upsert_symbol_map,
    upsert_symbol_rule,
)
from ..terminals.discovery import CURATED_BROKER_SERVERS, discover_mt5_environment
from ..terminals.provision import terminal_memory_mb
from .auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    authenticate,
    create_session_token,
    read_session,
    require_user,
    verify_csrf,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="MT5 Trade Copier Dashboard")
_bus_pub = Publisher()

# Pluggable connect runner so tests can inject a FakeAdapter-backed connector;
# in production it spawns a clean ``python -m copier.worker --connect-only`` subprocess
# so the web server process never binds to a terminal.
_connect_runner: Callable[[int], accounts.ConnectResult] | None = None


def set_connect_runner_for_tests(fn: Callable[[int], accounts.ConnectResult] | None) -> None:
    global _connect_runner
    _connect_runner = fn


def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cached_connect_result(acc) -> accounts.ConnectResult:
    from ..adapters.base import AccountInfo, MarginMode
    info = AccountInfo(
        login=int(acc.login),
        name=acc.account_name or "",
        server=acc.server,
        company=acc.broker,
        currency=acc.currency or "USD",
        balance=float(acc.balance or 0.0),
        equity=float(acc.equity if acc.equity is not None else (acc.balance or 0.0)),
        margin_free=float(acc.equity if acc.equity is not None else (acc.balance or 0.0)),
        leverage=int(acc.leverage or 100),
        margin_mode=MarginMode(acc.margin_mode or "hedging"),
        trade_allowed=True,
        algo_allowed=True,
    )
    return accounts.ConnectResult(True, accounts.format_success(info), info=info)


def _supervisor_running(port: int = DEFAULT_EVENT_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3) as sock:
            sock.sendall(b'{"topic":"ping","payload":{}}\n')
            return True
    except OSError:
        return False


def _connect_via_worker(account_id: int, timeout_s: float = 75.0) -> accounts.ConnectResult:
    """Ask the account's running worker to retry now and wait for the real outcome.

    Spawning a second process against the same terminal makes both fight over the
    MT5 IPC pipe (``IPC send failed`` / endless reconnect backoff), so when the
    supervisor is running the worker is the only one allowed to touch the terminal.
    """
    from ..worker import attempt_key
    t0 = time.time()
    _bus_pub.publish_sync("control", {"cmd": "reconnect_now", "account_id": account_id})
    deadline = t0 + timeout_s
    while time.time() < deadline:
        time.sleep(0.5)
        acc = accounts.get_account(account_id)
        synced = _as_utc(acc.last_sync_at)
        if acc.status == "connected" and synced and synced.timestamp() >= t0:
            return _cached_connect_result(acc)
        with session_scope() as s:
            row = s.get(AppSetting, attempt_key(account_id))
            raw = row.value if row else None
        if raw:
            try:
                rec = json.loads(raw)
            except ValueError:
                rec = {}
            if float(rec.get("ts", 0)) >= t0 and not rec.get("ok"):
                return accounts.ConnectResult(False, str(rec.get("error") or "connect failed"))
    acc = accounts.get_account(account_id)
    return accounts.ConnectResult(
        False, f"No answer from the account worker within {timeout_s:.0f}s. Last status: "
               f"{acc.status} - {acc.last_error or 'n/a'}")


def _run_connect(account_id: int) -> accounts.ConnectResult:
    if _connect_runner is not None:
        return _connect_runner(account_id)
    acc = accounts.get_account(account_id)
    synced = _as_utc(acc.last_sync_at)
    fresh = synced is not None and (datetime.now(timezone.utc) - synced).total_seconds() < 15
    # Worker is live and already connected: report its status instead of touching the terminal.
    if acc.status == "connected" and acc.account_name and acc.balance is not None and fresh:
        return _cached_connect_result(acc)
    if acc.enabled and _supervisor_running():
        return _connect_via_worker(account_id)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "copier.worker", "--account-id", str(account_id), "--connect-only"],
            capture_output=True,
            text=True,
            timeout=70,
            check=False,
        )
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        return accounts.ConnectResult(
            success=bool(data.get("success")),
            message=str(data.get("message", "")),
            warnings=list(data.get("warnings") or []),
            error_code=data.get("error_code"),
        )
    except Exception:  # noqa: BLE001
        return accounts.connect_account(account_id, keep_connected=False)


def _redirect(url: str, *, ok: str | None = None, err: str | None = None, warn: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    params = []
    if ok:
        params.append(f"ok={quote_plus(ok)}")
    if err:
        params.append(f"err={quote_plus(err)}")
    if warn:
        params.append(f"warn={quote_plus(warn)}")
    target = f"{url}{sep}{'&'.join(params)}" if params else url
    return RedirectResponse(url=target, status_code=303)


def _ctx(request: Request, nav: str, **extra) -> dict:
    init_db()
    return {
        "request": request,
        "session": read_session(request),
        "nav": nav,
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
        "flash_warn": request.query_params.get("warn"),
        **extra,
    }


# ================================================================ Auth
@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse("/accounts" if read_session(request) else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    init_db()
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "session": None, "admin_initialized": admin_auth.admin_exists(), "error": None},
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(...)):
    init_db()
    ip = request.client.host if request.client else "127.0.0.1"
    ok, msg = authenticate(ip, password)
    if not ok:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "session": None, "admin_initialized": admin_auth.admin_exists(), "error": msg},
            status_code=401,
        )
    token, _ = create_session_token("admin")
    resp = RedirectResponse("/accounts", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
    )
    return resp


@app.post("/logout")
def logout_submit(request: Request, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ================================================================ Health
@app.get("/health")
def health_endpoint():
    init_db()
    accs = accounts.list_accounts()
    with session_scope() as s:
        online_row = s.get(AppSetting, "internet_online")
        paused_row = s.get(AppSetting, "global_paused")
        internet_online = (online_row.value != "0") if online_row else True
        global_paused = (paused_row.value == "1") if paused_row else False

    error_accounts = [a.id for a in accs if a.enabled and a.status == "error"]
    healthy = internet_online and not error_accounts
    payload = {
        "healthy": healthy,
        "internet_online": internet_online,
        "global_paused": global_paused,
        "accounts": [
            {
                "id": a.id,
                "role": a.role,
                "broker": a.broker,
                "server": a.server,
                "login": a.login,
                "status": a.status,
                "last_error": a.last_error,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
            }
            for a in accs
        ],
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)


# ================================================================ Accounts & Templates
@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    require_user(request)
    accounts.ensure_discovered_templates()
    accs = accounts.list_accounts()
    for a in accs:
        a.ram_mb = terminal_memory_mb(Path(a.terminal_dir)) if a.terminal_dir else None
    tmpls = accounts.list_templates()
    catalog = discover_mt5_environment()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        _ctx(
            request,
            "accounts",
            accounts=accs,
            templates=tmpls,
            catalog=catalog,
            curated_broker_servers=CURATED_BROKER_SERVERS,
        ),
    )


@app.post("/templates/add")
def template_add_submit(
    request: Request,
    csrf: str = Form(...),
    broker: str = Form(...),
    source_dir: str = Form(...),
):
    verify_csrf(request, csrf)
    try:
        accounts.add_template(broker, source_dir)
        return _redirect("/accounts", ok=f"Saved terminal template for '{broker}'.")
    except accounts.ValidationError as e:
        return _redirect("/accounts", err=str(e))


@app.post("/accounts/add")
def account_add_submit(
    request: Request,
    csrf: str = Form(...),
    role: str = Form(...),
    broker: str = Form(...),
    server: str = Form(...),
    login: int = Form(...),
    password: str = Form(...),
):
    verify_csrf(request, csrf)
    try:
        # If the user typed a broker label (e.g. "Exness" or "XMGlobal") that isn't a template name yet,
        # automatically bind it to the first discovered MT5 installation on this PC so they never get an error.
        tmpls = accounts.ensure_discovered_templates()
        has_local_mt5 = bool(tmpls)
        if tmpls and not any(t.broker.lower() == broker.strip().lower() for t in tmpls):
            accounts.add_template(broker.strip(), tmpls[0].source_dir)
        elif not tmpls:
            # Cloud/Serverless preview mode (e.g. Vercel Linux) where no local terminal64.exe exists:
            from ..db import TerminalTemplate, session_scope
            with session_scope() as s:
                s.add(TerminalTemplate(broker=broker.strip(), source_dir="/cloud-preview"))
        acc_id = accounts.add_account(
            role=role, broker=broker, server=server, login=login, password=password, provision=has_local_mt5
        )
        record_event_log(f"Added {role} account #{acc_id} ({login}@{server})", account_id=acc_id, source="web")
        return _redirect("/accounts", ok=f"Account #{acc_id} ({login}@{server}) added and portable terminal provisioned.")
    except (accounts.ValidationError, FileNotFoundError) as e:
        return _redirect("/accounts", err=str(e))


@app.post("/accounts/{account_id}/connect")
def account_connect_submit(request: Request, account_id: int, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    res = _run_connect(account_id)
    warn = " | ".join(res.warnings) if res.warnings else None
    if res.success:
        return _redirect("/accounts", ok=res.message, warn=warn)
    return _redirect("/accounts", err=f"Connect failed: {res.message}")


@app.post("/accounts/{account_id}/delete")
def account_delete_submit(request: Request, account_id: int, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    try:
        acc = accounts.get_account(account_id)
        label = f"{acc.role} account #{account_id} ({acc.login}@{acc.server})"
    except accounts.ValidationError:
        return _redirect("/accounts", err=f"Account #{account_id} not found.")
    accounts.delete_account(account_id)
    ip = request.client.host if request.client else "?"
    record_event_log(f"Deleted {label} from {ip}", source="web", level="warn")
    return _redirect("/accounts", ok=f"Account #{account_id} deleted.")


# ================================================================ Slave Settings
@app.get("/slaves", response_class=HTMLResponse)
def slaves_page(request: Request, slave_id: int | None = None):
    require_user(request)
    slaves = [a for a in accounts.list_accounts() if a.role == "slave"]
    selected = None
    if slaves:
        selected = next((s for s in slaves if s.id == slave_id), slaves[0])
    cfg = load_slave_config(selected.id) if selected else None
    return templates.TemplateResponse(
        request, "slaves.html", _ctx(request, "slaves", slaves=slaves, selected=selected, cfg=cfg)
    )


@app.post("/slaves/{slave_id}/settings")
def slave_settings_submit(
    request: Request,
    slave_id: int,
    csrf: str = Form(...),
    paused: int = Form(0),
    reverse: int = Form(0),
    lot_mode: str = Form("multiplier"),
    lot_value: float = Form(1.0),
    min_lot: str = Form(""),
    max_lot: str = Form(""),
    max_open_trades: str = Form(""),
    deviation_points: int = Form(20),
    symbol_filter_mode: str = Form("none"),
    symbol_filter: str = Form(""),
    close_orphans: int = Form(0),
):
    verify_csrf(request, csrf)
    filt = [s.strip().upper() for s in symbol_filter.split(",") if s.strip()]
    ver = save_slave_settings(
        slave_id,
        paused=bool(paused),
        reverse=bool(reverse),
        lot_mode=lot_mode,
        lot_value=float(lot_value),
        min_lot=float(min_lot) if min_lot.strip() else None,
        max_lot=float(max_lot) if max_lot.strip() else None,
        max_open_trades=int(max_open_trades) if max_open_trades.strip() else None,
        deviation_points=int(deviation_points),
        symbol_filter_mode=symbol_filter_mode,
        symbol_filter=filt,
        close_orphans=bool(close_orphans),
    )
    _bus_pub.publish_sync("control", {"cmd": "config_changed", "slave_id": slave_id, "version": ver})
    return _redirect(f"/slaves?slave_id={slave_id}", ok=f"Slave #{slave_id} rules saved and hot-reloaded (v{ver}).")


@app.post("/slaves/{slave_id}/symbol-rule")
def slave_symbol_rule_submit(
    request: Request,
    slave_id: int,
    csrf: str = Form(...),
    master_symbol: str = Form(...),
    enabled: int = Form(1),
    lot_mode: str = Form(""),
    lot_value: str = Form(""),
    allowed_lots: str = Form(""),
    not_allowed_action: str = Form("snap"),
):
    verify_csrf(request, csrf)
    lots_list = [float(x.strip()) for x in allowed_lots.split(",") if x.strip()]
    ver = upsert_symbol_rule(
        slave_id,
        master_symbol,
        enabled=bool(enabled),
        lot_mode=lot_mode.strip() or None,
        lot_value=float(lot_value) if lot_value.strip() else None,
        allowed_lots=lots_list,
        not_allowed_action=not_allowed_action,
    )
    _bus_pub.publish_sync("control", {"cmd": "config_changed", "slave_id": slave_id, "version": ver})
    return _redirect(
        f"/slaves?slave_id={slave_id}",
        ok=f"Saved per-symbol rule for {master_symbol.upper()} (v{ver}).",
    )


@app.post("/slaves/{slave_id}/symbol-map")
def slave_symbol_map_submit(
    request: Request,
    slave_id: int,
    csrf: str = Form(...),
    master_symbol: str = Form(...),
    slave_symbol: str = Form(...),
):
    verify_csrf(request, csrf)
    ver = upsert_symbol_map(slave_id, master_symbol, slave_symbol, auto_detected=False)
    _bus_pub.publish_sync("control", {"cmd": "config_changed", "slave_id": slave_id, "version": ver})
    return _redirect(
        f"/slaves?slave_id={slave_id}",
        ok=f"Mapped {master_symbol} → {slave_symbol} on Slave #{slave_id}.",
    )


# ================================================================ Live Trades & Audit
@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request):
    require_user(request)
    with session_scope() as s:
        tmaps = list(
            s.scalars(
                select(TicketMap).where(TicketMap.state.in_(("open", "pending_send"))).order_by(desc(TicketMap.id))
            )
        )
        audits = list(s.scalars(select(TradeAudit).order_by(desc(TradeAudit.id)).limit(100)))
    return templates.TemplateResponse(
        request, "trades.html", _ctx(request, "trades", ticket_maps=tmaps, audits=audits)
    )


@app.post("/trades/reconcile")
def trades_reconcile_submit(request: Request, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    _bus_pub.publish_sync("control", {"cmd": "reconcile_now"})
    return _redirect("/trades", ok="Triggered immediate reconciliation across all connected slaves.")


# ================================================================ Logs
@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, account_id: str | None = None, level: str | None = None):
    require_user(request)
    acc_filter = int(account_id) if (account_id and account_id.strip().isdigit()) else None
    lvl_filter = level.strip().lower() if level else None
    with session_scope() as s:
        q = select(EventLog).order_by(desc(EventLog.id)).limit(200)
        if acc_filter is not None:
            q = q.where(EventLog.account_id == acc_filter)
        if lvl_filter:
            q = q.where(EventLog.level == lvl_filter)
        events = list(s.scalars(q))
    return templates.TemplateResponse(
        request,
        "logs.html",
        _ctx(request, "logs", events=events, account_id=acc_filter, level=lvl_filter or ""),
    )


# ================================================================ System
@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    require_user(request)
    tok, cid = get_telegram_config()
    hb = get_heartbeat_url()
    with session_scope() as s:
        online_row = s.get(AppSetting, "internet_online")
        paused_row = s.get(AppSetting, "global_paused")
        internet_online = (online_row.value != "0") if online_row else True
        global_paused = (paused_row.value == "1") if paused_row else False
    return templates.TemplateResponse(
        request,
        "system.html",
        _ctx(
            request,
            "system",
            internet_online=internet_online,
            global_paused=global_paused,
            has_bot_token=bool(tok),
            chat_id=cid,
            heartbeat_url=hb,
        ),
    )


@app.post("/system/toggle-pause")
def system_toggle_pause(request: Request, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    with session_scope() as s:
        paused_row = s.get(AppSetting, "global_paused")
        new_paused = not (paused_row and paused_row.value == "1")
        s.merge(AppSetting(key="global_paused", value="1" if new_paused else "0"))
    _bus_pub.publish_sync("control", {"cmd": "global_pause", "paused": new_paused})
    return _redirect("/system", ok="Global copying paused." if new_paused else "Global copying resumed.")


@app.post("/system/alerts")
def system_alerts_submit(
    request: Request,
    csrf: str = Form(...),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    heartbeat_url: str = Form(""),
):
    verify_csrf(request, csrf)
    existing_tok, _ = get_telegram_config()
    set_telegram_config(bot_token.strip() or (existing_tok or ""), chat_id.strip())
    set_heartbeat_url(heartbeat_url.strip())
    return _redirect("/system", ok="Alert and heartbeat settings saved.")

"""Account & template service layer (shared by the CLI and, later, the dashboard)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select

from . import crypto
from .adapters import AccountInfo, BrokerAdapter, ConnectError, Credentials, MarginMode, get_adapter
from .db import Account, SlaveSettings, TerminalTemplate, session_scope
from .db.models import utcnow
from .terminals.provision import TERMINAL_EXE, provision_terminal, validate_template

log = logging.getLogger(__name__)

MAX_SLAVES = 10


class ValidationError(ValueError):
    pass


# ------------------------------------------------------------------ templates
def add_template(broker: str, source_dir: str) -> int:
    broker = broker.strip()
    check = validate_template(source_dir)
    if not check.ok:
        raise ValidationError(check.reason)
    with session_scope() as s:
        existing = s.scalar(select(TerminalTemplate).where(TerminalTemplate.broker == broker))
        if existing:
            existing.source_dir = str(Path(source_dir).resolve())
            return existing.id
        t = TerminalTemplate(broker=broker, source_dir=str(Path(source_dir).resolve()))
        s.add(t)
        s.flush()
        if check.servers_dat is None:
            log.warning("No servers.dat found for template %s; login will rely on server search", broker)
        return t.id


def ensure_discovered_templates() -> list[TerminalTemplate]:
    """Auto-register any installed MT5 terminals found on this machine if no templates exist yet."""
    from .terminals.discovery import discover_mt5_environment
    existing = list_templates()
    if existing:
        return existing
    cat = discover_mt5_environment()
    for t in cat.terminals:
        try:
            add_template(t.label, t.install_dir)
        except Exception:  # noqa: BLE001
            continue
    return list_templates()


def list_templates() -> list[TerminalTemplate]:
    with session_scope() as s:
        return list(s.scalars(select(TerminalTemplate).order_by(TerminalTemplate.broker)))


# ------------------------------------------------------------------ accounts
def add_account(*, role: str, broker: str, server: str, login: int, password: str,
                label: str | None = None, provision: bool = True) -> int:
    role = role.lower().strip()
    if role not in ("master", "slave"):
        raise ValidationError("role must be 'master' or 'slave'")
    if not password:
        raise ValidationError("password is required")
    try:
        login = int(login)
    except (TypeError, ValueError):
        raise ValidationError("login must be numeric") from None

    with session_scope() as s:
        tmpl = s.scalar(select(TerminalTemplate).where(TerminalTemplate.broker == broker.strip()))
        if tmpl is None:
            raise ValidationError(f"No terminal template for broker '{broker}'. Add one first "
                                  f"(path to the broker's installed MT5 folder).")
        if s.scalar(select(Account).where(Account.server == server, Account.login == login)):
            raise ValidationError(f"Account {login}@{server} already exists")
        if role == "master" and s.scalar(select(func.count()).select_from(Account)
                                         .where(Account.role == "master")):
            raise ValidationError("A master account already exists (only one master is supported)")
        if role == "slave" and s.scalar(select(func.count()).select_from(Account)
                                        .where(Account.role == "slave")) >= MAX_SLAVES:
            raise ValidationError(f"Maximum of {MAX_SLAVES} slave accounts reached")

        acc = Account(role=role, label=label, broker=broker.strip(), server=server.strip(), login=login,
                      password_enc=crypto.encrypt(password), template_id=tmpl.id)
        s.add(acc)
        s.flush()
        if role == "slave":
            s.add(SlaveSettings(account_id=acc.id))
        acc_id, src = acc.id, tmpl.source_dir

    if provision:
        dest = provision_terminal(acc_id, src)
        with session_scope() as s:
            s.get(Account, acc_id).terminal_dir = str(dest)
    log.info("Added %s account id=%s login=%s server=%s", role, acc_id, login, server)
    return acc_id


def list_accounts() -> list[Account]:
    with session_scope() as s:
        return list(s.scalars(select(Account).order_by(Account.role, Account.id)))


def get_account(account_id: int) -> Account:
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is None:
            raise ValidationError(f"Account {account_id} not found")
        return acc


def update_password(account_id: int, password: str) -> None:
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is None:
            raise ValidationError(f"Account {account_id} not found")
        acc.password_enc = crypto.encrypt(password)


def delete_account(account_id: int) -> None:
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is not None:
            s.delete(acc)


def credentials_for(acc: Account) -> Credentials:
    return Credentials(login=int(acc.login), password=crypto.decrypt(acc.password_enc), server=acc.server)


# ------------------------------------------------------------------ connect
@dataclass
class ConnectResult:
    success: bool
    message: str
    info: AccountInfo | None = None
    warnings: list[str] = field(default_factory=list)
    error_code: int | None = None


def format_success(info: AccountInfo) -> str:
    return (f"Connected: {info.name} · {info.server} · Balance {info.balance:,.2f} {info.currency} · "
            f"Leverage 1:{info.leverage}")


def _warnings(role: str, info: AccountInfo) -> list[str]:
    w = []
    if role == "slave" and not info.trade_allowed:
        w.append("Trading is not allowed on this account (investor/read-only password?). Copies will fail.")
    if role == "slave" and not info.algo_allowed:
        w.append("Algo Trading is disabled in the terminal or for this account. Copies will fail with "
                 "retcode 10027 until it is enabled.")
    if info.margin_mode is MarginMode.NETTING:
        w.append("Netting account: positions per symbol are aggregated (see docs: netting limitations).")
    return w


def connect_account(account_id: int, adapter: BrokerAdapter | None = None,
                    keep_connected: bool = False) -> ConnectResult:
    """Launch the account's portable terminal, log in, and record status.

    Must run in a process that owns no other terminal (MT5 API limitation).
    """
    acc = get_account(account_id)
    adapter = adapter or get_adapter("mt5")
    terminal = Path(acc.terminal_dir) / TERMINAL_EXE if acc.terminal_dir else None
    try:
        creds = credentials_for(acc)
        info = adapter.connect(creds, terminal)
    except ConnectError as e:
        msg = e.reason + (f". {e.hint}" if e.hint else "")
        _record(account_id, status="error", last_error=msg)
        log.warning("Connect failed for account %s: %s", account_id, msg)
        return ConnectResult(False, msg, error_code=e.code)
    except Exception as e:  # noqa: BLE001
        msg = f"Unexpected error: {type(e).__name__}: {e}"
        _record(account_id, status="error", last_error=msg)
        log.exception("Connect crashed for account %s", account_id)
        return ConnectResult(False, msg)

    if info.server.lower() != acc.server.lower():
        log.warning("Account %s: server reported as %s (configured %s)", account_id, info.server, acc.server)
    _record(account_id, status="connected", last_error=None, account_name=info.name, currency=info.currency,
            balance=info.balance, equity=info.equity, leverage=info.leverage,
            margin_mode=info.margin_mode.value, last_sync_at=utcnow())
    if not keep_connected:
        adapter.disconnect()
    res = ConnectResult(True, format_success(info), info=info, warnings=_warnings(acc.role, info))
    log.info("Account %s %s", account_id, res.message)
    return res


def _record(account_id: int, **fields) -> None:
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is not None:
            for k, v in fields.items():
                setattr(acc, k, v)

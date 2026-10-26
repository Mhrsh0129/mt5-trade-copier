"""Command-line interface (Phase 1). The dashboard replaces most of this later.

    tc init                       create data dirs, key, DB; prompt admin password
    tc set-password               change admin password
    tc template add <broker> <installed_mt5_dir>
    tc template list
    tc account add --role slave --broker X --server S --login N   (password prompted)
    tc account list
    tc account connect <id>       launch terminal, log in, print result
    tc account reprovision <id>   re-copy the terminal from its template
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import accounts, crypto
from .config import get_paths
from .db import init_db
from .logging_setup import setup_logging


def _prompt_password(label: str, confirm: bool = True) -> str:
    while True:
        p1 = getpass.getpass(f"{label}: ")
        if not confirm:
            return p1
        if p1 == getpass.getpass(f"{label} (again): "):
            return p1
        print("Passwords do not match, try again.")


def cmd_init(args: argparse.Namespace) -> int:
    paths = get_paths()
    created = crypto.ensure_key()
    print(f"Data dir : {paths.data_dir}")
    print(f"Key file : {paths.key_file} ({'created' if created else 'exists'}; "
          f"{'DPAPI-protected' if crypto.HAS_DPAPI else 'raw'})")
    print(f"Database : {paths.db_file} (schema v{init_db()})")
    if args.no_prompt and not args.admin_password:
        return 0
    from . import auth
    if args.admin_password:
        auth.set_admin_password(args.admin_password)
    elif not auth.admin_exists():
        auth.set_admin_password(_prompt_password("Set admin password"))
    print("Admin    :", "set" if auth.admin_exists() else "NOT SET (run `tc set-password`)")
    return 0


def cmd_set_password(_args: argparse.Namespace) -> int:
    from . import auth
    init_db()
    auth.set_admin_password(_prompt_password("New admin password"))
    print("Admin password updated.")
    return 0


def cmd_template_add(args: argparse.Namespace) -> int:
    init_db()
    tid = accounts.add_template(args.broker, args.path)
    print(f"Template '{args.broker}' saved (id={tid}).")
    return 0


def cmd_template_list(_args: argparse.Namespace) -> int:
    init_db()
    for t in accounts.list_templates():
        print(f"{t.id:>3}  {t.broker:<30} {t.source_dir}")
    return 0


def cmd_account_add(args: argparse.Namespace) -> int:
    init_db()
    password = args.password or _prompt_password(f"Password for {args.login}", confirm=False)
    acc_id = accounts.add_account(role=args.role, broker=args.broker, server=args.server, login=args.login,
                                  password=password, label=args.label)
    print(f"Account added (id={acc_id}). Terminal: {accounts.get_account(acc_id).terminal_dir}")
    print(f"Next: tc account connect {acc_id}")
    return 0


def cmd_account_list(_args: argparse.Namespace) -> int:
    init_db()
    rows = accounts.list_accounts()
    if not rows:
        print("No accounts.")
    for a in rows:
        bal = f"{a.balance:,.2f} {a.currency}" if a.balance is not None else "-"
        print(f"{a.id:>3} {a.role:<6} {a.broker:<20} {a.login:<12} {a.server:<28} {a.status:<12} {bal:<18} "
              f"{a.last_sync_at or ''} {('ERR: ' + a.last_error) if a.status == 'error' else ''}")
    return 0


def cmd_account_connect(args: argparse.Namespace) -> int:
    init_db()
    res = accounts.connect_account(args.id, keep_connected=False)
    if args.json:
        print(json.dumps({"success": res.success, "message": res.message, "warnings": res.warnings,
                          "error_code": res.error_code}))
    else:
        print(("✅ " if res.success else "❌ ") + res.message)
        for w in res.warnings:
            print("⚠️  " + w)
    return 0 if res.success else 2


def cmd_account_reprovision(args: argparse.Namespace) -> int:
    from .terminals.provision import provision_terminal
    init_db()
    acc = accounts.get_account(args.id)
    tmpl = next(t for t in accounts.list_templates() if t.id == acc.template_id)
    dest = provision_terminal(acc.id, tmpl.source_dir, force=args.force)
    print(f"Terminal ready at {dest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tc", description="MT5 trade copier")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--admin-password", help="non-interactive (setup.ps1 passes it via stdin instead)")
    s.add_argument("--no-prompt", action="store_true")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("set-password").set_defaults(fn=cmd_set_password)

    t = sub.add_parser("template").add_subparsers(dest="tcmd", required=True)
    ta = t.add_parser("add")
    ta.add_argument("broker")
    ta.add_argument("path", help="installed MT5 folder containing terminal64.exe")
    ta.set_defaults(fn=cmd_template_add)
    t.add_parser("list").set_defaults(fn=cmd_template_list)

    a = sub.add_parser("account").add_subparsers(dest="acmd", required=True)
    aa = a.add_parser("add")
    aa.add_argument("--role", required=True, choices=["master", "slave"])
    aa.add_argument("--broker", required=True)
    aa.add_argument("--server", required=True)
    aa.add_argument("--login", required=True, type=int)
    aa.add_argument("--label")
    aa.add_argument("--password", help=argparse.SUPPRESS)  # testing only; prefer the prompt
    aa.set_defaults(fn=cmd_account_add)
    a.add_parser("list").set_defaults(fn=cmd_account_list)
    ac = a.add_parser("connect")
    ac.add_argument("id", type=int)
    ac.add_argument("--json", action="store_true")
    ac.set_defaults(fn=cmd_account_connect)
    ar = a.add_parser("reprovision")
    ar.add_argument("id", type=int)
    ar.add_argument("--force", action="store_true", help="delete and re-copy the terminal folder")
    ar.set_defaults(fn=cmd_account_reprovision)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # cp1252 consoles vs ✅/❌
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    setup_logging("cli", console=False)
    try:
        return args.fn(args)
    except (accounts.ValidationError, FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

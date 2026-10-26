# Self-Hosted MT5 Trade Copier

A 24/7 Windows 10/11 self-hosted MetaTrader 5 trade copier (**1 Master account → up to 10 Slave accounts**) with a FastAPI web dashboard, per-account isolated `/portable` MT5 terminals, per-slave symbol & allowed-lot rules, automatic reconciliation, and laptop power/network resilience.

---

## Architecture Highlights

- **One Worker Process per Account (`copier.worker`)**: Because the official `MetaTrader5` Python API binds to one terminal per OS process, each account runs in its own supervised worker process with its own portable MT5 installation (`%ProgramData%\TradeCopier\terminals\acc_<id>\terminal64.exe /portable`). Accounts from different brokers and servers run side by side without interference.
- **Platform-Neutral Broker Adapter (`copier.adapters`)**: All broker calls go through `BrokerAdapter` (`MT5Adapter` for production, `FakeAdapter` for fast deterministic unit/integration tests).
- **Sub-Second Copy Engine (`copier.engine`)**:
  - Master worker polls positions and pending orders every `50 ms`, diffs snapshots, and broadcasts events over the localhost IPC bus (`copier.bus`).
  - Slave workers execute in parallel, supporting market opens, closes, partial closes, SL/TP modifications, and pending orders.
  - Write-ahead `ticket_map` persistence + magic-number tagging (`700000000000 + master_ticket`) guarantee idempotency across crashes (no duplicate opens, no missed closes).
- **Security**:
  - Broker passwords and Telegram tokens are encrypted at rest with **Fernet** (`cryptography`).
  - The Fernet key is stored outside the SQLite database in `%ProgramData%\TradeCopier\secrets\fernet.key.dpapi` and protected with **Windows DPAPI** (machine scope).
  - Structured JSON logs (`%ProgramData%\TradeCopier\logs\*.jsonl`) pass through a redacting filter that scrubs passwords and tokens.
- **24/7 Laptop Resilience (`setup.ps1` + `copier.supervisor`)**:
  - Configures Windows power settings to never sleep on AC/DC and disables lid-close sleep.
  - Registers a Windows Scheduled Task (`TradeCopier`) to auto-start on boot/logon and auto-restart on crash.
  - Monitors internet connectivity: pauses copying when offline, queues Telegram alerts in a persistent outbox, flushes alerts on recovery, and reconciles all slaves against the master.
  - Pings an external heartbeat URL (e.g. `healthchecks.io`) every 60 seconds so you get alerted if the laptop loses power or goes offline.

---

## Quick Start (One-Time Setup on Windows 10/11)

1. **Install at least one broker's MetaTrader 5 terminal** normally (e.g., into `C:\Program Files\<Broker> MetaTrader 5`) so you have a template folder containing `terminal64.exe`.
2. **Run `setup.ps1` in an Administrator PowerShell prompt**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
   The script will:
   - Create `.venv` and install dependencies from `requirements.txt`
   - Initialize `%ProgramData%\TradeCopier\`, generate the DPAPI-protected Fernet key, and create the SQLite WAL database (`copier.db`)
   - Prompt you to set the **Dashboard Admin Password**
   - Optionally register your first broker MT5 folder template
   - Harden the laptop power plan (never sleep, lid close = Do nothing)
   - Register and start the `TradeCopier` Scheduled Task
3. **Open the Web Dashboard**:
   Navigate to `http://127.0.0.1:8000` and sign in with your admin password.

---

## Using the Dashboard

1. **Accounts (`/accounts`)**:
   - Under **Broker Terminal Templates**, register each broker's installed MT5 folder once.
   - Under **Add Account**, select role (`Master` or `Slave`), broker template, server name, login, and password. The app automatically provisions a dedicated `/portable` terminal folder for that account.
   - Click **Connect** to launch the account's terminal, verify login, and view account name, server, balance, and leverage (or the exact MT5 error code/reason on failure).
2. **Slave Settings (`/slaves`)**:
   - Configure per-slave pause switch, reverse direction (`BUY ↔ SELL` + `SL ↔ TP` swap), default lot mode (`Multiplier`, `Fixed Lot`, `% of Balance`, `Risk %`), min/max lot, max open trades, symbol whitelist/blacklist, and the optional *Close Orphaned Trades* toggle (off by default).
   - Add **Per-Symbol Rules** including an explicit **Allowed Lot Sizes** list (e.g., `0.01, 0.05, 0.10`) and choose whether out-of-list lots **snap to the nearest allowed value** or **skip the trade**.
   - Add explicit **Symbol Mappings** (e.g., `XAUUSD → GOLD`), while common suffixes (`.m`, `.pro`, `-ECN`, `_i`) and common aliases are auto-detected.
   - Every save increments `config_version` and broadcasts `config_changed` so workers hot-reload rules immediately without restarting.
3. **Live Trades (`/trades`)**:
   - View active `Master ↔ Slave` ticket maps, end-to-end execution latency in milliseconds, and trigger manual reconciliation at any time.
4. **Logs (`/logs`) & System (`/system`)**:
   - Filter structured events by account or severity, configure Telegram Bot alerts, set your external heartbeat URL, or toggle global pause.
5. **Health Check Endpoint (`/health`)**:
   - Returns HTTP `200` (or `503` when offline or an enabled account is in error state) with JSON status per component.

---

## CLI & Running Tests

You can also manage accounts from the command line or run the automated test suite:

```powershell
# Run the full unit & integration test suite (47 tests)
py -3.14 -m pytest -q

# CLI commands
py -3.14 -m copier init
py -3.14 -m copier template add ICMarkets "C:\Program Files\ICMarkets MetaTrader 5"
py -3.14 -m copier account add --role master --broker ICMarkets --server ICMarketsSC-Demo --login 12345678
py -3.14 -m copier account connect 1
py -3.14 -m copier account list

# Start the supervisor manually (starts IPC hub + web UI + account workers)
py -3.14 -m copier.supervisor --port 8000
```

See [docs/DEMO_TEST_PLAN.md](docs/DEMO_TEST_PLAN.md) for the step-by-step demo verification checklist.

# MT5 Demo-Account Test Plan

Use at least **3 MetaTrader 5 demo accounts** (1 Master + 2 Slaves, ideally across different brokers or symbol conventions) before enabling any live account.

---

## A. Provisioning & Connection Verification

1. **Register a Broker Template**
   - In the dashboard (**Accounts** page → *Broker Terminal Templates*), add your broker's installed MT5 folder (e.g., `C:\Program Files\MetaTrader 5`).
   - Verify that pointing to an installer folder without `terminal64.exe` shows a clear error message.
2. **Add Master & Slave Accounts**
   - Add 1 `master` account and 2 `slave` accounts.
   - Verify each account creates an isolated folder under `%ProgramData%\TradeCopier\terminals\acc_<id>\` with `config\common.ini` (`AllowLiveTrading=1`, `Enabled=1`) and without copying `accounts.dat`.
3. **Test "Connect" Button**
   - Click **Connect** on an account with valid credentials → verify the banner shows:
     `Connected: <Account Name> · <Server> · Balance <Balance> <Currency> · Leverage 1:<Leverage>`.
   - Enter an intentionally wrong password on a second test account and click **Connect** → verify the banner displays the exact MT5 reason (`Authorization failed (code -6)`), and confirm the plaintext password never appears in `%ProgramData%\TradeCopier\logs\*.jsonl` or `copier.db`.

---

## B. Market Orders, Partial Closes & SL/TP Sync

1. **Market Open**
   - Open a `0.10` lot `BUY` on the Master account.
   - Verify both slaves open their corresponding positions within `< 1000 ms` (check the **Latency** column on the **Live Trades** page).
   - Check the slave terminal's *Trade* tab: verify the order comment is `TC|<master_ticket>` and the magic number is `700000000000 + <master_ticket>`.
2. **SL / TP Modification**
   - Set or drag `SL` and `TP` on the Master position.
   - Verify both slaves update their `SL` and `TP` within 1 second.
3. **Partial Close**
   - Partially close `50%` (`0.05` lots) of the Master position.
   - Verify each slave closes `50%` of its own mapped lot size and keeps the remaining `50%` open.
4. **Full Close**
   - Close the remaining Master position.
   - Verify all slaves close their mapped positions and the ticket map state transitions to `closed`.

---

## C. Per-Slave Lot Rules, Allowed Lot Sizes & Reverse Mode

1. **Allowed Lot Sizes (`Snap` vs `Skip`)**
   - On **Slave Settings**, add a per-symbol rule for `XAUUSD`:
     - Allowed Lot Sizes: `0.01, 0.05, 0.10`
     - Action: `Snap to Nearest Allowed`
   - Open `0.04` lots of `XAUUSD` on Master → verify Slave opens `0.05` lots.
   - Change Action to `Skip Trade` (hot-reloaded without restart) and open `0.03` lots on Master → verify Slave skips the trade and records `lot 0.03 not in allowed list` in the Trade Audit table.
2. **Reverse Direction**
   - Enable **Reverse (BUY ↔ SELL, swap SL/TP)** on Slave #2.
   - Open a `BUY` on Master with `SL` below price and `TP` above price → verify Slave #2 opens a `SELL` with `SL` above price and `TP` below price.
3. **Per-Slave Pause Switch**
   - Set Slave #1 to **Paused** and open a trade on Master → verify Slave #2 copies it while Slave #1 records `skipped (slave paused)`.

---

## D. Pending Orders Lifecycle

1. Place a `Buy Limit` on Master → verify Slaves place a matching `Buy Limit` (or `Sell Limit` if reversed).
2. Modify the pending entry price and `SL`/`TP` on Master → verify Slaves modify their pending order.
3. Cancel the pending order on Master → verify Slaves cancel their pending order.
4. Place a pending order close to market and let it trigger on Master (`PENDING_FILLED`) → verify Slaves either link their already-triggered position or cancel their unfilled pending order and open at market without creating duplicate positions.

---

## E. Symbol Mapping & Suffix Auto-Detection

1. Test a slave broker that uses suffixes (e.g., `EURUSD.m` or `EURUSD.pro`) without explicit mapping → verify automatic suffix resolution maps `EURUSD → EURUSD.m`.
2. Test `XAUUSD` on Master against a slave broker that names gold `GOLD` → verify automatic alias resolution or explicit mapping on the **Slave Settings** page.

---

## F. Laptop Resilience, Internet Loss & Crash Recovery

1. **Kill a Slave Terminal Process**
   - Terminate `terminal64.exe` for Slave #1 in Task Manager.
   - Verify the worker watchdog detects the dead terminal within seconds, relaunches it, logs back in, and reconciles open positions.
2. **Simulated Wi-Fi / Internet Drop**
   - Open a position on Master (copied to Slaves).
   - Disconnect Wi-Fi on the laptop for ~60 seconds.
   - Verify the **System** page and `/health` endpoint report `internet_online: false` and `global_paused: true`.
   - From another device (e.g. mobile MT5 app), close the Master position while the laptop is offline.
   - Reconnect Wi-Fi on the laptop:
     - Verify the supervisor detects internet recovery, flushes the queued Telegram alerts, and triggers reconciliation.
     - Verify the Slave position that was closed on Master during the outage is automatically closed (`closed_missed`).
3. **Orphan Detection**
   - Open a manual trade on a Slave account using a copier magic number (or run `scripts/spike_phase1.py --trade`), with `Close Orphaned Trades` set to **Off (Default)**.
   - Trigger **Reconcile All Slaves Now** on the **Live Trades** page → verify the orphan is flagged in logs and Telegram alerts, and is **not** closed unless `Close Orphaned Trades` is enabled.

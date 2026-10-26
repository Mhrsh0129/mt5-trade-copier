"""Slave trade execution engine (architecture §5).

Processes :class:`TradeEvent` records against a :class:`BrokerAdapter` for one
slave account. Handles:
- Per-slave pause switch and global pause
- Symbol filter (whitelist/blacklist/per-symbol enabled) & symbol mapping
- Max open trades limit
- Lot calculation + allowed-lots snap/skip + broker step/min/max validation
- Free-margin safety check before sending market orders
- Broker stop-level validation (if SL/TP violates stops_level, opens without stops and modifies after)
- Reverse direction (flips BUY<->SELL, swaps SL<->TP, flips pending order types)
- Hedging vs Netting slave accounts
- Retries (up to 3 attempts, ~100 ms apart) on retryable retcodes
- Write-ahead ``ticket_map`` persistence + magic-number tagging
- ``trade_audit`` recording with end-to-end latency in milliseconds
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ..adapters.base import (
    BrokerAdapter,
    ExecCode,
    ExecResult,
    MarginMode,
    OpenRequest,
    PendingRequest,
    PendingType,
    Side,
    SymbolSpec,
)
from ..db import TradeAudit, session_scope
from .events import EventType, TradeEvent
from .lots import compute_open_lot, compute_partial_close_lot, snap_to_step
from .rules import SlaveConfig, load_slave_config
from .symbols import resolve_symbol
from .ticket_map import (
    begin_send,
     encode_magic,
    format_comment,
    get_ticket_map,
    is_copier_magic,
    list_open_maps,
    mark_closed_or_failed,
    mark_open,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_S = 0.08
MARGIN_SAFETY_FACTOR = 0.95


class SlaveExecutor:
    def __init__(
        self,
        slave_id: int,
        adapter: BrokerAdapter,
        *,
        config: SlaveConfig | None = None,
        alert_cb: Callable[[str, str, str], None] | None = None,
        retry_delay_s: float = RETRY_DELAY_S,
    ) -> None:
        self.slave_id = slave_id
        self.adapter = adapter
        self.config = config or load_slave_config(slave_id)
        self.global_paused = False
        self.alert_cb = alert_cb
        self.retry_delay_s = retry_delay_s
        self._slave_specs: dict[str, SymbolSpec] | None = None

    def reload_config(self) -> None:
        self.config = load_slave_config(self.slave_id)

    def _specs_dict(self) -> dict[str, SymbolSpec]:
        if self._slave_specs is None:
            self._slave_specs = {s.name: s for s in self.adapter.symbols()}
        return self._slave_specs

    def _alert(self, dedupe_key: str, text: str, level: str = "warn") -> None:
        if self.alert_cb is not None:
            try:
                self.alert_cb(dedupe_key, text, level)
            except Exception:  # noqa: BLE001
                pass

    def _record_audit(
        self,
        ev: TradeEvent,
        *,
        slave_symbol: str | None,
        result: str,
        retcode: str | None = None,
        requested_lot: float | None = None,
        filled_lot: float | None = None,
        detail: str | None = None,
    ) -> TradeAudit:
        latency_ms = max(0, int(time.time() * 1000) - int(ev.detected_at_ms))
        row = TradeAudit(
            slave_id=self.slave_id,
            event_type=ev.event_type.value,
            event_seq=ev.seq,
            master_position_id=ev.master_position_id or ev.master_order_id,
            master_symbol=ev.symbol,
            slave_symbol=slave_symbol,
            result=result,
            retcode=retcode,
            master_lot=ev.volume,
            requested_lot=requested_lot,
            filled_lot=filled_lot,
            latency_ms=latency_ms,
            detail=detail,
        )
        with session_scope() as s:
            s.add(row)
            s.flush()
        return row

    def _with_retries(self, fn: Callable[[], ExecResult]) -> ExecResult:
        res = fn()
        attempts = 1
        while not res.ok and res.code.retryable and attempts < MAX_RETRIES:
            if self.retry_delay_s > 0:
                time.sleep(self.retry_delay_s)
            res = fn()
            attempts += 1
        return res

    # ================================================================ entry point
    def handle_event(self, ev: TradeEvent) -> TradeAudit | None:
        if self.global_paused:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="global pause (offline)")
        if self.config.paused:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="slave paused")

        et = ev.event_type
        if et in (EventType.POSITION_OPENED, EventType.PENDING_FILLED):
            return self._on_position_opened(ev)
        if et is EventType.POSITION_CLOSED:
            return self._on_position_closed(ev)
        if et is EventType.POSITION_PARTIAL_CLOSE:
            return self._on_partial_close(ev)
        if et is EventType.POSITION_INCREASED:
            return self._on_position_increased(ev)
        if et is EventType.POSITION_SLTP_MODIFIED:
            return self._on_sltp_modified(ev)
        if et is EventType.PENDING_PLACED:
            return self._on_pending_placed(ev)
        if et is EventType.PENDING_MODIFIED:
            return self._on_pending_modified(ev)
        if et is EventType.PENDING_DELETED:
            return self._on_pending_deleted(ev)
        return None

    # ================================================================ helpers
    def _resolve_and_check_symbol(self, master_symbol: str) -> tuple[SymbolSpec | None, str | None]:
        allowed, reason = self.config.check_symbol_allowed(master_symbol)
        if not allowed:
            return None, reason
        res = resolve_symbol(
            master_symbol,
            explicit_map=self.config.symbol_map,
            slave_symbols=self._specs_dict(),
        )
        if res.slave_symbol is None:
            return None, f"unmapped symbol {master_symbol}"
        spec = self.adapter.symbol_spec(res.slave_symbol)
        if spec is None:
            return None, f"slave broker has no symbol {res.slave_symbol}"
        if not spec.trade_allowed:
            return None, f"trading disabled for {res.slave_symbol} on slave broker"
        return spec, None

    def _effective_side_and_stops(self, side_str: str, sl: float, tp: float) -> tuple[Side, float, float]:
        side = Side(side_str)
        if self.config.reverse:
            # Reversing direction flips BUY<->SELL and swaps SL<->TP prices
            return side.flipped(), (tp if self.config.copy_sltp else 0.0), (sl if self.config.copy_sltp else 0.0)
        if not self.config.copy_sltp:
            return side, 0.0, 0.0
        return side, sl, tp

    def _stops_valid(self, spec: SymbolSpec, side: Side, price: float, sl: float, tp: float) -> bool:
        min_dist = max(spec.stops_level, spec.freeze_level) * spec.point
        if min_dist <= 0:
            return True
        if side is Side.BUY:
            if sl > 0 and (price - sl) < min_dist - 1e-9:
                return False
            if tp > 0 and (tp - price) < min_dist - 1e-9:
                return False
        else:
            if sl > 0 and (sl - price) < min_dist - 1e-9:
                return False
            if tp > 0 and (price - tp) < min_dist - 1e-9:
                return False
        return True

    # ================================================================ market open
    def _on_position_opened(self, ev: TradeEvent) -> TradeAudit:
        master_id = int(ev.master_position_id or 0)

        # If this position came from a pending order we already copied, link or cancel the slave pending
        if ev.event_type is EventType.PENDING_FILLED and ev.master_order_id:
            pend_map = get_ticket_map(self.slave_id, int(ev.master_order_id), kind="pending")
            if pend_map and pend_map.state == "open" and pend_map.slave_order_id:
                # Check if slave pending already triggered on its own
                existing_pos = next(
                    (p for p in self.adapter.positions() if p.position_id == pend_map.slave_order_id
                     or decode_matches_pending(p.magic, ev.master_order_id)),
                    None,
                )
                if existing_pos is not None:
                    mark_closed_or_failed(self.slave_id, int(ev.master_order_id), kind="pending", state="closed")
                    mark_open(
                        self.slave_id,
                        master_id,
                        kind="position",
                        slave_position_id=existing_pos.position_id,
                        slave_volume=existing_pos.volume,
                        master_volume=ev.volume,
                        sl=existing_pos.sl,
                        tp=existing_pos.tp,
                    )
                    return self._record_audit(
                        ev,
                        slave_symbol=existing_pos.symbol,
                        result="ok",
                        retcode="PENDING_TRIGGERED",
                        requested_lot=existing_pos.volume,
                        filled_lot=existing_pos.volume,
                        detail="slave pending already filled",
                    )
                # Otherwise cancel the unfilled slave pending and proceed with market open
                self.adapter.cancel_pending(pend_map.slave_order_id)
                mark_closed_or_failed(self.slave_id, int(ev.master_order_id), kind="pending", state="closed")

        # Idempotency check: never duplicate an already-mapped master position
        existing_map = get_ticket_map(self.slave_id, master_id, kind="position")
        if existing_map and existing_map.state == "open":
            return self._record_audit(
                ev,
                slave_symbol=existing_map.slave_symbol,
                result="skipped",
                detail=f"master position {master_id} already mapped to slave {existing_map.slave_position_id}",
            )

        spec, err = self._resolve_and_check_symbol(ev.symbol)
        if spec is None:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail=err)

        # Max open trades check (count copier positions on this slave)
        if self.config.max_open_trades is not None and self.config.max_open_trades > 0:
            open_count = len(list_open_maps(self.slave_id, kind="position"))
            if open_count >= self.config.max_open_trades:
                return self._record_audit(
                    ev,
                    slave_symbol=spec.name,
                    result="skipped",
                    detail=f"max_open_trades ({self.config.max_open_trades}) reached",
                )

        info = self.adapter.account_info()
        lot_mode, lot_val, allowed_lots, not_allowed_action = self.config.effective_lot_params(ev.symbol)
        sl_points = 0.0
        tick_val_per_pt = 0.0
        if ev.sl > 0 and ev.price > 0 and spec.point > 0:
            sl_points = abs(ev.price - ev.sl) / spec.point
            # Standard forex/CFD approximation when tick_value isn't separately queried: contract_size * point
            tick_val_per_pt = spec.contract_size * spec.point
        lot_res = compute_open_lot(
            ev.volume,
            lot_mode=lot_mode,
            lot_value=lot_val,
            master_balance=ev.master_balance,
            slave_balance=info.balance,
            sl_points=sl_points,
            tick_value_per_lot_per_point=tick_val_per_pt,
            min_lot=self.config.min_lot,
            max_lot=self.config.max_lot,
            allowed_lots=allowed_lots,
            not_allowed_action=not_allowed_action,
            broker_min=spec.volume_min,
            broker_max=spec.volume_max,
            broker_step=spec.volume_step,
        )
        if not lot_res.ok:
            return self._record_audit(ev, slave_symbol=spec.name, result="skipped", detail=lot_res.reason)

        volume = lot_res.volume
        side, sl, tp = self._effective_side_and_stops(ev.side, ev.sl, ev.tp)

        # Free margin check
        tick = self.adapter.tick(spec.name)
        ref_price = (tick.ask if side is Side.BUY else tick.bid) if tick else ev.price
        if ref_price > 0:
            req_margin = self.adapter.calc_margin(side, spec.name, volume, ref_price)
            if req_margin is not None and info.margin_free > 0 and req_margin > info.margin_free * MARGIN_SAFETY_FACTOR:
                msg = f"insufficient free margin: required {req_margin:.2f} > safe free {info.margin_free * MARGIN_SAFETY_FACTOR:.2f}"
                self._alert(f"margin:{self.slave_id}:{spec.name}", f"Slave #{self.slave_id}: {msg}")
                return self._record_audit(
                    ev, slave_symbol=spec.name, result="skipped", requested_lot=volume, detail=msg
                )

        # Check stop levels; if too close, open with 0 stops and apply SL/TP right after
        defer_stops = (sl > 0 or tp > 0) and ref_price > 0 and not self._stops_valid(spec, side, ref_price, sl, tp)
        send_sl = 0.0 if defer_stops else sl
        send_tp = 0.0 if defer_stops else tp

        magic = encode_magic(master_id)
        comment = format_comment(master_id)

        # Write-ahead reservation before sending to broker
        begin_send(
            self.slave_id,
            master_id,
            kind="position",
            slave_symbol=spec.name,
            master_volume=ev.volume,
            slave_volume=volume,
            sl=sl,
            tp=tp,
        )

        open_req = OpenRequest(
            symbol=spec.name,
            side=side,
            volume=volume,
            sl=send_sl,
            tp=send_tp,
            deviation=self.config.deviation_points,
            magic=magic,
            comment=comment,
        )
        exec_res = self._with_retries(lambda: self.adapter.market_open(open_req))
        if not exec_res.ok:
            mark_closed_or_failed(self.slave_id, master_id, kind="position", state="failed")
            self._alert(
                f"open_fail:{self.slave_id}:{master_id}",
                f"Slave #{self.slave_id} failed to copy open {spec.name} {volume}: {exec_res.code.value} ({exec_res.message})",
            )
            return self._record_audit(
                ev,
                slave_symbol=spec.name,
                result="failed",
                retcode=exec_res.code.value,
                requested_lot=volume,
                detail=exec_res.message,
            )

        if defer_stops and exec_res.position_id:
            self.adapter.modify_position(exec_res.position_id, sl, tp)

        filled = exec_res.volume or volume
        mark_open(
            self.slave_id,
            master_id,
            kind="position",
            slave_position_id=exec_res.position_id,
            slave_order_id=exec_res.order_id,
            slave_volume=filled,
            master_volume=ev.volume,
            sl=sl,
            tp=tp,
        )
        return self._record_audit(
            ev,
            slave_symbol=spec.name,
            result="ok",
            retcode=str(exec_res.retcode or exec_res.code.value),
            requested_lot=volume,
            filled_lot=filled,
            detail=lot_res.reason if lot_res.reason != "ok" else None,
        )

    # ================================================================ market close / partial close
    def _on_position_closed(self, ev: TradeEvent) -> TradeAudit:
        master_id = int(ev.master_position_id or 0)
        tmap = get_ticket_map(self.slave_id, master_id, kind="position")
        if tmap is None or tmap.state not in ("open", "pending_send") or not tmap.slave_position_id:
            return self._record_audit(
                ev, slave_symbol=tmap.slave_symbol if tmap else None, result="skipped", detail="no open ticket map"
            )

        info = self.adapter.account_info()
        # Netting slave: close only this master position's mapped share of the net position
        close_vol = tmap.slave_volume if info.margin_mode is MarginMode.NETTING else None

        res = self._with_retries(
            lambda: self.adapter.close(
                int(tmap.slave_position_id),
                volume=close_vol,
                deviation=self.config.deviation_points,
                magic=encode_magic(master_id),
                comment=format_comment(master_id),
            )
        )
        if res.code is ExecCode.NOT_FOUND:
            # Slave's own SL/TP or manual close already closed it -> treat as success
            mark_closed_or_failed(self.slave_id, master_id, kind="position", state="closed")
            return self._record_audit(
                ev,
                slave_symbol=tmap.slave_symbol,
                result="already_closed",
                retcode=res.code.value,
                requested_lot=tmap.slave_volume,
                filled_lot=0.0,
                detail="slave position already closed",
            )
        if not res.ok:
            self._alert(
                f"close_fail:{self.slave_id}:{master_id}",
                f"Slave #{self.slave_id} failed to close position {tmap.slave_position_id}: {res.code.value}",
            )
            return self._record_audit(
                ev,
                slave_symbol=tmap.slave_symbol,
                result="failed",
                retcode=res.code.value,
                requested_lot=tmap.slave_volume,
                detail=res.message,
            )

        mark_closed_or_failed(self.slave_id, master_id, kind="position", state="closed")
        return self._record_audit(
            ev,
            slave_symbol=tmap.slave_symbol,
            result="ok",
            retcode=str(res.retcode or res.code.value),
            requested_lot=tmap.slave_volume,
            filled_lot=res.volume or tmap.slave_volume,
        )

    def _on_partial_close(self, ev: TradeEvent) -> TradeAudit:
        master_id = int(ev.master_position_id or 0)
        tmap = get_ticket_map(self.slave_id, master_id, kind="position")
        if tmap is None or tmap.state != "open" or not tmap.slave_position_id or not tmap.slave_volume:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="no open ticket map")

        spec = self.adapter.symbol_spec(tmap.slave_symbol or ev.symbol)
        b_min = spec.volume_min if spec else 0.01
        b_step = spec.volume_step if spec else 0.01
        ratio = ev.close_ratio if ev.close_ratio is not None else 0.5
        close_vol = compute_partial_close_lot(
            tmap.slave_volume, ratio, broker_min=b_min, broker_step=b_step
        )

        res = self._with_retries(
            lambda: self.adapter.close(
                int(tmap.slave_position_id),
                volume=close_vol,
                deviation=self.config.deviation_points,
                magic=encode_magic(master_id),
                comment=format_comment(master_id),
            )
        )
        if res.code is ExecCode.NOT_FOUND:
            mark_closed_or_failed(self.slave_id, master_id, kind="position", state="closed")
            return self._record_audit(ev, slave_symbol=tmap.slave_symbol, result="already_closed")
        if not res.ok:
            return self._record_audit(
                ev,
                slave_symbol=tmap.slave_symbol,
                result="failed",
                retcode=res.code.value,
                requested_lot=close_vol,
                detail=res.message,
            )

        remaining = round(max(0.0, tmap.slave_volume - close_vol), 8)
        if remaining < b_min - 1e-9:
            mark_closed_or_failed(self.slave_id, master_id, kind="position", state="closed")
        else:
            # Find updated position id (some MT5 hedging servers issue a new ticket on partial close;
            # FakeAdapter and most MT5 servers keep the same ticket)
            pos_id = tmap.slave_position_id
            live_by_magic = next(
                (p for p in self.adapter.positions() if p.magic == encode_magic(master_id)), None
            )
            if live_by_magic is not None:
                pos_id = live_by_magic.position_id
            mark_open(
                self.slave_id,
                master_id,
                kind="position",
                slave_position_id=pos_id,
                slave_volume=remaining,
                master_volume=ev.volume,
            )
        return self._record_audit(
            ev,
            slave_symbol=tmap.slave_symbol,
            result="ok",
            retcode=str(res.retcode or res.code.value),
            requested_lot=close_vol,
            filled_lot=res.volume or close_vol,
        )

    def _on_position_increased(self, ev: TradeEvent) -> TradeAudit:
        """Netting master added volume to an existing position."""
        master_id = int(ev.master_position_id or 0)
        tmap = get_ticket_map(self.slave_id, master_id, kind="position")
        if tmap is None or tmap.state != "open" or not tmap.slave_symbol:
            return self._on_position_opened(ev)
        spec = self.adapter.symbol_spec(tmap.slave_symbol)
        if spec is None:
            return self._record_audit(ev, slave_symbol=tmap.slave_symbol, result="skipped", detail="missing spec")
        info = self.adapter.account_info()
        lot_mode, lot_val, allowed_lots, not_allowed_action = self.config.effective_lot_params(ev.symbol)
        sl_points = 0.0
        tick_val_per_pt = 0.0
        if ev.sl > 0 and ev.price > 0 and spec.point > 0:
            sl_points = abs(ev.price - ev.sl) / spec.point
            # Standard forex/CFD approximation when tick_value isn't separately queried: contract_size * point
            tick_val_per_pt = spec.contract_size * spec.point
        lot_res = compute_open_lot(
            ev.volume,
            lot_mode=lot_mode,
            lot_value=lot_val,
            master_balance=ev.master_balance,
            slave_balance=info.balance,
            sl_points=sl_points,
            tick_value_per_lot_per_point=tick_val_per_pt,
            min_lot=self.config.min_lot,
            max_lot=self.config.max_lot,
            allowed_lots=allowed_lots,
            not_allowed_action=not_allowed_action,
            broker_min=spec.volume_min,
            broker_max=spec.volume_max,
            broker_step=spec.volume_step,
        )
        if not lot_res.ok:
            return self._record_audit(ev, slave_symbol=spec.name, result="skipped", detail=lot_res.reason)
        side, sl, tp = self._effective_side_and_stops(ev.side, ev.sl, ev.tp)
        req = OpenRequest(
            symbol=spec.name,
            side=side,
            volume=lot_res.volume,
            sl=sl,
            tp=tp,
            deviation=self.config.deviation_points,
            magic=encode_magic(master_id),
            comment=format_comment(master_id),
            position_id=tmap.slave_position_id if info.margin_mode is MarginMode.NETTING else None,
        )
        res = self._with_retries(lambda: self.adapter.market_open(req))
        if not res.ok:
            return self._record_audit(
                ev, slave_symbol=spec.name, result="failed", retcode=res.code.value, detail=res.message
            )
        new_slave_vol = round((tmap.slave_volume or 0.0) + (res.volume or lot_res.volume), 8)
        mark_open(
            self.slave_id,
            master_id,
            kind="position",
            slave_position_id=res.position_id or tmap.slave_position_id,
            slave_volume=new_slave_vol,
            master_volume=(ev.prev_volume or 0.0) + ev.volume,
        )
        return self._record_audit(
            ev,
            slave_symbol=spec.name,
            result="ok",
            retcode=str(res.retcode or res.code.value),
            requested_lot=lot_res.volume,
            filled_lot=res.volume or lot_res.volume,
        )

    # ================================================================ SL/TP modify
    def _on_sltp_modified(self, ev: TradeEvent) -> TradeAudit:
        if not self.config.copy_sltp:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="copy_sltp disabled")
        master_id = int(ev.master_position_id or 0)
        tmap = get_ticket_map(self.slave_id, master_id, kind="position")
        if tmap is None or tmap.state != "open" or not tmap.slave_position_id:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="no open ticket map")

        _, sl, tp = self._effective_side_and_stops(ev.side, ev.sl, ev.tp)
        res = self._with_retries(lambda: self.adapter.modify_position(int(tmap.slave_position_id), sl, tp))
        if not res.ok:
            return self._record_audit(
                ev, slave_symbol=tmap.slave_symbol, result="failed", retcode=res.code.value, detail=res.message
            )
        mark_open(self.slave_id, master_id, kind="position", sl=sl, tp=tp)
        return self._record_audit(
            ev, slave_symbol=tmap.slave_symbol, result="ok", retcode=str(res.retcode or res.code.value)
        )

    # ================================================================ pending orders
    def _on_pending_placed(self, ev: TradeEvent) -> TradeAudit:
        if not self.config.copy_pending:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="copy_pending disabled")
        master_oid = int(ev.master_order_id or 0)
        existing = get_ticket_map(self.slave_id, master_oid, kind="pending")
        if existing and existing.state == "open":
            return self._record_audit(ev, slave_symbol=existing.slave_symbol, result="skipped", detail="already mapped")

        spec, err = self._resolve_and_check_symbol(ev.symbol)
        if spec is None:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail=err)

        info = self.adapter.account_info()
        lot_mode, lot_val, allowed_lots, not_allowed_action = self.config.effective_lot_params(ev.symbol)
        sl_points = 0.0
        tick_val_per_pt = 0.0
        if ev.sl > 0 and ev.price > 0 and spec.point > 0:
            sl_points = abs(ev.price - ev.sl) / spec.point
            # Standard forex/CFD approximation when tick_value isn't separately queried: contract_size * point
            tick_val_per_pt = spec.contract_size * spec.point
        lot_res = compute_open_lot(
            ev.volume,
            lot_mode=lot_mode,
            lot_value=lot_val,
            master_balance=ev.master_balance,
            slave_balance=info.balance,
            sl_points=sl_points,
            tick_value_per_lot_per_point=tick_val_per_pt,
            min_lot=self.config.min_lot,
            max_lot=self.config.max_lot,
            allowed_lots=allowed_lots,
            not_allowed_action=not_allowed_action,
            broker_min=spec.volume_min,
            broker_max=spec.volume_max,
            broker_step=spec.volume_step,
        )
        if not lot_res.ok:
            return self._record_audit(ev, slave_symbol=spec.name, result="skipped", detail=lot_res.reason)

        ptype = PendingType(ev.pending_type) if ev.pending_type else PendingType.BUY_LIMIT
        if self.config.reverse:
            ptype = ptype.flipped()
        _, sl, tp = self._effective_side_and_stops(ev.side, ev.sl, ev.tp)

        begin_send(
            self.slave_id,
            master_oid,
            kind="pending",
            slave_symbol=spec.name,
            master_volume=ev.volume,
            slave_volume=lot_res.volume,
            sl=sl,
            tp=tp,
            master_order_id=master_oid,
        )
        req = PendingRequest(
            symbol=spec.name,
            type=ptype,
            volume=lot_res.volume,
            price=ev.price,
            stop_limit=ev.stop_limit,
            sl=sl,
            tp=tp,
            expiration=ev.expiration,
            magic=encode_magic(master_oid),
            comment=format_comment(master_oid),
        )
        res = self._with_retries(lambda: self.adapter.place_pending(req))
        if not res.ok:
            mark_closed_or_failed(self.slave_id, master_oid, kind="pending", state="failed")
            return self._record_audit(
                ev,
                slave_symbol=spec.name,
                result="failed",
                retcode=res.code.value,
                requested_lot=lot_res.volume,
                detail=res.message,
            )
        mark_open(
            self.slave_id,
            master_oid,
            kind="pending",
            slave_order_id=res.order_id,
            slave_volume=lot_res.volume,
            master_volume=ev.volume,
            sl=sl,
            tp=tp,
        )
        return self._record_audit(
            ev,
            slave_symbol=spec.name,
            result="ok",
            retcode=str(res.retcode or res.code.value),
            requested_lot=lot_res.volume,
            filled_lot=lot_res.volume,
        )

    def _on_pending_modified(self, ev: TradeEvent) -> TradeAudit:
        master_oid = int(ev.master_order_id or 0)
        tmap = get_ticket_map(self.slave_id, master_oid, kind="pending")
        if tmap is None or tmap.state != "open" or not tmap.slave_order_id:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="no open pending map")
        _, sl, tp = self._effective_side_and_stops(ev.side, ev.sl, ev.tp)
        res = self._with_retries(
            lambda: self.adapter.modify_pending(
                int(tmap.slave_order_id), ev.price, sl, tp, stop_limit=ev.stop_limit, expiration=ev.expiration
            )
        )
        if not res.ok:
            return self._record_audit(
                ev, slave_symbol=tmap.slave_symbol, result="failed", retcode=res.code.value, detail=res.message
            )
        mark_open(self.slave_id, master_oid, kind="pending", sl=sl, tp=tp)
        return self._record_audit(ev, slave_symbol=tmap.slave_symbol, result="ok")

    def _on_pending_deleted(self, ev: TradeEvent) -> TradeAudit:
        master_oid = int(ev.master_order_id or 0)
        tmap = get_ticket_map(self.slave_id, master_oid, kind="pending")
        if tmap is None or tmap.state != "open" or not tmap.slave_order_id:
            return self._record_audit(ev, slave_symbol=None, result="skipped", detail="no open pending map")
        res = self._with_retries(lambda: self.adapter.cancel_pending(int(tmap.slave_order_id)))
        mark_closed_or_failed(self.slave_id, master_oid, kind="pending", state="closed")
        if res.code is ExecCode.NOT_FOUND:
            return self._record_audit(ev, slave_symbol=tmap.slave_symbol, result="already_closed")
        return self._record_audit(
            ev,
            slave_symbol=tmap.slave_symbol,
            result="ok" if res.ok else "failed",
            retcode=res.code.value,
            detail=res.message if not res.ok else None,
        )


def decode_matches_pending(magic: int | None, master_order_id: int) -> bool:
    return bool(is_copier_magic(magic) and (magic == encode_magic(master_order_id)))

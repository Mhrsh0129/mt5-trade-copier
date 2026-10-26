"""SQLAlchemy ORM models. Full schema from the architecture doc (§7).

Phase 1 uses accounts / terminal_templates / admin_user / app_settings; the
other tables are created now so later phases need no destructive migration.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String,
                        Text, UniqueConstraint, BigInteger)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, default="admin")
    password_hash: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppSetting(Base):
    """Key/value settings. Secret values are stored Fernet-encrypted in ``value_enc``."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class TerminalTemplate(Base):
    """An installed broker MT5 folder that gets copied per account."""
    __tablename__ = "terminal_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker: Mapped[str] = mapped_column(String(128), unique=True)
    source_dir: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accounts: Mapped[list["Account"]] = relationship(back_populates="template")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("server", "login", name="uq_account_server_login"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(8))  # master | slave
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    broker: Mapped[str] = mapped_column(String(128))
    server: Mapped[str] = mapped_column(String(128))
    login: Mapped[int] = mapped_column(BigInteger)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("terminal_templates.id"), nullable=True)
    terminal_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Live status (written by the worker)
    status: Mapped[str] = mapped_column(String(16), default="disconnected")  # connected|disconnected|error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # hedging|netting|exchange

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    template: Mapped[TerminalTemplate | None] = relationship(back_populates="accounts")
    settings: Mapped["SlaveSettings | None"] = relationship(back_populates="account", uselist=False,
                                                            cascade="all, delete-orphan")


class SlaveSettings(Base):
    __tablename__ = "slave_settings"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    reverse: Mapped[bool] = mapped_column(Boolean, default=False)
    lot_mode: Mapped[str] = mapped_column(String(16), default="multiplier")  # fixed|multiplier|balance_ratio|risk_pct
    lot_value: Mapped[float] = mapped_column(Float, default=1.0)
    min_lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_open_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol_filter_mode: Mapped[str] = mapped_column(String(10), default="none")  # none|whitelist|blacklist
    symbol_filter: Mapped[list] = mapped_column(JSON, default=list)
    deviation_points: Mapped[int] = mapped_column(Integer, default=20)
    copy_pending: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_sltp: Mapped[bool] = mapped_column(Boolean, default=True)
    late_copy_max_age_s: Mapped[int] = mapped_column(Integer, default=300)
    late_copy_max_points: Mapped[int] = mapped_column(Integer, default=30)
    close_orphans: Mapped[bool] = mapped_column(Boolean, default=False)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    account: Mapped[Account] = relationship(back_populates="settings")


class SymbolRule(Base):
    __tablename__ = "symbol_rules"
    __table_args__ = (UniqueConstraint("account_id", "master_symbol"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    master_symbol: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lot_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None = inherit
    lot_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    allowed_lots: Mapped[list] = mapped_column(JSON, default=list)
    not_allowed_action: Mapped[str] = mapped_column(String(8), default="snap")  # snap|skip


class SymbolMap(Base):
    __tablename__ = "symbol_map"
    __table_args__ = (UniqueConstraint("account_id", "master_symbol"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    master_symbol: Mapped[str] = mapped_column(String(32))
    slave_symbol: Mapped[str] = mapped_column(String(32))
    auto_detected: Mapped[bool] = mapped_column(Boolean, default=False)


class TicketMap(Base):
    __tablename__ = "ticket_map"
    __table_args__ = (UniqueConstraint("slave_id", "master_position_id", "kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slave_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8), default="position")  # position|pending
    master_position_id: Mapped[int] = mapped_column(BigInteger)
    master_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    slave_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    slave_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    slave_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="pending_send")
    master_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    slave_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TradeAudit(Base):
    __tablename__ = "trade_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    slave_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32))
    event_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    master_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    master_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slave_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result: Mapped[str] = mapped_column(String(24))  # ok|skipped|failed|already_closed
    retcode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    master_lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertOutbox(Base):
    __tablename__ = "alert_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(8), default="warn")
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class EventLog(Base):
    __tablename__ = "events_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)

"""
Modelos — DC Dashboard

Dinero SIEMPRE en centavos enteros (BigInteger), nunca float.
Porcentajes en basis points (SmallInteger, 0-10000 = 0%-100%).

Las restricciones EXCLUDE USING gist (evitar solapamiento de fechas para
media_buyer_source_mappings y compensation_rules) se agregan como SQL crudo
en la migración de Alembic — SQLAlchemy no las modela nativamente. Aquí se
documenta la invariante que esa restricción garantiza a nivel de base de datos.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class UserRole(str, enum.Enum):
    ROYALSPACE_ADMIN = "ROYALSPACE_ADMIN"
    DIXON_MANAGER    = "DIXON_MANAGER"
    MEDIA_BUYER      = "MEDIA_BUYER"


class SettlementStatus(str, enum.Enum):
    OPEN                    = "OPEN"
    PENDING_RECONCILIATION  = "PENDING_RECONCILIATION"
    READY                   = "READY"
    APPROVED                = "APPROVED"
    PAID                    = "PAID"
    LOCKED                  = "LOCKED"


class AdjustmentType(str, enum.Enum):
    BONUS                              = "BONUS"
    CHARGEBACK                         = "CHARGEBACK"
    CORRECTION                         = "CORRECTION"
    POST_SETTLEMENT_CALLGRID_ADJUSTMENT = "POST_SETTLEMENT_CALLGRID_ADJUSTMENT"


class AdjustmentStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    VOIDED  = "VOIDED"


class SyncStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED  = "FAILED"
    PARTIAL = "PARTIAL"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)

    # Scoping: DIXON_MANAGER -> partner_id; MEDIA_BUYER -> media_buyer_id.
    # ROYALSPACE_ADMIN deja ambos en NULL (ve todo, sin scope).
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    media_buyer_id: Mapped[int | None] = mapped_column(ForeignKey("media_buyers.id"), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Partner(Base):
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)                  # "Dixon Colmenares"
    callgrid_vendor_sub_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)  # 40
    callgrid_vendor_raw_name: Mapped[str] = mapped_column(Text, nullable=False)  # "(40) Dixon Colmenares"
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    media_buyers: Mapped[list["MediaBuyer"]] = relationship(back_populates="partner")


class MediaBuyer(Base):
    __tablename__ = "media_buyers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)          # "DC1"
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    # ID numérico de la cuenta de Meta Ads (sin prefijo "act_") — si está
    # presente, el sync nocturno importa su gasto automáticamente vía
    # MetaAdSpendProvider. NULL = sigue siendo carga manual.
    meta_ad_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    partner: Mapped["Partner"] = relationship(back_populates="media_buyers")


class CallgridSource(Base):
    __tablename__ = "callgrid_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callgrid_sub_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)  # 44
    callgrid_source_raw_name: Mapped[str] = mapped_column(Text, nullable=False)         # "(44) DC1"
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MediaBuyerSourceMapping(Base):
    """
    Mapea una CallgridSource a un MediaBuyer, con vigencia por fecha.
    Invariante garantizada en Postgres (migración): una Source nunca mapea a
    dos media buyers el mismo día — EXCLUDE USING gist sobre
    (callgrid_source_id, daterange(effective_from, effective_to, '[)')).
    """
    __tablename__ = "media_buyer_source_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_buyer_id: Mapped[int] = mapped_column(ForeignKey("media_buyers.id"), nullable=False, index=True)
    callgrid_source_id: Mapped[int] = mapped_column(ForeignKey("callgrid_sources.id"), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)  # NULL = sigue activo


class DailyCallgridMetric(Base):
    """Puntero actual por source/día. call_payout_history es el ledger de auditoría append-only."""
    __tablename__ = "daily_callgrid_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callgrid_source_id: Mapped[int] = mapped_column(ForeignKey("callgrid_sources.id"), nullable=False, index=True)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ended_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    raw_response: Mapped[dict] = mapped_column(JSON, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"), nullable=True)

    __table_args__ = (UniqueConstraint("callgrid_source_id", "metric_date"),)


class CallPayoutHistory(Base):
    """Ledger append-only — cada sync escribe una fila acá, cambie o no el valor."""
    __tablename__ = "call_payout_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callgrid_source_id: Mapped[int] = mapped_column(ForeignKey("callgrid_sources.id"), nullable=False, index=True)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ended_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    raw_response: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"), nullable=True)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL = sync rutinario


class AdSpend(Base):
    """Versionado, nunca se muta en el lugar — is_current + superseded_by_id."""
    __tablename__ = "ad_spend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_buyer_id: Mapped[int] = mapped_column(ForeignKey("media_buyers.id"), nullable=False, index=True)
    spend_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    entered_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("ad_spend.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class CompensationRule(Base):
    """
    effective_from DEBE ser lunes (CHECK en Postgres vía migración +
    validación en la app) — así una sola liquidación semanal nunca cruza
    dos tasas distintas.
    """
    __tablename__ = "compensation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_buyer_id: Mapped[int] = mapped_column(ForeignKey("media_buyers.id"), nullable=False, index=True)
    mb_percentage_bps: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 3000 = 30.00%
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        CheckConstraint("mb_percentage_bps BETWEEN 0 AND 10000", name="ck_comp_rule_bps_range"),
    )


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_buyer_id: Mapped[int] = mapped_column(ForeignKey("media_buyers.id"), nullable=False, index=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)  # lunes
    period_end: Mapped[date] = mapped_column(Date, nullable=False)    # domingo
    reporting_timezone: Mapped[str] = mapped_column(Text, nullable=False, default="America/New_York")

    status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus, name="settlement_status"), nullable=False, default=SettlementStatus.OPEN
    )
    compensation_rule_id: Mapped[int | None] = mapped_column(ForeignKey("compensation_rules.id"), nullable=True)
    mb_percentage_bps_snapshot: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    gross_payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ad_spend_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    incoming_deficit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    post_settlement_adjustment_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    net_profit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mb_earnings_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    dixon_earnings_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outgoing_deficit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    computed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("media_buyer_id", "period_start", name="uq_settlement_mb_period"),
    )

    line_items: Mapped[list["SettlementLineItem"]] = relationship(back_populates="settlement")


class SettlementLineItem(Base):
    """Snapshot congelado por source/día al momento del cálculo — nunca se relee tras READY."""
    __tablename__ = "settlement_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), nullable=False, index=True)
    callgrid_source_id: Mapped[int] = mapped_column(ForeignKey("callgrid_sources.id"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)

    payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ended_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ad_spend_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    settlement: Mapped["Settlement"] = relationship(back_populates="line_items")


class FinancialAdjustment(Base):
    __tablename__ = "financial_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_buyer_id: Mapped[int] = mapped_column(ForeignKey("media_buyers.id"), nullable=False, index=True)
    adjustment_type: Mapped[AdjustmentType] = mapped_column(Enum(AdjustmentType, name="adjustment_type"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)  # signo: + credito, - debito
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    source_settlement_id: Mapped[int | None] = mapped_column(ForeignKey("settlements.id"), nullable=True)
    applied_to_settlement_id: Mapped[int | None] = mapped_column(ForeignKey("settlements.id"), nullable=True)
    status: Mapped[AdjustmentStatus] = mapped_column(
        Enum(AdjustmentStatus, name="adjustment_status"), nullable=False, default=AdjustmentStatus.PENDING
    )

    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    voided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[SyncStatus] = mapped_column(Enum(SyncStatus, name="sync_status"), nullable=False, default=SyncStatus.RUNNING)
    pivot_used: Mapped[str | None] = mapped_column(Text, nullable=True)  # "SourceName"
    date_range_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_range_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    sources_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

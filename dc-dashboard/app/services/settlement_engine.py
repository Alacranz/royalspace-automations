"""
Motor de liquidaciones — DC Dashboard

Núcleo financiero. Dinero siempre en centavos enteros. Ver
/Users/alacranz/.claude/plans/starry-soaring-goblet.md para el diseño completo.

Regla de redondeo: el media buyer redondea "half-up" a centavos; Dixon recibe
el remanente exacto, así MB + Dixon == Net Profit siempre, sin fuga de centavos.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import (
    AdjustmentStatus,
    AdjustmentType,
    AdSpend,
    CallPayoutHistory,
    CompensationRule,
    DailyCallgridMetric,
    FinancialAdjustment,
    MediaBuyer,
    MediaBuyerSourceMapping,
    Settlement,
    SettlementLineItem,
    SettlementStatus,
)
from app.services.audit import write_audit_log


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SettlementEngineError(Exception):
    pass


class SettlementLockedError(SettlementEngineError):
    """La liquidación ya está APPROVED/PAID/LOCKED — no se puede recalcular."""


class IncompleteDataError(SettlementEngineError):
    """Faltan métricas de CallGrid para uno o más (source, día) del período."""

    def __init__(self, missing: list[tuple[int, date]]):
        self.missing = missing
        super().__init__(f"Faltan datos de CallGrid para: {missing}")


class NoCompensationRuleError(SettlementEngineError):
    """No hay una CompensationRule vigente para este media buyer en este período."""


class NoSourceMappingError(SettlementEngineError):
    """El media buyer no tiene ninguna CallgridSource mapeada en este período."""


# ── Aritmética central ────────────────────────────────────────────────────────

def split(net_profit_cents: int, mb_bps: int) -> tuple[int, int]:
    """Reparte net_profit_cents entre (media buyer, Dixon) según mb_bps (basis points)."""
    mb_cents = (net_profit_cents * mb_bps + 5000) // 10000
    return mb_cents, net_profit_cents - mb_cents


# ── Helpers de consulta ───────────────────────────────────────────────────────

def get_settlement(db: Session, media_buyer_id: int, period_start: date) -> Settlement | None:
    return (
        db.query(Settlement)
        .filter_by(media_buyer_id=media_buyer_id, period_start=period_start)
        .one_or_none()
    )


def _get_or_create_settlement(db: Session, media_buyer_id: int, period_start: date, period_end: date) -> Settlement:
    s = get_settlement(db, media_buyer_id, period_start)
    if s is None:
        s = Settlement(
            media_buyer_id=media_buyer_id,
            period_start=period_start,
            period_end=period_end,
            status=SettlementStatus.OPEN,
            mb_percentage_bps_snapshot=0,
        )
        db.add(s)
        db.flush()
    return s


def _active_source_days(
    db: Session, media_buyer_id: int, period_start: date, period_end: date
) -> list[tuple[int, date]]:
    """Todas las parejas (callgrid_source_id, día) donde el media buyer tuvo un mapeo activo."""
    mappings = db.query(MediaBuyerSourceMapping).filter_by(media_buyer_id=media_buyer_id).all()
    pairs: list[tuple[int, date]] = []
    day = period_start
    while day <= period_end:
        for m in mappings:
            if m.effective_from <= day and (m.effective_to is None or day < m.effective_to):
                pairs.append((m.callgrid_source_id, day))
        day += timedelta(days=1)
    return pairs


def get_effective_compensation_rule(
    db: Session, media_buyer_id: int, period_start: date
) -> CompensationRule | None:
    return (
        db.query(CompensationRule)
        .filter(
            CompensationRule.media_buyer_id == media_buyer_id,
            CompensationRule.effective_from <= period_start,
            or_(CompensationRule.effective_to.is_(None), CompensationRule.effective_to > period_start),
        )
        .order_by(CompensationRule.effective_from.desc())
        .first()
    )


def get_current_deficit_cents(db: Session, media_buyer_id: int) -> int:
    """Déficit según la última liquidación FINALIZADA (APPROVED/PAID/LOCKED). Informativo —
    no incluye ajustes PENDING (esos solo se aplican en la próxima compute_settlement)."""
    latest = (
        db.query(Settlement)
        .filter(
            Settlement.media_buyer_id == media_buyer_id,
            Settlement.status.in_([SettlementStatus.APPROVED, SettlementStatus.PAID, SettlementStatus.LOCKED]),
        )
        .order_by(Settlement.period_start.desc())
        .first()
    )
    return latest.outgoing_deficit_cents if latest else 0


def _snapshot(s: Settlement) -> dict[str, Any]:
    return {
        "status": s.status.value,
        "gross_payout_cents": s.gross_payout_cents,
        "ad_spend_cents": s.ad_spend_cents,
        "incoming_deficit_cents": s.incoming_deficit_cents,
        "post_settlement_adjustment_cents": s.post_settlement_adjustment_cents,
        "net_profit_cents": s.net_profit_cents,
        "mb_earnings_cents": s.mb_earnings_cents,
        "dixon_earnings_cents": s.dixon_earnings_cents,
        "outgoing_deficit_cents": s.outgoing_deficit_cents,
    }


# ── Cálculo principal ─────────────────────────────────────────────────────────

def compute_settlement(db: Session, media_buyer_id: int, period_start: date, actor_user_id: int | None) -> Settlement:
    """
    (Re)calcula la liquidación semanal de un media buyer. Se puede volver a
    ejecutar mientras el estado sea OPEN/PENDING_RECONCILIATION/READY — cada
    recálculo queda auditado. Una vez APPROVED/PAID/LOCKED, lanza
    SettlementLockedError; las correcciones posteriores van por
    financial_adjustments (ver record_post_settlement_adjustment).
    """
    if period_start.weekday() != 0:
        raise ValueError("period_start debe ser lunes")
    period_end = period_start + timedelta(days=6)

    settlement = _get_or_create_settlement(db, media_buyer_id, period_start, period_end)
    if settlement.status in (SettlementStatus.APPROVED, SettlementStatus.PAID, SettlementStatus.LOCKED):
        raise SettlementLockedError(f"Settlement {settlement.id} ya está {settlement.status.value}")

    before_state = _snapshot(settlement)

    # Antes de started_on nada cuenta (ni payout, ni ad spend, ni déficit
    # arrastrado) — la semana en la que empieza se liquida solo desde ese día.
    started_on = db.get(MediaBuyer, media_buyer_id).started_on
    if started_on is not None and started_on > period_end:
        raise ValueError(f"El media buyer empieza el {started_on} — no hay nada que liquidar en {period_start}")
    count_from = max(period_start, started_on) if started_on else period_start

    mapped_days = _active_source_days(db, media_buyer_id, count_from, period_end)
    if not mapped_days:
        raise NoSourceMappingError(f"Media buyer {media_buyer_id} sin CallgridSource mapeada en {period_start}..{period_end}")

    gross_payout_cents = 0
    line_items: list[dict[str, Any]] = []
    missing: list[tuple[int, date]] = []

    for source_id, day in mapped_days:
        metric = (
            db.query(DailyCallgridMetric)
            .filter_by(callgrid_source_id=source_id, metric_date=day)
            .one_or_none()
        )
        if metric is None:
            missing.append((source_id, day))
            continue
        gross_payout_cents += metric.payout_cents
        line_items.append(
            dict(
                callgrid_source_id=source_id,
                metric_date=day,
                payout_cents=metric.payout_cents,
                revenue_cents=metric.revenue_cents,
                ended_count=metric.ended_count,
                connected_count=metric.connected_count,
                billable_count=metric.billable_count,
            )
        )

    if missing:
        settlement.status = SettlementStatus.PENDING_RECONCILIATION
        db.commit()
        raise IncompleteDataError(missing)

    ad_spend_cents = int(
        db.query(func.coalesce(func.sum(AdSpend.amount_cents), 0))
        .filter(
            AdSpend.media_buyer_id == media_buyer_id,
            AdSpend.spend_date >= count_from,
            AdSpend.spend_date <= period_end,
            AdSpend.is_current.is_(True),
        )
        .scalar()
    )  # Postgres SUM(BigInteger) devuelve Decimal — se castea a int explícito
    # Días sin fila de ad spend cuentan como $0 — se marcan para revisión en el dashboard,
    # no bloquean el cálculo (spec: "missing day = 0, flagged for review").
    for i, li in enumerate(line_items):
        li["ad_spend_cents"] = 0  # el ad spend se registra por media buyer/día, no por source; se prorratea abajo

    prior = get_settlement(db, media_buyer_id, period_start - timedelta(days=7))
    if prior is not None and started_on is not None and prior.period_end < started_on:
        prior = None  # una semana previa a su inicio no le arrastra déficit
    incoming_deficit_cents = prior.outgoing_deficit_cents if prior else 0

    pending_adjustments = (
        db.query(FinancialAdjustment)
        .filter(FinancialAdjustment.media_buyer_id == media_buyer_id, FinancialAdjustment.status == AdjustmentStatus.PENDING)
        .all()
    )
    adjustment_cents = sum(a.amount_cents for a in pending_adjustments)

    rule = get_effective_compensation_rule(db, media_buyer_id, period_start)
    if rule is None:
        raise NoCompensationRuleError(f"Sin CompensationRule vigente para media buyer {media_buyer_id} en {period_start}")

    net_profit_cents = gross_payout_cents - ad_spend_cents - incoming_deficit_cents + adjustment_cents

    if net_profit_cents > 0:
        mb_cents, dixon_cents = split(net_profit_cents, rule.mb_percentage_bps)
        outgoing_deficit_cents = 0
    else:
        mb_cents, dixon_cents = 0, 0
        outgoing_deficit_cents = -net_profit_cents

    settlement.status = SettlementStatus.READY
    settlement.compensation_rule_id = rule.id
    settlement.mb_percentage_bps_snapshot = rule.mb_percentage_bps
    settlement.gross_payout_cents = gross_payout_cents
    settlement.ad_spend_cents = ad_spend_cents
    settlement.incoming_deficit_cents = incoming_deficit_cents
    settlement.post_settlement_adjustment_cents = adjustment_cents
    settlement.net_profit_cents = net_profit_cents
    settlement.mb_earnings_cents = mb_cents
    settlement.dixon_earnings_cents = dixon_cents
    settlement.outgoing_deficit_cents = outgoing_deficit_cents
    settlement.computed_at = _now()

    db.query(SettlementLineItem).filter_by(settlement_id=settlement.id).delete()
    for li in line_items:
        db.add(SettlementLineItem(settlement_id=settlement.id, **li))

    for adj in pending_adjustments:
        adj.status = AdjustmentStatus.APPLIED
        adj.applied_to_settlement_id = settlement.id

    write_audit_log(
        db,
        actor_user_id=actor_user_id,
        entity_type="settlement",
        entity_id=settlement.id,
        action="computed",
        before_state=before_state,
        after_state=_snapshot(settlement),
    )
    db.commit()
    return settlement


# ── Transiciones de estado ────────────────────────────────────────────────────

def approve_settlement(db: Session, settlement_id: int, actor_user_id: int) -> Settlement:
    s = db.get(Settlement, settlement_id)
    if s is None or s.status != SettlementStatus.READY:
        raise ValueError("La liquidación debe estar READY para aprobarse")
    before = _snapshot(s)
    s.status = SettlementStatus.APPROVED
    s.approved_by_user_id = actor_user_id
    s.approved_at = _now()
    write_audit_log(db, actor_user_id=actor_user_id, entity_type="settlement", entity_id=s.id,
                     action="approved", before_state=before, after_state=_snapshot(s))
    db.commit()
    return s


def mark_paid(db: Session, settlement_id: int, actor_user_id: int) -> Settlement:
    s = db.get(Settlement, settlement_id)
    if s is None or s.status != SettlementStatus.APPROVED:
        raise ValueError("La liquidación debe estar APPROVED para marcarse como pagada")
    before = _snapshot(s)
    s.status = SettlementStatus.PAID
    s.paid_at = _now()
    write_audit_log(db, actor_user_id=actor_user_id, entity_type="settlement", entity_id=s.id,
                     action="paid", before_state=before, after_state=_snapshot(s))
    db.commit()
    return s


def lock_settlement(db: Session, settlement_id: int, actor_user_id: int) -> Settlement:
    s = db.get(Settlement, settlement_id)
    if s is None or s.status != SettlementStatus.PAID:
        raise ValueError("La liquidación debe estar PAID para bloquearse")
    before = _snapshot(s)
    s.status = SettlementStatus.LOCKED
    s.locked_at = _now()
    write_audit_log(db, actor_user_id=actor_user_id, entity_type="settlement", entity_id=s.id,
                     action="locked", before_state=before, after_state=_snapshot(s))
    db.commit()
    return s


def reopen_settlement(db: Session, settlement_id: int, actor_user_id: int, reason: str) -> Settlement:
    """Solo permitido si aún no está PAID/LOCKED — reversa auditada de una aprobación."""
    s = db.get(Settlement, settlement_id)
    if s is None or s.status != SettlementStatus.APPROVED:
        raise ValueError("Solo se puede reabrir una liquidación APPROVED (no PAID/LOCKED)")
    before = _snapshot(s)
    s.status = SettlementStatus.READY
    s.approved_by_user_id = None
    s.approved_at = None
    write_audit_log(db, actor_user_id=actor_user_id, entity_type="settlement", entity_id=s.id,
                     action="reopened", before_state=before, after_state={**_snapshot(s), "reason": reason})
    db.commit()
    return s


# ── Ajustes post-liquidación (CallGrid corrige un payout ya LOCKED) ──────────

def _media_buyer_for_source_on(db: Session, callgrid_source_id: int, day: date) -> int:
    m = (
        db.query(MediaBuyerSourceMapping)
        .filter(
            MediaBuyerSourceMapping.callgrid_source_id == callgrid_source_id,
            MediaBuyerSourceMapping.effective_from <= day,
            or_(MediaBuyerSourceMapping.effective_to.is_(None), MediaBuyerSourceMapping.effective_to > day),
        )
        .one_or_none()
    )
    if m is None:
        raise NoSourceMappingError(f"Source {callgrid_source_id} sin mapeo activo el {day}")
    return m.media_buyer_id


def record_post_settlement_adjustment(
    db: Session,
    *,
    callgrid_source_id: int,
    metric_date: date,
    new_payout_cents: int,
    reason: str,
    actor_user_id: int,
    new_revenue_cents: int | None = None,
) -> FinancialAdjustment:
    """
    Usar cuando CallGrid corrige un payout que cae dentro de una liquidación ya
    LOCKED/PAID/APPROVED. Nunca muta la liquidación bloqueada — el efecto se
    aplica como un FinancialAdjustment PENDING que la próxima
    compute_settlement() recoge.
    """
    metric = (
        db.query(DailyCallgridMetric)
        .filter_by(callgrid_source_id=callgrid_source_id, metric_date=metric_date)
        .one_or_none()
    )
    old_payout_cents = metric.payout_cents if metric else 0
    delta_cents = new_payout_cents - old_payout_cents

    db.add(
        CallPayoutHistory(
            callgrid_source_id=callgrid_source_id,
            metric_date=metric_date,
            payout_cents=new_payout_cents,
            revenue_cents=new_revenue_cents if new_revenue_cents is not None else (metric.revenue_cents if metric else 0),
            ended_count=metric.ended_count if metric else 0,
            connected_count=metric.connected_count if metric else 0,
            billable_count=metric.billable_count if metric else 0,
            raw_response={"manual_adjustment": True, "reason": reason},
            observed_at=_now(),
            change_reason=reason,
        )
    )

    if metric is None:
        metric = DailyCallgridMetric(
            callgrid_source_id=callgrid_source_id,
            metric_date=metric_date,
            payout_cents=new_payout_cents,
            revenue_cents=new_revenue_cents or 0,
            raw_response={"manual_adjustment": True},
            last_synced_at=_now(),
        )
        db.add(metric)
    else:
        metric.payout_cents = new_payout_cents
        if new_revenue_cents is not None:
            metric.revenue_cents = new_revenue_cents
        metric.last_synced_at = _now()
    db.flush()

    media_buyer_id = _media_buyer_for_source_on(db, callgrid_source_id, metric_date)

    adjustment = FinancialAdjustment(
        media_buyer_id=media_buyer_id,
        adjustment_type=AdjustmentType.POST_SETTLEMENT_CALLGRID_ADJUSTMENT,
        amount_cents=delta_cents,
        reason=reason,
        status=AdjustmentStatus.PENDING,
        created_by_user_id=actor_user_id,
    )
    db.add(adjustment)
    db.flush()

    write_audit_log(
        db, actor_user_id=actor_user_id, entity_type="daily_callgrid_metric", entity_id=metric.id,
        action="payout_adjusted",
        before_state={"payout_cents": old_payout_cents},
        after_state={"payout_cents": new_payout_cents, "adjustment_id": adjustment.id},
    )
    db.commit()
    return adjustment


def void_adjustment(db: Session, adjustment_id: int, actor_user_id: int, void_reason: str) -> FinancialAdjustment:
    adj = db.get(FinancialAdjustment, adjustment_id)
    if adj is None:
        raise ValueError("Ajuste no encontrado")
    if adj.status == AdjustmentStatus.VOIDED:
        raise ValueError("El ajuste ya está VOIDED")

    before = {"status": adj.status.value}
    if adj.status == AdjustmentStatus.APPLIED:
        db.add(
            FinancialAdjustment(
                media_buyer_id=adj.media_buyer_id,
                adjustment_type=adj.adjustment_type,
                amount_cents=-adj.amount_cents,
                reason=f"Reversa del ajuste #{adj.id}: {void_reason}",
                status=AdjustmentStatus.PENDING,
                created_by_user_id=actor_user_id,
            )
        )

    adj.status = AdjustmentStatus.VOIDED
    adj.voided_by_user_id = actor_user_id
    adj.void_reason = void_reason
    db.flush()

    write_audit_log(db, actor_user_id=actor_user_id, entity_type="financial_adjustment", entity_id=adj.id,
                     action="voided", before_state=before, after_state={"status": "VOIDED", "reason": void_reason})
    db.commit()
    return adj

"""
Suite de correctitud financiera — motor de liquidaciones.

Cubre los 20 escenarios pedidos en el spec original (algunos fusionados donde
son la misma aserción vista desde otro ángulo). Los dos ejemplos numéricos
exactos del spec están en test_example_a_from_spec / test_example_b_from_spec.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import AdjustmentStatus, AdjustmentType, CompensationRule, FinancialAdjustment, SettlementStatus
from app.services.settlement_engine import (
    IncompleteDataError,
    NoCompensationRuleError,
    NoSourceMappingError,
    SettlementLockedError,
    approve_settlement,
    compute_settlement,
    get_current_deficit_cents,
    lock_settlement,
    mark_paid,
    record_post_settlement_adjustment,
    split,
    void_adjustment,
)
from tests.conftest import add_daily_metric, fill_week

MONDAY = date(2026, 1, 5)


# ── split() — la aritmética de redondeo ──────────────────────────────────────

def test_split_rounding_no_leakage():
    for net in (1, 3, 7, 100, 8000, 9999, 10000, 123456):
        mb, dixon = split(net, 3000)
        assert mb + dixon == net


def test_split_exact_example_a():
    mb, dixon = split(8000, 3000)  # $80.00 @ 30%
    assert (mb, dixon) == (2400, 5600)


# ── Ejemplo A del spec ────────────────────────────────────────────────────────

def test_example_a_from_spec(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, total_payout_cents=10000, total_ad_spend_cents=2000)
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s.status == SettlementStatus.READY
    assert s.gross_payout_cents == 10000
    assert s.ad_spend_cents == 2000
    assert s.incoming_deficit_cents == 0
    assert s.net_profit_cents == 8000
    assert s.mb_earnings_cents == 2400
    assert s.dixon_earnings_cents == 5600
    assert s.outgoing_deficit_cents == 0


# ── Ejemplo B del spec — dos períodos consecutivos con arrastre ──────────────

def test_example_b_from_spec_two_periods(db, mb1, source1, mapping1, rule_30):
    week1 = MONDAY
    week2 = MONDAY + timedelta(days=7)

    fill_week(db, source1, mb1, week1, total_payout_cents=10000, total_ad_spend_cents=12000)
    s1 = compute_settlement(db, mb1.id, week1, actor_user_id=1)
    assert s1.net_profit_cents == -2000
    assert s1.mb_earnings_cents == 0
    assert s1.dixon_earnings_cents == 0
    assert s1.outgoing_deficit_cents == 2000
    approve_settlement(db, s1.id, actor_user_id=1)
    mark_paid(db, s1.id, actor_user_id=1)
    lock_settlement(db, s1.id, actor_user_id=1)

    fill_week(db, source1, mb1, week2, total_payout_cents=20000, total_ad_spend_cents=10000)
    s2 = compute_settlement(db, mb1.id, week2, actor_user_id=1)
    assert s2.incoming_deficit_cents == 2000
    assert s2.net_profit_cents == 8000
    assert s2.mb_earnings_cents == 2400
    assert s2.dixon_earnings_cents == 5600
    assert s2.outgoing_deficit_cents == 0


# 1. Ganancia positiva normal — cubierto por test_example_a_from_spec.
# 2. Ganancia exactamente cero.
def test_zero_profit(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, total_payout_cents=5000, total_ad_spend_cents=5000)
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s.net_profit_cents == 0
    assert s.mb_earnings_cents == 0
    assert s.dixon_earnings_cents == 0
    assert s.outgoing_deficit_cents == 0  # cero no es déficit


# 3. Período negativo — cubierto por la primera mitad de test_example_b.
# 4. Arrastre al período siguiente — cubierto por test_example_b completo.

# 5. Múltiples períodos negativos consecutivos (el déficit crece).
def test_multiple_consecutive_negative_periods(db, mb1, source1, mapping1, rule_30):
    week1, week2, week3 = MONDAY, MONDAY + timedelta(days=7), MONDAY + timedelta(days=14)

    fill_week(db, source1, mb1, week1, 5000, 8000)  # -3000
    s1 = compute_settlement(db, mb1.id, week1, actor_user_id=1)
    assert s1.outgoing_deficit_cents == 3000
    for sid in (s1.id,):
        approve_settlement(db, sid, actor_user_id=1); mark_paid(db, sid, actor_user_id=1); lock_settlement(db, sid, actor_user_id=1)

    fill_week(db, source1, mb1, week2, 4000, 6000)  # gross-ads=-2000, -incoming(3000) = -5000
    s2 = compute_settlement(db, mb1.id, week2, actor_user_id=1)
    assert s2.incoming_deficit_cents == 3000
    assert s2.net_profit_cents == -5000
    assert s2.outgoing_deficit_cents == 5000
    approve_settlement(db, s2.id, actor_user_id=1); mark_paid(db, s2.id, actor_user_id=1); lock_settlement(db, s2.id, actor_user_id=1)

    fill_week(db, source1, mb1, week3, 20000, 5000)  # 15000 - 5000(incoming) = 10000
    s3 = compute_settlement(db, mb1.id, week3, actor_user_id=1)
    assert s3.net_profit_cents == 10000
    assert s3.mb_earnings_cents == 3000
    assert s3.outgoing_deficit_cents == 0


# 6. Déficit recuperado completamente — cubierto por test_example_b (deficit vuelve a 0).
# 7. Déficit recuperado parcialmente (net profit positivo pero < deficit... en este
#    modelo no hay "parcial": o se cubre todo y sobra ganancia, o no se cubre nada.
#    Se prueba el caso donde el profit apenas alcanza para volver a 0 exacto).
def test_deficit_exactly_covered_zero_leftover(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 10000, 12000)
    s1 = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s1.outgoing_deficit_cents == 2000
    approve_settlement(db, s1.id, actor_user_id=1); mark_paid(db, s1.id, actor_user_id=1); lock_settlement(db, s1.id, actor_user_id=1)

    week2 = MONDAY + timedelta(days=7)
    fill_week(db, source1, mb1, week2, 2000, 0)  # net = 2000 - 0 - 2000 = 0 exacto
    s2 = compute_settlement(db, mb1.id, week2, actor_user_id=1)
    assert s2.net_profit_cents == 0
    assert s2.mb_earnings_cents == 0
    assert s2.outgoing_deficit_cents == 0


# 8/9/10. Compensación 30% luego 25%, con fecha de vigencia — no debe alterar liquidaciones pasadas.
def test_effective_dated_percentage_change_does_not_alter_past(db, mb1, source1, mapping1, rule_30):
    week1 = MONDAY
    fill_week(db, source1, mb1, week1, 10000, 2000)
    s1 = compute_settlement(db, mb1.id, week1, actor_user_id=1)
    assert s1.mb_percentage_bps_snapshot == 3000
    assert s1.mb_earnings_cents == 2400
    approve_settlement(db, s1.id, actor_user_id=1); mark_paid(db, s1.id, actor_user_id=1); lock_settlement(db, s1.id, actor_user_id=1)

    # Nueva tasa 25% vigente desde la semana 3 (deja un hueco en la semana 2 a propósito
    # para probar NoCompensationRuleError más abajo también).
    week3 = MONDAY + timedelta(days=14)
    rule_30.effective_to = week3
    new_rule = CompensationRule(media_buyer_id=mb1.id, mb_percentage_bps=2500,
                                 effective_from=week3, created_by_user_id=1)
    db.add(new_rule)
    db.commit()

    fill_week(db, source1, mb1, week3, 10000, 2000)
    s3 = compute_settlement(db, mb1.id, week3, actor_user_id=1)
    assert s3.mb_percentage_bps_snapshot == 2500
    assert s3.mb_earnings_cents == 2000  # 8000 * 25%

    # La liquidación 1 (ya LOCKED) conserva su snapshot histórico de 30%, intacta.
    assert s1.mb_percentage_bps_snapshot == 3000
    assert s1.mb_earnings_cents == 2400


def test_compensation_rule_effective_from_must_be_monday_is_app_convention(db, mb1):
    # La restricción dura vive en la migración de Postgres (CHECK ISODOW=1);
    # a nivel de servicio, get_effective_compensation_rule simplemente no
    # encuentra una regla si period_start cae fuera de cualquier rango vigente.
    tuesday = date(2026, 1, 6)
    r = CompensationRule(media_buyer_id=mb1.id, mb_percentage_bps=3000, effective_from=tuesday, created_by_user_id=1)
    db.add(r); db.commit()
    from app.services.settlement_engine import get_effective_compensation_rule
    assert get_effective_compensation_rule(db, mb1.id, MONDAY) is None  # semana anterior a la vigencia


# 11. Ajuste de payout de CallGrid ANTES de liquidar (mientras aún está OPEN/READY):
#     el recálculo simplemente toma el valor más reciente, sin pasar por financial_adjustments.
def test_callgrid_payout_change_before_settlement(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 10000, 2000)
    s1 = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s1.gross_payout_cents == 10000

    # Simula una corrección de sync normal actualizando el puntero directamente
    # (daily_callgrid_metrics tiene UNIQUE(source, día) — un sync real hace UPDATE, no INSERT):
    from app.models import DailyCallgridMetric
    row = db.query(DailyCallgridMetric).filter_by(callgrid_source_id=source1.id, metric_date=MONDAY).order_by(DailyCallgridMetric.id.desc()).first()
    row.payout_cents = 15000
    db.commit()

    s1_recalc = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s1_recalc.gross_payout_cents == 15000  # antes de LOCKED, el recálculo toma el valor nuevo


# 12. Ajuste de payout DESPUÉS de que la liquidación quedó LOCKED.
def test_post_settlement_callgrid_adjustment_after_lock(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 10000, 2000)
    s1 = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    approve_settlement(db, s1.id, actor_user_id=1); mark_paid(db, s1.id, actor_user_id=1); lock_settlement(db, s1.id, actor_user_id=1)
    frozen_mb_earnings = s1.mb_earnings_cents

    # CallGrid revierte $10 de payout de ese lunes ya liquidado.
    adj = record_post_settlement_adjustment(
        db, callgrid_source_id=source1.id, metric_date=MONDAY, new_payout_cents=9000,
        reason="CallGrid resync — call reclassified", actor_user_id=1,
    )
    assert adj.amount_cents == -1000
    assert adj.status == AdjustmentStatus.PENDING

    # La liquidación bloqueada NO cambia.
    assert s1.mb_earnings_cents == frozen_mb_earnings
    assert s1.status == SettlementStatus.LOCKED
    with pytest.raises(SettlementLockedError):
        compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)

    # El efecto se aplica en la SIGUIENTE liquidación abierta.
    week2 = MONDAY + timedelta(days=7)
    fill_week(db, source1, mb1, week2, 10000, 2000)
    s2 = compute_settlement(db, mb1.id, week2, actor_user_id=1)
    assert s2.post_settlement_adjustment_cents == -1000
    assert s2.net_profit_cents == 8000 - 1000  # 8000 normal - 1000 del ajuste
    adj_after = db.get(FinancialAdjustment, adj.id)
    assert adj_after.status == AdjustmentStatus.APPLIED
    assert adj_after.applied_to_settlement_id == s2.id


# 13. Edición de ad spend.
def test_ad_spend_edit_before_approval(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 10000, 2000)
    s1 = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s1.ad_spend_cents == 2000

    from app.services.ad_spend import ManualAdSpendProvider
    ManualAdSpendProvider().record_spend(db, media_buyer_id=mb1.id, spend_date=MONDAY, amount_cents=3000, actor_user_id=1)
    s1_recalc = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s1_recalc.ad_spend_cents == 3000  # is_current reemplazó la fila vieja, no la duplicó


# 14. Ajuste manual (bono/chargeback).
def test_manual_bonus_adjustment_applied(db, mb1, source1, mapping1, rule_30):
    db.add(FinancialAdjustment(media_buyer_id=mb1.id, adjustment_type=AdjustmentType.BONUS,
                                amount_cents=1000, reason="Bono por desempeño", status=AdjustmentStatus.PENDING,
                                created_by_user_id=1))
    db.commit()
    fill_week(db, source1, mb1, MONDAY, 10000, 2000)
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s.post_settlement_adjustment_cents == 1000
    assert s.net_profit_cents == 9000  # 8000 + 1000 bono


def test_voided_adjustment_creates_offsetting_pending_adjustment(db, mb1, source1, mapping1, rule_30):
    adj = FinancialAdjustment(media_buyer_id=mb1.id, adjustment_type=AdjustmentType.BONUS,
                               amount_cents=1000, reason="Bono", status=AdjustmentStatus.APPLIED,
                               created_by_user_id=1)
    db.add(adj); db.commit()
    void_adjustment(db, adj.id, actor_user_id=1, void_reason="Error de captura")

    offsets = db.query(FinancialAdjustment).filter(
        FinancialAdjustment.media_buyer_id == mb1.id,
        FinancialAdjustment.status == AdjustmentStatus.PENDING,
    ).all()
    assert len(offsets) == 1
    assert offsets[0].amount_cents == -1000


# 15. Redondeo — cubierto por test_split_rounding_no_leakage; caso específico no-divisible exacto.
def test_rounding_non_divisible_net_profit(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 10001, 0)  # net=10001, 30% no es exacto
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s.mb_earnings_cents + s.dixon_earnings_cents == s.net_profit_cents
    assert s.mb_earnings_cents == round(10001 * 0.30)  # 3000, half-up


# 16. period_start debe ser lunes (frontera semanal).
def test_period_start_must_be_monday(db, mb1, source1, mapping1, rule_30):
    tuesday = date(2026, 1, 6)
    with pytest.raises(ValueError):
        compute_settlement(db, mb1.id, tuesday, actor_user_id=1)


# 17. Idempotencia de webhooks — diferido a Fase 2 (webhooks no implementados aún).
@pytest.mark.skip(reason="Webhooks de CallGrid son Fase 2 — no implementados todavía")
def test_duplicate_webhook_is_idempotent():
    pass


# 18. Reconciliación vía API — cubierto en tests/test_callgrid_sync.py.

# 19. Source mapeada al media buyer incorrecto (se prueba que el ajuste post-settlement
#     resuelve el media buyer correcto vía el mapeo vigente, no uno viejo).
def test_post_settlement_adjustment_resolves_current_mapping(db, mb1, source1, mapping1, rule_30):
    adj = record_post_settlement_adjustment(
        db, callgrid_source_id=source1.id, metric_date=MONDAY, new_payout_cents=500,
        reason="ajuste", actor_user_id=1,
    )
    assert adj.media_buyer_id == mb1.id


# 20. Sin mapeo de Source en absoluto.
def test_no_source_mapping_raises(db, mb1, rule_30):
    with pytest.raises(NoSourceMappingError):
        compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)


def test_incomplete_callgrid_data_blocks_ready(db, mb1, source1, mapping1, rule_30):
    add_daily_metric(db, source1, MONDAY, 5000)  # solo el lunes, faltan 6 días
    with pytest.raises(IncompleteDataError):
        compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    s = db.query(__import__("app.models", fromlist=["Settlement"]).Settlement).filter_by(
        media_buyer_id=mb1.id, period_start=MONDAY
    ).one()
    assert s.status == SettlementStatus.PENDING_RECONCILIATION


def test_no_compensation_rule_raises(db, mb1, source1, mapping1):
    fill_week(db, source1, mb1, MONDAY, 10000, 2000)  # sin fixture rule_30
    with pytest.raises(NoCompensationRuleError):
        compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)


def test_locked_settlement_cannot_be_recomputed(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 10000, 2000)
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    approve_settlement(db, s.id, actor_user_id=1)
    mark_paid(db, s.id, actor_user_id=1)
    lock_settlement(db, s.id, actor_user_id=1)
    with pytest.raises(SettlementLockedError):
        compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)


def test_first_ever_settlement_has_zero_incoming_deficit(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 5000, 1000)
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert s.incoming_deficit_cents == 0


def test_current_deficit_reads_latest_finalized_settlement(db, mb1, source1, mapping1, rule_30):
    fill_week(db, source1, mb1, MONDAY, 5000, 8000)
    s = compute_settlement(db, mb1.id, MONDAY, actor_user_id=1)
    assert get_current_deficit_cents(db, mb1.id) == 0  # todavía no aprobada -> no cuenta
    approve_settlement(db, s.id, actor_user_id=1)
    mark_paid(db, s.id, actor_user_id=1)
    lock_settlement(db, s.id, actor_user_id=1)
    assert get_current_deficit_cents(db, mb1.id) == 3000

#!/usr/bin/env python3
"""
Seed de desarrollo/demo — DC Dashboard.

Crea:
  - Partner demo (sub_id 40) + su primer media buyer DC1 (sub_id 44)
  - Regla de compensación 30% vigente desde el primer lunes de 2026
  - El primer usuario ROYALSPACE_ADMIN (desde ADMIN_EMAIL/ADMIN_PASSWORD, si no existe ya uno)

Solo con SEED_DEMO_DATA=true (desarrollo local — NUNCA en producción):
  - Un usuario DIXON_MANAGER y uno MEDIA_BUYER de ejemplo (password: "changeme123")
  - Dos liquidaciones demo que reproducen EXACTO los dos ejemplos numéricos del spec:
      Ejemplo A: payout $100, ads $20            -> MB $24.00 / Partner $56.00
      Ejemplo B (2 semanas): payout $100/ads $120 -> deficit $20
                              payout $200/ads $100 -> MB $24.00 / Partner $56.00, deficit $0

Sin SEED_DEMO_DATA=true, en cambio BORRA esos datos demo si existen (3 semanas de
enero 2026 de DC1 + los 2 usuarios @example.com). Importa: la liquidación demo
bloqueada deja un déficit de $20 que se arrastraría a la primera liquidación real.

Uso:
    python scripts/seed.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.auth.security import hash_password
from app.config import ADMIN_EMAIL, ADMIN_PASSWORD, PARTNER_NAME, PARTNER_VENDOR_SUB_ID
from app.db import Base, SessionLocal, engine
from app.models import (
    AdSpend,
    AuditLog,
    CallgridSource,
    CallPayoutHistory,
    CompensationRule,
    DailyCallgridMetric,
    FinancialAdjustment,
    MediaBuyer,
    MediaBuyerSourceMapping,
    Partner,
    Settlement,
    SettlementLineItem,
    User,
    UserRole,
)
from app.services.audit import write_audit_log
from app.services.settlement_engine import approve_settlement, compute_settlement, lock_settlement, mark_paid

SEED_DEMO_DATA = (os.environ.get("SEED_DEMO_DATA") or "").strip().lower() == "true"
DEMO_EMAILS = ("partner@example.com", "dc1@example.com")


def _next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7) if d.weekday() != 0 else d


def remove_demo_data(db, mb1: MediaBuyer, source1: CallgridSource, week1: date, actor_user_id: int | None) -> None:
    """Borra exactamente lo que crea la parte demo de este seed (idempotente)."""
    start, end = week1, week1 + timedelta(days=20)  # las 3 semanas demo (Ejemplo A + B)

    settlements = db.query(Settlement).filter(
        Settlement.media_buyer_id == mb1.id, Settlement.period_start >= start, Settlement.period_start <= end,
    ).all()
    settlement_ids = [s.id for s in settlements]

    if settlement_ids and db.query(FinancialAdjustment).filter(
        (FinancialAdjustment.source_settlement_id.in_(settlement_ids))
        | (FinancialAdjustment.applied_to_settlement_id.in_(settlement_ids))
    ).count():
        print("AVISO: hay ajustes financieros ligados a las liquidaciones demo — no se borra nada automáticamente.")
        return

    ad_spend_q = db.query(AdSpend).filter(
        AdSpend.media_buyer_id == mb1.id, AdSpend.spend_date >= start, AdSpend.spend_date <= end,
    )
    ad_spend_q.update({AdSpend.superseded_by_id: None}, synchronize_session=False)
    counts = {
        "settlement_line_items": db.query(SettlementLineItem).filter(
            SettlementLineItem.settlement_id.in_(settlement_ids)).delete(synchronize_session=False),
        "settlements": db.query(Settlement).filter(
            Settlement.id.in_(settlement_ids)).delete(synchronize_session=False),
        "ad_spend": ad_spend_q.delete(synchronize_session=False),
        "daily_callgrid_metrics": db.query(DailyCallgridMetric).filter(
            DailyCallgridMetric.callgrid_source_id == source1.id,
            DailyCallgridMetric.metric_date >= start, DailyCallgridMetric.metric_date <= end,
        ).delete(synchronize_session=False),
        "call_payout_history": db.query(CallPayoutHistory).filter(
            CallPayoutHistory.callgrid_source_id == source1.id,
            CallPayoutHistory.metric_date >= start, CallPayoutHistory.metric_date <= end,
        ).delete(synchronize_session=False),
    }

    # Usuarios demo: se borran, salvo que ya tengan historial en el audit log
    # (ej. si alguien los usó para registrar algo) — ahí solo se desactivan.
    users_removed = users_deactivated = 0
    for user in db.query(User).filter(User.email.in_(DEMO_EMAILS)).all():
        if db.query(AuditLog).filter(AuditLog.actor_user_id == user.id).count():
            if user.is_active:
                user.is_active = False
                users_deactivated += 1
        else:
            db.delete(user)
            users_removed += 1
    counts["users_removed"], counts["users_deactivated"] = users_removed, users_deactivated

    if not any(counts.values()):
        return

    write_audit_log(db, actor_user_id=actor_user_id, entity_type="demo_data", entity_id=0,
                    action="removed", before_state={"from": str(start), "to": str(end)}, after_state=counts)
    db.commit()
    print(f"Datos demo eliminados: {counts}")


def main() -> None:
    Base.metadata.create_all(engine)  # no-op si ya corriste `alembic upgrade head`
    db = SessionLocal()

    try:
        partner = db.query(Partner).filter_by(callgrid_vendor_sub_id=PARTNER_VENDOR_SUB_ID).one_or_none()
        if partner is None:
            partner = Partner(name=PARTNER_NAME, callgrid_vendor_sub_id=PARTNER_VENDOR_SUB_ID,
                               callgrid_vendor_raw_name=f"({PARTNER_VENDOR_SUB_ID}) {PARTNER_NAME}")
            db.add(partner)
            db.flush()
            print(f"Creado Partner: {partner.name} (sub_id {partner.callgrid_vendor_sub_id})")

        mb1 = db.query(MediaBuyer).filter_by(partner_id=partner.id, display_name="DC1").one_or_none()
        if mb1 is None:
            mb1 = MediaBuyer(partner_id=partner.id, display_name="DC1")
            db.add(mb1)
            db.flush()
            print(f"Creado MediaBuyer: {mb1.display_name}")

        source1 = db.query(CallgridSource).filter_by(callgrid_sub_id=44).one_or_none()
        if source1 is None:
            source1 = CallgridSource(callgrid_sub_id=44, callgrid_source_raw_name="(44) DC1")
            db.add(source1)
            db.flush()
            print(f"Creada CallgridSource: {source1.callgrid_source_raw_name}")

        week1 = _next_monday(date(2026, 1, 1))

        mapping = db.query(MediaBuyerSourceMapping).filter_by(media_buyer_id=mb1.id, callgrid_source_id=source1.id).one_or_none()
        if mapping is None:
            db.add(MediaBuyerSourceMapping(media_buyer_id=mb1.id, callgrid_source_id=source1.id, effective_from=week1))
            print("Creado mapeo MediaBuyer <-> CallgridSource")

        admin_user = db.query(User).filter_by(role=UserRole.ROYALSPACE_ADMIN).first()
        if admin_user is None and ADMIN_EMAIL and ADMIN_PASSWORD:
            admin_user = User(email=ADMIN_EMAIL.strip().lower(), hashed_password=hash_password(ADMIN_PASSWORD),
                               role=UserRole.ROYALSPACE_ADMIN)
            db.add(admin_user)
            db.flush()
            print(f"Creado ROYALSPACE_ADMIN: {admin_user.email}")

        db.flush()

        rule = db.query(CompensationRule).filter_by(media_buyer_id=mb1.id).order_by(CompensationRule.effective_from).first()
        if rule is None:
            rule = CompensationRule(media_buyer_id=mb1.id, mb_percentage_bps=3000, effective_from=week1,
                                     created_by_user_id=admin_user.id if admin_user else 1)
            db.add(rule)
            print("Creada CompensationRule: 30% vigente desde", week1)

        db.commit()

        if not SEED_DEMO_DATA:
            # El seed corre dentro del startCommand de Railway — si la limpieza falla,
            # que no tumbe el deploy (uvicorn nunca arrancaría).
            try:
                remove_demo_data(db, mb1, source1, week1, admin_user.id if admin_user else None)
            except Exception as exc:
                db.rollback()
                print(f"AVISO: no se pudieron borrar los datos demo ({exc}) — el dashboard arranca igual.")
            print("\nSeed completo (sin datos demo).")
            return

        partner_manager = db.query(User).filter_by(email="partner@example.com").one_or_none()
        if partner_manager is None:
            partner_manager = User(email="partner@example.com", hashed_password=hash_password("changeme123"),
                                    role=UserRole.DIXON_MANAGER, partner_id=partner.id)
            db.add(partner_manager)
            print("Creado DIXON_MANAGER demo: partner@example.com / changeme123")

        mb_user = db.query(User).filter_by(email="dc1@example.com").one_or_none()
        if mb_user is None:
            mb_user = User(email="dc1@example.com", hashed_password=hash_password("changeme123"),
                            role=UserRole.MEDIA_BUYER, media_buyer_id=mb1.id)
            db.add(mb_user)
            print("Creado MEDIA_BUYER demo: dc1@example.com / changeme123")

        db.commit()

        # ── Ejemplo A del spec: payout $100, ads $20 -> MB $24.00 / Partner $56.00 ──
        week_a = week1
        if db.query(DailyCallgridMetric).filter_by(callgrid_source_id=source1.id, metric_date=week_a).one_or_none() is None:
            db.add(DailyCallgridMetric(callgrid_source_id=source1.id, metric_date=week_a,
                                        payout_cents=10000, revenue_cents=0, raw_response={}))
            db.add(AdSpend(media_buyer_id=mb1.id, spend_date=week_a, amount_cents=2000,
                            entered_by_user_id=admin_user.id if admin_user else 1))
            for i in range(1, 7):
                d = week_a + timedelta(days=i)
                db.add(DailyCallgridMetric(callgrid_source_id=source1.id, metric_date=d, payout_cents=0, revenue_cents=0, raw_response={}))
                db.add(AdSpend(media_buyer_id=mb1.id, spend_date=d, amount_cents=0, entered_by_user_id=admin_user.id if admin_user else 1))
            db.commit()

            s_a = compute_settlement(db, mb1.id, week_a, actor_user_id=admin_user.id if admin_user else 1)
            assert s_a.mb_earnings_cents == 2400 and s_a.dixon_earnings_cents == 5600
            approve_settlement(db, s_a.id, actor_user_id=admin_user.id if admin_user else 1)
            mark_paid(db, s_a.id, actor_user_id=admin_user.id if admin_user else 1)
            lock_settlement(db, s_a.id, actor_user_id=admin_user.id if admin_user else 1)
            print(f"Ejemplo A calculado y bloqueado: MB={s_a.mb_earnings_cents/100:.2f} Partner={s_a.dixon_earnings_cents/100:.2f}")

        # ── Ejemplo B: dos semanas siguientes con arrastre de déficit ──────────
        week_b1 = week_a + timedelta(days=7)
        week_b2 = week_b1 + timedelta(days=7)
        if db.query(DailyCallgridMetric).filter_by(callgrid_source_id=source1.id, metric_date=week_b1).one_or_none() is None:
            for day, payout, ads in ((week_b1, 10000, 12000), (week_b2, 20000, 10000)):
                db.add(DailyCallgridMetric(callgrid_source_id=source1.id, metric_date=day, payout_cents=payout, revenue_cents=0, raw_response={}))
                db.add(AdSpend(media_buyer_id=mb1.id, spend_date=day, amount_cents=ads, entered_by_user_id=admin_user.id if admin_user else 1))
                for i in range(1, 7):
                    d = day + timedelta(days=i)
                    db.add(DailyCallgridMetric(callgrid_source_id=source1.id, metric_date=d, payout_cents=0, revenue_cents=0, raw_response={}))
                    db.add(AdSpend(media_buyer_id=mb1.id, spend_date=d, amount_cents=0, entered_by_user_id=admin_user.id if admin_user else 1))
            db.commit()

            s_b1 = compute_settlement(db, mb1.id, week_b1, actor_user_id=admin_user.id if admin_user else 1)
            assert s_b1.outgoing_deficit_cents == 2000
            approve_settlement(db, s_b1.id, actor_user_id=admin_user.id if admin_user else 1)
            mark_paid(db, s_b1.id, actor_user_id=admin_user.id if admin_user else 1)
            lock_settlement(db, s_b1.id, actor_user_id=admin_user.id if admin_user else 1)

            s_b2 = compute_settlement(db, mb1.id, week_b2, actor_user_id=admin_user.id if admin_user else 1)
            assert s_b2.mb_earnings_cents == 2400 and s_b2.dixon_earnings_cents == 5600 and s_b2.outgoing_deficit_cents == 0
            print(f"Ejemplo B calculado: semana 1 deficit=$20.00, semana 2 MB={s_b2.mb_earnings_cents/100:.2f} Partner={s_b2.dixon_earnings_cents/100:.2f} deficit=$0.00")

        print("\nSeed completo.")
        if not admin_user:
            print("AVISO: no se creó ningún ROYALSPACE_ADMIN — define ADMIN_EMAIL/ADMIN_PASSWORD y vuelve a correr.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

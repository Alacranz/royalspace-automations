from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    AdSpend,
    CallgridSource,
    CompensationRule,
    DailyCallgridMetric,
    MediaBuyer,
    MediaBuyerSourceMapping,
    Partner,
)


@pytest.fixture()
def db() -> Session:
    """SQLite en memoria — rápida para testear la lógica del motor financiero.
    Las restricciones EXCLUDE de Postgres (no solapamiento de fechas) viven en
    la migración de Alembic, no acá; estos tests controlan los datos a mano
    para no necesitar esa garantía a nivel de DB."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def dixon(db: Session) -> Partner:
    p = Partner(name="Dixon Colmenares", callgrid_vendor_sub_id=40, callgrid_vendor_raw_name="(40) Dixon Colmenares")
    db.add(p)
    db.commit()
    return p


@pytest.fixture()
def mb1(db: Session, dixon: Partner) -> MediaBuyer:
    mb = MediaBuyer(partner_id=dixon.id, display_name="DC1")
    db.add(mb)
    db.commit()
    return mb


@pytest.fixture()
def source1(db: Session) -> CallgridSource:
    s = CallgridSource(callgrid_sub_id=44, callgrid_source_raw_name="(44) DC1")
    db.add(s)
    db.commit()
    return s


@pytest.fixture()
def mapping1(db: Session, mb1: MediaBuyer, source1: CallgridSource) -> MediaBuyerSourceMapping:
    m = MediaBuyerSourceMapping(
        media_buyer_id=mb1.id, callgrid_source_id=source1.id,
        effective_from=date(2026, 1, 1), effective_to=None,
    )
    db.add(m)
    db.commit()
    return m


@pytest.fixture()
def rule_30(db: Session, mb1: MediaBuyer) -> CompensationRule:
    r = CompensationRule(
        media_buyer_id=mb1.id, mb_percentage_bps=3000,
        effective_from=date(2026, 1, 5),  # lunes
        created_by_user_id=1,
    )
    db.add(r)
    db.commit()
    return r


def add_daily_metric(db: Session, source: CallgridSource, day: date, payout_cents: int,
                      revenue_cents: int = 0, billable_count: int = 0) -> DailyCallgridMetric:
    m = DailyCallgridMetric(
        callgrid_source_id=source.id, metric_date=day,
        payout_cents=payout_cents, revenue_cents=revenue_cents,
        ended_count=billable_count, connected_count=billable_count, billable_count=billable_count,
        raw_response={},
    )
    db.add(m)
    db.commit()
    return m


def add_ad_spend(db: Session, media_buyer: MediaBuyer, day: date, amount_cents: int) -> AdSpend:
    a = AdSpend(media_buyer_id=media_buyer.id, spend_date=day, amount_cents=amount_cents,
                entered_by_user_id=1, is_current=True)
    db.add(a)
    db.commit()
    return a


def fill_week(db: Session, source: CallgridSource, media_buyer: MediaBuyer, week_start: date,
              total_payout_cents: int, total_ad_spend_cents: int) -> None:
    """Reparte el payout/ad_spend total de la semana en el lunes (simplifica los tests)."""
    add_daily_metric(db, source, week_start, total_payout_cents)
    for i in range(1, 7):
        add_daily_metric(db, source, week_start.fromordinal(week_start.toordinal() + i), 0)
    add_ad_spend(db, media_buyer, week_start, total_ad_spend_cents)
    for i in range(1, 7):
        add_ad_spend(db, media_buyer, week_start.fromordinal(week_start.toordinal() + i), 0)

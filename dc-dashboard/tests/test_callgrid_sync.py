from __future__ import annotations

from datetime import date

from app.callgrid import sync as callgrid_sync
from app.models import DailyCallgridMetric
from tests.conftest import add_daily_metric

DAY = date(2026, 10, 1)


def _metric(db, source):
    return db.query(DailyCallgridMetric).filter_by(callgrid_source_id=source.id, metric_date=DAY).one_or_none()


def test_day_without_calls_writes_zero_row(db, source1, monkeypatch):
    """Sin fila, compute_settlement toma el día como no sincronizado y bloquea la liquidación."""
    monkeypatch.setattr(callgrid_sync, "get_source_revenue_by_day", lambda *a, **k: {})
    callgrid_sync.sync_day(db, DAY)
    m = _metric(db, source1)
    assert m is not None and m.payout_cents == 0


def test_empty_response_never_overwrites_existing_payout(db, source1, monkeypatch):
    add_daily_metric(db, source1, DAY, 7000)
    monkeypatch.setattr(callgrid_sync, "get_source_revenue_by_day", lambda *a, **k: {})
    callgrid_sync.sync_day(db, DAY)
    assert _metric(db, source1).payout_cents == 7000

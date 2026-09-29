from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.callgrid.client import get_source_revenue_by_day
from app.config import CALLGRID_API_KEY, CALLGRID_ORG_ID
from app.models import CallgridSource, CallPayoutHistory, DailyCallgridMetric, SyncRun, SyncStatus


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sync_day(db: Session, day: date, report_timezone: str = "US/Eastern") -> SyncRun:
    """
    Sincroniza un día para todas las CallgridSource activas. Escribe/actualiza
    el puntero en daily_callgrid_metrics y SIEMPRE agrega una fila a
    call_payout_history (ledger de auditoría append-only), cambie o no el valor.
    """
    run = SyncRun(started_at=_now(), status=SyncStatus.RUNNING, pivot_used="SourceName",
                  date_range_start=day, date_range_end=day)
    db.add(run)
    db.flush()

    sources = {str(s.callgrid_sub_id): s for s in db.query(CallgridSource).filter_by(is_active=True).all()}
    synced = 0
    try:
        remote = get_source_revenue_by_day(CALLGRID_API_KEY, CALLGRID_ORG_ID, day, report_timezone=report_timezone)

        for sub_id, source in sources.items():
            data = remote.get(sub_id)
            if data is None:
                continue  # sin llamadas ese día para esa source — no es un error

            existing = (
                db.query(DailyCallgridMetric)
                .filter_by(callgrid_source_id=source.id, metric_date=day)
                .one_or_none()
            )
            if existing is None:
                existing = DailyCallgridMetric(callgrid_source_id=source.id, metric_date=day, raw_response={})
                db.add(existing)

            existing.payout_cents = data["payout_cents"]
            existing.revenue_cents = data["revenue_cents"]
            existing.ended_count = data["ended_count"]
            existing.connected_count = data["connected_count"]
            existing.billable_count = data["billable_count"]
            existing.raw_response = data
            existing.last_synced_at = _now()
            existing.sync_run_id = run.id

            db.add(
                CallPayoutHistory(
                    callgrid_source_id=source.id, metric_date=day,
                    payout_cents=data["payout_cents"], revenue_cents=data["revenue_cents"],
                    ended_count=data["ended_count"], connected_count=data["connected_count"],
                    billable_count=data["billable_count"], raw_response=data,
                    observed_at=_now(), sync_run_id=run.id,
                )
            )
            synced += 1

        run.status = SyncStatus.SUCCESS
    except Exception as exc:
        run.status = SyncStatus.FAILED
        run.error_message = str(exc)
    finally:
        run.sources_synced = synced
        run.completed_at = _now()
        db.commit()

    return run


def nightly_resync(db: Session, days_back: int = 7, report_timezone: str = "US/Eastern") -> list[SyncRun]:
    """Resincroniza los últimos N días — CallGrid puede ajustar payouts después
    del hecho (disputas, llamadas reclasificadas), así que no basta con sincronizar
    solo el día actual."""
    today = date.today()
    runs = []
    for i in range(days_back):
        day = today - timedelta(days=i)
        runs.append(sync_day(db, day, report_timezone=report_timezone))
    return runs

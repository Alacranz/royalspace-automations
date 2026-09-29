from __future__ import annotations

import csv
import io

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.config import REPORTING_TIMEZONE
from app.db import get_db
from app.models import Settlement, User, UserRole
from app.routers import admin, auth, media_buyer, partner

app = FastAPI(title="DC Dashboard")

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(partner.router)
app.include_router(media_buyer.router)


@app.exception_handler(HTTPException)
async def redirect_on_auth_failure(request: Request, exc: HTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    return await http_exception_handler(request, exc)


@app.get("/")
def root():
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/export/settlements.csv")
def export_settlements_csv(db: Session = Depends(get_db), user: User = Depends(require_role(
    UserRole.ROYALSPACE_ADMIN, UserRole.DIXON_MANAGER, UserRole.MEDIA_BUYER,
))):
    from app.auth.deps import scoped_media_buyer_ids

    query = db.query(Settlement)
    scoped = scoped_media_buyer_ids(db, user)
    if scoped is not None:
        query = query.filter(Settlement.media_buyer_id.in_(scoped))
    settlements = query.order_by(Settlement.period_start.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "media_buyer_id", "period_start", "period_end", "status", "gross_payout",
        "ad_spend", "incoming_deficit", "net_profit", "mb_earnings", "dixon_earnings", "outgoing_deficit",
    ])
    for s in settlements:
        writer.writerow([
            s.media_buyer_id, s.period_start, s.period_end, s.status.value,
            s.gross_payout_cents / 100, s.ad_spend_cents / 100, s.incoming_deficit_cents / 100,
            s.net_profit_cents / 100, s.mb_earnings_cents / 100, s.dixon_earnings_cents / 100,
            s.outgoing_deficit_cents / 100,
        ])
    buf.seek(0)
    return StreamingResponse(buf, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=settlements.csv"})


def _run_nightly_sync() -> None:
    from app.callgrid.sync import nightly_resync
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        nightly_resync(db, days_back=7, report_timezone=REPORTING_TIMEZONE)
    finally:
        db.close()


def start_nightly_sync_scheduler() -> None:
    """
    Mismo patrón que manychat/main.py::start_github_scheduler en royalspace-automations:
    proceso siempre-activo en Railway dispara el resync en vez de depender de un
    cron externo. Corre todos los días a las 3:00 AM en REPORTING_TIMEZONE.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_nightly_sync,
        CronTrigger(hour=3, minute=0, timezone=REPORTING_TIMEZONE),
        id="nightly_callgrid_resync",
        misfire_grace_time=600,
    )
    scheduler.start()
    print("[scheduler] Nightly CallGrid resync iniciado (3:00 AM)")


@app.on_event("startup")
async def startup() -> None:
    # El scheduler es secundario — un fallo ahí nunca debe tumbar el servidor
    # (lección de manychat/main.py en royalspace-automations, 2026-09-28).
    try:
        start_nightly_sync_scheduler()
    except Exception as exc:
        print(f"[scheduler] ERROR al iniciar el scheduler de sync: {exc}")

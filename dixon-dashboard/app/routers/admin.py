from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_role
from app.callgrid.sync import nightly_resync
from app.db import get_db
from app.models import (
    AdSpend,
    AuditLog,
    CallgridSource,
    CompensationRule,
    FinancialAdjustment,
    MediaBuyer,
    MediaBuyerSourceMapping,
    Partner,
    Settlement,
    SyncRun,
    User,
    UserRole,
)
from app.auth.security import hash_password
from app.services import settlement_engine as engine
from app.services.ad_spend import ManualAdSpendProvider
from app.services.audit import write_audit_log
from app.services.formatting import money

router = APIRouter(prefix="/admin", dependencies=[Depends(require_role(UserRole.ROYALSPACE_ADMIN))])
templates = Jinja2Templates(directory="app/templates")


def _this_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today - timedelta(days=today.weekday())


@router.get("")
def overview(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    partners = db.query(Partner).all()
    media_buyers = db.query(MediaBuyer).all()
    last_sync = db.query(SyncRun).order_by(SyncRun.started_at.desc()).first()
    return templates.TemplateResponse(
        request, "admin/overview.html",
        {"user": user, "partners": partners, "media_buyers": media_buyers, "last_sync": last_sync},
    )


# ── CallGrid sync ─────────────────────────────────────────────────────────────

@router.get("/sync")
def sync_status(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    runs = db.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(20).all()
    return templates.TemplateResponse(request, "admin/sync.html", {"user": user, "runs": runs})


@router.post("/sync/now")
def sync_now(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    nightly_resync(db, days_back=7)
    return RedirectResponse(url="/admin/sync", status_code=303)


# ── Media buyers ──────────────────────────────────────────────────────────────

@router.get("/media-buyers")
def media_buyers_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    partners = db.query(Partner).all()
    media_buyers = db.query(MediaBuyer).all()
    sources = db.query(CallgridSource).all()
    mappings = db.query(MediaBuyerSourceMapping).all()
    rules = db.query(CompensationRule).all()
    return templates.TemplateResponse(
        request, "admin/media_buyers.html",
        {"user": user, "partners": partners, "media_buyers": media_buyers, "sources": sources,
         "mappings": mappings, "rules": rules},
    )


@router.post("/media-buyers/create")
def create_media_buyer(
    partner_id: int = Form(...), display_name: str = Form(...),
    callgrid_sub_id: int = Form(...), callgrid_raw_name: str = Form(...),
    mb_percentage_bps: int = Form(3000),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    mb = MediaBuyer(partner_id=partner_id, display_name=display_name)
    db.add(mb)
    db.flush()

    source = db.query(CallgridSource).filter_by(callgrid_sub_id=callgrid_sub_id).one_or_none()
    if source is None:
        source = CallgridSource(callgrid_sub_id=callgrid_sub_id, callgrid_source_raw_name=callgrid_raw_name)
        db.add(source)
        db.flush()

    db.add(MediaBuyerSourceMapping(media_buyer_id=mb.id, callgrid_source_id=source.id, effective_from=_this_monday()))
    db.add(CompensationRule(media_buyer_id=mb.id, mb_percentage_bps=mb_percentage_bps,
                             effective_from=_this_monday(), created_by_user_id=user.id))
    db.commit()
    return RedirectResponse(url="/admin/media-buyers", status_code=303)


@router.post("/media-buyers/{media_buyer_id}/rate")
def change_compensation_rate(
    media_buyer_id: int, mb_percentage_bps: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """effective_from siempre es el próximo lunes — nunca corta una semana a la mitad."""
    next_monday = _this_monday() + timedelta(days=7)
    current = engine.get_effective_compensation_rule(db, media_buyer_id, next_monday)
    if current is not None:
        current.effective_to = next_monday
    db.add(CompensationRule(media_buyer_id=media_buyer_id, mb_percentage_bps=mb_percentage_bps,
                             effective_from=next_monday, created_by_user_id=user.id))
    db.commit()
    return RedirectResponse(url="/admin/media-buyers", status_code=303)


# ── Ad spend ──────────────────────────────────────────────────────────────────

@router.get("/ad-spend")
def ad_spend_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    media_buyers = db.query(MediaBuyer).all()
    recent = db.query(AdSpend).filter_by(is_current=True).order_by(AdSpend.spend_date.desc()).limit(50).all()
    return templates.TemplateResponse(
        request, "admin/ad_spend.html",
        {"user": user, "media_buyers": media_buyers, "recent": recent, "money": money},
    )


@router.post("/ad-spend/create")
def ad_spend_create(
    media_buyer_id: int = Form(...), spend_date: date = Form(...), amount: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    ManualAdSpendProvider().record_spend(
        db, media_buyer_id=media_buyer_id, spend_date=spend_date,
        amount_cents=round(amount * 100), actor_user_id=user.id, notes=notes or None,
    )
    return RedirectResponse(url="/admin/ad-spend", status_code=303)


# ── Liquidaciones ─────────────────────────────────────────────────────────────

@router.get("/settlements")
def settlements_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    media_buyers = db.query(MediaBuyer).all()
    settlements = db.query(Settlement).order_by(Settlement.period_start.desc()).limit(50).all()
    return templates.TemplateResponse(
        request, "admin/settlements.html",
        {"user": user, "media_buyers": media_buyers, "settlements": settlements, "money": money,
         "this_monday": _this_monday()},
    )


@router.post("/settlements/compute")
def settlements_compute(
    media_buyer_id: int = Form(...), period_start: date = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    error = None
    try:
        engine.compute_settlement(db, media_buyer_id, period_start, actor_user_id=user.id)
    except engine.SettlementEngineError as exc:
        error = str(exc)
    return RedirectResponse(url=f"/admin/settlements{'?error=' + error if error else ''}", status_code=303)


@router.post("/settlements/{settlement_id}/approve")
def settlements_approve(settlement_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    engine.approve_settlement(db, settlement_id, actor_user_id=user.id)
    return RedirectResponse(url="/admin/settlements", status_code=303)


@router.post("/settlements/{settlement_id}/pay")
def settlements_pay(settlement_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    engine.mark_paid(db, settlement_id, actor_user_id=user.id)
    return RedirectResponse(url="/admin/settlements", status_code=303)


@router.post("/settlements/{settlement_id}/lock")
def settlements_lock(settlement_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    engine.lock_settlement(db, settlement_id, actor_user_id=user.id)
    return RedirectResponse(url="/admin/settlements", status_code=303)


# ── Ajustes financieros ───────────────────────────────────────────────────────

@router.post("/adjustments/create")
def adjustments_create(
    media_buyer_id: int = Form(...), adjustment_type: str = Form(...),
    amount: float = Form(...), reason: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    from app.models import AdjustmentStatus, AdjustmentType
    db.add(FinancialAdjustment(
        media_buyer_id=media_buyer_id, adjustment_type=AdjustmentType(adjustment_type),
        amount_cents=round(amount * 100), reason=reason, status=AdjustmentStatus.PENDING,
        created_by_user_id=user.id,
    ))
    db.commit()
    return RedirectResponse(url="/admin/settlements", status_code=303)


# ── Usuarios ──────────────────────────────────────────────────────────────────

@router.get("/users")
def users_list(request: Request, error: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    users = db.query(User).order_by(User.id).all()
    partners = db.query(Partner).all()
    media_buyers = db.query(MediaBuyer).all()
    return templates.TemplateResponse(
        request, "admin/users.html",
        {"user": user, "users": users, "partners": partners, "media_buyers": media_buyers,
         "roles": list(UserRole), "error": error or None},
    )


@router.post("/users/create")
def users_create(
    email: str = Form(...), password: str = Form(...), role: str = Form(...),
    partner_id: str = Form(""), media_buyer_id: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    email = email.strip().lower()
    if db.query(User).filter_by(email=email).one_or_none() is not None:
        return RedirectResponse(url="/admin/users?error=Ya+existe+un+usuario+con+ese+email", status_code=303)

    try:
        hashed = hash_password(password)
    except ValueError as exc:
        return RedirectResponse(url=f"/admin/users?error={exc}", status_code=303)

    new_user = User(
        email=email,
        hashed_password=hashed,
        role=UserRole(role),
        partner_id=int(partner_id) if partner_id else None,
        media_buyer_id=int(media_buyer_id) if media_buyer_id else None,
    )
    db.add(new_user)
    db.flush()
    write_audit_log(db, actor_user_id=user.id, entity_type="user", entity_id=new_user.id,
                     action="created", before_state=None, after_state={"email": new_user.email, "role": role})
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/deactivate")
def users_deactivate(user_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    target = db.get(User, user_id)
    if target is not None and target.is_active:
        target.is_active = False
        write_audit_log(db, actor_user_id=user.id, entity_type="user", entity_id=target.id,
                         action="deactivated", before_state={"is_active": True}, after_state={"is_active": False})
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/reactivate")
def users_reactivate(user_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    target = db.get(User, user_id)
    if target is not None and not target.is_active:
        target.is_active = True
        write_audit_log(db, actor_user_id=user.id, entity_type="user", entity_id=target.id,
                         action="reactivated", before_state={"is_active": False}, after_state={"is_active": True})
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


# ── Auditoría ─────────────────────────────────────────────────────────────────

@router.get("/audit-log")
def audit_log(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    logs = db.query(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "admin/audit_log.html", {"user": user, "logs": logs})

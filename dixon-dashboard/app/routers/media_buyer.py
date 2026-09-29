from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_role
from app.db import get_db
from app.models import MediaBuyer, Settlement, SettlementStatus, User, UserRole
from app.services.formatting import money
from app.services.settlement_engine import get_current_deficit_cents

router = APIRouter(prefix="/mb", dependencies=[Depends(require_role(UserRole.MEDIA_BUYER))])
templates = Jinja2Templates(directory="app/templates")


def _this_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today - timedelta(days=today.weekday())


def _own_media_buyer(db: Session, user: User) -> MediaBuyer:
    if not user.media_buyer_id:
        raise HTTPException(status_code=403, detail="Este usuario no tiene un media buyer asociado")
    mb = db.get(MediaBuyer, user.media_buyer_id)
    if mb is None:
        raise HTTPException(status_code=404, detail="Media buyer no encontrado")
    return mb


@router.get("")
def overview(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    mb = _own_media_buyer(db, user)
    week_start = _this_monday()

    current = (
        db.query(Settlement)
        .filter_by(media_buyer_id=mb.id, period_start=week_start)
        .one_or_none()
    )
    deficit_cents = get_current_deficit_cents(db, mb.id)

    history = (
        db.query(Settlement)
        .filter(Settlement.media_buyer_id == mb.id, Settlement.status != SettlementStatus.OPEN)
        .order_by(Settlement.period_start.desc())
        .limit(12)
        .all()
    )

    waterfall = None
    if current is not None:
        waterfall = {
            "gross_payout": money(current.gross_payout_cents),
            "ad_spend": money(-current.ad_spend_cents),
            "incoming_deficit": money(-current.incoming_deficit_cents),
            "adjustment": money(current.post_settlement_adjustment_cents),
            "net_profit": money(current.net_profit_cents),
            "share_pct": f"{current.mb_percentage_bps_snapshot / 100:.1f}%",
            "your_earnings": money(current.mb_earnings_cents),
            "no_profit": current.net_profit_cents <= 0,
            "new_deficit": money(current.outgoing_deficit_cents),
        }

    return templates.TemplateResponse(
        request, "media_buyer/overview.html",
        {
            "user": user, "media_buyer": mb, "waterfall": waterfall,
            "current_deficit": money(deficit_cents), "history": history, "money": money,
            "week_start": week_start,
        },
    )

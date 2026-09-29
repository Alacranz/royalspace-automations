from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_role
from app.db import get_db
from app.models import MediaBuyer, Settlement, User, UserRole
from app.services.formatting import money

router = APIRouter(prefix="/dixon", dependencies=[Depends(require_role(UserRole.DIXON_MANAGER, UserRole.ROYALSPACE_ADMIN))])
templates = Jinja2Templates(directory="app/templates")


def _this_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today - timedelta(days=today.weekday())


@router.get("")
def overview(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    partner_id = user.partner_id
    media_buyers = db.query(MediaBuyer).filter_by(partner_id=partner_id, is_active=True).all() if partner_id else []
    week_start = _this_monday()

    rows = []
    totals = {"payout": 0, "ad_spend": 0, "net": 0, "mb": 0, "dixon": 0}
    for mb in media_buyers:
        s = db.query(Settlement).filter_by(media_buyer_id=mb.id, period_start=week_start).one_or_none()
        rows.append({
            "media_buyer": mb,
            "payout": money(s.gross_payout_cents) if s else "—",
            "ad_spend": money(s.ad_spend_cents) if s else "—",
            "net": money(s.net_profit_cents) if s else "—",
            "mb_earnings": money(s.mb_earnings_cents) if s else "—",
            "dixon_earnings": money(s.dixon_earnings_cents) if s else "—",
        })
        if s:
            totals["payout"] += s.gross_payout_cents
            totals["ad_spend"] += s.ad_spend_cents
            totals["net"] += s.net_profit_cents
            totals["mb"] += s.mb_earnings_cents
            totals["dixon"] += s.dixon_earnings_cents

    return templates.TemplateResponse(
        request, "dixon/overview.html",
        {"user": user, "rows": rows, "week_start": week_start,
         "totals": {k: money(v) for k, v in totals.items()}},
    )

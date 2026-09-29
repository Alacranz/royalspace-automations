from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import MediaBuyer, User, UserRole
from app.services.ad_spend import MetaAdSpendProvider


def _system_actor_id(db: Session) -> int:
    """Usa el primer ROYALSPACE_ADMIN como actor para las filas de auditoría
    generadas por el sync automático (no hay un usuario humano detrás)."""
    admin = db.query(User).filter_by(role=UserRole.ROYALSPACE_ADMIN).first()
    return admin.id if admin else 1


def sync_day(db: Session, day: date) -> dict:
    """Sincroniza el ad spend de Meta para todos los media buyers que tengan
    meta_ad_account_id configurado, para un día específico. Retorna un resumen."""
    actor_id = _system_actor_id(db)
    provider = MetaAdSpendProvider()

    media_buyers = db.query(MediaBuyer).filter(
        MediaBuyer.is_active.is_(True), MediaBuyer.meta_ad_account_id.isnot(None)
    ).all()

    synced, errors = 0, []
    for mb in media_buyers:
        try:
            provider.sync_media_buyer_day(
                db, media_buyer_id=mb.id, ad_account_id=mb.meta_ad_account_id,
                spend_date=day, actor_user_id=actor_id,
            )
            synced += 1
        except Exception as exc:
            errors.append(f"{mb.display_name} ({day}): {exc}")

    return {"date": day, "synced": synced, "errors": errors}


def nightly_resync(db: Session, days_back: int = 7) -> list[dict]:
    """Mismo criterio que callgrid/sync.py::nightly_resync — Meta también puede
    ajustar el spend reportado de días recientes (atribución tardía)."""
    from app.config import META_ACCESS_TOKEN

    if not META_ACCESS_TOKEN:
        return []  # feature opcional — sin token no hay nada que sincronizar

    today = date.today()
    return [sync_day(db, today - timedelta(days=i)) for i in range(days_back)]

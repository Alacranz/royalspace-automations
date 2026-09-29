"""
Ad Spend — versionado (nunca se muta en el lugar) + abstracción de proveedor.

ManualAdSpendProvider es lo único activo en Fase 1. MetaAdSpendProvider (Fase 3,
importación automática desde Meta Marketing API) implementará la misma interfaz
sin tocar el motor de liquidaciones.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from sqlalchemy.orm import Session

from app.models import AdSpend
from app.services.audit import write_audit_log


class AdSpendProvider(ABC):
    @abstractmethod
    def record_spend(self, db: Session, *, media_buyer_id: int, spend_date: date, amount_cents: int,
                      actor_user_id: int, notes: str | None = None) -> AdSpend:
        raise NotImplementedError


class ManualAdSpendProvider(AdSpendProvider):
    """Un ROYALSPACE_ADMIN o DIXON_MANAGER registra el gasto a mano. Nunca un MEDIA_BUYER."""

    def record_spend(self, db: Session, *, media_buyer_id: int, spend_date: date, amount_cents: int,
                      actor_user_id: int, notes: str | None = None) -> AdSpend:
        existing = (
            db.query(AdSpend)
            .filter_by(media_buyer_id=media_buyer_id, spend_date=spend_date, is_current=True)
            .one_or_none()
        )
        new_row = AdSpend(
            media_buyer_id=media_buyer_id,
            spend_date=spend_date,
            amount_cents=amount_cents,
            notes=notes,
            entered_by_user_id=actor_user_id,
            is_current=True,
        )
        db.add(new_row)
        db.flush()

        if existing is not None:
            existing.is_current = False
            existing.superseded_by_id = new_row.id
            write_audit_log(
                db, actor_user_id=actor_user_id, entity_type="ad_spend", entity_id=new_row.id,
                action="edited",
                before_state={"amount_cents": existing.amount_cents},
                after_state={"amount_cents": amount_cents},
            )
        else:
            write_audit_log(
                db, actor_user_id=actor_user_id, entity_type="ad_spend", entity_id=new_row.id,
                action="created", before_state=None, after_state={"amount_cents": amount_cents},
            )

        db.commit()
        return new_row


def delete_ad_spend(db: Session, ad_spend_id: int, actor_user_id: int) -> None:
    """"Eliminar" = marcar is_current=False, nunca DELETE físico — se conserva para auditoría."""
    row = db.get(AdSpend, ad_spend_id)
    if row is None or not row.is_current:
        raise ValueError("Fila de ad spend no encontrada o ya no vigente")
    row.is_current = False
    write_audit_log(db, actor_user_id=actor_user_id, entity_type="ad_spend", entity_id=row.id,
                     action="deleted", before_state={"amount_cents": row.amount_cents}, after_state=None)
    db.commit()

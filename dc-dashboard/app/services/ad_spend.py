"""
Ad Spend — versionado (nunca se muta en el lugar) + abstracción de proveedor.

ManualAdSpendProvider y MetaAdSpendProvider implementan la misma interfaz —
el motor de liquidaciones (settlement_engine.py) no sabe ni le importa de
dónde vino cada fila de ad_spend, solo lee is_current=True.
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


class MetaAdSpendProvider(AdSpendProvider):
    """
    Importa el gasto desde Meta Ads Graph API para media buyers con
    MediaBuyer.meta_ad_account_id seteado. Reusa la misma lógica de
    versionado que ManualAdSpendProvider — un admin sigue pudiendo corregir
    manualmente un día específico después (queda como nueva versión "current",
    auditada, sin perder el valor que vino de Meta).
    """

    def record_spend(self, db: Session, *, media_buyer_id: int, spend_date: date, amount_cents: int,
                      actor_user_id: int, notes: str | None = None) -> AdSpend:
        return ManualAdSpendProvider().record_spend(
            db, media_buyer_id=media_buyer_id, spend_date=spend_date, amount_cents=amount_cents,
            actor_user_id=actor_user_id, notes=notes or "Sincronizado automáticamente desde Meta Ads",
        )

    def sync_media_buyer_day(self, db: Session, *, media_buyer_id: int, ad_account_id: str,
                              spend_date: date, actor_user_id: int) -> AdSpend:
        from app.config import META_ACCESS_TOKEN, META_API_VERSION
        from app.meta.client import get_ad_spend

        amount_cents = get_ad_spend(META_ACCESS_TOKEN, META_API_VERSION, ad_account_id, spend_date)
        return self.record_spend(
            db, media_buyer_id=media_buyer_id, spend_date=spend_date, amount_cents=amount_cents,
            actor_user_id=actor_user_id,
        )


def delete_ad_spend(db: Session, ad_spend_id: int, actor_user_id: int) -> None:
    """"Eliminar" = marcar is_current=False, nunca DELETE físico — se conserva para auditoría."""
    row = db.get(AdSpend, ad_spend_id)
    if row is None or not row.is_current:
        raise ValueError("Fila de ad spend no encontrada o ya no vigente")
    row.is_current = False
    write_audit_log(db, actor_user_id=actor_user_id, entity_type="ad_spend", entity_id=row.id,
                     action="deleted", before_state={"amount_cents": row.amount_cents}, after_state=None)
    db.commit()

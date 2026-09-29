from __future__ import annotations

from collections.abc import Sequence

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.security import SESSION_COOKIE_NAME, read_session_token
from app.db import get_db
from app.models import User, UserRole


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user_id = read_session_token(token)
    if user_id is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_role(*allowed_roles: UserRole):
    """Dependencia FastAPI — 403 si el usuario actual no tiene uno de estos roles.
    Nunca confiar solo en ocultar botones en la plantilla: esto se aplica en cada ruta."""

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="No tienes permiso para ver esto")
        return user

    return _dependency


def scoped_media_buyer_ids(db: Session, user: User) -> Sequence[int] | None:
    """
    Devuelve la lista de media_buyer_id que este usuario puede ver, o None si
    puede ver TODOS (ROYALSPACE_ADMIN). Usar en cada query de datos financieros
    — nunca confiar en la UI para limitar qué ve cada rol.
    """
    if user.role == UserRole.ROYALSPACE_ADMIN:
        return None
    if user.role == UserRole.DIXON_MANAGER:
        from app.models import MediaBuyer  # import local para evitar ciclo
        rows = db.query(MediaBuyer.id).filter(MediaBuyer.partner_id == user.partner_id).all()
        return [r[0] for r in rows]
    if user.role == UserRole.MEDIA_BUYER:
        return [user.media_buyer_id] if user.media_buyer_id else []
    return []

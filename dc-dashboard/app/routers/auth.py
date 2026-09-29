from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth.security import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, create_session_token, verify_password
from app.db import get_db
from app.models import User, UserRole

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _home_for_role(role: UserRole) -> str:
    return {
        UserRole.ROYALSPACE_ADMIN: "/admin",
        UserRole.DIXON_MANAGER: "/partner",
        UserRole.MEDIA_BUYER: "/mb",
    }[role]


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=email.strip().lower()).one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request, "login.html", {"error": "Email o contraseña incorrectos"}, status_code=401)

    token = create_session_token(user.id)
    response = RedirectResponse(url=_home_for_role(user.role), status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, token, max_age=SESSION_MAX_AGE_SECONDS, httponly=True, samesite="lax")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

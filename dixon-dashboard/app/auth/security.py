from __future__ import annotations

import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import SESSION_SECRET_KEY

_serializer = URLSafeTimedSerializer(SESSION_SECRET_KEY, salt="dixon-dashboard-session")

SESSION_COOKIE_NAME = "dixon_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 14  # 14 días

# bcrypt trunca a 72 bytes — se valida explícito en vez de dejar que falle silencioso.
_MAX_PASSWORD_BYTES = 72


def hash_password(plain_password: str) -> str:
    raw = plain_password.encode("utf-8")
    if len(raw) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"La contraseña no puede superar {_MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_session_token(user_id: int) -> str:
    return _serializer.dumps({"user_id": user_id})


def read_session_token(token: str) -> int | None:
    """Devuelve el user_id si el token es válido y no expiró, o None."""
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return None
    return data.get("user_id")

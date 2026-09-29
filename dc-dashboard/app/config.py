"""
Configuración — DC Dashboard

Convención heredada de royalspace-automations: secrets requeridos via
os.environ["X"] (KeyError si falta), opcionales via os.environ.get("X", default).
"""
from __future__ import annotations

import os


def _require_latin1(name: str, value: str) -> str:
    """CALLGRID_API_KEY/ORG_ID viajan en headers HTTP (latin-1 estricto). Fallar
    acá con un mensaje claro evita el UnicodeEncodeError críptico que sale si
    el valor quedó truncado al copiarlo (ej. con '…' en vez del texto completo)."""
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{name} contiene un carácter inválido ({value[exc.start:exc.end]!r}) — "
            f"revisa que lo hayas copiado completo, no una versión truncada/visual."
        ) from exc
    return value


CALLGRID_API_KEY = _require_latin1("CALLGRID_API_KEY", os.environ["CALLGRID_API_KEY"])
CALLGRID_ORG_ID  = _require_latin1("CALLGRID_ORG_ID", os.environ["CALLGRID_ORG_ID"])

DATABASE_URL = os.environ["DATABASE_URL"]

SESSION_SECRET_KEY = os.environ["SESSION_SECRET_KEY"]

# Bootstrap del primer admin — opcionales porque solo se usan si no hay
# ningún ROYALSPACE_ADMIN todavía (ver scripts/seed.py y app/main.py startup).
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

REPORTING_TIMEZONE = os.environ.get("REPORTING_TIMEZONE") or "America/New_York"

# Partner conocido en CallGrid (ver CLAUDE.md / plan).
PARTNER_NAME           = "Dixon Colmenares"
PARTNER_VENDOR_SUB_ID  = 40

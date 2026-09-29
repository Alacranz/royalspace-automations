"""
Configuración — DC Dashboard

Convención heredada de royalspace-automations: secrets requeridos via
os.environ["X"] (KeyError si falta), opcionales via os.environ.get("X", default).
"""
from __future__ import annotations

import os

CALLGRID_API_KEY = os.environ["CALLGRID_API_KEY"]
CALLGRID_ORG_ID  = os.environ["CALLGRID_ORG_ID"]

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

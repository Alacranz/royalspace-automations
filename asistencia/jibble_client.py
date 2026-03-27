"""
Jibble API client — utilidades compartidas
Royalspace 2026
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytz
import requests

# ── Constantes ───────────────────────────────────────────────────────────────
JIBBLE_TOKEN_URL       = "https://identity.prod.jibble.io/connect/token"
JIBBLE_PEOPLE_URL      = "https://workspace.prod.jibble.io/v1/People"
JIBBLE_ENTRIES_URL     = "https://time-tracking.prod.jibble.io/v1/TimeEntries"

# Venezuela Standard Time — UTC-4, sin horario de verano
VET = pytz.timezone("America/Caracas")

# Rango razonable de entrada (para resolver ambigüedad UTC vs local)
REASONABLE_START = (6, 0)   # 06:00
REASONABLE_END   = (13, 0)  # 13:00


# ── Autenticación ─────────────────────────────────────────────────────────────
def get_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        JIBBLE_TOKEN_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── People ────────────────────────────────────────────────────────────────────
def get_people(token: str, org_id: str) -> list:
    resp = requests.get(
        f"{JIBBLE_PEOPLE_URL}?organizationId={org_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── Time Entries ──────────────────────────────────────────────────────────────
def get_time_entries_page(token: str, skip: int, top: int, orderby: str = "createdAt desc") -> list:
    resp = requests.get(
        JIBBLE_ENTRIES_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"$orderby": orderby, "$skip": skip, "$top": top},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("value", [])


# ── Parseo de fechas ──────────────────────────────────────────────────────────
def parse_created_at(s: str) -> Optional[datetime]:
    """
    Parsea el campo createdAt de Jibble.
    Formato esperado: "MM/dd/yyyy HH:mm:ss" (UTC implícito en el servidor Jibble).
    Retorna un datetime aware en UTC.
    """
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        # ISO con offset/Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def _in_reasonable_range(dt: datetime) -> bool:
    """True si la hora de dt cae en el rango razonable de entrada (06:00-13:00)."""
    t = (dt.hour, dt.minute)
    return REASONABLE_START <= t <= REASONABLE_END


def parse_time_smart(s: str, prefer_utc_fallback: bool = False) -> Optional[datetime]:
    """
    Parsea el campo `time` de una TimeEntry de Jibble a un datetime aware en VET.

    Jibble puede devolver este campo como UTC o ya en hora local según el contexto.
    Se prueban ambas interpretaciones y se elige la que cae en el rango razonable
    de entrada (06:00-13:00 Caracas):

      A) Interpretar como UTC → convertir a Caracas (VET)
      B) Interpretar como ya Caracas (sin convertir)

    Si solo A cae en rango → retorna A.
    Si solo B cae en rango → retorna B.
    Si ambas o ninguna (empate):
      - prefer_utc_fallback=False  → retorna B  (comportamiento del reporte diario)
      - prefer_utc_fallback=True   → retorna A  (comportamiento del reporte mensual)
    """
    if not s or not s.strip():
        return None
    s = s.strip()
    try:
        # ISO con timezone explícito (Z u offset) → conversión directa, sin ambigüedad
        if s.endswith("Z") or (len(s) > 6 and s[-6] in "+-" and s[-3] == ":"):
            normalized = s[:-1] + "+00:00" if s.endswith("Z") else s
            return datetime.fromisoformat(normalized).astimezone(VET)

        dt_naive = datetime.fromisoformat(s)

        # Opción A: tratar como UTC → Caracas
        a = dt_naive.replace(tzinfo=timezone.utc).astimezone(VET)

        # Opción B: tratar como ya Caracas
        b = VET.localize(dt_naive)

        a_ok = _in_reasonable_range(a)
        b_ok = _in_reasonable_range(b)

        if a_ok and not b_ok:
            return a
        if b_ok and not a_ok:
            return b

        # Empate: usa la preferencia del llamador
        return a if prefer_utc_fallback else b

    except (ValueError, AttributeError, OverflowError):
        return None

#!/usr/bin/env python3
"""
Reporte diario de asistencia — Jibble → Discord
Royalspace 2026 | Port de jibble_asistencia_1030.ps1 v1.5.5

Lógica replicada exactamente:
  - Solo entradas de tipo "In" del día actual en hora Caracas (VET)
  - Primera marca por persona (la más temprana)
  - Clasificación: A TIEMPO 08:30-09:15 | TARDE 09:16-11:00 | FUERA DE RANGO resto
  - Delta en minutos respecto a 09:00 (floor, sin segundos)
  - Corte de paginación por createdAt < inicio del día
  - Empate UTC/local → prefiere hora directa (sin convertir)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# Asegurar que Python encuentre jibble_client en el mismo directorio
sys.path.insert(0, os.path.dirname(__file__))
from jibble_client import (
    VET,
    get_people,
    get_time_entries_page,
    get_token,
    parse_created_at,
    parse_time_smart,
)

# ── Configuración (desde variables de entorno / GitHub Secrets) ───────────────
CLIENT_ID       = os.environ["JIBBLE_CLIENT_ID"]
CLIENT_SECRET   = os.environ["JIBBLE_CLIENT_SECRET"]
ORG_ID          = os.environ["JIBBLE_ORG_ID"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_ASISTENCIA"]

# Ventanas horarias (hora, minuto) — Caracas
ON_TIME_START = (8, 30)
ON_TIME_END   = (9, 15)
LATE_START    = (9, 16)
LATE_END      = (11, 0)
BASE_TIME     = (9, 0)   # referencia para delta

# Excluidos (preferredName, minúsculas)
EXCLUDE_NAMES = {"edwar"}

PAGE_SIZE = 200
MAX_PAGES = 80


# ── Discord ───────────────────────────────────────────────────────────────────
def send_discord(title: str, body: str) -> None:
    resp = requests.post(
        DISCORD_WEBHOOK,
        json={"content": f"**{title}**\n{body}"},
        timeout=15,
    )
    resp.raise_for_status()


# ── Clasificación ─────────────────────────────────────────────────────────────
def classify(hour: int, minute: int) -> str:
    t = (hour, minute)
    if ON_TIME_START <= t <= ON_TIME_END:
        return "A TIEMPO"
    if LATE_START <= t <= LATE_END:
        return "TARDE"
    return "FUERA DE RANGO"


def minutes_delta(hour: int, minute: int) -> int:
    """Delta en minutos (floor, sin segundos) respecto a BASE_TIME (09:00)."""
    return hour * 60 + minute - (BASE_TIME[0] * 60 + BASE_TIME[1])


def fmt_delta(delta: int) -> str:
    return f"(+{delta} min)" if delta >= 0 else f"({delta} min)"


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    now_caracas = datetime.now(timezone.utc).astimezone(VET)
    today_start = now_caracas.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)

    token = get_token(CLIENT_ID, CLIENT_SECRET)
    print("Token OK")

    is_saturday = now_caracas.weekday() == 5
    saturday_workers = {"clara"}

    all_people = [
        p for p in get_people(token, ORG_ID)
        if p.get("status") == "Joined"
        and (p.get("preferredName") or "").strip().lower() not in EXCLUDE_NAMES
    ]
    # Sábados: solo quienes trabajan ese día
    people = [
        p for p in all_people
        if not is_saturday
        or (p.get("preferredName") or "").strip().lower() in saturday_workers
    ]
    print(f"Personas activas: {len(people)}{' (sábado — solo Clara)' if is_saturday else ''}")

    # personId → primera marca "In" del día (datetime VET)
    first_in: dict[str, datetime] = {}

    stop = False
    for page in range(MAX_PAGES):
        if stop:
            break

        items = get_time_entries_page(token, page * PAGE_SIZE, PAGE_SIZE, "createdAt desc")
        if not items:
            break

        for e in items:
            # Corte de paginación: cuando createdAt < inicio del día en Caracas
            created = parse_created_at(str(e.get("createdAt", "")))
            if created:
                created_caracas = created.astimezone(VET)
                if created_caracas < today_start:
                    stop = True
                    break

            if str(e.get("type", "")) != "In":
                continue

            # prefer_utc_fallback=False → empate: usar hora directa (igual que el .ps1 diario)
            t = parse_time_smart(str(e.get("time", "")), prefer_utc_fallback=False)
            if not t:
                continue

            # Solo entradas de hoy
            if t < today_start or t >= today_end:
                continue

            pid = str(e.get("personId", ""))
            if pid not in first_in or t < first_in[pid]:
                first_in[pid] = t

    print(f"Entradas encontradas: {len(first_in)}")

    # ── Clasificar por persona ────────────────────────────────────────────────
    on_time: list[tuple[datetime, str]] = []
    late:    list[tuple[datetime, str]] = []
    out:     list[tuple[datetime, str]] = []
    missing: list[str] = []

    for p in people:
        pid  = str(p.get("id", ""))
        name = p.get("preferredName", "?")

        if pid in first_in:
            cin    = first_in[pid]
            bucket = classify(cin.hour, cin.minute)
            delta  = minutes_delta(cin.hour, cin.minute)
            line   = f"{name} - {cin.strftime('%H:%M')}  {fmt_delta(delta)}"

            if bucket == "A TIEMPO":
                on_time.append((cin, line))
            elif bucket == "TARDE":
                late.append((cin, line))
            else:
                out.append((cin, line))
        else:
            missing.append(f"{name} - Sin entrada registrada")

    # Ordenar cada sección por hora de entrada
    on_time.sort(key=lambda x: x[0])
    late.sort(key=lambda x: x[0])
    out.sort(key=lambda x: x[0])

    def lines(bucket: list[tuple[datetime, str]]) -> str:
        return "\n".join(l for _, l in bucket) if bucket else "Ninguno"

    # ── Mensaje Discord ───────────────────────────────────────────────────────
    body  = f"Fecha: {now_caracas.strftime('%d-%m-%Y')}\n"
    body += f"Generado: {now_caracas.strftime('%H:%M')} (24h)\n\n"
    body += f"A TIEMPO:\n{lines(on_time)}\n\n"
    body += f"TARDE:\n{lines(late)}\n\n"
    body += f"FUERA DE RANGO:\n{lines(out)}\n\n"
    body += f"SIN MARCAR:\n" + ("\n".join(missing) if missing else "Ninguno")

    send_discord("[ASISTENCIA] Entrada diaria", body)
    print("Mensaje enviado a Discord. OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            send_discord("[ASISTENCIA] ERROR", f"Reporte diario falló: {exc}")
        except Exception:
            pass
        sys.exit(1)

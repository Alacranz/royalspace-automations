#!/usr/bin/env python3
"""
Reporte diario de asistencia — Jibble → Discord
Royalspace 2026 | Port de jibble_asistencia_1030.ps1 v1.5.5

Lógica replicada exactamente:
  - Solo entradas de tipo "In" del día actual en hora Caracas (VET)
  - Primera marca por persona (la más temprana)
  - Clasificación: A TIEMPO 07:00-09:15 (09:10 desde 2026-09-30) | TARDE resto
    de la hora hasta 11:00 | FUERA DE RANGO el resto — ver on_time_end_for()
  - Sábados: solo SATURDAY_WORKERS (Clara y Edixon desde 2026-10-17)
  - Clara entra lun 12:00 / mié 11:15 / jue 12:00 desde 2026-10-13 (clases) —
    ver entry_time_for(); si al correr el reporte aún no le toca, sale en
    "ENTRA MÁS TARDE" en vez de "SIN MARCAR"
  - Delta en minutos respecto a 09:00 (floor, sin segundos)
  - Corte de paginación por createdAt < inicio del día
  - Empate UTC/local → prefiere hora directa (sin convertir)
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
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
# ON_TIME_START ampliado de 8:30 a 7:00 (2026-09-15): llegar temprano no
# debería marcarse como "FUERA DE RANGO" igual que llegar tarde.
ON_TIME_START = (7, 0)
BASE_TIME     = (9, 0)   # hora de entrada general (referencia para delta)
LATE_WINDOW_MIN = 120    # TARDE hasta 2h después de la hora de entrada (9:00 → 11:00)

# La tolerancia de "A TIEMPO" se endurece de 15 a 10 min a partir del 2026-09-30
# (decisión 2026-09-29 — el reporte del 29 ya corrió con la regla vieja, así
# que el cambio se ata a la fecha, no al momento del deploy, para que no
# importe si este código se despliega antes o después de que corra el
# reporte del día 29).
_ON_TIME_TIGHTEN_DATE = date(2026, 9, 30)

# Horario de entrada de Clara L-V por sus clases, vigente desde el martes
# 2026-10-13 (decisión 2026-10-28, retroactiva para que octubre se penalice
# según este horario). weekday → hora de entrada. Martes, viernes y sábados:
# 9:00 como todos. Debe coincidir con monthly_report.py.
_CLARA_SCHEDULE_SINCE = date(2026, 10, 13)
_CLARA_ENTRY = {0: (12, 0), 2: (11, 15), 3: (12, 0)}   # lun, mié, jue


def grace_minutes_for(d: date) -> int:
    return 10 if d >= _ON_TIME_TIGHTEN_DATE else 15


def entry_time_for(name: str, d: date) -> tuple[int, int]:
    if "clara" in name.strip().lower() and d >= _CLARA_SCHEDULE_SINCE:
        return _CLARA_ENTRY.get(d.weekday(), BASE_TIME)
    return BASE_TIME

# Sábados: solo Clara y Edixon Cordova, desde el 2026-10-17 (decisión
# 2026-10-12; entre 2026-08-01 y esa fecha nadie trabajaba sábados).
# Substring match sobre preferredName en minúsculas.
SATURDAY_WORKERS = {"clara", "edixon", "cordova"}
_SATURDAY_WORKERS_SINCE = date(2026, 10, 17)


def saturday_workers_for(d: date) -> set[str]:
    return SATURDAY_WORKERS if d >= _SATURDAY_WORKERS_SINCE else set()


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
def classify(hour: int, minute: int, name: str, d: date) -> str:
    t     = hour * 60 + minute
    entry = entry_time_for(name, d)
    start = entry[0] * 60 + entry[1]
    if t < ON_TIME_START[0] * 60 + ON_TIME_START[1]:
        return "FUERA DE RANGO"
    if t <= start + grace_minutes_for(d):
        return "A TIEMPO"
    if t <= start + LATE_WINDOW_MIN:
        return "TARDE"
    return "FUERA DE RANGO"


def minutes_delta(hour: int, minute: int, name: str, d: date) -> int:
    """Delta en minutos (floor, sin segundos) respecto a su hora de entrada."""
    entry = entry_time_for(name, d)
    return hour * 60 + minute - (entry[0] * 60 + entry[1])


def fmt_delta(delta: int) -> str:
    return f"(+{delta} min)" if delta >= 0 else f"({delta} min)"


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    now_caracas = datetime.now(timezone.utc).astimezone(VET)
    today_start = now_caracas.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    today       = now_caracas.date()
    print(f"Regla de hoy ({today}): A TIEMPO hasta {grace_minutes_for(today)} min después de la hora de entrada")

    token = get_token(CLIENT_ID, CLIENT_SECRET)
    print("Token OK")

    is_saturday = now_caracas.weekday() == 5
    saturday_workers = saturday_workers_for(now_caracas.date())

    if is_saturday and not saturday_workers:
        print("Sábado — no hay reporte de asistencia.")
        return

    all_people = [
        p for p in get_people(token, ORG_ID)
        if p.get("status") == "Joined"
        and (p.get("preferredName") or "").strip().lower() not in EXCLUDE_NAMES
    ]
    # Sábados: solo quienes trabajan ese día
    people = [
        p for p in all_people
        if not is_saturday
        or any(kw in (p.get("preferredName") or "").strip().lower() for kw in saturday_workers)
    ]
    print(f"Personas activas: {len(people)}{' (sábado — solo Clara y Edixon)' if is_saturday else ''}")

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
    later:   list[str] = []   # aún no le toca entrar (ej. Clara los días de clase)
    now_min = now_caracas.hour * 60 + now_caracas.minute

    for p in people:
        pid  = str(p.get("id", ""))
        name = p.get("preferredName", "?")

        if pid in first_in:
            cin    = first_in[pid]
            bucket = classify(cin.hour, cin.minute, name, today)
            delta  = minutes_delta(cin.hour, cin.minute, name, today)
            line   = f"{name} - {cin.strftime('%H:%M')}  {fmt_delta(delta)}"
            entry  = entry_time_for(name, today)
            if entry != BASE_TIME:
                line += f"  [entrada {entry[0]:02d}:{entry[1]:02d}]"

            if bucket == "A TIEMPO":
                on_time.append((cin, line))
            elif bucket == "TARDE":
                late.append((cin, line))
            else:
                out.append((cin, line))
        else:
            entry = entry_time_for(name, today)
            if entry[0] * 60 + entry[1] + grace_minutes_for(today) > now_min:
                later.append(f"{name} - Entra a las {entry[0]:02d}:{entry[1]:02d}")
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
    if later:
        body += "\n\nENTRA MÁS TARDE (horario especial):\n" + "\n".join(later)

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

#!/usr/bin/env python3
"""
ZIP Code Report — Royalspace 2026

Genera un ranking de zip codes por revenue para el canal de Discord.
Dos modos:
  --weekly  : semana anterior (lun–dom)
  --monthly : mes anterior completo

Formato: # | Zip | Ciudad, Estado
Ordenado por revenue descendente, sin mostrar cantidades.
Ciudad y estado obtenidos de zippopotam.us (gratuito, sin API key).

Env vars requeridas:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  DISCORD_WEBHOOK_ZIP
  REPORT_MODE = "weekly" | "monthly"
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytz
import requests

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
WEBHOOK        = os.environ["DISCORD_WEBHOOK_ZIP"]

TZ_NAME   = "America/New_York"
PAGE_SIZE = 1000

VALUE_COLUMNS = [
    "publisherName",
    "tag:gather:zipcode",
    "tag:Geo:ZipCode",
    "hasConverted",
    "conversionAmount",
]


# ── Zip → Ciudad, Estado ──────────────────────────────────────────────────────

_zip_cache: dict[str, str] = {}


def zip_location(zip_code: str) -> str:
    """
    Retorna "Ciudad, ST" para un zip code US.
    Usa zippopotam.us — gratuito, sin key.
    Fallback: solo el zip code si no encuentra.
    """
    if zip_code in _zip_cache:
        return _zip_cache[zip_code]

    try:
        resp = requests.get(
            f"https://api.zippopotam.us/us/{zip_code}",
            timeout=5,
        )
        if resp.status_code == 200:
            data  = resp.json()
            place = data.get("places", [{}])[0]
            city  = place.get("place name", "")
            state = place.get("state abbreviation", "")
            location = f"{city}, {state}" if city and state else zip_code
        else:
            location = zip_code
    except Exception:
        location = zip_code

    _zip_cache[zip_code] = location
    return location


# ── Ringba ────────────────────────────────────────────────────────────────────

def _post_calllogs(start_utc: datetime, end_utc: datetime, offset: int) -> dict:
    url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs"
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    body = {
        "reportStart":  start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":    end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":         PAGE_SIZE,
        "offset":       offset,
        "valueColumns": [{"column": c} for c in VALUE_COLUMNS],
    }
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_zip_data(start_utc: datetime, end_utc: datetime) -> dict:
    """
    Retorna zip_code → {revenue, conversions, calls}.
    Usa gather:zipcode con fallback a Geo:ZipCode.
    """
    zip_map = defaultdict(lambda: {"revenue": 0.0, "conversions": 0, "calls": 0})

    chunk_start = start_utc
    while chunk_start < end_utc:
        chunk_end = min(chunk_start + timedelta(hours=24) - timedelta(seconds=1), end_utc)
        offset = 0

        for _ in range(100):
            data    = _post_calllogs(chunk_start, chunk_end, offset)
            records = (data.get("report") or {}).get("records") or []
            if not records:
                break

            for r in records:
                zip_code = (
                    r.get("tag:gather:zipcode")
                    or r.get("tag:Geo:ZipCode")
                    or ""
                )
                zip_code = str(zip_code).strip()
                if not zip_code or zip_code in ("None", "null", "0"):
                    continue
                if zip_code.isdigit() and len(zip_code) < 5:
                    zip_code = zip_code.zfill(5)

                z = zip_map[zip_code]
                z["calls"] += 1
                if r.get("hasConverted") is True:
                    z["conversions"] += 1
                    try:
                        z["revenue"] += float(r.get("conversionAmount") or 0)
                    except (ValueError, TypeError):
                        pass

            offset += len(records)
            if len(records) < PAGE_SIZE:
                break

        chunk_start += timedelta(hours=24)

    return dict(zip_map)


# ── Date ranges ───────────────────────────────────────────────────────────────

def weekly_range() -> tuple[datetime, datetime, str]:
    tz    = pytz.timezone(TZ_NAME)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    start_utc = last_monday.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc   = last_sunday.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    label = f"{last_monday.strftime('%d %b')} — {last_sunday.strftime('%d %b %Y')}"
    return start_utc, end_utc, label


def monthly_range() -> tuple[datetime, datetime, str]:
    tz         = pytz.timezone(TZ_NAME)
    now_local  = datetime.now(tz)
    first_this = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev  = first_this - timedelta(seconds=1)
    first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc  = first_prev.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc    = last_prev.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    MONTHS_ES  = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
    }
    label = f"{MONTHS_ES[first_prev.month]} {first_prev.year}"
    return start_utc, end_utc, label


# ── Format ────────────────────────────────────────────────────────────────────

def _rank_by_revenue(zip_map: dict) -> list:
    """Ordena por revenue desc. Desempate: conversiones desc. Retorna TODOS."""
    return sorted(
        zip_map.items(),
        key=lambda x: (x[1]["revenue"], x[1]["conversions"]),
        reverse=True,
    )


def build_messages(zip_map: dict, label: str, period_label: str) -> list[str]:
    """
    Genera lista de mensajes Discord (máx 1900 chars cada uno)
    con todos los zip codes. El primero incluye el header.
    """
    ranked = _rank_by_revenue(zip_map)
    if not ranked:
        return [f"**📍 ZIP CODE REPORT — {period_label}**\n{label}\nSin datos de zip codes."]

    total = len(ranked)
    print(f"  Resolviendo ciudad/estado para {total} zips...")
    rows = []
    for zip_code, _ in ranked:
        location = zip_location(zip_code)
        rows.append((zip_code, location))
        time.sleep(0.05)  # evitar rate limit de zippopotam.us

    # Construir líneas de datos
    data_lines = []
    for i, (zip_code, location) in enumerate(rows, 1):
        data_lines.append(f"  {i:>4}  {zip_code:<7}  {location}")

    # Paginar en mensajes de máx 1900 chars
    messages = []
    HEADER_FIRST = (
        f"**📍 ZIP CODE REPORT — {period_label}**\n"
        f"Período: {label} · {total} zips\n"
    )
    COLS_HEADER = [
        "```",
        f"  {'#':>4}  {'Zip':<7}  Ubicación",
        "  " + "─" * 38,
    ]
    FOOTER = "```"

    is_first = True
    current_lines: list[str] = []
    current_prefix = HEADER_FIRST if is_first else ""

    for line in data_lines:
        # Calcular tamaño del bloque actual si agregamos esta línea
        block = current_prefix + "\n".join(COLS_HEADER + current_lines + [line] + [FOOTER])
        if len(block) > 1900 and current_lines:
            # Cerrar bloque actual y enviarlo
            msg = current_prefix + "\n".join(COLS_HEADER + current_lines + [FOOTER])
            messages.append(msg)
            # Siguiente bloque: sin header principal
            current_prefix = ""
            current_lines = [line]
        else:
            current_lines.append(line)

    # Último bloque
    if current_lines:
        msg = current_prefix + "\n".join(COLS_HEADER + current_lines + [FOOTER])
        messages.append(msg)

    return messages


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    mode = os.environ.get("REPORT_MODE", "").lower()

    if "--weekly" in args or mode == "weekly":
        report_mode  = "weekly"
        period_label = "SEMANAL"
    elif "--monthly" in args or mode == "monthly":
        report_mode  = "monthly"
        period_label = "MENSUAL"
    else:
        print("ERROR: especifica --weekly o --monthly (o REPORT_MODE=weekly|monthly)", file=sys.stderr)
        sys.exit(1)

    print(f"Modo: {report_mode}")

    if report_mode == "weekly":
        start_utc, end_utc, label = weekly_range()
    else:
        start_utc, end_utc, label = monthly_range()

    print(f"Rango: {start_utc.strftime('%Y-%m-%d')} → {end_utc.strftime('%Y-%m-%d')}")
    print("Consultando Ringba...")

    zip_map = fetch_zip_data(start_utc, end_utc)
    total_zips = len(zip_map)
    total_rev  = sum(d["revenue"] for d in zip_map.values())
    print(f"  Zips únicos: {total_zips} | Revenue total: ${total_rev:.2f}")

    messages = build_messages(zip_map, label, period_label)
    print(f"  Mensajes Discord a enviar: {len(messages)}")

    print("Enviando a Discord...")
    for i, msg in enumerate(messages, 1):
        discord_send(WEBHOOK, msg)
        if i < len(messages):
            time.sleep(0.5)  # evitar rate limit Discord
    print("✓ Completado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

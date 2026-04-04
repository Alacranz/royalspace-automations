#!/usr/bin/env python3
"""
ZIP Code Report — Royalspace 2026

Genera un ranking de zip codes por conversiones para el canal de Discord.
Dos modos:
  --weekly  : últimos 7 días, sin conteos (solo ranking)
  --monthly : mes anterior completo, con conteos de conversiones

Requiere env vars:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  DISCORD_WEBHOOK_ZIP (webhook del canal de zip reports)

Opcional:
  REPORT_MODE = "weekly" | "monthly"  (alternativa a --weekly / --monthly)
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
import requests

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
WEBHOOK        = os.environ["DISCORD_WEBHOOK_ZIP"]

TZ_NAME  = "America/New_York"
PAGE_SIZE = 1000
TOP_N     = 20

VALUE_COLUMNS = [
    "publisherName",
    "tag:gather:zipcode",
    "tag:Geo:ZipCode",
    "hasConverted",
    "conversionAmount",
]


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


def fetch_zip_data(start_utc: datetime, end_utc: datetime) -> dict[str, dict]:
    """
    Retorna dict: zip_code → {conversions, revenue, calls}
    Usa gather:zipcode con fallback a Geo:ZipCode.
    Solo incluye llamadas con zip code presente.
    """
    zip_map: dict[str, dict] = defaultdict(lambda: {"conversions": 0, "revenue": 0.0, "calls": 0})
    offset = 0

    # Paginar por chunks diarios para estabilidad
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

                # Normalizar a 5 dígitos si es numérico
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
    """Últimos 7 días completos (lunes–domingo de la semana anterior)."""
    tz = pytz.timezone(TZ_NAME)
    now_local = datetime.now(tz)
    # Semana anterior: lunes a domingo
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_monday = today.weekday()  # 0=lun
    last_monday = today - timedelta(days=days_since_monday + 7)
    last_sunday = last_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

    start_utc = last_monday.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc   = last_sunday.astimezone(timezone.utc).replace(tzinfo=timezone.utc)

    label = f"{last_monday.strftime('%d %b')} — {last_sunday.strftime('%d %b %Y')}"
    return start_utc, end_utc, label


def monthly_range() -> tuple[datetime, datetime, str]:
    """Mes anterior completo."""
    tz = pytz.timezone(TZ_NAME)
    now_local = datetime.now(tz)
    first_of_this_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_prev_month  = first_of_this_month - timedelta(seconds=1)
    first_of_prev_month = last_of_prev_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    start_utc = first_of_prev_month.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc   = last_of_prev_month.astimezone(timezone.utc).replace(tzinfo=timezone.utc)

    MONTHS_ES = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
    }
    label = f"{MONTHS_ES[first_of_prev_month.month]} {first_of_prev_month.year}"
    return start_utc, end_utc, label


# ── Format ────────────────────────────────────────────────────────────────────

def format_weekly(zip_map: dict, label: str) -> str:
    """Ranking sin conteos — solo posición y zip code."""
    # Ordenar por conversiones desc, luego por calls como desempate
    ranked = sorted(
        zip_map.items(),
        key=lambda x: (x[1]["conversions"], x[1]["calls"]),
        reverse=True,
    )[:TOP_N]

    if not ranked:
        return f"**📍 ZIP CODE REPORT — SEMANAL**\n{label}\nSin datos de zip codes esta semana."

    lines = [
        f"**📍 TOP {len(ranked)} ZIP CODES — SEMANAL**",
        f"Semana: {label}",
        "*(Fuente: gather — zip ingresado por el paciente)*",
        "```",
    ]
    # Dos columnas para aprovechar el espacio
    half = (len(ranked) + 1) // 2
    col1 = ranked[:half]
    col2 = ranked[half:]

    for i, (item1, item2) in enumerate(zip(col1, col2 + [(None, None)] * half)):
        pos1 = i + 1
        z1   = item1[0]
        if item2 and item2[0]:
            pos2 = i + 1 + half
            z2   = item2[0]
            lines.append(f"  {pos1:>2}. {z1:<10}  {pos2:>2}. {z2}")
        else:
            lines.append(f"  {pos1:>2}. {z1}")

    lines.append("```")
    return "\n".join(lines)


def format_monthly(zip_map: dict, label: str) -> str:
    """Ranking con conteo de conversiones."""
    ranked = sorted(
        zip_map.items(),
        key=lambda x: (x[1]["conversions"], x[1]["calls"]),
        reverse=True,
    )[:TOP_N]

    if not ranked:
        return f"**📍 ZIP CODE REPORT — MENSUAL**\n{label}\nSin datos de zip codes este mes."

    lines = [
        f"**📍 TOP {len(ranked)} ZIP CODES — MENSUAL**",
        f"Período: {label}",
        "*(Fuente: gather — zip ingresado por el paciente)*",
        "```",
        f"  {'#':>2}  {'Zip':<10}  {'Convs':>5}  {'Llamadas':>8}",
        "  " + "─" * 34,
    ]
    for i, (zip_code, data) in enumerate(ranked, 1):
        convs = data["conversions"]
        calls = data["calls"]
        lines.append(f"  {i:>2}  {zip_code:<10}  {convs:>5}  {calls:>8}")

    lines.append("```")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Determinar modo
    args = sys.argv[1:]
    mode = os.environ.get("REPORT_MODE", "").lower()

    if "--weekly" in args or mode == "weekly":
        report_mode = "weekly"
    elif "--monthly" in args or mode == "monthly":
        report_mode = "monthly"
    else:
        print("ERROR: especifica --weekly o --monthly (o REPORT_MODE=weekly|monthly)", file=sys.stderr)
        sys.exit(1)

    print(f"Modo: {report_mode}")

    if report_mode == "weekly":
        start_utc, end_utc, label = weekly_range()
    else:
        start_utc, end_utc, label = monthly_range()

    print(f"Rango: {start_utc.strftime('%Y-%m-%d')} → {end_utc.strftime('%Y-%m-%d')}")
    print("Consultando Ringba (con valueColumns para zip)...")

    zip_map = fetch_zip_data(start_utc, end_utc)
    total_zips = len(zip_map)
    total_convs = sum(d["conversions"] for d in zip_map.values())
    print(f"  Zips únicos: {total_zips} | Total conversiones con zip: {total_convs}")

    if report_mode == "weekly":
        msg = format_weekly(zip_map, label)
    else:
        msg = format_monthly(zip_map, label)

    # Truncar si excede límite Discord
    if len(msg) > 1900:
        msg = msg[:1900] + "\n..."

    print("Enviando a Discord...")
    discord_send(WEBHOOK, msg)
    print("✓ Enviado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

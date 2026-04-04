#!/usr/bin/env python3
"""
ZIP Code Report — Royalspace 2026

Genera un ranking de zip codes por conversiones para el canal de Discord.
Dos modos:
  --weekly  : semana anterior (lun–dom), solo ranking sin conteos
  --monthly : mes anterior completo, con conteos de conversiones

Un solo webhook para ambos modos (mismo canal).

Env vars requeridas:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  DISCORD_WEBHOOK_ZIP
  REPORT_MODE = "weekly" | "monthly"
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import pytz
import requests

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send

RINGBA_TOKEN = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_ZIP"]

TZ_NAME   = "America/New_York"
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


def fetch_zip_data(start_utc: datetime, end_utc: datetime) -> dict:
    """
    Retorna zip_code → {conversions, revenue, calls}.
    Usa gather:zipcode con fallback a Geo:ZipCode.
    Incluye todos los publishers.
    """
    zip_map = defaultdict(lambda: {"conversions": 0, "revenue": 0.0, "calls": 0})

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

def _rank(zip_map: dict) -> list:
    return sorted(
        zip_map.items(),
        key=lambda x: (x[1]["conversions"], x[1]["calls"]),
        reverse=True,
    )[:TOP_N]


def format_weekly(zip_map: dict, label: str) -> str:
    ranked = _rank(zip_map)
    if not ranked:
        return f"**📍 ZIP CODE REPORT — SEMANAL**\nSemana: {label}\nSin datos de zip codes esta semana."

    lines = [
        f"**📍 TOP {len(ranked)} ZIP CODES — SEMANAL**",
        f"Semana: {label}",
        "```",
    ]
    half = (len(ranked) + 1) // 2
    col1, col2 = ranked[:half], ranked[half:]
    for i in range(half):
        z1   = col1[i][0]
        line = f"  {i+1:>2}. {z1:<10}"
        if i < len(col2):
            line += f"  {i+1+half:>2}. {col2[i][0]}"
        lines.append(line)
    lines.append("```")
    return "\n".join(lines)


def format_monthly(zip_map: dict, label: str) -> str:
    ranked = _rank(zip_map)
    if not ranked:
        return f"**📍 ZIP CODE REPORT — MENSUAL**\nPeríodo: {label}\nSin datos de zip codes este mes."

    lines = [
        f"**📍 ZIP CODE REPORT — MENSUAL**",
        f"Período: {label}",
        "```",
        f"  {'#':>2}  {'Zip':<10}  {'Convs':>5}  {'Llamadas':>8}",
        "  " + "─" * 34,
    ]
    for i, (zip_code, data) in enumerate(ranked, 1):
        lines.append(f"  {i:>2}  {zip_code:<10}  {data['conversions']:>5}  {data['calls']:>8}")
    lines.append("```")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
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
    print("Consultando Ringba...")

    zip_map = fetch_zip_data(start_utc, end_utc)
    print(f"  Zips únicos: {len(zip_map)} | Conversiones: {sum(d['conversions'] for d in zip_map.values())}")

    msg = format_weekly(zip_map, label) if report_mode == "weekly" else format_monthly(zip_map, label)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n..."

    print("Enviando a Discord...")
    discord_send(WEBHOOK, msg)
    print("✓ Completado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

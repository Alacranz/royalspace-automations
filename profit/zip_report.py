#!/usr/bin/env python3
"""
ZIP Code Report — Royalspace 2026

Genera un ranking de zip codes por conversiones para el canal de Discord.
Dos modos:
  --weekly  : semana anterior (lun–dom), sin conteos (solo ranking)
  --monthly : mes anterior completo, con conteos de conversiones

Envía a dos webhooks:
  DISCORD_WEBHOOK_ZIP_INTERNAL → todos los datos (incluye grupo privado)
  DISCORD_WEBHOOK_ZIP_EXTERNAL → excluye llamadas del grupo privado

Publishers privados (config: accounts_private_groups):
  "you", "T.I Angela Monroy"

Env vars requeridas:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  DISCORD_WEBHOOK_ZIP_INTERNAL
  DISCORD_WEBHOOK_ZIP_EXTERNAL
  REPORT_MODE = "weekly" | "monthly"
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
import requests

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send

RINGBA_TOKEN     = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT   = os.environ["RINGBA_ACCOUNT_ID"]
WEBHOOK_INTERNAL = os.environ["DISCORD_WEBHOOK_ZIP_INTERNAL"]
WEBHOOK_EXTERNAL = os.environ["DISCORD_WEBHOOK_ZIP_EXTERNAL"]

CONFIG_PATH = Path(__file__).parent / "config.json"
TZ_NAME     = "America/New_York"
PAGE_SIZE   = 1000
TOP_N       = 20

VALUE_COLUMNS = [
    "publisherName",
    "tag:gather:zipcode",
    "tag:Geo:ZipCode",
    "hasConverted",
    "conversionAmount",
]


def load_private_publishers() -> set[str]:
    """Carga los publisher names del grupo privado desde config.json."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    private: set[str] = set()
    for group in config.get("accounts_private_groups") or []:
        for pub in group.get("publishers") or []:
            private.add(pub.strip().lower())
    return private


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


def fetch_zip_data(
    start_utc: datetime,
    end_utc: datetime,
    private_publishers: set[str],
) -> tuple[dict, dict]:
    """
    Retorna (zip_map_all, zip_map_public).
    zip_map_all    → todos los publishers (para internal)
    zip_map_public → excluye grupo privado (para external)
    Cada map: zip_code → {conversions, revenue, calls}
    """
    def empty_map():
        return defaultdict(lambda: {"conversions": 0, "revenue": 0.0, "calls": 0})

    zip_all    = empty_map()
    zip_public = empty_map()

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

                converted = r.get("hasConverted") is True
                try:
                    revenue = float(r.get("conversionAmount") or 0)
                except (ValueError, TypeError):
                    revenue = 0.0

                # Siempre agregar a all
                z = zip_all[zip_code]
                z["calls"] += 1
                if converted:
                    z["conversions"] += 1
                    z["revenue"] += revenue

                # Solo agregar a public si no es publisher privado
                pub_name = str(r.get("publisherName") or "").strip().lower()
                is_private = any(priv in pub_name for priv in private_publishers)
                if not is_private:
                    z2 = zip_public[zip_code]
                    z2["calls"] += 1
                    if converted:
                        z2["conversions"] += 1
                        z2["revenue"] += revenue

            offset += len(records)
            if len(records) < PAGE_SIZE:
                break

        chunk_start += timedelta(hours=24)

    return dict(zip_all), dict(zip_public)


# ── Date ranges ───────────────────────────────────────────────────────────────

def weekly_range() -> tuple[datetime, datetime, str]:
    tz = pytz.timezone(TZ_NAME)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    start_utc = last_monday.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc   = last_sunday.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    label = f"{last_monday.strftime('%d %b')} — {last_sunday.strftime('%d %b %Y')}"
    return start_utc, end_utc, label


def monthly_range() -> tuple[datetime, datetime, str]:
    tz = pytz.timezone(TZ_NAME)
    now_local = datetime.now(tz)
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
        z1 = col1[i][0]
        line = f"  {i+1:>2}. {z1:<10}"
        if i < len(col2):
            z2 = col2[i][0]
            line += f"  {i+1+half:>2}. {z2}"
        lines.append(line)
    lines.append("```")
    return "\n".join(lines)


def format_monthly(zip_map: dict, label: str) -> str:
    ranked = _rank(zip_map)
    if not ranked:
        return f"**📍 ZIP CODE REPORT — MENSUAL**\nPeríodo: {label}\nSin datos de zip codes este mes."

    lines = [
        f"**📍 TOP {len(ranked)} ZIP CODES — MENSUAL**",
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

    private_publishers = load_private_publishers()
    print(f"Publishers privados: {private_publishers}")

    print("Consultando Ringba...")
    zip_all, zip_public = fetch_zip_data(start_utc, end_utc, private_publishers)

    print(f"  Zips (todos): {len(zip_all)} | Zips (público): {len(zip_public)}")
    print(f"  Conversiones (todos): {sum(d['conversions'] for d in zip_all.values())}")
    print(f"  Conversiones (público): {sum(d['conversions'] for d in zip_public.values())}")

    formatter = format_weekly if report_mode == "weekly" else format_monthly

    msg_internal = formatter(zip_all, label)
    msg_external = formatter(zip_public, label)

    if len(msg_internal) > 1900:
        msg_internal = msg_internal[:1900] + "\n..."
    if len(msg_external) > 1900:
        msg_external = msg_external[:1900] + "\n..."

    print("Enviando a Discord (internal)...")
    discord_send(WEBHOOK_INTERNAL, msg_internal)

    print("Enviando a Discord (external)...")
    discord_send(WEBHOOK_EXTERNAL, msg_external)

    print("✓ Completado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

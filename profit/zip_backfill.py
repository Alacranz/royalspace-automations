#!/usr/bin/env python3
"""
ZIP Backfill — Royalspace 2026

Script de una sola corrida para rellenar meses faltantes en el Google Sheet.
Por cada mes especificado en BACKFILL_MONTHS:
  - Fetches todos los zip codes del mes completo (o hasta hoy si es el mes actual)
  - Escribe/sobreescribe la hoja correspondiente en Google Sheets
  - Envía una notificación por Discord al terminar todo

Env vars requeridas:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  DISCORD_WEBHOOK_MB_INTERNAL
  GOOGLE_SERVICE_ACCOUNT_JSON
  ZIP_SPREADSHEET_ID
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import gspread
import pytz
import requests
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
WEBHOOK        = os.environ["DISCORD_WEBHOOK_MB_INTERNAL"]
SA_JSON        = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID = os.environ["ZIP_SPREADSHEET_ID"]

TZ_NAME   = "America/New_York"
PAGE_SIZE = 1000

MONTHS_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Ago",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

# Meses a rellenar: (año, mes) — el mes actual se toma hasta hoy
BACKFILL_MONTHS = [
    (2026, 2),  # Febrero 2026 — completo
    (2026, 3),  # Marzo 2026   — completo
    (2026, 4),  # Abril 2026   — hasta hoy
]

VALUE_COLUMNS = [
    "publisherName",
    "tag:gather:zipcode",
    "tag:Geo:ZipCode",
    "hasConverted",
    "conversionAmount",
]


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet(tab_name: str):
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    try:
        sheet = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=5000, cols=3)
        print(f"  Hoja creada: '{tab_name}'")
    return sheet


def write_to_sheet(rows: list[tuple[str, str, str]], tab_name: str) -> None:
    sheet = get_sheet(tab_name)
    data  = [["Zipcode", "City", "State"]]
    for zip_code, city, state in rows:
        data.append([zip_code, city, state])
    sheet.clear()
    sheet.update(range_name="A1", values=data)
    sheet.resize(rows=len(data), cols=3)
    print(f"  Escrito en hoja '{tab_name}': {len(rows)} zips")


# ── Zip → Ciudad, Estado ──────────────────────────────────────────────────────

_zip_cache: dict[str, tuple[str, str]] = {}


def zip_location(zip_code: str) -> tuple[str, str]:
    if zip_code in _zip_cache:
        return _zip_cache[zip_code]
    try:
        resp = requests.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=5)
        if resp.status_code == 200:
            place = resp.json().get("places", [{}])[0]
            city  = place.get("place name", "")
            state = place.get("state abbreviation", "")
        else:
            city, state = "", ""
    except Exception:
        city, state = "", ""
    _zip_cache[zip_code] = (city, state)
    return city, state


# ── Ringba ────────────────────────────────────────────────────────────────────

def _post_calllogs(start_utc: datetime, end_utc: datetime, offset: int) -> dict:
    resp = requests.post(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
        headers={
            "Authorization": f"Token {RINGBA_TOKEN}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        },
        json={
            "reportStart":  start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd":    end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size":         PAGE_SIZE,
            "offset":       offset,
            "valueColumns": [{"column": c} for c in VALUE_COLUMNS],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_zip_data(start_utc: datetime, end_utc: datetime) -> dict:
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


# ── Main ──────────────────────────────────────────────────────────────────────

def month_range(year: int, month: int) -> tuple[datetime, datetime]:
    """Retorna (start_utc, end_utc) para el mes dado. Si es el mes actual, end = ahora."""
    tz    = pytz.timezone(TZ_NAME)
    now   = datetime.now(tz)
    start = tz.localize(datetime(year, month, 1, 0, 0, 0))
    start_utc = start.astimezone(timezone.utc).replace(tzinfo=timezone.utc)

    is_current = (year == now.year and month == now.month)
    if is_current:
        end_utc = datetime.now(timezone.utc)
    else:
        # Primer día del mes siguiente - 1 segundo
        if month == 12:
            next_month = tz.localize(datetime(year + 1, 1, 1, 0, 0, 0))
        else:
            next_month = tz.localize(datetime(year, month + 1, 1, 0, 0, 0))
        end_utc = (next_month - timedelta(seconds=1)).astimezone(timezone.utc).replace(tzinfo=timezone.utc)

    return start_utc, end_utc


def main() -> None:
    summary = []

    for year, month in BACKFILL_MONTHS:
        tab_name  = f"{MONTHS_ES[month]} {year}"
        start_utc, end_utc = month_range(year, month)
        is_partial = (year == datetime.now(timezone.utc).year and
                      month == datetime.now(timezone.utc).month)
        tipo = "parcial (hasta hoy)" if is_partial else "completo"

        print(f"\n{'='*50}")
        print(f"Procesando: {tab_name} ({tipo})")
        print(f"Rango: {start_utc.strftime('%Y-%m-%d')} → {end_utc.strftime('%Y-%m-%d')}")
        print("Consultando Ringba...")

        zip_map = fetch_zip_data(start_utc, end_utc)
        total_zips = len(zip_map)
        print(f"  Zips únicos: {total_zips}")

        ranked = sorted(
            zip_map.items(),
            key=lambda x: (x[1]["revenue"], x[1]["conversions"]),
            reverse=True,
        )

        print(f"  Resolviendo ciudad/estado para {total_zips} zips...")
        rows: list[tuple[str, str, str]] = []
        for i, (zip_code, _) in enumerate(ranked):
            city, state = zip_location(zip_code)
            rows.append((zip_code, city, state))
            if (i + 1) % 100 == 0:
                print(f"    {i+1}/{total_zips} resueltos...")
            time.sleep(0.05)

        print("Escribiendo en Google Sheets...")
        write_to_sheet(rows, tab_name)
        summary.append(f"  ✅ {tab_name} ({tipo}) — {total_zips} zips")

    # Notificación final Discord
    print("\n" + "="*50)
    print("Backfill completado. Enviando resumen a Discord...")
    msg = "**📍 ZIP Backfill completado**\n" + "\n".join(summary) + \
          f"\nhttps://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
    discord_send(WEBHOOK, msg)
    print("✓ Listo.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

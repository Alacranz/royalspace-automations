#!/usr/bin/env python3
"""
ZIP Code Report — Royalspace 2026

Dos modos:
  weekly  : fetch desde el inicio del mes actual hasta ahora
            → sobreescribe hoja del mes actual en Google Sheets
  monthly : fetch del mes anterior completo
            → sobreescribe hoja del mes anterior en Google Sheets

Formato Google Sheet: Zipcode | City | State
Ordenado por revenue descendente. Sin mostrar revenue, llamadas ni conversiones.
Ciudad/Estado via api.zippopotam.us (gratuito, sin key).

Env vars requeridas:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  DISCORD_WEBHOOK_MB_INTERNAL
  GOOGLE_SERVICE_ACCOUNT_JSON
  ZIP_SPREADSHEET_ID
  REPORT_MODE = "weekly" | "monthly"
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

RINGBA_TOKEN    = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT  = os.environ["RINGBA_ACCOUNT_ID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_MB_INTERNAL"]
SA_JSON         = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID  = os.environ["ZIP_SPREADSHEET_ID"]

TZ_NAME   = "America/New_York"
PAGE_SIZE = 1000

MONTHS_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Ago",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

VALUE_COLUMNS = [
    "publisherName",
    "tag:gather:zipcode",
    "tag:Geo:ZipCode",
    "hasConverted",
    "conversionAmount",
]


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet(tab_name: str):
    """Abre o crea la hoja con el nombre dado."""
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    # Buscar tab existente o crear nuevo
    try:
        sheet = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=5000, cols=3)
        print(f"  Hoja creada: '{tab_name}'")

    return sheet


def write_to_sheet(rows: list[tuple[str, str, str]], tab_name: str) -> None:
    """
    Sobreescribe la hoja con los datos dados.
    rows: lista de (zipcode, city, state)
    """
    sheet = get_sheet(tab_name)

    # Preparar datos: header + filas
    data = [["Zipcode", "City", "State"]]
    for zip_code, city, state in rows:
        data.append([zip_code, city, state])

    # Limpiar hoja, escribir y redimensionar al tamaño exacto
    sheet.clear()
    sheet.update(range_name="A1", values=data)
    sheet.resize(rows=len(data), cols=3)
    sheet.set_basic_filter()
    print(f"  Escrito en hoja '{tab_name}': {len(rows)} filas")


# ── Zip → Ciudad, Estado ──────────────────────────────────────────────────────

_zip_cache: dict[str, tuple[str, str]] = {}


def zip_location(zip_code: str) -> tuple[str, str]:
    """Retorna (city, state_abbr) para un zip code US via zippopotam.us."""
    if zip_code in _zip_cache:
        return _zip_cache[zip_code]

    try:
        resp = requests.get(
            f"https://api.zippopotam.us/us/{zip_code}",
            timeout=5,
        )
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
    gather:zipcode con fallback a Geo:ZipCode.
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

def current_month_range() -> tuple[datetime, datetime, str]:
    """Desde el inicio del mes actual hasta ahora."""
    tz        = pytz.timezone(TZ_NAME)
    now_local = datetime.now(tz)
    first     = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc = first.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc   = datetime.now(timezone.utc)
    tab_name  = f"{MONTHS_ES[now_local.month]} {now_local.year}"
    return start_utc, end_utc, tab_name


def previous_month_range() -> tuple[datetime, datetime, str]:
    """Mes anterior completo."""
    tz         = pytz.timezone(TZ_NAME)
    now_local  = datetime.now(tz)
    first_this = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev  = first_this - timedelta(seconds=1)
    first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc  = first_prev.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc    = last_prev.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    tab_name   = f"{MONTHS_ES[first_prev.month]} {first_prev.year}"
    return start_utc, end_utc, tab_name


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    mode = os.environ.get("REPORT_MODE", "").lower()

    if "--weekly" in args or mode == "weekly":
        report_mode = "weekly"
        start_utc, end_utc, tab_name = current_month_range()
    elif "--monthly" in args or mode == "monthly":
        report_mode = "monthly"
        start_utc, end_utc, tab_name = previous_month_range()
    else:
        print("ERROR: especifica --weekly o --monthly (o REPORT_MODE=weekly|monthly)", file=sys.stderr)
        sys.exit(1)

    print(f"Modo: {report_mode} → hoja: '{tab_name}'")
    print(f"Rango: {start_utc.strftime('%Y-%m-%d')} → {end_utc.strftime('%Y-%m-%d')}")
    print("Consultando Ringba...")

    zip_map = fetch_zip_data(start_utc, end_utc)
    total_zips = len(zip_map)
    print(f"  Zips únicos: {total_zips}")

    # Ordenar por revenue desc
    ranked = sorted(
        zip_map.items(),
        key=lambda x: (x[1]["revenue"], x[1]["conversions"]),
        reverse=True,
    )

    # Resolver ciudad/estado
    print(f"  Resolviendo ciudad/estado para {total_zips} zips...")
    rows: list[tuple[str, str, str]] = []
    for i, (zip_code, _) in enumerate(ranked):
        city, state = zip_location(zip_code)
        rows.append((zip_code, city, state))
        if (i + 1) % 100 == 0:
            print(f"    {i+1}/{total_zips} resueltos...")
        time.sleep(0.05)

    # Escribir en Google Sheets
    print("Escribiendo en Google Sheets...")
    write_to_sheet(rows, tab_name)

    # Notificación Discord
    label_tipo = "Mensual completo" if report_mode == "monthly" else "Actualización semanal"
    discord_send(
        WEBHOOK,
        f"**📍 ZIP Report actualizado** — {tab_name}\n"
        f"{label_tipo} · {total_zips} zips únicos\n"
        f"Ver: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
    )
    print("✓ Completado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

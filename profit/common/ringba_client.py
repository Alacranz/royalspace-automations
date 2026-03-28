"""
Ringba API client — Royalspace 2026

Funciones compartidas por true_profit, mb_alerts, mb_daily_summary,
spend_guard y ringba_monitor.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import pytz
import requests

RINGBA_BASE_URL = "https://api.ringba.com/v2"
PAGE_SIZE       = 1000
MAX_PAGES       = 100


# ── Utilidades ────────────────────────────────────────────────────────────────

def to_float(value) -> float:
    """Convierte cualquier valor a float sin lanzar excepciones."""
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return 0.0


def normalize_name(name: str) -> str:
    """
    Normaliza el nombre de un publisher para comparaciones.
    Elimina prefijos tipo "(123) " y convierte a minúsculas.
    Replica la función Normalize-Name del PS1.
    """
    if not name or not name.strip():
        return ""
    n = name.strip()
    n = re.sub(r'^\(\d+\)\s*', '', n)
    return n.strip().lower()


def get_midnight_utc(timezone_name: str) -> datetime:
    """
    Retorna la medianoche de HOY (en la timezone dada) como datetime UTC aware.
    Equivalente a Get-LocalMidnightUtc del PS1.
    """
    tz = pytz.timezone(timezone_name)
    now_local = datetime.now(tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc)


def get_yesterday_utc_range(timezone_name: str) -> tuple[datetime, datetime]:
    """
    Retorna (start_utc, end_utc) para el día de AYER completo en la timezone dada.
    Equivalente a Get-RingbaYesterday del PS1.
    end_utc = medianoche de hoy - 1 segundo.
    """
    tz = pytz.timezone(timezone_name)
    now_local  = datetime.now(tz)
    yesterday  = now_local.replace(hour=0, minute=0, second=0, microsecond=0).replace(
        day=now_local.day
    )
    # Ayer = hoy - 1 día
    from datetime import timedelta
    yesterday_start = yesterday - timedelta(days=1)
    yesterday_end   = yesterday - timedelta(seconds=1)

    start_utc = yesterday_start.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    end_utc   = yesterday_end.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    return start_utc, end_utc


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post_calllogs(
    token: str,
    account_id: str,
    start_utc: datetime,
    end_utc: datetime,
    size: int,
    offset: int,
) -> dict:
    url = f"{RINGBA_BASE_URL}/{account_id}/calllogs"
    headers = {
        "Authorization": f"Token {token}",
        "Accept":         "application/json",
        "Content-Type":   "application/json",
    }
    body = {
        "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        size,
        "offset":      offset,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ── Funciones principales ─────────────────────────────────────────────────────

def get_publisher_summary(
    token: str,
    account_id: str,
    start_utc: datetime,
    end_utc: datetime,
    exclude_duplicates: bool = False,
) -> dict[str, dict]:
    """
    Agrega call logs por publisher en el rango dado.
    Retorna dict: normalized_name → {raw, revenue, payout, calls, connected, conversions, profit_net}

    exclude_duplicates=True: omite llamadas marcadas como duplicadas (isDuplicate=True).
    Usar True para contabilidad semanal — la UI de Ringba Publisher Summary las excluye.

    Usado por: true_profit, mb_alerts, spend_guard, mb_daily_summary, weekly_accounting
    """
    publisher_map: dict[str, dict] = {}
    seen_ids:      set[str]        = set()   # deduplicación por inboundCallId
    offset        = 0
    skipped_dupes = 0
    total_count   = 0
    total_fetched = 0

    for page in range(1, MAX_PAGES + 1):
        data    = _post_calllogs(token, account_id, start_utc, end_utc, PAGE_SIZE, offset)
        records = (data.get("report") or {}).get("records") or []

        if page == 1:
            try:
                total_count = int((data.get("report") or {}).get("totalCount") or 0)
                print(f"  [Ringba] Total registros en rango: {total_count}")
            except (ValueError, TypeError):
                pass

        if not records:
            break

        for r in records:
            # Deduplicar por inboundCallId — evita contar el mismo record dos veces
            # cuando la paginación sin orden estable mueve records entre páginas
            call_id = str(r.get("inboundCallId") or r.get("callId") or "")
            if call_id and call_id in seen_ids:
                continue
            if call_id:
                seen_ids.add(call_id)

            # Excluir duplicados si se solicita
            if exclude_duplicates and r.get("isDuplicate") is True:
                skipped_dupes += 1
                continue

            raw = str(r.get("publisherName") or "")
            key = normalize_name(raw) or "unknown"

            if key not in publisher_map:
                publisher_map[key] = {
                    "raw":        raw,
                    "revenue":    0.0,
                    "payout":     0.0,
                    "calls":      0,
                    "connected":  0,
                    "conversions": 0,
                    "profit_net": 0.0,
                }

            m = publisher_map[key]
            m["calls"] += 1
            if r.get("hasConnected") is True:
                m["connected"]  += 1
            if r.get("hasConverted") is True:
                m["conversions"] += 1
            m["revenue"]    += to_float(r.get("conversionAmount"))
            m["payout"]     += to_float(r.get("payoutAmount"))
            m["profit_net"] += to_float(r.get("profitNet"))

        total_fetched += len(records)
        offset        += len(records)

        if total_count > 0 and offset >= total_count:
            break
        if len(records) < PAGE_SIZE:
            break

    print(f"  [Ringba] Registros procesados: {total_fetched} (únicos: {len(seen_ids)})")
    if exclude_duplicates:
        print(f"  [Ringba] Llamadas duplicadas excluidas: {skipped_dupes}")

    return publisher_map


def get_call_metrics(
    token: str,
    account_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict:
    """
    Agrega métricas totales (sin desglose por publisher) para un rango UTC.
    CVR = conversions / connected  (Conv/Conn ratio, igual que ringba_monitor.ps1).

    Retorna: {calls, connected, conversions, cvr, revenue, profit_net}
    Usado por: ringba_monitor
    """
    total_count  = 0
    connected    = 0
    conversions  = 0
    revenue      = 0.0
    profit_net   = 0.0
    offset       = 0

    for page in range(1, MAX_PAGES + 1):
        data = _post_calllogs(token, account_id, start_utc, end_utc, PAGE_SIZE, offset)

        if page == 1:
            try:
                total_count = int((data.get("report") or {}).get("totalCount") or 0)
            except (ValueError, TypeError):
                pass

        records = (data.get("report") or {}).get("records") or []
        if not records:
            break

        for r in records:
            if r.get("hasConnected") is True:
                connected  += 1
            if r.get("hasConverted") is True:
                conversions += 1
                revenue     += to_float(r.get("conversionAmount"))
            profit_net += to_float(r.get("profitNet"))

        offset += len(records)
        if total_count > 0 and offset >= total_count:
            break
        if len(records) < PAGE_SIZE:
            break

    cvr = conversions / connected if connected > 0 else 0.0

    return {
        "calls":       total_count,
        "connected":   connected,
        "conversions": conversions,
        "cvr":         cvr,
        "revenue":     revenue,
        "profit_net":  profit_net,
    }

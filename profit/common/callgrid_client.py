"""
CallGrid API client — Royalspace 2026

Cliente para el sistema de tracking de llamadas CallGrid, usado en paralelo
con Ringba durante la migración. Expone la misma interfaz que ringba_client
(get_publisher_summary, mismo formato de retorno) para poder sumar ambas
fuentes por publisher con merge_publisher_maps().

Verificado contra datos reales (2026-09-11):
  - VendorName trae el mismo formato de nombre que Ringba's publisherName,
    ej. "(19) T.I Kevin Pernia" — normalize_name() funciona igual para ambos.
  - RevenueAmount / PayoutAmount son los campos reales de dinero (los campos
    en minúscula "revenue"/"payout"/"CallRevenue"/"CallPayout" están en "0"
    incluso en llamadas con revenue real — no usar esos).
  - Los flags converted/billable/paid también aparecen en 0 en llamadas con
    revenue real, así que "conversión" se determina por RevenueAmount > 0.
  - totalCount del endpoint /api/call coincide exactamente con la métrica
    "Ended" del dashboard de CallGrid cuando el rango de fechas se calcula
    en hora Eastern (medianoche a medianoche), igual que hace Ringba.
"""
from __future__ import annotations

import json
from datetime import datetime

import requests

from common.ringba_client import normalize_name, to_float

CALLGRID_BASE_URL = "https://api.callgrid.com/api"
PAGE_SIZE = 200
MAX_PAGES = 200


def get_publisher_summary(
    api_key: str,
    start_utc: datetime,
    end_utc: datetime,
    exclude_duplicates: bool = False,
) -> dict[str, dict]:
    """
    Agrega call logs de CallGrid por publisher (VendorName) en el rango dado.
    Retorna dict: normalized_name → {raw, revenue, payout, calls, connected, conversions, profit_net}
    Mismo formato que ringba_client.get_publisher_summary().
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    publisher_map: dict[str, dict] = {}

    params: dict = {
        "useCursor": "true",
        "maxItems":  str(PAGE_SIZE),
        "startDate": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    search_after = None
    total_fetched = 0
    skipped_dupes = 0

    for _page in range(1, MAX_PAGES + 1):
        if search_after is not None:
            params["searchAfter"] = json.dumps(search_after)

        resp = requests.get(f"{CALLGRID_BASE_URL}/call", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        records = data.get("data") or []
        if not records:
            break

        for r in records:
            if exclude_duplicates and r.get("Duplicate") is True:
                skipped_dupes += 1
                continue

            raw = str(r.get("VendorName") or "")
            key = normalize_name(raw) or "unknown"

            if key not in publisher_map:
                publisher_map[key] = {
                    "raw":         raw,
                    "revenue":     0.0,
                    "payout":      0.0,
                    "calls":       0,
                    "connected":   0,
                    "conversions": 0,
                    "profit_net":  0.0,
                }

            m = publisher_map[key]
            m["calls"] += 1
            if r.get("CallConnected") is True:
                m["connected"] += 1

            revenue = to_float(r.get("RevenueAmount"))
            payout  = to_float(r.get("PayoutAmount"))
            if revenue > 0:
                m["conversions"] += 1
            m["revenue"]    += revenue
            m["payout"]     += payout
            m["profit_net"] += (revenue - payout)

        total_fetched += len(records)

        if not data.get("hasMore"):
            break
        search_after = data.get("nextCursor")
        if not search_after:
            break

    print(f"  [CallGrid] Total procesados: {total_fetched}")
    if exclude_duplicates:
        print(f"  [CallGrid] Llamadas duplicadas excluidas: {skipped_dupes}")

    return publisher_map


def merge_publisher_maps(*maps: dict[str, dict]) -> dict[str, dict]:
    """
    Suma varios publisher_map (mismo formato que get_publisher_summary) en uno
    solo, combinando por nombre normalizado. Usado para combinar Ringba +
    CallGrid durante la migración — ambos usan la misma nomenclatura de
    publishers, así que las claves coinciden directamente.
    """
    merged: dict[str, dict] = {}
    for m in maps:
        for key, vals in (m or {}).items():
            if key not in merged:
                merged[key] = {
                    "raw":         vals.get("raw", key),
                    "revenue":     0.0,
                    "payout":      0.0,
                    "calls":       0,
                    "connected":   0,
                    "conversions": 0,
                    "profit_net":  0.0,
                }
            dest = merged[key]
            dest["revenue"]     += vals.get("revenue", 0.0)
            dest["payout"]      += vals.get("payout", 0.0)
            dest["calls"]       += vals.get("calls", 0)
            dest["connected"]   += vals.get("connected", 0)
            dest["conversions"] += vals.get("conversions", 0)
            dest["profit_net"]  += vals.get("profit_net", 0.0)
    return merged

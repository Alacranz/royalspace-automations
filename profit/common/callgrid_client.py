"""
CallGrid API client — Royalspace 2026

Usa el endpoint agregado /api/reports/stats — el mismo que usa internamente
el dashboard de CallGrid (descubierto inspeccionando el Network tab del
navegador) — en vez de sumar registros crudos de /api/call uno por uno.
Esto evita depender de una suma manual propensa a errores: los totales ya
vienen calculados por CallGrid y coinciden exactamente con su propio
dashboard (verificado con datos reales, 2026-09-11).

Verificado:
  - POST con "Authorization: Bearer {api_key}" funciona igual que la sesión
    del navegador (cookie) — mismo JSON de respuesta exacto.
  - startDate/endDate son fechas simples "YYYY-MM-DD" (sin hora); el campo
    reportTimeZone hace la conversión de zona horaria del lado del servidor
    — no hace falta convertir a UTC manualmente como con /api/call.
  - pivot="VendorName" agrupa por publisher, misma nomenclatura que Ringba
    (ej. "(19) T.I Kevin Pernia") → normalize_name() ya existente aplica igual.
  - billable_count es más confiable que converted_count (que siempre viene
    en 0 en los datos de producción) para contar conversiones reales —
    verificado con el caso de Angela Monroy (payout=0 pero billable_count=13
    con revenue real, ya que Royalspace no le paga comisión a "you"/Angela).
"""
from __future__ import annotations

import re
from datetime import date

import requests

from common.ringba_client import normalize_name

CALLGRID_BASE_URL = "https://api.callgrid.com/api"

# Extrae el sub_id numérico de un nombre tipo "(1401) Rex Direct" — mismo
# formato para VendorName y BuyerName, verificado con datos reales.
_SUBID_RE = re.compile(r'^\(\s*(\d+)\s*\)\s*(.*)$')


def _bucket_value(bucket: dict, field: str) -> float:
    return float((bucket.get(field) or {}).get("value") or 0)


def _fetch_stats_buckets(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
    pivot: str,
    report_timezone: str = "US/Eastern",
) -> list[dict]:
    """
    POST /api/reports/stats — el mismo endpoint agregado que usa el
    dashboard de CallGrid internamente. Fechas de calendario simples;
    reportTimeZone hace la conversión de zona horaria del lado del servidor.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    all_buckets: list[dict] = []

    page = 0
    while True:
        body = {
            "startDate":      start_date.strftime("%Y-%m-%d"),
            "endDate":        end_date.strftime("%Y-%m-%d"),
            "pivot":          pivot,
            "pivot2":         "",
            "filters":        {"items": []},
            "permission":     "",
            "page":           page,
            "maxItems":       100,
            "reportTimeZone": report_timezone,
        }
        resp = requests.post(
            f"{CALLGRID_BASE_URL}/reports/stats",
            params={"organizationId": org_id},
            headers=headers,
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        buckets = ((data.get("aggregations") or {}).get("pivot_data") or {}).get("buckets") or []
        if not buckets:
            break
        all_buckets.extend(buckets)

        total_pages = int(data.get("totalPages") or 1)
        page += 1
        if page >= total_pages:
            break

    return all_buckets


def get_publisher_summary(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
    report_timezone: str = "US/Eastern",
) -> dict[str, dict]:
    """
    Agrega stats de CallGrid por publisher (VendorName) para el rango de
    fechas dado. Retorna el MISMO formato que
    ringba_client.get_publisher_summary():
    normalized_name → {raw, revenue, payout, calls, connected, conversions, profit_net}
    """
    publisher_map: dict[str, dict] = {}

    for b in _fetch_stats_buckets(api_key, org_id, start_date, end_date, "VendorName", report_timezone):
        key_raw = str(b.get("key") or "")
        raw = key_raw.split(":", 1)[1] if ":" in key_raw else key_raw
        key = normalize_name(raw) or "unknown"

        revenue = _bucket_value(b, "total_revenue")
        payout  = _bucket_value(b, "total_payout")

        publisher_map[key] = {
            "raw":         raw,
            "revenue":     revenue,
            "payout":      payout,
            "calls":       int(_bucket_value(b, "ended_count")),
            "connected":   int(_bucket_value(b, "connected_count")),
            "conversions": int(_bucket_value(b, "billable_count")),
            "profit_net":  revenue - payout,
        }

    print(f"  [CallGrid] Publishers agregados: {len(publisher_map)}")
    return publisher_map


def get_buyer_revenue(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
    report_timezone: str = "US/Eastern",
) -> dict[str, dict]:
    """
    Agrega stats de CallGrid por comprador (BuyerName) para el rango de
    fechas dado — usado por billing (facturación por cliente/clínica, no
    por media buyer). Retorna el MISMO formato que
    billing.ringba_buyer.get_buyer_revenue():
    buyer_sub_id (str) → {raw_name, sub_id, revenue, calls, conversions, connected}

    El sub_id se extrae del nombre "(1401) Rex Direct" — verificado que
    coincide exactamente con ringba_buyer_sub_id en billing/config.json.
    """
    buyer_map: dict[str, dict] = {}

    for b in _fetch_stats_buckets(api_key, org_id, start_date, end_date, "BuyerName", report_timezone):
        key_raw = str(b.get("key") or "")
        raw = key_raw.split(":", 1)[1] if ":" in key_raw else key_raw
        m = _SUBID_RE.match(raw)
        sub_id = m.group(1) if m else ""
        if not sub_id:
            continue

        buyer_map[sub_id] = {
            "raw_name":    raw,
            "sub_id":      sub_id,
            "revenue":     _bucket_value(b, "total_revenue"),
            "calls":       int(_bucket_value(b, "ended_count")),
            "connected":   int(_bucket_value(b, "connected_count")),
            "conversions": int(_bucket_value(b, "billable_count")),
        }

    print(f"  [CallGrid] Buyers agregados: {len(buyer_map)}")
    return buyer_map


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


def merge_buyer_maps(*maps: dict[str, dict]) -> dict[str, dict]:
    """
    Suma varios buyer_map (mismo formato que get_buyer_revenue) en uno solo,
    combinando por sub_id. Usado para combinar Ringba + CallGrid en billing —
    ambos usan el mismo sub_id numérico, así que las claves coinciden
    directamente.
    """
    merged: dict[str, dict] = {}
    for m in maps:
        for sub_id, vals in (m or {}).items():
            if sub_id not in merged:
                merged[sub_id] = {
                    "raw_name":    vals.get("raw_name", sub_id),
                    "sub_id":      sub_id,
                    "revenue":     0.0,
                    "calls":       0,
                    "connected":   0,
                    "conversions": 0,
                }
            dest = merged[sub_id]
            dest["revenue"]     += vals.get("revenue", 0.0)
            dest["calls"]       += vals.get("calls", 0)
            dest["connected"]   += vals.get("connected", 0)
            dest["conversions"] += vals.get("conversions", 0)
    return merged

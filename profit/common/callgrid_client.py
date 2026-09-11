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

from datetime import date

import requests

from common.ringba_client import normalize_name

CALLGRID_BASE_URL = "https://api.callgrid.com/api"


def _bucket_value(bucket: dict, field: str) -> float:
    return float((bucket.get(field) or {}).get("value") or 0)


def get_publisher_summary(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
    report_timezone: str = "US/Eastern",
) -> dict[str, dict]:
    """
    Agrega stats de CallGrid por publisher (VendorName) para el rango de
    fechas dado (fechas de calendario simples — la API convierte la zona
    horaria internamente). Retorna el MISMO formato que
    ringba_client.get_publisher_summary():
    normalized_name → {raw, revenue, payout, calls, connected, conversions, profit_net}
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    publisher_map: dict[str, dict] = {}

    page = 0
    while True:
        body = {
            "startDate":      start_date.strftime("%Y-%m-%d"),
            "endDate":        end_date.strftime("%Y-%m-%d"),
            "pivot":          "VendorName",
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

        for b in buckets:
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

        total_pages = int(data.get("totalPages") or 1)
        page += 1
        if page >= total_pages:
            break

    print(f"  [CallGrid] Publishers agregados: {len(publisher_map)}")
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

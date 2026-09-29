"""
CallGrid API client — DC Dashboard

Adaptado de profit/common/callgrid_client.py en royalspace-automations. Usa el
endpoint agregado POST /api/reports/stats — el mismo que usa internamente el
dashboard de CallGrid — en vez de sumar registros crudos de /api/call. Ese
enfoque crudo ya se probó en el otro repo y dio números que NO coincidían con
el dashboard oficial de CallGrid.

pivot="SourceName" (granularidad por media buyer) es NUEVO y NO está
verificado todavía contra la API en vivo — solo "VendorName" y "BuyerName"
fueron confirmados inspeccionando el Network tab del dashboard de CallGrid en
el otro repo. Correr scripts/discover_source_pivot.py antes de confiar en
get_source_revenue() en producción.
"""
from __future__ import annotations

import re
from datetime import date

import requests

CALLGRID_BASE_URL = "https://api.callgrid.com/api"

# Extrae el sub_id numérico de un nombre tipo "(44) DC1" — mismo formato
# verificado para VendorName/BuyerName en el otro repo.
_SUBID_RE = re.compile(r'^\(\s*(\d+)\s*\)\s*(.*)$')

MAX_ATTEMPTS = 5  # mismo patrón de reintentos que profit/common/ringba_client.py


def _bucket_value(bucket: dict, field: str) -> float:
    return float((bucket.get(field) or {}).get("value") or 0)


def _extract_sub_id(raw_name: str) -> str | None:
    m = _SUBID_RE.match(raw_name.strip()) if raw_name else None
    return m.group(1) if m else None


def _fetch_stats_buckets(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
    pivot: str,
    report_timezone: str = "US/Eastern",
) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    all_buckets: list[dict] = []
    page = 0

    while True:
        body = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "pivot": pivot,
            "pivot2": "",
            "filters": {"items": []},
            "permission": "",
            "page": page,
            "maxItems": 100,
            "reportTimeZone": report_timezone,
        }

        last_exc: Exception | None = None
        data = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = requests.post(
                    f"{CALLGRID_BASE_URL}/reports/stats",
                    params={"organizationId": org_id},
                    headers=headers,
                    json=body,
                    timeout=30,
                )
                if resp.status_code in (500, 502, 503, 504) and attempt < MAX_ATTEMPTS - 1:
                    import time
                    time.sleep(30 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt < MAX_ATTEMPTS - 1:
                    import time
                    time.sleep(30 * (attempt + 1))
                else:
                    raise
        if data is None:
            raise last_exc or RuntimeError("CallGrid API: reintentos agotados")

        buckets = ((data.get("aggregations") or {}).get("pivot_data") or {}).get("buckets") or []
        if not buckets:
            break
        all_buckets.extend(buckets)

        total_pages = int(data.get("totalPages") or 1)
        page += 1
        if page >= total_pages:
            break

    return all_buckets


def get_source_revenue(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
    report_timezone: str = "US/Eastern",
) -> dict[str, dict]:
    """
    Agrega payout/revenue/calls por CallGrid Source (media buyer) en el rango dado.
    Retorna dict: sub_id (str) -> {raw_name, sub_id, payout_cents, revenue_cents,
    ended_count, connected_count, billable_count}
    """
    buckets = _fetch_stats_buckets(api_key, org_id, start_date, end_date, pivot="SourceName", report_timezone=report_timezone)
    result: dict[str, dict] = {}

    for b in buckets:
        raw_name = str(b.get("key") or b.get("name") or "")
        sub_id = _extract_sub_id(raw_name)
        if not sub_id:
            continue
        result[sub_id] = {
            "raw_name": raw_name,
            "sub_id": sub_id,
            "payout_cents": round(_bucket_value(b, "total_payout") * 100),
            "revenue_cents": round(_bucket_value(b, "total_revenue") * 100),
            "ended_count": int(_bucket_value(b, "ended_count")),
            "connected_count": int(_bucket_value(b, "connected_count")),
            "billable_count": int(_bucket_value(b, "billable_count")),
        }
    return result


def get_source_revenue_by_day(
    api_key: str,
    org_id: str,
    day: date,
    report_timezone: str = "US/Eastern",
) -> dict[str, dict]:
    """Igual que get_source_revenue pero para un solo día — usado por el sync diario."""
    return get_source_revenue(api_key, org_id, day, day, report_timezone=report_timezone)

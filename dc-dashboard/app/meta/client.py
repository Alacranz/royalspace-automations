"""
Meta Ads Graph API client — DC Dashboard

Mismo patrón que profit/common/meta_client.py en royalspace-automations:
GET /act_{id}/insights con fields=spend, level=account. Reintentos con el
mismo backoff que ya se probó en profit/common/ringba_client.py.
"""
from __future__ import annotations

import time
from datetime import date

import requests

GRAPH_BASE_URL = "https://graph.facebook.com"
MAX_ATTEMPTS = 5


def get_ad_spend(
    access_token: str,
    api_version: str,
    ad_account_id: str,
    day: date,
) -> int:
    """
    Retorna el gasto (en centavos enteros) de una cuenta de Meta Ads para un
    día específico. ad_account_id sin el prefijo "act_" (se agrega acá).
    """
    url = f"{GRAPH_BASE_URL}/{api_version}/act_{ad_account_id}/insights"
    params = {
        "fields": "spend",
        "level": "account",
        "time_range": f'{{"since":"{day.isoformat()}","until":"{day.isoformat()}"}}',
        "access_token": access_token,
    }

    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code in (500, 502, 503, 504) and attempt < MAX_ATTEMPTS - 1:
                wait = 30 * (attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(30 * (attempt + 1))
            else:
                raise
    else:
        raise last_exc or RuntimeError("Meta Graph API: reintentos agotados")

    rows = data.get("data") or []
    if not rows:
        return 0  # sin gasto ese día — no es un error
    spend_dollars = float(rows[0].get("spend") or 0)
    return round(spend_dollars * 100)

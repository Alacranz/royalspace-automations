"""
Meta Ads API client — Royalspace 2026
"""
from __future__ import annotations

import requests


def get_spend(
    access_token: str,
    api_version: str,
    account_id: str,
    date_preset: str = "today",
) -> float:
    """
    Retorna el spend del día para una cuenta de Meta Ads.
    date_preset: 'today' | 'yesterday'
    Equivalente a Get-MetaAccountSpendToday / Get-MetaSpendYesterday del PS1.
    """
    clean_id = account_id.replace("act_", "")
    url = f"https://graph.facebook.com/{api_version}/act_{clean_id}/insights"
    params = {
        "fields":      "spend",
        "date_preset": date_preset,
        "level":       "account",
        "access_token": access_token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()

    data = resp.json().get("data") or []
    if data:
        try:
            return float(data[0].get("spend") or 0)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def get_spend_range(
    access_token: str,
    api_version: str,
    account_id: str,
    since: str,
    until: str,
) -> float:
    """
    Retorna el spend para un rango de fechas personalizado.
    since / until en formato YYYY-MM-DD.
    Equivalente a Get-MetaSpendYesterday pero con rango libre.
    """
    import json as _json
    clean_id = account_id.replace("act_", "")
    url = f"https://graph.facebook.com/{api_version}/act_{clean_id}/insights"
    params = {
        "fields":      "spend",
        "time_range":  _json.dumps({"since": since, "until": until}),
        "level":       "account",
        "access_token": access_token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if data:
        try:
            return float(data[0].get("spend") or 0)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def get_adset_insights(
    access_token: str,
    api_version: str,
    account_id: str,
    date_preset: str = "today",
) -> list[dict]:
    """
    Retorna métricas por anuncio (nivel 'ad') para detectar CPR alto.

    Campos por registro:
      adset_id, adset_name, ad_id, ad_name,
      spend, cost_per_result, objective, optimization_goal

    Pagina automáticamente hasta obtener todos los resultados del día.
    Usado por: alerts.py
    """
    clean_id = account_id.replace("act_", "")
    url = f"https://graph.facebook.com/{api_version}/act_{clean_id}/insights"

    fields = ",".join([
        "adset_id", "adset_name", "ad_id", "ad_name",
        "spend", "cost_per_result", "objective", "optimization_goal",
    ])

    params = {
        "fields":       fields,
        "date_preset":  date_preset,
        "level":        "ad",
        "limit":        500,
        "access_token": access_token,
    }

    results = []

    while url:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results.extend(data.get("data") or [])

        paging   = data.get("paging") or {}
        next_url = paging.get("next")
        if next_url:
            url    = next_url
            params = {}   # la URL ya lleva los parámetros embebidos
        else:
            break

    return results


def build_spend_map(
    access_token: str,
    api_version: str,
    config: dict,
    date_preset: str = "today",
    include_private_groups: bool = True,
) -> dict[str, float]:
    """
    Construye un mapa {facebook_ad_account_id → spend} para todos los MB
    (y opcionalmente para los private groups).
    """
    spend_map: dict[str, float] = {}

    if include_private_groups:
        for group in config.get("accounts_private_groups") or []:
            ad_id = str(group.get("facebook_ad_account_id") or "")
            if ad_id and ad_id not in spend_map:
                spend_map[ad_id] = get_spend(access_token, api_version, ad_id, date_preset)

    for mb in config.get("media_buyers") or []:
        ad_id = str(mb.get("facebook_ad_account_id") or "")
        if ad_id and ad_id not in spend_map:
            spend_map[ad_id] = get_spend(access_token, api_version, ad_id, date_preset)

    return spend_map

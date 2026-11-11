#!/usr/bin/env python3
"""
Foto horaria por media buyer para la Oficina de agentes — Royalspace 2026

Corre en el mismo job de profit_alerts.yml (modo meta, :20 de 10 a 23 VET), DESPUÉS
de las alertas, y no envía nada a Discord: solo manda una foto del día a la oficina
(servicio oficina-visual en Railway), que la muestra en vivo y la guarda para que el
Analista de Meta Ads la estudie al día siguiente.

Números del día (hora NY), siempre sumando las dos plataformas:
  CallGrid  /api/reports/stats pivot VendorName → revenue, payout, cost (telco), llamadas, pagadas
  Ringba    calllogs → revenue, payout, profitNet  → telco = revenue − payout − profitNet
  Meta Ads  insights nivel anuncio (today)        → gasto, resultados, ad sets sobre el CPR límite

Fórmulas (iguales a True Profit + telefonía, regla de Edwar 11-nov-2026):
  mb_profit = payout − spend × media_buyer_spend_share
  rs_profit = revenue − payout − telco − spend × royalspace_spend_share
  grupo privado: revenue − telco − 100% del gasto (sin payout)
  afiliado externo sin Meta: revenue − payout − telco
Estados: CRITICAL ≤ −$10, NEGATIVE < $0, LOW < $11, PROFITABLE ≥ $11 (sobre mb_profit).

Sin datos de pacientes: solo nombres de media buyers, ad sets y montos.
Se autentica con una firma derivada de CALLGRID_API_KEY (no hace falta secreto nuevo). Nunca debe tumbar las alertas.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytz
import requests

sys.path.insert(0, os.path.dirname(__file__))
from common.callgrid_client import _bucket_value, _fetch_stats_buckets
from common.meta_client import get_adset_insights, get_group_ad_account_ids
from common.ringba_client import get_midnight_utc, get_publisher_summary, get_today_local_date, normalize_name

NY = pytz.timezone("America/New_York")
CONFIG_PATH = Path(__file__).parent / "config.json"

RINGBA_TOKEN = os.environ.get("RINGBA_API_TOKEN", "")
RINGBA_ACCOUNT = os.environ.get("RINGBA_ACCOUNT_ID", "")
META_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
META_VERSION = os.environ.get("META_API_VERSION") or "v25.0"
CALLGRID_API_KEY = os.environ.get("CALLGRID_API_KEY", "")
CALLGRID_ORG_ID = os.environ.get("CALLGRID_ORG_ID", "")
OFICINA_URL = os.environ.get("OFICINA_URL") or "https://oficina-visual-production.up.railway.app"
# Sin secreto nuevo: la firma sale de la llave de CallGrid que ya existe en GitHub y en Railway
# (HMAC-SHA256, la llave nunca viaja). OFICINA_INGESTA_TOKEN, si existe, la reemplaza.
OFICINA_TOKEN = os.environ.get("OFICINA_INGESTA_TOKEN") or (
    hmac.new(CALLGRID_API_KEY.encode(), b"oficina-ingesta", hashlib.sha256).hexdigest() if CALLGRID_API_KEY else "")

CPR_MESSAGES = 0.90
CPR_WEBSITE = 10.00


def _parse_cpr(raw) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        item = raw[0]
        vals = item.get("values")
        try:
            return float(vals[0].get("value", 0)) if isinstance(vals, list) and vals else float(item.get("value", 0))
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _cpr_limite(objective: str, goal: str) -> float:
    o, g = (objective or "").upper(), (goal or "").upper()
    if "MESSAG" in o or "CONVERSATION" in g or "MESSAG" in g or "ENGAGEMENT" in o:
        return CPR_MESSAGES
    return CPR_WEBSITE


def estado(mb_profit: float) -> str:
    if mb_profit <= -10:
        return "CRITICAL"
    if mb_profit < 0:
        return "NEGATIVE"
    if mb_profit < 11:
        return "LOW"
    return "PROFITABLE"


def plataformas_hoy(tz_name: str) -> dict[str, dict]:
    """normalized publisher → suma de CallGrid + Ringba con telco."""
    out: dict[str, dict] = {}

    def fila(k, raw):
        return out.setdefault(k, {"raw": raw, "llamadas": 0, "conectadas": 0, "pagadas": 0, "revenue": 0.0,
                                  "payout": 0.0, "telco": 0.0, "fuentes": []})

    if CALLGRID_API_KEY and CALLGRID_ORG_ID:
        hoy = get_today_local_date(tz_name)
        for b in _fetch_stats_buckets(CALLGRID_API_KEY, CALLGRID_ORG_ID, hoy, hoy, "VendorName"):
            key_raw = str(b.get("key") or "")
            raw = key_raw.split(":", 1)[1] if ":" in key_raw else key_raw
            f = fila(normalize_name(raw) or "unknown", raw)
            f["llamadas"] += int(_bucket_value(b, "ended_count"))
            f["conectadas"] += int(_bucket_value(b, "connected_count"))
            f["pagadas"] += int(_bucket_value(b, "billable_count"))
            f["revenue"] += _bucket_value(b, "total_revenue")
            f["payout"] += _bucket_value(b, "total_payout")
            f["telco"] += _bucket_value(b, "cost")
            f["fuentes"].append("callgrid")

    if RINGBA_TOKEN and RINGBA_ACCOUNT:
        rb = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, get_midnight_utc(tz_name), datetime.now(timezone.utc))
        for k, r in rb.items():
            f = fila(k, r.get("raw", k))
            f["llamadas"] += int(r.get("calls", 0))
            f["conectadas"] += int(r.get("connected", 0))
            f["pagadas"] += int(r.get("conversions", 0))
            f["revenue"] += r.get("revenue", 0.0)
            f["payout"] += r.get("payout", 0.0)
            f["telco"] += max(0.0, r.get("revenue", 0.0) - r.get("payout", 0.0) - r.get("profit_net", 0.0))
            f["fuentes"].append("ringba")
    return out


def meta_hoy(ad_ids: list[str]) -> dict[str, dict]:
    """ad account → gasto, resultados y ad sets sobre el CPR límite (hoy, hora de la cuenta)."""
    out: dict[str, dict] = {}
    if not META_TOKEN:
        return out
    for ad_id in ad_ids:
        try:
            ads = get_adset_insights(META_TOKEN, META_VERSION, ad_id, "today")
        except Exception as e:
            print(f"  [Meta] {ad_id}: {e}")
            out[ad_id] = {"gasto": 0.0, "resultados": 0.0, "sobre_limite": [], "error": True}
            continue
        sets: dict[str, dict] = {}
        for a in ads:
            s = sets.setdefault(a.get("adset_id", ""), {"adset": a.get("adset_name", "?"), "gasto": 0.0, "resultados": 0.0,
                                                         "limite": _cpr_limite(a.get("objective"), a.get("optimization_goal"))})
            g = float(a.get("spend") or 0)
            cpr = _parse_cpr(a.get("cost_per_result"))
            s["gasto"] += g
            if cpr > 0:
                s["resultados"] += g / cpr
        sobre = []
        for s in sets.values():
            cpr = s["gasto"] / s["resultados"] if s["resultados"] else None
            if s["gasto"] >= s["limite"] and (cpr is None or cpr > s["limite"]):
                sobre.append({"adset": s["adset"][:80], "gasto": round(s["gasto"], 2),
                              "cpr": round(cpr, 2) if cpr else None, "limite": s["limite"]})
        out[ad_id] = {"gasto": sum(s["gasto"] for s in sets.values()),
                      "resultados": sum(s["resultados"] for s in sets.values()),
                      "sobre_limite": sorted(sobre, key=lambda x: -x["gasto"])[:5]}
    return out


def main() -> None:
    if not OFICINA_TOKEN:
        print("[Oficina] Sin CALLGRID_API_KEY ni OFICINA_INGESTA_TOKEN: no se envía foto.")
        return
    config = json.load(open(CONFIG_PATH, encoding="utf-8"))
    tz_name = config.get("timezone", "America/New_York")

    plat = plataformas_hoy(tz_name)
    ad_ids = [str(m.get("facebook_ad_account_id") or "") for m in config.get("media_buyers") or [] if m.get("active", True)]
    for g in config.get("accounts_private_groups") or []:
        ad_ids += get_group_ad_account_ids(g)
    meta = meta_hoy([a for a in dict.fromkeys(ad_ids) if a])

    filas, usados = [], set()

    def base(nombre, categoria, keys, cuentas):
        p = {"llamadas": 0, "conectadas": 0, "pagadas": 0, "revenue": 0.0, "payout": 0.0, "telco": 0.0}
        fuentes = set()
        for k in keys:
            usados.add(k)
            for c in p:
                p[c] += plat.get(k, {}).get(c, 0)
            fuentes |= set(plat.get(k, {}).get("fuentes", []))
        m = {"gasto": 0.0, "resultados": 0.0, "sobre_limite": []}
        for a in cuentas:
            x = meta.get(a, {})
            m["gasto"] += x.get("gasto", 0.0)
            m["resultados"] += x.get("resultados", 0.0)
            m["sobre_limite"] += x.get("sobre_limite", [])
        return {"nombre": nombre, "categoria": categoria, "plataformas": sorted(fuentes), **p, **m}

    for mb in config.get("media_buyers") or []:
        if not mb.get("active", True):
            continue
        r = base(mb["display_name"], mb.get("category", ""), [normalize_name(mb.get("publisher_name", ""))],
                 [str(mb.get("facebook_ad_account_id") or "")])
        rs_share, mb_share = float(mb.get("royalspace_spend_share", 0.5)), float(mb.get("media_buyer_spend_share", 0.5))
        r["mb_profit"] = r["payout"] - r["gasto"] * mb_share
        r["rs_profit"] = r["revenue"] - r["payout"] - r["telco"] - r["gasto"] * rs_share
        r["estado"] = estado(r["mb_profit"])
        filas.append(r)

    for g in config.get("accounts_private_groups") or []:
        if not g.get("active", True):
            continue
        r = base(g.get("group_name", "Grupo privado"), "privado", [normalize_name(p) for p in g.get("publishers") or []],
                 get_group_ad_account_ids(g))
        r["mb_profit"] = 0.0
        r["rs_profit"] = r["revenue"] - r["telco"] - r["gasto"]
        r["estado"] = "PROFITABLE" if r["rs_profit"] >= 11 else "LOW" if r["rs_profit"] >= 0 else "NEGATIVE" if r["rs_profit"] > -10 else "CRITICAL"
        filas.append(r)

    for k, p in plat.items():  # afiliados externos (sin Meta)
        if k in usados or k in ("unknown", "") or not (p["llamadas"] or p["revenue"]):
            continue
        r = {"nombre": p["raw"], "categoria": "afiliado", "plataformas": sorted(set(p["fuentes"])),
             **{c: p[c] for c in ("llamadas", "conectadas", "pagadas", "revenue", "payout", "telco")},
             "gasto": 0.0, "resultados": 0.0, "sobre_limite": []}
        r["mb_profit"] = 0.0
        r["rs_profit"] = r["revenue"] - r["payout"] - r["telco"]
        r["estado"] = "PROFITABLE" if r["rs_profit"] >= 0 else "NEGATIVE"
        filas.append(r)

    for r in filas:
        for c in ("revenue", "payout", "telco", "gasto", "resultados", "mb_profit", "rs_profit"):
            r[c] = round(r[c], 2)
        r["cpr"] = round(r["gasto"] / r["resultados"], 2) if r["resultados"] else None
        r["costo_por_pagada"] = round(r["gasto"] / r["pagadas"], 2) if r["pagadas"] and r["gasto"] else None

    ahora = datetime.now(NY)
    tot = {c: round(sum(r[c] for r in filas), 2) for c in ("llamadas", "conectadas", "pagadas", "revenue", "payout", "telco", "gasto", "rs_profit")}
    foto = {"dia": ahora.date().isoformat(), "hora_ny": ahora.strftime("%H:%M"), "fuentes": ["callgrid", "ringba", "meta"],
            "totales": tot, "media_buyers": sorted(filas, key=lambda r: r["rs_profit"])}
    resp = requests.post(f"{OFICINA_URL.rstrip('/')}/api/ingesta/mb", json=foto,
                         headers={"X-Oficina-Token": OFICINA_TOKEN}, timeout=30)
    print(f"[Oficina] Foto {foto['dia']} {foto['hora_ny']} NY: {len(filas)} filas, RS hoy ${tot['rs_profit']:.2f} → HTTP {resp.status_code}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # nunca tumbar el job de alertas
        print(f"[Oficina] Error enviando la foto: {type(e).__name__}: {e}")

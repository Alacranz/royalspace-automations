#!/usr/bin/env python3
"""
Alertas Inteligentes — Meta Ads + Ringba → Discord
Royalspace 2026

Detecta en tiempo real (corre cada hora):
  - Meta Ads  : CPR alto por ad set, según objetivo de campaña
  - Ringba    : Connection Rate o Conversion Rate bajo por publisher

Anti-spam: estado persistido en JSON entre runs via GitHub Actions cache.
             Ruta: profit/alerts_state/state.json

Routing de webhooks:
  MBs internos (Esteban, Clara, Douglas, Kevin, Luis, Edixon)
    → DISCORD_WEBHOOK_ALERTS_INTERNAL
  MBs externos (Caribay, Sebastian)
    → DISCORD_WEBHOOK_ALERTS_EXTERNAL
  Grupo privado (You, Angela) — Meta y Ringba
    → DISCORD_WEBHOOK_ALERTS_PRIVATE (canal de Angela); si no está, DISCORD_WEBHOOK_MOD
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send
from common.meta_client    import get_adset_insights, get_group_ad_account_ids
from common.ringba_client  import get_publisher_summary, normalize_name
from common.callgrid_client import (
    get_publisher_summary as get_callgrid_summary,
    merge_publisher_maps,
)

# ── Timezone ──────────────────────────────────────────────────────────────────
VET = pytz.timezone("America/Caracas")   # UTC-4, sin DST

# ── Secretos ──────────────────────────────────────────────────────────────────
RINGBA_TOKEN     = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT   = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_VERSION     = os.environ.get("META_API_VERSION") or "v25.0"
WEBHOOK_MOD      = os.environ["DISCORD_WEBHOOK_MOD"]
WEBHOOK_INTERNAL = os.environ["DISCORD_WEBHOOK_ALERTS_INTERNAL"]
WEBHOOK_EXTERNAL = os.environ["DISCORD_WEBHOOK_ALERTS_EXTERNAL"]
# Canal exclusivo del grupo privado (Angela) — 2026-10-28. Sin el secret,
# sus alertas siguen cayendo en #mod como antes.
WEBHOOK_PRIVATE  = os.environ.get("DISCORD_WEBHOOK_ALERTS_PRIVATE") or WEBHOOK_MOD
# CallGrid (migración desde Ringba) — opcional: sin esto, solo Ringba.
CALLGRID_API_KEY = os.environ.get("CALLGRID_API_KEY", "")
CALLGRID_ORG_ID  = os.environ.get("CALLGRID_ORG_ID", "")
# workflow_dispatch con coverage_report=true → solo manda a #mod el reporte de
# cobertura por MB (Ringba vs CallGrid hoy), sin evaluar alertas.
COVERAGE_REPORT  = (os.environ.get("COVERAGE_REPORT") or "").lower() == "true"

# ── Rutas ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH  = Path(__file__).parent / "alerts_state" / "state.json"

# Webhooks individuales por MB: {"display_name": "https://discord.com/..."}
_MB_WEBHOOKS: dict[str, str] = {}
try:
    _raw = os.environ.get("DISCORD_WEBHOOKS_MB_JSON", "")
    if _raw:
        _MB_WEBHOOKS = json.loads(_raw)
        print(f"[Config] {len(_MB_WEBHOOKS)} webhooks individuales de MB cargados")
except Exception as _e:
    print(f"[Config] DISCORD_WEBHOOKS_MB_JSON inválido: {_e}")

# ── Umbrales Meta ─────────────────────────────────────────────────────────────
CPR_MESSAGES = 0.90    # Objetivo Mensajes
CPR_WEBSITE  = 10.00   # Objetivo Sitio Web / Landing
MIN_SPEND    = 5.00    # Gasto mínimo para alertar (evita falsos positivos)

# Clasificación de tipo de campaña según optimization_goal
# MENSAJES: conversaciones de WhatsApp / Messenger
META_MSG_GOALS     = {"CONVERSATIONS", "REPLIES", "MESSAGING_APPOINTMENT_CONVERSION"}
# SITIO WEB / LANDING: pixel, conversiones externas, clics
META_WEBSITE_GOALS = {"OFFSITE_CONVERSIONS", "LINK_CLICKS", "LANDING_PAGE_VIEWS", "VALUE"}

# ── Umbrales Ringba ───────────────────────────────────────────────────────────
CONN_RATE_MIN = 20.0   # Connection Rate mínimo (%)
CONV_RATE_MIN = 20.0   # Conversion Rate mínimo (%)
MIN_INCOMING  = 10     # Llamadas mínimas en la hora para alertar

# ── Horarios VET ──────────────────────────────────────────────────────────────
META_ALERT_HOUR_MIN   = 10   # No alertar antes de 10 AM VET
RINGBA_ALERT_HOUR_MIN = 6    # No alertar entre 12am y 6am VET

# Ventana de llamadas a evaluar: desde la corrida anterior, entre 1h y 3h.
# GitHub descarta muchos triggers `schedule:` (confirmado 2026-10-28: 3-7
# corridas/día en vez de 24) — con una ventana fija de 1h se ignoraba la
# mayoría de las llamadas entre corridas.
CALLS_WINDOW_MIN_H = 1.0
CALLS_WINDOW_MAX_H = 3.0
ET = pytz.timezone("America/New_York")   # día de reporte de CallGrid

# ── Anti-spam ─────────────────────────────────────────────────────────────────
ALERT_REPEAT_HOURS = 2.0    # Repetir alerta si problema persiste N horas
WORSEN_FACTOR      = 1.20   # Alertar antes si el valor empeora ≥ 20%


# ═════════════════════════════════════════════════════════════════════════════
# ESTADO PERSISTIDO
# ═════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    """Carga el estado previo de alertas desde disco."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Estado] Error al cargar state.json: {e} — iniciando vacío")
    return {"meta": {}, "ringba": {}}


def save_state(state: dict) -> None:
    """Guarda el estado actualizado de alertas en disco."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ═════════════════════════════════════════════════════════════════════════════
# LÓGICA ANTI-SPAM
# ═════════════════════════════════════════════════════════════════════════════

def check_alert_needed(
    s: dict,
    current: float,
    threshold: float,
    worse_is_higher: bool = True,
) -> str:
    """
    Decide si enviar, suprimir o marcar recuperación para un indicador.

    worse_is_higher=True  → CPR: valor mayor es peor
    worse_is_higher=False → Rates: valor menor es peor

    Retorna uno de:
      "ALERT"    — primera vez que supera el umbral
      "REPEAT"   — sigue mal y ya pasaron 2 horas desde última alerta
      "WORSENED" — sigue mal y empeoró ≥ 20% adicional
      "SUPPRESS" — sigue mal pero no toca re-alertar todavía
      "RECOVERY" — estaba en alerta y ahora volvió a rango normal
      "OK"       — nunca estuvo en alerta y está bien
    """
    now_ts   = time.time()
    breached = (current > threshold) if worse_is_higher else (current < threshold)

    if not breached:
        return "RECOVERY" if s.get("in_alert") else "OK"

    # Problema activo
    if not s.get("in_alert"):
        return "ALERT"

    hours_since = (now_ts - s.get("last_alert_ts", 0)) / 3600
    if hours_since >= ALERT_REPEAT_HOURS:
        return "REPEAT"

    last_val = s.get("last_value", current)
    if worse_is_higher:
        worsened = current > last_val * WORSEN_FACTOR
    else:
        worsened = last_val > 0 and current < last_val / WORSEN_FACTOR

    return "WORSENED" if worsened else "SUPPRESS"


def mark_alert(s: dict, current: float) -> None:
    """Actualiza el estado tras enviar una alerta."""
    s["in_alert"]      = True
    s["last_alert_ts"] = time.time()
    s["last_value"]    = current


def mark_recovery(s: dict) -> None:
    """Limpia el estado tras una recuperación."""
    s["in_alert"]      = False
    s["last_alert_ts"] = 0.0
    s["last_value"]    = 0.0


# ═════════════════════════════════════════════════════════════════════════════
# ROUTING — webhook según categoría del MB
# ═════════════════════════════════════════════════════════════════════════════

def webhook_for(mb: dict) -> str:
    # Canal individual del MB tiene prioridad
    name = mb.get("display_name", "")
    if name in _MB_WEBHOOKS:
        return _MB_WEBHOOKS[name]
    # Fallback al canal compartido por categoría
    cat = str(mb.get("category") or "").lower()
    if cat == "internal":
        return WEBHOOK_INTERNAL
    if cat == "external":
        return WEBHOOK_EXTERNAL
    if cat == "private":
        return WEBHOOK_PRIVATE   # canal de Angela (o #mod si no está configurado)
    return WEBHOOK_MOD   # desconocido → #mod


# ═════════════════════════════════════════════════════════════════════════════
# META ADS — ALERTAS
# ═════════════════════════════════════════════════════════════════════════════

def cpr_threshold(objective: str, optimization_goal: str) -> float:
    """
    Determina el umbral CPR según el tipo de campaña.

    Prioridad:
      1. Si optimization_goal es explícitamente de sitio web → $10.00
      2. Si optimization_goal es de mensajes                 → $0.90
      3. Default conservador                                 → $10.00

    Nota: OUTCOME_ENGAGEMENT cubre tanto mensajes como engagement de posts,
    por eso NO se usa el objective solo — se usa el goal como discriminador.
    """
    goal = (optimization_goal or "").upper()
    if goal in META_WEBSITE_GOALS:
        return CPR_WEBSITE
    if goal in META_MSG_GOALS:
        return CPR_MESSAGES
    return CPR_WEBSITE   # desconocido → umbral más conservador


def parse_cpr(raw) -> float:
    """
    Parsea el campo cost_per_result de Meta API.

    Formatos observados en producción:
      - str / float : valor directo, e.g. "0.85"
      - list[dict]  : formato nuevo con 'values' anidado:
          [{"indicator": "actions:...", "values": [{"value": "1.003", ...}]}]
      - list[dict]  : formato antiguo con 'value' directo:
          [{"action_type": "...", "value": "0.85"}]
      - list[dict]  : sin values (sin resultados todavía):
          [{"indicator": "actions:..."}]   → retorna 0.0
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    if isinstance(raw, list) and raw:
        item = raw[0]
        if isinstance(item, dict):
            # Formato nuevo: values[0].value
            values = item.get("values")
            if isinstance(values, list) and values:
                try:
                    return float(values[0].get("value", 0))
                except (ValueError, TypeError):
                    return 0.0
            # Formato antiguo: value directo
            try:
                return float(item.get("value", 0))
            except (ValueError, TypeError):
                return 0.0
        try:
            return float(item)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def run_meta_alerts(config: dict, state: dict, now_vet: datetime) -> None:
    """Procesa alertas de Meta Ads para todas las cuentas."""
    hour = now_vet.hour
    if hour < META_ALERT_HOUR_MIN:
        print(f"[Meta] {hour:02d}:xx VET — antes de las {META_ALERT_HOUR_MIN}:00, sin alertas Meta.")
        return

    # Construir mapa: ad_account_id → mb_config
    account_map: dict[str, dict] = {}
    for mb in config.get("media_buyers") or []:
        ad_id = str(mb.get("facebook_ad_account_id") or "")
        if ad_id:
            account_map[ad_id] = mb

    # Grupos privados → canal privado de Angela, o #mod si no está configurado
    # (un grupo puede tener varias cuentas de Meta)
    for group in config.get("accounts_private_groups") or []:
        for ad_id in get_group_ad_account_ids(group):
            if ad_id:
                account_map[ad_id] = {
                    "display_name": group.get("group_name", "Royalspace Private"),
                    "category":     "private",
                    "facebook_ad_account_id": ad_id,
                }

    meta_state = state.setdefault("meta", {})

    for ad_id, mb in account_map.items():
        mb_name = mb.get("display_name", ad_id)
        hook    = webhook_for(mb)

        print(f"[Meta] {mb_name} ({ad_id})...")
        try:
            ads = get_adset_insights(META_TOKEN, META_VERSION, ad_id)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        print(f"  {len(ads)} anuncios encontrados (con gasto > $0)")

        for ad in ads:
            spend = float(ad.get("spend") or 0)

            adset_id   = ad.get("adset_id",          "")
            adset_name = ad.get("adset_name",         "?")
            ad_name    = ad.get("ad_name",            "?")
            objective  = ad.get("objective",          "")
            opt_goal   = ad.get("optimization_goal",  "")
            cpr_raw    = ad.get("cost_per_result")
            cpr        = parse_cpr(cpr_raw)
            threshold  = cpr_threshold(objective, opt_goal)
            tipo       = "MENSAJES" if threshold == CPR_MESSAGES else "SITIO WEB"

            # ── LOG DETALLADO (calibración de umbrales) ──────────────────────
            print(
                f"  AD SET : {adset_name}\n"
                f"    Anuncio       : {ad_name}\n"
                f"    objective     : {objective!r}\n"
                f"    optim. goal   : {opt_goal!r}\n"
                f"    → Tipo det.   : {tipo}\n"
                f"    → Umbral CPR  : ${threshold:.2f}\n"
                f"    CPR actual    : ${cpr:.2f}  (raw={cpr_raw!r})\n"
                f"    Gasto hoy     : ${spend:.2f}"
            )
            # ─────────────────────────────────────────────────────────────────

            if spend < MIN_SPEND:
                print(f"    → SKIP (gasto < ${MIN_SPEND:.2f})")
                continue

            if cpr <= 0:
                print(f"    → SKIP (sin resultados todavía)")
                continue

            entity_key = f"{ad_id}|{adset_id}"
            s          = meta_state.setdefault(entity_key, {})
            decision   = check_alert_needed(s, cpr, threshold, worse_is_higher=True)

            print(f"    → DECISIÓN: {decision}")

            discord_uid = mb.get("discord_user_id", "")
            mention     = f"<@{discord_uid}>\n" if discord_uid and hook != WEBHOOK_MOD else ""

            if decision in ("ALERT", "REPEAT", "WORSENED"):
                msg = mention + "\n".join([
                    "```",
                    "⚠ META ADS ALERTA",
                    "",
                    f"MB      : {mb_name}",
                    f"Ad Set  : {adset_name}",
                    f"Anuncio : {ad_name}",
                    f"CPR     : ${cpr:.2f}  (límite ${threshold:.2f})",
                    f"Gasto   : ${spend:.2f}",
                    "",
                    "Accion  : Revisar budget o pausar",
                    "```",
                ])
                try:
                    discord_send(hook, msg)
                    print(f"  → Alerta enviada")
                except Exception as e:
                    print(f"  → ERROR al enviar: {e}")
                mark_alert(s, cpr)

            elif decision == "RECOVERY":
                msg = mention + "\n".join([
                    "```",
                    f"RECUPERADO — {mb_name}",
                    f"Ad Set : {adset_name}",
                    f"CPR    : ${cpr:.2f}  (límite ${threshold:.2f})  — volvio a rango normal",
                    "```",
                ])
                try:
                    discord_send(hook, msg)
                    print(f"  → Recuperación enviada")
                except Exception as e:
                    print(f"  → ERROR al enviar recuperación: {e}")
                mark_recovery(s)

            # SUPPRESS / OK: no se envía nada


# ═════════════════════════════════════════════════════════════════════════════
# RINGBA — ALERTAS
# ═════════════════════════════════════════════════════════════════════════════

def build_publisher_map(config: dict) -> dict[str, dict]:
    """normalized_publisher_name → mb_config (MBs activos + grupos privados)."""
    pub_to_mb: dict[str, dict] = {}
    for mb in config.get("media_buyers") or []:
        if mb.get("active", True) is False:
            continue
        key = normalize_name(str(mb.get("publisher_name") or ""))
        if key:
            pub_to_mb[key] = mb
    for group in config.get("accounts_private_groups") or []:
        if group.get("active", True) is False:
            continue
        for pub in group.get("publishers") or []:
            key = normalize_name(str(pub))
            if key:
                pub_to_mb[key] = {"display_name": pub, "category": "private"}
    return pub_to_mb


def fetch_callgrid_delta(state: dict, now_utc: datetime) -> dict[str, dict]:
    """
    CallGrid solo entrega totales por DÍA (sin hora). Se pide el acumulado de
    hoy (ET) y se resta el acumulado guardado en la corrida anterior del mismo
    día → llamadas nuevas desde entonces. Sin base del mismo día (primera
    corrida del día), el delta es el acumulado desde medianoche ET.
    Si CallGrid falla se devuelve {} y NO se toca la base guardada.
    """
    if not (CALLGRID_API_KEY and CALLGRID_ORG_ID):
        print("[CallGrid] Sin credenciales — solo Ringba.")
        return {}

    today_et = now_utc.astimezone(ET).date()
    try:
        totals = get_callgrid_summary(CALLGRID_API_KEY, CALLGRID_ORG_ID, today_et, today_et)
    except Exception as e:
        print(f"[CallGrid] ERROR (solo Ringba esta corrida): {e}")
        return {}

    fields = ("calls", "connected", "conversions")
    cg_state = state.get("callgrid") or {}
    prev = cg_state.get("totals", {}) if cg_state.get("date") == today_et.isoformat() else {}
    if not prev:
        print("[CallGrid] Sin base de hoy — se usa el acumulado desde medianoche ET.")

    delta: dict[str, dict] = {}
    for key, cur in totals.items():
        before = prev.get(key, {})
        d = {f: max(0, int(cur.get(f, 0)) - int(before.get(f, 0))) for f in fields}
        if d["calls"] > 0:
            delta[key] = {"raw": cur.get("raw", key), **d}

    state["callgrid"] = {
        "date":   today_et.isoformat(),
        "totals": {k: {f: int(v.get(f, 0)) for f in fields} for k, v in totals.items()},
    }
    print(f"[CallGrid] Publishers con llamadas nuevas: {len(delta)}")
    return delta


def run_ringba_alerts(config: dict, state: dict, now_utc: datetime, now_vet: datetime) -> None:
    """Alertas de calidad de llamadas por publisher — Ringba + CallGrid combinados."""
    hour = now_vet.hour
    if 0 <= hour < RINGBA_ALERT_HOUR_MIN:
        print(f"[Llamadas] {hour:02d}:xx VET — madrugada, sin alertas de llamadas.")
        return

    last_ts = float(state.get("calls_last_run_ts") or 0)
    hours   = (now_utc.timestamp() - last_ts) / 3600 if last_ts else CALLS_WINDOW_MIN_H
    hours   = min(CALLS_WINDOW_MAX_H, max(CALLS_WINDOW_MIN_H, hours))
    start_utc = now_utc - timedelta(hours=hours)
    end_utc   = now_utc
    print(f"[Ringba] Ventana {hours:.1f}h: {start_utc.strftime('%H:%M')}–{end_utc.strftime('%H:%M')} UTC...")

    try:
        ringba_map = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)
    except Exception as e:
        print(f"[Ringba] ERROR: {e}")
        ringba_map = None

    callgrid_map = fetch_callgrid_delta(state, now_utc)
    if ringba_map is None and not callgrid_map:
        print("[Llamadas] Sin datos de Ringba ni CallGrid — se omite esta corrida.")
        return

    pub_map = merge_publisher_maps(ringba_map or {}, callgrid_map)
    state["calls_last_run_ts"] = now_utc.timestamp()
    window_label = f"ultimas {hours:.0f}h" if hours >= 1.5 else "ultima hora"

    pub_to_mb = build_publisher_map(config)

    # Cobertura: cada MB con sus llamadas por fuente, y publishers con
    # llamadas que no corresponden a ningún MB (nombre distinto en config).
    for key, mb in pub_to_mb.items():
        r = (ringba_map or {}).get(key, {}).get("calls", 0)
        c = callgrid_map.get(key, {}).get("calls", 0)
        print(f"  [Cobertura] {mb.get('display_name', key):<22} Ringba {r:>4}  CallGrid {c:>4}")
    unmatched = sorted(k for k in pub_map if k not in pub_to_mb and pub_map[k].get("calls", 0) > 0)
    if unmatched:
        print(f"  [Cobertura] Publishers con llamadas SIN MB en config: "
              + ", ".join(f"{pub_map[k].get('raw', k)} ({pub_map[k]['calls']})" for k in unmatched))

    ringba_state = state.setdefault("ringba", {})

    for pub_key, mb in pub_to_mb.items():
        rd = pub_map.get(pub_key)
        if not rd:
            continue

        incoming  = rd.get("calls",       0)
        connected = rd.get("connected",   0)
        converted = rd.get("conversions", 0)
        mb_name   = mb.get("display_name", pub_key)

        if incoming < MIN_INCOMING:
            print(f"  [{mb_name}] {incoming} llamadas — insuficiente (mín {MIN_INCOMING}), omitiendo")
            continue

        conn_rate = (connected / incoming  * 100) if incoming  > 0 else 0.0
        conv_rate = (converted / connected * 100) if connected > 0 else 0.0

        entity   = ringba_state.setdefault(pub_key, {})
        s_conn   = entity.setdefault("conn", {})
        s_conv   = entity.setdefault("conv", {})

        conn_dec = check_alert_needed(s_conn, conn_rate, CONN_RATE_MIN, worse_is_higher=False)
        conv_dec = check_alert_needed(s_conv, conv_rate, CONV_RATE_MIN, worse_is_higher=False)

        print(
            f"  [{mb_name}] In:{incoming} Con:{connected} Cvt:{converted} "
            f"ConnRate:{conn_rate:.1f}% ConvRate:{conv_rate:.1f}% "
            f"→ conn:{conn_dec} conv:{conv_dec}"
        )

        has_alert    = conn_dec in ("ALERT", "REPEAT", "WORSENED") or \
                       conv_dec in ("ALERT", "REPEAT", "WORSENED")
        has_recovery = (conn_dec == "RECOVERY") or (conv_dec == "RECOVERY")

        if has_alert:
            conn_flag = "!" if conn_dec in ("ALERT", "REPEAT", "WORSENED") else " "
            conv_flag = "!" if conv_dec in ("ALERT", "REPEAT", "WORSENED") else " "
            hook_mb   = webhook_for(mb)

            if hook_mb in (WEBHOOK_MOD, WEBHOOK_PRIVATE):
                # Publishers privados (You, Angela) → su canal (o #mod) con todos los datos
                msg = "\n".join([
                    "```",
                    "⚠ ALERTA LLAMADAS (Ringba + CallGrid)",
                    "",
                    f"Publisher   : {mb_name}",
                    f"Periodo     : {window_label}",
                    f"Llamadas    : Incoming {incoming}  Connected {connected}  Converted {converted}",
                    f"Conn Rate  {conn_flag}: {conn_rate:.1f}%  (minimo {CONN_RATE_MIN:.0f}%)",
                    f"Conv Rate  {conv_flag}: {conv_rate:.1f}%  (minimo {CONV_RATE_MIN:.0f}%)",
                    "",
                    "Accion      : Revisar grabaciones de llamadas",
                    "```",
                ])
                try:
                    discord_send(hook_mb, msg)
                    print(f"  → Alerta Ringba enviada al canal privado")
                except Exception as e:
                    print(f"  → ERROR enviando al canal privado: {e}")
            else:
                # MBs internos/externos → solo al canal del MB, sin datos financieros
                discord_uid = mb.get("discord_user_id", "")
                mention     = f"<@{discord_uid}>\n" if discord_uid else ""
                msg = mention + "\n".join([
                    "```",
                    "⚠ ALERTA DE RENDIMIENTO",
                    "",
                    f"Periodo      : {window_label}",
                    f"Llamadas     : Entrantes {incoming}  Conectadas {connected}  Convertidas {converted}",
                    f"Conn Rate  {conn_flag}: {conn_rate:.1f}%   (minimo {CONN_RATE_MIN:.0f}%)",
                    f"Conv Rate  {conv_flag}: {conv_rate:.1f}%   (minimo {CONV_RATE_MIN:.0f}%)",
                    "",
                    "Accion       : Revisar grabaciones de llamadas y",
                    "               verificar calidad del trafico",
                    "```",
                ])
                try:
                    discord_send(hook_mb, msg)
                    print(f"  → Alerta enviada al canal {mb.get('category', '')}")
                except Exception as e:
                    print(f"  → ERROR enviando alerta: {e}")

            if conn_dec in ("ALERT", "REPEAT", "WORSENED"):
                mark_alert(s_conn, conn_rate)
            if conv_dec in ("ALERT", "REPEAT", "WORSENED"):
                mark_alert(s_conv, conv_rate)

        if has_recovery:
            parts = []
            if conn_dec == "RECOVERY":
                parts.append(f"Conn Rate: {conn_rate:.1f}%")
                mark_recovery(s_conn)
            if conv_dec == "RECOVERY":
                parts.append(f"Conv Rate: {conv_rate:.1f}%")
                mark_recovery(s_conv)

            hook_mb = webhook_for(mb)
            if hook_mb in (WEBHOOK_MOD, WEBHOOK_PRIVATE):
                # Publishers privados → su canal (o #mod)
                msg = "\n".join([
                    "```",
                    f"RECUPERADO — {mb_name}",
                    f"{' | '.join(parts)}  — volvio a rango normal",
                    "```",
                ])
                try:
                    discord_send(hook_mb, msg)
                    print(f"  → Recuperación enviada al canal privado")
                except Exception as e:
                    print(f"  → ERROR enviando recuperación al canal privado: {e}")
            else:
                # MBs internos/externos → silencioso, solo actualizar estado
                print(f"  → [{mb_name}] Recuperado — estado actualizado, sin notificacion al canal")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def run_coverage_report(config: dict, now_utc: datetime) -> None:
    """
    Reporte manual a #mod: llamadas de HOY (desde medianoche ET) por MB en
    Ringba y CallGrid, más publishers con llamadas que no matchean ningún MB
    del config (nombre distinto en Ringba/CallGrid → sus alertas no salen).
    No toca el estado anti-spam.
    """
    today_et  = now_utc.astimezone(ET).date()
    start_utc = ET.localize(datetime(today_et.year, today_et.month, today_et.day)).astimezone(timezone.utc)

    try:
        ringba = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, now_utc)
        ringba_err = ""
    except Exception as e:
        ringba, ringba_err = {}, str(e)[:80]
    callgrid, callgrid_err = {}, ""
    if CALLGRID_API_KEY and CALLGRID_ORG_ID:
        try:
            callgrid = get_callgrid_summary(CALLGRID_API_KEY, CALLGRID_ORG_ID, today_et, today_et)
        except Exception as e:
            callgrid_err = str(e)[:80]
    else:
        callgrid_err = "sin credenciales"

    pub_to_mb = build_publisher_map(config)
    lines = [
        "```",
        f"COBERTURA ALERTAS — hoy {today_et.strftime('%d/%m')} (ET)",
        "",
        f"{'MB':<22}{'Ringba':>8}{'CallGrid':>10}",
    ]
    for key, mb in pub_to_mb.items():
        r = ringba.get(key, {}).get("calls", 0)
        c = callgrid.get(key, {}).get("calls", 0)
        flag = "  <- sin llamadas" if r == 0 and c == 0 else ""
        lines.append(f"{mb.get('display_name', key)[:21]:<22}{r:>8}{c:>10}{flag}")
    if ringba_err:
        lines.append(f"\nRingba ERROR: {ringba_err}")
    if callgrid_err:
        lines.append(f"CallGrid: {callgrid_err}")

    unmatched = [
        (src, v.get("raw", k), v.get("calls", 0))
        for src, m in (("Ringba", ringba), ("CallGrid", callgrid))
        for k, v in m.items() if k not in pub_to_mb and v.get("calls", 0) > 0
    ]
    lines.append("")
    if unmatched:
        lines.append("Publishers con llamadas SIN MB en config:")
        lines += [f"  {src:<8} {raw[:40]} ({calls})" for src, raw, calls in sorted(unmatched)]
    else:
        lines.append("Todos los publishers con llamadas tienen MB asignado.")
    lines.append("```")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1890] + "\n...```"
    discord_send(WEBHOOK_MOD, msg)
    print(msg)


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    now_vet = now_utc.astimezone(VET)
    print(f"[Alertas] {now_vet.strftime('%Y-%m-%d %H:%M')} VET")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    if COVERAGE_REPORT:
        run_coverage_report(config, now_utc)
        return

    state = load_state()

    run_meta_alerts(config, state, now_vet)
    run_ringba_alerts(config, state, now_utc, now_vet)

    save_state(state)
    print("[Alertas] Finalizado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print(f"ERROR FATAL: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            discord_send(WEBHOOK_MOD, f"[ALERTAS ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

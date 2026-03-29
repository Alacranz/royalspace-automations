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
    → DISCORD_WEBHOOK_MB_INTERNAL
  MBs externos (Caribay, Sebastian)
    → DISCORD_WEBHOOK_MB_EXTERNAL
  Publishers propios de RS (You, Angela) y Ringba
    → DISCORD_WEBHOOK_MOD
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
from common.meta_client    import get_adset_insights
from common.ringba_client  import get_publisher_summary, normalize_name

# ── Timezone ──────────────────────────────────────────────────────────────────
VET = pytz.timezone("America/Caracas")   # UTC-4, sin DST

# ── Secretos ──────────────────────────────────────────────────────────────────
RINGBA_TOKEN     = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT   = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_VERSION     = os.environ.get("META_API_VERSION", "v25.0")
WEBHOOK_MOD      = os.environ["DISCORD_WEBHOOK_MOD"]
WEBHOOK_INTERNAL = os.environ["DISCORD_WEBHOOK_MB_INTERNAL"]
WEBHOOK_EXTERNAL = os.environ["DISCORD_WEBHOOK_MB_EXTERNAL"]

# ── Rutas ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH  = Path(__file__).parent / "alerts_state" / "state.json"

# ── Umbrales Meta ─────────────────────────────────────────────────────────────
CPR_MESSAGES = 0.90    # Objetivo Mensajes
CPR_WEBSITE  = 10.00   # Objetivo Sitio Web / Landing
MIN_SPEND    = 5.00    # Gasto mínimo para alertar (evita falsos positivos)

# Objetivos Meta que se clasifican como "Mensajes"
META_MSG_OBJECTIVES = {"MESSAGES", "OUTCOME_ENGAGEMENT"}
META_MSG_GOALS      = {"CONVERSATIONS", "REPLIES", "MESSAGING_APPOINTMENT_CONVERSION"}

# ── Umbrales Ringba ───────────────────────────────────────────────────────────
CONN_RATE_MIN = 20.0   # Connection Rate mínimo (%)
CONV_RATE_MIN = 20.0   # Conversion Rate mínimo (%)
MIN_INCOMING  = 10     # Llamadas mínimas en la hora para alertar

# ── Horarios VET ──────────────────────────────────────────────────────────────
META_ALERT_HOUR_MIN   = 10   # No alertar antes de 10 AM VET
RINGBA_ALERT_HOUR_MIN = 6    # No alertar entre 12am y 6am VET

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
    cat = str(mb.get("category") or "").lower()
    if cat == "internal":
        return WEBHOOK_INTERNAL
    if cat == "external":
        return WEBHOOK_EXTERNAL
    return WEBHOOK_MOD   # private / desconocido → #mod


# ═════════════════════════════════════════════════════════════════════════════
# META ADS — ALERTAS
# ═════════════════════════════════════════════════════════════════════════════

def cpr_threshold(objective: str, optimization_goal: str) -> float:
    """Determina el umbral CPR según el tipo de campaña."""
    obj  = (objective        or "").upper()
    goal = (optimization_goal or "").upper()
    if obj in META_MSG_OBJECTIVES or goal in META_MSG_GOALS:
        return CPR_MESSAGES
    return CPR_WEBSITE


def parse_cpr(raw) -> float:
    """
    Parsea el campo cost_per_result de Meta API.
    Puede llegar como str, float, list[dict] o None.
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

    # Grupos privados → #mod
    for group in config.get("accounts_private_groups") or []:
        ad_id = str(group.get("facebook_ad_account_id") or "")
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

            if decision in ("ALERT", "REPEAT", "WORSENED"):
                msg = (
                    f"⚠️ **META ADS ALERTA**\n"
                    f"👤 MB: {mb_name}\n"
                    f"🎯 Ad Set: {adset_name}\n"
                    f"📢 Anuncio: {ad_name}\n"
                    f"💰 CPR actual: ${cpr:.2f} (límite: ${threshold:.2f})\n"
                    f"📊 Gasto hoy: ${spend:.2f}\n"
                    f"🔴 Acción sugerida: Revisar budget o pausar"
                )
                try:
                    discord_send(hook, msg)
                    print(f"  → Alerta enviada")
                except Exception as e:
                    print(f"  → ERROR al enviar: {e}")
                mark_alert(s, cpr)

            elif decision == "RECOVERY":
                msg = (
                    f"✅ **RECUPERADO**: {mb_name} — {adset_name}\n"
                    f"CPR volvió a rango normal: ${cpr:.2f} (límite: ${threshold:.2f})"
                )
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

def run_ringba_alerts(config: dict, state: dict, now_utc: datetime, now_vet: datetime) -> None:
    """Procesa alertas de Ringba por publisher (última hora)."""
    hour = now_vet.hour
    if 0 <= hour < RINGBA_ALERT_HOUR_MIN:
        print(f"[Ringba] {hour:02d}:xx VET — madrugada, sin alertas Ringba.")
        return

    start_utc = now_utc - timedelta(hours=1)
    end_utc   = now_utc
    print(f"[Ringba] Última hora: {start_utc.strftime('%H:%M')}–{end_utc.strftime('%H:%M')} UTC...")

    try:
        pub_map = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)
    except Exception as e:
        print(f"[Ringba] ERROR: {e}")
        return

    # Mapa: normalized_publisher_name → mb_config
    pub_to_mb: dict[str, dict] = {}
    for mb in config.get("media_buyers") or []:
        key = normalize_name(str(mb.get("publisher_name") or ""))
        if key:
            pub_to_mb[key] = mb
    for group in config.get("accounts_private_groups") or []:
        for pub in group.get("publishers") or []:
            key = normalize_name(str(pub))
            if key:
                pub_to_mb[key] = {"display_name": pub, "category": "private"}

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
            conn_line = (
                f"📉 Connection Rate: {conn_rate:.1f}% (mínimo: {CONN_RATE_MIN:.0f}%)"
                if conn_dec in ("ALERT", "REPEAT", "WORSENED") else
                f"✅ Connection Rate: {conn_rate:.1f}%"
            )
            conv_line = (
                f"📉 Conversion Rate: {conv_rate:.1f}% (mínimo: {CONV_RATE_MIN:.0f}%)"
                if conv_dec in ("ALERT", "REPEAT", "WORSENED") else
                f"✅ Conversion Rate: {conv_rate:.1f}%"
            )
            msg = (
                f"⚠️ **RINGBA ALERTA**\n"
                f"👤 Publisher: {mb_name}\n"
                f"📞 Incoming: {incoming} | Connected: {connected} | Converted: {converted}\n"
                f"{conn_line}\n"
                f"{conv_line}\n"
                f"🔴 Acción sugerida: Revisar grabaciones de llamadas"
            )
            try:
                discord_send(WEBHOOK_MOD, msg)
                print(f"  → Alerta Ringba enviada a #mod")
            except Exception as e:
                print(f"  → ERROR: {e}")

            if conn_dec in ("ALERT", "REPEAT", "WORSENED"):
                mark_alert(s_conn, conn_rate)
            if conv_dec in ("ALERT", "REPEAT", "WORSENED"):
                mark_alert(s_conv, conv_rate)

        if has_recovery:
            parts = []
            if conn_dec == "RECOVERY":
                parts.append(f"Connection Rate: {conn_rate:.1f}%")
                mark_recovery(s_conn)
            if conv_dec == "RECOVERY":
                parts.append(f"Conversion Rate: {conv_rate:.1f}%")
                mark_recovery(s_conv)
            msg = (
                f"✅ **RECUPERADO**: {mb_name} — {', '.join(parts)} "
                f"volvió a rango normal"
            )
            try:
                discord_send(WEBHOOK_MOD, msg)
                print(f"  → Recuperación Ringba enviada a #mod")
            except Exception as e:
                print(f"  → ERROR: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    now_utc = datetime.now(timezone.utc)
    now_vet = now_utc.astimezone(VET)
    print(f"[Alertas] {now_vet.strftime('%Y-%m-%d %H:%M')} VET")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

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

#!/usr/bin/env python3
"""
Reporte diario de costos — Dentista Latino Webhook
Envía a Discord #mod un resumen de uso y costos de Anthropic y Railway.
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

WEBHOOK_URL        = os.environ["DISCORD_WEBHOOK_MOD"]
WEBHOOK_STATS_URL  = os.environ["WEBHOOK_STATS_URL"]        # https://...railway.app/stats
RAILWAY_TOKEN      = os.environ.get("RAILWAY_TOKEN", "")
ANTHROPIC_BALANCE  = float(os.environ.get("ANTHROPIC_BALANCE", "13.25"))  # balance inicial conocido

# ── 1. Estadísticas del webhook ───────────────────────────────────────────────

def get_webhook_stats() -> dict:
    try:
        r = requests.get(WEBHOOK_STATS_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Error] No se pudo obtener stats del webhook: {e}")
        return {}

# ── 2. Créditos de Railway ────────────────────────────────────────────────────

def get_railway_credits() -> dict:
    if not RAILWAY_TOKEN:
        return {"error": "sin token"}
    query = """
    query {
      me {
        usage {
          estimatedUsage
          currentPeriodEnd
        }
        credits
      }
    }
    """
    try:
        r = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": query},
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}"},
            timeout=15,
        )
        data = r.json()
        me = data.get("data", {}).get("me", {})
        usage = me.get("usage", {})
        return {
            "estimated_usage": usage.get("estimatedUsage", 0),
            "credits":         me.get("credits", 0),
        }
    except Exception as e:
        print(f"[Error] Railway API: {e}")
        return {"error": str(e)}

# ── 3. Componer mensaje Discord ───────────────────────────────────────────────

def build_message(stats: dict, railway: dict) -> str:
    now   = datetime.now(timezone.utc)
    today = now.strftime("%d/%m/%Y")

    msgs_today  = stats.get("messages_today", "?")
    msgs_month  = stats.get("messages_month", "?")
    convs_today = stats.get("conversations_today", "?")
    convs_month = stats.get("conversations_month", "?")
    cost_today  = stats.get("cost_today_usd", 0)
    cost_month  = stats.get("cost_month_usd", 0)

    # Proyección del mes basada en días transcurridos
    day_of_month = now.day
    if day_of_month > 0 and isinstance(cost_month, (int, float)) and cost_month > 0:
        days_in_month = 30
        daily_avg     = cost_month / day_of_month
        projection    = daily_avg * days_in_month
    else:
        projection = 0.0

    # Balance estimado de Anthropic (balance inicial - gasto total acumulado)
    anthropic_remaining = ANTHROPIC_BALANCE - cost_month
    anthropic_pct       = (anthropic_remaining / ANTHROPIC_BALANCE * 100) if ANTHROPIC_BALANCE else 0

    # Alertas
    alerts = []
    if isinstance(anthropic_remaining, float) and anthropic_remaining < 3.0:
        alerts.append("🚨 ANTHROPIC: balance crítico < $3.00 — recargar YA")
    elif isinstance(anthropic_remaining, float) and anthropic_remaining < 6.0:
        alerts.append("⚠️ ANTHROPIC: balance bajo < $6.00 — recargar pronto")

    railway_credits = railway.get("credits", None)
    railway_usage   = railway.get("estimated_usage", None)
    if railway_credits is not None and railway_credits < 100:  # Railway credits en centavos
        alerts.append("⚠️ RAILWAY: créditos bajos — revisar plan")

    alert_block = "\n".join(alerts) if alerts else "✅ Todo en orden"

    # Railway display
    if "error" in railway:
        railway_block = f"  Sin datos (configura RAILWAY_TOKEN)"
    else:
        rw_used = f"${railway_usage/100:.2f}" if railway_usage is not None else "?"
        rw_bal  = f"${railway_credits/100:.2f}" if railway_credits is not None else "$5.00 (trial)"
        railway_block = f"  Gastado este mes   : {rw_used}\n  Créditos restantes : {rw_bal}"

    lines = [
        "```",
        f"REPORTE DIARIO — {today}",
        "",
        "─── WEBHOOK MANYCHAT ───────────────────",
        f"  Mensajes hoy        : {msgs_today}",
        f"  Mensajes este mes   : {msgs_month}",
        f"  Contactos hoy       : {convs_today}",
        f"  Contactos este mes  : {convs_month}",
        f"  Costo hoy           : ${cost_today:.4f}",
        f"  Costo este mes      : ${cost_month:.4f}",
        f"  Proyección mensual  : ${projection:.2f}",
        "",
        "─── ANTHROPIC (Claude Haiku) ───────────",
        f"  Balance disponible  : ${anthropic_remaining:.2f}  ({anthropic_pct:.0f}%)",
        f"  Gastado este mes    : ${cost_month:.4f}",
        f"  Proyección mensual  : ${projection:.2f}",
        "",
        "─── RAILWAY ────────────────────────────",
        railway_block,
        "",
        "─── ALERTAS ────────────────────────────",
        f"  {alert_block}",
        "```",
    ]
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Obteniendo stats del webhook...")
    stats = get_webhook_stats()

    print("Consultando Railway...")
    railway = get_railway_credits()

    message = build_message(stats, railway)
    print(message)

    r = requests.post(WEBHOOK_URL, json={"content": message}, timeout=15)
    r.raise_for_status()
    print("✅ Reporte enviado a Discord")


if __name__ == "__main__":
    main()

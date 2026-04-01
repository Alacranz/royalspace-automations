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
ANTHROPIC_BALANCE  = float(os.environ.get("ANTHROPIC_BALANCE") or "13.25")

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
    headers = {"Authorization": f"Bearer {RAILWAY_TOKEN}"}
    # Precios Railway Hobby (USD por unidad)
    PRICES = {
        "CPU_USAGE":        0.000463,   # por vCPU-minute
        "MEMORY_USAGE_GB":  0.000231,   # por GB-minute
        "NETWORK_TX_GB":    0.10,       # por GB egress
        "NETWORK_RX_GB":    0.0,        # ingress gratis
    }
    # Paso 1: obtener el projectId del proyecto del webhook
    query_projects = """
    query {
      projects { nodes { id name } }
    }
    """
    try:
        rp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": query_projects},
            headers=headers,
            timeout=15,
        )
        projects = rp.json().get("data", {}).get("projects", {}).get("nodes", [])
        print(f"[Railway] projects: {[(p['name'], p['id']) for p in projects]}")

        # Buscar el proyecto del webhook (nombre contiene "royalspace" o "manychat")
        project_id = None
        for p in projects:
            name_lower = p.get("name", "").lower()
            if any(k in name_lower for k in ["royalspace", "manychat", "webhook", "dental"]):
                project_id = p["id"]
                break
        if not project_id and projects:
            project_id = projects[0]["id"]  # usar el primero si no hay match
        print(f"[Railway] using projectId: {project_id}")

        # Paso 2: estimatedUsage por proyecto con precios reales
        query_usage = f"""
        query {{
          estimatedUsage(
            measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB, NETWORK_RX_GB]
            projectId: "{project_id}"
          ) {{
            projectId
            measurement
            estimatedValue
          }}
        }}
        """
        ru = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": query_usage},
            headers=headers,
            timeout=15,
        )
        raw = ru.json()
        errors = raw.get("errors", [])
        if errors:
            print(f"[Railway] error: {errors[0].get('message')}")
            return {"static": True}

        items = raw.get("data", {}).get("estimatedUsage", [])
        total_usd = sum(
            item.get("estimatedValue", 0) * PRICES.get(item.get("measurement", ""), 0)
            for item in items
        )
        print(f"[Railway] cost breakdown: { {i['measurement']: round(i['estimatedValue']*PRICES.get(i['measurement'],0),4) for i in items} }")
        print(f"[Railway] total estimated USD: {total_usd:.4f}")
        return {"estimated_usd": total_usd}
    except Exception as e:
        print(f"[Error] Railway: {e}")
        return {"static": True}

# ── 3. GitHub Actions usage ──────────────────────────────────────────────────

def get_github_usage() -> dict:
    token = os.environ.get("GH_BILLING_TOKEN", "")
    owner = os.environ.get("GITHUB_REPO_OWNER", "")
    if not token or not owner:
        return {"error": "sin token"}
    # Intenta endpoint de org primero, luego user
    for url in [
        f"https://api.github.com/orgs/{owner}/settings/billing/actions",
        f"https://api.github.com/users/{owner}/settings/billing/actions",
    ]:
        try:
            r = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "used_minutes":     data.get("total_minutes_used", 0),
                    "included_minutes": data.get("included_minutes", 2000),
                    "paid_minutes":     data.get("total_paid_minutes_used", 0),
                }
        except Exception as e:
            return {"error": str(e)}
    # Cuenta personal sin acceso al billing API — mostrar info estática
    return {"static": True, "used_minutes": 0, "included_minutes": 2000}


# ── 4. Componer mensaje Discord ───────────────────────────────────────────────

def build_message(stats: dict, railway: dict, github: dict) -> str:
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

    gh_used  = github.get("used_minutes", 0)
    gh_total = github.get("included_minutes", 2000)
    if "error" not in github:
        gh_pct = gh_used / gh_total * 100 if gh_total else 0
        if gh_pct >= 90:
            alerts.append("🚨 GITHUB: minutos al límite (>90%) — acciones pueden fallar")
        elif gh_pct >= 75:
            alerts.append("⚠️ GITHUB: minutos al 75% — monitorear")

    alert_block = "\n".join(alerts) if alerts else "✅ Todo en orden"

    # Railway display
    if "error" in railway:
        railway_block = f"  Error: {railway['error']}"
    elif "estimated_usd" in railway:
        rw_est   = railway["estimated_usd"]
        rw_free  = 5.00
        rw_left  = max(0.0, rw_free - rw_est)
        rw_pct   = rw_est / rw_free * 100 if rw_free else 0
        railway_block = (
            f"  Gastado este mes   : ${rw_est:.4f} ({rw_pct:.0f}% del free tier)\n"
            f"  Credito restante   : ${rw_left:.2f} / $5.00"
        )
        if rw_pct >= 90:
            alerts.append("🚨 RAILWAY: uso al límite del free tier ($5/mes)")
        elif rw_pct >= 70:
            alerts.append("⚠️ RAILWAY: uso al 70% del free tier ($5/mes)")
    else:
        railway_block = "  Hobby plan — ver uso: railway.com/dashboard"

    # GitHub display
    if "error" in github:
        github_block = f"  Error: {github['error']}"
    else:
        gh_left = gh_total - gh_used
        gh_pct  = gh_used / gh_total * 100 if gh_total else 0
        static_note = " (estimado)" if github.get("static") else ""
        github_block = (
            f"  Minutos usados     : {gh_used} / {gh_total} ({gh_pct:.0f}%){static_note}\n"
            f"  Minutos restantes  : {gh_left}\n"
            f"  Plan               : Free (2,000 min/mes gratis)"
        )

    lines = [
        "```",
        f"REPORTE DIARIO — {today}",
        "",
        "--- WEBHOOK MANYCHAT -------------------",
        f"  Mensajes hoy        : {msgs_today}",
        f"  Mensajes este mes   : {msgs_month}",
        f"  Contactos hoy       : {convs_today}",
        f"  Contactos este mes  : {convs_month}",
        f"  Costo hoy           : ${cost_today:.4f}",
        f"  Costo este mes      : ${cost_month:.4f}",
        f"  Proyeccion mensual  : ${projection:.2f}",
        "",
        "--- ANTHROPIC (Claude Haiku) -----------",
        f"  Balance disponible  : ${anthropic_remaining:.2f}  ({anthropic_pct:.0f}%)",
        f"  Gastado este mes    : ${cost_month:.4f}",
        f"  Proyeccion mensual  : ${projection:.2f}",
        "",
        "--- RAILWAY ----------------------------",
        railway_block,
        "",
        "--- GITHUB ACTIONS ---------------------",
        github_block,
        "",
        "--- ALERTAS ----------------------------",
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

    print("Consultando GitHub Actions...")
    github = get_github_usage()

    message = build_message(stats, railway, github)
    print(message)

    r = requests.post(WEBHOOK_URL, json={"content": message}, timeout=15)
    r.raise_for_status()
    print("✅ Reporte enviado a Discord")


if __name__ == "__main__":
    main()

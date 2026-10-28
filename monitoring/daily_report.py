#!/usr/bin/env python3
"""
Reporte diario de costos — Dentista Latino Webhook
Envía a Discord #mod un resumen de uso y costos de Anthropic y Railway.
"""
import os
import time
from datetime import datetime, timezone

import requests

WEBHOOK_URL        = os.environ["DISCORD_WEBHOOK_MOD"]
_raw_stats_url     = os.environ["WEBHOOK_STATS_URL"]
WEBHOOK_STATS_URL  = _raw_stats_url if _raw_stats_url.startswith("http") else f"https://{_raw_stats_url}"
STATS_API_KEY      = os.environ.get("STATS_API_KEY", "")   # /stats exige X-Api-Key desde c79a1f7
RAILWAY_TOKEN      = os.environ.get("RAILWAY_TOKEN", "")
# Workspace "Alacranz's Projects" (no es secreto). estimatedUsage sin
# workspaceId/projectId devuelve "Not Authorized".
RAILWAY_WORKSPACE_ID = os.environ.get("RAILWAY_WORKSPACE_ID") or "1a779787-715c-46fa-a153-8c26144e13f7"
RAILWAY_API_URL    = "https://backboard.railway.com/graphql/v2"

# ── 1. Estadísticas del webhook ───────────────────────────────────────────────

def get_webhook_stats() -> dict:
    """Pide stats de ayer al endpoint /stats?date=YYYY-MM-DD."""
    from datetime import timedelta
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"{WEBHOOK_STATS_URL}?date={yesterday}"
    # Railway puede estar hibernado — intentar hasta 3 veces con pausa entre intentos
    if not STATS_API_KEY:
        return {"_error": "falta el secret STATS_API_KEY en GitHub"}
    last_err = ""
    for attempt in range(1, 4):
        try:
            print(f"[Stats] Intento {attempt}/3 — {url}")
            r = requests.get(url, headers={"X-Api-Key": STATS_API_KEY}, timeout=45)
            if r.status_code == 401:
                # No tiene sentido reintentar: la clave no coincide con la de Railway
                return {"_error": "401 — STATS_API_KEY de GitHub no coincide con la de Railway"}
            r.raise_for_status()
            data = r.json()
            print(f"[Stats] OK: {data}")
            return data
        except Exception as e:
            last_err = str(e)
            print(f"[Stats] Intento {attempt} fallido: {e}")
            if attempt < 3:
                time.sleep(15)
    print(f"[Error] Stats no disponibles tras 3 intentos: {last_err}")
    return {"_error": last_err}

# ── 2. Créditos de Railway ────────────────────────────────────────────────────

def get_railway_credits() -> dict:
    if not RAILWAY_TOKEN:
        return {"error": "falta el secret RAILWAY_TOKEN en GitHub"}
    headers = {"Authorization": f"Bearer {RAILWAY_TOKEN}"}
    # Precios Railway Hobby (USD por unidad de recurso)
    PRICES = {
        "CPU_USAGE":       0.000463,  # por vCPU-minute
        "MEMORY_USAGE_GB": 0.000231,  # por GB-minute
        "NETWORK_TX_GB":   0.10,      # por GB egress
        "NETWORK_RX_GB":   0.0,       # ingress gratis
    }
    query_usage = """
    query($workspaceId: String) {
      estimatedUsage(workspaceId: $workspaceId, measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB, NETWORK_RX_GB]) {
        projectId
        measurement
        estimatedValue
      }
    }
    """
    try:
        r = requests.post(
            RAILWAY_API_URL,
            json={"query": query_usage, "variables": {"workspaceId": RAILWAY_WORKSPACE_ID}},
            headers=headers,
            timeout=15,
        )
        raw = r.json()
        errors = raw.get("errors", [])
        if errors:
            msg = errors[0].get("message", "error desconocido")
            print(f"[Railway] error: {msg}")
            if msg == "Not Authorized":
                msg = "token inválido o vencido — crear uno nuevo (Railway invalidó todos el 22-oct)"
            return {"error": msg}
        items = raw.get("data", {}).get("estimatedUsage", [])
        total_usd = sum(
            item.get("estimatedValue", 0) * PRICES.get(item.get("measurement", ""), 0)
            for item in items
        )
        breakdown = {i["measurement"]: round(i["estimatedValue"] * PRICES.get(i["measurement"], 0), 4) for i in items}
        print(f"[Railway] breakdown USD: {breakdown}")
        print(f"[Railway] total USD: {total_usd:.4f}")
        return {"estimated_usd": total_usd}
    except Exception as e:
        print(f"[Error] Railway: {e}")
        return {"error": str(e)[:120]}

# ── 3. GitHub Actions usage ──────────────────────────────────────────────────

def get_github_usage() -> dict:
    """Calcula minutos de GitHub Actions sumando duración de runs del mes actual."""
    token = os.environ.get("GH_BILLING_TOKEN", "")
    owner = os.environ.get("GITHUB_REPO_OWNER", "")
    repo  = "royalspace-automations"
    if not token or not owner:
        return {"error": "sin token"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    now = datetime.now(timezone.utc)
    # Primer día del mes actual
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        total_seconds = 0
        page = 1
        while True:
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
                headers=headers,
                params={"per_page": 100, "page": page, "created": f">={month_start}", "status": "completed"},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[GitHub] HTTP {r.status_code}: {r.text[:200]}")
                return {"error": f"HTTP {r.status_code}"}
            data = r.json()
            runs = data.get("workflow_runs", [])
            if not runs:
                break
            for run in runs:
                # run_duration_ms no siempre está — calcular desde created_at/updated_at
                dur_ms = run.get("run_duration_ms") or 0
                if not dur_ms:
                    try:
                        fmt = "%Y-%m-%dT%H:%M:%SZ"
                        created = datetime.strptime(run["created_at"], fmt).replace(tzinfo=timezone.utc)
                        updated = datetime.strptime(run["updated_at"], fmt).replace(tzinfo=timezone.utc)
                        dur_ms = (updated - created).total_seconds() * 1000
                    except Exception:
                        dur_ms = 0
                total_seconds += dur_ms / 1000
            if len(runs) < 100:
                break
            page += 1

        used_minutes   = int(total_seconds / 60)
        included       = 2000
        return {
            "used_minutes":     used_minutes,
            "included_minutes": included,
            "paid_minutes":     0,
        }
    except Exception as e:
        print(f"[Error] GitHub: {e}")
        return {"error": str(e)[:120]}


# ── 4. Componer mensaje Discord ───────────────────────────────────────────────

def build_message(stats: dict, railway: dict, github: dict) -> str:
    now   = datetime.now(timezone.utc)
    today = now.strftime("%d/%m/%Y")

    stats_error = stats.get("_error")
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

    # Alertas — incluye fuentes de datos caídas: antes decía "Todo en orden"
    # aunque ManyChat/Railway no hubieran devuelto nada.
    alerts = []
    if stats_error:
        alerts.append("⚠️ MANYCHAT: sin datos — ver ERROR arriba")
    if "error" in railway:
        alerts.append("⚠️ RAILWAY: sin datos — ver Error arriba")
    if "error" in github:
        alerts.append("⚠️ GITHUB: sin datos — ver Error arriba")

    gh_used  = github.get("used_minutes", 0)
    gh_total = github.get("included_minutes", 2000)
    if "error" not in github:
        gh_pct = gh_used / gh_total * 100 if gh_total else 0
        if gh_pct >= 90:
            alerts.append("🚨 GITHUB: minutos al límite (>90%) — acciones pueden fallar")
        elif gh_pct >= 75:
            alerts.append("⚠️ GITHUB: minutos al 75% — monitorear")

    # Railway display
    if "error" in railway:
        railway_block = f"  Error: {railway['error']}"
    else:
        rw_est   = railway["estimated_usd"]
        rw_incl  = 5.00   # crédito de uso incluido en el plan Hobby
        rw_pct   = rw_est / rw_incl * 100 if rw_incl else 0
        railway_block = (
            f"  Estimado del mes    : ${rw_est:.2f} ({rw_pct:.0f}% del crédito Hobby de $5)"
        )
        if rw_pct >= 100:
            alerts.append("⚠️ RAILWAY: el estimado del mes supera el crédito de $5 del plan Hobby")

    # GitHub display
    if "error" in github:
        github_block = f"  Error: {github['error']}"
    else:
        gh_left = gh_total - gh_used
        gh_pct  = gh_used / gh_total * 100 if gh_total else 0
        github_block = (
            f"  Minutos usados     : {gh_used} / {gh_total} ({gh_pct:.0f}%)\n"
            f"  Minutos restantes  : {gh_left}\n"
            f"  Plan               : Free (2,000 min/mes gratis)"
        )

    alert_block = "\n  ".join(alerts) if alerts else "✅ Todo en orden"
    na = lambda v: "N/A" if stats_error else v

    lines = [
        "```",
        f"REPORTE DIARIO — {today}",
        "",
        "--- WEBHOOK MANYCHAT -------------------",
        f"  Mensajes ayer       : {na(msgs_today)}",
        f"  Mensajes este mes   : {na(msgs_month)}",
        f"  Contactos ayer      : {na(convs_today)}",
        f"  Contactos este mes  : {na(convs_month)}",
        *([ f"  ERROR               : {stats_error}" ] if stats_error else []),
        "",
        "--- ANTHROPIC (bot ManyChat) -----------",
        f"  Gastado ayer        : {na(f'${cost_today:.2f}')}",
        f"  Gastado este mes    : {na(f'${cost_month:.2f}')}",
        f"  Proyeccion mensual  : {na(f'${projection:.2f}')}",
        "  Balance             : ver console.anthropic.com",
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

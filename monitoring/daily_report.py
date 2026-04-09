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
RAILWAY_TOKEN      = os.environ.get("RAILWAY_TOKEN", "")
ANTHROPIC_BALANCE  = float(os.environ.get("ANTHROPIC_BALANCE") or "50.20")

# ── 1. Estadísticas del webhook ───────────────────────────────────────────────

def get_webhook_stats() -> dict:
    """Pide stats de ayer al endpoint /stats?date=YYYY-MM-DD."""
    from datetime import timedelta
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"{WEBHOOK_STATS_URL}?date={yesterday}"
    # Railway puede estar hibernado — intentar hasta 3 veces con pausa entre intentos
    last_err = ""
    for attempt in range(1, 4):
        try:
            print(f"[Stats] Intento {attempt}/3 — {url}")
            r = requests.get(url, timeout=45)
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
        return {"error": "sin token"}
    headers = {"Authorization": f"Bearer {RAILWAY_TOKEN}"}
    # Precios Railway Hobby (USD por unidad de recurso)
    PRICES = {
        "CPU_USAGE":       0.000463,  # por vCPU-minute
        "MEMORY_USAGE_GB": 0.000231,  # por GB-minute
        "NETWORK_TX_GB":   0.10,      # por GB egress
        "NETWORK_RX_GB":   0.0,       # ingress gratis
    }
    query_usage = """
    query {
      estimatedUsage(measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB, NETWORK_RX_GB]) {
        projectId
        measurement
        estimatedValue
      }
    }
    """
    try:
        r = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": query_usage},
            headers=headers,
            timeout=15,
        )
        raw = r.json()
        errors = raw.get("errors", [])
        if errors:
            print(f"[Railway] error: {errors[0].get('message')}")
            return {"static": True}
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
        return {"static": True}

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
        return {"static": True, "used_minutes": 0, "included_minutes": 2000}


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
        f"  Mensajes ayer       : {msgs_today}",
        f"  Mensajes este mes   : {msgs_month}",
        f"  Contactos ayer      : {convs_today}",
        f"  Contactos este mes  : {convs_month}",
        f"  Costo ayer          : {f'${cost_today:.4f}' if not stats_error else 'N/A'}",
        f"  Costo este mes      : {f'${cost_month:.4f}' if not stats_error else 'N/A'}",
        f"  Proyeccion mensual  : {f'${projection:.2f}' if not stats_error else 'N/A'}",
        *([ f"  ERROR               : {stats_error}" ] if stats_error else []),
        "",
        "--- ANTHROPIC (Claude Haiku) -----------",
        f"  Balance disponible  : ${anthropic_remaining:.2f}  ({anthropic_pct:.0f}%)",
        f"  Gastado ayer        : {f'${cost_today:.4f}' if not stats_error else 'N/A'}",
        f"  Gastado este mes    : {f'${cost_month:.4f}' if not stats_error else 'N/A'}",
        f"  Proyeccion mensual  : {f'${projection:.2f}' if not stats_error else 'N/A'}",
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

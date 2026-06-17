#!/usr/bin/env python3
"""
MB Performance Advisor — Royalspace 2026

Corre al final del día laboral (7 PM EST, L-V).
Por cada MB activo:
  1. Datos de hoy (Ringba + Meta): spend, profit, calls, CVR
  2. Tendencia de los últimos 7 días
  3. Adsets de Meta con CPR alto y bajo
  4. Claude Haiku genera recomendaciones accionables en español
  5. Envía @mención al MB en Discord con el análisis

Mensajes:
  - Internos → DISCORD_WEBHOOK_MB_INTERNAL
  - Externos  → DISCORD_WEBHOOK_MB_EXTERNAL
  - Resumen   → DISCORD_WEBHOOK_MOD

Secrets requeridos:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  META_ACCESS_TOKEN, META_API_VERSION
  ANTHROPIC_API_KEY
  DISCORD_WEBHOOK_MOD, DISCORD_WEBHOOK_MB_INTERNAL, DISCORD_WEBHOOK_MB_EXTERNAL
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(__file__))
from common.business_hours import is_business_hours
from common.discord_client import send as discord_send
from common.meta_client import build_spend_map, get_adset_insights
from common.ringba_client import (
    get_midnight_utc,
    get_publisher_summary,
    normalize_name,
)

# ── Secrets ───────────────────────────────────────────────────────────────────

RINGBA_TOKEN     = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT   = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_VERSION     = os.environ.get("META_API_VERSION") or "v25.0"
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
WEBHOOK_MOD      = os.environ["DISCORD_WEBHOOK_MOD"]
WEBHOOK_INTERNAL = os.environ["DISCORD_WEBHOOK_MB_INTERNAL"]
WEBHOOK_EXTERNAL = os.environ["DISCORD_WEBHOOK_MB_EXTERNAL"]

# Webhooks individuales por MB (opcional)
_MB_WEBHOOKS: dict[str, str] = {}
try:
    _raw = os.environ.get("DISCORD_WEBHOOKS_MB_JSON", "")
    if _raw:
        _MB_WEBHOOKS = json.loads(_raw)
except Exception as _e:
    print(f"[Config] DISCORD_WEBHOOKS_MB_JSON inválido: {_e}")

CONFIG_PATH = Path(__file__).parent / "config.json"
TZ_NAME     = "America/New_York"


def _webhook_for(mb: dict) -> str:
    """Canal individual del MB si existe, si no el canal compartido por categoría."""
    name = mb.get("display_name", "")
    if name in _MB_WEBHOOKS:
        return _MB_WEBHOOKS[name]
    cat = str(mb.get("category") or "").lower()
    return WEBHOOK_INTERNAL if cat == "internal" else WEBHOOK_EXTERNAL


def fmt(v: float) -> str:
    return f"${v:.2f}"


def mb_status(mb_profit: float) -> str:
    if mb_profit <= -10:
        return "CRITICAL"
    if mb_profit < 0:
        return "NEGATIVE"
    if mb_profit < 11:
        return "LOW"
    return "PROFITABLE"


def trend_arrow(today: float, avg_7d: float) -> str:
    if avg_7d == 0:
        return "→"
    pct = ((today - avg_7d) / abs(avg_7d)) * 100
    if pct > 10:
        return f"↑ +{pct:.0f}%"
    if pct < -10:
        return f"↓ {pct:.0f}%"
    return f"→ {pct:+.0f}%"


# ── Claude Haiku ──────────────────────────────────────────────────────────────

def generate_advice(mb_name: str, context: dict) -> str:
    """
    Llama a Claude Haiku para generar recomendaciones accionables.
    context incluye: today, trend_7d, best_adsets, worst_adsets, status
    """
    prompt = f"""Eres el advisor de performance de Royalspace, agencia de marketing digital en el sector dental.
Media buyer: {mb_name}
Spend: {context['spend_today']} | Profit: {context['profit_today']} ({context['status']}) | CVR: {context['cvr']:.1%} | Tendencia: {context['trend']}
Peores adsets: {context['worst_adsets']}
Mejores adsets: {context['best_adsets']}

Da exactamente 2 acciones concretas para hoy. Sin headers, sin markdown, sin numeración. Máximo 2 líneas cortas en español.

REGLA ABSOLUTA: Nunca uses estas palabras ni variantes: pay-per-call, pay per call, leadgen, lead gen, lead generation, afiliado, affiliate, payout, publisher, Ringba, pixel, tracking. Usa solo lenguaje estándar: presupuesto, conversiones, campañas, anuncios, resultados, rendimiento."""

    headers = {
        "x-api-key":         ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 120,
        "messages":   [{"role": "user", "content": prompt}],
    }

    # Retry con backoff para errores transitorios (529 overloaded, 503, timeouts)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
                timeout=30,
            )
            if resp.status_code in (429, 503, 529) and attempt < 2:
                wait = 2 ** attempt  # 1s, 2s
                print(f"  [Claude] {resp.status_code} — reintentando en {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip()
        except (requests.exceptions.RequestException,) as e:
            last_exc = e
            if attempt < 2:
                wait = 2 ** attempt
                print(f"  [Claude] Error de red ({e}) — reintentando en {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise last_exc or RuntimeError("Claude API: reintentos agotados")


# ── Adset analysis ────────────────────────────────────────────────────────────

def analyze_adsets(adsets: list[dict], min_spend: float = 5.0) -> tuple[str, str]:
    """
    Retorna (worst_str, best_str) con los adsets más y menos eficientes.
    Filtra adsets con spend < min_spend para evitar ruido.
    """
    filtered = []
    for a in adsets:
        try:
            spend = float(a.get("spend") or 0)
            cpr_raw = a.get("cost_per_result")
            if not cpr_raw:
                continue
            # cost_per_result puede ser lista o dict con "value"
            if isinstance(cpr_raw, list):
                cpr = float(cpr_raw[0].get("value", 0)) if cpr_raw else 0
            elif isinstance(cpr_raw, dict):
                cpr = float(cpr_raw.get("value", 0))
            else:
                cpr = float(cpr_raw)
            if spend >= min_spend and cpr > 0:
                filtered.append({
                    "name":  a.get("ad_name") or a.get("adset_name") or "Unknown",
                    "spend": spend,
                    "cpr":   cpr,
                })
        except (ValueError, TypeError):
            continue

    if not filtered:
        return "Sin datos suficientes", "Sin datos suficientes"

    sorted_by_cpr = sorted(filtered, key=lambda x: x["cpr"])
    best  = sorted_by_cpr[:3]
    worst = sorted_by_cpr[-3:][::-1]

    def fmt_adsets(items):
        lines = []
        for a in items:
            name = a["name"][:35]
            lines.append(f"  • {name} — CPR {fmt(a['cpr'])} (spend {fmt(a['spend'])})")
        return "\n".join(lines) if lines else "  • Sin datos"

    return fmt_adsets(worst), fmt_adsets(best)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    now_utc   = datetime.now(timezone.utc)
    today_start = get_midnight_utc(TZ_NAME)

    # ── 7-day range ───────────────────────────────────────────────────────────
    week_start = today_start - timedelta(days=7)

    print("Consultando Ringba (hoy)...")
    ringba_today = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, today_start, now_utc)

    print("Consultando Ringba (7 días)...")
    ringba_week = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, week_start, today_start)

    print("Consultando Meta spend (hoy)...")
    spend_map = build_spend_map(META_TOKEN, META_VERSION, config, "today", include_private_groups=False)

    # ── Process each MB ───────────────────────────────────────────────────────
    summary_rows: list[str] = []

    for mb in config.get("media_buyers") or []:
        if not mb.get("active"):
            continue

        name     = mb["display_name"]
        key      = normalize_name(str(mb.get("publisher_name") or ""))
        ad_id    = str(mb.get("facebook_ad_account_id") or "")
        user_id  = mb.get("discord_user_id", "")
        mb_share = float(mb.get("media_buyer_spend_share", 0.5))

        # Today data
        rd_today   = ringba_today.get(key) or {}
        spend      = spend_map.get(ad_id, 0.0)
        payout     = rd_today.get("payout", 0.0)
        calls      = rd_today.get("calls", 0)
        connected  = rd_today.get("connected", 0)
        conversions = rd_today.get("conversions", 0)
        cvr        = conversions / connected if connected > 0 else 0.0
        mb_profit  = payout - (spend * mb_share)
        status     = mb_status(mb_profit)

        # Skip inactive MBs (no spend AND no calls today)
        if spend == 0 and calls == 0:
            print(f"  [{name}] Sin actividad hoy — omitiendo")
            continue

        # 7-day average
        rd_week      = ringba_week.get(key) or {}
        payout_7d    = rd_week.get("payout", 0.0)
        avg_profit_7d = (payout_7d - 0) / 7  # simplified: no spend data per day
        trend        = trend_arrow(mb_profit, avg_profit_7d / 7 if avg_profit_7d else mb_profit)

        # Adset insights
        print(f"  [{name}] Consultando adsets Meta...")
        try:
            adsets = get_adset_insights(META_TOKEN, META_VERSION, ad_id, "today")
            worst_str, best_str = analyze_adsets(adsets)
        except Exception as e:
            print(f"  [{name}] Error adsets: {e}")
            worst_str, best_str = "No disponible", "No disponible"

        # Claude advice
        print(f"  [{name}] Generando recomendaciones con Claude...")
        try:
            advice = generate_advice(name, {
                "spend_today":  fmt(spend),
                "profit_today": fmt(mb_profit),
                "status":       status,
                "calls":        calls,
                "connected":    connected,
                "cvr":          cvr,
                "trend":        trend,
                "worst_adsets": worst_str,
                "best_adsets":  best_str,
            })
        except Exception as e:
            print(f"  [{name}] Error Claude: {e}")
            advice = "No se pudo generar análisis automático."

        # Build Discord message
        status_emoji = {"PROFITABLE": "🟢", "LOW": "🟡", "NEGATIVE": "🔴", "CRITICAL": "🚨"}.get(status, "⚪")
        mention = f"<@{user_id}>" if user_id else f"**{name}**"

        msg_lines = [
            f"{mention} — **Análisis del día**",
            "```",
            f"Spend:   {fmt(spend):>10}   Profit MB: {fmt(mb_profit):>10}  {status_emoji} {status}",
            f"Llamadas:{calls:>5}   Conectadas:{connected:>5}   Conv:{conversions:>4}   CVR: {cvr:.1%}",
            f"Tendencia 7d: {trend}",
            "```",
        ]
        if worst_str not in ("Sin datos suficientes", "No disponible"):
            msg_lines += [f"**🔴 Pausar:**", f"```\n{worst_str}\n```"]
        if best_str not in ("Sin datos suficientes", "No disponible"):
            msg_lines += [f"**🟢 Escalar:**", f"```\n{best_str}\n```"]
        msg_lines += [f"**💡** {advice}"]
        msg = "\n".join(msg_lines)
        if len(msg) > 1900:
            msg = msg[:1900] + "\n..."

        # Enviar directamente al canal individual del MB (o al compartido como fallback)
        try:
            discord_send(_webhook_for(mb), msg)
            print(f"  [{name}] ✓ Mensaje enviado a Discord")
        except Exception as e:
            print(f"  [{name}] Error enviando a Discord: {e}")

        summary_rows.append(
            f"{'✅' if status == 'PROFITABLE' else '⚠️' if status == 'LOW' else '❌'} "
            f"{name:<20} {fmt(spend):>8} → {fmt(mb_profit):>8} ({status})"
        )

        print(f"  [{name}] ✓ {status} | Spend: {fmt(spend)} | Profit: {fmt(mb_profit)}")

    # Summary to MOD
    if summary_rows:
        now_et = datetime.now(timezone.utc) - timedelta(hours=4)
        summary = "```\nMB ADVISOR SUMMARY — " + now_et.strftime("%d/%m/%Y %I:%M %p ET") + "\n"
        summary += "━" * 50 + "\n"
        summary += "\n".join(summary_rows)
        summary += "\n```"
        discord_send(WEBHOOK_MOD, summary)

    print(f"\nAdvisor completado: {len(summary_rows)} MB(s) procesados.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            discord_send(WEBHOOK_MOD, f"[MB ADVISOR ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

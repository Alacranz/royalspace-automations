#!/usr/bin/env python3
"""
Executive Daily Brief — Royalspace 2026

Corre a las 8 AM VET (12:00 UTC) lunes-sábado.
Consolida Ringba + Meta (ayer), facturación (Zoho/Sheets) y ManyChat
en un único mensaje ejecutivo con diagnóstico generado por Claude Haiku.

Secrets requeridos:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  META_ACCESS_TOKEN, META_API_VERSION (opcional)
  GOOGLE_SERVICE_ACCOUNT_JSON, BILLING_SPREADSHEET_ID
  ANTHROPIC_API_KEY
  DISCORD_WEBHOOK_MOD
  WEBHOOK_STATS_URL         (opcional — stats de ManyChat)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import requests

# ── Path setup ────────────────────────────────────────────────────────────────
_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, os.path.join(_ROOT, "profit"))   # common.*
sys.path.insert(0, _ROOT)                            # billing.*

from common.discord_client import send as discord_send          # noqa: E402
from common.meta_client    import build_spend_map               # noqa: E402
from common.ringba_client  import (                             # noqa: E402
    get_publisher_summary, get_yesterday_utc_range, normalize_name
)
from billing.payment_tracker import (                            # noqa: E402
    get_outstanding_invoices, get_pending_state,
)

# ── Secrets ───────────────────────────────────────────────────────────────────
RINGBA_TOKEN     = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT   = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_VERSION     = os.environ.get("META_API_VERSION") or "v25.0"
GSHEETS_CREDS    = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID   = os.environ["BILLING_SPREADSHEET_ID"]
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
WEBHOOK_MOD      = os.environ["DISCORD_WEBHOOK_MOD"]
WEBHOOK_STATS    = os.environ.get("WEBHOOK_STATS_URL", "")

PROFIT_CONFIG  = os.path.join(_ROOT, "profit",  "config.json")
BILLING_CONFIG = os.path.join(_ROOT, "billing", "config.json")

DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(v: float) -> str:
    return f"${v:,.2f}"


def _claude_diagnosis(prompt: str) -> str:
    """Calls Claude Haiku via REST and returns a short diagnosis."""
    if not ANTHROPIC_KEY:
        return "API key de Anthropic no configurada."
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 250,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if not resp.ok:
            detail = f"Claude API {resp.status_code}: {resp.text[:500]}"
            print(f"  [Claude] Error: {detail}")
            return f"Error al generar diagnóstico: {detail}"
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [Claude] Error: {e}")
        return f"Error al generar diagnóstico: {e}"


def _get_manychat_stats() -> dict:
    if not WEBHOOK_STATS:
        return {}
    try:
        r = requests.get(WEBHOOK_STATS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [ManyChat] Error: {e}")
        return {}


# ── Main ─────────────────────────────────────────────────────────────────────

def run() -> None:
    with open(PROFIT_CONFIG,  encoding="utf-8") as f:
        profit_cfg = json.load(f)
    with open(BILLING_CONFIG, encoding="utf-8") as f:
        billing_cfg = json.load(f)

    tz_name = profit_cfg.get("timezone", "America/New_York")
    tz      = pytz.timezone(tz_name)

    yesterday      = (datetime.now(tz) - timedelta(days=1)).date()
    report_date    = yesterday.strftime("%d/%m/%Y")
    day_name       = DAYS_ES[yesterday.weekday()]
    start_utc, end_utc = get_yesterday_utc_range(tz_name)

    print(f"\n[Executive Brief] Procesando {report_date} ({day_name})")

    # ── 1. Fetch Meta + Ringba ─────────────────────────────────────────────────
    print("\n[Meta] Consultando spend de ayer...")
    spend_map = build_spend_map(
        META_TOKEN, META_VERSION, profit_cfg, "yesterday", include_private_groups=True
    )

    print("\n[Ringba] Consultando calllogs de ayer...")
    ringba = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)

    # ── 2. Profit calculation ──────────────────────────────────────────────────
    # Private groups
    priv_revenue = priv_spend = priv_profit = 0.0
    for group in profit_cfg.get("accounts_private_groups") or []:
        g_rev   = sum(
            ringba.get(normalize_name(str(p)), {}).get("revenue", 0.0)
            for p in (group.get("publishers") or [])
        )
        g_spend  = spend_map.get(str(group.get("facebook_ad_account_id") or ""), 0.0)
        priv_revenue += g_rev
        priv_spend   += g_spend
        priv_profit  += g_rev - g_spend

    # Media buyers
    mb_rs_profit = mb_mb_profit = total_spend = total_payout = 0.0
    mb_rows: list[dict] = []

    for mb in profit_cfg.get("media_buyers") or []:
        key      = normalize_name(str(mb.get("publisher_name") or ""))
        ad_id    = str(mb.get("facebook_ad_account_id") or "")
        rd       = ringba.get(key) or {}

        revenue      = rd.get("revenue", 0.0)
        payout       = rd.get("payout",  0.0)
        spend        = spend_map.get(ad_id, 0.0)
        rs_share     = float(mb.get("royalspace_spend_share",  0.5))
        mb_share_pct = float(mb.get("media_buyer_spend_share", 0.5))
        mb_share_amt = spend * mb_share_pct
        mb_p         = payout - mb_share_amt
        rs_p         = (revenue - payout) - (spend * rs_share)

        mb_rs_profit  += rs_p
        mb_mb_profit  += mb_p
        total_spend   += spend
        total_payout  += payout

        mb_rows.append({
            "name":      mb["display_name"],
            "mb_profit": mb_p,
            "rs_profit": rs_p,
        })

    rs_total     = priv_profit + mb_rs_profit
    combined_net = rs_total + mb_mb_profit

    best_mb  = max(mb_rows, key=lambda r: r["rs_profit"]) if mb_rows else None
    worst_mb = min(mb_rows, key=lambda r: r["rs_profit"]) if mb_rows else None

    # ── 3. Billing data ────────────────────────────────────────────────────────
    print("\n[Sheets] Consultando facturas pendientes...")
    outstanding = get_outstanding_invoices(SPREADSHEET_ID, GSHEETS_CREDS)
    pending_invoices = [r for r in outstanding if not r["overdue"]]
    overdue_invoices = [r for r in outstanding if r["overdue"]]

    def parse_amount(s: str) -> float:
        try:
            return float(str(s).replace("$", "").replace(",", ""))
        except ValueError:
            return 0.0

    pending_total = sum(parse_amount(r["revenue"]) for r in pending_invoices)
    overdue_total = sum(parse_amount(r["revenue"]) for r in overdue_invoices)

    print("\n[Sheets] Consultando acumulación de revenue...")
    accumulating: list[dict] = []
    accum_total = 0.0
    for buyer in billing_cfg.get("auto_invoice_buyers") or []:
        if not buyer.get("active", True):
            continue
        state = get_pending_state(SPREADSHEET_ID, GSHEETS_CREDS, buyer["discord_name"])
        if state["pending_revenue"] > 0:
            accumulating.append({
                "name":    buyer["discord_name"],
                "amount":  state["pending_revenue"],
                "months":  state["months_accumulated"],
                "from_m":  state["from_month"],
            })
            accum_total += state["pending_revenue"]

    # ── 4. ManyChat stats ──────────────────────────────────────────────────────
    mc_stats = _get_manychat_stats()

    # ── 5. Claude diagnosis ────────────────────────────────────────────────────
    print("\n[Claude] Generando diagnóstico...")
    diag_input = (
        f"Datos de ayer ({report_date}) para Royalspace, empresa de dental PPC.\n"
        f"RS Net Profit: {fmt(rs_total)} | Combined Net: {fmt(combined_net)}\n"
        f"Payout total: {fmt(total_payout)} | Spend total: {fmt(total_spend)}\n"
        f"Mejor MB: {best_mb['name']} {fmt(best_mb['rs_profit'])} RS | "
        f"Peor MB: {worst_mb['name']} {fmt(worst_mb['rs_profit'])} RS\n"
        f"Facturas pendientes: {len(pending_invoices)} (${pending_total:,.2f}) | "
        f"Vencidas: {len(overdue_invoices)} (${overdue_total:,.2f})\n"
        f"Buyers acumulando: {len(accumulating)} (${accum_total:,.2f} pendiente)\n"
    )
    if mc_stats:
        total_convs = mc_stats.get("conversations_today", "?")
        diag_input += f"Conversaciones ManyChat ayer: {total_convs}\n"

    diag_prompt = (
        "Eres el asistente ejecutivo de Royalspace. Con los datos de ayer, "
        "redacta un diagnóstico ejecutivo en español usando EXACTAMENTE este formato con 3 líneas separadas:\n\n"
        "**Operación:** [1 oración sobre el profit total y eficiencia del día]\n\n"
        "**Media Buyers:** [1 oración: quién lidera y quién requiere atención]\n\n"
        "**Facturación:** [1 oración sobre el estado de facturas pendientes y vencidas]\n\n"
        "Sé directo y concreto. Sin saludos, sin cierre, sin texto adicional fuera del formato.\n\n"
        + diag_input
    )
    diagnosis = _claude_diagnosis(diag_prompt)

    # ── 6. Build Discord message ───────────────────────────────────────────────
    lines = [
        "```",
        f"BRIEF EJECUTIVO — {report_date} ({day_name})",
        "",
        "── PROFIT DE AYER ──────────────────────────────",
        f"  RS Total       : {fmt(rs_total)}",
        f"  Combined Net   : {fmt(combined_net)}",
        f"  RS (privado)   : {fmt(priv_profit)}",
        f"  RS (de MBs)    : {fmt(mb_rs_profit)}",
        f"  MB Profit      : {fmt(mb_mb_profit)}",
        f"  Payout total   : {fmt(total_payout)}",
        f"  Spend total    : {fmt(total_spend)}",
        "",
    ]

    if best_mb and worst_mb:
        lines.append(f"  Mejor MB : {best_mb['name'][:18]} · {fmt(best_mb['rs_profit'])} RS")
        lines.append(f"  Peor MB  : {worst_mb['name'][:18]} · {fmt(worst_mb['rs_profit'])} RS")
        lines.append("")

    lines += [
        "── FACTURACION ─────────────────────────────────",
        f"  Pendientes : {len(pending_invoices)} factura(s) · {fmt(pending_total)}",
        f"  Vencidas   : {len(overdue_invoices)} factura(s) · {fmt(overdue_total)}",
    ]

    if accumulating:
        lines.append(f"  Acumulando : {len(accumulating)} buyer(s) · {fmt(accum_total)}")
        for a in accumulating:
            months = f"{a['months']} mes(es) desde {a['from_m']}" if a["from_m"] else f"{a['months']} mes(es)"
            lines.append(f"    {a['name'][:18]:<18} ${a['amount']:>9,.2f}  ({months})")

    lines.append("")

    if mc_stats:
        total_convs = mc_stats.get("conversations_today", "?")
        lines += [
            "── MANYCHAT ─────────────────────────────────────",
            f"  Conversaciones ayer : {total_convs}",
            "",
        ]

    lines += [
        "── DIAGNOSTICO ──────────────────────────────────",
        "```",
        "",
        diagnosis,
    ]

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1880] + "\n...\n```"

    print("\n[Discord] Enviando brief ejecutivo a #mod...")
    discord_send(WEBHOOK_MOD, msg)
    print("\n[Executive Brief] Listo.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        import traceback
        print(f"ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            discord_send(WEBHOOK_MOD, f"[EXECUTIVE BRIEF ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

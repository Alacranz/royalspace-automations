#!/usr/bin/env python3
"""
True Profit + MB Alerts — Ringba + Meta → Discord
Royalspace 2026 | Port de royalspace_true_profit.ps1 + royalspace_mb_alerts.ps1

Ambos scripts comparten datos y cadencia (cada 30 min), por lo que se
ejecutan en un solo run enviando 3 mensajes:

  1. True Profit → DISCORD_WEBHOOK_MOD (#mod)
       Resumen global: RS Profit, Combined Net, tabla MB (Rev/Spend/MB/RS),
       Top 3 External.

  2. MB Internal Performance → DISCORD_WEBHOOK_MB_INTERNAL (#mb-alerts)
       Tabla de MBs internos: Payout, Spend, MB Share, Profit, Status.

  3. MB External Performance → DISCORD_WEBHOOK_MB_EXTERNAL (#mb-alerts externo)
       Misma tabla para MBs externos.

Status de MB (de royalspace_mb_alerts.ps1):
  PROFITABLE : MB profit ≥ $11
  LOW        : $0 – $10.99
  NEGATIVE   : < $0
  CRITICAL   : ≤ -$10

Solo corre en horario laboral EST (lunes-viernes 8-20, sábado 8-14).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytz

sys.path.insert(0, os.path.dirname(__file__))
from common.business_hours import is_business_hours
from common.discord_client import send as discord_send
from common.meta_client import build_spend_map
from common.ringba_client import get_midnight_utc, get_publisher_summary, normalize_name

# ── Secretos ──────────────────────────────────────────────────────────────────
RINGBA_TOKEN      = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT    = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN        = os.environ["META_ACCESS_TOKEN"]
META_VERSION      = os.environ.get("META_API_VERSION", "v25.0")
WEBHOOK_MOD       = os.environ["DISCORD_WEBHOOK_MOD"]
WEBHOOK_INTERNAL  = os.environ["DISCORD_WEBHOOK_MB_INTERNAL"]
WEBHOOK_EXTERNAL  = os.environ["DISCORD_WEBHOOK_MB_EXTERNAL"]

CONFIG_PATH = Path(__file__).parent / "config.json"


def fmt(v: float) -> str:
    return f"${v:.2f}"


def mb_status(mb_profit: float) -> str:
    """Replica Get-MbStatus de royalspace_mb_alerts.ps1."""
    if mb_profit <= -10:
        return "CRITICAL"
    if mb_profit <= 0:
        return "NEGATIVE"
    if mb_profit < 11:
        return "LOW"
    return "PROFITABLE"


def build_mb_alerts_report(title: str, rows: list[dict]) -> str:
    """
    Formato de royalspace_mb_alerts.ps1 → New-MbDiscordReport.
    Columnas: Name | Payout | Spend | MB Share | Profit | Status
    Ordenado por MB Profit descendente.
    """
    sorted_rows = sorted(rows, key=lambda r: r["mb_profit"], reverse=True)
    lines = [
        "```",
        title,
        "",
        "Name                 Payout    Spend   MB Share   Profit     Status",
        "---------------------------------------------------------------------",
    ]
    for r in sorted_rows:
        name   = r["name"][:20].ljust(20)
        payout = fmt(r["payout"]).rjust(8)
        spend  = fmt(r["spend"]).rjust(8)
        share  = fmt(r["mb_share_amt"]).rjust(9)
        profit = fmt(r["mb_profit"]).rjust(8)
        status = mb_status(r["mb_profit"]).rjust(10)
        lines.append(f"{name} {payout} {spend} {share} {profit} {status}")

    if not sorted_rows:
        lines.append("No data")

    lines.append("```")
    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n..."
    return msg


def main() -> None:
    if not is_business_hours():
        print("Fuera de horario laboral. Script detenido.")
        return

    # Primer run del día (8:00 AM EST): enviar daily summary de ayer primero
    now_est = datetime.now(pytz.timezone("America/New_York"))
    if now_est.hour == 8:
        print("=== Primer run del día — enviando MB Daily Summary de ayer ===")
        from mb_daily_summary import main as daily_summary_main
        daily_summary_main()
        print("=== Daily Summary completado ===\n")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    tz_name = config.get("timezone", "America/New_York")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    start_utc = get_midnight_utc(tz_name)
    end_utc   = datetime.now(timezone.utc)

    print("Consultando Meta spend (today)...")
    spend_map = build_spend_map(META_TOKEN, META_VERSION, config, "today", include_private_groups=True)

    print("Consultando Ringba (today)...")
    ringba = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)

    # ── Publishers conocidos (para detectar afiliados externos) ───────────────
    known: set[str] = set()
    for group in config.get("accounts_private_groups") or []:
        for pub in group.get("publishers") or []:
            known.add(normalize_name(str(pub)))
    for mb in config.get("media_buyers") or []:
        known.add(normalize_name(str(mb.get("publisher_name") or "")))

    # ── Private Groups ────────────────────────────────────────────────────────
    priv_revenue = priv_spend = priv_profit = 0.0
    for group in config.get("accounts_private_groups") or []:
        g_rev = sum(
            ringba.get(normalize_name(str(p)), {}).get("revenue", 0.0)
            for p in (group.get("publishers") or [])
        )
        g_spend  = spend_map.get(str(group.get("facebook_ad_account_id") or ""), 0.0)
        g_profit = g_rev - g_spend
        priv_revenue += g_rev
        priv_spend   += g_spend
        priv_profit  += g_profit

    # ── Media Buyers ──────────────────────────────────────────────────────────
    mb_rs_profit = mb_mb_profit = 0.0
    mb_rows: list[dict] = []

    for mb in config.get("media_buyers") or []:
        key      = normalize_name(str(mb.get("publisher_name") or ""))
        ad_id    = str(mb.get("facebook_ad_account_id") or "")
        rd       = ringba.get(key) or {}

        revenue      = rd.get("revenue", 0.0)
        payout       = rd.get("payout",  0.0)
        spend        = spend_map.get(ad_id, 0.0)
        rs_share     = float(mb.get("royalspace_spend_share",  0.5))
        mb_share_pct = float(mb.get("media_buyer_spend_share", 0.5))
        mb_share_amt = spend * mb_share_pct

        mb_p = payout - mb_share_amt
        rs_p = (revenue - payout) - (spend * rs_share)

        mb_mb_profit += mb_p
        mb_rs_profit += rs_p
        mb_rows.append({
            "name":        mb["display_name"],
            "category":    str(mb.get("category") or "").lower(),
            "revenue":     revenue,
            "payout":      payout,
            "spend":       spend,
            "mb_share_amt": mb_share_amt,
            "mb_profit":   mb_p,
            "rs_profit":   rs_p,
        })

    mb_rows.sort(key=lambda r: r["rs_profit"], reverse=True)

    # ── Afiliados externos ────────────────────────────────────────────────────
    ext_rs_profit = 0.0
    ext_rows: list[dict] = []

    for key, rd in ringba.items():
        if key in known:
            continue
        rev = rd.get("revenue", 0.0)
        pay = rd.get("payout",  0.0)
        rs  = rev - pay
        ext_rs_profit += rs
        ext_rows.append({"name": rd["raw"], "revenue": rev, "payout": pay, "rs_profit": rs})

    # ── Totales finales ───────────────────────────────────────────────────────
    rs_total     = priv_profit + mb_rs_profit + ext_rs_profit
    combined_net = rs_total + mb_mb_profit

    # ─────────────────────────────────────────────────────────────────────────
    # MENSAJE 1: True Profit → #mod
    # Formato de royalspace_true_profit.ps1 → New-DiscordReport
    # ─────────────────────────────────────────────────────────────────────────
    lines = [
        "```",
        "ROYALSPACE TRUE PROFIT",
        "",
        f"Royalspace Profit : {fmt(rs_total)}",
        f"Combined Net      : {fmt(combined_net)}",
        "",
        f"Private Profit    : {fmt(priv_profit)}",
        f"RS Profit from MB : {fmt(mb_rs_profit)}",
        f"External Profit   : {fmt(ext_rs_profit)}",
        f"MB Profit Total   : {fmt(mb_mb_profit)}",
        "",
        "MEDIA BUYERS",
        "Name                 Rev      Spend       MB        RS",
        "--------------------------------------------------------",
    ]

    for r in mb_rows:
        name  = r["name"][:20].ljust(20)
        rev   = fmt(r["revenue"]).rjust(8)
        spend = fmt(r["spend"]).rjust(8)
        mb_p  = fmt(r["mb_profit"]).rjust(8)
        rs_p  = fmt(r["rs_profit"]).rjust(8)
        lines.append(f"{name} {rev} {spend} {mb_p} {rs_p}")

    lines.append("")

    if ext_rows:
        top3 = sorted(ext_rows, key=lambda r: r["rs_profit"], reverse=True)[:3]
        lines += [
            "TOP EXTERNAL",
            "Name                 RS Profit",
            "-------------------------------",
        ]
        for r in top3:
            clean = re.sub(r'^\(\d+\)\s*', '', r["name"])[:20].ljust(20)
            rs_p  = fmt(r["rs_profit"]).rjust(9)
            lines.append(f"{clean} {rs_p}")

    lines.append("```")
    msg_mod = "\n".join(lines)
    if len(msg_mod) > 1900:
        msg_mod = msg_mod[:1900] + "\n..."

    print("Enviando True Profit a #mod...")
    discord_send(WEBHOOK_MOD, msg_mod)

    # ─────────────────────────────────────────────────────────────────────────
    # MENSAJES 2 y 3: MB Alerts → #mb-alerts interno / externo
    # Formato de royalspace_mb_alerts.ps1 → New-MbDiscordReport
    # ─────────────────────────────────────────────────────────────────────────
    internal = [r for r in mb_rows if r["category"] == "internal"]
    external = [r for r in mb_rows if r["category"] == "external"]

    print("Enviando MB Internal Performance a #mb-alerts...")
    discord_send(WEBHOOK_INTERNAL, build_mb_alerts_report("MB INTERNAL PERFORMANCE", internal))

    print("Enviando MB External Performance a #mb-alerts externo...")
    discord_send(WEBHOOK_EXTERNAL, build_mb_alerts_report("MB EXTERNAL PERFORMANCE", external))

    print("OK — 3 mensajes enviados a Discord.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            discord_send(WEBHOOK_MOD, f"[TRUE PROFIT ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

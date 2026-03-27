#!/usr/bin/env python3
"""
True Profit — Ringba + Meta → Discord #mod
Royalspace 2026 | Port de royalspace_true_profit.ps1

Lógica replicada exactamente:
  - Solo corre en horario laboral EST (lunes-viernes 8-20, sábado 8-14)
  - Ventana Ringba: medianoche EST de hoy → ahora UTC
  - Meta: date_preset=today
  - Calcula profit para: private groups, media buyers (interno/externo), afiliados externos
  - Fórmulas:
      MB profit  = payout - (spend × mb_share)
      RS profit  = (revenue - payout) - (spend × rs_share)
      Private    = revenue - spend
      External   = revenue - payout
      RS Total   = private_profit + sum(rs_profit_mb) + sum(rs_profit_ext)
      Combined   = rs_total + sum(mb_profit)
  - Tabla ASCII en bloque de código Discord (máx 1900 chars)
  - Canal: ${{ secrets.DISCORD_WEBHOOK_MOD }}
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common.business_hours import is_business_hours
from common.discord_client import send as discord_send
from common.meta_client import build_spend_map
from common.ringba_client import get_midnight_utc, get_publisher_summary, normalize_name

# ── Secretos (GitHub Secrets → env vars) ─────────────────────────────────────
RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN     = os.environ["META_ACCESS_TOKEN"]
META_VERSION   = os.environ.get("META_API_VERSION", "v25.0")
WEBHOOK        = os.environ["DISCORD_WEBHOOK_MOD"]

CONFIG_PATH = Path(__file__).parent / "config.json"


def fmt(v: float) -> str:
    return f"${v:.2f}"


def main() -> None:
    if not is_business_hours():
        print("Fuera de horario laboral. Script detenido.")
        return

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

    # ── Mapa de publishers conocidos (para detectar externos) ─────────────────
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
        key   = normalize_name(str(mb.get("publisher_name") or ""))
        ad_id = str(mb.get("facebook_ad_account_id") or "")
        rd    = ringba.get(key) or {}

        revenue  = rd.get("revenue", 0.0)
        payout   = rd.get("payout",  0.0)
        spend    = spend_map.get(ad_id, 0.0)
        rs_share = float(mb.get("royalspace_spend_share",  0.5))
        mb_share = float(mb.get("media_buyer_spend_share", 0.5))

        mb_p = payout - (spend * mb_share)
        rs_p = (revenue - payout) - (spend * rs_share)

        mb_mb_profit += mb_p
        mb_rs_profit += rs_p
        mb_rows.append({
            "name": mb["display_name"],
            "revenue": revenue, "spend": spend,
            "mb_profit": mb_p,  "rs_profit": rs_p,
        })

    mb_rows.sort(key=lambda r: r["rs_profit"], reverse=True)

    # ── Afiliados externos (publishers no conocidos en Ringba) ────────────────
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

    # ── Formato Discord (bloque de código, máx 1900 chars) ───────────────────
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
    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1900] + "\n..."

    discord_send(WEBHOOK, message)
    print("Reporte enviado a Discord. OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            discord_send(WEBHOOK, f"[TRUE PROFIT ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

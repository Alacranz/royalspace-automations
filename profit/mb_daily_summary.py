#!/usr/bin/env python3
"""
MB Daily Summary — Ringba + Meta → Discord
Royalspace 2026 | Port de royalspace_mb_daily_summary.ps1

Corre una vez por la mañana y envía el resumen del día ANTERIOR:
  1. MB INTERNAL DAILY SUMMARY → DISCORD_WEBHOOK_MB_INTERNAL
  2. MB EXTERNAL DAILY SUMMARY → DISCORD_WEBHOOK_MB_EXTERNAL

No tiene verificación de horario laboral (corre incondicionalmente).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send
from common.meta_client import build_spend_map
from common.ringba_client import get_publisher_summary, get_yesterday_utc_range, normalize_name

# ── Secretos ──────────────────────────────────────────────────────────────────
RINGBA_TOKEN     = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT   = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_VERSION     = os.environ.get("META_API_VERSION", "v25.0")
WEBHOOK_INTERNAL = os.environ["DISCORD_WEBHOOK_MB_INTERNAL"]
WEBHOOK_EXTERNAL = os.environ["DISCORD_WEBHOOK_MB_EXTERNAL"]

import sentry_sdk
sentry_dsn = os.environ.get("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        traces_sample_rate=1.0,
    )

CONFIG_PATH = Path(__file__).parent / "config.json"


def fmt(v: float) -> str:
    return f"${v:.2f}"


def build_daily_report(title: str, rows: list[dict]) -> str:
    """
    Formato de royalspace_mb_daily_summary.ps1.
    Columnas: Name | Payout | Spend | MB Share | Profit
    Ordenado por Profit descendente.
    """
    sorted_rows = sorted(rows, key=lambda r: r["profit"], reverse=True)
    lines = [
        "```",
        title,
        "",
        "Name                 Payout    Spend   MB Share   Profit",
        "----------------------------------------------------------",
    ]
    for r in sorted_rows:
        name   = r["name"][:20].ljust(20)
        payout = fmt(r["payout"]).rjust(8)
        spend  = fmt(r["spend"]).rjust(8)
        share  = fmt(r["mb_share_amt"]).rjust(8)
        profit = fmt(r["profit"]).rjust(8)
        lines.append(f"{name} {payout} {spend} {share} {profit}")

    if not sorted_rows:
        lines.append("No data")

    lines.append("```")
    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n..."
    return msg


def main() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    tz_name = config.get("timezone", "America/New_York")

    # Fecha de ayer para el título del reporte (en la timezone configurada)
    tz = pytz.timezone(tz_name)
    yesterday_date = (datetime.now(tz) - timedelta(days=1)).date()
    report_date = yesterday_date.strftime("%d/%m/%Y")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    start_utc, end_utc = get_yesterday_utc_range(tz_name)

    print(f"Consultando Meta spend (yesterday = {report_date})...")
    spend_map = build_spend_map(
        META_TOKEN, META_VERSION, config, "yesterday", include_private_groups=False
    )

    print("Consultando Ringba (yesterday)...")
    ringba = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)

    # ── Calcular por MB ────────────────────────────────────────────────────────
    internal_rows: list[dict] = []
    external_rows: list[dict] = []

    for mb in config.get("media_buyers") or []:
        key          = normalize_name(str(mb.get("publisher_name") or ""))
        ad_id        = str(mb.get("facebook_ad_account_id") or "")
        rd           = ringba.get(key) or {}
        category     = str(mb.get("category") or "").lower()

        payout       = rd.get("payout", 0.0)
        spend        = spend_map.get(ad_id, 0.0)
        mb_share_pct = float(mb.get("media_buyer_spend_share", 0.5))
        mb_share_amt = spend * mb_share_pct
        profit       = payout - mb_share_amt

        row = {
            "name":         mb["display_name"],
            "payout":       payout,
            "spend":        spend,
            "mb_share_amt": mb_share_amt,
            "profit":       profit,
        }

        if category == "internal":
            internal_rows.append(row)
        else:
            external_rows.append(row)

    # ── Enviar mensajes ────────────────────────────────────────────────────────
    print("Enviando MB Internal Daily Summary...")
    discord_send(
        WEBHOOK_INTERNAL,
        build_daily_report(f"MB INTERNAL DAILY SUMMARY - {report_date}", internal_rows),
    )

    print("Enviando MB External Daily Summary...")
    discord_send(
        WEBHOOK_EXTERNAL,
        build_daily_report(f"MB EXTERNAL DAILY SUMMARY - {report_date}", external_rows),
    )

    print("OK — 2 mensajes enviados a Discord.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            discord_send(WEBHOOK_INTERNAL, f"[MB DAILY SUMMARY ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

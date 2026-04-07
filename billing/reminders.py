"""
Payment Reminders — Royalspace Billing

Runs weekly (every Monday) and sends a Discord report with:
  1. Current-month Ringba revenue for ALL buyers
  2. Outstanding invoices (PENDIENTE) with days elapsed

Required env vars:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  GOOGLE_SERVICE_ACCOUNT_JSON, BILLING_SPREADSHEET_ID
  DISCORD_WEBHOOK_BILLING
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytz

_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _ROOT)                          # for billing.* imports
sys.path.insert(0, os.path.join(_ROOT, "profit"))  # for common.* imports

from common.discord_client import send as discord_send   # noqa: E402
from billing.ringba_buyer import (                        # noqa: E402
    get_buyer_revenue, get_current_month_utc_range, find_buyer_data
)
from billing.payment_tracker import get_outstanding_invoices, refresh_statuses  # noqa: E402
from billing.sync_payments import run as sync_payments                        # noqa: E402
from billing.zoho_crm import get_crm_token, mark_deal_overdue, update_buyer_revenue  # noqa: E402

EST = pytz.timezone("America/New_York")

MONTHS_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def load_config() -> dict:
    path = os.path.join(_DIR, "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run() -> None:
    config          = load_config()
    tz_name         = config.get("timezone", "America/New_York")
    threshold       = config.get("invoice_threshold_usd", 0)
    reminder_buyers = [b for b in config["reminder_buyers"] if b.get("active", True)]

    ringba_token    = os.environ["RINGBA_API_TOKEN"]
    ringba_acct     = os.environ["RINGBA_ACCOUNT_ID"]
    gsheets_creds   = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    spreadsheet_id  = os.environ.get("BILLING_SPREADSHEET_ID", "")
    discord_webhook = os.environ["DISCORD_WEBHOOK_BILLING"]

    # ── Current month label ───────────────────────────────────────────────────
    now_est    = datetime.now(pytz.timezone(tz_name))
    month_label = f"{MONTHS_ES[now_est.month]} {now_est.year}"

    print(f"\n[Reminders] Current month: {month_label}")

    # ── Ringba: current month revenue by buyer ────────────────────────────────
    start_utc, end_utc = get_current_month_utc_range(tz_name)
    print(f"  Range: {start_utc.strftime('%m/%d')} → {end_utc.strftime('%m/%d %H:%M')} UTC")

    print("\n[Ringba] Fetching buyer revenue...")
    buyer_map = get_buyer_revenue(ringba_token, ringba_acct, start_utc, end_utc, verbose=False)

    # ── Sync pagos desde Zoho Books → Google Sheets ───────────────────────────
    zoho_client_id     = os.environ.get("ZOHO_CLIENT_ID", "")
    zoho_client_secret = os.environ.get("ZOHO_CLIENT_SECRET", "")
    zoho_refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN", "")
    if zoho_client_id and zoho_client_secret and zoho_refresh_token and gsheets_creds and spreadsheet_id:
        try:
            print("\n[Sync] Sincronizando pagos Zoho → Sheets...")
            sync_payments()
        except Exception as e:
            print(f"  [Sync] Error sincronizando pagos: {e}")
    else:
        print("\n[Sync] Skipped — faltan credenciales Zoho o Sheets")

    # ── Refresh invoice statuses in Sheet (PENDIENTE → VENCIDO if overdue) ────
    if gsheets_creds and spreadsheet_id:
        try:
            print("\n[Sheets] Refreshing invoice statuses...")
            refresh_statuses(spreadsheet_id, gsheets_creds)
        except Exception as e:
            print(f"  [Sheets] Error refreshing statuses: {e}")

    # ── Outstanding invoices from Sheets ─────────────────────────────────────
    outstanding = []
    if gsheets_creds and spreadsheet_id:
        try:
            outstanding = get_outstanding_invoices(spreadsheet_id, gsheets_creds)
            print(f"  [Sheets] {len(outstanding)} outstanding invoice(s)")
        except Exception as e:
            print(f"  [Sheets] Error fetching invoices: {e}")

    # ── CRM: mark overdue + update revenue ───────────────────────────────────
    crm_token = None
    if os.environ.get("ZOHO_REFRESH_TOKEN_CRM"):
        try:
            crm_token = get_crm_token()
            print("\n[CRM] Token OK")
        except Exception as e:
            print(f"\n[CRM] Token error (skipping CRM sync): {e}")

    if crm_token:
        # Mark overdue deals in CRM
        for inv in outstanding:
            if inv["overdue"] and inv["days_outstanding"] > 0:
                try:
                    mark_deal_overdue(crm_token, inv["invoice_number"], inv["days_outstanding"])
                except Exception as e:
                    print(f"  [CRM] Error marking overdue {inv['invoice_number']}: {e}")

        # Update revenue MTD per buyer in CRM
        for b in reminder_buyers:
            data = find_buyer_data(buyer_map, b["ringba_buyer_sub_id"])
            if data and data["revenue"] > 0:
                try:
                    update_buyer_revenue(crm_token, b["discord_name"], data["revenue"], month_label)
                except Exception as e:
                    print(f"  [CRM] Error updating revenue for {b['discord_name']}: {e}")

    # ── Build Discord messages ─────────────────────────────────────────────────
    _send_revenue_report(discord_webhook, month_label, reminder_buyers, buyer_map, threshold)

    if outstanding:
        _send_outstanding_report(discord_webhook, outstanding)

    print("\n[Reminders] Done.")


def _send_revenue_report(
    webhook: str,
    month_label: str,
    buyers: list[dict],
    buyer_map: dict,
    threshold: float = 0,
) -> None:
    threshold_label = f"  (threshold: ${threshold:,.0f})" if threshold > 0 else ""
    lines = [
        f"REVENUE POR BUYER — {month_label} (acumulado){threshold_label}",
        "",
        f"{'Buyer':<22} {'Revenue':>10}  {'Calls':>6}  {'Conv':>5}  {'':}",
        "-" * 58,
    ]

    total = 0.0
    for b in buyers:
        data = find_buyer_data(buyer_map, b["ringba_buyer_sub_id"])
        if data and data["revenue"] > 0:
            rev   = data["revenue"]
            calls = data["calls"]
            convs = data["conversions"]
            total += rev
            flag  = f"  BAJO THRESHOLD" if threshold > 0 and rev < threshold else ""
            lines.append(f"{b['discord_name']:<22} ${rev:>9,.2f}  {calls:>6}  {convs:>5}{flag}")
        else:
            lines.append(f"{b['discord_name']:<22} {'$0.00':>10}  {'—':>6}  {'—':>5}")

    lines.extend(["-" * 58, f"{'TOTAL':<22} ${total:>9,.2f}"])

    msg = "```\n" + "\n".join(lines) + "\n```"
    discord_send(webhook, msg)
    print("  [Discord] Revenue report sent")


def _send_outstanding_report(webhook: str, outstanding: list[dict]) -> None:
    lines = [
        "FACTURAS PENDIENTES",
        "",
        f"{'Buyer':<22} {'Mes':<14} {'Revenue':>10}  {'Factura':<14} {'Dias':>5}  Estado",
        "-" * 80,
    ]

    for inv in outstanding:
        status = "VENCIDA" if inv["overdue"] else "PENDIENTE"
        lines.append(
            f"{inv['buyer']:<22} {inv['month']:<14} {inv['revenue']:>10}  "
            f"{inv['invoice_number']:<14} {inv['days_outstanding']:>5}  {status}"
        )

    msg = "```\n" + "\n".join(lines) + "\n```"
    discord_send(webhook, msg)
    print("  [Discord] Outstanding invoices report sent")


if __name__ == "__main__":
    run()

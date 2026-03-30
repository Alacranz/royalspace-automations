"""
Invoice Generator — Royalspace Billing

Runs on day 1 of each month (via GitHub Actions).
For each active auto_invoice_buyer in config:
  1. Queries Ringba for the previous month's revenue
  2. Creates a Zoho Books invoice
  3. Sends the invoice via Zoho email
  4. Logs to Google Sheets "PAGOS 2026"

Sends a Discord summary to DISCORD_WEBHOOK_BILLING.

Required env vars:
  RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN
  GOOGLE_SERVICE_ACCOUNT_JSON, BILLING_SPREADSHEET_ID
  DISCORD_WEBHOOK_BILLING
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, date, timedelta

import pytz

# ── Path setup ────────────────────────────────────────────────────────────────
_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _ROOT)                              # for billing.* imports
sys.path.insert(0, os.path.join(_ROOT, "profit"))      # for common.* imports

from common.discord_client import send as discord_send  # noqa: E402
from billing.zoho_client import (                        # noqa: E402
    get_access_token, get_contact_id, create_invoice, send_invoice
)
from billing.ringba_buyer import get_buyer_revenue, get_month_utc_range, find_buyer_data  # noqa: E402
from billing.payment_tracker import log_invoice  # noqa: E402

EST = pytz.timezone("America/New_York")

MONTHS_EN = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

MONTHS_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def prev_month(today: date) -> tuple[int, int]:
    """Returns (year, month) for the month before today."""
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def load_config() -> dict:
    path = os.path.join(_DIR, "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run() -> None:
    config   = load_config()
    org_id   = config["zoho_org_id"]
    tz_name  = config.get("timezone", "America/New_York")
    due_days = config.get("invoice_due_days", 30)
    buyers   = [b for b in config["auto_invoice_buyers"] if b.get("active", True)]

    # ── Secrets ───────────────────────────────────────────────────────────────
    ringba_token    = os.environ["RINGBA_API_TOKEN"]
    ringba_acct     = os.environ["RINGBA_ACCOUNT_ID"]
    zoho_client_id  = os.environ["ZOHO_CLIENT_ID"]
    zoho_secret     = os.environ["ZOHO_CLIENT_SECRET"]
    zoho_refresh    = os.environ["ZOHO_REFRESH_TOKEN"]
    gsheets_creds   = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    spreadsheet_id  = os.environ["BILLING_SPREADSHEET_ID"]
    discord_webhook = os.environ["DISCORD_WEBHOOK_BILLING"]
    dry_run         = os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        print("  *** DRY RUN — facturas se crean en Zoho pero NO se envian ***")

    # ── Date range: previous month ─────────────────────────────────────────────
    today = datetime.now(pytz.timezone(tz_name)).date()
    year, month  = prev_month(today)
    month_label  = f"{MONTHS_ES[month]} {year}"    # Discord/Sheets (español)
    month_label_en = f"{MONTHS_EN[month]} {year}"  # Zoho invoices (inglés)
    invoice_date = today.strftime("%Y-%m-%d")
    due_dt       = today + timedelta(days=due_days)
    due_date_str = due_dt.strftime("%Y-%m-%d")

    print(f"\n[Invoice Generator] Billing month: {month_label}")
    print(f"  Invoice date: {invoice_date} | Due: {due_date_str}")

    start_utc, end_utc = get_month_utc_range(year, month, tz_name)
    print(f"  Ringba range: {start_utc.strftime('%Y-%m-%d')} → {end_utc.strftime('%Y-%m-%d')} UTC")

    # ── Fetch Ringba data ──────────────────────────────────────────────────────
    print("\n[Ringba] Fetching buyer revenue...")
    buyer_map = get_buyer_revenue(ringba_token, ringba_acct, start_utc, end_utc, verbose=True)

    # ── Zoho access token ──────────────────────────────────────────────────────
    print("\n[Zoho] Getting access token...")
    zoho_token = get_access_token(zoho_client_id, zoho_secret, zoho_refresh)
    print("  [Zoho] Token OK")

    # ── Process each buyer ─────────────────────────────────────────────────────
    results = []

    for buyer in buyers:
        ringba_name = buyer["ringba_buyer_name"]
        zoho_name   = buyer["zoho_contact_name"]
        item_name   = buyer.get("zoho_item_name", "Dental")
        display     = buyer["discord_name"]

        print(f"\n[{display}] Processing...")

        # Get Ringba revenue
        buyer_data = find_buyer_data(buyer_map, ringba_name)
        if buyer_data:
            revenue     = buyer_data["revenue"]
            calls       = buyer_data["calls"]
            conversions = buyer_data["conversions"]
        else:
            print(f"  [Warning] No Ringba data for '{ringba_name}' — skipping")
            results.append({"buyer": display, "status": "NO DATA", "revenue": 0.0, "invoice": ""})
            continue

        if revenue <= 0:
            print(f"  [Warning] Revenue is $0 for '{ringba_name}' — skipping invoice")
            results.append({"buyer": display, "status": "REVENUE $0", "revenue": 0.0, "invoice": ""})
            continue

        print(f"  Revenue: ${revenue:,.2f} | Calls: {calls} | Conversions: {conversions}")

        try:
            # Get Zoho contact ID
            contact_id = get_contact_id(zoho_token, org_id, zoho_name)
            print(f"  [Zoho] Contact ID: {contact_id}")

            # Build line item
            line_items = [{
                "name":        item_name,
                "description": f"Dental calls — {month_label_en}",
                "quantity":    1,
                "rate":        round(revenue, 2),
            }]

            # Create invoice
            invoice = create_invoice(
                token=zoho_token,
                org_id=org_id,
                contact_id=contact_id,
                invoice_date=invoice_date,
                due_date=due_date_str,
                line_items=line_items,
                reference_number=f"Royalspace — {month_label_en}",
            )
            invoice_number = invoice.get("invoice_number", "INV-??????")
            invoice_id     = invoice.get("invoice_id", "")
            print(f"  [Zoho] Invoice created: {invoice_number}")

            # Send invoice via Zoho email
            if dry_run:
                print(f"  [DRY RUN] Skipping email send for {invoice_number}")
            else:
                send_invoice(zoho_token, org_id, invoice_id)

            # Log to Google Sheets
            log_invoice(
                spreadsheet_id=spreadsheet_id,
                creds_json=gsheets_creds,
                buyer_name=display,
                billed_month=month_label,
                revenue=revenue,
                invoice_number=invoice_number,
                invoice_date=invoice_date,
                due_date=due_date_str,
            )

            results.append({
                "buyer":   display,
                "status":  "OK",
                "revenue": revenue,
                "invoice": invoice_number,
            })

        except Exception as e:
            print(f"  [ERROR] {e}")
            results.append({
                "buyer":   display,
                "status":  f"ERROR: {e}",
                "revenue": revenue,
                "invoice": "",
            })

    # ── Discord summary ────────────────────────────────────────────────────────
    _send_discord_summary(discord_webhook, month_label, invoice_date, due_date_str, results)

    print("\n[Invoice Generator] Done.")


def _send_discord_summary(
    webhook: str,
    month_label: str,
    invoice_date: str,
    due_date: str,
    results: list[dict],
) -> None:
    def fmt_date(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")

    lines = [
        f"FACTURACION MENSUAL — {month_label}",
        f"Fecha factura: {fmt_date(invoice_date)} | Vencimiento: {fmt_date(due_date)}",
        "",
        f"{'Buyer':<22} {'Revenue':>10}  {'Factura':<14} {'Estado'}",
        "-" * 62,
    ]
    for r in results:
        rev = f"${r['revenue']:>9,.2f}" if r["revenue"] else f"{'—':>10}"
        inv = r["invoice"] or "—"
        lines.append(f"{r['buyer']:<22} {rev}  {inv:<14} {r['status']}")

    msg = "```\n" + "\n".join(lines) + "\n```"
    discord_send(webhook, msg)
    print("\n[Discord] Summary sent")


if __name__ == "__main__":
    run()

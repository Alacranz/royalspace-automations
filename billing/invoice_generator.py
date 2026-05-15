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

import requests

import pytz

# ── Path setup ────────────────────────────────────────────────────────────────
_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _ROOT)                              # for billing.* imports
sys.path.insert(0, os.path.join(_ROOT, "profit"))      # for common.* imports

from common.discord_client import send as discord_send  # noqa: E402
from billing.zoho_client import (                        # noqa: E402
    get_access_token, get_contact_id, get_contact_emails, create_invoice, send_invoice
)
from billing.ringba_buyer import get_buyer_revenue, get_month_utc_range, find_buyer_data  # noqa: E402
from billing.payment_tracker import (                                # noqa: E402
    log_invoice, refresh_statuses,
    get_pending_state, set_pending_state, clear_pending_state,
)
from billing.zoho_crm import get_crm_token, upsert_contact, log_invoice_deal  # noqa: E402

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


def month_key(year: int, month: int) -> str:
    """Returns 'YYYY-MM' string for a given year/month."""
    return f"{year:04d}-{month:02d}"


def label_range_es(from_ym: str, to_ym: str) -> str:
    """Returns Spanish label for a month range. E.g. 'Enero–Marzo 2026' or 'Marzo 2026'."""
    fy, fm = int(from_ym[:4]), int(from_ym[5:])
    ty, tm = int(to_ym[:4]), int(to_ym[5:])
    if from_ym == to_ym:
        return f"{MONTHS_ES[fm]} {fy}"
    if fy == ty:
        return f"{MONTHS_ES[fm]}–{MONTHS_ES[tm]} {fy}"
    return f"{MONTHS_ES[fm]} {fy} – {MONTHS_ES[tm]} {ty}"


def label_range_en(from_ym: str, to_ym: str) -> str:
    """Returns English label for a month range."""
    fy, fm = int(from_ym[:4]), int(from_ym[5:])
    ty, tm = int(to_ym[:4]), int(to_ym[5:])
    if from_ym == to_ym:
        return f"{MONTHS_EN[fm]} {fy}"
    if fy == ty:
        return f"{MONTHS_EN[fm]}–{MONTHS_EN[tm]} {fy}"
    return f"{MONTHS_EN[fm]} {fy} – {MONTHS_EN[tm]} {ty}"


def run() -> None:
    config    = load_config()
    org_id    = config["zoho_org_id"]
    tz_name   = config.get("timezone", "America/New_York")
    threshold = config.get("invoice_threshold_usd", 0)
    buyers    = [b for b in config["auto_invoice_buyers"] if b.get("active", True)]

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

    # ── Previous month ─────────────────────────────────────────────────────────
    today        = datetime.now(pytz.timezone(tz_name)).date()
    year, month  = prev_month(today)
    current_ym   = month_key(year, month)   # "2026-03"
    invoice_date = today.strftime("%Y-%m-%d")

    print(f"\n[Invoice Generator] Processing month: {current_ym}")

    start_utc, end_utc = get_month_utc_range(year, month, tz_name)
    print(f"  Ringba range: {start_utc.strftime('%Y-%m-%d')} → {end_utc.strftime('%Y-%m-%d')} UTC")

    # ── Fetch Ringba data for previous month ───────────────────────────────────
    print("\n[Ringba] Fetching buyer revenue...")
    buyer_map = get_buyer_revenue(ringba_token, ringba_acct, start_utc, end_utc, verbose=True)

    # ── Zoho access token ──────────────────────────────────────────────────────
    print("\n[Zoho] Getting access token...")
    zoho_token = get_access_token(zoho_client_id, zoho_secret, zoho_refresh)
    print("  [Zoho] Token OK")

    # ── Zoho CRM token ─────────────────────────────────────────────────────────
    crm_token = None
    if os.environ.get("ZOHO_REFRESH_TOKEN_CRM"):
        try:
            crm_token = get_crm_token()
            print("  [CRM] Token OK")
        except Exception as e:
            print(f"  [CRM] Token error (skipping CRM sync): {e}")

    # ── Process each buyer ─────────────────────────────────────────────────────
    results = []

    for buyer in buyers:
        ringba_sub_id = buyer["ringba_buyer_sub_id"]
        zoho_name     = buyer["zoho_contact_name"]
        item_name     = buyer.get("zoho_item_name", "Dental")
        display       = buyer["discord_name"]
        do_send       = buyer.get("send_invoice", True)
        due_days      = buyer.get("due_days", config.get("invoice_due_days", 15))
        frequency     = buyer.get("billing_frequency", 1)  # 1=monthly, 2=bimonthly

        print(f"\n[{display}] Processing (freq={frequency}, due={due_days}d)...")

        # ── Load pending state from Google Sheets ──────────────────────────────
        state = get_pending_state(spreadsheet_id, gsheets_creds, display)
        pending_revenue    = state["pending_revenue"]
        pending_from       = state["from_month"]    # "" or "YYYY-MM"
        months_accumulated = state["months_accumulated"]

        # ── Get current month Ringba revenue ───────────────────────────────────
        buyer_data = find_buyer_data(buyer_map, ringba_sub_id)
        if buyer_data:
            current_revenue = buyer_data["revenue"]
            calls           = buyer_data["calls"]
            conversions     = buyer_data["conversions"]
        else:
            current_revenue = 0.0
            calls = conversions = 0
            print(f"  [Warning] No Ringba data for sub_id='{ringba_sub_id}'")

        # ── Accumulate ─────────────────────────────────────────────────────────
        total_revenue       = pending_revenue + current_revenue
        new_from            = pending_from if pending_from else current_ym
        new_months          = months_accumulated + 1

        print(f"  Pending: ${pending_revenue:,.2f} | Current: ${current_revenue:,.2f} | Total: ${total_revenue:,.2f}")
        print(f"  Range: {new_from} → {current_ym} ({new_months} month(s))")

        # ── Check if it's time to invoice ──────────────────────────────────────
        # Bimensual: only attempt invoice when months_accumulated+1 is multiple of frequency
        frequency_met = (new_months % frequency == 0)
        threshold_met = (threshold <= 0 or total_revenue >= threshold)

        if not frequency_met:
            print(f"  [Skip] Frequency not met ({new_months}/{frequency} months) — accumulating")
            set_pending_state(
                spreadsheet_id, gsheets_creds, display,
                total_revenue, new_from, current_ym, new_months,
                state["last_invoice_month"],
            )
            results.append({"buyer": display, "status": f"ACUMULANDO ({new_months}/{frequency} meses)", "revenue": total_revenue, "invoice": ""})
            continue

        if total_revenue <= 0:
            print(f"  [Skip] Total revenue is $0 — nothing to invoice")
            results.append({"buyer": display, "status": "REVENUE $0", "revenue": 0.0, "invoice": ""})
            continue

        if not threshold_met:
            print(f"  [Skip] Total ${total_revenue:,.2f} below threshold ${threshold:,.2f} — accumulating")
            set_pending_state(
                spreadsheet_id, gsheets_creds, display,
                total_revenue, new_from, current_ym, new_months,
                state["last_invoice_month"],
            )
            results.append({"buyer": display, "status": f"ACUMULANDO ${total_revenue:,.2f} / ${threshold:,.0f}", "revenue": total_revenue, "invoice": ""})
            continue

        # ── Build invoice labels ───────────────────────────────────────────────
        period_label_es = label_range_es(new_from, current_ym)
        period_label_en = label_range_en(new_from, current_ym)
        due_dt          = today + timedelta(days=due_days)
        due_date_str    = due_dt.strftime("%Y-%m-%d")

        print(f"  Invoicing: ${total_revenue:,.2f} | Period: {period_label_en} | Due: {due_date_str}")

        try:
            contact_id     = get_contact_id(zoho_token, org_id, zoho_name)
            contact_emails = get_contact_emails(zoho_token, org_id, contact_id)
            print(f"  [Zoho] Contact ID: {contact_id} | Emails: {len(contact_emails)} address(es)")

            line_items = [{
                "name":        item_name,
                "description": f"Dental calls — {period_label_en}",
                "quantity":    1,
                "rate":        round(total_revenue, 2),
            }]

            invoice = create_invoice(
                token=zoho_token,
                org_id=org_id,
                contact_id=contact_id,
                invoice_date=invoice_date,
                due_date=due_date_str,
                line_items=line_items,
                reference_number=f"Royalspace — {period_label_en}",
            )
            invoice_number = invoice.get("invoice_number", "INV-??????")
            invoice_id     = invoice.get("invoice_id", "")
            print(f"  [Zoho] Invoice created: {invoice_number}")

            if dry_run:
                print(f"  [DRY RUN] Skipping email send for {invoice_number}")
            elif not do_send:
                print(f"  [Info] send_invoice=false — invoice created as draft, not emailed")
            else:
                send_invoice(zoho_token, org_id, invoice_id, contact_emails)
                print(f"  [Zoho] Invoice sent to {len(contact_emails)} recipient(s)")

            log_invoice(
                spreadsheet_id=spreadsheet_id,
                creds_json=gsheets_creds,
                buyer_name=display,
                billed_month=period_label_es,
                revenue=total_revenue,
                invoice_number=invoice_number,
                invoice_date=invoice_date,
                due_date=due_date_str,
            )

            # ── CRM: upsert contact + create deal ─────────────────────────────
            if crm_token:
                try:
                    category   = buyer.get("category", "")
                    account_id = upsert_contact(crm_token, display, category)
                    log_invoice_deal(
                        token=crm_token,
                        account_id=account_id,
                        invoice_number=invoice_number,
                        buyer_name=display,
                        billed_month=period_label_es,
                        revenue=total_revenue,
                        due_date=due_date_str,
                    )
                except Exception as e:
                    print(f"  [CRM] Error logging deal: {e}")

            # Reset pending state — next cycle starts fresh
            clear_pending_state(spreadsheet_id, gsheets_creds, display, current_ym)

            results.append({
                "buyer":   display,
                "status":  "OK",
                "revenue": total_revenue,
                "invoice": invoice_number,
            })

        except requests.HTTPError as e:
            msg = f"HTTP {e.response.status_code}"
            print(f"  [ERROR] {msg}")
            results.append({"buyer": display, "status": f"ERROR: {msg}", "revenue": total_revenue, "invoice": ""})
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"  [ERROR] {msg}")
            results.append({"buyer": display, "status": f"ERROR: {msg}", "revenue": total_revenue, "invoice": ""})

    # ── Refresh invoice statuses in Sheet (PENDIENTE → VENCIDO if overdue) ────
    print("\n[Sheets] Refreshing invoice statuses...")
    try:
        refresh_statuses(spreadsheet_id, gsheets_creds)
    except Exception as e:
        print(f"  [Sheets] refresh_statuses failed: {e}")

    # ── Discord summary ────────────────────────────────────────────────────────
    _send_discord_summary(discord_webhook, current_ym, invoice_date, results)

    print("\n[Invoice Generator] Done.")


def _send_discord_summary(
    webhook: str,
    current_ym: str,
    invoice_date: str,
    results: list[dict],
) -> None:
    def fmt_date(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")

    fy, fm = int(current_ym[:4]), int(current_ym[5:])
    period_label = f"{MONTHS_ES[fm]} {fy}"

    lines = [
        f"FACTURACION — {period_label}",
        f"Fecha ejecucion: {fmt_date(invoice_date)}",
        "",
        f"{'Buyer':<22} {'Total':>10}  {'Factura':<14} {'Estado'}",
        "-" * 65,
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

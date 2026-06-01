"""
One-time import: Hypertarget Marketing Zoho invoices → PAGOS 2026

Fetches every Hypertarget invoice from Zoho Books and logs any that
are missing from the Google Sheet.  Safe to re-run — skips rows that
already have a matching invoice number.

Usage:
  Set the same env vars as billing_dashboard.yml and run:
    python billing/import_hypertarget.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _DIR)

from billing.zoho_client import get_access_token, get_contact_id, list_invoices  # noqa: E402
from billing.payment_tracker import log_invoice, update_payment                   # noqa: E402

BUYER_NAME    = "Hypertarget Marketing"
ZOHO_CONTACT  = "Hypertarget Marketing"
DUE_DAYS      = 15
TAB_NAME      = "PAGOS 2026"
SCOPES        = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MONTHS_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def _norm_date(d: str) -> str:
    """Normalize any date string to YYYY-MM-DD."""
    if not d:
        return datetime.utcnow().strftime("%Y-%m-%d")
    if len(d) == 10 and d[4] == "-":
        return d
    try:
        return datetime.strptime(d, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return d


def _billed_month(inv_date: str, reference: str) -> str:
    """
    Derive Spanish billed-month label.
    Prefers reference_number if set; otherwise assumes the invoice covers
    the month prior to the invoice date (standard billing: invoice in M+1).
    """
    if reference:
        return reference
    try:
        dt = datetime.strptime(inv_date, "%Y-%m-%d")
        # Invoice date is in M+1 → billed month is M
        first_of_inv = dt.replace(day=1)
        billed = (first_of_inv - timedelta(days=1)).replace(day=1)
        return f"{MONTHS_ES[billed.month]} {billed.year}"
    except ValueError:
        return inv_date


def _get_existing_invoice_numbers(spreadsheet_id: str, creds_json: str) -> set[str]:
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    gc    = gspread.authorize(creds)
    try:
        ws   = gc.open_by_key(spreadsheet_id).worksheet(TAB_NAME)
        rows = ws.get_all_values()
        # Column E (index 4) = N° Factura
        return {row[4] for row in rows[1:] if len(row) > 4 and row[4]}
    except gspread.exceptions.WorksheetNotFound:
        return set()


def run() -> None:
    client_id      = os.environ["ZOHO_CLIENT_ID"]
    client_secret  = os.environ["ZOHO_CLIENT_SECRET"]
    refresh_token  = os.environ["ZOHO_REFRESH_TOKEN"]
    org_id         = os.environ.get("ZOHO_ORG_ID", "771911284")
    creds_json     = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    spreadsheet_id = os.environ["BILLING_SPREADSHEET_ID"]

    print("[Import] Obteniendo token Zoho...")
    token = get_access_token(client_id, client_secret, refresh_token)

    print(f"[Import] Buscando contacto: {ZOHO_CONTACT}")
    contact_id = get_contact_id(token, org_id, ZOHO_CONTACT)
    print(f"[Import] Contact ID: {contact_id}")

    print(f"[Import] Obteniendo todas las facturas de {BUYER_NAME}...")
    all_invoices = list_invoices(token, org_id, contact_id=contact_id)
    print(f"[Import] {len(all_invoices)} factura(s) encontrada(s) en Zoho")

    if not all_invoices:
        print("[Import] Sin facturas. Fin.")
        return

    existing = _get_existing_invoice_numbers(spreadsheet_id, creds_json)
    print(f"[Import] Facturas ya en el sheet: {existing or '(ninguna)'}")

    imported = 0
    for inv in all_invoices:
        inv_number = inv.get("invoice_number", "")
        if not inv_number:
            continue

        if inv_number in existing:
            print(f"  [Skip] {inv_number} — ya existe en PAGOS 2026")
            continue

        inv_date  = _norm_date(inv.get("date") or inv.get("invoice_date", ""))
        due_raw   = inv.get("due_date", "")
        due_date  = _norm_date(due_raw) if due_raw else (
            (datetime.strptime(inv_date, "%Y-%m-%d") + timedelta(days=DUE_DAYS)).strftime("%Y-%m-%d")
        )

        total     = float(inv.get("total", 0))
        status    = inv.get("status", "").lower()
        reference = inv.get("reference_number", "")
        month_lbl = _billed_month(inv_date, reference)

        print(f"  [+] {inv_number} | {month_lbl} | ${total:,.2f} | status={status}")

        log_invoice(
            spreadsheet_id=spreadsheet_id,
            creds_json=creds_json,
            buyer_name=BUYER_NAME,
            billed_month=month_lbl,
            revenue=total,
            invoice_number=inv_number,
            invoice_date=inv_date,
            due_date=due_date,
            notes="Importado desde Zoho (creado manualmente)",
        )
        imported += 1
        existing.add(inv_number)

        if status == "paid":
            payment_date = _norm_date(
                inv.get("last_payment_date") or
                inv.get("payment_made_date") or
                datetime.utcnow().strftime("%Y-%m-%d")
            )
            update_payment(
                spreadsheet_id=spreadsheet_id,
                creds_json=creds_json,
                invoice_number=inv_number,
                payment_date=payment_date,
            )

    print(f"\n[Import] Listo. {imported} factura(s) importada(s).")


if __name__ == "__main__":
    run()

"""
Google Sheets payment tracker — Royalspace Billing

Logs invoices and payment status to the "PAGOS 2026" tab.

Sheet columns (A–J):
  A  Fecha Factura    — date invoice was created (DD/MM/YYYY)
  B  Buyer            — buyer display name
  C  Mes Facturado    — billed month (e.g. "Febrero 2026")
  D  Revenue Ringba   — amount invoiced ($)
  E  N° Factura       — Zoho invoice number (INV-XXXXXX)
  F  Fecha Vencimiento — due date (DD/MM/YYYY)
  G  Fecha Pago       — payment received date (empty until paid)
  H  Dias Pendientes  — days outstanding (formula or manual)
  I  Estado           — PENDIENTE / PAGADO / VENCIDO
  J  Notas            — free text
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES    = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
TAB_NAME     = "PAGOS 2026"
PENDING_TAB  = "BILLING_STATE"

PENDING_HEADERS = [
    "Buyer",
    "Pending Revenue",
    "From Month",       # YYYY-MM del mes más antiguo no facturado
    "To Month",         # YYYY-MM del mes más reciente no facturado
    "Months Accumulated",
    "Last Invoice Month",  # YYYY-MM del último mes que fue incluido en una factura
    "Last Updated",
]

HEADERS = [
    "Fecha Factura",
    "Buyer",
    "Mes Facturado",
    "Revenue Ringba",
    "N° Factura",
    "Fecha Vencimiento",
    "Fecha Pago",
    "Dias Pendientes",
    "Estado",
    "Notas",
]


def _with_retry(fn, *args, max_attempts: int = 3, wait_seconds: int = 65, **kwargs):
    """
    Reintenta una llamada a gspread hasta max_attempts veces si Google
    Sheets devuelve 429 (quota exceeded) o 503 (service unavailable).
    """
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            err = str(e)
            is_retryable = "429" in err or "503" in err
            if is_retryable and attempt < max_attempts - 1:
                code = "429" if "429" in err else "503"
                print(f"  [Sheets] {code} — esperando {wait_seconds}s antes de reintentar ({attempt + 2}/{max_attempts})...")
                time.sleep(wait_seconds)
            else:
                raise


def _open_sheet(spreadsheet_id: str, creds_json: str):
    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)

    # Create tab if it doesn't exist
    try:
        ws = spreadsheet.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=TAB_NAME, rows=500, cols=10)
        # Write headers
        ws.update("A1:J1", [HEADERS])
        # Bold the header row
        ws.format("A1:J1", {"textFormat": {"bold": True}})

    return ws


def log_invoice(
    spreadsheet_id: str,
    creds_json: str,
    buyer_name: str,
    billed_month: str,         # e.g. "Febrero 2026"
    revenue: float,
    invoice_number: str,       # e.g. "INV-000192"
    invoice_date: str,         # "YYYY-MM-DD"
    due_date: str,             # "YYYY-MM-DD"
    notes: str = "",
) -> int:
    """
    Appends a new invoice row. Returns the row number written.
    """
    ws = _open_sheet(spreadsheet_id, creds_json)

    def fmt_date(d: str) -> str:
        """Convert YYYY-MM-DD → DD/MM/YYYY."""
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")

    row = [
        fmt_date(invoice_date),
        buyer_name,
        billed_month,
        f"${revenue:,.2f}",
        invoice_number,
        fmt_date(due_date),
        "",              # Fecha Pago — empty until received
        "",              # Dias Pendientes — fill manually or via update_payment
        "PENDIENTE",
        notes,
    ]

    _with_retry(ws.append_row, row, value_input_option="USER_ENTERED")
    # Find the row we just wrote (last row)
    all_values = _with_retry(ws.get_all_values)
    row_num = len(all_values)
    print(f"  [Sheets] Logged invoice {invoice_number} for {buyer_name} at row {row_num}")
    return row_num


def update_payment(
    spreadsheet_id: str,
    creds_json: str,
    invoice_number: str,
    payment_date: str,          # "YYYY-MM-DD"
    notes: str = "",
) -> bool:
    """
    Marks an invoice as PAGADO. Finds the row by invoice number.
    Returns True if found and updated.
    """
    ws = _open_sheet(spreadsheet_id, creds_json)
    all_values = _with_retry(ws.get_all_values)

    payment_str = datetime.strptime(payment_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    invoice_col = 4  # E = index 4 (0-based)

    for i, row in enumerate(all_values[1:], start=2):  # skip header
        if len(row) > invoice_col and row[invoice_col] == invoice_number:
            # Calculate days outstanding from invoice date to payment date
            invoice_date_str = row[0]  # A = Fecha Factura (DD/MM/YYYY)
            try:
                inv_date = datetime.strptime(invoice_date_str, "%d/%m/%Y")
                pay_date = datetime.strptime(payment_date, "%Y-%m-%d")
                days = (pay_date - inv_date).days
            except ValueError:
                days = ""

            _with_retry(ws.update, [[payment_str, days, "PAGADO", notes or row[9] if len(row) > 9 else ""]], f"G{i}:J{i}")
            print(f"  [Sheets] Marked {invoice_number} as PAGADO on {payment_str} ({days} days)")
            return True

    print(f"  [Sheets] Invoice {invoice_number} not found in sheet")
    return False


def refresh_statuses(spreadsheet_id: str, creds_json: str) -> int:
    """
    Sweeps all PENDIENTE rows and updates any whose due date has passed to VENCIDO.
    Returns the number of rows updated.
    Retries up to 3 times with 65s sleep on 429 rate-limit errors.
    """
    for attempt in range(3):
        try:
            ws = _open_sheet(spreadsheet_id, creds_json)
            all_values = ws.get_all_values()
            break
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < 2:
                wait = 65
                print(f"  [Sheets] Rate limit hit — waiting {wait}s before retry {attempt + 2}/3...")
                time.sleep(wait)
            else:
                raise
    else:
        return 0

    today = datetime.utcnow().date()
    updated = 0

    for i, row in enumerate(all_values[1:], start=2):  # skip header
        if len(row) < 9:
            continue
        if row[8].strip().upper() != "PENDIENTE":
            continue
        try:
            due = datetime.strptime(row[5], "%d/%m/%Y").date()
        except ValueError:
            continue
        if today > due:
            ws.update(f"I{i}", [["VENCIDO"]])
            updated += 1
            print(f"  [Sheets] Row {i} ({row[4]}) → VENCIDO")

    if updated:
        print(f"  [Sheets] {updated} invoice(s) marked VENCIDO")
    else:
        print("  [Sheets] All invoices up to date")
    return updated


def _open_pending_sheet(spreadsheet_id: str, creds_json: str):
    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)
    try:
        ws = spreadsheet.worksheet(PENDING_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=PENDING_TAB, rows=100, cols=7)
        ws.update("A1:G1", [PENDING_HEADERS])
        ws.format("A1:G1", {"textFormat": {"bold": True}})
    return ws


def get_pending_state(spreadsheet_id: str, creds_json: str, buyer_name: str) -> dict:
    """
    Returns accumulated pending revenue state for a buyer.
    {row, pending_revenue, from_month, to_month, months_accumulated, last_invoice_month}
    """
    ws = _open_pending_sheet(spreadsheet_id, creds_json)
    all_values = _with_retry(ws.get_all_values)
    for i, row in enumerate(all_values[1:], start=2):
        if row and row[0] == buyer_name:
            return {
                "row":                i,
                "pending_revenue":    float(row[1]) if len(row) > 1 and row[1] else 0.0,
                "from_month":         row[2] if len(row) > 2 else "",
                "to_month":           row[3] if len(row) > 3 else "",
                "months_accumulated": int(row[4]) if len(row) > 4 and row[4] else 0,
                "last_invoice_month": row[5] if len(row) > 5 else "",
            }
    return {
        "row":                None,
        "pending_revenue":    0.0,
        "from_month":         "",
        "to_month":           "",
        "months_accumulated": 0,
        "last_invoice_month": "",
    }


def set_pending_state(
    spreadsheet_id: str,
    creds_json: str,
    buyer_name: str,
    pending_revenue: float,
    from_month: str,        # "YYYY-MM"
    to_month: str,          # "YYYY-MM"
    months_accumulated: int,
    last_invoice_month: str = "",
) -> None:
    """Upserts the pending billing state for a buyer."""
    ws = _open_pending_sheet(spreadsheet_id, creds_json)
    all_values = _with_retry(ws.get_all_values)
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    row_data = [
        buyer_name,
        round(pending_revenue, 2),
        from_month,
        to_month,
        months_accumulated,
        last_invoice_month,
        now_str,
    ]
    for i, row in enumerate(all_values[1:], start=2):
        if row and row[0] == buyer_name:
            _with_retry(ws.update, [row_data], f"A{i}:G{i}")
            return
    _with_retry(ws.append_row, row_data, value_input_option="USER_ENTERED")


def clear_pending_state(
    spreadsheet_id: str,
    creds_json: str,
    buyer_name: str,
    last_invoice_month: str,
) -> None:
    """Resets accumulated pending revenue to 0 after a successful invoice."""
    set_pending_state(
        spreadsheet_id, creds_json, buyer_name,
        pending_revenue=0.0,
        from_month="",
        to_month="",
        months_accumulated=0,
        last_invoice_month=last_invoice_month,
    )


def get_outstanding_invoices(spreadsheet_id: str, creds_json: str) -> list[dict]:
    """
    Returns all rows with Estado = PENDIENTE.
    Each entry: {buyer, month, revenue, invoice_number, invoice_date, due_date, days_outstanding}
    """
    ws = _open_sheet(spreadsheet_id, creds_json)
    all_values = _with_retry(ws.get_all_values)
    today = datetime.utcnow().date()
    outstanding = []

    for row in all_values[1:]:  # skip header
        if len(row) < 9:
            continue
        if row[8].strip().upper() != "PENDIENTE":
            continue
        try:
            due = datetime.strptime(row[5], "%d/%m/%Y").date()
            inv = datetime.strptime(row[0], "%d/%m/%Y").date()
            days = (today - inv).days
            overdue = today > due
        except ValueError:
            days = 0
            overdue = False

        outstanding.append({
            "buyer":           row[1],
            "month":           row[2],
            "revenue":         row[3],
            "invoice_number":  row[4],
            "invoice_date":    row[0],
            "due_date":        row[5],
            "days_outstanding": days,
            "overdue":         overdue,
        })

    return outstanding

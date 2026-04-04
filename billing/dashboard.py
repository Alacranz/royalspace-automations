"""
Billing Dashboard — Royalspace

Populates a DASHBOARD tab in Google Sheets with consolidated billing data.
Designed to be read by Looker Studio for visual reporting.

Tabs written:
  - DASHBOARD_SUMMARY   → KPI totals (facturado, cobrado, pendiente, vencido, por facturar)
  - DASHBOARD_BUYERS    → per-buyer breakdown
  - DASHBOARD_INVOICES  → active invoices (PENDIENTE + VENCIDO only)

Required env vars:
  GOOGLE_SERVICE_ACCOUNT_JSON, BILLING_SPREADSHEET_ID
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TAB_PAGOS   = "PAGOS 2026"
TAB_STATE   = "BILLING_STATE"
TAB_SUMMARY = "DASHBOARD_SUMMARY"
TAB_BUYERS  = "DASHBOARD_BUYERS"
TAB_ACTIVE  = "DASHBOARD_INVOICES"

TZ = pytz.timezone("America/Caracas")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _open_spreadsheet(spreadsheet_id: str, creds_json: str):
    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(spreadsheet_id)


def _get_or_create_tab(spreadsheet, title: str, rows: int = 200, cols: int = 10):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _parse_amount(val: str) -> float:
    """Parse '$1,234.56' → 1234.56"""
    try:
        return float(str(val).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_date(val: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


# ── Data reading ──────────────────────────────────────────────────────────────

def _read_pagos(spreadsheet) -> list[dict]:
    try:
        ws = spreadsheet.worksheet(TAB_PAGOS)
    except gspread.exceptions.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    invoices = []
    for row in rows[1:]:
        if len(row) < 9 or not row[1]:
            continue
        invoices.append({
            "fecha_factura":  row[0],
            "buyer":          row[1],
            "mes_facturado":  row[2],
            "revenue":        _parse_amount(row[3]),
            "numero":         row[4],
            "fecha_venc":     row[5],
            "fecha_pago":     row[6],
            "dias":           row[7],
            "estado":         row[8].strip().upper(),
            "notas":          row[9] if len(row) > 9 else "",
        })
    return invoices


def _read_billing_state(spreadsheet) -> list[dict]:
    try:
        ws = spreadsheet.worksheet(TAB_STATE)
    except gspread.exceptions.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    state = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        state.append({
            "buyer":          row[0],
            "pending":        float(row[1]) if len(row) > 1 and row[1] else 0.0,
            "from_month":     row[2] if len(row) > 2 else "",
            "to_month":       row[3] if len(row) > 3 else "",
            "months_accum":   row[4] if len(row) > 4 else "",
            "last_invoice":   row[5] if len(row) > 5 else "",
        })
    return state


# ── Calculations ──────────────────────────────────────────────────────────────

def _calc_summary(invoices: list[dict], pending_state: list[dict]) -> dict:
    total_invoiced = sum(i["revenue"] for i in invoices)
    total_paid     = sum(i["revenue"] for i in invoices if i["estado"] == "PAGADO")
    total_pending  = sum(i["revenue"] for i in invoices if i["estado"] == "PENDIENTE")
    total_overdue  = sum(i["revenue"] for i in invoices if i["estado"] == "VENCIDO")
    to_invoice     = sum(s["pending"] for s in pending_state)
    return {
        "total_invoiced": total_invoiced,
        "total_paid":     total_paid,
        "total_pending":  total_pending,
        "total_overdue":  total_overdue,
        "to_invoice":     to_invoice,
    }


def _calc_buyers(invoices: list[dict], pending_state: list[dict]) -> list[dict]:
    buyers: dict[str, dict] = {}
    for inv in invoices:
        b = inv["buyer"]
        if b not in buyers:
            buyers[b] = {
                "buyer":         b,
                "invoiced":      0.0,
                "paid":          0.0,
                "pending":       0.0,
                "overdue":       0.0,
                "to_invoice":    0.0,
                "last_invoice":  "",
                "invoice_count": 0,
            }
        buyers[b]["invoiced"]      += inv["revenue"]
        buyers[b]["invoice_count"] += 1
        if inv["estado"] == "PAGADO":
            buyers[b]["paid"]    += inv["revenue"]
        elif inv["estado"] == "PENDIENTE":
            buyers[b]["pending"] += inv["revenue"]
        elif inv["estado"] == "VENCIDO":
            buyers[b]["overdue"] += inv["revenue"]
        # Track latest invoice date
        if inv["fecha_factura"] and inv["fecha_factura"] > buyers[b]["last_invoice"]:
            buyers[b]["last_invoice"] = inv["fecha_factura"]

    for s in pending_state:
        b = s["buyer"]
        if b in buyers:
            buyers[b]["to_invoice"] = s["pending"]
        else:
            buyers[b] = {
                "buyer":         b,
                "invoiced":      0.0,
                "paid":          0.0,
                "pending":       0.0,
                "overdue":       0.0,
                "to_invoice":    s["pending"],
                "last_invoice":  "",
                "invoice_count": 0,
            }

    return sorted(buyers.values(), key=lambda x: x["invoiced"], reverse=True)


def _calc_active(invoices: list[dict]) -> list[dict]:
    today = datetime.utcnow().date()
    active = []
    for inv in invoices:
        if inv["estado"] not in ("PENDIENTE", "VENCIDO"):
            continue
        due = _parse_date(inv["fecha_venc"])
        days_overdue = (today - due.date()).days if due and today > due.date() else 0
        active.append({**inv, "days_overdue": days_overdue})
    return sorted(active, key=lambda x: x["days_overdue"], reverse=True)


# ── Sheet writing ─────────────────────────────────────────────────────────────

def _write_summary(spreadsheet, summary: dict, updated_at: str) -> None:
    ws = _get_or_create_tab(spreadsheet, TAB_SUMMARY, rows=20, cols=3)
    ws.clear()

    data = [
        ["Métrica", "Valor", "Actualizado"],
        ["Total Facturado 2026", f"${summary['total_invoiced']:,.2f}", updated_at],
        ["Total Cobrado",        f"${summary['total_paid']:,.2f}",     ""],
        ["Total Pendiente",      f"${summary['total_pending']:,.2f}",  ""],
        ["Total Vencido",        f"${summary['total_overdue']:,.2f}",  ""],
        ["Revenue por Facturar", f"${summary['to_invoice']:,.2f}",     ""],
    ]
    ws.update("A1:C6", data)
    ws.format("A1:C1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.13, "green": 0.13, "blue": 0.13}})
    ws.format("A2:A6", {"textFormat": {"bold": True}})
    print(f"  [Dashboard] DASHBOARD_SUMMARY written")


def _write_buyers(spreadsheet, buyers: list[dict]) -> None:
    ws = _get_or_create_tab(spreadsheet, TAB_BUYERS, rows=50, cols=8)
    ws.clear()

    headers = ["Buyer", "Facturado", "Cobrado", "Pendiente", "Vencido", "Por Facturar", "Facturas", "Última Factura"]
    rows = [headers]
    for b in buyers:
        rows.append([
            b["buyer"],
            f"${b['invoiced']:,.2f}",
            f"${b['paid']:,.2f}",
            f"${b['pending']:,.2f}",
            f"${b['overdue']:,.2f}",
            f"${b['to_invoice']:,.2f}",
            b["invoice_count"],
            b["last_invoice"],
        ])
    ws.update(f"A1:H{len(rows)}", rows)
    ws.format("A1:H1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.13, "green": 0.13, "blue": 0.13}})
    print(f"  [Dashboard] DASHBOARD_BUYERS written ({len(buyers)} buyers)")


def _write_active(spreadsheet, active: list[dict]) -> None:
    ws = _get_or_create_tab(spreadsheet, TAB_ACTIVE, rows=100, cols=8)
    ws.clear()

    headers = ["Buyer", "Mes", "Revenue", "N° Factura", "Vencimiento", "Días Vencido", "Estado", "Notas"]
    rows = [headers]
    for inv in active:
        rows.append([
            inv["buyer"],
            inv["mes_facturado"],
            f"${inv['revenue']:,.2f}",
            inv["numero"],
            inv["fecha_venc"],
            inv["days_overdue"] if inv["days_overdue"] > 0 else "",
            inv["estado"],
            inv["notas"],
        ])
    ws.update(f"A1:H{len(rows)}", rows)
    ws.format("A1:H1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.13, "green": 0.13, "blue": 0.13}})

    # Color rows by status
    for i, inv in enumerate(active, start=2):
        if inv["estado"] == "VENCIDO":
            ws.format(f"A{i}:H{i}", {"backgroundColor": {"red": 1.0, "green": 0.9, "blue": 0.9}})
        elif inv["estado"] == "PENDIENTE":
            ws.format(f"A{i}:H{i}", {"backgroundColor": {"red": 1.0, "green": 0.97, "blue": 0.85}})

    print(f"  [Dashboard] DASHBOARD_INVOICES written ({len(active)} active invoices)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    spreadsheet_id = os.environ["BILLING_SPREADSHEET_ID"]
    creds_json     = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    now_vet    = datetime.now(TZ)
    updated_at = now_vet.strftime("%d/%m/%Y %H:%M VET")

    print(f"[Dashboard] Starting at {updated_at}")

    spreadsheet  = _open_spreadsheet(spreadsheet_id, creds_json)
    invoices     = _read_pagos(spreadsheet)
    pending_state = _read_billing_state(spreadsheet)

    print(f"  [Dashboard] {len(invoices)} invoices, {len(pending_state)} buyers in state")

    summary = _calc_summary(invoices, pending_state)
    buyers  = _calc_buyers(invoices, pending_state)
    active  = _calc_active(invoices)

    _write_summary(spreadsheet, summary, updated_at)
    _write_buyers(spreadsheet, buyers)
    _write_active(spreadsheet, active)

    print(f"[Dashboard] Done. Summary:")
    print(f"  Facturado:  ${summary['total_invoiced']:,.2f}")
    print(f"  Cobrado:    ${summary['total_paid']:,.2f}")
    print(f"  Pendiente:  ${summary['total_pending']:,.2f}")
    print(f"  Vencido:    ${summary['total_overdue']:,.2f}")
    print(f"  x Facturar: ${summary['to_invoice']:,.2f}")


if __name__ == "__main__":
    main()

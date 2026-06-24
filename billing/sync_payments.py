"""
Zoho → Google Sheets payment sync — Royalspace Billing

Consulta todas las facturas pagadas en Zoho Books y actualiza
el estado en Google Sheets (PENDIENTE/VENCIDO → PAGADO).

Lee el sheet UNA SOLA VEZ, procesa todo en memoria, y escribe
solo las filas que cambiaron — evita rate limit de Google Sheets API.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _DIR)

from billing.zoho_client import get_access_token, list_invoices  # noqa: E402
from billing.dashboard import main as refresh_dashboard           # noqa: E402
from billing.zoho_crm import get_crm_token, mark_deal_paid        # noqa: E402

SCOPES   = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
TAB_NAME = "PAGOS 2026"


def _open_sheet(spreadsheet_id: str, creds_json: str):
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(spreadsheet_id).worksheet(TAB_NAME)


def run() -> None:
    client_id      = os.environ["ZOHO_CLIENT_ID"]
    client_secret  = os.environ["ZOHO_CLIENT_SECRET"]
    refresh_token  = os.environ["ZOHO_REFRESH_TOKEN"]
    org_id         = os.environ.get("ZOHO_ORG_ID", "771911284")
    creds_json     = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    spreadsheet_id = os.environ["BILLING_SPREADSHEET_ID"]

    print("[Sync] Obteniendo token Zoho...")
    token = get_access_token(client_id, client_secret, refresh_token)

    crm_token = None
    if os.environ.get("ZOHO_REFRESH_TOKEN_CRM"):
        try:
            crm_token = get_crm_token()
            print("[Sync] CRM token OK")
        except Exception as e:
            print(f"[Sync] CRM token error (skipping CRM sync): {e}")

    print("[Sync] Consultando facturas pagadas en Zoho Books...")
    paid_invoices = list_invoices(token, org_id, status="paid")
    print(f"[Sync] {len(paid_invoices)} factura(s) pagada(s) en Zoho")

    if not paid_invoices:
        print("[Sync] Nada que sincronizar.")
        return

    # Construir mapa invoice_number → payment_date desde Zoho
    zoho_paid: dict[str, str] = {}
    for inv in paid_invoices:
        number = inv.get("invoice_number", "")
        if not number:
            continue
        payment_date = (
            inv.get("last_payment_date") or
            inv.get("payment_made_date") or
            inv.get("invoice_date", "")
        )
        if not payment_date:
            payment_date = datetime.utcnow().strftime("%Y-%m-%d")
        elif len(payment_date) == 10 and "-" not in payment_date:
            try:
                payment_date = datetime.strptime(payment_date, "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                payment_date = datetime.utcnow().strftime("%Y-%m-%d")
        zoho_paid[number] = payment_date

    # Leer el sheet UNA SOLA VEZ
    print("[Sync] Leyendo Google Sheets (una sola lectura)...")
    ws         = _open_sheet(spreadsheet_id, creds_json)
    all_values = ws.get_all_values()

    # Columnas: A=0 FechaFactura, B=1 Buyer, C=2 Mes, D=3 Revenue,
    #           E=4 N°Factura, F=5 Vencimiento, G=6 FechaPago, H=7 Dias, I=8 Estado
    INVOICE_COL = 4  # E (0-based)
    INV_DATE_COL = 0
    STATE_COL    = 8  # I

    updates_needed: list[tuple[int, str, str, int]] = []  # (row_1based, invoice_number, payment_date, days)

    for i, row in enumerate(all_values[1:], start=2):  # skip header
        if len(row) <= INVOICE_COL:
            continue
        inv_number = row[INVOICE_COL]
        estado     = row[STATE_COL].strip().upper() if len(row) > STATE_COL else ""

        if estado == "PAGADO":
            continue  # ya está pagado, skip

        if inv_number in zoho_paid:
            payment_date = zoho_paid[inv_number]
            # Calcular días desde fecha de factura
            try:
                inv_dt  = datetime.strptime(row[INV_DATE_COL], "%d/%m/%Y")
                pay_dt  = datetime.strptime(payment_date, "%Y-%m-%d")
                days    = (pay_dt - inv_dt).days
            except ValueError:
                days = 0
            updates_needed.append((i, inv_number, payment_date, days))

    print(f"[Sync] {len(updates_needed)} factura(s) a actualizar en Sheet")

    # Agrupar todas las actualizaciones en UNA sola llamada batch_update —
    # evita exceder la cuota de escrituras/minuto de Google Sheets cuando
    # hay varias facturas que sincronizar (antes: 1 llamada por factura).
    batch_data = []
    for row_num, inv_number, payment_date, days in updates_needed:
        payment_str = datetime.strptime(payment_date, "%Y-%m-%d").strftime("%d/%m/%Y")
        batch_data.append({"range": f"G{row_num}:I{row_num}", "values": [[payment_str, days, "PAGADO"]]})
        print(f"  ✓ {inv_number} → PAGADO ({payment_date}, {days} días)")

    if batch_data:
        ws.batch_update(batch_data)
    synced = len(updates_needed)

    if crm_token:
        for _, inv_number, payment_date, _ in updates_needed:
            try:
                mark_deal_paid(crm_token, inv_number, payment_date)
            except Exception as e:
                print(f"  [CRM] Error marcando deal {inv_number}: {e}")

    print(f"\n[Sync] Completado: {synced} actualizada(s).")

    if synced > 0:
        print("\n[Sync] Refrescando dashboard...")
        try:
            refresh_dashboard()
        except Exception as e:
            print(f"  [Sync] Error refrescando dashboard: {e}")


if __name__ == "__main__":
    run()

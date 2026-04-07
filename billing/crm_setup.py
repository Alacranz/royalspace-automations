"""
CRM Setup — Royalspace Billing

Script de configuración inicial: crea todos los buyers como cuentas
en Zoho CRM y crea deals para las facturas existentes en Google Sheets.

Correr UNA SOLA VEZ via workflow_dispatch después del primer deploy.
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

from billing.zoho_crm import get_crm_token, upsert_contact, log_invoice_deal, mark_deal_paid  # noqa: E402

SCOPES   = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
TAB_NAME = "PAGOS 2026"

# discord_name → category (para el tipo de cuenta en CRM)
BUYER_CATEGORIES = {
    "Rex Direct":        "external",
    "Ray Advertising":   "external",
    "Aragon Advertising":"external",
    "1800Dentist":       "external",
    "UNIK":              "external",
    "MarketCall":        "external",
    "ClickDealer":       "external",
}


def load_config() -> dict:
    with open(os.path.join(_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _open_sheet(spreadsheet_id: str, creds_json: str):
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(spreadsheet_id).worksheet(TAB_NAME)


def run() -> None:
    creds_json     = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    spreadsheet_id = os.environ["BILLING_SPREADSHEET_ID"]

    print("[CRM Setup] Obteniendo token CRM...")
    crm_token = get_crm_token()
    print("[CRM Setup] Token OK")

    config = load_config()
    # Todos los buyers únicos (union de auto_invoice + reminder)
    all_buyers = {b["discord_name"] for b in config["auto_invoice_buyers"]}
    all_buyers |= {b["discord_name"] for b in config["reminder_buyers"]}

    # ── Paso 1: Crear/actualizar cuentas en CRM ───────────────────────────────
    print(f"\n[CRM Setup] Creando {len(all_buyers)} cuenta(s) en CRM...")
    account_map: dict[str, str] = {}  # discord_name → account_id
    for name in sorted(all_buyers):
        category = BUYER_CATEGORIES.get(name, "external")
        account_id = upsert_contact(crm_token, name, category)
        account_map[name] = account_id

    # ── Paso 2: Leer PAGOS 2026 y crear deals para facturas existentes ────────
    print("\n[CRM Setup] Leyendo facturas del Sheet...")
    ws         = _open_sheet(spreadsheet_id, creds_json)
    all_values = ws.get_all_values()

    # Columnas: A=0 FechaFactura, B=1 Buyer, C=2 Mes, D=3 Revenue,
    #           E=4 N°Factura, F=5 Vencimiento, G=6 FechaPago, H=7 Dias, I=8 Estado
    deals_created = 0
    deals_paid    = 0
    deals_skipped = 0

    for row in all_values[1:]:
        if len(row) < 9 or not row[4]:
            continue

        buyer_name     = row[1].strip()
        billed_month   = row[2].strip()
        revenue_str    = row[3].strip()
        invoice_number = row[4].strip()
        due_date_str   = row[5].strip()
        payment_date   = row[6].strip()
        estado         = row[8].strip().upper()

        # Convertir revenue
        try:
            revenue = float(revenue_str.replace("$", "").replace(",", ""))
        except ValueError:
            revenue = 0.0

        # Convertir due_date DD/MM/YYYY → YYYY-MM-DD
        try:
            due_date = datetime.strptime(due_date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            due_date = datetime.utcnow().strftime("%Y-%m-%d")

        # Obtener o crear account
        account_id = account_map.get(buyer_name)
        if not account_id:
            category   = BUYER_CATEGORIES.get(buyer_name, "external")
            account_id = upsert_contact(crm_token, buyer_name, category)
            account_map[buyer_name] = account_id

        # Crear deal
        try:
            log_invoice_deal(
                token=crm_token,
                account_id=account_id,
                invoice_number=invoice_number,
                buyer_name=buyer_name,
                billed_month=billed_month,
                revenue=revenue,
                due_date=due_date,
            )
            deals_created += 1
        except Exception as e:
            print(f"  [CRM Setup] Error creando deal {invoice_number}: {e}")
            deals_skipped += 1
            continue

        # Si ya está pagada, marcarla como Closed Won
        if estado == "PAGADO" and payment_date:
            try:
                pay_dt = datetime.strptime(payment_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                mark_deal_paid(crm_token, invoice_number, pay_dt)
                deals_paid += 1
            except Exception as e:
                print(f"  [CRM Setup] Error marcando pagada {invoice_number}: {e}")

    print(f"\n[CRM Setup] Completado:")
    print(f"  Cuentas creadas/actualizadas : {len(account_map)}")
    print(f"  Deals creados                : {deals_created}")
    print(f"  Deals marcados pagados       : {deals_paid}")
    print(f"  Deals con error              : {deals_skipped}")


if __name__ == "__main__":
    run()

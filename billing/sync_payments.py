"""
Zoho → Google Sheets payment sync — Royalspace Billing

Consulta todas las facturas pagadas en Zoho Books y actualiza
el estado en Google Sheets (PENDIENTE/VENCIDO → PAGADO).

Corre automáticamente junto con los recordatorios semanales (lunes 9AM EST).
También puede correrse manualmente via workflow_dispatch.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _DIR)

from billing.zoho_client import get_access_token, list_invoices  # noqa: E402
from billing.payment_tracker import update_payment                # noqa: E402
from billing.dashboard import main as refresh_dashboard           # noqa: E402
from billing.zoho_crm import get_crm_token, mark_deal_paid        # noqa: E402


def run() -> None:
    client_id     = os.environ["ZOHO_CLIENT_ID"]
    client_secret = os.environ["ZOHO_CLIENT_SECRET"]
    refresh_token = os.environ["ZOHO_REFRESH_TOKEN"]
    org_id        = os.environ.get("ZOHO_ORG_ID", "771911284")
    creds_json    = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
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
    print(f"[Sync] {len(paid_invoices)} factura(s) pagada(s) encontradas en Zoho")

    if not paid_invoices:
        print("[Sync] Nada que sincronizar.")
        return

    synced = 0
    skipped = 0

    for inv in paid_invoices:
        invoice_number = inv.get("invoice_number", "")
        # Zoho devuelve last_payment_date o payment_made_date
        payment_date = (
            inv.get("last_payment_date") or
            inv.get("payment_made_date") or
            inv.get("invoice_date", "")
        )

        if not invoice_number:
            continue

        # payment_date puede venir como "YYYY-MM-DD" o estar vacío
        if not payment_date:
            # Usar fecha de hoy como fallback
            payment_date = datetime.utcnow().strftime("%Y-%m-%d")
        elif len(payment_date) == 10 and "-" not in payment_date:
            # Formato DD/MM/YYYY → convertir a YYYY-MM-DD
            try:
                payment_date = datetime.strptime(payment_date, "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                payment_date = datetime.utcnow().strftime("%Y-%m-%d")

        updated = update_payment(
            spreadsheet_id=spreadsheet_id,
            creds_json=creds_json,
            invoice_number=invoice_number,
            payment_date=payment_date,
        )

        if updated:
            synced += 1
            print(f"  ✓ {invoice_number} → PAGADO ({payment_date})")
            if crm_token:
                try:
                    mark_deal_paid(crm_token, invoice_number, payment_date)
                except Exception as e:
                    print(f"  [CRM] Error marcando deal {invoice_number}: {e}")
        else:
            skipped += 1

    print(f"\n[Sync] Completado: {synced} actualizada(s), {skipped} no encontrada(s) en Sheet (ya pagadas o no registradas).")

    if synced > 0:
        print("\n[Sync] Refrescando dashboard...")
        try:
            refresh_dashboard()
        except Exception as e:
            print(f"  [Sync] Error refrescando dashboard: {e}")


if __name__ == "__main__":
    run()

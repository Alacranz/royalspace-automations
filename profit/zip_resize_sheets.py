#!/usr/bin/env python3
"""
Redimensiona las hojas de zip codes al tamaño exacto de sus datos.
Script de una sola corrida — no re-fetcha nada de Ringba.
"""
from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SA_JSON        = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID = os.environ["ZIP_SPREADSHEET_ID"]

TABS_TO_FIX = ["Feb 2026", "Mar 2026", "Abr 2026"]


def main():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    for tab_name in TABS_TO_FIX:
        try:
            sheet = spreadsheet.worksheet(tab_name)
            # Contar filas con datos reales
            all_values = sheet.get_all_values()
            actual_rows = len(all_values)
            sheet.resize(rows=actual_rows, cols=3)
            sheet.set_basic_filter()
            print(f"  ✅ '{tab_name}' → redimensionada a {actual_rows} filas + filtro aplicado")
        except gspread.WorksheetNotFound:
            print(f"  ⚠️  '{tab_name}' no encontrada — omitida")
        except Exception as e:
            print(f"  ❌ '{tab_name}' error: {e}")

    print("Listo.")


if __name__ == "__main__":
    main()

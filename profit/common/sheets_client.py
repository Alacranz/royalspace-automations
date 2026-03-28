"""
Google Sheets client — Royalspace 2026
Wrapper sobre gspread con autenticación por service account.
"""
from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_spreadsheet() -> gspread.Spreadsheet:
    """
    Retorna el objeto Spreadsheet autenticado.
    Requiere GOOGLE_SERVICE_ACCOUNT_JSON y SPREADSHEET_ID en el entorno.
    """
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds   = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    gc      = gspread.authorize(creds)
    return gc.open_by_key(os.environ["SPREADSHEET_ID"])

#!/usr/bin/env python3
"""
Monthly Accounting — Ringba + Meta → Google Sheets ("2026")
Royalspace 2026

Variables de entorno requeridas:
  MONTH: MM/YYYY — mes a calcular (ej: 03/2026)

Más: RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID, META_ACCESS_TOKEN,
     META_API_VERSION, DISCORD_WEBHOOK_MOD,
     GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID.

Columnas hoja "2026":
  A  Name
  B  Revenue       — payout Ringba
  C  Adspent       — gasto Meta (negativo)
  D  Adspent 50%   = C*50%
  E  Profit        = B+D
  F  Spent/plus    = 'Week 2026'!M[última semana mes anterior]
  G  Spent         — VACÍO (manual: penalizaciones asistencia, etc.)
  H  Profit/Loss   = E+F+G  (sin prime)
  I  Royal Prime   — calculado según tabla de revenue
  J  Profit/Loss   = B+F+G+I+(C*50%)  (con prime)
  K  —             vacía
  L  Nota          — manual
  M  Revenue Dif   = SI(B_prev=0, NOD(), (B-B_prev)/B_prev)
  N  Profit Dif    = SI(E_prev=0, "N/A", (E-E_prev)/ABS(E_prev))
  O  Indicador     = ▲ / 🔻 / 🔹

  Q  Revenue       — tabla guía de premios (referencia visual)
  R  Royal Prime   — idem
"""
from __future__ import annotations

import json
import os
import re
import sys
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

import pytz

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send
from common.meta_client    import get_spend_range
from common.ringba_client  import get_publisher_summary, normalize_name
from common.sheets_client  import get_spreadsheet

# ── Secretos ──────────────────────────────────────────────────────────────────
RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN     = os.environ["META_ACCESS_TOKEN"]
META_VERSION   = os.environ.get("META_API_VERSION") or "v25.0"
WEBHOOK_MOD    = os.environ["DISCORD_WEBHOOK_MOD"]
MONTH_STR      = os.environ["MONTH"]   # MM/YYYY

CONFIG_PATH = Path(__file__).parent / "config.json"
VET = pytz.timezone("America/Caracas")
EST = pytz.timezone("America/New_York")

# ── Orden fijo de Media Buyers ─────────────────────────────────────────────────
MB_ORDER = [
    {"sheet_name": "Esteban",   "config_display": "Esteban Ramirez"},
    {"sheet_name": "Douglas",   "config_display": "Douglas Contreras"},
    {"sheet_name": "Luis",      "config_display": "Luis Salas"},
    {"sheet_name": "Kevin",     "config_display": "Kevin Pernia"},
    {"sheet_name": "Clara",     "config_display": "Clara Castro"},
    {"sheet_name": "Cordova",   "config_display": "Edixon Cordova"},
    {"sheet_name": "Sebastian", "config_display": "Sebastian Reyes"},
    {"sheet_name": "Caribay",   "config_display": "Caribay Flores"},
]

# ── Columnas (1-based) ────────────────────────────────────────────────────────
COL_NAME             = 1   # A
COL_REVENUE          = 2   # B
COL_ADSPENT          = 3   # C
COL_ADSPENT50        = 4   # D
COL_PROFIT           = 5   # E
COL_SPENTPLUS        = 6   # F  ← 'Week 2026'!M[row]
COL_SPENT            = 7   # G  ← vacío/manual
COL_PROFITLOSS_NOPRIME = 8 # H  = E+F+G
COL_ROYALPRIME       = 9   # I
COL_PROFITLOSS       = 10  # J  = B+F+G+I+(C*50%)
COL_EMPTY            = 11  # K  ← vacía
COL_NOTA             = 12  # L  ← manual
COL_REVDIF           = 13  # M
COL_PROFITDIF        = 14  # N
COL_INDICATOR        = 15  # O
TOTAL_COLS           = 15

# Tabla guía de premios — columnas Q–T (17–20)
COL_PRIZE_REV    = 17  # Q  Revenue
COL_PRIZE_RPRIME = 18  # R  Royal Prime
COL_PRIZE_PROFIT = 19  # S  Profit
COL_PRIZE_PRIME  = 20  # T  Prime

# ── Tabla de Royal Prime ───────────────────────────────────────────────────────
# (revenue_threshold, royal_prime, profit_target, prime_prize)
PRIME_TABLE = [
    (500,  50,  400,  300),
    (1000, 100, 500,  500),
    (1500, 150, 600,  1000),
    (2000, 200, 700,  1000),
    (2500, 250, 1000, 1100),
    (3000, 300, 1200, 1500),
    (3500, 350, 1400, 1600),
    (4000, 400, 1700, 2000),
]

# ── Colores ───────────────────────────────────────────────────────────────────
C_HEADER = {"red": 0.122, "green": 0.286, "blue": 0.490}  # #1F497D
C_DATA   = {"red": 0.812, "green": 0.886, "blue": 0.953}  # #CFE2F3
C_WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
C_BLACK  = {"red": 0.0,   "green": 0.0,   "blue": 0.0}
C_GOLD   = {"red": 1.0,   "green": 0.843, "blue": 0.0}
C_GRAY   = {"red": 0.3,   "green": 0.3,   "blue": 0.3}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════════════════════

def parse_month() -> tuple[int, int]:
    parts = MONTH_STR.strip().split("/")
    return int(parts[0]), int(parts[1])


def month_utc_range(year: int, month: int):
    days  = monthrange(year, month)[1]
    start = VET.localize(datetime(year, month, 1,    0,  0,  0)).astimezone(timezone.utc)
    end   = VET.localize(datetime(year, month, days, 23, 59, 59)).astimezone(timezone.utc)
    return start, end


def col_letter(col: int) -> str:
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def cr(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


def grid_range(sheet_id: int, r1: int, c1: int, r2: int, c2: int) -> dict:
    return {
        "sheetId":          sheet_id,
        "startRowIndex":    r1 - 1,
        "endRowIndex":      r2,
        "startColumnIndex": c1 - 1,
        "endColumnIndex":   c2,
    }


def get_royal_prime(revenue: float) -> float:
    """Retorna el Royal Prime correspondiente al revenue del mes."""
    prime = 0.0
    for threshold, amount, *_ in sorted(PRIME_TABLE, key=lambda x: x[0], reverse=True):
        if revenue >= threshold:
            prime = amount
            break
    return prime


# ═══════════════════════════════════════════════════════════════════════════════
# DATOS
# ═══════════════════════════════════════════════════════════════════════════════

def load_mb_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return {
        mb["display_name"]: {
            "publisher_name":         mb["publisher_name"],
            "facebook_ad_account_id": mb["facebook_ad_account_id"],
        }
        for mb in cfg.get("media_buyers") or []
    }


def fetch_ringba_payouts(start_utc, end_utc) -> dict[str, float]:
    # exclude_duplicates=True para coincidir con la UI de Ringba Publisher Summary
    pub_map = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc,
                                    exclude_duplicates=True)
    result  = {key: data["payout"] for key, data in pub_map.items()}
    print("  [Ringba] Payouts:")
    for k, v in sorted(result.items(), key=lambda x: -x[1]):
        if v > 0:
            print(f"    {k}: ${v:.2f}")
    return result


def fetch_meta_spends(year: int, month: int, mb_config: dict) -> dict[str, float]:
    days  = monthrange(year, month)[1]
    since = f"{year}-{month:02d}-01"
    until = f"{year}-{month:02d}-{days:02d}"
    spend_map: dict[str, float] = {}
    for mb in MB_ORDER:
        cfg   = mb_config.get(mb["config_display"]) or {}
        ad_id = cfg.get("facebook_ad_account_id") or ""
        if ad_id and ad_id not in spend_map:
            try:
                spend = get_spend_range(META_TOKEN, META_VERSION, ad_id, since, until)
            except Exception as e:
                print(f"  Advertencia Meta {mb['sheet_name']}: {e}")
                spend = 0.0
            spend_map[ad_id] = spend
            print(f"  Meta {mb['sheet_name']}: ${spend:.2f}")
    return spend_map


def find_prev_month_pagado_rows(ws_weekly, year: int, month: int) -> dict[str, int]:
    """
    Busca en 'Week 2026' los rows de cada MB en la ÚLTIMA semana del mes anterior.
    Retorna {sheet_name → row_number} para usar en la fórmula F = 'Week 2026'!M[row].

    Col M en la hoja semanal = COL_PAGADO (pagado, columna 13).
    """
    prev_month = month - 1
    prev_year  = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    mb_names      = {mb["sheet_name"] for mb in MB_ORDER}
    col_a         = ws_weekly.col_values(1)
    date_pattern  = re.compile(r'\d{2}/\d{2}/\d{2}\s*[-–]\s*\d{2}/\d{2}/\d{2}')

    # Encontrar filas de encabezado de fecha y sus índices
    table_starts = []
    for idx, val in enumerate(col_a):
        if date_pattern.match(str(val).strip()):
            table_starts.append(idx + 1)   # 1-based

    # Buscar la última tabla que pertenezca al mes anterior
    for start_row in reversed(table_starts):
        header_val = str(col_a[start_row - 1]).strip()
        # Parsear fecha final: "DD/MM/YY - DD/MM/YY"
        parts = re.split(r'\s*[-–]\s*', header_val)
        if len(parts) < 2:
            continue
        start_str = parts[0].strip()
        try:
            start_date = datetime.strptime(start_str, "%d/%m/%y")
        except ValueError:
            continue
        # Comparar por fecha de INICIO: la última semana que empieza en el mes anterior
        # (cubre semanas que cruzan meses, ej: 24/02 – 01/03)
        if start_date.month != prev_month or start_date.year != prev_year:
            continue

        # Tabla del mes anterior encontrada — leer filas de MB
        result = {}
        for i in range(start_row + 2, start_row + 2 + len(MB_ORDER) + 3):
            if i - 1 >= len(col_a):
                break
            cell = str(col_a[i - 1]).strip()
            if cell in mb_names:
                result[cell] = i
        if result:
            print(f"  [Weekly] Última semana de {prev_month:02d}/{prev_year} "
                  f"encontrada en fila {start_row}. Rows pagado: {result}")
            return result

    print(f"  [Weekly] Advertencia: no se encontró tabla de "
          f"{prev_month:02d}/{prev_year}. F quedará vacío.")
    return {}


def read_sistema_datos(spreadsheet) -> dict[str, float]:
    """
    Lee penalizaciones de asistencia desde hoja 'SISTEMA_DATOS'.
    Retorna {sheet_name → penalty} (negativo o 0).
    """
    result = {mb["sheet_name"]: 0.0 for mb in MB_ORDER}
    try:
        ws   = spreadsheet.worksheet("SISTEMA_DATOS")
        rows = ws.get_all_values()
        for row in rows:
            if len(row) < 2:
                continue
            name    = str(row[0]).strip()
            pen_str = str(row[1]).strip().replace("$", "").replace(",", "")
            if name in result:
                try:
                    result[name] = float(pen_str)
                except ValueError:
                    pass
    except Exception as e:
        print(f"Advertencia SISTEMA_DATOS: {e}. Penalizaciones = 0.")
    print(f"  [SISTEMA_DATOS] Penalizaciones: {result}")
    return result


def find_prev_month_mb_rows_in_2026(ws_2026) -> dict[str, int]:
    """
    Busca en la hoja '2026' el último row de cada MB (mes anterior).
    Retorna {sheet_name → row_number} para Revenue Dif y Profit Dif.
    """
    mb_names = {mb["sheet_name"] for mb in MB_ORDER}
    col_a    = ws_2026.col_values(1)
    result   = {}
    for idx, val in enumerate(col_a):
        cell = str(val).strip()
        if cell in mb_names:
            result[cell] = idx + 1   # 1-based (última ocurrencia = mes anterior)
    print(f"  [2026] Rows mes anterior: {result}")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DE TABLA
# ═══════════════════════════════════════════════════════════════════════════════

def build_table(
    month_label: str,
    mb_data: list[dict],
    start_row: int,
    prev_2026_rows: dict[str, int],
    prev_weekly_rows: dict[str, int],
) -> list[list]:
    """
    Construye la matriz de valores para la hoja '2026'.
    """
    # ── Header fila 1 ─────────────────────────────────────────────────────────
    h1 = [""] * TOTAL_COLS
    h1[COL_NAME       - 1] = month_label
    h1[COL_ADSPENT    - 1] = "total"
    h1[COL_ADSPENT50  - 1] = "50%"
    h1[COL_ROYALPRIME - 1] = "Winner"

    # ── Header fila 2 ─────────────────────────────────────────────────────────
    h2 = [""] * TOTAL_COLS
    h2[COL_NAME              - 1] = "Name"
    h2[COL_REVENUE           - 1] = "Revenue"
    h2[COL_ADSPENT           - 1] = "Adspent"
    h2[COL_ADSPENT50         - 1] = "Adspent 50%"
    h2[COL_PROFIT            - 1] = "Profit"
    h2[COL_SPENTPLUS         - 1] = "Spent/plus"
    h2[COL_SPENT             - 1] = "Spent"
    h2[COL_PROFITLOSS_NOPRIME - 1] = "Profit / Loss"
    h2[COL_ROYALPRIME        - 1] = "Royal Prime"
    h2[COL_PROFITLOSS        - 1] = "Profit / Loss"
    h2[COL_NOTA              - 1] = "Nota"
    h2[COL_REVDIF            - 1] = "Revenue Dif"
    h2[COL_PROFITDIF         - 1] = "Profit Dif"

    # ── Filas de MB ───────────────────────────────────────────────────────────
    data_rows = []
    for i, mb in enumerate(mb_data):
        row_n = start_row + 2 + i

        B = cr(row_n, COL_REVENUE)
        C = cr(row_n, COL_ADSPENT)
        D = cr(row_n, COL_ADSPENT50)
        E = cr(row_n, COL_PROFIT)
        F = cr(row_n, COL_SPENTPLUS)
        G = cr(row_n, COL_SPENT)
        I = cr(row_n, COL_ROYALPRIME)

        # F: referencia a pagado (col M=13) de la última semana del mes anterior
        weekly_row = prev_weekly_rows.get(mb["name"])
        if weekly_row:
            f_val = f"='Week 2026'!M{weekly_row}"
        else:
            f_val = ""

        row = [""] * TOTAL_COLS
        row[COL_NAME              - 1] = mb["name"]
        row[COL_REVENUE           - 1] = mb["payout"]
        row[COL_ADSPENT           - 1] = -mb["spend"]           # negativo
        row[COL_ADSPENT50         - 1] = f"={C}*50%"
        row[COL_PROFIT            - 1] = f"={B}+{D}"
        row[COL_SPENTPLUS         - 1] = f_val
        penalty = mb.get("attendance_penalty", 0.0)
        row[COL_SPENT             - 1] = -abs(penalty) if penalty else ""  # negativo si hay penalización
        row[COL_PROFITLOSS_NOPRIME - 1] = f"={E}+{F}+{G}"       # sin prime
        row[COL_ROYALPRIME        - 1] = mb.get("royal_prime") or ""
        row[COL_PROFITLOSS        - 1] = f"={B}+{F}+{G}+{I}+({C}*50%)"  # con prime
        row[COL_EMPTY             - 1] = ""                      # K vacía
        row[COL_NOTA              - 1] = ""                      # manual

        # Revenue Dif y Profit Dif vs mes anterior
        prev_row = prev_2026_rows.get(mb["name"])
        if prev_row:
            prev_B = cr(prev_row, COL_REVENUE)
            prev_E = cr(prev_row, COL_PROFIT)
            row[COL_REVDIF    - 1] = f"=SI({prev_B}=0,NOD(),({B}-{prev_B})/{prev_B})"
            row[COL_PROFITDIF - 1] = f'=SI({prev_E}=0,"N/A",({E}-{prev_E})/ABS({prev_E}))'
            N_ref = cr(row_n, COL_PROFITDIF)
            row[COL_INDICATOR - 1] = (
                f'=SI(ESNOD({N_ref}),"",SI({N_ref}>0,"▲",SI({N_ref}<0,"🔻","🔹")))'
            )

        data_rows.append(row)

    # ── Fila TOTAL ────────────────────────────────────────────────────────────
    tot_row_n = start_row + 2 + len(mb_data)
    mb_s      = start_row + 2
    mb_e      = tot_row_n - 1

    def s(col: int) -> str:
        return f"=SUMA({cr(mb_s, col)}:{cr(mb_e, col)})"

    total = [""] * TOTAL_COLS
    total[COL_NAME              - 1] = "TOTAL"
    total[COL_REVENUE           - 1] = s(COL_REVENUE)
    total[COL_ADSPENT           - 1] = s(COL_ADSPENT)
    total[COL_ADSPENT50         - 1] = s(COL_ADSPENT50)
    total[COL_PROFIT            - 1] = s(COL_PROFIT)
    total[COL_SPENTPLUS         - 1] = s(COL_SPENTPLUS)
    total[COL_SPENT             - 1] = s(COL_SPENT)
    total[COL_PROFITLOSS_NOPRIME - 1] = s(COL_PROFITLOSS_NOPRIME)
    total[COL_ROYALPRIME        - 1] = s(COL_ROYALPRIME)
    total[COL_PROFITLOSS        - 1] = s(COL_PROFITLOSS)

    # Revenue Dif y Profit Dif del TOTAL vs. el TOTAL del mes anterior
    if prev_2026_rows:
        prev_total_row = max(prev_2026_rows.values()) + 1  # TOTAL = última fila MB + 1
        prev_B = cr(prev_total_row, COL_REVENUE)
        prev_E = cr(prev_total_row, COL_PROFIT)
        tot_B  = cr(tot_row_n,      COL_REVENUE)
        tot_E  = cr(tot_row_n,      COL_PROFIT)
        total[COL_REVDIF    - 1] = f"=SI({prev_B}=0,NOD(),({tot_B}-{prev_B})/{prev_B})"
        total[COL_PROFITDIF - 1] = f'=SI({prev_E}=0,"N/A",({tot_E}-{prev_E})/ABS({prev_E}))'
        N_ref  = cr(tot_row_n, COL_PROFITDIF)
        total[COL_INDICATOR - 1] = (
            f'=SI(ESNOD({N_ref}),"",SI({N_ref}>0,"▲",SI({N_ref}<0,"🔻","🔹")))'
        )

    return [h1, h2] + data_rows + [total]


def build_prize_table() -> list[list]:
    """
    Construye la tabla guía de premios para columnas Q–T.
    Headers: Revenue | Royal Prime | Profit | Prime
    """
    rows = [["Revenue", "Royal Prime", "Profit", "Prime"]]
    for rev, royal_prime, profit, prime in PRIME_TABLE:
        rows.append([rev, royal_prime, profit, prime])
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATO
# ═══════════════════════════════════════════════════════════════════════════════

def apply_formatting(spreadsheet, ws, start_row: int, mb_count: int, prize_start_row: int) -> None:
    sheet_id = ws.id
    row_date = start_row
    row_cols = start_row + 1
    row_mb_s = start_row + 2
    row_mb_e = start_row + 2 + mb_count - 1
    row_tot  = start_row + 2 + mb_count

    currency_fmt = {"type": "NUMBER", "pattern": "$#,##0.00;[RED]-$#,##0.00"}
    pct_fmt      = {"type": "NUMBER", "pattern": "0.00%;[RED]-0.00%"}
    border       = {"style": "SOLID",   "width": 1, "color": C_GRAY}
    border_thick = {"style": "SOLID",   "width": 2, "color": C_GRAY}
    no_border    = {"style": "NONE"}

    def fmt_req(r1, c1, r2, c2, bg=None, fg=None, bold=None, num_fmt=None):
        cell_fmt = {}
        fields   = []
        if bg is not None:
            cell_fmt["backgroundColor"] = bg
            fields.append("backgroundColor")
        text_fmt = {}
        if fg   is not None: text_fmt["foregroundColor"] = fg
        if bold is not None: text_fmt["bold"] = bold
        if text_fmt:
            cell_fmt["textFormat"] = text_fmt
            fields.append("textFormat")
        if num_fmt is not None:
            cell_fmt["numberFormat"] = num_fmt
            fields.append("numberFormat")
        return {
            "repeatCell": {
                "range": grid_range(sheet_id, r1, c1, r2, c2),
                "cell":  {"userEnteredFormat": cell_fmt},
                "fields": "userEnteredFormat(" + ",".join(fields) + ")",
            }
        }

    reqs = []

    # ── Colores ───────────────────────────────────────────────────────────────
    # Rango principal A–J (1–10) + M–O (13–15) coloreados; K(11) y L(12) blanco

    # Fila de fecha: solo col A con fondo; B–O sin fondo (igual que FEB manual)
    reqs.append(fmt_req(row_date, 1,  row_date, 10,         C_HEADER, C_WHITE, True))
    reqs.append(fmt_req(row_date, 11, row_date, TOTAL_COLS, C_WHITE,  None,    None))

    # Fila de columnas
    reqs.append(fmt_req(row_cols, 1,  row_cols, 10,         C_HEADER, C_WHITE, True))
    reqs.append(fmt_req(row_cols, 11, row_cols, 12,         C_WHITE,  None,    None))
    reqs.append(fmt_req(row_cols, 13, row_cols, TOTAL_COLS, C_HEADER, C_WHITE, True))

    # Royal Prime header → dorado
    reqs.append(fmt_req(row_cols, COL_ROYALPRIME, row_cols, COL_ROYALPRIME,
                        C_HEADER, C_GOLD, True))

    # Filas MB
    reqs.append(fmt_req(row_mb_s, 1,  row_mb_e, 10,         C_DATA,  C_BLACK, False))
    reqs.append(fmt_req(row_mb_s, 11, row_mb_e, 12,         C_WHITE, None,    None))
    reqs.append(fmt_req(row_mb_s, 13, row_mb_e, TOTAL_COLS, C_DATA,  C_BLACK, False))

    # Fila TOTAL
    reqs.append(fmt_req(row_tot, 1,  row_tot, 10,           C_HEADER, C_WHITE, True))
    reqs.append(fmt_req(row_tot, 11, row_tot, 12,           C_WHITE,  None,    None))
    reqs.append(fmt_req(row_tot, 13, row_tot, TOTAL_COLS,   C_HEADER, C_WHITE, True))

    # ── Formato numérico ──────────────────────────────────────────────────────
    currency_cols = [
        COL_REVENUE, COL_ADSPENT, COL_ADSPENT50, COL_PROFIT,
        COL_SPENTPLUS, COL_SPENT, COL_PROFITLOSS_NOPRIME,
        COL_ROYALPRIME, COL_PROFITLOSS,
    ]
    for col in currency_cols:
        reqs.append({
            "repeatCell": {
                "range": grid_range(sheet_id, row_mb_s, col, row_tot, col),
                "cell":  {"userEnteredFormat": {"numberFormat": currency_fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    for col in [COL_REVDIF, COL_PROFITDIF]:
        reqs.append({
            "repeatCell": {
                "range": grid_range(sheet_id, row_mb_s, col, row_tot, col),
                "cell":  {"userEnteredFormat": {"numberFormat": pct_fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # ── Bordes ────────────────────────────────────────────────────────────────
    # Tabla principal A–J
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 1, row_tot, 10),
            "top": border, "bottom": border, "left": border, "right": border,
            "innerHorizontal": border, "innerVertical": border,
        }
    })
    # K–L sin bordes
    reqs.append({
        "updateBorders": {
            "range": grid_range(sheet_id, row_date, 11, row_tot, 12),
            "top": no_border, "bottom": no_border,
            "left": no_border, "right": no_border,
            "innerHorizontal": no_border, "innerVertical": no_border,
        }
    })
    # M–O (Revenue Dif, Profit Dif, Indicator)
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 13, row_tot, TOTAL_COLS),
            "top": border, "bottom": border, "left": border, "right": border,
            "innerHorizontal": border, "innerVertical": border,
        }
    })

    # ── Bordes gruesos en col E (right), H (right), J (right) ─────────────────
    # Borde derecho grueso en E (col 5) — separa Profit de Spent/plus
    for col in [COL_PROFIT, COL_PROFITLOSS_NOPRIME, COL_PROFITLOSS]:
        reqs.append({
            "updateBorders": {
                "range": grid_range(sheet_id, row_date, col, row_tot, col),
                "right": border_thick,
            }
        })

    # ── Tabla guía de premios Q–T: formato ───────────────────────────────────
    prize_end_row = prize_start_row + len(PRIME_TABLE)  # header + 8 filas de datos

    # Header Q–T
    reqs.append({
        "repeatCell": {
            "range": grid_range(sheet_id, prize_start_row, COL_PRIZE_REV, prize_start_row, COL_PRIZE_PRIME),
            "cell":  {"userEnteredFormat": {
                "backgroundColor": C_HEADER,
                "textFormat": {"bold": True, "foregroundColor": C_WHITE},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })
    # Datos Q–T
    reqs.append({
        "repeatCell": {
            "range": grid_range(sheet_id, prize_start_row + 1, COL_PRIZE_REV, prize_end_row + 1, COL_PRIZE_PRIME),
            "cell":  {"userEnteredFormat": {
                "backgroundColor": C_DATA,
                "textFormat": {"foregroundColor": C_BLACK},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })
    # Bordes Q–T
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, prize_start_row, COL_PRIZE_REV, prize_end_row + 1, COL_PRIZE_PRIME),
            "top": border, "bottom": border, "left": border, "right": border,
            "innerHorizontal": border, "innerVertical": border,
        }
    })
    # Formato numérico en Q y R (Revenue y Royal Prime)
    for col in [COL_PRIZE_REV, COL_PRIZE_RPRIME]:
        reqs.append({
            "repeatCell": {
                "range": grid_range(sheet_id, prize_start_row + 1, col, prize_end_row + 1, col),
                "cell":  {"userEnteredFormat": {"numberFormat": currency_fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    spreadsheet.batch_update({"requests": reqs})


def apply_attendance_notes(spreadsheet, ws, start_row: int, mb_data: list[dict]) -> None:
    """
    Agrega notas en la celda G de cada MB que tenga penalización de asistencia.
    Formato: "-3 Asistencia"
    """
    sheet_id = ws.id
    reqs = []
    for i, mb in enumerate(mb_data):
        penalty = mb.get("attendance_penalty", 0.0)
        if not penalty:
            continue
        row_n = start_row + 2 + i
        reqs.append({
            "repeatCell": {
                "range": {
                    "sheetId":          sheet_id,
                    "startRowIndex":    row_n - 1,
                    "endRowIndex":      row_n,
                    "startColumnIndex": COL_SPENT - 1,
                    "endColumnIndex":   COL_SPENT,
                },
                "cell":   {"note": f"-{abs(penalty):g} Asistencia"},
                "fields": "note",
            }
        })
    if reqs:
        spreadsheet.batch_update({"requests": reqs})
        noted = [mb["name"] for mb in mb_data if mb.get("attendance_penalty")]
        print(f"Notas de asistencia agregadas en G: {noted}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    month, year = parse_month()
    month_names = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
    }
    days        = monthrange(year, month)[1]
    month_label = f"01/{month:02d}/{str(year)[2:]} - {days:02d}/{month:02d}/{str(year)[2:]}"
    print(f"Mes: {month_names[month]} {year}  ({month_label})")

    # ── 1. Rango del mes ──────────────────────────────────────────────────────
    start_utc, end_utc = month_utc_range(year, month)
    print(f"Rango UTC → {start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} / "
          f"{end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")

    # ── 2. Config ─────────────────────────────────────────────────────────────
    mb_config = load_mb_config()

    # ── 3. Ringba y Meta ──────────────────────────────────────────────────────
    print("Consultando Ringba...")
    payouts = fetch_ringba_payouts(start_utc, end_utc)

    print("Consultando Meta...")
    spends  = fetch_meta_spends(year, month, mb_config)

    # ── 4. Google Sheets ──────────────────────────────────────────────────────
    print("Conectando con Google Sheets...")
    spreadsheet = get_spreadsheet()

    # Hoja semanal: última semana del mes anterior (para columna F)
    ws_weekly = spreadsheet.worksheet("Week 2026")
    prev_weekly_rows = find_prev_month_pagado_rows(ws_weekly, year, month)

    # Hoja 2026: mes anterior (para Revenue Dif y Profit Dif)
    try:
        ws_2026 = spreadsheet.worksheet("2026")
    except Exception:
        ws_2026 = spreadsheet.add_worksheet("2026", rows=500, cols=20)

    prev_2026_rows = find_prev_month_mb_rows_in_2026(ws_2026)

    # Penalizaciones de asistencia (SISTEMA_DATOS)
    print("Leyendo penalizaciones de asistencia...")
    attendance_map = read_sistema_datos(spreadsheet)

    # ── 5. Construir datos por MB ─────────────────────────────────────────────
    mb_data = []
    for mb in MB_ORDER:
        cfg     = mb_config.get(mb["config_display"]) or {}
        pub_key = normalize_name(cfg.get("publisher_name") or "")
        ad_id   = cfg.get("facebook_ad_account_id") or ""
        payout  = payouts.get(pub_key, 0.0)
        spend   = spends.get(ad_id, 0.0)
        prime   = get_royal_prime(payout)
        penalty = attendance_map.get(mb["sheet_name"], 0.0)

        print(f"  {mb['sheet_name']}: payout=${payout:.2f}  spend=${spend:.2f}  "
              f"prime=${prime:.0f}  asistencia=-${penalty:.0f}")

        mb_data.append({
            "name":               mb["sheet_name"],
            "payout":             payout,
            "spend":              spend,
            "royal_prime":        prime if prime > 0 else "",
            "attendance_penalty": penalty,
        })

    # ── 6. Determinar fila de inicio ──────────────────────────────────────────
    col_a    = ws_2026.col_values(1)
    last_row = max((i + 1 for i, v in enumerate(col_a) if str(v).strip()), default=0)
    start_row = last_row + 3 if last_row > 0 else 1
    print(f"Última fila con datos: {last_row}  →  nueva tabla en fila {start_row}")

    # ── 7. Escribir tabla principal ───────────────────────────────────────────
    all_rows = build_table(month_label, mb_data, start_row, prev_2026_rows, prev_weekly_rows)
    end_row  = start_row + len(all_rows) - 1
    rng      = f"A{start_row}:{col_letter(TOTAL_COLS)}{end_row}"
    ws_2026.update(range_name=rng, values=all_rows, value_input_option="USER_ENTERED")
    print(f"Tabla escrita en {rng}")

    # ── 8. Escribir tabla guía de premios (Q-R) ───────────────────────────────
    prize_rows = build_prize_table()
    prize_end  = start_row + len(prize_rows) - 1
    prize_rng  = f"Q{start_row}:T{prize_end}"
    ws_2026.update(range_name=prize_rng, values=prize_rows, value_input_option="USER_ENTERED")
    print(f"Tabla de premios escrita en {prize_rng}")

    # ── 9. Formato ────────────────────────────────────────────────────────────
    apply_formatting(spreadsheet, ws_2026, start_row, len(mb_data), start_row)
    print("Formato aplicado")

    # ── 10. Notas de penalización de asistencia en G ──────────────────────────
    apply_attendance_notes(spreadsheet, ws_2026, start_row, mb_data)

    # ── 11. Discord ───────────────────────────────────────────────────────────
    penalized = [mb["name"] for mb in mb_data if mb.get("attendance_penalty")]
    penalty_note = (
        f"  Penalizaciones en G: {', '.join(penalized)}" if penalized
        else "  Sin penalizaciones de asistencia."
    )
    msg = (
        f"✅ **Contabilidad mensual: {month_names[month]} {year}**\n"
        f"Tabla escrita en hoja '2026'.\n{penalty_note}"
    )
    discord_send(WEBHOOK_MOD, msg)
    print("OK — confirmación enviada a Discord #mod")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            discord_send(WEBHOOK_MOD, f"[CONTABILIDAD MENSUAL ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

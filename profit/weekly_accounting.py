#!/usr/bin/env python3
"""
Weekly Accounting — Ringba + Meta → Google Sheets
Royalspace 2026

Variables de entorno requeridas:
  DATE_INICIO: DD/MM/YYYY — primer día de la semana (ej: 02/03/2026)
  DATE_FIN:    DD/MM/YYYY — último día de la semana (ej: 08/03/2026)

Más: RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID, META_ACCESS_TOKEN,
     META_API_VERSION, DISCORD_WEBHOOK_MOD,
     GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID.

Lógica:
  1. Fetcha payout de Ringba y spend de Meta para el rango dado.
  2. Escribe nueva tabla en hoja "Week 2026" con fórmulas.
  3. Si es la última semana del mes:
     - Calcula Royal Prime (payout acumulado del mes desde Ringba).
     - Lee penalizaciones de asistencia desde hoja "SISTEMA_DATOS".
     - Escribe tabla mensual en hoja "2026".
  4. Envía confirmación a Discord #mod.
"""
from __future__ import annotations

import json
import os
import sys
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send
from common.meta_client    import get_spend_range
from common.ringba_client  import get_publisher_summary, normalize_name
from common.sheets_client  import get_spreadsheet

# ── Secretos ──────────────────────────────────────────────────────────────────
RINGBA_TOKEN    = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT  = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN      = os.environ["META_ACCESS_TOKEN"]
META_VERSION    = os.environ.get("META_API_VERSION", "v25.0")
WEBHOOK_MOD     = os.environ["DISCORD_WEBHOOK_MOD"]
DATE_INICIO_STR = os.environ["DATE_INICIO"]   # DD/MM/YYYY
DATE_FIN_STR    = os.environ["DATE_FIN"]      # DD/MM/YYYY

CONFIG_PATH = Path(__file__).parent / "config.json"
EST         = pytz.timezone("America/New_York")   # horario laboral y Meta
VET         = pytz.timezone("America/Caracas")    # UTC-4 fijo — igual que Ringba UI

# ── Orden fijo de Media Buyers ─────────────────────────────────────────────────
# sheet_name    : nombre que aparece en columna A del sheet
# config_display: display_name en config.json (para lookup de publisher/meta)
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
COL_NAME       = 1   # A
COL_REVENUE    = 2   # B  — payout de Ringba (positivo)
COL_ADSPENT    = 3   # C  — gasto Meta (negativo → convención contable)
COL_ADSPENT50  = 4   # D  = C*50%
COL_PROFIT     = 5   # E  = B+D
COL_SPENTPLUS  = 6   # F  = M semana anterior
COL_SPENT      = 7   # G  — manual / penalidad asistencia (última semana)
COL_ROYALPRIME = 8   # H  — solo última semana del mes
COL_PROFITLOSS = 9   # I  = B+F+G+H+(C*50%)
COL_PAYMENT    = 10  # J  — manual
COL_FUTUREDEBT = 11  # K  = I-J
COL_NOTA       = 12  # L  — manual
COL_PAGADO     = 13  # M  — manual
COL_REVDIF     = 14  # N
COL_PROFITDIF  = 15  # O
COL_INDICATOR  = 16  # P
TOTAL_COLS     = 16

# ── Colores RGB (0.0 – 1.0) ───────────────────────────────────────────────────
# Fallback: se intentan leer de las tablas existentes en el Sheet.
# Si no hay tablas previas, se usa este azul medio oscuro estándar.
C_HEADER_DATE = {"red": 0.122, "green": 0.286, "blue": 0.490}   # #1F497D — fallback
C_HEADER_COLS = {"red": 0.122, "green": 0.286, "blue": 0.490}   # #1F497D — fallback
C_ROW_TOTAL   = {"red": 0.122, "green": 0.286, "blue": 0.490}   # #1F497D — fallback
C_DATA_ROW    = {"red": 0.812, "green": 0.886, "blue": 0.953}   # #CFE2F3 — celeste 3 fallback
C_WHITE       = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
C_BLACK       = {"red": 0.0,   "green": 0.0,   "blue": 0.0}
C_GOLD        = {"red": 1.0,   "green": 0.843, "blue": 0.0}     # #FFD700
C_GRAY_BORDER = {"red": 0.3,   "green": 0.3,   "blue": 0.3}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILIDADES DE FECHA
# ═══════════════════════════════════════════════════════════════════════════════

def parse_dates():
    """
    Parsea DATE_INICIO / DATE_FIN (DD/MM/YYYY) y retorna:
    d_inicio, d_fin, start_utc, end_utc, since_str, until_str
    """
    d_inicio = datetime.strptime(DATE_INICIO_STR, "%d/%m/%Y")
    d_fin    = datetime.strptime(DATE_FIN_STR,    "%d/%m/%Y")

    # Usar VET (UTC-4, igual que Ringba UI) para que el rango coincida exactamente
    start_utc = VET.localize(
        datetime(d_inicio.year, d_inicio.month, d_inicio.day, 0, 0, 0)
    ).astimezone(timezone.utc)
    end_utc = VET.localize(
        datetime(d_fin.year, d_fin.month, d_fin.day, 23, 59, 59)
    ).astimezone(timezone.utc)

    since_str = d_inicio.strftime("%Y-%m-%d")
    until_str = d_fin.strftime("%Y-%m-%d")

    return d_inicio, d_fin, start_utc, end_utc, since_str, until_str


def is_last_week_of_month(d_fin: datetime) -> bool:
    """Es la última semana si d_fin + 7 días cae en el mes siguiente."""
    return (d_fin + timedelta(days=7)).month != d_fin.month


def month_utc_range(year: int, month: int):
    """UTC range del mes completo (en VET, igual que Ringba UI)."""
    days = monthrange(year, month)[1]
    start = VET.localize(datetime(year, month, 1, 0, 0, 0)).astimezone(timezone.utc)
    end   = VET.localize(datetime(year, month, days, 23, 59, 59)).astimezone(timezone.utc)
    return start, end


def fmt_header(d: datetime) -> str:
    return d.strftime("%d/%m/%y")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG Y DATOS
# ═══════════════════════════════════════════════════════════════════════════════

def load_mb_config() -> dict:
    """
    Lee config.json y retorna:
    {config_display → {publisher_name, facebook_ad_account_id, mb_share}}
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    lookup = {}
    for mb in config.get("media_buyers") or []:
        lookup[mb["display_name"]] = {
            "publisher_name":        mb["publisher_name"],
            "facebook_ad_account_id": mb["facebook_ad_account_id"],
            "mb_share":              float(mb.get("media_buyer_spend_share", 0.5)),
        }
    return lookup


def fetch_ringba_payouts(start_utc, end_utc) -> dict[str, float]:
    """
    Retorna {normalized_publisher_name → payout}.

    Usa get_publisher_summary() — mismo método que true_profit.py (alertas cada
    30 min). Suma payoutAmount de todos los registros del rango, igual que la
    columna Payout del Publisher Summary de Ringba.
    """
    pub_map = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)
    result  = {key: data["payout"] for key, data in pub_map.items()}

    print(f"  [Ringba] Payout por publisher:")
    for k, v in sorted(result.items(), key=lambda x: -x[1]):
        if v > 0:
            print(f"    {k}: ${v:.2f}")
    return result


def fetch_meta_spends(since: str, until: str, mb_config: dict) -> dict[str, float]:
    """Retorna {facebook_ad_account_id → spend}."""
    spend_map: dict[str, float] = {}
    meta_errors: list[str] = []
    for mb in MB_ORDER:
        cfg  = mb_config.get(mb["config_display"]) or {}
        ad_id = cfg.get("facebook_ad_account_id") or ""
        if ad_id and ad_id not in spend_map:
            try:
                spend = get_spend_range(META_TOKEN, META_VERSION, ad_id, since, until)
            except Exception as e:
                msg = f"⚠️ Meta API error — {mb['sheet_name']} ({ad_id}): {e}"
                print(f"  {msg}")
                meta_errors.append(msg)
                spend = 0.0
            spend_map[ad_id] = spend
            print(f"  Meta {mb['sheet_name']}: ${spend:.2f}")

    if meta_errors:
        alert = (
            f"🚨 **[CONTABILIDAD] Error Meta API — adspent usando $0.00**\n"
            + "\n".join(meta_errors)
            + "\nRevisa el Sheet y corrige manualmente si es necesario."
        )
        try:
            discord_send(WEBHOOK_MOD, alert)
        except Exception:
            pass

    return spend_map


def read_royal_prime_table(spreadsheet) -> list[tuple[float, float]]:
    """
    Lee la tabla de Royal Prime desde la hoja '2026'.
    Retorna lista de (revenue_threshold, prime_amount) ordenada ascendente.
    Fallback hardcoded si no se puede leer.
    """
    fallback = [
        (500, 50), (1000, 100), (1500, 150), (2000, 200),
        (2500, 250), (3000, 300), (3500, 350), (4000, 400),
    ]
    try:
        ws   = spreadsheet.worksheet("2026")
        vals = ws.get_all_values()
        rev_col = prime_col = header_row = None

        for r_idx, row in enumerate(vals):
            for c_idx, cell in enumerate(row):
                s = str(cell).strip().lower()
                if s == "revenue":
                    rev_col    = c_idx
                if s == "royal prime":
                    prime_col  = c_idx
                    header_row = r_idx

            if rev_col is not None and prime_col is not None and header_row is not None:
                table = []
                for data_row in vals[header_row + 1:]:
                    if len(data_row) <= max(rev_col, prime_col):
                        break
                    r_str = data_row[rev_col].replace("$", "").replace(",", "").strip()
                    p_str = data_row[prime_col].replace("$", "").replace(",", "").strip()
                    if not r_str and not p_str:
                        break
                    try:
                        table.append((float(r_str), float(p_str)))
                    except ValueError:
                        break
                if table:
                    return sorted(table, key=lambda x: x[0])
                break
    except Exception as e:
        print(f"Advertencia: Royal Prime table — {e}. Usando fallback.")

    return fallback


def get_royal_prime(monthly_payout: float, prime_table: list) -> float:
    """Retorna el Royal Prime correspondiente al payout acumulado del mes."""
    prime = 0.0
    for threshold, amount in sorted(prime_table, key=lambda x: x[0], reverse=True):
        if monthly_payout >= threshold:
            prime = amount
            break
    return prime


def read_sistema_datos(spreadsheet) -> dict[str, float]:
    """
    Lee penalizaciones desde hoja 'SISTEMA_DATOS'.
    Retorna {sheet_name → penalty} (penalty es negativo o 0).
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
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# UTILIDADES DE SHEET
# ═══════════════════════════════════════════════════════════════════════════════

def col_letter(col: int) -> str:
    """1-based column index → letter (1=A, 2=B, …, 26=Z, 27=AA, …)."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def cr(row: int, col: int) -> str:
    """Cell reference: row 1-based, col 1-based → e.g. 'B5'."""
    return f"{col_letter(col)}{row}"


def grid_range(sheet_id: int, r1: int, c1: int, r2: int, c2: int) -> dict:
    """0-based grid range for Sheets API."""
    return {
        "sheetId":          sheet_id,
        "startRowIndex":    r1 - 1,
        "endRowIndex":      r2,
        "startColumnIndex": c1 - 1,
        "endColumnIndex":   c2,
    }


def find_previous_rows(ws) -> dict:
    """
    Escanea columna A y retorna:
      {mb_name → last_row_number}
      "_last_data_row"  → último row con datos
      "_last_total_row" → último row con "TOTAL"
      "_last_name_row"  → último row con "Name" (encabezado de columnas)
    """
    mb_names = {mb["sheet_name"] for mb in MB_ORDER}
    col_a    = ws.col_values(1)   # lista 0-indexed, valores de col A

    result = {name: None for name in mb_names}
    result["_last_data_row"]  = 0
    result["_last_total_row"] = None
    result["_last_name_row"]  = None   # fila con "Name" (fila 2 de cada tabla)

    for idx, val in enumerate(col_a):
        row_num = idx + 1
        cell    = str(val).strip()
        if cell in mb_names:
            result[cell]             = row_num
            result["_last_data_row"] = row_num
        elif cell.upper() == "TOTAL":
            result["_last_total_row"] = row_num
            result["_last_data_row"]  = row_num
        elif cell.lower() == "name":
            result["_last_name_row"]  = row_num
            result["_last_data_row"]  = row_num
        elif cell:
            result["_last_data_row"]  = row_num

    return result


def _read_cell_bg_color(spreadsheet, sheet_title: str, row: int, col: int) -> dict | None:
    """
    Lee el backgroundColor de una celda vía Sheets API.
    Retorna dict {red, green, blue} o None si la celda es blanca / no tiene color.
    """
    try:
        cell_ref = f"'{sheet_title}'!{col_letter(col)}{row}"
        resp = spreadsheet.client.request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet.id}",
            params={
                "ranges":          cell_ref,
                "includeGridData": "true",
                "fields":          "sheets.data.rowData.values.userEnteredFormat.backgroundColor",
            },
        )
        data  = resp.json()
        color = (
            data.get("sheets", [{}])[0]
                .get("data",    [{}])[0]
                .get("rowData", [{}])[0]
                .get("values",  [{}])[0]
                .get("userEnteredFormat", {})
                .get("backgroundColor")
        )
        if not color:
            return None
        r = color.get("red",   1.0)
        g = color.get("green", 1.0)
        b = color.get("blue",  1.0)
        # Solo devolver si es significativamente distinto de blanco
        if r < 0.95 or g < 0.95 or b < 0.95:
            return {"red": r, "green": g, "blue": b}
    except Exception as e:
        print(f"  (no se pudo leer color de celda: {e})")
    return None


def read_existing_header_colors(spreadsheet, ws, name_row: int | None) -> tuple:
    """
    Lee los colores de las filas de la tabla anterior.
    name_row: fila con "Name" (fila 2 de la última tabla).
    Retorna (date_color, cols_color, data_row_color).
    Usa los colores constantes como fallback si no hay tabla previa.
    """
    if not name_row or name_row < 2:
        return C_HEADER_DATE, C_HEADER_COLS, C_DATA_ROW

    date_color     = _read_cell_bg_color(spreadsheet, ws.title, name_row - 1, 1)
    cols_color     = _read_cell_bg_color(spreadsheet, ws.title, name_row,     1)
    data_row_color = _read_cell_bg_color(spreadsheet, ws.title, name_row + 1, 1)  # 1.ª fila de MB

    date_color     = date_color     or C_HEADER_DATE
    cols_color     = cols_color     or C_HEADER_COLS
    data_row_color = data_row_color or C_DATA_ROW

    print(f"  Colores leídos → date: {date_color}  "
          f"cols: {cols_color}  data: {data_row_color}")
    return date_color, cols_color, data_row_color


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN Y ESCRITURA DE TABLA
# ═══════════════════════════════════════════════════════════════════════════════

def build_table_values(
    d_inicio: datetime,
    d_fin: datetime,
    mb_rows_data: list[dict],
    prev: dict,
    start_row: int,
) -> tuple[list[list], dict, int]:
    """
    Construye la matriz de valores (11 filas × 16 columnas).
    Retorna: (rows, {mb_name → sheet_row_number}, total_row_number)

    Convención de adspent: se almacena negativo (gasto).
    Todas las fórmulas en español (SI, SUMA, ESNOD, NOD, ABS).
    """
    header_row_1 = [""] * TOTAL_COLS
    header_row_1[COL_NAME - 1]      = f"{fmt_header(d_inicio)} - {fmt_header(d_fin)}"
    header_row_1[COL_ADSPENT - 1]   = "total"
    header_row_1[COL_ADSPENT50 - 1] = "50%"
    header_row_1[COL_ROYALPRIME - 1] = "Winner"

    header_row_2 = [""] * TOTAL_COLS
    header_row_2[COL_NAME - 1]       = "Name"
    header_row_2[COL_REVENUE - 1]    = "Revenue"
    header_row_2[COL_ADSPENT - 1]    = "Adspent"
    header_row_2[COL_ADSPENT50 - 1]  = "Adspent 50%"
    header_row_2[COL_PROFIT - 1]     = "Profit"
    header_row_2[COL_SPENTPLUS - 1]  = "Spent/plus"
    header_row_2[COL_SPENT - 1]      = "Spent"
    header_row_2[COL_ROYALPRIME - 1] = "Royal Prime"
    header_row_2[COL_PROFITLOSS - 1] = "Profit/Loss"
    header_row_2[COL_PAYMENT - 1]    = "Payment"
    header_row_2[COL_FUTUREDEBT - 1] = "Future debt"
    header_row_2[COL_NOTA - 1]       = "Nota"
    header_row_2[COL_PAGADO - 1]     = "pagado"
    header_row_2[COL_REVDIF - 1]     = "Revenue Dif"
    header_row_2[COL_PROFITDIF - 1]  = "Profit Dif"

    data_rows    = []
    mb_row_nums  = {}

    for i, mb in enumerate(mb_rows_data):
        row_n = start_row + 2 + i    # +2: date header + col headers

        mb_row_nums[mb["name"]] = row_n

        B = cr(row_n, COL_REVENUE)
        C = cr(row_n, COL_ADSPENT)
        H = cr(row_n, COL_SPENTPLUS)
        I = cr(row_n, COL_SPENT)
        J = cr(row_n, COL_ROYALPRIME)
        K = cr(row_n, COL_PROFITLOSS)
        L = cr(row_n, COL_PAYMENT)
        E = cr(row_n, COL_PROFIT)

        # H: referencia directa al M de la semana anterior
        prev_mb_row = prev.get(mb["name"])
        if prev_mb_row:
            h_val = f"={cr(prev_mb_row, COL_FUTUREDEBT)}"
        else:
            h_val = 0

        row = [""] * TOTAL_COLS
        row[COL_NAME - 1]       = mb["name"]
        row[COL_REVENUE - 1]    = mb["payout"]
        row[COL_ADSPENT - 1]    = -mb["spend"]         # negativo (convención)
        row[COL_ADSPENT50 - 1]  = f"={C}*50%"
        row[COL_PROFIT - 1]     = f"={B}+{cr(row_n, COL_ADSPENT50)}"
        row[COL_SPENTPLUS - 1]  = h_val
        row[COL_SPENT - 1]      = mb.get("attendance_penalty") or ""
        row[COL_ROYALPRIME - 1] = mb.get("royal_prime") or ""
        row[COL_PROFITLOSS - 1] = f"={B}+{H}+{I}+{J}+({C}*50%)"
        row[COL_PAYMENT - 1]    = ""
        row[COL_FUTUREDEBT - 1] = f"={K}-{L}"
        row[COL_NOTA - 1]       = ""
        row[COL_PAGADO - 1]     = ""

        # Revenue Dif (P) — compara con semana anterior
        if prev_mb_row:
            prev_B = cr(prev_mb_row, COL_REVENUE)
            row[COL_REVDIF - 1] = (
                f"=SI({prev_B}=0,NOD(),({B}-{prev_B})/{prev_B})"
            )
            prev_E = cr(prev_mb_row, COL_PROFIT)
            row[COL_PROFITDIF - 1] = (
                f'=SI({prev_E}=0,"N/A",({E}-{prev_E})/ABS({prev_E}))'
            )
        else:
            row[COL_REVDIF - 1]    = ""
            row[COL_PROFITDIF - 1] = ""

        # Indicator (R)
        Q = cr(row_n, COL_PROFITDIF)
        row[COL_INDICATOR - 1] = (
            f'=SI(ESNOD({Q}),"",SI({Q}>0,"▲",SI({Q}<0,"🔻","🔹")))'
        )

        data_rows.append(row)

    # ── TOTAL row ──────────────────────────────────────────────────────────────
    total_row_n  = start_row + 2 + len(mb_rows_data)
    mb_first_row = start_row + 2
    mb_last_row  = total_row_n - 1

    def sums(col: int) -> str:
        return f"=SUMA({cr(mb_first_row, col)}:{cr(mb_last_row, col)})"

    total_row = [""] * TOTAL_COLS
    total_row[COL_NAME - 1]       = "TOTAL"
    total_row[COL_REVENUE - 1]    = sums(COL_REVENUE)
    total_row[COL_ADSPENT - 1]    = sums(COL_ADSPENT)
    total_row[COL_ADSPENT50 - 1]  = sums(COL_ADSPENT50)
    total_row[COL_PROFIT - 1]     = sums(COL_PROFIT)
    total_row[COL_SPENTPLUS - 1]  = sums(COL_SPENTPLUS)
    total_row[COL_SPENT - 1]      = sums(COL_SPENT)
    total_row[COL_ROYALPRIME - 1] = sums(COL_ROYALPRIME)
    total_row[COL_PROFITLOSS - 1] = sums(COL_PROFITLOSS)
    total_row[COL_PAYMENT - 1]    = sums(COL_PAYMENT)
    total_row[COL_FUTUREDEBT - 1] = sums(COL_FUTUREDEBT)

    # Revenue Dif / Profit Dif para TOTAL
    prev_total = prev.get("_last_total_row")
    if prev_total:
        B_tot      = cr(total_row_n, COL_REVENUE)
        E_tot      = cr(total_row_n, COL_PROFIT)
        prev_B_tot = cr(prev_total,  COL_REVENUE)
        prev_E_tot = cr(prev_total,  COL_PROFIT)
        total_row[COL_REVDIF - 1] = (
            f'=SI({prev_B_tot}=0,"N/A",({B_tot}-{prev_B_tot})/{prev_B_tot})'
        )
        total_row[COL_PROFITDIF - 1] = (
            f'=SI({prev_E_tot}=0,"N/A",({E_tot}-{prev_E_tot})/ABS({prev_E_tot}))'
        )
        Q_tot = cr(total_row_n, COL_PROFITDIF)
        total_row[COL_INDICATOR - 1] = (
            f'=SI(ESNOD({Q_tot}),"",SI({Q_tot}>0,"▲",SI({Q_tot}<0,"🔻","🔹")))'
        )

    all_rows = [header_row_1, header_row_2] + data_rows + [total_row]
    return all_rows, mb_row_nums, total_row_n


def apply_formatting(
    spreadsheet, ws, start_row: int,
    header_colors: tuple | None = None,
) -> None:
    """
    Aplica colores, negrita y formato numérico a la tabla recién creada.
    Usa el Sheets API a través de gspread.

    header_colors: (date_color, cols_color) leídos del Sheet.
                   Si es None se usan los colores constantes de arriba.
    """
    sheet_id = ws.id
    mb_count = len(MB_ORDER)

    row_date = start_row
    row_cols = start_row + 1
    row_mb_s = start_row + 2
    row_mb_e = start_row + 2 + mb_count - 1
    row_tot  = start_row + 2 + mb_count

    if header_colors and len(header_colors) == 3:
        h_date, h_cols, h_data = header_colors
    elif header_colors and len(header_colors) == 2:
        h_date, h_cols = header_colors
        h_data = C_DATA_ROW
    else:
        h_date, h_cols, h_data = C_HEADER_DATE, C_HEADER_COLS, C_DATA_ROW
    h_total = h_date   # la fila TOTAL usa el mismo color que la fila de fecha

    def fmt_req(r1, c1, r2, c2, bg=None, fg=None, bold=None, num_fmt=None):
        """Helper para crear repeatCell requests."""
        cell_fmt = {}
        fields   = []

        if bg is not None:
            cell_fmt["backgroundColor"] = bg
            fields.append("backgroundColor")

        text_fmt = {}
        if fg is not None:
            text_fmt["foregroundColor"] = fg
        if bold is not None:
            text_fmt["bold"] = bold
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

    currency_fmt = {"type": "NUMBER", "pattern": "$#,##0.00;[RED]-$#,##0.00"}
    pct_fmt      = {"type": "NUMBER", "pattern": "0.00%;[RED]-0.00%"}

    reqs = []

    # ── Colores de fila ────────────────────────────────────────────────────────
    # L–M (cols 12–13) nunca reciben color — son anotación manual.
    # Encabezados: A–K (1–11) y N–P (14–16) coloreados; L–M blanco explícito.
    reqs.append(fmt_req(row_date, 1,  row_date, 11,         h_date,  C_WHITE, True))
    reqs.append(fmt_req(row_date, 12, row_date, 13,         C_WHITE, None,    None))
    reqs.append(fmt_req(row_date, 14, row_date, TOTAL_COLS, h_date,  C_WHITE, True))
    reqs.append(fmt_req(row_cols, 1,  row_cols, 11,         h_cols,  C_WHITE, True))
    reqs.append(fmt_req(row_cols, 12, row_cols, 13,         C_WHITE, None,    None))
    reqs.append(fmt_req(row_cols, 14, row_cols, TOTAL_COLS, h_cols,  C_WHITE, True))
    # Royal Prime header → gold
    reqs.append(fmt_req(row_cols, COL_ROYALPRIME, row_cols, COL_ROYALPRIME,
                        h_cols, C_GOLD, True))

    # Filas de datos (MB): celeste en A–K y N–P; L–M blanco
    reqs.append(fmt_req(row_mb_s, 1,  row_mb_e, 11,         h_data, C_BLACK, False))
    reqs.append(fmt_req(row_mb_s, 12, row_mb_e, 13,         C_WHITE, None,   None))
    reqs.append(fmt_req(row_mb_s, 14, row_mb_e, TOTAL_COLS, h_data, C_BLACK, False))

    # Fila TOTAL: A–K y N–P coloreados; L–M blanco
    reqs.append(fmt_req(row_tot, 1,  row_tot, 11,         h_total, C_WHITE, True))
    reqs.append(fmt_req(row_tot, 12, row_tot, 13,         C_WHITE, None,    None))
    reqs.append(fmt_req(row_tot, 14, row_tot, TOTAL_COLS, h_total, C_WHITE, True))

    # ── Formato numérico: moneda en columnas de valores ────────────────────────
    currency_cols = [COL_REVENUE, COL_ADSPENT, COL_ADSPENT50, COL_PROFIT,
                     COL_SPENTPLUS, COL_SPENT, COL_ROYALPRIME,
                     COL_PROFITLOSS, COL_PAYMENT, COL_FUTUREDEBT]
    for col in currency_cols:
        reqs.append({
            "repeatCell": {
                "range": grid_range(sheet_id, row_mb_s, col, row_tot, col),
                "cell":  {"userEnteredFormat": {"numberFormat": currency_fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # ── Formato numérico: porcentaje en columnas Dif ──────────────────────────
    for col in [COL_REVDIF, COL_PROFITDIF]:
        reqs.append({
            "repeatCell": {
                "range": grid_range(sheet_id, row_mb_s, col, row_tot, col),
                "cell":  {"userEnteredFormat": {"numberFormat": pct_fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # ── Bordes ────────────────────────────────────────────────────────────────
    # Estructura: A–K = tabla principal  |  L–M = sin bordes  |  N–P = mini-tabla aparte
    border = {"style": "SOLID", "width": 1, "color": C_GRAY_BORDER}
    no_border = {"style": "NONE"}

    # 1. Tabla principal A–K (columnas 1–11)
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 1, row_tot, 11),
            "top":             border,
            "bottom":          border,
            "left":            border,
            "right":           border,
            "innerHorizontal": border,
            "innerVertical":   border,
        }
    })

    # 2. Limpiar bordes en L–M (columnas 12–13) — notas manuales, sin tabla
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 12, row_tot, 13),
            "top":             no_border,
            "bottom":          no_border,
            "left":            no_border,
            "right":           no_border,
            "innerHorizontal": no_border,
            "innerVertical":   no_border,
        }
    })

    # 3. Mini-tabla N–P (columnas 14–16) — Revenue Dif, Profit Dif, Indicator
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 14, row_tot, 16),
            "top":             border,
            "bottom":          border,
            "left":            border,
            "right":           border,
            "innerHorizontal": border,
            "innerVertical":   border,
        }
    })

    # ── Fusionar celda del rango de fechas (A:B fila del header) ──────────────
    reqs.append({
        "mergeCells": {
            "range":     grid_range(sheet_id, row_date, 1, row_date, 2),
            "mergeType": "MERGE_ALL",
        }
    })

    # ── Anchos de columna (píxeles) ───────────────────────────────────────────
    col_widths = {
        COL_NAME:       110,   # A — Name
        COL_REVENUE:     85,   # B — Revenue
        COL_ADSPENT:     90,   # C — Adspent
        COL_ADSPENT50:   90,   # D — Adspent 50%
        COL_PROFIT:      80,   # E — Profit
        COL_SPENTPLUS:   90,   # F — Spent/plus
        COL_SPENT:       75,   # G — Spent
        COL_ROYALPRIME:  90,   # H — Royal Prime
        COL_PROFITLOSS:  90,   # I — Profit/Loss
        COL_PAYMENT:     85,   # J — Payment
        COL_FUTUREDEBT:  90,   # K — Future debt
        COL_NOTA:        80,   # L — Nota
        COL_PAGADO:      90,   # M — pagado
        COL_REVDIF:      85,   # N — Revenue Dif
        COL_PROFITDIF:   80,   # O — Profit Dif
        COL_INDICATOR:   40,   # P — indicador
    }
    for col, px in col_widths.items():
        reqs.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId":    sheet_id,
                    "dimension":  "COLUMNS",
                    "startIndex": col - 1,
                    "endIndex":   col,
                },
                "properties": {"pixelSize": px},
                "fields":     "pixelSize",
            }
        })

    spreadsheet.batch_update({"requests": reqs})


# ═══════════════════════════════════════════════════════════════════════════════
# TABLA MENSUAL (hoja "2026")
# ═══════════════════════════════════════════════════════════════════════════════

def write_monthly_table(
    spreadsheet,
    month_label: str,
    monthly_mb_data: list[dict],
    year: int,
    month: int,
) -> None:
    """
    Escribe la tabla mensual en la hoja '2026'.
    Columnas: Name | Revenue | Adspent | Adspent 50% | Profit |
              Spent/plus | Spent | Royal Prime | Profit/Loss | Payment | Future debt |
              Revenue Dif | Profit Dif
    """
    MONTH_COLS = 13  # A → M

    try:
        ws_2026 = spreadsheet.worksheet("2026")
    except Exception:
        ws_2026 = spreadsheet.add_worksheet("2026", rows=500, cols=20)

    col_a   = ws_2026.col_values(1)
    last_row = 0
    prev_total_row = None
    for idx, val in enumerate(col_a):
        row_n = idx + 1
        cell  = str(val).strip()
        if cell:
            last_row = row_n
        if cell.upper() == "TOTAL":
            prev_total_row = row_n

    start_row = last_row + 3 if last_row > 0 else 1

    # ── Headers ───────────────────────────────────────────────────────────────
    h1 = [""] * MONTH_COLS
    h1[0] = month_label

    h2 = ["Name", "Revenue", "Adspent", "Adspent 50%", "Profit",
          "Spent/plus", "Spent", "Royal Prime", "Profit/Loss",
          "Payment", "Future debt", "Revenue Dif", "Profit Dif"]

    # ── Filas de MB ───────────────────────────────────────────────────────────
    data_rows = []
    for i, mb in enumerate(monthly_mb_data):
        row_n = start_row + 2 + i
        B = f"B{row_n}"
        C = f"C{row_n}"
        D = f"D{row_n}"
        E = f"E{row_n}"
        K = f"K{row_n}"
        L = f"L{row_n}"

        row = [""] * MONTH_COLS
        row[0]  = mb["name"]
        row[1]  = mb["payout"]
        row[2]  = -mb["spend"]
        row[3]  = f"={C}*50%"
        row[4]  = f"={B}+{D}"
        row[5]  = ""   # Spent/plus mensual — vacío
        row[6]  = mb.get("attendance_penalty") or ""
        row[7]  = mb.get("royal_prime") or ""
        row[8]  = f"={B}+F{row_n}+G{row_n}+H{row_n}+I{row_n}+J{row_n}+({C}*50%)"
        row[9]  = ""   # Payment
        row[10] = f"={K}-{L}"

        # Dif vs mes anterior
        if prev_total_row:
            # usamos offset: fila del MB en tabla anterior
            prev_row = prev_total_row - (len(monthly_mb_data) - i)
            prev_B = f"B{prev_row}"
            prev_E = f"E{prev_row}"
            row[11] = f"=SI({prev_B}=0,NOD(),({B}-{prev_B})/{prev_B})"
            row[12] = f'=SI({prev_E}=0,"N/A",({E}-{prev_E})/ABS({prev_E}))'

        data_rows.append(row)

    # ── TOTAL ──────────────────────────────────────────────────────────────────
    tot_row_n = start_row + 2 + len(monthly_mb_data)
    mb_s      = start_row + 2
    mb_e      = tot_row_n - 1

    def s(col_letter: str) -> str:
        return f"=SUMA({col_letter}{mb_s}:{col_letter}{mb_e})"

    total = [
        "TOTAL", s("B"), s("C"), s("D"), s("E"),
        s("F"), s("G"), s("H"), s("I"), s("J"), s("K"),
    ]
    # Revenue Dif / Profit Dif para TOTAL
    if prev_total_row:
        B_t  = f"B{tot_row_n}"
        E_t  = f"E{tot_row_n}"
        pB_t = f"B{prev_total_row}"
        pE_t = f"E{prev_total_row}"
        total.append(f'=SI({pB_t}=0,"N/A",({B_t}-{pB_t})/{pB_t})')
        total.append(f'=SI({pE_t}=0,"N/A",({E_t}-{pE_t})/ABS({pE_t}))')
    else:
        total.extend(["", ""])

    all_rows = [h1, h2] + data_rows + [total]

    # ── Escribir en Sheet ──────────────────────────────────────────────────────
    end_col = col_letter(MONTH_COLS)
    end_row = start_row + len(all_rows) - 1
    rng     = f"A{start_row}:{end_col}{end_row}"
    ws_2026.update(range_name=rng, values=all_rows, value_input_option="USER_ENTERED")

    # ── Formatear ──────────────────────────────────────────────────────────────
    sheet_id = ws_2026.id
    c_rows   = start_row + 1
    mb_s_r   = start_row + 2
    tot_r    = start_row + 2 + len(monthly_mb_data)

    fmt_reqs = [
        {   # date header
            "repeatCell": {
                "range": grid_range(sheet_id, start_row, 1, start_row, MONTH_COLS),
                "cell":  {"userEnteredFormat": {
                    "backgroundColor": C_HEADER_DATE,
                    "textFormat": {"bold": True, "foregroundColor": C_WHITE},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        {   # column headers
            "repeatCell": {
                "range": grid_range(sheet_id, c_rows, 1, c_rows, MONTH_COLS),
                "cell":  {"userEnteredFormat": {
                    "backgroundColor": C_HEADER_COLS,
                    "textFormat": {"bold": True, "foregroundColor": C_WHITE},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        {   # total row — mismo color que encabezado de fecha
            "repeatCell": {
                "range": grid_range(sheet_id, tot_r, 1, tot_r, MONTH_COLS),
                "cell":  {"userEnteredFormat": {
                    "backgroundColor": C_HEADER_DATE,
                    "textFormat": {"bold": True, "foregroundColor": C_WHITE},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
    ]
    # Filas de datos: fondo blanco, texto negro (sin color de fondo)
    fmt_reqs.append({
        "repeatCell": {
            "range": grid_range(sheet_id, mb_s_r, 1, tot_r - 1, MONTH_COLS),
            "cell":  {"userEnteredFormat": {
                "backgroundColor": C_WHITE,
                "textFormat": {"foregroundColor": C_BLACK},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })
    # número formato moneda
    currency_fmt = {"type": "NUMBER", "pattern": "#,##0.00;[RED]-#,##0.00"}
    for c_idx in range(2, 12):  # columns B–K (1-based 2–11)
        fmt_reqs.append({
            "repeatCell": {
                "range": grid_range(sheet_id, mb_s_r, c_idx, tot_r, c_idx),
                "cell":  {"userEnteredFormat": {"numberFormat": currency_fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    spreadsheet.batch_update({"requests": fmt_reqs})
    print(f"Tabla mensual escrita en '2026': {month_label}, fila {start_row}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # ── 1. Fechas ──────────────────────────────────────────────────────────────
    d_inicio, d_fin, start_utc, end_utc, since_str, until_str = parse_dates()
    last_week = is_last_week_of_month(d_fin)
    week_label = f"{fmt_header(d_inicio)} - {fmt_header(d_fin)}"
    print(f"Semana: {week_label}  |  Última del mes: {last_week}")

    # ── 2. Config ──────────────────────────────────────────────────────────────
    mb_config = load_mb_config()

    # ── 3. Datos semanales ─────────────────────────────────────────────────────
    print(f"Rango UTC → Start: {start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}  "
          f"End: {end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("Consultando Ringba (weekly)...")
    payouts_weekly = fetch_ringba_payouts(start_utc, end_utc)

    print("Consultando Meta (weekly)...")
    spends_weekly = fetch_meta_spends(since_str, until_str, mb_config)

    # ── 4. Royal Prime y penalizaciones (solo última semana) ───────────────────
    royal_prime_map   : dict[str, float] = {}
    attendance_map    : dict[str, float] = {}
    payouts_monthly   : dict[str, float] = {}
    spends_monthly    : dict[str, float] = {}

    spreadsheet = get_spreadsheet()

    if last_week:
        year  = d_fin.year
        month = d_fin.month
        m_start_utc, m_end_utc = month_utc_range(year, month)
        m_since = EST.localize(datetime(year, month, 1)).strftime("%Y-%m-%d")
        m_until = until_str

        print("Consultando Ringba (monthly para Royal Prime)...")
        payouts_monthly = fetch_ringba_payouts(m_start_utc, m_end_utc)

        print("Consultando Meta (monthly para tabla mensual)...")
        spends_monthly = fetch_meta_spends(m_since, m_until, mb_config)

        prime_table = read_royal_prime_table(spreadsheet)
        print(f"Tabla Royal Prime: {prime_table}")

        attendance_map = read_sistema_datos(spreadsheet)
        print(f"Penalizaciones asistencia: {attendance_map}")

        for mb in MB_ORDER:
            cfg = mb_config.get(mb["config_display"]) or {}
            pub_key = normalize_name(cfg.get("publisher_name") or "")
            monthly_payout = payouts_monthly.get(pub_key, 0.0)
            royal_prime_map[mb["sheet_name"]] = get_royal_prime(monthly_payout, prime_table)
            print(f"  Royal Prime {mb['sheet_name']}: ${royal_prime_map[mb['sheet_name']]:.0f} (payout mes: ${monthly_payout:.2f})")

    # ── 5. Construir filas de MB ───────────────────────────────────────────────
    print("\nResumen semanal por MB:")
    mb_rows_data = []
    for mb in MB_ORDER:
        cfg     = mb_config.get(mb["config_display"]) or {}
        pub_key = normalize_name(cfg.get("publisher_name") or "")
        ad_id   = cfg.get("facebook_ad_account_id") or ""

        payout = payouts_weekly.get(pub_key, 0.0)
        spend  = spends_weekly.get(ad_id, 0.0)

        print(f"  {mb['sheet_name']}: payout=${payout:.2f}  spend=${spend:.2f}")

        row = {
            "name":               mb["sheet_name"],
            "payout":             payout,
            "spend":              spend,
            "attendance_penalty": attendance_map.get(mb["sheet_name"], "") if last_week else "",
            "royal_prime":        royal_prime_map.get(mb["sheet_name"], "") if last_week else "",
        }
        mb_rows_data.append(row)

    # ── 6. Escribir en "Week 2026" ─────────────────────────────────────────────
    print("Conectando con Google Sheets...")
    ws = spreadsheet.worksheet("Week 2026")

    prev = find_previous_rows(ws)
    last_data = prev["_last_data_row"]
    start_row = last_data + 3 if last_data > 0 else 1
    print(f"Último row con datos: {last_data}  →  nueva tabla en fila {start_row}")

    # Leer colores de la tabla anterior para replicarlos exactamente
    print("Leyendo colores existentes del Sheet...")
    header_colors = read_existing_header_colors(spreadsheet, ws, prev.get("_last_name_row"))

    table_values, mb_row_nums, total_row_n = build_table_values(
        d_inicio, d_fin, mb_rows_data, prev, start_row
    )

    # Escribir valores
    n_rows  = len(table_values)
    end_col = col_letter(TOTAL_COLS)
    rng     = f"A{start_row}:{end_col}{start_row + n_rows - 1}"
    ws.update(range_name=rng, values=table_values, value_input_option="USER_ENTERED")
    print(f"Valores escritos en {rng}")

    # Formatear (con los colores leídos del Sheet)
    apply_formatting(spreadsheet, ws, start_row, header_colors)
    print("Formato aplicado")

    # ── 7. Tabla mensual en "2026" (solo última semana) ────────────────────────
    if last_week:
        year  = d_fin.year
        month = d_fin.month
        import locale
        month_names = {
            1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
            5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
            9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
        }
        month_label = f"{month_names[month].capitalize()} {year}"

        monthly_mb = []
        for mb in MB_ORDER:
            cfg     = mb_config.get(mb["config_display"]) or {}
            pub_key = normalize_name(cfg.get("publisher_name") or "")
            ad_id   = cfg.get("facebook_ad_account_id") or ""
            monthly_mb.append({
                "name":               mb["sheet_name"],
                "payout":             payouts_monthly.get(pub_key, 0.0),
                "spend":              spends_monthly.get(ad_id, 0.0),
                "attendance_penalty": attendance_map.get(mb["sheet_name"], ""),
                "royal_prime":        royal_prime_map.get(mb["sheet_name"], ""),
            })

        write_monthly_table(spreadsheet, month_label, monthly_mb, year, month)

    # ── 8. Confirmación Discord ────────────────────────────────────────────────
    suffix = "\n📅 Tabla mensual también actualizada en '2026'." if last_week else ""
    msg = (
        f"✅ **Contabilidad completada: {week_label}**\n"
        f"Revisa el Sheet para verificar los datos.{suffix}"
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
            discord_send(WEBHOOK_MOD, f"[CONTABILIDAD ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

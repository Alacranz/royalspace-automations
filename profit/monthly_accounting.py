#!/usr/bin/env python3
"""
Monthly Accounting — Ringba + Meta → Google Sheets ("2026")
Royalspace 2026

Variables de entorno requeridas:
  MONTH: MM/YYYY — mes a calcular (ej: 03/2026)

Más: RINGBA_API_TOKEN, RINGBA_ACCOUNT_ID, META_ACCESS_TOKEN,
     META_API_VERSION, DISCORD_WEBHOOK_MOD,
     GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID.

Lógica:
  1. Calcula rango completo del mes.
  2. Fetcha payout de Ringba y spend de Meta para todo el mes.
  3. Lee tabla Royal Prime y penalizaciones de asistencia.
  4. Escribe tabla mensual en hoja "2026" con formato igual al manual.
  5. Envía confirmación a Discord #mod.
"""
from __future__ import annotations

import json
import os
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
RINGBA_TOKEN  = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]
META_TOKEN    = os.environ["META_ACCESS_TOKEN"]
META_VERSION  = os.environ.get("META_API_VERSION") or "v25.0"
WEBHOOK_MOD   = os.environ["DISCORD_WEBHOOK_MOD"]
MONTH_STR     = os.environ["MONTH"]   # MM/YYYY

CONFIG_PATH = Path(__file__).parent / "config.json"
VET = pytz.timezone("America/Caracas")   # UTC-4, igual que Ringba UI
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

# ── Columnas (1-based) — igual que FEB manual ─────────────────────────────────
COL_NAME       = 1   # A
COL_REVENUE    = 2   # B — payout de Ringba
COL_ADSPENT    = 3   # C — gasto Meta (negativo)
COL_ADSPENT50  = 4   # D = C*50%
COL_PROFIT     = 5   # E = B+D
COL_SPENTPLUS  = 6   # F — vacío en mensual
COL_SPENT      = 7   # G — penalidad asistencia
COL_ROYALPRIME = 8   # H — Royal Prime
COL_PROFITLOSS = 9   # I = B+F+G+H+(C*50%)
COL_NOTA       = 10  # J — manual (sin bordes)
COL_REVDIF     = 11  # K — Revenue Dif vs mes anterior
COL_PROFITDIF  = 12  # L — Profit Dif vs mes anterior
TOTAL_COLS     = 12

# ── Colores ───────────────────────────────────────────────────────────────────
C_HEADER  = {"red": 0.122, "green": 0.286, "blue": 0.490}   # #1F497D azul oscuro
C_DATA    = {"red": 0.812, "green": 0.886, "blue": 0.953}   # #CFE2F3 celeste
C_WHITE   = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
C_BLACK   = {"red": 0.0,   "green": 0.0,   "blue": 0.0}
C_GOLD    = {"red": 1.0,   "green": 0.843, "blue": 0.0}     # #FFD700
C_GRAY    = {"red": 0.3,   "green": 0.3,   "blue": 0.3}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════════════════════

def parse_month() -> tuple[int, int]:
    """Parsea MONTH (MM/YYYY) → (month, year)."""
    parts = MONTH_STR.strip().split("/")
    return int(parts[0]), int(parts[1])


def month_utc_range(year: int, month: int):
    """UTC range del mes completo en VET (igual que Ringba UI)."""
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
    pub_map = get_publisher_summary(RINGBA_TOKEN, RINGBA_ACCOUNT, start_utc, end_utc)
    result  = {key: data["payout"] for key, data in pub_map.items()}
    print("  [Ringba] Payouts:")
    for k, v in sorted(result.items(), key=lambda x: -x[1]):
        if v > 0:
            print(f"    {k}: ${v:.2f}")
    return result


def fetch_meta_spends(year: int, month: int, mb_config: dict) -> dict[str, float]:
    days    = monthrange(year, month)[1]
    since   = f"{year}-{month:02d}-01"
    until   = f"{year}-{month:02d}-{days:02d}"
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


def read_royal_prime_table(spreadsheet) -> list[tuple[float, float]]:
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
        print(f"Advertencia Royal Prime table: {e}. Usando fallback.")
    return fallback


def get_royal_prime(monthly_payout: float, prime_table: list) -> float:
    prime = 0.0
    for threshold, amount in sorted(prime_table, key=lambda x: x[0], reverse=True):
        if monthly_payout >= threshold:
            prime = amount
            break
    return prime


def read_sistema_datos(spreadsheet) -> dict[str, float]:
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


def find_prev_month_mb_rows(ws) -> dict[str, int]:
    """
    Escanea columna A de la hoja '2026' y retorna el último row de cada MB.
    Usado para Revenue Dif / Profit Dif vs mes anterior.
    """
    mb_names = {mb["sheet_name"] for mb in MB_ORDER}
    col_a    = ws.col_values(1)
    result   = {}
    for idx, val in enumerate(col_a):
        cell = str(val).strip()
        if cell in mb_names:
            result[cell] = idx + 1   # 1-based
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DE TABLA
# ═══════════════════════════════════════════════════════════════════════════════

def build_table(
    month_label: str,
    mb_data: list[dict],
    start_row: int,
    prev_mb_rows: dict[str, int],
) -> list[list]:
    """
    Construye matriz de filas para la hoja '2026'.
    Estructura (12 cols):
      A  B        C        D           E       F          G      H           I            J     K           L
      Name Revenue Adspent Adspent50%  Profit  Spent/plus Spent  RoyalPrime  Profit/Loss  Nota  RevenueDif  ProfitDif
    """
    # ── Header fila 1: rango de mes + sub-etiquetas ───────────────────────────
    h1 = [""] * TOTAL_COLS
    h1[COL_NAME      - 1] = month_label
    h1[COL_ADSPENT   - 1] = "total"
    h1[COL_ADSPENT50 - 1] = "50%"
    h1[COL_ROYALPRIME - 1] = "Winner"

    # ── Header fila 2: nombres de columnas ────────────────────────────────────
    h2 = [""] * TOTAL_COLS
    h2[COL_NAME       - 1] = "Name"
    h2[COL_REVENUE    - 1] = "Revenue"
    h2[COL_ADSPENT    - 1] = "Adspent"
    h2[COL_ADSPENT50  - 1] = "Adspent 50%"
    h2[COL_PROFIT     - 1] = "Profit"
    h2[COL_SPENTPLUS  - 1] = "Spent/plus"
    h2[COL_SPENT      - 1] = "Spent"
    h2[COL_ROYALPRIME - 1] = "Royal Prime"
    h2[COL_PROFITLOSS - 1] = "Profit/Loss"
    h2[COL_NOTA       - 1] = "Nota"
    h2[COL_REVDIF     - 1] = "Revenue Dif"
    h2[COL_PROFITDIF  - 1] = "Profit Dif"

    # ── Filas de MB ───────────────────────────────────────────────────────────
    data_rows = []
    for i, mb in enumerate(mb_data):
        row_n = start_row + 2 + i   # +2: header1 + header2

        B = cr(row_n, COL_REVENUE)
        C = cr(row_n, COL_ADSPENT)
        D = cr(row_n, COL_ADSPENT50)
        F = cr(row_n, COL_SPENTPLUS)
        G = cr(row_n, COL_SPENT)
        H = cr(row_n, COL_ROYALPRIME)
        E = cr(row_n, COL_PROFIT)

        row = [""] * TOTAL_COLS
        row[COL_NAME       - 1] = mb["name"]
        row[COL_REVENUE    - 1] = mb["payout"]
        row[COL_ADSPENT    - 1] = -mb["spend"]        # negativo (convención contable)
        row[COL_ADSPENT50  - 1] = f"={C}*50%"
        row[COL_PROFIT     - 1] = f"={B}+{D}"
        row[COL_SPENTPLUS  - 1] = ""                  # vacío en mensual
        row[COL_SPENT      - 1] = mb.get("attendance_penalty") or ""
        row[COL_ROYALPRIME - 1] = mb.get("royal_prime") or ""
        row[COL_PROFITLOSS - 1] = f"={B}+{F}+{G}+{H}+({C}*50%)"
        row[COL_NOTA       - 1] = ""                  # manual

        # Revenue Dif / Profit Dif vs mes anterior
        prev_row = prev_mb_rows.get(mb["name"])
        if prev_row:
            prev_B = cr(prev_row, COL_REVENUE)
            prev_E = cr(prev_row, COL_PROFIT)
            row[COL_REVDIF    - 1] = f"=SI({prev_B}=0,NOD(),({B}-{prev_B})/{prev_B})"
            row[COL_PROFITDIF - 1] = f'=SI({prev_E}=0,"N/A",({E}-{prev_E})/ABS({prev_E}))'

        data_rows.append(row)

    # ── Fila TOTAL ────────────────────────────────────────────────────────────
    tot_row_n = start_row + 2 + len(mb_data)
    mb_s      = start_row + 2
    mb_e      = tot_row_n - 1

    def s(col: int) -> str:
        return f"=SUMA({cr(mb_s, col)}:{cr(mb_e, col)})"

    total = [""] * TOTAL_COLS
    total[COL_NAME       - 1] = "TOTAL"
    total[COL_REVENUE    - 1] = s(COL_REVENUE)
    total[COL_ADSPENT    - 1] = s(COL_ADSPENT)
    total[COL_ADSPENT50  - 1] = s(COL_ADSPENT50)
    total[COL_PROFIT     - 1] = s(COL_PROFIT)
    total[COL_SPENTPLUS  - 1] = s(COL_SPENTPLUS)
    total[COL_SPENT      - 1] = s(COL_SPENT)
    total[COL_ROYALPRIME - 1] = s(COL_ROYALPRIME)
    total[COL_PROFITLOSS - 1] = s(COL_PROFITLOSS)

    return [h1, h2] + data_rows + [total]


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATO
# ═══════════════════════════════════════════════════════════════════════════════

def apply_formatting(spreadsheet, ws, start_row: int, mb_count: int) -> None:
    sheet_id  = ws.id
    row_date  = start_row
    row_cols  = start_row + 1
    row_mb_s  = start_row + 2
    row_mb_e  = start_row + 2 + mb_count - 1
    row_tot   = start_row + 2 + mb_count

    currency_fmt = {"type": "NUMBER", "pattern": "$#,##0.00;[RED]-$#,##0.00"}
    pct_fmt      = {"type": "NUMBER", "pattern": "0.00%;[RED]-0.00%"}
    border       = {"style": "SOLID", "width": 1, "color": C_GRAY}
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

    # ── Colores de filas ──────────────────────────────────────────────────────
    # Header fecha: A–I y K–L coloreados; J (Nota) blanco
    reqs.append(fmt_req(row_date, 1,  row_date, 9,           C_HEADER, C_WHITE, True))
    reqs.append(fmt_req(row_date, 10, row_date, 10,          C_WHITE,  None,    None))
    reqs.append(fmt_req(row_date, 11, row_date, TOTAL_COLS,  C_HEADER, C_WHITE, True))

    # Header columnas: A–I y K–L coloreados; J blanco
    reqs.append(fmt_req(row_cols, 1,  row_cols, 9,           C_HEADER, C_WHITE, True))
    reqs.append(fmt_req(row_cols, 10, row_cols, 10,          C_WHITE,  None,    None))
    reqs.append(fmt_req(row_cols, 11, row_cols, TOTAL_COLS,  C_HEADER, C_WHITE, True))

    # Royal Prime header → dorado
    reqs.append(fmt_req(row_cols, COL_ROYALPRIME, row_cols, COL_ROYALPRIME,
                        C_HEADER, C_GOLD, True))

    # Filas MB: celeste A–I y K–L; J blanco
    reqs.append(fmt_req(row_mb_s, 1,  row_mb_e, 9,           C_DATA,  C_BLACK, False))
    reqs.append(fmt_req(row_mb_s, 10, row_mb_e, 10,          C_WHITE, None,    None))
    reqs.append(fmt_req(row_mb_s, 11, row_mb_e, TOTAL_COLS,  C_DATA,  C_BLACK, False))

    # Fila TOTAL: A–I y K–L coloreados; J blanco
    reqs.append(fmt_req(row_tot, 1,  row_tot, 9,             C_HEADER, C_WHITE, True))
    reqs.append(fmt_req(row_tot, 10, row_tot, 10,            C_WHITE,  None,    None))
    reqs.append(fmt_req(row_tot, 11, row_tot, TOTAL_COLS,    C_HEADER, C_WHITE, True))

    # ── Formato numérico ──────────────────────────────────────────────────────
    currency_cols = [COL_REVENUE, COL_ADSPENT, COL_ADSPENT50, COL_PROFIT,
                     COL_SPENTPLUS, COL_SPENT, COL_ROYALPRIME, COL_PROFITLOSS]
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
    # Tabla principal A–I
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 1, row_tot, 9),
            "top":             border, "bottom": border,
            "left":            border, "right":  border,
            "innerHorizontal": border, "innerVertical": border,
        }
    })
    # J (Nota) — sin bordes
    reqs.append({
        "updateBorders": {
            "range": grid_range(sheet_id, row_date, 10, row_tot, 10),
            "top": no_border, "bottom": no_border,
            "left": no_border, "right": no_border,
        }
    })
    # K–L (Revenue Dif, Profit Dif)
    reqs.append({
        "updateBorders": {
            "range":           grid_range(sheet_id, row_date, 11, row_tot, TOTAL_COLS),
            "top":             border, "bottom": border,
            "left":            border, "right":  border,
            "innerHorizontal": border, "innerVertical": border,
        }
    })

    spreadsheet.batch_update({"requests": reqs})


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

    # ── 4. Royal Prime y asistencia ───────────────────────────────────────────
    print("Conectando con Google Sheets...")
    spreadsheet = get_spreadsheet()

    prime_table    = read_royal_prime_table(spreadsheet)
    attendance_map = read_sistema_datos(spreadsheet)
    print(f"Royal Prime table: {prime_table}")
    print(f"Penalizaciones: {attendance_map}")

    # ── 5. Construir datos por MB ─────────────────────────────────────────────
    mb_data = []
    for mb in MB_ORDER:
        cfg     = mb_config.get(mb["config_display"]) or {}
        pub_key = normalize_name(cfg.get("publisher_name") or "")
        ad_id   = cfg.get("facebook_ad_account_id") or ""
        payout  = payouts.get(pub_key, 0.0)
        spend   = spends.get(ad_id, 0.0)
        prime   = get_royal_prime(payout, prime_table)
        penalty = attendance_map.get(mb["sheet_name"], 0.0)

        print(f"  {mb['sheet_name']}: payout=${payout:.2f}  spend=${spend:.2f}  "
              f"prime=${prime:.0f}  penalty=${penalty:.2f}")

        mb_data.append({
            "name":               mb["sheet_name"],
            "payout":             payout,
            "spend":              spend,
            "royal_prime":        prime if prime > 0 else "",
            "attendance_penalty": penalty if penalty != 0 else "",
        })

    # ── 6. Leer hoja "2026" ───────────────────────────────────────────────────
    try:
        ws_2026 = spreadsheet.worksheet("2026")
    except Exception:
        ws_2026 = spreadsheet.add_worksheet("2026", rows=500, cols=15)

    # Encontrar última fila con datos y rows de mes anterior por MB
    prev_mb_rows = find_prev_month_mb_rows(ws_2026)
    print(f"Rows mes anterior: {prev_mb_rows}")

    col_a    = ws_2026.col_values(1)
    last_row = max((i + 1 for i, v in enumerate(col_a) if str(v).strip()), default=0)
    start_row = last_row + 3 if last_row > 0 else 1
    print(f"Última fila con datos: {last_row}  →  nueva tabla en fila {start_row}")

    # ── 7. Escribir tabla ─────────────────────────────────────────────────────
    all_rows = build_table(month_label, mb_data, start_row, prev_mb_rows)
    end_row  = start_row + len(all_rows) - 1
    rng      = f"A{start_row}:{col_letter(TOTAL_COLS)}{end_row}"
    ws_2026.update(range_name=rng, values=all_rows, value_input_option="USER_ENTERED")
    print(f"Valores escritos en {rng}")

    # ── 8. Formato ────────────────────────────────────────────────────────────
    apply_formatting(spreadsheet, ws_2026, start_row, len(mb_data))
    print("Formato aplicado")

    # ── 9. Discord ────────────────────────────────────────────────────────────
    msg = (
        f"✅ **Contabilidad mensual completada: {month_names[month]} {year}**\n"
        f"Tabla escrita en la hoja '2026' — revisa Revenue Dif y Profit Dif."
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

"""
ZIP Spanish — completa la pestaña "Spanish" del sheet de ZIPs con Ciudad y Estado.

Edwar pega en la pestaña "Spanish" el listado de ZIPs con cobertura en español
— en cualquier columna, con o sin encabezado, uno por
celda o varios separados por comas/espacios. Este script:
  1. Lee todos los ZIPs de la pestaña (rellena el 0 inicial que Sheets quita: 1234 → 01234)
  2. Busca Ciudad/Estado en api.zippopotam.us (igual que zip_report.py); los que ya
     tienen ciudad en la hoja no se vuelven a buscar
  3. Reescribe la pestaña: Zipcode | City | State, ordenada por Estado y Ciudad, sin duplicados,
     con filtro, más un resumen a la derecha (State | ZIPs) de mayor a menor
     para que los media buyers vean rápido a qué estados apuntar.

Env: GOOGLE_SERVICE_ACCOUNT_JSON, ZIP_SPREADSHEET_ID, (opcional) ZIP_SPANISH_TAB="Spanish"
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter

import gspread
import requests
from google.oauth2.service_account import Credentials

SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID = os.environ["ZIP_SPREADSHEET_ID"]
TAB = os.environ.get("ZIP_SPANISH_TAB") or "Spanish"


def abrir():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        print(f"No existe la pestaña '{TAB}' en el sheet. Créala y pega los ZIPs en ella.")
        sys.exit(1)


def leer_zips(valores: list[list[str]]) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Devuelve (zips en orden sin duplicados, {zip: (city, state)} ya escritos en la hoja)."""
    procesada = bool(valores) and [c.strip().lower() for c in valores[0][:3]] == ["zipcode", "city", "state"]
    zips, conocidos = [], {}
    for fila in valores:
        # Si la hoja ya tiene el formato de este script, solo la columna A son ZIPs
        # (E-F es el resumen y no se lee); si es un listado recién pegado, se leen todas las celdas.
        celdas = fila[:1] if procesada else fila
        for celda in celdas:
            for t in re.findall(r"\d+", str(celda)):
                if 3 <= len(t) <= 5:
                    z = t.zfill(5)
                    if z not in zips:
                        zips.append(z)
        if procesada and len(fila) >= 3 and re.fullmatch(r"\d{3,5}", str(fila[0]).strip()) \
                and str(fila[1]).strip() and fila[1].strip() != "(no encontrado)":
            conocidos[str(fila[0]).strip().zfill(5)] = (fila[1].strip(), fila[2].strip())
    return zips, conocidos


def ubicar(z: str) -> tuple[str, str]:
    for intento in range(3):
        try:
            r = requests.get(f"https://api.zippopotam.us/us/{z}", timeout=8)
            if r.status_code == 200:
                p = (r.json().get("places") or [{}])[0]
                return p.get("place name", ""), p.get("state abbreviation", "")
            if r.status_code == 404:
                return "", ""
        except Exception:
            pass
        time.sleep(1 + intento)
    return "", ""


def main() -> None:
    hoja = abrir()
    valores = hoja.get_all_values()
    zips, conocidos = leer_zips(valores)
    # ZIPs que llegan por el formulario del workflow (Run workflow → zips): se suman a los de la hoja
    for t in re.findall(r"\d+", os.environ.get("ZIPS_NUEVOS") or ""):
        if 3 <= len(t) <= 5 and t.zfill(5) not in zips:
            zips.append(t.zfill(5))
    if not zips:
        print(f"La pestaña '{TAB}' no tiene ZIPs.")
        return
    filas, nuevos, sin_dato = [], 0, []
    for z in zips:
        if z in conocidos:
            city, state = conocidos[z]
        else:
            city, state = ubicar(z)
            nuevos += 1
            time.sleep(0.05)
        if not city:
            sin_dato.append(z)
        filas.append((z, city or "(no encontrado)", state or ""))
    filas.sort(key=lambda r: (r[2] == "", r[2], r[1], r[0]))
    por_estado = Counter(r[2] for r in filas if r[2])

    data = [["Zipcode", "City", "State"]] + [list(r) for r in filas]
    resumen = [["State", "ZIPs"]] + [[s, n] for s, n in por_estado.most_common()]
    n = len(data)
    hoja.clear()
    hoja.resize(rows=max(n, len(resumen)) + 5, cols=6)
    hoja.update(range_name="A1", values=data, value_input_option="RAW")
    hoja.update(range_name="E1", values=resumen, value_input_option="RAW")
    hoja.format(f"A2:A{n}", {"numberFormat": {"type": "TEXT"}})
    hoja.format("A1:C1", {"textFormat": {"bold": True}})
    hoja.format("E1:F1", {"textFormat": {"bold": True}})
    hoja.freeze(rows=1)
    hoja.set_basic_filter(f"A1:C{n}")
    print(f"Spanish: {len(filas)} ZIPs en {len(por_estado)} estados "
          f"({nuevos} buscados ahora, {len(sin_dato)} sin ciudad: {', '.join(sin_dato[:10])})")


if __name__ == "__main__":
    main()

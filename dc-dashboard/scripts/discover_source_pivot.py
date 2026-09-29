#!/usr/bin/env python3
"""
Verifica pivot="SourceName" contra la API real de CallGrid — correr esto
ANTES de confiar en app/callgrid/client.py::get_source_revenue() en producción.

Uso:
    CALLGRID_API_KEY=... CALLGRID_ORG_ID=... python scripts/discover_source_pivot.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.callgrid.client import _fetch_stats_buckets, get_source_revenue  # noqa: E402

API_KEY = os.environ["CALLGRID_API_KEY"]
ORG_ID = os.environ["CALLGRID_ORG_ID"]


def _check_ascii(name: str, value: str) -> None:
    """CALLGRID_API_KEY/ORG_ID viajan en headers HTTP (latin-1 estricto). Un
    error de codificación acá casi siempre significa que copiaste una versión
    truncada del valor (ej. con "…" en vez del texto completo), no un problema
    real de la API."""
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        bad_char = value[exc.start:exc.end]
        print(f"ERROR: {name} contiene un carácter inválido ({bad_char!r}) en la posición {exc.start}.")
        print(f"  Valor actual: {value!r}")
        print("  Esto casi siempre pasa por copiar una versión truncada/visual del valor")
        print("  (ej. con '…' en vez del texto completo). Vuelve a copiarlo usando el botón")
        print("  de 'copiar' de CallGrid, no seleccionando el texto mostrado en pantalla.")
        sys.exit(1)


def main() -> None:
    _check_ascii("CALLGRID_API_KEY", API_KEY)
    _check_ascii("CALLGRID_ORG_ID", ORG_ID)

    end = date.today()
    start = end - timedelta(days=7)

    print(f"Probando pivot='SourceName' del {start} al {end}...")
    try:
        buckets = _fetch_stats_buckets(API_KEY, ORG_ID, start, end, pivot="SourceName")
    except Exception as exc:
        print(f"FALLÓ pivot='SourceName': {exc}")
        print("Revisa manualmente el Network tab del dashboard de CallGrid al filtrar")
        print("por Source, y ajusta el valor de 'pivot' en app/callgrid/client.py.")
        return

    if not buckets:
        print("pivot='SourceName' respondió OK pero sin buckets — ¿hay datos en este rango?")
        return

    print(f"OK — {len(buckets)} buckets recibidos. Primeros 5:")
    for b in buckets[:5]:
        print(f"  {b}")

    print("\nBuscando específicamente la Source (44) DC1...")
    sources = get_source_revenue(API_KEY, ORG_ID, start, end)
    if "44" in sources:
        print(f"  Encontrada: {sources['44']}")
    else:
        print(f"  NO encontrada. Sub_ids vistos: {list(sources.keys())}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Script de diagnóstico — obtener lista completa de columnas disponibles en Ringba
y probar solicitar las columnas de zip code por su ID exacto.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

def main():
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    # PASO 1: Obtener todas las columnas disponibles
    print("=== COLUMNAS DISPONIBLES EN /calllogs/columns ===\n")
    resp = requests.get(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs/columns",
        headers=headers, timeout=30
    )
    resp.raise_for_status()
    columns_data = resp.json()
    columns = columns_data.get("columns", [])
    print(f"Total columnas: {len(columns)}\n")

    # Imprimir todas las columnas, destacando las que tienen zip/geo/gather/tag
    zip_col_ids = []
    print("TODAS LAS COLUMNAS (id → title | isTag):")
    for col in columns:
        col_id    = col.get("id", "")
        title     = col.get("title", "")
        is_tag    = col.get("isTag", False)
        tag_name  = col.get("tagName", "")
        highlight = ""
        if any(z in col_id.lower() or z in title.lower() or z in tag_name.lower()
               for z in ["zip", "geo", "gather", "city", "state"]):
            highlight = " <<<< ZIP/GEO/GATHER"
            zip_col_ids.append(col_id)
        print(f"  {col_id:<40} | {title:<30} | isTag={is_tag} | tagName={tag_name}{highlight}")

    # PASO 2: Si encontramos columnas de zip, solicitarlas explícitamente
    if zip_col_ids:
        print(f"\n=== COLUMNAS DE ZIP ENCONTRADAS: {zip_col_ids} ===")
        start_utc = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
        end_utc   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)

        body = {
            "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size":        10,
            "offset":      0,
            "columns":     zip_col_ids + ["inboundCallId", "inboundPhoneNumber", "hasConverted", "conversionAmount"],
        }
        resp2 = requests.post(
            f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
            headers=headers, json=body, timeout=60
        )
        resp2.raise_for_status()
        records = (resp2.json().get("report") or {}).get("records") or []
        print(f"Registros obtenidos: {len(records)}")
        if records:
            print("Primer registro:")
            print(json.dumps(records[0], indent=2))
    else:
        print("\nNo se encontraron columnas con zip/geo/gather en la lista.")
        print("Buscando columnas con isTag=True:")
        tag_cols = [c for c in columns if c.get("isTag")]
        for c in tag_cols:
            print(f"  {c.get('id')} | {c.get('title')} | tagName={c.get('tagName')}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Script de diagnóstico — inspecciona los campos disponibles en un call log de Ringba.
Corre una vez manualmente para descubrir los nombres exactos de campos de zip code.

Uso: python profit/inspect_ringba_fields.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

def main():
    # Últimas 24 horas
    end_utc   = datetime.now(timezone.utc)
    start_utc = end_utc - timedelta(days=3)

    url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs"
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    body = {
        "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        5,
        "offset":      0,
    }

    print("Consultando Ringba...")
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    records = (data.get("report") or {}).get("records") or []
    if not records:
        print("No se encontraron registros en los últimos 3 días.")
        return

    # Tomar el primer registro convertido si hay
    converted = [r for r in records if r.get("hasConverted") is True]
    record = converted[0] if converted else records[0]

    print(f"\nTotal registros obtenidos: {len(records)}")
    print(f"Registros convertidos: {len(converted)}")
    print(f"\n{'='*60}")
    print("TODOS LOS CAMPOS DEL PRIMER REGISTRO:")
    print('='*60)

    def print_fields(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in sorted(obj.items()):
                full_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)) and v:
                    print(f"  {full_key}:")
                    print_fields(v, full_key)
                else:
                    print(f"  {full_key}: {repr(v)}")
        elif isinstance(obj, list) and obj:
            print_fields(obj[0], f"{prefix}[0]")

    print_fields(record)

    # Buscar específicamente campos con "zip" en el nombre
    print(f"\n{'='*60}")
    print("CAMPOS QUE CONTIENEN 'zip' (case-insensitive):")
    print('='*60)

    def find_zip_fields(obj, prefix=""):
        results = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if "zip" in k.lower():
                    results.append((full_key, v))
                if isinstance(v, (dict, list)):
                    results.extend(find_zip_fields(v, full_key))
        elif isinstance(obj, list) and obj:
            results.extend(find_zip_fields(obj[0], f"{prefix}[0]"))
        return results

    zip_fields = find_zip_fields(record)
    if zip_fields:
        for k, v in zip_fields:
            print(f"  {k}: {repr(v)}")
    else:
        print("  Ningún campo con 'zip' encontrado en este registro.")

    # Buscar campos con "geo"
    print(f"\n{'='*60}")
    print("CAMPOS QUE CONTIENEN 'geo' (case-insensitive):")
    print('='*60)

    def find_geo_fields(obj, prefix=""):
        results = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if "geo" in k.lower():
                    results.append((full_key, v))
                if isinstance(v, (dict, list)):
                    results.extend(find_geo_fields(v, full_key))
        elif isinstance(obj, list) and obj:
            results.extend(find_geo_fields(obj[0], f"{prefix}[0]"))
        return results

    geo_fields = find_geo_fields(record)
    if geo_fields:
        for k, v in geo_fields:
            print(f"  {k}: {repr(v)}")
    else:
        print("  Ningún campo con 'geo' encontrado.")

    # Buscar campos con "gather"
    print(f"\n{'='*60}")
    print("CAMPOS QUE CONTIENEN 'gather' (case-insensitive):")
    print('='*60)

    def find_gather_fields(obj, prefix=""):
        results = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if "gather" in k.lower():
                    results.append((full_key, v))
                if isinstance(v, (dict, list)):
                    results.extend(find_gather_fields(v, full_key))
        elif isinstance(obj, list) and obj:
            results.extend(find_gather_fields(obj[0], f"{prefix}[0]"))
        return results

    gather_fields = find_gather_fields(record)
    if gather_fields:
        for k, v in gather_fields:
            print(f"  {k}: {repr(v)}")
    else:
        print("  Ningún campo con 'gather' encontrado.")

if __name__ == "__main__":
    main()

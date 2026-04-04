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
    # Abril 3 2026 (rango completo del día)
    start_utc = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
    end_utc   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)

    url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs"
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    body = {
        "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        1000,
        "offset":      0,
    }

    print("Consultando Ringba...")
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    records = (data.get("report") or {}).get("records") or []
    if not records:
        print("No se encontraron registros.")
        return

    # Buscar los caller IDs específicos
    target_ids = {"+15626443102", "+15595480476"}
    target_records = [r for r in records if r.get("callerId") in target_ids or r.get("inboundCallId") in target_ids or str(r.get("callerNumber","")) in target_ids]

    print(f"\nTotal registros: {len(records)}")
    print(f"Registros con caller IDs objetivo: {len(target_records)}")

    to_inspect = target_records if target_records else records[:2]

    for i, record in enumerate(to_inspect):
        caller = record.get("callerId") or record.get("callerNumber") or record.get("inboundCallId", "?")
        print(f"\n{'='*60}")
        print(f"REGISTRO {i+1} — Caller: {caller}")
        print('='*60)

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

        def find_fields(obj, keyword, prefix=""):
            results = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    full_key = f"{prefix}.{k}" if prefix else k
                    if keyword in k.lower():
                        results.append((full_key, v))
                    if isinstance(v, (dict, list)):
                        results.extend(find_fields(v, keyword, full_key))
            elif isinstance(obj, list) and obj:
                results.extend(find_fields(obj[0], keyword, f"{prefix}[0]"))
            return results

        for keyword in ["zip", "geo", "gather", "city", "state", "caller"]:
            fields = find_fields(record, keyword)
            if fields:
                print(f"\n  -- '{keyword}' fields --")
                for k, v in fields:
                    print(f"    {k}: {repr(v)}")

if __name__ == "__main__":
    main()

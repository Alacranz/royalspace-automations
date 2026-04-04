#!/usr/bin/env python3
"""
Script de diagnóstico — inspecciona campos de zip code en Ringba call logs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

TARGET_PHONES = {"+15626443102", "+15595480476", "15626443102", "15595480476"}


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


def print_all_fields(obj, prefix="", indent=2):
    pad = " " * indent
    if isinstance(obj, dict):
        for k, v in sorted(obj.items()):
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and v:
                print(f"{pad}{full_key}:")
                print_all_fields(v, full_key, indent + 2)
            elif isinstance(v, list) and v:
                print(f"{pad}{full_key}: [{len(v)} items]")
                if isinstance(v[0], dict):
                    print_all_fields(v[0], f"{full_key}[0]", indent + 2)
            else:
                print(f"{pad}{full_key}: {repr(v)}")


def matches_target(record):
    """Check all string fields for target phone numbers."""
    def search(obj):
        if isinstance(obj, str):
            clean = obj.replace("+", "").replace("-", "").replace(" ", "")
            return any(t.replace("+", "") in clean for t in TARGET_PHONES)
        if isinstance(obj, dict):
            return any(search(v) for v in obj.values())
        if isinstance(obj, list):
            return any(search(i) for i in obj)
        return False
    return search(record)


def main():
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

    print("Consultando Ringba (04/03/2026)...")
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    records = (data.get("report") or {}).get("records") or []
    print(f"Total registros: {len(records)}")

    # Search for target phone numbers in all fields
    target_records = [r for r in records if matches_target(r)]
    print(f"Registros con números objetivo: {len(target_records)}")

    # Print all fields of first record to see structure
    print(f"\n{'='*60}")
    print("ESTRUCTURA COMPLETA — PRIMER REGISTRO:")
    print('='*60)
    if records:
        print_all_fields(records[0])

    # Print target records if found
    if target_records:
        for i, record in enumerate(target_records):
            print(f"\n{'='*60}")
            print(f"REGISTRO OBJETIVO {i+1}:")
            print('='*60)
            print_all_fields(record)

            print(f"\n  -- Campos relevantes --")
            for kw in ["zip", "geo", "gather", "city", "state", "caller", "convert", "revenue"]:
                fields = find_fields(record, kw)
                if fields:
                    print(f"  [{kw}]")
                    for k, v in fields:
                        print(f"    {k}: {repr(v)}")
    else:
        print("\nNingún registro encontró los números objetivo.")
        print("Mostrando campos zip/geo/gather del primer registro convertido:")
        converted = [r for r in records if r.get("hasConverted") is True]
        sample = converted[0] if converted else (records[0] if records else None)
        if sample:
            for kw in ["zip", "geo", "gather", "city", "state"]:
                fields = find_fields(sample, kw)
                if fields:
                    print(f"\n  [{kw}]")
                    for k, v in fields:
                        print(f"    {k}: {repr(v)}")


if __name__ == "__main__":
    main()

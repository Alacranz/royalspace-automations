#!/usr/bin/env python3
"""
Script de diagnóstico — prueba columnas de zip code en Ringba call logs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

# Candidatos de nombres de columna para zip code en Ringba
ZIP_COLUMN_CANDIDATES = [
    "Gather:Zipcode",
    "gather:zipcode",
    "GatherZipcode",
    "gatherZipcode",
    "Geo:ZipCode",
    "geo:zipCode",
    "GeoZipCode",
    "geoZipCode",
    "zipCode",
    "ZipCode",
    "callerZip",
    "Address:Zip 5",
    "tag:Gather:Zipcode",
    "tag:Address:Zip 5",
]


def main():
    start_utc = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
    end_utc   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)

    url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs"
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    # Test 1: Request with explicit columns including zip candidates
    print("=== TEST 1: Request con columnas de zip explícitas ===")
    body = {
        "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        5,
        "offset":      0,
        "columns":     ZIP_COLUMN_CANDIDATES,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    records = (data.get("report") or {}).get("records") or []
    print(f"Registros: {len(records)}")
    if records:
        r = records[0]
        found = {k: v for k, v in r.items() if any(
            z.lower() in k.lower() for z in ["zip", "geo", "gather", "city", "state"]
        )}
        print(f"Campos zip/geo/gather encontrados: {found if found else 'NINGUNO'}")
        print(f"Todos los campos del registro: {list(r.keys())}")

    # Test 2: Check if API returns available columns list
    print("\n=== TEST 2: Estructura completa de la respuesta (no records) ===")
    body2 = {
        "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        1,
        "offset":      0,
    }
    resp2 = requests.post(url, headers=headers, json=body2, timeout=60)
    data2 = resp2.json()
    # Print top-level keys of response
    print(f"Top-level keys en response: {list(data2.keys())}")
    report = data2.get("report") or {}
    print(f"Keys en report: {list(report.keys())}")
    # Check if there are available columns listed
    if "columns" in report:
        print(f"Columnas disponibles: {report['columns']}")
    if "availableColumns" in report:
        print(f"Available columns: {report['availableColumns']}")

    # Test 3: Try different API endpoint for call details
    print("\n=== TEST 3: Endpoint alternativo /calldetails ===")
    # Use the inboundCallId from a converted call found earlier
    call_id = "RGB23C90E8115B985F7FEF1541F04C3E109A572B859V3VKG01"
    detail_url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs/{call_id}"
    resp3 = requests.get(detail_url, headers={"Authorization": f"Token {RINGBA_TOKEN}", "Accept": "application/json"}, timeout=30)
    print(f"Status: {resp3.status_code}")
    if resp3.status_code == 200:
        d3 = resp3.json()
        print(f"Keys: {list(d3.keys())}")
        # Search for zip fields
        def find_zip(obj, prefix=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    fk = f"{prefix}.{k}" if prefix else k
                    if any(z in k.lower() for z in ["zip", "geo", "gather", "city"]):
                        print(f"  FOUND: {fk} = {repr(v)}")
                    if isinstance(v, (dict, list)):
                        find_zip(v, fk)
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:3]):
                    find_zip(item, f"{prefix}[{i}]")
        find_zip(d3)
    else:
        print(f"Response: {resp3.text[:300]}")

    # Test 4: Try /calllogs with "tags" or "include" parameter
    print("\n=== TEST 4: Request con includeTags/includeFields ===")
    for extra_param in [
        {"includeTags": True},
        {"includeCustomFields": True},
        {"includeAll": True},
        {"fields": ["gatherZipcode", "geoZipCode", "callerZip"]},
    ]:
        body4 = {
            "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size":        1,
            "offset":      0,
            **extra_param,
        }
        resp4 = requests.post(url, headers=headers, json=body4, timeout=30)
        records4 = (resp4.json().get("report") or {}).get("records") or []
        if records4:
            r4 = records4[0]
            found4 = {k: v for k, v in r4.items() if any(
                z.lower() in k.lower() for z in ["zip", "geo", "gather"]
            )}
            if found4:
                print(f"  Param {extra_param}: ENCONTRADO → {found4}")
            else:
                print(f"  Param {extra_param}: campos nuevos = {set(r4.keys()) - set(records[0].keys() if records else set())}")


if __name__ == "__main__":
    main()

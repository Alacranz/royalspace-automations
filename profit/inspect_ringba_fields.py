#!/usr/bin/env python3
"""
Script de diagnóstico — confirmar que gather Y Geo ZipCode
se leen correctamente con valueColumns para caller IDs específicos.

Caller IDs de prueba:
  +15626443102  → debe tener tag:Geo:ZipCode
  +15595480476  → debe tener tag:gather:zipcode
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

BASE_HEADERS = {
    "Authorization": f"Token {RINGBA_TOKEN}",
    "Accept":        "application/json",
    "Content-Type":  "application/json",
}

# Rango amplio — últimos 7 días para asegurar que capturamos ambas llamadas
START_UTC = datetime(2026, 3, 28, 0, 0, 0, tzinfo=timezone.utc)
END_UTC   = datetime(2026, 4, 5, 0, 0, 0, tzinfo=timezone.utc)

TARGET_CALLERS = {
    "+15626443102": "esperamos Geo:ZipCode",
    "+15595480476": "esperamos gather:zipcode",
}

VALUE_COLUMNS = [
    "inboundCallId",
    "inboundPhoneNumber",
    "publisherName",
    "tag:gather:zipcode",
    "tag:Geo:ZipCode",
    "hasConverted",
    "callDt",
]


def post_calllogs(offset: int) -> dict:
    resp = requests.post(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
        headers=BASE_HEADERS,
        json={
            "reportStart":  START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd":    END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size":         1000,
            "offset":       offset,
            "valueColumns": [{"column": c} for c in VALUE_COLUMNS],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    print("=== Buscando por inboundPhoneNumber (caller ID) ===")
    print(f"Rango: {START_UTC.date()} → {END_UTC.date()}")
    print(f"Callers objetivo: {list(TARGET_CALLERS.keys())}\n")

    found: dict[str, list] = {}
    offset = 0
    total = 0

    while True:
        data    = post_calllogs(offset)
        records = (data.get("report") or {}).get("records") or []
        if not records:
            break

        for r in records:
            total += 1
            phone = r.get("inboundPhoneNumber", "")
            if phone in TARGET_CALLERS:
                found.setdefault(phone, []).append(r)

        offset += len(records)
        if len(records) < 1000:
            break

    print(f"Records totales revisados: {total}")
    print(f"Callers encontrados: {len(found)}/{len(TARGET_CALLERS)}\n")

    for phone, expected in TARGET_CALLERS.items():
        records_for_phone = found.get(phone, [])
        if not records_for_phone:
            print(f"[{phone}] NO ENCONTRADO en el rango — prueba ampliar fechas")
            continue

        print(f"[{phone}] — {expected} — {len(records_for_phone)} llamada(s)")
        for r in records_for_phone:
            gather_zip = r.get("tag:gather:zipcode", "AUSENTE")
            geo_zip    = r.get("tag:Geo:ZipCode", "AUSENTE")
            publisher  = r.get("publisherName", "?")
            call_id    = r.get("inboundCallId", "?")
            converted  = r.get("hasConverted", "false")
            print(f"  callId:             {call_id[:30]}...")
            print(f"  Publisher:          {publisher}")
            print(f"  tag:gather:zipcode → {gather_zip}")
            print(f"  tag:Geo:ZipCode    → {geo_zip}")
            print(f"  hasConverted       → {converted}")
            print(f"  Campos presentes:   {list(r.keys())}")
            print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Script de diagnóstico — confirmar que gather Y Geo ZipCode
se leen correctamente con valueColumns para call IDs específicos.

Call IDs de prueba:
  - RGBA8DBFCC333D6CC29776AF44CF9904C38AB7B1E75V34N901  (tiene gather:zipcode)
  - RGBE1552C3A609B3D38FC215118F3511D5190C775D5V37IB01  (¿tiene Geo:ZipCode?)
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

# Rango amplio para cubrir ambos call IDs (ambos son del 3 abr 2026)
START_UTC = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
END_UTC   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)

TARGET_CALL_IDS = {
    "RGBA8DBFCC333D6CC29776AF44CF9904C38AB7B1E75V34N901",  # gather zip conocido: 87110
    "RGBE1552C3A609B3D38FC215118F3511D5190C775D5V37IB01",  # esperamos Geo:ZipCode aquí
    "RGB7F340DBC11C7AC82141E6580BD35F3F024109299V3DNJ01",  # tercer ID del batch anterior
}


def post_calllogs(body: dict) -> dict:
    resp = requests.post(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
        headers=BASE_HEADERS, json=body, timeout=60
    )
    resp.raise_for_status()
    return resp.json()


def main():
    VALUE_COLUMNS = [
        "inboundCallId",
        "publisherName",
        "tag:gather:zipcode",
        "tag:Geo:ZipCode",
        "hasConverted",
        "conversionAmount",
    ]

    print("=== Buscando call IDs específicos con valueColumns ===")
    print(f"Columnas solicitadas: {VALUE_COLUMNS}\n")

    # Traer todos los records del día para encontrar los IDs específicos
    found = {}
    offset = 0
    while True:
        data = post_calllogs({
            "reportStart":  START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd":    END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size":         1000,
            "offset":       offset,
            "valueColumns": [{"column": c} for c in VALUE_COLUMNS],
        })
        records = (data.get("report") or {}).get("records") or []
        if not records:
            break

        for r in records:
            cid = r.get("inboundCallId", "")
            if cid in TARGET_CALL_IDS:
                found[cid] = r

        offset += len(records)
        if len(records) < 1000:
            break

    print(f"Records totales revisados: ~{offset}")
    print(f"Call IDs objetivo encontrados: {len(found)}/{len(TARGET_CALL_IDS)}\n")

    for cid in TARGET_CALL_IDS:
        r = found.get(cid)
        if not r:
            print(f"[{cid[:20]}...] NO ENCONTRADO en el rango")
            continue

        gather_zip = r.get("tag:gather:zipcode", "AUSENTE")
        geo_zip    = r.get("tag:Geo:ZipCode", "AUSENTE")
        publisher  = r.get("publisherName", "?")
        converted  = r.get("hasConverted", "?")

        print(f"[{cid[:20]}...]")
        print(f"  Publisher:        {publisher}")
        print(f"  tag:gather:zipcode → {gather_zip}")
        print(f"  tag:Geo:ZipCode    → {geo_zip}")
        print(f"  hasConverted       → {converted}")
        print(f"  Todos los campos:  {list(r.keys())}")
        print()

    # Estadísticas generales del día
    print("=== Estadísticas del día (gather vs geo) ===")
    offset = 0
    total = 0
    has_gather = 0
    has_geo    = 0
    has_either = 0
    has_both   = 0

    while True:
        data = post_calllogs({
            "reportStart":  START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportEnd":    END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size":         1000,
            "offset":       offset,
            "valueColumns": [{"column": c} for c in VALUE_COLUMNS],
        })
        records = (data.get("report") or {}).get("records") or []
        if not records:
            break

        for r in records:
            total += 1
            g = r.get("tag:gather:zipcode")
            geo = r.get("tag:Geo:ZipCode")
            g_ok   = bool(g and str(g).strip() not in ("", "None", "null", "0"))
            geo_ok = bool(geo and str(geo).strip() not in ("", "None", "null", "0"))
            if g_ok:
                has_gather += 1
            if geo_ok:
                has_geo += 1
            if g_ok or geo_ok:
                has_either += 1
            if g_ok and geo_ok:
                has_both += 1

        offset += len(records)
        if len(records) < 1000:
            break

    print(f"  Total llamadas: {total}")
    print(f"  Con gather:zipcode:  {has_gather} ({has_gather/total*100:.1f}%)")
    print(f"  Con Geo:ZipCode:     {has_geo} ({has_geo/total*100:.1f}%)")
    print(f"  Con al menos uno:    {has_either} ({has_either/total*100:.1f}%)")
    print(f"  Con ambos:           {has_both} ({has_both/total*100:.1f}%)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Script de diagnóstico — confirmar formato valueColumns para tags de zip.
Formato correcto según soporte Ringba:
  "valueColumns": [{"column": "tag:Geo:ZipCode"}, {"column": "tag:gather:zipcode"}]
"""
from __future__ import annotations

import json
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

START_UTC = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
END_UTC   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)


def post_calllogs(body: dict) -> dict:
    resp = requests.post(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
        headers=BASE_HEADERS, json=body, timeout=60
    )
    resp.raise_for_status()
    return resp.json()


def main():
    body = {
        "reportStart": START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        5,
        "offset":      0,
        "valueColumns": [
            {"column": "callDt"},
            {"column": "tag:gather:zipcode"},
            {"column": "tag:Geo:ZipCode"},
            {"column": "campaignName"},
            {"column": "publisherName"},
            {"column": "inboundPhoneNumber"},
            {"column": "hasConverted"},
            {"column": "payoutAmount"},
        ],
    }

    print("=== TEST valueColumns con tags ===")
    data = post_calllogs(body)
    report = data.get("report") or {}
    records = report.get("records") or []

    print(f"Total records: {report.get('totalCount', '?')}")
    print(f"Records en este batch: {len(records)}")

    if not records:
        print("Sin records — revisar rango de fechas")
        print(f"Raw response: {json.dumps(data)[:500]}")
        return

    print(f"\nCampos del primer record ({len(records[0])} total):")
    for k, v in sorted(records[0].items()):
        print(f"  {k}: {v}")

    print("\n=== Todos los records (zip fields) ===")
    for r in records:
        geo_zip    = r.get("tag:Geo:ZipCode") or r.get("geoZipCode") or r.get("Geo:ZipCode") or "?"
        gather_zip = r.get("tag:gather:zipcode") or r.get("gather:zipcode") or r.get("gatherZipcode") or "?"
        publisher  = r.get("publisherName", "?")
        converted  = r.get("hasConverted", "?")
        payout     = r.get("payoutAmount", "?")
        print(f"  {publisher} | geo_zip={geo_zip} | gather_zip={gather_zip} | converted={converted} | payout={payout}")

    # Buscar con qué key exacta viene el zip en el response
    print("\n=== Keys exactas con 'zip', 'geo', 'gather' en el primer record ===")
    for k, v in records[0].items():
        if any(z in k.lower() for z in ["zip", "geo", "gather", "city", "state"]):
            print(f"  KEY='{k}' → VALUE='{v}'")


if __name__ == "__main__":
    main()

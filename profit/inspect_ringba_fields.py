#!/usr/bin/env python3
"""
Script de diagnóstico — obtener zip codes via tags en Ringba.
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
BASE_BODY = {
    "reportStart": START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "reportEnd":   END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "size":        5,
    "offset":      0,
}


def post_calllogs(extra: dict) -> list[dict]:
    resp = requests.post(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
        headers=BASE_HEADERS, json={**BASE_BODY, **extra}, timeout=60
    )
    resp.raise_for_status()
    return (resp.json().get("report") or {}).get("records") or []


def find_zip(records: list[dict]) -> dict:
    """Find any zip-related fields in records."""
    found = {}
    for r in records:
        for k, v in r.items():
            if any(z in k.lower() for z in ["zip", "geo", "gather", "city", "state"]):
                found[k] = v
    return found


def main():
    # PASO 1: Ver todos los tags disponibles en la cuenta
    print("=== TODOS LOS TAGS DE LA CUENTA ===")
    resp = requests.get(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/tags",
        headers=BASE_HEADERS, timeout=30
    )
    tags = resp.json() if resp.status_code == 200 else []
    print(f"Total tags: {len(tags)}")
    for t in tags:
        tag_name = t.get("tagName", "")
        tag_type = t.get("tagType", "")
        tag_id   = t.get("id") or t.get("tagId") or ""
        if any(z in tag_name.lower() or z in tag_type.lower()
               for z in ["zip", "geo", "gather", "address", "city", "state"]):
            print(f"  *** {tag_type} | {tag_name} | id={tag_id} ***")
        else:
            print(f"  {tag_type} | {tag_name} | id={tag_id}")

    # PASO 2: Probar con column IDs usando el formato tag
    print("\n=== TEST: columns con formato 'tag:Geo:ZipCode' ===")
    tag_candidates = [
        "tag:Geo:ZipCode",
        "tag:gather:zipcode",
        "tag:Gather:Zipcode",
        "tag:Address:Zip 5",
        "Geo:ZipCode",
        "gather:zipcode",
        "Gather:Zipcode",
    ]
    for col_id in tag_candidates:
        try:
            records = post_calllogs({"columns": [col_id, "inboundCallId", "hasConverted", "conversionAmount"]})
            if records:
                zips = find_zip(records)
                new_keys = [k for k in records[0].keys() if k not in ["inboundCallId", "hasConverted", "conversionAmount"]]
                print(f"  '{col_id}' → nuevos campos: {new_keys} | zip fields: {zips or 'NINGUNO'}")
        except Exception as e:
            print(f"  '{col_id}' → ERROR: {e}")

    # PASO 3: Probar el body con "tagColumns"
    print("\n=== TEST: body con 'tagColumns' ===")
    for param in ["tagColumns", "tagFilters", "tagIds"]:
        try:
            records = post_calllogs({param: ["Geo:ZipCode", "gather:zipcode"]})
            if records:
                zips = find_zip(records)
                print(f"  '{param}' → zip fields: {zips or 'NINGUNO'}")
                if zips:
                    print(f"  ENCONTRADO con '{param}'!")
        except Exception as e:
            print(f"  '{param}' → ERROR: {str(e)[:100]}")

    # PASO 4: Ver el full JSON de un tag de la cuenta para entender su estructura
    print("\n=== FULL JSON DE LOS PRIMEROS 10 TAGS ===")
    for t in tags[:10]:
        print(f"  {json.dumps(t)}")


if __name__ == "__main__":
    main()

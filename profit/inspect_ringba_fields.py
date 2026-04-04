#!/usr/bin/env python3
"""
Script de diagnóstico — probar cómo obtener tags (Gather:Zipcode, Geo:Zip Code) de Ringba.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

# inboundCallId de una llamada que sabemos tiene Gather:Zipcode = 93703
KNOWN_CALL_ID_GATHER = "RGB23C90E8115B985F7FEF1541F04C3E109A572B859V3VKG01"

def post_calllogs(body):
    url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs"
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()

def get_record_fields(data):
    records = (data.get("report") or {}).get("records") or []
    if not records:
        return None, []
    return records[0], list(records[0].keys())

def main():
    start_utc = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
    end_utc   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)
    base = {
        "reportStart": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportEnd":   end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size":        5,
        "offset":      0,
    }

    # TEST 1: tags como lista de strings
    print("=== TEST 1: body con 'tags' como lista ===")
    try:
        data = post_calllogs({**base, "tags": ["Gather:Zipcode", "Geo:Zip Code", "Geo:ZipCode"]})
        r, keys = get_record_fields(data)
        print(f"Keys: {keys}")
        if r:
            zip_fields = {k: v for k, v in r.items() if any(z in k.lower() for z in ["zip", "geo", "gather", "tag"])}
            print(f"Zip/geo/gather/tag fields: {zip_fields or 'NINGUNO'}")
    except Exception as e:
        print(f"Error: {e}")

    # TEST 2: tagNames como lista
    print("\n=== TEST 2: body con 'tagNames' ===")
    try:
        data = post_calllogs({**base, "tagNames": ["Gather:Zipcode", "Geo:Zip Code"]})
        r, keys = get_record_fields(data)
        new_keys = [k for k in keys if "zip" in k.lower() or "geo" in k.lower() or "gather" in k.lower() or "tag" in k.lower()]
        print(f"Nuevos campos: {new_keys or 'NINGUNO'}")
    except Exception as e:
        print(f"Error: {e}")

    # TEST 3: includeTags con nombres específicos
    print("\n=== TEST 3: 'includeTags' con lista de nombres ===")
    try:
        data = post_calllogs({**base, "includeTags": ["Gather:Zipcode", "Geo:Zip Code"]})
        r, keys = get_record_fields(data)
        new_keys = [k for k in keys if "zip" in k.lower() or "gather" in k.lower()]
        print(f"Nuevos campos: {new_keys or 'NINGUNO'}")
    except Exception as e:
        print(f"Error: {e}")

    # TEST 4: Endpoint específico por call ID
    print("\n=== TEST 4: GET /calllogs/{callId} ===")
    for path in [
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs/{KNOWN_CALL_ID_GATHER}",
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calls/{KNOWN_CALL_ID_GATHER}",
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calls/{KNOWN_CALL_ID_GATHER}/tags",
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs/{KNOWN_CALL_ID_GATHER}/tags",
    ]:
        headers = {"Authorization": f"Token {RINGBA_TOKEN}", "Accept": "application/json"}
        resp = requests.get(path, headers=headers, timeout=30)
        print(f"  {path.split(RINGBA_ACCOUNT)[1]} → {resp.status_code}")
        if resp.status_code == 200:
            d = resp.json()
            print(f"  Response: {json.dumps(d)[:500]}")

    # TEST 5: POST con "reportColumns" incluyendo tag names
    print("\n=== TEST 5: 'reportColumns' con tag names ===")
    try:
        data = post_calllogs({**base, "reportColumns": [
            "inboundCallId", "publisherName", "hasConverted", "conversionAmount",
            "Gather:Zipcode", "Geo:Zip Code", "Geo:City", "Geo:State"
        ]})
        r, keys = get_record_fields(data)
        print(f"Keys: {keys}")
        zip_fields = {k: v for k, v in (r or {}).items() if any(z in k.lower() for z in ["zip", "geo", "gather"])}
        print(f"Zip fields: {zip_fields or 'NINGUNO'}")
    except Exception as e:
        print(f"Error: {e}")

    # TEST 6: Endpoint /tags del account
    print("\n=== TEST 6: GET /tags del account ===")
    for path in [
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/tags",
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs/columns",
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/reports/columns",
    ]:
        headers = {"Authorization": f"Token {RINGBA_TOKEN}", "Accept": "application/json"}
        resp = requests.get(path, headers=headers, timeout=30)
        print(f"  {path.split(RINGBA_ACCOUNT)[1]} → {resp.status_code}")
        if resp.status_code == 200:
            print(f"  Response: {json.dumps(resp.json())[:500]}")


if __name__ == "__main__":
    main()

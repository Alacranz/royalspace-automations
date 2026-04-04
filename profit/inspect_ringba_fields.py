#!/usr/bin/env python3
"""
Script de diagnóstico — imprime JSON crudo de los dos caller IDs objetivo.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

RINGBA_TOKEN   = os.environ["RINGBA_API_TOKEN"]
RINGBA_ACCOUNT = os.environ["RINGBA_ACCOUNT_ID"]

# Los dos inboundCallIds encontrados anteriormente
TARGET_CALL_IDS = {
    "RGB23C90E8115B985F7FEF1541F04C3E109A572B859V3VKG01",  # +15595480476 Gather:Zipcode
    "RGB333DDC65D53D706B35221764BF8381063F987012V3K3601",  # +15595480476
    "RGB700B8DDB98119AAA363832A2496EFA7074E30007V3WSC01",  # +15595480476
}

TARGET_PHONES = {"15626443102", "15595480476"}


def main():
    start_utc = datetime(2026, 4, 3, 0, 0, 0, tzinfo=timezone.utc)
    end_utc   = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)

    url = f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs"
    headers = {
        "Authorization": f"Token {RINGBA_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    # Request con size grande para obtener todos los registros del día
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
    print(f"Total registros: {len(records)}")

    # Buscar por inboundPhoneNumber o inboundCallId
    found = []
    for r in records:
        phone = str(r.get("inboundPhoneNumber") or "").replace("+", "")
        call_id = str(r.get("inboundCallId") or "")
        if phone in TARGET_PHONES or call_id in TARGET_CALL_IDS:
            found.append(r)

    print(f"Registros objetivo encontrados: {len(found)}")

    for i, r in enumerate(found):
        print(f"\n{'='*70}")
        print(f"REGISTRO {i+1} — Phone: {r.get('inboundPhoneNumber')} | Converted: {r.get('hasConverted')}")
        print(f"{'='*70}")
        # Print raw JSON completo
        print(json.dumps(r, indent=2))

    # También imprimir el primer registro convertido cualquiera para ver estructura
    converted = [r for r in records if r.get("hasConverted") is True and r not in found]
    if converted:
        print(f"\n{'='*70}")
        print("PRIMER CONVERTIDO ADICIONAL (estructura completa):")
        print(f"{'='*70}")
        print(json.dumps(converted[0], indent=2))


if __name__ == "__main__":
    main()

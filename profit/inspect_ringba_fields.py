#!/usr/bin/env python3
"""
Script de diagnóstico — encontrar cómo obtener Geo:ZipCode de Ringba.

Dos hipótesis a probar:
  A) El param 'columns' funciona pero los IDs correctos son los de /calllogs/columns
     (verificar si el param realmente filtra campos, y qué IDs usa la API para tags)
  B) Los tags están en el detalle de una llamada individual
     (GET /calllogs/{callId} o GET /calllogs/{callId}/tags)
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
    "size":        3,
    "offset":      0,
}


def get(path: str, **kwargs) -> dict:
    resp = requests.get(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/{path}",
        headers=BASE_HEADERS, timeout=30, **kwargs
    )
    print(f"  GET {path} → {resp.status_code}")
    return resp


def post_calllogs(extra: dict) -> list[dict]:
    resp = requests.post(
        f"https://api.ringba.com/v2/{RINGBA_ACCOUNT}/calllogs",
        headers=BASE_HEADERS, json={**BASE_BODY, **extra}, timeout=60
    )
    resp.raise_for_status()
    return (resp.json().get("report") or {}).get("records") or []


def main():
    # ── PASO 1: Verificar si 'columns' realmente filtra ───────────────────────
    # Si enviamos solo 'publisherName' y obtenemos solo 1 campo, el param funciona.
    # Si obtenemos 31 campos, el param es ignorado.
    print("=== PASO 1: ¿El param 'columns' funciona? ===")
    records = post_calllogs({"columns": ["publisherName"]})
    if records:
        keys = list(records[0].keys())
        print(f"  Campos devueltos con columns=['publisherName']: {len(keys)}")
        print(f"  Keys: {keys[:10]}...")
        if len(keys) <= 3:
            print("  → columns SÍ filtra (API respeta el param)")
        else:
            print("  → columns NO filtra (API ignora el param, devuelve default)")

    # ── PASO 2: Obtener un inboundCallId real ─────────────────────────────────
    print("\n=== PASO 2: Obtener inboundCallId ===")
    records_full = post_calllogs({})
    call_ids = []
    for r in records_full:
        cid = r.get("inboundCallId") or r.get("callId") or r.get("id")
        if cid:
            call_ids.append(str(cid))
    print(f"  Call IDs encontrados: {call_ids}")

    if not call_ids:
        print("  ERROR: no se encontraron call IDs — abortar")
        return

    call_id = call_ids[0]

    # ── PASO 3: Probar endpoints de detalle de llamada ────────────────────────
    print(f"\n=== PASO 3: Endpoints de detalle para callId={call_id} ===")

    # 3a: GET /calllogs/{callId}
    r1 = get(f"calllogs/{call_id}")
    if r1.status_code == 200:
        data = r1.json()
        print(f"  Respuesta: {json.dumps(data)[:500]}")
    else:
        print(f"  Respuesta: {r1.text[:200]}")

    # 3b: GET /calllogs/{callId}/tags
    r2 = get(f"calllogs/{call_id}/tags")
    if r2.status_code == 200:
        data = r2.json()
        print(f"  /tags respuesta: {json.dumps(data)[:500]}")
    else:
        print(f"  /tags respuesta: {r2.text[:200]}")

    # 3c: GET /calls/{callId}
    r3 = get(f"calls/{call_id}")
    if r3.status_code == 200:
        data = r3.json()
        # Buscar zip en todos los campos
        flat = json.dumps(data).lower()
        has_zip = any(z in flat for z in ["zipcode", "zip code", "zip_code", "zipcd"])
        print(f"  /calls/{call_id} → {len(str(data))} chars | has_zip={has_zip}")
        print(f"  Keys top-level: {list(data.keys()) if isinstance(data, dict) else 'list'}")
    else:
        print(f"  /calls/{call_id} respuesta: {r3.text[:200]}")

    # ── PASO 4: Probar /calllogs/columns con params ───────────────────────────
    print("\n=== PASO 4: /calllogs/columns con params ===")
    for params in [
        {},
        {"includeTags": "true"},
        {"type": "tag"},
        {"includeAll": "true"},
    ]:
        r = get("calllogs/columns", params=params)
        if r.status_code == 200:
            cols = r.json().get("columns") or r.json()
            if isinstance(cols, list):
                tag_cols = [c for c in cols if any(
                    z in json.dumps(c).lower()
                    for z in ["zip", "geo", "city", "gather"]
                )]
                print(f"  params={params} → {len(cols)} cols | zip/geo cols: {tag_cols or 'NINGUNO'}")
            else:
                print(f"  params={params} → {str(r.json())[:200]}")

    # ── PASO 5: Probar columnas conocidas del sistema de tags ─────────────────
    # La API de /calllogs/columns usa IDs como "campaignName", "publisherName".
    # Los tags del sistema usan tagType+tagName. ¿Cuál es su column ID?
    print("\n=== PASO 5: Probar column IDs de tags del sistema ===")
    # Formato posible: camelCase del tagType+tagName
    candidates = [
        "geoZipCode",      # Geo + ZipCode → camelCase
        "geoZip",
        "zipCode",
        "zipcode",
        "callerZip",
        "callerZipCode",
        "callerCity",
        "geoCity",
        "geoCountry",      # Geo + Country (existe en tags, si devuelve algo sabemos el formato)
        "geoSubDivision",  # Geo + SubDivision = state
        "technologyIPAddress",
        "inboundNumberAreaCode",  # InboundNumber + AreaCode (existe, fácil de verificar)
    ]
    for col in candidates:
        try:
            recs = post_calllogs({"columns": [col, "publisherName"]})
            if recs:
                keys = list(recs[0].keys())
                # Si hay exactamente 1-2 campos nuevos, el col funcionó
                interesting = len(keys) < 10
                if interesting or any(z in json.dumps(keys).lower() for z in ["zip", "geo", "city"]):
                    print(f"  '{col}' → {len(keys)} campos: {keys}")
                else:
                    print(f"  '{col}' → {len(keys)} campos (default, ignorado)")
        except Exception as e:
            print(f"  '{col}' → ERROR: {e}")

    # ── PASO 6: Imprimir un record completo sin filtros ───────────────────────
    print("\n=== PASO 6: Record completo (todos los campos) ===")
    if records_full:
        r = records_full[0]
        print(f"  Total campos: {len(r)}")
        for k, v in sorted(r.items()):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

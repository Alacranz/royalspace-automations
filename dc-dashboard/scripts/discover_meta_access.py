#!/usr/bin/env python3
"""
Verifica si un META_ACCESS_TOKEN existente puede leer una cuenta de Meta Ads
específica (ej. la de Dixon) — corre esto ANTES de asumir que el token que ya
usan para profit/ en royalspace-automations sirve para la cuenta nueva.

Uso:
    META_ACCESS_TOKEN=... python scripts/discover_meta_access.py 849812830292271
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.meta.client import get_ad_spend  # noqa: E402

TOKEN = os.environ["META_ACCESS_TOKEN"]
API_VERSION = os.environ.get("META_API_VERSION") or "v25.0"


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: META_ACCESS_TOKEN=... python scripts/discover_meta_access.py <ad_account_id>")
        sys.exit(1)

    ad_account_id = sys.argv[1].removeprefix("act_")
    yesterday = date.today() - timedelta(days=1)

    print(f"Probando acceso a la cuenta {ad_account_id} (gasto de {yesterday})...")
    try:
        spend_cents = get_ad_spend(TOKEN, API_VERSION, ad_account_id, yesterday)
    except Exception as exc:
        print(f"FALLÓ: {exc}")
        print("\nEsto normalmente significa que este token NO tiene acceso a esa cuenta")
        print("todavía — hace falta generar uno nuevo (o compartir la cuenta con el")
        print("Business Manager, no solo con tu perfil personal).")
        sys.exit(1)

    print(f"OK — el token SÍ puede leer esta cuenta. Gasto de ayer: ${spend_cents / 100:.2f}")
    print("Puedes usar este mismo META_ACCESS_TOKEN en Railway, no hace falta uno nuevo.")


if __name__ == "__main__":
    main()

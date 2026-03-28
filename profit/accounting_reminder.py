#!/usr/bin/env python3
"""
Accounting Reminder — Discord #mod
Royalspace 2026

Se ejecuta cada lunes a las 9 AM EST via GitHub Actions.
Calcula la semana anterior y envía un recordatorio en Discord.
NO ejecuta ningún cálculo ni toca el Sheet.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytz

sys.path.insert(0, os.path.dirname(__file__))
from common.discord_client import send as discord_send

EST         = pytz.timezone("America/New_York")
WEBHOOK_MOD = os.environ["DISCORD_WEBHOOK_MOD"]


def get_last_week_range() -> tuple[datetime, datetime]:
    """Retorna lunes y domingo de la semana pasada en EST."""
    now         = datetime.now(EST)
    last_monday = now - timedelta(days=now.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def fmt(d: datetime) -> str:
    return d.strftime("%d/%m/%y")


def main() -> None:
    start, end = get_last_week_range()
    label = f"{fmt(start)} - {fmt(end)}"

    msg = (
        f"⏰ **Contabilidad pendiente: {label}**\n"
        f"Recuerda calcular cuando Ringba esté actualizado.\n"
        f"Activa el workflow manualmente cuando estés listo."
    )

    discord_send(WEBHOOK_MOD, msg)
    print(f"Recordatorio enviado: {label}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            discord_send(WEBHOOK_MOD, f"[RECORDATORIO CONTABILIDAD ERROR] {exc}")
        except Exception:
            pass
        sys.exit(1)

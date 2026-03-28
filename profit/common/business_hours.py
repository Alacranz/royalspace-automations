"""
Verificación de horario laboral — Royalspace 2026

Timezone: America/New_York (EST/EDT, equivalente a "Eastern Standard Time" de Windows)
Horario:
  Lunes-Viernes : 08:00 – 20:00
  Sábado        : 08:00 – 14:00
  Domingo       : nunca

Replica Test-BusinessWindow del PS1.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytz

EST = pytz.timezone("America/New_York")


def is_business_hours(now: datetime | None = None) -> bool:
    """
    True si el momento dado (o ahora UTC) cae dentro del horario laboral en EST.
    Se puede omitir con la variable de entorno FORCE_RUN=true (para pruebas manuales).
    """
    import os
    if os.environ.get("FORCE_RUN", "").lower() == "true":
        return True

    if now is None:
        now = datetime.now(timezone.utc)

    now_est = now.astimezone(EST)
    day  = now_est.weekday()   # 0=Lunes … 6=Domingo
    hour = now_est.hour

    if day == 6:               # Domingo
        return False
    if day == 5:               # Sábado
        return 8 <= hour < 14
    return 8 <= hour < 20      # Lunes-Viernes

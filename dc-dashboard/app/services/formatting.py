from __future__ import annotations


def money(cents: int) -> str:
    """Formato monetario del proyecto: f"${v:.2f}" — dos decimales siempre."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"

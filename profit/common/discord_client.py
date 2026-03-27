"""
Discord webhook sender — Royalspace 2026
"""
import requests

MAX_LENGTH = 1900


def send(webhook: str, message: str) -> None:
    """Envía un mensaje a un webhook de Discord. Trunca si supera 1900 caracteres."""
    if not webhook or not webhook.strip():
        raise ValueError("Webhook de Discord vacío")
    if len(message) > MAX_LENGTH:
        message = message[:MAX_LENGTH] + "\n..."
    resp = requests.post(webhook, json={"content": message}, timeout=15)
    resp.raise_for_status()

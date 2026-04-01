#!/usr/bin/env python3
"""
ManyChat Webhook — Dentista Latino
Royalspace 2026

POST /chat
  Body (JSON from ManyChat External Request):
    subscriber_id   : string  — ManyChat subscriber ID (unique per user)
    last_input_text : string  — last message from user
    first_name      : string  — user's first name (optional)
    zip_code        : string  — zip code stored in ManyChat field (optional)

Response:
    { "response": "...", "action": "respond|pause|skip" }

Actions:
  respond — send response text to user
  pause   — user opted out, pause automations in ManyChat
  skip    — no response needed (empty/unrecognized)
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import anthropic
import requests as http_requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH           = os.environ.get("DB_PATH", "manychat/conversations.db")
MAX_HISTORY       = 10   # number of past messages to include in context
MAX_TOKENS        = 200  # Claude response limit

SYSTEM_PROMPT = """Eres un asistente virtual de Dentista Latino, un servicio que conecta a personas de habla hispana en Estados Unidos con dentistas de confianza en su área.

Tu objetivo es conseguir que la persona llame a la clínica dental para ser atendida. Eres amable, empático y conciso.

Instrucciones:
- Responde SIEMPRE en español, sin importar el idioma del usuario
- Si el usuario no ha dado su código postal (zip code), pídelo amablemente
- Sé breve: máximo 2-3 oraciones por respuesta
- NUNCA inventes precios, direcciones, nombres de clínicas ni horarios específicos
- Habla siempre en primera persona del plural como parte del equipo de Dentista Latino: usa "llámanos", "cuando nos llames", "nosotros te ayudamos", "cuéntanos", nunca en tercera persona como "llama a la clínica" o "ellos te dirán"
- Cuando pregunten por precios, seguros, planes de pago, tratamientos, ubicación o cualquier detalle específico: reconoce su pregunta brevemente y diles que para esa información lo mejor es que nos llamen porque podemos darle toda la información exacta y personalizada
- Siempre termina empujando a que llamen: usa frases como "lo mejor es que nos llames", "te recomiendo que nos llames", "cuando nos llames te explicamos todo"
- El número o la forma de contacto se la darán en el siguiente paso del proceso, tú solo debes motivarlos a llamar
- Si la persona ya fue atendida o no necesita ayuda, agradece y despídete
- No repitas siempre la misma frase para empujar a llamar, varía el lenguaje para que se sienta natural"""

OPT_OUT_PHRASES = [
    "ya me atendi", "ya me atendieron", "ya me atendio", "ya fui atendido",
    "ya encontre", "ya tengo dentista", "ya no necesito",
    "no me interesa", "no quiero", "stop", "parar", "detener",
    "baja", "borrar", "eliminar", "cancelar suscripcion",
    "no molestes", "dejame", "gracias ya", "ya no",
]

ZIP_REGEX = re.compile(r'\b\d{5}\b')

# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id TEXT    NOT NULL,
                role          TEXT    NOT NULL,
                content       TEXT    NOT NULL,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sub_created
            ON messages (subscriber_id, created_at)
        """)
        conn.commit()


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_history(subscriber_id: str) -> list[dict]:
    """Return last MAX_HISTORY messages for this subscriber."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE subscriber_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (subscriber_id, MAX_HISTORY),
        ).fetchall()
    # reverse to chronological order
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(subscriber_id: str, role: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (subscriber_id, role, content) VALUES (?, ?, ?)",
            (subscriber_id, role, content),
        )
        # prune old messages (keep last MAX_HISTORY * 2)
        conn.execute(
            """
            DELETE FROM messages WHERE subscriber_id = ? AND id NOT IN (
                SELECT id FROM messages WHERE subscriber_id = ?
                ORDER BY created_at DESC LIMIT ?
            )
            """,
            (subscriber_id, subscriber_id, MAX_HISTORY * 2),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Strip accents and lowercase for comparison."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def is_opt_out(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in OPT_OUT_PHRASES)


def extract_zip(text: str) -> str | None:
    match = ZIP_REGEX.search(text)
    return match.group(0) if match else None


def resolve_zip(zip_code: str) -> str:
    """Return 'City, State' for a US zip code, or the zip itself if lookup fails."""
    try:
        resp = http_requests.get(
            f"https://api.zippopotam.us/us/{zip_code}", timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            place = data["places"][0]
            return f"{place['place name']}, {place['state abbreviation']}"
    except Exception:
        pass
    return zip_code


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Dentista Latino Webhook")


@app.on_event("startup")
def startup() -> None:
    init_db()


class ChatRequest(BaseModel):
    subscriber_id:   str
    last_input_text: str
    first_name:      str = ""
    zip_code:        str = ""


@app.post("/chat")
async def chat(req: ChatRequest) -> JSONResponse:
    text = req.last_input_text.strip()

    if not text:
        return JSONResponse({"response": "", "action": "skip"})

    # ── Opt-out detection ──────────────────────────────────────────────────────
    if is_opt_out(text):
        farewell = "Entendido, ¡gracias por comunicarte con Dentista Latino! Te deseamos lo mejor."
        save_message(req.subscriber_id, "user", text)
        save_message(req.subscriber_id, "assistant", farewell)
        return JSONResponse({"response": farewell, "action": "pause"})

    # ── Build Claude context ───────────────────────────────────────────────────
    history = get_history(req.subscriber_id)

    # Inject zip_code context if available
    zip_info = req.zip_code or extract_zip(text) or ""
    context_note = ""
    if req.first_name:
        context_note += f"El nombre del usuario es {req.first_name}. "
    if zip_info:
        location = resolve_zip(zip_info)
        context_note += f"Su código postal es {zip_info} ({location}). Usa el nombre de la ciudad '{location}' al referirte al área, no el número del zip code. "

    system = SYSTEM_PROMPT
    if context_note:
        system += f"\n\nContexto del usuario: {context_note}"

    messages = history + [{"role": "user", "content": text}]

    # ── Call Claude ────────────────────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
        )
        reply = result.content[0].text.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude error: {type(e).__name__}")

    # ── Save to history ────────────────────────────────────────────────────────
    save_message(req.subscriber_id, "user", text)
    save_message(req.subscriber_id, "assistant", reply)

    return JSONResponse({"response": reply, "action": "respond"})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

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
  pause   — user opted out or is angry, pause automations in ManyChat
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
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH           = os.environ.get("DB_PATH", "manychat/conversations.db")
MAX_HISTORY       = 20   # mensajes del historial que lee Claude
MAX_TOKENS        = 220  # límite de respuesta de Claude

SYSTEM_PROMPT = """You are a virtual assistant for Dentista Latino, a service that connects Spanish-speaking people in the United States with trusted dentists in their area.

Your goal is to get the person to call so they can be attended by a dentist. You are warm, empathetic, and concise.

LANGUAGE RULE (most important):
- Always respond in the SAME language the user writes in.
- If they write in Spanish → respond in Spanish.
- If they write in English → respond in English.
- If they mix languages → follow the dominant language of their message.

Instructions:
- Always speak in first person plural as part of the Dentista Latino team: use "call us", "when you call us", "we can help you", "llámanos", "cuando nos llames", "nosotros te ayudamos". Never say "call the clinic" or "they will tell you".
- If the user has not given their zip code, ask for it warmly.
- Keep responses brief: maximum 2-3 sentences.
- NEVER invent prices, addresses, clinic names, or schedules.
- When asked about prices, insurance, payment plans, treatments, location, or any specific detail: briefly acknowledge their question and tell them the best thing is to call us because we can give them exact and personalized information.
- Always end by encouraging them to call: vary the phrasing naturally ("the best thing is to call us", "I recommend you give us a call", "when you call us we'll explain everything", "lo mejor es que nos llames", "te recomiendo que nos llames").
- The contact number will be provided in the next step of the process — your role is only to motivate them to call.
- READ THE CONVERSATION HISTORY carefully. If the user has previously mentioned a problem (no coverage in their area, called and no one answered, was told there are no clinics nearby), acknowledge that context with empathy before responding. Do not ignore what they have already shared.
- If the user mentions there are no clinics in their area or no coverage: empathize, tell them you are looking for nearby options, and ask them to stay available while the team finds a solution.
- If the person has already been attended or no longer needs help, thank them warmly and say goodbye.
- Do not repeat the exact same closing phrase every time — vary it so it feels natural."""

# ── Opt-out y detección de enojo ──────────────────────────────────────────────
# Normalizados (sin tildes, minúsculas) — se comparan contra el texto normalizado del usuario

OPT_OUT_PHRASES = [
    # Atendido / ya no necesita
    "ya me atendi", "ya me atendieron", "ya me atendio", "ya fui atendido",
    "ya encontre dentista", "ya tengo dentista", "ya no necesito",
    "ya fui al dentista", "ya me revisaron",
    # Rechazo explícito
    "no me interesa", "no quiero", "no necesito", "no gracias",
    "stop", "parar", "detener", "unsubscribe",
    "baja", "borrar", "eliminar", "cancelar suscripcion", "darme de baja",
    "no me escribas", "no me escriban", "no me mandes", "no me manden",
    "no me molestes", "no me molesten", "dejame en paz", "dejenme en paz",
    "no me contactes", "no me contacten", "bloqueame", "te voy a bloquear",
    # Enojo / acusaciones
    "esto es una estafa", "son unos estafadores", "es una estafa",
    "esto es fraude", "son unos fraudulentos", "es un fraude",
    "son unos mentirosos", "esto es mentira", "son unos ladrones",
    "voy a reportar", "los voy a reportar", "esto es spam", "es spam",
    "no me jodan", "no me jodas",
    # Groserías fuertes (normalizadas sin tilde)
    "mierda", "puta madre", "puta mierda", "que mierda",
    "hijueputa", "hijodeputa", "hijo de puta", "hp ",
    "malparido", "malparida",
    "verga", "a la verga", "vete a la verga",
    "chinga tu madre", "chingada madre", "chinga tu",
    "me cago en", "cagada",
    "coño", "cono ",
    "pendejo", "pendeja", "pendejos",
    "cabron", "cabrona", "cabrones",
    "imbecil", "estupido", "estupida", "idiota", "eres un idiota",
    "marica", "marico", "maricon",
    "gilipollas", "gilipolla",
    "que se jodan", "jodan",
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
    """Return last MAX_HISTORY messages for this subscriber (chronological)."""
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
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(subscriber_id: str, role: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (subscriber_id, role, content) VALUES (?, ?, ?)",
            (subscriber_id, role, content),
        )
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
    """Strip accents and lowercase."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def is_opt_out(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in OPT_OUT_PHRASES)


def extract_zip(text: str) -> str | None:
    match = ZIP_REGEX.search(text)
    return match.group(0) if match else None


def resolve_zip(zip_code: str) -> str:
    """Return 'City, ST' for a US zip code, or the zip itself if lookup fails."""
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

    # ── Opt-out / enojo ───────────────────────────────────────────────────────
    if is_opt_out(text):
        save_message(req.subscriber_id, "user", text)
        return JSONResponse({"response": "", "action": "pause"})

    # ── Contexto del usuario ──────────────────────────────────────────────────
    history = get_history(req.subscriber_id)

    zip_info = req.zip_code or extract_zip(text) or ""
    context_parts: list[str] = []

    if req.first_name:
        context_parts.append(f"The user's name is {req.first_name}.")
    if zip_info:
        location = resolve_zip(zip_info)
        context_parts.append(
            f"The user's zip code is {zip_info} ({location}). "
            f"When referring to their area, use the city name '{location}', not the zip code number."
        )
    if context_parts:
        system = SYSTEM_PROMPT + "\n\nUser context: " + " ".join(context_parts)
    else:
        system = SYSTEM_PROMPT

    messages = history + [{"role": "user", "content": text}]

    # ── Claude ────────────────────────────────────────────────────────────────
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

    # ── Guardar historial ─────────────────────────────────────────────────────
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

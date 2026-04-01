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
    { "response": "...", "action": "respond|pause|skip", "detected_zip": "12345" }

Actions:
  respond — send response text to user
  pause   — user opted out or is angry, pause automations in ManyChat
  skip    — no response needed
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import anthropic
import requests as http_requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
DB_PATH            = os.environ.get("DB_PATH", "manychat/conversations.db")
MAX_HISTORY        = 30   # mensajes que lee Claude
MAX_TOKENS         = 250
MSGS_BEFORE_ZIP_INSIST = 3  # mensajes sin zip antes de insistir más

SYSTEM_PROMPT = """You are a virtual assistant for Dentista Latino, a service that connects Spanish-speaking people in the United States with trusted dentists in their area.

Your goal is to get the person to call so they can be attended by a dentist. You are warm, empathetic, and concise.

LANGUAGE RULE (highest priority):
- Always respond in the SAME language the user writes in.
- Spanish message → respond in Spanish. English message → respond in English.
- Mixed languages → follow the dominant language.

TONE RULES:
- Always speak in first person plural as part of the Dentista Latino team: "llámanos", "cuando nos llames", "nosotros te ayudamos", "call us", "when you call us". NEVER say "llama a la clínica" or "ellos te dirán" or "call the clinic".
- Be brief: maximum 2-3 sentences per response.
- Vary your phrasing naturally — don't repeat the same closing line every time.
- NEVER invent prices, addresses, clinic names, or schedules.

BEHAVIOR RULES:
- READ THE FULL CONVERSATION HISTORY carefully before responding. If the user mentioned something before (a problem, a pain, that they called and no one answered, no coverage in their area), acknowledge it with empathy — never ignore prior context.
- If this is a returning user (has prior conversation history from a previous session), open by warmly acknowledging you've spoken before.
- If the user has not given their zip code, ask for it warmly. If they have sent 3 or more messages WITHOUT providing a zip code, be more direct: tell them you really need their zip code to find the closest dentist and help them — make it feel urgent and necessary.
- When asked about prices, insurance, payment plans, treatments, location, or any specific detail: briefly acknowledge and redirect them to call us because every office is different and we can give them exact, personalized information.
- Always end encouraging them to call us.
- The contact number will be provided in the next step — your role is only to motivate them to call.
- If the person has already been attended or no longer needs help, thank them warmly and say goodbye.

URGENCY SIGNALS — when detected, respond with more energy and urgency to call NOW:
- DENTAL EMERGENCY / PAIN: If the user mentions tooth pain, toothache, broken tooth, abscess, infection, swelling, bleeding, can't sleep from pain → respond with empathy and strong urgency: this needs attention NOW, call us immediately, don't wait.
- HIGH-VALUE TREATMENT: If the user mentions implants, veneers, full dentures, smile makeover, cosmetic dentistry, full mouth restoration → show extra enthusiasm, these treatments change lives, make them feel this is exactly what we specialize in.

NO COVERAGE: If the user mentions they were told there are no clinics in their area or no coverage: empathize, tell them you are actively looking for nearby options, ask them to stay available."""

# ── Keywords ──────────────────────────────────────────────────────────────────

PAIN_KEYWORDS = [
    "dolor", "me duele", "duele", "dolor de muela", "dolor de diente",
    "muela rota", "diente roto", "diente partido", "absceso", "abseso",
    "infeccion", "inflamado", "inflamacion", "hinchado", "hinchazon",
    "sangrado", "sangra", "no puedo dormir", "emergencia", "urgente", "urgencia",
    "pain", "toothache", "tooth pain", "hurts", "hurting", "broken tooth",
    "abscess", "infection", "swollen", "bleeding", "emergency", "urgent",
    "can't sleep", "cannot sleep",
]

HIGH_VALUE_KEYWORDS = [
    "implante", "implantes", "implant", "implants",
    "carilla", "carillas", "veneer", "veneers", "porcelain",
    "protesis", "dentadura", "denture", "dentures", "full mouth",
    "blanqueamiento", "whitening", "teeth whitening",
    "estetica dental", "cosmetic", "smile makeover", "smile design",
    "corona", "coronas", "crown", "crowns",
    "restauracion completa", "full restoration",
]

OPT_OUT_PHRASES = [
    # Atendido
    "ya me atendi", "ya me atendieron", "ya me atendio", "ya fui atendido",
    "ya encontre dentista", "ya tengo dentista", "ya no necesito",
    "ya fui al dentista", "ya me revisaron",
    # Rechazo
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
    # Groserías
    "mierda", "puta madre", "puta mierda", "que mierda",
    "hijueputa", "hijodeputa", "hijo de puta", "hp ",
    "malparido", "malparida",
    "verga", "a la verga", "vete a la verga",
    "chinga tu madre", "chingada madre", "chinga tu",
    "me cago en", "cagada",
    "coño", "cono ",
    "pendejo", "pendeja", "pendejos",
    "cabron", "cabrona", "cabrones",
    "imbecil", "estupido", "estupida", "idiota",
    "marica", "marico", "maricon",
    "gilipollas",
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
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content, created_at FROM messages
            WHERE subscriber_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (subscriber_id, MAX_HISTORY),
        ).fetchall()
    return [
        {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        for r in reversed(rows)
    ]


def count_user_messages(subscriber_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE subscriber_id = ? AND role = 'user'",
            (subscriber_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def is_returning_user(history: list[dict]) -> bool:
    """True if the oldest message in history is from a previous calendar day (UTC)."""
    if not history:
        return False
    today = datetime.now(timezone.utc).date().isoformat()[:10]
    oldest = history[0].get("created_at", "")[:10]
    return bool(oldest) and oldest < today


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
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def is_opt_out(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in OPT_OUT_PHRASES)


def has_keyword(text: str, keywords: list[str]) -> bool:
    normalized = normalize_text(text)
    return any(kw in normalized for kw in keywords)


def extract_zip(text: str) -> str | None:
    match = ZIP_REGEX.search(text)
    return match.group(0) if match else None


def resolve_zip(zip_code: str) -> str:
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
        return JSONResponse({"response": "", "action": "skip", "detected_zip": ""})

    # ── Opt-out / enojo ───────────────────────────────────────────────────────
    if is_opt_out(text):
        save_message(req.subscriber_id, "user", text)
        return JSONResponse({"response": "", "action": "pause", "detected_zip": ""})

    # ── Historial y contexto ──────────────────────────────────────────────────
    history_full = get_history(req.subscriber_id)
    history_for_claude = [{"role": h["role"], "content": h["content"]} for h in history_full]

    returning = is_returning_user(history_full)
    user_msg_count = count_user_messages(req.subscriber_id)

    # Zip: primero el campo de ManyChat, si no lo que escribe en el mensaje
    detected_zip = req.zip_code or extract_zip(text) or ""

    # ── Señales de urgencia ───────────────────────────────────────────────────
    is_pain      = has_keyword(text, PAIN_KEYWORDS)
    is_high_value = has_keyword(text, HIGH_VALUE_KEYWORDS)

    # ── Construir contexto para Claude ───────────────────────────────────────
    context_parts: list[str] = []

    if req.first_name:
        context_parts.append(f"The user's name is {req.first_name}.")

    if detected_zip:
        location = resolve_zip(detected_zip)
        context_parts.append(
            f"Zip code: {detected_zip} ({location}). "
            f"Refer to their area as '{location}', never as the zip number."
        )

    if returning:
        context_parts.append(
            "This is a RETURNING USER — they have spoken with us before on a previous day. "
            "Warmly acknowledge you remember them or that they've contacted us before."
        )

    if not detected_zip and user_msg_count >= MSGS_BEFORE_ZIP_INSIST:
        context_parts.append(
            f"IMPORTANT: The user has sent {user_msg_count} messages WITHOUT providing a zip code. "
            "Be more direct and insistent: you NEED their zip code to find the nearest dentist. "
            "Make it feel urgent — you can't help them find a dentist without it."
        )

    if is_pain:
        context_parts.append(
            "DENTAL EMERGENCY / PAIN DETECTED. Respond with empathy and STRONG urgency. "
            "This person needs attention NOW. Prioritize getting them to call us immediately — "
            "do not delay, dental pain/infections can worsen quickly."
        )

    if is_high_value:
        context_parts.append(
            "HIGH-VALUE TREATMENT MENTIONED (implants, veneers, dentures, cosmetic, etc.). "
            "Show extra enthusiasm and warmth. These treatments are life-changing. "
            "Create excitement and strong motivation to call us to discuss their specific case."
        )

    system = SYSTEM_PROMPT
    if context_parts:
        system += "\n\nCurrent user context:\n- " + "\n- ".join(context_parts)

    messages = history_for_claude + [{"role": "user", "content": text}]

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

    return JSONResponse({
        "response":     reply,
        "action":       "respond",
        "detected_zip": detected_zip,
    })


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

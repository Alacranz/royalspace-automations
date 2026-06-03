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

import hashlib
import os
import re
import sqlite3
import time as _time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

import pytz

import anthropic
import requests as http_requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
DB_PATH            = os.environ.get("DB_PATH", "manychat/conversations.db")
MAX_HISTORY        = 15   # mensajes que lee Claude (reducido de 30 → baja costo de historial)
MAX_TOKENS         = 250
MSGS_BEFORE_ZIP_INSIST = 3  # mensajes sin zip antes de insistir más

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

META_PIXEL_ID          = os.environ.get("META_PIXEL_ID", "")
META_CONVERSIONS_TOKEN = os.environ.get("META_CONVERSIONS_TOKEN", "")
RINGBA_WEBHOOK_SECRET  = os.environ.get("RINGBA_WEBHOOK_SECRET", "")

SYSTEM_PROMPT = """LANGUAGE RULE — ABSOLUTE PRIORITY (overrides everything else):
- Read the user's CURRENT message AND the full conversation history to determine their language.
- English message → respond 100% in English. Zero Spanish words.
- Spanish message → respond 100% in Spanish. Zero English words.
- Ambiguous message (zip code, city name, "Yes", "Ok", numbers) → look at the rest of the conversation. If prior messages were in English, respond in English. If in Spanish, respond in Spanish. If no prior context, respond in SPANISH (Dentista Latino primarily serves Spanish-speaking patients).
- This is non-negotiable. When truly uncertain with no context, default to SPANISH. Never mix languages in a single response.

You are part of a team that connects people in the United States with trusted dentists in their area.

Your goal is to get the person to call so they can be attended by a dentist. You are warm, empathetic, and concise.

IDENTITY RULE (absolute, never break):
- You are a person on the Dentista Latino team. Never say, imply, or hint that you are a bot, AI, virtual assistant, automated system, or anything non-human. If someone asks or accuses you of being a bot, do NOT confirm or deny — simply stay in character, be warm, and redirect to calling us.

GREETING RULE (critical):
- NEVER start your response with "Hola", "¡Hola!", "Hello", "Gracias por contactarnos", or any greeting unless the user's VERY FIRST message in the entire conversation is itself a greeting. If there is any prior history at all, never greet again — go straight to helping. Repeating greetings mid-conversation is robotic and confusing.
- NEVER introduce yourself with a name or title. Do not say "Soy la Doctora X", "Me llamo X", "I'm [name]", or use any personal name for yourself.
- If the user sends a short confirmatory message ("Ok", "Si", "Sí", "Gracias", "Entendido", "Perfecto") mid-conversation, DO NOT start fresh or re-greet. Simply acknowledge briefly and continue from the last topic. Example: "Perfecto, aquí estamos cuando lo necesites." or redirect to call if that was the last topic.

TONE RULES:
- Always speak in first person plural as part of the Dentista Latino team: "llámanos", "cuando nos llames", "nosotros te ayudamos", "call us", "when you call us". NEVER say "llama a la clínica" or "ellos te dirán" or "call the clinic".
- Be brief: maximum 2-3 sentences per response.
- Vary your phrasing naturally — never repeat the same sentence, closing line, or call-to-action twice in a row. Each response should feel fresh.
- NEVER invent prices, addresses, clinic names, or schedules.
- Sound like a real, warm human — conversational, natural, empathetic. Never stiff or robotic.
- NEVER use markdown formatting. No asterisks (**word**), no underscores (_word_), no symbols for formatting. Plain text only — Messenger does not render markdown and it will appear as literal characters, confusing the user.
- NEVER mention any clinic name, brand, or service name — not even your own. Never say "Dentista Latino", "Dental Care", or any other name. Instead use: "nuestro servicio", "nuestras clínicas", "nuestro equipo", "our service", "our clinics", "our team". If a user asks which company or page this is, deflect warmly and redirect to calling — do not name yourself or any competitor.

BEHAVIOR RULES:
- READ THE FULL CONVERSATION HISTORY carefully before responding. If the user mentioned something before (a problem, a pain, that they called and no one answered, no coverage in their area), acknowledge it with empathy — never ignore prior context.
- If this is a returning user (has prior conversation history from a previous session), open by warmly acknowledging you've spoken before.
- If the user has not given their zip code, ask for it warmly. If they have sent 3 or more messages WITHOUT providing a zip code, be more direct: tell them you really need their zip code to find the closest dentist and help them — make it feel urgent and necessary.
- When asked about prices, insurance, payment plans, treatments, location, or any specific detail: briefly acknowledge and redirect them to call us because every office is different and we can give them exact, personalized information.
- NEVER ask "¿Estás listo para llamar?" or "¿Te gustaría llamar?" or any variation. Never ask permission to call — just send them. If you have their location and/or treatment need, redirect to call immediately without asking if they're ready.
- ONE QUESTION RULE: Never ask more than one question at a time. If you need info, ask only the single most important thing. Never stack two questions in one message.
- AFTER REDIRECTING TO CALL: Once you have sent the user to call (told them to use the button, press 1, etc.), do NOT follow up with another question or message. The conversation goal is complete — they have what they need. Do not ask "¿Hay algo más?" or any follow-up after redirecting to call.
- Always end encouraging them to call us.
- The contact number will be provided in the next step — your role is only to motivate them to call.
- MANDATORY CALL INSTRUCTION — NO EXCEPTIONS: Every single message that ends with a call to action MUST include this exact phrase (or very close equivalent) before or after the number: "Si al llamar escuchas un mensaje en inglés, presiona 1 para continuar." In English: "If you hear a message in English when you call, press 1 to continue." You MUST include this EVERY TIME, without exception. Never skip it. If you forget this, callers hang up thinking they dialed the wrong number.
- If the person has already been attended or no longer needs help, thank them warmly and say goodbye.

IMAGES / ATTACHMENTS:
- If the user sent a photo or image, NEVER mention that you cannot see it or that you are limited. Simply respond naturally to what they likely need — if it's a dental context, assume they are showing a dental concern and respond with empathy and warmth. Example: "Gracias por compartir eso. Cuéntame un poco más — ¿qué molestia estás sintiendo?"

WHEN USER ASKS FOR A HUMAN / REAL PERSON:
- Do NOT say you are a bot or virtual assistant. Simply redirect warmly: the best way to speak with someone and get help is to call us. That's where the real help happens — a real conversation with someone who can solve their dental situation. Make calling feel like the natural next step.

PHONE NUMBERS — ABSOLUTE RULES (never break any of these):
- NEVER include any phone number in your response text. Not any number. The phone number is shown automatically via a button — you must NEVER repeat it, invent it, or mention it in your message.
- NEVER ask the user for their phone number under any circumstance. Not even if they say nobody answered, not even if they seem frustrated, not even if they ask you to call them. We do NOT make outbound calls — ever. Patients always call us.
- If the user gives you their phone number voluntarily, do NOT repeat it back, do NOT acknowledge it, do NOT say you saved it, do NOT promise to call. Ignore the number and redirect them to call us directly using the button.
- NEVER say "te llamaremos", "te llamamos", "alguien te llamará", "we will call you", "nuestro equipo te contactará", or any variation implying an outbound call. We only receive calls.

WHEN USER SAYS NOBODY ANSWERED / CAN'T REACH US:
- If the user says nobody answered, the line was busy, or they couldn't get through: empathize and encourage them to try again. Example: "Entiendo, a veces hay espera. Intenta de nuevo en unos minutos — cuando llames, si escuchas un mensaje en inglés presiona 1 para continuar en español y alguien te atenderá." NEVER offer to call them back or ask for their number.

URGENCY SIGNALS — when detected, respond with more energy and urgency to call NOW:
- DENTAL EMERGENCY / PAIN: If the user mentions tooth pain, toothache, broken tooth, abscess, infection, swelling, bleeding, can't sleep from pain → respond with empathy and strong urgency: this needs attention NOW, call us immediately, don't wait.
- HIGH-VALUE TREATMENT: If the user mentions implants, veneers, full dentures, smile makeover, cosmetic dentistry, full mouth restoration → show extra enthusiasm, these treatments change lives, make them feel this is exactly what we specialize in.

NO COVERAGE IN THEIR US AREA: If the user mentions they were told there are no clinics in their area or no coverage: empathize, tell them you are actively looking for nearby options, ask them to stay available.

GEOGRAPHIC COVERAGE — UNITED STATES ONLY (critical, never break):
- This service operates EXCLUSIVELY in the United States. We do NOT have coverage in Mexico, Latin America, Canada, or any other country.
- If the user mentions ANY city, state, or location that is NOT in the United States — including but not limited to: México, "estado de México", CDMX, Guadalajara, Monterrey, Tijuana, Neza, Puebla, Oaxaca, Jalisco, Veracruz, or any other non-US location — respond warmly but clearly that you cannot help them from outside the US.
- Example (ES): "Lamentablemente nuestro servicio solo está disponible en los Estados Unidos. Si alguna vez te encuentras en EE.UU., con mucho gusto te ayudamos a encontrar un dentista."
- Example (EN): "Unfortunately our service is only available in the United States. If you're ever in the US, we'd love to help you find a dentist."
- NEVER invent coverage in non-US locations. NEVER say "tenemos opciones en [non-US city/country]" or anything implying you can connect them with a dentist outside the US."""

# ── Detección de idioma ───────────────────────────────────────────────────────

# Palabras claramente inglesas (excluye ambiguas como ok, si, no, a, me)
_EN_MARKERS = {
    "i", "the", "is", "are", "do", "you", "yes", "need", "want", "my",
    "have", "get", "can", "and", "to", "for", "in", "of", "it", "that",
    "this", "we", "they", "he", "she", "what", "how", "when", "where",
    "dentist", "teeth", "tooth", "pain", "help", "hi", "hello",
    "please", "thank", "thanks", "looking", "appointment",
    "cleaning", "price", "cost", "insurance", "location", "address",
    "available", "today", "tomorrow", "schedule", "near", "coverage",
    "treatment", "crown", "implant", "whitening", "denture", "extraction",
    "emergency", "office", "clinic", "call", "with", "your", "our",
}
# Palabras claramente españolas
_ES_MARKERS = {
    "yo", "es", "el", "la", "los", "las", "un", "una", "soy", "estoy",
    "quiero", "necesito", "tengo", "hola", "gracias", "como", "para",
    "que", "que", "si", "mi", "tu", "su", "nos", "dentista",
    "diente", "muela", "dolor", "ayuda", "buenas", "buenos",
    "donde", "cuando", "cuanto", "tambien", "clinica",
    # Palabras de consulta dental comunes en español
    "precio", "precios", "costo", "costos",
    "limpieza", "limpiezas", "cita", "citas",
    "ubicacion", "ubicaciones", "direccion", "cerca",
    "hoy", "manana", "semana", "dia", "dias",
    "seguro", "cobertura", "pago", "pagos",
    "tratamiento", "extraccion", "corona", "implante",
    "blanqueamiento", "protesis", "consulta", "emergencia",
    "disponible", "disponibles", "atencion", "servicio",
    "tiene", "tenemos", "puedo", "podemos", "pueden",
    "rapido", "rapida", "facil", "bueno", "buena",
    "telefono", "ciudad", "area", "zona",
    "queria", "quería", "podria", "podría", "necesito",
    "dental", "dientes", "muelas", "boca",
}
# Palabras ambiguas que se usan igual en ambos idiomas (ignorar para detección)
_AMBIGUOUS = {"ok", "okay", "si", "no", "a", "me", "de"}


def detect_language(text: str, history: list | None = None) -> str:
    """
    Retorna 'en', 'es', o 'unknown'.
    Si el mensaje actual es ambiguo (corto o solo palabras neutras),
    analiza el historial de la conversación para determinar el idioma dominante.
    """
    words = set(re.sub(r"[^a-záéíóúüñ ]", " ", text.lower()).split()) - _AMBIGUOUS
    en_hits = len(words & _EN_MARKERS)
    es_hits = len(words & _ES_MARKERS)

    if en_hits > es_hits:
        return "en"
    if es_hits > en_hits:
        return "es"

    # Mensaje ambiguo → analizar historial reciente
    if history:
        hist_en, hist_es = 0, 0
        for msg in history[-8:]:
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            hw = set(re.sub(r"[^a-záéíóúüñ ]", " ", content.lower()).split()) - _AMBIGUOUS
            hist_en += len(hw & _EN_MARKERS)
            hist_es += len(hw & _ES_MARKERS)
        if hist_en > hist_es:
            return "en"
        if hist_es > hist_en:
            return "es"

    return "unknown"


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

ZIP_REGEX        = re.compile(r'\b\d{5}\b')
IMAGE_URL_REGEX  = re.compile(r'https?://\S+\.(jpg|jpeg|png|gif|webp|bmp|tiff|heic)(\?\S*)?', re.IGNORECASE)
ATTACHMENT_REGEX = re.compile(r'(messenger\.com|fbcdn|facebook\.com|cloudfront\.net|cdn\.)', re.IGNORECASE)

HUMAN_AGENT_PHRASES = [
    "comuniqueme con alguien real", "comuniqueme con una persona",
    "quiero hablar con alguien real", "quiero hablar con una persona real",
    "hablar con un humano", "hablar con una persona real",
    "pon me con alguien", "ponme con alguien",
    "con un agente", "con un representante", "con un asesor",
    "no eres real", "eres un bot", "eres una ia", "eres inteligencia artificial",
    "estas hablando en automatico", "respuesta automatica",
    "I want to speak to a real person", "talk to a human", "real person",
    "speak with someone", "talk to someone real", "connect me with an agent",
]

# ── Database ──────────────────────────────────────────────────────────────────

HAIKU_INPUT_COST  = 0.80 / 1_000_000   # $ per token
HAIKU_OUTPUT_COST = 4.00 / 1_000_000   # $ per token


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                input_tokens  INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd      REAL    NOT NULL,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Idioma persistido por suscriptor — evita cambios de idioma entre mensajes
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_language (
                subscriber_id TEXT PRIMARY KEY,
                language      TEXT NOT NULL,
                updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Registro de eventos Ringba → Meta Conversions API
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ringba_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                phone_last4     TEXT,
                status          TEXT,
                events_received INTEGER DEFAULT 0,
                meta_error      TEXT,
                payload_keys    TEXT
            )
        """)
        conn.commit()


def get_subscriber_language(subscriber_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT language FROM subscriber_language WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
    return row["language"] if row else None


def set_subscriber_language(subscriber_id: str, language: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO subscriber_language (subscriber_id, language, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(subscriber_id) DO UPDATE SET language=excluded.language, updated_at=CURRENT_TIMESTAMP""",
            (subscriber_id, language),
        )


def log_tokens(input_tokens: int, output_tokens: int) -> None:
    cost = input_tokens * HAIKU_INPUT_COST + output_tokens * HAIKU_OUTPUT_COST
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO token_log (input_tokens, output_tokens, cost_usd) VALUES (?,?,?)",
            (input_tokens, output_tokens, cost),
        )


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


def is_image_or_attachment(text: str) -> bool:
    """True if the message is just an image/attachment URL with no real text."""
    stripped = text.strip()
    if IMAGE_URL_REGEX.match(stripped):
        return True
    if ATTACHMENT_REGEX.search(stripped) and stripped.startswith("http"):
        return True
    # ManyChat a veces envía solo el tipo de adjunto
    if stripped.lower() in ("image", "photo", "sticker", "video", "audio", "file", "attachment"):
        return True
    # Si después de quitar la URL no queda texto real
    cleaned = IMAGE_URL_REGEX.sub("", stripped).strip()
    if not cleaned and "http" in stripped:
        return True
    return False


def is_requesting_human(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in HUMAN_AGENT_PHRASES)


def extract_zip(text: str) -> str | None:
    match = ZIP_REGEX.search(text)
    return match.group(0) if match else None


def _zip_in_history(history: list[dict]) -> str | None:
    """Scan user messages in history (most recent first) for a zip code."""
    for msg in reversed(history):
        if msg["role"] == "user":
            z = extract_zip(msg["content"])
            if z:
                return z
    return None


BUSINESS_TZ = pytz.timezone("America/New_York")
# L-V 9:00-19:00 EST, S 9:00-17:00 EST
BUSINESS_HOURS = {
    0: (9, 19),  # Monday
    1: (9, 19),  # Tuesday
    2: (9, 19),  # Wednesday
    3: (9, 19),  # Thursday
    4: (9, 19),  # Friday
    5: (9, 17),  # Saturday
    # Sunday: closed
}

DAY_NAMES_ES = {0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves", 4: "viernes", 5: "sábado"}
DAY_NAMES_EN = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday"}


def get_next_open_label(lang: str = "es") -> str:
    """Returns 'hoy a las 9:00 AM' / 'mañana…' / 'el lunes…' in the right language."""
    now = datetime.now(BUSINESS_TZ)
    # Same day: not open yet (before 9 AM)
    if BUSINESS_HOURS.get(now.weekday()) and now.hour < 9:
        return "hoy a las 9:00 AM" if lang != "en" else "today at 9:00 AM"
    # Next available day
    for delta in range(1, 8):
        candidate = now + timedelta(days=delta)
        if BUSINESS_HOURS.get(candidate.weekday()):
            if delta == 1:
                return "mañana a las 9:00 AM" if lang != "en" else "tomorrow at 9:00 AM"
            if lang == "en":
                return f"on {DAY_NAMES_EN[candidate.weekday()]} at 9:00 AM"
            return f"el {DAY_NAMES_ES[candidate.weekday()]} a las 9:00 AM"
    return "pronto" if lang != "en" else "soon"


def is_business_hours() -> bool:
    now = datetime.now(BUSINESS_TZ)
    hours = BUSINESS_HOURS.get(now.weekday())
    if not hours:
        return False
    return hours[0] <= now.hour < hours[1]


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
    return ""


def _clean_zip_field(raw: str) -> str:
    """Devuelve vacío si el campo es una variable ManyChat sin resolver ({{...}})."""
    if not raw:
        return ""
    stripped = raw.strip()
    if stripped.startswith("{{") or stripped.startswith("{cuf"):
        return ""
    return stripped


# ── FastAPI ───────────────────────────────────────────────────────────────────

try:
    import sentry_sdk
    _sentry_dsn = os.environ.get("SENTRY_DSN")
    if _sentry_dsn:
        sentry_sdk.init(dsn=_sentry_dsn, traces_sample_rate=1.0)
except ImportError:
    pass

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
    # Sanitizar historial: eliminar placeholders ManyChat sin resolver ({{cuf_XXXXX}})
    _MANYCHAT_VAR = re.compile(r'\{\{[^}]+\}\}')
    history_for_claude = [
        {"role": h["role"], "content": _MANYCHAT_VAR.sub("", h["content"]).strip()}
        for h in history_full
    ]

    returning = is_returning_user(history_full)
    user_msg_count = count_user_messages(req.subscriber_id)

    # Zip: mensaje actual → historial de conversación → campo ManyChat
    # Validar zip de ManyChat — puede llegar como placeholder sin resolver: {{cuf_14413596}}
    mc_zip = extract_zip(req.zip_code) if req.zip_code else ""
    detected_zip = extract_zip(text) or _zip_in_history(history_full) or mc_zip or ""

    # ── Detectar tipo de mensaje ──────────────────────────────────────────────
    image_sent      = is_image_or_attachment(text)
    wants_human     = is_requesting_human(text)
    is_pain         = has_keyword(text, PAIN_KEYWORDS)
    is_high_value   = has_keyword(text, HIGH_VALUE_KEYWORDS)

    # Si es solo imagen/adjunto sin texto, usar un placeholder para Claude
    text_for_claude = "[El usuario envió una imagen o archivo adjunto]" if image_sent else text

    # ── Idioma: detectar PRIMERO para usarlo en todo el contexto ─────────────
    detected_lang = detect_language(text, history_for_claude)
    stored_lang   = get_subscriber_language(req.subscriber_id)
    if detected_lang in ("en", "es"):
        set_subscriber_language(req.subscriber_id, detected_lang)
        final_lang = detected_lang
        _lang_source = "current"   # señal fuerte: el mensaje actual tiene marcadores claros
    elif stored_lang in ("en", "es"):
        final_lang = stored_lang
        _lang_source = "stored"    # señal débil: idioma de conversación previa
    else:
        final_lang = "en"
        _lang_source = "default"   # sin señal: Claude decide

    # Última respuesta del bot — para forzar variación explícita
    last_assistant_msg = next(
        (m["content"] for m in reversed(history_for_claude) if m["role"] == "assistant"),
        None,
    )

    # ── Construir contexto para Claude ───────────────────────────────────────
    context_parts: list[str] = []

    if req.first_name:
        context_parts.append(f"The user's name is {req.first_name}.")

    if detected_zip:
        location = resolve_zip(detected_zip)
        if location:
            context_parts.append(
                f"Zip code: {detected_zip} ({location}). "
                f"Refer to their area as '{location}', never as the zip number."
            )

    if returning:
        context_parts.append(
            "This is a RETURNING USER — they have spoken with us before on a previous day. "
            "Warmly acknowledge you remember them or that they've contacted us before."
        )

    if image_sent:
        context_parts.append(
            "The user sent an image or photo. Do NOT mention that you cannot see it. "
            "Respond naturally and warmly as if they are showing you a dental concern. "
            "Ask them what they are feeling or what brought them here today."
        )

    if wants_human:
        context_parts.append(
            "The user wants to speak with a real person. Do NOT say you are a bot or assistant. "
            "Simply respond warmly and redirect: the best way to get real help and speak with someone "
            "is to call us — that's where everything gets resolved. Make calling feel like the natural answer."
        )

    if not detected_zip and user_msg_count >= MSGS_BEFORE_ZIP_INSIST and not _zip_in_history(history_full):
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

    if last_assistant_msg:
        preview = last_assistant_msg[:180]
        context_parts.append(
            f"ANTI-REPEAT — YOUR LAST RESPONSE WAS: \"{preview}\". "
            f"You MUST NOT reuse the same opening words, same sentence structure, or same call-to-action. "
            f"Rephrase completely — different angle, different words, same goal."
        )

    if is_business_hours():
        context_parts.append(
            "AVAILABILITY: We are currently OPEN and available right now (hours: Mon-Fri 9AM-7PM EST, Sat 9AM-5PM EST). "
            "If the user asks whether you are available, open, or working today — answer YES directly and warmly, then invite them to call. "
            "Example (ES): 'Sí, estamos disponibles ahora mismo. Llámanos y te conectamos con un dentista en tu área.' "
            "Example (EN): 'Yes, we are available right now. Give us a call and we'll connect you with a dentist in your area.'"
        )

    if not is_business_hours():
        next_open = get_next_open_label(final_lang)
        if final_lang == "en":
            oot_msg = (
                f"There are no agents available right now. "
                f"Starting {next_open} we can help you without any problem. "
                f"Would you like us to notify you when we're available?"
            )
        else:
            oot_msg = (
                f"Ahora mismo no hay agentes disponibles. "
                f"A partir de {next_open} te podemos atender sin problema. "
                f"¿Quieres que te avise cuando estemos disponibles?"
            )
        context_parts.append(
            f"FUERA DE HORARIO — responde EXACTAMENTE con este mensaje, sin agregar nada más: \"{oot_msg}\""
        )

    if _lang_source == "current":
        # Señal fuerte: el mensaje actual tiene marcadores claros → mandato estricto
        if final_lang == "en":
            context_parts.insert(0, "LANGUAGE: The user is writing in English. Respond 100% in English. No Spanish words.")
        else:
            context_parts.insert(0, "LANGUAGE: El usuario está escribiendo en español. Responde 100% en español. Sin palabras en inglés.")
    elif _lang_source == "stored":
        # Señal débil: idioma de conversación previa → sugerencia, Claude puede corregir
        if final_lang == "en":
            context_parts.insert(0, (
                "LANGUAGE PREFERENCE: This user has previously communicated in English. "
                "Respond in English unless their current message clearly uses Spanish words."
            ))
        else:
            context_parts.insert(0, (
                "PREFERENCIA DE IDIOMA: Este usuario ha comunicado antes en español. "
                "Responde en español a menos que su mensaje actual use palabras claramente en inglés."
            ))
    else:
        # Sin señal — primer mensaje ambiguo (zip, ciudad, etc.)
        # Dentista Latino sirve principalmente pacientes hispanohablantes,
        # así que el default correcto es español, no inglés.
        context_parts.insert(0, (
            "LANGUAGE: No prior context. Dentista Latino primarily serves Spanish-speaking patients. "
            "If this message is ambiguous (zip code, city name, short reply, numbers), respond in SPANISH. "
            "Only respond in English if the message clearly contains English words."
        ))

    # System prompt: parte estática cacheada + contexto dinámico sin cachear
    system_blocks: list[dict] = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if context_parts:
        system_blocks.append({
            "type": "text",
            "text": "\n\nCurrent user context:\n- " + "\n- ".join(context_parts),
        })

    messages = history_for_claude + [{"role": "user", "content": text_for_claude}]

    # ── Claude ────────────────────────────────────────────────────────────────
    try:
        result = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            messages=messages,
        )
        reply = result.content[0].text.strip()
        log_tokens(result.usage.input_tokens, result.usage.output_tokens)
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


@app.get("/billing/ping")
async def billing_ping() -> dict:
    return {"status": "ok", "version": "aa78697", "billing_routes": True}


@app.get("/stats")
async def stats(date: str = "") -> JSONResponse:
    now   = datetime.now(timezone.utc)
    today = date if date else now.strftime("%Y-%m-%d")
    month = today[:7]  # YYYY-MM
    with get_conn() as conn:
        def q(sql, *args):
            return conn.execute(sql, args).fetchone()[0] or 0

        msgs_today   = q("SELECT COUNT(*) FROM messages WHERE role='user' AND created_at LIKE ?", f"{today}%")
        msgs_month   = q("SELECT COUNT(*) FROM messages WHERE role='user' AND created_at LIKE ?", f"{month}%")
        convs_today  = q("SELECT COUNT(DISTINCT subscriber_id) FROM messages WHERE role='user' AND created_at LIKE ?", f"{today}%")
        convs_month  = q("SELECT COUNT(DISTINCT subscriber_id) FROM messages WHERE role='user' AND created_at LIKE ?", f"{month}%")
        cost_today   = q("SELECT COALESCE(SUM(cost_usd),0) FROM token_log WHERE created_at LIKE ?", f"{today}%")
        cost_month   = q("SELECT COALESCE(SUM(cost_usd),0) FROM token_log WHERE created_at LIKE ?", f"{month}%")
        tok_in_month = q("SELECT COALESCE(SUM(input_tokens),0) FROM token_log WHERE created_at LIKE ?", f"{month}%")
        tok_out_month= q("SELECT COALESCE(SUM(output_tokens),0) FROM token_log WHERE created_at LIKE ?", f"{month}%")

    return JSONResponse({
        "date": today,
        "messages_today":       msgs_today,
        "messages_month":       msgs_month,
        "conversations_today":  convs_today,
        "conversations_month":  convs_month,
        "cost_today_usd":       round(cost_today, 4),
        "cost_month_usd":       round(cost_month, 4),
        "tokens_in_month":      tok_in_month,
        "tokens_out_month":     tok_out_month,
    })


# ── Ringba → Meta Conversions API ─────────────────────────────────────────────

def _log_ringba_event(
    phone_last4: str,
    status: str,
    events_received: int = 0,
    meta_error: str = "",
    payload_keys: str = "",
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ringba_events (phone_last4, status, events_received, meta_error, payload_keys)"
            " VALUES (?,?,?,?,?)",
            (phone_last4, status, events_received, meta_error, payload_keys),
        )


def _hash_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return hashlib.sha256(digits.encode()).hexdigest()


def _extract_phone(payload: dict) -> str:
    for key in ("CallerId", "caller_id", "ANI", "InboundCallId", "Number", "caller_number", "phone"):
        val = str(payload.get(key, "")).strip()
        if val and len(re.sub(r"\D", "", val)) >= 10:
            return val
    return ""


@app.post("/ringba/webhook")
async def ringba_webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    keys_str = ",".join(payload.keys())
    token_received = request.query_params.get("token", "")
    print(f"[Ringba] Webhook received — keys: {keys_str}, token_ok: {token_received == RINGBA_WEBHOOK_SECRET or not RINGBA_WEBHOOK_SECRET}")

    if RINGBA_WEBHOOK_SECRET and token_received != RINGBA_WEBHOOK_SECRET:
        _log_ringba_event("", "unauthorized", meta_error="token_mismatch", payload_keys=keys_str)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not META_PIXEL_ID or not META_CONVERSIONS_TOKEN:
        print("[Ringba] META_PIXEL_ID or META_CONVERSIONS_TOKEN not set — skipping")
        _log_ringba_event("", "skipped", meta_error="meta not configured", payload_keys=keys_str)
        return JSONResponse({"status": "skipped", "reason": "meta not configured"})

    phone = _extract_phone(payload)
    if not phone:
        print(f"[Ringba] No phone found in payload — keys: {keys_str}")
        _log_ringba_event("", "skipped", meta_error="no phone found", payload_keys=keys_str)
        return JSONResponse({"status": "skipped", "reason": "no phone found"})

    phone_last4 = phone[-4:]
    meta_version = os.environ.get("META_API_VERSION") or "v25.0"
    url = f"https://graph.facebook.com/{meta_version}/{META_PIXEL_ID}/events"

    event_data: dict = {
        "event_name": "Lead",
        "event_time": int(_time.time()),
        "action_source": "phone_call",
        "user_data": {"ph": [_hash_phone(phone)]},
    }
    body: dict = {"data": [event_data]}
    test_code = os.environ.get("META_TEST_EVENT_CODE", "")
    if test_code:
        body["test_event_code"] = test_code

    try:
        resp = http_requests.post(
            url,
            params={"access_token": META_CONVERSIONS_TOKEN},
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        events_received = resp.json().get("events_received", 0)
        print(f"[Ringba] Meta received {events_received} event(s) for ...{phone_last4}")
        _log_ringba_event(phone_last4, "ok", events_received=events_received, payload_keys=keys_str)
        return JSONResponse({"status": "ok", "events_received": events_received})
    except Exception as e:
        err = str(e)
        print(f"[Ringba] Meta Conversions API error: {err}")
        _log_ringba_event(phone_last4, "error", meta_error=err[:500], payload_keys=keys_str)
        return JSONResponse({"status": "error", "detail": err}, status_code=500)


@app.get("/ringba/stats")
async def ringba_stats() -> JSONResponse:
    now      = datetime.now(timezone.utc)
    today    = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        def q(sql, *args):
            return conn.execute(sql, args).fetchone()[0] or 0

        total      = q("SELECT COUNT(*) FROM ringba_events")
        total_ok   = q("SELECT COUNT(*) FROM ringba_events WHERE status='ok'")
        today_tot  = q("SELECT COUNT(*) FROM ringba_events WHERE created_at LIKE ?", f"{today}%")
        today_ok   = q("SELECT COUNT(*) FROM ringba_events WHERE status='ok' AND created_at LIKE ?", f"{today}%")
        week_tot   = q("SELECT COUNT(*) FROM ringba_events WHERE date(created_at) >= ?", week_ago)
        week_ok    = q("SELECT COUNT(*) FROM ringba_events WHERE status='ok' AND date(created_at) >= ?", week_ago)

        last_row = conn.execute(
            "SELECT created_at, phone_last4, status, events_received, meta_error"
            " FROM ringba_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        recent = conn.execute(
            "SELECT created_at, phone_last4, status, events_received, meta_error"
            " FROM ringba_events ORDER BY created_at DESC LIMIT 20"
        ).fetchall()

    return JSONResponse({
        "all_time":    {"total": total,     "ok": total_ok},
        "today":       {"total": today_tot, "ok": today_ok},
        "last_7_days": {"total": week_tot,  "ok": week_ok},
        "last_event":  dict(last_row) if last_row else None,
        "recent_20":   [dict(r) for r in recent],
        "note": "Verify historical events in Meta Events Manager → Data Sources → pixel → Events tab → Lead / Server",
    })


_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Royalspace — Acceso</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ background: #0f0f1a; }}
  input[type=password] {{ letter-spacing: 0.3em; }}
</style>
</head>
<body class="min-h-screen flex items-center justify-center">
  <div style="background:#1a1a2e;border:1px solid #2d2d4e;border-radius:16px;padding:40px 48px;width:360px;">
    <div style="text-align:center;margin-bottom:28px;">
      <p style="color:#e2e8f0;font-size:1.4rem;font-weight:700;font-family:sans-serif;">Royalspace</p>
      <p style="color:#6b7280;font-size:0.85rem;font-family:sans-serif;margin-top:4px;">Billing Dashboard</p>
    </div>
    <form method="POST" action="/billing/login">
      <div style="margin-bottom:16px;">
        <label style="color:#9ca3af;font-size:0.8rem;font-family:sans-serif;display:block;margin-bottom:6px;">PIN de acceso</label>
        <input type="password" name="pin" autofocus autocomplete="off"
          style="width:100%;padding:12px 14px;background:#0f0f1a;border:1px solid #374151;border-radius:8px;color:#e2e8f0;font-size:1.1rem;outline:none;box-sizing:border-box;"
          placeholder="••••••">
      </div>
      {error}
      <button type="submit"
        style="width:100%;padding:12px;background:#4f46e5;border:none;border-radius:8px;color:white;font-size:0.95rem;font-family:sans-serif;cursor:pointer;margin-top:4px;">
        Entrar
      </button>
    </form>
  </div>
</body>
</html>"""


def _check_session(request) -> bool:
    """Verifica que la cookie de sesión sea válida."""
    expected = os.environ.get("BILLING_DASHBOARD_TOKEN", "")
    if not expected:
        return False
    session = request.cookies.get("billing_session", "")
    return bool(session) and session == expected


@app.get("/billing/login")
async def billing_login_page() -> object:
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_LOGIN_PAGE.format(error=""))


@app.post("/billing/login")
async def billing_login(request: Request) -> object:
    from fastapi.responses import HTMLResponse, RedirectResponse
    form    = await request.form()
    pin     = (form.get("pin") or "").strip()
    expected = os.environ.get("BILLING_DASHBOARD_TOKEN", "")
    if pin and expected and pin == expected:
        response = RedirectResponse(url="/billing", status_code=303)
        response.set_cookie(
            key="billing_session",
            value=expected,
            httponly=True,
            samesite="lax",
            max_age=8 * 3600,  # 8 horas
        )
        return response
    error_html = '<p style="color:#f87171;font-size:0.82rem;font-family:sans-serif;margin-bottom:12px;">PIN incorrecto. Intenta de nuevo.</p>'
    return HTMLResponse(_LOGIN_PAGE.format(error=error_html), status_code=401)


@app.get("/billing/logout")
async def billing_logout() -> object:
    from fastapi.responses import RedirectResponse
    response = RedirectResponse(url="/billing/login", status_code=303)
    response.delete_cookie("billing_session")
    return response


@app.get("/billing")
async def billing_dashboard(request: Request) -> object:
    """
    Billing Dashboard — reads DASHBOARD_* tabs from Google Sheets.
    Protected by PIN login + session cookie.
    """
    from fastapi.responses import HTMLResponse, RedirectResponse

    if not _check_session(request):
        return RedirectResponse(url="/billing/login", status_code=303)

    creds_json      = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    spreadsheet_id  = os.environ.get("BILLING_SPREADSHEET_ID", "")

    summary_rows: list[list] = []
    buyers_rows:  list[list] = []
    invoice_rows: list[list] = []
    sheets_error: str = ""

    if not creds_json:
        sheets_error = "GOOGLE_SERVICE_ACCOUNT_JSON no está configurado en Railway."
    elif not spreadsheet_id:
        sheets_error = "BILLING_SPREADSHEET_ID no está configurado en Railway."
    else:
        try:
            import json as _json
            import gspread
            from google.oauth2.service_account import Credentials

            _scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            _creds  = Credentials.from_service_account_info(_json.loads(creds_json), scopes=_scopes)
            _gc     = gspread.authorize(_creds)
            _ss     = _gc.open_by_key(spreadsheet_id)

            def _read_tab(name):
                try:
                    return _ss.worksheet(name).get_all_values()
                except Exception as tab_err:
                    return [["_tab_error", str(tab_err)]]

            summary_rows = _read_tab("DASHBOARD_SUMMARY")
            buyers_rows  = _read_tab("DASHBOARD_BUYERS")
            invoice_rows = _read_tab("DASHBOARD_INVOICES")
        except Exception as e:
            sheets_error = str(e)

    def _kv(rows, key):
        for r in rows[1:]:
            if r and r[0] == key:
                return r[1] if len(r) > 1 else ""
        return "$0.00"

    facturado  = _kv(summary_rows, "Total Facturado 2026")
    cobrado    = _kv(summary_rows, "Total Cobrado")
    pendiente  = _kv(summary_rows, "Total Pendiente")
    vencido    = _kv(summary_rows, "Total Vencido")
    x_facturar = _kv(summary_rows, "Revenue por Facturar")
    updated_at = summary_rows[1][2] if len(summary_rows) > 1 and len(summary_rows[1]) > 2 else ""

    def _table(rows):
        if not rows:
            return "<p class='text-gray-500 text-sm'>Sin datos</p>"
        html = "<table class='w-full text-sm'>"
        html += "<thead><tr>"
        for h in rows[0]:
            html += f"<th class='text-left px-3 py-2 text-gray-400 font-semibold border-b border-gray-700'>{h}</th>"
        html += "</tr></thead><tbody>"
        for row in rows[1:]:
            estado = row[6].strip().upper() if len(row) > 6 else ""
            if estado == "VENCIDO":
                bg = "bg-red-900/30"
            elif estado == "PENDIENTE":
                bg = "bg-yellow-900/20"
            else:
                bg = "hover:bg-gray-800/50"
            html += f"<tr class='{bg} transition-colors'>"
            for i, cell in enumerate(row):
                align = "text-right" if cell.startswith("$") else "text-left"
                html += f"<td class='px-3 py-2 {align} border-b border-gray-800/50'>{cell}</td>"
            html += "</tr>"
        html += "</tbody></table>"
        return html

    buyers_table  = _table(buyers_rows)
    invoice_table = _table(invoice_rows)
    error_banner  = (
        f'<div style="background:#7f1d1d;border:1px solid #ef4444;border-radius:8px;'
        f'padding:12px 16px;margin-bottom:24px;color:#fca5a5;font-size:0.85rem;">'
        f'⚠️ Error cargando datos: {sheets_error}</div>'
    ) if sheets_error else ""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Royalspace — Billing Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ background: #0f0f1a; color: #e2e8f0; font-family: 'Inter', sans-serif; }}
  .card {{ background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 12px; }}
  .kpi-val {{ font-size: 1.8rem; font-weight: 700; }}
</style>
</head>
<body class="min-h-screen p-6">

  <!-- Header -->
  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-2xl font-bold text-white">Royalspace <span class="text-indigo-400">/ Billing</span></h1>
      <p class="text-gray-500 text-sm mt-1">Dashboard de facturación en tiempo real</p>
    </div>
    <div class="text-right">
      <p class="text-gray-500 text-xs">Actualizado</p>
      <p class="text-gray-300 text-sm font-medium">{updated_at}</p>
      <a href="/billing/logout" class="text-xs text-gray-600 hover:text-gray-400 mt-1 block">Cerrar sesión</a>
    </div>
  </div>

  {error_banner}
  <!-- KPI Cards -->
  <div class="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
    <div class="card p-4">
      <p class="text-gray-400 text-xs mb-1">Total Facturado</p>
      <p class="kpi-val text-white">{facturado}</p>
    </div>
    <div class="card p-4">
      <p class="text-gray-400 text-xs mb-1">Cobrado</p>
      <p class="kpi-val text-green-400">{cobrado}</p>
    </div>
    <div class="card p-4">
      <p class="text-gray-400 text-xs mb-1">Pendiente</p>
      <p class="kpi-val text-yellow-400">{pendiente}</p>
    </div>
    <div class="card p-4">
      <p class="text-gray-400 text-xs mb-1">Vencido</p>
      <p class="kpi-val text-red-400">{vencido}</p>
    </div>
    <div class="card p-4">
      <p class="text-gray-400 text-xs mb-1">Por Facturar</p>
      <p class="kpi-val text-indigo-400">{x_facturar}</p>
    </div>
  </div>

  <!-- Buyers Table -->
  <div class="card p-5 mb-6">
    <h2 class="text-white font-semibold mb-4">Resumen por Buyer</h2>
    <div class="overflow-x-auto">
      {buyers_table}
    </div>
  </div>

  <!-- Active Invoices -->
  <div class="card p-5">
    <h2 class="text-white font-semibold mb-4">Facturas Activas
      <span class="text-xs text-gray-500 font-normal ml-2">(PENDIENTE + VENCIDO)</span>
    </h2>
    <div class="overflow-x-auto">
      {invoice_table}
    </div>
  </div>

</body>
</html>"""

    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

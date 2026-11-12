"""
Messenger directo con Meta (sin ManyChat).

Meta llama a /messenger/webhook con cada mensaje de las páginas conectadas a la app.
Usa el MISMO cerebro que /chat (responder() de main.py), así que las reglas del bot no cambian.

Modo por página (variable MESSENGER_MODE, JSON {page_id: modo}; por defecto "observar"):
  observar  → solo registra que llegó el mensaje (sin texto). No responde. Sirve para probar,
              sin tocar ManyChat, si Meta entrega mensajes de pacientes que no son admins.
  responder → contesta con la IA. Usar SOLO cuando ManyChat ya no esté respondiendo en esa página
              (si no, la persona recibe dos respuestas).

Reglas para no arriesgar la página (política de Messenger):
  - Solo responde a mensajes de la persona (messaging_type RESPONSE, dentro de las 24 h). No envía nada
    por su cuenta todavía (los recordatorios vendrán aparte y solo dentro de las 24 h).
  - Una sola respuesta por mensaje: ignora reenvíos de Meta (dedupe por mid), ecos y lecturas.
  - Responde a 200 de inmediato y procesa en segundo plano (Meta exige respuesta rápida; los bots
    automáticos deben contestar en < 30 s).
  - Si la persona pide que no le escriban (opt-out), esa conversación queda en pausa.
  - Verifica la firma X-Hub-Signature-256 con el App Secret: nadie más puede inyectar mensajes.
  - MESSENGER_AVISO (opcional): texto que se manda una vez al inicio de cada conversación
    (p. ej. aviso de atención automatizada donde la ley lo exija, como California).

Variables (Railway, servicio royalspace-automations):
  META_APP_SECRET         App Secret de la app de Meta
  MESSENGER_VERIFY_TOKEN  texto cualquiera que también se pone en el panel de Meta
  MESSENGER_PAGE_TOKENS   JSON {"<page_id>": "<page access token>"}
  MESSENGER_MODE          JSON {"<page_id>": "observar" | "responder"}
  MESSENGER_AVISO         opcional
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()
GRAPH = f"https://graph.facebook.com/{os.environ.get('META_API_VERSION') or 'v25.0'}"
_cfg: dict = {}


def configurar(responder, chat_request, get_conn) -> None:
    _cfg.update(responder=responder, chat_request=chat_request, get_conn=get_conn)
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS fb_eventos (
            mid TEXT PRIMARY KEY, page_id TEXT, psid_hash TEXT, ts INTEGER,
            es_admin_test INTEGER DEFAULT 0, accion TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS fb_estado (
            page_id TEXT, psid TEXT, pausado INTEGER DEFAULT 0, avisado INTEGER DEFAULT 0,
            PRIMARY KEY (page_id, psid))""")


def _json_env(nombre: str) -> dict:
    try:
        return json.loads(os.environ.get(nombre) or "{}")
    except json.JSONDecodeError:
        return {}


def _modo(page_id: str) -> str:
    return _json_env("MESSENGER_MODE").get(page_id, "observar")


def _firma_ok(cuerpo: bytes, firma: str) -> bool:
    secreto = os.environ.get("META_APP_SECRET", "")
    if not secreto or not firma.startswith("sha256="):
        return False
    esperado = hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, firma[7:])


@router.get("/messenger/webhook")
async def verificar(request: Request):
    q = request.query_params
    token = os.environ.get("MESSENGER_VERIFY_TOKEN", "")
    if token and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="forbidden")


@router.post("/messenger/webhook")
async def recibir(request: Request, tareas: BackgroundTasks):
    cuerpo = await request.body()
    if not _firma_ok(cuerpo, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=401, detail="bad signature")
    data = json.loads(cuerpo or b"{}")
    if data.get("object") != "page":
        return {"ok": True}
    for entry in data.get("entry", []):
        page_id = str(entry.get("id"))
        for ev in entry.get("messaging", []):
            msg = ev.get("message") or {}
            if not msg or msg.get("is_echo"):
                continue  # ecos de nuestras propias respuestas, lecturas, entregas
            mid = msg.get("mid") or f"{page_id}:{ev.get('timestamp')}"
            psid = str((ev.get("sender") or {}).get("id", ""))
            texto = (msg.get("text") or "").strip()
            if not texto and msg.get("attachments"):
                texto = "[imagen o archivo adjunto]"
            if not psid or not _registrar(mid, page_id, psid):
                continue  # reenvío de Meta: ya lo procesamos
            if _modo(page_id) == "responder":
                tareas.add_task(_contestar, page_id, psid, texto, mid)
    return {"ok": True}


def _registrar(mid: str, page_id: str, psid: str) -> bool:
    with _cfg["get_conn"]() as c:
        try:
            c.execute("INSERT INTO fb_eventos (mid, page_id, psid_hash, ts, accion) VALUES (?,?,?,?,?)",
                      (mid, page_id, hashlib.sha256(psid.encode()).hexdigest()[:16], int(time.time()),
                       _modo(page_id)))
            return True
        except Exception:
            return False


def _enviar(page_id: str, psid: str, texto: str) -> bool:
    token = _json_env("MESSENGER_PAGE_TOKENS").get(page_id)
    if not token:
        print(f"[messenger] sin token para la página {page_id}", flush=True)
        return False
    ok = True
    for i in range(0, len(texto), 1900):  # límite de Messenger: 2000 caracteres por mensaje
        r = requests.post(f"{GRAPH}/me/messages", params={"access_token": token}, timeout=15, json={
            "recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": texto[i:i + 1900]}})
        if r.status_code != 200:
            ok = False
            print(f"[messenger] error al enviar ({r.status_code}): {r.text[:200]}", flush=True)
            break
    return ok


def _accion(page_id: str, psid: str, tipo: str) -> None:
    token = _json_env("MESSENGER_PAGE_TOKENS").get(page_id)
    if token:
        try:
            requests.post(f"{GRAPH}/me/messages", params={"access_token": token}, timeout=5,
                          json={"recipient": {"id": psid}, "sender_action": tipo})
        except Exception:
            pass


def _contestar(page_id: str, psid: str, texto: str, mid: str) -> None:
    with _cfg["get_conn"]() as c:
        fila = c.execute("SELECT pausado, avisado FROM fb_estado WHERE page_id=? AND psid=?",
                         (page_id, psid)).fetchone()
    if fila and fila[0]:
        return  # pidió que no le escriban
    _accion(page_id, psid, "mark_seen")
    _accion(page_id, psid, "typing_on")
    aviso = os.environ.get("MESSENGER_AVISO", "").strip()
    if aviso and not (fila and fila[1]):
        _enviar(page_id, psid, aviso)
    try:
        res = _cfg["responder"](_cfg["chat_request"](subscriber_id=f"fb:{page_id}:{psid}", last_input_text=texto))
    except Exception as e:
        print(f"[messenger] error de la IA: {type(e).__name__}", flush=True)
        return
    with _cfg["get_conn"]() as c:
        c.execute("""INSERT INTO fb_estado (page_id, psid, pausado, avisado) VALUES (?,?,?,?)
                     ON CONFLICT(page_id, psid) DO UPDATE SET pausado=excluded.pausado, avisado=excluded.avisado""",
                  (page_id, psid, 1 if res.get("action") == "pause" else 0, 1 if aviso else 0))
    if res.get("response"):
        _enviar(page_id, psid, res["response"])
    _accion(page_id, psid, "typing_off")


@router.get("/messenger/status")
async def estado(request: Request):
    clave = os.environ.get("STATS_API_KEY", "")
    if clave and request.headers.get("X-Api-Key", "") != clave:
        raise HTTPException(status_code=401, detail="unauthorized")
    desde = int(time.time()) - 7 * 86400
    with _cfg["get_conn"]() as c:
        filas = c.execute("""SELECT page_id, COUNT(*), COUNT(DISTINCT psid_hash), MAX(ts)
                             FROM fb_eventos WHERE ts >= ? GROUP BY page_id""", (desde,)).fetchall()
    return {"paginas": [{"page_id": f[0], "mensajes_7d": f[1], "personas_7d": f[2],
                         "ultimo": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(f[3])),
                         "modo": _modo(f[0])} for f in filas],
            "configurado": {"app_secret": bool(os.environ.get("META_APP_SECRET")),
                            "verify_token": bool(os.environ.get("MESSENGER_VERIFY_TOKEN")),
                            "paginas_con_token": list(_json_env("MESSENGER_PAGE_TOKENS").keys())}}

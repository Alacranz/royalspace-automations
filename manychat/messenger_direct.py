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
import re
import threading
import time

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()
GRAPH = f"https://graph.facebook.com/{os.environ.get('META_API_VERSION') or 'v25.0'}"
_cfg: dict = {}


def configurar(responder, chat_request, get_conn) -> None:
    _cfg.update(responder=responder, chat_request=chat_request, get_conn=get_conn)
    threading.Thread(target=_revisar_tokens, daemon=True).start()
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS fb_eventos (
            mid TEXT PRIMARY KEY, page_id TEXT, psid_hash TEXT, ts INTEGER,
            es_admin_test INTEGER DEFAULT 0, accion TEXT)""")
        _purgar(c)
        c.execute("""CREATE TABLE IF NOT EXISTS fb_estado (
            page_id TEXT, psid TEXT, pausado INTEGER DEFAULT 0, avisado INTEGER DEFAULT 0,
            PRIMARY KEY (page_id, psid))""")
        for col in ("ult_usuario INTEGER DEFAULT 0", "recordatorios INTEGER DEFAULT 0", "idioma TEXT DEFAULT 'es'", "otra_app INTEGER DEFAULT 0", "tarjeta INTEGER DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE fb_estado ADD COLUMN {col}")
            except Exception:
                pass  # ya existe
    threading.Thread(target=_bucle_recordatorios, daemon=True).start()


_ultima_purga = [0.0]
_BORRAR = re.compile(r"\b(delete my data|borrar? mis datos|elimin(a|ar) mis datos)\b", re.I)


def _purgar(c) -> None:
    """Política de privacidad: el historial se guarda máximo 90 días."""
    for sql, args in (("DELETE FROM messages WHERE created_at < datetime('now', '-90 days')", ()),
                      ("DELETE FROM fb_eventos WHERE ts < ?", (int(time.time()) - 90 * 86400,))):
        try:
            c.execute(sql, args)
        except Exception:
            pass  # tabla todavía no creada (primer arranque)
    _ultima_purga[0] = time.time()


def _revisar_tokens() -> None:
    """Al arrancar: confirma en el log que cada token de página funciona (sin mostrar los tokens)."""
    raw = os.environ.get("MESSENGER_PAGE_TOKENS")
    if not raw:
        print("[messenger] MESSENGER_PAGE_TOKENS vacío", flush=True)
        return
    tokens = _json_env("MESSENGER_PAGE_TOKENS")
    if not tokens:
        print("[messenger] MESSENGER_PAGE_TOKENS no es un JSON válido (revisa comillas y comas)", flush=True)
        return
    app_id = os.environ.get("META_APP_ID") or "1147303537768031"  # Royalspace Messenger (no es secreto)
    app_tok = f"{app_id}|{os.environ.get('META_APP_SECRET', '')}"
    necesarios = {"pages_messaging", "pages_manage_metadata"}
    for pid, tok in tokens.items():
        try:
            r = requests.get(f"{GRAPH}/debug_token",
                             params={"input_token": tok, "access_token": app_tok}, timeout=10)
            d = (r.json() or {}).get("data") or {}
            if not d.get("is_valid"):
                err = (d.get("error") or r.json().get("error") or {})
                print(f"[messenger] token FALLA {pid}: inválido · {str(err.get('message', ''))[:90]}", flush=True)
                continue
            scopes = set(d.get("scopes") or [])
            faltan = sorted(necesarios - scopes)
            pagina_ok = str(d.get("profile_id") or pid) == pid
            app_ok = str(d.get("app_id")) == app_id
            estado = "OK" if not faltan and pagina_ok and app_ok else "INCOMPLETO"
            print(f"[messenger] token {estado} {pid} · tipo {d.get('type')} · app_ok={app_ok} · "
                  f"pagina_ok={pagina_ok} · vence={d.get('expires_at') or 0} · "
                  f"faltan={','.join(faltan) or '-'} · permisos={','.join(sorted(scopes))}", flush=True)
        except Exception as e:
            print(f"[messenger] token FALLA {pid}: {type(e).__name__}", flush=True)
    print(f"[messenger] {len(tokens)} páginas en el JSON; modos: {_json_env('MESSENGER_MODE') or 'todas en observar'}", flush=True)


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
        eventos = [(ev, "messaging") for ev in entry.get("messaging", [])] + \
                  [(ev, "standby") for ev in entry.get("standby", [])]
        for ev, canal in eventos:
            msg = ev.get("message") or {}
            if msg.get("is_echo"):
                _eco(page_id, ev, msg)
                continue
            if not msg:
                continue  # lecturas, entregas
            mid = msg.get("mid") or f"{page_id}:{ev.get('timestamp')}"
            psid = str((ev.get("sender") or {}).get("id", ""))
            texto = (msg.get("text") or "").strip()
            if not texto and msg.get("attachments"):
                texto = "[imagen o archivo adjunto]"
            if not psid or not _registrar(mid, page_id, psid):
                continue  # reenvío de Meta: ya lo procesamos
            print(f"[messenger] evento {canal} página {page_id} · de {hashlib.sha256(psid.encode()).hexdigest()[:8]} · "
                  f"modo {_modo(page_id)}", flush=True)
            # standby = otra app (ManyChat) controla la conversación: solo observamos, nunca respondemos
            if canal == "messaging" and _modo(page_id) == "responder":
                tareas.add_task(_contestar, page_id, psid, texto, mid)
    return {"ok": True}

_lock = threading.Lock()
_pendientes: dict = {}
_ultimo_mid: dict = {}
NUESTRA_APP = os.environ.get("META_APP_ID") or "1147303537768031"


def _eco(page_id: str, ev: dict, msg: dict) -> None:
    """Si otra app (ManyChat) le escribió a esta persona, ese chat sigue siendo suyo: no respondemos encima."""
    app = str(msg.get("app_id") or "")
    if not app or app == NUESTRA_APP:
        return
    psid = str((ev.get("recipient") or {}).get("id", ""))
    if not psid:
        return
    with _cfg["get_conn"]() as c:
        c.execute("""INSERT INTO fb_estado (page_id, psid, otra_app) VALUES (?,?,?)
                     ON CONFLICT(page_id, psid) DO UPDATE SET otra_app=excluded.otra_app""",
                  (page_id, psid, int(time.time())))
    print(f"[messenger] chat {hashlib.sha256(psid.encode()).hexdigest()[:8]} atendido por otra app ({app})", flush=True)


def _otra_app_reciente(page_id: str, psid: str) -> bool:
    with _cfg["get_conn"]() as c:
        f = c.execute("SELECT otra_app FROM fb_estado WHERE page_id=? AND psid=?", (page_id, psid)).fetchone()
    return bool(f and f[0] and time.time() - f[0] < 24 * 3600)


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


def _telefono(page_id: str, idioma: str) -> str:
    """MESSENGER_PAGE_PHONES = {"<page_id>": "+18447750120"} o {"<page_id>": {"en": "+1…", "es": "+1…"}}."""
    v = _json_env("MESSENGER_PAGE_PHONES").get(page_id)
    if isinstance(v, dict):
        v = v.get(idioma) or v.get("en") or v.get("es")
    num = re.sub(r"\D", "", str(v or ""))
    if len(num) == 10:
        num = "1" + num
    return f"+{num}" if len(num) == 11 else ""


def _boton_llamar(page_id: str, psid: str, idioma: str, texto: str = "") -> bool:
    """Tarjeta igual a la de ManyChat: '📞 CALL NOW' + botón con el número de ESA página."""
    tel = _telefono(page_id, idioma)
    if not tel:
        print(f"[messenger] sin teléfono configurado para la página {page_id}: no envío botón", flush=True)
        return False
    token = _json_env("MESSENGER_PAGE_TOKENS").get(page_id)
    titulo = f"({tel[2:5]}) {tel[5:8]}-{tel[8:]}"
    cuerpo = texto or ("📞 LLAMA AHORA\nSi al llamar escuchas un mensaje en inglés, presiona 1 para continuar."
                       if idioma == "es" else "📞 CALL NOW")
    try:
        r = requests.post(f"{GRAPH}/me/messages", params={"access_token": token}, timeout=15, json={
            "recipient": {"id": psid}, "messaging_type": "RESPONSE",
            "message": {"attachment": {"type": "template", "payload": {
                "template_type": "button", "text": cuerpo[:640],
                "buttons": [{"type": "phone_number", "title": titulo, "payload": tel}]}}}})
    except Exception as e:
        print(f"[messenger] error botón: {type(e).__name__}", flush=True)
        return False
    if r.status_code != 200:
        print(f"[messenger] error botón ({r.status_code}): {r.text[:200]}", flush=True)
    return r.status_code == 200


_seq_ia: dict = {}


def _programar_tarjeta(page_id: str, psid: str, idioma: str) -> None:
    """Tarjeta de llamar: inmediata en la primera respuesta; después, una sola vez cuando la charla
    se calma (MESSENGER_TARJETA_SEG sin que la persona escriba ni la IA responda)."""
    clave = (page_id, psid)
    with _lock:
        _seq_ia[clave] = _seq_ia.get(clave, 0) + 1
        seq = _seq_ia[clave]
    with _cfg["get_conn"]() as c:
        f = c.execute("SELECT tarjeta FROM fb_estado WHERE page_id=? AND psid=?", (page_id, psid)).fetchone()
    if not (f and f[0]):
        _mandar_tarjeta(page_id, psid, idioma)
        return

    def esperar():
        time.sleep(int(os.environ.get("MESSENGER_TARJETA_SEG") or 15))
        with _lock:
            if _seq_ia.get(clave) != seq or _pendientes.get(clave):
                return  # la conversación siguió: la tarjeta saldrá cuando se calme
        _mandar_tarjeta(page_id, psid, idioma)
    threading.Thread(target=esperar, daemon=True).start()


def _mandar_tarjeta(page_id: str, psid: str, idioma: str) -> None:
    if _boton_llamar(page_id, psid, idioma):
        with _cfg["get_conn"]() as c:
            c.execute("UPDATE fb_estado SET tarjeta=? WHERE page_id=? AND psid=?", (int(time.time()), page_id, psid))


_RECORDATORIOS = {
    # (minutos desde el último mensaje de la persona, texto) — siempre dentro de la ventana de 24 h de Meta
    "es": [(30, "¿Todavía buscas dentista? 🦷 Llámanos y te conectamos con uno cerca de ti. "
                "Si al llamar escuchas un mensaje en inglés, presiona 1 para continuar."),
           (22 * 60, "Solo para recordarte 😊 Seguimos aquí para ayudarte a encontrar dentista en tu zona. "
                     "Toca el botón para llamar. Si escuchas un mensaje en inglés, presiona 1.")],
    "en": [(30, "Still looking for a dentist? 🦷 Give us a call and we'll connect you with one near you."),
           (22 * 60, "Just a quick reminder 😊 We're still here to help you find a dentist in your area. "
                     "Tap the button to call.")],
}


def _bucle_recordatorios() -> None:
    while True:
        time.sleep(300)
        try:
            _enviar_recordatorios()
        except Exception as e:
            print(f"[messenger] error recordatorios: {type(e).__name__}", flush=True)


def _enviar_recordatorios() -> None:
    ahora = int(time.time())
    with _cfg["get_conn"]() as c:
        filas = c.execute("""SELECT page_id, psid, recordatorios, ult_usuario, idioma FROM fb_estado
                             WHERE pausado = 0 AND ult_usuario > ? AND recordatorios < 2""",
                          (ahora - 23 * 3600 - 1800,)).fetchall()
    for page_id, psid, n, ult, idioma in filas:
        if _modo(page_id) != "responder" or _otra_app_reciente(page_id, psid):
            continue
        idioma = idioma if idioma in _RECORDATORIOS else "es"
        minutos, texto = _RECORDATORIOS[idioma][n]
        if ahora - ult < minutos * 60 or ahora - ult > 23 * 3600 + 1800:
            continue  # todavía no toca, o ya se cerró la ventana de 24 h
        enviado = _boton_llamar(page_id, psid, idioma, texto)
        with _cfg["get_conn"]() as c:
            c.execute("UPDATE fb_estado SET recordatorios = ? WHERE page_id = ? AND psid = ?",
                      (n + 1, page_id, psid))
        print(f"[messenger] recordatorio {n + 1} a {hashlib.sha256(psid.encode()).hexdigest()[:8]} "
              f"({idioma}) enviado={enviado}", flush=True)


def _contestar(page_id: str, psid: str, texto: str, mid: str) -> None:
    with _cfg["get_conn"]() as c:
        fila = c.execute("SELECT pausado, avisado FROM fb_estado WHERE page_id=? AND psid=?",
                         (page_id, psid)).fetchone()
    if _BORRAR.search(texto or ""):
        sid = f"fb:{page_id}:{psid}"
        with _cfg["get_conn"]() as c:
            c.execute("DELETE FROM messages WHERE subscriber_id = ?", (sid,))
            c.execute("DELETE FROM subscriber_language WHERE subscriber_id = ?", (sid,))
            c.execute("DELETE FROM fb_estado WHERE page_id = ? AND psid = ?", (page_id, psid))
        es = bool(re.search(r"borr|elimin", texto, re.I))
        _enviar(page_id, psid, "Listo, borramos tu historial de conversación con nosotros." if es
                else "Done — we deleted your conversation history with us.")
        return
    quien = hashlib.sha256(psid.encode()).hexdigest()[:8]
    clave = (page_id, psid)
    with _lock:
        _pendientes.setdefault(clave, []).append(texto)
        _ultimo_mid[clave] = mid
    # Damos 15 s: (1) ver si ManyChat contesta este mismo mensaje y (2) juntar mensajes seguidos.
    time.sleep(int(os.environ.get("MESSENGER_ESPERA_SEG") or 15))
    with _lock:
        if _ultimo_mid.get(clave) != mid:
            return  # llegó otro mensaje después: ese responde a todo junto
        texto = "\n".join(t for t in _pendientes.pop(clave, []) if t) or texto
        _ultimo_mid.pop(clave, None)
    if _otra_app_reciente(page_id, psid):
        print(f"[messenger] {quien}: este chat lo atiende otra app (ManyChat) — no respondo", flush=True)
        return
    if fila and fila[0]:
        print(f"[messenger] {quien}: conversación pausada, no respondo", flush=True)
        return  # pidió que no le escriban
    if time.time() - _ultima_purga[0] > 86400:
        with _cfg["get_conn"]() as c:
            _purgar(c)
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
    idioma = res.get("language") if res.get("language") in ("en", "es") else "es"
    with _cfg["get_conn"]() as c:
        c.execute("""INSERT INTO fb_estado (page_id, psid, pausado, avisado, ult_usuario, recordatorios, idioma)
                     VALUES (?,?,?,?,?,0,?)
                     ON CONFLICT(page_id, psid) DO UPDATE SET pausado=excluded.pausado, avisado=excluded.avisado,
                       ult_usuario=excluded.ult_usuario, recordatorios=0, idioma=excluded.idioma""",
                  (page_id, psid, 1 if res.get("action") == "pause" else 0, 1 if aviso else 0,
                   int(time.time()), idioma))
    ok = _enviar(page_id, psid, res["response"]) if res.get("response") else None
    if ok and res.get("action") == "respond":
        _programar_tarjeta(page_id, psid, idioma)
    print(f"[messenger] {quien}: IA acción={res.get('action')} · {len(res.get('response') or '')} chars · "
          f"enviado={ok}", flush=True)
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


# ── Páginas legales públicas (las exige Meta para publicar la app) ──────────────
from fastapi.responses import HTMLResponse  # noqa: E402

_EMPRESA = "Royalspace, LLC"
_DIRECCION = "8206 Louisiana Blvd NE, Albuquerque, NM 87113, USA"
_CORREO = os.environ.get("LEGAL_CONTACT_EMAIL") or "edwarilichreyes@gmail.com"
_FECHA = "November 12, 2026"


def _pagina(titulo: str, cuerpo: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{titulo} — {_EMPRESA}</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:40px auto;
padding:0 20px;line-height:1.6;color:#222}}h1{{font-size:1.6rem}}h2{{font-size:1.15rem;margin-top:1.8em}}
hr{{margin:2.5em 0;border:0;border-top:1px solid #ddd}}small{{color:#666}}</style></head><body>
<h1>{titulo}</h1><small>{_EMPRESA} · Last updated / Última actualización: {_FECHA}</small>{cuerpo}
<hr><p><small>{_EMPRESA} · {_DIRECCION} · <a href="mailto:{_CORREO}">{_CORREO}</a></small></p></body></html>""")


@router.get("/legal/privacy", response_class=HTMLResponse)
async def privacidad():
    return _pagina("Privacy Policy / Política de privacidad", f"""
<h2>Who we are</h2><p>{_EMPRESA} operates Facebook Pages that help people find a dentist near them. When you message
one of our Pages on Messenger, an automated assistant may answer your questions and invite you to call our phone line,
where you are connected with a dental office in your area.</p>
<h2>What we collect</h2><ul><li>Your messages to our Page and our replies.</li>
<li>Your Page-scoped Messenger ID and the first name Messenger shares with us.</li>
<li>Information you choose to give us, such as your ZIP code, city or the dental treatment you are interested in.</li></ul>
<p>We do not ask for, and you should not send us, medical records, insurance numbers or payment information.</p>
<h2>How we use it</h2><ul><li>To reply to your messages and help you find a dentist in your area.</li>
<li>To keep the context of your conversation so you don't have to repeat yourself.</li>
<li>To measure and improve our service.</li></ul>
<p>Replies may be generated by an automated assistant (artificial intelligence). We do not sell your information and
we do not use it for any purpose other than answering your inquiry.</p>
<h2>Who we share it with</h2><p>Service providers that help us run the service (cloud hosting and an AI provider that
generates replies), only to provide the service. Meta Platforms processes your messages as the operator of Messenger.</p>
<h2>How long we keep it</h2><p>Conversation history is kept for up to 90 days and then deleted, unless you ask us to
delete it sooner.</p>
<h2>Your choices</h2><p>You can stop receiving messages at any time by writing "stop". To request a copy or deletion of
your data, see our <a href="/legal/data-deletion">data deletion instructions</a> or email us at {_CORREO}.</p>
<hr>
<h2>Quiénes somos</h2><p>{_EMPRESA} opera páginas de Facebook que ayudan a las personas a encontrar un dentista cerca.
Cuando escribes a una de nuestras páginas en Messenger, un asistente automatizado puede responder tus preguntas e
invitarte a llamar a nuestra línea, donde te conectamos con una clínica dental de tu zona.</p>
<h2>Qué datos recopilamos</h2><ul><li>Tus mensajes a la página y nuestras respuestas.</li>
<li>Tu identificador de Messenger para esta página y el nombre que Messenger nos comparte.</li>
<li>Lo que tú decidas contarnos, como tu código postal, ciudad o el tratamiento que te interesa.</li></ul>
<p>No pedimos, y no debes enviarnos, historias médicas, números de seguro ni datos de pago.</p>
<h2>Para qué los usamos</h2><p>Para responderte, ayudarte a encontrar un dentista en tu zona, mantener el contexto de
la conversación y mejorar el servicio. Las respuestas pueden ser generadas por un asistente automatizado (inteligencia
artificial). No vendemos tus datos.</p>
<h2>Con quién los compartimos</h2><p>Solo con proveedores que nos ayudan a operar el servicio (alojamiento en la nube
y un proveedor de IA que genera las respuestas). Meta procesa tus mensajes como operador de Messenger.</p>
<h2>Cuánto tiempo los guardamos</h2><p>El historial se guarda hasta 90 días y luego se borra, salvo que pidas
borrarlo antes.</p>
<h2>Tus opciones</h2><p>Escribe "stop" para dejar de recibir mensajes. Para pedir una copia o el borrado de tus datos,
sigue las <a href="/legal/data-deletion">instrucciones de borrado</a> o escríbenos a {_CORREO}.</p>""")


@router.get("/legal/terms", response_class=HTMLResponse)
async def condiciones():
    return _pagina("Terms of Service / Condiciones del servicio", f"""
<h2>The service</h2><p>Our Messenger assistant provides general information and helps you get in touch with dental
offices by phone. It does not provide medical or dental advice, diagnosis or treatment, and it is not an emergency
service. In a medical emergency, call 911.</p>
<h2>No guarantees</h2><p>Availability, prices and appointments are set by each dental office. Replies may be automated
and may contain mistakes; please confirm details directly with the office.</p>
<h2>Acceptable use</h2><p>Do not use the service to send unlawful, abusive or spam content.</p>
<h2>Contact</h2><p>{_EMPRESA}, {_DIRECCION}, {_CORREO}.</p>
<hr>
<h2>El servicio</h2><p>Nuestro asistente de Messenger da información general y te ayuda a comunicarte por teléfono con
clínicas dentales. No ofrece consejo, diagnóstico ni tratamiento médico u odontológico, y no es un servicio de
emergencias. En una emergencia médica, llama al 911.</p>
<h2>Sin garantías</h2><p>La disponibilidad, los precios y las citas los define cada clínica. Las respuestas pueden ser
automatizadas y contener errores; confirma los detalles directamente con la clínica.</p>
<h2>Uso aceptable</h2><p>No uses el servicio para enviar contenido ilegal, abusivo o spam.</p>""")


@router.get("/legal/data-deletion", response_class=HTMLResponse)
async def borrado():
    return _pagina("Data Deletion / Borrado de datos", f"""
<h2>How to delete your data</h2><ol><li>Send a message to the same Page with the words <b>"delete my data"</b>, or</li>
<li>email <a href="mailto:{_CORREO}">{_CORREO}</a> with the name of the Page you messaged and the approximate date.</li></ol>
<p>We delete your conversation history and identifiers within 30 days and confirm by the same channel. You can also
remove the conversation from your own Messenger at any time.</p>
<hr>
<h2>Cómo borrar tus datos</h2><ol><li>Escribe a la misma página <b>"borrar mis datos"</b>, o</li>
<li>escríbenos a <a href="mailto:{_CORREO}">{_CORREO}</a> indicando la página y la fecha aproximada.</li></ol>
<p>Borramos tu historial e identificadores en un máximo de 30 días y te lo confirmamos por el mismo medio.</p>""")

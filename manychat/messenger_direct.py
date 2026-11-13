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


def configurar(responder, chat_request, get_conn, detectar_idioma=None, abierto=None, proxima_apertura=None) -> None:
    _cfg.update(responder=responder, chat_request=chat_request, get_conn=get_conn,
                detectar_idioma=detectar_idioma, abierto=abierto or (lambda: True),
                proxima_apertura=proxima_apertura or (lambda lang="es": "mañana"))
    threading.Thread(target=_revisar_tokens, daemon=True).start()
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS fb_eventos (
            mid TEXT PRIMARY KEY, page_id TEXT, psid_hash TEXT, ts INTEGER,
            es_admin_test INTEGER DEFAULT 0, accion TEXT)""")
        _purgar(c)
        c.execute("""CREATE TABLE IF NOT EXISTS fb_estado (
            page_id TEXT, psid TEXT, pausado INTEGER DEFAULT 0, avisado INTEGER DEFAULT 0,
            PRIMARY KEY (page_id, psid))""")
        for col in ("ult_usuario INTEGER DEFAULT 0", "recordatorios INTEGER DEFAULT 0", "idioma TEXT DEFAULT 'es'", "otra_app INTEGER DEFAULT 0", "tarjeta INTEGER DEFAULT 0",
                    "avisar INTEGER DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE fb_estado ADD COLUMN {col}")
            except Exception:
                pass  # ya existe
    threading.Thread(target=_bucle_recordatorios, daemon=True).start()
    threading.Thread(target=_bucle_oficina, daemon=True).start()
    threading.Thread(target=_inventario_y_limpieza, daemon=True).start()


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


def _tokens() -> dict:
    """Tokens de página: los obtenidos con META_USER_TOKEN (guardados en la base) mandan sobre la variable."""
    t = dict(_json_env("MESSENGER_PAGE_TOKENS"))
    try:
        with _cfg["get_conn"]() as c:
            t.update({str(p): tok for p, tok in c.execute("SELECT page_id, token FROM fb_tokens").fetchall()})
    except Exception:
        pass
    return t


def _token(page_id: str) -> str | None:
    return _tokens().get(page_id)


def _tokens_desde_usuario() -> None:
    """META_USER_TOKEN (de Graph API Explorer) → token de usuario de larga duración → tokens de página que no
    vencen, con todos los permisos que tenga el usuario. Se guardan en la base (volumen), nunca en el log."""
    ut = (os.environ.get("META_USER_TOKEN") or "").strip()
    if not ut:
        return
    app_id = os.environ.get("META_APP_ID") or "1147303537768031"
    try:
        r = requests.get(f"{GRAPH}/oauth/access_token", timeout=15, params={
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": os.environ.get("META_APP_SECRET", ""), "fb_exchange_token": ut})
        largo = (r.json() or {}).get("access_token") or ut
        r = requests.get(f"{GRAPH}/me/accounts", timeout=15,
                         params={"access_token": largo, "fields": "id,name,access_token", "limit": 100})
        d = r.json() or {}
        if r.status_code != 200:
            print(f"[messenger] META_USER_TOKEN no sirvió: {str(d.get('error', {}).get('message', ''))[:150]}", flush=True)
            return
        nuestras = set(_json_env("MESSENGER_PAGE_PHONES")) | set(_json_env("MESSENGER_PAGE_TOKENS"))
        n = 0
        with _cfg["get_conn"]() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fb_tokens (page_id TEXT PRIMARY KEY, token TEXT, ts INTEGER)")
            for p in d.get("data", []):
                if str(p.get("id")) in nuestras and p.get("access_token"):
                    c.execute("INSERT OR REPLACE INTO fb_tokens (page_id, token, ts) VALUES (?,?,?)",
                              (str(p["id"]), p["access_token"], int(time.time())))
                    n += 1
        print(f"[messenger] tokens de página renovados desde META_USER_TOKEN: {n} de {len(nuestras)}", flush=True)
    except Exception as e:
        print(f"[messenger] error al renovar tokens: {type(e).__name__}", flush=True)


# ── Limpieza de etiquetas de la bandeja (inventario y borrado SOLO con lista aprobada por Edwar) ──
ETIQUETAS_NUESTRAS = {"Sugerir zip", "Pide humano", "Fuera de horario", "Medicaid", "Fuera de USA", "Urgencia / dolor", "Alto valor",
                      "Español", "Inglés"}


def _etiquetas_de(page_id: str) -> list[dict]:
    token, out, url = _token(page_id), [], f"{GRAPH}/me/custom_labels"
    params = {"access_token": token, "fields": "page_label_name", "limit": 100}
    while url and token:
        r = requests.get(url, params=params, timeout=15)
        d = r.json() or {}
        if r.status_code != 200:
            print(f"[messenger] etiquetas {page_id}: error {str(d.get('error', {}).get('message', ''))[:150]}", flush=True)
            break
        out += d.get("data", [])
        url, params = (d.get("paging") or {}).get("next"), None
    return out


def _inventario_y_limpieza() -> None:
    """MESSENGER_INVENTARIO_ETIQUETAS=1 → anota en el log cuántas etiquetas hay por página.
    MESSENGER_BORRAR_ETIQUETAS=["Nombre", ...] → borra todas las que se llamen así.
    MESSENGER_DEDUP_ETIQUETAS=["Nombre", ...] → deja 1 (la más antigua) y borra las copias.
    MESSENGER_LIMPIEZA_PAGINAS=["page_id", ...] → limita la limpieza a esas páginas. Nunca toca las nuestras."""
    borrar = set(json.loads(os.environ.get("MESSENGER_BORRAR_ETIQUETAS") or "[]")) - ETIQUETAS_NUESTRAS
    dedup = set(json.loads(os.environ.get("MESSENGER_DEDUP_ETIQUETAS") or "[]")) - ETIQUETAS_NUESTRAS
    solo = set(json.loads(os.environ.get("MESSENGER_LIMPIEZA_PAGINAS") or "[]"))
    if not (os.environ.get("MESSENGER_INVENTARIO_ETIQUETAS") or borrar or dedup):
        return
    time.sleep(20)
    from collections import Counter
    for pid, nombre in PAGINAS.items():
        ets = _etiquetas_de(pid)
        if (borrar or dedup) and (not solo or pid in solo):
            vistos, a_borrar = set(), []
            for e in sorted(ets, key=lambda e: int(e["id"])):
                n = e.get("page_label_name", "")
                if n in borrar or (n in dedup and n in vistos):
                    a_borrar.append(e)
                vistos.add(n)
            ok = fallo = 0
            for e in a_borrar:
                r = requests.delete(f"{GRAPH}/{e['id']}", params={"access_token": _token(pid)}, timeout=15)
                if r.status_code == 200:
                    ok += 1
                else:
                    fallo += 1
                    if fallo <= 2:
                        print(f"[messenger] no se pudo borrar '{e.get('page_label_name')}': {r.text[:150]}", flush=True)
            print(f"[messenger] LIMPIEZA {nombre}: borradas {ok}, fallaron {fallo}", flush=True)
            ets = _etiquetas_de(pid)
        cuenta = Counter(e.get("page_label_name", "?") for e in ets)
        sin_ad = [(k, v) for k, v in cuenta.most_common() if not k.startswith("ad_id.")]
        print(f"[messenger] INVENTARIO {nombre}: {len(ets)} etiquetas ({sum(v for k, v in cuenta.items() if k.startswith('ad_id.'))} de anuncios) · " +
              " · ".join(f"{k}×{v}" for k, v in sin_ad), flush=True)


def _revisar_tokens() -> None:
    """Al arrancar: confirma en el log que cada token de página funciona (sin mostrar los tokens)."""
    _tokens_desde_usuario()
    tokens = _tokens()
    if not tokens:
        print("[messenger] sin tokens de página (MESSENGER_PAGE_TOKENS vacío o JSON inválido)", flush=True)
        return
    app_id = os.environ.get("META_APP_ID") or "1147303537768031"  # Royalspace Messenger (no es secreto)
    app_tok = f"{app_id}|{os.environ.get('META_APP_SECRET', '')}"
    necesarios = {"pages_messaging", "pages_manage_metadata", "pages_read_engagement", "pages_manage_engagement"}
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
            if "pages_manage_metadata" in scopes:
                # Asegura que la página mande a la app mensajes, ecos y comentarios (feed).
                r2 = requests.post(f"{GRAPH}/{pid}/subscribed_apps", params={"access_token": tok}, timeout=10,
                                   data={"subscribed_fields": "messages,messaging_postbacks,messaging_handovers,"
                                                              "message_echoes,messaging_policy_enforcement,feed"})
                print(f"[messenger] suscripción de la página {pid}: "
                      f"{'ok' if r2.status_code == 200 else r2.text[:150]}", flush=True)
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
        for cambio in entry.get("changes", []):
            if cambio.get("field") == "feed":
                v = cambio.get("value") or {}
                if v.get("item") == "comment" and v.get("verb") == "add":
                    tareas.add_task(_comentario, page_id, v)
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
    token = _token(page_id)
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
    token = _token(page_id)
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
    token = _token(page_id)
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
        _metrica(page_id, "tarjetas")
        with _cfg["get_conn"]() as c:
            c.execute("UPDATE fb_estado SET tarjeta=? WHERE page_id=? AND psid=?", (int(time.time()), page_id, psid))


# ── Comentarios en publicaciones y anuncios (reemplaza "Comentarios Auto-Respuesta" de ManyChat) ──
_COMENTARIOS = {
    "es": ["Si estamos disponibles. Puedes llamarnos ahora para ver su caso y con gusto te ayudaremos con tu problema dental.",
           "Si tenemos clinicas en todo el Pais. 📞 No dudes en llamarnos, con gusto te proporcionaremos toda la información y asistencia necesaria.",
           "Tenemos clinicas en todo el area. Puedes llamarnos ahora para ver su caso y con gusto te ayudaremos con tu problema dental.",
           "Hola tenemos clinicas cercanas a tu ubicacion. Llamanos y con gusto te conectaremos con la clinica mas cercana a tu ubicacion."],
    "en": ["Yes, we're available! Call us now so we can review your case and gladly help with your dental problem.",
           "We have clinics all over the country. 📞 Give us a call and we'll gladly provide all the information and help you need.",
           "We have clinics all around your area. Call us now so we can review your case and help with your dental problem.",
           "Hi! We have clinics near your location. Call us and we'll gladly connect you with the closest clinic."],
}

def _modo_comentarios(page_id: str) -> str:
    return _json_env("MESSENGER_COMENTARIOS").get(page_id, "observar")


def _comentario(page_id: str, v: dict, forzar: bool = False) -> None:
    cid = str(v.get("comment_id") or "")
    autor = v.get("from") or {}
    if not cid or str(autor.get("id")) == page_id:
        return  # respuestas de la propia página
    if v.get("parent_id") and v.get("post_id") and v["parent_id"] != v["post_id"]:
        return  # solo comentarios de primer nivel (igual que ManyChat)
    if not forzar and not _registrar(f"c:{cid}", page_id, str(autor.get("id") or cid)):
        return  # Meta reenvió el mismo comentario
    if not forzar and _modo_comentarios(page_id) == "responder" and not _cfg["abierto"]():
        with _cfg["get_conn"]() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fb_cola_coment (cid TEXT PRIMARY KEY, page_id TEXT, v TEXT, ts INTEGER)")
            c.execute("INSERT OR IGNORE INTO fb_cola_coment VALUES (?,?,?,?)", (cid, page_id, json.dumps(v), int(time.time())))
        print(f"[messenger] comentario en página {page_id} fuera de horario — en espera hasta abrir", flush=True)
        return
    texto = (v.get("message") or "").strip()
    det = _cfg.get("detectar_idioma")
    idioma = "es"
    try:
        idioma = det(texto) if det and texto else "es"
    except Exception:
        pass
    idioma = idioma if idioma in ("en", "es") else "es"
    modo = _modo_comentarios(page_id)
    print(f"[messenger] comentario en página {page_id} ({idioma}) · modo {modo}", flush=True)
    if modo != "responder":
        return
    token = _token(page_id)
    tel = _telefono(page_id, idioma)
    if not token or not tel:
        print(f"[messenger] comentario sin token o teléfono para {page_id}", flush=True)
        return
    visible = f"({tel[2:5]}) {tel[5:8]}-{tel[8:]}"
    import random
    publico = f"{random.choice(_COMENTARIOS[idioma])}\n{visible}"
    r1 = requests.post(f"{GRAPH}/{cid}/comments", params={"access_token": token}, timeout=15,
                       data={"message": publico})
    r2 = requests.post(f"{GRAPH}/{cid}/likes", params={"access_token": token}, timeout=15)
    nombre = str(autor.get("name") or "").split(" ")[0]
    saludo = (f"Hola {nombre}, ¡podemos ayudarte! 📞" if idioma == "es" else f"Hi {nombre}, we can help! 📞").replace("  ", " ")
    r3 = requests.post(f"{GRAPH}/me/messages", params={"access_token": token}, timeout=15, json={
        "recipient": {"comment_id": cid}, "message": {"attachment": {"type": "template", "payload": {
            "template_type": "button", "text": saludo,
            "buttons": [{"type": "phone_number", "title": visible, "payload": tel}]}}}})
    def res(r):
        return "ok" if r.status_code == 200 else f"error {r.status_code}: {r.text[:120]}"
    print(f"[messenger] comentario respondido · público {res(r1)} · like {res(r2)} · privado {res(r3)}", flush=True)
    _metrica(page_id, "comentarios" if r1.status_code == 200 and r3.status_code == 200 else "errores_comentario")


# ── Métricas para la oficina 3D y el resumen diario (solo conteos, sin datos de pacientes) ──
PAGINAS = {
    "617088438163804": "ProCare - Dental Center", "579457641927025": "Dra Selena Humboldt",
    "615312844992080": "Dra. Olivia Garcia", "483631408161200": "Dentista Arlet Allerton",
    "198222373382362": "Dental Smile Clinic", "206710499198387": "Dr. Dentista Marina Wilson",
    "220499631147731": "PerfectSmile", "231985673330880": "Dental Care Professionals",
    "100806385416210": "Dentista Latino",
}
ETIQUETAS_MANUALES = ("Sugerir zip", "Pide humano", "Fuera de horario")
OFFSET_CCS = 4 * 3600  # Caracas = UTC-4, sin horario de verano


def _dia_ccs(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ts or time.time()) - OFFSET_CCS))


def _inicio_dia(dia: str) -> int:
    import calendar
    return calendar.timegm(time.strptime(dia, "%Y-%m-%d")) + OFFSET_CCS


def _metrica(page_id: str, clave: str, n: int = 1) -> None:
    try:
        with _cfg["get_conn"]() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fb_metricas (dia TEXT, page_id TEXT, clave TEXT, n INTEGER, "
                      "PRIMARY KEY (dia, page_id, clave))")
            c.execute("""INSERT INTO fb_metricas VALUES (?,?,?,?)
                         ON CONFLICT(dia, page_id, clave) DO UPDATE SET n = n + excluded.n""",
                      (_dia_ccs(), page_id, clave, n))
    except Exception as e:
        print(f"[messenger] métrica: {type(e).__name__}", flush=True)


def resumen(dia: str | None = None) -> dict:
    """Números del día (hora Caracas) por página: chats, idioma, respuestas, comentarios, etiquetas, errores."""
    dia = dia or _dia_ccs()
    t0, t1 = _inicio_dia(dia), _inicio_dia(dia) + 86400
    pags = {pid: {"nombre": n, "chats": 0, "es": 0, "en": 0, "etiquetas": {}} for pid, n in PAGINAS.items()}
    manuales = []
    with _cfg["get_conn"]() as c:
        for tabla in ("fb_metricas (dia TEXT, page_id TEXT, clave TEXT, n INTEGER, PRIMARY KEY (dia, page_id, clave))",
                      "fb_etiquetado (page_id TEXT, psid TEXT, nombre TEXT, ts INTEGER, PRIMARY KEY (page_id, psid, nombre))"):
            c.execute(f"CREATE TABLE IF NOT EXISTS {tabla}")
        for pid, clave, n in c.execute("SELECT page_id, clave, n FROM fb_metricas WHERE dia=?", (dia,)).fetchall():
            pags.setdefault(pid, {"nombre": pid, "chats": 0, "es": 0, "en": 0, "etiquetas": {}})[clave] = n
        for pid, idioma, n in c.execute("""SELECT page_id, idioma, COUNT(*) FROM fb_estado
                                           WHERE ult_usuario >= ? AND ult_usuario < ? GROUP BY page_id, idioma""",
                                        (t0, t1)).fetchall():
            if pid in pags:
                pags[pid]["chats"] += n
                if idioma in ("es", "en"):
                    pags[pid][idioma] += n
        for pid, nombre, n in c.execute("""SELECT page_id, nombre, COUNT(*) FROM fb_etiquetado
                                           WHERE ts >= ? AND ts < ? GROUP BY page_id, nombre""", (t0, t1)).fetchall():
            if pid in pags:
                pags[pid]["etiquetas"][nombre] = n
        for pid, psid, nombre, ts in c.execute(
                f"""SELECT page_id, psid, nombre, ts FROM fb_etiquetado WHERE ts >= ? AND nombre IN
                    ({",".join("?" * len(ETIQUETAS_MANUALES))}) ORDER BY ts DESC LIMIT 50""",
                (int(time.time()) - 48 * 3600, *ETIQUETAS_MANUALES)).fetchall():
            manuales.append({"page_id": pid, "pagina": PAGINAS.get(pid, pid), "etiqueta": nombre,
                             "chat": hashlib.sha256(psid.encode()).hexdigest()[:8],
                             "hora_caracas": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts - OFFSET_CCS))})
    claves = ("chats", "es", "en", "respuestas", "tarjetas", "recordatorios", "comentarios",
              "errores_envio", "errores_comentario", "dejado_a_otra_app")
    tot = {k: sum(p.get(k, 0) for p in pags.values()) for k in claves}
    tot["etiquetas"] = {}
    for p in pags.values():
        for k, v in p["etiquetas"].items():
            tot["etiquetas"][k] = tot["etiquetas"].get(k, 0) + v
    return {"dia": dia, "hora_caracas": time.strftime("%H:%M", time.gmtime(time.time() - OFFSET_CCS)),
            "totales": tot, "paginas": pags, "manuales": manuales}


def _enviar_a_oficina() -> None:
    url = (os.environ.get("OFICINA_URL") or "https://oficina-visual-production.up.railway.app") + "/api/ingesta/messenger"
    k = os.environ.get("CALLGRID_API_KEY", "")
    if not k:
        return
    firma = hmac.new(k.encode(), b"oficina-ingesta", hashlib.sha256).hexdigest()
    try:
        r = requests.post(url, json=resumen(), headers={"x-oficina-token": firma}, timeout=15)
        if r.status_code != 200:
            print(f"[messenger] oficina respondió {r.status_code}: {r.text[:120]}", flush=True)
    except Exception as e:
        print(f"[messenger] oficina: {type(e).__name__}", flush=True)


def _bucle_oficina() -> None:
    time.sleep(60)
    while True:
        _enviar_a_oficina()
        time.sleep(int(os.environ.get("OFICINA_CADA_SEG") or 600))


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


_AVISO_APERTURA = {
    "es": "¡Hola! 😊 Ya estamos disponibles. Llámanos y te conectamos con un dentista en tu área.",
    "en": "Hi! 😊 We're open now. Give us a call and we'll connect you with a dentist in your area.",
}


def _avisar_apertura() -> None:
    """Al abrir, escribe a quienes nos contactaron fuera de horario (solo dentro de la ventana de 24 h)."""
    ahora = int(time.time())
    with _cfg["get_conn"]() as c:
        filas = c.execute("""SELECT page_id, psid, idioma FROM fb_estado WHERE avisar = 1 AND pausado = 0
                             AND ult_usuario > ?""", (ahora - 23 * 3600 - 1800,)).fetchall()
        c.execute("UPDATE fb_estado SET avisar = 0 WHERE avisar = 1 AND ult_usuario <= ?", (ahora - 23 * 3600 - 1800,))
    for page_id, psid, idioma in filas:
        if _modo(page_id) != "responder" or _otra_app_reciente(page_id, psid):
            continue
        idioma = idioma if idioma in _AVISO_APERTURA else "es"
        enviado = _boton_llamar(page_id, psid, idioma, _AVISO_APERTURA[idioma])
        with _cfg["get_conn"]() as c:
            # el aviso de apertura reemplaza al recordatorio de 30 min (queda solo el de 22 h)
            c.execute("UPDATE fb_estado SET avisar = 0, recordatorios = MAX(recordatorios, 1), tarjeta = ? "
                      "WHERE page_id = ? AND psid = ?",
                      (ahora if enviado else 0, page_id, psid))
        _metrica(page_id, "avisos_apertura" if enviado else "errores_envio")
        print(f"[messenger] aviso de apertura a {hashlib.sha256(psid.encode()).hexdigest()[:8]} enviado={enviado}", flush=True)


def _atender_cola() -> None:
    """Al abrir: la IA contesta lo que llegó fuera de horario. Si ya pasaron las 24 h de Meta, el chat
    queda con la etiqueta «Fuera de horario» para que lo atienda un MB (un humano sí puede hasta 7 días)."""
    ahora = int(time.time())
    with _cfg["get_conn"]() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fb_cola (page_id TEXT, psid TEXT, texto TEXT, ts INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS fb_cola_coment (cid TEXT PRIMARY KEY, page_id TEXT, v TEXT, ts INTEGER)")
        filas = c.execute("SELECT page_id, psid, GROUP_CONCAT(texto, '\n'), MAX(ts) FROM fb_cola "
                          "GROUP BY page_id, psid").fetchall()
        c.execute("DELETE FROM fb_cola")
        comentarios = c.execute("SELECT cid, page_id, v, ts FROM fb_cola_coment").fetchall()
        c.execute("DELETE FROM fb_cola_coment")
    for page_id, psid, texto, ts in filas:
        quien = hashlib.sha256(psid.encode()).hexdigest()[:8]
        if ahora - ts < 23 * 3600 + 1800:
            print(f"[messenger] {quien}: respondiendo lo que llegó fuera de horario", flush=True)
            _contestar(page_id, psid, texto, f"cola-{ts}", forzar=True)
        else:
            _etiquetar_una_vez(page_id, psid, "Fuera de horario")
            _metrica(page_id, "fuera_horario_a_mano")
            print(f"[messenger] {quien}: pasaron más de 24 h — etiqueta «Fuera de horario» para un MB", flush=True)
    for cid, page_id, v, ts in comentarios:
        if ahora - ts < 6 * 86400:  # Meta permite respuesta privada a un comentario hasta 7 días
            _comentario(page_id, json.loads(v), forzar=True)


def _enviar_recordatorios() -> None:
    if not _cfg["abierto"]():
        return  # fuera de horario no se manda nada
    _avisar_apertura()  # quienes quedaron marcados con la versión anterior
    _atender_cola()
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
        _metrica(page_id, "recordatorios" if enviado else "errores_envio")
        with _cfg["get_conn"]() as c:
            c.execute("UPDATE fb_estado SET recordatorios = ? WHERE page_id = ? AND psid = ?",
                      (n + 1, page_id, psid))
        print(f"[messenger] recordatorio {n + 1} a {hashlib.sha256(psid.encode()).hexdigest()[:8]} "
              f"({idioma}) enviado={enviado}", flush=True)


ETIQUETA_MANUAL = os.environ.get("MESSENGER_ETIQUETA_MANUAL") or "Sugerir zip"


def _etiquetar(page_id: str, psid: str, nombre: str) -> bool:
    """Pone una etiqueta de la bandeja de Meta Business Suite al chat (para filtrarlo y atenderlo a mano)."""
    token = _token(page_id)
    if not token:
        return False
    try:
        with _cfg["get_conn"]() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fb_etiquetas (page_id TEXT, nombre TEXT, id TEXT, PRIMARY KEY (page_id, nombre))")
            f = c.execute("SELECT id FROM fb_etiquetas WHERE page_id=? AND nombre=?", (page_id, nombre)).fetchone()
        eid = f[0] if f else None
        if not eid:
            r = requests.get(f"{GRAPH}/me/custom_labels", params={"access_token": token, "fields": "page_label_name",
                                                                   "limit": 200}, timeout=10)
            eid = next((x["id"] for x in (r.json() or {}).get("data", []) if x.get("page_label_name") == nombre), None)
            if not eid:
                r = requests.post(f"{GRAPH}/me/custom_labels", params={"access_token": token}, timeout=10,
                                  json={"page_label_name": nombre})
                eid = (r.json() or {}).get("id")
                if not eid:
                    print(f"[messenger] no pude crear la etiqueta en {page_id}: {r.text[:200]}", flush=True)
                    return False
            with _cfg["get_conn"]() as c:
                c.execute("INSERT OR REPLACE INTO fb_etiquetas VALUES (?,?,?)", (page_id, nombre, str(eid)))
        r = requests.post(f"{GRAPH}/{eid}/label", params={"access_token": token}, timeout=10, json={"user": psid})
        if r.status_code != 200:
            print(f"[messenger] no pude etiquetar en {page_id}: {r.text[:200]}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[messenger] error etiqueta: {type(e).__name__}", flush=True)
        return False


def _etiquetar_una_vez(page_id: str, psid: str, nombre: str) -> bool:
    with _cfg["get_conn"]() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fb_etiquetado (page_id TEXT, psid TEXT, nombre TEXT, ts INTEGER, "
                  "PRIMARY KEY (page_id, psid, nombre))")
        if c.execute("SELECT 1 FROM fb_etiquetado WHERE page_id=? AND psid=? AND nombre=?",
                     (page_id, psid, nombre)).fetchone():
            return True
    ok = _etiquetar(page_id, psid, nombre)
    if ok:
        with _cfg["get_conn"]() as c:
            c.execute("INSERT OR IGNORE INTO fb_etiquetado VALUES (?,?,?,?)", (page_id, psid, nombre, int(time.time())))
        print(f"[messenger] etiqueta '{nombre}' · página {page_id} · chat {hashlib.sha256(psid.encode()).hexdigest()[:8]}",
              flush=True)
    return ok


ETIQUETAS_IDIOMA = {"es": "Español", "en": "Inglés"}


def _etiqueta_idioma(page_id: str, psid: str, res: dict) -> None:
    """Una sola etiqueta de idioma por chat, y solo cuando el idioma se detectó con seguridad."""
    lang = res.get("language")
    if lang not in ETIQUETAS_IDIOMA or res.get("language_source") == "default":
        return
    with _cfg["get_conn"]() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fb_etiquetado (page_id TEXT, psid TEXT, nombre TEXT, ts INTEGER, "
                  "PRIMARY KEY (page_id, psid, nombre))")
        ya = c.execute("SELECT 1 FROM fb_etiquetado WHERE page_id=? AND psid=? AND nombre IN (?,?)",
                       (page_id, psid, *ETIQUETAS_IDIOMA.values())).fetchone()
    if not ya:
        _etiquetar_una_vez(page_id, psid, ETIQUETAS_IDIOMA[lang])


def _pausa(res: dict) -> int:
    """1 = pidió que no le escriban (para siempre). Timestamp = pasado a un humano (la IA se aparta 24 h)."""
    if res.get("action") != "pause":
        return 0
    return int(time.time()) if res.get("response") else 1


def _contestar(page_id: str, psid: str, texto: str, mid: str, forzar: bool = False) -> None:
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
    if not forzar:
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
        _metrica(page_id, "dejado_a_otra_app")
        return
    if fila and fila[0] and (fila[0] == 1 or time.time() - fila[0] < 24 * 3600):
        print(f"[messenger] {quien}: conversación pausada, no respondo", flush=True)
        return  # pidió que no le escriban
    if not forzar and not _cfg["abierto"]():
        # Fuera de horario: no contestamos. El mensaje queda sin leer en la bandeja (el MB lo puede atender)
        # y la IA lo responde al abrir, si sigue dentro de las 24 h de Meta.
        with _cfg["get_conn"]() as c:
            c.execute("CREATE TABLE IF NOT EXISTS fb_cola (page_id TEXT, psid TEXT, texto TEXT, ts INTEGER)")
            c.execute("INSERT INTO fb_cola VALUES (?,?,?,?)", (page_id, psid, texto, int(time.time())))
        _metrica(page_id, "en_espera_fuera_horario")
        print(f"[messenger] {quien}: fuera de horario — en espera hasta abrir", flush=True)
        return
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
                  (page_id, psid, _pausa(res), 1 if aviso else 0,
                   int(time.time()), idioma))
    ok = _enviar(page_id, psid, res["response"]) if res.get("response") else None
    if ok is not None:
        _metrica(page_id, "respuestas" if ok else "errores_envio")
    for nombre in res.get("tags") or []:
        _etiquetar_una_vez(page_id, psid, nombre)
    _etiqueta_idioma(page_id, psid, res)
    if res.get("action") == "pause" and res.get("response"):
        etiquetado = _etiquetar_una_vez(page_id, psid, ETIQUETA_MANUAL)
        print(f"[messenger] NECESITA HUMANO · página {page_id} · chat {quien} · etiqueta={etiquetado}", flush=True)
    if ok and res.get("action") == "respond":
        if res.get("abierto", True):
            _programar_tarjeta(page_id, psid, idioma)
        else:
            # Fuera de horario: sin botón de llamar; le escribimos al abrir (si sigue dentro de las 24 h de Meta)
            # salvo que haya dicho que no quiere el aviso.
            no_quiere = bool(re.search(r"^\s*(no|no gracias|nah|no thanks|no thank you)\b", (texto or "").lower()))
            with _cfg["get_conn"]() as c:
                c.execute("UPDATE fb_estado SET avisar=? WHERE page_id=? AND psid=?", (0 if no_quiere else 1, page_id, psid))
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
            "hoy": resumen(), "ayer": resumen(_dia_ccs(time.time() - 86400)),
            "configurado": {"app_secret": bool(os.environ.get("META_APP_SECRET")),
                            "verify_token": bool(os.environ.get("MESSENGER_VERIFY_TOKEN")),
                            "paginas_con_token": list(_tokens().keys())}}


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

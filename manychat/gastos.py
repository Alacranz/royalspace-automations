"""
Bot de Telegram para registrar gastos personales en Notion (base "Gastos", página Personal).

Edwar manda al bot:
  • foto de la factura (en Bs, calculada a tasa BCV del día)  y/o
  • captura del Zelle de Amerant (en USD)                     y/o
  • texto: "23.50 zelle excelsior gama"
Claude Haiku lee la foto o el texto; si factura y Zelle llegan con menos de 15 min de diferencia
se juntan en el mismo gasto. La tasa BCV se toma de ve.dolarapi.com cuando falta uno de los montos.

Variables (Railway):
  TELEGRAM_GASTOS_TOKEN   token del bot (@BotFather)
  NOTION_TOKEN            secreto de la integración de Notion con acceso a la base Gastos
  GASTOS_DB_ID            id de la base (por defecto la creada el 2026-11-12)
  ANTHROPIC_API_KEY       ya existe
Solo atiende al primer chat que le escriba (queda guardado como dueño); los demás se ignoran.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time

import requests
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_cfg: dict = {}

TG = "https://api.telegram.org"
NOTION = "https://api.notion.com/v1"
NOTION_VER = "2022-06-28"
MODELO = "claude-haiku-4-5-20251001"
DB_ID = os.environ.get("GASTOS_DB_ID") or "11cafaab549f4bd98987a971283195d7"
CATEGORIAS = ["Supermercado", "Comida fuera", "Transporte y gasolina", "Casa y servicios", "Salud",
              "Equipos y música", "Suscripciones", "Ropa y personal", "Otros"]
METODOS = ["Zelle Amerant", "Tarjeta Amerant", "Efectivo USD", "Bolívares", "Otro"]
JUNTAR_SEG = 15 * 60
OFFSET_CCS = dt.timezone(dt.timedelta(hours=-4))


def _tok() -> str:
    return os.environ.get("TELEGRAM_GASTOS_TOKEN", "")


def _secreto() -> str:
    return hashlib.sha256(("gastos:" + _tok()).encode()).hexdigest()[:32]


def configurar(get_conn, base_url: str) -> None:
    _cfg.update(get_conn=get_conn, base_url=base_url.rstrip("/"))
    with get_conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS gastos_estado (clave TEXT PRIMARY KEY, valor TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS gastos_abiertos (chat TEXT PRIMARY KEY, page_id TEXT, ts INTEGER, "
                  "tiene_bs INTEGER, tiene_usd INTEGER, usd REAL, bs REAL)")
    if _tok():
        threading.Thread(target=_registrar_webhook, daemon=True).start()


def _registrar_webhook() -> None:
    time.sleep(5)
    url = f"{_cfg['base_url']}/gastos/telegram"
    try:
        r = requests.post(f"{TG}/bot{_tok()}/setWebhook", timeout=15,
                          json={"url": url, "secret_token": _secreto(), "allowed_updates": ["message"]})
        print(f"[gastos] webhook de Telegram: {r.json().get('description', r.status_code)}", flush=True)
    except Exception as e:
        print(f"[gastos] no pude registrar el webhook: {type(e).__name__}", flush=True)


# ── Estado (dueño del bot, último gasto abierto) ────────────────────────────────────────────────
def _get(clave):
    with _cfg["get_conn"]() as c:
        f = c.execute("SELECT valor FROM gastos_estado WHERE clave=?", (clave,)).fetchone()
    return f[0] if f else None


def _set(clave, valor):
    with _cfg["get_conn"]() as c:
        c.execute("INSERT OR REPLACE INTO gastos_estado VALUES (?,?)", (clave, valor))


# ── Telegram ────────────────────────────────────────────────────────────────────────────────────
def _decir(chat, texto):
    try:
        requests.post(f"{TG}/bot{_tok()}/sendMessage", timeout=15,
                      json={"chat_id": chat, "text": texto, "parse_mode": "HTML"})
    except Exception:
        pass


def _descargar_foto(file_id) -> bytes:
    r = requests.get(f"{TG}/bot{_tok()}/getFile", params={"file_id": file_id}, timeout=15).json()
    ruta = r["result"]["file_path"]
    return requests.get(f"{TG}/file/bot{_tok()}/{ruta}", timeout=30).content


# ── Claude: leer factura / captura / texto ──────────────────────────────────────────────────────
PROMPT = f"""Extrae un gasto personal y responde SOLO un JSON con estas claves:
{{"tipo": "factura"|"zelle"|"texto"|"otro",
  "comercio": "nombre corto del comercio o destinatario",
  "fecha": "YYYY-MM-DD o null",
  "monto_usd": número o null,     // lo pagado en dólares (captura de Zelle o lo que diga el texto en $)
  "monto_bs": número o null,      // TOTAL de la factura en bolívares (Bs), número sin separadores de miles
  "tasa": número o null,          // tasa Bs/USD si aparece impresa en la factura
  "categoria": una de {CATEGORIAS},
  "metodo": una de {METODOS} o null,
  "nota": "detalle breve útil (ej. productos principales) o vacío"}}
Reglas: en Venezuela la coma suele ser decimal y el punto de miles ("1.234,56" = 1234.56).
Una captura de Zelle de Amerant → tipo "zelle", metodo "Zelle Amerant", monto_usd, comercio = destinatario.
Si el texto no dice método, usa null. No inventes montos que no veas."""


def _claude(contenido) -> dict:
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
        "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01",
        "content-type": "application/json"}, json={
        "model": MODELO, "max_tokens": 400, "system": PROMPT,
        "messages": [{"role": "user", "content": contenido}]})
    texto = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", texto, re.S)
    return json.loads(m.group(0)) if m else {}


def _leer_foto(img: bytes, pie: str) -> dict:
    return _claude([
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(img).decode()}},
        {"type": "text", "text": pie or "Lee esta imagen."}])


# ── Tasa BCV ────────────────────────────────────────────────────────────────────────────────────
def _tasa_bcv() -> float | None:
    hoy = dt.datetime.now(OFFSET_CCS).date().isoformat()
    guardada = _get(f"bcv:{hoy}")
    if guardada:
        return float(guardada)
    try:
        d = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=15).json()
        t = float(d.get("promedio") or d.get("venta") or 0)
        if t > 0:
            _set(f"bcv:{hoy}", str(t))
            return t
    except Exception as e:
        print(f"[gastos] tasa BCV no disponible: {type(e).__name__}", flush=True)
    return None


# ── Notion ──────────────────────────────────────────────────────────────────────────────────────
def _nh():
    return {"Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}", "Notion-Version": NOTION_VER,
            "Content-Type": "application/json"}


def _subir_archivo(img: bytes, nombre: str) -> str | None:
    try:
        r = requests.post(f"{NOTION}/file_uploads", headers=_nh(), timeout=20,
                          json={"filename": nombre, "content_type": "image/jpeg"}).json()
        h = {k: v for k, v in _nh().items() if k != "Content-Type"}
        s = requests.post(f"{NOTION}/file_uploads/{r['id']}/send", headers=h, timeout=60,
                          files={"file": (nombre, img, "image/jpeg")})
        return r["id"] if s.status_code == 200 else None
    except Exception as e:
        print(f"[gastos] no pude subir la factura: {type(e).__name__}", flush=True)
        return None


def _props(g: dict, origen: str, archivo: str | None) -> dict:
    p = {"Origen": {"select": {"name": origen}}}
    if g.get("comercio"):
        p["Comercio"] = {"title": [{"text": {"content": str(g["comercio"])[:100]}}]}
    if g.get("fecha"):
        p["Fecha"] = {"date": {"start": g["fecha"]}}
    for campo, prop in (("monto_usd", "Monto USD"), ("monto_bs", "Monto Bs"), ("tasa", "Tasa BCV")):
        if g.get(campo) is not None:
            p[prop] = {"number": round(float(g[campo]), 2)}
    if g.get("categoria") in CATEGORIAS:
        p["Categoría"] = {"select": {"name": g["categoria"]}}
    if g.get("metodo") in METODOS:
        p["Método"] = {"select": {"name": g["metodo"]}}
    if g.get("nota"):
        p["Nota"] = {"rich_text": [{"text": {"content": str(g["nota"])[:500]}}]}
    if archivo:
        p["Factura"] = {"files": [{"type": "file_upload", "file_upload": {"id": archivo}, "name": "factura.jpg"}]}
    return p


def _crear(g, origen, archivo) -> str | None:
    r = requests.post(f"{NOTION}/pages", headers=_nh(), timeout=20,
                      json={"parent": {"database_id": DB_ID}, "properties": _props(g, origen, archivo)})
    if r.status_code != 200:
        print(f"[gastos] Notion rechazó el gasto: {r.text[:200]}", flush=True)
        return None
    return r.json()["id"]


def _actualizar(page_id, g, origen, archivo) -> bool:
    props = _props(g, origen, archivo)
    if origen != "Factura":
        props.pop("Origen", None)  # la factura manda como origen
    r = requests.patch(f"{NOTION}/pages/{page_id}", headers=_nh(), timeout=20, json={"properties": props})
    return r.status_code == 200


# ── Lógica de un gasto ──────────────────────────────────────────────────────────────────────────
def _completar(g: dict) -> dict:
    g = {k: v for k, v in g.items() if v not in ("", None)}
    g.setdefault("fecha", dt.datetime.now(OFFSET_CCS).date().isoformat())
    usd, bs = g.get("monto_usd"), g.get("monto_bs")
    if usd and bs and not g.get("tasa"):
        g["tasa"] = float(bs) / float(usd)
    elif (usd or bs) and not g.get("tasa"):
        t = _tasa_bcv()
        if t:
            g["tasa"] = t
    if usd and not bs and g.get("tasa"):
        g["monto_bs"] = float(usd) * float(g["tasa"])
    if bs and not usd and g.get("tasa"):
        g["monto_usd"] = float(bs) / float(g["tasa"])
    return g


def _fmt(g):
    usd = f"${float(g['monto_usd']):,.2f}" if g.get("monto_usd") else "—"
    bs = f"Bs {float(g['monto_bs']):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if g.get("monto_bs") else "—"
    tasa = f" (tasa {float(g['tasa']):.2f})" if g.get("tasa") else ""
    return f"<b>{g.get('comercio', 'Gasto')}</b> · {usd} · {bs}{tasa} · {g.get('categoria', 'Otros')}"


def _procesar(chat: str, msg: dict) -> None:
    try:
        texto = (msg.get("caption") or msg.get("text") or "").strip()
        if texto.lower() in ("/resumen", "resumen"):
            return _decir(chat, _resumen_mes())
        if texto.lower() in ("/deshacer", "deshacer", "borra el último", "borrar ultimo"):
            return _deshacer(chat)
        if texto.startswith("/start"):
            return _decir(chat, "👋 Listo. Mándame la <b>foto de la factura</b>, la <b>captura del Zelle</b> "
                                "o escribe algo como <i>23.50 zelle excelsior</i>. Comandos: /resumen y /deshacer.")
        img = archivo = None
        if msg.get("photo"):
            img = _descargar_foto(msg["photo"][-1]["file_id"])
            g = _leer_foto(img, texto)
        elif texto:
            g = _claude(texto)
            g["tipo"] = "texto"
        else:
            return
        if not (g.get("monto_usd") or g.get("monto_bs")):
            return _decir(chat, "🤔 No encontré un monto. Prueba con otra foto o escríbelo, ej: <i>23.50 zelle farmatodo</i>.")
        origen = {"factura": "Factura", "zelle": "Captura Zelle"}.get(g.get("tipo"), "Texto")
        if img and origen == "Factura":
            archivo = _subir_archivo(img, f"factura-{int(time.time())}.jpg")

        # ¿Completa el último gasto (factura + Zelle de la misma compra)?
        with _cfg["get_conn"]() as c:
            ab = c.execute("SELECT page_id, ts, tiene_bs, tiene_usd, usd, bs FROM gastos_abiertos WHERE chat=?",
                           (chat,)).fetchone()
        nuevo_bs, nuevo_usd = bool(g.get("monto_bs")), bool(g.get("monto_usd")) and g.get("tipo") == "zelle"
        if ab and time.time() - ab[1] < JUNTAR_SEG and ((nuevo_bs and not ab[2]) or (nuevo_usd and not ab[3])):
            if ab[4] and not g.get("monto_usd"):
                g["monto_usd"] = ab[4]
            if ab[5] and not g.get("monto_bs"):
                g["monto_bs"] = ab[5]
            g.pop("tasa", None)
            g = _completar(g)
            if g.get("tipo") == "zelle":  # el comercio/categoría de la factura es mejor que el destinatario Zelle
                g.pop("comercio", None); g.pop("categoria", None)
            ok = _actualizar(ab[0], g, origen, archivo)
            with _cfg["get_conn"]() as c:
                c.execute("DELETE FROM gastos_abiertos WHERE chat=?", (chat,))
            return _decir(chat, ("🔗 Lo junté con el gasto anterior:\n" + _fmt(g)) if ok else "⚠️ No pude actualizar Notion.")

        g = _completar(g)
        pid = _crear(g, origen, archivo)
        if not pid:
            return _decir(chat, "⚠️ No pude guardar en Notion. Revisa que la integración tenga acceso a la base Gastos.")
        with _cfg["get_conn"]() as c:
            c.execute("INSERT OR REPLACE INTO gastos_abiertos VALUES (?,?,?,?,?,?,?)",
                      (chat, pid, int(time.time()), int(origen == "Factura" or g.get("tipo") == "texto" and bool(g.get("monto_bs"))),
                       int(origen == "Captura Zelle"), g.get("monto_usd"), g.get("monto_bs")))
        _set(f"ultimo:{chat}", pid)
        extra = ""
        if origen == "Factura":
            extra = "\n<i>Si pagaste con Zelle, mándame la captura y lo junto.</i>"
        elif origen == "Captura Zelle":
            extra = "\n<i>Mándame la foto de la factura para agregar el comercio y los Bs.</i>"
        _decir(chat, "✅ Guardado:\n" + _fmt(g) + extra)
    except Exception as e:
        print(f"[gastos] error: {type(e).__name__}: {e}", flush=True)
        _decir(chat, "⚠️ Algo falló al registrar ese gasto. Intenta de nuevo en un momento.")


def _deshacer(chat):
    pid = _get(f"ultimo:{chat}")
    if not pid:
        return _decir(chat, "No hay un gasto reciente para borrar.")
    r = requests.patch(f"{NOTION}/pages/{pid}", headers=_nh(), timeout=20, json={"archived": True})
    _set(f"ultimo:{chat}", "")
    with _cfg["get_conn"]() as c:
        c.execute("DELETE FROM gastos_abiertos WHERE chat=?", (chat,))
    _decir(chat, "🗑️ Borré el último gasto." if r.status_code == 200 else "⚠️ No pude borrarlo en Notion.")


def _resumen_mes() -> str:
    hoy = dt.datetime.now(OFFSET_CCS).date()
    inicio = hoy.replace(day=1).isoformat()
    filas, cursor = [], None
    while True:
        body = {"filter": {"property": "Fecha", "date": {"on_or_after": inicio}}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"{NOTION}/databases/{DB_ID}/query", headers=_nh(), timeout=20, json=body).json()
        filas += r.get("results", [])
        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")
    por_cat, total = {}, 0.0
    for f in filas:
        p = f["properties"]
        usd = (p.get("Monto USD") or {}).get("number") or 0
        cat = ((p.get("Categoría") or {}).get("select") or {}).get("name") or "Otros"
        por_cat[cat] = por_cat.get(cat, 0) + usd
        total += usd
    lineas = [f"📊 <b>{hoy.strftime('%m/%Y')}</b>: ${total:,.2f} en {len(filas)} gastos"]
    lineas += [f"• {c}: ${v:,.2f}" for c, v in sorted(por_cat.items(), key=lambda x: -x[1])]
    return "\n".join(lineas)


# ── Webhook ─────────────────────────────────────────────────────────────────────────────────────
@router.post("/gastos/telegram")
async def webhook(request: Request, tareas: BackgroundTasks):
    if not _tok() or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != _secreto():
        return JSONResponse({"ok": False}, status_code=401)
    upd = await request.json()
    msg = upd.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id") or "")
    if not chat:
        return {"ok": True}
    dueno = _get("dueno")
    if not dueno:
        _set("dueno", chat)
        dueno = chat
        print("[gastos] dueño del bot registrado", flush=True)
    if chat != dueno:
        return {"ok": True}  # cualquier otra persona se ignora
    tareas.add_task(_procesar, chat, msg)
    return {"ok": True}

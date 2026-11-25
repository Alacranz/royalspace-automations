"""
Revisión periódica del bot de Messenger (el "revisor").

GET /messenger/revision?dias=3&analizar=1   (header X-Api-Key = STATS_API_KEY)

Junta en un solo JSON todo lo que hace falta para saber si el bot está funcionando bien y dónde se
pierden llamadas, sin exponer datos de pacientes:
  - números del período (respuestas, tarjetas, recordatorios 1/2/3, errores de envío, de la IA, del botón,
    etiquetas que fallaron, chats que pasaron a un humano y cuántos de esos atendió alguien)
  - salud del sistema: tokens de cada página, token de usuario de Meta, etiquetas duplicadas, pendientes
  - calidad de las respuestas: reglas fijas (¿el bot recuerda marcar el ZIP al mandar a llamar?) y una
    lectura de una muestra de conversaciones con Claude Haiku, que devuelve hallazgos y mejoras
    (los números de teléfono y correos se tapan antes de mandar el texto)

Lo consume monitoring/messenger_review.py (workflow messenger_review.yml, L-M-V), que agrega lo del IVR de
CallGrid y manda el reporte a Discord.
"""
from __future__ import annotations

import json
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request

try:
    from manychat import messenger_direct as md
except ImportError:  # uvicorn corriendo desde manychat/
    import messenger_direct as md

router = APIRouter()

CLAVES = ("respuestas", "tarjetas", "recordatorios", "recordatorio_1", "recordatorio_2", "recordatorio_3",
          "avisos_apertura", "errores_envio", "no_disponible", "error_ia", "error_boton", "etiqueta_fallo",
          "etiqueta_reintento_ok", "handoff_zip", "comentarios", "errores_comentario", "en_espera_fuera_horario",
          "fuera_horario_a_mano", "dejado_a_otra_app",
          "sin_boton_fuera_de_servicio", "sin_boton_pidiendo_zip", "sin_boton_ya_resuelto")

_TEL = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_MAIL = re.compile(r"\S+@\S+\.\S+")
_LLAMAR = re.compile(r"\b(llam|call|marc|bot[oó]n|button)", re.I)
_ZIP = re.compile(r"(c[oó]digo postal|zip)", re.I)
_SIN_RESPUESTA = re.compile(r"(nadie (me )?contest|no (me )?contest|no (me )?atiend|se cort|colg|no (hay|tienen) "
                            r"(clinica|cobertura)|no answer|nobody answer|no one answer|hung up|got disconnected|"
                            r"call dropped)", re.I)


def _tapar(texto: str) -> str:
    return _MAIL.sub("[correo]", _TEL.sub("[tel]", texto or ""))


def _dias(dias: int) -> list[str]:
    return [md._dia_ccs(time.time() - i * 86400) for i in range(dias)]


def _numeros(c, dias: list[str]) -> dict:
    marcas = ",".join("?" * len(dias))
    tot = {k: 0 for k in CLAVES}
    por_pagina: dict = {}
    try:
        filas = c.execute(f"SELECT page_id, clave, SUM(n) FROM fb_metricas WHERE dia IN ({marcas}) "
                          f"GROUP BY page_id, clave", dias).fetchall()
    except Exception:
        filas = []
    for pid, clave, n in filas:
        tot[clave] = tot.get(clave, 0) + (n or 0)
        por_pagina.setdefault(md.PAGINAS.get(pid, pid), {})[clave] = n or 0
    return {"totales": tot, "por_pagina": por_pagina}


def _handoffs(c, desde: int) -> dict:
    """Chats que la IA pasó a un humano ('Sugerir zip') y cuántos tuvieron respuesta de una persona después
    (un mensaje enviado desde la bandeja de Meta queda registrado como 'otra app' en fb_estado)."""
    try:
        filas = c.execute("""SELECT e.page_id, e.ts, COALESCE(s.otra_app, 0) FROM fb_etiquetado e
                             LEFT JOIN fb_estado s ON s.page_id = e.page_id AND s.psid = e.psid
                             WHERE e.nombre = ? AND e.ts >= ?""", (md.ETIQUETA_MANUAL, desde)).fetchall()
    except Exception:
        filas = []
    atendidos = sum(1 for _, ts, otra in filas if otra and otra >= ts)
    por_pagina: dict = {}
    for pid, ts, otra in filas:
        p = por_pagina.setdefault(md.PAGINAS.get(pid, pid), {"total": 0, "sin_atender": 0})
        p["total"] += 1
        if not (otra and otra >= ts):
            p["sin_atender"] += 1
    return {"total": len(filas), "atendidos_por_humano": atendidos, "sin_atender": len(filas) - atendidos,
            "por_pagina": por_pagina}


def _sistema(c) -> dict:
    out: dict = {"tokens": {}, "etiquetas_duplicadas": [], "etiquetas_pendientes": 0}
    for pid, tok in md._tokens().items():
        if pid in md.PAGINAS:
            out["tokens"][md.PAGINAS[pid]] = md._chequear_token(pid, tok)
    try:
        f = c.execute("SELECT valor, ts FROM fb_sistema WHERE clave = 'meta_user_token'").fetchone()
        if f:
            out["meta_user_token"] = f[0]
    except Exception:
        pass
    try:
        out["etiquetas_duplicadas"] = [{"pagina": md.PAGINAS.get(p, p), "etiqueta": n, "copias": k}
                                       for p, n, k in c.execute("SELECT page_id, nombre, copias FROM fb_etiquetas_dup")]
    except Exception:
        pass
    try:
        out["etiquetas_pendientes"] = c.execute("SELECT COUNT(*) FROM fb_etiquetas_pendientes").fetchone()[0]
    except Exception:
        pass
    return out


def _conversaciones(c, desde: int) -> dict:
    """Reglas fijas sobre los textos + la muestra para la lectura con IA."""
    desde_sql = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(desde))
    filas = c.execute("""SELECT subscriber_id, role, content, created_at FROM messages
                         WHERE subscriber_id LIKE 'fb:%' AND created_at >= ? ORDER BY subscriber_id, created_at""",
                      (desde_sql,)).fetchall()
    chats: dict = {}
    for sid, rol, texto, ts in filas:
        chats.setdefault(sid, []).append((rol, texto or ""))
    cta = cta_zip = sin_respuesta = solo_uno = 0
    largos = []
    for msgs in chats.values():
        usuario = [t for r, t in msgs if r == "user"]
        if len(usuario) == 1:
            solo_uno += 1
        sin_respuesta += sum(1 for t in usuario if _SIN_RESPUESTA.search(t))
        for r, t in msgs:
            if r == "assistant" and t:
                largos.append(len(t))
                if _LLAMAR.search(t):
                    cta += 1
                    if _ZIP.search(t):
                        cta_zip += 1
    # Muestra para la IA: las conversaciones más largas primero (donde más cosas pasan) y algunas cortas.
    orden = sorted(chats.items(), key=lambda kv: len(kv[1]), reverse=True)
    muestra = orden[:28] + orden[-12:] if len(orden) > 40 else orden
    return {
        "chats": len(chats),
        "mensajes_paciente": sum(1 for _, r, _, _ in filas if r == "user"),
        "chats_con_un_solo_mensaje": solo_uno,
        "respuestas_que_mandan_a_llamar": cta,
        "de_esas_recuerdan_el_zip": cta_zip,
        "pct_recuerdan_zip": round(100 * cta_zip / cta, 1) if cta else None,
        "pacientes_que_dicen_que_no_les_contestan": sin_respuesta,
        "largo_promedio_respuesta": round(sum(largos) / len(largos)) if largos else 0,
        "_muestra": [[(r, _tapar(t)[:500]) for r, t in msgs[-14:]] for _, msgs in muestra],
    }


_PROMPT = """Eres el revisor de calidad de un chat de Facebook Messenger que atiende a pacientes hispanos y
angloparlantes en EE.UU. que buscan dentista. El objetivo del chat es UNO solo: que la persona LLAME por teléfono
(nunca se agenda por chat, nunca se piden datos personales, nunca se revela que es un bot).

Flujo real de la llamada: (1) idioma: presionar 1 para español; (2) marcar el código postal de 5 dígitos
— si no lo marcan, la llamada se pierde; (3) algunas clínicas preguntan por Medicaid: deben responder "No" o 2.

Lee la muestra de conversaciones (los teléfonos y correos están tapados) y devuelve SOLO un JSON con esta forma:
{"hallazgos": [{"problema": "...", "veces": N, "gravedad": "alta|media|baja", "ejemplo": "paráfrasis corta, sin
datos personales", "solucion": "cambio concreto en el prompt o en el flujo"}],
 "objeciones_frecuentes": ["...", "..."],
 "ideas_para_mas_llamadas": ["...", "..."],
 "resumen": "2-3 frases"}

Busca en especial: respuestas que mandan a llamar SIN recordar marcar el código postal; mezcla de idiomas;
saludos repetidos; respuestas largas; preguntas innecesarias antes de mandar a llamar; precios, seguros o datos
inventados; mención de ser bot; pacientes que dicen que llamaron y nadie contestó o se cortó; confusión con el
menú telefónico; personas fuera de EE.UU.; objeciones (precio, Medicaid, desconfianza, "es estafa") que el bot
no manejó bien; conversaciones donde la persona quería llamar y algo lo impidió. Máximo 8 hallazgos, ordenados
por impacto en la cantidad de llamadas. Escribe en español."""


def _leer_con_ia(muestra: list) -> dict:
    clave = os.environ.get("ANTHROPIC_API_KEY", "")
    if not clave or not muestra:
        return {"error": "sin ANTHROPIC_API_KEY o sin conversaciones"}
    texto = "\n\n".join(
        f"--- Conversación {i + 1} ---\n" + "\n".join(f"{'PACIENTE' if r == 'user' else 'BOT'}: {t}" for r, t in conv)
        for i, conv in enumerate(muestra))
    try:
        import anthropic
        cli = anthropic.Anthropic(api_key=clave)
        r = cli.messages.create(model="claude-haiku-4-5-20251001", max_tokens=2500, system=_PROMPT,
                                messages=[{"role": "user", "content": texto[:120_000]}])
        crudo = r.content[0].text.strip()
        crudo = crudo[crudo.find("{"): crudo.rfind("}") + 1]
        return json.loads(crudo)
    except Exception as e:
        return {"error": f"lectura con IA falló ({type(e).__name__})"}


def revision(dias: int = 3, analizar: bool = True) -> dict:
    dias = max(1, min(int(dias), 14))
    desde = int(time.time()) - dias * 86400
    with md._cfg["get_conn"]() as c:
        numeros = _numeros(c, _dias(dias))
        handoffs = _handoffs(c, desde)
        sistema = _sistema(c)
        conv = _conversaciones(c, desde)
    muestra = conv.pop("_muestra")
    return {"generado": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "dias": dias,
            "numeros": numeros, "handoffs": handoffs, "sistema": sistema, "conversaciones": conv,
            "calidad": _leer_con_ia(muestra) if analizar else None}


@router.get("/messenger/revision")
async def revision_endpoint(request: Request, dias: int = 3, analizar: int = 1):
    clave = os.environ.get("STATS_API_KEY", "")
    if not clave or request.headers.get("X-Api-Key", "") != clave:
        raise HTTPException(status_code=401, detail="unauthorized")
    import asyncio
    return await asyncio.to_thread(revision, dias, bool(analizar))

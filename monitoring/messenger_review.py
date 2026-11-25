"""
Revisor del bot de Messenger — Royalspace (lunes, miércoles y viernes).

1. Pide al bot (Railway) su revisión: /messenger/revision → números, salud y lectura de conversaciones.
2. Agrega cuántos llamantes se pierden en el IVR de CallGrid (no eligen idioma o no marcan el ZIP).
3. Manda el reporte a Discord (#mod) para decidir qué arreglar.

Lo dispara el scheduler del bot (manychat/main.py) con workflow_dispatch; también se puede correr a mano.
Secrets: WEBHOOK_STATS_URL, STATS_API_KEY, CALLGRID_API_KEY, CALLGRID_ORG_ID, DISCORD_WEBHOOK_MOD.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytz
import requests

VET = pytz.timezone("America/Caracas")
EST = pytz.timezone("America/New_York")
MAX = 1900


def _dias_por_defecto() -> int:
    # Lunes cubre vie-sáb-dom; miércoles y viernes cubren los 2 días anteriores.
    return 3 if datetime.now(VET).weekday() == 0 else 2


def revision_del_bot(dias: int) -> dict:
    base = (os.environ.get("WEBHOOK_STATS_URL") or "https://royalspace-automations-production.up.railway.app/stats")
    base = base.rsplit("/stats", 1)[0]
    r = requests.get(f"{base}/messenger/revision", params={"dias": dias, "analizar": 1},
                     headers={"X-Api-Key": os.environ.get("STATS_API_KEY", "")}, timeout=240)
    r.raise_for_status()
    return r.json()


def _callgrid(dias: int, reglas: list[dict]) -> dict:
    """Totales de CallGrid (reports/stats) con filtros por tags. Devuelve {llamadas, unicos}."""
    hoy = datetime.now(EST).date()
    body = {"startDate": str(hoy - timedelta(days=dias - 1)), "endDate": str(hoy), "pivot": "CampaignName",
            "pivot2": "", "filters": {"items": [{"rules": [r], "operator": "AND"} for r in reglas]},
            "permission": "", "page": 0, "maxItems": 100, "reportTimeZone": "US/Eastern"}
    r = requests.post("https://api.callgrid.com/api/reports/stats",
                      params={"organizationId": os.environ["CALLGRID_ORG_ID"]},
                      headers={"Authorization": f"Bearer {os.environ['CALLGRID_API_KEY']}"}, json=body, timeout=60)
    r.raise_for_status()
    pie = r.json().get("footerTotals") or {}
    return {"llamadas": int(pie.get("totalCalls") or 0), "unicos": int(pie.get("unique_ended") or 0)}


def ivr_callgrid(dias: int) -> dict:
    """Dónde se pierden los llamantes antes de llegar a un comprador."""
    total = _callgrid(dias, [])
    sin_idioma = _callgrid(dias, [{"tagName": "KeyPress", "condition": "isEmpty", "values": []}])
    sin_zip = _callgrid(dias, [{"tagName": "KeyPress", "condition": "isNotEmpty", "values": []},
                               {"tagName": "IVRZip", "condition": "isEmpty", "values": []},
                               {"tagName": "VisitorZipCode", "condition": "isEmpty", "values": []}])
    return {"total": total, "sin_idioma": sin_idioma, "sin_zip": sin_zip}


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


def armar_mensajes(rev: dict, ivr: dict | None, dias: int) -> list[str]:
    n = rev["numeros"]["totales"]
    h = rev["handoffs"]
    conv = rev["conversaciones"]
    sis = rev["sistema"]
    hoy = datetime.now(VET).strftime("%d-%b")
    alertas: list[str] = []

    # ── Señales que piden acción ──
    if conv.get("pct_recuerdan_zip") is not None and conv["pct_recuerdan_zip"] < 80:
        alertas.append(f"🔴 Solo {conv['pct_recuerdan_zip']}% de las respuestas que mandan a llamar recuerdan marcar el ZIP")
    if ivr and ivr["total"]["unicos"]:
        pz = ivr["sin_zip"]["unicos"] / ivr["total"]["unicos"]
        if pz >= 0.10:
            alertas.append(f"🔴 {ivr['sin_zip']['unicos']} llamantes ({_pct(ivr['sin_zip']['unicos'], ivr['total']['unicos'])}) "
                           f"no marcaron el ZIP en el IVR")
    if h["sin_atender"]:
        alertas.append(f"🟠 {h['sin_atender']} de {h['total']} chats pasados a un humano (Sugerir zip) siguen sin respuesta")
    if n.get("error_ia"):
        alertas.append(f"🔴 La IA falló {n['error_ia']} veces (esos pacientes no recibieron respuesta)")
    if n.get("errores_envio") or n.get("error_boton"):
        alertas.append(f"🟠 Errores al enviar: {n.get('errores_envio', 0)} mensajes · {n.get('error_boton', 0)} botones de llamar")
    if n.get("etiqueta_fallo"):
        alertas.append(f"🟡 {n['etiqueta_fallo']} etiquetas no se pudieron poner (reintentadas OK: {n.get('etiqueta_reintento_ok', 0)})")
    malos = {p: e for p, e in (sis.get("tokens") or {}).items() if e != "OK"}
    for p, e in malos.items():
        alertas.append(f"🔴 Token de {p}: {e}")
    if sis.get("meta_user_token") and sis["meta_user_token"] != "ok":
        alertas.append(f"🟡 Token de usuario de Meta: {sis['meta_user_token']} (sirve para renovar los de página)")
    if sis.get("etiquetas_duplicadas"):
        d = ", ".join(f"{x['pagina']}: {x['etiqueta']}×{x['copias']}" for x in sis["etiquetas_duplicadas"][:6])
        alertas.append(f"🟡 Etiquetas duplicadas en la bandeja ({d}) — el bot usa la más antigua")

    m1 = [f"🤖 **Revisión del bot de Messenger** · {hoy} · últimos {dias} días", ""]
    m1 += alertas or ["🟢 Sin fallas importantes."]
    m1 += ["", "```",
           f"Chats              {conv['chats']:>6}   (solo 1 mensaje: {conv['chats_con_un_solo_mensaje']})",
           f"Respuestas         {n.get('respuestas', 0):>6}   largo prom. {conv['largo_promedio_respuesta']} car.",
           f"Botón de llamar    {n.get('tarjetas', 0):>6}",
           f"Recordatorios      {n.get('recordatorios', 0):>6}   (30m {n.get('recordatorio_1', 0)} · 3h {n.get('recordatorio_2', 0)} · 22h {n.get('recordatorio_3', 0)})",
           f"Avisos de apertura {n.get('avisos_apertura', 0):>6}",
           f"Pasados a humano   {h['total']:>6}   atendidos {h['atendidos_por_humano']}",
           f"Dicen 'no contestan' {conv['pacientes_que_dicen_que_no_les_contestan']:>4}",
           f"Recuerdan el ZIP   {conv['de_esas_recuerdan_el_zip']:>4}/{conv['respuestas_que_mandan_a_llamar']}"]
    if ivr:
        t = ivr["total"]["unicos"]
        m1 += [f"IVR CallGrid       {t:>6} llamantes · sin idioma {ivr['sin_idioma']['unicos']} "
               f"({_pct(ivr['sin_idioma']['unicos'], t)}) · sin ZIP {ivr['sin_zip']['unicos']} ({_pct(ivr['sin_zip']['unicos'], t)})"]
    m1.append("```")

    mensajes = ["\n".join(m1)]
    cal = rev.get("calidad") or {}
    if cal.get("error"):
        mensajes.append(f"⚠️ Lectura de conversaciones: {cal['error']}")
    elif cal:
        m2 = ["🔎 **Lo que vio el revisor en las conversaciones**", cal.get("resumen", ""), ""]
        for x in cal.get("hallazgos", [])[:8]:
            icono = {"alta": "🔴", "media": "🟠"}.get(x.get("gravedad"), "🟡")
            m2.append(f"{icono} **{x.get('problema', '')}** ({x.get('veces', '?')}×) — {x.get('ejemplo', '')}\n"
                      f"   ➜ {x.get('solucion', '')}")
        if cal.get("objeciones_frecuentes"):
            m2 += ["", "**Objeciones frecuentes:** " + " · ".join(cal["objeciones_frecuentes"][:6])]
        if cal.get("ideas_para_mas_llamadas"):
            m2 += ["", "**Ideas para más llamadas:**"] + [f"• {i}" for i in cal["ideas_para_mas_llamadas"][:5]]
        mensajes.append("\n".join(m2))
    return mensajes


def _partir(texto: str) -> list[str]:
    partes, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > MAX:
            partes.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        partes.append(actual)
    return partes


def main() -> int:
    dias = int(os.environ.get("DIAS") or 0) or _dias_por_defecto()
    try:
        rev = revision_del_bot(dias)
    except Exception as e:
        rev = None
        error = f"🔴 **Revisor del bot de Messenger**: no pude leer la revisión del bot ({type(e).__name__}). " \
                f"¿Está caído el servicio royalspace-automations en Railway?"
    ivr = None
    try:
        ivr = ivr_callgrid(dias)
    except Exception as e:
        print(f"CallGrid IVR: {type(e).__name__}: {e}")
    mensajes = armar_mensajes(rev, ivr, dias) if rev else [error]
    webhook = os.environ.get("DISCORD_WEBHOOK_MOD", "")
    for m in mensajes:
        for parte in _partir(m):
            print(parte)
            if webhook:
                requests.post(webhook, json={"content": parte}, timeout=15).raise_for_status()
    return 0 if rev else 1


if __name__ == "__main__":
    sys.exit(main())

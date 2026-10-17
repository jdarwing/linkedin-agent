"""
notifier.py — Correo de consulta "un toque" y lectura segura de respuestas
==========================================================================
Envía a Darwing un correo con los perfiles "consult". Cada perfil trae dos botones
(✅ Aceptar / ❌ Ignorar). Cada botón abre un correo ya escrito, dirigido al agente,
con un CÓDIGO ÚNICO de ese perfil en el asunto: Darwing solo toca "Enviar".

Seguridad (hallazgo H9 del Consejo):
  - Solo se procesan correos cuyo remitente es NOTIFY_EMAIL y que pasan la
    verificación de autenticidad del proveedor (DMARC/DKIM, cabecera Authentication-Results).
  - Cada código es aleatorio, distinto por perfil y de un solo uso; no se reutilizan lotes.
  - Los textos del perfil se escapan en el HTML (nadie puede inyectar botones o texto).
  - Responder NO acepta en LinkedIn: deja el perfil "aprobado" y el agente le devuelve a
    Darwing la lista con enlaces para que acepte él en la app (D7-A). El agente no hace clic.

Fase 0 (D8-B, R7, R8, R15): el buzón es el Gmail personal de Darwing, así que la lectura es
  - en modo SOLO LECTURA (no marca como leído, no mueve ni borra nada);
  - filtrada en el servidor por remitente (NOTIFY_EMAIL) y asunto ("LinkedIn Agent");
  - la firma se toma solo del primer encabezado Authentication-Results, el de Google;
  - el plazo se mide con la hora en que Gmail RECIBIÓ la respuesta, no con la fecha que trae;
  - un correo con más de MAX_CODIGOS_POR_CORREO códigos se rechaza entero.

Estado: data/consult_state.json  (un registro por código, nunca se sobrescriben lotes)

Uso:
    python agent/notifier.py --preview   # genera data/email_preview.html con los 'consult' del último dry-run
    python agent/notifier.py --test      # envía un correo de PRUEBA (perfiles ficticios) a NOTIFY_EMAIL
    python agent/notifier.py --check     # lee respuestas y actualiza el estado
"""

import csv
import email
import email.header
import email.utils
import html
import imaplib
import json
import logging
import os
import re
import secrets
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

import logconf
import memoria
import secretos

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

DECISIONS_FILE = Path(os.getenv("DRYRUN_DECISIONS_FILE", "data/dryrun_decisions.json"))
STATE_FILE     = Path(os.getenv("CONSULT_STATE_FILE", "data/consult_state.json"))
PREVIEW_FILE   = Path(os.getenv("EMAIL_PREVIEW_FILE", "data/email_preview.html"))
DECISIONS_CSV  = Path(os.getenv("DECISIONS_CSV", "data/decisions.csv"))
LOG_FILE       = Path(os.getenv("LOG_FILE", "logs/agent.log"))

NOTIFY_EMAIL   = os.getenv("NOTIFY_EMAIL", "").strip()          # correo de Darwing (quien decide)
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "").strip()             # buzón que usa el agente: hoy el Gmail personal de Darwing (D8-B)
SMTP_PASS      = "".join(secretos.obtener("SMTP_PASS").split())   # quita espacios (incl. no separables) que Google muestra en la clave
IMAP_HOST      = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT      = int(os.getenv("IMAP_PORT", "993"))

HOURS_TO_REPLY = int(os.getenv("HOURS_TO_REPLY", "48"))         # D4: 48 h (decisión de Darwing, 16-oct)
REQUIRE_AUTH   = os.getenv("REQUIRE_EMAIL_AUTH", "1") != "0"     # exigir DMARC/DKIM del remitente
AUTHSERV_ID    = os.getenv("AUTHSERV_ID", "mx.google.com").lower()   # quien firma el primer Authentication-Results
ASUNTO_CLAVE   = "LinkedIn Agent"                                 # todo correo válido lo lleva en el asunto
MAX_CODIGOS_POR_CORREO = int(os.getenv("MAX_CODIGOS_POR_CORREO", "25"))

TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"               # sin 0/O/1/I/L para evitar confusiones
TOKEN_LEN      = 8
TOKEN_RE       = re.compile(r"\b([A-HJKMNP-Z2-9]{4})-([A-HJKMNP-Z2-9]{4})\b")
CMD_RE         = re.compile(r"\b(ACEPTAR|ACCEPT|IGNORAR|IGNORE)\b", re.I)

# ── Logging ────────────────────────────────────────────────────────────────────

log = logconf.get("notifier")


# ── Estado ─────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"items": {}, "mensajes_procesados": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def new_token(existing) -> str:
    while True:
        raw = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))
        tok = f"{raw[:4]}-{raw[4:]}"
        if tok not in existing:
            return tok


CSV_FIELDS = ["fecha", "nombre", "cargo", "conexiones_comun", "profile_url",
              "decision", "score", "reason", "fuente"]


def append_to_csv(rows: list) -> None:
    if not rows:
        return
    DECISIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not DECISIONS_CSV.exists()
    with DECISIONS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)


# ── Construcción del correo ────────────────────────────────────────────────────

def _safe_url(url: str) -> str:
    url = (url or "").strip()
    return url if url.startswith("https://www.linkedin.com/in/") else ""


def _mailto(agent_addr: str, asunto: str, cuerpo: str) -> str:
    cuerpo = cuerpo.replace("\n", "\r\n")
    return f"mailto:{agent_addr}?subject={quote(asunto)}&body={quote(cuerpo)}"


def _boton(href: str, texto: str, fondo: str, color: str = "#ffffff") -> str:
    return (f'<a href="{html.escape(href)}" style="display:inline-block;padding:12px 18px;'
            f'margin:4px 8px 4px 0;background:{fondo};color:{color};text-decoration:none;'
            f'border-radius:6px;font-weight:bold;font-size:15px;">{texto}</a>')


def _linea_rol(it: dict) -> str:
    """Cargo y empresa actuales (del perfil) si existen; si no, el titular de la invitación."""
    cargo, empresa = (it.get("cargo_actual") or "").strip(), (it.get("empresa_actual") or "").strip()
    if cargo or empresa:
        return " · ".join(x for x in (cargo, empresa) if x)
    return (it.get("cargo") or "").strip()


def plantilla_respuesta(items: list) -> str:
    """Cuerpo prellenado del correo de respuesta: una línea por perfil, con '?' para que Darwing elija."""
    lineas = ["Cambia el ? por A (aceptar) o I (ignorar) y envía este correo.",
              "Puedes dejar ? en los que aún no decidas.", ""]
    for it in items:
        nombre = (it.get("nombre") or "")[:40]
        lineas.append(f"? {it['token']} {it.get('numero', '')}. {nombre}")
    return "\n".join(lineas)


_FOTOS_CID: dict = {}


def _foto_html(it: dict) -> str:
    ruta = it.get("foto")
    if not ruta or not Path(ruta).exists():
        return ('<div style="width:56px;height:56px;border-radius:28px;background:#e4e4e7;color:#71717a;'
                'font-size:22px;line-height:56px;text-align:center;">' + html.escape((it.get("nombre") or "?")[:1]) + '</div>')
    cid = f"foto-{it['token']}"
    _FOTOS_CID[cid] = ruta
    return (f'<img src="cid:{cid}" width="56" height="56" alt="" '
            f'style="width:56px;height:56px;border-radius:28px;object-fit:cover;display:block;">')


def _puntaje_html(score) -> str:
    """Puntaje 1–10 de la evaluación, con color: 1–3 bajo, 4–6 medio, 7–10 alto."""
    try:
        v = max(1, min(10, int(score)))
    except (TypeError, ValueError):
        return ""
    fondo, color = (("#fee2e2", "#b91c1c") if v <= 3 else ("#fef3c7", "#92400e") if v <= 6 else ("#dcfce7", "#15803d"))
    return (f'<span style="display:inline-block;margin-left:6px;padding:2px 8px;border-radius:10px;'
            f'background:{fondo};color:{color};font-size:13px;font-weight:bold;vertical-align:middle;">{v}/10</span>')


_RECOMENDACIONES = {
    "aceptar":     ("El agente recomienda aceptar", "#15803d", "#dcfce7"),
    "consultar":   ("El agente no tiene una recomendación clara", "#92400e", "#fef3c7"),
    "sin_evaluar": ("El agente NO pudo evaluarlo: decide tú con el perfil", "#b91c1c", "#fee2e2"),
}


def _recomendacion_html(rec) -> str:
    """Muestra la recomendación sin marcar nada de antemano: la respuesta siempre empieza en '?'."""
    if rec not in _RECOMENDACIONES:
        return ""
    texto, color, fondo = _RECOMENDACIONES[rec]
    return (f'<div style="display:inline-block;font-size:12px;font-weight:bold;margin-top:6px;padding:2px 8px;'
            f'border-radius:10px;color:{color};background:{fondo};">{texto}</div>')


def aprobados_sin_clic(state: dict) -> list:
    return sorted([i for i in state["items"].values()
                   if i.get("estado") == "aprobado" and not i.get("ejecutado_en_linkedin")],
                  key=lambda i: i.get("resuelto", ""))


def lista_clic_html(state: dict) -> str:
    """D7-A: lo aprobado lo acepta Darwing en la app. Lista con enlace al perfil de cada persona."""
    pend = aprobados_sin_clic(state)
    if not pend:
        return ""
    filas = ""
    for k, it in enumerate(pend, 1):
        url = _safe_url(it.get("profile_url"))
        nombre = html.escape(it.get("nombre") or "")
        enlace = (f' · <a href="{html.escape(url)}" style="color:#0a66c2;">abrir perfil</a>' if url else "")
        filas += f'<li style="margin:2px 0;">{k}. {nombre}{enlace}</li>'
    return f"""
  <div style="background:#ffffff;border:1px solid #15803d;border-radius:8px;padding:14px 16px;margin:0 0 16px;font-size:14px;">
    <div style="font-weight:bold;margin-bottom:6px;">👆 Para aceptar tú en LinkedIn ({len(pend)})</div>
    <div style="color:#52525b;margin-bottom:6px;">Ya los aprobaste. Ábrelos desde la app de LinkedIn (Mi red → Invitaciones) y pulsa Aceptar. El agente no hace clic.</div>
    <ul style="margin:4px 0 0;padding-left:18px;list-style:none;">{filas}</ul>
  </div>"""


def build_email(items: list, agent_addr: str, deadline: datetime, titulo: str | None = None,
                intro_html: str = "") -> tuple:
    """Devuelve (asunto, html, texto_plano). Un solo correo de respuesta para todo el lote."""
    plazo = deadline.astimezone(timezone(timedelta(hours=-5))).strftime("%d/%m %H:%M")  # hora Lima
    n = len(items)
    titulo = titulo or f"{n} {'perfil' if n == 1 else 'perfiles'} para tu decisión"
    asunto = f"[LinkedIn Agent] {titulo}"
    tokens = [it["token"] for it in items]

    href_lote  = _mailto(agent_addr, "DECISIONES LinkedIn Agent", plantilla_respuesta(items))
    # R15: ya no hay "Aceptar a todos": cada persona pasa por la mirada de Darwing.
    href_nadie = _mailto(agent_addr, "IGNORAR · LinkedIn Agent", f"IGNORAR {' '.join(tokens)}")

    tarjetas = ""
    for it in items:
        url = _safe_url(it.get("profile_url"))
        nombre = html.escape(it.get("nombre") or "")
        rol_txt = _linea_rol(it)
        rol = html.escape(rol_txt if len(rol_txt) <= 120 else rol_txt[:117].rsplit(" ", 1)[0] + "…")
        titular = (it.get("cargo") or "").strip()
        titular_html = (f'<div style="font-size:12px;color:#a1a1aa;margin-top:2px;">Titular: {html.escape(titular[:140])}</div>'
                        if titular and titular != _linea_rol(it) else "")
        ubic = html.escape(it.get("ubicacion") or "")
        razon_txt = (it.get("reason") or "").strip()
        if len(razon_txt) > 220:
            razon_txt = razon_txt[:217].rsplit(" ", 1)[0] + "…"
        razon = html.escape(razon_txt)
        comunes = int(it.get("conexiones_comun") or 0)
        ver = (f' · <a href="{html.escape(url)}" style="color:#0a66c2;">ver perfil</a>' if url else "")
        extra = f" · 📍 {ubic}" if ubic else ""
        tags = it.get("trayectoria_tags") or []
        tray_html = (f'<div style="font-size:13px;color:#15803d;margin-top:4px;">🍇 Trayectoria: {html.escape(", ".join(tags[:3]))}</div>'
                     if tags else "")
        rec_html = _recomendacion_html(it.get("recomendacion"))
        tarjetas += f"""
  <div style="border:1px solid #e4e4e7;border-radius:8px;padding:14px 16px;margin:0 0 10px;background:#ffffff;">
   <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
    <td valign="top" width="64" style="width:64px;padding-right:10px;">{_foto_html(it)}</td>
    <td valign="top">
    <div style="font-size:16px;font-weight:bold;color:#18181b;"><span style="color:#0a66c2;">{it.get('numero', '')}.</span> {nombre} {_puntaje_html(it.get("score"))}</div>
    <div style="font-size:14px;color:#3f3f46;margin:2px 0 2px;">{rol}</div>{titular_html}
    <div style="font-size:13px;color:#71717a;margin-top:4px;">🤝 {comunes} en común{extra}{ver}</div>
    {rec_html}
    <div style="font-size:13px;color:#52525b;margin-top:8px;"><b>Por qué te lo consulto:</b> {razon}</div>
    {tray_html}
    <div style="font-size:11px;color:#a1a1aa;margin-top:6px;">Código {it["token"]}</div>
    </td></tr></table>
  </div>"""

    botones = f"""
  <div style="background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;padding:14px 16px;margin:0 0 16px;">
    {_boton(href_lote, "📝 Decidir todo en un solo correo", "#0a66c2")}
    <div style="font-size:13px;color:#52525b;margin:6px 0 10px;">Se abre un correo con la lista: cambia cada <b>?</b> por <b>A</b> (aceptar) o <b>I</b> (ignorar) y envíalo una sola vez.</div>
    {_boton(href_nadie, f"❌ Ignorar a {'los ' + str(n) if n > 1 else 'este'}", "#f4f4f5", "#3f3f46")}
  </div>"""

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;padding:0;background:#f4f4f5;">
<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;padding:20px 14px;color:#18181b;">
  <div style="font-size:20px;font-weight:bold;margin-bottom:4px;">{html.escape(titulo)}</div>
  <div style="font-size:14px;color:#52525b;margin-bottom:14px;">
    Plazo: <b>{plazo}</b> (hora Lima). Lo que no respondas para entonces se ignora.
  </div>
  {intro_html}
  {botones}
  {tarjetas}
  <div style="font-size:12px;color:#71717a;margin-top:16px;line-height:1.5;">
    Te confirmaré por correo lo que reciba y lo que quede pendiente. Ignorar no le avisa a la persona.
    Solo cuentan las respuestas desde tu correo.<br>
    Si el botón no abre tu correo: responde escribiendo una línea por persona, por ejemplo <b>A {html.escape(tokens[0])}</b>.
  </div>
</div></body></html>"""

    texto = [titulo, f"Plazo: {plazo} (hora Lima). Sin respuesta = se ignoran.", "",
             "Recomendación del agente en cada caso; nada viene marcado: decides tú.", "",
             "Responde con una línea por persona: A <código> para aceptar, I <código> para ignorar.", ""]
    for it in items:
        texto += [f"{it.get('numero', '')}. {it.get('nombre')} ({it.get('score', '?')}/10) | {_linea_rol(it)} | {it.get('conexiones_comun', 0)} en común",
                  f"   {_RECOMENDACIONES.get(it.get('recomendacion'), ('',))[0]}",
                  f"   Por qué: {it.get('reason')}", f"   Código: {it['token']}", ""]
    return asunto, html_body, "\n".join(texto)


def _send(asunto: str, html_body: str, texto: str) -> bool:
    if not (SMTP_USER and SMTP_PASS and NOTIFY_EMAIL):
        log.error("Faltan SMTP_USER, SMTP_PASS o NOTIFY_EMAIL en .env — no se envió el correo.")
        return False
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(texto, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    cids = [c for c in re.findall(r'src="cid:([^"]+)"', html_body) if c in _FOTOS_CID]
    if cids:
        msg = MIMEMultipart("related")
        msg.attach(alt)
        for cid in dict.fromkeys(cids):
            try:
                img = MIMEImage(Path(_FOTOS_CID[cid]).read_bytes())
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
                msg.attach(img)
            except Exception as e:
                log.warning(f"No se pudo adjuntar la foto {cid}: {e}")
    else:
        msg = alt
    msg["Subject"] = asunto
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    msg["Message-ID"] = email.utils.make_msgid(domain="linkedin-agent.local")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(SMTP_USER, [NOTIFY_EMAIL], msg.as_string())
        log.info(f"✓ Correo enviado a {NOTIFY_EMAIL}: {asunto}" + (f" ({len(set(cids))} fotos)" if cids else ""))
        return True
    except Exception as e:
        log.error(f"Error enviando correo: {e}")
        return False


# ── Flujo de envío ─────────────────────────────────────────────────────────────

def notify_consult_profiles(dry_run: bool = False, resultados: list | None = None) -> None:
    """Registra los perfiles 'consult' nuevos con un código y envía (o previsualiza) el correo."""
    if resultados is None:
        if not DECISIONS_FILE.exists():
            log.error(f"No se encontró {DECISIONS_FILE}.")
            return
        resultados = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))

    state = load_state()
    # No volver a consultar a alguien ya consultado (pendiente, aprobado, ignorado o vencido):
    # los ignorados siguen pendientes en LinkedIn y reaparecen en cada corrida.
    ya_consultados = {memoria.huella_url(v.get("profile_url")) for v in state["items"].values() if v.get("profile_url")}
    consultar = [r for r in resultados
                 if r.get("decision") == "consult" and memoria.huella_url(r.get("profile_url")) not in ya_consultados]
    if not consultar:
        log.info("No hay perfiles nuevos para consultar.")
        return

    deadline = _now() + timedelta(hours=HOURS_TO_REPLY)
    items = []
    for r in consultar:
        tok = new_token(state["items"])
        item = {
            "token": tok,
            "numero": len(items) + 1,
            "nombre": r.get("nombre"), "cargo": r.get("cargo"),
            "cargo_actual": r.get("cargo_actual"), "empresa_actual": r.get("empresa_actual"),
            "ubicacion": r.get("ubicacion"),
            "foto": r.get("foto"),
            "trayectoria_tags": ((r.get("señales") or {}).get("grupo_historia") or (r.get("señales") or {}).get("grupo_actual") or [])
                                + ((r.get("señales") or {}).get("sector") or (r.get("señales") or {}).get("sector_palabras") or []),
            "conexiones_comun": r.get("conexiones_comun"), "profile_url": r.get("profile_url"),
            "score": r.get("score"), "reason": r.get("reason"),
            "recomendacion": r.get("recomendacion", ""),
            "creado": _now().isoformat(), "plazo": deadline.isoformat(),
            "estado": "pendiente", "ejecutado_en_linkedin": False,
        }
        items.append(item)
        state["items"][tok] = item

    asunto, html_body, texto = build_email(items, SMTP_USER or "agente@ejemplo.com", deadline,
                                           intro_html=lista_clic_html(state))

    if dry_run:
        PREVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
        for cid, ruta in _FOTOS_CID.items():
            html_body = html_body.replace(f"cid:{cid}", Path(ruta).resolve().as_uri())
        PREVIEW_FILE.write_text(html_body, encoding="utf-8")
        log.info(f"DRY-RUN: NO se envió correo. Vista previa con {len(items)} perfiles en {PREVIEW_FILE}")
        return  # en dry-run no se guarda estado

    if _send(asunto, html_body, texto):
        save_state(state)


def send_test_email() -> None:
    """Correo de prueba con perfiles ficticios. Sus códigos no corresponden a nadie real."""
    deadline = _now() + timedelta(hours=HOURS_TO_REPLY)
    items = [
        {"token": "PRUE-BA22", "numero": 1, "nombre": "Persona de Prueba Uno", "cargo": "Gerente Comercial · Exportadora de fruta",
         "conexiones_comun": 12, "profile_url": "", "reason": "Correo de prueba: toca un botón y envía para verificar el circuito."},
        {"token": "PRUE-BA33", "numero": 2, "nombre": "Persona de Prueba Dos", "cargo": "Consultora en calidad y certificaciones",
         "conexiones_comun": 4, "profile_url": "", "reason": "Correo de prueba: no afecta a ninguna solicitud real."},
    ]
    asunto, html_body, texto = build_email(items, SMTP_USER or "agente@ejemplo.com", deadline)
    if _send("[PRUEBA] " + asunto, html_body, texto):
        state = load_state()
        state["prueba_enviada"] = _now().isoformat()   # para que --check lea el buzón aunque no haya consultas reales
        save_state(state)


# ── Lectura de respuestas ──────────────────────────────────────────────────────

def _decode(value: str) -> str:
    out = ""
    for part, charset in email.header.decode_header(value or ""):
        out += part.decode(charset or "utf-8", errors="ignore") if isinstance(part, bytes) else part
    return out


def _sender_ok(msg) -> tuple:
    """
    El remitente debe ser NOTIFY_EMAIL y, si REQUIRE_AUTH, pasar DMARC (o DKIM alineado).
    R8: solo cuenta el PRIMER encabezado Authentication-Results, que es el que agrega Google
    (AUTHSERV_ID) al recibir. Los que vienen más abajo los pudo escribir cualquiera.
    """
    _, addr = email.utils.parseaddr(_decode(msg.get("From", "")))
    addr = addr.lower()
    if not NOTIFY_EMAIL or addr != NOTIFY_EMAIL.lower():
        return False, f"remitente no autorizado ({addr or 'vacío'})"
    if not REQUIRE_AUTH:
        return True, "remitente ok (sin verificación de firma)"
    dominio = re.escape(addr.split("@")[-1])
    todos = msg.get_all("Authentication-Results") or []
    if not todos:
        return False, "sin encabezado de autenticación"
    auth = " ".join(_decode(todos[0]).split()).lower()
    if not re.match(re.escape(AUTHSERV_ID) + r"\s*;", auth):
        return False, f"el primer encabezado de autenticación no es de {AUTHSERV_ID}"
    if re.search(r"\bdmarc=pass\b[^;]*\bheader\.from=" + dominio + r"(?![\w.-])", auth):
        return True, "dmarc=pass"
    if re.search(r"\bdkim=pass\b[^;]*\bheader\.(?:i|d)=@?" + dominio + r"(?![\w.-])", auth):
        return True, "dkim=pass alineado"
    return False, "sin firma DMARC/DKIM válida del remitente"


def _texto_propio(msg) -> str:
    """Asunto + texto escrito por Darwing (sin el mensaje citado)."""
    body = ""
    part_iter = msg.walk() if msg.is_multipart() else [msg]
    for part in part_iter:
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            break
    propias = []
    for line in body.splitlines():
        l = line.strip()
        if l.startswith(">"):
            continue
        if re.match(r"^(on .+wrote:|el .+escribi[oó]:|-{2,}\s*original|de:\s|from:\s|enviado:|sent:)", l, re.I):
            break
        propias.append(l)
    return _decode(msg.get("Subject", "")) + "\n" + "\n".join(propias)


LINE_CMD_RE = re.compile(r"^\s*[-*•]?\s*(ACEPTAR|ACCEPT|IGNORAR|IGNORE|SI|SÍ|NO|A|I)\b[\s:.\-]*", re.I)


def parse_commands(texto: str) -> dict:
    """
    {token: 'accept'|'ignore'}. Formatos aceptados (uno por línea):
      A K7F2-9QXA  /  I K7F2-9QXA  /  ACEPTAR K7F2-9QXA 3K7Z-RTW7  /  IGNORAR ...
    Líneas con '?' se ignoran (aún sin decidir). Un código con órdenes contradictorias se descarta.
    """
    result, conflict = {}, set()
    for line in texto.splitlines():
        m = LINE_CMD_RE.match(line)
        if not m:
            # también vale la orden en medio de la línea (p. ej. el asunto "Re: ACEPTAR X")
            m2 = re.search(r"\b(ACEPTAR|ACCEPT|IGNORAR|IGNORE)\b", line, re.I)
            if not m2:
                continue
            palabra, resto = m2.group(1), line[m2.end():]
        else:
            palabra, resto = m.group(1), line[m.end():]
        decision = "accept" if palabra.upper() in ("ACEPTAR", "ACCEPT", "SI", "SÍ", "A") else "ignore"
        for a, b in TOKEN_RE.findall(resto.upper()):
            tok = f"{a}-{b}"
            if tok in result and result[tok] != decision:
                conflict.add(tok)
            result[tok] = decision
    for tok in conflict:
        result.pop(tok, None)
    return result


def apply_commands(state: dict, comandos: dict, fuente: str, recibido: datetime | None = None) -> list:
    """Aplica órdenes a códigos pendientes. `recibido` es la hora en que Gmail RECIBIÓ la respuesta (R15)."""
    cambios = []
    recibido = recibido or _now()
    for tok, decision in comandos.items():
        item = state["items"].get(tok)
        if tok.startswith("PRUE-"):
            log.info(f"✓ PRUEBA OK: llegó la orden {decision.upper()} {tok} desde el remitente verificado ({fuente}).")
            continue
        if not item:
            log.warning(f"Código desconocido ignorado: {tok}")
            continue
        if item["estado"] != "pendiente":
            log.info(f"Código {tok} ya resuelto ({item['estado']}) — sin cambios (un solo uso).")
            continue
        if recibido > datetime.fromisoformat(item["plazo"]):
            log.info(f"Código {tok} llegó fuera de plazo — se mantiene la regla de vencimiento.")
            continue
        item["estado"] = "aprobado" if decision == "accept" else "ignorado"
        item["resuelto"] = _now().isoformat()
        item["fuente"] = fuente
        cambios.append(item)
    return cambios


def expire_pending(state: dict) -> list:
    vencidos = []
    for item in state["items"].values():
        if item["estado"] == "pendiente" and _now() > datetime.fromisoformat(item["plazo"]):
            item["estado"] = "vencido"
            item["resuelto"] = _now().isoformat()
            item["fuente"] = f"sin_respuesta_{HOURS_TO_REPLY}h"
            vencidos.append(item)
    return vencidos


def _recepcion(cabecera_fetch: bytes) -> datetime | None:
    """Hora de recepción en Gmail (INTERNALDATE), en UTC."""
    tt = imaplib.Internaldate2tuple(cabecera_fetch or b"")
    return datetime.fromtimestamp(time.mktime(tt), tz=timezone.utc) if tt else None


def leer_buzon(state: dict, desde: datetime, cambios_total: list, conectar=None) -> dict:
    """
    Lee el buzón en SOLO LECTURA y aplica las órdenes válidas. `conectar` permite usar un
    buzón de prueba. Devuelve un pequeño resumen para el log y las pruebas.
    """
    conectar = conectar or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT))
    resumen = {"revisados": 0, "con_ordenes": 0, "rechazados": 0}
    with conectar() as mail:
        mail.login(SMTP_USER, SMTP_PASS)
        mail.select("INBOX", readonly=True)          # R7: no marca como leído ni mueve nada
        criterio = f'(FROM "{NOTIFY_EMAIL}" SUBJECT "{ASUNTO_CLAVE}" SINCE {(desde - timedelta(days=1)).strftime("%d-%b-%Y")})'
        _, data = mail.search(None, criterio)
        for num in (data[0] or b"").split():
            resumen["revisados"] += 1
            _, msg_data = mail.fetch(num, "(INTERNALDATE BODY.PEEK[])")
            partes = [x for x in msg_data if isinstance(x, tuple)]
            if not partes:
                continue
            msg = email.message_from_bytes(partes[0][1])
            mid = msg.get("Message-ID") or f"sin-id-{num.decode()}"
            if mid in state["mensajes_procesados"]:
                continue
            if ASUNTO_CLAVE.lower() not in _decode(msg.get("Subject", "")).lower():
                continue                              # doble filtro: el servidor ya lo aplicó
            comandos = parse_commands(_texto_propio(msg))
            if not comandos:
                continue
            resumen["con_ordenes"] += 1
            ok, motivo = _sender_ok(msg)
            if ok and len(comandos) > MAX_CODIGOS_POR_CORREO:
                ok, motivo = False, f"trae {len(comandos)} códigos (máximo {MAX_CODIGOS_POR_CORREO} por correo)"
            if not ok:
                resumen["rechazados"] += 1
                log.warning(f"⛔ Correo con órdenes RECHAZADO: {motivo}.")
                state["mensajes_procesados"].append(mid)
                continue
            recibido = _recepcion(partes[0][0])
            if recibido is None:
                resumen["rechazados"] += 1
                log.warning("⛔ Correo con órdenes RECHAZADO: no se pudo leer la hora de recepción.")
                state["mensajes_procesados"].append(mid)
                continue
            cambios = apply_commands(state, comandos, f"correo ({motivo})", recibido)
            for c in cambios:
                log.info(f"  {logconf.ref(c)} → {c['estado'].upper()} (código {c['token']})")
            cambios_total.extend(cambios)
            state["mensajes_procesados"].append(mid)
    log.info(f"Buzón: {resumen['revisados']} correos del remitente con el asunto del agente, "
             f"{resumen['con_ordenes']} con órdenes, {resumen['rechazados']} rechazados. Nada se modificó en el buzón.")
    return resumen


def check_replies(dry_run: bool = False) -> list:
    """
    Lee el buzón del agente, aplica solo órdenes válidas de NOTIFY_EMAIL y vence lo que pasó de plazo.
    Devuelve los perfiles APROBADOS que Darwing aún no acepta en la app (para el resumen).
    """
    state = load_state()
    cambios_total = []
    fechas = [datetime.fromisoformat(i["creado"]) for i in state["items"].values() if i["estado"] == "pendiente"]
    prueba = state.get("prueba_enviada")
    if prueba and _now() - datetime.fromisoformat(prueba) < timedelta(hours=HOURS_TO_REPLY):
        fechas.append(datetime.fromisoformat(prueba))
    if not fechas:
        log.info("No hay consultas pendientes.")
    elif not (SMTP_USER and SMTP_PASS):
        log.error("SMTP_USER/SMTP_PASS no configurados — no se pueden leer respuestas.")
    else:
        desde = min(fechas)
        try:
            leer_buzon(state, desde, cambios_total)
        except Exception as e:
            log.error(f"Error leyendo el buzón: {type(e).__name__}: {str(e)[:120]}")

    vencidos = expire_pending(state)
    for v in vencidos:
        log.info(f"  {logconf.ref(v)} → sin respuesta en {HOURS_TO_REPLY} h: se ignora")

    # Registrar en el historial las decisiones finales que no requieren LinkedIn
    fecha = _now().strftime("%Y-%m-%d %H:%M")
    filas = [{"fecha": fecha, "nombre": i["nombre"], "cargo": i["cargo"],
              "conexiones_comun": i["conexiones_comun"], "profile_url": i["profile_url"],
              "decision": "accept" if i["estado"] == "aprobado" else "ignore",
              "score": i.get("score", ""), "reason": i.get("reason", ""),
              "fuente": ("darwing_" if i["estado"] != "vencido" else "") + i["fuente"]}
             for i in state["items"].values()
             if i["estado"] in ("aprobado", "ignorado", "vencido") and not i.get("registrado")]

    if dry_run:
        log.info(f"DRY-RUN: estado de consultas no modificado ({len(filas)} decisiones se registrarían).")
    else:
        append_to_csv(filas)
        for i in state["items"].values():
            if i["estado"] in ("aprobado", "ignorado", "vencido"):
                i["registrado"] = True
        save_state(state)
        if cambios_total:
            send_confirmation(state, cambios_total)

    return aprobados_sin_clic(state)


def send_confirmation(state: dict, cambios: list) -> None:
    """Confirma a Darwing lo recibido y le reenvía, con el mismo botón, lo que sigue pendiente."""
    aprob = [c for c in cambios if c["estado"] == "aprobado"]
    ign = [c for c in cambios if c["estado"] == "ignorado"]
    pend = sorted([i for i in state["items"].values() if i["estado"] == "pendiente"],
                  key=lambda i: (i.get("creado", ""), i.get("numero") or 0))
    pend = [dict(i, numero=k) for k, i in enumerate(pend, 1)]   # renumerar para este correo
    def lista(xs):
        return "".join(f"<li>{html.escape(x.get('nombre') or '')}</li>" for x in xs) or "<li>—</li>"
    resumen = f"""
  <div style="background:#ffffff;border:1px solid #e4e4e7;border-radius:8px;padding:14px 16px;margin:0 0 16px;font-size:14px;">
    <div style="font-weight:bold;margin-bottom:6px;">Recibí tu respuesta ✓</div>
    <div>✅ Aprobados ({len(aprob)}):</div><ul style="margin:4px 0 8px;">{lista(aprob)}</ul>
    <div>❌ Ignorados ({len(ign)}):</div><ul style="margin:4px 0 8px;">{lista(ign)}</ul>
  </div>""" + lista_clic_html(state)
    if pend:
        plazo = min(datetime.fromisoformat(i["plazo"]) for i in pend)
        asunto, html_body, texto = build_email(
            pend, SMTP_USER, plazo, titulo=f"Recibido ✓ · te quedan {len(pend)} por decidir", intro_html=resumen)
    else:
        asunto = "[LinkedIn Agent] Recibido ✓ · no te queda nada pendiente"
        html_body = (f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="background:#f4f4f5;"><div style="font-family:-apple-system,'
                     f'Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;padding:20px 14px;">{resumen}</div></body></html>')
        texto = f"Recibido. Aprobados: {len(aprob)}. Ignorados: {len(ign)}. No queda nada pendiente."
    _send(asunto, html_body, texto)


def cerrar_aprobados_ausentes(urls_visibles: list, cobertura_completa: bool) -> int:
    """
    Si Darwing ya aceptó en la app, la invitación desaparece de "Recibidas". Solo cuando la
    lectura vio TODAS las invitaciones se marcan como hechas las aprobadas que ya no están.
    """
    if not cobertura_completa:
        return 0
    visibles = {memoria.norm(u) for u in urls_visibles}
    state = load_state()
    n = 0
    for i in state["items"].values():
        if (i.get("estado") == "aprobado" and not i.get("ejecutado_en_linkedin")
                and memoria.norm(i.get("profile_url")) not in visibles):
            i["ejecutado_en_linkedin"] = True
            i["ejecutado"] = _now().isoformat()
            i["ejecutado_por"] = "darwing_en_la_app"
            n += 1
    if n:
        save_state(state)
    return n


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--check" in sys.argv:
        aprobados = check_replies(dry_run="--dry-run" in sys.argv)
        print(f"\nAprobados por correo pendientes de ejecutar en LinkedIn: {len(aprobados)}")
    elif "--test" in sys.argv:
        send_test_email()
    elif "--preview" in sys.argv:
        notify_consult_profiles(dry_run=True)
    else:
        print("Uso: python agent/notifier.py --preview | --test | --check [--dry-run]")

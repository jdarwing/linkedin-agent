"""
notifier.py — Notificaciones por email (Gmail) y lectura de respuestas
=======================================================================
Envía un email con los perfiles "consultar" numerados a jdarwing@hotmail.com.
Darwing responde el mismo email con "1: ACEPTAR, 2: IGNORAR" etc.
En la siguiente ejecución, el agente lee las respuestas vía IMAP (Gmail) y actualiza decisions.csv.

Nota: el email SE ENVÍA desde Gmail (SMTP_USER = tu cuenta Gmail).
La respuesta de Darwing llega al inbox de Gmail → IMAP lee de Gmail.

Uso:
    python agent/notifier.py --send    # envía email con perfiles pendientes
    python agent/notifier.py --check   # lee respuestas y actualiza decisions.csv
"""

import csv
import email as email_lib
import email.header
import imaplib
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

DECISIONS_FILE  = Path(os.getenv("DECISIONS_FILE",  "data/decisions_preview.json"))
PENDING_FILE    = Path(os.getenv("PENDING_FILE",     "data/pending_consult.json"))
DECISIONS_CSV   = Path(os.getenv("DECISIONS_CSV",   "data/decisions.csv"))
LOG_FILE        = Path(os.getenv("LOG_FILE",         "logs/agent.log"))

NOTIFY_EMAIL    = os.getenv("NOTIFY_EMAIL",  "jdarwing@hotmail.com")
SMTP_HOST       = os.getenv("SMTP_HOST",     "smtp.gmail.com")       # Gmail SMTP
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER       = os.getenv("SMTP_USER",     "")   # tu cuenta Gmail completa
SMTP_PASS       = os.getenv("SMTP_PASS",     "")   # App Password de Gmail (16 chars)

# IMAP: lee la bandeja de SMTP_USER (Gmail) donde llegan las respuestas de Darwing.
# Hotmail desactivó auth básica; Gmail con App Password sí funciona.
IMAP_HOST       = os.getenv("IMAP_HOST",     "imap.gmail.com")
IMAP_PORT       = int(os.getenv("IMAP_PORT", "993"))

HOURS_TO_REPLY  = 48   # horas para responder antes de ignorar automáticamente

# ── Logging ────────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("notifier")


# ── CSV helpers ────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "fecha", "nombre", "cargo", "conexiones_comun", "profile_url",
    "decision", "score", "reason", "fuente",
]


def append_to_csv(rows: list) -> None:
    """Agrega filas al CSV de decisiones históricas."""
    DECISIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not DECISIONS_CSV.exists()
    with DECISIONS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Construcción del email HTML ────────────────────────────────────────────────

def build_email_html(perfiles: list, sent_at: str) -> str:
    deadline = (
        datetime.fromisoformat(sent_at) + timedelta(hours=HOURS_TO_REPLY)
    ).strftime("%d/%m/%Y %H:%M")

    filas = ""
    for i, p in enumerate(perfiles, 1):
        conexiones = p.get("conexiones_comun", 0)
        filas += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:12px 8px;font-weight:bold;color:#0a66c2;font-size:16px;">{i}</td>
          <td style="padding:12px 8px;">
            <strong><a href="{p.get('profile_url','#')}" style="color:#0a66c2;text-decoration:none;">{p.get('nombre','')}</a></strong><br>
            <span style="color:#555;font-size:13px;">{p.get('cargo','')}</span><br>
            <span style="color:#888;font-size:12px;">🤝 {conexiones} conexiones en común</span>
          </td>
          <td style="padding:12px 8px;color:#555;font-size:13px;">{p.get('reason','')}</td>
        </tr>"""

    instrucciones = "".join([
        f"<li><b>{i}: ACEPTAR</b> o <b>{i}: IGNORAR</b></li>"
        for i in range(1, len(perfiles) + 1)
    ])

    return f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;color:#333;">
  <div style="background:#0a66c2;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h2 style="color:#fff;margin:0;">LinkedIn Agent — {len(perfiles)} perfiles para revisar</h2>
    <p style="color:#cce0ff;margin:4px 0 0;">Tienes hasta el <strong style="color:#fff;">{deadline}</strong> para responder</p>
  </div>

  <div style="padding:24px;background:#f9f9f9;border:1px solid #ddd;border-top:none;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:6px;border:1px solid #eee;">
      <thead>
        <tr style="background:#f0f4f8;">
          <th style="padding:10px 8px;text-align:left;width:30px;">#</th>
          <th style="padding:10px 8px;text-align:left;">Perfil</th>
          <th style="padding:10px 8px;text-align:left;">Por qué consultar</th>
        </tr>
      </thead>
      <tbody>{filas}</tbody>
    </table>

    <div style="margin-top:24px;padding:16px;background:#fff3cd;border-radius:6px;border:1px solid #ffc107;">
      <h3 style="margin:0 0 8px;color:#856404;">📧 Cómo responder</h3>
      <p style="margin:0 0 8px;">Responde <strong>este mismo email</strong> con tus decisiones, una por línea:</p>
      <ul style="margin:0;padding-left:20px;">
        {instrucciones}
      </ul>
      <p style="margin:8px 0 0;color:#856404;font-size:13px;">
        Ejemplo de respuesta: <code>1: ACEPTAR<br>2: IGNORAR<br>3: ACEPTAR</code>
      </p>
    </div>

    <p style="margin-top:16px;color:#888;font-size:12px;">
      Si no respondes antes del {deadline}, los perfiles sin decisión serán ignorados automáticamente.<br>
      Ignorar en LinkedIn no notifica al solicitante.
    </p>
  </div>
</body>
</html>"""


# ── Envío de email ─────────────────────────────────────────────────────────────

def send_consult_email(perfiles: list) -> str | None:
    """Envía el email y devuelve el Message-ID para rastrear respuestas."""
    if not SMTP_USER or not SMTP_PASS:
        log.error("SMTP_USER o SMTP_PASS no configurados en .env")
        return None

    sent_at = datetime.now(timezone.utc).isoformat()
    html    = build_email_html(perfiles, sent_at)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[LinkedIn Agent] {len(perfiles)} perfiles para revisar — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg["X-LinkedIn-Agent-Sent"] = sent_at
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        message_id = msg.get("Message-ID", "")
        log.info(f"✓ Email enviado a {NOTIFY_EMAIL} con {len(perfiles)} perfiles")

        # Guardar estado de pendientes
        pending = {
            "sent_at":    sent_at,
            "deadline":   (datetime.now(timezone.utc) + timedelta(hours=HOURS_TO_REPLY)).isoformat(),
            "message_id": message_id,
            "perfiles":   perfiles,
        }
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        PENDING_FILE.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")
        return message_id

    except Exception as e:
        log.error(f"Error enviando email: {e}")
        return None


# ── Lectura de respuestas vía IMAP ─────────────────────────────────────────────

def _parse_reply_body(text: str) -> dict:
    """
    Extrae decisiones de texto como:
      1: ACEPTAR
      2: IGNORAR
      3: ACEPTAR
    Devuelve {1: "accept", 2: "ignore", ...}
    """
    decisions = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(\d+)\s*[:.\-]\s*(aceptar|accept|ignorar|ignore)", line, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            raw = m.group(2).lower()
            decisions[num] = "accept" if raw in ("aceptar", "accept") else "ignore"
    return decisions


def check_replies() -> list:
    """
    Lee el inbox de Gmail (IMAP_HOST) buscando respuestas al email de consulta.
    Devuelve lista de decisiones procesadas.
    """
    if not PENDING_FILE.exists():
        log.info("No hay perfiles pendientes de respuesta.")
        return []

    pending = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    perfiles  = pending["perfiles"]
    sent_at   = datetime.fromisoformat(pending["sent_at"])
    deadline  = datetime.fromisoformat(pending["deadline"])
    now       = datetime.now(timezone.utc)

    # Marcar como expirados los que superaron el deadline
    if now > deadline:
        log.info(f"Deadline de {HOURS_TO_REPLY}h superado. Perfiles sin respuesta → ignorar.")

    if not SMTP_USER or not SMTP_PASS:
        log.error("SMTP_USER o SMTP_PASS no configurados — no se puede leer IMAP")
        return []

    processed = []
    reply_decisions = {}

    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mail:
            mail.login(SMTP_USER, SMTP_PASS)
            mail.select("INBOX")

            # Buscar todos los emails desde la fecha de envío.
            # No usamos SUBJECT filter: Gmail falla con corchetes [ ] en el asunto.
            since_str = sent_at.strftime("%d-%b-%Y")
            _, data = mail.search(None, f'SINCE {since_str}')

            for num in data[0].split():
                _, msg_data = mail.fetch(num, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw_email)

                # Decodificar el asunto correctamente (puede venir en Base64 o QP)
                raw_subject = msg.get("Subject", "")
                decoded_parts = email.header.decode_header(raw_subject)
                subject = ""
                for part, charset in decoded_parts:
                    if isinstance(part, bytes):
                        subject += part.decode(charset or "utf-8", errors="ignore")
                    else:
                        subject += str(part)

                # Solo procesar respuestas al email del agente
                subject_lower = subject.lower()
                is_reply = any(prefix in subject_lower for prefix in ("re:", "rе:"))  # ascii + unicode
                has_agent = "linkedin" in subject_lower or "agent" in subject_lower
                if not is_reply or not has_agent:
                    continue

                log.info(f"Reply encontrado: {subject[:80]}")

                # Extraer cuerpo del email (texto plano primero)
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode(
                                    part.get_content_charset() or "utf-8",
                                    errors="ignore"
                                )
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode(
                            msg.get_content_charset() or "utf-8",
                            errors="ignore"
                        )

                reply_decisions = _parse_reply_body(body)
                if reply_decisions:
                    log.info(f"Decisiones parseadas: {reply_decisions}")
                    break  # tomar la primera respuesta válida con decisiones
                else:
                    log.warning(f"Reply encontrado pero sin decisiones válidas. Body preview: {body[:200]}")

    except Exception as e:
        log.error(f"Error leyendo IMAP: {e}")

    # Construir decisiones finales
    rows_csv = []
    for i, perfil in enumerate(perfiles, 1):
        if i in reply_decisions:
            decision = reply_decisions[i]
            fuente   = "email_reply"
            log.info(f"[{i}] {perfil['nombre']} → {decision.upper()} (por email)")
        elif now > deadline:
            decision = "ignore"
            fuente   = "timeout_48h"
            log.info(f"[{i}] {perfil['nombre']} → IGNORAR (timeout 48h)")
        else:
            log.info(f"[{i}] {perfil['nombre']} → aún dentro del plazo, sin respuesta")
            continue

        row = {
            "fecha":           now.strftime("%Y-%m-%d %H:%M"),
            "nombre":          perfil.get("nombre"),
            "cargo":           perfil.get("cargo"),
            "conexiones_comun": perfil.get("conexiones_comun"),
            "profile_url":     perfil.get("profile_url"),
            "decision":        decision,
            "score":           perfil.get("score", ""),
            "reason":          perfil.get("reason", ""),
            "fuente":          fuente,
        }
        rows_csv.append(row)
        processed.append({"perfil": perfil, "decision": decision})

    if rows_csv:
        append_to_csv(rows_csv)
        log.info(f"✓ {len(rows_csv)} decisiones registradas en {DECISIONS_CSV}")

    # Limpiar pendientes si se procesaron todos
    if len(processed) == len(perfiles):
        PENDING_FILE.unlink(missing_ok=True)
        log.info("Todos los perfiles procesados — archivo pendientes eliminado.")

    return processed


# ── Flujo principal ────────────────────────────────────────────────────────────

def notify_consult_profiles(dry_run: bool = False) -> None:
    """Lee decisions_preview.json y envía email con los perfiles 'consult'."""
    if not DECISIONS_FILE.exists():
        log.error(f"No se encontró {DECISIONS_FILE}. Ejecuta evaluator.py primero.")
        sys.exit(1)

    resultados = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
    consultar  = [r for r in resultados if r.get("decision") == "consult"]

    if not consultar:
        log.info("No hay perfiles 'consultar' en esta sesión.")
        return

    log.info(f"Preparando email con {len(consultar)} perfiles para consultar...")

    if dry_run:
        print(f"\n── DRY-RUN: se enviaría email con {len(consultar)} perfiles ──")
        for i, p in enumerate(consultar, 1):
            print(f"  {i}. {p['nombre']} (score {p['score']}) — {p['reason'][:60]}")
        return

    send_consult_email(consultar)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--check" in sys.argv:
        check_replies()
    elif "--send" in sys.argv or "--dry-run" in sys.argv:
        dry_run = "--dry-run" in sys.argv
        notify_consult_profiles(dry_run=dry_run)
    else:
        print("Uso: python agent/notifier.py --send | --check | --dry-run")

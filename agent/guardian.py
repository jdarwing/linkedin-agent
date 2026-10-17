"""
guardian.py — Guardián de la cuenta (funcionalidad 9, Fase 0)
=============================================================
Ante captcha, verificación, aviso de restricción o sesión caída: pausa TOTAL
(crea el archivo PAUSA con el motivo) y avisa por correo. El agente solo vuelve
a correr cuando Darwing borra el archivo PAUSA.

Para que un tercero no pueda pausar el agente (Mesa B, hallazgo 3):
  - las direcciones de riesgo se buscan solo en rutas propias de LinkedIn, nunca dentro de /in/;
  - el texto de la página se lee SIN las tarjetas de invitación (que escriben otros);
  - en los perfiles solo se miran la dirección, el título de la pestaña y un captcha incrustado.
"""

import re
from datetime import datetime
from pathlib import Path

PAUSA = Path(__file__).resolve().parent.parent / "PAUSA"
MARCA_AVISO_PENDIENTE = "Aviso por correo: PENDIENTE"
MARCA_AVISO_ENVIADO = "Aviso por correo: enviado"

_URL_ALERTA = re.compile(r"^https?://(?:[\w-]+\.)?linkedin\.com/(?:login|authwall|checkpoint|uas|challenge|security)(?:[/?#]|$)", re.I)
_TEXTO_ALERTA = re.compile(
    r"(?:resuelve|completa|solve|complete) (?:el |the )?captcha|security verification|verificaci[oó]n de seguridad|"
    r"let'?s do a quick security check|comprobaci[oó]n de seguridad r[aá]pida|verify (?:that )?you'?re (?:a )?human|"
    r"(?:account|cuenta) (?:has been |ha sido )?(?:restricted|restringida)|temporarily restricted|"
    r"restringido temporalmente|unusual activity|actividad inusual",
    re.I)

# Texto de la página sin las tarjetas (li / listitem) ni el contenido de perfiles de terceros
JS_TEXTO_PROPIO = """
() => {
  const c = document.body ? document.body.cloneNode(true) : null;
  if (!c) return {texto: '', titulo: document.title || '', captcha: false};
  c.querySelectorAll('li, [role="listitem"], script, style').forEach(e => e.remove());
  const captcha = !!document.querySelector('iframe[src*="captcha" i], #captcha-internal, [id*="captcha" i] iframe');
  return {texto: (c.innerText || c.textContent || '').slice(0, 5000), titulo: document.title || '', captcha};
}
"""


class AlertaCuenta(Exception):
    """LinkedIn mostró una señal de riesgo para la cuenta. El agente se detiene."""


def revisar(url: str, texto: str = "") -> str:
    """Devuelve el motivo de alerta, o '' si la página parece normal."""
    if _URL_ALERTA.search(url or ""):
        return f"LinkedIn redirigió a una página de acceso o verificación ({(url or '')[:80]})"
    m = _TEXTO_ALERTA.search(texto or "")
    if m:
        return f"La página muestra una señal de verificación o restricción («{m.group(0)}»)"
    return ""


def _datos(page) -> dict:
    try:
        return page.evaluate(JS_TEXTO_PROPIO) or {}
    except Exception:
        return {}


def revisar_pagina(page) -> None:
    """Página propia de LinkedIn (invitaciones): dirección, título, captcha y texto sin tarjetas."""
    d = _datos(page)
    motivo = revisar(page.url, (d.get("titulo", "") + "\n" + d.get("texto", "")))
    if not motivo and d.get("captcha"):
        motivo = "La página muestra un captcha"
    if motivo:
        raise AlertaCuenta(motivo)


def revisar_perfil(page) -> None:
    """Perfil de un tercero: solo dirección, título de la pestaña y captcha incrustado (nunca su texto)."""
    d = _datos(page)
    titulo = d.get("titulo", "")
    motivo = revisar(page.url) or ("La página muestra un captcha" if d.get("captcha") else "")
    if not motivo and re.search(r"^(security verification|verificaci[oó]n de seguridad)\b", titulo, re.I):
        motivo = f"La pestaña dice «{titulo[:60]}»"
    if motivo:
        raise AlertaCuenta(motivo)


def pausar(motivo: str) -> None:
    PAUSA.write_text(
        f"Agente en pausa automática desde {datetime.now().strftime('%d-%m-%Y %H:%M')} (guardián de la cuenta).\n"
        f"Motivo: {motivo}\n"
        f"{MARCA_AVISO_PENDIENTE}\n"
        "Mientras exista este archivo no corren ni la corrida programada ni la revisión del correo.\n"
        "Revisa tu cuenta de LinkedIn desde la app y, solo cuando esté normal, borra este archivo.\n",
        encoding="utf-8")


def en_pausa() -> bool:
    return PAUSA.exists()


def aviso_pendiente() -> str:
    """Si el aviso GUARDIÁN no salió, devuelve el motivo para reintentarlo; si no, ''."""
    try:
        t = PAUSA.read_text(encoding="utf-8")
    except Exception:
        return ""
    if MARCA_AVISO_PENDIENTE not in t:
        return ""
    m = re.search(r"^Motivo: (.+)$", t, re.M)
    return m.group(1) if m else "pausa automática"


def marcar_aviso_enviado() -> None:
    try:
        PAUSA.write_text(PAUSA.read_text(encoding="utf-8").replace(MARCA_AVISO_PENDIENTE, MARCA_AVISO_ENVIADO),
                         encoding="utf-8")
    except Exception:
        pass

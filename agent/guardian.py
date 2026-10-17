"""
guardian.py — Guardián de la cuenta (funcionalidad 9, Fase 0)
=============================================================
Ante captcha, verificación, aviso de restricción o sesión caída: pausa TOTAL
(crea el archivo PAUSA con el motivo) y avisa por correo. El agente solo vuelve
a correr cuando Darwing borra el archivo PAUSA.
"""

import re
from datetime import datetime
from pathlib import Path

PAUSA = Path(__file__).resolve().parent.parent / "PAUSA"

_URL_ALERTA = re.compile(r"/(login|authwall|checkpoint|uas|challenge)\b|/security/", re.I)
_TEXTO_ALERTA = re.compile(
    r"(?:resuelve|completa|solve|complete) (?:el |the )?captcha|security verification|verificaci[oó]n de seguridad|let'?s do a quick security check|"
    r"comprobaci[oó]n de seguridad r[aá]pida|verify (?:that )?you'?re (?:a )?human|demuestra que eres|"
    r"(?:account|cuenta) (?:has been |ha sido )?(?:restricted|restringida)|temporarily restricted|"
    r"restringido temporalmente|unusual activity|actividad inusual",
    re.I)


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


def revisar_pagina(page) -> None:
    """Lanza AlertaCuenta si la página actual muestra una señal de riesgo."""
    try:
        texto = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)")
    except Exception:
        texto = ""
    motivo = revisar(page.url, texto)
    if motivo:
        raise AlertaCuenta(motivo)


def pausar(motivo: str) -> None:
    PAUSA.write_text(
        f"Agente en pausa automática desde {datetime.now().strftime('%d-%m-%Y %H:%M')} (guardián de la cuenta).\n"
        f"Motivo: {motivo}\n"
        "Mientras exista este archivo no corren ni la corrida programada ni la revisión del correo.\n"
        "Revisa tu cuenta de LinkedIn desde la app y, solo cuando esté normal, borra este archivo.\n",
        encoding="utf-8")


def en_pausa() -> bool:
    return PAUSA.exists()

"""
huella.py — Navegador con huella coherente con la Mac (Fase 0, R6)
==================================================================
Antes cada módulo declaraba "Windows / Chrome 124" en una Mac. Ahora el user agent
se arma con la versión real del Chromium instalado y la plataforma real (macOS).
"""

import re


def user_agent(browser) -> str:
    version = getattr(browser, "version", "") or ""
    major = re.match(r"(\d+)", version)
    major = major.group(1) if major else "130"
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def opciones_contexto(browser, session_file: str | None = None) -> dict:
    opciones = dict(viewport={"width": 1366, "height": 900}, user_agent=user_agent(browser),
                    locale="es-PE", timezone_id="America/Lima")
    if session_file:
        opciones["storage_state"] = session_file
    return opciones

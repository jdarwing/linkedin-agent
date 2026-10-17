"""
secretos.py — Lectura de claves (Fase 0, R9)
===========================================
Orden de búsqueda:
  1. Llavero de macOS: servicio "linkedin-agent", cuenta = nombre de la clave.
     Para guardar una clave (en la Terminal de la Mac):
        security add-generic-password -U -s linkedin-agent -a OPENAI_API_KEY -w
     (pide la clave sin mostrarla). Luego se puede borrar esa línea del .env.
  2. Variable de entorno o .env (respaldo mientras se migra).
La contraseña de LinkedIn ya no se usa: el inicio de sesión es manual (auth.py).
"""

import os
import subprocess
import sys

SERVICIO = "linkedin-agent"


def _llavero(nombre: str) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICIO, "-a", nombre, "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def obtener(nombre: str, por_defecto: str = "") -> str:
    return _llavero(nombre) or os.getenv(nombre, por_defecto) or por_defecto

"""
logconf.py — Registro (logs) común del agente, con rotación
===========================================================
Fase 0 (D11-A, R10): un solo archivo de log que rota cada semana y conserva
13 semanas (unos 90 días). Los mensajes no llevan textos de perfiles:
para identificar a una persona se usa `ref(perfil)`, un código corto.
"""

import hashlib
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FILE = Path(os.getenv("LOG_FILE", "logs/agent.log"))
_CONFIGURADO = False


def configurar() -> None:
    global _CONFIGURADO
    if _CONFIGURADO:
        return
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if os.getenv("LOG_SOLO_CONSOLA") != "1":
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        archivo = TimedRotatingFileHandler(LOG_FILE, when="W0", backupCount=13, encoding="utf-8")
        archivo.setFormatter(fmt)
        raiz.addHandler(archivo)
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(fmt)
    raiz.addHandler(consola)
    _CONFIGURADO = True


def get(nombre: str) -> logging.Logger:
    configurar()
    return logging.getLogger(nombre)


def ref(perfil_o_url) -> str:
    """Código corto y estable para nombrar a una persona en los logs sin escribir sus datos."""
    url = perfil_o_url if isinstance(perfil_o_url, str) else (perfil_o_url or {}).get("profile_url") or ""
    url = url.strip().rstrip("/").lower()
    return "p-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:8] if url else "p-sinurl"

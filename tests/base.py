"""
base.py — Entorno aislado para las pruebas
==========================================
Cada prueba corre en una carpeta temporal con una copia de config/reglas.json.
Nunca se lee el .env real, el llavero ni la red: los módulos que tocarían
LinkedIn, OpenAI o el correo se reemplazan por dobles de prueba.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
AGENTE = RAIZ / "agent"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ── Aislamiento antes de importar cualquier módulo del agente ─────────────────
_TMP = Path(tempfile.mkdtemp(prefix="linkedin-agent-test-"))
(_TMP / "config").mkdir()
shutil.copy(RAIZ / "config" / "reglas.json", _TMP / "config" / "reglas.json")
os.chdir(_TMP)

for k in list(os.environ):
    if k.startswith(("LINKEDIN_", "OPENAI", "ANTHROPIC", "SMTP_", "IMAP_", "EVAL_")):
        del os.environ[k]
os.environ.update({
    "LOG_SOLO_CONSOLA": "1",
    "NOTIFY_EMAIL": "darwing@hotmail.com",
    "SMTP_USER": "agente.prueba@gmail.com",
    "SMTP_PASS": "clave-de-prueba",
    "OPENAI_API_KEY": "sk-prueba-no-real",
    "EVAL_PROVIDER": "openai",
    "LINKEDIN_AGENT_LIVE": "0",
})

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False          # nunca leer el .env real

try:                                                 # Playwright solo hace falta en las pruebas de DOM
    import playwright.sync_api  # noqa: F401
    HAY_PLAYWRIGHT = True
except Exception:
    HAY_PLAYWRIGHT = False
    pw = types.ModuleType("playwright"); api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: (_ for _ in ()).throw(RuntimeError("Playwright no disponible en pruebas"))
    api.TimeoutError = TimeoutError
    pw.sync_api = api
    sys.modules.setdefault("playwright", pw); sys.modules.setdefault("playwright.sync_api", api)

# Ninguna prueba puede conectarse a Gmail ni a otro servidor
import imaplib  # noqa: E402
import smtplib  # noqa: E402
import socket   # noqa: E402


def _sin_red(*a, **k):
    raise ConnectionRefusedError("Red bloqueada en las pruebas")


imaplib.IMAP4_SSL = _sin_red
smtplib.SMTP = _sin_red
socket.create_connection = _sin_red

sys.path.insert(0, str(AGENTE))
import secretos  # noqa: E402
secretos._llavero = lambda nombre: ""               # nunca leer el llavero real

import logconf  # noqa: E402,F401


class Caso(unittest.TestCase):
    """Limpia data/ entre pruebas para que cada una empiece de cero."""

    def setUp(self):
        self.tmp = _TMP
        for d in ("data", "logs"):
            shutil.rmtree(_TMP / d, ignore_errors=True)
        (_TMP / "data").mkdir()
        if not (_TMP / "config" / "reglas.json").exists():
            shutil.copy(RAIZ / "config" / "reglas.json", _TMP / "config" / "reglas.json")

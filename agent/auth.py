"""
auth.py — Autenticación de LinkedIn con Playwright
===================================================
Abre LinkedIn en un Chromium headed, inicia sesión con las
credenciales del .env y guarda las cookies en data/session.json
para que el agente principal las reutilice sin volver a loguearse.

Uso:
    python agent/auth.py

Notas:
 • Si LinkedIn pide verificación 2FA, el script pausa y espera
   que completes el paso manualmente en el navegador.
 • La ventana NO se cierra automáticamente hasta que guardas.
 • Vuelve a correr este script sólo si session.json expira
   (el agente te avisará con un error de sesión caducada).
"""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Configuración ──────────────────────────────────────────────────────────────

load_dotenv()

EMAIL    = os.getenv("LINKEDIN_EMAIL", "")
PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")
SESSION_FILE = Path(os.getenv("SESSION_FILE", "data/session.json"))

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL           = "https://www.linkedin.com/feed/"

# ── Helpers ────────────────────────────────────────────────────────────────────

def validate_env() -> None:
    missing = [k for k, v in {"LINKEDIN_EMAIL": EMAIL, "LINKEDIN_PASSWORD": PASSWORD}.items() if not v]
    if missing:
        print(f"[ERROR] Faltan variables de entorno: {', '.join(missing)}")
        print("        Copia .env.example a .env y complétalo.")
        sys.exit(1)


def human_pause(seconds: float) -> None:
    """Pausa aleatoria para imitar comportamiento humano."""
    import random
    time.sleep(seconds + random.uniform(0.3, 1.2))


def is_logged_in(page) -> bool:
    """Comprueba si ya estamos en el feed (login exitoso)."""
    return "feed" in page.url or "mynetwork" in page.url


def wait_for_2fa(page, timeout_minutes: int = 3) -> bool:
    """
    Espera que el usuario complete 2FA manualmente.
    Devuelve True si el login se completó, False si el timeout expiró.
    """
    deadline = time.time() + timeout_minutes * 60
    print(f"\n[2FA] LinkedIn pide verificación adicional.")
    print(f"      Tienes {timeout_minutes} minutos para completarla en el navegador.")
    print("      Presiona Enter aquí cuando hayas terminado, o espera el timeout.")

    # Aceptar input del usuario en hilo separado para no bloquear el chequeo
    import threading
    user_confirmed = threading.Event()

    def wait_input():
        input()
        user_confirmed.set()

    t = threading.Thread(target=wait_input, daemon=True)
    t.start()

    while time.time() < deadline:
        if user_confirmed.is_set() or is_logged_in(page):
            return True
        time.sleep(2)

    return False


# ── Flujo principal ────────────────────────────────────────────────────────────

def login_and_save_session() -> None:
    validate_env()
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    print("\n[auth] Iniciando sesión en LinkedIn...")
    print(f"[auth] Sesión se guardará en: {SESSION_FILE}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,          # ventana visible — LinkedIn detecta headless
            slow_mo=80,              # ralentiza acciones para parecer humano
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",  # oculta WebDriver flag
            ],
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-PE",
            timezone_id="America/Lima",
        )

        page = context.new_page()

        # ── 1. Ir directo al feed para detectar sesión activa ─────────────────
        print("[auth] Verificando si ya hay sesión activa...")
        try:
            page.goto(FEED_URL, wait_until="domcontentloaded", timeout=20_000)
        except PWTimeout:
            print("[ERROR] Timeout al cargar LinkedIn. Revisa tu conexión.")
            browser.close()
            sys.exit(1)

        # Esperar redirecciones
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass

        human_pause(1.5)

        # Si llegamos al feed, ya estamos logueados
        if is_logged_in(page):
            print("[auth] Sesión activa detectada — no es necesario loguearse.")
            _save_cookies(context, browser)
            return

        # Si LinkedIn nos mandó a login, completar el formulario
        print("[auth] Sin sesión activa. Iniciando login...")
        try:
            page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass

        human_pause(1.0)

        # ── 2. Completar formulario de login ──────────────────────────────────
        try:
            page.wait_for_selector("#username", timeout=12_000)
            page.fill("#username", EMAIL)
            human_pause(0.8)
            page.fill("#password", PASSWORD)
            human_pause(0.6)
            page.click('[type="submit"]')
        except Exception as e:
            if is_logged_in(page):
                print("[auth] Sesión activa detectada.")
                _save_cookies(context, browser)
                return
            print(f"[ERROR] No se encontró el formulario de login: {e}")
            browser.close()
            sys.exit(1)

        # ── 3. Esperar resultado del login ────────────────────────────────────
        human_pause(3)

        current_url = page.url

        if "checkpoint" in current_url or "challenge" in current_url or "security" in current_url:
            # LinkedIn pidió verificación adicional (2FA, SMS, email, captcha)
            success = wait_for_2fa(page)
            if not success:
                print("\n[ERROR] Timeout de verificación. Vuelve a correr auth.py.")
                browser.close()
                sys.exit(1)

        elif "feed" not in page.url and "mynetwork" not in page.url:
            # Otro estado inesperado — mostrar URL para diagnóstico
            print(f"\n[WARN] URL inesperada después del login: {page.url}")
            print("       Verifica credenciales o completa cualquier verificación en el navegador.")
            input("       Presiona Enter cuando estés en el feed de LinkedIn...")

        # ── 4. Confirmar y guardar ─────────────────────────────────────────────
        if not is_logged_in(page):
            print("[ERROR] Login fallido. Revisa las credenciales en .env")
            browser.close()
            sys.exit(1)

        print(f"\n[auth] ✓ Login exitoso. URL actual: {page.url}")
        _save_cookies(context, browser)


def _save_cookies(context, browser) -> None:
    """Guarda el estado completo del contexto (cookies + storage) en SESSION_FILE."""
    state = context.storage_state()
    SESSION_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    print(f"[auth] ✓ Sesión guardada en {SESSION_FILE}")
    print("[auth]   Cierra el navegador cuando quieras (o en 5 segundos automáticamente).\n")
    time.sleep(5)
    browser.close()


# ── Utilidad: cargar sesión guardada ──────────────────────────────────────────

def load_session(playwright, headless: bool = True):
    """
    Crea un contexto de Playwright con la sesión guardada.
    Llamar desde el agente principal:

        from agent.auth import load_session
        with sync_playwright() as p:
            context = load_session(p)
            page = context.new_page()
            ...
    """
    if not SESSION_FILE.exists():
        raise FileNotFoundError(
            f"No se encontró {SESSION_FILE}. "
            "Ejecuta 'python agent/auth.py' primero para autenticarte."
        )

    browser = playwright.chromium.launch(
        headless=headless,
        slow_mo=50,
        args=["--disable-blink-features=AutomationControlled"],
    )

    context = browser.new_context(
        storage_state=str(SESSION_FILE),
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="es-PE",
        timezone_id="America/Lima",
    )

    return browser, context


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    login_and_save_session()

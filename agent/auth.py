"""
auth.py — Autenticación de LinkedIn con Playwright
===================================================
Abre LinkedIn en un Chromium con ventana, Darwing inicia sesión A MANO y
se guardan las cookies en data/session.json para que el agente las reutilice.

Fase 0 (R9): la contraseña de LinkedIn ya no se guarda en .env ni se escribe sola.
El correo (LINKEDIN_EMAIL) solo se usa para prellenar el campo de usuario.

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

sys.path.insert(0, str(Path(__file__).parent))
import huella

# ── Configuración ──────────────────────────────────────────────────────────────

load_dotenv()

EMAIL    = os.getenv("LINKEDIN_EMAIL", "")
SESSION_FILE = Path(os.getenv("SESSION_FILE", "data/session.json"))

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL           = "https://www.linkedin.com/feed/"

# ── Helpers ────────────────────────────────────────────────────────────────────

def validate_env() -> None:
    if os.getenv("LINKEDIN_PASSWORD"):
        print("[aviso] LINKEDIN_PASSWORD sigue en .env y ya no se usa: bórrala de ese archivo.")


def human_pause(seconds: float) -> None:
    """Pausa aleatoria para imitar comportamiento humano."""
    import random
    time.sleep(seconds + random.uniform(0.3, 1.2))


def is_logged_in(page) -> bool:
    """
    Comprueba si la sesión está activa verificando tanto la URL
    como la presencia de elementos del DOM que solo existen
    cuando el usuario está autenticado.
    """
    url_ok = "feed" in page.url or "mynetwork" in page.url or "in/" in page.url
    if not url_ok:
        return False

    # Verificar que hay elementos reales de sesión autenticada
    # (la barra de navegación superior solo aparece cuando estás logueado)
    for selector in [
        "nav.global-nav",
        ".global-nav__me",
        "[data-control-name='identity_welcome_message']",
        "a[href*='/feed/']",
        "#global-nav",
    ]:
        try:
            el = page.query_selector(selector)
            if el:
                return True
        except Exception:
            pass

    return False


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

        context = browser.new_context(**huella.opciones_contexto(browser))

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

        human_pause(2.5)

        # Verificar sesión real (URL + elementos DOM)
        if is_logged_in(page):
            print("[auth] Sesión activa detectada — no es necesario loguearse.")
            _save_cookies(context, browser)
            return

        # Sin sesión válida → ir a login y pedir credenciales
        print("[auth] Sin sesión activa. Abre el navegador y loguéate manualmente.")
        print("[auth] Cuando estés en el feed de LinkedIn, presiona Enter aquí.")
        print("[auth] Iniciando login...")
        try:
            page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass

        human_pause(1.0)

        # ── 2. Navegar al login y completar formulario ────────────────────────
        try:
            page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass

        human_pause(1.5)

        # Inicio de sesión manual: solo se prellena el usuario; la contraseña la escribe Darwing
        try:
            page.wait_for_selector("#username", timeout=8_000)
            if EMAIL:
                page.fill("#username", EMAIL)
            print("[auth] Escribe tu contraseña en la ventana y completa la verificación si la pide.")
        except PWTimeout:
            print("\n[auth] Loguéate manualmente en la ventana del navegador.")

        # Manejar 2FA o verificación adicional
        if "checkpoint" in page.url or "challenge" in page.url or "security" in page.url:
            success = wait_for_2fa(page)
            if not success:
                print("\n[ERROR] Timeout de verificación. Vuelve a correr auth.py.")
                browser.close()
                sys.exit(1)

        # Si aún no estamos logueados, esperar login manual
        if not is_logged_in(page):
            print("\n[auth] Esperando que completes el login en el navegador...")
            input("       → Cuando veas tu feed de LinkedIn, presiona Enter aquí: ")
            human_pause(2.0)

        # ── 3. Confirmar y guardar ────────────────────────────────────────────
        if not is_logged_in(page):
            print("[ERROR] No se detectó la sesión iniciada. Vuelve a correr auth.py.")
            browser.close()
            sys.exit(1)

        print(f"\n[auth] ✓ Login exitoso. URL actual: {page.url}")
        _save_cookies(context, browser)


def _save_cookies(context, browser) -> None:
    """Guarda el estado completo del contexto (cookies + storage) en SESSION_FILE."""
    state = context.storage_state()
    SESSION_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.chmod(SESSION_FILE, 0o600)
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

    context = browser.new_context(**huella.opciones_contexto(browser, str(SESSION_FILE)))

    return browser, context


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    login_and_save_session()

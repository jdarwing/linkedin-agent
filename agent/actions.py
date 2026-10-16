"""
actions.py — Ejecutar acciones en LinkedIn (aceptar / ignorar solicitudes)
==========================================================================
Lee data/decisions_preview.json y actúa sobre las solicitudes pendientes:
  - accept → hace clic en el botón "Aceptar" de la tarjeta
  - ignore → registra la decisión sin tocar LinkedIn (no notifica al solicitante)
  - consult → omite (espera resolución manual vía email)

Protecciones:
  - Circuit breaker: máximo MAX_ACCEPTS_PER_SESSION aceptaciones por ejecución
  - Pausa aleatoria entre acciones (simula comportamiento humano)
  - No acepta más si LinkedIn muestra captcha o redirige a login

Uso:
    python agent/actions.py              # ejecuta las acciones reales
    python agent/actions.py --dry-run   # muestra qué haría sin hacer nada
"""

import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

SESSION_FILE      = Path(os.getenv("SESSION_FILE",   "data/session.json"))
DECISIONS_FILE    = Path(os.getenv("DECISIONS_FILE", "data/decisions_preview.json"))
DECISIONS_CSV     = Path(os.getenv("DECISIONS_CSV",  "data/decisions.csv"))
LOG_FILE          = Path(os.getenv("LOG_FILE",        "logs/agent.log"))

MAX_ACCEPTS       = int(os.getenv("MAX_ACCEPTS_PER_SESSION", "15"))
PAUSE_MIN         = float(os.getenv("PAUSE_MIN_SEC",  "3.0"))
PAUSE_MAX         = float(os.getenv("PAUSE_MAX_SEC",  "7.0"))

INVITATIONS_URL   = "https://www.linkedin.com/mynetwork/invitation-manager/received/"

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
log = logging.getLogger("actions")


# ── CSV helpers ────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "fecha", "nombre", "cargo", "conexiones_comun", "profile_url",
    "decision", "score", "reason", "fuente",
]


def append_to_csv(rows: list) -> None:
    DECISIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not DECISIONS_CSV.exists()
    with DECISIONS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Pausa humana ───────────────────────────────────────────────────────────────

def human_pause(min_s: float = None, max_s: float = None) -> None:
    lo = min_s if min_s is not None else PAUSE_MIN
    hi = max_s if max_s is not None else PAUSE_MAX
    time.sleep(lo + random.uniform(0, hi - lo))


# ── Detección de problemas ─────────────────────────────────────────────────────

def check_session_ok(page) -> bool:
    url = page.url
    if any(x in url for x in ("login", "authwall", "uas", "checkpoint")):
        log.error(f"Sesión expirada o captcha detectado. URL: {url}")
        return False
    return True


# ── JavaScript para encontrar y hacer clic en Aceptar ─────────────────────────

JS_CLICK_ACCEPT = """
(profileUrl) => {
  // Busca todos los links /in/ y encuentra el card del perfil dado
  const allLinks = Array.from(document.querySelectorAll('a[href*="/in/"]'));
  const targetLink = allLinks.find(a => {
    const href = (a.href || '').split('?')[0].replace(/\\/$/, '');
    return href === profileUrl || href.endsWith(profileUrl.split('/in/')[1] || '');
  });

  if (!targetLink) return { ok: false, reason: 'link_not_found' };

  // Subir en el DOM para encontrar el card (li o listitem)
  let card = targetLink;
  for (let i = 0; i < 16; i++) {
    card = card.parentElement;
    if (!card) break;
    const tag = card.tagName.toLowerCase();
    const role = card.getAttribute('role') || '';
    if (tag === 'li' || role === 'listitem') break;
  }
  if (!card) return { ok: false, reason: 'card_not_found' };

  // Buscar el botón Aceptar dentro del card
  const btns = Array.from(card.querySelectorAll('button'));
  const acceptBtn = btns.find(b => {
    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
    return t === 'aceptar' || t === 'accept';
  });

  if (!acceptBtn) return { ok: false, reason: 'button_not_found' };
  if (acceptBtn.disabled) return { ok: false, reason: 'button_disabled' };

  acceptBtn.click();
  return { ok: true, reason: 'clicked' };
}
"""


# ── Acción sobre un perfil ─────────────────────────────────────────────────────

def accept_profile(page, perfil: dict) -> bool:
    """
    Busca la tarjeta del perfil en la página y hace clic en Aceptar.
    Devuelve True si tuvo éxito.
    """
    profile_url = (perfil.get("profile_url") or "").strip().rstrip("/")
    nombre      = perfil.get("nombre", "?")

    if not profile_url:
        log.warning(f"  ⚠ Sin URL de perfil para {nombre} — omitiendo")
        return False

    try:
        result = page.evaluate(JS_CLICK_ACCEPT, profile_url)
        if result.get("ok"):
            log.info(f"  ✅ Aceptado: {nombre} ({profile_url})")
            return True
        else:
            reason = result.get("reason", "?")
            log.warning(f"  ⚠ No se pudo aceptar {nombre}: {reason}")

            # Fallback: scroll hacia el perfil y reintentar
            if reason in ("link_not_found", "button_not_found"):
                log.info(f"  🔄 Intentando scroll + reintento para {nombre}...")
                page.evaluate("window.scrollBy(0, 400)")
                human_pause(1.5, 2.5)
                result2 = page.evaluate(JS_CLICK_ACCEPT, profile_url)
                if result2.get("ok"):
                    log.info(f"  ✅ Aceptado (2do intento): {nombre}")
                    return True
                log.warning(f"  ✗ Falló 2do intento: {result2.get('reason')}")

            return False

    except Exception as e:
        log.error(f"  Error al aceptar {nombre}: {e}")
        return False


# ── Flujo principal ────────────────────────────────────────────────────────────

def run_actions(dry_run: bool = False, resultados: list | None = None) -> dict:
    # ── Candado de seguridad: sin autorización explícita, todo es simulación ──
    if not dry_run and os.getenv("LINKEDIN_AGENT_LIVE", "0").strip() != "1":
        log.warning("🔒 LINKEDIN_AGENT_LIVE != 1 → modo real BLOQUEADO. Se ejecuta como DRY-RUN; "
                    "no se aceptará ninguna solicitud en LinkedIn.")
        dry_run = True

    if resultados is None:
        if not DECISIONS_FILE.exists():
            log.error(f"No se encontró {DECISIONS_FILE}. Ejecuta evaluator.py primero.")
            sys.exit(1)
        resultados = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))

    to_accept  = [r for r in resultados if r.get("decision") == "accept"]
    to_ignore  = [r for r in resultados if r.get("decision") == "ignore"]
    to_consult = [r for r in resultados if r.get("decision") == "consult"]

    log.info(f"Decisiones cargadas → ✅ {len(to_accept)} aceptar | "
             f"🔴 {len(to_ignore)} ignorar | 🟡 {len(to_consult)} consultar")

    if len(to_accept) > MAX_ACCEPTS:
        log.warning(
            f"Circuit breaker: {len(to_accept)} aceptaciones supera el límite "
            f"MAX_ACCEPTS_PER_SESSION={MAX_ACCEPTS}. Procesando solo los primeros {MAX_ACCEPTS}."
        )
        to_accept = to_accept[:MAX_ACCEPTS]

    # ── Registrar los "ignore" sin abrir el navegador ─────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    ignore_rows = [
        {
            "fecha":           now_str,
            "nombre":          p.get("nombre"),
            "cargo":           p.get("cargo"),
            "conexiones_comun": p.get("conexiones_comun"),
            "profile_url":     p.get("profile_url"),
            "decision":        "ignore",
            "score":           p.get("score", ""),
            "reason":          p.get("reason", ""),
            "fuente":          "ai_auto",
        }
        for p in to_ignore
    ]

    if not dry_run and ignore_rows:
        append_to_csv(ignore_rows)
        log.info(f"✓ {len(ignore_rows)} perfiles 'ignore' registrados en CSV (sin tocar LinkedIn)")

    # ── Si no hay nada que aceptar, terminar ──────────────────────────────────
    if not to_accept:
        log.info("No hay perfiles para aceptar. Proceso finalizado.")
        return {"accepted": 0, "failed": 0, "ignored": len(to_ignore), "consult": len(to_consult)}

    if dry_run:
        print(f"\n── DRY-RUN: se aceptarían {len(to_accept)} perfiles ──")
        for i, p in enumerate(to_accept, 1):
            print(f"  {i}. {p['nombre']} (score {p['score']}) — {p['reason'][:70]}")
        print(f"  🔴 {len(to_ignore)} ignorados (solo log, sin acción en LinkedIn)")
        print(f"  🟡 {len(to_consult)} en espera de respuesta manual por email")
        return {"accepted": 0, "failed": 0, "ignored": len(to_ignore), "consult": len(to_consult)}

    # ── Abrir navegador y ejecutar aceptaciones ───────────────────────────────
    if not SESSION_FILE.exists():
        log.error(f"No se encontró {SESSION_FILE}. Ejecuta 'python agent/auth.py' primero.")
        sys.exit(1)

    accepted_rows = []
    accepted_count = 0
    failed_count   = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=50,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(SESSION_FILE),
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-PE",
            timezone_id="America/Lima",
        )
        page = context.new_page()

        # ── Navegar a invitaciones ─────────────────────────────────────────────
        log.info(f"Navegando a {INVITATIONS_URL}...")
        try:
            page.goto(INVITATIONS_URL, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("Timeout al cargar la página — continuando...")

        human_pause(2.5, 4.0)

        if not check_session_ok(page):
            browser.close()
            log.error("Sesión inválida — abortando.")
            sys.exit(1)

        # Esperar que aparezcan botones Aceptar
        try:
            page.wait_for_function(
                """() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    return btns.some(b => {
                        const t = (b.innerText || '').trim().toLowerCase();
                        return t === 'aceptar' || t === 'accept';
                    });
                }""",
                timeout=15_000,
            )
            log.info("Página de invitaciones cargada correctamente.")
        except PWTimeout:
            log.warning("No se detectaron botones de aceptar tras 15s — puede que no haya invitaciones visibles.")

        # Scroll inicial para cargar todas las tarjetas
        log.info("Scroll para cargar todos los cards...")
        prev_buttons = 0
        stalls = 0
        while stalls < 5:
            count = page.evaluate("() => document.querySelectorAll('button').length")
            if count >= prev_buttons + 3:
                stalls = 0
            else:
                stalls += 1
            prev_buttons = count
            page.evaluate("window.scrollBy(0, 700)")
            human_pause(1.0, 1.8)

        # Volver arriba para empezar desde el primero
        page.evaluate("window.scrollTo(0, 0)")
        human_pause(1.5, 2.5)

        # ── Procesar cada aceptación ───────────────────────────────────────────
        log.info(f"Iniciando aceptación de {len(to_accept)} perfiles...")

        for i, perfil in enumerate(to_accept, 1):
            nombre = perfil.get("nombre", "?")
            log.info(f"[{i}/{len(to_accept)}] Procesando: {nombre}")

            # Verificar sesión antes de cada acción
            if not check_session_ok(page):
                log.error("Sesión perdida — deteniendo proceso.")
                break

            success = accept_profile(page, perfil)

            if success:
                accepted_count += 1
                accepted_rows.append({
                    "fecha":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "nombre":          perfil.get("nombre"),
                    "cargo":           perfil.get("cargo"),
                    "conexiones_comun": perfil.get("conexiones_comun"),
                    "profile_url":     perfil.get("profile_url"),
                    "decision":        "accept",
                    "score":           perfil.get("score", ""),
                    "reason":          perfil.get("reason", ""),
                    "fuente":          "ai_auto",
                })
            else:
                failed_count += 1

            # Circuit breaker dinámico
            if accepted_count >= MAX_ACCEPTS:
                log.warning(f"Circuit breaker activado: {accepted_count} aceptaciones en esta sesión.")
                break

            # Pausa humana entre acciones (excepto después del último)
            if i < len(to_accept):
                pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
                log.info(f"  ⏳ Pausa de {pause:.1f}s antes de la siguiente...")
                time.sleep(pause)

        browser.close()

    # ── Guardar resultados ────────────────────────────────────────────────────
    all_rows = accepted_rows + ignore_rows
    if accepted_rows:
        append_to_csv(accepted_rows)

    log.info(f"\n── RESUMEN ──────────────────────────────────────────────────")
    log.info(f"  ✅ Aceptados:    {accepted_count}")
    log.info(f"  ✗  Fallidos:     {failed_count}")
    log.info(f"  🔴 Ignorados:    {len(to_ignore)}  (registrados, sin acción en LinkedIn)")
    log.info(f"  🟡 Consultados:  {len(to_consult)} (pendientes de respuesta por email)")
    log.info(f"  📄 Total en CSV: {len(all_rows)}")

    return {
        "accepted": accepted_count,
        "failed":   failed_count,
        "ignored":  len(to_ignore),
        "consult":  len(to_consult),
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    stats = run_actions(dry_run=dry_run)
    if not dry_run:
        print(f"\n✅ {stats['accepted']} aceptados | "
              f"🔴 {stats['ignored']} ignorados (log) | "
              f"🟡 {stats['consult']} pendientes por email")

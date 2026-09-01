"""
scraper.py — Extracción de solicitudes de conexión pendientes
=============================================================
Usa JavaScript para extraer datos de los cards de invitación,
resistente a cambios de clases CSS dinámicas de LinkedIn/React.

Uso:
    python agent/scraper.py              # extrae y guarda en data/pending_requests.json
    python agent/scraper.py --dry-run   # imprime sin guardar
"""

import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

SESSION_FILE = Path(os.getenv("SESSION_FILE", "data/session.json"))
DATA_FILE    = Path(os.getenv("DATA_FILE",    "data/pending_requests.json"))
LOG_FILE     = Path(os.getenv("LOG_FILE",     "logs/agent.log"))

INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/received/"
MAX_PER_RUN     = 30

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
log = logging.getLogger("scraper")


def human_pause(base: float = 1.5) -> None:
    time.sleep(base + random.uniform(0.2, 0.8))


# ── Extracción vía JavaScript ──────────────────────────────────────────────────

# Este script corre en el navegador y extrae los datos directamente del DOM.
# Busca los botones "Aceptar"/"Accept" para localizar cada card,
# luego sube en el árbol DOM para capturar nombre, cargo y empresa.
JS_EXTRACT = """
() => {
  const results = [];

  // Textos de UI a ignorar (botones, acciones, tooltips)
  const UI_NOISE = new Set([
    'aceptar','accept','ignorar','ignore','message','mensaje',
    'connect','conectar','follow','seguir','withdraw','retirar',
  ]);

  // Buscar todos los botones de aceptar para localizar los cards
  const acceptBtns = Array.from(document.querySelectorAll('button')).filter(btn => {
    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
    return txt === 'aceptar' || txt === 'accept';
  });

  for (const btn of acceptBtns) {
    try {
      // Subir en el DOM hasta encontrar el contenedor del card
      let card = btn;
      for (let i = 0; i < 14; i++) {
        card = card.parentElement;
        if (!card) break;
        const tag = card.tagName.toLowerCase();
        const role = card.getAttribute('role') || '';
        if (tag === 'li' || role === 'listitem') break;
      }
      if (!card) continue;

      const cardText = card.innerText || '';

      // ── Nombre ──────────────────────────────────────────────────────────
      let nombre = '';
      let profileUrl = '';
      const links = Array.from(card.querySelectorAll('a[href*="/in/"]'));
      for (const link of links) {
        const txt = (link.innerText || link.textContent || '').trim().replace(/\\s+/g, ' ');
        if (txt.length > 2 && txt.length < 100 && !txt.includes('\\n')) {
          nombre = txt;
          profileUrl = (link.href || '').split('?')[0].replace(/\\/$/, '');
          break;
        }
      }

      // Fallback nombre
      if (!nombre) {
        const spans = Array.from(card.querySelectorAll('span[aria-hidden="true"], strong, h3'));
        for (const s of spans) {
          const txt = (s.innerText || s.textContent || '').trim().replace(/\\s+/g, ' ');
          if (txt.length > 2 && txt.length < 80 && !txt.match(/^\\d/)) {
            nombre = txt;
            break;
          }
        }
      }

      // ── Cargo (headline de LinkedIn) ─────────────────────────────────────
      // El headline aparece como un span/p que:
      //   • no es el nombre
      //   • no contiene "quiere conectar" / "wants to connect"
      //   • no es texto de conexiones en común
      //   • no es un botón/acción
      //   • suele contener "|" o " en " o " at " o ser texto largo
      let cargo = '';
      const allSpans = Array.from(card.querySelectorAll('span, p'));
      for (const s of allSpans) {
        // Ignorar nodos que contienen hijos con texto propio (evita duplicados)
        if (s.querySelector('span, p, a')) continue;

        const raw = (s.innerText || s.textContent || '').trim();
        const txt = raw.replace(/\\s+/g, ' ');

        if (!txt || txt.length < 3 || txt.length > 300) continue;
        if (txt === nombre) continue;
        if (UI_NOISE.has(txt.toLowerCase())) continue;

        // Descartar tooltips/aria-labels repetidos del nombre
        if (nombre && txt.toLowerCase().includes(nombre.toLowerCase())) continue;

        // Descartar "quiere conectar contigo" / "wants to connect with you"
        if (
          txt.toLowerCase().includes('quiere conectar') ||
          txt.toLowerCase().includes('wants to connect') ||
          txt.toLowerCase().includes('invited you') ||
          txt.toLowerCase().includes('te invitó')
        ) continue;

        // Descartar texto de conexiones en común
        if (
          txt.toLowerCase().includes('común') ||
          txt.toLowerCase().includes('mutual') ||
          txt.toLowerCase().includes('contacto') ||
          txt.toLowerCase().includes('connection')
        ) continue;

        // Descartar números solos o textos tipo "1er grado"
        if (txt.match(/^\\d+\\s*(er|º|st|nd|rd|th)?\\s*(grado|degree)?$/i)) continue;

        // Este span es el headline
        cargo = txt;
        break;
      }

      // ── Conexiones en común ──────────────────────────────────────────────
      // LinkedIn ES: "Yoselyn Malamud y 62 contactos más en común"
      //              "3 contactos en común"
      // LinkedIn EN: "62 mutual connections"  "John and 5 mutual connections"
      let conexionesComun = 0;
      const patronesComun = [
        /(\\d+)\\s+contactos?\\s*(más\\s*)?en\\s+común/i,
        /(\\d+)\\s+conexiones?\\s*(más\\s*)?en\\s+común/i,
        /(\\d+)\\s+mutual\\s+connections?/i,
        /y\\s+(\\d+)\\s+contactos?/i,
        /and\\s+(\\d+)\\s+others?/i,
      ];
      for (const pat of patronesComun) {
        const m = cardText.match(pat);
        if (m) {
          conexionesComun = parseInt(m[1], 10);
          break;
        }
      }
      // Si hay texto de "X y N contactos" sumar 1 por la persona nombrada
      if (conexionesComun > 0 && /[A-Za-záéíóúÁÉÍÓÚñÑ]+\\s+y\\s+\\d+/i.test(cardText)) {
        conexionesComun += 1;
      }

      // ── Mensaje personalizado ────────────────────────────────────────────
      let mensaje = '';
      const msgEl = card.querySelector('[data-test-invitation-message], .invitation-card__custom-message');
      if (msgEl) mensaje = (msgEl.innerText || '').trim();

      if (nombre) {
        results.push({
          nombre,
          cargo,
          empresa: '',          // LinkedIn ya no separa cargo/empresa — el headline es suficiente
          conexiones_comun: conexionesComun,
          mensaje,
          profile_url: profileUrl,
          extraido_en: new Date().toISOString(),
        });
      }
    } catch (e) {
      // Ignorar errores en cards individuales
    }
  }

  return results;
}
"""


# ── Scroll progresivo ──────────────────────────────────────────────────────────

def scroll_and_wait(page, max_items: int = MAX_PER_RUN) -> None:
    """Hace scroll hasta que se estabilizan los botones Accept visibles."""
    prev = 0
    stalls = 0
    while stalls < 6:
        count = page.evaluate("""
            () => document.querySelectorAll('button').length
        """)
        if count >= prev + 5:
            stalls = 0
        else:
            stalls += 1
        prev = count
        page.evaluate("window.scrollBy(0, 800)")
        human_pause(1.2)


# ── Flujo principal ────────────────────────────────────────────────────────────

def scrape_invitations(dry_run: bool = False) -> list:
    if not SESSION_FILE.exists():
        log.error(f"No se encontró {SESSION_FILE}. Ejecuta 'python agent/auth.py' primero.")
        sys.exit(1)

    log.info("Iniciando extracción de solicitudes pendientes...")

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
        try:
            page.goto(INVITATIONS_URL, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("Timeout al cargar — continuando...")

        # Esperar a que aparezca al menos un botón Accept/Aceptar
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
            log.info("Botones de aceptar detectados en la página.")
        except PWTimeout:
            log.warning("No se detectaron botones de aceptar tras 15s.")

        # Verificar sesión
        if "login" in page.url or "authwall" in page.url or "uas" in page.url:
            log.error("Sesión expirada. Ejecuta 'python agent/auth.py' para renovarla.")
            browser.close()
            sys.exit(1)

        log.info(f"URL actual: {page.url}")
        human_pause(2.0)

        # ── Scroll para cargar todos los cards ────────────────────────────────
        log.info("Haciendo scroll para cargar todas las invitaciones...")
        scroll_and_wait(page, MAX_PER_RUN)

        # ── Extraer datos con JavaScript ──────────────────────────────────────
        log.info("Extrayendo datos de los cards...")
        perfiles = page.evaluate(JS_EXTRACT)

        # Limitar al máximo por sesión
        perfiles = perfiles[:MAX_PER_RUN]

        browser.close()

    log.info(f"Total extraídos: {len(perfiles)}")
    for i, p in enumerate(perfiles):
        log.info(
            f"[{i+1}] {p['nombre']} | {p['cargo']} | "
            f"{p['empresa']} | {p['conexiones_comun']} conexiones"
        )

    if not perfiles:
        log.info("No se encontraron solicitudes pendientes.")
        return []

    if not dry_run:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(
            json.dumps(perfiles, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(f"✓ Guardado en {DATA_FILE}")
    else:
        print("\n── RESULTADO DRY-RUN ──────────────────────────────────")
        print(json.dumps(perfiles, indent=2, ensure_ascii=False))

    return perfiles


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    resultados = scrape_invitations(dry_run=dry_run)
    print(f"\nTotal extraídos: {len(resultados)}")

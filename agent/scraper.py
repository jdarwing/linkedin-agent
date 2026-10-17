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
import re
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
MAX_PER_RUN     = int(os.getenv("MAX_PER_RUN", "30"))
DEBUG_HTML_FILE = Path(os.getenv("DEBUG_HTML_FILE", "data/debug_invitations.html"))
DEBUG_PERFIL_FILE = Path(os.getenv("DEBUG_PERFIL_FILE", "data/debug_perfil.html"))
ENRIQUECER      = os.getenv("ENRIQUECER_PERFILES", "1") != "0"   # visitar cada perfil nuevo para leer cargo y empresa actuales
STATE_FILE      = Path(os.getenv("CONSULT_STATE_FILE", "data/consult_state.json"))
DECISIONS_CSV   = Path(os.getenv("DECISIONS_CSV", "data/decisions.csv"))

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

# Cuenta las tarjetas reales (botón "Aceptar"/"Accept"), no todos los botones de la página
JS_COUNT_CARDS = """
() => Array.from(document.querySelectorAll('button')).filter(b => {
  const t = (b.innerText || b.textContent || '').trim().toLowerCase();
  return t === 'aceptar' || t === 'accept';
}).length
"""

# Total de invitaciones que declara LinkedIn en la página (si lo muestra), p. ej. "Recibidas (37)"
JS_TOTAL = """
() => {
  const txt = document.body.innerText || '';
  const m = txt.match(/(?:Recibidas|Received|Invitaciones|Invitations)\\s*\\(\\s*(\\d[\\d.,]*)\\s*\\)/i);
  return m ? parseInt(m[1].replace(/[.,]/g, ''), 10) : null;
}
"""

# Carga más tarjetas: lleva la última tarjeta a la vista (sirve aunque el scroll sea de un
# contenedor interno y no de la ventana) y pulsa "Mostrar más"/"Show more" si existe.
# Solo pulsa botones de paginación; nunca Aceptar/Ignorar.
JS_LOAD_MORE = """
() => {
  const accept = Array.from(document.querySelectorAll('button')).filter(b => {
    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
    return t === 'aceptar' || t === 'accept';
  });
  if (accept.length) accept[accept.length - 1].scrollIntoView({block: 'end'});
  window.scrollBy(0, 800);
  const more = Array.from(document.querySelectorAll('button')).find(b => {
    const t = (b.innerText || b.textContent || '').trim();
    return /^(mostrar|ver|cargar)\\s+más(\\s+invitaciones)?$|^(show|see|load)\\s+more(\\s+invitations)?$/i.test(t);
  });
  if (more && !more.disabled) {
    more.scrollIntoView({block: 'center'});
    more.click();
    return 'mas';
  }
  return 'scroll';
}
"""


def scroll_and_wait(page, max_items: int = MAX_PER_RUN) -> int:
    """
    Carga tarjetas hasta llegar a max_items o hasta que dejen de aparecer nuevas.
    LinkedIn muestra ~10 invitaciones por página; el resto llega con scroll
    dentro del contenedor o con el botón "Mostrar más".
    """
    prev, stalls, rondas, clics_mas = -1, 0, 0, 0
    while stalls < 4 and rondas < 40:
        count = page.evaluate(JS_COUNT_CARDS)
        if count >= max_items:
            break
        if count > prev:
            stalls = 0
        else:
            stalls += 1
        prev = count
        if page.evaluate(JS_LOAD_MORE) == "mas":
            clics_mas += 1
        rondas += 1
        human_pause(1.5)
    final = page.evaluate(JS_COUNT_CARDS)
    log.info(f"Carga de tarjetas: {final} visibles tras {rondas} rondas "
             f"({clics_mas} clics en 'Mostrar más').")
    return final


# ── Perfiles ya conocidos (para leer solo solicitudes NUEVAS) ─────────────────

def perfiles_conocidos() -> set:
    """URLs ya consultadas o ya decididas en corridas anteriores."""
    urls = set()
    try:
        if STATE_FILE.exists():
            urls |= {v.get("profile_url") for v in json.loads(STATE_FILE.read_text(encoding="utf-8"))["items"].values()}
    except Exception as e:
        log.warning(f"No se pudo leer {STATE_FILE}: {e}")
    try:
        if DECISIONS_CSV.exists():
            import csv
            with DECISIONS_CSV.open(encoding="utf-8") as f:
                urls |= {r.get("profile_url") for r in csv.DictReader(f)}
    except Exception as e:
        log.warning(f"No se pudo leer {DECISIONS_CSV}: {e}")
    return {u.rstrip("/") for u in urls if u}


# ── Enriquecimiento: cargo y empresa actuales desde el perfil ──────────────────

JS_PERFIL = """
() => {
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const lines = el => {
    const out = [];
    if (!el) return out;
    for (const l of (el.innerText || '').split('\\n')) {
      const t = clean(l);
      if (t && out[out.length - 1] !== t) out.push(t);
    }
    return out;
  };
  const res = {titular: '', empresa_cabecera: '', ubicacion: '', acerca_de: '',
               experiencia_lineas: [], tiene_foto: false};
  const main = document.querySelector('main') || document.body;
  const secciones = Array.from(main.querySelectorAll('section'));
  const titulo = s => { const h = s.querySelector('h2'); return h ? clean(h.innerText) : ''; };

  // Cabecera: la sección que tiene "Información de contacto"
  const top = secciones.find(s => /Información de contacto|Contact info/i.test(s.innerText || '')) || secciones[0];
  const tl = lines(top);
  const iC = tl.findIndex(t => /^(Información de contacto|Contact info)$/i.test(t));
  if (iC > 0) {
    let k = iC - 1;
    while (k > 0 && /^[·•]$/.test(tl[k])) k--;
    res.ubicacion = tl[k] || '';
    const lineaEmpresa = tl[k - 1] || '';
    if (lineaEmpresa.includes(' · ')) res.empresa_cabecera = lineaEmpresa.split(' · ')[0].replace(/\\s+y\\s+\\d+$|\\s+and\\s+\\d+$/i, '').trim();
    // titular: primera línea larga después del nombre y del grado de conexión
    res.titular = tl.slice(1, k - 1).find(t => t.length > 3 && !/^[·•]\\s*\\d|^[·•]$/.test(t)) || '';
  }
  res.tiene_foto = !!(top && top.querySelector('img[src*="profile-displayphoto"], img[srcset*="profile-displayphoto"]'));

  const acerca = secciones.find(s => /^(Acerca de|About)$/i.test(titulo(s)));
  if (acerca) res.acerca_de = lines(acerca).filter(t => !/^(Acerca de|About|…|más|more|see more|ver más)$/i.test(t)).join(' ').slice(0, 600);

  let exp = null;
  const ancla = document.getElementById('experience');
  if (ancla) exp = ancla.closest('section');
  if (!exp) exp = secciones.find(s => /^(Experiencia|Experience)$/i.test(titulo(s)));
  if (exp) {
    const li = exp.querySelector('li') || exp;
    res.experiencia_lineas = lines(li)
      .filter(t => !/^(Experiencia|Experience|Mostrar|Show|Ver todo|See all|…|más)$/i.test(t))
      .filter((t, i, a) => a.indexOf(t) === i)
      .slice(0, 8);
  }
  return res;
}
"""

JS_HAY_EXPERIENCIA = """
() => !!(document.getElementById('experience') ||
  Array.from(document.querySelectorAll('main section h2')).some(h => /^(Experiencia|Experience)$/i.test((h.innerText || '').trim())))
"""

_DURACION = re.compile(r"^\d+\s*(años?|mes(es)?|yrs?|years?|mos?|months?)\b", re.I)
_FECHAS   = re.compile(r"(actualidad|present|\b(19|20)\d{2}\b)", re.I)


def interpretar_experiencia(lineas: list) -> dict:
    """Saca cargo y empresa de la primera entrada de Experiencia (rol único o empresa con varios roles)."""
    if not lineas:
        return {}
    actual = any(re.search(r"actualidad|present", l, re.I) for l in lineas[:5])
    if len(lineas) >= 3 and _DURACION.search(lineas[1].split(" · ")[-1]) and not _FECHAS.search(lineas[1]):
        empresa, cargo = lineas[0], lineas[2]                # empresa con varios cargos
    else:
        cargo = lineas[0]
        empresa = lineas[1].split(" · ")[0] if len(lineas) > 1 and not _FECHAS.search(lineas[1]) else ""
    return {"cargo_actual": cargo[:120], "empresa_actual": empresa[:120], "experiencia_es_actual": actual}


def enriquecer_perfiles(page, perfiles: list, dry_run: bool = False) -> None:
    """Visita cada perfil (pausas humanas) y agrega cargo_actual, empresa_actual, ubicacion y tiene_foto."""
    for k, perfil in enumerate(perfiles, 1):
        url = perfil.get("profile_url")
        if not url:
            continue
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(random.uniform(2.5, 4.5))
            if any(x in page.url for x in ("login", "authwall", "checkpoint", "uas")):
                log.warning("⚠ LinkedIn pidió verificación al visitar perfiles: se detiene el enriquecimiento.")
                return
            for _ in range(10):                                 # la sección Experiencia carga al bajar
                if page.evaluate(JS_HAY_EXPERIENCIA):
                    break
                page.evaluate("window.scrollBy(0, 1000)")
                time.sleep(random.uniform(0.8, 1.6))
            datos = page.evaluate(JS_PERFIL)
            exp = interpretar_experiencia(datos.get("experiencia_lineas", []))
            perfil.update(exp)
            # Respaldo: la cabecera del perfil muestra la empresa actual aunque no cargue Experiencia
            if not perfil.get("empresa_actual") and datos.get("empresa_cabecera"):
                perfil["empresa_actual"] = datos["empresa_cabecera"]
            if not perfil.get("cargo_actual") and datos.get("titular"):
                perfil["cargo_actual"] = ""                     # el titular ya va en 'cargo'; no se duplica
            perfil["experiencia_reciente"] = " | ".join(datos.get("experiencia_lineas", [])[:5])
            perfil["ubicacion"] = datos.get("ubicacion", "")
            perfil["acerca_de"] = datos.get("acerca_de", "")
            perfil["tiene_foto"] = datos.get("tiene_foto", False)
            if dry_run and k == 1:
                DEBUG_PERFIL_FILE.write_text(page.content(), encoding="utf-8")
            log.info(f"  [{k}/{len(perfiles)}] {perfil['nombre']}: "
                     f"{perfil.get('cargo_actual') or '(cargo: ver titular)'} · {perfil.get('empresa_actual') or '¿empresa?'}"
                     f"{' · ' + perfil['ubicacion'] if perfil.get('ubicacion') else ''}")
        except Exception as e:
            log.warning(f"  No se pudo leer el perfil de {perfil.get('nombre')}: {e}")
        time.sleep(random.uniform(4.0, 9.0))                    # ritmo humano entre perfiles
    sin_datos = sum(1 for p in perfiles if not (p.get("cargo_actual") or p.get("empresa_actual")))
    if perfiles and sin_datos > len(perfiles) / 2:
        log.warning(f"⚠ {sin_datos} de {len(perfiles)} perfiles sin cargo actual: revisar selectores (data/debug_perfil.html).")


# ── Flujo principal ────────────────────────────────────────────────────────────

NAVEGADOR_VISIBLE = os.getenv("NAVEGADOR_VISIBLE", "0") == "1"   # 1 = ventana visible (para diagnosticar)


def abrir_navegador(p):
    """
    Por defecto, Chromium completo en modo invisible (headless "nuevo", sin ventana), para no
    interrumpir el trabajo en la Mac. Es la misma versión de Chromium que la visible, no el
    "headless shell" que LinkedIn detecta con más facilidad. Si falla, abre la ventana visible.
    """
    args = ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"]
    if not NAVEGADOR_VISIBLE:
        try:
            log.info("Navegador en segundo plano (sin ventana).")
            return p.chromium.launch(channel="chromium", headless=True, slow_mo=50, args=args)
        except Exception as e:
            log.warning(f"No se pudo abrir en segundo plano ({e}); se usa ventana visible.")
    return p.chromium.launch(headless=False, slow_mo=50, args=args)


def scrape_invitations(dry_run: bool = False) -> list:
    if not SESSION_FILE.exists():
        log.error(f"No se encontró {SESSION_FILE}. Ejecuta 'python agent/auth.py' primero.")
        sys.exit(1)

    log.info("Iniciando extracción de solicitudes pendientes...")

    with sync_playwright() as p:
        browser = abrir_navegador(p)
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
        total_declarado = page.evaluate(JS_TOTAL)
        if total_declarado is not None:
            log.info(f"LinkedIn declara {total_declarado} invitaciones recibidas.")
        conocidos = perfiles_conocidos()
        scroll_and_wait(page, min(MAX_PER_RUN + len(conocidos), 100))

        # ── Extraer datos con JavaScript ──────────────────────────────────────
        log.info("Extrayendo datos de los cards...")
        perfiles = page.evaluate(JS_EXTRACT)

        # Chequeo de cobertura: avisar si se extrajo menos de lo esperado
        if total_declarado:
            esperado = min(total_declarado, MAX_PER_RUN)
            if len(perfiles) < esperado:
                log.warning(f"⚠ COBERTURA INCOMPLETA: {len(perfiles)} extraídos de {esperado} esperados "
                            f"({total_declarado} declarados, tope {MAX_PER_RUN}).")
        else:
            log.warning("⚠ No se pudo leer el total de invitaciones en la página; cobertura no verificable.")

        # En dry-run se guarda el HTML para ajustar selectores (queda local; está en .gitignore)
        if dry_run:
            try:
                DEBUG_HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
                DEBUG_HTML_FILE.write_text(page.content(), encoding="utf-8")
                log.info(f"HTML de la página guardado en {DEBUG_HTML_FILE} (solo diagnóstico local).")
            except Exception as e:
                log.warning(f"No se pudo guardar el HTML de diagnóstico: {e}")

        # Limitar al máximo por sesión
        total_vistos = len(perfiles)
        perfiles = [p for p in perfiles if (p.get("profile_url") or "").rstrip("/") not in conocidos][:MAX_PER_RUN]
        log.info(f"{total_vistos} invitaciones visibles → {len(perfiles)} nuevas (tope {MAX_PER_RUN}).")

        if ENRIQUECER and perfiles:
            log.info("Leyendo cargo y empresa actuales de cada perfil nuevo...")
            enriquecer_perfiles(page, perfiles, dry_run=dry_run)

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

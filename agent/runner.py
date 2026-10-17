"""
runner.py — Orquestador del pipeline LinkedIn Agent
====================================================
Encadena todos los pasos en secuencia con un solo comando:

  PIPELINE COMPLETO (uso diario):
    python agent/runner.py

  SOLO REVISAR RESPUESTAS DE EMAIL (sin scraping):
    python agent/runner.py --check-replies

  SIMULAR SIN HACER NADA REAL:
    python agent/runner.py --dry-run

  SALTAR EL SCRAPER (reusar pending_requests.json existente):
    python agent/runner.py --skip-scraper

Flujo normal:
  1. scraper.py   → extrae solicitudes pendientes de LinkedIn
  2. evaluator.py → evalúa perfiles con OpenAI (accept / consult / ignore)
  3. actions.py   → acepta en LinkedIn los perfiles con decision=accept
  4. notifier.py  → envía email con perfiles "consult" (si los hay)

Flujo --check-replies (correr después de responder el email):
  1. notifier.py --check → lee respuestas IMAP y actualiza decisions.csv
  2. actions.py          → acepta los perfiles que aprobaste por email
"""

import logging
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Importar módulos del agente
sys.path.insert(0, str(Path(__file__).parent))
from scraper   import scrape_invitations
from evaluator import evaluate_all
from actions   import run_actions
from notifier  import notify_consult_profiles, check_replies, mark_executed

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

LOG_FILE       = Path(os.getenv("LOG_FILE",        "logs/agent.log"))
DECISIONS_FILE = Path(os.getenv("DECISIONS_FILE",  "data/decisions_preview.json"))
PENDING_FILE   = Path(os.getenv("PENDING_FILE",    "data/pending_consult.json"))

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
log = logging.getLogger("runner")


# ── Helpers ────────────────────────────────────────────────────────────────────

def banner(texto: str) -> None:
    linea = "─" * 60
    log.info(f"\n{linea}")
    log.info(f"  {texto}")
    log.info(linea)


def step_ok(nombre: str, resultado) -> bool:
    """Devuelve True si el paso produjo resultados útiles."""
    if resultado is None:
        log.warning(f"⚠ {nombre} no devolvió resultados.")
        return False
    if isinstance(resultado, list) and len(resultado) == 0:
        log.info(f"ℹ {nombre} completado — sin elementos para procesar.")
        return False
    return True


# ── Flujo principal ────────────────────────────────────────────────────────────

def run_pipeline(dry_run: bool = False, skip_scraper: bool = False) -> None:
    inicio = datetime.now(timezone.utc)
    log.info(f"\n{'='*60}")
    log.info(f"  LinkedIn Agent — inicio {inicio.strftime('%d/%m/%Y %H:%M UTC')}")
    log.info(f"  Modo: {'DRY-RUN' if dry_run else 'PRODUCCIÓN'}")
    log.info(f"{'='*60}\n")

    # ── PASO 0: Respuestas a correos de consulta anteriores ───────────────────
    try:
        aprobados_correo = check_replies(dry_run=dry_run)
        if aprobados_correo:
            log.info(f"✓ {len(aprobados_correo)} perfiles aprobados por Darwing (correo) pendientes de aceptar.")
    except Exception as e:
        log.error(f"Error leyendo respuestas de correo: {e}")
        aprobados_correo = []

    # ── PASO 1: Scraper ────────────────────────────────────────────────────────
    if skip_scraper:
        log.info("⏭ Paso 1/4 OMITIDO: --skip-scraper activo (usando pending_requests.json existente)")
        perfiles_raw = None  # evaluator leerá el archivo directamente
    else:
        banner("PASO 1 / 4 — Extracción de solicitudes (scraper)")
        try:
            perfiles_raw = scrape_invitations(dry_run=dry_run)
            if not step_ok("Scraper", perfiles_raw):
                log.info("No hay solicitudes pendientes. Pipeline finalizado.")
                return
            log.info(f"✓ {len(perfiles_raw)} perfiles extraídos.")
        except SystemExit as e:
            log.error(f"Scraper abortó (código {e.code}). Revisa la sesión o el archivo session.json.")
            return
        except Exception as e:
            log.error(f"Error inesperado en scraper: {e}")
            return

    # ── PASO 2: Evaluador ──────────────────────────────────────────────────────
    banner("PASO 2 / 4 — Evaluación de perfiles (evaluator)")
    try:
        resultados = evaluate_all(dry_run=dry_run, perfiles=perfiles_raw)
        if not step_ok("Evaluator", resultados):
            log.info("Sin decisiones. Pipeline finalizado.")
            return

        acepta   = [r for r in resultados if r["decision"] == "accept"]
        consulta = [r for r in resultados if r["decision"] == "consult"]
        ignora   = [r for r in resultados if r["decision"] == "ignore"]
        log.info(f"✓ Evaluación: ✅ {len(acepta)} accept | 🟡 {len(consulta)} consult | 🔴 {len(ignora)} ignore")

    except SystemExit as e:
        log.error(f"Evaluator abortó (código {e.code}). Revisa ANTHROPIC_API_KEY / OPENAI_API_KEY en .env.")
        return
    except Exception as e:
        log.error(f"Error inesperado en evaluator: {e}")
        return

    # ── PASO 3: Acciones en LinkedIn ───────────────────────────────────────────
    banner("PASO 3 / 4 — Ejecución de acciones en LinkedIn (actions)")
    try:
        stats = run_actions(dry_run=dry_run, resultados=resultados + aprobados_correo)
        mark_executed(stats.get("accepted_urls", []))
        log.info(
            f"✓ Acciones: ✅ {stats['accepted']} aceptados | "
            f"🔴 {stats['ignored']} ignorados | 🟡 {stats['consult']} pendientes email"
        )
    except SystemExit as e:
        log.error(f"Actions abortó (código {e.code}).")
        return
    except Exception as e:
        log.error(f"Error inesperado en actions: {e}")
        return

    # ── PASO 4: Notificación email (solo si hay perfiles "consult") ────────────
    if len(consulta) > 0:
        banner("PASO 4 / 4 — Envío de email (notifier)")
        try:
            notify_consult_profiles(dry_run=dry_run, resultados=resultados)
            if dry_run:
                log.info(f"✓ DRY-RUN: NO se envió email ({len(consulta)} perfiles habrían ido a consulta).")
            else:
                log.info(f"✓ Email enviado con {len(consulta)} perfiles para revisar.")
        except Exception as e:
            log.error(f"Error enviando email: {e}")
    else:
        log.info("ℹ Paso 4/4 omitido — no hay perfiles 'consult' que notificar.")

    # ── Resumen final ──────────────────────────────────────────────────────────
    duracion = (datetime.now(timezone.utc) - inicio).seconds
    log.info(f"\n{'='*60}")
    log.info(f"  Pipeline completado en {duracion}s")
    log.info(f"  ✅ Aceptados hoy:      {stats.get('accepted', 0)}")
    log.info(f"  🔴 Ignorados (log):    {stats.get('ignored', 0)}")
    log.info(f"  🟡 Pendientes email:   {len(consulta)}")
    if len(consulta) > 0 and not dry_run:
        log.info(f"  📧 Revisa tu correo: un toque por perfil, tienes 48 h para responder.")
    log.info(f"{'='*60}\n")


def run_check_replies() -> None:
    """Lee respuestas del correo y acepta en LinkedIn los perfiles aprobados (respeta el candado)."""
    inicio = datetime.now(timezone.utc)
    banner(f"LinkedIn Agent — CHECK REPLIES {inicio.strftime('%d/%m/%Y %H:%M UTC')}")
    try:
        aprobados = check_replies()
    except Exception as e:
        log.error(f"Error leyendo respuestas: {e}")
        return
    if not aprobados:
        log.info("Ningún perfil aprobado pendiente de aceptar. Nada que hacer.")
        return
    log.info(f"✓ {len(aprobados)} perfiles aprobados por correo → aceptación en LinkedIn")
    stats = {}
    try:
        stats = run_actions(dry_run=False, resultados=aprobados)   # el candado LIVE puede convertirlo en simulación
        mark_executed(stats.get("accepted_urls", []))
    except Exception as e:
        log.error(f"Error en actions: {e}")
    log.info(f"  ✅ Aceptados en LinkedIn: {stats.get('accepted', 0)} de {len(aprobados)} aprobados")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--check-replies" in args:
        run_check_replies()
    else:
        dry_run      = "--dry-run"      in args
        skip_scraper = "--skip-scraper" in args
        run_pipeline(dry_run=dry_run, skip_scraper=skip_scraper)

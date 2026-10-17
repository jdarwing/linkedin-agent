"""
runner.py — Orquestador del LinkedIn Agent
==========================================
Fase 0 · "la IA prepara, tú haces clic" (D7-A). El agente NO acepta a nadie en LinkedIn:
lee las invitaciones nuevas, las evalúa y te consulta por correo con su recomendación.
Lo que apruebas te vuelve como lista con enlaces para que lo aceptes tú en la app.

  CORRIDA COMPLETA (lunes, miércoles y viernes, vía run_scheduled.sh):
    python agent/runner.py

  ENSAYO (lee LinkedIn y evalúa, pero no envía correos ni guarda estado):
    python agent/runner.py --dry-run

  ENSAYO SIN ABRIR LINKEDIN (reusa data/pending_requests.json):
    python agent/runner.py --dry-run --skip-scraper

  SOLO REVISAR RESPUESTAS DEL CORREO (cada hora, sin abrir LinkedIn):
    python agent/runner.py --solo-correo

Pausa: si existe el archivo PAUSA en la carpeta del proyecto no corre nada, salvo un
--dry-run pedido a mano. El guardián crea PAUSA solo ante una señal de LinkedIn.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

import alertas      # noqa: E402
import guardian     # noqa: E402
import logconf      # noqa: E402
import memoria      # noqa: E402
import notifier     # noqa: E402

log = logconf.get("runner")

# Códigos de salida
OK, DEGRADADA, DETENIDA, PAUSADA = 0, 1, 2, 3


def preparar(dry_run: bool, conservar_lectura: bool = False) -> None:
    """Respaldo diario, datos mínimos y limpieza de temporales (R17, D11-A)."""
    if not conservar_lectura:
        borrados = memoria.borrar_temporales()
        if borrados:
            log.info(f"Temporales con datos de perfiles borrados: {', '.join(borrados)}")
    if dry_run:
        return
    try:
        if memoria.respaldo_diario():
            log.info("✓ Respaldo diario del estado guardado en data/respaldo/.")
        res = memoria.purgar()
        if res["fotos"] or res["anonimizados"]:
            log.info(f"Datos mínimos: {res['fotos']} fotos borradas, {res['anonimizados']} registros anonimizados.")
    except Exception as e:
        log.error(f"Error en respaldo o limpieza: {type(e).__name__}: {e}")


def registrar_lectura(resultados: list) -> None:
    """P3 / D10-A: toda decisión queda en memoria (también con el candado), así nadie se revisita."""
    version = next((r.get("version_reglas") for r in resultados if r.get("version_reglas")), "")
    nuevos = memoria.registrar(resultados, version)
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    memoria.anotar_csv([{
        "fecha": fecha, "nombre": r.get("nombre"), "cargo": r.get("cargo"),
        "conexiones_comun": r.get("conexiones_comun"), "profile_url": r.get("profile_url"),
        "decision": "ignore", "score": r.get("score", ""), "reason": r.get("reason", ""),
        "fuente": "agente_ignora"} for r in resultados if r.get("decision") == "ignore"])
    log.info(f"✓ Memoria: {nuevos} perfiles nuevos registrados.")


def run_pipeline(dry_run: bool = False, skip_scraper: bool = False) -> int:
    inicio = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info(f"LinkedIn Agent — inicio {inicio.strftime('%d/%m/%Y %H:%M UTC')} — "
             f"{'ENSAYO (dry-run)' if dry_run else 'corrida normal'} — el agente no acepta en LinkedIn")
    preparar(dry_run, conservar_lectura=skip_scraper)
    motivos: list = []

    # ── 1. Respuestas del correo ───────────────────────────────────────────────
    try:
        pendientes_clic = notifier.check_replies(dry_run=dry_run)
        if pendientes_clic:
            log.info(f"👆 {len(pendientes_clic)} aprobados esperan que los aceptes tú en la app.")
    except Exception as e:
        motivos.append(f"No se pudo revisar el correo: {type(e).__name__}.")

    # ── 2. Lectura de LinkedIn ─────────────────────────────────────────────────
    perfiles = None
    if skip_scraper:
        log.info("Lectura de LinkedIn omitida (--skip-scraper): se usa data/pending_requests.json.")
    else:
        import scraper
        try:
            perfiles = scraper.scrape_invitations(dry_run=dry_run)
        except guardian.AlertaCuenta as e:
            guardian.pausar(str(e))
            alertas.enviar("GUARDIÁN", [str(e)])
            log.error("⛔ Guardián: agente en PAUSA total hasta que Darwing lo autorice.")
            return PAUSADA
        except SystemExit:
            alertas.enviar("DETENIDA", ["Falta la sesión de LinkedIn (data/session.json): hay que correr auth.py."], dry_run)
            return DETENIDA
        except Exception as e:
            alertas.enviar("DEGRADADA", [f"La lectura de LinkedIn falló: {type(e).__name__}."] + motivos, dry_run)
            return DEGRADADA
        motivos += scraper.DIAGNOSTICO.get("alertas", [])
        if not dry_run:
            n = notifier.cerrar_aprobados_ausentes(scraper.DIAGNOSTICO.get("urls_visibles", []),
                                                   scraper.DIAGNOSTICO.get("cobertura_completa", False))
            if n:
                log.info(f"✓ {n} aprobados ya no están en Recibidas: los aceptaste en la app.")
        if not perfiles:
            log.info("No hay solicitudes nuevas.")
            return _cerrar(motivos, dry_run)

    # ── 3. Evaluación (solo recomendaciones) ───────────────────────────────────
    import evaluator
    try:
        resultados = evaluator.evaluate_all(dry_run=dry_run, perfiles=perfiles)
    except evaluator.ReglasError as e:
        alertas.enviar("DETENIDA", [f"Reglas no disponibles: {e}. No se evaluó a nadie (falla cerrada)."], dry_run)
        return DETENIDA
    except SystemExit:
        alertas.enviar("DETENIDA", ["Falta la clave del modelo de IA. No se evaluó a nadie."], dry_run)
        return DETENIDA
    except Exception as e:
        alertas.enviar("DEGRADADA", [f"La evaluación falló: {type(e).__name__}."] + motivos, dry_run)
        return DEGRADADA

    if any(r.get("decision") == "accept" for r in resultados):          # defensa en profundidad (R3)
        raise RuntimeError("El evaluador devolvió 'accept': el agente nunca acepta por su cuenta.")
    errores = [r for r in resultados if r.get("recomendacion") == "sin_evaluar"]
    if errores:
        motivos.append(f"{len(errores)} perfiles no se pudieron evaluar (error de la API o respuesta ilegible): "
                       "te llegan sin recomendación.")

    consulta = [r for r in resultados if r["decision"] == "consult"]
    recomienda = sum(1 for r in consulta if r.get("recomendacion") == "aceptar")
    ignora = sum(1 for r in resultados if r["decision"] == "ignore")
    log.info(f"Resultado: {len(consulta)} a tu decisión ({recomienda} con recomendación de aceptar) | "
             f"{ignora} ignorados por el agente (solo registro local, no toca LinkedIn).")

    if not dry_run:
        registrar_lectura(resultados)

    # ── 4. Correo de consulta ──────────────────────────────────────────────────
    if consulta:
        try:
            notifier.notify_consult_profiles(dry_run=dry_run, resultados=resultados)
        except Exception as e:
            motivos.append(f"No se pudo enviar el correo de consulta: {type(e).__name__}.")

    return _cerrar(motivos, dry_run, inicio)


def _cerrar(motivos: list, dry_run: bool, inicio: datetime | None = None) -> int:
    if motivos:
        alertas.enviar("DEGRADADA", motivos, dry_run)
    if inicio:
        log.info(f"Corrida terminada en {(datetime.now(timezone.utc) - inicio).seconds}s "
                 f"{'(DEGRADADA)' if motivos else ''}")
    return DEGRADADA if motivos else OK


def run_solo_correo() -> int:
    preparar(dry_run=False, conservar_lectura=True)
    notifier.check_replies()
    return OK


if __name__ == "__main__":
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    if guardian.en_pausa():
        if not dry_run:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Omitido: agente en PAUSA (ver archivo PAUSA).")
            sys.exit(PAUSADA)
        log.warning("Agente en PAUSA: se permite solo este ensayo pedido a mano (--dry-run).")

    if "--solo-correo" in args or "--check-replies" in args:
        sys.exit(run_solo_correo())
    sys.exit(run_pipeline(dry_run=dry_run, skip_scraper="--skip-scraper" in args))

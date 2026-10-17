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

Falla cerrada (Mesa B): antes de abrir LinkedIn se verifican las reglas, la clave del
modelo y la memoria. Si algo falla, la corrida se detiene SIN visitar a nadie y avisa.
"""

import fcntl
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


def preparar(dry_run: bool, conservar_lectura: bool = False, purgar: bool = True) -> None:
    """Respaldo diario, datos mínimos y limpieza de temporales (R17, D11-A)."""
    if not conservar_lectura:
        borrados = memoria.borrar_temporales()
        if borrados:
            log.info(f"Temporales con datos de perfiles borrados: {', '.join(borrados)}")
        try:
            memoria.borrar_fotos_huerfanas()
        except Exception as e:
            log.warning(f"No se pudieron borrar fotos sin caso abierto: {type(e).__name__}")
    if dry_run:
        return
    try:
        if memoria.respaldo_diario():
            log.info("✓ Respaldo diario del estado guardado en data/respaldo/.")
        if not purgar:
            return
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


def verificaciones_previas() -> list:
    """Todo lo que debe estar sano ANTES de abrir LinkedIn (Mesa B, hallazgos 1 y 4)."""
    import evaluator
    problemas = []
    try:
        evaluator.cargar_reglas()
    except evaluator.ReglasError as e:
        problemas.append(f"Reglas no disponibles: {e}.")
    if not evaluator.clave_disponible():
        problemas.append("Falta la clave del modelo de IA (llavero o .env).")
    try:
        memoria.conocidos()
    except memoria.MemoriaDanada as e:
        problemas.append(str(e))
    except Exception as e:
        problemas.append(f"El estado de consultas (consult_state.json) no se puede leer: {type(e).__name__}.")
    return problemas


def _motivos_correo() -> list:
    m = []
    if notifier.ULTIMA_REVISION.get("error"):
        m.append(notifier.ULTIMA_REVISION["error"])
    for r in notifier.ULTIMA_REVISION.get("rechazos", []):
        m.append(f"Se rechazó una respuesta tuya: {r}. Si fuiste tú, vuelve a enviarla.")
    return m


def run_pipeline(dry_run: bool = False, skip_scraper: bool = False) -> int:
    inicio = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info(f"LinkedIn Agent — inicio {inicio.strftime('%d/%m/%Y %H:%M UTC')} — "
             f"{'ENSAYO (dry-run)' if dry_run else 'corrida normal'} — el agente no acepta en LinkedIn")
    preparar(dry_run, conservar_lectura=skip_scraper)
    motivos: list = []

    # ── 0. Chequeo previo: sin reglas, clave o memoria sanas no se abre LinkedIn ──
    problemas = verificaciones_previas()
    if problemas:
        alertas.enviar("DETENIDA", problemas + ["No se abrió LinkedIn ni se visitó a nadie."], dry_run)
        return DETENIDA

    # ── 1. Respuestas del correo ───────────────────────────────────────────────
    try:
        pendientes_clic = notifier.check_replies(dry_run=dry_run)
        if pendientes_clic:
            log.info(f"👆 {len(pendientes_clic)} aprobados esperan que los aceptes tú en la app.")
        motivos += _motivos_correo()
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
            if alertas.enviar("GUARDIÁN", [str(e)]):
                guardian.marcar_aviso_enviado()
            log.error("⛔ Guardián: agente en PAUSA total hasta que Darwing lo autorice.")
            return PAUSADA
        except SystemExit:
            alertas.enviar("DETENIDA", ["Falta la sesión de LinkedIn (data/session.json): hay que correr auth.py."], dry_run)
            return DETENIDA
        except memoria.MemoriaDanada as e:
            alertas.enviar("DETENIDA", [str(e)], dry_run)
            return DETENIDA
        except Exception as e:
            alertas.enviar("DEGRADADA", [f"La lectura de LinkedIn falló: {type(e).__name__}."] + motivos, dry_run)
            return DEGRADADA
        motivos += scraper.DIAGNOSTICO.get("alertas", [])
        if not dry_run:
            try:
                n = notifier.cerrar_aprobados_ausentes(scraper.DIAGNOSTICO.get("urls_visibles", []),
                                                       scraper.DIAGNOSTICO.get("cobertura_completa", False))
                if n:
                    log.info(f"✓ {n} aprobados ya no están en Recibidas: los aceptaste en la app.")
            except Exception as e:
                motivos.append(f"No se pudo actualizar la lista de aprobados: {type(e).__name__}.")
        # Visitados en una corrida anterior que se cortó, o en un ensayo: se evalúan sin volver a visitarlos
        vistos = {memoria.huella_url(p.get("profile_url")) for p in perfiles or []}
        pendientes = [p for p in memoria.por_evaluar() if memoria.huella_url(p.get("profile_url")) not in vistos]
        if pendientes:
            log.info(f"{len(pendientes)} perfiles ya visitados se evalúan ahora (sin volver a visitarlos).")
            perfiles = (perfiles or []) + pendientes
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
    """Revisión horaria: solo el correo. La limpieza de datos se hace en la corrida completa (Mesa B, 8)."""
    preparar(dry_run=False, conservar_lectura=True, purgar=False)
    notifier.check_replies()
    motivos = _motivos_correo()
    if motivos:
        alertas.enviar("DEGRADADA", motivos)
        return DEGRADADA
    return OK


def main(args: list) -> int:
    dry_run = "--dry-run" in args

    if guardian.en_pausa():
        motivo = guardian.aviso_pendiente()
        if motivo and alertas.enviar("GUARDIÁN", [motivo]):       # el aviso no había salido: se reintenta
            guardian.marcar_aviso_enviado()
        if not dry_run:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Omitido: agente en PAUSA (ver archivo PAUSA).")
            return PAUSADA
        log.warning("Agente en PAUSA: se permite solo este ensayo pedido a mano (--dry-run).")

    solo_correo = "--solo-correo" in args or "--check-replies" in args
    candado = candado_ejecucion(esperar=not solo_correo)
    if candado is None:
        log.info("Otra corrida está en curso: la revisión del correo se hace en la próxima hora.")
        return OK
    if solo_correo:
        return run_solo_correo()
    return run_pipeline(dry_run=dry_run, skip_scraper="--skip-scraper" in args)


def candado_ejecucion(esperar: bool):
    """
    Evita que la revisión horaria del correo y la corrida programada escriban el estado a la vez.
    La corrida espera su turno; la revisión horaria, si está ocupado, se salta (vuelve en una hora).
    """
    ruta = Path("data/.corrida.lock")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    f = open(ruta, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if esperar else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        return None
    return f


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:                       # nada termina en silencio (Mesa B, 5)
        log.exception("Error no previsto")
        try:
            alertas.enviar("DETENIDA", [f"Error no previsto: {type(e).__name__}. Revisa logs/agent.log."])
        finally:
            sys.exit(DETENIDA)

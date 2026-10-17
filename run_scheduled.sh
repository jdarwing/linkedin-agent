#!/bin/bash
# run_scheduled.sh — Wrapper con pausa aleatoria para el pipeline LinkedIn Agent
# Ejecutar desde crontab: garantiza que corra entre 12:30 y 13:30 hora Lima
#
# Crontab entry (17:30 UTC = 12:30 Lima, lunes/miércoles/viernes):
#   30 17 * * 1,3,5 /Users/darwingcasana/Proyectos/linkedin-agent/run_scheduled.sh >> /Users/darwingcasana/Proyectos/linkedin-agent/logs/cron.log 2>&1

# ── Directorio del proyecto ────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

# ── Pausa manual (decisión D0, 17-oct-2026) ───────────────────────────────────
# Si existe el archivo PAUSA en la carpeta del proyecto, la corrida programada no se ejecuta.
# La revisión horaria del correo tampoco (guardia en runner.py). El guardián de la cuenta
# crea PAUSA solo si LinkedIn muestra verificación o aviso. Para reanudar: borrar PAUSA.
if [ -f "$PROJECT_DIR/PAUSA" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Corrida omitida: agente en PAUSA (ver archivo PAUSA)."
    exit 0
fi

# ── Pausa aleatoria: 0–150 minutos (Fase 0, R6: horario variable) ─────────────
# launchd arranca a las 12:30; la lectura real cae en cualquier momento entre 12:30 y 15:00
SLEEP_SEC=$(( (RANDOM * 32768 + RANDOM) % 9000 ))
[ "${SKIP_RANDOM_SLEEP:-0}" = "1" ] && SLEEP_SEC=0   # para pruebas manuales
SLEEP_MIN=$(( SLEEP_SEC / 60 ))
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Inicio — pausa aleatoria de ${SLEEP_MIN}m ${SLEEP_SEC}s antes de ejecutar..."
sleep "$SLEEP_SEC"

# ── Activar entorno virtual ────────────────────────────────────────────────────
source "$PROJECT_DIR/.venv/bin/activate"

# ── Verificar que el venv está activo ─────────────────────────────────────────
if ! python -c "import openai" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: entorno virtual no cargado correctamente."
    exit 1
fi

# ── Ejecutar el pipeline ───────────────────────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ejecutando pipeline LinkedIn Agent..."
python "$PROJECT_DIR/agent/runner.py"
EXIT_CODE=$?

# 0 = normal · 1 = DEGRADADA (se avisó por correo) · 2 = DETENIDA · 3 = en PAUSA
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pipeline finalizado (código de salida: $EXIT_CODE)"
exit $EXIT_CODE

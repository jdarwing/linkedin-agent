#!/bin/bash
# run_scheduled.sh — Wrapper con pausa aleatoria para el pipeline LinkedIn Agent
# Ejecutar desde crontab: garantiza que corra entre 12:30 y 13:30 hora Lima
#
# Crontab entry (17:30 UTC = 12:30 Lima, lunes/miércoles/viernes):
#   30 17 * * 1,3,5 /Users/darwingcasana/Proyectos/linkedin-agent/run_scheduled.sh >> /Users/darwingcasana/Proyectos/linkedin-agent/logs/cron.log 2>&1

# ── Directorio del proyecto ────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

# ── Pausa aleatoria: 0–60 minutos ─────────────────────────────────────────────
# Esto distribuye la ejecución aleatoriamente dentro de la ventana 12:30–13:30
SLEEP_SEC=$(( RANDOM % 3600 ))
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

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pipeline finalizado (código de salida: $EXIT_CODE)"
exit $EXIT_CODE

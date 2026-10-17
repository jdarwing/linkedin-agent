"""
evaluator.py — Evaluación de perfiles con Claude API
=====================================================
Lee data/pending_requests.json, llama a la API de Anthropic
por cada perfil y devuelve la decisión: accept / consult / ignore.

Uso:
    python agent/evaluator.py              # evalúa y guarda en data/decisions_preview.json
    python agent/evaluator.py --dry-run   # imprime sin guardar
"""

import json
import logging
import re
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

DATA_FILE     = Path(os.getenv("DATA_FILE",     "data/pending_requests.json"))
DECISIONS_FILE = Path(os.getenv("DECISIONS_FILE", "data/decisions_preview.json"))
LOG_FILE      = Path(os.getenv("LOG_FILE",      "logs/agent.log"))

DRYRUN_DECISIONS_FILE = Path(os.getenv("DRYRUN_DECISIONS_FILE", "data/dryrun_decisions.json"))

def _real_key(v: str) -> str:
    v = (v or "").strip()
    return "" if (not v or v.endswith("...") or "aqui" in v) else v

ANTHROPIC_API_KEY = _real_key(os.getenv("ANTHROPIC_API_KEY", ""))
OPENAI_API_KEY    = _real_key(os.getenv("OPENAI_API_KEY", ""))
# Proveedor: Claude por defecto si hay clave de Anthropic; OpenAI como respaldo.
PROVIDER = (os.getenv("EVAL_PROVIDER") or ("anthropic" if ANTHROPIC_API_KEY else "openai")).lower()
MODEL    = os.getenv("EVAL_MODEL") or ("claude-sonnet-5-5" if PROVIDER == "anthropic" else "gpt-4o")
MAX_TOKENS        = 300
PAUSE_BETWEEN     = 1.0   # segundos entre llamadas (rate limit)

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
log = logging.getLogger("evaluator")

# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
Eres el asistente de Darwing, Gerente General de Agrícola Don Ricardo, empresa agro-exportadora de uvas y arándanos en Ica, Perú. Tu tarea es evaluar solicitudes de conexión de LinkedIn y decidir: accept, consult o ignore.

Evalúa cada perfil con un score de 1 a 10. Score ≥ 7 → accept. Score 4–6 → consult. Score ≤ 3 → ignore. Responde siempre en JSON: {"decision": "accept|consult|ignore", "score": N, "reason": "..."}

PRIORIDAD MÁXIMA (score 10, siempre accept sin analizar nada más): Si el headline menciona "Agrícola Don Ricardo", "ADR" o "Nisira" — son la empresa del usuario y su aliada estratégica. Cualquier persona que trabaje o haya trabajado allí es conexión prioritaria absoluta.

Priorizar (aceptar): agroindustria y agro-export global, producción y comercialización de uvas de mesa y arándanos en cualquier país productor (Perú, Chile, Sudáfrica, España, EE.UU., Australia, etc.), packing y postcosecha, brokers y traders de fruta fresca en cualquier mercado mundial, fondos de inversión en agro o sostenibilidad a nivel global (PE, VC, ESG, impact funds), compradores internacionales de fruta fresca, sostenibilidad y gestión del agua, certificaciones (GlobalG.A.P., Rainforest, BRC), instituciones peruanas del sector (SENASA, MIDAGRI, ANA, ADEX), liderazgo empresarial, innovación agrícola, IA aplicada al agro (precision farming, visión computacional, AgTech), transformación digital en agroindustria, directivos y gerentes, conexiones en común (≥3 es señal fuerte), mensajes personalizados.

Consultar si: reclutadores especializados, proveedores tech agro, salud/educación en Ica (CSR), perfil incompleto con cargo relevante, contacto internacional en agro o retail de frutas, inmobiliaria con conexiones en Ica o perfil directivo, tecnología de otros sectores si el cargo es directivo o tiene foco en Latinoamérica, consultoras globales con práctica en agro.

Ignorar si: MLM, venta directa de seguros/créditos, sin foto + sin empresa + sin conexiones, tecnología de consumo sin aplicación en agro (salvo directivos/LATAM → consultar), sector financiero especulativo (crypto, forex), gaming, moda, perfil creado hace menos de 30 días sin conexiones.

La duda siempre va a consult, nunca a ignore.

Responde SOLO con el JSON, sin texto adicional.
""".strip()


# ── Evaluación de un perfil ────────────────────────────────────────────────────

def build_user_message(perfil: dict) -> str:
    """Construye el mensaje para Claude con los datos del perfil."""
    nombre   = perfil.get("nombre", "Desconocido")
    cargo    = perfil.get("cargo", "")
    empresa  = perfil.get("empresa", "")
    conexiones = perfil.get("conexiones_comun", 0)
    mensaje  = perfil.get("mensaje", "")
    url      = perfil.get("profile_url", "")

    partes = [f"Nombre: {nombre}"]
    if cargo:
        partes.append(f"Headline: {cargo}")
    if perfil.get("cargo_actual"):
        partes.append(f"Cargo actual (sección Experiencia): {perfil['cargo_actual']}")
    if perfil.get("empresa_actual") or empresa:
        partes.append(f"Empresa actual: {perfil.get('empresa_actual') or empresa}")
    if perfil.get("experiencia_reciente"):
        partes.append(f"Experiencia más reciente (texto del perfil): {perfil['experiencia_reciente'][:400]}")
    if perfil.get("estudios"):
        partes.append(f"Estudios (no muestra empresa actual): {perfil['estudios']}")
    if perfil.get("acerca_de"):
        partes.append(f"Acerca de (resumen del perfil): {perfil['acerca_de'][:400]}")
    if perfil.get("ubicacion"):
        partes.append(f"Ubicación: {perfil['ubicacion']}")
    if "tiene_foto" in perfil:
        partes.append(f"Tiene foto de perfil: {'sí' if perfil['tiene_foto'] else 'no'}")
    partes.append(f"Conexiones en común: {conexiones}")
    if mensaje:
        partes.append(f"Mensaje personalizado: {mensaje}")
    if url:
        partes.append(f"Perfil: {url}")

    return "\n".join(partes)


def make_client():
    if PROVIDER == "anthropic":
        import anthropic
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    import openai
    return openai.OpenAI(api_key=OPENAI_API_KEY)


def _call_model(client, user_msg: str) -> str:
    if PROVIDER == "anthropic":
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content.strip()


def evaluate_profile(client, perfil: dict) -> dict:
    """Llama al modelo y devuelve la decisión parseada."""
    user_msg = build_user_message(perfil)
    raw = ""

    try:
        raw = _call_model(client, user_msg)
        m = re.search(r"\{.*\}", raw, re.S)   # tolera ```json``` u otro texto alrededor
        resultado = json.loads(m.group(0) if m else raw)

        # Validar campos mínimos
        decision = resultado.get("decision", "consult").lower()
        if decision not in {"accept", "consult", "ignore"}:
            decision = "consult"
        score  = int(resultado.get("score", 5))
        reason = resultado.get("reason", "Sin razón")

        return {
            "nombre":     perfil.get("nombre"),
            "cargo":      perfil.get("cargo"),
            "cargo_actual":   perfil.get("cargo_actual"),
            "empresa_actual": perfil.get("empresa_actual"),
            "ubicacion":      perfil.get("ubicacion"),
            "empresa":    perfil.get("empresa"),
            "conexiones_comun": perfil.get("conexiones_comun"),
            "profile_url": perfil.get("profile_url"),
            "decision":   decision,
            "score":      score,
            "reason":     reason,
            "raw_response": raw,
        }

    except json.JSONDecodeError as e:
        log.warning(f"JSON inválido para {perfil.get('nombre')}: {e} | raw: {raw[:200]}")
        return _fallback(perfil, "consult", 5, f"Error parsing JSON: {e}")
    except Exception as e:
        log.error(f"Error evaluando {perfil.get('nombre')}: {e}")
        return _fallback(perfil, "consult", 5, f"Error API: {e}")


def _fallback(perfil: dict, decision: str, score: int, reason: str) -> dict:
    return {
        "nombre":     perfil.get("nombre"),
        "cargo":      perfil.get("cargo"),
        "cargo_actual":   perfil.get("cargo_actual"),
        "empresa_actual": perfil.get("empresa_actual"),
        "ubicacion":      perfil.get("ubicacion"),
        "empresa":    perfil.get("empresa"),
        "conexiones_comun": perfil.get("conexiones_comun"),
        "profile_url": perfil.get("profile_url"),
        "decision":   decision,
        "score":      score,
        "reason":     reason,
        "raw_response": "",
    }


# ── Flujo principal ────────────────────────────────────────────────────────────

def evaluate_all(dry_run: bool = False, perfiles: list | None = None) -> list:
    key = ANTHROPIC_API_KEY if PROVIDER == "anthropic" else OPENAI_API_KEY
    if not key:
        log.error("Falta una clave real en .env: ANTHROPIC_API_KEY (Claude) u OPENAI_API_KEY.")
        sys.exit(1)

    if perfiles is None:
        if not DATA_FILE.exists():
            log.error(f"No se encontró {DATA_FILE}. Ejecuta 'python agent/scraper.py' primero.")
            sys.exit(1)
        perfiles = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    log.info(f"Evaluando {len(perfiles)} perfiles con {PROVIDER} ({MODEL})...")

    client = make_client()
    resultados = []

    for i, perfil in enumerate(perfiles, 1):
        nombre = perfil.get("nombre", "?")
        log.info(f"[{i}/{len(perfiles)}] Evaluando: {nombre}")

        resultado = evaluate_profile(client, perfil)
        resultados.append(resultado)

        emoji = {"accept": "✅", "consult": "🟡", "ignore": "🔴"}.get(resultado["decision"], "❓")
        log.info(
            f"  {emoji} {resultado['decision'].upper()} (score {resultado['score']}) — "
            f"{resultado['reason'][:80]}"
        )

        if i < len(perfiles):
            time.sleep(PAUSE_BETWEEN)

    # ── Resumen ───────────────────────────────────────────────────────────────
    acepta   = [r for r in resultados if r["decision"] == "accept"]
    consulta = [r for r in resultados if r["decision"] == "consult"]
    ignora   = [r for r in resultados if r["decision"] == "ignore"]

    log.info(f"\n── RESUMEN ──────────────────────────────────────────────────")
    log.info(f"  ✅ Aceptar:   {len(acepta)}")
    log.info(f"  🟡 Consultar: {len(consulta)}")
    log.info(f"  🔴 Ignorar:   {len(ignora)}")

    if dry_run:
        # Archivo aparte: nunca lo lee actions.py en modo real
        DRYRUN_DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        DRYRUN_DECISIONS_FILE.write_text(
            json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(f"✓ DRY-RUN: decisiones guardadas para revisión en {DRYRUN_DECISIONS_FILE}")
    else:
        DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        DECISIONS_FILE.write_text(
            json.dumps(resultados, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(f"✓ Decisiones guardadas en {DECISIONS_FILE}")

    return resultados


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    evaluate_all(dry_run=dry_run)

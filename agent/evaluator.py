"""
evaluator.py — Evaluación de perfiles con Claude API
=====================================================
Lee data/pending_requests.json, llama al modelo por cada perfil y devuelve
su lectura: accept / consult / ignore, o "error" si no pudo evaluar.

Fase 0:
  - R14: un error de la API o una respuesta ilegible es "error". Ninguna regla lo sube
    y el perfil va a Darwing sin recomendación; la corrida se marca DEGRADADA.
  - R16: si falta config/reglas.json o está incompleto, la corrida se detiene (falla cerrada).
  - El resultado del modelo y de las reglas es solo una RECOMENDACIÓN: el agente ya no acepta.

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

import logconf
import secretos

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

DATA_FILE     = Path(os.getenv("DATA_FILE",     "data/pending_requests.json"))
DECISIONS_FILE = Path(os.getenv("DECISIONS_FILE", "data/decisions_preview.json"))
LOG_FILE      = Path(os.getenv("LOG_FILE",      "logs/agent.log"))

DRYRUN_DECISIONS_FILE = Path(os.getenv("DRYRUN_DECISIONS_FILE", "data/dryrun_decisions.json"))

def _real_key(v: str) -> str:
    v = (v or "").strip()
    return "" if (not v or v.endswith("...") or "aqui" in v) else v

ANTHROPIC_API_KEY = _real_key(secretos.obtener("ANTHROPIC_API_KEY"))
OPENAI_API_KEY    = _real_key(secretos.obtener("OPENAI_API_KEY"))
REGLAS_FILE       = Path(os.getenv("REGLAS_FILE", "config/reglas.json"))
# Proveedor: Claude por defecto si hay clave de Anthropic; OpenAI como respaldo.
PROVIDER = (os.getenv("EVAL_PROVIDER") or ("anthropic" if ANTHROPIC_API_KEY else "openai")).lower()
MODEL    = os.getenv("EVAL_MODEL") or ("claude-sonnet-5-5" if PROVIDER == "anthropic" else "gpt-4o")
MAX_TOKENS        = 300
PAUSE_BETWEEN     = 1.0   # segundos entre llamadas (rate limit)

# ── Logging ────────────────────────────────────────────────────────────────────

log = logconf.get("evaluator")

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
    if perfil.get("trayectoria"):
        partes.append(f"Trayectoria completa (texto de su experiencia en LinkedIn): {perfil['trayectoria'][:900]}")
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
        score  = int(resultado.get("score", 5))   # un puntaje no numérico cuenta como respuesta ilegible
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

    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
        log.warning(f"Respuesta ilegible del modelo para {logconf.ref(perfil)}: {type(e).__name__}")
        return _fallback(perfil, "error", None, "El agente no pudo evaluar este perfil (respuesta ilegible del modelo).")
    except Exception as e:
        log.error(f"Error de la API evaluando {logconf.ref(perfil)}: {type(e).__name__}: {str(e)[:120]}")
        return _fallback(perfil, "error", None, "El agente no pudo evaluar este perfil (error de la API).")


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

# ── Regla D5 (decisión de Darwing): quien DICE ser de ADR o del grupo va a consulta ─────

def _sin_tildes(x: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", x or "") if unicodedata.category(c) != "Mn").lower()


class ReglasError(Exception):
    """config/reglas.json falta, no se puede leer o le faltan secciones: la corrida no sigue."""


SECCIONES_OBLIGATORIAS = ("D5_empresas_propias_y_grupo", "trayectoria")


def cargar_reglas(ruta: Path | None = None) -> dict:
    """Falla cerrada (R16): sin reglas válidas no se evalúa a nadie."""
    ruta = Path(ruta or REGLAS_FILE)
    try:
        cfg = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReglasError(f"No existe {ruta}")
    except Exception as ex:
        raise ReglasError(f"No se pudo leer {ruta}: {type(ex).__name__}")
    faltan = [k for k in SECCIONES_OBLIGATORIAS if not isinstance(cfg.get(k), dict)]
    if faltan:
        raise ReglasError(f"A {ruta} le faltan secciones: {', '.join(faltan)}")
    if not cfg["D5_empresas_propias_y_grupo"].get("empresas"):
        raise ReglasError(f"{ruta} no trae la lista de empresas del grupo (D5)")
    return cfg


def _coinciden(texto: str, terminos: list) -> list:
    """Términos (empresas o palabras) que aparecen como palabra completa, sin importar tildes ni mayúsculas."""
    t = _sin_tildes(texto)
    vistos = []
    for term in terminos:
        if term and re.search(r"(?<![\w])" + re.escape(_sin_tildes(term)) + r"(?![\w])", t) and term not in vistos:
            vistos.append(term)
    return vistos


def señales_trayectoria(perfil: dict, cfg: dict) -> dict:
    """Detecta empresas del grupo Frutura/ADR y del sector en el perfil y en toda su experiencia."""
    grupo = cfg.get("D5_empresas_propias_y_grupo", {}).get("empresas", [])
    tray = cfg.get("trayectoria", {})
    actual = " ".join(str(perfil.get(k) or "") for k in ("cargo", "empresa_actual", "cargo_actual"))
    historia = " ".join(str(perfil.get(k) or "") for k in ("trayectoria", "experiencia_reciente", "acerca_de"))
    def grupo_en(texto):
        return [e["nombre"] for e in grupo if _coinciden(texto, [e["nombre"]] + e.get("variantes", []))]
    sector_emp = tray.get("empresas_sector", [])
    sector_pal = tray.get("palabras_sector", [])
    return {
        "grupo_actual":   grupo_en(actual),
        "grupo_historia": grupo_en(historia),
        "sector":         _coinciden(actual + " " + historia, sector_emp)[:4],
        "sector_palabras": _coinciden(actual + " " + historia, sector_pal)[:4],
    }


def aplicar_reglas(resultado: dict, perfil: dict, cfg: dict | None = None) -> dict:
    """
    Reglas deterministas de Darwing sobre la evaluación del modelo:
      D5 + trayectoria (17-oct): grupo Frutura/ADR → aceptar con >=10 contactos en común; si no, consultar con marca.
      Sector (empresas o vocabulario del sector) → +10 en común: aceptar; nunca ignorar (pasa a consultar).
    """
    if resultado.get("decision") == "error":
        return resultado                    # R14: un error es error; ninguna regla lo sube
    cfg = cfg if cfg is not None else cargar_reglas()
    verificados = {u.rstrip("/") for u in cfg.get("D5_empresas_propias_y_grupo", {}).get("colaboradores_verificados", [])}
    minimo = int(cfg.get("trayectoria", {}).get("min_contactos_comun", 10))
    try:
        comunes = int(perfil.get("conexiones_comun") or 0)
    except (TypeError, ValueError):
        comunes = 0
    sen = señales_trayectoria(perfil, cfg)
    resultado["señales"] = sen
    previo = resultado.get("reason") or ""

    if (perfil.get("profile_url") or "").rstrip("/") in verificados:
        resultado.update(decision="accept", reason="Colaborador verificado del grupo. " + previo)
        return resultado

    grupo = sen["grupo_actual"] or sen["grupo_historia"]
    if grupo:
        verbo = "Trabaja en" if sen["grupo_actual"] else "Trabajó en"
        if comunes >= minimo:
            resultado.update(decision="accept", score=max(int(resultado.get("score") or 0), 8),
                             reason=f"{verbo} {grupo[0]} (grupo) y tienen {comunes} contactos en común. " + previo)
        else:
            resultado.update(decision="consult",
                             reason=f"Dice que {verbo.lower()} {grupo[0]} (sin verificar, {comunes} en común): confírmalo tú. " + previo)
        return resultado

    sector = sen["sector"] or sen["sector_palabras"]
    if sector:
        etiqueta = ", ".join(sector[:3])
        if resultado.get("decision") == "ignore":
            resultado.update(decision="consult", reason=f"Tiene trayectoria en el sector ({etiqueta}); no se ignora. " + previo)
        elif resultado.get("decision") == "consult" and comunes >= minimo:
            resultado.update(decision="accept", score=max(int(resultado.get("score") or 0), 7),
                             reason=f"Trayectoria en el sector ({etiqueta}) y {comunes} contactos en común. " + previo)
    return resultado


RECOMENDACION = {"accept": "aceptar", "consult": "consultar", "ignore": "ignorar", "error": "sin_evaluar"}


def a_recomendacion(resultado: dict) -> dict:
    """
    R3 (Fase 0): el agente ya no acepta por su cuenta. Lo que antes era "accept" pasa a
    consulta con la recomendación "aceptar"; un error pasa a consulta sin recomendación.
    "ignore" sigue siendo solo un registro local: no toca LinkedIn ni avisa a la persona.
    """
    lectura = resultado.get("decision")
    resultado["lectura_agente"] = lectura
    resultado["recomendacion"] = RECOMENDACION.get(lectura, "sin_evaluar")
    resultado["decision"] = "ignore" if lectura == "ignore" else "consult"
    return resultado


def evaluate_all(dry_run: bool = False, perfiles: list | None = None, client=None) -> list:
    cfg = cargar_reglas()                   # R16: antes de cualquier llamada; si falla, no se evalúa a nadie
    key = ANTHROPIC_API_KEY if PROVIDER == "anthropic" else OPENAI_API_KEY
    if client is None and not key:
        log.error("Falta una clave real: ANTHROPIC_API_KEY (Claude) u OPENAI_API_KEY (llavero o .env).")
        sys.exit(1)

    if perfiles is None:
        if not DATA_FILE.exists():
            log.error(f"No se encontró {DATA_FILE}. Ejecuta 'python agent/scraper.py' primero.")
            sys.exit(1)
        perfiles = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    log.info(f"Evaluando {len(perfiles)} perfiles con {PROVIDER} ({MODEL})...")

    client = client or make_client()
    version = cfg.get("_version", "")
    resultados = []

    for i, perfil in enumerate(perfiles, 1):
        resultado = a_recomendacion(aplicar_reglas(evaluate_profile(client, perfil), perfil, cfg))
        resultado["version_reglas"] = version
        for k in ("foto", "conexiones_comun"):
            if perfil.get(k) is not None:
                resultado[k] = perfil.get(k)
        resultados.append(resultado)

        log.info(f"[{i}/{len(perfiles)}] {logconf.ref(perfil)}: recomendación {resultado['recomendacion']} (score {resultado['score']})")

        if i < len(perfiles):
            time.sleep(PAUSE_BETWEEN)

    # ── Resumen ───────────────────────────────────────────────────────────────
    acepta   = [r for r in resultados if r["lectura_agente"] == "accept"]
    consulta = [r for r in resultados if r["lectura_agente"] == "consult"]
    ignora   = [r for r in resultados if r["lectura_agente"] == "ignore"]
    errores  = [r for r in resultados if r["lectura_agente"] == "error"]

    log.info(f"Lectura del modelo + reglas: {len(acepta)} recomendaría aceptar | {len(consulta)} consultar | "
             f"{len(ignora)} ignorar | {len(errores)} con error")

    if dry_run:
        # Para que Darwing revise el dry-run. Es temporal: se borra al inicio de la próxima corrida (D11-A).
        DRYRUN_DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        DRYRUN_DECISIONS_FILE.write_text(
            json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(f"✓ DRY-RUN: recomendaciones guardadas para revisión en {DRYRUN_DECISIONS_FILE}")

    return resultados


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    evaluate_all(dry_run=dry_run)

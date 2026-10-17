"""
memoria.py — Memoria completa, respaldo y datos mínimos (Fase 0)
================================================================
  - P3 / D10-A: se registra TODA decisión (también con el candado puesto), así cada
    perfil se visita una sola vez. Archivo: data/memoria.json, una entrada por URL.
    La visita se anota ANTES de abrir el perfil, y lo leído queda guardado hasta que se
    evalúa: si la corrida se corta, la siguiente evalúa sin volver a visitar (Mesa B, 2).
    Si la memoria está dañada, la corrida se detiene antes de abrir LinkedIn (Mesa B, 4).
  - R17: respaldo diario local de consult_state.json, decisions.csv y memoria.json
    en data/respaldo/AAAA-MM-DD/ (se conservan 14 días).
  - D11-A: fotos y páginas de diagnóstico se borran al cerrar cada caso; lo no aceptado
    se conserva 90 días y después se anonimiza (queda solo un código, para no volver a visitarlo).
"""

import csv
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR        = Path(os.getenv("DATA_DIR", "data"))
MEMORIA_FILE    = Path(os.getenv("MEMORIA_FILE", str(DATA_DIR / "memoria.json")))
STATE_FILE      = Path(os.getenv("CONSULT_STATE_FILE", str(DATA_DIR / "consult_state.json")))
DECISIONS_CSV   = Path(os.getenv("DECISIONS_CSV", str(DATA_DIR / "decisions.csv")))
RESPALDO_DIR    = Path(os.getenv("RESPALDO_DIR", str(DATA_DIR / "respaldo")))
FOTOS_DIR       = Path(os.getenv("FOTOS_DIR", str(DATA_DIR / "fotos")))
DIAS_RETENCION  = int(os.getenv("DIAS_RETENCION", "90"))
DIAS_RESPALDO   = 14
TEMPORALES      = ["debug_invitations.html", "debug_perfil.html", "debug_experiencia.html",
                   "email_preview.html", "pending_requests.json", "decisions_preview.json",
                   "dryrun_decisions.json", "pending_consult.json"]

CSV_FIELDS = ["fecha", "nombre", "cargo", "conexiones_comun", "profile_url",
              "decision", "score", "reason", "fuente"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def norm(url: str) -> str:
    return (url or "").strip().split("?")[0].rstrip("/").lower()


def huella_url(url: str) -> str:
    if (url or "").startswith("h:"):          # ya anonimizada
        return url
    return "h:" + hashlib.sha256(norm(url).encode("utf-8")).hexdigest()[:20]


# ── Memoria ────────────────────────────────────────────────────────────────────

class MemoriaDanada(Exception):
    """data/memoria.json existe pero no se puede leer: no se abre LinkedIn hasta restaurarla."""


def cargar() -> dict:
    if not MEMORIA_FILE.exists():
        return {"perfiles": {}}
    try:
        mem = json.loads(MEMORIA_FILE.read_text(encoding="utf-8"))
        if not isinstance(mem.get("perfiles"), dict):
            raise ValueError("sin sección 'perfiles'")
        return mem
    except Exception as e:
        raise MemoriaDanada(f"{MEMORIA_FILE} está dañado ({type(e).__name__}). "
                            f"Se puede restaurar desde {RESPALDO_DIR}/<fecha>/memoria.json")


def _privado(ruta: Path) -> None:
    try:
        os.chmod(ruta, 0o600)
    except Exception:
        pass


def guardar(mem: dict) -> None:
    MEMORIA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMORIA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(mem, indent=1, ensure_ascii=False), encoding="utf-8")
    _privado(tmp)
    tmp.replace(MEMORIA_FILE)


def marcar_visita(perfil: dict) -> None:
    """Se llama ANTES de abrir el perfil: aunque la corrida se corte, no se vuelve a visitar."""
    url = norm(perfil.get("profile_url"))
    if not url:
        return
    mem = cargar()
    clave = huella_url(url)
    if clave not in mem["perfiles"]:
        mem["perfiles"][clave] = {"url": url, "visto": _now().isoformat(), "estado": "por_evaluar",
                                  "datos": dict(perfil)}
        guardar(mem)


def guardar_lectura(perfil: dict) -> None:
    """Guarda lo leído del perfil hasta que se evalúe (luego se descarta: D11-A)."""
    mem = cargar()
    e = mem["perfiles"].get(huella_url(perfil.get("profile_url")))
    if e is not None and e.get("estado") == "por_evaluar":
        e["datos"] = dict(perfil)
        guardar(mem)


def por_evaluar() -> list:
    """Perfiles ya visitados que todavía no se evaluaron (corrida cortada o ensayo)."""
    return [dict(e["datos"]) for e in cargar()["perfiles"].values()
            if e.get("estado") == "por_evaluar" and e.get("datos")]


def registrar(resultados: list, version: str = "") -> int:
    """Guarda la decisión del agente de cada perfil evaluado. Devuelve cuántos son nuevos."""
    mem = cargar()
    nuevos = 0
    for r in resultados:
        url = norm(r.get("profile_url"))
        if not url:
            continue
        clave = huella_url(url)
        if clave not in mem["perfiles"]:
            nuevos += 1
        previo = mem["perfiles"].get(clave, {})
        mem["perfiles"][clave] = {
            "url": url,
            "visto": previo.get("visto") or _now().isoformat(),
            "evaluado": _now().isoformat(),
            "estado": "evaluado",
            "decision_agente": r.get("decision"),
            "recomendacion": r.get("recomendacion", ""),
            "version_reglas": version,
        }
    guardar(mem)
    return nuevos


def conocido(url: str, mem: dict | None = None) -> bool:
    mem = mem if mem is not None else cargar()
    return huella_url(url) in mem["perfiles"]


def conocidos() -> set:
    """Huellas de todas las URLs ya vistas (memoria, consultas y CSV). Si algo está dañado, falla."""
    claves = set(cargar()["perfiles"].keys())
    if STATE_FILE.exists():
        for v in json.loads(STATE_FILE.read_text(encoding="utf-8"))["items"].values():
            if v.get("profile_url"):
                claves.add(huella_url(v["profile_url"]))
    try:
        if DECISIONS_CSV.exists():
            with DECISIONS_CSV.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("profile_url"):
                        claves.add(huella_url(row["profile_url"]))
    except Exception:
        pass
    return claves


def anotar_csv(rows: list) -> None:
    if not rows:
        return
    DECISIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not DECISIONS_CSV.exists()
    with DECISIONS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerows(rows)
    _privado(DECISIONS_CSV)


# ── Respaldo diario (R17) ──────────────────────────────────────────────────────

def respaldo_diario() -> Path | None:
    hoy = RESPALDO_DIR / _now().strftime("%Y-%m-%d")
    if hoy.exists():
        return None
    hoy.mkdir(parents=True, exist_ok=True)
    os.chmod(hoy, 0o700)
    for f in (STATE_FILE, DECISIONS_CSV, MEMORIA_FILE):
        if f.exists():
            shutil.copy2(f, hoy / f.name)
    limite = (_now() - timedelta(days=DIAS_RESPALDO)).strftime("%Y-%m-%d")
    for d in RESPALDO_DIR.iterdir():
        if d.is_dir() and d.name < limite:
            shutil.rmtree(d, ignore_errors=True)
    return hoy


# ── Datos mínimos (D11-A) ──────────────────────────────────────────────────────

def borrar_foto(ruta: str | None) -> None:
    if not ruta:
        return
    p = Path(ruta)
    try:
        if p.exists() and FOTOS_DIR.resolve() in p.resolve().parents:
            p.unlink()
    except Exception:
        pass


def borrar_fotos_huerfanas() -> int:
    """Fotos que no pertenecen a una consulta abierta (p. ej., las de un ensayo). No toca el estado."""
    if not FOTOS_DIR.exists():
        return 0
    abiertas = set()
    if STATE_FILE.exists():
        for it in json.loads(STATE_FILE.read_text(encoding="utf-8"))["items"].values():
            if it.get("foto") and it.get("estado") in ("pendiente", "aprobado") and not it.get("ejecutado_en_linkedin"):
                abiertas.add(str(Path(it["foto"]).resolve()))
    n = 0
    for f in FOTOS_DIR.iterdir():
        if f.is_file() and str(f.resolve()) not in abiertas:
            f.unlink(); n += 1
    return n


def borrar_temporales() -> list:
    """Páginas de diagnóstico y archivos intermedios con datos de perfiles: no se guardan entre corridas."""
    borrados = []
    for nombre in TEMPORALES:
        p = DATA_DIR / nombre
        if p.exists():
            p.unlink()
            borrados.append(nombre)
    return borrados


def purgar(dias: int = DIAS_RETENCION) -> dict:
    """
    - Casos cerrados en consult_state: borra su foto.
    - Lo no aceptado con más de `dias`: se anonimiza en consult_state, decisions.csv y memoria
      (queda solo la huella de la URL y la decisión, para no volver a visitar a la persona).
    - Fotos sin caso abierto: se borran.
    """
    limite = _now() - timedelta(days=dias)
    res = {"fotos": 0, "anonimizados": 0}
    abiertas = set()

    aprobados = set()
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        aprobados = {huella_url(i.get("profile_url")) for i in state["items"].values()
                     if i.get("estado") == "aprobado" and i.get("profile_url")}
        for tok, it in list(state["items"].items()):
            cerrado = it.get("estado") in ("ignorado", "vencido") or it.get("ejecutado_en_linkedin")
            if cerrado and it.get("foto"):
                borrar_foto(it["foto"]); it["foto"] = None; res["fotos"] += 1
            elif it.get("foto"):
                abiertas.add(str(Path(it["foto"]).resolve()))
            fecha = it.get("resuelto") or it.get("creado")
            if (it.get("estado") in ("ignorado", "vencido") and fecha
                    and datetime.fromisoformat(fecha) < limite and it.get("nombre")):
                state["items"][tok] = {"token": tok, "estado": it["estado"], "registrado": True,
                                       "profile_url": huella_url(it.get("profile_url")),
                                       "plazo": it.get("plazo"), "creado": it.get("creado"),
                                       "resuelto": it.get("resuelto"), "anonimizado": True}
                res["anonimizados"] += 1
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        _privado(tmp)
        tmp.replace(STATE_FILE)

    if FOTOS_DIR.exists():
        for f in FOTOS_DIR.iterdir():
            if f.is_file() and str(f.resolve()) not in abiertas:
                f.unlink(); res["fotos"] += 1

    if DECISIONS_CSV.exists():
        with DECISIONS_CSV.open(encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
        cambio = False
        for row in filas:
            try:
                fecha = datetime.strptime(row.get("fecha", ""), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if row.get("decision") != "accept" and fecha < limite and row.get("nombre"):
                row.update(nombre="", cargo="", reason="", conexiones_comun="",
                           profile_url=huella_url(row.get("profile_url")))
                cambio = True; res["anonimizados"] += 1
        if cambio:
            with DECISIONS_CSV.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                w.writeheader(); w.writerows(filas)
            _privado(DECISIONS_CSV)

    mem = cargar()
    for clave, v in mem["perfiles"].items():
        if v.get("url") and clave not in aprobados and datetime.fromisoformat(v["visto"]) < limite:
            v.pop("url"); v.pop("datos", None); res["anonimizados"] += 1
    guardar(mem)
    return res

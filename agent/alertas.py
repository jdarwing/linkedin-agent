"""
alertas.py — Avisos a Darwing cuando la corrida no es confiable (Fase 0, R11)
============================================================================
Tipos:
  DEGRADADA  la corrida terminó, pero sus resultados no son confiables
             (página de LinkedIn cambiada, cobertura incompleta, errores de la API...).
  GUARDIÁN   LinkedIn mostró verificación, captcha o aviso: el agente quedó en pausa total.
  DETENIDA   la corrida se detuvo antes de evaluar (falta reglas.json, falta la clave...).
Se envía como máximo un aviso del mismo tipo cada 12 horas.
"""

import html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import logconf

log = logconf.get("alertas")
ALERTAS_FILE = Path(os.getenv("ALERTAS_FILE", "data/alertas.json"))
INTERVALO = timedelta(hours=12)

TITULOS = {
    "DEGRADADA": "Corrida DEGRADADA: revisa antes de confiar en el resultado",
    "GUARDIÁN":  "GUARDIÁN: el agente se pausó por una señal de LinkedIn",
    "DETENIDA":  "Corrida DETENIDA: no se evaluó a nadie",
}


def _cargar() -> dict:
    try:
        return json.loads(ALERTAS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def construir(tipo: str, motivos: list) -> tuple:
    titulo = TITULOS.get(tipo, tipo)
    asunto = f"[LinkedIn Agent] {titulo}"
    items = "".join(f"<li>{html.escape(m)}</li>" for m in motivos)
    pie = ("El archivo PAUSA quedó creado en la carpeta del agente. Revisa tu cuenta en la app de LinkedIn; "
           "cuando esté normal, avísale a Claude o borra el archivo PAUSA."
           if tipo == "GUARDIÁN" else
           "No se aceptó nada en LinkedIn. La próxima corrida lo vuelve a intentar.")
    cuerpo = (f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="background:#f4f4f5;">'
              f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;padding:20px 14px;">'
              f'<div style="font-size:20px;font-weight:bold;margin-bottom:8px;">{html.escape(titulo)}</div>'
              f'<ul style="font-size:14px;">{items}</ul>'
              f'<div style="font-size:13px;color:#52525b;">{html.escape(pie)}</div></div></body></html>')
    texto = titulo + "\n\n" + "\n".join(f"- {m}" for m in motivos) + "\n\n" + pie
    return asunto, cuerpo, texto


def enviar(tipo: str, motivos: list, dry_run: bool = False) -> bool:
    if not motivos:
        return False
    for m in motivos:
        log.warning(f"⚠ {tipo}: {m}")
    if dry_run:
        log.info(f"DRY-RUN: no se envía el aviso {tipo}.")
        return False
    estado = _cargar()
    ultimo = estado.get(tipo)
    ahora = datetime.now(timezone.utc)
    if ultimo and ahora - datetime.fromisoformat(ultimo) < INTERVALO:
        log.info(f"Aviso {tipo} ya enviado hace menos de 12 h; no se repite.")
        return False
    import notifier
    asunto, cuerpo, texto = construir(tipo, motivos)
    if notifier._send(asunto, cuerpo, texto):
        estado[tipo] = ahora.isoformat()
        ALERTAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERTAS_FILE.write_text(json.dumps(estado, indent=2), encoding="utf-8")
        return True
    return False

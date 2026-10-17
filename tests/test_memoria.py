"""Memoria completa (P3, D10-A), respaldo (R17) y datos mínimos (D11-A)."""

import csv
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.base import Caso
import memoria
import notifier


def viejo(dias):
    return (datetime.now(timezone.utc) - timedelta(days=dias))


class Memoria(Caso):
    def test_registra_todo_y_reconoce_con_url_variante(self):
        memoria.registrar([{"profile_url": "https://www.linkedin.com/in/Ana-P/", "decision": "ignore"},
                           {"profile_url": "https://www.linkedin.com/in/beto", "decision": "consult"}])
        c = memoria.conocidos()
        self.assertIn(memoria.huella_url("https://www.linkedin.com/in/ana-p?trk=x"), c)
        self.assertIn(memoria.huella_url("https://www.linkedin.com/in/beto/"), c)
        self.assertEqual(oct(os.stat(memoria.MEMORIA_FILE).st_mode & 0o777), "0o600")

    def test_ana_y_mariana_son_personas_distintas_R1(self):
        memoria.registrar([{"profile_url": "https://www.linkedin.com/in/mariana", "decision": "ignore"}])
        self.assertNotIn(memoria.huella_url("https://www.linkedin.com/in/ana"), memoria.conocidos())

    def test_huella_es_idempotente(self):
        h = memoria.huella_url("https://www.linkedin.com/in/x")
        self.assertEqual(memoria.huella_url(h), h)


class Respaldo(Caso):
    def test_un_respaldo_por_dia(self):
        notifier.save_state({"items": {}, "mensajes_procesados": []})
        d = memoria.respaldo_diario()
        self.assertTrue((d / "consult_state.json").exists())
        self.assertIsNone(memoria.respaldo_diario())


class DatosMinimos(Caso):
    def test_purga_90_dias(self):
        fotos = Path("data/fotos"); fotos.mkdir(parents=True)
        (fotos / "cerrado.jpg").write_bytes(b"x"); (fotos / "abierto.jpg").write_bytes(b"x")
        (fotos / "huerfana.jpg").write_bytes(b"x")
        items = {
            "AAAA-AAAA": {"token": "AAAA-AAAA", "nombre": "Vieja", "cargo": "c", "estado": "ignorado",
                          "profile_url": "https://www.linkedin.com/in/vieja", "foto": "data/fotos/cerrado.jpg",
                          "creado": viejo(100).isoformat(), "resuelto": viejo(95).isoformat(), "plazo": viejo(98).isoformat()},
            "BBBB-BBBB": {"token": "BBBB-BBBB", "nombre": "Abierta", "cargo": "c", "estado": "pendiente",
                          "profile_url": "https://www.linkedin.com/in/abierta", "foto": "data/fotos/abierto.jpg",
                          "creado": viejo(1).isoformat(), "plazo": (viejo(1) + timedelta(hours=48)).isoformat()},
            "CCCC-CCCC": {"token": "CCCC-CCCC", "nombre": "Aprobada", "cargo": "c", "estado": "aprobado",
                          "profile_url": "https://www.linkedin.com/in/aprobada",
                          "creado": viejo(120).isoformat(), "resuelto": viejo(119).isoformat(), "plazo": viejo(118).isoformat()},
        }
        notifier.save_state({"items": items, "mensajes_procesados": []})
        memoria.anotar_csv([
            {"fecha": viejo(100).strftime("%Y-%m-%d %H:%M"), "nombre": "Vieja", "profile_url": "https://www.linkedin.com/in/vieja", "decision": "ignore"},
            {"fecha": viejo(100).strftime("%Y-%m-%d %H:%M"), "nombre": "Aprobada", "profile_url": "https://www.linkedin.com/in/aprobada", "decision": "accept"},
            {"fecha": viejo(5).strftime("%Y-%m-%d %H:%M"), "nombre": "Reciente", "profile_url": "https://www.linkedin.com/in/reciente", "decision": "ignore"}])
        mem = {"perfiles": {
            memoria.huella_url("https://www.linkedin.com/in/vieja"): {"url": "https://www.linkedin.com/in/vieja", "visto": viejo(100).isoformat()},
            memoria.huella_url("https://www.linkedin.com/in/aprobada"): {"url": "https://www.linkedin.com/in/aprobada", "visto": viejo(120).isoformat()}}}
        memoria.guardar(mem)

        memoria.purgar()

        self.assertFalse((fotos / "cerrado.jpg").exists())
        self.assertFalse((fotos / "huerfana.jpg").exists())
        self.assertTrue((fotos / "abierto.jpg").exists())
        st = notifier.load_state()["items"]
        self.assertNotIn("nombre", st["AAAA-AAAA"])
        self.assertTrue(st["AAAA-AAAA"]["profile_url"].startswith("h:"))
        self.assertEqual(st["CCCC-CCCC"]["nombre"], "Aprobada")
        with open(memoria.DECISIONS_CSV, encoding="utf-8") as f:
            nombres = [r["nombre"] for r in csv.DictReader(f)]
        self.assertNotIn("Vieja", nombres); self.assertIn("Aprobada", nombres); self.assertIn("Reciente", nombres)
        m = memoria.cargar()["perfiles"]
        self.assertNotIn("url", m[memoria.huella_url("https://www.linkedin.com/in/vieja")])
        self.assertIn("url", m[memoria.huella_url("https://www.linkedin.com/in/aprobada")])
        # anonimizada, pero sigue siendo "conocida": no se vuelve a visitar
        self.assertIn(memoria.huella_url("https://www.linkedin.com/in/vieja"), memoria.conocidos())

    def test_temporales_se_borran(self):
        for n in ("debug_perfil.html", "pending_requests.json", "email_preview.html"):
            Path("data", n).write_text("x")
        self.assertEqual(len(memoria.borrar_temporales()), 3)
        self.assertFalse(Path("data/debug_perfil.html").exists())


if __name__ == "__main__":
    unittest.main()

"""Corrida completa con dobles de prueba: recomendaciones, memoria, avisos y guardián."""

import csv
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

from tests.base import Caso
from tests.test_evaluador import ClienteFalso
import alertas
import evaluator
import guardian
import memoria
import notifier
import runner


class Corrida(Caso):
    def setUp(self):
        super().setUp()
        self.enviados = []
        notifier._send = lambda asunto, html_body, texto: self.enviados.append((asunto, html_body)) or True
        evaluator.PAUSE_BETWEEN = 0
        guardian.PAUSA = Path("PAUSA_prueba")
        if guardian.PAUSA.exists():
            guardian.PAUSA.unlink()
        perfiles = [{"nombre": f"P{i}", "cargo": c, "conexiones_comun": n,
                     "profile_url": f"https://www.linkedin.com/in/p{i}"}
                    for i, (c, n) in enumerate([("Gerente comercial", 4), ("Vendedor de seguros", 0), ("Analista", 2)])]
        Path("data/pending_requests.json").write_text(json.dumps(perfiles), encoding="utf-8")
        self.cliente = ClienteFalso(['{"decision":"accept","score":8,"reason":"a"}',
                                     '{"decision":"ignore","score":2,"reason":"i"}',
                                     ConnectionError("caída")])
        evaluator.make_client = lambda: self.cliente

    def test_corrida_sin_linkedin(self):
        codigo = runner.run_pipeline(dry_run=False, skip_scraper=True)
        self.assertEqual(codigo, runner.DEGRADADA)               # hubo un error de API → se avisa
        asuntos = [a for a, _ in self.enviados]
        self.assertTrue(any("2 perfiles para tu decisión" in a for a in asuntos), asuntos)
        self.assertTrue(any("DEGRADADA" in a for a in asuntos), asuntos)
        self.assertEqual(len(memoria.cargar()["perfiles"]), 3)   # memoria completa, también el ignorado
        with open(memoria.DECISIONS_CSV, encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
        self.assertEqual([(r["decision"], r["fuente"]) for r in filas], [("ignore", "agente_ignora")])
        estados = notifier.load_state()["items"].values()
        self.assertEqual(sorted(i["recomendacion"] for i in estados), ["aceptar", "sin_evaluar"])
        self.assertTrue(all(i["estado"] == "pendiente" for i in estados))

    def test_dry_run_no_guarda_ni_envia(self):
        runner.run_pipeline(dry_run=True, skip_scraper=True)
        self.assertEqual(self.enviados, [])
        self.assertFalse(memoria.MEMORIA_FILE.exists())
        self.assertFalse(notifier.STATE_FILE.exists())
        self.assertTrue(Path("data/dryrun_decisions.json").exists())
        self.assertTrue(Path("data/email_preview.html").exists())

    def test_sin_reglas_se_detiene_y_avisa(self):
        Path("config/reglas.json").unlink()
        self.assertEqual(runner.run_pipeline(dry_run=False, skip_scraper=True), runner.DETENIDA)
        self.assertEqual(self.cliente.llamadas, 0)
        self.assertTrue(any("DETENIDA" in a for a, _ in self.enviados))

    def test_guardian_pausa_todo(self):
        falso = ModuleType("scraper")
        falso.DIAGNOSTICO = {}
        def leer(dry_run=False):
            raise guardian.AlertaCuenta("LinkedIn redirigió a una página de verificación")
        falso.scrape_invitations = leer
        sys.modules["scraper"] = falso
        try:
            self.assertEqual(runner.run_pipeline(dry_run=False), runner.PAUSADA)
        finally:
            del sys.modules["scraper"]
        self.assertTrue(guardian.en_pausa())
        self.assertTrue(any("GUARDIÁN" in a for a, _ in self.enviados))
        self.assertEqual(self.cliente.llamadas, 0)

    def test_degradada_si_la_pagina_cambio(self):
        falso = ModuleType("scraper")
        falso.DIAGNOSTICO = {"alertas": ["No se encontró ninguna invitación y la página tampoco dice que no haya."],
                             "urls_visibles": [], "cobertura_completa": False}
        falso.scrape_invitations = lambda dry_run=False: []
        sys.modules["scraper"] = falso
        try:
            self.assertEqual(runner.run_pipeline(dry_run=False), runner.DEGRADADA)
        finally:
            del sys.modules["scraper"]
        self.assertTrue(any("DEGRADADA" in a for a, _ in self.enviados))

    def test_aviso_no_se_repite_en_12_horas(self):
        alertas.enviar("DEGRADADA", ["x"]); alertas.enviar("DEGRADADA", ["x"])
        self.assertEqual(sum("DEGRADADA" in a for a, _ in self.enviados), 1)


if __name__ == "__main__":
    unittest.main()

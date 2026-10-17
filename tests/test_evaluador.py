"""Evaluador: error de la API (R14), falla cerrada (R16) y sin aceptación autónoma (R3)."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.base import Caso
import evaluator


class ClienteFalso:
    """Imita client.chat.completions.create de OpenAI."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.llamadas += 1
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])


SECTOR = {"nombre": "Ana Prueba", "cargo": "Jefa de cosecha de arándanos", "conexiones_comun": 12,
          "profile_url": "https://www.linkedin.com/in/ana-prueba", "acerca_de": "Arándanos y uvas"}


class ErrorDeAPI(Caso):
    def setUp(self):
        super().setUp()
        evaluator.PAUSE_BETWEEN = 0

    def test_error_api_no_lo_sube_ninguna_regla_R14(self):
        # Escenario de la Mesa D: API caída + "arándanos" + 12 en común antes terminaba en "aceptar"
        r = evaluator.evaluate_all(perfiles=[SECTOR], client=ClienteFalso([ConnectionError("caída")]))[0]
        self.assertEqual(r["lectura_agente"], "error")
        self.assertEqual(r["recomendacion"], "sin_evaluar")
        self.assertEqual(r["decision"], "consult")

    def test_respuesta_ilegible_es_error(self):
        for raw in ("no es json", '{"decision": "accept", "score": "diez"}', "[1, 2]"):
            r = evaluator.evaluate_all(perfiles=[SECTOR], client=ClienteFalso([raw]))[0]
            self.assertEqual(r["recomendacion"], "sin_evaluar", raw)

    def test_aplicar_reglas_respeta_el_error(self):
        res = {"decision": "error", "score": None, "reason": "x", "profile_url": SECTOR["profile_url"]}
        self.assertEqual(evaluator.aplicar_reglas(dict(res), SECTOR)["decision"], "error")


class FallaCerrada(Caso):
    def test_sin_reglas_no_se_evalua_a_nadie_R16(self):
        Path("config/reglas.json").unlink()
        cliente = ClienteFalso(['{"decision":"accept","score":9,"reason":"x"}'])
        with self.assertRaises(evaluator.ReglasError):
            evaluator.evaluate_all(perfiles=[SECTOR], client=cliente)
        self.assertEqual(cliente.llamadas, 0)

    def test_reglas_incompletas(self):
        Path("config/reglas.json").write_text(json.dumps({"_version": "x"}), encoding="utf-8")
        with self.assertRaises(evaluator.ReglasError):
            evaluator.cargar_reglas()

    def test_reglas_ilegibles(self):
        Path("config/reglas.json").write_text("{roto", encoding="utf-8")
        with self.assertRaises(evaluator.ReglasError):
            evaluator.cargar_reglas()


class SinAceptacionAutonoma(Caso):
    def setUp(self):
        super().setUp()
        evaluator.PAUSE_BETWEEN = 0

    def test_lo_que_el_modelo_acepta_pasa_a_recomendacion_R3(self):
        perfiles = [dict(SECTOR, profile_url=f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        cliente = ClienteFalso(['{"decision":"accept","score":9,"reason":"a"}',
                                '{"decision":"consult","score":5,"reason":"c"}',
                                '{"decision":"ignore","score":2,"reason":"i"}'])
        res = evaluator.evaluate_all(perfiles=perfiles, client=cliente)
        self.assertNotIn("accept", [r["decision"] for r in res])
        self.assertEqual([r["recomendacion"] for r in res], ["aceptar", "aceptar", "consultar"])
        # el segundo sube a "aceptar" por la regla de sector (12 en común) pero sigue siendo consulta
        self.assertEqual([r["decision"] for r in res], ["consult", "consult", "consult"])
        self.assertTrue(all(r["version_reglas"] for r in res))

    def test_ignore_sigue_siendo_solo_registro(self):
        p = {"nombre": "X", "cargo": "Vendedor de seguros", "conexiones_comun": 0,
             "profile_url": "https://www.linkedin.com/in/x"}
        r = evaluator.evaluate_all(perfiles=[p], client=ClienteFalso(['{"decision":"ignore","score":2,"reason":"i"}']))[0]
        self.assertEqual((r["decision"], r["recomendacion"]), ("ignore", "ignorar"))

    def test_grupo_con_10_en_comun_es_recomendacion_no_aceptacion(self):
        p = {"nombre": "Y", "cargo": "Jefe de packing en Agrícola Don Ricardo", "conexiones_comun": 15,
             "profile_url": "https://www.linkedin.com/in/y"}
        r = evaluator.evaluate_all(perfiles=[p], client=ClienteFalso(['{"decision":"consult","score":5,"reason":"c"}']))[0]
        self.assertEqual((r["decision"], r["recomendacion"]), ("consult", "aceptar"))


if __name__ == "__main__":
    unittest.main()

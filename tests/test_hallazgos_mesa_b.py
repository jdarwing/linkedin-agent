"""Pruebas de los hallazgos de la Mesa B (revisión independiente del 17-oct-2026)."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

from tests.base import Caso
from tests.test_correo import estado_con
from tests.test_evaluador import ClienteFalso
import alertas
import evaluator
import guardian
import memoria
import notifier
import runner


class ScraperEspia(ModuleType):
    def __init__(self, devuelve=None):
        super().__init__("scraper")
        self.llamado = False
        self.DIAGNOSTICO = {"alertas": [], "urls_visibles": [], "cobertura_completa": False}
        self._devuelve = devuelve or []

    def scrape_invitations(self, dry_run=False):
        self.llamado = True
        return list(self._devuelve)


class Base(Caso):
    def setUp(self):
        super().setUp()
        self.enviados = []
        notifier._send = lambda a, h, t: self.enviados.append(a) or True
        evaluator.PAUSE_BETWEEN = 0
        guardian.PAUSA = Path("PAUSA_prueba")
        if guardian.PAUSA.exists():
            guardian.PAUSA.unlink()
        self.espia = ScraperEspia()
        sys.modules["scraper"] = self.espia

    def tearDown(self):
        sys.modules.pop("scraper", None)


class H1_ChequeoPrevio(Base):
    def test_sin_reglas_no_se_abre_linkedin(self):
        Path("config/reglas.json").unlink()
        self.assertEqual(runner.run_pipeline(), runner.DETENIDA)
        self.assertFalse(self.espia.llamado)
        self.assertTrue(any("DETENIDA" in a for a in self.enviados))

    def test_sin_clave_no_se_abre_linkedin(self):
        original = evaluator.OPENAI_API_KEY
        evaluator.OPENAI_API_KEY = ""
        try:
            self.assertEqual(runner.run_pipeline(), runner.DETENIDA)
        finally:
            evaluator.OPENAI_API_KEY = original
        self.assertFalse(self.espia.llamado)


class H2_VisitaRegistradaAlMomento(Base):
    def test_visita_y_lectura_quedan_por_evaluar(self):
        p = {"nombre": "Ana", "profile_url": "https://www.linkedin.com/in/ana", "cargo": "x"}
        memoria.marcar_visita(p)
        memoria.guardar_lectura(dict(p, cargo_actual="Gerente"))
        self.assertIn(memoria.huella_url(p["profile_url"]), memoria.conocidos())
        self.assertEqual(memoria.por_evaluar()[0]["cargo_actual"], "Gerente")
        memoria.registrar([{"profile_url": p["profile_url"], "decision": "consult"}])
        self.assertEqual(memoria.por_evaluar(), [])
        self.assertNotIn("datos", memoria.cargar()["perfiles"][memoria.huella_url(p["profile_url"])])

    def test_corrida_siguiente_evalua_sin_revisitar(self):
        memoria.marcar_visita({"nombre": "Ana", "profile_url": "https://www.linkedin.com/in/ana", "cargo": "Gerente",
                               "conexiones_comun": 1})
        cliente = ClienteFalso(['{"decision":"consult","score":5,"reason":"c"}'])
        evaluator.make_client = lambda: cliente
        runner.run_pipeline()
        self.assertTrue(self.espia.llamado)
        self.assertEqual(cliente.llamadas, 1)
        self.assertEqual(memoria.por_evaluar(), [])
        self.assertTrue(any("1 perfil para tu decisión" in a for a in self.enviados), self.enviados)


class H3_NadiePausaDesdeAfuera(unittest.TestCase):
    def test_url_de_perfil_con_palabras_de_riesgo(self):
        for u in ("https://www.linkedin.com/in/login-perez", "https://www.linkedin.com/in/challenge-x",
                  "https://www.linkedin.com/in/uas-maria/", "https://www.linkedin.com/in/ana/?trk=checkpoint"):
            self.assertEqual(guardian.revisar(u), "", u)

    def test_rutas_reales_de_linkedin(self):
        self.assertTrue(guardian.revisar("https://www.linkedin.com/checkpoint/challenge/x"))
        self.assertTrue(guardian.revisar("https://linkedin.com/uas/login?x"))
        self.assertEqual(guardian.revisar("https://evil.com/checkpoint/x"), "")


class H4_MemoriaDanada(Base):
    def test_memoria_danada_detiene_sin_abrir_linkedin(self):
        memoria.MEMORIA_FILE.write_text("{roto", encoding="utf-8")
        self.assertEqual(runner.run_pipeline(), runner.DETENIDA)
        self.assertFalse(self.espia.llamado)
        self.assertTrue(any("DETENIDA" in a for a in self.enviados))


class H5_FallosConAviso(Base):
    def test_buzon_caido_no_vence_y_avisa(self):
        creado = datetime.now(timezone.utc) - timedelta(hours=50)
        notifier.save_state(estado_con(["K7F2-9QXA"], creado=creado))
        original = notifier.leer_buzon
        notifier.leer_buzon = lambda *a, **k: (_ for _ in ()).throw(OSError("clave revocada"))
        try:
            self.assertEqual(runner.run_solo_correo(), runner.DEGRADADA)
        finally:
            notifier.leer_buzon = original
        self.assertEqual(notifier.load_state()["items"]["K7F2-9QXA"]["estado"], "pendiente")
        self.assertTrue(any("DEGRADADA" in a for a in self.enviados))


class H6_AvisoGuardianSeReintenta(Base):
    def test_reintento_del_aviso(self):
        guardian.pausar("verificación pedida")
        self.assertEqual(guardian.aviso_pendiente(), "verificación pedida")
        self.assertEqual(runner.main([]), runner.PAUSADA)
        self.assertTrue(any("GUARDIÁN" in a for a in self.enviados))
        self.assertEqual(guardian.aviso_pendiente(), "")


class H8_SinPurgaEnLaRevisionHoraria(Base):
    def test_solo_correo_no_borra_fotos_de_consultas(self):
        fotos = Path("data/fotos"); fotos.mkdir(parents=True)
        (fotos / "x.jpg").write_bytes(b"x")
        st = estado_con(["K7F2-9QXA"]); st["items"]["K7F2-9QXA"]["foto"] = "data/fotos/x.jpg"
        notifier.save_state(st)
        runner.run_solo_correo()
        self.assertTrue((fotos / "x.jpg").exists())

    def test_candado_de_ejecucion(self):
        a = runner.candado_ejecucion(esperar=False)
        self.assertIsNotNone(a)
        self.assertIsNone(runner.candado_ejecucion(esperar=False))
        a.close()


class H9_HoraDeRecepcion(unittest.TestCase):
    def test_internaldate_despues_del_cuerpo(self):
        data = [(b"1 (BODY[] {5}", b"hola!"), b' INTERNALDATE "17-Oct-2026 17:00:00 +0000")']
        self.assertEqual(notifier._recepcion(data), datetime(2026, 10, 17, 17, 0, tzinfo=timezone.utc))

    def test_el_cuerpo_no_puede_fingir_la_hora(self):
        data = [(b"1 (BODY[] {40}", b'INTERNALDATE "01-Jan-2026 00:00:00 +0000"'), b")"]
        self.assertIsNone(notifier._recepcion(data))


class H10_Privacidad(Base):
    def test_permisos_privados(self):
        notifier.save_state({"items": {}, "mensajes_procesados": []})
        memoria.anotar_csv([{"fecha": "2026-10-17 10:00", "decision": "ignore"}])
        for f in (notifier.STATE_FILE, memoria.DECISIONS_CSV):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600, f)

    def test_fotos_de_un_ensayo_se_borran_en_la_siguiente_corrida(self):
        fotos = Path("data/fotos"); fotos.mkdir(parents=True)
        (fotos / "ensayo.jpg").write_bytes(b"x")
        runner.preparar(dry_run=True)
        self.assertFalse((fotos / "ensayo.jpg").exists())


class H13_SinSegundaVisita(unittest.TestCase):
    def test_no_queda_codigo_de_details_experience(self):
        from tests.base import AGENTE
        src = (AGENTE / "scraper.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r'\+\s*"/details/experience')
        self.assertNotIn("def leer_trayectoria", src)


class N1_CorreoQueNoSale(Base):
    def test_nadie_se_pierde_si_falla_el_envio(self):
        memoria.marcar_visita({"nombre": "Ana", "profile_url": "https://www.linkedin.com/in/ana", "cargo": "Gerente",
                               "conexiones_comun": 1})
        evaluator.make_client = lambda: ClienteFalso(['{"decision":"consult","score":5,"reason":"c"}',
                                                      '{"decision":"consult","score":5,"reason":"c"}'])
        notifier._send = lambda a, h, t: self.enviados.append("FALLÓ:" + a) and False
        self.assertEqual(runner.run_pipeline(), runner.DEGRADADA)
        self.assertEqual(len(memoria.por_evaluar()), 1)          # sigue pendiente
        self.assertEqual(notifier.load_state()["items"], {})
        notifier._send = lambda a, h, t: self.enviados.append(a) or True
        runner.run_pipeline()
        self.assertEqual(memoria.por_evaluar(), [])
        self.assertEqual(len(notifier.load_state()["items"]), 1)  # ahora sí te llegó


class N5_ReintentosDeHora(Base):
    def test_tope_de_tres_reintentos(self):
        from tests.test_correo import FakeIMAP, correo
        st = estado_con(["K7F2-9QXA"])
        class SinHora(FakeIMAP):
            def fetch(self, num, partes):
                raw, _ = self.mensajes[int(num) - 1]
                return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]
        for i in range(3):
            fake = SinHora([(correo(), datetime.now(timezone.utc))])
            res = notifier.leer_buzon(st, datetime.now(timezone.utc) - timedelta(hours=1), [], conectar=lambda: fake)
        self.assertEqual(res["motivos"], ["hora_ilegible"])
        self.assertIn("<m1@hotmail.com>", st["mensajes_procesados"])


class N6_CorridaColgada(Base):
    def test_aviso_si_el_candado_lleva_mas_de_4_horas(self):
        tomado = runner.candado_ejecucion(esperar=False)
        runner.CANDADO.write_text(str(datetime.now().timestamp() - 5 * 3600))
        try:
            self.assertEqual(runner.main(["--solo-correo"]), runner.DEGRADADA)
        finally:
            tomado.close()
        self.assertTrue(any("DEGRADADA" in a for a in self.enviados))


if __name__ == "__main__":
    unittest.main()

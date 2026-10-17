"""
Lectura de páginas (JavaScript del scraper) sobre HTML de ejemplo con personas ficticias.
Nunca abre linkedin.com: el HTML se carga desde tests/fixtures. Necesita Playwright y Chromium;
si no están, se omite.
"""

import unittest

from tests.base import FIXTURES, HAY_PLAYWRIGHT, Caso

if HAY_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright
import scraper


@unittest.skipUnless(HAY_PLAYWRIGHT, "Playwright no instalado")
class Dom(Caso):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()

    def pagina(self, nombre):
        page = self.browser.new_page()
        page.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())
        page.set_content((FIXTURES / nombre).read_text(encoding="utf-8"))
        return page

    def test_tarjetas(self):
        page = self.pagina("invitaciones.html")
        r = {p["nombre"]: p for p in page.evaluate(scraper.JS_EXTRACT)}
        self.assertEqual(set(r), {"Ana Ficticia", "Mariana Ficticia", "Carlos Ficticio", "Dora Ficticia"})
        self.assertEqual(r["Ana Ficticia"]["profile_url"], "https://www.linkedin.com/in/ana-ficticia")
        self.assertEqual(r["Ana Ficticia"]["conexiones_comun"], 63)      # 62 + la persona nombrada
        self.assertEqual(r["Mariana Ficticia"]["conexiones_comun"], 3)
        self.assertEqual(r["Dora Ficticia"]["conexiones_comun"], 0)
        self.assertEqual(r["Ana Ficticia"]["cargo"], "Gerente de Calidad en Exportadora Uno")
        self.assertEqual(page.evaluate(scraper.JS_TOTAL), 4)
        self.assertEqual(page.evaluate(scraper.JS_COUNT_CARDS), 4)

    @unittest.expectedFailure
    def test_conteo_con_separador_de_miles_pendiente_fase_1(self):
        # P4: "1.234" hoy se lee como 234. Se corrige en la Fase 1; esta prueba lo deja a la vista.
        page = self.pagina("invitaciones.html")
        r = {p["nombre"]: p for p in page.evaluate(scraper.JS_EXTRACT)}
        self.assertEqual(r["Carlos Ficticio"]["conexiones_comun"], 1234)

    def test_mostrar_mas_nunca_pulsa_aceptar(self):
        page = self.pagina("invitaciones.html")
        self.assertEqual(page.evaluate(scraper.JS_LOAD_MORE), "mas")
        self.assertEqual(page.evaluate("window.clics"), ["Mostrar más"])

    def test_perfil_en_una_sola_visita_D10(self):
        page = self.pagina("perfil.html")
        d = page.evaluate(scraper.JS_PERFIL)
        self.assertTrue(d["tiene_foto"])
        self.assertIn("Diez años", d["acerca_de"])
        tray = " | ".join(d["experiencia_todas"])
        self.assertIn("Agrícola Don Ricardo", tray)        # la trayectoria sale de la misma página
        self.assertIn("Exportadora Uno", tray)
        exp = scraper.interpretar_experiencia(d["experiencia_lineas"])
        self.assertEqual(exp["cargo_actual"], "Gerente de Calidad")
        self.assertTrue(exp["experiencia_es_actual"])

    def test_guardian_ignora_el_texto_de_las_tarjetas(self):
        import guardian
        page = self.pagina("invitaciones.html")
        page.evaluate("""() => { const p = document.querySelector('li p span');
                                 p.innerText = 'Detectamos actividad inusual · Let\\'s do a quick security check'; }""")
        guardian.revisar_pagina(page)                       # no debe lanzar: ese texto lo escribió un tercero

    def test_guardian_detecta_verificacion_real(self):
        import guardian
        page = self.browser.new_page()
        page.set_content("<html><head><title>Security Verification | LinkedIn</title></head>"
                         "<body><main><h1>Let's do a quick security check</h1></main></body></html>")
        with self.assertRaises(guardian.AlertaCuenta):
            guardian.revisar_pagina(page)
        with self.assertRaises(guardian.AlertaCuenta):
            guardian.revisar_perfil(page)

    def test_perfil_con_texto_de_riesgo_no_pausa(self):
        import guardian
        page = self.pagina("perfil.html")
        page.evaluate("() => document.querySelector('main').insertAdjacentHTML('beforeend', '<p>Your account has been restricted</p>')")
        guardian.revisar_perfil(page)

    def test_sin_invitaciones(self):
        self.assertTrue(scraper.SIN_INVITACIONES.search("No tienes invitaciones pendientes"))
        self.assertTrue(scraper.SIN_INVITACIONES.search("Recibidas (0)"))


if __name__ == "__main__":
    unittest.main()

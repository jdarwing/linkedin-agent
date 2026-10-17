"""Revisión estática: el código del clic ya no existe (D7-A, R1) y la huella es la de la Mac (R6)."""

import re
import unittest
from pathlib import Path

from tests.base import AGENTE, RAIZ, Caso
import guardian
import notifier


def fuentes():
    return {p.name: p.read_text(encoding="utf-8") for p in AGENTE.glob("*.py")}


class SinClic(unittest.TestCase):
    def test_no_existe_el_modulo_de_aceptacion(self):
        self.assertFalse((AGENTE / "actions.py").exists())
        for nombre, src in fuentes().items():
            self.assertNotRegex(src, r"\bimport actions\b|from actions\b|run_actions", nombre)

    def test_ningun_script_pulsa_aceptar(self):
        clics = []
        for nombre, src in fuentes().items():
            for m in re.finditer(r"([\w.]+)\.click\(", src):
                clics.append((nombre, m.group(1)))
        # El único clic permitido: "Mostrar más" para paginar las invitaciones al leerlas
        self.assertEqual(clics, [("scraper.py", "more")])
        self.assertNotIn("JS_CLICK_ACCEPT", "".join(fuentes().values()))

    def test_mostrar_mas_nunca_es_aceptar(self):
        src = fuentes()["scraper.py"]
        js = src[src.index('JS_LOAD_MORE = """'):]
        js = js[:js.index('"""', 20)]
        regex = re.search(r"/\^\(mostrar.*?/i", js).group(0)
        self.assertNotIn("aceptar", regex.lower()); self.assertNotIn("accept", regex.lower())

    def test_huella_de_mac(self):
        for nombre, src in fuentes().items():
            self.assertNotIn("Windows NT", src, nombre)

    def test_contrasena_de_linkedin_fuera_del_codigo_y_del_env(self):
        self.assertNotIn("LINKEDIN_PASSWORD\"", fuentes()["auth.py"].replace('os.getenv("LINKEDIN_PASSWORD")', ""))
        env = RAIZ / ".env"
        if env.exists():
            self.assertNotRegex(env.read_text(encoding="utf-8"), r"(?m)^LINKEDIN_PASSWORD=")


class Guardian(Caso):
    def test_urls_de_riesgo(self):
        for url in ("https://www.linkedin.com/checkpoint/challenge/abc", "https://www.linkedin.com/login",
                    "https://www.linkedin.com/authwall?x", "https://www.linkedin.com/uas/login"):
            self.assertTrue(guardian.revisar(url), url)
        self.assertEqual(guardian.revisar("https://www.linkedin.com/mynetwork/invitation-manager/received/"), "")

    def test_textos_de_riesgo(self):
        u = "https://www.linkedin.com/mynetwork/invitation-manager/received/"
        self.assertTrue(guardian.revisar(u, "Let's do a quick security check"))
        self.assertTrue(guardian.revisar(u, "Tu cuenta ha sido restringida temporalmente"))
        self.assertTrue(guardian.revisar(u, "Your account has been restricted"))

    def test_un_titular_con_captcha_no_pausa(self):
        u = "https://www.linkedin.com/mynetwork/invitation-manager/received/"
        self.assertEqual(guardian.revisar(u, "Ingeniera de seguridad · reCAPTCHA y antifraude"), "")

    def test_pausar_crea_el_archivo(self):
        guardian.PAUSA = Path("PAUSA_prueba")
        guardian.pausar("verificación")
        self.assertTrue(guardian.en_pausa())
        self.assertIn("verificación", guardian.PAUSA.read_text(encoding="utf-8"))


class CierreDeAprobados(Caso):
    def estado(self):
        notifier.save_state({"items": {
            "AAAA-AAAA": {"token": "AAAA-AAAA", "estado": "aprobado", "profile_url": "https://www.linkedin.com/in/ana"},
            "BBBB-BBBB": {"token": "BBBB-BBBB", "estado": "aprobado", "profile_url": "https://www.linkedin.com/in/beto"},
        }, "mensajes_procesados": []})

    def test_coincidencia_exacta_ana_no_es_mariana_R1(self):
        self.estado()
        n = notifier.cerrar_aprobados_ausentes(["https://www.linkedin.com/in/mariana",
                                                "https://www.linkedin.com/in/beto/"], cobertura_completa=True)
        self.assertEqual(n, 1)
        it = notifier.load_state()["items"]
        self.assertTrue(it["AAAA-AAAA"]["ejecutado_en_linkedin"])
        self.assertFalse(it["BBBB-BBBB"].get("ejecutado_en_linkedin"))

    def test_sin_cobertura_completa_no_se_cierra_nada(self):
        self.estado()
        self.assertEqual(notifier.cerrar_aprobados_ausentes([], cobertura_completa=False), 0)


if __name__ == "__main__":
    unittest.main()

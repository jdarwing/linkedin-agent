"""Canal de correo: órdenes, firma (R8), plazos y tope (R15), solo lectura (R7) y contenido del correo."""

import email
import imaplib
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from tests.base import Caso
import notifier

GOOGLE_OK = ("mx.google.com; dkim=pass header.i=@hotmail.com header.s=selector1 header.b=abc; "
             "spf=pass (google.com: domain of darwing@hotmail.com designates 1.2.3.4 as permitted sender) "
             "smtp.mailfrom=darwing@hotmail.com; dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=hotmail.com")
GOOGLE_FALLA = ("mx.google.com; dkim=none; spf=softfail smtp.mailfrom=darwing@hotmail.com; "
                "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=hotmail.com")


def correo(asunto="DECISIONES LinkedIn Agent", cuerpo="A K7F2-9QXA", de="darwing@hotmail.com",
           auth=(GOOGLE_OK,), fecha=None, mid="<m1@hotmail.com>"):
    m = MIMEText(cuerpo, "plain", "utf-8")
    m["Subject"], m["From"], m["To"], m["Message-ID"] = asunto, de, "agente.prueba@gmail.com", mid
    m["Date"] = email.utils.format_datetime(fecha or datetime.now(timezone.utc))
    # Gmail antepone sus encabezados: el primero de la lista es el suyo
    raw = "".join(f"Authentication-Results: {a}\r\n" for a in auth).encode() + m.as_bytes()
    return raw


def estado_con(tokens, plazo_horas=48, creado=None):
    creado = creado or datetime.now(timezone.utc)
    return {"items": {t: {"token": t, "numero": i + 1, "nombre": f"Persona {i}", "cargo": "x",
                          "conexiones_comun": 3, "profile_url": f"https://www.linkedin.com/in/persona-{i}",
                          "creado": creado.isoformat(),
                          "plazo": (creado + timedelta(hours=plazo_horas)).isoformat(),
                          "estado": "pendiente", "ejecutado_en_linkedin": False}
                      for i, t in enumerate(tokens)},
            "mensajes_procesados": []}


class FakeIMAP:
    """Buzón de prueba que registra cada operación."""

    def __init__(self, mensajes):
        self.mensajes = mensajes            # lista de (bytes, datetime de recepción)
        self.llamadas = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def login(self, u, p): self.llamadas.append(("login",))

    def select(self, buzon, readonly=False):
        self.llamadas.append(("select", buzon, readonly)); return "OK", [b"1"]

    def search(self, charset, criterio):
        self.llamadas.append(("search", criterio))
        return "OK", [" ".join(str(i + 1) for i in range(len(self.mensajes))).encode()]

    def fetch(self, num, partes):
        self.llamadas.append(("fetch", partes))
        raw, recibido = self.mensajes[int(num) - 1]
        idate = imaplib.Time2Internaldate(time.mktime(recibido.astimezone().timetuple())).encode()
        return "OK", [(b"%s (INTERNALDATE %s BODY[] {%d}" % (num, idate, len(raw)), raw), b")"]

    def __getattr__(self, nombre):         # store, copy, expunge, move...: prohibidos
        def prohibido(*a, **k):
            self.llamadas.append(("PROHIBIDO", nombre)); raise AssertionError(f"IMAP.{nombre} no debe usarse")
        return prohibido


class Ordenes(unittest.TestCase):
    def test_formatos(self):
        t = "A K7F2-9QXA\nI 3K7Z-RTW7\n? MMMM-NNNN 3. Pendiente\nACEPTAR PQRS-TUVW"
        self.assertEqual(notifier.parse_commands(t),
                         {"K7F2-9QXA": "accept", "3K7Z-RTW7": "ignore", "PQRS-TUVW": "accept"})

    def test_orden_contradictoria_se_descarta(self):
        self.assertEqual(notifier.parse_commands("A K7F2-9QXA\nI K7F2-9QXA"), {})


class Firma(Caso):
    def msg(self, **kw):
        return email.message_from_bytes(correo(**kw))

    def test_firma_de_google_valida(self):
        self.assertEqual(notifier._sender_ok(self.msg()), (True, "dmarc=pass"))

    def test_encabezado_falso_debajo_del_de_google_no_pasa_R8(self):
        # Caso reproducido por la Mesa B: Google dice fail; el atacante agrega "dmarc=pass" más abajo
        falso = "mx.google.com; dmarc=pass (p=NONE) header.from=hotmail.com"
        ok, motivo = notifier._sender_ok(self.msg(auth=(GOOGLE_FALLA, falso)))
        self.assertFalse(ok, motivo)

    def test_primer_encabezado_que_no_es_de_google_no_pasa(self):
        ok, _ = notifier._sender_ok(self.msg(auth=("atacante.com; dmarc=pass header.from=hotmail.com",)))
        self.assertFalse(ok)

    def test_remitente_distinto_no_pasa(self):
        ok, motivo = notifier._sender_ok(self.msg(de="otro@hotmail.com"))
        self.assertFalse(ok); self.assertIn("no autorizado", motivo)

    def test_dkim_de_otro_dominio_no_alinea(self):
        a = "mx.google.com; dkim=pass header.i=@hotmail.com.evil.net; dmarc=fail header.from=hotmail.com"
        self.assertFalse(notifier._sender_ok(self.msg(auth=(a,)))[0])

    def test_sin_encabezado_no_pasa(self):
        self.assertFalse(notifier._sender_ok(self.msg(auth=()))[0])


class Plazos(Caso):
    def test_un_solo_uso_y_plazo_por_recepcion(self):
        st = estado_con(["K7F2-9QXA"], plazo_horas=48)
        tarde = datetime.now(timezone.utc) + timedelta(hours=49)
        self.assertEqual(notifier.apply_commands(st, {"K7F2-9QXA": "accept"}, "t", tarde), [])
        cambios = notifier.apply_commands(st, {"K7F2-9QXA": "accept"}, "t", datetime.now(timezone.utc))
        self.assertEqual(len(cambios), 1)
        self.assertEqual(notifier.apply_commands(st, {"K7F2-9QXA": "ignore"}, "t"), [])
        self.assertEqual(st["items"]["K7F2-9QXA"]["estado"], "aprobado")


class Buzon(Caso):
    def leer(self, mensajes, st):
        fake = FakeIMAP(mensajes)
        cambios = []
        res = notifier.leer_buzon(st, datetime.now(timezone.utc) - timedelta(hours=1), cambios, conectar=lambda: fake)
        return fake, cambios, res

    def test_solo_lectura_y_filtro_R7(self):
        st = estado_con(["K7F2-9QXA"])
        fake, cambios, _ = self.leer([(correo(), datetime.now(timezone.utc))], st)
        self.assertEqual(len(cambios), 1)
        self.assertIn(("select", "INBOX", True), fake.llamadas)
        criterio = [c for c in fake.llamadas if c[0] == "search"][0][1]
        self.assertIn('FROM "darwing@hotmail.com"', criterio)
        self.assertIn('SUBJECT "LinkedIn Agent"', criterio)
        self.assertTrue(all("PEEK" in c[1] for c in fake.llamadas if c[0] == "fetch"))
        self.assertFalse([c for c in fake.llamadas if c[0] == "PROHIBIDO"])

    def test_fecha_antigua_no_salva_un_codigo_vencido_R15(self):
        creado = datetime.now(timezone.utc) - timedelta(hours=50)
        st = estado_con(["K7F2-9QXA"], creado=creado)
        raw = correo(fecha=creado + timedelta(hours=1))          # el correo "dice" que se envió a tiempo
        _, cambios, _ = self.leer([(raw, datetime.now(timezone.utc))], st)   # pero llegó tarde
        self.assertEqual(cambios, [])
        self.assertEqual(st["items"]["K7F2-9QXA"]["estado"], "pendiente")

    def test_tope_de_codigos_por_correo_R15(self):
        toks = [f"AAAA-{a}{b}{c}{d}" for a, b, c, d in zip("BCDEFGHJKMNPQRSTUVWXYZ2345", "B" * 26, "C" * 26, "D" * 26)]
        st = estado_con(toks)
        cuerpo = "\n".join(f"A {t}" for t in toks)
        _, cambios, res = self.leer([(correo(cuerpo=cuerpo), datetime.now(timezone.utc))], st)
        self.assertEqual(len(toks), 26)
        self.assertEqual(cambios, []); self.assertEqual(res["rechazados"], 1)

    def test_asunto_ajeno_se_ignora(self):
        st = estado_con(["K7F2-9QXA"])
        _, cambios, _ = self.leer([(correo(asunto="Hola"), datetime.now(timezone.utc))], st)
        self.assertEqual(cambios, [])


class Contenido(Caso):
    def items(self):
        return [{"token": "K7F2-9QXA", "numero": 1, "nombre": "<script>alert(1)</script>", "cargo": "Gerente",
                 "conexiones_comun": 12, "profile_url": "https://www.linkedin.com/in/x", "reason": "r",
                 "score": 6, "recomendacion": "aceptar"},
                {"token": "3K7Z-RTW7", "numero": 2, "nombre": "Dos", "cargo": "", "conexiones_comun": 0,
                 "profile_url": "javascript:alert(1)", "reason": "r", "recomendacion": "sin_evaluar"}]

    def test_sin_aceptar_a_todos_y_nada_marcado_de_antemano(self):
        asunto, cuerpo, texto = notifier.build_email(self.items(), "a@gmail.com", datetime.now(timezone.utc))
        self.assertNotIn("Aceptar a", cuerpo)
        self.assertNotIn("subject=ACEPTAR", cuerpo)
        plantilla = notifier.plantilla_respuesta(self.items())
        self.assertIn("? K7F2-9QXA", plantilla); self.assertIn("? 3K7Z-RTW7", plantilla)
        self.assertIn("El agente recomienda aceptar", cuerpo)
        self.assertIn("NO pudo evaluarlo", cuerpo)
        self.assertNotIn("<script>", cuerpo)
        self.assertNotIn("javascript:", cuerpo)

    def test_enlaces_iphone_app_y_mac_chrome(self):
        asunto, cuerpo, texto = notifier.build_email(self.items(), "a@gmail.com", datetime.now(timezone.utc))
        self.assertIn('href="googlechromes://www.linkedin.com/in/x"', cuerpo)
        self.assertIn('href="https://www.linkedin.com/in/x"', cuerpo)
        self.assertIn("📱 iPhone", cuerpo); self.assertIn("💻 Mac", cuerpo)
        self.assertNotIn("googlechromes://javascript", cuerpo)
        self.assertEqual(notifier._chrome_url("https://evil.com/in/x"), "")

    def test_lista_para_el_clic_de_darwing(self):
        st = estado_con(["K7F2-9QXA", "3K7Z-RTW7"])
        st["items"]["K7F2-9QXA"]["estado"] = "aprobado"
        st["items"]["3K7Z-RTW7"].update(estado="aprobado", ejecutado_en_linkedin=True)
        h = notifier.lista_clic_html(st)
        self.assertIn("Para aceptar tú en LinkedIn (1)", h)
        self.assertIn("googlechromes://www.linkedin.com/in/persona-0", h)
        self.assertIn("https://www.linkedin.com/in/persona-0", h)
        self.assertNotIn("persona-1", h)


if __name__ == "__main__":
    unittest.main()

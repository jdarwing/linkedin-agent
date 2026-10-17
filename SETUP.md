# LinkedIn Agent — Configuración inicial (Fase 1)

## 1. Clonar / crear el proyecto

```bash
# Si usas Git
git init linkedin-agent
cd linkedin-agent
# copia aquí todos los archivos de esta entrega
```

## 2. Crear entorno virtual e instalar dependencias

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

pip install -r requirements.txt

# Instalar el navegador Chromium que usará Playwright
playwright install chromium
```

## 3. Configurar variables de entorno

```bash
cp .env.example .env
```

Abre `.env` y completa:
- `ANTHROPIC_API_KEY` — tu clave de la API de Anthropic
- `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` — credenciales de LinkedIn
- `NOTIFY_EMAIL` — correo donde recibirás los casos "consultar"
- `SMTP_*` — datos de tu servidor de correo (Gmail recomienda contraseñas de app)

## 4. Primera autenticación

```bash
python agent/auth.py
```

Esto abre un navegador Chromium visible. El script:
1. Entra a linkedin.com/login
2. Completa usuario y contraseña
3. Si LinkedIn pide 2FA o verificación → te deja hacerlo manualmente
4. Guarda las cookies en `data/session.json`

> **Nota:** Sólo necesitas correr `auth.py` la primera vez, o cuando la sesión expire.
> La sesión suele durar semanas si el agente corre con regularidad.

## 5. Verificar que la sesión funciona

```bash
python -c "
from playwright.sync_api import sync_playwright
from agent.auth import load_session

with sync_playwright() as p:
    browser, ctx = load_session(p, headless=False)
    page = ctx.new_page()
    page.goto('https://www.linkedin.com/mynetwork/invitation-manager/')
    input('¿Ves las solicitudes pendientes? Enter para cerrar...')
    browser.close()
"
```

Si ves la página de solicitudes: **Fase 1 completa. ✓**
Si LinkedIn pide login de nuevo: vuelve a correr `agent/auth.py`.

## Estructura del proyecto

```
linkedin-agent/
├── agent/
│   ├── __init__.py
│   ├── auth.py          ← Fase 1 (este archivo)
│   ├── scraper.py       ← Fase 3 (extracción de solicitudes)
│   ├── evaluator.py     ← Fase 4 (Claude API)
│   ├── actions.py       ← Fase 5 (aceptar / ignorar)
│   ├── notifier.py      ← Fase 6 (email / Slack)
│   └── runner.py        ← Fase 8 (orquestador principal)
├── data/
│   ├── session.json     ← cookies (generado por auth.py)
│   ├── decisions.csv    ← historial de decisiones
│   └── weekly_count.json
├── logs/
│   └── agent.log
├── .env                 ← creado por ti (no subir a Git)
├── .env.example
├── .gitignore
├── requirements.txt
└── SETUP.md
```

## Próximos pasos

| Fase | Archivo          | Qué hace                                      |
|------|------------------|-----------------------------------------------|
| 3    | `scraper.py`     | Lee solicitudes pendientes de LinkedIn        |
| 4    | `evaluator.py`   | Llama a Claude API para puntuar cada perfil   |
| 5    | `actions.py`     | Acepta o deja pasar según el score            |
| 6    | `notifier.py`    | Envía email/Slack con los casos "consultar"   |
| 7    | dry-run          | Corre en modo solo-lectura para calibrar      |
| 8    | `runner.py`      | Orquesta todo + circuit breaker + cron        |

---

## Fase 0 · Cierre de seguridad (17-oct-2026)

**El agente ya no acepta en LinkedIn (D7-A).** Lee las invitaciones nuevas, las evalúa y te
consulta por correo con su recomendación. Lo que apruebas te vuelve como lista con enlaces
para que lo aceptes tú en la app. El código del clic (`agent/actions.py`) se eliminó.

**Pruebas** (no abren LinkedIn ni envían correos):

    source .venv/bin/activate
    python -m unittest discover -s tests -t .

**Ensayo sin abrir LinkedIn** (usa `data/pending_requests.json`, llama al modelo, no envía nada):

    python agent/runner.py --dry-run --skip-scraper
    open data/email_preview.html

**Claves en el llavero de macOS** (opcional, recomendado). Cada comando pide la clave sin mostrarla.
No borres la línea del `.env` hasta comprobar que el agente la lee desde launchd (la primera corrida
programada debe enviar su correo con normalidad); si el llavero no responde, el agente usa el `.env`:

    security add-generic-password -U -s linkedin-agent -a OPENAI_API_KEY -w
    security add-generic-password -U -s linkedin-agent -a SMTP_PASS -w

**Inicio de sesión en LinkedIn:** `python agent/auth.py` abre la ventana y tú escribes la contraseña.
Ya no se guarda en `.env`.

**Falla cerrada:** antes de abrir LinkedIn se verifican las reglas, la clave del modelo y la memoria
(`data/memoria.json`). Si algo falla, no se visita a nadie y te llega un aviso DETENIDA. Una memoria
dañada se restaura copiando `data/respaldo/<fecha>/memoria.json`.

**Códigos de salida de la corrida:** 0 normal · 1 DEGRADADA (te llega un aviso) · 2 DETENIDA · 3 en PAUSA.

**Guardián:** si LinkedIn pide verificación o muestra un aviso, el agente crea el archivo `PAUSA`
y te escribe. No vuelve a correr hasta que borres `PAUSA`.

**Enlaces de perfiles en el correo:** cada perfil trae dos botones. 📱 iPhone abre la app de LinkedIn;
💻 Mac abre Google Chrome, aunque el navegador predeterminado sea otro. Para que funcione el botón de la
Mac, corre una sola vez:

    bash ~/Proyectos/linkedin-agent/herramientas/instalar_abrir_en_chrome.sh

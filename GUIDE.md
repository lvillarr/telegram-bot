# Guía: Bot Telegram Multiagente con Claude

Guía completa para replicar este sistema en cualquier gerencia o subgerencia desde cero.

---

## Arquitectura del sistema

```
Usuario (Telegram)
       │
       ▼
   bot.py (Python)
       │
       ├── Claude API (Anthropic) ── Agentes IA
       │        └── SYSTEM_PROMPT
       │              ├── orquestador.md   ← Líder estratégico
       │              ├── agente_td.md     ← Transformación Digital
       │              ├── agente_ia.md     ← Inteligencia Artificial
       │              └── agente_eo.md     ← Excelencia Operacional
       │
       └── Groq API ── Whisper (transcripción de audio)
```

El bot recibe mensajes (texto, imagen, audio, documentos), los procesa con Claude usando el contexto de los agentes y responde en Telegram. Opcionalmente genera artefactos (Excel, gráfico PNG, dashboard HTML).

---

## Requisitos previos

### Cuentas y API keys necesarias

| Servicio | Para qué | Costo | URL |
|---|---|---|---|
| **Telegram** | Crear el bot | Gratis | telegram.org |
| **Anthropic** | Claude API (LLM) | ~$5–20/mes según uso | console.anthropic.com |
| **Groq** | Transcripción de audio | Gratis (con límites) | console.groq.com |
| **GitHub** | Repositorio del código | Gratis | github.com |
| **Railway** | Deploy del bot (servidor) | ~$5/mes | railway.app |

### Software local necesario
- Python 3.11+
- Git
- Editor de código (VS Code recomendado)

---

## Paso 1 — Crear el bot en Telegram

1. Abre Telegram y busca **@BotFather**
2. Escribe `/newbot`
3. Elige un nombre (ej: `Gerencia Finanzas Bot`)
4. Elige un username (ej: `finanzas_arauco_bot`) — debe terminar en `bot`
5. BotFather te entrega un **token** → guárdalo, lo necesitarás después

```
Ejemplo de token:
7412345678:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## Paso 2 — Obtener las API keys

### Anthropic (Claude)
1. Ve a [console.anthropic.com](https://console.anthropic.com)
2. **API Keys** → **Create Key**
3. Copia la key: `sk-ant-api03-...`

### Groq (audio)
1. Ve a [console.groq.com](https://console.groq.com)
2. **API Keys** → **Create API Key**
3. Copia la key: `gsk_...`

---

## Paso 3 — Estructura de archivos del proyecto

Crea esta estructura en tu computador:

```
mi-bot/
├── bot.py                  ← Código principal del bot
├── requirements.txt        ← Dependencias Python
├── prompts/
│   ├── orquestador.md      ← Definición del orquestador
│   ├── agente_1.md         ← Agente especializado 1
│   ├── agente_2.md         ← Agente especializado 2
│   └── agente_3.md         ← Agente especializado 3
```

---

## Paso 4 — Definir los agentes (archivos .md)

Esta es la parte más importante: defines **quién es** cada agente, **qué sabe** y **cómo actúa**.

### Estructura de un archivo de agente

```markdown
# Agente: [Nombre del rol]
**Rol**: [Descripción del rol en una línea]

## Identidad y perfil profesional
[Quién es, cuánta experiencia tiene, en qué es experto]

## Dominio de conocimiento
[Lista de temas, sistemas, procesos que domina]

## Glosario del área
| Término | Definición |
|---|---|
| KPI | Indicador clave de desempeño |

## Comportamiento esperado
[Cómo responde, qué tono usa, qué entregables genera]
```

### Ejemplo: Agente para Gerencia de Finanzas

**`prompts/agente_finanzas.md`**
```markdown
# Agente: Analista de Finanzas Corporativas
**Rol**: Especialista en análisis financiero, presupuestos y reporting ejecutivo

## Identidad
Eres un analista financiero senior con 15 años de experiencia en empresas del
sector forestal e industrial chileno. Dominas el análisis de estados financieros,
control presupuestario, proyecciones y reporting para directorio.

## Dominio de conocimiento
- Estados financieros: balance, EERR, flujo de caja
- Presupuestos anuales y control de gestión
- KPIs financieros: EBITDA, ROE, ROI, margen operacional
- Sistemas: SAP FI/CO, HFM (Hyperion), Power BI financiero
- Normativa: IFRS, SVS, auditorías internas

## Glosario
| Término | Definición |
|---|---|
| EBITDA | Ganancias antes de intereses, impuestos, depreciación y amortización |
| HFM | Hyperion Financial Management — consolidación financiera corporativa |
| Centro de costo | Unidad organizacional que acumula costos para control de gestión |

## Comportamiento
- Responde con precisión numérica y cita siempre la fuente del dato
- Formato numérico chileno: punto (.) como miles, coma (,) como decimal
- Cuantifica el impacto financiero de cada hallazgo o recomendación
- Estructura: situación → análisis → recomendación → próximo paso
```

### Ejemplo: Orquestador para Gerencia de Finanzas

**`prompts/orquestador.md`**
```markdown
# Orquestador: Gerente de Finanzas
**Rol**: Líder del sistema multiagente financiero

## Identidad
Eres el Gerente de Finanzas, responsable de la gestión financiera integral
de la compañía. Coordinas los análisis de tus agentes especializados y
presentas conclusiones ejecutivas al directorio y la administración.

## Estructura del equipo
| Área | Agente | Foco |
|---|---|---|
| Finanzas | agente_finanzas | Análisis financiero y KPIs |
| Control de Gestión | agente_control | Presupuesto y desviaciones |
| Tesorería | agente_tesoreria | Flujo de caja y liquidez |

## Estilo de comunicación
- Ejecutivo, directo y orientado a decisiones
- Cuantifica siempre: impacto en CLP, USD, % margen
- Estructura: situación → hallazgo → recomendación → próximo paso
```

---

## Paso 5 — Código del bot (bot.py)

Copia este archivo base y personaliza las secciones marcadas con `# PERSONALIZAR`:

```python
import os, io, re, json, time, base64, tempfile
import anthropic
import groq as groq_lib
import openpyxl
import pdfplumber
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from docx import Document as DocxDocument
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ContextTypes


# ─────────────────────────────────────────────
# PERSONALIZAR: nombre y descripción del sistema
# ─────────────────────────────────────────────
NOMBRE_SISTEMA = "Gerencia de Finanzas"
DESCRIPCION_SISTEMA = "asistente digital del equipo de Finanzas Corporativas"

# ─────────────────────────────────────────────
# PERSONALIZAR: carga tus archivos de agentes
# ─────────────────────────────────────────────
def load_prompt(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

orquestador = load_prompt(os.path.join(PROMPTS_DIR, "orquestador.md"))
agente_1    = load_prompt(os.path.join(PROMPTS_DIR, "agente_finanzas.md"))
agente_2    = load_prompt(os.path.join(PROMPTS_DIR, "agente_control.md"))
agente_3    = load_prompt(os.path.join(PROMPTS_DIR, "agente_tesoreria.md"))


# ─────────────────────────────────────────────
# PERSONALIZAR: identidad del sistema
# ─────────────────────────────────────────────
IDENTIDAD = f"""
Eres el {DESCRIPCION_SISTEMA}. NO eres un asistente genérico.
Representas a un equipo de agentes especializados:

- Orquestador (Gerente de Finanzas): coordina y sintetiza
- Agente Finanzas: análisis financiero, KPIs, EBITDA, estados financieros
- Agente Control de Gestión: presupuesto, desviaciones, centros de costo
- Agente Tesorería: flujo de caja, liquidez, inversiones

Cuando te pregunten qué eres, describe ÚNICAMENTE estos roles.
No inventes capacidades ni agentes adicionales.
"""

SYSTEM_PROMPT = f"""
{IDENTIDAD}
---
{orquestador}
---
{agente_1}
---
{agente_2}
---
{agente_3}

---
## Reglas generales
- No inventes datos financieros. Si no tienes los datos, dilo e indica qué fuente se necesita.
- Formato numérico chileno: punto (.) como miles, coma (,) como decimal.
- Respuestas concisas por defecto. Detalladas si el usuario lo pide.
- En Telegram NO uses encabezados #. Usa *negrita* y emojis para estructurar.
"""


# ─────────────────────────────────────────────
# PERSONALIZAR: mensaje de inicio
# ─────────────────────────────────────────────
START_TEXT = f"""💼 *{NOMBRE_SISTEMA}*

Soy el asistente digital de tu equipo. Integro tres agentes especializados:

📊 *Agente Finanzas*
Análisis financiero, KPIs, EBITDA, estados financieros, SAP FI/CO

📋 *Agente Control de Gestión*
Presupuesto, desviaciones, centros de costo, HFM

💰 *Agente Tesorería*
Flujo de caja, liquidez, inversiones, forex

🧭 *Orquestador (Gerente de Finanzas)*
Coordina los agentes y entrega análisis ejecutivos

---
*Comandos:*
/modelo — Seleccionar modelo LLM
/reset — Reiniciar conversación
/artifact — Genera Excel, gráfico o dashboard HTML

Puedes enviar texto, imagen, audio, PDF, Word o Excel."""


# ─── Clientes API ───
client      = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
groq_client = groq_lib.Groq(api_key=os.environ["GROQ_API_KEY"])

# ─── Modelos disponibles ───
MODELS = {
    "haiku":  ("claude-haiku-4-5-20251001", "⚡ Haiku",  "Rápido y económico"),
    "sonnet": ("claude-sonnet-4-6",          "🧠 Sonnet", "Balanceado"),
    "opus":   ("claude-opus-4-6",            "🚀 Opus",   "Máxima capacidad"),
}
DEFAULT_MODEL = "haiku"

MAX_HISTORY = 20


# ─── Funciones de utilidad ───
def fmt(text: str) -> str:
    text = re.sub(r'^#{3,}\s+(.+)$',  r'*\1*',        text, flags=re.MULTILINE)
    text = re.sub(r'^#{2}\s+(.+)$',   r'\n*\1*',       text, flags=re.MULTILINE)
    text = re.sub(r'^#{1}\s+(.+)$',   r'\n*━━ \1 ━━*', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*',   r'*\1*',         text)
    text = re.sub(r'^[-=]{3,}\s*$',   '─────────────', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}',          '\n\n',           text)
    return text.strip()

def trim_history(history): return history[-MAX_HISTORY:]

def get_model(context): return MODELS[context.user_data.get("model", DEFAULT_MODEL)][0]
def get_model_label(context): return MODELS[context.user_data.get("model", DEFAULT_MODEL)][1]

def model_keyboard(current):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅ ' if k==current else ''}{l} — {d}", callback_data=f"mdl_{k}")]
        for k, (_, l, d) in MODELS.items()
    ])

def claude_response(system, user_msg, max_tokens=512,
                    model="claude-haiku-4-5-20251001", history=None):
    messages = list(history or [])
    messages.append({"role": "user", "content": user_msg})
    for attempt in range(3):
        try:
            r = client.messages.create(model=model, max_tokens=max_tokens,
                                       system=system, messages=messages)
            return r.content[0].text
        except anthropic.APIStatusError as e:
            if e.status_code == 500 and attempt < 2:
                time.sleep(2 ** attempt); continue
            raise

def push_history(context, user_msg, reply):
    h = context.user_data.setdefault("history", [])
    h += [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}]
    context.user_data["history"] = trim_history(h)

ARTIFACT_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("📊 Excel",   callback_data="art_excel"),
    InlineKeyboardButton("📈 Gráfico", callback_data="art_chart"),
    InlineKeyboardButton("🌐 HTML",    callback_data="art_html"),
]])
```

> El resto del código (handlers de imagen, audio, documentos, artefactos) es idéntico al bot original — cópialo directamente desde `bot.py` del repositorio base.

---

## Paso 6 — requirements.txt

```txt
python-telegram-bot==21.3
anthropic
groq
openpyxl
matplotlib
pdfplumber
python-docx
```

---

## Paso 7 — Subir el código a GitHub

```bash
# En tu computador, dentro de la carpeta mi-bot/
git init
git add .
git commit -m "primer commit — bot multiagente finanzas"

# Crea un repo en github.com y luego:
git remote add origin https://github.com/tu-usuario/mi-bot.git
git push -u origin main
```

---

## Paso 8 — Deploy en Railway

1. Ve a [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Selecciona tu repo `mi-bot`
3. Railway detecta Python automáticamente y hace el primer deploy

### Agregar variables de entorno

Ve a tu servicio → **Variables** → agrega:

| Variable | Valor |
|---|---|
| `TELEGRAM_TOKEN` | El token de BotFather |
| `ANTHROPIC_API_KEY` | Tu key de Anthropic |
| `GROQ_API_KEY` | Tu key de Groq |

Railway redespliega automáticamente al guardar las variables.

### Verificar el deploy

En el tab **Deployments** deberías ver los logs con:
```
Application started
Bot polling...
```

---

## Paso 9 — Probar el bot

1. Busca tu bot en Telegram por el username que elegiste en BotFather
2. Escribe `/start` — debería responder con el mensaje de bienvenida personalizado
3. Haz una pregunta sobre tu área y verifica que responde con el contexto correcto

---

## Checklist de personalización

Antes de lanzar a producción, verifica:

- [ ] `prompts/orquestador.md` define el rol del líder con el contexto de tu área
- [ ] Cada agente tiene glosario con términos específicos del área
- [ ] `IDENTIDAD` en `bot.py` lista correctamente los agentes disponibles
- [ ] `START_TEXT` describe las capacidades reales del sistema
- [ ] Las tres API keys están en Railway
- [ ] El bot responde `/start` correctamente en Telegram
- [ ] El bot responde preguntas del dominio sin inventar datos

---

## Personalización avanzada

### Agregar o quitar agentes

En `bot.py`, carga los archivos y agrégalos al `SYSTEM_PROMPT`:

```python
# Agregar un agente nuevo
agente_rrhh = load_prompt(os.path.join(PROMPTS_DIR, "agente_rrhh.md"))

SYSTEM_PROMPT = f"""
{IDENTIDAD}
---
{orquestador}
---
{agente_1}
---
{agente_rrhh}   # ← nuevo agente
"""
```

### Agregar un skill personalizado

```python
SKILL_PROMPTS["informe"] = """📄 /informe — Genera un informe ejecutivo financiero

Actúa como el Gerente de Finanzas. Estructura el informe con:
- Resumen ejecutivo (3 líneas máximo)
- KPIs del período con comparación vs presupuesto
- Principales desviaciones y causas
- Recomendaciones y próximos pasos"""
```

Luego registra el comando:
```python
app.add_handler(CommandHandler("informe", skill_handler))
```

### Cambiar el branding del dashboard HTML

En `ARTIFACT_PROMPTS["html"]` cambia los colores y logo:

```python
# Ejemplo para otra empresa
ARTIFACT_PROMPTS["html"] = """Genera un dashboard HTML con estos colores:
- Primario: #1A3C6B (azul corporativo)
- Acento: #F5A623 (naranja)
- Logo: <img src="URL_DE_TU_LOGO" height="32">
...
"""
```

---

## Mantenimiento y versiones

```bash
# Actualizar los agentes (prompts)
# 1. Edita el archivo .md correspondiente
# 2. Sube los cambios:
git add prompts/
git commit -m "actualiza agente finanzas con nuevos KPIs Q2"
git push origin main
# Railway redeploya automáticamente

# Crear una nueva versión
git tag -a v1.1.0 -m "v1.1.0 — descripción de cambios"
git push origin v1.1.0
```

---

## Resumen del flujo completo

```
1. BotFather          → TELEGRAM_TOKEN
2. Anthropic          → ANTHROPIC_API_KEY
3. Groq               → GROQ_API_KEY
4. Escribir prompts/  → definir agentes del área
5. Copiar bot.py      → personalizar IDENTIDAD y START_TEXT
6. GitHub             → subir código
7. Railway            → conectar repo + agregar variables
8. Telegram           → /start y probar
```

Tiempo estimado desde cero hasta bot funcionando: **2–4 horas**.

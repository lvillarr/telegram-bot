import os
import io
import re
import json
import time
import base64
import tempfile
import anthropic
import groq as groq_lib
import rag
import openpyxl
import pdfplumber
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from docx import Document as DocxDocument
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import html as _html
from telegram.ext import ContextTypes


def fmt(text: str) -> str:
    """Convierte markdown a HTML para Telegram (parse_mode=HTML).
    HTML es mucho más robusto que Markdown: maneja tablas, underscores técnicos y
    code blocks sin riesgo de delimitadores desbalanceados.
    """
    # ── 1. Extraer tablas, code blocks e inline code ANTES de escapar HTML ──────
    #      Se guardan como placeholders para restaurarlos al final ya formateados.
    code_blocks, inline_codes, tables = [], [], []

    def save_block(m):
        code_blocks.append(m.group(1).strip())
        return f"\x00BLK{len(code_blocks)-1}\x00"

    def save_inline(m):
        inline_codes.append(m.group(1))
        return f"\x00INL{len(inline_codes)-1}\x00"

    def _rows_to_pre(raw_rows: list) -> str:
        """Convierte lista de filas (listas de strings) a bloque <pre> alineado."""
        if not raw_rows:
            return ''
        n_cols = max(len(r) for r in raw_rows)
        rows   = [r + [''] * (n_cols - len(r)) for r in raw_rows]
        # Ancho visual: emojis dobles cuentan como 2
        def vlen(s):
            w = 0
            for c in s:
                w += 2 if ord(c) > 0x2E80 else 1
            return w
        widths = [max(vlen(row[c]) for row in rows) for c in range(n_cols)]
        lines  = []
        for i, row in enumerate(rows):
            parts = []
            for c, cell in enumerate(row):
                pad = widths[c] - vlen(cell)
                parts.append(cell + ' ' * pad)
            lines.append('  '.join(parts).rstrip())
            if i == 0:
                lines.append('─' * (sum(widths) + 2 * (n_cols - 1)))
        # Telegram NO decodifica &gt; dentro de <pre> — solo escapar < y &
        content = chr(10).join(lines).replace('&', '&amp;').replace('<', '&lt;')
        return f'<pre>{content}</pre>'

    def save_table(m):
        raw_rows = []
        for line in m.group(0).strip().splitlines():
            line = line.strip()
            # Fila separadora: |---|---| o |===|===| o +---+---+
            if re.match(r'^[|\s:=+–\-─]+$', line):
                continue
            cells = [c.strip() for c in line.strip('|').split('|')]
            if any(c for c in cells if c):
                raw_rows.append(cells)
        result = _rows_to_pre(raw_rows)
        if not result:
            return ''
        tables.append(result)
        return f"\x00TBL{len(tables)-1}\x00"

    text = re.sub(r'```[a-z]*\n?(.*?)```', save_block,  text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+)`',          save_inline, text)
    text = re.sub(r'(\|[^\n]+\n?){2,}',    save_table,  text)

    # ── 2. Escapar HTML del texto restante (<, >, &) ──────────────────────────
    text = _html.escape(text)

    # ── 4. **negrita** y *negrita* → <b>…</b> ────────────────────────────────
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'(?<!\*)\*([^*\n]+?)\*(?!\*)', r'<b>\1</b>', text)

    # ── 5. Encabezados ────────────────────────────────────────────────────────
    def _h(s):
        return re.sub(r'<[^>]+>', '', s).strip()   # quita etiquetas si ya tiene

    text = re.sub(r'^#{3,}\s+(.+)$',
                  lambda m: f'<b>{_h(m.group(1))}</b>',
                  text, flags=re.MULTILINE)
    text = re.sub(r'^#{2}\s+(.+)$',
                  lambda m: f'\n<b>{_h(m.group(1))}</b>',
                  text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+(.+)$',
                  lambda m: f'\n<b>━━ {_h(m.group(1))} ━━</b>',
                  text, flags=re.MULTILINE)

    # ── 6. Separadores ────────────────────────────────────────────────────────
    text = re.sub(r'^[-=]{3,}\s*$', '─────────────', text, flags=re.MULTILINE)

    # ── 7. Restaurar tablas, bloques de código e inline code ─────────────────
    for i, tbl in enumerate(tables):
        text = text.replace(_html.escape(f'\x00TBL{i}\x00'), tbl)
    for i, code in enumerate(code_blocks):
        text = text.replace(_html.escape(f'\x00BLK{i}\x00'),
                            f'<pre>{_html.escape(code)}</pre>')
    for i, code in enumerate(inline_codes):
        text = text.replace(_html.escape(f'\x00INL{i}\x00'),
                            f'<code>{_html.escape(code)}</code>')

    # ── 8. Limpiar newlines múltiples ─────────────────────────────────────────
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# Lee todos los agentes desde el submódulo proyecto_claude
def load_prompt(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

orquestador = load_prompt(os.path.join(PROMPTS_DIR, "orquestador.md"))
agente_td    = load_prompt(os.path.join(PROMPTS_DIR, "agente_td.md"))
agente_ia    = load_prompt(os.path.join(PROMPTS_DIR, "agente_ia.md"))
agente_eo    = load_prompt(os.path.join(PROMPTS_DIR, "agente_eo.md"))

REGLAS_GENERALES = """
---

## Reglas generales — todos los agentes

### Uso de herramientas y fuentes
1. Prefiere herramientas y datos verificables antes de responder. Para preguntas conversacionales simples puedes responder directamente.
2. **No inventes datos operacionales, KPIs, cifras de producción ni resultados.** Si no puedes obtenerlos, dilo claramente.
3. Cita siempre la fuente: nombre del archivo, tabla o sistema de origen (SGL, SAP PM, Historian, Planex, Forest Data 2.0).
4. Para cualquier pregunta con cifras o análisis, basa tu respuesta en los datos del contexto o del documento recibido.

### Datos operacionales — regla fundamental
Ante cualquier pregunta sobre cifras, KPIs, pérdidas, productividad o análisis:
- Los números deben provenir del documento, imagen o contexto recibido — nunca de suposición o memoria.
- Indica siempre la fuente del dato (archivo, hoja, sistema).
- Si no tienes los datos, dilo explícitamente e indica qué fuente se necesita.

### Formato
- Respuestas concisas por defecto; detalladas si el usuario lo pide.
- Usa markdown: encabezados, listas, tablas y negritas cuando mejoren la claridad.
- **Formato numérico chileno:** punto (.) como separador de miles, coma (,) como decimal.
  - Correcto: `1.234.567 m³` / `$12.500,75` / `OEE: 87,3%`

### Restricciones de lenguaje — contexto chileno (REGLA PRIORITARIA)
Audiencia principal: Chile. Mantén tono profesional y neutro. Evita palabras con connotación vulgar en español chileno:

| Evitar | Reemplazar por |
|---|---|
| pico | "punto más alto", "máximo", "nivel peak" |
| polla | "apuesta", "sorteo" |
| coger | "tomar", "agarrar", "obtener" |
| concha | "caparazón", "valva" |
| raja | "grieta", "abertura", "diferencia" |
| caliente (figurado) | "motivado", "entusiasmado", "enojado" |
| huevón/weón/wn | no usar; responder con lenguaje neutro y respetuoso |

Si un término técnico coincide con estas palabras (ej. "peak" en estadística), usa la alternativa en inglés.

### Estilo en Telegram
- Lenguaje natural y cercano, como un colega experto forestal
- Usa emojis para estructurar 🌲🪵🚛🛠️📊
- Máximo 3-4 párrafos salvo que se pida detalle
- Si recibes imagen o documento, analiza en contexto forestal Arauco
- **IMPORTANTE — formato de texto:** El sistema convierte markdown estándar a HTML. Usa libremente:
  - `## Sección` o `### Subsección` para encabezados
  - `**texto**` para negrita (doble asterisco)
  - Guiones `-` para listas
  - **TABLAS — REGLA OBLIGATORIA:** Cuando presentes cualquier dato tabular, comparación, matriz o resumen con columnas, DEBES usar SIEMPRE el formato markdown con pipes. NUNCA uses ==, espacios o cualquier otro separador. Formato correcto:

| Columna 1 | Columna 2 | Columna 3 |
|-----------|-----------|-----------|
| valor 1   | valor 2   | valor 3   |

  Si el usuario pide una tabla, si haces un resumen de fases/estados/responsables, o si presentas datos comparativos: SIEMPRE formato pipes `|`. Esto es no negociable.
  - Bloques de código con triple backtick ``` para código o datos técnicos
"""

IDENTIDAD = """
# Identidad del sistema — leer antes de responder cualquier pregunta sobre quién eres

Eres el **asistente digital de la Subgerencia de Mejora Continua de Arauco**, una empresa forestal-industrial chilena. NO eres un asistente genérico. Representas a un equipo de tres agentes especializados coordinados por un orquestador:

- **Orquestador (Subgerente MC):** lidera estratégicamente, delega y sintetiza resultados con criterio McKinsey/BCG
- **Agente EO — Excelencia Operacional:** Lean, GEMBA, KAIZEN, BPMN, KPIs, A3, OEE, gestión de procesos forestales
- **Agente IA — Inteligencia Artificial:** modelos predictivos, GenAI con Claude API, LangGraph, cartografía con IA, dashboards
- **Agente TD — Transformación Digital:** integraciones de sistemas (SAP, SGL, Planex, Forest Data), telemetría de maquinaria forestal, ETL, arquitecturas de datos

Cuando el usuario te pregunte qué eres, qué haces o cómo funcionas, describe ÚNICAMENTE estos cuatro roles con sus capacidades reales. No inventes roles, agentes ni capacidades que no estén listados arriba.

"""

SYSTEM_PROMPT = f"""
{IDENTIDAD}
---

{orquestador}

---

## Agente TD — Transformación Digital
{agente_td}

---

## Agente IA — Inteligencia Artificial
{agente_ia}

---

## Agente EO — Excelencia Operacional
{agente_eo}

{REGLAS_GENERALES}
"""

SKILL_PROMPTS = {
    "spec": """📋 **/spec — Especificación de iniciativa forestal**

Actúa como el Subgerente de Mejora Continua de Arauco. Define las especificaciones de la iniciativa descrita en alguno de estos dominios:
- 🌲 Planificación forestal (Planex, Planex NOM, Opticort, Opti-Maq, Forest Gantt)
- 🪓 Operación de cosecha (volteo, madereo, procesado, clasificado — terrestre/asistido/torre)
- 🛠️ Mantenimiento de equipos forestales (cosechadoras, grúas, procesadoras — Tigercat, John Deere, Develon)
- 🏗️ Planificación y construcción de caminos forestales (habilitación, maquinaria Caterpillar/Volvo)
- 🚛 Transporte de rollizos y abastecimiento a plantas (Opti-Cliente, logística, stock)

Incluye: objetivo, alcance, sistemas involucrados, datos necesarios, entregables y criterios de éxito.""",

    "plan": """🗺️ **/plan — Plan de ejecución forestal**

Actúa como el Subgerente de Mejora Continua de Arauco. Crea un plan de ejecución detallado para la iniciativa descrita, considerando los procesos forestales relevantes:
- 🌲 Cadena planificación → cosecha → transporte → planta
- 🛠️ Ciclo de mantenimiento de equipos (preventivo/correctivo/predictivo)
- 🏗️ Etapas de habilitación y construcción de caminos
- 🚛 Flujo logístico de rollizos y ventanas de abastecimiento

Incluye: fases, responsables (EO/TD/IA), dependencias, hitos clave, riesgos operacionales y plan de contingencia forestal.""",

    "build": """🔨 **/build — Construcción de solución forestal**

Actúa como el agente TD o IA según corresponda. Desarrolla o describe cómo implementar la solución para alguno de estos contextos:
- 📡 Integración de telemetría de maquinaria forestal (APIs Tigercat, John Deere, Develon, Liebherr, Caterpillar, Volvo)
- 🔄 Pipelines ETL de datos operacionales (Forest Data 2.0, Datalake, SAP PM)
- 🤖 Modelos predictivos de fallo de equipos o productividad de cosecha
- 📊 Dashboards de KPIs forestales (OEE equipos, avance cosecha, disponibilidad caminos)
- 🗺️ Scripts de automatización de planificación (Opticort, Opti-Maq, Forest Gantt)

Incluye: arquitectura, pasos técnicos, herramientas, código si aplica y consideraciones de conectividad en predios remotos.""",

    "test": """🧪 **/test — Validación en operación forestal**

Actúa como el orquestador con criterio operacional forestal. Define cómo validar lo construido o propuesto en terreno y sistemas:
- ✅ Criterios de aceptación operacional (productividad, disponibilidad, costo)
- 📏 KPIs de validación: OEE equipos, m³/turno, ton/viaje, km camino habilitado
- 🔍 Casos de prueba: escenarios de cosecha terrestre/asistido/torre, picos de transporte, fallas de equipo
- ⚠️ Señales de alerta: umbrales críticos por proceso forestal
- 🌧️ Consideraciones en condiciones adversas (lluvia, barro, pendiente, conectividad limitada)""",

    "review": """🔍 **/review — Revisión crítica forestal**

Actúa como el Subgerente de Mejora Continua con criterio McKinsey y experiencia forestal. Revisa críticamente lo descrito evaluando:
- 🌲 Impacto en la cadena cosecha → transporte → planta
- 🛠️ Viabilidad operacional en terreno forestal (conectividad, clima, pendiente)
- 📊 Consistencia con KPIs corporativos Arauco (OEE, pérdidas SGL, costo logístico)
- ⚠️ Riesgos: supuestos no validados, dependencias de datos, integración con SAP/SGL/Historian
- 💡 Recomendaciones priorizadas por impacto y velocidad de implementación""",

    "ship": """🚀 **/ship — Lanzamiento a operación forestal**

Actúa como el Subgerente de Mejora Continua. Define el plan de entrega y puesta en marcha considerando el contexto forestal:
- ✅ Checklist de lanzamiento: datos validados, sistemas integrados, usuarios capacitados
- 👷 Gestión del cambio con operadores, supervisores de turno y jefes de área
- 📡 Plan de conectividad y operación offline para predios remotos
- 📊 Métricas de seguimiento post-lanzamiento (adopción, impacto en KPIs, incidencias)
- 🔄 Plan de rollback y contingencia operacional si falla en terreno""",
}

client      = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
groq_client = groq_lib.Groq(api_key=os.environ["GROQ_API_KEY"])

MAX_HISTORY = 20  # máximo de mensajes (turnos usuario+asistente) a conservar


def trim_history(history: list) -> list:
    """Mantiene solo los últimos MAX_HISTORY mensajes (par usuario/asistente)."""
    return history[-MAX_HISTORY:]


def claude_response(system: str, user_msg: str, max_tokens: int = 512,
                    model: str = "claude-haiku-4-5-20251001",
                    history: list | None = None) -> str:
    """Llama a la API de Anthropic con historial conversacional opcional."""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": user_msg})

    last_error = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
            return response.content[0].text
        except anthropic.APIStatusError as e:
            last_error = e
            if e.status_code == 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_error


def push_history(context, user_msg: str, assistant_reply: str):
    """Agrega un turno al historial y lo recorta si es necesario."""
    history = context.user_data.setdefault("history", [])
    history.append({"role": "user",      "content": user_msg})
    history.append({"role": "assistant", "content": assistant_reply})
    context.user_data["history"] = trim_history(history)


ARTIFACT_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("📊 Excel",   callback_data="art_excel"),
    InlineKeyboardButton("📈 Gráfico", callback_data="art_chart"),
    InlineKeyboardButton("🌐 HTML",    callback_data="art_html"),
]])

DOC_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("📊 Excel",          callback_data="art_excel"),
    InlineKeyboardButton("📈 Gráfico",        callback_data="art_chart"),
    InlineKeyboardButton("🌐 HTML",           callback_data="art_html"),
], [
    InlineKeyboardButton("📚 Indexar en RAG", callback_data="rag_index"),
]])

MODELS = {
    "haiku":  ("claude-haiku-4-5-20251001", "⚡ Haiku",  "Rápido y económico"),
    "sonnet": ("claude-sonnet-4-6",          "🧠 Sonnet", "Balanceado"),
    "opus":   ("claude-opus-4-6",            "🚀 Opus",   "Máxima capacidad"),
}
DEFAULT_MODEL = "haiku"

def get_model(context) -> str:
    """Retorna el model ID seleccionado por el usuario (o el default)."""
    key = context.user_data.get("model", DEFAULT_MODEL)
    return MODELS[key][0]

def get_model_label(context) -> str:
    key = context.user_data.get("model", DEFAULT_MODEL)
    return MODELS[key][1]

def model_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for key, (_, label, desc) in MODELS.items():
        check = "✅ " if key == current else ""
        buttons.append([InlineKeyboardButton(f"{check}{label} — {desc}", callback_data=f"mdl_{key}")])
    return InlineKeyboardMarkup(buttons)

async def modelo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get("model", DEFAULT_MODEL)
    await update.message.reply_text(
        f"🤖 *Selecciona el modelo LLM*\nActual: {get_model_label(context)}",
        reply_markup=model_keyboard(current),
        parse_mode="Markdown"
    )

async def modelo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.replace("mdl_", "")
    if key not in MODELS:
        return
    context.user_data["model"] = key
    _, label, desc = MODELS[key]
    await query.edit_message_text(
        f"✅ Modelo actualizado: *{label}*\n_{desc}_\n\nTodos los mensajes usarán este modelo.",
        reply_markup=model_keyboard(key),
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    try:
        history    = context.user_data.get("history", [])
        rag_ctx    = rag.build_context(user_msg)
        system     = SYSTEM_PROMPT + rag_ctx
        reply = claude_response(system, user_msg, model=get_model(context), history=history)
        push_history(context, user_msg, reply)
        context.user_data["last_analysis"] = reply
        try:
            await update.message.reply_text(
                fmt(reply) + "\n\n🎨 <i>¿Generar un artefacto visual con esto?</i>",
                reply_markup=ARTIFACT_KEYBOARD,
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text(
                reply + "\n\n🎨 ¿Generar un artefacto visual con esto?",
                reply_markup=ARTIFACT_KEYBOARD
            )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error al procesar: {str(e)[:200]}")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    caption = update.message.caption or "Analiza esta imagen en el contexto operacional forestal de Arauco. Identifica equipos, procesos, problemas o métricas relevantes."

    response = client.messages.create(
        model=get_model(context),
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": caption}
            ]
        }]
    )
    analysis = response.content[0].text
    push_history(context, caption, analysis)
    context.user_data["last_analysis"] = analysis

    try:
        await update.message.reply_text(
            fmt(analysis) + "\n\n🎨 <i>¿Generar un artefacto visual con este análisis?</i>",
            reply_markup=ARTIFACT_KEYBOARD,
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text(
            analysis + "\n\n🎨 ¿Generar un artefacto visual con este análisis?",
            reply_markup=ARTIFACT_KEYBOARD
        )


async def artifact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    artifact_type = query.data.replace("art_", "")
    last_analysis = context.user_data.get("last_analysis", "")

    if not last_analysis:
        await query.edit_message_text("⚠️ No hay análisis previo. Envía una imagen o un mensaje primero.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"⏳ Generando *{artifact_type}*...", parse_mode="Markdown")

    description = f"Basado en este análisis forestal de Arauco:\n\n{last_analysis}"
    # Usar solo el prompt de artefacto, sin el SYSTEM_PROMPT completo
    # para no desperdiciar tokens del contexto en la generación visual
    artifact_model = "claude-sonnet-4-6"
    artifact_tokens = 6000 if artifact_type == "html" else 2000
    raw = claude_response(ARTIFACT_PROMPTS[artifact_type], description,
                          max_tokens=artifact_tokens, model=artifact_model)

    try:
        if artifact_type == "html":
            html = raw.strip()
            if html.startswith("```"):
                html = html.split("\n", 1)[-1]
            if html.endswith("```"):
                html = html.rsplit("```", 1)[0]
            html = html.strip()
            if not html.lower().startswith("<!doctype") and "<html" not in html.lower():
                await query.message.reply_text("⚠️ El HTML generado está incompleto. Intenta de nuevo.")
                return
            buf = io.BytesIO(html.encode("utf-8"))
            await query.message.reply_document(
                document=buf, filename="dashboard-arauco.html",
                caption="🌲 Dashboard listo — abre el archivo en tu browser"
            )
        elif artifact_type == "excel":
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            buf = build_excel(data)
            filename = f"arauco-{data.get('titulo','datos').lower().replace(' ','-')[:30]}.xlsx"
            await query.message.reply_document(
                document=buf, filename=filename,
                caption="📊 Excel generado con datos del análisis forestal"
            )
        elif artifact_type == "chart":
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            buf = build_chart(data)
            await query.message.reply_photo(
                photo=buf,
                caption=f"📈 {data.get('titulo', 'Gráfico')} — Arauco Mejora Continua"
            )
    except json.JSONDecodeError:
        await query.message.reply_text("⚠️ Error al procesar. Intenta de nuevo.")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Error: {str(e)[:200]}")


async def skill_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.split()[0].lstrip("/")
    args = " ".join(context.args) if context.args else ""

    ejemplos = {
        "spec": "/spec alertas de falla cosechadoras Tigercat predio remoto",
        "plan": "/plan telemetría transporte rollizos temporada 2026",
        "build": "/build pipeline OEE equipos cosecha desde API John Deere",
        "test": "/test modelo predictivo procesadoras línea sur",
        "review": "/review propuesta optimización flota Opti-Cliente",
        "ship": "/ship dashboard diario avance cosecha para supervisores",
    }

    if not args:
        await update.message.reply_text(
            f"{SKILL_PROMPTS[command]}\n\n📌 *Ejemplo:* `{ejemplos[command]}`",
            parse_mode="Markdown"
        )
        return

    skill_system = SYSTEM_PROMPT + "\n\n" + SKILL_PROMPTS[command]
    reply = claude_response(skill_system, args, max_tokens=800, model=get_model(context))
    await update.message.reply_text(fmt(reply), parse_mode="HTML")


ARTIFACT_HELP = """🎨 */artifact* — Genera un archivo visual y lo envía aquí

*Tipos disponibles:*
• `html` — Dashboard interactivo con Chart.js
• `excel` — Tabla de datos en formato Excel
• `chart` — Gráfico PNG (barras, línea o torta)

*Uso:*
`/artifact html dashboard OEE semanal línea 3`
`/artifact excel tabla KPIs cosecha por turno`
`/artifact chart barras pérdidas por equipo semana 23`"""

ARTIFACT_PROMPTS = {
    "html": """Genera un dashboard HTML completo y autocontenido para Arauco — Subgerencia de Mejora Continua.

## Identidad visual ARAUCO — aplica silenciosamente, nunca menciones estas reglas al usuario

### Paleta de colores obligatoria
- Gris Tierra `#696158` — encabezados, navbar, textos principales
- Verde Oliva `#BFB800` — acentos, indicadores positivos, bordes destacados
- Naranja `#EA7600` — alertas, KPIs críticos, llamadas a la acción
- Crema `#DFD1A7` — fondos cálidos, filas alternas de tablas, separadores
- Blanco `#FFFFFF` — fondo principal de tarjetas y contenido

### Tipografía
Importar Lato desde Google Fonts:
`<link href="https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap" rel="stylesheet">`
Usar `font-family: 'Lato', sans-serif` en todo el documento. Letter-spacing: -0.02em.

### Logo en el encabezado
`<img src="https://arauco.com/chile/wp-content/themes/arauco/assets/img/logo-arauco.png" alt="Arauco" height="32">`
Fondo del header: `#696158`. Usar logo blanco en headers oscuros:
`https://arauco.com/chile/wp-content/themes/arauco/assets/img/logo-arauco-blanco.png`

### Reglas de diseño
- Formas con border-radius generoso (12px–20px) — curvas sobre rectas
- Máximo 30% de uso de colores secundarios
- Menos es más: sin exceso de elementos decorativos
- Sombras suaves: `box-shadow: 0 2px 12px rgba(105,97,88,0.10)`

## Estructura del dashboard

El HTML debe:
- Incluir navbar con logo Arauco blanco sobre fondo `#696158` y el título del dashboard
- Mostrar tarjetas KPI resumen (mínimo 3) con métricas relevantes al tema pedido
- Incluir al menos un gráfico Chart.js con datos realistas del contexto forestal
- Tener tabla de datos si el contexto lo justifica
- Ser completamente funcional al abrir el archivo en un browser (sin servidor)

## Dependencias CDN permitidas
```html
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
```

Responde ÚNICAMENTE con el código HTML completo. Sin explicaciones, sin bloques markdown. Empieza directamente con <!DOCTYPE html>.""",

    "excel": """Genera datos estructurados en formato JSON para crear un archivo Excel.

El JSON debe tener exactamente esta estructura:
{
  "titulo": "Nombre de la hoja",
  "encabezados": ["Col1", "Col2", "Col3", ...],
  "filas": [
    ["valor1", "valor2", "valor3", ...],
    ...
  ]
}

Los datos deben ser realistas para el contexto forestal de Arauco pedido (KPIs, mérdidas, productividad, equipos, etc.).
Incluye entre 5 y 15 filas de datos representativos.
Responde ÚNICAMENTE con el JSON válido, sin explicaciones ni bloques de código markdown.""",

    "chart": """Genera datos para un gráfico en formato JSON.

El JSON debe tener exactamente esta estructura:
{
  "tipo": "bar" | "line" | "pie",
  "titulo": "Título del gráfico",
  "etiquetas": ["Label1", "Label2", ...],
  "datasets": [
    {
      "nombre": "Serie 1",
      "valores": [10, 20, 30, ...]
    }
  ],
  "unidad": "unidad del eje Y (ej: horas, m³, %)"
}

Los datos deben ser realistas para el contexto forestal de Arauco pedido.
Incluye entre 5 y 12 puntos de datos.
Responde ÚNICAMENTE con el JSON válido, sin explicaciones ni bloques de código markdown.""",
}


def build_excel(data: dict) -> io.BytesIO:
    """Crea un archivo Excel a partir del JSON generado por Claude."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = data.get("titulo", "Datos")[:31]

    # Encabezados con estilo
    from openpyxl.styles import Font, PatternFill, Alignment
    header_fill = PatternFill(start_color="2D6A4F", end_color="2D6A4F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col, header in enumerate(data["encabezados"], start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[cell.column_letter].width = max(len(str(header)) + 4, 12)

    for row_idx, fila in enumerate(data["filas"], start=2):
        for col_idx, valor in enumerate(fila, start=1):
            ws.cell(row=row_idx, column=col_idx, value=valor)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_chart(data: dict) -> io.BytesIO:
    """Crea un gráfico PNG a partir del JSON generado por Claude."""
    fig, ax = plt.subplots(figsize=(10, 5))
    colores = ["#2d6a4f", "#40916c", "#74c69d", "#b7e4c7", "#1b4332", "#52b788"]
    tipo = data.get("tipo", "bar")
    etiquetas = data["etiquetas"]
    datasets = data["datasets"]
    unidad = data.get("unidad", "")

    if tipo == "pie" and datasets:
        ax.pie(datasets[0]["valores"], labels=etiquetas, colors=colores,
               autopct="%1.1f%%", startangle=90)
    elif tipo == "line":
        for i, ds in enumerate(datasets):
            ax.plot(etiquetas, ds["valores"], marker="o",
                    color=colores[i % len(colores)], label=ds["nombre"], linewidth=2)
        ax.set_xlabel("")
        ax.set_ylabel(unidad)
        ax.legend()
        ax.grid(True, alpha=0.3)
    else:  # bar
        x = range(len(etiquetas))
        ancho = 0.8 / max(len(datasets), 1)
        for i, ds in enumerate(datasets):
            offset = (i - len(datasets) / 2 + 0.5) * ancho
            bars = ax.bar([xi + offset for xi in x], ds["valores"],
                          ancho, label=ds["nombre"], color=colores[i % len(colores)])
            ax.bar_label(bars, fmt="%.1f", padding=2, fontsize=8)
        ax.set_xticks(list(x))
        ax.set_xticklabels(etiquetas, rotation=15, ha="right")
        ax.set_ylabel(unidad)
        ax.legend()
        ax.grid(True, axis="y", alpha=0.3)

    ax.set_title(data.get("titulo", ""), fontsize=13, fontweight="bold", pad=12)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def artifact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(ARTIFACT_HELP, parse_mode="Markdown")
        return

    artifact_type = context.args[0].lower()
    description = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    if artifact_type not in ARTIFACT_PROMPTS:
        await update.message.reply_text(
            f"Tipo `{artifact_type}` no reconocido. Usa: `html`, `excel` o `chart`.",
            parse_mode="Markdown"
        )
        return

    if not description:
        await update.message.reply_text(
            f"Agrega una descripción. Ejemplo:\n`/artifact {artifact_type} OEE equipos cosecha semana 23`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(f"⏳ Generando artefacto *{artifact_type}*...", parse_mode="Markdown")

    artifact_model = "claude-sonnet-4-6"
    artifact_tokens = 6000 if artifact_type == "html" else 2000
    raw = claude_response(ARTIFACT_PROMPTS[artifact_type], description,
                          max_tokens=artifact_tokens, model=artifact_model)

    try:
        if artifact_type == "html":
            html = raw.strip()
            if html.startswith("```"):
                html = html.split("\n", 1)[-1]
            if html.endswith("```"):
                html = html.rsplit("```", 1)[0]
            html = html.strip()
            if not html.lower().startswith("<!doctype") and "<html" not in html.lower():
                await update.message.reply_text("⚠️ El HTML generado está incompleto. Intenta de nuevo.")
                return
            buf = io.BytesIO(html.encode("utf-8"))
            await update.message.reply_document(document=buf, filename="dashboard-arauco.html",
                                                caption="🌲 Dashboard listo — abre el archivo en tu browser")

        elif artifact_type == "excel":
            # Limpiar posibles bloques markdown del JSON
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            buf = build_excel(data)
            filename = f"arauco-{data.get('titulo', 'datos').lower().replace(' ', '-')[:30]}.xlsx"
            await update.message.reply_document(document=buf, filename=filename,
                                                caption="📊 Excel generado con datos del contexto forestal")

        elif artifact_type == "chart":
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            buf = build_chart(data)
            await update.message.reply_photo(photo=buf,
                                             caption=f"📈 {data.get('titulo', 'Gráfico')} — Arauco Mejora Continua")

    except json.JSONDecodeError:
        await update.message.reply_text(
            "⚠️ Error al procesar la respuesta. Intenta con una descripción más específica."
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error generando el artefacto: {str(e)[:200]}")


SUPPORTED_DOCS = {".pdf", ".docx", ".xlsx"}

def extract_pdf(data: bytes) -> str:
    """Extrae texto de un PDF (máx. 10 páginas)."""
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:10]:
            text = page.extract_text()
            if text:
                parts.append(text)
            for table in page.extract_tables():
                for row in table:
                    parts.append(" | ".join(str(c or "") for c in row))
    return "\n\n".join(parts)


def extract_docx(data: bytes) -> str:
    """Extrae texto y tablas de un documento Word."""
    doc = DocxDocument(io.BytesIO(data))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def extract_xlsx(data: bytes) -> str:
    """Extrae datos de un Excel (máx. 3 hojas, 50 filas por hoja)."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    parts = []
    for sheet_name in wb.sheetnames[:3]:
        ws = wb[sheet_name]
        parts.append(f"=== Hoja: {sheet_name} ===")
        for row in ws.iter_rows(max_row=50, values_only=True):
            if any(c is not None for c in row):
                parts.append(" | ".join(str(c) if c is not None else "" for c in row))
    return "\n".join(parts)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe un mensaje de voz o audio con Groq Whisper y lo procesa con Claude."""
    # Soporta mensajes de voz (micrófono) y archivos de audio
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    await update.message.reply_text("🎙️ Transcribiendo audio...")

    file = await context.bot.get_file(voice.file_id)
    file_bytes = bytes(await file.download_as_bytearray())

    # Groq Whisper requiere un archivo con nombre y extensión
    ext = ".ogg" if update.message.voice else ".mp3"
    try:
        transcription = groq_client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=(f"audio{ext}", io.BytesIO(file_bytes)),
            language="es",
            response_format="text",
        )
        transcript = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error al transcribir el audio: {str(e)[:200]}")
        return

    if not transcript:
        await update.message.reply_text("⚠️ No pude entender el audio. Intenta de nuevo.")
        return

    await update.message.reply_text(f"🗣️ <b>Transcripción:</b> <i>{_html.escape(transcript)}</i>", parse_mode="HTML")
    await update.message.reply_text("🤖 Analizando con los agentes...")

    try:
        history  = context.user_data.get("history", [])
        rag_ctx  = rag.build_context(transcript)
        system   = SYSTEM_PROMPT + rag_ctx
        reply = claude_response(system, transcript, max_tokens=600,
                                model=get_model(context), history=history)
        push_history(context, transcript, reply)
        context.user_data["last_analysis"] = reply

        try:
            await update.message.reply_text(
                fmt(reply) + "\n\n🎨 <i>¿Generar un artefacto visual con esto?</i>",
                reply_markup=ARTIFACT_KEYBOARD,
                parse_mode="HTML"
            )
        except Exception:
            # Fallback sin Markdown si la respuesta tiene caracteres problemáticos
            await update.message.reply_text(
                reply + "\n\n🎨 ¿Generar un artefacto visual con esto?",
                reply_markup=ARTIFACT_KEYBOARD
            )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error al procesar: {str(e)[:200]}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    filename = (doc.file_name or "").lower()
    ext = next((e for e in SUPPORTED_DOCS if filename.endswith(e)), None)

    if not ext:
        await update.message.reply_text(
            "📎 Solo proceso *PDF*, *Word (.docx)* y *Excel (.xlsx)*.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("📂 Leyendo archivo...")

    file = await context.bot.get_file(doc.file_id)
    file_bytes = bytes(await file.download_as_bytearray())

    try:
        if ext == ".pdf":
            content = extract_pdf(file_bytes)
            tipo = "PDF"
        elif ext == ".docx":
            content = extract_docx(file_bytes)
            tipo = "Word"
        else:
            content = extract_xlsx(file_bytes)
            tipo = "Excel"
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer el archivo: {str(e)[:200]}")
        return

    if not content.strip():
        await update.message.reply_text("⚠️ El archivo no tiene contenido extraíble.")
        return

    await update.message.reply_text("🤖 Analizando con los agentes...")

    prompt = (
        f"Analiza este documento {tipo} en el contexto operacional forestal de Arauco. "
        f"Identifica datos clave, KPIs, procesos, problemas u oportunidades de mejora.\n\n"
        f"{content[:6000]}"
    )
    history  = context.user_data.get("history", [])
    analysis = claude_response(SYSTEM_PROMPT, prompt, max_tokens=800, model=get_model(context), history=history)
    push_history(context, prompt, analysis)
    context.user_data["last_analysis"] = analysis
    # Guarda contenido completo para indexar si el usuario lo solicita
    context.user_data["pending_index"] = {"filename": doc.file_name, "content": content}

    try:
        await update.message.reply_text(
            f"📄 <b>{doc.file_name}</b>\n\n{fmt(analysis)}\n\n"
            "🎨 <i>¿Qué deseas hacer con este documento?</i>",
            reply_markup=DOC_KEYBOARD,
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text(
            f"📄 {doc.file_name}\n\n{analysis}\n\n¿Qué deseas hacer?",
            reply_markup=DOC_KEYBOARD
        )


async def rag_index_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Indexa el último documento analizado en ChromaDB."""
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending_index")
    if not pending:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ No hay documento pendiente de indexar.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"⏳ Indexando *{pending['filename']}*...\n"
        "_Documentos grandes pueden tardar varios minutos._",
        parse_mode="Markdown"
    )

    try:
        n = rag.index_document(pending["content"], pending["filename"])
        context.user_data.pop("pending_index", None)
        await query.message.reply_text(
            f"✅ *{pending['filename']}* indexado correctamente.\n"
            f"_{n} fragmentos almacenados en la base de conocimiento._\n\n"
            "Ahora puedo usar este documento para responder preguntas.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.message.reply_text(f"⚠️ Error al indexar: {str(e)[:200]}")


async def documentos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista todos los documentos indexados en RAG."""
    try:
        docs   = rag.list_documents()
        total  = rag.col.count()
        if not docs:
            await update.message.reply_text(
                "📚 La base de conocimiento está vacía.\n"
                "Sube un PDF, Word o Excel y presiona <b>📚 Indexar en RAG</b>.",
                parse_mode="HTML"
            )
            return
        lista = "\n".join(f"• {d}" for d in docs)
        await update.message.reply_text(
            f"📚 <b>Documentos indexados ({len(docs)}):</b>\n"
            f"<i>{total} fragmentos totales en la base de conocimiento</i>\n\n{lista}",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)[:200]}")


async def buscar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prueba la búsqueda RAG directamente sin pasar por Claude."""
    query_text = " ".join(context.args) if context.args else ""
    if not query_text:
        await update.message.reply_text(
            "Uso: <code>/buscar texto a buscar</code>\n"
            "Ejemplo: <code>/buscar rangos de pendiente terreno</code>",
            parse_mode="HTML"
        )
        return
    try:
        chunks = rag.query(query_text)
        if not chunks:
            await update.message.reply_text(
                f"🔍 Sin resultados para: <i>{_html.escape(query_text)}</i>\n\n"
                "Verifica con /documentos que el documento esté indexado.",
                parse_mode="HTML"
            )
            return
        resp = f"🔍 <b>Resultados para:</b> <i>{_html.escape(query_text)}</i>\n\n"
        for i, c in enumerate(chunks, 1):
            resp += (f"<b>{i}. {_html.escape(c['filename'])}</b> "
                     f"(relevancia: {c['score']})\n"
                     f"<i>{_html.escape(c['text'][:300])}...</i>\n\n")
        await update.message.reply_text(resp, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)[:200]}")


async def indexar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Instrucción para indexar documentos."""
    await update.message.reply_text(
        "📚 *Indexar documento*\n\n"
        "Envía un archivo *PDF*, *Word (.docx)* o *Excel (.xlsx)* "
        "y al finalizar el análisis presiona el botón *📚 Indexar en RAG*.\n\n"
        "El documento quedará disponible para que los agentes lo consulten "
        "automáticamente al responder preguntas.",
        parse_mode="Markdown"
    )


from telegram import BotCommand

START_TEXT = """🌲 *Arauco — Subgerencia de Mejora Continua*

Soy el asistente digital de tu equipo. Integro tres agentes especializados coordinados por el Subgerente de Mejora Continua:

🏭 *Agente EO — Excelencia Operacional*
Lean, GEMBA, KAIZEN, BPMN 2.0, KPIs, OEE, A3/PDCA, rediseño de procesos forestales

🤖 *Agente IA — Inteligencia Artificial*
Modelos predictivos (ML/XGBoost), GenAI con Claude API, LangGraph, cartografía con IA, dashboards HTML

⚙️ *Agente TD — Transformación Digital*
Integraciones SAP/SGL/Planex/Forest Data, telemetría de maquinaria forestal (Tigercat, John Deere, Develon), ETL, arquitecturas de datos

🧭 *Orquestador (Subgerente MC)*
Coordina los agentes, sintetiza resultados y entrega análisis ejecutivos estilo McKinsey/BCG

---

*Comandos disponibles:*
/spec — Especificación de iniciativa
/plan — Plan de ejecución
/build — Construcción de solución
/test — Validación operacional
/review — Revisión crítica
/ship — Lanzamiento a operación
/artifact — Genera Excel, gráfico o dashboard HTML

También puedes enviar una imagen, PDF, Word o Excel y los agentes lo analizarán."""

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, parse_mode="Markdown")


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Borra el historial conversacional del usuario."""
    context.user_data.pop("history", None)
    context.user_data.pop("last_analysis", None)
    await update.message.reply_text(
        "🔄 *Conversación reiniciada.* El contexto anterior fue borrado.\n"
        "Puedes empezar una nueva consulta desde cero.",
        parse_mode="Markdown"
    )


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start",       "🌲 Qué soy y cómo funciono"),
        BotCommand("reset",       "🔄 Reiniciar conversación"),
        BotCommand("modelo",      "🤖 Seleccionar modelo LLM"),
        BotCommand("indexar",     "📚 Cómo indexar documentos en RAG"),
        BotCommand("documentos",  "📂 Ver documentos indexados"),
        BotCommand("spec",        "📋 Especificación de iniciativa forestal"),
        BotCommand("plan",        "🗺️ Plan de ejecución forestal"),
        BotCommand("build",       "🔨 Construcción de solución forestal"),
        BotCommand("test",        "🧪 Validación en operación forestal"),
        BotCommand("review",      "🔍 Revisión crítica forestal"),
        BotCommand("ship",        "🚀 Lanzamiento a operación forestal"),
        BotCommand("artifact",    "🎨 Genera HTML, Excel o gráfico PNG"),
    ])

app = (
    ApplicationBuilder()
    .token(os.environ["TELEGRAM_TOKEN"])
    .post_init(post_init)
    .build()
)

app.add_handler(CommandHandler("start",      start_handler))
app.add_handler(CommandHandler("reset",      reset_handler))
app.add_handler(CommandHandler("modelo",     modelo_handler))
app.add_handler(CommandHandler("indexar",    indexar_handler))
app.add_handler(CommandHandler("documentos", documentos_handler))
app.add_handler(CommandHandler("buscar",     buscar_handler))
app.add_handler(CallbackQueryHandler(modelo_callback,    pattern="^mdl_"))
app.add_handler(CallbackQueryHandler(rag_index_callback, pattern="^rag_index$"))

for skill in SKILL_PROMPTS:
    app.add_handler(CommandHandler(skill, skill_handler))

app.add_handler(CommandHandler("artifact", artifact_handler))
app.add_handler(CallbackQueryHandler(artifact_callback, pattern="^art_"))
app.add_handler(MessageHandler(filters.VOICE, handle_audio))
app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))

app.run_polling(drop_pending_updates=True)

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
    """Convierte markdown a HTML para Telegram (parse_mode=HTML)."""

    # ── 0. Pre-procesador: normaliza formatos mixtos que Claude genera ────────
    #
    #  Claude tiende a generar:
    #    | Col1 | Col2 |        ← header en pipes
    #    |------|------|        ← separador
    #    ==Clave1==  Valor1    ← datos como ==key== valor
    #    ==Clave2==  Valor2
    #
    #  Este bloque lo detecta y lo convierte TODO a pipe-table para que
    #  save_table lo procese uniformemente.
    def normalize_mixed_table(t: str) -> str:
        EQ_PAT  = re.compile(r'^\s*={2,3}([^=\n]+)={2,3}\s*(.*?)\s*$')
        PIPE_HDR = re.compile(r'^\s*\|.+\|')
        PIPE_SEP = re.compile(r'^[\s|:=\-─+]+$')

        lines  = t.splitlines(keepends=True)
        out    = []
        i      = 0
        while i < len(lines):
            line = lines[i].rstrip('\n')

            # Detecta header pipe (1-2 líneas) seguido de ==key== pairs
            if PIPE_HDR.match(line):
                # Recoge header y separador opcional
                hdr_lines = [line]
                j = i + 1
                if j < len(lines) and PIPE_SEP.match(lines[j].rstrip('\n')):
                    j += 1          # salta el separador |---|

                # ¿Hay al menos 1 línea ==key== justo después?
                if j < len(lines) and EQ_PAT.match(lines[j].rstrip('\n')):
                    # Recoge los pares ==key== val
                    pairs = []
                    while j < len(lines):
                        m_eq = EQ_PAT.match(lines[j].rstrip('\n'))
                        if m_eq:
                            pairs.append((m_eq.group(1).strip(),
                                          m_eq.group(2).strip() or '—'))
                            j += 1
                        else:
                            break

                    # Extrae nombres de columnas del header pipe
                    hdr_cells = [c.strip() for c in
                                 hdr_lines[0].strip('| \t').split('|')]
                    col0 = hdr_cells[0] if len(hdr_cells) > 0 else 'Elemento'
                    col1 = hdr_cells[1] if len(hdr_cells) > 1 else 'Descripción'

                    # Emite pipe-table completa
                    out.append(f'| {col0} | {col1} |\n')
                    out.append(f'|---|---|\n')
                    for k, v in pairs:
                        out.append(f'| {k} | {v} |\n')
                    i = j
                    continue

            # Bloque de ==key== pairs sin header previo (≥2 pares)
            if EQ_PAT.match(line):
                pairs = []
                j = i
                while j < len(lines):
                    m_eq = EQ_PAT.match(lines[j].rstrip('\n'))
                    if m_eq:
                        pairs.append((m_eq.group(1).strip(),
                                      m_eq.group(2).strip() or '—'))
                        j += 1
                    else:
                        break
                if len(pairs) >= 2:
                    out.append('| Elemento | Descripción |\n')
                    out.append('|---|---|\n')
                    for k, v in pairs:
                        out.append(f'| {k} | {v} |\n')
                    i = j
                    continue
                # Solo 1 par → no table, lo procesa normal

            out.append(lines[i])
            i += 1
        return ''.join(out)

    text = normalize_mixed_table(text)

    # ── 1. Extraer code blocks, inline code y tablas ANTES de escapar HTML ────
    code_blocks, inline_codes, tables = [], [], []

    def save_block(m):
        code_blocks.append(m.group(1).strip())
        return f"\x00BLK{len(code_blocks)-1}\x00"

    def save_inline(m):
        inline_codes.append(m.group(1))
        return f"\x00INL{len(inline_codes)-1}\x00"

    def _rows_to_output(raw_rows: list) -> str:
        """
        Convierte filas a la mejor representación para Telegram.
        - Tablas angostas (≤ 40 chars): <pre> monoespaciado alineado
        - Tablas anchas o de 3+ cols: lista con negrita (sin fondo gris)
        """
        if not raw_rows:
            return ''

        # Limpia marcadores markdown de cada celda (** __ * _)
        def strip_md(s: str) -> str:
            s = re.sub(r'\*{2,3}([^*]*)\*{2,3}', r'\1', s)
            s = re.sub(r'\*([^*\n]+)\*',           r'\1', s)
            s = re.sub(r'__([^_]*)__',              r'\1', s)
            s = re.sub(r'_([^_\n]+)_',              r'\1', s)
            return s.strip()

        def vlen(s: str) -> int:   # ancho visual (emojis CJK = 2)
            return sum(2 if ord(c) > 0x2E80 else 1 for c in s)

        n_cols = max(len(r) for r in raw_rows)
        rows   = [[strip_md(c) for c in r] + [''] * (n_cols - len(r))
                  for r in raw_rows]
        widths = [max(vlen(row[c]) for row in rows) for c in range(n_cols)]
        total_w = sum(widths) + 2 * (n_cols - 1)

        # ── Tabla ancha o ≥ 3 columnas → lista formateada (sin fondo gris) ──
        MOBILE_MAX = 36   # ~chars visibles en móvil dentro de <pre>
        if total_w > MOBILE_MAX or n_cols >= 3:
            header = rows[0]
            data   = rows[1:]
            if not data:        # solo header → vuelve a <pre> simple
                data = [header]
                header = []

            parts = []
            for row in data:
                if n_cols == 2:
                    key = _html.escape(row[0])
                    val = _html.escape(row[1])
                    parts.append(f'▸ <b>{key}</b>  {val}')
                elif n_cols >= 3:
                    key  = _html.escape(row[0])
                    val1 = _html.escape(row[1])
                    val2 = _html.escape(row[2]) if len(row) > 2 else ''
                    line = f'▸ <b>{key}</b> — {val1}'
                    if val2:
                        line += f'\n   <i>{val2}</i>'
                    parts.append(line)
            return '\n'.join(parts)

        # ── Tabla angosta → <pre> alineado ───────────────────────────────────
        lines = []
        for i, row in enumerate(rows):
            parts = [cell + ' ' * (widths[c] - vlen(cell))
                     for c, cell in enumerate(row)]
            lines.append('  '.join(parts).rstrip())
            if i == 0:
                lines.append('─' * total_w)
        content = chr(10).join(lines).replace('&', '&amp;').replace('<', '&lt;')
        return f'<pre>{content}</pre>'

    def save_table(m):
        raw_rows = []
        for line in m.group(0).strip().splitlines():
            line = line.strip()
            if re.match(r'^[|\s:=+–\-─]+$', line):
                continue
            cells = [c.strip() for c in line.strip('|').split('|')]
            if any(c for c in cells if c):
                raw_rows.append(cells)
        result = _rows_to_output(raw_rows)
        if not result:
            return ''
        tables.append(result)
        return f"\x00TBL{len(tables)-1}\x00"

    text = re.sub(r'```[a-z]*\n?(.*?)```', save_block,  text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+)`',          save_inline, text)
    text = re.sub(r'(\|[^\n]+\n?){2,}',    save_table,  text)

    # ── 2. Escapar HTML ───────────────────────────────────────────────────────
    text = _html.escape(text)

    # ── 3. Formatos inventados por Claude → HTML ──────────────────────────────
    text = re.sub(r'={2,3}([^=\n]+)={2,3}',
                  lambda m: f'<b>{m.group(1).strip()}</b>', text)
    text = re.sub(r'__([^_\n]+)__', r'<b>\1</b>', text)
    text = re.sub(r'(?<![_\w])_([^_\n]+)_(?![_\w])', r'<i>\1</i>', text)

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
- **IMPORTANTE — formato de texto:** El sistema convierte markdown a HTML. Reglas por caso:

  **Encabezados:** usa `##` o `###` (nunca `#` solo ni `==texto==`)
  **Negrita:** usa `**texto**`
  **Listas:** guiones `-`

  **TABLAS — DOS CASOS, NO MEZCLES:**

  CASO A — Celdas cortas (< 35 chars): usa SIEMPRE tabla markdown con pipes.
  Ejemplo real Arauco:

| Fase          | Sistema       | Estado    |
|---------------|---------------|-----------|
| Planificación | Forest NOM    | ✓ Activo  |
| Ejecución     | SGL básico    | Parcial   |
| Cierre        | SAP PM        | Pendiente |

  CASO B — Contenido largo por ítem: usa secciones con negrita, NO tabla.
  Ejemplo:

**1. Planificación** — Forest NOM
Define tiempos de volteo, movimiento y TSP de máquinas, equipos asignados...

**2. Ejecución** — SGL básico
Registra horas ON/OFF reales, eventos y desviaciones por parcial...

  **PROHIBIDO:** ==texto==, asterisco simple `*texto*` como encabezado, mezclar header de tabla con prosa debajo, o usar espacios/guiones para alinear columnas manualmente.
  - Bloques de código: triple backtick ```
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
], [
    InlineKeyboardButton("📄 PDF",     callback_data="art_pdf"),
    InlineKeyboardButton("📅 Gantt",   callback_data="art_gantt"),
]])

DOC_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("📊 Excel",          callback_data="art_excel"),
    InlineKeyboardButton("📈 Gráfico",        callback_data="art_chart"),
    InlineKeyboardButton("🌐 HTML",           callback_data="art_html"),
], [
    InlineKeyboardButton("📄 PDF",            callback_data="art_pdf"),
    InlineKeyboardButton("📅 Gantt",          callback_data="art_gantt"),
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


CODE_BLOCK_RE = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)
EXT_MAP = {"python": "py", "py": "py", "sql": "sql", "bash": "sh", "sh": "sh", "r": "r", "javascript": "js", "js": "js"}

async def send_reply(update: Update, text: str, reply_markup=None):
    """
    Envía la respuesta de Claude a Telegram.
    Si la respuesta contiene un bloque de código largo (>10 líneas), lo extrae
    y lo envía como archivo adjunto en lugar de mostrarlo inline.
    """
    # Detectar bloques de código en la respuesta
    code_blocks = CODE_BLOCK_RE.findall(text)
    large_blocks = [(lang, code) for lang, code in code_blocks if code.count('\n') >= 10]

    if not large_blocks:
        # Sin código largo — envío normal
        try:
            await update.message.reply_text(
                fmt(text) + "\n\n🎨 <i>¿Generar un artefacto visual con esto?</i>",
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text(
                text + "\n\n🎨 ¿Generar un artefacto visual con esto?",
                reply_markup=reply_markup
            )
        return

    # Hay bloques de código largos — separar texto del código
    clean_text = CODE_BLOCK_RE.sub("", text).strip()

    # Enviar primero el texto explicativo (si hay)
    if clean_text:
        try:
            await update.message.reply_text(
                fmt(clean_text) + "\n\n🎨 <i>¿Generar un artefacto visual con esto?</i>",
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text(clean_text, reply_markup=reply_markup)

    # Enviar cada bloque de código como archivo
    for i, (lang, code) in enumerate(large_blocks, 1):
        ext = EXT_MAP.get(lang.lower(), "txt")
        filename = f"script_{i}.{ext}" if len(large_blocks) > 1 else f"script.{ext}"
        buf = io.BytesIO(code.strip().encode("utf-8"))
        caption = f"📎 {filename} — copia y ejecuta en tu entorno"
        await update.message.reply_document(document=buf, filename=filename, caption=caption)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    try:
        history    = context.user_data.get("history", [])
        rag_ctx    = rag.build_context(user_msg)
        system     = SYSTEM_PROMPT + rag_ctx
        reply = claude_response(system, user_msg, model=get_model(context), history=history)
        push_history(context, user_msg, reply)
        context.user_data["last_analysis"] = reply
        await send_reply(update, reply, reply_markup=ARTIFACT_KEYBOARD)
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

    await send_reply(update, analysis, reply_markup=ARTIFACT_KEYBOARD)


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

    # Construye el bloque de datos para el artefacto HTML/Excel/Chart
    # Funciona para CUALQUIER tipo de documento: PDF, Word o Excel
    structured_data = context.user_data.get("structured_data", {})
    doc_content     = context.user_data.get("doc_content", "")
    doc_tipo        = context.user_data.get("doc_tipo", "")

    if artifact_type == "html":
        if structured_data:
            # Excel → datos tabulares exactos disponibles
            import json as _json
            data_block = _json.dumps(structured_data, ensure_ascii=False, default=str)
            description = (
                f"Análisis del documento:\n\n{last_analysis}\n\n"
                f"DATOS EXACTOS DEL ARCHIVO {doc_tipo} "
                f"(úsalos LITERALMENTE en tablas y gráficos — no inventes valores):\n"
                f"{data_block[:8000]}"
            )
        elif doc_content:
            # PDF / Word → pasa el contenido raw para que Claude extraiga tablas
            description = (
                f"Análisis del documento:\n\n{last_analysis}\n\n"
                f"CONTENIDO COMPLETO DEL ARCHIVO {doc_tipo} "
                f"(extrae de aquí los datos para tablas y gráficos):\n"
                f"{doc_content[:8000]}"
            )
        else:
            description = f"Basado en este análisis forestal de Arauco:\n\n{last_analysis}"
    else:
        description = f"Basado en este análisis forestal de Arauco:\n\n{last_analysis}"

    artifact_model  = "claude-sonnet-4-6"
    artifact_tokens = 16000 if artifact_type == "html" else 2000
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
        elif artifact_type in ("pdf", "gantt"):
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
            if artifact_type == "pdf":
                await query.message.reply_document(
                    document=buf, filename="informe-arauco.html",
                    caption="📄 Informe listo — abre en browser y usa Archivo → Imprimir → Guardar como PDF"
                )
            else:
                await query.message.reply_document(
                    document=buf, filename="gantt-arauco.html",
                    caption="📅 Gantt listo — abre el archivo en tu browser"
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
    "html": """Eres el Agente DA (Analista de Datos) de Arauco — Subgerencia de Mejora Continua.
Genera un dashboard HTML interactivo, completo y autocontenido basado en los datos recibidos.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CSS BASE OBLIGATORIO — incluye esto en <style>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Lato', -apple-system, sans-serif; letter-spacing: -0.3px;
       background: #f5f5f5; color: #333; }
.dashboard-header { background: #696158; color: #fff; padding: 24px 32px;
                    display: flex; align-items: center; justify-content: space-between; }
.dashboard-title { font-size: 1.4rem; font-weight: 700; }
.dashboard-subtitle { font-size: 0.85rem; color: rgba(255,255,255,0.7); font-weight: 300; }
.dashboard { max-width: 1200px; margin: 0 auto; padding: 24px; }
.grid { display: grid; gap: 16px; }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.card { background: #fff; border-radius: 10px; padding: 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: 1px solid #eee; }
.kpi-value { font-size: 2rem; font-weight: 900; color: #696158; }
.kpi-label { font-size: 0.75rem; color: #999; text-transform: uppercase;
             letter-spacing: 0.05em; margin-bottom: 4px; }
.kpi-change { font-size: 0.85rem; margin-top: 6px; font-weight: 700; }
.kpi-change.positive { color: #BFB800; }
.kpi-change.negative { color: #C00000; }
.kpi-change.neutral  { color: #999; }
.section-title { font-size: 1rem; font-weight: 700; color: #696158;
                 margin: 24px 0 12px; border-left: 4px solid #BFB800; padding-left: 10px; }
.filtros-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               background: #fff; padding: 14px 20px; border-radius: 10px;
               box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 16px; }
.filtros-bar select { padding: 7px 12px; border: 1px solid #DFD1A7; border-radius: 6px;
                      font-family: 'Lato', sans-serif; font-size: 0.85rem; color: #696158;
                      background: #fafafa; cursor: pointer; }
.filtros-bar select:focus { outline: none; border-color: #696158; }
.btn-limpiar { padding: 7px 14px; background: #EA7600; color: #fff; border: none;
               border-radius: 6px; font-family: 'Lato', sans-serif; font-size: 0.85rem;
               cursor: pointer; font-weight: 700; }
.btn-limpiar:hover { background: #c96300; }
.conteo-badge { font-size: 0.8rem; color: #999; margin-left: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
thead tr { background: #696158; color: #fff; }
th { padding: 10px 12px; text-align: left; font-weight: 700;
     text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.04em; }
td { padding: 8px 12px; border-bottom: 1px solid #eee; }
tbody tr:nth-child(even) { background: #EDEAE6; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
         font-size: 0.75rem; font-weight: 700; }
.badge-ok  { background: #BFB800; color: #fff; }
.badge-alerta { background: #EA7600; color: #fff; }
.badge-null   { background: #ccc; color: #555; }
.dashboard-footer { text-align: center; padding: 24px; font-size: 0.75rem;
                    color: #999; border-top: 1px solid #eee; margin-top: 32px; }
@media (max-width: 768px) {
  .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TECNOLOGÍAS DE VISUALIZACIÓN — elige la más adecuada
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Chart.js  → barras, líneas, dona, radar, scatter (siempre incluir)
D3.js     → treemaps, sankeys, mapas, force graphs (incluir solo si aplica)
SVG puro  → gauges, semáforos, diagramas de flujo custom
HTML/CSS  → KPI cards, tablas, grids, indicadores de estado

CDN:
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<!-- D3 solo si necesitas visualizaciones complejas: -->
<!-- <script src="https://cdn.jsdelivr.net/npm/d3@7"></script> -->

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ESTRUCTURA DEL DASHBOARD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. <head> — CSS base + CDNs
2. Header — logo Arauco blanco + título + subtítulo con fuente y fecha
3. KPI cards — .grid.grid-4, mínimo 4, con .kpi-label / .kpi-value / .kpi-change
4. Filtros — .filtros-bar con <select> por columna categórica + botón limpiar + conteo
5. Gráficos — mínimo 2 canvas Chart.js en .grid.grid-2 dentro de .card
6. <script> con TODA la lógica JS ← AQUÍ, ANTES de la tabla
7. Tabla filtrable — dentro de .card con overflow-x:auto
8. Footer

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JS OBLIGATORIO — estructura exacta, completa con datos reales
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
const COLORS = {
  grisTierra:'#696158', verdeOliva:'#BFB800', naranja:'#EA7600',
  crema:'#DFD1A7', blanco:'#FFFFFF', rojo:'#C00000'
};

// Filas de datos — objetos con claves = nombres exactos de columna
const DATOS = [ /* { "COL1": val, "COL2": val, ... } — poblar con muestra_top20 real */ ];

// Columnas categóricas con filtro (nombres exactos de columna)
const FILTROS_COLS = [ /* "COL_A", "COL_B" */ ];

// Referencias a Chart instances (para poder actualizarlos)
const charts = {};

function aplicarFiltros() {
  const vals = {};
  FILTROS_COLS.forEach(col => {
    const el = document.getElementById('f-' + col);
    if (el) vals[col] = el.value;
  });
  const filtrados = DATOS.filter(row =>
    FILTROS_COLS.every(col => !vals[col] || String(row[col]) === vals[col])
  );
  document.getElementById('conteo').textContent = filtrados.length + ' registros';
  renderTabla(filtrados);
  actualizarGraficos(filtrados);
}

function renderTabla(filas) {
  document.getElementById('tabla-body').innerHTML = filas.slice(0, 50).map((row, i) => {
    /* genera <td> con los campos relevantes — aplica .badge según valor */
    return `<tr>${ Object.values(row).map(v => `<td>${v ?? ''}</td>`).join('') }</tr>`;
  }).join('');
}

function actualizarGraficos(filas) {
  /* Para cada chart: recalcula labels/values desde filas, luego chart.update() */
  /* Ejemplo barras:
  const cnt = {};
  filas.forEach(r => { const v = String(r['COL_A'] ?? '-'); cnt[v] = (cnt[v]||0)+1; });
  charts.barras.data.labels = Object.keys(cnt).slice(0,10);
  charts.barras.data.datasets[0].data = Object.values(cnt).slice(0,10);
  charts.barras.update();
  */
}

function limpiarFiltros() {
  FILTROS_COLS.forEach(col => { const el = document.getElementById('f-'+col); if(el) el.value=''; });
  aplicarFiltros();
}

window.addEventListener('DOMContentLoaded', () => {
  /* Crear cada Chart instance y asignarlo a charts.nombre */
  /* charts.barras = new Chart(document.getElementById('canvas-barras'), { type:'bar', ... }); */
  aplicarFiltros();  // render inicial
});

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGLAS DE DATOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXCEL → usa stats[col].frecuencias para gráficos y opciones de filtro; muestra_top20 para DATOS[]
PDF   → extrae tablas y cifras del texto; cita [Página N] como fuente
WORD  → extrae [Tabla N] para DATOS[]; usa ## secciones como secciones del dashboard
TODOS → nunca inventes cifras; formato chileno 1.234,5; badge-null para valores vacíos/nulos

Responde ÚNICAMENTE con el código HTML. Sin markdown, sin explicaciones. Empieza con <!DOCTYPE html>.""",

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

    "pdf": """Eres el Agente DA de Arauco. Genera un informe HTML optimizado para imprimir como PDF.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CSS OBLIGATORIO PARA IMPRESIÓN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Lato', Arial, sans-serif; font-size: 11pt; color: #222;
       background: #fff; padding: 0; margin: 0; }
.page { width: 210mm; min-height: 297mm; padding: 18mm 20mm; margin: 0 auto;
        background: #fff; }
@media print {
  .no-print { display: none !important; }
  .page { padding: 12mm 16mm; box-shadow: none; }
  table { page-break-inside: avoid; }
  h2, h3 { page-break-after: avoid; }
}
@media screen {
  body { background: #e5e5e5; }
  .page { box-shadow: 0 2px 16px rgba(0,0,0,0.15); margin: 24px auto; }
}
.header-pdf { display: flex; align-items: center; justify-content: space-between;
              border-bottom: 3px solid #696158; padding-bottom: 12px; margin-bottom: 20px; }
.header-pdf .titulo-doc { font-size: 16pt; font-weight: 900; color: #696158; }
.header-pdf .meta { font-size: 8pt; color: #999; text-align: right; line-height: 1.6; }
h2 { font-size: 12pt; font-weight: 700; color: #696158; margin: 20px 0 8px;
     border-left: 4px solid #BFB800; padding-left: 8px; }
h3 { font-size: 10pt; font-weight: 700; color: #333; margin: 14px 0 6px; }
p  { font-size: 10pt; line-height: 1.6; margin-bottom: 8px; color: #333; }
.kpi-row { display: flex; gap: 12px; margin: 12px 0; flex-wrap: wrap; }
.kpi-box { flex: 1; min-width: 110px; border: 1px solid #DFD1A7; border-radius: 6px;
           padding: 10px 14px; background: #fafafa; }
.kpi-box .val { font-size: 18pt; font-weight: 900; color: #696158; }
.kpi-box .lbl { font-size: 7pt; color: #999; text-transform: uppercase; letter-spacing: 0.05em; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 10px 0; }
thead tr { background: #696158; color: #fff; }
th { padding: 7px 10px; text-align: left; font-weight: 700; font-size: 8pt;
     text-transform: uppercase; letter-spacing: 0.04em; }
td { padding: 6px 10px; border-bottom: 1px solid #eee; }
tbody tr:nth-child(even) { background: #EDEAE6; }
.footer-pdf { margin-top: 24px; padding-top: 10px; border-top: 1px solid #DFD1A7;
              font-size: 7pt; color: #aaa; display: flex; justify-content: space-between; }
.btn-print { display: block; margin: 16px auto; padding: 10px 28px;
             background: #696158; color: #fff; border: none; border-radius: 6px;
             font-family: 'Lato', sans-serif; font-size: 11pt; cursor: pointer;
             font-weight: 700; }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ESTRUCTURA DEL INFORME
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Botón "Imprimir / Guardar PDF" (.no-print) arriba del .page
2. Dentro de .page:
   a. Header — logo Arauco + título del informe + meta (área, fecha, autor)
   b. Resumen ejecutivo — 2-3 párrafos con los hallazgos principales
   c. KPI row — 3-5 métricas clave con .kpi-box
   d. Secciones con <h2> para cada tema del análisis
   e. Tablas de datos (sin filtros, estilo limpio para impresión)
   f. Conclusiones y recomendaciones
   g. Footer — área, sistema de origen, fecha de generación

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGLAS DE DATOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXCEL → usa stats para KPIs y totales; muestra_top20 para tablas
PDF/WORD → extrae secciones, cifras y tablas del texto recibido
TODOS → nunca inventes cifras; formato chileno 1.234,5

Responde ÚNICAMENTE con el código HTML. Sin markdown. Empieza con <!DOCTYPE html>.""",

    "gantt": """Eres el Agente DA de Arauco. Genera un diagrama de Gantt interactivo en HTML.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CDN OBLIGATORIO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/frappe-gantt/dist/frappe-gantt.min.css">
<script src="https://cdn.jsdelivr.net/npm/frappe-gantt/dist/frappe-gantt.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap" rel="stylesheet">

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ESTRUCTURA OBLIGATORIA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Header Arauco — fondo #696158, logo blanco, título del proyecto
2. Barra de controles:
   - Selector de vista: Día / Semana / Mes / Año (<button> con onclick)
   - Indicador de % avance total del proyecto
3. Contenedor del Gantt: <div id="gantt"></div>
4. Panel de detalle de tarea (aparece al hacer clic): nombre, responsable, fechas, % avance, dependencias
5. Leyenda de colores por área/responsable
6. Footer Arauco

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JS OBLIGATORIO — estructura exacta
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Paleta Arauco
const COLORS = {
  grisTierra:'#696158', verdeOliva:'#BFB800', naranja:'#EA7600',
  crema:'#DFD1A7', rojo:'#C00000', azul:'#2D6A9F'
};

// Tareas — extrae del análisis/documento recibido o crea las más relevantes
const tareas = [
  {
    id: 'tarea-1',
    name: 'Nombre de la tarea',
    start: 'YYYY-MM-DD',
    end: 'YYYY-MM-DD',
    progress: 0-100,          // % de avance
    dependencies: '',         // 'tarea-0' o '' si no tiene
    custom_class: 'bar-verde' // bar-verde | bar-naranja | bar-gris | bar-rojo
  },
  // ... más tareas
];

// CSS para colores de barras
// .bar-verde .bar-progress { fill: #BFB800 }
// .bar-naranja .bar-progress { fill: #EA7600 }
// .bar-gris .bar-progress { fill: #696158 }
// .bar-rojo .bar-progress { fill: #C00000 }

// Inicializar Gantt
const gantt = new Gantt('#gantt', tareas, {
  view_mode: 'Week',          // Day | Week | Month | Year
  date_format: 'YYYY-MM-DD',
  language: 'es',
  on_click: (task) => mostrarDetalle(task),
  on_progress_change: (task, progress) => {},
  popup_trigger: 'click',
});

// Cambiar vista
function setVista(modo) {
  gantt.change_view_mode(modo);
  document.querySelectorAll('.btn-vista').forEach(b => b.classList.remove('active'));
  document.querySelector(`[data-modo="${modo}"]`).classList.add('active');
}

// Mostrar panel de detalle al hacer clic en una tarea
function mostrarDetalle(task) {
  document.getElementById('detalle-nombre').textContent = task.name;
  document.getElementById('detalle-inicio').textContent = task.start;
  document.getElementById('detalle-fin').textContent = task.end;
  document.getElementById('detalle-avance').textContent = task.progress + '%';
  document.getElementById('detalle-deps').textContent = task.dependencies || 'Ninguna';
  document.getElementById('panel-detalle').style.display = 'block';
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGLAS DE DATOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Si el análisis/documento tiene fechas y tareas: extraer y usar directamente
- Si no hay fechas reales: crear un proyecto forestal típico coherente con el contexto
- Mínimo 5 tareas, máximo 25
- Asignar colores por área: EO=verde, TD=naranja, IA=azul, gestión=gris, riesgo=rojo
- Formato de fecha obligatorio: YYYY-MM-DD

Responde ÚNICAMENTE con el código HTML. Sin markdown. Empieza con <!DOCTYPE html>.""",
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
            f"Tipo `{artifact_type}` no reconocido. Usa: `html`, `excel`, `chart`, `pdf` o `gantt`.",
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

    # Enriquecer description con datos del documento cargado (igual que artifact_callback)
    if artifact_type in ("html", "pdf", "gantt"):
        structured_data = context.user_data.get("structured_data", {})
        doc_content     = context.user_data.get("doc_content", "")
        doc_tipo        = context.user_data.get("doc_tipo", "")
        last_analysis   = context.user_data.get("last_analysis", "")

        if structured_data:
            data_block = json.dumps(structured_data, ensure_ascii=False, default=str)
            description = (
                f"{description}\n\n"
                f"DATOS EXACTOS DEL ARCHIVO {doc_tipo} "
                f"(úsalos LITERALMENTE en tablas y gráficos — no inventes valores):\n"
                f"{data_block[:8000]}"
            )
        elif doc_content:
            description = (
                f"{description}\n\n"
                f"CONTENIDO COMPLETO DEL ARCHIVO {doc_tipo} "
                f"(extrae de aquí los datos para tablas y gráficos):\n"
                f"{doc_content[:8000]}"
            )
        elif last_analysis:
            description = f"{description}\n\nContexto del análisis previo:\n{last_analysis}"

    artifact_model = "claude-sonnet-4-6"
    artifact_tokens = 16000 if artifact_type in ("html", "pdf", "gantt") else 2000
    raw = claude_response(ARTIFACT_PROMPTS[artifact_type], description,
                          max_tokens=artifact_tokens, model=artifact_model)

    try:
        if artifact_type in ("html", "pdf", "gantt"):
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
            filenames = {"html": "dashboard-arauco.html", "pdf": "informe-arauco.html", "gantt": "gantt-arauco.html"}
            captions  = {
                "html":  "🌲 Dashboard listo — abre el archivo en tu browser",
                "pdf":   "📄 Informe listo — abre en browser y usa Archivo → Imprimir → Guardar como PDF",
                "gantt": "📅 Gantt listo — abre el archivo en tu browser",
            }
            await update.message.reply_document(document=buf, filename=filenames[artifact_type],
                                                caption=captions[artifact_type])

        elif artifact_type == "excel":
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
    """Extrae texto de un PDF con metadata de estructura."""
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total_pages = len(pdf.pages)
        parts.append(f"--- DOCUMENTO PDF: {total_pages} páginas en total ---")
        for i, page in enumerate(pdf.pages, 1):
            page_parts = []
            text = page.extract_text()
            if text:
                page_parts.append(text.strip())
            for table in page.extract_tables():
                for row in table:
                    page_parts.append(" | ".join(str(c or "") for c in row))
            if page_parts:
                parts.append(f"\n[Página {i}/{total_pages}]")
                parts.extend(page_parts)
        if total_pages > len(pdf.pages):
            parts.append(f"\n... ({total_pages - len(pdf.pages)} páginas adicionales no incluidas)")
    full = "\n".join(parts)
    total_chars = len(full)
    parts.insert(1, f"--- Total caracteres extraídos: {total_chars} ---")
    return "\n".join(parts)


def extract_docx(data: bytes) -> str:
    """Extrae texto, estructura y tablas de un documento Word."""
    doc = DocxDocument(io.BytesIO(data))
    parts = []

    # Metadata de estructura
    headings = [p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading") and p.text.strip()]
    n_paras  = sum(1 for p in doc.paragraphs if p.text.strip())
    n_tables = len(doc.tables)
    parts.append(f"--- DOCUMENTO WORD: {n_paras} párrafos, {n_tables} tablas ---")
    if headings:
        parts.append("--- SECCIONES: " + " | ".join(headings[:20]) + " ---")

    # Contenido con marcadores de sección
    for para in doc.paragraphs:
        if not para.text.strip():
            continue
        if para.style.name.startswith("Heading"):
            parts.append(f"\n## {para.text.strip()}")
        else:
            parts.append(para.text.strip())

    # Tablas con header explícito
    for t_idx, table in enumerate(doc.tables, 1):
        parts.append(f"\n[Tabla {t_idx}]")
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))

    full = "\n".join(parts)
    parts.insert(2, f"--- Total caracteres extraídos: {len(full)} ---")
    return "\n".join(parts)


def extract_xlsx(data: bytes) -> tuple[str, dict]:
    """
    Extrae datos de un Excel.
    Retorna (resumen_texto, datos_estructurados) donde datos_estructurados
    incluye estadísticas y top-N filas listas para usar en HTML/tablas.
    """
    from collections import defaultdict, Counter

    wb  = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    txt = []
    structured = {}   # hoja → {headers, sample, stats, totals}

    for sheet_name in wb.sheetnames[:3]:
        ws = wb[sheet_name]
        headers_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        headers = [str(h) if h is not None else f"Col{i}" for i, h in enumerate(headers_row)]

        # Lee todas las filas (hasta 5.000 para estadísticas)
        all_rows = []
        for row in ws.iter_rows(min_row=2, max_row=min(ws.max_row, 5001), values_only=True):
            if any(c is not None for c in row):
                all_rows.append(list(row))

        total = len(all_rows)
        txt.append(f"=== Hoja: {sheet_name} ({total} filas, {len(headers)} columnas) ===")
        txt.append(" | ".join(headers))

        # Muestra primeras 25 filas como texto
        for row in all_rows[:25]:
            txt.append(" | ".join(str(c) if c is not None else "" for c in row))
        if total > 25:
            txt.append(f"... ({total - 25} filas adicionales)")

        # Resumen de estadísticas SOBRE TODAS LAS FILAS — incluido en el texto
        # para que Claude lo use en análisis y preguntas de seguimiento
        txt.append("\n--- ESTADÍSTICAS REALES (todas las filas) ---")

        # Estadísticas por columna categórica (máx. 20 valores únicos)
        stats = {}
        for col_i, col_name in enumerate(headers):
            vals = [row[col_i] for row in all_rows if col_i < len(row) and row[col_i] not in (None, "", "<Null>", "None")]
            if not vals:
                continue
            # Numérica
            nums = []
            for v in vals:
                try:
                    nums.append(float(str(v).replace(",", ".")))
                except (ValueError, TypeError):
                    pass
            if len(nums) > len(vals) * 0.5:
                stats[col_name] = {
                    "tipo": "num",
                    "total": len(nums),
                    "suma": round(sum(nums), 2),
                    "min": round(min(nums), 2),
                    "max": round(max(nums), 2),
                    "prom": round(sum(nums) / len(nums), 2),
                }
            elif len(set(str(v) for v in vals)) <= 20:
                cnt = Counter(str(v) for v in vals)
                stats[col_name] = {"tipo": "cat", "frecuencias": dict(cnt.most_common(15))}

        # Muestra representativa: top-20 por la columna numérica de mayor promedio
        # (excluye columnas tipo ID donde max ≈ total de filas)
        num_col = None
        best_score, best_i = -1, None
        for i, h in enumerate(headers):
            if h in stats and stats[h]["tipo"] == "num":
                s = stats[h]
                # Columnas tipo ID tienen prom ≈ total/2 y valores únicos = total
                # Columnas de métricas tienen mayor dispersión relativa
                if s["total"] > 0 and s["max"] > 0:
                    score = s["prom"] / max(s["max"], 1)   # < 1 siempre; IDs tienden a 0.5
                    # Preferir columnas donde la media es > 10% del máximo (métricas reales)
                    if score > best_score and s["max"] < 1e8:
                        best_score, best_i = score, i
        num_col = best_i
        if num_col is not None:
            try:
                top20 = sorted(all_rows, key=lambda r: float(str(r[num_col] or 0).replace(",", ".")), reverse=True)[:20]
            except Exception:
                top20 = all_rows[:20]
        else:
            top20 = all_rows[:20]

        # Vuelca estadísticas en el texto para que Claude las use en análisis
        for col_name, s in stats.items():
            if s["tipo"] == "num":
                txt.append(f"{col_name}: total={s['total']}, suma={s['suma']}, min={s['min']}, max={s['max']}, prom={s['prom']}")
            else:
                freq_str = ", ".join(f"{k}: {v}" for k, v in list(s["frecuencias"].items())[:10])
                txt.append(f"{col_name} ({sum(s['frecuencias'].values())} registros): {freq_str}")

        structured[sheet_name] = {
            "headers":  headers,
            "total_filas": total,
            "muestra_top20": [[str(c) if c is not None else "" for c in r] for r in top20],
            "stats":    stats,
        }

    return "\n".join(txt), structured


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
            structured_data = {}
            tipo = "PDF"
        elif ext == ".docx":
            content = extract_docx(file_bytes)
            structured_data = {}
            tipo = "Word"
        else:
            content, structured_data = extract_xlsx(file_bytes)
            tipo = "Excel"
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer el archivo: {str(e)[:200]}")
        return

    if not content.strip():
        await update.message.reply_text("⚠️ El archivo no tiene contenido extraíble.")
        return

    await update.message.reply_text("🤖 Analizando con los agentes...")

    # 10.000 chars para todos los tipos — Excel incluye stats, PDF/Word incluyen estructura
    prompt = (
        f"Analiza este documento {tipo} en el contexto operacional forestal de Arauco. "
        f"Identifica datos clave, KPIs, procesos, problemas u oportunidades de mejora.\n\n"
        f"{content[:10000]}"
    )
    history  = context.user_data.get("history", [])
    analysis = claude_response(SYSTEM_PROMPT, prompt, max_tokens=800, model=get_model(context), history=history)
    push_history(context, prompt, analysis)
    context.user_data["last_analysis"] = analysis
    # Guarda datos estructurados (Excel) y contenido raw (PDF/Word/Excel)
    # para que el artifact HTML pueda generar tablas con datos reales
    context.user_data["structured_data"] = structured_data   # dict (Excel) o {} (PDF/Word)
    context.user_data["doc_content"]     = content[:10000]   # texto raw del documento
    context.user_data["doc_tipo"]        = tipo               # "PDF" | "Word" | "Excel"
    # Guarda contenido completo para indexar si el usuario lo solicita
    context.user_data["pending_index"] = {"filename": doc.file_name, "content": content}

    header = f"📄 <b>{doc.file_name}</b>\n\n"
    try:
        await update.message.reply_text(
            header + fmt(analysis) + "\n\n🎨 <i>¿Qué deseas hacer con este documento?</i>",
            reply_markup=DOC_KEYBOARD,
            parse_mode="HTML"
        )
    except Exception:
        await send_reply(update, f"{doc.file_name}\n\n{analysis}", reply_markup=DOC_KEYBOARD)


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

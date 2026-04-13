import os
import io
import json
import base64
import tempfile
import anthropic
import openpyxl
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from telegram import Update
from telegram.ext import ContextTypes

# Lee todos los agentes desde el submódulo proyecto_claude
def load_prompt(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""

BASE_DIR = os.path.join(os.path.dirname(__file__), "proyecto_claude")

orquestador = load_prompt(os.path.join(BASE_DIR, "orquestador", "CLAUDE.md"))
agente_td    = load_prompt(os.path.join(BASE_DIR, "agentes", "TD", "CLAUDE.md"))
agente_ia    = load_prompt(os.path.join(BASE_DIR, "agentes", "IA", "CLAUDE.md"))
agente_eo    = load_prompt(os.path.join(BASE_DIR, "agentes", "EO", "CLAUDE.md"))

SYSTEM_PROMPT = f"""
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

---

## Estilo de comunicación en Telegram

- Responde en lenguaje natural y cercano, como un colega experto forestal
- Usa emojis para estructurar y dar vida a las respuestas 🌲🪵🚛🛠️📊
- Usa negritas y listas cuando ayuden a la claridad
- Si te envían una imagen, analízala en el contexto operacional forestal de Arauco
- Sé conciso pero completo — máximo 3-4 párrafos salvo que se pida detalle
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

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def claude_response(system: str, user_msg: str, max_tokens: int = 512) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}]
    )
    return response.content[0].text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = claude_response(SYSTEM_PROMPT, update.message.text)
    await update.message.reply_text(reply)


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    caption = update.message.caption or "Analiza esta imagen en el contexto operacional forestal de Arauco. Identifica equipos, procesos, problemas o métricas relevantes."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
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
    await update.message.reply_text(response.content[0].text)


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
    reply = claude_response(skill_system, args, max_tokens=800)
    await update.message.reply_text(reply)


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
    "html": """Genera un dashboard HTML completo y autocontenido (sin dependencias externas descargadas, usa CDN de Chart.js: https://cdn.jsdelivr.net/npm/chart.js).

El HTML debe:
- Tener un título claro relacionado al contexto forestal de Arauco
- Incluir al menos un gráfico Chart.js con datos de ejemplo realistas para el contexto pedido
- Usar colores corporativos verdes (#2d6a4f, #40916c, #74c69d) y fondo blanco
- Mostrar KPIs o métricas relevantes en tarjetas resumen sobre el gráfico
- Ser completamente funcional al abrir el archivo .html en un browser

Responde ÚNICAMENTE con el código HTML completo, sin explicaciones ni bloques de código markdown. Empieza directamente con <!DOCTYPE html>.""",

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

    system = SYSTEM_PROMPT + "\n\n" + ARTIFACT_PROMPTS[artifact_type]
    raw = claude_response(system, description, max_tokens=2000)

    try:
        if artifact_type == "html":
            buf = io.BytesIO(raw.encode("utf-8"))
            filename = f"dashboard-arauco.html"
            await update.message.reply_document(document=buf, filename=filename,
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


from telegram import BotCommand

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("spec",     "📋 Especificación de iniciativa forestal"),
        BotCommand("plan",     "🗺️ Plan de ejecución forestal"),
        BotCommand("build",    "🔨 Construcción de solución forestal"),
        BotCommand("test",     "🧪 Validación en operación forestal"),
        BotCommand("review",   "🔍 Revisión crítica forestal"),
        BotCommand("ship",     "🚀 Lanzamiento a operación forestal"),
        BotCommand("artifact", "🎨 Genera HTML, Excel o gráfico PNG"),
    ])

app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).post_init(post_init).build()

for skill in SKILL_PROMPTS:
    app.add_handler(CommandHandler(skill, skill_handler))

app.add_handler(CommandHandler("artifact", artifact_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))

app.run_polling()

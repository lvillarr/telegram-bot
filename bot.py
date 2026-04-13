import os
import base64
import anthropic
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


app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()

for skill in SKILL_PROMPTS:
    app.add_handler(CommandHandler(skill, skill_handler))

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))

app.run_polling()

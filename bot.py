import os
import base64
import anthropic
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram import Update
from telegram.ext import ContextTypes

with open("orquestador_prompt.txt", "r") as f:
    BASE_PROMPT = f.read()

SYSTEM_PROMPT = BASE_PROMPT + """

---

## Estilo de comunicación en Telegram

- Responde en lenguaje natural y cercano, como un colega experto
- Usa emojis para estructurar y dar vida a las respuestas 📊✅⚠️🌲
- Usa negritas y listas cuando ayuden a la claridad
- Si te envían una imagen, analízala en el contexto operacional de Arauco
- Sé conciso pero completo — máximo 3-4 párrafos salvo que se pida detalle
"""

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}]
    )
    await update.message.reply_text(response.content[0].text)


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]  # mejor resolución
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    caption = update.message.caption or "Analiza esta imagen en el contexto operacional de Arauco."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64
                    }
                },
                {
                    "type": "text",
                    "text": caption
                }
            ]
        }]
    )
    await update.message.reply_text(response.content[0].text)


app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))
app.run_polling()

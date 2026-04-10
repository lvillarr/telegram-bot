import os
import anthropic
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram import Update
from telegram.ext import ContextTypes

with open("orquestador_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}]
    )
    await update.message.reply_text(response.content[0].text)

app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.run_polling()

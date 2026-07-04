import os
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

TOKEN = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Здравствуйте! Напишите вашу жалобу одним сообщением — она будет передана."
    )


async def handle_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "private":
        return

    user = update.effective_user
    text = update.message.text

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=f"📩 Жалоба от {user.full_name} (@{user.username or 'нет username'}):\n\n{text}"
    )
    await update.message.reply_text("Спасибо, ваша жалоба передана.")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_complaint))
    app.run_polling()


if __name__ == "__main__":
    main()

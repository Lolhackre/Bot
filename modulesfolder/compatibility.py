
import sys
import asyncio
import config
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from database import db_get_user_rank, db_get_command_rank, db_module_enabled_get, db_format_user_link
 


 
# ---------- !совместимость ----------
COMPATIBILITY_COMMENTS = [
    (0, 20, "😬 Ну... бывает и хуже. Наверное."),
    (20, 40, "🤔 Есть над чем работать."),
    (40, 60, "🙂 Вполне неплохо, можно дружить."),
    (60, 80, "😄 О, тут реально что-то есть!"),
    (80, 95, "🔥 Практически родственные души!"),
    (95, 101, "💯 Идеальное совпадение, это судьба!"),
]
 
def _compatibility_comment(percent):
    for low, high, comment in COMPATIBILITY_COMMENTS:
        if low <= percent < high:
            return comment
    return COMPATIBILITY_COMMENTS[-1][2]
 
async def command_compatibility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db_module_enabled_get("compatibility") == False:
        await update.message.reply_text("⚠️ Модуль совместимости отключен администратором.")
        return
    


    if str(update.message.chat_id) != str(config.MAIN_GROUP_CHAT_ID):
        return
 
    user = update.effective_user
    is_creator = (user.id == 8049751536)
 
    if db_get_user_rank(user.id) < db_get_command_rank("совместимость"):
        await update.message.reply_text("⛔ <b>Недостаточно прав для этой команды.</b>", parse_mode=ParseMode.HTML)
        return

    try:
        current_rank = await asyncio.to_thread(db_get_user_rank, user.id)
        min_rank = await asyncio.to_thread(db_get_command_rank, "совместимость")
    except Exception as e:
        print(f"Ошибка при чтении рангов из БД: {e}", file=sys.stderr)
        return
 
    if not is_creator and current_rank < min_rank:
        await update.message.reply_text("⛔ <b>Недостаточно прав для этой команды.</b>", parse_mode=ParseMode.HTML)
        return
 
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Ответьте этой командой на сообщение того, с кем хотите проверить совместимость.")
        return
 
    target_user = update.message.reply_to_message.from_user
    if target_user.is_bot:
        await update.message.reply_text("🤖 С ботами совместимость не считается, у нас тут не свидание.")
        return
 
    if target_user.id == user.id:
        await update.message.reply_text("🪞 Совместимость с самим собой — 100%. Ты идеальная пара для себя, поздравляю.")
        return
 
    # Детерминированный процент — одна и та же пара всегда получает один и тот же результат
    a, b = sorted([user.id, target_user.id])
    seed_value = (a * 2654435761 + b) & 0xFFFFFFFF
    percent = seed_value % 101
 
    actor_link = db_format_user_link(user.id, user.username, user.full_name)
    target_link = db_format_user_link(target_user.id, target_user.username, target_user.full_name)
    comment = _compatibility_comment(percent)
 
    filled = round(percent / 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
 
    text = (
        f"💞 <b>Совместимость</b>\n"
        f"{actor_link} + {target_link}\n\n"
        f"{bar} <b>{percent}%</b>\n"
        f"{comment}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
 
import random
import sqlite3
import sys
import asyncio
from datetime import datetime, timedelta
import config
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from html import escape
from database import db_get_user_rank, db_get_command_rank, db_module_enabled, db_module_enabled_get,db_get_random_active_user, db_format_user_link
 

 


# ---------- !кто из нас [прилагательное] ----------
WHO_OF_US_TEMPLATES = [
    "🏆 Титул «Самый {adj} в чате» уходит к {name}!",
    "🎖 По итогам тайного голосования небес, самый {adj} тут — {name}.",
    "🔮 Магический шар указал на {name}. Официально: самый {adj}.",
    "📢 Внимание! Самым {adj} в этом чате признан(а) {name}!",
]
 
async def command_who_of_us(update: Update, context: ContextTypes.DEFAULT_TYPE, adjective: str):
    if str(update.message.chat_id) != str(config.MAIN_GROUP_CHAT_ID):
        return
    if db_module_enabled_get("whois") == False:
        await update.message.reply_text("⛔ <b>Модуль \"кто is\" отключен.</b>", parse_mode=ParseMode.HTML)
        return
    user = update.effective_user
    is_creator = (user.id == 8049751536)
 
    try:
        current_rank = await asyncio.to_thread(db_get_user_rank, user.id)
        min_rank = await asyncio.to_thread(db_get_command_rank, "кто_из_нас")
    except Exception as e:
        print(f"Ошибка при чтении рангов из БД: {e}", file=sys.stderr)
        return
 
    if not is_creator and current_rank < min_rank:
        await update.message.reply_text("⛔ <b>Недостаточно прав для этой команды.</b>", parse_mode=ParseMode.HTML)
        return
 
    adjective = adjective.strip()
    if not adjective:
        await update.message.reply_text("⚠️ Укажи прилагательное. Пример: <code>!кто из нас ленивый</code>", parse_mode=ParseMode.HTML)
        return
 
    if len(adjective) > 40:
        await update.message.reply_text("⚠️ Слишком длинное прилагательное, покороче давай.")
        return
 
    try:
        winner = await asyncio.to_thread(db_get_random_active_user)
    except Exception as e:
        print(f"Ошибка при выборе случайного участника: {e}", file=sys.stderr)
        return
 
    if not winner:
        await update.message.reply_text("😶 В базе пока нет активных участников для розыгрыша.")
        return
 
    w_id, w_username, w_full_name = winner
    winner_link = db_format_user_link(w_id, w_username, w_full_name)
 
    template = random.choice(WHO_OF_US_TEMPLATES)
    text = template.format(adj=escape(adjective), name=winner_link)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

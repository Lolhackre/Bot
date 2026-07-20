
import sqlite3
import sys
import asyncio
import config
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from html import escape
from database import db_get_user_rank, db_get_command_rank, db_module_enabled, db_module_enabled_get, db_format_user_link

def db_get_balabol_and_silent():
    """Собираем данные из БД в один быстрый синхронный заход"""
    with sqlite3.connect(config.DB_PATH) as conn:
        # 1. Ищем ТОП-1 балабола дня
        cur_balabol = conn.execute("""
            SELECT user_id, username, full_name, daily_messages_count 
            FROM users 
            WHERE daily_messages_count > 0
            ORDER BY daily_messages_count DESC 
            LIMIT 1
        """)
        balabol_row = cur_balabol.fetchone()
        
        # 2. Ищем случайного «Молчуна дня»
        cur_silent = conn.execute("""
            SELECT user_id, username, full_name 
            FROM users 
            WHERE daily_messages_count = 0
            ORDER BY RANDOM() 
            LIMIT 1
        """)
        silent_row = cur_silent.fetchone()
        
        return balabol_row, silent_row
 
def db_reset_daily_counters():
    """Сбрасываем счетчики отдельной быстрой транзакцией"""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("UPDATE users SET daily_messages_count = 0")
        conn.commit()
 
 
async def daily_balabol_check(context: ContextTypes.DEFAULT_TYPE):
    if db_module_enabled_get("balabol") == False:
        return
    try:
        # Шаг 1: Быстро забираем данные из БД в отдельном потоке (база сразу освобождается)
        balabol_row, silent_row = await asyncio.to_thread(db_get_balabol_and_silent)
        
        if not balabol_row and not silent_row:
            return
 
        # Шаг 2: Формируем текст
        text = "🏆 <b>ИТОГИ ДНЯ: РЕЙТИНГ АКТИВНОСТИ</b> 🏆\n\n"
        
        if balabol_row:
            b_uid, b_un, b_fn, b_count = balabol_row
            b_name = escape(b_fn or b_un or f"ID: {b_uid}")
            text += f"📢 <b>Балабол дня:</b> {b_name}\n💬 Настрочил целых <b>{b_count}</b> сообщ. за сутки!\n\n"
        
        if silent_row:
            s_uid, s_un, s_fn = silent_row
            s_name = escape(s_fn or s_un or f"ID: {s_uid}")
            text += f"🤫 <b>Молчун дня:</b> {s_name}\n🤐 Не проронил ни слова. Партизан года!\n\n"
            
        text += "🏅 Эти шуточные звания закреплены в топе до завтрашнего вечера!"
 
        # Шаг 3: Отправляем в Telegram (база в это время отдыхает и доступна для других тасок)
        msg = await context.bot.send_message(
            chat_id=config.MAIN_GROUP_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )
        
            
        # Шаг 4: Быстро сбрасываем суточные счетчики в БД
        await asyncio.to_thread(db_reset_daily_counters)
 
    except Exception as e:
        print(f"Ошибка в daily_balabol_check: {e}", file=sys.stderr)
 
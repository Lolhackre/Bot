import os
import sqlite3
from datetime import time as dtime
from html import escape

import pytz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
    filters,
)

# ---------- Настройки ----------
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])            # чат админов, куда идут жалобы
MAIN_GROUP_CHAT_ID = int(os.environ["MAIN_GROUP_CHAT_ID"])  # основная группа с людьми

DB_PATH = os.environ.get("DB_PATH", "bot.db")
KYIV_TZ = pytz.timezone("Europe/Kyiv")

POLL_OPTIONS = [
    "Мытница",
    "Хрещатик",
    "Долина роз",
    "Музей",
    "Химпас",
    "ЖД вокзал",
    "Юго-Запад",
    "Дом природы",
    "Дружба народов",
    "Не гуляю",
]

WALK_SCORE = 5        # очков кармы за участие в прогулке
NOT_WALKING_PENALTY = 2  # очков штрафа за выбор "Не гуляю"
MESSAGE_SCORE = 1     # очков за одно сообщение в группе


# ---------- База данных ----------
def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            messages_count INTEGER DEFAULT 0,
            walks_count INTEGER DEFAULT 0,
            walk_karma INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            poll_id TEXT PRIMARY KEY,
            not_walking_index INTEGER
        )
    """)
    # На случай, если база уже существовала без колонки walk_karma
    cur = conn.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cur.fetchall()]
    if "walk_karma" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN walk_karma INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def db_upsert_user(user_id, username, full_name):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO users (user_id, username, full_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
    """, (user_id, username, full_name))
    conn.commit()
    conn.close()


def db_add_message(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET messages_count = messages_count + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def db_add_walk(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE users
        SET walks_count = walks_count + 1,
            walk_karma = walk_karma + ?
        WHERE user_id=?
    """, (WALK_SCORE, user_id))
    conn.commit()
    conn.close()


def db_penalize_not_walking(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE users
        SET walk_karma = walk_karma - ?
        WHERE user_id=?
    """, (NOT_WALKING_PENALTY, user_id))
    conn.commit()
    conn.close()


def db_top_by_messages(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(f"""
        SELECT user_id, username, full_name, messages_count,
               (messages_count * {MESSAGE_SCORE}) AS score
        FROM users
        WHERE messages_count > 0
        ORDER BY score DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def db_top_by_walks(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        SELECT user_id, username, full_name, walks_count, walk_karma
        FROM users
        WHERE walk_karma != 0 OR walks_count > 0
        ORDER BY walk_karma DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def db_get_user_stats(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(f"""
        SELECT user_id, username, full_name, messages_count,
               (messages_count * {MESSAGE_SCORE}) AS message_score,
               walks_count, walk_karma
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def db_all_usernames():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT username FROM users WHERE username IS NOT NULL")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def db_save_poll(poll_id, not_walking_index):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO polls (poll_id, not_walking_index) VALUES (?, ?)",
        (poll_id, not_walking_index),
    )
    conn.commit()
    conn.close()


def db_get_poll(poll_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT not_walking_index FROM polls WHERE poll_id=?", (poll_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# ---------- Жалобы (личка -> админ чат) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Здравствуйте! Напишите вашу жалобу одним сообщением — она будет передана."
    )


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "private":
        return

    user = update.effective_user
    text = update.message.text

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"📩 Жалоба от {user.full_name} (@{user.username or 'нет username'}):\n\n{text}"
    )
    await update.message.reply_text("Спасибо, ваша жалоба передана.")


# ---------- Карма: считаем сообщения в основной группе ----------
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != MAIN_GROUP_CHAT_ID:
        return

    user = update.effective_user
    if user.is_bot:
        return

    text = update.message.text or ""
    if text.startswith("!"):
        return  # текстовые команды вида !карма не считаются обычным сообщением

    db_upsert_user(user.id, user.username, user.full_name)
    db_add_message(user.id)


# ---------- Текстовые команды в стиле "!карма" ----------
async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != MAIN_GROUP_CHAT_ID:
        return

    text = (update.message.text or "").strip().lower()
    if text in ("!карма", "!топ"):
        await show_karma(update, context)
    elif text in ("!моя карма", "!моякарма"):
        await show_my_karma(update, context)


# ---------- Команда /карма ----------
def format_user_link(user_id, username, full_name):
    """
    Ссылка на профиль пользователя, кликабельная (можно открыть профиль),
    но БЕЗ символа @ в тексте — поэтому Telegram не считает это упоминанием
    и не шлёт человеку уведомление о пинге.
    """
    display_name = escape(full_name or username or str(user_id))
    url = f"tg://user?id={user_id}"
    return f'<a href="{url}">{display_name}</a>'


async def show_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    messages_rows = db_top_by_messages(10)
    walks_rows = db_top_by_walks(10)

    if not messages_rows and not walks_rows:
        await update.message.reply_text("Пока нет данных для статистики.")
        return

    lines = ["💬 Карма за общение:\n"]
    if messages_rows:
        for i, (user_id, username, full_name, messages, score) in enumerate(messages_rows, start=1):
            name = format_user_link(user_id, username, full_name)
            lines.append(f"{i}. {name} — {score} очков ({messages} сообщ.)")
    else:
        lines.append("Пока нет данных.")

    lines.append("")  # пустая строка-разделитель
    lines.append("🚶 Карма за прогулки:\n")
    if walks_rows:
        for i, (user_id, username, full_name, walks, score) in enumerate(walks_rows, start=1):
            name = format_user_link(user_id, username, full_name)
            lines.append(f"{i}. {name} — {score} очков кармы | Прогулок: {walks}")
    else:
        lines.append("Пока нет данных.")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def show_my_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db_get_user_stats(user.id)

    if stats is None or (stats[3] == 0 and stats[5] == 0):
        await update.message.reply_text("У вас пока нет статистики — напишите пару сообщений в чате или проголосуйте в опросе о прогулке.")
        return

    _, username, full_name, messages, message_score, walks, walk_karma = stats
    name = format_user_link(user.id, username, full_name)

    text = (
        f"📊 Статистика для {name}:\n\n"
        f"💬 Карма за общение: {message_score} очков ({messages} сообщ.)\n"
        f"🚶 Карма за прогулки: {walk_karma} очков (Прогулок: {walks})"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ---------- Ежедневный опрос ----------
async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    message = await context.bot.send_poll(
        chat_id=MAIN_GROUP_CHAT_ID,
        question="🌆 Где сегодня гуляем?",
        options=POLL_OPTIONS,
        is_anonymous=False,
        allows_multiple_answers=True,
    )

    not_walking_index = POLL_OPTIONS.index("Не гуляю")
    db_save_poll(message.poll.id, not_walking_index)

    await context.bot.pin_chat_message(
        chat_id=MAIN_GROUP_CHAT_ID,
        message_id=message.message_id,
        disable_notification=False,  # уведомление о закреплении придёт всем
    )

    usernames = db_all_usernames()
    if usernames:
        mentions = " ".join(f"@{u}" for u in usernames)
        chunk = ""
        for part in mentions.split(" "):
            if len(chunk) + len(part) + 1 > 3500:
                await context.bot.send_message(chat_id=MAIN_GROUP_CHAT_ID, text=chunk)
                chunk = ""
            chunk += part + " "
        if chunk.strip():
            await context.bot.send_message(
                chat_id=MAIN_GROUP_CHAT_ID,
                text=f"👆 Отметьтесь в опросе выше!\n{chunk.strip()}"
            )


# ---------- Обработка голосов (для кармы) ----------
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    not_walking_index = db_get_poll(answer.poll_id)
    if not_walking_index is None:
        return

    user = answer.user
    db_upsert_user(user.id, user.username, user.full_name)

    chosen = answer.option_ids
    if not chosen:
        return  # человек снял голос — ничего не делаем

    if not_walking_index in chosen:
        # Выбрано "Не гуляю" (даже вместе с другими местами) — штраф,
        # карма за прогулку не начисляется, счётчик прогулок не растёт
        db_penalize_not_walking(user.id)
    else:
        # Выбрано одно или несколько мест без "Не гуляю" —
        # засчитывается как ОДНА прогулка (не умножается на число мест)
        db_add_walk(user.id)


def main():
    db_init()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["karma", "top"], show_karma))
    app.add_handler(CommandHandler("mykarma", show_my_karma))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^!(карма|топ|моя\s*карма)$") & filters.ChatType.GROUPS,
        handle_text_command
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        handle_group_message
    ))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    app.job_queue.run_daily(
        send_daily_poll,
        time=dtime(hour=20, minute=0, tzinfo=KYIV_TZ),
    )

    app.run_polling()


if __name__ == "__main__":
    main()
        

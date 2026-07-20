import sqlite3
from datetime import datetime
from config import DB_PATH, MESSAGE_SCORE, WALK_SCORE, NOT_WALKING_PENALTY, DEFAULT_RANK_NAMES, DEFAULT_COMMAND_RANKS, DB_MODULES_ENABLED
from html import escape
import json

from telegram.ext import (
    Application,
    MessageHandler, 
    CommandHandler,
    PollAnswerHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")  # Защита базы от падений и блокировок
    return conn

def db_init():
    with db_connect() as conn:
        # Таблица пользователей с новым полем daily_messages_count
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                messages_count INTEGER DEFAULT 0,
                walks_count INTEGER DEFAULT 0,
                walk_karma INTEGER DEFAULT 0,
                permission_rank INTEGER DEFAULT 0,
                days_inactive INTEGER DEFAULT 0,
                last_activity TEXT,
                daily_messages_count INTEGER DEFAULT 0,
                nickname TEXT,
                status_text TEXT,
                birthday TEXT
            )
        """)
        
        # Автоматическая миграция на случай, если база уже создана ранее
        try:
            conn.execute("ALTER TABLE users ADD COLUMN daily_messages_count INTEGER DEFAULT 0;")
        except sqlite3.OperationalError:
            pass  # Поле уже существует, всё ок

        try:
            conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT;")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE users ADD COLUMN status_text TEXT;")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE users ADD COLUMN birthday TEXT;")  # хранится в формате ДД.ММ
        except sqlite3.OperationalError:
            pass
            
        # Таблица опросов
        conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                poll_id TEXT PRIMARY KEY,
                poll_type TEXT, -- 'place' или 'attendance'
                message_id INTEGER,
                chat_id TEXT,
                options_json TEXT,
                not_walking_index INTEGER
            )
        """)

        # Таблица названий рангов (можно менять на лету командой !команда)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rank_names (
                rank INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)

        # Засеиваем дефолтные названия рангов, если их еще нет
        for rank, name in DEFAULT_RANK_NAMES.items():
            conn.execute("""
                INSERT OR IGNORE INTO rank_names (rank, name) VALUES (?, ?)
            """, (rank, name))

        # Таблица минимального ранга доступа для настраиваемых команд (!доступ)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS command_access (
                command TEXT PRIMARY KEY,
                min_rank INTEGER NOT NULL
            )
        """)
        for cmd, rank in DEFAULT_COMMAND_RANKS.items():
            conn.execute("""
                INSERT OR IGNORE INTO command_access (command, min_rank) VALUES (?, ?)
            """, (cmd, rank))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS modules (
                module_name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        for module_name, enabled in DB_MODULES_ENABLED.items():
            conn.execute("""
                INSERT OR IGNORE INTO modules (module_name, enabled) VALUES (?, ?)
            """, (module_name, int(enabled)))

        conn.commit()


def db_format_user_link(user_id, username, full_name):
    nickname = db_get_nickname(user_id)
    display_name = escape(nickname or full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

def db_module_enabled(module_name):
    """Проверяет, включен ли модуль в базе (по умолчанию True, если нет записи)"""
    with db_connect() as conn:
        cur = conn.execute("SELECT enabled FROM modules WHERE module_name = ?", (module_name,))
        row = cur.fetchone()
        if row is None:
            # Если модуля нет в базе, создаем запись с enabled=1
            conn.execute("INSERT INTO modules (module_name, enabled) VALUES (?, 1)", (module_name,))
            conn.commit()
            return True
        return bool(row[0])

def db_module_enabled_get(module_name):
    """Возвращает True/False, включен ли модуль в базе (по умолчанию True, если нет записи)"""
    with db_connect() as conn:
        cur = conn.execute("SELECT enabled FROM modules WHERE module_name = ?", (module_name,))
        row = cur.fetchone()
        if row is None:
            # Если модуля нет в базе, создаем запись с enabled=1
            conn.execute("INSERT INTO modules (module_name, enabled) VALUES (?, 1)", (module_name,))
            conn.commit()
            return True
        return bool(row[0])

def db_log_message(user_id, username, full_name, is_command=False):
    """Обновляет профиль, сбрасывает счетчик молчания и инкрементирует сообщения (общие и за день)"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg_inc = 0 if is_command else 1
    
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, full_name, messages_count, daily_messages_count, permission_rank, days_inactive, last_activity)
            VALUES (?, ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                username = excluded.username, 
                full_name = excluded.full_name,
                messages_count = messages_count + ?,
                daily_messages_count = daily_messages_count + ?,
                days_inactive = 0,
                last_activity = ?
        """, (user_id, username, full_name, msg_inc, msg_inc, now_str, msg_inc, msg_inc, now_str))
        conn.commit()

def db_get_user_rank(user_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT permission_rank FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 1

def db_fix_default_rank_bug():
    """Сбрасывает ранг 1 (баг старого дефолта) обратно в 0 — Участник.
    Не трогает ранги 2 и выше, так как они точно назначались вручную."""
    with db_connect() as conn:
        cur = conn.execute("UPDATE users SET permission_rank = 0 WHERE permission_rank = 1")
        conn.commit()
        return cur.rowcount

def db_set_user_rank(user_id, rank):
    with db_connect() as conn:
        conn.execute("UPDATE users SET permission_rank = ? WHERE user_id = ?", (rank, user_id))
        conn.commit()

def db_get_rank_names():
    """Возвращает словарь {ранг: название} для всех рангов 0-6"""
    with db_connect() as conn:
        cur = conn.execute("SELECT rank, name FROM rank_names")
        rows = cur.fetchall()
    names = dict(DEFAULT_RANK_NAMES)  # фолбек на случай отсутствия записи
    names.update({rank: name for rank, name in rows})
    return names

def db_get_rank_name(rank):
    """Возвращает название конкретного ранга (с фолбеком на дефолт/номер)"""
    with db_connect() as conn:
        cur = conn.execute("SELECT name FROM rank_names WHERE rank = ?", (rank,))
        row = cur.fetchone()
    if row:
        return row[0]
    return DEFAULT_RANK_NAMES.get(rank, str(rank))

def db_set_rank_name(rank, name):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO rank_names (rank, name) VALUES (?, ?)
            ON CONFLICT(rank) DO UPDATE SET name = excluded.name
        """, (rank, name))
        conn.commit()

def db_get_command_ranks():
    """Возвращает словарь {ключ_команды: минимальный_ранг} для всех настраиваемых команд"""
    with db_connect() as conn:
        cur = conn.execute("SELECT command, min_rank FROM command_access")
        rows = cur.fetchall()
    ranks = dict(DEFAULT_COMMAND_RANKS)  # фолбек на случай отсутствия записи
    ranks.update({cmd: rank for cmd, rank in rows})
    return ranks

def db_get_command_rank(command):
    """Возвращает минимальный ранг доступа для конкретной команды (с фолбеком на дефолт)"""
    with db_connect() as conn:
        cur = conn.execute("SELECT min_rank FROM command_access WHERE command = ?", (command,))
        row = cur.fetchone()
    if row is not None:
        return row[0]
    return DEFAULT_COMMAND_RANKS.get(command, 6)

def db_set_command_rank(command, min_rank):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO command_access (command, min_rank) VALUES (?, ?)
            ON CONFLICT(command) DO UPDATE SET min_rank = excluded.min_rank
        """, (command, min_rank))
        conn.commit()

def db_add_walk(user_id):
    with db_connect() as conn:
        conn.execute("""
            UPDATE users
            SET walks_count = walks_count + 1, walk_karma = walk_karma + ?
            WHERE user_id = ?
        """, (WALK_SCORE, user_id))
        conn.commit()

def db_penalize_not_walking(user_id):
    with db_connect() as conn:
        conn.execute("UPDATE users SET walk_karma = walk_karma - ? WHERE user_id = ?", (NOT_WALKING_PENALTY, user_id))
        conn.commit()

def db_save_poll(poll_id, poll_type, message_id, chat_id, options_json, not_walking_index=None):
    with db_connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO polls (poll_id, poll_type, message_id, chat_id, options_json, not_walking_index)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (poll_id, poll_type, message_id, chat_id, options_json, not_walking_index))
        conn.commit()

def db_get_poll(poll_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT poll_type, message_id, chat_id, options_json, not_walking_index FROM polls WHERE poll_id = ?", (poll_id,))
        return cur.fetchone()

def db_top_by_messages(limit=10):
    with db_connect() as conn:
        cur = conn.execute(f"SELECT user_id, username, full_name, messages_count, (messages_count * {MESSAGE_SCORE}) FROM users WHERE messages_count > 0 ORDER BY messages_count DESC LIMIT ?", (limit,))
        return cur.fetchall()

def db_top_by_walks(limit=10):
    with db_connect() as conn:
        cur = conn.execute("SELECT user_id, username, full_name, walks_count, walk_karma FROM users WHERE walk_karma != 0 OR walks_count > 0 ORDER BY walk_karma DESC LIMIT ?", (limit,))
        return cur.fetchall()

def db_get_user_stats(user_id):
    with db_connect() as conn:
        cur = conn.execute(f"SELECT user_id, username, full_name, messages_count, (messages_count * {MESSAGE_SCORE}), walks_count, walk_karma, permission_rank, days_inactive FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def db_set_nickname(user_id, nickname):
    """Устанавливает (или сбрасывает, если nickname=None) кастомный ник участника."""
    with db_connect() as conn:
        conn.execute("UPDATE users SET nickname = ? WHERE user_id = ?", (nickname, user_id))
        conn.commit()

def db_get_nickname(user_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT nickname FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None

def db_set_status(user_id, status_text):
    """Устанавливает (или сбрасывает, если status_text=None) статус/описание профиля."""
    with db_connect() as conn:
        conn.execute("UPDATE users SET status_text = ? WHERE user_id = ?", (status_text, user_id))
        conn.commit()

def db_get_status(user_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT status_text FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None

def db_set_birthday(user_id, birthday_str):
    """Устанавливает (или сбрасывает, если birthday_str=None) день рождения в формате ДД.ММ"""
    with db_connect() as conn:
        conn.execute("UPDATE users SET birthday = ? WHERE user_id = ?", (birthday_str, user_id))
        conn.commit()

def db_get_birthday(user_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT birthday FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None

def db_get_profile_extra(user_id):
    """Возвращает (nickname, status_text, birthday) одним запросом — для показа в !моякарма"""
    with db_connect() as conn:
        cur = conn.execute("SELECT nickname, status_text, birthday FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row if row else (None, None, None)

def db_get_todays_birthdays(day_month_str):
    """Возвращает [(user_id, username, full_name, nickname), ...] у кого сегодня ДР (формат ДД.ММ)"""
    with db_connect() as conn:
        cur = conn.execute("SELECT user_id, username, full_name, nickname FROM users WHERE birthday = ?", (day_month_str,))
        return cur.fetchall()

def db_get_random_active_user(exclude_user_id=None):
    """Возвращает случайного участника, который хоть раз писал в чат (для !кто из нас)"""
    with db_connect() as conn:
        if exclude_user_id is not None:
            cur = conn.execute(
                "SELECT user_id, username, full_name FROM users WHERE messages_count > 0 AND user_id != ? ORDER BY RANDOM() LIMIT 1",
                (exclude_user_id,)
            )
        else:
            cur = conn.execute(
                "SELECT user_id, username, full_name FROM users WHERE messages_count > 0 ORDER BY RANDOM() LIMIT 1"
            )
        return cur.fetchone()

def db_get_all_users():
    with db_connect() as conn:
        cur = conn.execute("SELECT user_id, username, full_name, days_inactive FROM users")
        return cur.fetchall()

def db_increment_inactivity():
    """Увеличивает счетчик дней молчания для всех на 1"""
    with db_connect() as conn:
        conn.execute("UPDATE users SET days_inactive = days_inactive + 1")
        conn.commit()

def db_get_last_poll():
    """Выносим синхронный SQL в отдельный поток, чтобы не фризить бота"""
    with db_connect() as conn:
        cur = conn.execute(
            "SELECT poll_id, message_id FROM polls WHERE poll_type='place' ORDER BY rowid DESC LIMIT 1"
        )
        return cur.fetchone()

def db_add_penalty(user_id: int, amount: int = 500):
    """Увеличивает сумму штрафов пользователя на заданную величину."""
    with db_connect() as conn:
        # Создаем таблицу, если её еще нет
        conn.execute("""
            CREATE TABLE IF NOT EXISTS penalties (
                user_id INTEGER PRIMARY KEY,
                total_amount INTEGER DEFAULT 0
            )
        """)
        # Вставляем запись или суммируем штраф, если юзер уже есть в базе
        conn.execute("""
            INSERT INTO penalties (user_id, total_amount) 
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET total_amount = total_amount + ?
        """, (user_id, amount, amount))
        conn.commit()

def db_get_penalty(user_id: int) -> int:
    """Возвращает текущую сумму штрафов пользователя."""
    with db_connect() as conn:
        cur = conn.execute("SELECT total_amount FROM penalties WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0
    
def db_get_user_last_vote(user_id, poll_id):
    with db_connect() as conn:
        cur = conn.execute("SELECT option_ids FROM user_current_votes WHERE user_id = ? AND poll_id = ?", (user_id, str(poll_id)))
        row = cur.fetchone()
        return json.loads(row[0]) if row else []

def db_update_user_vote(user_id, poll_id, option_ids):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO user_current_votes (user_id, poll_id, option_ids)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, poll_id) DO UPDATE SET option_ids = excluded.option_ids
        """, (user_id, str(poll_id), json.dumps(option_ids)))
        conn.commit()

def db_change_walk_karma(user_id, amount):
    with db_connect() as conn:
        conn.execute("""
            UPDATE users 
            SET walk_karma = walk_karma + ? 
            WHERE user_id = ?
        """, (amount, user_id))
        conn.commit()

# ---------- Безопасные функции начисления и снятия прогулок (Защита от фарма) ----------
def db_apply_walk_attendance(user_id):
    """Начисляет +1 к прогулкам и +1 к карме (уменьшено по запросу)"""
    with db_connect() as conn:
        conn.execute("""
            UPDATE users 
            SET walks_count = walks_count + 1,
                walk_karma = walk_karma + 1
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()

def db_revert_walk_attendance(user_id):
    """Зеркально снимает прогулку и убирает ровно 1 очко кармы"""
    with db_connect() as conn:
        conn.execute("""
            UPDATE users 
            SET walks_count = MAX(0, walks_count - 1),
                walk_karma = walk_karma - 1
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()

def db_get_user_id_by_username(username: str):
    username = username.lstrip('@').lower()
    with db_connect() as conn:
        cur = conn.execute("SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1", (username,))
        row = cur.fetchone()
        return row[0] if row else None

def db_reset_user_stats(user_id):
    """Полное обнуление статистики пользователя"""
    with db_connect() as conn:
        conn.execute("""
            UPDATE users 
            SET messages_count = 0,
                daily_messages_count = 0, -- Добавлено обнуление ежедневного счетчика
                walks_count = 0,
                walk_karma = 0,
                days_inactive = 0
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()

async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE, arg_text: str = None):
    """
    Определяет целевого пользователя.
    1. Если есть reply_to_message — берёт автора реплая.
    2. Если в сообщении есть текстовый тег (@username) — вытаскивает объект пользователя из Telegram Entities.
    3. Если передан чистый ID — ищет по ID.
    """
    # 1. Проверяем реплай (самый высокий приоритет)
    if update.message and update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user.is_bot:
            return None
        return (target_user.id, target_user.username, target_user.full_name)

    # 2. Если реплая нет, проверяем Entities (как Iris)
    if update.message and update.message.entities:
        for entity in update.message.entities:
            # Если это текстовое упоминание (юзер без тега, но кликабельный)
            if entity.type == "text_mention" and entity.user:
                t_user = entity.user
                if not t_user.is_bot:
                    return (t_user.id, t_user.username, t_user.full_name)
            
            # Если это обычный @mention (@username)
            if entity.type == "mention" and arg_text and arg_text.startswith("@"):
                # Попробуем достать объект пользователя, если библиотека его привязала
                if hasattr(entity, 'user') and entity.user:
                    t_user = entity.user
                    if not t_user.is_bot:
                        return (t_user.id, t_user.username, t_user.full_name)

    # 3. Фолбэк (запасной вариант), если передан аргумент
    if not arg_text:
        return None

    arg_text = arg_text.strip()

    # Если передан чистый числовой ID
    if arg_text.isdigit():
        target_id = int(arg_text)
        try:
            chat = await context.bot.get_chat(target_id)
            first = chat.first_name or ""
            last = chat.last_name or ""
            full_name = f"{first} {last}".strip() or chat.title or "Пользователь"
            return (chat.id, chat.username, full_name)
        except Exception:
            pass

    # Если это @username, но Telegram не привязал entity (редкий случай), ищем локально в БД
    if arg_text.startswith("@"):
        username_to_search = arg_text[1:].lower()
        target_id = db_get_user_id_by_username(username_to_search)
        if target_id:
            try:
                import sqlite3
                with db_connect() as conn:
                    cur = conn.execute("SELECT username, full_name FROM users WHERE user_id = ? LIMIT 1", (target_id,))
                    row = cur.fetchone()
                    if row:
                        return (target_id, row[0], row[1])
            except Exception:
                pass

    return None

def format_rank(rank, is_creator=False):
    """Возвращает читаемое название ранга, например 'Заместитель (5)'"""
    if is_creator:
        return "Тех.Админ"
    name = db_get_rank_name(rank)
    return f"{name} ({rank})"

def compute_level(total_score):
    """Простая RPG-кривая уровней: на N-й уровень нужно 10*N^2 очков суммарно."""
    total_score = max(total_score, 0)
    level = int((total_score / 10) ** 0.5) + 1
    current_threshold = 10 * (level - 1) ** 2
    next_threshold = 10 * level ** 2
    into_level = total_score - current_threshold
    span = next_threshold - current_threshold
    progress = into_level / span if span > 0 else 1.0
    return level, into_level, span, progress

def format_silent_ping(username):
    return f'<a href="t.me/{username}">&#8288;</a>'


def db_adjust_penalty(user_id: int, delta: int):
    """Изменяет сумму штрафов пользователя на delta (может быть отрицательным — например,
    когда Глава выдаёт деньги и тем самым списывает часть штрафа). Не уходит ниже 0."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS penalties (
                user_id INTEGER PRIMARY KEY,
                total_amount INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT INTO penalties (user_id, total_amount)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET total_amount = MAX(0, total_amount + ?)
        """, (user_id, max(0, delta), delta))
        conn.commit()

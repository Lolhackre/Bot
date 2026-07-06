import sqlite3
from datetime import datetime
from config import DB_PATH, MESSAGE_SCORE, WALK_SCORE, NOT_WALKING_PENALTY, DEFAULT_RANK_NAMES, DEFAULT_COMMAND_RANKS

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

        conn.commit()

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

def db_get_all_users():
    with db_connect() as conn:
        cur = conn.execute("SELECT user_id, username, full_name, days_inactive FROM users")
        return cur.fetchall()

def db_increment_inactivity():
    """Увеличивает счетчик дней молчания для всех на 1"""
    with db_connect() as conn:
        conn.execute("UPDATE users SET days_inactive = days_inactive + 1")
        conn.commit()

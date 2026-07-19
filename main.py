import sqlite3
import sys
import json
from datetime import time as dtime, datetime, timedelta
from html import escape

import funmodule
import bunker_and_agent
import extra_features
import mafia

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
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

import config
import tempfile
import os
from fishaudio import FishAudio
from fishaudio.utils import save
from database import (
    db_init, db_log_message, db_get_user_rank, 
    db_save_poll, db_get_poll, 
    db_top_by_messages, db_top_by_walks, db_get_user_stats,
    db_get_all_users, db_increment_inactivity, db_set_user_rank,
    db_get_rank_names, db_get_rank_name, db_set_rank_name,
    db_get_command_rank, db_set_command_rank, db_get_command_ranks,
    db_fix_default_rank_bug,
    db_set_nickname, db_get_nickname, db_set_status, db_get_status,
    db_set_birthday, db_get_birthday, db_get_profile_extra, db_get_todays_birthdays,
    db_get_last_poll
)

# Глобальное состояние для отмены опроса на текущий вечер
CANCELLED_POLL_REASON = None
# Флаг, указывающий, что опрос посещаемости уже запущен вручную через !форс (блокирует авто-опросы)
FORCED_ATTENDANCE_ACTIVE = False
# Состояние режима "Стоп Срач"
SRACH_LOCK_ACTIVE = False
SRACH_LOCK_JOB = None # Флаг, указывающий, что режим "Стоп Срач" активен

# ---------- Таблица отслеживания текущих голосов пользователей ----------
def init_votes_tracking():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_current_votes (
                user_id INTEGER,
                poll_id TEXT,
                option_ids TEXT,
                PRIMARY KEY (user_id, poll_id)
            )
        """)
        conn.commit()

def db_get_user_last_vote(user_id, poll_id):
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute("SELECT option_ids FROM user_current_votes WHERE user_id = ? AND poll_id = ?", (user_id, str(poll_id)))
        row = cur.fetchone()
        return json.loads(row[0]) if row else []

def db_update_user_vote(user_id, poll_id, option_ids):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO user_current_votes (user_id, poll_id, option_ids)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, poll_id) DO UPDATE SET option_ids = excluded.option_ids
        """, (user_id, str(poll_id), json.dumps(option_ids)))
        conn.commit()

def db_change_walk_karma(user_id, amount):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            UPDATE users 
            SET walk_karma = walk_karma + ? 
            WHERE user_id = ?
        """, (amount, user_id))
        conn.commit()

# ---------- Безопасные функции начисления и снятия прогулок (Защита от фарма) ----------
def db_apply_walk_attendance(user_id):
    """Начисляет +1 к прогулкам и +1 к карме (уменьшено по запросу)"""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            UPDATE users 
            SET walks_count = walks_count + 1,
                walk_karma = walk_karma + 1
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()

def db_revert_walk_attendance(user_id):
    """Зеркально снимает прогулку и убирает ровно 1 очко кармы"""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            UPDATE users 
            SET walks_count = MAX(0, walks_count - 1),
                walk_karma = walk_karma - 1
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()

def db_get_user_id_by_username(username: str):
    username = username.lstrip('@').lower()
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute("SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1", (username,))
        row = cur.fetchone()
        return row[0] if row else None

def db_reset_user_stats(user_id):
    """Полное обнуление статистики пользователя"""
    with sqlite3.connect(config.DB_PATH) as conn:
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

# ---------- Вспомогательные функции ----------
def format_user_link(user_id, username, full_name):
    nickname = db_get_nickname(user_id)
    display_name = escape(nickname or full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

def parse_birthday(date_part: str):
    """Парсит ДД.ММ или ДД.ММ.ГГГГ и возвращает нормализованную строку ДД.ММ, либо None если невалидно"""
    parts = date_part.strip().split(".")
    if len(parts) not in (2, 3):
        return None
    try:
        day = int(parts[0])
        month = int(parts[1])
    except ValueError:
        return None
    # Валидация через datetime (используем невисокосный год 2001, чтобы 29 февраля отсеивалось корректно только в високосных)
    try:
        datetime(2000, month, day)  # 2000 — високосный, чтобы разрешить 29.02
    except ValueError:
        return None
    return f"{day:02d}.{month:02d}"

def format_silent_ping(username):
    return f'<a href="t.me/{username}">&#8288;</a>'

def format_rank(rank, is_creator=False):
    """Возвращает читаемое название ранга, например 'Заместитель (5)'"""
    if is_creator:
        return "Тех.Админ"
    name = db_get_rank_name(rank)
    return f"{name} ({rank})"

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
                with sqlite3.connect(config.DB_PATH) as conn:
                    cur = conn.execute("SELECT username, full_name FROM users WHERE user_id = ? LIMIT 1", (target_id,))
                    row = cur.fetchone()
                    if row:
                        return (target_id, row[0], row[1])
            except Exception:
                pass

    return None

# ---------- Базовые команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здравствуйте! Напишите вашу жалобу одним сообщением — она будет передана.")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "private":
        return
    
    user = update.effective_user
    raw_text = (update.message.text or "").strip()
    
    # Проверяем наличие команды !админ
    if raw_text.lower().startswith("!админ"):
        current_rank = db_get_user_rank(user.id)
        
        # Железный вайтлист для создателя или ранг >= 5
        if user.id == 8049751536 or current_rank >= 5:
            admin_msg = raw_text[6:].strip()
            if not admin_msg:
                return
            
            await context.bot.send_message(
                chat_id=config.ADMIN_CHAT_ID,
                text=admin_msg
            )
            
            await update.message.reply_text(
                f"От лица бота было отослано ваше сообщение в админ группу: {admin_msg}"
            )
        else:
            # Если нет прав — полный игнор
            pass
        return

    # Команда !войс/!voic — доступна в ЛС только Главе/Тех.админу (проверка внутри command_voic)
    if raw_text.lower().startswith("!войс") or raw_text.lower().startswith("!voic"):
        await command_voic(update, context)
        return

    # Логика для обычных сообщений (жалоб)
    await context.bot.send_message(
        chat_id=config.ADMIN_CHAT_ID,
        text=f"📩 Жалоба от {user.full_name} (@{user.username or 'нет'}):\n\n{raw_text}"
    )
    await update.message.reply_text("Спасибо, ваша жалоба передана.")

# ---------- Логика кармы и активности ----------
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != config.MAIN_GROUP_CHAT_ID:
        return
    user = update.effective_user
    if user.is_bot:
        return

    text = update.message.text or ""
    stripped_lower = text.strip().lower()
    is_command = text.startswith("!") or text.startswith("/") or text.startswith("+")

    # Команда "инфа" без "!" — своя инфа, инфа в ответ на сообщение, или "инфа @юзер"
    bare_words = stripped_lower.split(maxsplit=1)
    bare_first = bare_words[0] if bare_words else ""
    bare_rest = bare_words[1] if len(bare_words) > 1 else None

    if bare_first == "инфа":
        is_command = True
        db_log_message(user.id, user.username, user.full_name, is_command=is_command)
        resolved = await resolve_target_user(update, context, bare_rest)
        if resolved is None:
            target_id, target_username, target_full_name = user.id, user.username, user.full_name
        else:
            target_id, target_username, target_full_name = resolved
        await show_profile(update, context, target_id, target_username, target_full_name)
        return

    # Действие словом-триггером БЕЗ "!" — ответом на сообщение ИЛИ словом + @юзер (например "ударить @юзер")
    if bare_first in funmodule.ACTIONS:
        has_reply = bool(update.message.reply_to_message)
        has_valid_arg = bool(bare_rest) and (bare_rest.startswith("@") or bare_rest.isdigit())

        # Жёсткий фильтр: если после триггера идёт текст, но это не юзернейм и не ID — полностью ИГНОРИРУЕМ
        if bare_rest and not has_valid_arg:
            return

        # Если нет ни ответа на сообщение, ни валидного аргумента — также выходим без реакции
        if not (has_reply or has_valid_arg):
            return

        is_command = True
        db_log_message(user.id, user.username, user.full_name, is_command=is_command)

        current_rank = db_get_user_rank(user.id)
        is_creator = (user.id == 8049751536)
        min_rank = db_get_command_rank("действие")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав для действий. Требуется ранг {format_rank(min_rank)}+.")
            return

        resolved = await resolve_target_user(update, context, bare_rest)
        if resolved is None:
            await update.message.reply_text("⚠️ Не удалось определить, к кому применить действие. Ответьте на сообщение или укажите @username.")
            return
            
        await funmodule.command_action(update, context, bare_first, resolved)
        return

    db_log_message(user.id, user.username, user.full_name, is_command=is_command)

async def _srach_auto_unlock_callback(context: ContextTypes.DEFAULT_TYPE):
    """Вызывается автоматически по истечении таймера"""
    global SRACH_LOCK_ACTIVE, SRACH_LOCK_JOB
    
    SRACH_LOCK_ACTIVE = False
    SRACH_LOCK_JOB = None
    
    job = context.job
    if job and job.chat_id:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text="📢 <b>ВРЕМЯ ОХЛАЖДЕНИЯ ИСТЕКЛО</b>\n"
                 "Режим «Стоп Срач» автоматически завершён. Чат снова доступен для всех участников. Общайтесь культурно!",
            parse_mode=ParseMode.HTML
        )

async def watch_srach_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Хэндлер-надсмотрщик: моментально удаляет любые сообщения во время Стоп Срача"""
    global SRACH_LOCK_ACTIVE
    
    # Если режим тишины не активен, просто выходим и даем коду идти дальше
    if not SRACH_LOCK_ACTIVE:
        return
        
    user_id = update.effective_user.id
    current_rank = db_get_user_rank(user_id)
    is_creator = (user_id == 8049751536)
    # Если режим АКТИВЕН, проверяем ранг
    # (Сюда нужно скопировать твою логику получения ранга, например:)
    # user_id = update.effective_user.id
    # current_rank = db_get_user_rank(user_id) 
    # is_creator = (user_id == CREATOR_ID)
    
    # ПРИМЕР (подставь свои переменные определения ранга):
    if not is_creator and current_rank < 5:
        try:
            await update.message.delete()
        except Exception:
            pass
        # Важно: вызываем ApplicationHandlerStop, чтобы другие хэндлеры 
        # (включая команды) даже не пытались обрабатывать это удаленное сообщение!
        raise ApplicationHandlerStop()

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != config.MAIN_GROUP_CHAT_ID:
        return
    
    raw_text = (update.message.text or "").strip()
    text = raw_text.lower()
    user_id = update.effective_user.id
    current_rank = db_get_user_rank(user_id)
    is_creator = (user_id == 8049751536)


    # 0a. Команда +ник [новый ник] — кастомный ник в статистике и действиях (пусто = сброс)
    if text.startswith("+ник"):
        new_nick = raw_text[4:].strip()
        if not new_nick:
            db_set_nickname(user_id, None)
            await update.message.reply_text("✅ Кастомный ник сброшен, будет отображаться обычное имя.")
            return
        if len(new_nick) > 32:
            await update.message.reply_text("⚠️ Ник слишком длинный (максимум 32 символа).")
            return
        db_set_nickname(user_id, new_nick)
        await update.message.reply_text(
            f"✅ Теперь в статистике и действиях бота вы будете отображаться как: <b>{escape(new_nick)}</b>",
            parse_mode=ParseMode.HTML
        )
        return

    # 0b. Команда +статус [текст] — личный статус/описание в профиле (пусто = сброс)
    if text.startswith("+статус"):
        new_status = raw_text[7:].strip()
        if not new_status:
            db_set_status(user_id, None)
            await update.message.reply_text("✅ Статус очищен.")
            return
        if len(new_status) > 100:
            await update.message.reply_text("⚠️ Статус слишком длинный (максимум 100 символов).")
            return
        db_set_status(user_id, new_status)
        await update.message.reply_text(
            f"✅ Новый статус установлен: <i>{escape(new_status)}</i>",
            parse_mode=ParseMode.HTML
        )
        return

    # 0c. Команда +день рождения [ДД.ММ или ДД.ММ.ГГГГ] — сохраняет дату для авто-поздравления (пусто = сброс)
    if text.startswith("+день рождения"):
        date_part = raw_text[len("+день рождения"):].strip()
        if not date_part:
            db_set_birthday(user_id, None)
            await update.message.reply_text("✅ Дата рождения удалена.")
            return
        parsed = parse_birthday(date_part)
        if not parsed:
            await update.message.reply_text(
                "⚠️ Неверный формат. Используйте: <code>+день рождения ДД.ММ</code> (например <code>+день рождения 15.03</code>)",
                parse_mode=ParseMode.HTML
            )
            return
        db_set_birthday(user_id, parsed)
        await update.message.reply_text(f"🎂 Дата рождения сохранена: {parsed}. Не забуду поздравить!")
        return

    # 0d. Команда !инфа [@юзер / ID] — профиль (свой, ответом на сообщение, или чужой по @юзер/ID)
    if text == "!инфа" or text.startswith("!инфа "):
        arg = raw_text[5:].strip()
        resolved = await resolve_target_user(update, context, arg if arg else None)
        if resolved is None:
            target_id = user_id
            target_username = update.effective_user.username
            target_full_name = update.effective_user.full_name
        else:
            target_id, target_username, target_full_name = resolved
        await show_profile(update, context, target_id, target_username, target_full_name)
        return

    # 1. Справка по командам (!хелп / !помощь)
    if text in ("!хелп", "!помощь"):
        cmd_ranks = db_get_command_ranks()
        help_text = (
            "📖 <b>Справка по командам бота:</b>\n\n"
            "💬 <code>!карма</code> или <code>!топ</code> — Показать топ участников по общению и прогулкам.\n"
            "👤 <code>!моя карма</code> или <code>!моякарма</code> — Показать личную статистику и ваш ранг.\n"
            "ℹ️ <code>!инфа</code> / <code>инфа</code> / <code>!инфа @юзер</code> — Показать инфу о себе или другом участнике (можно и ответом на сообщение).\n"
            "❓ <code>!хелп</code> или <code>!помощь</code> — Вызов этого меню.\n\n"
            "🖊 <b>Профиль:</b>\n"
            "🏷 <code>+ник [текст]</code> — Задать кастомный ник для статистики и действий (пусто — сбросить).\n"
            "💭 <code>+статус [текст]</code> — Задать статус/описание профиля (пусто — сбросить).\n"
            "🎂 <code>+день рождения ДД.ММ</code> — Сохранить дату рождения, бот поздравит в этот день (пусто — удалить).\n\n"
            "☢️ <b>Игра «Бункер»:</b>\n"
            "🚪 <code>!бункер [число выживших]</code> — Создать лобби игры (минимум 4 игрока).\n"
            "🛑 <code>!бункер стоп</code> — Остановить текущую игру (создатель лобби или админ).\n"
            "🔪 <code>!мафия</code> — Создать лобби игры Мафия (минимум 4 игрока). Роль смотрится кнопкой «🎭 Моя роль».\n"
            "🛑 <code>!мафия стоп</code> — Остановить текущую игру в Мафию (создатель лобби или админ).\n"
            "🎴 <code>!карта</code> — Прислать заново кнопки своих карт (можно в любой момент игры, не только в начале).\n\n"
            "⚖️ <code>!суд</code> [причина] — Ответом на сообщение участника устроить шуточный суд чата (с приговором и голосовым оглашением).\n"
        )

        if is_creator or current_rank >= cmd_ranks.get("действие", 0):
            help_text += (
                "🎭 <code>!действия</code> — Список действий, которые можно применить к участнику ответом на его сообщение.\n"
            )

        if is_creator or current_rank >= cmd_ranks.get("отменить_выбор", 3):
            help_text += (
                f"\n⚡ <b>Команды модерации ({format_rank(cmd_ranks.get('отменить_выбор', 3))}+):</b>\n"
                "🚫 <code>!отменить выбор [причина]</code> — Отмена планового вечернего опроса на сегодня.\n"
            )
        if is_creator or current_rank >= cmd_ranks.get("форс", 4):
            help_text += (
                f"📍 <code>!форс [Место]</code> — Преждевременный выбор места и запуск опроса посещаемости. (Ранг {format_rank(cmd_ranks.get('форс', 4))}+)\n"
            )
        if is_creator or current_rank >= cmd_ranks.get("обнулить", 6):
            help_text += (
                f"🔄 <code>!обнулить [@username / ID]</code> — Сбросить всю карму, шаги и дни молчания (или ответом). (Ранг {format_rank(cmd_ranks.get('обнулить', 6))}+)\n"
            )
        if is_creator or current_rank >= cmd_ranks.get("переименовать_ранг", 6):
            help_text += (
                f"🏷 <code>!команда [0-6] [новое название]</code> — Переименовать ранг доступа. (Ранг {format_rank(cmd_ranks.get('переименовать_ранг', 6))}+)\n"
            )
        if is_creator or current_rank >= cmd_ranks.get("тест_опрос", 6):
            help_text += (
                f"🧪 <code>/test_poll</code> — Принудительный мгновенный запуск тестового опроса. (Ранг {format_rank(cmd_ranks.get('тест_опрос', 6))}+)\n"
            )
        if is_creator or current_rank >= cmd_ranks.get("бункер_отмена", 3):
            help_text += (
                f"🗑 <code>!отменить бункер</code> — Расформировать несобранное лобби Бункера. (Ранг {format_rank(cmd_ranks.get('бункер_отмена', 3))}+)\n"
            )
        if is_creator or current_rank >= cmd_ranks.get("бункер_афк", 4):
            help_text += (
                f"⏱ <code>!афк бункер [@юзер / ID]</code> — Исключить афк-игрока из Бункера (можно и ответом на сообщение). (Ранг {format_rank(cmd_ranks.get('бункер_афк', 4))}+)\n"
            )

        if is_creator or current_rank >= 6:
            help_text += (
                f"\n👑 <b>Команды Создателя ({format_rank(6)}, не настраивается):</b>\n"
                "⚙️ <code>!сет ранк [@username / ID] [0-6]</code> — Назначить ранг доступа.\n"
                "🔐 <code>!доступ [команда] [0-6]</code> — Настроить минимальный ранг доступа к команде.\n"
                "🩹 <code>!исправить ранги</code> — Разово сбросить всех со рангом 1 обратно в Участники (0).\n"
            )
            
        help_text += f"\n🎖 <i>Ваш текущий уровень доступа: {format_rank(current_rank, is_creator)}</i>"
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
        return

    # 2. Обработка кармы
    if text in ("!карма", "!топ"):
        await show_karma(update, context)
        return
    elif text in ("!моя карма", "!моякарма"):
        await show_my_karma(update, context)
    elif text == "!уровень":
        await show_level(update, context)
        return

    # 3. Команда !отменить выбор (причина) — Ранг >= 3
    if text.startswith("!отменить выбор"):
        min_rank = db_get_command_rank("отменить_выбор")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}")
            return
        
        reason = raw_text[15:].strip()
        if not reason:
            await update.message.reply_text("⚠️ Пожалуйста, укажите причину отмены.")
            return
        
        global CANCELLED_POLL_REASON
        CANCELLED_POLL_REASON = reason
        await update.message.reply_text(f"🚫 Вечерний опрос отменен администратором.\n📍 <b>Причина:</b> {escape(reason)}", parse_mode=ParseMode.HTML)
        return
# === КОМАНДА: СТОП СРАЧ ===
 # === КОМАНДА: СТОП СРАЧ ===
    if text.startswith("!стоп срач"):
        global SRACH_LOCK_ACTIVE, SRACH_LOCK_JOB

        min_rank = db_get_command_rank("стоп_срач")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}")
            return

        arg = text[10:].strip().lower()

        # --- СЦЕНАРИЙ 1: СБРОС РЕЖИМА ---
        if arg == "сброс":
            if not SRACH_LOCK_ACTIVE:
                await update.message.reply_text("⚠️ Режим «Стоп Срач» и так не был активен.")
                return

            SRACH_LOCK_ACTIVE = False
            
            # Отменяем таймер автовыключения, если он существует
            if SRACH_LOCK_JOB:
                SRACH_LOCK_JOB.schedule_removal()
                SRACH_LOCK_JOB = None

            await update.message.reply_text(
                "🟢 <b>РЕЖИМ СТОП СРАЧ ЗАВЕРШЕН ДОСРОЧНО!</b>\n"
                f"Администратор {update.effective_user.mention_html()} открыл чат. Пожалуйста, соблюдайте правила общения.",
                parse_mode=ParseMode.HTML
            )
            return

        # --- СЦЕНАРИЙ 2: АКТИВАЦИЯ РЕЖИМА ---
        if not arg.isdigit():
            await update.message.reply_text("⚠️ Использование: <code>!стоп срач [минуты]</code> или <code>!стоп срач сброс</code>.", parse_mode=ParseMode.HTML)
            return

        minutes = int(arg)
        if minutes <= 0 or minutes > 1440:
            await update.message.reply_text("⚠️ Укажите адекватное время в минутах (от 1 до 1440).")
            return

        SRACH_LOCK_ACTIVE = True

        # Сбрасываем прошлый таймер, если админ решил "продлить" или перевызвал команду
        if SRACH_LOCK_JOB:
            SRACH_LOCK_JOB.schedule_removal()

        # Планируем автоматическое открытие чата через N минут
        SRACH_LOCK_JOB = context.job_queue.run_once(
            _srach_auto_unlock_callback,
            when=timedelta(minutes=minutes),
            chat_id=update.effective_chat.id
        )

        # Красивое уведомление в чат
        await update.message.reply_text(
            f"🚨 <b>ОБЪЯВЛЕН РЕЖИМ «СТОП СРАЧ»!</b> 🚨\n"
            f"─────────────────────────\n"
            f"🤬 В чате зафиксирован критический уровень токсичности.\n"
            f"⏳ Блокировка установлена на: <b>{minutes} мин.</b>\n"
            f"🚫 <b>Все новые сообщения от обычных участников удаляются автоматически!</b>\n"
            f"✍️ Писать могут только администраторы рангом <b>5+</b>.\n"
            f"─────────────────────────\n"
            f"👮‍♂️ Режим активировал: {update.effective_user.mention_html()}",
            parse_mode=ParseMode.HTML
        )
        return
    # 4. Команда !форс [Место] — Ранг >= 4
    if text.startswith("!форс"):
        min_rank = db_get_command_rank("форс")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}")
            return

        lines = raw_text.split('\n')
        first_line = lines[0].strip()
        
        place_name = first_line[5:].strip()
        if not place_name:
            await update.message.reply_text("⚠️ Используйте формат: <code>!форс Место</code> (и по желанию с новой строки укажите причину)", parse_mode=ParseMode.HTML)
            return

        reason_text = None
        if len(lines) > 1:
            reason_text = "\n".join(lines[1:]).strip()

        global FORCED_ATTENDANCE_ACTIVE
        FORCED_ATTENDANCE_ACTIVE = True

        msg_text = f"📍 Администратор утвердил место для сегодняшней прогулки: <b>{escape(place_name)}</b>\n"
        if reason_text:
            msg_text += f"ℹ️ <b>Причина/Инфо:</b> {escape(reason_text)}\n"
        msg_text += "\nЗапускаю опрос посещаемости..."

        await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)

        attendance_options = ["Да, гуляю", "Нет, не гуляю", "Еще подумаю"]
        attendance_msg = await context.bot.send_poll(
            chat_id=update.message.chat_id,
            question=f"📍 Место: {place_name}\nКто придет?",
            options=attendance_options,
            is_anonymous=False,
            allows_multiple_answers=False,
            protect_content=True
        )
        
        try:
            await context.bot.pin_chat_message(chat_id=update.message.chat_id, message_id=attendance_msg.message_id)
        except Exception:
            pass
            
        db_save_poll(
            poll_id=attendance_msg.poll.id,
            poll_type="attendance",
            message_id=attendance_msg.message_id,
            chat_id=str(update.message.chat_id),
            options_json=json.dumps(attendance_options),
        )
        return

    # 5. Команда !сет ранк — Ранг 6
    if text.startswith("!сет ранк"):
        if not is_creator and current_rank < 6: 
            await update.message.reply_text(f"У вас нет прав для изменения рангов. Требуется {format_rank(6)}.")
            return

        parts = raw_text.split() 
        target_id = None
        rank_val = None
        display_name = None 

        # Вычисляем числовое значение ранга
        if update.message.reply_to_message:
            if len(parts) >= 3:
                try:
                    rank_val = int(parts[2])
                except ValueError:
                    pass
        else:
            if len(parts) >= 4:
                try:
                    rank_val = int(parts[3])
                except ValueError:
                    pass

        if rank_val is None or not (0 <= rank_val <= 6):
            rank_list = ", ".join(f"{r} — {n}" for r, n in sorted(db_get_rank_names().items()))
            await update.message.reply_text(f"⚠️ Неверный формат ранга. Используйте число от 0 до 6.\nПример: `!сет ранк @username 3` или ответом `!сет ранк 3`.\n\nДоступные ранги: {rank_list}")
            return

        # Ищем цель через универсальный resolve_target_user
        target_arg = None if update.message.reply_to_message else (parts[2] if len(parts) >= 3 else None)
        resolved = await resolve_target_user(update, context, target_arg)

        if resolved is not None:
            target_id, target_username, target_full_name = resolved
            display_name = format_user_link(target_id, target_username, target_full_name)
        else:
            await update.message.reply_text("❌ Не удалось найти пользователя.")
            return

        db_set_user_rank(target_id, rank_val)
        await update.message.reply_text(f"✅ Пользователю {display_name} успешно присвоен ранг {format_rank(rank_val)}", parse_mode=ParseMode.HTML)
        return

    # 6. Команда !обнулить — по умолчанию Ранг 6
    if text.startswith("!обнулить"):
        min_rank = db_get_command_rank("обнулить")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Эта команда доступна только с ранга {format_rank(min_rank)}.")
            return

        parts = raw_text.split()
        target_arg = None if update.message.reply_to_message else (parts[1] if len(parts) >= 2 else None)
        
        resolved = await resolve_target_user(update, context, target_arg)
        if resolved is None:
            await update.message.reply_text("❌ Не удалось определить пользователя для обнуления. Укажите @username, ID или ответьте на его сообщение.")
            return

        target_id, target_username, target_full_name = resolved
        display_name = format_user_link(target_id, target_username, target_full_name)

        keyboard = [
            [
                InlineKeyboardButton("✅ Да, обнулить", callback_data=f"reset_yes:{target_id}"),
                InlineKeyboardButton("❌ Нет, отмена", callback_data=f"reset_no:{target_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"❓ Вы уверены, что хотите полностью сбросить карму и статистику пользователя {display_name}?",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
        return

    # 7. Команда !команда [ранг] [новое название] — по умолчанию Ранг 6
    if text.startswith("!команда"):
        min_rank = db_get_command_rank("переименовать_ранг")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Эта команда доступна только с ранга {format_rank(min_rank)}.")
            return

        parts = raw_text.split(maxsplit=2)
        if len(parts) < 3:
            rank_list = "\n".join(f"{r} — {n}" for r, n in sorted(db_get_rank_names().items()))
            await update.message.reply_text(
                f"⚠️ Используйте формат: <code>!команда [0-6] [новое название]</code>\nПример: <code>!команда 4 Куратор группы</code>\n\n"
                f"Текущие названия рангов:\n{escape(rank_list)}",
                parse_mode=ParseMode.HTML
            )
            return

        try:
            rank_val = int(parts[1])
        except ValueError:
            await update.message.reply_text("⚠️ Ранг должен быть числом от 0 до 6.")
            return

        if not (0 <= rank_val <= 6):
            await update.message.reply_text("⚠️ Ранг должен быть числом от 0 до 6.")
            return

        new_name = parts[2].strip()
        if not new_name:
            await update.message.reply_text("⚠️ Укажите новое название ранга.")
            return

        old_name = db_get_rank_name(rank_val)
        db_set_rank_name(rank_val, new_name)
        await update.message.reply_text(
            f"✅ Ранг {rank_val} переименован: <b>{escape(old_name)}</b> → <b>{escape(new_name)}</b>",
            parse_mode=ParseMode.HTML
        )
        return

    # 8. Команда !доступ [команда] [ранг] — Ранг 6
    if text.startswith("!доступ"):
        if not is_creator and current_rank < 6:
            await update.message.reply_text(f"⛔ Эта команда доступна только {format_rank(6)}.")
            return

        parts = raw_text.split()
        if len(parts) < 3:
            current_ranks = db_get_command_ranks()
            lines = ["🔐 <b>Текущий минимальный ранг доступа к командам:</b>\n"]
            for key, label in config.COMMAND_LABELS.items():
                lines.append(f"<code>{key}</code> — {label}: {format_rank(current_ranks.get(key, 6))}")
            lines.append("\nФормат: <code>!доступ [команда] [0-6]</code>\nПример: <code>!доступ форс 3</code>")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        cmd_key = parts[1].lower()
        if cmd_key not in config.COMMAND_LABELS:
            valid_keys = ", ".join(config.COMMAND_LABELS.keys())
            await update.message.reply_text(f"❌ Неизвестная команда «{escape(cmd_key)}». Доступные варианты: {valid_keys}")
            return

        try:
            rank_val = int(parts[2])
        except ValueError:
            await update.message.reply_text("⚠️ Ранг должен быть числом от 0 до 6.")
            return

        if not (0 <= rank_val <= 6):
            await update.message.reply_text("⚠️ Ранг должен быть числом от 0 до 6.")
            return

        db_set_command_rank(cmd_key, rank_val)
        await update.message.reply_text(
            f"✅ Теперь «{config.COMMAND_LABELS[cmd_key]}» доступна с ранга {format_rank(rank_val)} и выше.",
            parse_mode=ParseMode.HTML
        )
        return

    # 9. Список действий между участниками
    if text in ("!действия", "!действие"):
        lines = ["🎭 <b>Все доступные действия (включая 18+):</b>\n\n"]
        for key, (emoji, _) in sorted(funmodule.ACTIONS.items()):
            lines.append(f"{emoji} <code>!{key}</code>")
        
        min_rank = db_get_command_rank("действие")
        lines.append(f"\nℹ️ Используй ответом на сообщение, или !действие @username, или прямо !ударить @username\nТребуется ранг {format_rank(min_rank)}+.")
        
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

# 10. Сами действия (обнять, укусить ухо и т.д.) без знака "!"
    clean_text = text.strip()
    action_key = None
    action_target_arg = None

    # Перебираем ключи, начиная с САМЫХ ДЛИННЫХ фразовых команд
    for key in sorted(funmodule.ACTIONS.keys(), key=len, reverse=True):
        # Строгая проверка: совпадает ли начало строки с ключом из словаря
        if clean_text.lower().startswith(key.lower()):
            action_key = key
            # Берем хвост сообщения, который идет строго ЗА командой
            raw_arg = clean_text[len(key):].strip()
            # Если там пусто — аргумента нет (значит, это реплай). Если текст есть — это юзер/ID
            action_target_arg = raw_arg if raw_arg else None
            break

    # Если действие найдено
    if action_key:
        has_reply = bool(update.message.reply_to_message)
        
        # Перепроверяем аргумент на валидность только если он РЕАЛЬНО передан
        has_valid_arg = False
        if action_target_arg:
            # Обрезаем возможный мусор, смотрим, начинается ли на @ или состоит из цифр
            has_valid_arg = action_target_arg.startswith("@") or action_target_arg.isdigit()

        # Если текст после команды есть, но это не @юзер и не ID — игнорим (защита от флуда)
        if action_target_arg and not has_valid_arg:
            return

        # Если нет ни реплая, ни правильного аргумента — скипаем
        if not (has_reply or has_valid_arg):
            return

        # Проверка рангов
        min_rank = db_get_command_rank("действие")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав для действий. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}")
            return
            
        # Пытаемся определить юзера
        resolved = await resolve_target_user(update, context, action_target_arg)
        if resolved is None:
            await update.message.reply_text("⚠️ Не удалось определить, к кому применить действие. Ответьте на сообщение или укажите @username/ID.")
            return
            
        # Запускаем! Отправит правильный emoji и текст
        await funmodule.command_action(update, context, action_key, resolved)
        return

    # 11. Команда !исправить ранги — Ранг 6
    if text.startswith("!исправить ранги"):
        if not is_creator and current_rank < 6:
            await update.message.reply_text(f"⛔ Эта команда доступна только {format_rank(6)}.")
            return

        fixed_count = db_fix_default_rank_bug()
        await update.message.reply_text(
            f"✅ Готово. Сброшено на «{db_get_rank_name(0)}» участников: {fixed_count}.\n"
            f"ℹ️ Затронуты только те, у кого стоял ранг 1 — ранги 2+ не трогались."
        )
        return

    if text == "!отмазка":
        await funmodule.command_excuse(update, context)
        return

    if text == "!цитата":
        await funmodule.command_quote(update, context)
        return

    if text == "!совместимость":
        await funmodule.command_compatibility(update, context)
        return

    if text.startswith("!суд"):
        await extra_features.command_court(update, context)
        return

    if text in ("!карта", "!карты"):
        await bunker_and_agent.command_bunker_cards(update, context)
        return

    if text.startswith("!кто из нас"):
        adjective = raw_text[len("!кто из нас"):].strip()
        await funmodule.command_who_of_us(update, context, adjective)
        return

    if text.startswith("!бункер стоп") or text.startswith("!бункер отмена"):
        game = bunker_and_agent.BUNKER_GAMES.get(update.message.chat_id)
        if not game:
            await update.message.reply_text("ℹ️ В этом чате сейчас нет активной игры в Бункер.")
            return
        if not is_creator and current_rank < 6 and user_id != game["host_id"]:
            await update.message.reply_text("⛔ Остановить игру может только создатель лобби или админ.")
            return
        del bunker_and_agent.BUNKER_GAMES[update.message.chat_id]
        await update.message.reply_text("🛑 Игра в Бункер остановлена.")
        return

    # !отменить бункер — расформировать лобби, если игра ещё не началась (т.е. не набралось людей)
    if text.startswith("!отменить бункер"):
        min_rank = db_get_command_rank("бункер_отмена")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(
                f"⛔ Отказано в доступе. Ваш ранг: {format_rank(current_rank)}. Требуется ранг: {format_rank(min_rank)}."
            )
            return
        game = bunker_and_agent.BUNKER_GAMES.get(update.message.chat_id)
        if not game:
            await update.message.reply_text("ℹ️ В этом чате сейчас нет активной игры в Бункер.")
            return
        if game["phase"] != "lobby":
            await update.message.reply_text(
                "⚠️ Игра уже началась, расформировать лобби нельзя. Чтобы прервать саму игру, используйте <code>!бункер стоп</code>.",
                parse_mode=ParseMode.HTML
            )
            return
        del bunker_and_agent.BUNKER_GAMES[update.message.chat_id]
        await update.message.reply_text("🗑 Лобби Бункера расформировано, игра не состоится.")
        return

    # !афк бункер @юз / реплаем — исключить неактивного игрока из лобби или из уже идущей игры
    if text.startswith("!афк бункер"):
        min_rank = db_get_command_rank("бункер_афк")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(
                f"⛔ Отказано в доступе. Ваш ранг: {format_rank(current_rank)}. Требуется ранг: {format_rank(min_rank)}."
            )
            return
        arg = raw_text[len("!афк бункер"):].strip()
        resolved = await resolve_target_user(update, context, arg if arg else None)
        if resolved is None:
            await update.message.reply_text(
                "⚠️ Укажи участника: ответом на его сообщение, через @юзернейм или ID.\n"
                "Например: <code>!афк бункер @username</code>",
                parse_mode=ParseMode.HTML
            )
            return
        target_id, target_username, target_full_name = resolved
        ok, info = await bunker_and_agent.kick_afk_player(update.message.chat_id, target_id, context)
        if not ok:
            await update.message.reply_text(f"⚠️ {info}")
            return
        if info == "lobby":
            await update.message.reply_text(
                f"🚪 {format_user_link(target_id, target_username, target_full_name)} удалён(а) из лобби Бункера за афк.",
                parse_mode=ParseMode.HTML
            )
        return

    if text.startswith("!бункер"):
        await bunker_and_agent.command_bunker_start(update, context)
        return

    if text.startswith("!мафия стоп") or text.startswith("!мафия отмена"):
        game = mafia.MAFIA_GAMES.get(update.message.chat_id)
        if not game:
            await update.message.reply_text("ℹ️ В этом чате сейчас нет активной игры в Мафию.")
            return
        if not is_creator and current_rank < 6 and user_id != game["host_id"]:
            await update.message.reply_text("⛔ Остановить игру может только создатель лобби или админ.")
            return
        mafia.cancel_pending_timer(game)
        del mafia.MAFIA_GAMES[update.message.chat_id]
        await update.message.reply_text("🛑 Игра в Мафию остановлена.")
        return

    if text.startswith("!мафия"):
        await mafia.command_mafia_start(update, context)
        return

    if text.startswith("!войс") or text.startswith("/войс") or text.startswith("!voic") or text.startswith("/voic"):
        await command_voic(update, context)
        return
    
    if text.startswith("!флаг") or text.startswith("/checkflag"):
        if not is_creator and current_rank < 6:
            await update.message.reply_text(f"⛔ Эта команда доступна только {format_rank(6)}.")
            return
        await update.message.reply_text(
            f"Флаг форс-опроса: {FORCED_ATTENDANCE_ACTIVE}.\n"
            f"Причина отмены: {escape(CANCELLED_POLL_REASON) if CANCELLED_POLL_REASON else 'не указана'}.\n"
            f"Режим «Стоп Срач» активен: {SRACH_LOCK_ACTIVE}\n"
            f"Таймер «Стоп Срач»: {SRACH_LOCK_JOB.next_t if SRACH_LOCK_JOB else 'не установлен'}\n"
        )
        return
    
# ---------- Обработка нажатий на кнопки подтверждения обнуления ----------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    current_rank = db_get_user_rank(user_id)

    # Проверка прав: только создатель или ранг 6 могут обнулять
    if query.from_user.id != 8049751536 and current_rank < 6:
        await query.answer(f"⛔ Вы не являетесь создателем ({format_rank(6)}), вам нельзя нажимать эту кнопку!", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data.startswith("reset_yes:"):
        target_id = int(data.split(":")[1])
        display_name = f"ID: {target_id}"
        
        # Пытаемся получить красивое имя из локальной БД для финального сообщения
        try:
            import sqlite3
            with sqlite3.connect(config.DB_PATH) as conn:
                cur = conn.execute("SELECT username, full_name FROM users WHERE user_id = ? LIMIT 1", (target_id,))
                row = cur.fetchone()
                if row:
                    display_name = format_user_link(target_id, row[0], row[1])
        except Exception:
            pass

        # Вызываем функцию обнуления из вашей БД
        db_reset_user_stats(target_id)
        await query.edit_message_text(
            text=f"🔄 Статистика пользователя {display_name} была полностью обнулена администратором.",
            parse_mode=ParseMode.HTML
        )

    elif data.startswith("reset_no:"):
        await query.edit_message_text(text="❌ Операция обнуления была отменена.")  
async def show_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m_rows = db_top_by_messages(10)
    w_rows = db_top_by_walks(10)
    if not m_rows and not w_rows:
        await update.message.reply_text("Пока нет данных для статистики.")
        return

    lines = ["💬 Карма за общение:\n"]
    for i, (uid, un, fn, msgs, score) in enumerate(m_rows, start=1):
        lines.append(f"{i}. {format_user_link(uid, un, fn)} — {score} очков ({msgs} сообщ.)")
    
    lines.append("\n🚶 Карма за прогулки:\n")
    for i, (uid, un, fn, walks, score) in enumerate(w_rows, start=1):
        lines.append(f"{i}. {format_user_link(uid, un, fn)} — {score} очков | Прогулок: {walks}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id, target_username, target_full_name):
    """Показывает 'инфу' (профиль) любого участника — себя или другого."""
    stats = db_get_user_stats(target_id)
    if not stats:
        await update.message.reply_text("ℹ️ У этого пользователя пока нет статистики.")
        return
    _, un, fn, msgs, m_score, walks, w_karma, rank, inactive = stats
    is_creator = (target_id == 8049751536)
    nickname, status_text, birthday = db_get_profile_extra(target_id)
    display_name = format_user_link(target_id, un or target_username, fn or target_full_name)

    text = (
        f"ℹ️ <b>Инфа:</b> {display_name}\n"
        f"Ранг доступа: {format_rank(rank, is_creator)}\n"
        f"Дней молчания: {inactive}\n"
    )
    if status_text:
        text += f"💭 Статус: <i>{escape(status_text)}</i>\n"
    if birthday:
        text += f"🎂 День рождения: {birthday}\n"
    text += (
        f"\n💬 Карма за общение: {m_score} очков ({msgs} сообщ.)\n"
        f"🚶 Карма за прогулки: {w_karma} очков (Прогулок: {walks})"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

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

async def show_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    min_rank = db_get_command_rank("уровень")
    current_rank = db_get_user_rank(user.id)
    is_creator = (user.id == 8049751536)
    if not is_creator and current_rank < min_rank:
        await update.message.reply_text("⛔ Недостаточно прав для этой команды.")
        return

    stats = db_get_user_stats(user.id)
    if not stats:
        await update.message.reply_text("У вас пока нет статистики.")
        return

    _, un, fn, msgs, m_score, walks, w_karma, rank, inactive = stats
    total_score = m_score + w_karma
    level, into_level, span, progress = compute_level(total_score)

    filled = round(progress * 10)
    bar = "🟦" * filled + "⬜" * (10 - filled)

    text = (
        f"🧬 <b>Уровень для {format_user_link(user.id, un, fn)}</b>\n\n"
        f"⭐ Текущий уровень: <b>{level}</b>\n"
        f"{bar} {into_level}/{span} очков до след. уровня\n\n"
        f"💬 Карма за общение: {m_score}\n"
        f"🚶 Карма за прогулки: {w_karma}\n"
        f"🎖 Ранг доступа: {format_rank(rank, is_creator)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def show_my_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db_get_user_stats(user.id)
    if not stats:
        await update.message.reply_text("У вас пока нет статистики.")
        return
    _, un, fn, msgs, m_score, walks, w_karma, rank, inactive = stats
    is_creator = (user.id == 8049751536)
    nickname, status_text, birthday = db_get_profile_extra(user.id)
    text = (
        f"📊 Статистика для {format_user_link(user.id, un, fn)}:\n"
        f"Ранг доступа: {format_rank(rank, is_creator)}\n"
        f"Дней молчания: {inactive}\n"
    )
    if status_text:
        text += f"💭 Статус: <i>{escape(status_text)}</i>\n"
    if birthday:
        text += f"🎂 День рождения: {birthday}\n"
    text += (
        f"\n💬 Карма за общение: {m_score} очков ({msgs} сообщ.)\n"
        f"🚶 Карма за прогулки: {w_karma} очков (Прогулок: {walks})"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- Цепочка автоматических опросов ----------
async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Отправляет ежедневный опрос.
    Возвращает True, если опрос успешно создан и записан в БД.
    Возвращает False, если опрос был отменен администрацией.
    """
    global CANCELLED_POLL_REASON, FORCED_ATTENDANCE_ACTIVE
    
    # Если опрос отменили или зафорсили заранее — скипаем создание стандартного опроса
    if FORCED_ATTENDANCE_ACTIVE or CANCELLED_POLL_REASON is not None:
        if CANCELLED_POLL_REASON is not None:
            try:
                await context.bot.send_message(
                    chat_id=config.MAIN_GROUP_CHAT_ID,
                    text=f"📢 Напоминание: плановый опрос мест на сегодня отменен.\n📍 <b>Место прогулки определено заранее администрацией:</b> {escape(CANCELLED_POLL_REASON)}",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                print(f"Ошибка отправки отмены опроса: {e}", file=sys.stderr)
                
        # Сбрасываем флаги для следующего дня
        CANCELLED_POLL_REASON = None
        FORCED_ATTENDANCE_ACTIVE = False 
        print("Флаг опроса сброшен")
        return False

    try:
        # Создаём опрос в телеграме
        message = await context.bot.send_poll(
            chat_id=config.MAIN_GROUP_CHAT_ID,
            question="🌆 Где завтра гуляем?",
            options=config.POLL_OPTIONS,
            is_anonymous=False,
            allows_multiple_answers=True,
            protect_content=True
        )
        
        # БЕЗОПАСНЫЙ ПОИСК ИНДЕКСА: если "Не гуляю" нет в списке, ставим -1, чтобы не падать
        try:
            not_walking_idx = config.POLL_OPTIONS.index("Не гуляю")
        except ValueError:
            not_walking_idx = -1  # Дефолтное значение, если элемент не найден
        
        # Запись в базу данных
        await asyncio.to_thread(
            db_save_poll,
            poll_id=message.poll.id,
            poll_type="place",
            message_id=message.message_id,
            chat_id=str(config.MAIN_GROUP_CHAT_ID),
            options_json=json.dumps(config.POLL_OPTIONS),
            not_walking_index=not_walking_idx
        )
        
        # Пробуем закрепить сообщение
        try:
            await context.bot.pin_chat_message(chat_id=config.MAIN_GROUP_CHAT_ID, message_id=message.message_id)
        except Exception as e:
            print(f"Ошибка закрепления в 20:00: {e}", file=sys.stderr)
        print("✅ Успешно отправлен опрос в 20:00. ID опроса:", message.poll.id)
        return True

    except Exception as fatal_e:
        # Ловим любые критические ошибки (например, если чат не найден), чтобы таска не падала молча
        print(f"🚨 Критическая ошибка в send_daily_poll: {fatal_e}", file=sys.stderr)
        return False

async def close_place_and_start_attendance(context: ContextTypes.DEFAULT_TYPE):
    # Убираем проверку глобального флага FORCED_ATTENDANCE_ACTIVE здесь, 
    # так как отмена/форс теперь полностью контролируются на этапе создания опроса в 20:00.

    job_data = context.job.data
    target_chat_id = job_data["chat_id"]
    place_poll_msg_id = job_data["message_id"]
    place_poll_id = job_data["poll_id"]

    try:
        # 1. Сразу пробуем открепить старый опрос
        try:
            await context.bot.unpin_chat_message(chat_id=target_chat_id, message_id=place_poll_msg_id)
        except Exception:
            pass

        # 2. Останавливаем опрос в Telegram
        stopped_poll = await context.bot.stop_poll(chat_id=target_chat_id, message_id=place_poll_msg_id)
        
        # 3. Читаем данные старого опроса из БД в отдельном потоке (асинхронно)
        poll_data = await asyncio.to_thread(db_get_poll, place_poll_id)
        if not poll_data:
            print(f"Ошибка: Не нашли опрос {place_poll_id} в базе данных!", file=sys.stderr)
            return

        options = json.loads(poll_data[3])
        not_walking_idx = poll_data[4]

        max_votes = 0  # Считаем от 0, чтобы ловить варианты, где есть реальные голоса
        winning_place = "Никто не проголосовал 🤷‍♂️"
        has_votes = False

        # 4. Считаем результаты
        for option in stopped_poll.options:
            try:
                idx = options.index(option.text)
            except ValueError:
                continue
            
            # Пропускаем вариант "Не гуляю"
            if idx == not_walking_idx:
                continue
                
            if option.voter_count > 0:
                has_votes = True
                
            if option.voter_count > max_votes:
                max_votes = option.voter_count
                winning_place = option.text
        
        # Если голоса были, но вышло равенство при 0 результатов (никто не выбрал места кроме "Не гуляю")
        if not has_votes:
            winning_place = "Никто не выбрал место 🤷‍♂️"

        # 5. Публикуем итоги
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"🎰 Итоги голосования! Выбрано место: <b>{winning_place}</b>. Запускаю опрос посещаемости...",
            parse_mode=ParseMode.HTML
        )
        
        # 6. Отправляем новый опрос на посещаемость
        attendance_options = ["Да, гуляю", "Нет, не гуляю", "Еще подумаю"]
        attendance_msg = await context.bot.send_poll(
            chat_id=target_chat_id,
            question=f"📍 Место: {winning_place}\nКто придет?",
            options=attendance_options,
            is_anonymous=False,
            allows_multiple_answers=False,
            protect_content=True
        )
        
        # 7. Закрепляем новый опрос
        try:
            await context.bot.pin_chat_message(chat_id=target_chat_id, message_id=attendance_msg.message_id)
        except Exception:
            pass
            
        # 8. Записываем новый опрос в БД в отдельном потоке (асинхронно)
        await asyncio.to_thread(
            db_save_poll,
            poll_id=attendance_msg.poll.id,
            poll_type="attendance",
            message_id=attendance_msg.message_id,
            chat_id=str(target_chat_id),
            options_json=json.dumps(attendance_options),
        )
        print(f"✅ Успешно обработан опрос в 13:00. Место: {winning_place}, ID нового опроса: {attendance_msg.poll.id}")
    except Exception as e:
        print(f"Ошибка при обработке цепочки опросов в 13:00: {e}", file=sys.stderr)

# ---------- Обработка голосов ----------
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_info = db_get_poll(answer.poll_id)
    if not poll_info:
        return

    poll_type, message_id, chat_id, options_json, not_walking_index = poll_info
    user = answer.user
    
    db_log_message(user.id, user.username, user.full_name, is_command=True)

    old_chosen = db_get_user_last_vote(user.id, answer.poll_id)
    new_chosen = answer.option_ids
    db_update_user_vote(user.id, answer.poll_id, new_chosen)

    options = json.loads(options_json)

    if poll_type in ("place", "test_place"):
        for idx in old_chosen:
            if idx not in new_chosen:
                if not_walking_index is not None and idx == not_walking_index:
                    db_change_walk_karma(user.id, 1) 
                else:
                    db_change_walk_karma(user.id, -3)

        for idx in new_chosen:
            if idx not in old_chosen:
                if not_walking_index is not None and idx == not_walking_index:
                    db_change_walk_karma(user.id, -1)
                else:
                    db_change_walk_karma(user.id, 3)

        if poll_type == "test_place" and new_chosen:
            selected_option = options[new_chosen[0]]
            try:
                await context.bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
                await context.bot.stop_poll(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🧪 Тест: {format_user_link(user.id, user.username, user.full_name)} выбрал вариант [{selected_option}]. Запускаю опрос посещаемости...",
                parse_mode=ParseMode.HTML
            )
            
            attendance_options = ["Да, гуляю", "Нет, не гуляю", "Еще подумаю"]
            attendance_msg = await context.bot.send_poll(
                chat_id=chat_id,
                question=f"📍 Тестовое место: {selected_option}\nКто придет?",
                options=attendance_options,
                is_anonymous=False,
                allows_multiple_answers=False,
                protect_content=True
            )
            try:
                await context.bot.pin_chat_message(chat_id=chat_id, message_id=attendance_msg.message_id)
            except Exception:
                pass
            db_save_poll(poll_id=attendance_msg.poll.id, poll_type="attendance", message_id=attendance_msg.message_id, chat_id=str(chat_id), options_json=json.dumps(attendance_options))

    elif poll_type == "attendance":
        if old_chosen and (not new_chosen or new_chosen[0] != old_chosen[0]):
            old_idx = old_chosen[0]
            old_choice_text = options[old_idx].lower()
            
            if "да" in old_choice_text or "буду" in old_choice_text:
                db_revert_walk_attendance(user.id)
            elif "нет" in old_choice_text or "не буду" in old_choice_text:
                db_change_walk_karma(user.id, 1)
            elif "думаю" in old_choice_text:
                pass

        if new_chosen:
            new_idx = new_chosen[0]
            user_choice_text = options[new_idx].lower()

            if "да" in user_choice_text or "буду" in user_choice_text:
                db_apply_walk_attendance(user.id)  
            elif "нет" in user_choice_text or "не буду" in user_choice_text:
                db_change_walk_karma(user.id, -1)  
            elif "думаю" in user_choice_text:
                pass  

# ---------- Крон-задачи контроля активности ----------
from telegram.error import NetworkError, TimedOut, RetryAfter

async def daily_activity_check(context: ContextTypes.DEFAULT_TYPE):
    db_increment_inactivity()
    users = db_get_all_users()
    
    for uid, username, full_name, days_inactive in users:
        if days_inactive > 0 and days_inactive % config.PING_INTERVAL_DAYS == 0:
            
            # 1. Безопасная проверка: в чате ли еще юзер
            try:
                member = await context.bot.get_chat_member(chat_id=config.MAIN_GROUP_CHAT_ID, user_id=uid)
                if member.status in ["left", "kicked"]:
                    continue
            except (NetworkError, TimedOut):
                # Если упала сеть на проверке — не падаем, просто попробуем в следующий раз
                print(f"⚠️ Сетевая ошибка при проверке пользователя {uid}, пропускаем...", file=sys.stderr)
                continue
            except Exception:
                continue

            silent_mention = format_user_link(uid, username, full_name)
            display_name = escape(full_name or username or str(uid))
            
            # 2. Безопасная отправка сообщения
            try:
                await context.bot.send_message(
                    chat_id=config.MAIN_GROUP_CHAT_ID,
                    text=f"{silent_mention}🔔 Эй, {display_name}, ты молчишь уже {days_inactive} дня(ней)! Напиши хоть точку, чтобы остаться в группе.",
                    parse_mode=ParseMode.HTML
                )
                await asyncio.sleep(0.1) # Защита от флуда
                
            except RetryAfter as e:
                # Если Телеграм просит притормозить (Flood Control)
                await asyncio.sleep(e.retry_after)
            except (NetworkError, TimedOut) as net_err:
                # ВОТ ТУТ МЫ ЛОВИМ ТОТ САМЫЙ BAD GATEWAY / TIMEOUT
                print(f"📡 Ошибка сети Telegram ({net_err}) при пинге {uid}. Пропускаем.", file=sys.stderr)
                # Даем сети «отдохнуть» пару секунд перед следующим юзером
                await asyncio.sleep(2) 
            except Exception as e:
                print(f"Ошибка отправки пинга для {uid}: {e}", file=sys.stderr)

async def daily_birthday_check(context: ContextTypes.DEFAULT_TYPE):
    """Поздравляет всех, у кого сегодня день рождения (проверка по формату ДД.ММ)"""
    today_str = datetime.now(config.KYIV_TZ).strftime("%d.%m")
    birthday_people = db_get_todays_birthdays(today_str)

    for uid, username, full_name, nickname in birthday_people:
        display_name = escape(nickname or full_name or username or str(uid))
        try:
            msg = await context.bot.send_message(
                chat_id=config.MAIN_GROUP_CHAT_ID,
                text=f"🎉🎂 Сегодня день рождения у {format_user_link(uid, username, full_name)}!\nПоздравляем, {display_name}! 🥳🎁",
                parse_mode=ParseMode.HTML
            )
            try:
                await context.bot.pin_chat_message(chat_id=config.MAIN_GROUP_CHAT_ID, message_id=msg.message_id)
            except Exception:
                pass
        except Exception as e:
            print(f"Ошибка при поздравлении с ДР ({uid}): {e}", file=sys.stderr)
import asyncio

async def command_voic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_creator = (user.id == 8049751536)
    current_rank = db_get_user_rank(user.id)
    is_private = update.message.chat.type == "private"

    if is_private:
        # В личке команда доступна только Главе/Тех.админу
        if not is_creator and current_rank < 6:
            return
    else:
        if str(update.message.chat_id) != config.MAIN_GROUP_CHAT_ID:
            return
        # Ограничение доступа в группе (Создатель или Ранг >= 5)
        if not is_creator and current_rank < 5:
            await update.message.reply_text("⛔ У тебя нет доступа к этой команде.")
            return

    raw_text = update.message.text or ""

    # Автоматически определяем длину команды, чтобы корректно отрезать её от текста
    first_word = raw_text.split()[0].lower() if raw_text.split() else ""
    rest = raw_text[len(first_word):].strip()

    # Определяем голос: первое слово после команды может быть именем голоса из VOICE_LIBRARY
    voice_key = config.DEFAULT_VOICE_KEY
    text = rest
    rest_parts = rest.split(maxsplit=1)
    if rest_parts and rest_parts[0].lower() in config.VOICE_LIBRARY:
        voice_key = rest_parts[0].lower()
        text = rest_parts[1] if len(rest_parts) > 1 else ""

    if not text:
        voice_list = ", ".join(config.VOICE_LIBRARY.keys())
        await update.message.reply_text(
            f"✅ Использование:\n<code>!войс Твой текст здесь</code> (голос по умолчанию)\n"
            f"<code>!войс [имя] Твой текст здесь</code>\n\n"
            f"Доступные голоса: {voice_list}",
            parse_mode=ParseMode.HTML
        )
        return

    if len(text) > 600:
        await update.message.reply_text("❌ Слишком длинный текст (макс 600 символов).")
        return

    voice_info = config.VOICE_LIBRARY[voice_key]
    status_msg = await update.message.reply_text(f"🎙 Генерирую голос {voice_info['label']}...")
    tmp_path = None

    # Куда отправлять готовое голосовое: если команда из ЛС — всегда в основную группу,
    # если из группы — в тот же чат (реплаем на сообщение отправителя)
    target_chat_id = config.MAIN_GROUP_CHAT_ID if is_private else update.message.chat_id
    reply_to = None if is_private else update.message.message_id

    try:
        # Используем обычный FishAudio, который точно есть в библиотеке
        client = FishAudio(api_key=config.FISH_API_KEY)
        
        # Запускаем синхронную генерацию в отдельном потоке, чтобы бот не зависал
        audio = await asyncio.to_thread(
            client.tts.convert,
            text=text,
            reference_id=voice_info["id"],
            model="s2.1-pro-free"
        )
        
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            save(audio, tmp_file.name)
            tmp_path = tmp_file.name

        with open(tmp_path, 'rb') as voice:
            await context.bot.send_voice(
                chat_id=target_chat_id,
                voice=voice,
                reply_to_message_id=reply_to
            )
        
        if is_private:
            await status_msg.edit_text("✅ Отправлено в группу.")
        else:
            await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при генерации голоса:\n<code>{escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        print(f"[VOIC ERROR] {e}")
        
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as e:
                print(f"[VOIC CLEANUP ERROR] Не удалось удалить файл {tmp_path}: {e}")

async def test_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_rank = db_get_user_rank(user_id)
    min_rank = db_get_command_rank("тест_опрос")

    if user_id != 8049751536 and current_rank < min_rank:
        await update.message.reply_text(f"⛔ Отказано в доступе. Ваш ранг: {format_rank(current_rank)}. Требуется ранг: {format_rank(min_rank)}.")
        return

    message = await update.message.reply_poll(
        question="🧪 [ТЕСТ] Где сегодня гуляем?",
        options=config.POLL_OPTIONS,
        is_anonymous=False,
        allows_multiple_answers=False
    )
    
    try:
        await context.bot.pin_chat_message(chat_id=update.message.chat_id, message_id=message.message_id)
    except Exception:
        pass
    
    try:
        not_walking_idx = config.POLL_OPTIONS.index("Не гуляю")
    except ValueError:
        not_walking_idx = None

    db_save_poll(
        poll_id=message.poll.id,
        poll_type="test_place",
        message_id=message.message_id,
        chat_id=str(update.message.chat_id),
        options_json=json.dumps(config.POLL_OPTIONS),
        not_walking_index=not_walking_idx
    )





async def poll_job_wrapper(ctx):
    # Запускаем отправку опроса и сохраняем результат (True или False)
    poll_created = await send_daily_poll(ctx)
    
    # Если send_daily_poll вернула False (был скип), то ничего на завтра не планируем
    if not poll_created:
        return
        
    # Считаем время корректно, защищаясь от багов с переходом на летнее/зимнее время
    # Если мы дошли до сюда, значит опрос создался. Планируем закрытие на завтра в 13:00.
    now = datetime.now(config.KYIV_TZ)
    tomorrow = now + timedelta(days=1)
    tomorrow_13 = datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, 13, 0, 0
    ).astimezone(config.KYIV_TZ)
    
    # Берем ID только что созданного опроса из базы (в отдельном потоке)
    row = await asyncio.to_thread(db_get_last_poll)
        
    if row:
        ctx.job_queue.run_once(
            close_place_and_start_attendance,
            when=tomorrow_13,
            data={"poll_id": row[0], "message_id": row[1], "chat_id": config.MAIN_GROUP_CHAT_ID}
        )
    else:
        print("Ошибка: Опрос был создан, но не найден в БД для планирования закрытия!", file=sys.stderr)
# Регистрация ежедневной таски (тут всё ок)


# ---------- Главная функция инициализации ----------
def main():
    db_init()
    init_votes_tracking()
    bunker_and_agent.init_agent_db()
    extra_features.init_extra_features_db()
    app = Application.builder().token(config.TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["karma", "top"], show_karma))
    app.add_handler(CommandHandler("mykarma", show_my_karma))
    app.add_handler(CommandHandler("test_poll", test_poll_command))
    app.add_handler(CommandHandler("voic", command_voic))

    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.UpdateType.EDITED, handle_private_message))
    # 1. Самый первый хэндлер — проверяет режим «Стоп Срач» для ВСЕХ сообщений
    app.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.UpdateType.EDITED, watch_srach_lock),
        group=0
    )

    # 2. Твой старый хэндлер для команд (теперь он будет спать спокойно во время срача)
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"^[!+]") & filters.ChatType.GROUPS & ~filters.UpdateType.EDITED, handle_text_command),
        group=1
    )
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.UpdateType.EDITED, handle_group_message), group=1) 

    # 3. Отдельная группа — отслеживание реплаев для фичи "Тайный агент" (не мешает командам выше)
    app.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.GROUPS & filters.REPLY & ~filters.UpdateType.EDITED, bunker_and_agent.watch_agent_replies),
        group=2
    )

    # Кнопки игры "Бункер" (bj/bs/bv) должны быть доступны ВСЕМ игрокам, а не только рангу 6+,
    # поэтому регистрируем их ПЕРЕД общим handle_callback_query (внутри группы срабатывает первый совпавший хэндлер)
    app.add_handler(CallbackQueryHandler(bunker_and_agent.handle_bunker_callback, pattern=r"^(bj|bs|bv|bc|ba|bu|bt|bf|bg):"))
    app.add_handler(CallbackQueryHandler(mafia.handle_mafia_callback, pattern=r"^(mj|ms|mr|mk|md|mc|mv):"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    jq = app.job_queue
    


    jq.run_daily(poll_job_wrapper, time=dtime(hour=20, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(daily_activity_check, time=dtime(hour=10, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(daily_birthday_check, time=dtime(hour=9, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(funmodule.daily_balabol_check, time=dtime(hour=22, minute=00, tzinfo=config.KYIV_TZ))

    # "Тайный агент": пары назначаются в понедельник, награда подводится в воскресенье вечером
    jq.run_daily(
        bunker_and_agent.weekly_agent_pairing_job,
        time=dtime(hour=9, minute=0, tzinfo=config.KYIV_TZ),
        days=(0,)
    )
    jq.run_daily(
        bunker_and_agent.weekly_agent_reward_job,
        time=dtime(hour=21, minute=0, tzinfo=config.KYIV_TZ),
        days=(6,)
    )
    print("Бот успешно запущен.")
    app.run_polling()

    
if __name__ == "__main__":
    main()

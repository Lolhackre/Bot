import sqlite3
import sys
import json
from datetime import time as dtime, datetime, timedelta
from html import escape

import funmodule

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
)

import config
from database import (
    db_init, db_log_message, db_get_user_rank, 
    db_save_poll, db_get_poll, 
    db_top_by_messages, db_top_by_walks, db_get_user_stats,
    db_get_all_users, db_increment_inactivity, db_set_user_rank,
    db_get_rank_names, db_get_rank_name, db_set_rank_name,
    db_get_command_rank, db_set_command_rank, db_get_command_ranks,
    db_fix_default_rank_bug
)

# Глобальное состояние для отмены опроса на текущий вечер
CANCELLED_POLL_REASON = None
# Флаг, указывающий, что опрос посещаемости уже запущен вручную через !форс (блокирует авто-опросы)
FORCED_ATTENDANCE_ACTIVE = False

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
    display_name = escape(full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

def format_silent_ping(user_id):
    return f'<a href="tg://user?id={user_id}">&#8288;</a>'

def format_rank(rank, is_creator=False):
    """Возвращает читаемое название ранга, например 'Заместитель (5)'"""
    if is_creator:
        return "Создатель"
    name = db_get_rank_name(rank)
    return f"{name} ({rank})"

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
    is_command = text.startswith("!") or text.startswith("/")

    # Действие словом-триггером БЕЗ "!" — срабатывает только ответом на чье-то сообщение
    if update.message.reply_to_message and stripped_lower in funmodule.ACTIONS:
        is_command = True
        db_log_message(user.id, user.username, user.full_name, is_command=is_command)

        current_rank = db_get_user_rank(user.id)
        is_creator = (user.id == 8049751536)
        min_rank = db_get_command_rank("действие")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав для действий. Требуется ранг {format_rank(min_rank)}+.")
            return

        await funmodule.command_action(update, context, stripped_lower)
        return

    db_log_message(user.id, user.username, user.full_name, is_command=is_command)

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != config.MAIN_GROUP_CHAT_ID:
        return
    
    raw_text = (update.message.text or "").strip()
    text = raw_text.lower()
    user_id = update.effective_user.id
    current_rank = db_get_user_rank(user_id)
    is_creator = (user_id == 8049751536)

    # 1. Справка по командам (!хелп / !помощь)
    if text in ("!хелп", "!помощь"):
        cmd_ranks = db_get_command_ranks()
        help_text = (
            "📖 <b>Справка по командам бота:</b>\n\n"
            "💬 <code>!карма</code> или <code>!топ</code> — Показать топ участников по общению и прогулкам.\n"
            "👤 <code>!моя карма</code> или <code>!моякарма</code> — Показать личную статистику и ваш ранг.\n"
            "❓ <code>!хелп</code> или <code>!помощь</code> — Вызов этого меню.\n"
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

    # NEW: 4. Команда !форс [Место] + перенос строки для причины — Ранг >= 4
    if text.startswith("!форс"):
        min_rank = db_get_command_rank("форс")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}")
            return

        # Разделяем строку по переносу (Shift+Enter), чтобы отделить первую строку от причины
        lines = raw_text.split('\n')
        first_line = lines[0].strip()
        
        # Место берется из первой строки после команды "!форс "
        place_name = first_line[5:].strip()
        if not place_name:
            await update.message.reply_text("⚠️ Используйте формат: <code>!форс Место</code> (и по желанию с новой строки укажите причину)", parse_mode=ParseMode.HTML)
            return

        # Если есть строки ниже — собираем их в причину
        reason_text = None
        if len(lines) > 1:
            reason_text = "\n".join(lines[1:]).strip()

        global FORCED_ATTENDANCE_ACTIVE
        FORCED_ATTENDANCE_ACTIVE = True  # Блокируем автоматическую вечернюю цепочку

        msg_text = f"📍 Администратор утвердил место для сегодняшней прогулки: <b>{escape(place_name)}</b>\n"
        if reason_text:
            msg_text += f"ℹ️ <b>Причина/Инфо:</b> {escape(reason_text)}\n"
        msg_text += "\nЗапускаю опрос посещаемости..."

        await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)

        # Сразу генерируем опрос посещаемости
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

        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            target_id = target_user.id
            display_name = format_user_link(target_user.id, target_user.username, target_user.full_name)
            if len(parts) >= 3:
                try:
                    rank_val = int(parts[2])
                except ValueError:
                    pass
        else:
            if len(parts) >= 4:
                target_param = parts[2]
                try:
                    rank_val = int(parts[3])
                except ValueError:
                    pass
                
                if target_param.startswith("@"):
                    target_id = db_get_user_id_by_username(target_param.lower())
                    display_name = escape(target_param)
                else:
                    try:
                        target_id = int(target_param)
                        display_name = f"ID: {target_id}"
                    except ValueError:
                        target_id = None

        if rank_val is None or not (0 <= rank_val <= 6):
            rank_list = ", ".join(f"{r} — {n}" for r, n in sorted(db_get_rank_names().items()))
            await update.message.reply_text(f"⚠️ Неверный формат ранга. Используйте число от 0 до 6.\nПример: `!сет ранк @username 3` или ответом `!сет ранк 3`.\n\nДоступные ранги: {rank_list}")
            return

        if target_id is None:
            await update.message.reply_text("❌ Не удалось найти пользователя.")
            return

        with sqlite3.connect(config.DB_PATH) as conn:
            cur = conn.execute("SELECT username, full_name FROM users WHERE user_id = ? LIMIT 1", (target_id,))
            row = cur.fetchone()
            if row:
                display_name = format_user_link(target_id, row[0], row[1])

        db_set_user_rank(target_id, rank_val)
        await update.message.reply_text(f"✅ Пользователю {display_name} успешно присвоен ранг {format_rank(rank_val)}", parse_mode=ParseMode.HTML)
        return

    # 6. Команда !обнулить — по умолчанию Ранг 6, настраивается через !доступ
    if text.startswith("!обнулить"):
        min_rank = db_get_command_rank("обнулить")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Эта команда доступна только с ранга {format_rank(min_rank)}.")
            return

        parts = raw_text.split()
        target_id = None
        display_name = None

        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            target_id = target_user.id
            display_name = format_user_link(target_user.id, target_user.username, target_user.full_name)
        else:
            if len(parts) >= 2:
                target_param = parts[1]
                if target_param.startswith("@"):
                    target_id = db_get_user_id_by_username(target_param.lower())
                    display_name = escape(target_param)
                else:
                    try:
                        target_id = int(target_param)
                        display_name = f"ID: {target_id}"
                    except ValueError:
                        target_id = None

        if target_id is None:
            await update.message.reply_text("❌ Не удалось определить пользователя для обнуления. Укажите @username, ID или ответьте на его сообщение.")
            return

        with sqlite3.connect(config.DB_PATH) as conn:
            cur = conn.execute("SELECT username, full_name FROM users WHERE user_id = ? LIMIT 1", (target_id,))
            row = cur.fetchone()
            if row:
                display_name = format_user_link(target_id, row[0], row[1])

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
    # 7. Команда !команда [ранг] [новое название] — по умолчанию Ранг 6, настраивается через !доступ
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

    # 8. Команда !доступ [команда] [ранг] — Ранг 6 (жестко, не настраивается — управляет самой системой прав)
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
        lines = ["🎭 <b>Действия, доступные ответом на сообщение участника:</b>\n"]
        for key, (emoji, _) in funmodule.ACTIONS.items():
            lines.append(f"{emoji} <code>!{key}</code>")
        min_rank = db_get_command_rank("действие")
        lines.append(f"\nℹ️ Ответьте одной из этих команд на сообщение участника. Требуется ранг {format_rank(min_rank)}+.")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # 10. Сами действия (!обнять, !ударить и т.д.) — ответ на сообщение другого участника
    action_key = text[1:] if text.startswith("!") else text
    if action_key in funmodule.ACTIONS:
        min_rank = db_get_command_rank("действие")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Недостаточно прав для действий. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}")
            return
        await funmodule.command_action(update, context, action_key)
        return

    # 11. Команда !исправить ранги — Ранг 6 (жестко). Одноразовая массовая починка бага дефолтного ранга.
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

    # Находим блок с проверками команд в handle_text_command и добавляем туда:
    if text == "!отмазка":
        await funmodule.command_excuse(update, context)
        return

# ---------- Обработка нажатий на кнопки подтверждения обнуления ----------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    current_rank = db_get_user_rank(user_id)

    if query.from_user.id != 8049751536 and current_rank < 6:
        await query.answer(f"⛔ Вы не являетесь создателем ({format_rank(6)}), вам нельзя нажимать эту кнопку!", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data.startswith("reset_yes:"):
        target_id = int(data.split(":")[1])
        display_name = f"ID: {target_id}"
        
        with sqlite3.connect(config.DB_PATH) as conn:
            cur = conn.execute("SELECT username, full_name FROM users WHERE user_id = ? LIMIT 1", (target_id,))
            row = cur.fetchone()
            if row:
                display_name = format_user_link(target_id, row[0], row[1])

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

async def show_my_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db_get_user_stats(user.id)
    if not stats:
        await update.message.reply_text("У вас пока нет статистики.")
        return
    _, un, fn, msgs, m_score, walks, w_karma, rank, inactive = stats
    is_creator = (user.id == 8049751536)
    text = (
        f"📊 Статистика для {format_user_link(user.id, un, fn)}:\n"
        f"Ранг доступа: {format_rank(rank, is_creator)}\n"
        f"Дней молчания: {inactive}\n\n"
        f"💬 Карма за общение: {m_score} очков ({msgs} сообщ.)\n"
        f"🚶 Карма за прогулки: {w_karma} очков (Прогулок: {walks})"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- Цепочка автоматических опросов ----------
async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    global CANCELLED_POLL_REASON, FORCED_ATTENDANCE_ACTIVE
    
    # Если зафорсили опрос или отменили его, плановый опрос мест (20:00) скипается
    if FORCED_ATTENDANCE_ACTIVE or CANCELLED_POLL_REASON is not None:
        if CANCELLED_POLL_REASON is not None:
            await context.bot.send_message(
                chat_id=config.MAIN_GROUP_CHAT_ID,
                text=f"📢 Напоминание: плановый опрос мест на сегодня отменен.\n📍 <b>Место прогулки определено заранее администрацией:</b> {escape(CANCELLED_POLL_REASON)}",
                parse_mode=ParseMode.HTML
            )
        CANCELLED_POLL_REASON = None
        FORCED_ATTENDANCE_ACTIVE = False # Сбрасываем флаг для следующего дня
        return

    message = await context.bot.send_poll(
        chat_id=config.MAIN_GROUP_CHAT_ID,
        question="🌆 Где завтра гуляем?",
        options=config.POLL_OPTIONS,
        is_anonymous=False,
        allows_multiple_answers=True,
        protect_content=True
    )
    not_walking_idx = config.POLL_OPTIONS.index("Не гуляю")
    db_save_poll(
        poll_id=message.poll.id,
        poll_type="place",
        message_id=message.message_id,
        chat_id=str(config.MAIN_GROUP_CHAT_ID),
        options_json=json.dumps(config.POLL_OPTIONS),
        not_walking_index=not_walking_idx
    )
    
    try:
        await context.bot.pin_chat_message(chat_id=config.MAIN_GROUP_CHAT_ID, message_id=message.message_id)
    except Exception as e:
        print(f"Ошибка закрепления в 20:00: {e}", file=sys.stderr)

async def close_place_and_start_attendance(context: ContextTypes.DEFAULT_TYPE):
    # Если администрация зафорсила опрос раньше, дневной автоматический триггер закрытия (13:00) не должен выполняться
    global FORCED_ATTENDANCE_ACTIVE
    if FORCED_ATTENDANCE_ACTIVE:
        FORCED_ATTENDANCE_ACTIVE = False
        return

    job_data = context.job.data
    target_chat_id = job_data["chat_id"]
    place_poll_msg_id = job_data["message_id"]
    place_poll_id = job_data["poll_id"]

    try:
        try:
            await context.bot.unpin_chat_message(chat_id=target_chat_id, message_id=place_poll_msg_id)
        except Exception:
            pass

        stopped_poll = await context.bot.stop_poll(chat_id=target_chat_id, message_id=place_poll_msg_id)
        
        poll_data = db_get_poll(place_poll_id)
        options = json.loads(poll_data[3])
        not_walking_idx = poll_data[4]

        max_votes = -1
        winning_place = "Не определено"

        for option in stopped_poll.options:
            try:
                idx = options.index(option.text)
            except ValueError:
                continue
            if idx == not_walking_idx:
                continue
            if option.voter_count > max_votes:
                max_votes = option.voter_count
                winning_place = option.text

        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"🎰 Итоги голосования! Выбрано место: <b>{winning_place}</b>. Запускаю опрос посещаемости...",
            parse_mode=ParseMode.HTML
        )
        
        attendance_options = ["Да, гуляю", "Нет, не гуляю", "Еще подумаю"]
        attendance_msg = await context.bot.send_poll(
            chat_id=target_chat_id,
            question=f"📍 Место: {winning_place}\nКто придет?",
            options=attendance_options,
            is_anonymous=False,
            allows_multiple_answers=False,
            protect_content=True
        )
        
        try:
            await context.bot.pin_chat_message(chat_id=target_chat_id, message_id=attendance_msg.message_id)
        except Exception:
            pass
            
        db_save_poll(
            poll_id=attendance_msg.poll.id,
            poll_type="attendance",
            message_id=attendance_msg.message_id,
            chat_id=str(target_chat_id),
            options_json=json.dumps(attendance_options),
        )

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
async def daily_activity_check(context: ContextTypes.DEFAULT_TYPE):
    db_increment_inactivity()
    users = db_get_all_users()
    
    for uid, username, full_name, days_inactive in users:
        if days_inactive >= config.MAX_INACTIVE_DAYS:
            try:
                await context.bot.ban_chat_member(chat_id=config.MAIN_GROUP_CHAT_ID, user_id=uid)
                await context.bot.unban_chat_member(chat_id=config.MAIN_GROUP_CHAT_ID, user_id=uid)
                await context.bot.send_message(
                    chat_id=config.MAIN_GROUP_CHAT_ID,
                    text=f"❌ {format_user_link(uid, username, full_name)} был исключен за неактивность в течение {config.MAX_INACTIVE_DAYS} дней.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        elif days_inactive > 0 and days_inactive % config.PING_INTERVAL_DAYS == 0:
            silent_mention = format_silent_ping(uid)
            display_name = escape(full_name or username or str(uid))
            await context.bot.send_message(
                chat_id=config.MAIN_GROUP_CHAT_ID,
                text=f"{silent_mention}🔔 Эй, {display_name}, ты молчишь уже {days_inactive} дня(ней)! Напиши хоть точку, чтобы остаться в группе.",
                parse_mode=ParseMode.HTML
            )

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

# ---------- Главная функция инициализации ----------
def main():
    db_init()
    init_votes_tracking()
    app = Application.builder().token(config.TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["karma", "top"], show_karma))
    app.add_handler(CommandHandler("mykarma", show_my_karma))
    app.add_handler(CommandHandler("test_poll", test_poll_command))

    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^!") & filters.ChatType.GROUPS, handle_text_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, handle_group_message), group=1) 
    
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    jq = app.job_queue
    
    async def poll_job_wrapper(ctx):
        await send_daily_poll(ctx)
        
        now = datetime.now(config.KYIV_TZ)
        tomorrow_13 = (now + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
        
        with sqlite3.connect(config.DB_PATH) as conn:
            cur = conn.execute("SELECT poll_id, message_id FROM polls WHERE poll_type='place' ORDER BY rowid DESC LIMIT 1")
            row = cur.fetchone()
            
        if row:
            ctx.job_queue.run_once(
                close_place_and_start_attendance,
                when=tomorrow_13,
                data={"poll_id": row[0], "message_id": row[1], "chat_id": config.MAIN_GROUP_CHAT_ID}
            )

    jq.run_daily(poll_job_wrapper, time=dtime(hour=20, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(daily_activity_check, time=dtime(hour=4, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(funmodule.daily_balabol_check, time=dtime(hour=22, minute=00, tzinfo=config.KYIV_TZ))
    print("Бот успешно запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()

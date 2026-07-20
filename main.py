import sqlite3
import sys
import json
from datetime import time as dtime, datetime, timedelta
from html import escape

import modulesfolder.bunker as bunker
import extra_features
import modulesfolder.mafia as mafia

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
    db_get_all_users, db_increment_inactivity,
    db_get_command_rank,
    db_get_todays_birthdays,
    db_get_last_poll,db_add_penalty,db_get_penalty,db_get_balance_info,db_get_user_last_vote, db_update_user_vote,
    db_change_walk_karma,db_apply_walk_attendance,db_revert_walk_attendance,
    db_reset_user_stats, db_format_user_link,
    resolve_target_user,format_rank
)

import modulesfolder.actions as actions
import modulesfolder.whois as whois
import modulesfolder.quotes as quotes
import modulesfolder.excuses as excuses
import modulesfolder.compatibility as compatibility
import modulesfolder.balabol as balabol

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



# ---------- Вспомогательные функции ----------







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


    # Действие словом-триггером БЕЗ "!" — ответом на сообщение ИЛИ словом + @юзер (например "ударить @юзер")
    if bare_first in actions.ACTIONS:
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
            
        await actions.command_action(update, context, bare_first, resolved)
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

from commands import dispatch_command

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != config.MAIN_GROUP_CHAT_ID:
        return
    
    raw_text = (update.message.text or "").strip()
    text = raw_text.lower()
    user_id = update.effective_user.id
    current_rank = db_get_user_rank(user_id)
    is_creator = (user_id == 8049751536)


    handled = await dispatch_command(
        update, context, raw_text, text, user_id, current_rank, is_creator
    )
    if handled:
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


    if text == "!отмазка":
        await excuses.command_excuse(update, context)
        return

    if text == "!цитата":
        await quotes.command_quote(update, context)
        return

    if text == "!совместимость":
        await compatibility.command_compatibility(update, context)
        return

    if text.startswith("!суд"):
        await extra_features.command_court(update, context)
        return

    if text in ("!карта", "!карты"):
        await bunker.command_bunker_cards(update, context)
        return

    if text.startswith("!кто из нас"):
        adjective = raw_text[len("!кто из нас"):].strip()
        await whois.command_who_of_us(update, context, adjective)
        return

    if text.startswith("!бункер стоп") or text.startswith("!бункер отмена"):
        game = bunker.BUNKER_GAMES.get(update.message.chat_id)
        if not game:
            await update.message.reply_text("ℹ️ В этом чате сейчас нет активной игры в Бункер.")
            return
        if not is_creator and current_rank < 6 and user_id != game["host_id"]:
            await update.message.reply_text("⛔ Остановить игру может только создатель лобби или админ.")
            return
        del bunker.BUNKER_GAMES[update.message.chat_id]
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
        game = bunker.BUNKER_GAMES.get(update.message.chat_id)
        if not game:
            await update.message.reply_text("ℹ️ В этом чате сейчас нет активной игры в Бункер.")
            return
        if game["phase"] != "lobby":
            await update.message.reply_text(
                "⚠️ Игра уже началась, расформировать лобби нельзя. Чтобы прервать саму игру, используйте <code>!бункер стоп</code>.",
                parse_mode=ParseMode.HTML
            )
            return
        del bunker.BUNKER_GAMES[update.message.chat_id]
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
        ok, info = await bunker.kick_afk_player(update.message.chat_id, target_id, context)
        if not ok:
            await update.message.reply_text(f"⚠️ {info}")
            return
        if info == "lobby":
            await update.message.reply_text(
                f"🚪 {db_format_user_link(target_id, target_username, target_full_name)} удалён(а) из лобби Бункера за афк.",
                parse_mode=ParseMode.HTML
            )
        return

    if text.startswith("!бункер"):
        await bunker.command_bunker_start(update, context)
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
    
# Укажи здесь unique_id твоего штрафного стикера
PENALTY_STICKER_UNIQUE_ID = "AgADpqEAAi642Uo"  

async def handle_penalty_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    # 1. Проверяем, что это именно ТОТ САМЫЙ стикер
    if not message.sticker or message.sticker.file_unique_id != PENALTY_STICKER_UNIQUE_ID:
        return

    # 2. Проверяем права отправителя (например, только создатель или ранг 5+)
    user_id = message.from_user.id
    current_rank = db_get_user_rank(user_id) # Твоя функция рангов
    is_creator = (user_id == 8049751536) # Твоя функция проверки создателя

    if not is_creator and current_rank < 3:
        return # Если выдавать штрафы может только высшая администрация

    # 3. Проверяем, что стикер отправлен В ОТВЕТ на чьё-то сообщение
    if not message.reply_to_message:
        return

    target_user = message.reply_to_message.from_user
    
    # Защита: нельзя выдавать штраф ботам
    if target_user.is_bot:
        return

    # 4. Начисляем штраф в 500 единиц (сначала съедает баланс, если он был, потом уходит в штраф)
    db_add_penalty(target_user.id, 500)
    total_penalty, total_balance = db_get_balance_info(target_user.id)

    if total_penalty > 0:
        balance_line = f"⚠️ Общая сумма штрафов: <b>{total_penalty:,}</b>".replace(",", " ")
    elif total_balance > 0:
        balance_line = f"💰 Штраф погашен балансом, остаток баланса: <b>{total_balance:,}</b>".replace(",", " ")
    else:
        balance_line = "✅ Штрафов и баланса нет (0)."

    # 5. Выдаём красивый ответ в чат
    target_link = message.reply_to_message.from_user.mention_html()
    await message.reply_text(
        f"🚨 <b>ВЫПИСАН ШТРАФ!</b>\n"
        f"Участнику {target_link} начислен штраф в размере <b>500 грн/руб/очков</b>!\n"
        f"{balance_line}",
        parse_mode=ParseMode.HTML
    )
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
                    display_name = db_format_user_link(target_id, row[0], row[1])
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
                text=f"🧪 Тест: {db_format_user_link(user.id, user.username, user.full_name)} выбрал вариант [{selected_option}]. Запускаю опрос посещаемости...",
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

            silent_mention = db_format_user_link(uid, username, full_name)
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
                text=f"🎉🎂 Сегодня день рождения у {db_format_user_link(uid, username, full_name)}!\nПоздравляем, {display_name}! 🥳🎁",
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
    bunker.init_agent_db()
    extra_features.init_extra_features_db()
    app = Application.builder().token(config.TOKEN).build()

    app.add_handler(CommandHandler("start", start))
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
        MessageHandler(filters.TEXT & filters.ChatType.GROUPS & filters.REPLY & ~filters.UpdateType.EDITED, bunker.watch_agent_replies),
        group=2
    )

    app.add_handler(
        MessageHandler(filters.Sticker.ALL & filters.ChatType.GROUPS, handle_penalty_sticker),
        group=0
    )

    # Кнопки игры "Бункер" (bj/bs/bv) должны быть доступны ВСЕМ игрокам, а не только рангу 6+,
    # поэтому регистрируем их ПЕРЕД общим handle_callback_query (внутри группы срабатывает первый совпавший хэндлер)
    app.add_handler(CallbackQueryHandler(bunker.handle_bunker_callback, pattern=r"^(bj|bs|bv|bc|ba|bu|bt|bf|bg):"))
    app.add_handler(CallbackQueryHandler(mafia.handle_mafia_callback, pattern=r"^(mj|ms|mr|mk|mm|mg|md|mc|mv):"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(PollAnswerHandler(handle_poll_answer))


    jq = app.job_queue
    


    jq.run_daily(poll_job_wrapper, time=dtime(hour=20, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(daily_activity_check, time=dtime(hour=10, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(daily_birthday_check, time=dtime(hour=9, minute=0, tzinfo=config.KYIV_TZ))
    jq.run_daily(balabol.daily_balabol_check, time=dtime(hour=22, minute=00, tzinfo=config.KYIV_TZ))

    # "Тайный агент": пары назначаются в понедельник, награда подводится в воскресенье вечером
    jq.run_daily(
        bunker.weekly_agent_pairing_job,
        time=dtime(hour=9, minute=0, tzinfo=config.KYIV_TZ),
        days=(0,)
    )
    jq.run_daily(
        bunker.weekly_agent_reward_job,
        time=dtime(hour=21, minute=0, tzinfo=config.KYIV_TZ),
        days=(6,)
    )
    print("Бот успешно запущен.")
    app.run_polling()

    
if __name__ == "__main__":
    main()

# commands/profile.py
from html import escape
from telegram import Update
from telegram.ext import ContextTypes
import config
from telegram.constants import ParseMode
from datetime import time as dtime, datetime, timedelta
from database import db_get_command_rank,format_rank,db_get_user_rank,db_set_nickname, db_set_status, db_set_birthday, resolve_target_user,db_module_enabled_get, db_get_user_stats, db_get_profile_extra, db_get_penalty, db_format_user_link, compute_level
# Импортируй тут нужные функции базы данных и форматирования

async def show_profile(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    target_id=None, 
    target_username=None, 
    target_full_name=None
):
    """Показывает полную карточку 'инфа' (профиль + уровень) себя или другого участника."""
    message = update.message
    sender = update.effective_user

    # 1. Проверка прав на команду
    min_rank = db_get_command_rank("инфа")
    sender_rank = db_get_user_rank(sender.id)
    is_sender_creator = (sender.id == 8049751536)

    if not is_sender_creator and sender_rank < min_rank:
        await message.reply_text("⛔ Недостаточно прав для этой команды.")
        return

    # 2. Если target_id не передан из внешнего хэндлера — определяем сами
    if target_id is None:
        if message.reply_to_message:
            reply_user = message.reply_to_message.from_user
            target_id = reply_user.id
            target_username = reply_user.username
            target_full_name = reply_user.full_name
        else:
            target_id = sender.id
            target_username = sender.username
            target_full_name = sender.full_name

    # 3. Получаем статистику из базы данных
    stats = db_get_user_stats(target_id)
    if not stats:
        await message.reply_text("ℹ️ У этого пользователя пока нет статистики.")
        return

    _, un, fn, msgs, m_score, walks, w_karma, rank, inactive = stats
    is_target_creator = (target_id == 8049751536)

    # 4. Расчет уровня и прогресс-бара
    total_score = m_score + w_karma
    level, into_level, span, progress = compute_level(total_score)
    filled = round(progress * 10)
    bar = "🟦" * filled + "⬜" * (10 - filled)

    # 5. Доп. данные (профиль, штрафы)
    nickname, status_text, birthday = db_get_profile_extra(target_id)
    user_penalty = db_get_penalty(target_id)
    
    display_name = db_format_user_link(target_id, un or target_username, fn or target_full_name)

    # 6. Сборка единого текста
    text = (
        f"ℹ️ <b>Инфа:</b> {display_name}\n"
        f"🎖 Ранг доступа: {format_rank(rank, is_target_creator)}\n"
        f"😴 Дней молчания: {inactive}\n"
    )

    if status_text:
        text += f"💭 Статус: <i>{escape(status_text)}</i>\n"
    if birthday:
        text += f"🎂 День рождения: {birthday}\n"

    text += (
        f"\n⭐ Текущий уровень: <b>{level}</b>\n"
        f"{bar} {into_level}/{span} очков до след. уровня\n\n"
        f"💬 Карма за общение: {m_score} очков ({msgs} сообщ.)\n"
        f"🚶 Карма за прогулки: {w_karma} очков ({walks} прог.)\n"
        f"💸 <b>Штрафы:</b> {user_penalty:,} грн\n"
    ).replace(",", " ")

    await message.reply_text(
        text, 
        parse_mode=ParseMode.HTML, 
        disable_web_page_preview=True
    )

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

async def handle_profile_commands(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    raw_text: str, 
    text: str, 
    user_id: int, 
    current_rank: int, 
    is_creator: bool
) -> bool:
    
    # +ник
    if text.startswith("+ник"):
        new_nick = raw_text[4:].strip()
        if not new_nick:
            db_set_nickname(user_id, None)
            await update.message.reply_text("✅ Кастомный ник сброшен, будет отображаться обычное имя.")
            return True
        if len(new_nick) > 32:
            await update.message.reply_text("⚠️ Ник слишком длинный (максимум 32 символа).")
            return True
        db_set_nickname(user_id, new_nick)
        await update.message.reply_text(
            f"✅ Теперь в статистике и действиях бота вы будете отображаться как: <b>{escape(new_nick)}</b>",
            parse_mode="HTML"
        )
        return True

    # +статус
    if text.startswith("+статус"):
        new_status = raw_text[7:].strip()
        if not new_status:
            db_set_status(user_id, None)
            await update.message.reply_text("✅ Статус очищен.")
            return True
        if len(new_status) > 100:
            await update.message.reply_text("⚠️ Статус слишком длинный (максимум 100 симво символов).")
            return True
        db_set_status(user_id, new_status)
        await update.message.reply_text(
            f"✅ Новый статус установлен: <i>{escape(new_status)}</i>",
            parse_mode="HTML"
        )
        return True

    # +день рождения
    if text.startswith("+день рождения"):
        date_part = raw_text[len("+день рождения"):].strip()
        if not date_part:
            db_set_birthday(user_id, None)
            await update.message.reply_text("✅ Дата рождения удалена.")
            return True
        parsed = parse_birthday(date_part)
        if not parsed:
            await update.message.reply_text(
                "⚠️ Неверный формат. Используйте: <code>+день рождения ДД.ММ</code>",
                parse_mode="HTML"
            )
            return True
        db_set_birthday(user_id, parsed)
        await update.message.reply_text(f"🎂 Дата рождения сохранена: {parsed}. Не забуду поздравить!")
        return True

    # !инфа
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
        return True

    return False  # Команда не относится к профилю
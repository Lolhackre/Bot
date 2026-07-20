# commands/actions.py
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# Импортируем твои существующие библиотеки и хелперы
import actions  # Твой модуль с ACTIONS и command_action
from database import db_get_command_rank, resolve_target_user, format_rank


async def handle_action_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
    text: str,
    user_id: int,
    current_rank: int,
    is_creator: bool,
) -> bool:
    """Обрабатывает меню действий и сами интерактивные действия (обнять, ударить и т.д.)."""

    # 1. Список всех действий (!действия / !действие)
    if text in ("!действия", "!действие"):
        lines = ["🎭 <b>Все доступные действия (включая 18+):</b>\n\n"]
        for key, (emoji, _) in sorted(actions.ACTIONS.items()):
            lines.append(f"{emoji} <code>!{key}</code>")

        min_rank = db_get_command_rank("действие")
        lines.append(
            f"\nℹ️ Используй ответом на сообщение, или !действие @username, или прямо !ударить @username\n"
            f"Требуется ранг {format_rank(min_rank)}+."
        )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return True

    # 2. Обработка самого действия
    clean_text = text.strip()
    action_key = None
    action_target_arg = None

    # Если текст начинается с '!', отсекаем его для поиска ключа
    search_text = clean_text[1:] if clean_text.startswith("!") else clean_text

    # Перебираем ключи, начиная с САМЫХ ДЛИННЫХ фразовых команд
    for key in sorted(actions.ACTIONS.keys(), key=len, reverse=True):
        if search_text.lower().startswith(key.lower()):
            action_key = key
            # Берем хвост сообщения, который идет строго ЗА командой
            raw_arg = search_text[len(key) :].strip()
            action_target_arg = raw_arg if raw_arg else None
            break

    # Если действие найдено в тексте
    if action_key:
        has_reply = bool(update.message.reply_to_message)

        # Перепроверяем аргумент на валидность (только если передан)
        has_valid_arg = False
        if action_target_arg:
            has_valid_arg = action_target_arg.startswith("@") or action_target_arg.isdigit()

        # Если текст после команды есть, но это не @юзер и не ID — скипаем (защита от флуда)
        if action_target_arg and not has_valid_arg:
            return False

        # Если нет ни реплая, ни правильного аргумента — скипаем
        if not (has_reply or has_valid_arg):
            return False

        # Проверка рангов
        min_rank = db_get_command_rank("действие")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(
                f"⛔ Недостаточно прав для действий. Требуется ранг {format_rank(min_rank)}+. Ваш ранг: {format_rank(current_rank)}"
            )
            return True

        # Определяем юзера
        resolved = await resolve_target_user(update, context, action_target_arg)
        if resolved is None:
            await update.message.reply_text(
                "⚠️ Не удалось определить, к кому применить действие. Ответьте на сообщение или укажите @username/ID."
            )
            return True

        # Запускаем отправку действия
        await actions.command_action(update, context, action_key, resolved)
        return True

    return False  # Сообщение не является действием
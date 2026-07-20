# commands/admin.py
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
import config

from database import (
    format_rank,db_get_command_rank,db_fix_default_rank_bug,
    db_get_rank_name,db_get_rank_names,resolve_target_user,
    db_format_user_link,db_set_user_rank,db_set_rank_name,db_get_command_ranks,
    db_set_command_rank
    )

async def handle_admin_commands(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    raw_text: str, 
    text: str, 
    user_id: int, 
    current_rank: int, 
    is_creator: bool
) -> bool:

    # !сет ранк
    if text.startswith("!сет ранк"):
        if not is_creator and current_rank < 6: 
            await update.message.reply_text(f"У вас нет прав для изменения рангов. Требуется {format_rank(6)}.")
            return True

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
            display_name = db_format_user_link(target_id, target_username, target_full_name)
        else:
            await update.message.reply_text("❌ Не удалось найти пользователя.")
            return

        db_set_user_rank(target_id, rank_val)
        await update.message.reply_text(f"✅ Пользователю {display_name} успешно присвоен ранг {format_rank(rank_val)}", parse_mode=ParseMode.HTML)
        return True

    # !доступ
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
        return True

    # !обнулить
    if text.startswith("!обнулить"):
        min_rank = db_get_command_rank("обнулить")
        if not is_creator and current_rank < min_rank:
            await update.message.reply_text(f"⛔ Эта команда доступна только с ранга {format_rank(min_rank)}.")
            return True

        parts = raw_text.split()
        target_arg = None if update.message.reply_to_message else (parts[1] if len(parts) >= 2 else None)
        
        resolved = await resolve_target_user(update, context, target_arg)
        if resolved is None:
            await update.message.reply_text("❌ Не удалось определить пользователя для обнуления. Укажите @username, ID или ответьте на его сообщение.")
            return

        target_id, target_username, target_full_name = resolved
        display_name = db_format_user_link(target_id, target_username, target_full_name)

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
        return True

    # !команда
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
        return True

    # !исправить ранги
    if text.startswith("!исправить ранги"):
        if not is_creator and current_rank < 6:
            await update.message.reply_text(f"⛔ Эта команда доступна только {format_rank(6)}.")
            return

        fixed_count = db_fix_default_rank_bug()
        await update.message.reply_text(
            f"✅ Готово. Сброшено на «{db_get_rank_name(0)}» участников: {fixed_count}.\n"
            f"ℹ️ Затронуты только те, у кого стоял ранг 1 — ранги 2+ не трогались."
        )
        return True

    return False
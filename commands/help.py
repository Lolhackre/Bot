# commands/info.py
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import db_get_command_ranks, format_rank,db_top_by_messages,db_top_by_walks,db_format_user_link, format_silent_ping

async def show_karma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m_rows = db_top_by_messages(10)
    w_rows = db_top_by_walks(10)
    if not m_rows and not w_rows:
        await update.message.reply_text("Пока нет данных для статистики.")
        return

    lines = ["💬 Карма за общение:\n"]
    for i, (uid, un, fn, msgs, score) in enumerate(m_rows, start=1):
        lines.append(f"{i}. {format_silent_ping(un)} — {score} очков ({msgs} сообщ.)")
    
    lines.append("\n🚶 Карма за прогулки:\n")
    for i, (uid, un, fn, walks, score) in enumerate(w_rows, start=1):
        lines.append(f"{i}. {format_silent_ping(un)} — {score} очков | Прогулок: {walks}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def handle_info_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
    text: str,
    user_id: int,
    current_rank: int,
    is_creator: bool,
) -> bool:
    """Модуль информационных команд (!хелп, !помощь, !топ, !флаг)."""

    # 1. Справка по командам (!хелп / !помощь)
    if text in ("!хелп", "!помощь"):
        cmd_ranks = db_get_command_ranks()
        help_text = (
            "📖 <b>Справка по командам бота:</b>\n\n"
            "💬 <code>!топ</code> — Показать топ участников по общению и прогулкам.\n"
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
        return True

    # Сюда же можно перенести команду !топ, когда дойдешь до неё:
    # if text in ("!топ", "!top"):
    #     await show_top_stats(update, context)
    #     return True
        # 2. Обработка кармы
    if text in ("!топ"):
        await show_karma(update, context)
        return True
    
    

    return False  # Команда не относится к info
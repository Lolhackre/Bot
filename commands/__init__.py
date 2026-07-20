# commands/__init__.py
from typing import Callable, Awaitable, Optional, Dict, Tuple
from telegram import Update
from telegram.ext import ContextTypes

# Импортируем наши модули команд
from . import profile, admin, help, actions

# Тип функции-обработчика: async def handler(update, context, raw_text, text, user_id, current_rank, is_creator)
HandlerType = Callable[[Update, ContextTypes.DEFAULT_TYPE, str, str, int, int, bool], Awaitable[bool]]

# Список префиксных команд и их функций
# Возвращают True, если команда обработана, иначе False
COMMAND_HANDLERS = [
    # Модуль профиля
    profile.handle_profile_commands,
    
    # Модуль модерации и администрирования
    admin.handle_admin_commands,

    # Инфо-команды (!хелп, !топ, !флаг)
    help.handle_info_commands,
    
    # Действия (!обнять, !укусить и т.д.)
    actions.handle_action_commands,
]

async def dispatch_command(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    raw_text: str, 
    text: str, 
    user_id: int, 
    current_rank: int, 
    is_creator: bool
) -> bool:
    """Прогоняет полученный текст по всем зарегистрированным обработчикам."""
    for handler in COMMAND_HANDLERS:
        handled = await handler(update, context, raw_text, text, user_id, current_rank, is_creator)
        if handled:
            return True
    return False
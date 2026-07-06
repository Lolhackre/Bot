import os
import pytz

# Токен и ID чатов берём из переменных окружения для безопасности (с фолбеком на ваши значения)
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
MAIN_GROUP_CHAT_ID = os.environ.get("MAIN_GROUP_CHAT_ID")

# Имя базы данных. Чтобы она не стиралась, проверяйте, чтобы Docker или VPS не очищали этот файл
DB_PATH = os.environ.get("DB_PATH", "bot.db")
KYIV_TZ = pytz.timezone("Europe/Kyiv")

POLL_OPTIONS = [
    "Мытница", "Хрещатик", "Долина роз", "Музей", 
    "Химпас", "ЖД вокзал", "Юго-Запад", "Дом природы", 
    "Дружба народов"
]

# Скоринг
WALK_SCORE = 5
NOT_WALKING_PENALTY = 2
MESSAGE_SCORE = 1

# Настройки активности
MAX_INACTIVE_DAYS = 10
PING_INTERVAL_DAYS = 2
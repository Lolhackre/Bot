"""
extra_features.py
------------------
Дополнительные "приколюхи" для чата, НЕ связанные с явкой/прогулками:
  1. Пасхалки с редким шансом (текст или голос)
  2. Еженедельная летопись чата в архаичном стиле
  3. !суд <реплай> [причина] — шуточный суд с голосовым приговором
  4. Слово дня с подставой + отслеживание, кто первым его употребил

Модуль полностью самостоятельный и не изменяет существующий код бота.
Чтобы включить фичи, в main.py нужно добавить несколько строк — см. README-блок
в конце этого файла с точным списком того, что добавить.
"""

import random
import re
import sys
import asyncio
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config
from fishaudio import FishAudio
from fishaudio.utils import save


# ==========================================================
# ИНИЦИАЛИЗАЦИЯ БД (вызвать один раз при старте, из main())
# ==========================================================
def init_extra_features_db():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                text TEXT,
                ts TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS egg_catches (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                catches INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS word_of_day_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                word TEXT,
                definition TEXT,
                date TEXT,
                claimed_by INTEGER
            )
        """)
        conn.commit()


def _format_link(user_id, username, full_name):
    display_name = escape(full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'


# ==========================================================
# 1. ПАСХАЛКИ С РЕДКИМ ШАНСОМ
# ==========================================================
EGG_CHANCE = 350  # примерно 1 из 350 сообщений

EGG_TEXT_REACTIONS = [
    "🎉 Оу, редкий момент! Ты — счастливчик дня.",
    "✨ Система засекла аномалию. Поздравляю, это было красиво.",
    "🍀 Одно из ~350 сообщений — именно твоё. Не знаю зачем, но держи это знание.",
    "🎰 Джекпот! Правда бесполезный, но джекпот.",
    "🌟 Ты только что поймал(а) невидимую звезду. Никто, кроме бота, этого не видел.",
    "🔮 Вселенная моргнула именно на этом сообщении.",
    "🎲 Кубик судьбы выпал именно на тебе. Смысла ноль, эффект приятный.",
    "🏆 Достижение разблокировано: «Оказался(ась) в нужном чате в нужную секунду».",
]

EGG_VOICE_PHRASES = [
    "Ого. Редкий момент. Наслаждайся.",
    "Поздравляю, ты поймал редкость.",
    "Система отметила именно это сообщение.",
    "Ты выиграл в лотерею, приз — ничего.",
]


def _bump_egg_catch(user_id, username, full_name):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO egg_catches (user_id, username, full_name, catches)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                catches = catches + 1
        """, (user_id, username, full_name))
        conn.commit()


async def _trigger_easter_egg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    try:
        await asyncio.to_thread(_bump_egg_catch, user.id, user.username, user.full_name)
    except Exception as e:
        print(f"[EGG DB ERROR] {e}", file=sys.stderr)

    # 25% случаев — голосом (дороже по ресурсам), остальное — текстом
    if random.random() < 0.25:
        phrase = random.choice(EGG_VOICE_PHRASES)
        voice_key = random.choice(list(config.VOICE_LIBRARY.keys()))
        voice_info = config.VOICE_LIBRARY[voice_key]
        tmp_path = None
        try:
            client = FishAudio(api_key=config.FISH_API_KEY)
            audio = await asyncio.to_thread(
                client.tts.convert,
                text=phrase,
                reference_id=voice_info["id"],
                model="s2.1-pro-free"
            )
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                save(audio, tmp_file.name)
                tmp_path = tmp_file.name
            with open(tmp_path, "rb") as voice:
                await message.reply_voice(voice=voice)
        except Exception as e:
            print(f"[EGG VOICE ERROR] {e}", file=sys.stderr)
            await message.reply_text(random.choice(EGG_TEXT_REACTIONS))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    else:
        await message.reply_text(random.choice(EGG_TEXT_REACTIONS))


async def command_egg_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!редкости — топ по пойманным пасхалкам"""
    if str(update.message.chat_id) != str(config.MAIN_GROUP_CHAT_ID):
        return
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT user_id, username, full_name, catches FROM egg_catches ORDER BY catches DESC LIMIT 10"
            ).fetchall()
    except Exception as e:
        print(f"[EGG LEADERBOARD ERROR] {e}", file=sys.stderr)
        return

    if not rows:
        await update.message.reply_text("😶 Пока никто не ловил редкие моменты.")
        return

    text = "🍀 <b>Топ ловцов редких моментов</b>\n\n"
    for i, (uid, uname, fname, catches) in enumerate(rows, start=1):
        link = _format_link(uid, uname, fname)
        text += f"{i}. {link} — {catches}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ==========================================================
# 2. ЛЕТОПИСЬ ЧАТА (еженедельная, в архаичном стиле)
# ==========================================================
CHRONICLE_KEEP_DAYS = 14  # сколько дней хранить сообщения в логе

CHRONICLE_INTRO = [
    "📜 <b>ЛЕТОПИСЬ ЧАТА</b>\nВ лето 2026-е, седмицу минувшую, писано было следующее...\n",
    "📜 <b>ЛЕТОПИСЬ ЧАТА</b>\nСедмица сия была богата на деяния, и вот что запомнилось летописцу...\n",
    "📜 <b>ЛЕТОПИСЬ ЧАТА</b>\nИ собрал летописец слова недели сей, дабы не канули они в забвение...\n",
]

CHRONICLE_LINE_TEMPLATES = [
    "И молвил {name}: «{text}»",
    "А {name}, не убоявшись осуждения, изрёк: «{text}»",
    "Восстал {name} и провозгласил: «{text}»",
    "Летописец же записал слова {name}: «{text}»",
    "Не обошлось без {name}, кой заявил: «{text}»",
    "И был глас {name} в чате том: «{text}»",
    "На что {name} ответствовал: «{text}»",
]

CHRONICLE_OUTRO = [
    "\n📖 На сём летопись сия окончена. До следующей седмицы.",
    "\n📖 Так и закончилась неделя сия, полная слов и деяний. Аминь.",
]


def _log_message_for_chronicle(user_id, username, full_name, text):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO chat_messages_log (user_id, username, full_name, text, ts)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, full_name, text, now_str))
        conn.commit()


def _prune_and_sample_chronicle_messages(sample_size=8):
    cutoff = (datetime.now() - timedelta(days=CHRONICLE_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("DELETE FROM chat_messages_log WHERE ts < ?", (cutoff,))
        conn.commit()
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT user_id, username, full_name, text FROM chat_messages_log WHERE ts >= ? ORDER BY RANDOM() LIMIT ?",
            (week_ago, sample_size)
        ).fetchall()
        return rows


async def weekly_chronicle_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await asyncio.to_thread(_prune_and_sample_chronicle_messages, 8)
        if not rows:
            return

        text = random.choice(CHRONICLE_INTRO)
        for user_id, username, full_name, msg_text in rows:
            name = _format_link(user_id, username, full_name)
            trimmed = escape(msg_text[:150])
            line = random.choice(CHRONICLE_LINE_TEMPLATES).format(name=name, text=trimmed)
            text += f"\n{line}\n"
        text += random.choice(CHRONICLE_OUTRO)

        await context.bot.send_message(
            chat_id=config.MAIN_GROUP_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"[CHRONICLE ERROR] {e}", file=sys.stderr)


# ==========================================================
# 3. ГОЛОСОВОЙ "СУД"
# ==========================================================
COURT_ARTICLES = [
    "ст. 1 Устава Чата — злостное игнорирование опроса",
    "ст. 2 Устава Чата — наглое опоздание на прогулку",
    "ст. 3 Устава Чата — распространение сомнительных шуток",
    "ст. 4 Устава Чата — подозрительная тишина в важный момент",
    "ст. 5 Устава Чата — публичное употребление капслока без повода",
    "ст. 6 Устава Чата — систематическое «щас выйду» без выхода",
    "ст. 7 Устава Чата — незаконное присвоение звания балабола",
]

COURT_SENTENCES = [
    "обязать 3 дня писать сообщения только с эмодзи",
    "приговорить к публичному извинению перед чатом в стихах",
    "обязать организовать следующую прогулку лично",
    "лишить права жаловаться на погоду на 1 неделю",
    "приговорить к вечному титулу «Обвиняемый №1»",
    "обязать 24 часа отвечать всем только словом «согласен»",
    "приговорить к чистосердечному голосовому признанию",
]

DEFAULT_COURT_REASONS = [
    "неустановленное, но явно что-то было",
    "то самое, все всё поняли",
    "по совокупности прошлых прегрешений",
]


async def command_court(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!суд (в ответ на сообщение) [причина] — шуточный суд с голосовым приговором"""
    message = update.message

    if not message.reply_to_message:
        await message.reply_text("⚠️ Ответьте командой !суд на сообщение того, кого хотите судить.")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply_text("🤖 Ботов не судим, у них дипломатический иммунитет.")
        return

    raw_text = message.text or ""
    parts = raw_text.split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else random.choice(DEFAULT_COURT_REASONS)
    reason = reason[:150]

    target_link = _format_link(target.id, target.username, target.full_name)
    article = random.choice(COURT_ARTICLES)
    sentence = random.choice(COURT_SENTENCES)

    verdict_text = (
        f"⚖️ <b>СУД ЧАТА ЗАСЕДАЕТ</b>\n\n"
        f"Подсудимый: {target_link}\n"
        f"Статья: {escape(article)}\n"
        f"Обвинение: {escape(reason)}\n\n"
        f"🔨 Приговор: {escape(sentence)}."
    )
    await message.reply_text(verdict_text, parse_mode=ParseMode.HTML)

    # Голосовой приговор через уже существующую библиотеку голосов
    voice_key = config.DEFAULT_VOICE_KEY
    voice_info = config.VOICE_LIBRARY[voice_key]
    speech_text = f"Именем чата. Обвиняется {target.full_name or target.username}. Статья: {article}. Приговор: {sentence}."
    tmp_path = None
    try:
        client = FishAudio(api_key=config.FISH_API_KEY)
        audio = await asyncio.to_thread(
            client.tts.convert,
            text=speech_text[:600],
            reference_id=voice_info["id"],
            model="s2.1-pro-free"
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            save(audio, tmp_file.name)
            tmp_path = tmp_file.name
        with open(tmp_path, "rb") as voice:
            await message.reply_voice(voice=voice)
    except Exception as e:
        print(f"[COURT VOICE ERROR] {e}", file=sys.stderr)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ==========================================================
# 4. СЛОВО ДНЯ С ПОДСТАВОЙ
# ==========================================================
WORD_OF_DAY_LIST = [
    ("залипундель", "состояние, когда открыл чат и забыл зачем"),
    ("отгуляйсь", "вежливая форма отказа идти на прогулку"),
    ("кармодрочер", "тот, кто слишком одержим счётом кармы"),
    ("молчаллер", "участник, который прочитал, но не ответил"),
    ("прогулофоб", "тот, кто соглашается идти, но не идёт"),
    ("войсодел", "тот, кто злоупотребляет командой !войс"),
    ("рангоман", "тот, кто мечтает о повышении ранга больше, чем о прогулке"),
    ("опросонибудь", "универсальная отмазка не голосовать вовремя"),
    ("балаболжец", "тот, кто набрал сообщений, но всё они не по делу"),
    ("тортометатель", "участник, склонный к шуточным розыгрышам"),
]


def _set_word_of_day(word, definition):
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            INSERT INTO word_of_day_state (id, word, definition, date, claimed_by)
            VALUES (1, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                word = excluded.word,
                definition = excluded.definition,
                date = excluded.date,
                claimed_by = NULL
        """, (word, definition, today))
        conn.commit()


def _get_word_of_day_state():
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT word, definition, date, claimed_by FROM word_of_day_state WHERE id = 1"
        ).fetchone()
        return row


def _claim_word_of_day(user_id):
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE word_of_day_state SET claimed_by = ? WHERE id = 1 AND claimed_by IS NULL", (user_id,)
        )
        conn.commit()
        return cur.rowcount


async def post_word_of_day(context: ContextTypes.DEFAULT_TYPE):
    try:
        word, definition = random.choice(WORD_OF_DAY_LIST)
        await asyncio.to_thread(_set_word_of_day, word, definition)
        text = (
            f"📖 <b>СЛОВО ДНЯ</b>\n\n"
            f"«<b>{escape(word)}</b>» — {escape(definition)}\n\n"
            f"Кто первый употребит это слово в чате сегодня — получит титул «Словотворец дня» 🏆"
        )
        await context.bot.send_message(
            chat_id=config.MAIN_GROUP_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"[WORD OF DAY POST ERROR] {e}", file=sys.stderr)


async def _check_word_of_day_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or ""

    try:
        state = await asyncio.to_thread(_get_word_of_day_state)
    except Exception as e:
        print(f"[WORD OF DAY CHECK ERROR] {e}", file=sys.stderr)
        return

    if not state:
        return
    word, definition, date, claimed_by = state
    if not word or claimed_by is not None:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if date != today:
        return  # слово вчерашнее, новое ещё не постили

    if not re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
        return

    user = update.effective_user
    try:
        changed = await asyncio.to_thread(_claim_word_of_day, user.id)
    except Exception as e:
        print(f"[WORD OF DAY CLAIM ERROR] {e}", file=sys.stderr)
        return

    if changed:
        link = _format_link(user.id, user.username, user.full_name)
        await message.reply_text(
            f"🏆 {link} первым употребил(а) слово дня «{escape(word)}»! Официально — Словотворец дня 🎉",
            parse_mode=ParseMode.HTML
        )


# ==========================================================
# ЕДИНЫЙ "ФОНОВЫЙ" ОБРАБОТЧИК
# (объединяет летопись + слово дня + пасхалку в один MessageHandler,
#  чтобы не плодить лишние регистрации; команды "!", "+", "/" не трогает)
# ==========================================================
async def extra_features_passive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    if text.startswith("!") or text.startswith("+") or text.startswith("/"):
        return  # команды не трогаем — только органический чат

    user = update.effective_user
    if user is None or user.is_bot:
        return

    # 1. Логируем сообщение для летописи (не слишком короткое/длинное)
    if 8 <= len(text) <= 300:
        try:
            await asyncio.to_thread(_log_message_for_chronicle, user.id, user.username, user.full_name, text)
        except Exception as e:
            print(f"[CHRONICLE LOG ERROR] {e}", file=sys.stderr)

    # 2. Проверка слова дня
    await _check_word_of_day_usage(update, context)

    # 3. Пасхалка (редкий шанс)
    if random.randint(1, EGG_CHANCE) == 1:
        await _trigger_easter_egg(update, context)


# ==========================================================
# ЧТО ДОБАВИТЬ В main.py, ЧТОБЫ ВКЛЮЧИТЬ ЭТИ ФИЧИ
# (ничего из существующего кода менять не нужно — только добавить)
# ==========================================================
#
# 1) В блок импортов добавить:
#     import extra_features
#
# 2) В функции main(), сразу после db_init() и init_votes_tracking(), добавить:
#     extra_features.init_extra_features_db()
#
# 3) Там же, среди регистрации хендлеров, добавить (можно в любом месте после
#    app = Application.builder()...build()):
#
#     app.add_handler(
#         MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.UpdateType.EDITED,
#                         extra_features.extra_features_passive_handler),
#         group=10
#     )
#     app.add_handler(
#         MessageHandler(filters.Regex(r'(?i)^!суд(\s|$)') & filters.ChatType.GROUPS,
#                         extra_features.command_court),
#         group=11
#     )
#     app.add_handler(
#         MessageHandler(filters.Regex(r'(?i)^!редкости(\s|$)') & filters.ChatType.GROUPS,
#                         extra_features.command_egg_leaderboard),
#         group=12
#     )
#
#    (группы 10-12 выбраны специально "подальше" от твоих существующих 0-1,
#     чтобы гарантированно не конфликтовать с !-диспетчером и режимом "Стоп Срач")
#
# 4) В блоке jq.run_daily(...) добавить две новые задачи:
#
#     jq.run_daily(extra_features.weekly_chronicle_job,
#                  time=dtime(hour=21, minute=0, tzinfo=config.KYIV_TZ),
#                  days=(6,))  # воскресенье
#     jq.run_daily(extra_features.post_word_of_day,
#                  time=dtime(hour=12, minute=0, tzinfo=config.KYIV_TZ))
#
# Больше НИЧЕГО менять не нужно — твой существующий код остаётся как есть.

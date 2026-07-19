import random
import sys
import asyncio
import sqlite3
from datetime import datetime, timedelta
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config


def _format_link(user_id, username, full_name):
    display_name = escape(full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'


def init_agent_db():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS secret_agent_pairs (
                week_key TEXT PRIMARY KEY,
                user_a INTEGER,
                user_b INTEGER,
                a_replied INTEGER DEFAULT 0,
                b_replied INTEGER DEFAULT 0
            )
        """)
        conn.commit()


def _current_week_key():
    year, week, _ = datetime.now().isocalendar()
    return f"{year}-W{week}"


def _pick_two_random_active_users():
    with sqlite3.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE messages_count > 0 ORDER BY RANDOM() LIMIT 2"
        ).fetchall()
    return [r[0] for r in rows]


async def weekly_agent_pairing_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        user_ids = await asyncio.to_thread(_pick_two_random_active_users)
        if len(user_ids) < 2:
            return
        week_key = _current_week_key()
        with sqlite3.connect(config.DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO secret_agent_pairs (week_key, user_a, user_b, a_replied, b_replied)
                VALUES (?, ?, ?, 0, 0)
            """, (week_key, user_ids[0], user_ids[1]))
            conn.commit()
    except Exception as e:
        print(f"[AGENT PAIRING ERROR] {e}", file=sys.stderr)


def _mark_agent_reply(sender_id, target_id, week_key):
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_a, user_b, a_replied, b_replied FROM secret_agent_pairs WHERE week_key = ?",
            (week_key,)
        ).fetchone()
        if not row:
            return
        user_a, user_b, a_replied, b_replied = row
        if sender_id == user_a and target_id == user_b:
            conn.execute("UPDATE secret_agent_pairs SET a_replied = 1 WHERE week_key = ?", (week_key,))
            conn.commit()
        elif sender_id == user_b and target_id == user_a:
            conn.execute("UPDATE secret_agent_pairs SET b_replied = 1 WHERE week_key = ?", (week_key,))
            conn.commit()


async def watch_agent_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.reply_to_message:
        return
    sender = update.effective_user
    target = message.reply_to_message.from_user
    if sender is None or target is None or sender.id == target.id or target.is_bot:
        return
    week_key = _current_week_key()
    try:
        await asyncio.to_thread(_mark_agent_reply, sender.id, target.id, week_key)
    except Exception as e:
        print(f"[AGENT REPLY TRACK ERROR] {e}", file=sys.stderr)


async def weekly_agent_reward_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        week_key = _current_week_key()
        with sqlite3.connect(config.DB_PATH) as conn:
            row = conn.execute(
                "SELECT user_a, user_b, a_replied, b_replied FROM secret_agent_pairs WHERE week_key = ?",
                (week_key,)
            ).fetchone()
            if not row:
                return
            user_a, user_b, a_replied, b_replied = row
            if not (a_replied and b_replied):
                return
            conn.execute("UPDATE users SET walk_karma = walk_karma + 10 WHERE user_id = ?", (user_a,))
            conn.execute("UPDATE users SET walk_karma = walk_karma + 10 WHERE user_id = ?", (user_b,))
            conn.commit()
            name_a = conn.execute("SELECT username, full_name FROM users WHERE user_id = ?", (user_a,)).fetchone()
            name_b = conn.execute("SELECT username, full_name FROM users WHERE user_id = ?", (user_b,)).fetchone()

        if name_a and name_b:
            link_a = _format_link(user_a, name_a[0], name_a[1])
            link_b = _format_link(user_b, name_b[0], name_b[1])
            await context.bot.send_message(
                chat_id=config.MAIN_GROUP_CHAT_ID,
                text=(
                    f"🎯 Странное совпадение: {link_a} и {link_b} чаще всех отвечали друг другу "
                    f"на этой неделе. Управление начисляет им бонус кармы."
                ),
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        print(f"[AGENT REWARD ERROR] {e}", file=sys.stderr)


PROFESSIONS = [
    "Хирург", "Программист", "Учитель физкультуры", "Электрик", "Повар",
    "Психолог", "Военный снайпер", "Фермер", "Ветеринар", "Инженер-строитель",
    "Диджей", "Пожарный", "Биолог", "Дальнобойщик", "Стоматолог",
    "Сантехник", "Актёр", "Полицейский", "Пилот", "Журналист",
]

HEALTH = [
    "Полностью здоров(а)", "Астма", "Аллергия на пыль", "Плохое зрение",
    "Хронический насморк", "Бессонница", "Слабое сердце", "Диабет 2 типа",
    "Мигрени", "Проблемы со спиной", "Лёгкая клаустрофобия",
    "Отличная физическая форма", "Пищевая аллергия", "Плоскостопие", "Хороший иммунитет",
]

HOBBIES = [
    "Рыбалка", "Программирование", "Игра на гитаре", "Йога", "Охота",
    "Шахматы", "Выращивание растений", "Кулинария", "Рисование",
    "Бег на длинные дистанции", "Ремонт техники", "Чтение",
    "Настольные игры", "Фотография", "Вязание",
]

PHOBIAS = [
    "Боязнь высоты", "Боязнь темноты", "Боязнь замкнутых пространств",
    "Боязнь пауков", "Боязнь толпы", "Боязнь глубокой воды",
    "Боязнь микробов", "Боязнь одиночества", "Боязнь громких звуков",
    "Нет явных фобий", "Боязнь огня", "Боязнь птиц",
]

BAGGAGE = [
    "Аптечка первой помощи", "Ящик семян", "Генератор на ручной тяге",
    "Рация", "Гитара", "Запас питьевой воды", "Набор инструментов",
    "Книги по выживанию", "Швейная машинка", "Солнечная батарея",
    "Мешок круп", "Оружие для охоты", "Компас и карты",
]

FACTS = [
    "Раньше был(а) судим(а) за мелкое хулиганство", "Врал(а) о своей профессии всю жизнь",
    "Тайно ненавидит животных", "Бывший спортсмен-разрядник", "Знает 4 языка",
    "Панически боится начальства", "Однажды спас(ла) человека из огня",
    "Никогда не признаёт ошибок", "Уже был(а) в подобной ситуации",
    "Состоит в тайном клубе по интересам",
]

CATASTROPHES = [
    "☢️ Глобальная ядерная война уничтожила большую часть поверхности Земли.",
    "🦠 Быстро распространяющийся вирус превращает заражённых в агрессивных существ.",
    "🌋 Серия супервулканических извержений накрыла планету пеплом на годы вперёд.",
    "👽 Инопланетное вторжение вынудило человечество спрятаться под землёй.",
    "🌊 Глобальное наводнение затопило большую часть суши.",
]

BUNKER_INFOS = [
    "Бункер рассчитан на ограниченное количество мест и запасов на 5 лет.",
    "В бункере хватит еды и воды на всех, но мест для проживания меньше, чем желающих.",
    "Бункер небольшой, кислорода и провизии хватит только на часть группы.",
]

CARD_ORDER = ["Возраст", "Профессия", "Здоровье", "Хобби", "Фобия", "Багаж", "Факт"]

BUNKER_GAMES = {}


def _generate_card():
    return {
        "Возраст": str(random.randint(18, 65)),
        "Профессия": random.choice(PROFESSIONS),
        "Здоровье": random.choice(HEALTH),
        "Хобби": random.choice(HOBBIES),
        "Фобия": random.choice(PHOBIAS),
        "Багаж": random.choice(BAGGAGE),
        "Факт": random.choice(FACTS),
    }


def _lobby_text(game):
    names = ", ".join(escape(p["name"]) for p in game["players"].values()) or "пока никого"
    return (
        f"☢️ <b>ИГРА БУНКЕР</b>\n\n"
        f"{game['catastrophe']}\n\n"
        f"{game['bunker_info']}\n\n"
        f"Участники ({len(game['players'])}): {names}\n\n"
        f"Нажмите кнопку, чтобы присоединиться. Начать игру может только тот, кто её создал."
    )


def _lobby_keyboard(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚪 Присоединиться", callback_data=f"bj:{chat_id}")],
        [InlineKeyboardButton("▶️ Начать игру", callback_data=f"bs:{chat_id}")],
    ])


async def command_bunker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    chat_id = chat.id
    existing = BUNKER_GAMES.get(chat_id)
    if existing and existing["phase"] != "finished":
        await update.message.reply_text("⚠️ В этом чате уже идёт игра в Бункер. Дождитесь окончания.")
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(maxsplit=1)
    override_survivors = None
    if len(parts) > 1 and parts[1].strip().isdigit():
        override_survivors = max(1, int(parts[1].strip()))

    host = update.effective_user
    game = {
        "phase": "lobby",
        "host_id": host.id,
        "players": {},
        "order": [],
        "round_index": 0,
        "votes": {},
        "catastrophe": random.choice(CATASTROPHES),
        "bunker_info": random.choice(BUNKER_INFOS),
        "survivors_target": None,
        "override_survivors": override_survivors,
        "voting_alive": [],
        "voting_message_id": None,
    }
    BUNKER_GAMES[chat_id] = game

    await update.message.reply_text(
        _lobby_text(game),
        parse_mode=ParseMode.HTML,
        reply_markup=_lobby_keyboard(chat_id)
    )


async def _run_reveal_round(chat_id, context):
    game = BUNKER_GAMES[chat_id]
    category_index = game["round_index"]

    if category_index >= len(CARD_ORDER):
        # Все характеристики уже раскрыты, но нужно ещё исключать людей —
        # просто показываем полную карточку каждого оставшегося участника
        alive = [uid for uid in game["order"] if game["players"][uid]["alive"]]
        lines = ["📋 <b>Все характеристики уже раскрыты. Информация об оставшихся участниках:</b>\n"]
        for uid in alive:
            player = game["players"][uid]
            card_text = ", ".join(f"{k}: {v}" for k, v in player["card"].items())
            lines.append(f"• {escape(player['name'])} — {escape(card_text)}")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)
        await asyncio.sleep(1)
        await _start_voting(chat_id, context)
        return

    category = CARD_ORDER[category_index]
    lines = [f"📋 <b>Раунд {category_index + 1}: {escape(category)}</b>\n"]
    for uid in game["order"]:
        player = game["players"][uid]
        if not player["alive"]:
            continue
        player["revealed"].append(category)
        # Дублируем всю уже известную информацию, а не только новую характеристику,
        # чтобы карточка каждого игрока была видна целиком с накоплением раундов
        known = ", ".join(f"{k}: {player['card'][k]}" for k in CARD_ORDER if k in player["revealed"])
        lines.append(f"• {escape(player['name'])} — {escape(known)}")

    game["round_index"] += 1
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)
    await asyncio.sleep(1)
    await _start_voting(chat_id, context)


def _voting_text(game, alive):
    lines = ["🗳 <b>Голосование: кого исключить из бункера?</b>\n"]
    lines.append(f"Проголосовали: {len(game['votes'])}/{len(alive)}\n")
    if game["votes"]:
        for voter_id, target in game["votes"].items():
            voter_name = escape(game["players"][voter_id]["name"])
            if target == "skip":
                lines.append(f"✅ {voter_name} — пропустил(а) голосование")
            else:
                target_name = escape(game["players"][target]["name"])
                lines.append(f"✅ {voter_name} → {target_name}")
    return "\n".join(lines)


def _voting_keyboard(chat_id, alive):
    game = BUNKER_GAMES[chat_id]
    buttons = [
        [InlineKeyboardButton(f"❌ {game['players'][uid]['name']}", callback_data=f"bv:{chat_id}:{uid}")]
        for uid in alive
    ]
    buttons.append([InlineKeyboardButton("⏭ Пропустить голос", callback_data=f"bv:{chat_id}:skip")])
    return InlineKeyboardMarkup(buttons)


async def _start_voting(chat_id, context):
    game = BUNKER_GAMES[chat_id]
    alive = [uid for uid in game["order"] if game["players"][uid]["alive"]]

    if len(alive) <= game["survivors_target"]:
        await _finish_game(chat_id, context)
        return

    game["phase"] = "voting"
    game["votes"] = {}
    game["voting_alive"] = alive

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=_voting_text(game, alive),
        parse_mode=ParseMode.HTML,
        reply_markup=_voting_keyboard(chat_id, alive)
    )
    game["voting_message_id"] = message.message_id


async def _tally_votes(chat_id, context):
    game = BUNKER_GAMES[chat_id]
    counts = {}
    skip_count = 0
    for target in game["votes"].values():
        if target == "skip":
            skip_count += 1
        else:
            counts[target] = counts.get(target, 0) + 1

    vote_count = sum(counts.values())

    # Если пропустивших голосование больше либо столько же, сколько проголосовавших
    # за исключение — большинство не хочет никого выгонять, исключения не будет,
    # даже если кто-то один проголосовал против конкретного человека.
    if not counts or skip_count >= vote_count:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🤝 Большинство пропустило голосование — в этом раунде никого не исключаем."
        )
        await _run_reveal_round(chat_id, context)
        return

    max_votes = max(counts.values())
    candidates = [uid for uid, c in counts.items() if c == max_votes]
    eliminated_id = random.choice(candidates)
    game["players"][eliminated_id]["alive"] = False
    eliminated_player = game["players"][eliminated_id]
    name = eliminated_player["name"]
    card_text = ", ".join(f"{k}: {v}" for k, v in eliminated_player["card"].items())

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"💀 {escape(name)} не попадает в бункер и остаётся снаружи.\n\n"
            f"📇 Его(её) карта полностью раскрывается:\n{escape(card_text)}"
        )
    )

    alive = [uid for uid in game["order"] if game["players"][uid]["alive"]]
    if len(alive) <= game["survivors_target"]:
        await _finish_game(chat_id, context)
    else:
        await _run_reveal_round(chat_id, context)


async def _finish_game(chat_id, context):
    game = BUNKER_GAMES[chat_id]
    game["phase"] = "finished"
    alive = [uid for uid in game["order"] if game["players"][uid]["alive"]]

    lines = ["🏁 <b>ИГРА ОКОНЧЕНА</b>\n", "Выжившие в бункере:\n"]
    for uid in alive:
        player = game["players"][uid]
        card_text = ", ".join(f"{k}: {v}" for k, v in player["card"].items())
        lines.append(f"✅ {escape(player['name'])} - {escape(card_text)}")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)
    del BUNKER_GAMES[chat_id]


async def handle_bunker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user
    parts = data.split(":")

    if len(parts) < 2:
        return

    action = parts[0]
    try:
        chat_id = int(parts[1])
    except ValueError:
        return

    game = BUNKER_GAMES.get(chat_id)
    if not game or game["phase"] == "finished":
        await query.answer("Игра не найдена или уже завершена.", show_alert=True)
        return

    if action == "bj":
        if game["phase"] != "lobby":
            await query.answer("Присоединение уже закрыто.", show_alert=True)
            return
        if user.id in game["players"]:
            await query.answer("Ты уже в игре.")
            return
        game["players"][user.id] = {
            "name": user.full_name or user.username or str(user.id),
            "card": _generate_card(),
            "revealed": [],
            "alive": True,
        }
        game["order"].append(user.id)
        await query.answer("Присоединился(-ась) к игре!")
        await query.edit_message_text(
            _lobby_text(game),
            parse_mode=ParseMode.HTML,
            reply_markup=_lobby_keyboard(chat_id)
        )

    elif action == "bs":
        if game["phase"] != "lobby":
            await query.answer("Игра уже начата.", show_alert=True)
            return
        if user.id != game["host_id"]:
            await query.answer("Начать игру может только тот, кто её создал.", show_alert=True)
            return
        if len(game["players"]) < 4:
            await query.answer("Нужно минимум 4 игрока.", show_alert=True)
            return

        game["phase"] = "running"
        if game["override_survivors"] and game["override_survivors"] < len(game["players"]):
            game["survivors_target"] = game["override_survivors"]
        else:
            game["survivors_target"] = max(2, (len(game["players"]) + 1) // 2)
        random.shuffle(game["order"])

        await query.answer("Игра начинается!")
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚨 Игра началась! Участников: {len(game['players'])}. "
                f"В бункере есть место для {game['survivors_target']}.\n"
                f"Каждый раунд открывается одна характеристика всех участников, затем голосование за исключение."
            )
        )
        await _run_reveal_round(chat_id, context)

    elif action == "bv":
        if len(parts) < 3:
            return
        target_raw = parts[2]

        if game["phase"] != "voting":
            await query.answer("Сейчас не время голосовать.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"]:
            await query.answer("Ты не участвуешь в игре или уже выбыл(а).", show_alert=True)
            return
        if user.id in game["votes"]:
            await query.answer("Ты уже проголосовал(а), изменить голос нельзя.", show_alert=True)
            return

        if target_raw == "skip":
            game["votes"][user.id] = "skip"
            await query.answer("Голос пропущен ⏭")
        else:
            try:
                target_id = int(target_raw)
            except ValueError:
                return
            if target_id not in game["players"] or not game["players"][target_id]["alive"]:
                await query.answer("Этот игрок уже выбыл из игры.", show_alert=True)
                return
            game["votes"][user.id] = target_id
            await query.answer("Голос принят! ✅")

        alive = game.get("voting_alive") or [uid for uid in game["order"] if game["players"][uid]["alive"]]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.get("voting_message_id", query.message.message_id),
                text=_voting_text(game, alive),
                parse_mode=ParseMode.HTML,
                reply_markup=_voting_keyboard(chat_id, alive)
            )
        except Exception:
            pass

        if len(game["votes"]) >= len(alive):
            await _tally_votes(chat_id, context)

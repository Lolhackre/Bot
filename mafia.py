import random
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# ─────────────────────────────────────────────────────────────────────────
# Игра "Мафия" — лобби через кнопки в чате, ночь (мафия/доктор/комиссар),
# день (обсуждение + голосование за линч). Хранится в памяти, как и Бункер.
# ─────────────────────────────────────────────────────────────────────────

MAFIA_GAMES = {}

ROLE_NAMES = {
    "mafia": "🔪 Мафия",
    "doctor": "💊 Доктор",
    "detective": "🕵️ Комиссар",
    "civilian": "👤 Мирный житель",
}

ROLE_DESCRIPTIONS = {
    "mafia": "Каждую ночь вместе с остальной мафией выбираешь, кого убрать. Днём притворяйся мирным.",
    "doctor": "Каждую ночь можешь спасти одного игрока (в т.ч. себя) от убийства мафии.",
    "detective": "Каждую ночь можешь проверить одного игрока и узнать, мафия он или нет.",
    "civilian": "Особых способностей нет. Твоя задача — вычислить мафию на голосовании днём.",
}


def _format_link(user_id, username, full_name):
    display_name = escape(full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'


def _alive_ids(game):
    return [uid for uid in game["order"] if game["players"][uid]["alive"]]


def _alive_mafia(game):
    return [uid for uid in _alive_ids(game) if game["players"][uid]["role"] == "mafia"]


def _alive_non_mafia(game):
    return [uid for uid in _alive_ids(game) if game["players"][uid]["role"] != "mafia"]


def _role_counts_for(count):
    """Подбирает состав ролей в зависимости от числа игроков."""
    if count < 5:
        mafia, doctor, detective = 1, 0, 0
    elif count < 7:
        mafia, doctor, detective = 1, 1, 1
    elif count < 10:
        mafia, doctor, detective = 2, 1, 1
    else:
        mafia, doctor, detective = 3, 1, 1
    return mafia, doctor, detective


def _assign_roles(game):
    player_ids = list(game["players"].keys())
    random.shuffle(player_ids)
    mafia_n, doctor_n, detective_n = _role_counts_for(len(player_ids))

    idx = 0
    for _ in range(mafia_n):
        game["players"][player_ids[idx]]["role"] = "mafia"
        idx += 1
    for _ in range(doctor_n):
        game["players"][player_ids[idx]]["role"] = "doctor"
        idx += 1
    for _ in range(detective_n):
        game["players"][player_ids[idx]]["role"] = "detective"
        idx += 1
    for uid in player_ids[idx:]:
        game["players"][uid]["role"] = "civilian"


# ---------- Лобби ----------

def _lobby_text(game):
    names = ", ".join(escape(p["name"]) for p in game["players"].values()) or "пока никого"
    return (
        f"🔪 <b>ИГРА МАФИЯ</b>\n\n"
        f"Участники ({len(game['players'])}): {names}\n\n"
        f"Нужно минимум 4 игрока. Нажмите кнопку, чтобы присоединиться.\n"
        f"Начать игру может только тот, кто её создал."
    )


def _lobby_keyboard(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚪 Присоединиться", callback_data=f"mj:{chat_id}")],
        [InlineKeyboardButton("🎭 Моя роль", callback_data=f"mr:{chat_id}")],
        [InlineKeyboardButton("▶️ Начать игру", callback_data=f"ms:{chat_id}")],
    ])


def _role_keyboard(chat_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Моя роль", callback_data=f"mr:{chat_id}")]])


def _role_alert_text(game, uid):
    role = game["players"][uid]["role"]
    lines = [ROLE_NAMES[role], ROLE_DESCRIPTIONS[role]]
    if role == "mafia":
        teammates = [game["players"][m]["name"] for m in _alive_mafia(game) if m != uid]
        # На случай, если проверка "моя роль" происходит до того, как игрок сам стал живым в списке
        if not teammates:
            teammates = [game["players"][m]["name"] for m in game["order"]
                         if game["players"][m]["role"] == "mafia" and m != uid]
        if teammates:
            lines.append("Твои сообщники: " + ", ".join(teammates))
        else:
            lines.append("Ты единственный представитель мафии.")
    text = "\n".join(lines)
    if len(text) > 200:
        text = text[:197] + "..."
    return text


async def command_mafia_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    chat_id = chat.id
    existing = MAFIA_GAMES.get(chat_id)
    if existing and existing["phase"] != "finished":
        await update.message.reply_text("⚠️ В этом чате уже идёт игра в Мафию. Дождитесь окончания.")
        return

    host = update.effective_user
    game = {
        "phase": "lobby",
        "host_id": host.id,
        "players": {},
        "order": [],
        "round_index": 0,
        "mafia_votes": {},
        "doctor_target": None,
        "doctor_acted": False,
        "detective_acted": False,
        "day_votes": {},
        "lobby_message_id": None,
        "night_msg_ids": {},
        "voting_message_id": None,
    }
    MAFIA_GAMES[chat_id] = game

    message = await update.message.reply_text(
        _lobby_text(game),
        parse_mode=ParseMode.HTML,
        reply_markup=_lobby_keyboard(chat_id)
    )
    game["lobby_message_id"] = message.message_id


async def command_mafia_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!роль — присылает свежую кнопку для просмотра своей роли."""
    chat = update.effective_chat
    game = MAFIA_GAMES.get(chat.id)
    if not game or game["phase"] == "finished":
        await update.message.reply_text("ℹ️ В этом чате сейчас нет активной игры в Мафию.")
        return
    if update.effective_user.id not in game["players"]:
        await update.message.reply_text("⚠️ Ты не участвуешь в текущей игре в Мафию.")
        return
    await update.message.reply_text(
        "🎭 Жми кнопку — роль увидишь только ты:",
        reply_markup=_role_keyboard(chat.id)
    )


# ---------- Ночь ----------

def _mafia_vote_keyboard(chat_id, game):
    targets = _alive_non_mafia(game)
    counts = {}
    for t in game["mafia_votes"].values():
        counts[t] = counts.get(t, 0) + 1
    buttons = [
        [InlineKeyboardButton(
            f"🔪 {counts.get(uid, 0)} — {game['players'][uid]['name']}",
            callback_data=f"mk:{chat_id}:{uid}"
        )]
        for uid in targets
    ]
    return InlineKeyboardMarkup(buttons)


def _doctor_keyboard(chat_id, game):
    targets = _alive_ids(game)
    buttons = [
        [InlineKeyboardButton(game["players"][uid]["name"], callback_data=f"md:{chat_id}:{uid}")]
        for uid in targets
    ]
    return InlineKeyboardMarkup(buttons)


def _detective_keyboard(chat_id, game, detective_id):
    targets = [uid for uid in _alive_ids(game) if uid != detective_id]
    buttons = [
        [InlineKeyboardButton(game["players"][uid]["name"], callback_data=f"mc:{chat_id}:{uid}")]
        for uid in targets
    ]
    return InlineKeyboardMarkup(buttons)


async def _start_night(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "night"
    game["round_index"] += 1
    game["mafia_votes"] = {}
    game["doctor_target"] = None
    game["doctor_acted"] = False
    game["detective_acted"] = False
    game["night_msg_ids"] = {}

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 <b>Ночь {game['round_index']}</b>. Город засыпает...",
        parse_mode=ParseMode.HTML
    )

    mafia_alive = _alive_mafia(game)
    if mafia_alive:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔪 Мафия выбирает жертву (голосуют только живые мафиози):",
            reply_markup=_mafia_vote_keyboard(chat_id, game)
        )
        game["night_msg_ids"]["mafia"] = msg.message_id

    doctors = [uid for uid in _alive_ids(game) if game["players"][uid]["role"] == "doctor"]
    if doctors:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="💊 Доктор выбирает, кого спасти этой ночью:",
            reply_markup=_doctor_keyboard(chat_id, game)
        )
        game["night_msg_ids"]["doctor"] = msg.message_id
    else:
        game["doctor_acted"] = True

    detectives = [uid for uid in _alive_ids(game) if game["players"][uid]["role"] == "detective"]
    if detectives:
        det_id = detectives[0]
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🕵️ Комиссар выбирает, кого проверить этой ночью (результат увидит только он):",
            reply_markup=_detective_keyboard(chat_id, game, det_id)
        )
        game["night_msg_ids"]["detective"] = msg.message_id
    else:
        game["detective_acted"] = True

    await _maybe_resolve_night(chat_id, context)


async def _maybe_resolve_night(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    if game["phase"] != "night":
        return
    mafia_alive = _alive_mafia(game)
    mafia_done = len(game["mafia_votes"]) >= len(mafia_alive) if mafia_alive else True
    if mafia_done and game["doctor_acted"] and game["detective_acted"]:
        await _resolve_night(chat_id, context)


async def _resolve_night(chat_id, context):
    game = MAFIA_GAMES[chat_id]

    # Считаем итог голосования мафии (большинство, ничья — случайно из лидеров)
    killed_id = None
    if game["mafia_votes"]:
        counts = {}
        for target in game["mafia_votes"].values():
            counts[target] = counts.get(target, 0) + 1
        top = max(counts.values())
        leaders = [uid for uid, c in counts.items() if c == top]
        killed_id = random.choice(leaders)

    saved = killed_id is not None and killed_id == game["doctor_target"]

    lines = [f"☀️ <b>Утро после {game['round_index']}-й ночи</b>\n"]
    if killed_id is None:
        lines.append("Мафия не смогла определиться с жертвой — этой ночью никто не пострадал.")
    elif saved:
        victim_name = escape(game["players"][killed_id]["name"])
        lines.append(f"Мафия напала на {victim_name}, но доктор успел его спасти! 💊")
    else:
        game["players"][killed_id]["alive"] = False
        victim_name = escape(game["players"][killed_id]["name"])
        role_name = ROLE_NAMES[game["players"][killed_id]["role"]]
        lines.append(f"💀 Этой ночью убит(а) {victim_name}. Его роль: {role_name}.")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

    winner = _check_win(game)
    if winner:
        await _finish_game(chat_id, context, winner)
        return

    await _start_day_voting(chat_id, context)


# ---------- День ----------

def _voting_text(game, alive):
    lines = ["🗳 <b>Голосование: кого линчуем сегодня?</b>\n"]
    lines.append(f"Проголосовали: {len(game['day_votes'])}/{len(alive)}\n")
    for voter_id, target in game["day_votes"].items():
        voter_name = escape(game["players"][voter_id]["name"])
        if target == "skip":
            lines.append(f"✅ {voter_name} — пропустил(а) голосование")
        else:
            target_name = escape(game["players"][target]["name"])
            lines.append(f"✅ {voter_name} → {target_name}")
    return "\n".join(lines)


def _voting_keyboard(chat_id, game, alive):
    counts = {}
    for target in game["day_votes"].values():
        if target != "skip":
            counts[target] = counts.get(target, 0) + 1
    buttons = [
        [InlineKeyboardButton(
            f"🗳 {counts.get(uid, 0)} — {game['players'][uid]['name']}",
            callback_data=f"mv:{chat_id}:{uid}"
        )]
        for uid in alive
    ]
    buttons.append([InlineKeyboardButton("⏭ Пропустить голос", callback_data=f"mv:{chat_id}:skip")])
    buttons.append([InlineKeyboardButton("🎭 Моя роль", callback_data=f"mr:{chat_id}")])
    return InlineKeyboardMarkup(buttons)


async def _start_day_voting(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "day_voting"
    game["day_votes"] = {}
    alive = _alive_ids(game)

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=_voting_text(game, alive),
        parse_mode=ParseMode.HTML,
        reply_markup=_voting_keyboard(chat_id, game, alive)
    )
    game["voting_message_id"] = message.message_id


async def _tally_day_votes(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    counts = {}
    for target in game["day_votes"].values():
        if target != "skip":
            counts[target] = counts.get(target, 0) + 1

    lynched_id = None
    if counts:
        top = max(counts.values())
        leaders = [uid for uid, c in counts.items() if c == top]
        # Если голоса разделились поровну между несколькими — линча не будет
        if len(leaders) == 1:
            lynched_id = leaders[0]

    if lynched_id is None:
        text = "🤷 Город не смог определиться (или все проголосовали за пропуск) — сегодня никого не линчуют."
    else:
        game["players"][lynched_id]["alive"] = False
        name = escape(game["players"][lynched_id]["name"])
        role_name = ROLE_NAMES[game["players"][lynched_id]["role"]]
        text = f"⚖️ По итогам голосования линчован(а) {name}. Его роль: {role_name}."

    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    winner = _check_win(game)
    if winner:
        await _finish_game(chat_id, context, winner)
        return

    await _start_night(chat_id, context)


# ---------- Победа / завершение ----------

def _check_win(game):
    mafia_alive = len(_alive_mafia(game))
    others_alive = len(_alive_non_mafia(game))
    if mafia_alive == 0:
        return "civilians"
    if mafia_alive >= others_alive:
        return "mafia"
    return None


async def _finish_game(chat_id, context, winner):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "finished"

    if winner == "mafia":
        header = "🔪 <b>Победила мафия!</b>"
    else:
        header = "🎉 <b>Победили мирные жители!</b>"

    lines = [header, "", "Итоговые роли:"]
    for uid in game["order"]:
        p = game["players"][uid]
        status = "живой" if p["alive"] else "выбыл(а)"
        lines.append(f"{ROLE_NAMES[p['role']]} — {escape(p['name'])} ({status})")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)
    del MAFIA_GAMES[chat_id]


# ---------- Обработка кнопок ----------

async def handle_mafia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    game = MAFIA_GAMES.get(chat_id)
    if not game or game["phase"] == "finished":
        await query.answer("Игра не найдена или уже завершена.", show_alert=True)
        return

    if action == "mr":
        if user.id not in game["players"]:
            await query.answer("Ты ещё не присоединился(-ась) к игре.", show_alert=True)
            return
        if game["phase"] == "lobby":
            await query.answer("Роли ещё не розданы — игра не началась.", show_alert=True)
            return
        await query.answer(text=_role_alert_text(game, user.id), show_alert=True)
        return

    if action == "mj":
        if game["phase"] != "lobby":
            await query.answer("Присоединение уже закрыто.", show_alert=True)
            return
        if user.id in game["players"]:
            await query.answer("Ты уже в игре.")
            return
        game["players"][user.id] = {
            "name": user.full_name or user.username or str(user.id),
            "role": None,
            "alive": True,
        }
        game["order"].append(user.id)
        await query.answer("Присоединился(-ась) к игре!")
        await query.edit_message_text(
            _lobby_text(game),
            parse_mode=ParseMode.HTML,
            reply_markup=_lobby_keyboard(chat_id)
        )
        return

    if action == "ms":
        if game["phase"] != "lobby":
            await query.answer("Игра уже начата.", show_alert=True)
            return
        if user.id != game["host_id"]:
            await query.answer("Начать игру может только тот, кто её создал.", show_alert=True)
            return
        if len(game["players"]) < 4:
            await query.answer("Нужно минимум 4 игрока.", show_alert=True)
            return

        _assign_roles(game)
        mafia_n, doctor_n, detective_n = _role_counts_for(len(game["players"]))
        civilian_n = len(game["players"]) - mafia_n - doctor_n - detective_n

        await query.answer("Игра начинается!")
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚨 Игра началась! Игроков: {len(game['players'])}.\n"
                f"Состав ролей: 🔪 Мафия ×{mafia_n}, 💊 Доктор ×{doctor_n}, "
                f"🕵️ Комиссар ×{detective_n}, 👤 Мирные ×{civilian_n}.\n"
                f"Узнать свою роль можно кнопкой «🎭 Моя роль» или командой <code>!роль</code>."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_role_keyboard(chat_id)
        )
        await _start_night(chat_id, context)
        return

    if action == "mk":
        if game["phase"] != "night":
            await query.answer("Сейчас не ночь.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "mafia":
            await query.answer("Голосовать за жертву может только живой участник мафии.", show_alert=True)
            return
        if user.id in game["mafia_votes"]:
            await query.answer("Ты уже проголосовал(а).", show_alert=True)
            return
        if len(parts) < 3:
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"] or \
                game["players"][target_id]["role"] == "mafia":
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["mafia_votes"][user.id] = target_id
        await query.answer(f"Голос принят! Цель: {game['players'][target_id]['name']}")
        msg_id = game["night_msg_ids"].get("mafia")
        if msg_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=msg_id,
                    reply_markup=_mafia_vote_keyboard(chat_id, game)
                )
            except Exception:
                pass
        await _maybe_resolve_night(chat_id, context)
        return

    if action == "md":
        if game["phase"] != "night":
            await query.answer("Сейчас не ночь.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "doctor":
            await query.answer("Лечить может только живой доктор.", show_alert=True)
            return
        if game["doctor_acted"]:
            await query.answer("Ты уже сделал(а) выбор этой ночью.", show_alert=True)
            return
        if len(parts) < 3:
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"]:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["doctor_target"] = target_id
        game["doctor_acted"] = True
        await query.answer(f"Ты выбрал(а) спасти: {game['players'][target_id]['name']}")
        msg_id = game["night_msg_ids"].get("doctor")
        if msg_id:
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
            except Exception:
                pass
        await _maybe_resolve_night(chat_id, context)
        return

    if action == "mc":
        if game["phase"] != "night":
            await query.answer("Сейчас не ночь.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "detective":
            await query.answer("Проверять может только живой комиссар.", show_alert=True)
            return
        if game["detective_acted"]:
            await query.answer("Ты уже сделал(а) проверку этой ночью.", show_alert=True)
            return
        if len(parts) < 3:
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"]:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        is_mafia = game["players"][target_id]["role"] == "mafia"
        result = "🔪 Мафия!" if is_mafia else "👤 Не мафия."
        game["detective_acted"] = True
        target_name = escape(game["players"][target_id]["name"])
        await query.answer(text=f"{target_name}: {result}", show_alert=True)
        msg_id = game["night_msg_ids"].get("detective")
        if msg_id:
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
            except Exception:
                pass
        await _maybe_resolve_night(chat_id, context)
        return

    if action == "mv":
        if game["phase"] != "day_voting":
            await query.answer("Сейчас не время голосовать.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"]:
            await query.answer("Ты не участвуешь в игре или уже выбыл(а).", show_alert=True)
            return
        if user.id in game["day_votes"]:
            await query.answer("Ты уже проголосовал(а), изменить голос нельзя.", show_alert=True)
            return
        if len(parts) < 3:
            return
        target_raw = parts[2]
        if target_raw == "skip":
            game["day_votes"][user.id] = "skip"
            await query.answer("Голос пропущен ⏭")
        else:
            try:
                target_id = int(target_raw)
            except ValueError:
                return
            if target_id not in game["players"] or not game["players"][target_id]["alive"]:
                await query.answer("Этот игрок уже выбыл из игры.", show_alert=True)
                return
            game["day_votes"][user.id] = target_id
            await query.answer(f"Голос принят! ✅ Ты проголосовал(а) за {game['players'][target_id]['name']}")

        alive = _alive_ids(game)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.get("voting_message_id", query.message.message_id),
                text=_voting_text(game, alive),
                parse_mode=ParseMode.HTML,
                reply_markup=_voting_keyboard(chat_id, game, alive)
            )
        except Exception:
            pass

        if len(game["day_votes"]) >= len(alive):
            await _tally_day_votes(chat_id, context)
        return

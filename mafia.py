import random
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

# ─────────────────────────────────────────────────────────────────────────
# Игра "Мафия" — всё происходит прямо в чате (лобби, ночь, день), никаких
# сообщений в личку. Ночью роли ходят ПО ОЧЕРЕДИ: мафия → маньяк → комиссар
# → доктор. На каждый ход даётся 20 секунд, после хода (или по истечении
# времени) сообщение с кнопками выбора удаляется и ход передаётся дальше.
# Хранится в памяти, как и игра Бункер.
# ─────────────────────────────────────────────────────────────────────────

MAFIA_GAMES = {}
STEP_SECONDS = 20

ROLE_NAMES = {
    "mafia": "🔪 Мафия",
    "doctor": "💊 Доктор",
    "detective": "🕵️ Комиссар",
    "maniac": "🗡 Маньяк",
    "civilian": "👤 Мирный житель",
}

ROLE_DESCRIPTIONS = {
    "mafia": "Каждую ночь вместе с остальной мафией выбираешь, кого убрать. Днём притворяйся мирным.",
    "doctor": "Каждую ночь можешь спасти одного игрока (в т.ч. себя) от гибели.",
    "detective": "Каждую ночь можешь проверить одного игрока и узнать его принадлежность.",
    "maniac": "Каждую ночь в одиночку выбираешь жертву. Ты не в сговоре с мафией. Побеждаешь, если останешься единственным живым игроком.",
    "civilian": "Особых способностей нет. Твоя задача — вычислить мафию и маньяка на голосовании днём.",
}

# Категория, которую видит комиссар при проверке
DETECTIVE_RESULT = {
    "mafia": "🔪 Мафия!",
    "maniac": "🗡 Маньяк-одиночка!",
    "doctor": "👤 Мирный житель.",
    "detective": "👤 Мирный житель.",
    "civilian": "👤 Мирный житель.",
}


def _format_link(user_id, username, full_name):
    display_name = escape(full_name or username or str(user_id))
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'


def _alive_ids(game):
    return [uid for uid in game["order"] if game["players"][uid]["alive"]]


def _alive_with_role(game, role):
    return [uid for uid in _alive_ids(game) if game["players"][uid]["role"] == role]


def _alive_mafia(game):
    return _alive_with_role(game, "mafia")


def _alive_non_mafia(game):
    return [uid for uid in _alive_ids(game) if game["players"][uid]["role"] != "mafia"]


def _role_counts_for(count):
    """Подбирает состав ролей в зависимости от числа игроков."""
    if count < 5:
        return {"mafia": 1, "doctor": 0, "detective": 0, "maniac": 0}
    if count < 7:
        return {"mafia": 1, "doctor": 1, "detective": 1, "maniac": 0}
    if count < 10:
        return {"mafia": 2, "doctor": 1, "detective": 1, "maniac": 0}
    return {"mafia": 2, "doctor": 1, "detective": 1, "maniac": 1}


def _assign_roles(game):
    player_ids = list(game["players"].keys())
    random.shuffle(player_ids)
    counts = _role_counts_for(len(player_ids))

    idx = 0
    for role in ("mafia", "doctor", "detective", "maniac"):
        for _ in range(counts[role]):
            game["players"][player_ids[idx]]["role"] = role
            idx += 1
    for uid in player_ids[idx:]:
        game["players"][uid]["role"] = "civilian"
    return counts


def _role_button_row(chat_id):
    return [InlineKeyboardButton("🎭 Моя роль", callback_data=f"mr:{chat_id}")]


def _with_role_button(chat_id, rows):
    return InlineKeyboardMarkup(rows + [_role_button_row(chat_id)])


def _role_alert_text(game, uid):
    role = game["players"][uid]["role"]
    lines = [ROLE_NAMES[role], ROLE_DESCRIPTIONS[role]]
    if role == "mafia":
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
        _role_button_row(chat_id),
        [InlineKeyboardButton("▶️ Начать игру", callback_data=f"ms:{chat_id}")],
    ])


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
        "night_steps": [],
        "night_step_index": 0,
        "current_step": None,
        "step_message_id": None,
        "step_timer_job": None,
        "mafia_votes": {},
        "maniac_target": None,
        "doctor_target": None,
        "day_votes": {},
        "lobby_message_id": None,
        "voting_message_id": None,
    }
    MAFIA_GAMES[chat_id] = game

    message = await update.message.reply_text(
        _lobby_text(game),
        parse_mode="HTML",
        reply_markup=_lobby_keyboard(chat_id)
    )
    game["lobby_message_id"] = message.message_id


def cancel_pending_timer(game):
    job = game.get("step_timer_job")
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass
        game["step_timer_job"] = None


# ---------- Ночь: пошаговые ходы ----------

def _active_night_steps(game):
    steps = []
    if _alive_mafia(game):
        steps.append("mafia")
    if _alive_with_role(game, "maniac"):
        steps.append("maniac")
    if _alive_with_role(game, "detective"):
        steps.append("detective")
    if _alive_with_role(game, "doctor"):
        steps.append("doctor")
    return steps


def _mafia_vote_keyboard(chat_id, game):
    targets = _alive_non_mafia(game)
    counts = {}
    for t in game["mafia_votes"].values():
        counts[t] = counts.get(t, 0) + 1
    rows = [
        [InlineKeyboardButton(
            f"🔪 {counts.get(uid, 0)} — {game['players'][uid]['name']}",
            callback_data=f"mk:{chat_id}:{uid}"
        )]
        for uid in targets
    ]
    return _with_role_button(chat_id, rows)


def _single_target_keyboard(chat_id, action, targets, game):
    rows = [
        [InlineKeyboardButton(game["players"][uid]["name"], callback_data=f"{action}:{chat_id}:{uid}")]
        for uid in targets
    ]
    return _with_role_button(chat_id, rows)


def _step_prompt(chat_id, game, step):
    if step == "mafia":
        text = f"🔪 <b>Ход мафии</b> ({STEP_SECONDS} сек). Мафия выбирает жертву:"
        kb = _mafia_vote_keyboard(chat_id, game)
    elif step == "maniac":
        maniac_id = _alive_with_role(game, "maniac")[0]
        targets = [uid for uid in _alive_ids(game) if uid != maniac_id]
        text = f"🗡 <b>Ход маньяка</b> ({STEP_SECONDS} сек). Маньяк выбирает жертву:"
        kb = _single_target_keyboard(chat_id, "mm", targets, game)
    elif step == "detective":
        det_id = _alive_with_role(game, "detective")[0]
        targets = [uid for uid in _alive_ids(game) if uid != det_id]
        text = f"🕵️ <b>Ход комиссара</b> ({STEP_SECONDS} сек). Комиссар выбирает, кого проверить:"
        kb = _single_target_keyboard(chat_id, "mc", targets, game)
    else:  # doctor
        targets = _alive_ids(game)
        text = f"💊 <b>Ход доктора</b> ({STEP_SECONDS} сек). Доктор выбирает, кого спасти:"
        kb = _single_target_keyboard(chat_id, "md", targets, game)
    return text, kb


async def _start_night(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "night"
    game["round_index"] += 1
    game["mafia_votes"] = {}
    game["maniac_target"] = None
    game["doctor_target"] = None
    game["night_steps"] = _active_night_steps(game)
    game["night_step_index"] = 0
    game["current_step"] = None

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 <b>Ночь {game['round_index']}</b>. Город засыпает...",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([_role_button_row(chat_id)])
    )
    await _run_next_night_step(chat_id, context)


async def _run_next_night_step(chat_id, context):
    game = MAFIA_GAMES.get(chat_id)
    if not game or game["phase"] != "night":
        return
    idx = game["night_step_index"]
    steps = game["night_steps"]
    if idx >= len(steps):
        await _resolve_night(chat_id, context)
        return

    step = steps[idx]
    game["current_step"] = step
    text, kb = _step_prompt(chat_id, game, step)
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)
    game["step_message_id"] = msg.message_id

    job = context.job_queue.run_once(
        _step_timeout_job,
        when=STEP_SECONDS,
        data={"chat_id": chat_id, "step": step}
    )
    game["step_timer_job"] = job


async def _step_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    step = data["step"]
    game = MAFIA_GAMES.get(chat_id)
    if not game or game["phase"] != "night" or game.get("current_step") != step:
        return
    game["step_timer_job"] = None
    await _advance_night_step(chat_id, context)


async def _advance_night_step(chat_id, context):
    """Завершает текущий ночной ход: убирает таймер, удаляет сообщение, идёт дальше."""
    game = MAFIA_GAMES.get(chat_id)
    if not game:
        return
    cancel_pending_timer(game)
    msg_id = game.get("step_message_id")
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    game["step_message_id"] = None
    game["current_step"] = None
    game["night_step_index"] += 1
    await _run_next_night_step(chat_id, context)


async def _resolve_night(chat_id, context):
    game = MAFIA_GAMES[chat_id]

    mafia_target = None
    if game["mafia_votes"]:
        counts = {}
        for target in game["mafia_votes"].values():
            counts[target] = counts.get(target, 0) + 1
        top = max(counts.values())
        leaders = [uid for uid, c in counts.items() if c == top]
        mafia_target = random.choice(leaders)

    maniac_target = game.get("maniac_target")
    doctor_target = game.get("doctor_target")

    deaths = set()
    if mafia_target is not None and mafia_target != doctor_target:
        deaths.add(mafia_target)
    if maniac_target is not None and maniac_target != doctor_target:
        deaths.add(maniac_target)

    if not deaths:
        text = (
            "🌅 Этой ночью никто не погиб.\n"
            "Возможно, доктор кого-то спас, а возможно, убийцы так и не выбрали жертву."
        )
    else:
        lines = [f"🌅 <b>Утро после {game['round_index']}-й ночи</b>\n"]
        for uid in deaths:
            game["players"][uid]["alive"] = False
            name = escape(game["players"][uid]["name"])
            role_name = ROLE_NAMES[game["players"][uid]["role"]]
            lines.append(f"💀 {name} — погиб(ла). Его роль: {role_name}.")
        text = "\n".join(lines)

    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([_role_button_row(chat_id)])
    )

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
    rows = [
        [InlineKeyboardButton(
            f"🗳 {counts.get(uid, 0)} — {game['players'][uid]['name']}",
            callback_data=f"mv:{chat_id}:{uid}"
        )]
        for uid in alive
    ]
    rows.append([InlineKeyboardButton("⏭ Пропустить голос", callback_data=f"mv:{chat_id}:skip")])
    return _with_role_button(chat_id, rows)


async def _start_day_voting(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "day_voting"
    game["day_votes"] = {}
    alive = _alive_ids(game)

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=_voting_text(game, alive),
        parse_mode="HTML",
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
        if len(leaders) == 1:
            lynched_id = leaders[0]

    if lynched_id is None:
        text = "🤷 Город не смог определиться (или все проголосовали за пропуск) — сегодня никого не линчуют."
    else:
        game["players"][lynched_id]["alive"] = False
        name = escape(game["players"][lynched_id]["name"])
        role_name = ROLE_NAMES[game["players"][lynched_id]["role"]]
        text = f"⚖️ По итогам голосования линчован(а) {name}. Его роль: {role_name}."

    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([_role_button_row(chat_id)])
    )

    winner = _check_win(game)
    if winner:
        await _finish_game(chat_id, context, winner)
        return

    await _start_night(chat_id, context)


# ---------- Победа / завершение ----------

def _check_win(game):
    alive = _alive_ids(game)
    mafia_alive = [uid for uid in alive if game["players"][uid]["role"] == "mafia"]
    maniac_alive = [uid for uid in alive if game["players"][uid]["role"] == "maniac"]
    town_alive = [uid for uid in alive if game["players"][uid]["role"] not in ("mafia", "maniac")]

    if len(alive) <= 1 and maniac_alive:
        return "maniac"
    if not mafia_alive and not maniac_alive:
        return "civilians"
    if not town_alive and not maniac_alive:
        return "mafia"
    if not town_alive and not mafia_alive and maniac_alive:
        return "maniac"
    if not maniac_alive and len(mafia_alive) >= len(town_alive):
        return "mafia"
    return None


async def _finish_game(chat_id, context, winner):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "finished"
    cancel_pending_timer(game)

    if winner == "mafia":
        header = "🔪 <b>Победила мафия!</b>"
    elif winner == "maniac":
        header = "🗡 <b>Победил маньяк-одиночка!</b>"
    else:
        header = "🎉 <b>Победили мирные жители!</b>"

    lines = [header, "", "Итоговые роли:"]
    for uid in game["order"]:
        p = game["players"][uid]
        status = "живой" if p["alive"] else "выбыл(а)"
        lines.append(f"{ROLE_NAMES[p['role']]} — {escape(p['name'])} ({status})")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
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

    # ---- Моя роль (доступна всем игрокам в любой момент, кроме лобби) ----
    if action == "mr":
        if user.id not in game["players"]:
            await query.answer("Ты ещё не присоединился(-ась) к игре.", show_alert=True)
            return
        if game["phase"] == "lobby":
            await query.answer("Роли ещё не розданы — игра не началась.", show_alert=True)
            return
        await query.answer(text=_role_alert_text(game, user.id), show_alert=True)
        return

    # ---- Лобби ----
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
            parse_mode="HTML",
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

        counts = _assign_roles(game)
        civilian_n = len(game["players"]) - sum(counts.values())

        await query.answer("Игра начинается!")
        await query.edit_message_reply_markup(reply_markup=None)
        role_summary = (
            f"🔪 Мафия ×{counts['mafia']}, 💊 Доктор ×{counts['doctor']}, "
            f"🕵️ Комиссар ×{counts['detective']}"
        )
        if counts["maniac"]:
            role_summary += f", 🗡 Маньяк ×{counts['maniac']}"
        role_summary += f", 👤 Мирные ×{civilian_n}"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚨 Игра началась! Игроков: {len(game['players'])}.\n"
                f"Состав ролей: {role_summary}.\n"
                f"Ночью каждая роль ходит по очереди, на ход даётся {STEP_SECONDS} секунд. "
                f"Свою роль можно посмотреть кнопкой «🎭 Моя роль» под любым сообщением игры."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([_role_button_row(chat_id)])
        )
        await _start_night(chat_id, context)
        return

    # ---- Ход мафии ----
    if action == "mk":
        if game["phase"] != "night" or game.get("current_step") != "mafia":
            await query.answer("Сейчас не ход мафии.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "mafia":
            await query.answer("Выбирать жертву может только живой участник мафии.", show_alert=True)
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

        if len(game["mafia_votes"]) >= len(_alive_mafia(game)):
            await _advance_night_step(chat_id, context)
        else:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=game["step_message_id"],
                    reply_markup=_mafia_vote_keyboard(chat_id, game)
                )
            except Exception:
                pass
        return

    # ---- Ход маньяка ----
    if action == "mm":
        if game["phase"] != "night" or game.get("current_step") != "maniac":
            await query.answer("Сейчас не ход маньяка.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "maniac":
            await query.answer("Выбирать жертву может только живой маньяк.", show_alert=True)
            return
        if len(parts) < 3:
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"] or target_id == user.id:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["maniac_target"] = target_id
        await query.answer(f"Ты выбрал(а) жертву: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход комиссара ----
    if action == "mc":
        if game["phase"] != "night" or game.get("current_step") != "detective":
            await query.answer("Сейчас не ход комиссара.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "detective":
            await query.answer("Проверять может только живой комиссар.", show_alert=True)
            return
        if len(parts) < 3:
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"] or target_id == user.id:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        result = DETECTIVE_RESULT[game["players"][target_id]["role"]]
        target_name = escape(game["players"][target_id]["name"])
        await query.answer(text=f"{target_name}: {result}", show_alert=True)
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход доктора ----
    if action == "md":
        if game["phase"] != "night" or game.get("current_step") != "doctor":
            await query.answer("Сейчас не ход доктора.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "doctor":
            await query.answer("Лечить может только живой доктор.", show_alert=True)
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
        await query.answer(f"Ты выбрал(а) спасти: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Дневное голосование ----
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
                parse_mode="HTML",
                reply_markup=_voting_keyboard(chat_id, game, alive)
            )
        except Exception:
            pass

        if len(game["day_votes"]) >= len(alive):
            await _tally_day_votes(chat_id, context)
        return

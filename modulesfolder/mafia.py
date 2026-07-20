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
ROLE_REMINDER_SECONDS = 20  # Сколько висит предупреждение "посмотри свою роль" перед первой ночью

ROLE_NAMES = {
    "mafia": "🔪 Мафия",
    "don": "🎩 Дон",
    "vampire": "🧛 Кровопийца",
    "doctor": "💊 Доктор",
    "detective": "🕵️ Комиссар",
    "sergeant": "👮 Сержант",
    "maniac": "🗡 Маньяк",
    "jester": "🃏 Шут",
    "vigilante": "🔫 Мститель",
    "put": "💋 Путана",
    "lawyer": "⚖️ Адвокат",
    "witch": "🧙‍♀️ Ведьма",
    "provocateur": "🎭 Провокатор",
    "werewolf": "🐺 Оборотень",
    "informant": "🕵️‍♂️ Информатор",
    "civilian": "👤 Мирный житель",
}

ROLE_DESCRIPTIONS = {
    "mafia": "Каждую ночь вместе с остальной мафией (и Доном) выбираешь, кого убрать. Днём притворяйся мирным.",
    "don": "Глава мафии. Голосуешь вместе с мафией за жертву, а также раз в ночь можешь проверить любого игрока и узнать, не комиссар ли он. При проверке комиссаром ты выглядишь как мирный житель.",
    "vampire": "Член мафии. Голосуешь вместе с остальной мафией за общую жертву. Дополнительно один раз за игру можешь тайно «напиться крови» другого игрока на отдельном ходу — если доктор его не спасёт, он погибнет этой же ночью независимо от общего выбора мафии. При проверке комиссаром ты выглядишь как обычная мафия.",
    "doctor": "Каждую ночь можешь спасти одного игрока (в т.ч. себя) от гибели.",
    "detective": "Каждую ночь можешь проверить одного игрока и узнать его принадлежность.",
    "sergeant": "Каждую ночь надеваешь наручники на одного игрока — на следующий день он не сможет голосовать.",
    "maniac": "Каждую ночь в одиночку выбираешь жертву. Ты не в сговоре с мафией. Побеждаешь, если останешься единственным живым игроком.",
    "jester": "Особых способностей нет. Твоя единственная цель — быть линчёванным на голосовании днём: если тебя казнят, ты побеждаешь в одиночку, а все остальные проигрывают.",
    "vigilante": "Ты на стороне мирных. Один раз за всю игру можешь ночью выстрелить и убить любого игрока (кроме себя). Способность одноразовая, используй с умом.",
    "put": "Каждую ночь выбираешь одного игрока и «отвлекаешь» его — этой ночью он не сможет использовать свою способность (и не сможет проголосовать, если это единственный голос мафии).",
    "lawyer": "Один раз за игру можешь ночью взять игрока под защиту — если днём его выберут для казни, приговор будет отменён.",
    "witch": "Один раз за игру можешь ночью воскресить любого погибшего игрока.",
    "provocateur": "Ты на стороне мирных. Каждую ночь можешь спровоцировать одного игрока — узнаешь только сам факт, действовал ли он этой ночью (использовал способность/голосовал), но не что именно он делал.",
    "werewolf": "Каждую ночь в одиночку выбираешь жертву, как маньяк. Ты не в сговоре с мафией, но для комиссара при проверке выглядишь как мафия. Побеждаешь, если останешься единственным живым игроком.",
    "informant": "Ты на стороне мирных. Один раз за игру можешь ночью запросить сводку — узнать, сколько мафии (включая Дона) ещё живо. Личности не раскрываются.",
    "civilian": "Особых способностей нет. Твоя задача — вычислить мафию и маньяка на голосовании днём.",
}

# Категория, которую видит комиссар при проверке
DETECTIVE_RESULT = {
    "mafia": "🔪 Мафия!",
    "don": "👤 Мирный житель.",  # Дон невидим для комиссара
    "vampire": "🔪 Мафия!",
    "maniac": "🗡 Маньяк-одиночка!",
    "doctor": "👤 Мирный житель.",
    "detective": "👤 Мирный житель.",
    "sergeant": "👤 Мирный житель.",
    "jester": "👤 Мирный житель.",
    "vigilante": "👤 Мирный житель.",
    "put": "👤 Мирный житель.",
    "lawyer": "👤 Мирный житель.",
    "witch": "👤 Мирный житель.",
    "provocateur": "👤 Мирный житель.",
    "werewolf": "🔪 Мафия!",  # Оборотень маскируется под мафию для комиссара
    "informant": "👤 Мирный житель.",
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
    """Мафия + Дон + Кровопийца — общая команда для ночного убийства и подсчёта победы."""
    return _alive_with_role(game, "mafia") + _alive_with_role(game, "don") + _alive_with_role(game, "vampire")


def _effective_mafia_voters(game):
    """Живые мафиози/Дон/Кровопийца, которые не отвлечены путаной этой ночью."""
    return [uid for uid in _alive_mafia(game) if uid != game.get("put_target")]


def _alive_non_mafia(game):
    return [uid for uid in _alive_ids(game) if game["players"][uid]["role"] not in ("mafia", "don", "vampire")]


def _role_counts_for(count):
    """Подбирает состав ролей в зависимости от числа игроков (поддержка лобби до 20 человек)."""
    base = {"mafia": 1, "don": 0, "doctor": 0, "detective": 0, "sergeant": 0,
            "maniac": 0, "jester": 0, "vigilante": 0, "put": 0, "lawyer": 0, "witch": 0,
            "provocateur": 0, "werewolf": 0, "informant": 0, "vampire": 0}
    if count < 5:
        return base
    if count < 7:
        return {**base, "doctor": 1, "detective": 1}
    if count < 9:
        return {**base, "mafia": 1, "don": 1, "doctor": 1, "detective": 1, "jester": 1}
    if count < 11:
        return {**base, "mafia": 1, "don": 1, "doctor": 1, "detective": 1, "jester": 1, "vigilante": 1}
    if count < 13:
        return {**base, "mafia": 2, "don": 1, "doctor": 1, "detective": 1, "maniac": 1,
                "jester": 1, "vigilante": 1}
    if count < 16:
        return {**base, "mafia": 2, "don": 1, "doctor": 1, "detective": 1, "maniac": 1,
                "jester": 1, "vigilante": 1, "put": 1, "sergeant": 1, "informant": 1}
    if count < 19:
        return {**base, "mafia": 3, "don": 1, "doctor": 1, "detective": 1, "maniac": 2,
                "jester": 1, "vigilante": 1, "put": 1, "sergeant": 1, "lawyer": 1,
                "informant": 1, "provocateur": 1, "vampire": 1}
    # 19-20 игроков
    return {**base, "mafia": 3, "don": 1, "doctor": 1, "detective": 1, "maniac": 2,
            "jester": 1, "vigilante": 1, "put": 1, "sergeant": 1, "lawyer": 1, "witch": 1,
            "informant": 1, "provocateur": 1, "werewolf": 1, "vampire": 1}


MAX_PLAYERS = 20

ROLE_ASSIGN_ORDER = ("mafia", "don", "vampire", "doctor", "detective", "sergeant",
                     "maniac", "werewolf", "jester", "vigilante", "put", "lawyer", "witch",
                     "informant", "provocateur")


def _assign_roles(game):
    player_ids = list(game["players"].keys())
    random.shuffle(player_ids)
    counts = _role_counts_for(len(player_ids))

    idx = 0
    for role in ROLE_ASSIGN_ORDER:
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
    if role in ("mafia", "don", "vampire"):
        teammates = [game["players"][m]["name"] for m in game["order"]
                     if game["players"][m]["role"] in ("mafia", "don", "vampire") and m != uid]
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
        f"Нужно минимум 4 игрока (максимум {MAX_PLAYERS}). Нажмите кнопку, чтобы присоединиться.\n"
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
        "werewolf_target": None,
        "vampire_target": None,
        "vigilante_target": None,
        "doctor_target": None,
        "put_target": None,
        "lawyer_target": None,
        "cuffed_id": None,
        "night_actors": set(),
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
    if _alive_with_role(game, "put"):
        steps.append("put")
    if _alive_mafia(game):
        steps.append("mafia")
    if _alive_with_role(game, "don"):
        steps.append("don_check")
    if any(not game["players"][uid]["ability_used"] for uid in _alive_with_role(game, "vampire")):
        steps.append("vampire")
    if _alive_with_role(game, "maniac"):
        steps.append("maniac")
    if _alive_with_role(game, "werewolf"):
        steps.append("werewolf")
    if any(not game["players"][uid]["ability_used"] for uid in _alive_with_role(game, "vigilante")):
        steps.append("vigilante")
    if any(not game["players"][uid]["ability_used"] for uid in _alive_with_role(game, "witch")):
        steps.append("witch")
    if any(not game["players"][uid]["ability_used"] for uid in _alive_with_role(game, "lawyer")):
        steps.append("lawyer")
    if any(not game["players"][uid]["ability_used"] for uid in _alive_with_role(game, "informant")):
        steps.append("informant")
    if _alive_with_role(game, "sergeant"):
        steps.append("sergeant")
    if _alive_with_role(game, "detective"):
        steps.append("detective")
    if _alive_with_role(game, "doctor"):
        steps.append("doctor")
    # Провокатор ходит последним — чтобы узнать, кто действовал этой ночью
    if _alive_with_role(game, "provocateur"):
        steps.append("provocateur")
    return steps


def _step_role_holders(game, step):
    """Живые исполнители данного ночного хода (без учёта блокировки путаной)."""
    if step == "put":
        return _alive_with_role(game, "put")
    if step == "mafia":
        return _alive_mafia(game)
    if step == "don_check":
        return _alive_with_role(game, "don")
    if step == "vampire":
        return [uid for uid in _alive_with_role(game, "vampire") if not game["players"][uid]["ability_used"]]
    if step == "maniac":
        return _alive_with_role(game, "maniac")
    if step == "werewolf":
        return _alive_with_role(game, "werewolf")
    if step == "informant":
        return [uid for uid in _alive_with_role(game, "informant") if not game["players"][uid]["ability_used"]]
    if step == "provocateur":
        return _alive_with_role(game, "provocateur")
    if step == "vigilante":
        return [uid for uid in _alive_with_role(game, "vigilante") if not game["players"][uid]["ability_used"]]
    if step == "witch":
        if not any(not p["alive"] for p in game["players"].values()):
            return []  # некого воскрешать
        return [uid for uid in _alive_with_role(game, "witch") if not game["players"][uid]["ability_used"]]
    if step == "lawyer":
        return [uid for uid in _alive_with_role(game, "lawyer") if not game["players"][uid]["ability_used"]]
    if step == "sergeant":
        return _alive_with_role(game, "sergeant")
    if step == "detective":
        return _alive_with_role(game, "detective")
    if step == "doctor":
        return _alive_with_role(game, "doctor")
    return []


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


def _single_target_keyboard(chat_id, action, targets, game, skip=False):
    rows = [
        [InlineKeyboardButton(game["players"][uid]["name"], callback_data=f"{action}:{chat_id}:{uid}")]
        for uid in targets
    ]
    if skip:
        rows.append([InlineKeyboardButton("⏭ Пропустить (не тратить способность)", callback_data=f"{action}:{chat_id}:skip")])
    return _with_role_button(chat_id, rows)


def _informant_keyboard(chat_id):
    rows = [
        [InlineKeyboardButton("🔍 Узнать сводку", callback_data=f"mi:{chat_id}:use")],
        [InlineKeyboardButton("⏭ Пропустить (не тратить способность)", callback_data=f"mi:{chat_id}:skip")],
    ]
    return _with_role_button(chat_id, rows)


def _step_prompt(chat_id, game, step):
    if step == "put":
        put_id = _alive_with_role(game, "put")[0]
        targets = [uid for uid in _alive_ids(game) if uid != put_id]
        text = f"💋 <b>Ход путаны</b> ({STEP_SECONDS} сек). Путана выбирает, кого «отвлечь» этой ночью:"
        kb = _single_target_keyboard(chat_id, "mp", targets, game)
    elif step == "mafia":
        text = f"🔪 <b>Ход мафии</b> ({STEP_SECONDS} сек). Мафия выбирает жертву:"
        kb = _mafia_vote_keyboard(chat_id, game)
    elif step == "don_check":
        don_id = _alive_with_role(game, "don")[0]
        targets = [uid for uid in _alive_ids(game) if uid != don_id]
        text = f"🎩 <b>Ход Дона</b> ({STEP_SECONDS} сек). Дон проверяет, не комиссар ли игрок:"
        kb = _single_target_keyboard(chat_id, "mo", targets, game)
    elif step == "vampire":
        vamp_id = next(uid for uid in _alive_with_role(game, "vampire") if not game["players"][uid]["ability_used"])
        targets = [uid for uid in _alive_ids(game) if uid != vamp_id]
        text = (
            f"🧛 <b>Ход Кровопийцы</b> ({STEP_SECONDS} сек). Разовая тайная жертва — можешь напиться крови "
            f"или пропустить (способность потратится только при использовании):"
        )
        kb = _single_target_keyboard(chat_id, "mbl", targets, game, skip=True)
    elif step == "maniac":
        maniac_id = _alive_with_role(game, "maniac")[0]
        targets = [uid for uid in _alive_ids(game) if uid != maniac_id]
        text = f"🗡 <b>Ход маньяка</b> ({STEP_SECONDS} сек). Маньяк выбирает жертву:"
        kb = _single_target_keyboard(chat_id, "mm", targets, game)
    elif step == "werewolf":
        wolf_id = _alive_with_role(game, "werewolf")[0]
        targets = [uid for uid in _alive_ids(game) if uid != wolf_id]
        text = f"🐺 <b>Ход оборотня</b> ({STEP_SECONDS} сек). Оборотень выбирает жертву:"
        kb = _single_target_keyboard(chat_id, "mww", targets, game)
    elif step == "vigilante":
        vig_id = next(uid for uid in _alive_with_role(game, "vigilante") if not game["players"][uid]["ability_used"])
        targets = [uid for uid in _alive_ids(game) if uid != vig_id]
        text = (
            f"🔫 <b>Ход мстителя</b> ({STEP_SECONDS} сек). Разовый выстрел — можешь выбрать цель "
            f"или пропустить (способность потратится только при выстреле):"
        )
        kb = _single_target_keyboard(chat_id, "mg", targets, game, skip=True)
    elif step == "detective":
        det_id = _alive_with_role(game, "detective")[0]
        targets = [uid for uid in _alive_ids(game) if uid != det_id]
        text = f"🕵️ <b>Ход комиссара</b> ({STEP_SECONDS} сек). Комиссар выбирает, кого проверить:"
        kb = _single_target_keyboard(chat_id, "mc", targets, game)
    elif step == "witch":
        dead = [uid for uid in game["order"] if not game["players"][uid]["alive"]]
        text = (
            f"🧙‍♀️ <b>Ход ведьмы</b> ({STEP_SECONDS} сек). Ведьма может воскресить одного погибшего "
            f"(разово за игру) или пропустить:"
        )
        kb = _single_target_keyboard(chat_id, "mw", dead, game, skip=True)
    elif step == "lawyer":
        targets = _alive_ids(game)
        text = (
            f"⚖️ <b>Ход адвоката</b> ({STEP_SECONDS} сек). Адвокат может взять игрока под защиту от "
            f"завтрашней казни (разово за игру) или пропустить:"
        )
        kb = _single_target_keyboard(chat_id, "ml", targets, game, skip=True)
    elif step == "informant":
        text = (
            f"🕵️‍♂️ <b>Ход информатора</b> ({STEP_SECONDS} сек). Можно запросить сводку о живой мафии "
            f"(разово за игру) или пропустить:"
        )
        kb = _informant_keyboard(chat_id)
    elif step == "provocateur":
        prov_id = _alive_with_role(game, "provocateur")[0]
        targets = [uid for uid in _alive_ids(game) if uid != prov_id]
        text = (
            f"🎭 <b>Ход провокатора</b> ({STEP_SECONDS} сек). Выбери, кого спровоцировать — узнаешь, "
            f"действовал ли он этой ночью:"
        )
        kb = _single_target_keyboard(chat_id, "mprv", targets, game)
    elif step == "sergeant":
        sergeant_id = _alive_with_role(game, "sergeant")[0]
        targets = [uid for uid in _alive_ids(game) if uid != sergeant_id]
        text = (
            f"👮 <b>Ход сержанта</b> ({STEP_SECONDS} сек). Сержант надевает наручники — "
            f"выбранный не сможет проголосовать завтра:"
        )
        kb = _single_target_keyboard(chat_id, "msg", targets, game)
    else:  # doctor
        targets = _alive_ids(game)
        text = f"💊 <b>Ход доктора</b> ({STEP_SECONDS} сек). Доктор выбирает, кого спасти:"
        kb = _single_target_keyboard(chat_id, "md", targets, game)
    return text, kb


async def _start_role_reminder(chat_id, context):
    """Показывает предупреждение 'посмотри свою роль' (висит ROLE_REMINDER_SECONDS сек) — это ЕЩЁ НЕ ночь,
    просто пауза перед стартом, чтобы все успели глянуть свою роль кнопкой «🎭 Моя роль»."""
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "role_reminder"  # блокирует новые "mj" (присоединение) на время паузы

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👀 <b>Успейте посмотреть свою роль!</b>\n"
            f"У вас есть {ROLE_REMINDER_SECONDS} секунд — нажмите «🎭 Моя роль» под этим сообщением.\n"
            f"Это ещё не ночь, просто время подготовиться."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([_role_button_row(chat_id)])
    )

    job = context.job_queue.run_once(
        _role_reminder_timeout_job,
        when=ROLE_REMINDER_SECONDS,
        data={"chat_id": chat_id, "message_id": msg.message_id}
    )
    game["step_timer_job"] = job


async def _role_reminder_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    game = MAFIA_GAMES.get(chat_id)
    if not game or game["phase"] != "role_reminder":
        return
    game["step_timer_job"] = None
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=data["message_id"])
    except Exception:
        pass
    await _start_night(chat_id, context)


async def _start_night(chat_id, context):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "night"
    game["round_index"] += 1
    game["mafia_votes"] = {}
    game["maniac_target"] = None
    game["werewolf_target"] = None
    game["vampire_target"] = None
    game["vigilante_target"] = None
    game["doctor_target"] = None
    game["put_target"] = None
    game["lawyer_target"] = None
    game["cuffed_id"] = None
    game["night_actors"] = set()
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
    steps = game["night_steps"]

    while game["night_step_index"] < len(steps):
        step = steps[game["night_step_index"]]
        eligible = [h for h in _step_role_holders(game, step) if h != game.get("put_target")]
        if not eligible:
            # некому ходить (единственный исполнитель отвлечён путаной, или способность недоступна)
            game["night_step_index"] += 1
            continue
        break
    else:
        await _resolve_night(chat_id, context)
        return

    idx = game["night_step_index"]
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
    werewolf_target = game.get("werewolf_target")
    vampire_target = game.get("vampire_target")
    vigilante_target = game.get("vigilante_target")
    doctor_target = game.get("doctor_target")

    deaths = set()
    if mafia_target is not None and mafia_target != doctor_target:
        deaths.add(mafia_target)
    if maniac_target is not None and maniac_target != doctor_target:
        deaths.add(maniac_target)
    if werewolf_target is not None and werewolf_target != doctor_target:
        deaths.add(werewolf_target)
    if vampire_target is not None and vampire_target != doctor_target:
        deaths.add(vampire_target)
    if vigilante_target is not None and vigilante_target != doctor_target:
        deaths.add(vigilante_target)

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

def _voting_text(game, alive, eligible=None):
    if eligible is None:
        eligible = [uid for uid in alive if uid != game.get("cuffed_id")]
    lines = ["🗳 <b>Голосование: кого линчуем сегодня?</b>\n"]
    lines.append(f"Проголосовали: {len(game['day_votes'])}/{len(eligible)}\n")
    if game.get("cuffed_id") in alive:
        cuffed_name = escape(game["players"][game["cuffed_id"]]["name"])
        lines.append(f"🔒 {cuffed_name} в наручниках и не голосует сегодня.\n")
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

    saved_by_lawyer = lynched_id is not None and lynched_id == game.get("lawyer_target")

    if lynched_id is None:
        text = "🤷 Город не смог определиться (или все проголосовали за пропуск) — сегодня никого не линчуют."
    elif saved_by_lawyer:
        name = escape(game["players"][lynched_id]["name"])
        text = f"⚖️ Город проголосовал за казнь {name}, но адвокат добился оправдания — казнь отменена!"
    else:
        game["players"][lynched_id]["alive"] = False
        name = escape(game["players"][lynched_id]["name"])
        role_name = ROLE_NAMES[game["players"][lynched_id]["role"]]
        text = f"⚖️ По итогам голосования линчован(а) {name}. Его роль: {role_name}."

    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([_role_button_row(chat_id)])
    )

    # Шут побеждает немедленно, если именно его линчевали (и адвокат его не спас)
    if lynched_id is not None and not saved_by_lawyer and game["players"][lynched_id]["role"] == "jester":
        await _finish_game(chat_id, context, "jester")
        return

    winner = _check_win(game)
    if winner:
        await _finish_game(chat_id, context, winner)
        return

    await _start_night(chat_id, context)


# ---------- Победа / завершение ----------

def _check_win(game):
    alive = _alive_ids(game)
    mafia_alive = [uid for uid in alive if game["players"][uid]["role"] in ("mafia", "don", "vampire")]
    # Маньяк и Оборотень — оба одиночки-убийцы вне сговора с мафией и городом.
    solo_alive = [uid for uid in alive if game["players"][uid]["role"] in ("maniac", "werewolf")]
    town_alive = [uid for uid in alive
                  if game["players"][uid]["role"] not in ("mafia", "don", "vampire", "maniac", "werewolf")]

    if len(alive) <= 1 and solo_alive:
        return "solo"
    if not mafia_alive and not solo_alive:
        return "civilians"
    if not town_alive and not solo_alive:
        return "mafia"
    if not town_alive and not mafia_alive and solo_alive:
        return "solo"
    if not solo_alive and len(mafia_alive) >= len(town_alive):
        return "mafia"
    return None


async def _finish_game(chat_id, context, winner):
    game = MAFIA_GAMES[chat_id]
    game["phase"] = "finished"
    cancel_pending_timer(game)

    if winner == "mafia":
        header = "🔪 <b>Победила мафия!</b>"
    elif winner == "solo":
        solo_survivor = next(
            (uid for uid in game["order"]
             if game["players"][uid]["alive"] and game["players"][uid]["role"] in ("maniac", "werewolf")),
            None
        )
        if solo_survivor and game["players"][solo_survivor]["role"] == "werewolf":
            header = "🐺 <b>Победил оборотень-одиночка!</b>"
        else:
            header = "🗡 <b>Победил маньяк-одиночка!</b>"
    elif winner == "jester":
        header = "🃏 <b>Победил Шут!</b> Он специально добился своей казни — все остальные проиграли."
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
        if len(game["players"]) >= MAX_PLAYERS:
            await query.answer(f"⛔ Лобби заполнено (максимум {MAX_PLAYERS} игроков).", show_alert=True)
            return
        game["players"][user.id] = {
            "name": user.full_name or user.username or str(user.id),
            "role": None,
            "alive": True,
            "ability_used": False,
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
        if counts["don"]:
            role_summary += f", 🎩 Дон ×{counts['don']}"
        if counts["vampire"]:
            role_summary += f", 🧛 Кровопийца ×{counts['vampire']}"
        if counts["maniac"]:
            role_summary += f", 🗡 Маньяк ×{counts['maniac']}"
        if counts["vigilante"]:
            role_summary += f", 🔫 Мститель ×{counts['vigilante']}"
        if counts["jester"]:
            role_summary += f", 🃏 Шут ×{counts['jester']}"
        if counts["put"]:
            role_summary += f", 💋 Путана ×{counts['put']}"
        if counts["sergeant"]:
            role_summary += f", 👮 Сержант ×{counts['sergeant']}"
        if counts["lawyer"]:
            role_summary += f", ⚖️ Адвокат ×{counts['lawyer']}"
        if counts["witch"]:
            role_summary += f", 🧙‍♀️ Ведьма ×{counts['witch']}"
        if counts["werewolf"]:
            role_summary += f", 🐺 Оборотень ×{counts['werewolf']}"
        if counts["provocateur"]:
            role_summary += f", 🎭 Провокатор ×{counts['provocateur']}"
        if counts["informant"]:
            role_summary += f", 🕵️‍♂️ Информатор ×{counts['informant']}"
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
        await _start_role_reminder(chat_id, context)
        return

    # ---- Ход мафии ----
    if action == "mk":
        if game["phase"] != "night" or game.get("current_step") != "mafia":
            await query.answer("Сейчас не ход мафии.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] not in ("mafia", "don", "vampire"):
            await query.answer("Выбирать жертву может только живой участник мафии.", show_alert=True)
            return
        if user.id == game.get("put_target"):
            await query.answer("Тебя этой ночью отвлекла путана — ты не можешь голосовать.", show_alert=True)
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
                game["players"][target_id]["role"] in ("mafia", "don", "vampire"):
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["mafia_votes"][user.id] = target_id
        game["night_actors"].add(user.id)
        await query.answer(f"Голос принят! Цель: {game['players'][target_id]['name']}")

        if len(game["mafia_votes"]) >= len(_effective_mafia_voters(game)):
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

    # ---- Ход путаны ----
    if action == "mp":
        if game["phase"] != "night" or game.get("current_step") != "put":
            await query.answer("Сейчас не ход путаны.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "put":
            await query.answer("Отвлекать может только живая путана.", show_alert=True)
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
        game["put_target"] = target_id
        game["night_actors"].add(user.id)
        await query.answer(f"Ты отвлекла: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Проверка Дона ----
    if action == "mo":
        if game["phase"] != "night" or game.get("current_step") != "don_check":
            await query.answer("Сейчас не ход Дона.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "don":
            await query.answer("Проверять может только живой Дон.", show_alert=True)
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
        is_detective = game["players"][target_id]["role"] == "detective"
        target_name = escape(game["players"][target_id]["name"])
        result = "🕵️ Это комиссар!" if is_detective else "👤 Не комиссар."
        game["night_actors"].add(user.id)
        await query.answer(text=f"{target_name}: {result}", show_alert=True)
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход Кровопийцы (разовый тайный укус) ----
    if action == "mbl":
        if game["phase"] != "night" or game.get("current_step") != "vampire":
            await query.answer("Сейчас не ход Кровопийцы.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "vampire":
            await query.answer("Пить кровь может только живой Кровопийца.", show_alert=True)
            return
        if game["players"][user.id]["ability_used"]:
            await query.answer("Ты уже использовал(а) свою единственную способность.", show_alert=True)
            return
        if len(parts) < 3:
            return
        target_raw = parts[2]
        if target_raw == "skip":
            await query.answer("Пропустил(а) — способность сохранена на будущее.")
            await _advance_night_step(chat_id, context)
            return
        try:
            target_id = int(target_raw)
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"] or target_id == user.id:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["vampire_target"] = target_id
        game["players"][user.id]["ability_used"] = True
        game["night_actors"].add(user.id)
        await query.answer(f"Ты напился(-лась) крови: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход ведьмы ----
    if action == "mw":
        if game["phase"] != "night" or game.get("current_step") != "witch":
            await query.answer("Сейчас не ход ведьмы.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "witch":
            await query.answer("Воскрешать может только живая ведьма.", show_alert=True)
            return
        if game["players"][user.id]["ability_used"]:
            await query.answer("Ты уже использовала свою способность.", show_alert=True)
            return
        if len(parts) < 3:
            return
        target_raw = parts[2]
        if target_raw == "skip":
            await query.answer("Пропустила — способность сохранена на будущее.")
            await _advance_night_step(chat_id, context)
            return
        try:
            target_id = int(target_raw)
        except ValueError:
            return
        if target_id not in game["players"] or game["players"][target_id]["alive"]:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["players"][target_id]["alive"] = True
        game["players"][user.id]["ability_used"] = True
        game["night_actors"].add(user.id)
        await query.answer(f"Ты воскресила: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход адвоката ----
    if action == "ml":
        if game["phase"] != "night" or game.get("current_step") != "lawyer":
            await query.answer("Сейчас не ход адвоката.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "lawyer":
            await query.answer("Защищать может только живой адвокат.", show_alert=True)
            return
        if game["players"][user.id]["ability_used"]:
            await query.answer("Ты уже использовал(а) свою способность.", show_alert=True)
            return
        if len(parts) < 3:
            return
        target_raw = parts[2]
        if target_raw == "skip":
            await query.answer("Пропустил(а) — способность сохранена на будущее.")
            await _advance_night_step(chat_id, context)
            return
        try:
            target_id = int(target_raw)
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"]:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["lawyer_target"] = target_id
        game["players"][user.id]["ability_used"] = True
        game["night_actors"].add(user.id)
        await query.answer(f"Ты взял(а) под защиту: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход сержанта ----
    if action == "msg":
        if game["phase"] != "night" or game.get("current_step") != "sergeant":
            await query.answer("Сейчас не ход сержанта.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "sergeant":
            await query.answer("Заковывать может только живой сержант.", show_alert=True)
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
        game["cuffed_id"] = target_id
        game["night_actors"].add(user.id)
        await query.answer(f"Ты заковал(а): {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход оборотня ----
    if action == "mww":
        if game["phase"] != "night" or game.get("current_step") != "werewolf":
            await query.answer("Сейчас не ход оборотня.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "werewolf":
            await query.answer("Выбирать жертву может только живой оборотень.", show_alert=True)
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
        game["werewolf_target"] = target_id
        game["night_actors"].add(user.id)
        await query.answer(f"Ты выбрал(а) жертву: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход информатора ----
    if action == "mi":
        if game["phase"] != "night" or game.get("current_step") != "informant":
            await query.answer("Сейчас не ход информатора.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "informant":
            await query.answer("Запросить сводку может только живой информатор.", show_alert=True)
            return
        if game["players"][user.id]["ability_used"]:
            await query.answer("Ты уже использовал(а) свою способность.", show_alert=True)
            return
        if len(parts) < 3:
            return
        choice = parts[2]
        if choice == "skip":
            await query.answer("Пропустил(а) — способность сохранена на будущее.")
            await _advance_night_step(chat_id, context)
            return
        if choice != "use":
            return
        mafia_alive_n = len(_alive_mafia(game))
        game["players"][user.id]["ability_used"] = True
        game["night_actors"].add(user.id)
        await query.answer(text=f"🔪 Живой мафии (вкл. Дона) сейчас: {mafia_alive_n}.", show_alert=True)
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход провокатора ----
    if action == "mprv":
        if game["phase"] != "night" or game.get("current_step") != "provocateur":
            await query.answer("Сейчас не ход провокатора.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "provocateur":
            await query.answer("Провоцировать может только живой провокатор.", show_alert=True)
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
        target_name = escape(game["players"][target_id]["name"])
        acted = target_id in game["night_actors"]
        result = "действовал(а) этой ночью." if acted else "этой ночью бездействовал(а)."
        await query.answer(text=f"{target_name}: {result}", show_alert=True)
        await _advance_night_step(chat_id, context)
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
        game["night_actors"].add(user.id)
        await query.answer(f"Ты выбрал(а) жертву: {game['players'][target_id]['name']}")
        await _advance_night_step(chat_id, context)
        return

    # ---- Ход мстителя (разовый выстрел) ----
    if action == "mg":
        if game["phase"] != "night" or game.get("current_step") != "vigilante":
            await query.answer("Сейчас не ход мстителя.", show_alert=True)
            return
        if user.id not in game["players"] or not game["players"][user.id]["alive"] or \
                game["players"][user.id]["role"] != "vigilante":
            await query.answer("Стрелять может только живой мститель.", show_alert=True)
            return
        if game["players"][user.id]["ability_used"]:
            await query.answer("Ты уже использовал(а) свой единственный выстрел.", show_alert=True)
            return
        if len(parts) < 3:
            return
        target_raw = parts[2]
        if target_raw == "skip":
            await query.answer("Пропустил(а) — способность сохранена на будущее.")
            await _advance_night_step(chat_id, context)
            return
        try:
            target_id = int(target_raw)
        except ValueError:
            return
        if target_id not in game["players"] or not game["players"][target_id]["alive"] or target_id == user.id:
            await query.answer("Недопустимая цель.", show_alert=True)
            return
        game["vigilante_target"] = target_id
        game["players"][user.id]["ability_used"] = True
        game["night_actors"].add(user.id)
        await query.answer(f"Выстрел сделан! Цель: {game['players'][target_id]['name']}")
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
        game["night_actors"].add(user.id)
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
        game["night_actors"].add(user.id)
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
        if user.id == game.get("cuffed_id"):
            await query.answer("Ты в наручниках сержанта и не можешь голосовать сегодня.", show_alert=True)
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
        eligible = [uid for uid in alive if uid != game.get("cuffed_id")]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.get("voting_message_id", query.message.message_id),
                text=_voting_text(game, alive, eligible),
                parse_mode="HTML",
                reply_markup=_voting_keyboard(chat_id, game, alive)
            )
        except Exception:
            pass

        if len(game["day_votes"]) >= len(eligible):
            await _tally_day_votes(chat_id, context)
        return

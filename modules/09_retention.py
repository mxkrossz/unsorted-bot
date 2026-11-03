# ---------- Автоматическое удержание (v19) ----------

RT_WINDOW_H = 48          # сколько часов после срока отправки ещё можно отправить (чтобы не слать «поздно»)
RT_LOOP_SEC = 1800

RT_LABELS = {
    "3d":       "За несколько дней до конца",
    "end":      "В день окончания (платившим)",
    "gift_end": "В день окончания (подарок / без оплат)",
    "d2":       "Через 2 дня после конца",
    "d7":       "Через 7 дней",
    "d30":      "Через 30 дней",
}
RT_DEFAULTS = {
    # stage: (enabled, delay_h, pct, valid_h, text, sort)
    "3d":       (1, -72, 0, 72, REMIND_TEXTS["3d"], 1),
    "end":      (1, 0, 0, 72, REMIND_TEXTS["end"], 2),
    "gift_end": (1, 0, 20, 72,
                 "🎁 Подарочный Premium закончился — держи скидку {pct}% на любой тариф Premium "
                 "(кроме пробного). Действует до {valid_until}.\n\n"
                 "Полная история удалённых сообщений и одноразовые фото и видео без лимита.", 3),
    "d2":       (1, 48, 0, 72,
                 "👋 Скучаем! Premium закончился пару дней назад. Вернись: полная история удалённых "
                 "и одноразовые без лимита снова с тобой.", 4),
    "d7":       (1, 168, 15, 72,
                 "🎁 Персональная скидка {pct}% на продление Premium — только для тебя, до {valid_until}.", 5),
    "d30":      (1, 720, 25, 120,
                 "🔥 Специальное предложение для возвращения: скидка {pct}% на любой тариф до {valid_until}. "
                 "Мы многое добавили — загляни.", 6),
}

def rt_seed():
    for stage, (en, dl, pct, vh, text, srt) in RT_DEFAULTS.items():
        db.execute("INSERT OR IGNORE INTO retention_cfg (stage, enabled, delay_h, pct, valid_h, text, sort) "
                   "VALUES (?,?,?,?,?,?,?)", (stage, en, dl, pct, vh, text, srt))
    db.commit()

def rt_rows():
    rt_seed()
    return db.execute("SELECT stage, enabled, delay_h, pct, valid_h, text FROM retention_cfg ORDER BY sort").fetchall()

def rt_fmt(text, **kw) -> str:
    try:
        return text.format(**kw)
    except Exception:
        return text

def rt_sample_kw():
    return dict(until=fmt_ts(now_ts()), pct=15, valid_until=fmt_ts(now_ts() + 72 * 3600),
                free=FREE_HISTORY_DAYS, once=FREE_ONETIME_PER_DAY)

def rt_delay_text(stage, delay_h) -> str:
    if stage == "3d":
        return f"за {abs(delay_h)} ч до конца"
    if delay_h == 0:
        return "в момент окончания"
    return f"через {delay_h} ч после конца"

async def check_retention() -> int:
    """Лестница уведомлений вокруг окончания Premium. Каждое — один раз на каждый срок Premium."""
    now = now_ts()
    cfg = rt_rows()
    staff = {r[0] for r in db.execute("SELECT user_id FROM staff").fetchall()}
    users = db.execute(
        "SELECT user_id, premium_until, notifications FROM users WHERE premium_until > ? AND premium_until < ? "
        "AND user_id != ?", (now - 34 * 86400, now + 11 * 86400, ADMIN_ID)).fetchall()
    sent = 0
    for uid, until, notif in users:
        if uid in staff or is_tester(uid) or is_user_banned(uid)[0]:
            continue
        paid_ever = db.execute("SELECT 1 FROM orders WHERE user_id=? AND status='paid'", (uid,)).fetchone() is not None
        el = now - until                       # секунд после конца (до конца — отрицательно)
        rebought = db.execute("SELECT 1 FROM orders WHERE user_id=? AND status='paid' AND paid_at>?",
                              (uid, until)).fetchone() is not None
        for stage, enabled, delay_h, pct, valid_h, text in cfg:
            if not enabled:
                continue
            start = delay_h * 3600
            end = -86400 if stage == "3d" else start + RT_WINDOW_H * 3600
            if not (start <= el < end):
                continue
            if stage == "end" and not paid_ever:
                continue
            if stage == "gift_end" and paid_ever:
                continue
            if stage != "3d" and rebought:
                continue
            if stage in ("gift_end", "d2", "d7", "d30") and not notif:
                continue
            if db.execute("SELECT 1 FROM premium_reminders WHERE user_id=? AND until=? AND stage=?",
                          (uid, until, stage)).fetchone():
                continue
            db.execute("INSERT OR IGNORE INTO premium_reminders (user_id, until, stage) VALUES (?,?,?)",
                       (uid, until, stage))
            db.commit()
            if stage == "gift_end":
                if db.execute("SELECT 1 FROM day3_offers WHERE user_id=?", (uid,)).fetchone():
                    continue                   # скидку после подарка этот человек уже получал
                db.execute("INSERT OR IGNORE INTO day3_offers (user_id, sent_at) VALUES (?,?)", (uid, now))
                db.commit()
            eff_pct, eff_until = pct, now + valid_h * 3600
            if pct:
                cur = active_discount(uid)
                if cur and cur[0] >= pct:
                    eff_pct, eff_until = cur
                else:
                    db.execute("UPDATE users SET discount_pct=?, discount_until=?, discount_code=? WHERE user_id=?",
                               (pct, eff_until, f"RT{stage}", uid))
                    db.commit()
            msg = rt_fmt(text, until=fmt_ts(until), pct=eff_pct, valid_until=fmt_ts(eff_until),
                         free=FREE_HISTORY_DAYS, once=FREE_ONETIME_PER_DAY)
            try:
                await bot.send_message(uid, msg, reply_markup=tariff_keyboard(uid) if pct else remind_keyboard())
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
            break                              # не больше одного сообщения человеку за проход
    return sent

async def retention_loop():
    while True:
        try:
            n = await check_retention()
            if n:
                logging.info(f"Удержание: отправлено {n}")
        except Exception:
            logging.exception("Ошибка retention_loop")
        await asyncio.sleep(RT_LOOP_SEC)

def rt_editor_view():
    lines = ["🔁 Удержание — уведомления вокруг окончания Premium\n"]
    kb = []
    for stage, enabled, delay_h, pct, valid_h, text in rt_rows():
        n = db.execute("SELECT COUNT(*) FROM premium_reminders WHERE stage=?", (stage,)).fetchone()[0]
        disc = f", скидка {pct}% на {valid_h} ч" if pct else ""
        lines.append(f"{'✅' if enabled else '🚫'} {RT_LABELS[stage]}: {rt_delay_text(stage, delay_h)}{disc} · "
                     f"отправлено: {n}")
        kb.append([InlineKeyboardButton(text=f"{'✅' if enabled else '🚫'} {RT_LABELS[stage]}",
                                        callback_data=f"rt:v:{stage}")])
    lines.append("\nСообщения не уходят тем, кто уже продлил Premium, и каждое отправляется один раз на срок Premium. "
                 "В тексте можно использовать {until}, {pct}, {valid_until}.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

def rt_detail_view(stage):
    r = db.execute("SELECT stage, enabled, delay_h, pct, valid_h, text FROM retention_cfg WHERE stage=?",
                   (stage,)).fetchone()
    if not r:
        return "Не найдено.", None
    _s, enabled, delay_h, pct, valid_h, text = r
    n = db.execute("SELECT COUNT(*) FROM premium_reminders WHERE stage=?", (stage,)).fetchone()[0]
    t = (f"🔁 {RT_LABELS[stage]}\nСтатус: {'включено' if enabled else 'выключено'}\n"
         f"Когда: {rt_delay_text(stage, delay_h)}\nСкидка: {pct}% на {valid_h} ч\nОтправлено: {n}\n\n"
         f"Текст:\n{text}")
    kb = [[InlineKeyboardButton(text="🔕 Выключить" if enabled else "🔔 Включить", callback_data=f"rt:t:{stage}"),
           InlineKeyboardButton(text="👁 Пример", callback_data=f"rt:p:{stage}")],
          [InlineKeyboardButton(text="⏱ Срок", callback_data=f"rt:w:delay:{stage}"),
           InlineKeyboardButton(text="💸 Скидка %", callback_data=f"rt:w:pct:{stage}")],
          [InlineKeyboardButton(text="⏳ Срок скидки (ч)", callback_data=f"rt:w:valid:{stage}"),
           InlineKeyboardButton(text="✏️ Текст", callback_data=f"rt:w:text:{stage}")],
          [InlineKeyboardButton(text="↩️ По умолчанию", callback_data=f"rt:r:{stage}"),
           InlineKeyboardButton(text="← К списку", callback_data="rt:list")]]
    return t, InlineKeyboardMarkup(inline_keyboard=kb)

async def handle_rt_text(message, uid, text) -> bool:
    if text == "🔁 Удержание":
        if not is_owner(uid):
            return False
        t, kb = rt_editor_view()
        await message.answer(t, reply_markup=kb)
        return True
    return False

async def handle_rt_wait(message, uid, text) -> bool:
    v = get_setting(f"rt_wait_{uid}")
    if not v:
        return False
    field, _, rest = v.partition(":")
    stage, _, ts = rest.partition("|")
    try:
        ts = int(ts)
    except ValueError:
        ts = 0
    if time.time() - ts > 1800 or not is_owner(uid) or text in MENU_TEXTS:
        del_setting(f"rt_wait_{uid}")
        return False
    if not text:
        await message.answer("Пришли текстом.", reply_markup=back_only_keyboard())
        return True
    if field == "text":
        if len(text) > 1000:
            await message.answer("Слишком длинно (максимум 1000 символов).")
            return True
        try:
            text.format(**rt_sample_kw())
        except Exception:
            await message.answer("В тексте ошибка в фигурных скобках. Доступны только {until}, {pct}, {valid_until}.")
            return True
        db.execute("UPDATE retention_cfg SET text=? WHERE stage=?", (text, stage))
    else:
        if not text.isdigit():
            await message.answer("Введи число.")
            return True
        val = int(text)
        if field == "delay":
            lo, hi = (25, 240) if stage == "3d" else (0, 2160)
            if not lo <= val <= hi:
                await message.answer(f"Число часов от {lo} до {hi}.")
                return True
            db.execute("UPDATE retention_cfg SET delay_h=? WHERE stage=?", (-val if stage == "3d" else val, stage))
        elif field == "pct":
            if not 0 <= val <= 90:
                await message.answer("Скидка от 0 до 90 процентов.")
                return True
            db.execute("UPDATE retention_cfg SET pct=? WHERE stage=?", (val, stage))
        elif field == "valid":
            if not 1 <= val <= 720:
                await message.answer("Срок скидки от 1 до 720 часов.")
                return True
            db.execute("UPDATE retention_cfg SET valid_h=? WHERE stage=?", (val, stage))
        else:
            del_setting(f"rt_wait_{uid}")
            return False
    db.commit()
    del_setting(f"rt_wait_{uid}")
    log_action(uid, "rt_edit", None, f"{stage} {field}")
    t, kb = rt_detail_view(stage)
    await message.answer("✅ Сохранено.")
    await message.answer(t, reply_markup=kb)
    return True

async def on_rt_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_owner(uid):
        await callback.answer("Нет доступа.")
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    async def show(t, kb):
        try:
            await callback.message.edit_text(t, reply_markup=kb)
        except Exception:
            await callback.message.answer(t, reply_markup=kb)

    if action == "list":
        t, kb = rt_editor_view()
        await show(t, kb)
    elif action in ("v", "t", "p", "r") and len(parts) > 2 and parts[2] in RT_DEFAULTS:
        stage = parts[2]
        if action == "t":
            db.execute("UPDATE retention_cfg SET enabled=1-enabled WHERE stage=?", (stage,))
            db.commit()
            log_action(uid, "rt_edit", None, f"{stage} вкл/выкл")
        elif action == "r":
            en, dl, pct, vh, text, srt = RT_DEFAULTS[stage]
            db.execute("UPDATE retention_cfg SET enabled=?, delay_h=?, pct=?, valid_h=?, text=? WHERE stage=?",
                       (en, dl, pct, vh, text, stage))
            db.commit()
            log_action(uid, "rt_edit", None, f"{stage} по умолчанию")
        elif action == "p":
            row = db.execute("SELECT pct, text FROM retention_cfg WHERE stage=?", (stage,)).fetchone()
            kw = rt_sample_kw()
            kw["pct"] = row[0] or kw["pct"]
            await callback.message.answer("👁 Так увидит пользователь:\n\n" + rt_fmt(row[1], **kw))
            await callback.answer()
            return
        t, kb = rt_detail_view(stage)
        await show(t, kb)
    elif action == "w" and len(parts) > 3 and parts[2] in ("delay", "pct", "valid", "text") and parts[3] in RT_DEFAULTS:
        field, stage = parts[2], parts[3]
        set_setting(f"rt_wait_{uid}", f"{field}:{stage}|{int(time.time())}")
        hint = {"delay": ("за сколько часов до конца Premium отправлять (25–240)" if stage == "3d"
                          else "через сколько часов после окончания отправлять (0–2160)"),
                "pct": "размер скидки в процентах (0–90, 0 — без скидки)",
                "valid": "сколько часов действует скидка (1–720)",
                "text": "новый текст сообщения (можно {until}, {pct}, {valid_until})"}[field]
        await callback.message.answer(f"Введи: {hint}.\nДля отмены нажми ← Назад", reply_markup=back_only_keyboard())
    await callback.answer()


# ---------- Обратная связь: поддержка и идеи (v16) ----------

FB_MAX_LEN = 2000        # максимум символов в обращении
FB_HOURLY_LIMIT = 3      # новых обращений/идей от одного человека в час
FB_OPEN_LIMIT = 5        # одновременно открытых обращений в поддержку
FB_WAIT_SEC = 3600       # сколько ждём текст после нажатия кнопки

TK_STATUS = {"open": "🟡 открыто", "answered": "💬 есть ответ", "closed": "✅ закрыто",
             "new": "🆕 на рассмотрении", "accepted": "✅ принято", "rejected": "❌ отклонено"}
TK_ICON = {"support": "🛠", "idea": "💡"}

def staff_recipients() -> list:
    ids = [ADMIN_ID]
    for (u,) in db.execute("SELECT user_id FROM staff").fetchall():
        if u not in ids:
            ids.append(u)
    return ids

def feedback_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🐛 Сообщить о баге"), KeyboardButton(text="🛠 Обращение в поддержку")],
        [KeyboardButton(text="💡 Предложить функцию"), KeyboardButton(text="📨 Мои обращения")],
        [KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def fb_wait_set(uid, key_prefix, kind):
    set_setting(f"{key_prefix}_{uid}", f"{kind}|{int(time.time())}")

def fb_wait_get(uid, key_prefix):
    key = f"{key_prefix}_{uid}"
    v = get_setting(key)
    if not v:
        return None
    kind, _, ts = v.partition("|")
    try:
        ts = int(ts)
    except ValueError:
        ts = 0
    if time.time() - ts > FB_WAIT_SEC:
        del_setting(key)
        return None
    return kind

def ticket_create(uid, username, kind, text) -> int:
    now = now_ts()
    status = "new" if kind == "idea" else "open"
    cur = db.execute(
        "INSERT INTO tickets (user_id, username, kind, text, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)", (uid, username or "", kind, text, status, now, now))
    tid = cur.lastrowid
    db.execute("INSERT INTO ticket_msgs (ticket_id, sender, sender_id, text, ts) VALUES (?,?,?,?,?)",
               (tid, "user", uid, text, now))
    db.commit()
    return tid

def ticket_add_msg(tid, sender, sender_id, text, status=None, handled_by=None):
    now = now_ts()
    db.execute("INSERT INTO ticket_msgs (ticket_id, sender, sender_id, text, ts) VALUES (?,?,?,?,?)",
               (tid, sender, sender_id, text, now))
    if status:
        db.execute("UPDATE tickets SET status=?, updated_at=?, handled_by=COALESCE(?, handled_by) WHERE id=?",
                   (status, now, handled_by, tid))
    else:
        db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, tid))
    db.commit()

def ticket_get(tid):
    return db.execute("SELECT id, user_id, username, kind, text, status, created_at, handled_by "
                      "FROM tickets WHERE id=?", (tid,)).fetchone()

def ticket_history_text(tid, for_staff) -> str:
    t = ticket_get(tid)
    if not t:
        return "Обращение не найдено."
    _, uid, uname, kind, _text, status, created, _hb = t
    title = "Обращение" if kind == "support" else "Идея"
    head = f"{TK_ICON.get(kind, '')} {title} #{tid} · {TK_STATUS.get(status, status)}\n"
    if for_staff:
        head += f"От: {user_label(uid)}\n"
    head += f"Создано: {fmt_ts(created)}\n"
    parts = []
    for sender, ts, txt in db.execute(
            "SELECT sender, ts, text FROM ticket_msgs WHERE ticket_id=? ORDER BY id", (tid,)).fetchall():
        who = "Пользователь" if sender == "user" else "Поддержка"
        if not for_staff:
            who = "Ты" if sender == "user" else "Поддержка"
        parts.append(f"— {who}, {fmt_ts(ts)}:\n{txt}")
    body = "\n\n".join(parts)
    if len(body) > 3200:
        body = "…" + body[-3200:]
    return head + "\n" + body

def ticket_staff_keyboard(tid, kind, status) -> InlineKeyboardMarkup:
    rows = []
    if kind == "support" and status != "closed":
        rows.append([InlineKeyboardButton(text="✉️ Ответить", callback_data=f"tk:reply:{tid}"),
                     InlineKeyboardButton(text="✅ Закрыть", callback_data=f"tk:close:{tid}")])
    if kind == "idea" and status == "new":
        rows.append([InlineKeyboardButton(text="✅ Принять", callback_data=f"tk:acc:{tid}"),
                     InlineKeyboardButton(text="❌ Отклонить", callback_data=f"tk:rej:{tid}")])
    rows.append([InlineKeyboardButton(text="📜 История", callback_data=f"tk:view:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ticket_user_keyboard(tid, kind, status) -> InlineKeyboardMarkup:
    rows = []
    if kind == "support" and status != "closed":
        rows.append([InlineKeyboardButton(text="✍️ Дополнить / ответить", callback_data=f"tk:uans:{tid}")])
    rows.append([InlineKeyboardButton(text="📜 История", callback_data=f"tk:view:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def ticket_notify_staff(tid, header, text, exclude=None):
    t = ticket_get(tid)
    kb = ticket_staff_keyboard(tid, t[3], t[5])
    for rid in staff_recipients():
        if rid == exclude:
            continue
        try:
            await bot.send_message(rid, f"{header}\n\n{text}", reply_markup=kb)
        except Exception:
            logging.exception("ticket_notify_staff")

def tickets_staff_list(kind):
    if kind == "support":
        rows = db.execute(
            "SELECT id, user_id, username, text, status FROM tickets WHERE kind='support' "
            "AND status IN ('open','answered') ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC "
            "LIMIT 15").fetchall()
        title = "🛠 Обращения в поддержку (ждут ответа сверху)"
    else:
        rows = db.execute(
            "SELECT id, user_id, username, text, status FROM tickets WHERE kind='idea' AND status='new' "
            "ORDER BY id DESC LIMIT 15").fetchall()
        title = "💡 Идеи на рассмотрении"
    if not rows:
        return title + "\n\nПока пусто.", None
    kb = []
    for tid, uid, un, txt, st in rows:
        snippet = (txt or "").replace("\n", " ")[:28]
        kb.append([InlineKeyboardButton(
            text=f"#{tid} {'🟡' if st in ('open', 'new') else '💬'} {('@' + un) if un else uid}: {snippet}",
            callback_data=f"tk:view:{tid}")])
    return title + f"\n\nВсего в списке: {len(rows)}", InlineKeyboardMarkup(inline_keyboard=kb)

def tickets_user_list(uid):
    rows = db.execute(
        "SELECT id, kind, text, status FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    if not rows:
        return "У тебя пока нет обращений и идей.", None
    kb = []
    lines = []
    for tid, kind, txt, st in rows:
        snippet = (txt or "").replace("\n", " ")[:30]
        lines.append(f"#{tid} {TK_ICON.get(kind, '')} {TK_STATUS.get(st, st)} — {snippet}")
        kb.append([InlineKeyboardButton(text=f"#{tid} {TK_ICON.get(kind, '')} {TK_STATUS.get(st, st)}",
                                        callback_data=f"tk:view:{tid}")])
    return "📨 Твои обращения и идеи:\n\n" + "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

async def handle_feedback_text(message, uid, text) -> bool:
    if text == "💬 Обратная связь":
        await message.answer(
            "💬 Обратная связь\n\n"
            "🐛 Сообщить о баге — что-то работает не так\n"
            "🛠 Обращение в поддержку — вопрос или проблема, ответим здесь же\n"
            "💡 Предложить функцию — расскажи, чего не хватает\n"
            "📨 Мои обращения — история и статусы",
            reply_markup=feedback_keyboard())
        return True
    if text == "🛠 Обращение в поддержку":
        fb_wait_set(uid, "fb_wait", "support")
        await message.answer("🛠 Опиши проблему или вопрос одним сообщением.\nДля отмены нажми ← Назад",
                             reply_markup=back_only_keyboard())
        return True
    if text == "💡 Предложить функцию":
        fb_wait_set(uid, "fb_wait", "idea")
        await message.answer("💡 Расскажи, какой функции тебе не хватает, одним сообщением.\n"
                             "Для отмены нажми ← Назад", reply_markup=back_only_keyboard())
        return True
    if text == "📨 Мои обращения":
        t, kb = tickets_user_list(uid)
        await message.answer(t, reply_markup=kb or feedback_keyboard())
        return True
    if text in ("💬 Обращения", "💡 Идеи"):
        if not has_panel(uid):
            return False
        t, kb = tickets_staff_list("support" if text == "💬 Обращения" else "idea")
        await message.answer(t, reply_markup=kb or panel_kb(uid))
        return True
    return False

async def handle_feedback_wait(message, uid, text) -> bool:
    # --- сотрудник пишет ответ на обращение ---
    tid_s = fb_wait_get(uid, "tkr_wait")
    if tid_s:
        if text in MENU_TEXTS:
            del_setting(f"tkr_wait_{uid}")
        elif not has_panel(uid):
            del_setting(f"tkr_wait_{uid}")
        elif not text:
            await message.answer("Напиши ответ текстом.", reply_markup=back_only_keyboard())
            return True
        else:
            del_setting(f"tkr_wait_{uid}")
            t = ticket_get(int(tid_s))
            if not t or t[3] != "support":
                await message.answer("Обращение не найдено.", reply_markup=panel_kb(uid))
                return True
            if t[5] == "closed":
                await message.answer("Обращение уже закрыто.", reply_markup=panel_kb(uid))
                return True
            tid = t[0]
            ticket_add_msg(tid, "staff", uid, text[:FB_MAX_LEN], status="answered", handled_by=uid)
            log_action(uid, "ticket_reply", t[1], f"#{tid}")
            try:
                await bot.send_message(
                    t[1], f"💬 Ответ поддержки по обращению #{tid}:\n\n{text[:FB_MAX_LEN]}",
                    reply_markup=ticket_user_keyboard(tid, "support", "answered"))
                await message.answer(f"✅ Ответ на #{tid} отправлен.", reply_markup=panel_kb(uid))
            except Exception:
                await message.answer(f"⚠️ Ответ сохранён, но пользователь недоступен (бот заблокирован?). #{tid}",
                                     reply_markup=panel_kb(uid))
            if not is_owner(uid):
                try:
                    await bot.send_message(ADMIN_ID, f"ℹ️ {user_label(uid)} ответил на обращение #{tid} "
                                                     f"от {user_label(t[1])}:\n\n{text[:500]}")
                except Exception:
                    pass
            return True

    # --- пользователь пишет обращение / идею / дополнение ---
    kind = fb_wait_get(uid, "fb_wait")
    if not kind:
        return False
    if text in MENU_TEXTS:
        del_setting(f"fb_wait_{uid}")
        return False
    if not text:
        await message.answer("Напиши текстом.", reply_markup=back_only_keyboard())
        return True
    if len(text) > FB_MAX_LEN:
        await message.answer(f"Слишком длинно ({len(text)} из {FB_MAX_LEN} символов). Сократи, пожалуйста.",
                             reply_markup=back_only_keyboard())
        return True
    username = message.from_user.username or ""
    now = now_ts()
    if kind.startswith("ans:"):
        t = ticket_get(int(kind[4:]))
        del_setting(f"fb_wait_{uid}")
        if not t or t[1] != uid or t[5] == "closed":
            await message.answer("Это обращение уже закрыто. Создай новое: 🛠 Обращение в поддержку.",
                                 reply_markup=feedback_keyboard())
            return True
        recent = db.execute("SELECT COUNT(*) FROM ticket_msgs WHERE sender='user' AND sender_id=? AND ts>?",
                            (uid, now - 3600)).fetchone()[0]
        if recent >= 10:
            await message.answer("Слишком много сообщений за час. Попробуй позже.", reply_markup=feedback_keyboard())
            return True
        ticket_add_msg(t[0], "user", uid, text, status="open")
        await message.answer(f"✅ Дополнение к #{t[0]} отправлено.", reply_markup=feedback_keyboard())
        await ticket_notify_staff(t[0], f"🛠 Обращение #{t[0]} — новое сообщение от {user_label(uid)}", text)
        return True

    recent = db.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND created_at>?",
                        (uid, now - 3600)).fetchone()[0]
    if recent >= FB_HOURLY_LIMIT:
        del_setting(f"fb_wait_{uid}")
        await message.answer("Ты уже отправил несколько обращений за последний час. Попробуй чуть позже.",
                             reply_markup=feedback_keyboard())
        return True
    if kind == "support":
        opened = db.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND kind='support' "
                            "AND status IN ('open','answered')", (uid,)).fetchone()[0]
        if opened >= FB_OPEN_LIMIT:
            del_setting(f"fb_wait_{uid}")
            await message.answer("У тебя уже много открытых обращений. Дождись ответа или дополни одно из них "
                                 "в «📨 Мои обращения».", reply_markup=feedback_keyboard())
            return True
    del_setting(f"fb_wait_{uid}")
    tid = ticket_create(uid, username, "support" if kind == "support" else "idea", text)
    if kind == "support":
        await message.answer(f"✅ Обращение #{tid} принято. Ответ придёт сюда, в бота. "
                             f"Статус — в «📨 Мои обращения».", reply_markup=feedback_keyboard())
        await ticket_notify_staff(tid, f"🛠 Новое обращение #{tid} от {user_label(uid)}", text)
    else:
        await message.answer(f"✅ Спасибо! Идея #{tid} отправлена. Мы сообщим, что решили.",
                             reply_markup=feedback_keyboard())
        await ticket_notify_staff(tid, f"💡 Новая идея #{tid} от {user_label(uid)}", text)
    return True

async def on_ticket_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer()
        return
    action, tid = parts[1], int(parts[2])
    t = ticket_get(tid)
    if not t:
        await callback.answer("Не найдено.")
        return
    _, owner, _un, kind, _text, status, _created, _hb = t
    staff = has_panel(uid)
    if action == "view":
        if not staff and uid != owner:
            await callback.answer("Нет доступа.")
            return
        kb = ticket_staff_keyboard(tid, kind, status) if staff else ticket_user_keyboard(tid, kind, status)
        await callback.message.answer(ticket_history_text(tid, staff), reply_markup=kb)
        await callback.answer()
        return
    if action == "uans":
        if uid != owner or kind != "support":
            await callback.answer("Нет доступа.")
            return
        if status == "closed":
            await callback.answer("Обращение закрыто. Создай новое.", show_alert=True)
            return
        fb_wait_set(uid, "fb_wait", f"ans:{tid}")
        await callback.message.answer(f"✍️ Напиши сообщение по обращению #{tid}.\nДля отмены нажми ← Назад",
                                      reply_markup=back_only_keyboard())
        await callback.answer()
        return
    if not staff:
        await callback.answer("Нет доступа.")
        return
    if action == "reply":
        if kind != "support" or status == "closed":
            await callback.answer("Обращение закрыто.", show_alert=True)
            return
        fb_wait_set(uid, "tkr_wait", str(tid))
        await callback.message.answer(f"✉️ Напиши ответ на обращение #{tid} одним сообщением.\n"
                                      f"Для отмены нажми ← Назад", reply_markup=back_only_keyboard())
        await callback.answer()
        return
    if action == "close":
        if kind != "support" or status == "closed":
            await callback.answer("Уже закрыто.")
            return
        db.execute("UPDATE tickets SET status='closed', updated_at=?, handled_by=? WHERE id=?",
                   (now_ts(), uid, tid))
        db.commit()
        log_action(uid, "ticket_close", owner, f"#{tid}")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await bot.send_message(owner, f"✅ Обращение #{tid} закрыто. Если вопрос остался — "
                                          f"создай новое: 💬 Обратная связь → 🛠 Обращение в поддержку.")
        except Exception:
            pass
        await callback.answer("Закрыто.")
        return
    if action in ("acc", "rej"):
        if kind != "idea" or status != "new":
            await callback.answer("Уже решено.")
            return
        new_status = "accepted" if action == "acc" else "rejected"
        db.execute("UPDATE tickets SET status=?, updated_at=?, handled_by=? WHERE id=?",
                   (new_status, now_ts(), uid, tid))
        db.commit()
        log_action(uid, "idea_acc" if action == "acc" else "idea_rej", owner, f"#{tid}")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            if action == "acc":
                await bot.send_message(owner, f"💡 Твоя идея #{tid} принята — добавили в планы. Спасибо!")
            else:
                await bot.send_message(owner, f"💡 Идею #{tid} рассмотрели — пока не берём в работу. "
                                              f"Спасибо, что делишься, предлагай ещё!")
        except Exception:
            pass
        await callback.answer("Принято." if action == "acc" else "Отклонено.")
        return
    await callback.answer()


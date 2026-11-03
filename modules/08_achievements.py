# ---------- Достижения (v17) ----------

ACH_DISCOUNT_DAYS = 7       # сколько дней действует скидка-награда
ACH_LOOP_SEC = 900          # как часто проверять всех пользователей

ACH_METRICS = {
    "invited":   "приглашено друзей",
    "reg_days":  "дней с регистрации",
    "use_days":  "дней использования бота",
    "tester":    "участие в тестировании",
    "ideas":     "принятых идей",
    "purchases": "оплат Premium",
}
ACH_RTYPES = {
    "premium_days": "дни Premium",
    "discount":     "скидка на Premium",
    "status":       "особый статус",
    "none":         "только значок",
}

ACH_DEFAULTS = [
    ("invite3",  "Друг друзей",        "invited",  3,   "premium_days", "7"),
    ("invite10", "Амбассадор",         "invited",  10,  "status",       "🌟 Амбассадор"),
    ("reg30",    "Месяц с нами",       "reg_days", 30,  "premium_days", "3"),
    ("use90",    "Постоянный клиент",  "use_days", 90,  "discount",     "30"),
    ("use180",   "Ветеран",            "use_days", 180, "premium_days", "14"),
    ("tester",   "Помощник в тестах",  "tester",   1,   "premium_days", "7"),
]

def ach_off() -> bool:
    return get_setting("ach_off") != "0"        # по умолчанию автовыдача выключена

def ach_seed():
    if get_setting("ach_seeded"):
        return
    if db.execute("SELECT COUNT(*) FROM achievements").fetchone()[0] == 0:
        for i, (code, title, metric, thr, rtype, rval) in enumerate(ACH_DEFAULTS):
            db.execute("INSERT OR IGNORE INTO achievements (code, title, metric, threshold, rtype, rvalue, active, sort) "
                       "VALUES (?,?,?,?,?,?,1,?)", (code, title, metric, thr, rtype, rval, i))
        db.commit()
    set_setting("ach_seeded", "1")

def ach_rows(only_active=True):
    ach_seed()
    sql = ("SELECT code, title, metric, threshold, rtype, rvalue, active FROM achievements "
           + ("WHERE active=1 " if only_active else "") + "ORDER BY sort, code")
    return db.execute(sql).fetchall()

def ach_metric(uid, metric) -> int:
    now = int(time.time())
    if metric == "invited":
        return db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
    if metric == "reg_days":
        r = db.execute("SELECT CAST(julianday('now') - julianday(first_seen) AS INTEGER) FROM users WHERE user_id=?",
                       (uid,)).fetchone()
        return max(0, r[0] or 0) if r else 0
    if metric == "use_days":
        r = db.execute(
            "SELECT MIN(COALESCE(connected_at, first_msg_at)), "
            "MAX(CASE WHEN enabled=1 OR (enabled IS NULL AND COALESCE(last_msg_at,0) > ?) THEN 1 ELSE 0 END) "
            "FROM biz_conn WHERE owner_id=?", (now - 30 * 86400, uid)).fetchone()
        return max(0, (now - r[0]) // 86400) if r and r[0] and r[1] else 0
    if metric == "tester":
        return 1 if is_tester(uid) else 0
    if metric == "ideas":
        return db.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND kind='idea' AND status='accepted'",
                          (uid,)).fetchone()[0]
    if metric == "purchases":
        return db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0]
    return 0

def ach_cond_text(metric, thr) -> str:
    if metric == "invited":
        return f"пригласить друзей: {thr}"
    if metric == "reg_days":
        return f"{thr} дн. с регистрации"
    if metric == "use_days":
        return f"{thr} дн. использования бота"
    if metric == "tester":
        return "участие в тестировании"
    if metric == "ideas":
        return f"принятых идей: {thr}"
    if metric == "purchases":
        return f"оплат Premium: {thr}"
    return f"{metric} ≥ {thr}"

def ach_reward_text(rtype, rvalue) -> str:
    if rtype == "premium_days":
        return f"+{rvalue} дн. Premium"
    if rtype == "discount":
        return f"скидка {rvalue}% на {ACH_DISCOUNT_DAYS} дн."
    if rtype == "status":
        return f"статус «{rvalue}»"
    return "значок"

def ach_apply_reward(uid, rtype, rvalue):
    if rtype == "premium_days":
        grant_premium(uid, int(rvalue))
    elif rtype == "discount":
        pct = int(rvalue)
        cur = active_discount(uid)
        until = int(time.time()) + ACH_DISCOUNT_DAYS * 86400
        if cur:
            pct = max(pct, cur[0])
            until = max(until, cur[1])
        db.execute("UPDATE users SET discount_pct=?, discount_until=?, discount_code='ACH' WHERE user_id=?",
                   (pct, until, uid))
        db.commit()

def badge_line(uid) -> str:
    try:
        rows = db.execute(
            "SELECT a.rvalue FROM user_achievements u JOIN achievements a ON a.code=u.code "
            "WHERE u.user_id=? AND a.rtype='status' ORDER BY u.earned_at", (uid,)).fetchall()
    except Exception:
        return ""
    return "🏅 Статус: " + ", ".join(r[0] for r in rows) + "\n" if rows else ""

async def check_user_achievements(uid, notify=True) -> list:
    """Выдаёт заработанные достижения. Каждое — один раз (PRIMARY KEY), награда после записи."""
    if ach_off() or is_owner(uid) or is_user_banned(uid)[0]:
        return []
    got = []
    for code, title, metric, thr, rtype, rvalue, _a in ach_rows(True):
        if db.execute("SELECT 1 FROM user_achievements WHERE user_id=? AND code=?", (uid, code)).fetchone():
            continue
        if metric == "invited" and ref_banned(uid):
            continue
        if ach_metric(uid, metric) < thr:
            continue
        rt = ach_reward_text(rtype, rvalue)
        cur = db.execute("INSERT OR IGNORE INTO user_achievements (user_id, code, earned_at, reward) "
                         "VALUES (?,?,?,?)", (uid, code, int(time.time()), rt))
        db.commit()
        if cur.rowcount != 1:
            continue
        try:
            ach_apply_reward(uid, rtype, rvalue)
        except Exception:
            logging.exception("ach_apply_reward")
            db.execute("DELETE FROM user_achievements WHERE user_id=? AND code=?", (uid, code))
            db.commit()
            continue
        got.append((title, rt))
        if notify:
            try:
                await bot.send_message(uid, f"🏆 Новое достижение: «{title}»!\nНаграда: {rt}")
            except Exception:
                pass
    return got

async def achievements_loop():
    while True:
        try:
            if not ach_off():
                ids = [r[0] for r in db.execute("SELECT user_id FROM users").fetchall()]
                for i, u in enumerate(ids):
                    await check_user_achievements(u)
                    if i % 20 == 0:
                        await asyncio.sleep(0)
        except Exception:
            logging.exception("achievements_loop")
        await asyncio.sleep(ACH_LOOP_SEC)

def ach_user_text(uid) -> str:
    rows = ach_rows(True)
    earned = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT code, earned_at, reward FROM user_achievements WHERE user_id=?", (uid,)).fetchall()}
    done = sum(1 for r in rows if r[0] in earned)
    lines = [f"🏆 Достижения — получено {done} из {len(rows)}\n"]
    for code, title, metric, thr, rtype, rvalue, _a in rows:
        cond = ach_cond_text(metric, thr)
        rew = ach_reward_text(rtype, rvalue)
        if code in earned:
            lines.append(f"✅ {title}\n   {cond} → {rew} · {fmt_ts(earned[code][0])}")
        else:
            val = min(ach_metric(uid, metric), thr)
            filled = int(8 * val / thr) if thr else 8
            lines.append(f"⬜ {title}\n   {cond} → {rew}\n   [{'█' * filled}{'░' * (8 - filled)}] {val}/{thr}")
    if ach_off():
        lines.append("\nℹ️ Награды начнут выдаваться автоматически совсем скоро.")
    else:
        lines.append("\nНаграды выдаются автоматически, один раз за каждое достижение.")
    return "\n".join(lines)

def ach_editor_view():
    rows = ach_rows(False)
    off = ach_off()
    lines = [f"🏆 Редактор достижений\nАвтовыдача: {'⏸ ВЫКЛЮЧЕНА' if off else '▶️ ВКЛЮЧЕНА'}\n"]
    kb = []
    for code, title, metric, thr, rtype, rvalue, active in rows:
        got = db.execute("SELECT COUNT(*) FROM user_achievements WHERE code=?", (code,)).fetchone()[0]
        lines.append(f"{'✅' if active else '🚫'} {title}: {ach_cond_text(metric, thr)} → "
                     f"{ach_reward_text(rtype, rvalue)} (получили: {got})")
        kb.append([InlineKeyboardButton(text=f"{'✅' if active else '🚫'} {title}", callback_data=f"ac:v:{code}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="ac:new"),
               InlineKeyboardButton(text="▶️ Включить автовыдачу" if off else "⏸ Выключить автовыдачу",
                                    callback_data="ac:glob")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)

def ach_detail_view(code):
    r = db.execute("SELECT code, title, metric, threshold, rtype, rvalue, active FROM achievements WHERE code=?",
                   (code,)).fetchone()
    if not r:
        return "Достижение не найдено.", None
    _c, title, metric, thr, rtype, rvalue, active = r
    got = db.execute("SELECT COUNT(*) FROM user_achievements WHERE code=?", (code,)).fetchone()[0]
    text = (f"🏆 {title}\nУсловие: {ach_cond_text(metric, thr)}\nНаграда: {ach_reward_text(rtype, rvalue)}\n"
            f"Статус: {'включено' if active else 'выключено'}\nПолучили: {got}")
    kb = [[InlineKeyboardButton(text="🔕 Выключить" if active else "🔔 Включить", callback_data=f"ac:t:{code}"),
           InlineKeyboardButton(text="👥 Кто получил", callback_data=f"ac:who:{code}")],
          [InlineKeyboardButton(text="🎯 Порог", callback_data=f"ac:thr:{code}"),
           InlineKeyboardButton(text="🎁 Награда", callback_data=f"ac:rw:{code}")],
          [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ac:d:{code}"),
           InlineKeyboardButton(text="← К списку", callback_data="ac:list")]]
    return text, InlineKeyboardMarkup(inline_keyboard=kb)

def ach_rtype_keyboard(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{k}")] for k, label in ACH_RTYPES.items()])

def ach_validate_value(rtype, text):
    t = (text or "").strip()
    if rtype == "premium_days":
        if t.isdigit() and 1 <= int(t) <= 365:
            return True, str(int(t))
        return False, "Введи число дней от 1 до 365."
    if rtype == "discount":
        if t.isdigit() and 1 <= int(t) <= 90:
            return True, str(int(t))
        return False, "Введи процент скидки от 1 до 90."
    if rtype == "status":
        if 1 <= len(t) <= 30:
            return True, t
        return False, "Название статуса — до 30 символов."
    return True, ""

def ach_draft_get(uid):
    import json as _json
    v = get_setting(f"ach_draft_{uid}")
    if not v:
        return None
    try:
        d = _json.loads(v)
    except Exception:
        return None
    if time.time() - d.get("ts", 0) > 1800:
        del_setting(f"ach_draft_{uid}")
        return None
    return d

def ach_draft_set(uid, d):
    import json as _json
    d["ts"] = int(time.time())
    set_setting(f"ach_draft_{uid}", _json.dumps(d, ensure_ascii=False))

async def handle_ach_text(message, uid, text) -> bool:
    if text == "🏆 Достижения":
        if not is_owner(uid):
            await check_user_achievements(uid)
        await message.answer(ach_user_text(uid), reply_markup=cabinet_keyboard())
        return True
    if text == "🏆 Редактор достижений":
        if not is_owner(uid):
            return False
        t, kb = ach_editor_view()
        await message.answer(t, reply_markup=kb)
        return True
    return False

async def handle_ach_wait(message, uid, text) -> bool:
    d = ach_draft_get(uid)
    if not d:
        return False
    if text in MENU_TEXTS or not is_owner(uid):
        del_setting(f"ach_draft_{uid}")
        return False
    if not text:
        await message.answer("Пришли текстом.", reply_markup=back_only_keyboard())
        return True
    mode, step = d.get("mode"), d.get("step")
    if step == "thr":
        if not (text.isdigit() and 1 <= int(text) <= 100000):
            await message.answer("Введи число от 1 до 100000.")
            return True
        thr = int(text)
        if mode == "edit":
            db.execute("UPDATE achievements SET threshold=? WHERE code=?", (thr, d["code"]))
            db.commit()
            del_setting(f"ach_draft_{uid}")
            log_action(uid, "ach_edit", None, f"{d['code']} порог {thr}")
            t, kb = ach_detail_view(d["code"])
            await message.answer("✅ Порог изменён.")
            await message.answer(t, reply_markup=kb)
            return True
        d["thr"] = thr
        d["step"] = "rtype"
        ach_draft_set(uid, d)
        await message.answer("Какая награда?", reply_markup=ach_rtype_keyboard("ac:nr"))
        return True
    if step == "rval":
        okv, val = ach_validate_value(d["rtype"], text)
        if not okv:
            await message.answer(val)
            return True
        if mode == "edit":
            db.execute("UPDATE achievements SET rtype=?, rvalue=? WHERE code=?", (d["rtype"], val, d["code"]))
            db.commit()
            del_setting(f"ach_draft_{uid}")
            log_action(uid, "ach_edit", None, f"{d['code']} награда {d['rtype']} {val}")
            t, kb = ach_detail_view(d["code"])
            await message.answer("✅ Награда изменена.")
            await message.answer(t, reply_markup=kb)
            return True
        d["rvalue"] = val
        d["step"] = "title"
        ach_draft_set(uid, d)
        await message.answer("Придумай название достижения (до 40 символов):")
        return True
    if step == "title":
        title = text.strip()
        if not (1 <= len(title) <= 40):
            await message.answer("Название — от 1 до 40 символов.")
            return True
        code = f"c{int(time.time())}"
        mx = db.execute("SELECT COALESCE(MAX(sort),0) FROM achievements").fetchone()[0]
        db.execute("INSERT INTO achievements (code, title, metric, threshold, rtype, rvalue, active, sort) "
                   "VALUES (?,?,?,?,?,?,1,?)",
                   (code, title, d["metric"], d["thr"], d["rtype"], d.get("rvalue", ""), mx + 1))
        db.commit()
        del_setting(f"ach_draft_{uid}")
        log_action(uid, "ach_edit", None, f"создал {code}")
        t, kb = ach_detail_view(code)
        await message.answer("✅ Достижение создано.")
        await message.answer(t, reply_markup=kb)
        return True
    return False

async def on_ach_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_owner(uid):
        await callback.answer("Нет доступа.")
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    arg2 = parts[3] if len(parts) > 3 else ""

    async def show(t, kb):
        try:
            await callback.message.edit_text(t, reply_markup=kb)
        except Exception:
            await callback.message.answer(t, reply_markup=kb)

    if action == "list":
        t, kb = ach_editor_view()
        await show(t, kb)
    elif action == "glob":
        set_setting("ach_off", "0" if ach_off() else "1")
        log_action(uid, "ach_edit", None, "автовыдача " + ("выкл" if ach_off() else "вкл"))
        t, kb = ach_editor_view()
        await show(t, kb)
    elif action == "v":
        t, kb = ach_detail_view(arg)
        await show(t, kb)
    elif action == "t":
        db.execute("UPDATE achievements SET active=1-active WHERE code=?", (arg,))
        db.commit()
        log_action(uid, "ach_edit", None, f"{arg} вкл/выкл")
        t, kb = ach_detail_view(arg)
        await show(t, kb)
    elif action == "d":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"ac:dd:{arg}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"ac:v:{arg}")]])
        await show("Удалить достижение? Уже выданные награды у людей останутся.", kb)
    elif action == "dd":
        db.execute("DELETE FROM achievements WHERE code=?", (arg,))
        db.commit()
        log_action(uid, "ach_edit", None, f"удалил {arg}")
        t, kb = ach_editor_view()
        await show(t, kb)
    elif action == "who":
        rows = db.execute("SELECT user_id, earned_at FROM user_achievements WHERE code=? "
                          "ORDER BY earned_at DESC LIMIT 15", (arg,)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM user_achievements WHERE code=?", (arg,)).fetchone()[0]
        txt = f"👥 Получили: {total}\n\n" + ("\n".join(f"{user_label(u)} — {fmt_ts(ts)}" for u, ts in rows)
                                           if rows else "Пока никто.")
        await callback.message.answer(txt)
    elif action == "thr":
        ach_draft_set(uid, {"mode": "edit", "step": "thr", "code": arg})
        await callback.message.answer("Введи новый порог (число). Для отмены нажми ← Назад",
                                      reply_markup=back_only_keyboard())
    elif action == "rw":
        await callback.message.answer("Какая награда?", reply_markup=ach_rtype_keyboard(f"ac:rt:{arg}"))
    elif action == "rt":
        code, rtype = arg, arg2
        if rtype not in ACH_RTYPES:
            await callback.answer()
            return
        if rtype == "none":
            db.execute("UPDATE achievements SET rtype='none', rvalue='' WHERE code=?", (code,))
            db.commit()
            log_action(uid, "ach_edit", None, f"{code} награда none")
            t, kb = ach_detail_view(code)
            await show(t, kb)
        else:
            ach_draft_set(uid, {"mode": "edit", "step": "rval", "code": code, "rtype": rtype})
            hint = {"premium_days": "число дней Premium (1–365)", "discount": "процент скидки (1–90)",
                    "status": "название статуса (до 30 символов)"}[rtype]
            await callback.message.answer(f"Введи {hint}. Для отмены нажми ← Назад",
                                          reply_markup=back_only_keyboard())
    elif action == "new":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"ac:nm:{k}")] for k, label in ACH_METRICS.items()])
        await callback.message.answer("За что выдавать достижение?", reply_markup=kb)
    elif action == "nm":
        if arg not in ACH_METRICS:
            await callback.answer()
            return
        if arg == "tester":
            ach_draft_set(uid, {"mode": "new", "step": "rtype", "metric": arg, "thr": 1})
            await callback.message.answer("Какая награда?", reply_markup=ach_rtype_keyboard("ac:nr"))
        else:
            ach_draft_set(uid, {"mode": "new", "step": "thr", "metric": arg})
            await callback.message.answer(f"Сколько нужно ({ACH_METRICS[arg]})? Введи число. "
                                          f"Для отмены нажми ← Назад", reply_markup=back_only_keyboard())
    elif action == "nr":
        d = ach_draft_get(uid)
        if not d or d.get("mode") != "new" or arg not in ACH_RTYPES:
            await callback.answer("Начни заново: ➕ Добавить.")
            return
        d["rtype"] = arg
        if arg == "none":
            d["step"] = "title"
            ach_draft_set(uid, d)
            await callback.message.answer("Придумай название достижения (до 40 символов):",
                                          reply_markup=back_only_keyboard())
        else:
            d["step"] = "rval"
            ach_draft_set(uid, d)
            hint = {"premium_days": "число дней Premium (1–365)", "discount": "процент скидки (1–90)",
                    "status": "название статуса (до 30 символов)"}[arg]
            await callback.message.answer(f"Введи {hint}.", reply_markup=back_only_keyboard())
    await callback.answer()


# ---------- Состояние бота, скидка 3-го дня, бонус, FAQ, кабинеты ----------

BOOT_TS = int(time.time())
DAY3_DISCOUNT_PCT = 20
DAY3_VALID_HOURS = 72
BONUS_DAYS = 3
BONUS_COOLDOWN_DAYS = 30

def _dir_size(path) -> tuple:
    total = cnt = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    cnt += 1
                except OSError:
                    pass
    except Exception:
        pass
    return total, cnt

def _errors_24h():
    import subprocess
    try:
        out = subprocess.run(
            ["journalctl", "-u", "salesbot", "--since", "24 hours ago", "-p", "err", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=8).stdout
        return len([ln for ln in out.splitlines() if ln.strip()])
    except Exception:
        return None

def _mb(n) -> str:
    return f"{n / 1048576:.1f} МБ" if n < 1073741824 else f"{n / 1073741824:.2f} ГБ"

async def health_text(show_money=True) -> str:
    import shutil
    now = now_ts()
    up = read_uptime()
    alive = get_setting("last_alive")
    alive_age = (now - int(alive)) if alive and alive.isdigit() else None
    try:
        la = os.getloadavg()
        load = f"{la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}"
    except Exception:
        load = "н/д"
    try:
        du = shutil.disk_usage("/")
        disk = f"свободно {_mb(du.free)} из {_mb(du.total)}"
    except Exception:
        disk = "н/д"
    try:
        db_size = _mb(os.path.getsize(DB_PATH)) if os.path.exists(DB_PATH) else "н/д"
    except Exception:
        db_size = "н/д"
    m_size, m_cnt = await asyncio.to_thread(_dir_size, MEDIA_DIR)
    errs = await asyncio.to_thread(_errors_24h)
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    users = q("SELECT COUNT(*) FROM users")
    new24 = q("SELECT COUNT(*) FROM users WHERE first_seen >= datetime('now','-1 day')")
    prem = q("SELECT COUNT(*) FROM users WHERE premium_until > ?", now)
    return (
        "🩺 Состояние бота\n\n"
        f"🖥 Сервер работает: {fmt_dur(up) if up >= 0 else 'н/д'}\n"
        f"🤖 Бот работает: {fmt_dur(now - BOOT_TS)}\n"
        f"💓 Последний пульс: {fmt_dur(alive_age) + ' назад' if alive_age is not None else 'н/д'}\n"
        f"📈 Нагрузка (1/5/15 мин): {load}\n"
        f"💽 Диск: {disk}\n"
        f"🗄 База: {db_size}\n"
        f"🖼 Медиа: {_mb(m_size)} ({m_cnt} файлов)\n"
        f"⚠️ Ошибок за 24 ч: {errs if errs is not None else 'н/д'}\n\n"
        f"👥 Пользователей: {users} (за сутки +{new24})\n"
        f"💎 Premium активно: {prem}\n"
        f"💾 Сообщений в кэше: {q('SELECT COUNT(*) FROM message_cache')}\n"
        f"🧾 Заказов ждут: {q('SELECT COUNT(*) FROM orders WHERE status=?', 'pending')}\n"
        f"💸 Заявок на вывод: {q('SELECT COUNT(*) FROM withdrawals WHERE status=?', 'pending')}\n"
        f"🎁 Заявок на бонус: {q('SELECT COUNT(*) FROM bonus_requests WHERE status=?', 'pending')}\n"
        f"🐛 Открытых багов: {q('SELECT COUNT(*) FROM bug_reports WHERE status=?', 'open')}\n"
        f"❓ Вопросов в FAQ: {q('SELECT COUNT(*) FROM faq')}"
        + (f"\n\n{revenue_text()}" if show_money else ""))

# --- скидка на 3-й день ---

async def check_day3_offers() -> int:
    now = now_ts()
    rows = db.execute(
        "SELECT user_id FROM users WHERE first_seen <= datetime('now','-3 days') "
        "AND first_seen >= datetime('now','-60 days') AND COALESCE(premium_until,0) = 0 AND ? > 0 AND user_id != ? "
        "AND notifications = 1 AND user_id NOT IN (SELECT user_id FROM day3_offers) "
        "AND user_id NOT IN (SELECT user_id FROM orders WHERE status='paid') "
        "AND user_id NOT IN (SELECT user_id FROM staff) LIMIT 30", (now, ADMIN_ID)).fetchall()
    sent = 0
    for (uid,) in rows:
        db.execute("INSERT OR IGNORE INTO day3_offers (user_id, sent_at) VALUES (?,?)", (uid, now))
        db.commit()
        if is_user_banned(uid)[0] or active_discount(uid):
            continue
        db.execute("UPDATE users SET discount_pct=?, discount_until=?, discount_code='DAY3' WHERE user_id=?",
                   (DAY3_DISCOUNT_PCT, now + DAY3_VALID_HOURS * 3600, uid))
        db.commit()
        try:
            await bot.send_message(
                uid,
                f"🎁 Подарочный Premium закончился — держи скидку {DAY3_DISCOUNT_PCT}% на любой тариф Premium "
                f"(кроме пробного). Действует {DAY3_VALID_HOURS} ч, до {fmt_ts(now + DAY3_VALID_HOURS * 3600)}.\n\n"
                f"Полная история удалённых сообщений и одноразовые фото и видео без лимита.",
                reply_markup=tariff_keyboard(uid))
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return sent

async def offer_loop():
    while True:
        try:
            n = await check_day3_offers()
            if n:
                logging.info(f"Скидки 3-го дня: отправлено {n}")
        except Exception:
            logging.exception("Ошибка offer_loop")
        await asyncio.sleep(1800)

# --- бонус за репост ---

def bonus_post_text(uid) -> str:
    return ("🕵️ Нашёл бота, который присылает удалённые и изменённые сообщения из личных чатов "
            "и спасает одноразовые фото и видео. Работает через Telegram Business, подключается за минуту.\n\n"
            f"Попробовать: {ref_link(uid)}")

def bonus_state(uid):
    """('pending', None) | ('cooldown', до_какого_ts) | ('ok', None)"""
    if db.execute("SELECT 1 FROM bonus_requests WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        return "pending", None
    r = db.execute("SELECT MAX(done_at) FROM bonus_requests WHERE user_id=? AND status='approved'",
                   (uid,)).fetchone()[0]
    if r and r + BONUS_COOLDOWN_DAYS * 86400 > now_ts():
        return "cooldown", r + BONUS_COOLDOWN_DAYS * 86400
    return "ok", None

async def show_bonus(message, uid):
    st, until = bonus_state(uid)
    await message.answer(
        f"🎁 Бонус за репост\n\nРепостни пост ниже в свой канал или сторис — и получи {BONUS_DAYS} дн. Premium "
        f"бесплатно. Бонус можно получить раз в {BONUS_COOLDOWN_DAYS} дн.\n"
        f"Ссылка в посте — твоя реферальная: за тех, кто придёт по ней, тебе ещё и платят.\n\n"
        f"Как получить: 1) скопируй текст ниже и опубликуй; 2) нажми «✅ Я опубликовал» и пришли скриншот "
        f"или ссылку; 3) владелец проверит и начислит дни.")
    await message.answer(bonus_post_text(uid))
    if st == "pending":
        await message.answer("⏳ Твоя заявка уже на проверке.")
    elif st == "cooldown":
        await message.answer(f"Следующий бонус будет доступен после {fmt_ts(until)}.")
    else:
        await message.answer("Когда опубликуешь — нажми:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Я опубликовал", callback_data="bn:done")]]))

def bonus_keyboard(rid) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ +{BONUS_DAYS} дн", callback_data=f"bn:ok:{rid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"bn:no:{rid}")]])

async def bonus_callback(callback, uid, data):
    parts = data.split(":")
    if parts[1] == "done":
        st, until = bonus_state(uid)
        if st != "ok":
            await callback.answer("Заявка уже есть или бонус пока недоступен.", show_alert=True)
            return
        set_setting(f"bonus_wait_{uid}", "1")
        await callback.message.answer("Пришли скриншот поста или ссылку на него одним сообщением.\n"
                                      "Отмена — «← Назад».", reply_markup=back_only_keyboard())
        await callback.answer()
        return
    if not is_owner(uid):
        await callback.answer("Только владелец.")
        return
    rid = int(parts[2])
    r = db.execute("SELECT user_id, status FROM bonus_requests WHERE id=?", (rid,)).fetchone()
    if not r:
        await callback.answer("Заявка не найдена.")
        return
    target, status = r
    if status != "pending":
        await callback.answer("Уже обработана.")
        return
    if parts[1] == "ok":
        db.execute("UPDATE bonus_requests SET status='approved', done_at=?, days=? WHERE id=?",
                   (now_ts(), BONUS_DAYS, rid))
        db.commit()
        log_action(uid, "bonus_ok", target, f"#{rid} +{BONUS_DAYS} дн")
        until = await do_grant(uid, target, BONUS_DAYS)
        await callback.message.edit_text(f"✅ Бонус #{rid} начислен: {user_label(target)}, +{BONUS_DAYS} дн "
                                         f"(до {fmt_ts(until)})")
        await callback.answer("Начислено")
    else:
        db.execute("UPDATE bonus_requests SET status='rejected', done_at=? WHERE id=?", (now_ts(), rid))
        db.commit()
        log_action(uid, "bonus_no", target, f"#{rid}")
        await callback.message.edit_text(f"❌ Бонус #{rid} отклонён ({user_label(target)}).")
        try:
            await bot.send_message(target, f"Заявка на бонус не подтверждена. Если это ошибка — {OWNER_CONTACT}.")
        except Exception:
            pass
        await callback.answer("Отклонено")

# --- FAQ ---

def faq_rows():
    return db.execute("SELECT id, question FROM faq ORDER BY pos, id").fetchall()

def faq_user_view():
    rows = faq_rows()
    if not rows:
        return "❓ FAQ пока пуст.", None
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=q[:60], callback_data=f"faq:{i}")] for i, q in rows])
    return "❓ Частые вопросы — выбери:", kb

def faq_editor_view():
    rows = faq_rows()
    kb_rows = [[InlineKeyboardButton(text=f"✏️ {q[:50]}", callback_data=f"fq:e:{i}")] for i, q in rows]
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить вопрос", callback_data="fq:add")])
    return (f"❓ Редактор FAQ — вопросов: {len(rows)}\nВыбери вопрос, чтобы изменить, или добавь новый.",
            InlineKeyboardMarkup(inline_keyboard=kb_rows))

def faq_item_view(fid):
    r = db.execute("SELECT question, answer FROM faq WHERE id=?", (fid,)).fetchone()
    if not r:
        return None, None
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Вопрос", callback_data=f"fq:q:{fid}"),
         InlineKeyboardButton(text="✏️ Ответ", callback_data=f"fq:a:{fid}")],
        [InlineKeyboardButton(text="⬆️ Выше", callback_data=f"fq:up:{fid}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"fq:del:{fid}")],
        [InlineKeyboardButton(text="← К списку", callback_data="fq:list")]])
    return f"❓ {r[0]}\n\n{r[1]}", kb

async def faq_editor_callback(callback, uid, data):
    parts = data.split(":")
    act = parts[1]
    fid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    if act == "list":
        t, kb = faq_editor_view()
        await callback.message.edit_text(t, reply_markup=kb)
    elif act == "add":
        set_setting(f"faq_wait_{uid}", "addq")
        await callback.message.answer("Пришли текст вопроса (коротко, до 60 символов).\nОтмена — «← Назад».")
    elif act == "e" and fid:
        t, kb = faq_item_view(fid)
        if t is None:
            await callback.answer("Не найден.")
            return
        await callback.message.edit_text(t, reply_markup=kb)
    elif act == "q" and fid:
        set_setting(f"faq_wait_{uid}", f"eq:{fid}")
        await callback.message.answer("Пришли новый текст вопроса.\nОтмена — «← Назад».")
    elif act == "a" and fid:
        set_setting(f"faq_wait_{uid}", f"ea:{fid}")
        await callback.message.answer("Пришли новый текст ответа.\nОтмена — «← Назад».")
    elif act == "up" and fid:
        rows = [r[0] for r in faq_rows()]
        if fid in rows and rows.index(fid) > 0:
            i = rows.index(fid)
            rows[i - 1], rows[i] = rows[i], rows[i - 1]
            for pos, rid in enumerate(rows, 1):
                db.execute("UPDATE faq SET pos=? WHERE id=?", (pos, rid))
            db.commit()
        t, kb = faq_editor_view()
        await callback.message.edit_text(t, reply_markup=kb)
    elif act == "del" and fid:
        await callback.message.edit_text(
            "Удалить этот вопрос?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"fq:delok:{fid}"),
                InlineKeyboardButton(text="← Нет", callback_data=f"fq:e:{fid}")]]))
    elif act == "delok" and fid:
        db.execute("DELETE FROM faq WHERE id=?", (fid,))
        db.commit()
        log_action(uid, "faq_del", None, f"#{fid}")
        t, kb = faq_editor_view()
        await callback.message.edit_text(t, reply_markup=kb)
    await callback.answer()

# --- кабинет пользователя глазами админа ---

def admin_user_view(uid) -> str:
    q = lambda sql, *a: db.execute(sql, a).fetchone()
    inviter = q("SELECT referrer_id FROM referrals WHERE user_id=?", uid)
    invited = q("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", uid)[0]
    paid_inv = q("SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN referrals r ON r.user_id=o.user_id "
                 "WHERE r.referrer_id=? AND o.status='paid' AND COALESCE(o.currency,'RUB')!='BAL'", uid)[0]
    earned = q("SELECT COALESCE(SUM(amount_kop),0) FROM ref_ledger WHERE user_id=? "
               "AND kind IN ('signup','commission','clawback')", uid)[0]
    total, avail = ref_balance(uid)
    out = q("SELECT COALESCE(SUM(amount_kop),0) FROM withdrawals WHERE user_id=? AND status='paid'", uid)[0]
    pend_wd = q("SELECT COALESCE(SUM(amount_kop),0) FROM withdrawals WHERE user_id=? AND status='pending'", uid)[0]
    card_n, card_s = q("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=? AND status='paid' "
                       "AND COALESCE(currency,'RUB')='RUB'", uid)
    star_n, star_s = q("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=? AND status='paid' "
                       "AND currency='XTR'", uid)
    bal_n, bal_s = q("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=? AND status='paid' "
                     "AND currency='BAL'", uid)
    row = q("SELECT trial_used FROM users WHERE user_id=?", uid)
    d = active_discount(uid)
    bonus_n = q("SELECT COUNT(*) FROM bonus_requests WHERE user_id=? AND status='approved'", uid)[0]
    return (user_card_text(uid) + "\n\n"
            f"🔗 Пригласил его: {user_label(inviter[0]) if inviter else '—'}\n"
            f"🎟 Пробный использован: {'да' if row and row[0] else 'нет'}\n"
            f"🏷 Скидка: {f'{d[0]}% до {fmt_ts(d[1])}' if d else 'нет'}\n"
            f"🎁 Бонусов за репост: {bonus_n}\n\n"
            f"🧾 Оплаты: картой {card_n} шт. ({card_s} ₽), звёздами {star_n} шт. ({star_s} ⭐), "
            f"с баланса {bal_n} шт. ({bal_s} ₽)\n\n"
            f"👥 Рефералка:\n"
            f"• Приглашено: {invited} (платили: {paid_inv})\n"
            f"• Заработано всего: {fmt_rub(earned)}\n"
            f"• Доступно: {fmt_rub(max(avail, 0))}, ждёт {REF_HOLD_DAYS} дн.: {fmt_rub(max(total - avail, 0))}\n"
            f"• Выплачено: {fmt_rub(out)}, в заявках: {fmt_rub(pend_wd)}\n"
            f"• Реф. программа: {'⛔ закрыта' if ref_banned(uid) else 'активна'}")

# --- общий обработчик ожиданий и кнопки ---

async def handle_waits(message, uid, text) -> bool:
    for key in (f"faq_wait_{uid}", f"adm_wait_{uid}", f"bonus_wait_{uid}", f"tt_wait_{uid}", f"exp_wait_{uid}"):
        w = get_setting(key)
        if not w:
            continue
        if text in MENU_TEXTS:
            del_setting(key)
            continue
        if key.startswith("tt_wait"):
            if not is_owner(uid):
                del_setting(key)
                continue
            if not text:
                await message.answer("Пришли текстом.")
                return True
            if w == "add":
                target = resolve_user_id(text)
                if target is None:
                    await message.answer("Не нашёл такого. @ник работает только для тех, кто писал боту — "
                                         "попробуй числовой ID.")
                    return True
                del_setting(key)
                tester_add(target, uid)
                log_action(uid, "tester_add", target, "")
                try:
                    await bot.send_message(target, "🧪 Тебя назначили тестером! Тебе доступны все функции Premium "
                                                   "и ранний доступ к новому. Если что-то работает не так — "
                                                   "«🐛 Сообщить о баге».", reply_markup=main_keyboard(target))
                except Exception:
                    pass
                t, kb = tester_view()
                await message.answer(f"✅ {user_label(target)} теперь тестер.")
                await message.answer(t, reply_markup=kb)
                return True
            if w == "bc":
                del_setting(key)
                ids = [r[0] for r in db.execute("SELECT user_id FROM testers").fetchall()]
                sent_n = bad_n = 0
                for tid in ids:
                    try:
                        await bot.send_message(tid, f"🧪 Сообщение для тестеров:\n\n{text}")
                        sent_n += 1
                    except Exception:
                        bad_n += 1
                    await asyncio.sleep(0.05)
                await message.answer(f"✅ Отправлено тестерам: {sent_n}, не доставлено: {bad_n}")
                return True
            del_setting(key)
            continue
        if key.startswith("exp_wait"):
            if not w.isdigit() or now_ts() - int(w) > 600:
                del_setting(key)
                continue
            if not text:
                await message.answer("Пришли @ник собеседника текстом.")
                return True
            q = text.strip().lstrip("@").lower()
            found = [c for c in user_export_chats(uid, 500) if q and q in c[1].lower().lstrip("@")]
            if not found:
                await message.answer("Не нашёл такого среди твоих чатов. Проверь ник или выбери кнопкой из «📥 Экспорт».")
                return True
            del_setting(key)
            await message.answer("Нашёл — выбери:", reply_markup=export_chats_keyboard(found[:10]))
            return True
        if key.startswith("faq_wait"):
            if not is_owner(uid):
                del_setting(key)
                continue
            if not text:
                await message.answer("Пришли текстом.")
                return True
            if w == "addq":
                if len(text) > 60:
                    await message.answer("Слишком длинно (до 60 символов). Сократи и пришли снова.")
                    return True
                set_setting(f"faq_tmp_{uid}", text)
                set_setting(key, "adda")
                await message.answer("Теперь пришли текст ответа.")
                return True
            if len(text) > 3500:
                await message.answer("Слишком длинно (до 3500 символов).")
                return True
            if w == "adda":
                qtext = get_setting(f"faq_tmp_{uid}") or "Вопрос"
                pos = (db.execute("SELECT COALESCE(MAX(pos),0) FROM faq").fetchone()[0]) + 1
                db.execute("INSERT INTO faq (question, answer, pos) VALUES (?,?,?)", (qtext, text, pos))
                db.commit()
                del_setting(key)
                del_setting(f"faq_tmp_{uid}")
                log_action(uid, "faq_add", None, qtext[:40])
            elif w.startswith("eq:") or w.startswith("ea:"):
                fid = int(w.split(":")[1])
                if w.startswith("eq:"):
                    if len(text) > 60:
                        await message.answer("Слишком длинно (до 60 символов).")
                        return True
                    db.execute("UPDATE faq SET question=? WHERE id=?", (text, fid))
                else:
                    db.execute("UPDATE faq SET answer=? WHERE id=?", (text, fid))
                db.commit()
                del_setting(key)
                log_action(uid, "faq_edit", None, f"#{fid}")
            t, kb = faq_editor_view()
            await message.answer("✅ Сохранено.", reply_markup=panel_kb(uid))
            await message.answer(t, reply_markup=kb)
            return True
        if key.startswith("adm_wait"):
            if not has_panel(uid):
                del_setting(key)
                continue
            if not text:
                await message.answer("Пришли ID или @ник текстом.")
                return True
            target = resolve_user_id(text)
            if target is None:
                await message.answer("Не нашёл такого. @ник работает только для тех, кто писал боту — "
                                     "попробуй числовой ID.")
                return True
            del_setting(key)
            await message.answer(admin_user_view(target), reply_markup=grant_keyboard(target, uid))
            return True
        if key.startswith("bonus_wait"):
            if not (getattr(message, "photo", None) or getattr(message, "document", None) or len(text) >= 5):
                await message.answer("Пришли скриншот поста или ссылку на него.")
                return True
            st, _until = bonus_state(uid)
            del_setting(key)
            if st != "ok":
                await message.answer("Заявка уже есть или бонус пока недоступен.", reply_markup=main_keyboard(uid))
                return True
            cur = db.execute("INSERT INTO bonus_requests (user_id, status, created_at) VALUES (?, 'pending', ?)",
                             (uid, now_ts()))
            rid = cur.lastrowid
            db.commit()
            try:
                await bot.forward_message(ADMIN_ID, uid, getattr(message, "message_id", 0))
            except Exception:
                logging.exception("Не удалось переслать доказательство бонуса")
            try:
                await bot.send_message(ADMIN_ID, f"🎁 Заявка на бонус #{rid} от {user_label(uid)}\n"
                                                 f"Проверь пост и подтверди (+{BONUS_DAYS} дн Premium).",
                                       reply_markup=bonus_keyboard(rid))
            except Exception:
                logging.exception("Не удалось уведомить владельца о бонусе")
            await message.answer("✅ Заявка отправлена. Как только владелец проверит — дни зачислятся.",
                                 reply_markup=main_keyboard(uid))
            return True
    return False

async def handle_extra_text(message, uid, text) -> bool:
    if text == "📥 Экспорт":
        if is_owner(uid):
            return False            # у владельца свой экспорт (все чаты), ниже в админской части
        await show_user_export(message, uid)
        return True
    if text == "🧪 Тестеры":
        if not is_owner(uid):
            return False
        t, kb = tester_view()
        await message.answer(t, reply_markup=kb)
        return True
    if text == "❓ FAQ":
        t, kb = faq_user_view()
        await message.answer(t, reply_markup=kb)
        return True
    if text == "🩺 Состояние":
        if not has_panel(uid):
            return False
        await message.answer(await health_text(is_owner(uid)), reply_markup=panel_kb(uid))
        return True
    if text == "❓ Редактор FAQ":
        if not is_owner(uid):
            return False
        t, kb = faq_editor_view()
        await message.answer(t, reply_markup=kb)
        return True
    if text == "🔎 Кабинет юзера":
        if not has_panel(uid):
            return False
        set_setting(f"adm_wait_{uid}", "view")
        await message.answer("Пришли ID или @ник пользователя.\nДля отмены нажми ← Назад")
        return True
    return False

@dp.message(Command("health"))
async def cmd_health(message: Message):
    if not message.from_user or not has_panel(message.from_user.id):
        return
    await message.answer(await health_text(is_owner(message.from_user.id)))

@dp.message(Command("user"))
async def cmd_user(message: Message):
    if not message.from_user or not has_panel(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /user ID_или_@ник")
        return
    target = resolve_user_id(parts[1])
    if target is None:
        await message.answer("Не нашёл такого.")
        return
    await message.answer(admin_user_view(target), reply_markup=grant_keyboard(target, message.from_user.id))

# ---------- Подарок новым, тестеры, экспорт переписки ----------

import re as _re

NEW_USER_PREMIUM_DAYS = 30       # сколько дней Premium получает новый пользователь
EXPORT_MAX_PER_DAY = 3           # заявок на экспорт в сутки на человека
_OUT_RE = _re.compile(r"^\((?:❗️?){2}ты → (.*)\)$", _re.S)
_IN_RE = _re.compile(r"^\((.*)\)$", _re.S)

def safe_name(s) -> str:
    return _re.sub(r"[^\w@.\-]", "_", s or "chat")[:40]

# --- тестеры ---

def is_tester(uid) -> bool:
    return db.execute("SELECT 1 FROM testers WHERE user_id=?", (uid,)).fetchone() is not None

def tester_add(uid, by):
    ensure_user(uid)
    db.execute("INSERT OR IGNORE INTO testers (user_id, added_at, added_by) VALUES (?,?,?)", (uid, now_ts(), by))
    db.commit()

def tester_del(uid):
    db.execute("DELETE FROM testers WHERE user_id=?", (uid,))
    db.commit()

def tester_view():
    rows = db.execute("SELECT user_id FROM testers ORDER BY added_at").fetchall()
    text = ("🧪 Тестеры — получают все функции Premium и ранний доступ к новому.\n\n"
            + ("\n".join(f"• {user_label(r[0])}" for r in rows) if rows else "Пока никого нет."))
    kb_rows = [[InlineKeyboardButton(text=f"➖ {user_label(r[0])}"[:60], callback_data=f"tt:del:{r[0]}")]
               for r in rows]
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="tt:add")])
    if rows:
        kb_rows.append([InlineKeyboardButton(text="📣 Написать тестерам", callback_data="tt:bc")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)

@dp.message(Command("tester"))
async def cmd_tester(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /tester ID_или_@ник (снять: /untester ID_или_@ник)")
        return
    target = resolve_user_id(parts[1])
    if target is None:
        await message.answer("Не нашёл такого.")
        return
    tester_add(target, ADMIN_ID)
    log_action(ADMIN_ID, "tester_add", target, "")
    await message.answer(f"✅ {user_label(target)} теперь тестер.")

@dp.message(Command("untester"))
async def cmd_untester(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    target = resolve_user_id(parts[1]) if len(parts) == 2 else None
    if target is None:
        await message.answer("Формат: /untester ID_или_@ник")
        return
    tester_del(target)
    log_action(ADMIN_ID, "tester_del", target, "")
    await message.answer(f"✅ {user_label(target)} больше не тестер.")

# --- экспорт переписки ---

def parse_tag(tag):
    """(исходящее?, имя собеседника из тега)"""
    tag = tag or ""
    m = _OUT_RE.match(tag)
    if m:
        return True, m.group(1).strip()
    m = _IN_RE.match(tag)
    if m:
        return False, m.group(1).strip()
    return False, tag.strip()

def owner_label(owner_id) -> str:
    row = db.execute("SELECT username FROM users WHERE user_id=?", (owner_id,)).fetchone()
    return f"@{row[0]}" if row and row[0] else f"id{owner_id}"

def chat_partner_label(owner_id, chat_id) -> str:
    rows = db.execute("SELECT tag FROM message_cache WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT 200",
                      (owner_id, chat_id)).fetchall()
    parsed = [parse_tag(r[0]) for r in rows]
    outs = [n for o, n in parsed if o and n]
    ins = [n for o, n in parsed if not o and n]
    return outs[0] if outs else (ins[0] if ins else f"id{chat_id}")

def user_export_chats(owner_id, limit=20):
    """Чаты ТОЛЬКО этого владельца: [(chat_id, имя, число сообщений)]"""
    rows = db.execute("SELECT chat_id, COUNT(*) c FROM message_cache WHERE owner_id=? GROUP BY chat_id "
                      "ORDER BY c DESC LIMIT ?", (owner_id, limit)).fetchall()
    return [(cid, chat_partner_label(owner_id, cid), c) for cid, c in rows]

def export_chats_keyboard(chats) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{label} ({c} сообщ.)"[:60], callback_data=f"xr:{cid}")]
            for cid, label, c in chats]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="xr:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_export(owner_id, chat_id):
    """Файл переписки владельца owner_id с одним собеседником (chat_id). -> (bytes, info)"""
    rows = db.execute(
        "SELECT tag, body, time_str FROM message_cache WHERE owner_id=? AND chat_id=? "
        "ORDER BY COALESCE(sent_at,0), id", (owner_id, chat_id)).fetchall()
    unknown = db.execute("SELECT COUNT(*) FROM message_cache WHERE chat_id=? AND owner_id IS NULL",
                         (chat_id,)).fetchone()[0]
    me = owner_label(owner_id)
    partner = chat_partner_label(owner_id, chat_id)
    names = set()
    blocks = []
    for tag, body, ts in rows:
        out, name = parse_tag(tag)
        name = name or partner
        names.add(name)
        head = f"❗️❗️ты {me} → {name}" if out else f"{name} → ты {me}"
        blocks.append(f"{head}\n{body}\n({ts})\n---")
    period = f"{rows[0][2]} — {rows[-1][2]}" if rows else "—"
    head = [f"=== Экспорт переписки: ты {me} ↔ {partner} ===",
            f"Сообщений: {len(rows)}",
            f"Период: {period}",
            f"Дата экспорта: {datetime.now(TZ).strftime('%H:%M %d.%m.%y')}",
            f"Только переписка с этим собеседником. Это сообщения из кэша бота "
            f"(хранятся не дольше {TEXT_RETENTION_DAYS} дн.).",
            "=" * 40, ""]
    if len(names) > 1:
        head.insert(5, "Имена собеседника в истории: " + ", ".join(sorted(names)))
    text = "\n".join(head) + "\n" + "\n".join(blocks) + "\n"
    return text.encode("utf-8"), {"n": len(rows), "partner": partner, "me": me, "names": len(names),
                                  "unknown": unknown}

def export_keyboard_v2():
    rows = db.execute(
        "SELECT chat_id, owner_id, COUNT(*) c FROM message_cache WHERE owner_id IS NOT NULL "
        "GROUP BY chat_id, owner_id ORDER BY c DESC LIMIT 20").fetchall()
    if not rows:
        return None
    kb = [[InlineKeyboardButton(
        text=f"{owner_label(o)} ↔ {chat_partner_label(o, cid)} ({c})"[:60], callback_data=f"exp:{cid}:{o}")]
        for cid, o, c in rows]
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="exp:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def show_user_export(message, uid):
    if not is_premium(uid):
        await message.answer("📥 Экспорт переписки доступен в Premium: файл с историей твоей переписки с "
                             "выбранным человеком.", reply_markup=tariff_keyboard(uid))
        return
    chats = user_export_chats(uid, 20)
    if not chats:
        await message.answer("Пока нет сохранённых переписок — бот сохраняет сообщения только из чатов, "
                             "подключённых через Telegram Business, начиная с момента подключения.")
        return
    set_setting(f"exp_wait_{uid}", str(now_ts()))
    await message.answer(
        "📥 Экспорт переписки\n\nВыбери собеседника — бот создаст заявку. Владелец проверит файл, и он придёт "
        "тебе сюда (обычно в течение суток). В файле только твоя переписка с этим человеком.\n"
        "Нужного нет в списке — пришли его @ник.",
        reply_markup=export_chats_keyboard(chats))

async def create_export_request(uid, chat_id) -> str:
    if not is_premium(uid):
        return "Экспорт доступен только в Premium."
    own = db.execute("SELECT COUNT(*) FROM message_cache WHERE owner_id=? AND chat_id=?", (uid, chat_id)).fetchone()[0]
    if not own:
        return "Такой переписки у тебя нет."
    if db.execute("SELECT 1 FROM export_requests WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        return "У тебя уже есть заявка на экспорт — дождись ответа."
    if db.execute("SELECT COUNT(*) FROM export_requests WHERE user_id=? AND created_at>?",
                  (uid, now_ts() - 86400)).fetchone()[0] >= EXPORT_MAX_PER_DAY:
        return f"Не больше {EXPORT_MAX_PER_DAY} заявок на экспорт в сутки. Попробуй позже."
    data, info = build_export(uid, chat_id)
    cur = db.execute("INSERT INTO export_requests (user_id, chat_id, partner, n, status, created_at, file) "
                     "VALUES (?,?,?,?, 'pending', ?, ?)", (uid, chat_id, info["partner"], info["n"], now_ts(), data))
    rid = cur.lastrowid
    db.commit()
    log_action(uid, "export_request", uid, f"#{rid} {info['partner']} {info['n']} сообщ.")
    cap = (f"📥 Заявка на экспорт #{rid}\nОт: {user_label(uid)}\nСобеседник: {info['partner']}\n"
           f"Сообщений: {info['n']}\n"
           f"Разных имён собеседника: {info['names']}{' ⚠️' if info['names'] > 1 else ''}\n"
           f"Старых записей без владельца (в файл не входят): {info['unknown']}\n\n"
           f"Проверь файл. «Выдать» отправит его пользователю.")
    try:
        await bot.send_document(
            ADMIN_ID, BufferedInputFile(data, filename=f"export_req{rid}_{safe_name(info['partner'])}.txt"),
            caption=cap, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Выдать", callback_data=f"xo:ok:{rid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"xo:no:{rid}")]]))
    except Exception:
        logging.exception("Не удалось отправить заявку на экспорт владельцу")
    return f"✅ Заявка #{rid} создана: {info['partner']}, {info['n']} сообщ. Файл придёт сюда после проверки."

async def export_owner_action(callback, uid, data):
    if not is_owner(uid):
        await callback.answer("Только владелец.")
        return
    _, act, rid_s = data.split(":")
    r = db.execute("SELECT user_id, partner, n, status, file FROM export_requests WHERE id=?",
                   (int(rid_s),)).fetchone()
    if not r:
        await callback.answer("Заявка не найдена.")
        return
    target, partner, n, status, blob = r
    rid = int(rid_s)
    if status != "pending":
        await callback.answer("Уже обработана.")
        return
    if act == "ok":
        try:
            await bot.send_document(
                target, BufferedInputFile(bytes(blob), filename=f"export_{safe_name(partner)}.txt"),
                caption=f"📥 Экспорт переписки с {partner} ({n} сообщ.)")
        except Exception:
            logging.exception("Не удалось отправить экспорт пользователю")
            await callback.answer("Не удалось отправить пользователю.", show_alert=True)
            return
        db.execute("UPDATE export_requests SET status='approved', done_at=?, file=NULL WHERE id=?", (now_ts(), rid))
        db.commit()
        log_action(uid, "export_ok", target, f"#{rid}")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"✅ Экспорт #{rid} отправлен: {user_label(target)}")
        await callback.answer("Отправлено")
    else:
        db.execute("UPDATE export_requests SET status='rejected', done_at=?, file=NULL WHERE id=?", (now_ts(), rid))
        db.commit()
        log_action(uid, "export_no", target, f"#{rid}")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"❌ Экспорт #{rid} отклонён ({user_label(target)}).")
        try:
            await bot.send_message(target, f"Заявка на экспорт #{rid} отклонена. Вопросы: {OWNER_CONTACT}")
        except Exception:
            pass
        await callback.answer("Отклонено")

@dp.message(Command("exports"))
async def cmd_exports(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    rows = db.execute("SELECT id, user_id, partner, n, file FROM export_requests WHERE status='pending' "
                      "ORDER BY id").fetchall()
    if not rows:
        await message.answer("Заявок на экспорт нет.")
        return
    for rid, u, partner, n, blob in rows:
        await bot.send_document(
            ADMIN_ID, BufferedInputFile(bytes(blob), filename=f"export_req{rid}_{safe_name(partner)}.txt"),
            caption=f"📥 Заявка #{rid}: {user_label(u)} ↔ {partner}, {n} сообщ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Выдать", callback_data=f"xo:ok:{rid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"xo:no:{rid}")]]))


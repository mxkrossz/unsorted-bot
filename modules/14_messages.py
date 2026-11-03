# ---------- Обработчик обычных сообщений ----------

@dp.message()
async def on_plain_message(message: Message):
    if not message.from_user:
        return
    if message.business_connection_id:
        return
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    text = (message.text or "").strip()

    ensure_user(user_id, message.from_user.username or "")

    # Проверка бана
    banned, ban_msg = is_user_banned(user_id)
    if banned:
        await message.answer(ban_msg)
        return

    # 👀 — повторная пересылка медиа (для всех)
    if "👀" in text and message.reply_to_message:
        sent = await send_media_from_message(message.reply_to_message, user_id)
        if not sent:
            await message.answer("В том сообщении нет медиа.")
        return

    if await handle_rt_wait(message, user_id, text):
        return

    if await handle_ach_wait(message, user_id, text):
        return

    if await handle_feedback_wait(message, user_id, text):
        return

    if await handle_offer_wait(message, user_id, text):
        return

    if await handle_waits(message, user_id, text):
        return

    if get_setting(f"ref_wait_{user_id}") == "wd":
        if text in MENU_TEXTS:
            del_setting(f"ref_wait_{user_id}")
        else:
            await handle_withdraw_details(message, user_id, text)
            return

    # ---------- Кнопки меню ----------

    if text in CAB_TEXTS or text == "👤 Личный кабинет":
        if await handle_cabinet_text(message, user_id, text):
            return

    if await handle_rt_text(message, user_id, text):
        return

    if await handle_ach_text(message, user_id, text):
        return

    if await handle_feedback_text(message, user_id, text):
        return

    if text in ("❓ FAQ", "🩺 Состояние", "❓ Редактор FAQ", "🔎 Кабинет юзера", "🧪 Тестеры", "📥 Экспорт"):
        if await handle_extra_text(message, user_id, text):
            return

    if text == "← Назад":
        del_setting(f"bug_report_pending_{user_id}")
        del_setting(f"st_state_{user_id}")
        del_setting(f"promo_wait_{user_id}")
        del_setting(f"ref_wait_{user_id}")
        await message.answer("Главное меню:", reply_markup=main_keyboard(user_id))
        return

    if text == "📡 Мониторинг":
        mon  = "включён ✅" if user_monitoring_on(user_id) else "выключен ❌"
        edit = "включено ✅" if user_track_edits_on(user_id) else "выключено ❌"
        own  = "включены ✅" if user_own_edit_notify_on(user_id) else "выключены ❌"
        await message.answer(
            f"📡 Мониторинг удалённых: {mon}\n"
            f"✏️ Отслеживание правок: {edit}\n"
            f"🔔 Уведомления о своих правках: {own}\n\n"
            f"Выбери действие:",
            reply_markup=monitoring_keyboard(user_id)
        )
        return

    if text == "✅ Включить мониторинг":
        set_user_monitoring(user_id, 1)
        await message.answer("✅ Мониторинг включён.", reply_markup=monitoring_keyboard(user_id))
        return

    if text == "❌ Выключить мониторинг":
        set_user_monitoring(user_id, 0)
        await message.answer("❌ Мониторинг выключен.", reply_markup=monitoring_keyboard(user_id))
        return

    if text == "✏️ Включить отслеживание правок":
        set_user_track_edits(user_id, 1)
        await message.answer("✏️ Отслеживание правок включено.", reply_markup=monitoring_keyboard(user_id))
        return

    if text == "✏️ Выключить отслеживание правок":
        set_user_track_edits(user_id, 0)
        await message.answer("✏️ Отслеживание правок выключено.", reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🔔 Вкл. уведомления о своих правках":
        set_user_own_edit_notify(user_id, 1)
        await message.answer("🔔 Теперь ты будешь получать уведомления когда сам редактируешь сообщения.",
                             reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🔕 Выкл. уведомления о своих правках":
        set_user_own_edit_notify(user_id, 0)
        await message.answer("🔕 Уведомления о своих правках выключены.",
                             reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🔥 Вкл. спасение одноразовых":
        set_user_capture_once(user_id, 1)
        await message.answer(
            "🔥 Спасение одноразовых включено.\n"
            "Ответь на одноразовое фото/видео/кружок, не открывая его, — бот пришлёт файл тебе.",
            reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🛡 Выкл. защиту от ссылок":
        set_setting(f"lg_off_{user_id}", "1")
        await message.answer("🛡 Защита от ссылок выключена. Включить снова можно здесь же.",
                             reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🛡 Вкл. защиту от ссылок":
        set_setting(f"lg_off_{user_id}", "0")
        await message.answer("🛡 Защита от ссылок включена: бот предупредит о подозрительных ссылках во входящих сообщениях.",
                             reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🔥 Выкл. спасение одноразовых":
        set_user_capture_once(user_id, 0)
        await message.answer("🔥 Спасение одноразовых выключено.",
                             reply_markup=monitoring_keyboard(user_id))
        return

    if text == "🔔 Уведомления":
        status = "включены ✅" if user_notifications_on(user_id) else "выключены ❌"
        await message.answer(
            f"Уведомления от администратора сейчас {status}.\nВыбери действие:",
            reply_markup=notifications_keyboard()
        )
        return

    if text == "🔔 Включить уведомления":
        set_user_notifications(user_id, 1)
        await message.answer("🔔 Уведомления включены.", reply_markup=notifications_keyboard())
        return

    if text == "🔕 Выключить уведомления":
        set_user_notifications(user_id, 0)
        await message.answer("🔕 Уведомления выключены.", reply_markup=notifications_keyboard())
        return

    if text == "ℹ️ Мой статус":
        mon   = "✅ включён"  if user_monitoring_on(user_id)    else "❌ выключен"
        notif = "✅ включены" if user_notifications_on(user_id) else "❌ выключены"
        edits = "✅ включено" if user_track_edits_on(user_id)   else "❌ выключено"
        own   = "✅ включены" if user_own_edit_notify_on(user_id) else "❌ выключены"
        await message.answer(
            f"📊 Твой статус:\n\n"
            f"📡 Мониторинг удалённых: {mon}\n"
            f"✏️ Отслеживание правок: {edits}\n"
            f"🔔 Уведомления о своих правках: {own}\n"
            f"📢 Уведомления от админа: {notif}\n\n{role_line(user_id)}{tariff_status_text(user_id)}",
            reply_markup=main_keyboard(user_id)
        )
        return

    if text == "🎟 Промокод":
        set_setting(f"promo_wait_{user_id}", "1")
        await message.answer("Пришли промокод одним сообщением.\nДля отмены нажми ← Назад",
                             reply_markup=back_only_keyboard())
        return

    if text == "💎 Premium":
        await message.answer(premium_info_text(user_id), reply_markup=tariff_keyboard(user_id))
        return

    if text == "🐛 Сообщить о баге":
        set_setting(f"bug_report_pending_{user_id}", "1")
        await message.answer(
            "🐛 Опиши баг — что происходит, когда происходит, как воспроизвести.\n"
            "Для отмены нажми ← Назад",
            reply_markup=back_only_keyboard()
        )
        return

    if text == "🔧 Админ панель":
        if not has_panel(user_id):
            await message.answer("у тебя нет прав сюда входить, сорян :/",
                                 reply_markup=main_keyboard(user_id))
            return
        await message.answer("🔧 Админ панель:", reply_markup=panel_kb(user_id))
        return

    if text == "📊 Статистика":
        if not has_panel(user_id):
            return
        total_users    = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        mon_on         = db.execute("SELECT COUNT(*) FROM users WHERE monitoring=1").fetchone()[0]
        notif_on       = db.execute("SELECT COUNT(*) FROM users WHERE notifications=1").fetchone()[0]
        edits_on       = db.execute("SELECT COUNT(*) FROM users WHERE track_edits=1").fetchone()[0]
        banned_count   = db.execute("SELECT COUNT(*) FROM users WHERE banned_until IS NOT NULL").fetchone()[0]
        open_bugs      = db.execute("SELECT COUNT(*) FROM bug_reports WHERE status='open'").fetchone()[0]
        cache_count    = db.execute("SELECT COUNT(*) FROM message_cache").fetchone()[0]
        await message.answer(
            stats_business_text() + "\n\n"
            f"⚙️ Прочее\n\n"
            f"👥 Пользователей всего: {total_users}\n"
            f"📡 Мониторинг включён: {mon_on}\n"
            f"🔔 Уведомления включены: {notif_on}\n"
            f"✏️ Отслеживание правок: {edits_on}\n"
            f"🚫 Забанено: {banned_count}\n"
            f"🐛 Открытых заявок: {open_bugs}\n\n"
            f"💾 Сообщений в кэше: {cache_count:,}".replace(",", " "),
            reply_markup=panel_kb(user_id)
        )
        return

    if text == "📋 Заявки":
        if not has_panel(user_id):
            return
        reports = get_open_bug_reports()
        if not reports:
            await message.answer("Нет открытых заявок.", reply_markup=panel_kb(user_id))
            return
        await message.answer(f"🐛 Открытых заявок: {len(reports)}", reply_markup=panel_kb(user_id))
        for rep_id, rep_user_id, rep_username, rep_text, rep_date in reports:
            uname = f"@{rep_username}" if rep_username else f"id{rep_user_id}"
            await bot.send_message(
                user_id,
                f"🐛 Заявка #{rep_id}\n"
                f"От: {uname} (id{rep_user_id})\n"
                f"Когда: {rep_date}\n\n"
                f"{rep_text}",
                reply_markup=bug_report_keyboard(rep_id, rep_user_id, staff_only=not is_owner(user_id))
            )
        return

    if text == "📥 Экспорт":
        if user_id != ADMIN_ID:
            return
        kb = export_keyboard_v2()
        if not kb:
            await message.answer("Кэш пуст — нечего экспортировать.", reply_markup=admin_keyboard())
            return
        await message.answer("Выбери чат для экспорта:", reply_markup=kb)
        return

    if text == "📢 Рассылка":
        if user_id != ADMIN_ID:
            return
        count = db.execute("SELECT COUNT(*) FROM users WHERE notifications=1").fetchone()[0]
        set_setting("awaiting_broadcast", "1")
        await message.answer(
            f"Напиши текст рассылки — он уйдёт {count} пользователям.\n"
            "Для отмены нажми ← Назад"
        )
        return

    if text == "🔴🟢 Техработы":
        if user_id != ADMIN_ID:
            return
        count = db.execute("SELECT COUNT(*) FROM users WHERE notifications=1").fetchone()[0]
        await message.answer(
            f"Выбери сообщение для рассылки {count} пользователям:",
            reply_markup=tech_keyboard()
        )
        return

    if text == "💎 Выдать Premium":
        if not has_panel(user_id):
            return
        set_setting(f"st_state_{user_id}", "grant_target")
        await message.answer("Пришли ID или @ник пользователя.\nДля отмены нажми ← Назад")
        return

    if text == "🎟 Промокоды":
        if not has_panel(user_id):
            return
        t, kb = promo_list_view(user_id)
        await message.answer(t, reply_markup=kb)
        return

    if text == "🧾 Заказы":
        if not is_owner(user_id):
            return
        pend = db.execute(
            "SELECT id, user_id, tariff, price FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        await message.answer(f"{revenue_text()}\n\nОжидают подтверждения: {len(pend)}",
                             reply_markup=panel_kb(user_id))
        for oid, uid_, key, price in pend:
            t = tariff_by_key(key)
            await bot.send_message(
                user_id, f"🧾 Заказ #{oid}\nОт: {user_label(uid_)}\nТариф: {t[3]}\nСумма: {price} ₽",
                reply_markup=order_keyboard(oid))
        return

    if text == "💎 Список Premium":
        if not is_owner(user_id):
            return
        await message.answer(premium_list_text(), reply_markup=panel_kb(user_id))
        return

    if text == "👥 Админы":
        if not is_owner(user_id):
            return
        t, kb = staff_panel()
        await message.answer(t, reply_markup=kb)
        return

    if text == "📜 Журнал":
        if not is_owner(user_id):
            return
        await message.answer(journal_text(), reply_markup=panel_kb(user_id))
        return

    st_state = get_setting(f"st_state_{user_id}")
    if st_state and has_panel(user_id):
        if not text:
            await message.answer("Отправь текстом.")
            return
        if st_state == "grant_target":
            target = resolve_user_id(text)
            if target is None:
                await message.answer("Не нашёл такого. @ник работает только для тех, кто писал боту — "
                                     "попробуй числовой ID.")
                return
            del_setting(f"st_state_{user_id}")
            await message.answer(user_card_text(target) + "\n\nНа сколько дней выдать Premium?",
                                 reply_markup=grant_keyboard(target, user_id))
            return
        if st_state.startswith("grant_custom:") and is_owner(user_id):
            if not text.isdigit() or not 0 < int(text) <= 3650:
                await message.answer("Введи число дней от 1 до 3650.")
                return
            target = int(st_state.split(":")[1])
            del_setting(f"st_state_{user_id}")
            until = await do_grant(user_id, target, int(text))
            await message.answer(f"✅ Premium выдан: {user_label(target)} до {fmt_ts(until)}",
                                 reply_markup=panel_kb(user_id))
            return
        if st_state.startswith("pc_"):
            import re as _re
            parts = st_state.split(":")
            step, kind = parts[0], parts[1]
            is_o = is_owner(user_id)
            skey = f"st_state_{user_id}"
            if step == "pc_value":
                lim = promo_value_limit(kind, is_o)
                if not text.isdigit() or not 0 < int(text) <= lim:
                    await message.answer(f"Введи число от 1 до {lim}.")
                    return
                set_setting(skey, f"pc_uses:{kind}:{int(text)}")
                await message.answer("Сколько раз можно использовать код? " + (
                    "(1–100000, 0 = без ограничений)" if is_o else f"(1–{STAFF_PROMO_MAX_USES})"))
                return
            if step == "pc_uses":
                lo, hi = (0, 100000) if is_o else (1, STAFF_PROMO_MAX_USES)
                if not text.isdigit() or not lo <= int(text) <= hi:
                    await message.answer(f"Введи число от {lo} до {hi}.")
                    return
                set_setting(skey, f"pc_exp:{kind}:{parts[2]}:{int(text)}")
                await message.answer("Сколько дней действует код? " + (
                    "(1–365, 0 = бессрочно)" if is_o else f"(1–{STAFF_PROMO_MAX_EXPIRY_DAYS})"))
                return
            if step == "pc_exp":
                lo, hi = (0, 365) if is_o else (1, STAFF_PROMO_MAX_EXPIRY_DAYS)
                if not text.isdigit() or not lo <= int(text) <= hi:
                    await message.answer(f"Введи число от {lo} до {hi}.")
                    return
                set_setting(skey, f"pc_name:{kind}:{parts[2]}:{parts[3]}:{int(text)}")
                await message.answer("Название кода: латиница и цифры, 3–24 знака (например WELCOME).\n"
                                     "Или напиши «авто» — придумаю сам.\n"
                                     "Или «пачка 10» — создам 10 одноразовых кодов (по одной активации).")
                return
            if step == "pc_name":
                _mb = _re.fullmatch(r"(?:пачка|batch)\s+(\d{1,3})", text.strip().lower())
                if _mb:
                    cnt = int(_mb.group(1))
                    cap = 100 if is_o else 20
                    if not 1 <= cnt <= cap:
                        await message.answer(f"Пачка — от 1 до {cap} кодов.")
                        return
                    value, exp = int(parts[2]), int(parts[4])
                    del_setting(skey)
                    made = []
                    for _i in range(cnt):
                        _c = gen_promo_code()
                        create_promo(_c, kind, value, 1, exp, user_id)
                        pc_apply_audience(_c, user_id)
                        made.append(_c)
                    log_action(user_id, "promo_new", None, f"пачка {cnt} × {promo_kind_text(kind, value)}")
                    await message.answer(
                        f"✅ Создано одноразовых кодов: {cnt}\n{promo_kind_text(kind, value)} · "
                        f"{'бессрочно' if not exp else f'действуют {exp} дн'}\n\n" + "\n".join(made),
                        reply_markup=panel_kb(user_id))
                    return
                if text.lower() in ("авто", "auto"):
                    code = gen_promo_code()
                else:
                    code = text.strip().upper()
                    if not _re.fullmatch(r"[A-Z0-9_-]{3,24}", code):
                        await message.answer("Только латиница, цифры, _ и -, от 3 до 24 знаков.")
                        return
                    if db.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone():
                        await message.answer("Такой код уже есть, придумай другой.")
                        return
                value, uses, exp = int(parts[2]), int(parts[3]), int(parts[4])
                del_setting(skey)
                create_promo(code, kind, value, uses, exp, user_id)
                pc_apply_audience(code, user_id)
                log_action(user_id, "promo_new", None, f"{code} {promo_kind_text(kind, value)}")
                lim = str(uses) if uses else "без ограничений"
                await message.answer(
                    f"✅ Промокод создан: {code}\n{promo_kind_text(kind, value)} · использований: {lim} · "
                    f"{'бессрочно' if not exp else f'действует {exp} дн'}\n\n"
                    f"Как активировать: в боте нажать «🎟 Промокод» и отправить код.",
                    reply_markup=panel_kb(user_id))
                return
        if st_state == "staff_add" and is_owner(user_id):
            target = resolve_user_id(text)
            if target is None:
                await message.answer("Не нашёл такого. Пусть он сначала нажмёт /start у бота, "
                                     "либо пришли числовой ID.")
                return
            if target == ADMIN_ID:
                await message.answer("Ты и так владелец.")
                return
            del_setting(f"st_state_{user_id}")
            ensure_user(target)
            await message.answer(
                user_card_text(target) + "\n\nНазначить админом?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Назначить", callback_data=f"st:ok:{target}"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="st:no")]]))
            return

    if get_setting(f"promo_wait_{user_id}") == "1":
        del_setting(f"promo_wait_{user_id}")
        if not text:
            await message.answer("Отправь промокод текстом.", reply_markup=main_keyboard(user_id))
            return
        if not promo_rate_ok(user_id):
            await message.answer("Слишком много неверных попыток. Попробуй через час.",
                                 reply_markup=main_keyboard(user_id))
            return
        okp, ptext, pkind = redeem_promo(user_id, text)
        if not okp:
            promo_fail(user_id)
        if okp and pkind in ("discount", "rub"):
            await message.answer(ptext, reply_markup=tariff_keyboard(user_id))
        else:
            await message.answer(ptext, reply_markup=main_keyboard(user_id))
        return

    # ---------- Ожидание ввода bug report (для всех) ----------

    if get_setting(f"bug_report_pending_{user_id}") == "1":
        if not text:
            await message.answer("Опиши баг текстом.")
            return
        del_setting(f"bug_report_pending_{user_id}")
        username = message.from_user.username or ""
        add_bug_report(user_id, username, text)
        await message.answer("✅ Спасибо! Баг отправлен разработчику.", reply_markup=main_keyboard(user_id))
        uname_str = f"@{username}" if username else f"id{user_id}"
        try:
            last_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            await bot.send_message(
                ADMIN_ID,
                f"🐛 Новая заявка #{last_id} от {uname_str}:\n\n{text}",
                reply_markup=bug_report_keyboard(last_id, user_id)
            )
        except Exception:
            logging.exception("Ошибка отправки bug report админу")
        return

    # ---------- Только для админа ----------

    if user_id != ADMIN_ID:
        return

    if get_setting("awaiting_broadcast") == "1":
        if not text:
            await message.answer("Отправь текстовое сообщение.")
            return
        del_setting("awaiting_broadcast")
        await message.answer("Отправляю рассылку...")
        sent_count, failed = await broadcast_to_users(text)
        await message.answer(
            f"✅ Рассылка завершена.\nОтправлено: {sent_count}\nНе доставлено: {failed}",
            reply_markup=admin_keyboard()
        )
        return



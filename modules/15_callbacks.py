# ---------- Колбэки ----------

@dp.callback_query()
async def on_callback(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return

    data = callback.data
    user_id = callback.from_user.id

    # Broadcast (техработы) — только для админа
    if data.startswith("broadcast:"):
        if user_id != ADMIN_ID:
            await callback.answer("Нет доступа.")
            return
        action = data[10:]
        if action == "tech_start":
            text = "🔴 НАЧАЛИСЬ ТЕХНИЧЕСКИЕ РАБОТЫ! Бот временно недоступен."
        elif action == "tech_end":
            text = "🟢 ТЕХНИЧЕСКИЕ РАБОТЫ ЗАВЕРШЕНЫ! Бот снова работает в штатном режиме."
        else:
            await callback.answer("Неизвестное действие.")
            return
        await callback.message.edit_text("Отправляю рассылку...")
        sent_count, failed = await broadcast_to_users(text)
        await callback.message.edit_text(
            f"✅ Рассылка завершена.\nОтправлено: {sent_count}\nНе доставлено: {failed}"
        )
        await callback.answer()
        return

    if data.startswith("xr:"):
        arg = data[3:]
        del_setting(f"exp_wait_{user_id}")
        if arg == "cancel":
            await callback.message.edit_text("Экспорт отменён.")
        elif arg.lstrip("-").isdigit():
            await callback.message.edit_text(await create_export_request(user_id, int(arg)))
        await callback.answer()
        return

    if data.startswith("xo:"):
        await export_owner_action(callback, user_id, data)
        return

    if data.startswith("tt:"):
        if not is_owner(user_id):
            await callback.answer("Только владелец.")
            return
        parts = data.split(":")
        if parts[1] == "add":
            set_setting(f"tt_wait_{user_id}", "add")
            await callback.message.answer("Пришли ID или @ник будущего тестера.\nОтмена — «← Назад».")
        elif parts[1] == "bc":
            set_setting(f"tt_wait_{user_id}", "bc")
            await callback.message.answer("Пришли текст сообщения для всех тестеров.\nОтмена — «← Назад».")
        elif parts[1] == "del" and len(parts) > 2 and parts[2].lstrip("-").isdigit():
            tester_del(int(parts[2]))
            log_action(user_id, "tester_del", int(parts[2]), "")
            t, kb = tester_view()
            await callback.message.edit_text(t, reply_markup=kb)
        await callback.answer()
        return

    if data.startswith("faq:"):
        arg = data[4:]
        if arg == "list":
            t, kb = faq_user_view()
            await callback.message.edit_text(t, reply_markup=kb)
        elif arg.isdigit():
            r = db.execute("SELECT question, answer FROM faq WHERE id=?", (int(arg),)).fetchone()
            if not r:
                await callback.answer("Вопрос удалён.")
                return
            await callback.message.edit_text(
                f"❓ {r[0]}\n\n{r[1]}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="← К вопросам", callback_data="faq:list")]]))
        await callback.answer()
        return

    if data.startswith("fq:"):
        if not is_owner(user_id):
            await callback.answer("Только владелец.")
            return
        await faq_editor_callback(callback, user_id, data)
        return

    if data.startswith("bn:"):
        await bonus_callback(callback, user_id, data)
        return

    if data.startswith("rs:"):
        if user_id != ADMIN_ID:
            await callback.answer("Нет доступа.")
            return
        parts = data.split(":")
        if parts[1] == "forget":
            try:
                await callback.message.delete()
            except Exception:
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Забыто")
            return
        if parts[1] == "sorry":
            mins = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await callback.message.edit_text("Отправляю извинения...")
            text = ("🙏 Извините за технические неполадки: бот был недоступен"
                    + (f" около {fmt_dur(mins * 60)}" if mins >= 2 else " некоторое время")
                    + ". Сейчас всё работает штатно. Спасибо за терпение!")
            sent_count, failed = await broadcast_to_users(text)
            log_action(user_id, "sorry_broadcast", None, f"{sent_count} ок / {failed} ошибок")
            await callback.message.edit_text(
                f"✅ Извинения отправлены.\nОтправлено: {sent_count}\nНе доставлено: {failed}")
            await callback.answer()
            return
        await callback.answer()
        return

    if data.startswith("tk:"):
        await on_ticket_callback(callback)
        return

    if data.startswith("rt:"):
        await on_rt_callback(callback)
        return

    if data.startswith("ac:"):
        await on_ach_callback(callback)
        return

    # Bug report actions — только для админа
    if data.startswith("bug:"):
        if user_id != ADMIN_ID and not (is_staff(user_id) and data.startswith("bug:close:")):
            await callback.answer("Нет доступа.")
            return
        parts = data.split(":")
        action = parts[1]
        report_id = int(parts[2])
        rep_user_id = int(parts[3])

        if action == "close":
            close_bug_report(report_id)
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("✅ Заявка закрыта.")

        elif action.startswith("ban"):
            if action == "banperm":
                ban_user(rep_user_id, None)
                ban_text = "навсегда"
            else:
                hours = int(action[3:])
                ban_user(rep_user_id, hours)
                if hours < 24:
                    ban_text = f"на {hours}ч"
                else:
                    ban_text = f"на {hours // 24}д"
            close_bug_report(report_id)
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer(f"🚫 Забанен {ban_text}.")
            try:
                await bot.send_message(rep_user_id, f"⛔ Ты заблокирован {ban_text} за нарушение правил.")
            except Exception:
                pass

        elif action == "unban":
            unban_user(rep_user_id)
            await callback.answer("🔓 Пользователь разбанен.")

        return

    # Ввод промокода из экрана Premium
    if data == "promo:enter":
        set_setting(f"promo_wait_{user_id}", "1")
        await callback.message.answer("Пришли промокод одним сообщением.\nДля отмены нажми ← Назад")
        await callback.answer()
        return

    # Управление промокодами (владелец и админы)
    if data.startswith("pc:"):
        if not has_panel(user_id):
            await callback.answer("Нет доступа.")
            return
        parts = data.split(":")
        act = parts[1]
        if act == "list":
            t, kb = promo_list_view(user_id)
            await callback.message.edit_text(t, reply_markup=kb)
            await callback.answer()
            return
        if act == "new":
            await callback.message.edit_text(
                "Какой промокод создать?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎁 Дни Premium", callback_data="pc:kind:days")],
                    [InlineKeyboardButton(text="💸 Скидка в %", callback_data="pc:kind:disc")],
                    [InlineKeyboardButton(text="💰 Скидка в ₽", callback_data="pc:kind:rub")],
                    *([[InlineKeyboardButton(text="💵 Бонус на баланс", callback_data="pc:kind:bonus")]]
                      if is_owner(user_id) else []),
                    [InlineKeyboardButton(text="← К списку", callback_data="pc:list")]]))
            await callback.answer()
            return
        if act == "kind":
            kind = {"days": "days", "disc": "discount", "rub": "rub", "bonus": "bonus"}.get(parts[2])
            if not kind or (kind == "bonus" and not is_owner(user_id)):
                await callback.answer("Нет доступа.")
                return
            rows_a = [[InlineKeyboardButton(text=label, callback_data=f"pc:aud:{kind}:{a}")]
                      for a, label in PROMO_AUDIENCES.items()]
            await callback.message.answer("Кому доступен промокод?",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_a))
            await callback.answer()
            return
        if act == "aud":
            kind, aud = parts[2], parts[3]
            if kind not in ("days", "discount", "rub", "bonus") or aud not in PROMO_AUDIENCES \
                    or (kind == "bonus" and not is_owner(user_id)):
                await callback.answer("Нет доступа.")
                return
            lim = promo_value_limit(kind, is_owner(user_id))
            set_setting(f"pc_aud_{user_id}", aud)
            set_setting(f"st_state_{user_id}", f"pc_value:{kind}")
            q = {"days": f"Сколько дней Premium давать? (1–{lim})",
                 "discount": f"Сколько процентов скидки? (1–{lim})",
                 "rub": f"На сколько рублей скидка? (1–{lim}). Применяется к одному заказу.",
                 "bonus": f"Сколько рублей зачислять на баланс? (1–{lim}). Потратить на Premium можно, "
                          f"вывести нельзя."}[kind]
            await callback.message.answer(q + "\nДля отмены нажми ← Назад")
            await callback.answer()
            return
        if act == "on":
            t, kb = promo_detail_view(user_id, parts[2])
            if not t:
                await callback.answer("Нет доступа.")
                return
            db.execute("UPDATE promo_codes SET active=1 WHERE code=?", (parts[2],))
            db.commit()
            log_action(user_id, "promo_on", None, parts[2])
            t, kb = promo_detail_view(user_id, parts[2])
            await callback.message.edit_text(t, reply_markup=kb)
            await callback.answer("Включён")
            return
        if act == "stats":
            await callback.message.answer(promo_stats_text())
            await callback.answer()
            return
        if act == "show":
            t, kb = promo_detail_view(user_id, parts[2])
            if not t:
                await callback.answer("Не найден.")
                return
            await callback.message.edit_text(t, reply_markup=kb)
            await callback.answer()
            return
        if act == "off":
            t, kb = promo_detail_view(user_id, parts[2])
            if not t:
                await callback.answer("Нет доступа.")
                return
            db.execute("UPDATE promo_codes SET active=0 WHERE code=?", (parts[2],))
            db.commit()
            log_action(user_id, "promo_off", None, parts[2])
            t, kb = promo_detail_view(user_id, parts[2])
            await callback.message.edit_text(t, reply_markup=kb)
            await callback.answer("Отключён")
            return
        await callback.answer()
        return

    # Оплата звёздами
    if data.startswith("pay:star:"):
        key = data.split(":")[2]
        t = tariff_by_key(key)
        if not t:
            await callback.answer("Тариф не найден.")
            return
        if key == "trial" and not trial_available(user_id):
            await callback.answer("Пробный тариф можно взять один раз.", show_alert=True)
            return
        if await offer_gate(callback, user_id, key):
            return
        oid, stars, pct = create_star_order(user_id, key)
        try:
            await bot.send_invoice(
                chat_id=user_id,
                title=f"Premium · {t[3]}",
                description=(f"Полная история удалённых и одноразовые без лимита на {t[1]} дн. "
                             f"Заказ #{oid}. Условия: /terms"),
                payload=f"ord:{oid}",
                currency="XTR",
                prices=[LabeledPrice(label=t[3], amount=stars)],
                provider_token="",
            )
        except Exception:
            logging.exception("Не удалось выставить счёт в звёздах")
            await callback.message.answer("Не удалось создать счёт. Попробуй позже или оплати картой.")
        await callback.answer()
        return

    if data.startswith("pay:bal:"):
        if await offer_gate(callback, user_id, data.split(":")[2]):
            return
        await pay_from_balance(callback, user_id, data.split(":")[2])
        return

    if data == "wd:req":
        await start_withdraw(callback, user_id)
        return

    if data.startswith("wdo:"):
        await owner_withdraw_action(callback, user_id, data)
        return

    if data.startswith("offer:"):
        parts = data.split(":")
        act = parts[1] if len(parts) > 1 else ""
        if act == "show":
            chunks = offer_chunks()
            for i, part in enumerate(chunks):
                last = i == len(chunks) - 1
                await callback.message.answer(part, reply_markup=offer_keyboard(user_id) if last else None)
            await callback.answer()
            return
        if act in ("ok", "okv"):
            first = not offer_ok(user_id)
            offer_set_ok(user_id)
            if first:
                log_action(user_id, "offer_ok", None, "")
            key = parts[2] if len(parts) > 2 else None
            if key and key != "menu" and tariff_by_key(key):
                await callback.message.answer(method_text(user_id, key),
                                              reply_markup=method_keyboard(user_id, key))
            else:
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=offer_keyboard(user_id) if act == "okv" else tariff_keyboard(user_id))
                except Exception:
                    pass
            await callback.answer("Согласие сохранено ✅")
            return
        await callback.answer("Вы уже согласились с офертой ✅")
        return

    # Покупка Premium (любой пользователь)
    if data.startswith("buy:") or data.startswith("pay:card:"):
        key = data.split(":")[-1]
        via_card = data.startswith("pay:card:")
        if key == "menu":
            await callback.message.answer(premium_info_text(user_id), reply_markup=tariff_keyboard(user_id))
            await callback.answer()
            return
        t = tariff_by_key(key)
        if not t:
            await callback.answer("Тариф не найден.")
            return
        if key == "trial" and not trial_available(user_id):
            await callback.answer("Пробный тариф можно взять один раз.", show_alert=True)
            return
        if await offer_gate(callback, user_id, key):
            return
        if not via_card:
            await callback.message.answer(method_text(user_id, key), reply_markup=method_keyboard(user_id, key))
            await callback.answer()
            return
        oid, price, is_new = create_order(user_id, key)
        row = order_row(oid)
        await callback.message.answer(order_text(oid, key, price, row[5]),
                                      reply_markup=pay_keyboard(oid, user_id, key, price))
        if is_new:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🧾 Новый заказ #{oid}\nОт: {user_label(user_id)}\nТариф: {t[3]}\n"
                    f"Сумма: {price} ₽" + (f" (скидка {row[5]}%)" if row[5] else "")
                    + "\n\nЖди его сообщение в личке и сверь оплату.",
                    reply_markup=order_keyboard(oid))
            except Exception:
                logging.exception("Не удалось уведомить владельца о заказе")
        await callback.answer()
        return

    # Подтверждение заказов — только владелец
    if data.startswith("ord:"):
        if not is_owner(user_id):
            await callback.answer("Только владелец.")
            return
        _, act, oid_s = data.split(":")
        row = order_row(int(oid_s))
        if not row:
            await callback.answer("Заказ не найден.")
            return
        oid, buyer, key, days, price, pct, status = row
        if status != "pending":
            await callback.answer("Уже обработан.")
            return
        if act == "ok":
            db.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (now_ts(), oid))
            if key == "trial":
                db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (buyer,))
            if pct:
                db.execute("UPDATE users SET discount_pct=0, discount_until=0, discount_code=NULL "
                           "WHERE user_id=?", (buyer,))
            db.commit()
            log_action(user_id, "order_paid", buyer, f"#{oid} {days} дн {price} ₽")
            await credit_commission(oid)
            until = await do_grant(user_id, buyer, days)
            await callback.message.edit_text(
                f"✅ Заказ #{oid} оплачен: {user_label(buyer)}, +{days} дн (до {fmt_ts(until)}), {price} ₽")
            await callback.answer("Выдано")
        else:
            db.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
            db.commit()
            log_action(user_id, "order_no", buyer, f"#{oid}")
            await callback.message.edit_text(f"❌ Заказ #{oid} отклонён ({user_label(buyer)}).")
            try:
                await bot.send_message(
                    buyer, f"Заказ #{oid} не подтверждён. Если это ошибка — напиши {OWNER_CONTACT}.")
            except Exception:
                pass
            await callback.answer("Отклонён")
        return

    # Выдача Premium кнопками (владелец и админы)
    if data.startswith("pg:"):
        if not has_panel(user_id):
            await callback.answer("Нет доступа.")
            return
        _, tgt, val = data.split(":")
        if tgt == "close":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            return
        target = int(tgt)
        if val == "custom":
            if not is_owner(user_id):
                await callback.answer("Только владелец.")
                return
            set_setting(f"st_state_{user_id}", f"grant_custom:{target}")
            await callback.message.answer("Введи число дней (1–3650).")
            await callback.answer()
            return
        days = int(val)
        if days <= 0 or (days > STAFF_MAX_GRANT_DAYS and not is_owner(user_id)):
            await callback.answer(f"Админам — не больше {STAFF_MAX_GRANT_DAYS} дней за раз.")
            return
        until = await do_grant(user_id, target, days)
        await callback.message.edit_text(
            f"✅ Premium выдан: {user_label(target)}\n+{days} дн, до {fmt_ts(until)}")
        await callback.answer("Готово")
        return

    if data.startswith("pr:"):
        if not is_owner(user_id):
            await callback.answer("Только владелец.")
            return
        target = int(data[3:])
        revoke_premium(target)
        log_action(user_id, "revoke", target)
        await callback.message.edit_text(f"✅ Premium у {user_label(target)} отключён.")
        await callback.answer("Готово")
        return

    if data.startswith("st:"):
        if not is_owner(user_id):
            await callback.answer("Только владелец.")
            return
        parts = data.split(":")
        act = parts[1]
        if act == "add":
            set_setting(f"st_state_{user_id}", "staff_add")
            await callback.message.answer("Пришли ID или @ник будущего админа.\nДля отмены нажми ← Назад")
            await callback.answer()
            return
        if act == "no":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Отменено")
            return
        target = int(parts[2])
        if act == "ok":
            staff_add(target, user_id)
            log_action(user_id, "staff_add", target)
            await callback.message.edit_text(f"✅ {user_label(target)} теперь админ.")
            try:
                await bot.send_message(
                    target, "👮 Тебя назначили админом бота. Открой «🔧 Админ панель» в меню.",
                    reply_markup=main_keyboard(target))
            except Exception:
                pass
            await callback.answer("Готово")
            return
        if act == "del":
            staff_del(target)
            log_action(user_id, "staff_del", target)
            t, kb = staff_panel()
            await callback.message.edit_text(t, reply_markup=kb)
            try:
                await bot.send_message(target, "Ты больше не админ бота.",
                                       reply_markup=main_keyboard(target))
            except Exception:
                pass
            await callback.answer("Снят")
            return
        await callback.answer()
        return

    # Export — только для админа
    if data.startswith("exp:"):
        if user_id != ADMIN_ID:
            await callback.answer("Нет доступа.")
            return
        chat_key = data[4:]
        if chat_key == "cancel":
            await callback.message.edit_text("Экспорт отменён.")
            await callback.answer()
            return
        if ":" not in chat_key:
            await callback.message.edit_text("Кнопка устарела — открой «📥 Экспорт» заново.")
            await callback.answer()
            return
        chat_id, _own = (int(x) for x in chat_key.split(":"))
        await callback.message.edit_text("Генерирую файл экспорта...")
        try:
            file_bytes, _info = build_export(_own, chat_id)
            name = safe_name(_info["partner"])
            filename = f"export_{name}_{datetime.now(TZ).strftime('%d%m%y')}.txt"
            await bot.send_document(
                ADMIN_ID,
                BufferedInputFile(file_bytes, filename=filename),
                caption=f"📥 Экспорт чата: {name}"
            )
            await callback.message.edit_text("✅ Файл отправлен.")
        except Exception as e:
            logging.exception("Ошибка экспорта")
            await callback.message.edit_text(f"❌ Ошибка экспорта: {e}")
        await callback.answer()
        return

    # Остальные колбэки — только для админа
    if user_id != ADMIN_ID:
        await callback.answer("Нет доступа.")
        return


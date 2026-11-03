# ---------- Команды ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not message.from_user:
        return
    is_new = ensure_user(message.from_user.id, message.from_user.username or "")
    if is_new:
        _parts = (message.text or "").split(maxsplit=1)
        if len(_parts) == 2:
            try:
                await try_referral(message.from_user.id, _parts[1].strip())
            except Exception:
                logging.exception("Ошибка реферала")
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 привет, админ", reply_markup=main_keyboard(ADMIN_ID))
        return
    if is_new:
        try:
            username = fmt_user(message.from_user)
            await bot.send_message(
                ADMIN_ID,
                f"🆕 Новый пользователь подключился!\n"
                f"Кто: {username}\n"
                f"ID: {message.from_user.id}"
            )
        except Exception:
            logging.exception("Не удалось уведомить админа о новом пользователе")
    await message.answer(
        "Привет! Этот бот присылает тебе удалённые и изменённые сообщения из твоих "
        "личных чатов, а также спасает одноразовые фото и видео.\n\n"
        "Как подключить:\n"
        "1. Настройки → Telegram Business → Чат-боты\n"
        "2. Выбери этого бота и включи\n\n"
        "Если такого пункта нет или Telegram просит купить Premium — подключи вручную:\n"
        "Настройки → «Изм.» (справа сверху) → «Автоматизация чатов» → "
        "впиши @your_bot\n\n"
        "Дальше всё автоматически: кто-то удалил или изменил сообщение — бот пришлёт тебе "
        "уведомление.\n\n"
        "Что умеет бот:\n"
        "🗑 присылает удалённые и изменённые сообщения\n"
        "🔥 спасает одноразовые фото и видео\n"
        "🌐 переводит твои сообщения прямо в чате: напиши +перевод& текст (русский → английский) "
        "или +&перевод текст (английский → русский)\n"
        "🛡 предупреждает о мошеннических и фишинговых ссылках во входящих сообщениях\n"
        "🎙 расшифровывает голосовые и кружки: ответь на запись командой +распознать&\n"
        "🔍 проверяет файлы на вирусы (apk, exe, архивы, pdf, документы): ответь на файл командой +проверка&\n"
        "📝 превращает перечисления в аккуратный список: ответь на сообщение командой +список&\n\n"
        f"Лимиты в день: без Premium — одноразовых {FREE_ONETIME_PER_DAY}, переводов {FREE_TRANSLATE_PER_DAY}, "
        f"проверок ссылок {FREE_LINKS_PER_DAY} (только известные угрозы), расшифровок {FREE_STT_PER_DAY}, "
        f"проверок файлов {FREE_SCAN_PER_DAY} (файлы до {FREE_SCAN_MAX_MB} МБ), списков {FREE_LIST_PER_DAY}. "
        f"В 💎 Premium — одноразовые и расшифровки без лимита, переводов {PREMIUM_TRANSLATE_PER_DAY}, "
        f"проверок ссылок {PREMIUM_LINKS_PER_DAY} с расширенным анализом, проверка файлов без лимита (до {PREMIUM_SCAN_MAX_MB} МБ), списки без лимита.\n\n"
        "💎 Тарифы и лимиты — кнопка «💎 Premium». Создатель @your_contact",
        reply_markup=main_keyboard(message.from_user.id)
    )
    if is_new and NEW_USER_PREMIUM_DAYS > 0:
        await message.answer(
            f"🎁 Тебе в подарок {NEW_USER_PREMIUM_DAYS} дней Premium — до "
            f"{fmt_ts(get_premium_until(message.from_user.id))}: полная история удалённых, "
            f"одноразовые фото и видео без лимита, переводы ({PREMIUM_TRANSLATE_PER_DAY} в день), "
            f"защита от фишинга ({PREMIUM_LINKS_PER_DAY} проверок в день), расшифровка голосовых без лимита, "
            f"проверка файлов без лимита (до {PREMIUM_SCAN_MAX_MB} МБ), списки из сообщений без лимита, экспорт переписки.")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
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
        f"💾 Сообщений в кэше: {cache_count:,}".replace(",", " ")
    )

@dp.message(Command("upd"))
async def cmd_upd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    count = db.execute("SELECT COUNT(*) FROM users WHERE notifications=1").fetchone()[0]
    set_setting("awaiting_broadcast", "1")
    await message.answer(
        f"Напиши текст рассылки — он уйдёт {count} пользователям с включёнными уведомлениями.\n"
        "Для отмены напиши /cancel"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    del_setting("awaiting_broadcast")
    await message.answer("Отменено.", reply_markup=main_keyboard(ADMIN_ID))

@dp.message(Command("r"))
async def cmd_r(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    count = db.execute("SELECT COUNT(*) FROM users WHERE notifications=1").fetchone()[0]
    await message.answer(
        f"Выбери сообщение для рассылки {count} пользователям:",
        reply_markup=tech_keyboard()
    )

@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    """Telegram спрашивает, можно ли принять платёж (ответить надо за 10 секунд)."""
    try:
        payload = q.invoice_payload or ""
        row = None
        if payload.startswith("ord:") and payload[4:].isdigit():
            row = db.execute(
                "SELECT id, user_id, tariff, price, status, currency FROM orders WHERE id=?",
                (int(payload[4:]),)).fetchone()
        good = (row is not None and row[1] == q.from_user.id and row[4] == "star_pending"
                and q.currency == "XTR" and q.total_amount == row[3]
                and not (row[2] == "trial" and not trial_available(row[1])))
        if good:
            await q.answer(ok=True)
        else:
            await q.answer(ok=False, error_message="Заказ недействителен. Оформи покупку заново в меню «💎 Premium».")
    except Exception:
        logging.exception("Ошибка pre_checkout")
        try:
            await q.answer(ok=False, error_message="Временная ошибка. Попробуй ещё раз.")
        except Exception:
            pass

@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    uid = message.from_user.id
    charge = sp.telegram_payment_charge_id
    payload = sp.invoice_payload or ""
    row = None
    if payload.startswith("ord:") and payload[4:].isdigit():
        row = db.execute("SELECT id, user_id, tariff, days, price, discount_pct, status FROM orders WHERE id=?",
                         (int(payload[4:]),)).fetchone()
    if row and row[6] == "paid" and db.execute(
            "SELECT 1 FROM orders WHERE id=? AND charge_id=?", (row[0], charge)).fetchone():
        return   # повторное уведомление о том же платеже
    if not row or row[1] != uid or row[6] != "star_pending":
        # платёж есть, а подходящего заказа нет — записываем, чтобы можно было вернуть командой /refund
        cur = db.execute(
            "INSERT INTO orders (user_id, tariff, days, price, discount_pct, status, created_at, paid_at, "
            "currency, stars, charge_id) VALUES (?, 'unknown', 0, ?, 0, 'paid', ?, ?, 'XTR', ?, ?)",
            (uid, sp.total_amount, now_ts(), now_ts(), sp.total_amount, charge))
        db.commit()
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ Платёж звёздами без подходящего заказа: {user_label(uid)}, "
                                             f"{sp.total_amount} ⭐. Записан как заказ #{cur.lastrowid}. "
                                             f"Вернуть: /refund {cur.lastrowid}")
        except Exception:
            pass
        await message.answer("Платёж получен, но заказ не найден. Напиши в /paysupport — разберёмся.")
        return
    oid, buyer, key, days, price, pct, _ = row
    db.execute("UPDATE orders SET status='paid', paid_at=?, charge_id=? WHERE id=?", (now_ts(), charge, oid))
    if key == "trial":
        db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (buyer,))
    if pct:
        db.execute("UPDATE users SET discount_pct=0, discount_until=0, discount_code=NULL WHERE user_id=?", (buyer,))
    db.commit()
    log_action(buyer, "stars_paid", buyer, f"#{oid} {days} дн {price} ⭐")
    await credit_commission(oid)
    until = await do_grant(ADMIN_ID, buyer, days)
    await message.answer(f"✅ Оплата получена. Premium активен до {fmt_ts(until)}. Заказ #{oid}.",
                         reply_markup=main_keyboard(buyer))
    try:
        t = tariff_by_key(key)
        await bot.send_message(ADMIN_ID, f"⭐ Оплата звёздами: заказ #{oid}, {user_label(buyer)}, "
                                         f"{t[3] if t else key}, {price} ⭐ (≈ {price * STAR_USD:.2f} USD).")
    except Exception:
        pass

@dp.message(Command("refund"))
async def cmd_refund(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /refund НОМЕР_ЗАКАЗА (только для оплат звёздами)")
        return
    row = db.execute("SELECT id, user_id, days, status, COALESCE(currency,'RUB'), charge_id FROM orders WHERE id=?",
                     (int(parts[1]),)).fetchone()
    if not row:
        await message.answer("Такого заказа нет.")
        return
    oid, buyer, days, status, cur_, charge = row
    if cur_ != "XTR":
        await message.answer("Этот заказ оплачен картой — деньги возвращай вручную, а Premium забери через /revoke.")
        return
    if status != "paid" or not charge:
        await message.answer(f"Заказ #{oid} не в статусе «оплачен» (сейчас: {status}).")
        return
    if await refund_stars(buyer, charge):
        db.execute("UPDATE orders SET status='refunded' WHERE id=?", (oid,))
        db.commit()
        if days:
            shorten_premium(buyer, days)
        log_action(ADMIN_ID, "refund", buyer, f"#{oid}")
        clawback_order(oid)
        await message.answer(f"✅ Звёзды по заказу #{oid} возвращены, у {user_label(buyer)} снято {days} дн Premium.")
        try:
            await bot.send_message(buyer, f"Возврат по заказу #{oid} выполнен: звёзды вернулись на твой счёт.")
        except Exception:
            pass
    else:
        await message.answer("❌ Не получилось вернуть звёзды (подробности в логе: journalctl -u salesbot).")

# ---------- Личный кабинет, рефералка, вывод ----------

BOT_USERNAME = "your_bot"  # username бота без @
REF_REWARD_RUB = 50            # за каждого нового приглашённого
REF_COMMISSION_PCT = 2.5       # % с оплат приглашённых
REF_HOLD_DAYS = 7              # сколько дней деньги «зреют», прежде чем их можно вывести/потратить
REF_MIN_WITHDRAW_RUB = 1000    # минимум для вывода
REF_DAILY_LIMIT = 20           # максимум наград за заход в сутки на одного пригласившего
PAYOUT_DAY = 10                # выплаты — по этим числам месяца
FREE_PROXY_URL = ""            # или командой владельца: /setproxy ссылка

CAB_TEXTS = {"📋 Мой статус", "🔗 Реф. ссылка", "💸 Вывод", "➕ Пополнение", "🌐 Бесплатный прокси",
             "🎁 Бонус"}
MENU_TEXTS = CAB_TEXTS | {"← Назад", "👤 Личный кабинет", "📡 Мониторинг", "🔔 Уведомления", "ℹ️ Мой статус",
                          "🔧 Админ панель", "🐛 Сообщить о баге", "💎 Premium", "🎟 Промокод"}

PANEL_TEXTS = {"📢 Рассылка", "🔴🟢 Техработы", "📊 Статистика", "📋 Заявки", "💎 Выдать Premium",
               "💎 Список Premium", "👥 Админы", "📜 Журнал", "🧾 Заказы", "🎟 Промокоды", "📥 Экспорт",
               "🩺 Состояние", "❓ Редактор FAQ", "🔎 Кабинет юзера", "🧪 Тестеры"}
MENU_TEXTS = MENU_TEXTS | PANEL_TEXTS | {"❓ FAQ", "💬 Обратная связь", "🛠 Обращение в поддержку",
                                          "💡 Предложить функцию", "📨 Мои обращения", "💬 Обращения", "💡 Идеи",
                                          "🏆 Достижения", "🏆 Редактор достижений", "🔁 Удержание"}

def back_only_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="← Назад")]], resize_keyboard=True)

def cabinet_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Мой статус"), KeyboardButton(text="🔗 Реф. ссылка")],
        [KeyboardButton(text="💸 Вывод"), KeyboardButton(text="➕ Пополнение")],
        [KeyboardButton(text="🌐 Бесплатный прокси"), KeyboardButton(text="🎁 Бонус")],
        [KeyboardButton(text="🏆 Достижения")],
        [KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def fmt_rub(kop) -> str:
    v = kop / 100
    return f"{int(v)} ₽" if abs(v - round(v)) < 0.005 else f"{v:.2f} ₽"

def ref_link(uid) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

def ref_banned(uid) -> bool:
    return get_setting(f"ref_ban_{uid}") == "1"

def ledger_add(uid, kind, kop, src=None, order_id=None, hold=False, note=""):
    t = now_ts()
    db.execute(
        "INSERT INTO ref_ledger (user_id, kind, amount_kop, src_user, order_id, created_at, avail_at, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uid, kind, kop, src, order_id, t, t + REF_HOLD_DAYS * 86400 if hold else 0, note))
    db.commit()

def ref_balance(uid):
    """(всего, доступно сейчас) в копейках."""
    total = db.execute("SELECT COALESCE(SUM(amount_kop),0) FROM ref_ledger WHERE user_id=?", (uid,)).fetchone()[0]
    avail = db.execute("SELECT COALESCE(SUM(amount_kop),0) FROM ref_ledger WHERE user_id=? AND avail_at<=?",
                       (uid, now_ts())).fetchone()[0]
    return total, avail

async def try_referral(new_uid, payload) -> bool:
    """Вызывается только для совсем нового пользователя. payload вида ref_123456."""
    if not payload.startswith("ref_") or not payload[4:].isdigit():
        return False
    ref = int(payload[4:])
    if ref == new_uid:
        return False
    if not db.execute("SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone():
        return False
    if db.execute("SELECT 1 FROM referrals WHERE user_id=?", (new_uid,)).fetchone():
        return False
    banned = ref_banned(ref)
    today = db.execute("SELECT COUNT(*) FROM ref_ledger WHERE user_id=? AND kind='signup' AND created_at>?",
                       (ref, now_ts() - 86400)).fetchone()[0]
    rewarded = 0 if (banned or today >= REF_DAILY_LIMIT) else 1
    first_over = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded=0 AND created_at>?",
                            (ref, now_ts() - 86400)).fetchone()[0] == 0
    db.execute("INSERT INTO referrals (user_id, referrer_id, created_at, rewarded) VALUES (?,?,?,?)",
               (new_uid, ref, now_ts(), rewarded))
    db.commit()
    if not rewarded:
        if not banned and first_over:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ {user_label(ref)} достиг лимита наград за сутки "
                                                 f"({REF_DAILY_LIMIT}). Возможно накрутка — проверь.")
            except Exception:
                pass
        return False
    ledger_add(ref, "signup", REF_REWARD_RUB * 100, src=new_uid, hold=True, note="заход в бота")
    try:
        await bot.send_message(
            ref, f"🎉 По твоей ссылке зашёл новый пользователь! +{REF_REWARD_RUB} ₽ "
                 f"(станут доступны через {REF_HOLD_DAYS} дн.)")
    except Exception:
        pass
    return True

async def credit_commission(oid):
    row = db.execute("SELECT user_id, tariff, price, discount_pct, COALESCE(currency,'RUB'), status "
                     "FROM orders WHERE id=?", (oid,)).fetchone()
    if not row:
        return
    buyer, key, price, pct, cur, status = row
    if status != "paid" or cur == "BAL" or key == "unknown":
        return
    r = db.execute("SELECT referrer_id FROM referrals WHERE user_id=?", (buyer,)).fetchone()
    if not r or ref_banned(r[0]):
        return
    if db.execute("SELECT 1 FROM ref_ledger WHERE order_id=? AND kind='commission'", (oid,)).fetchone():
        return
    if cur == "XTR":
        t = tariff_by_key(key)
        if not t:
            return
        rub = round(t[2] * (100 - (pct or 0)) / 100)
    else:
        rub = price
    kop = int(round(rub * 100 * REF_COMMISSION_PCT / 100))
    if kop <= 0:
        return
    ledger_add(r[0], "commission", kop, src=buyer, order_id=oid, hold=True, note=f"{REF_COMMISSION_PCT}% с заказа")
    try:
        await bot.send_message(r[0], f"💰 Твой приглашённый оплатил Premium: +{fmt_rub(kop)} "
                                     f"(доступно через {REF_HOLD_DAYS} дн.)")
    except Exception:
        pass

def clawback_order(oid):
    rows = db.execute("SELECT user_id, amount_kop, src_user FROM ref_ledger WHERE order_id=? AND kind='commission'",
                      (oid,)).fetchall()
    if not rows or db.execute("SELECT 1 FROM ref_ledger WHERE order_id=? AND kind='clawback'", (oid,)).fetchone():
        return
    for uid, kop, src_ in rows:
        ledger_add(uid, "clawback", -kop, src=src_, order_id=oid, note="возврат заказа")

def cabinet_status_text(uid) -> str:
    invited = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
    paid = db.execute(
        "SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN referrals r ON r.user_id=o.user_id "
        "WHERE r.referrer_id=? AND o.status='paid' AND COALESCE(o.currency,'RUB')!='BAL'", (uid,)).fetchone()[0]
    earned = db.execute("SELECT COALESCE(SUM(amount_kop),0) FROM ref_ledger WHERE user_id=? "
                        "AND kind IN ('signup','commission','clawback')", (uid,)).fetchone()[0]
    total, avail = ref_balance(uid)
    hold = total - avail
    paid_out = db.execute("SELECT COALESCE(SUM(amount_kop),0) FROM withdrawals WHERE user_id=? AND status='paid'",
                          (uid,)).fetchone()[0]
    return (f"📋 Твой кабинет\n\n{role_line(uid)}{tariff_status_text(uid)}\n\n"
            f"👥 Приглашено: {invited} (из них платили: {paid})\n"
            f"💰 Заработано всего: {fmt_rub(earned)}\n"
            f"✅ Доступно: {fmt_rub(max(avail, 0))}\n"
            f"⏳ Ожидает {REF_HOLD_DAYS} дн.: {fmt_rub(max(hold, 0))}\n"
            f"💸 Выплачено: {fmt_rub(paid_out)}")

def ref_info_text(uid) -> str:
    return (f"🔗 Твоя реферальная ссылка:\n{ref_link(uid)}\n\n"
            f"• +{REF_REWARD_RUB} ₽ за каждого нового человека, который зашёл в бота по ссылке\n"
            f"• +{REF_COMMISSION_PCT}% с каждой оплаты Premium твоими приглашёнными\n"
            f"• Деньги становятся доступными через {REF_HOLD_DAYS} дн.\n"
            f"• Их можно потратить на Premium («➕ Пополнение») или вывести от "
            f"{REF_MIN_WITHDRAW_RUB} ₽ — выплаты {PAYOUT_DAY}-го числа\n"
            f"• Награда только за новых пользователей; накрутка и фейк-аккаунты — обнуление баланса и бан")

def proxy_url() -> str:
    return get_setting("free_proxy_url") or FREE_PROXY_URL

def normalize_proxy(link: str) -> str:
    link = link.strip()
    if link.startswith("tg://proxy?"):
        link = "https://t.me/proxy?" + link[len("tg://proxy?"):]
    return link

def pending_withdrawal(uid):
    return db.execute("SELECT id, amount_kop FROM withdrawals WHERE user_id=? AND status='pending'",
                      (uid,)).fetchone()

async def handle_cabinet_text(message, uid, text) -> bool:
    if text == "👤 Личный кабинет":
        await message.answer("👤 Личный кабинет", reply_markup=cabinet_keyboard())
        return True
    if text == "📋 Мой статус":
        await message.answer(cabinet_status_text(uid), reply_markup=cabinet_keyboard())
        return True
    if text == "🔗 Реф. ссылка":
        from urllib.parse import quote
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={quote(ref_link(uid))}&text={quote('Бот, который присылает удалённые сообщения')}")]])
        await message.answer(ref_info_text(uid), reply_markup=kb)
        return True
    if text == "💸 Вывод":
        total, avail = ref_balance_w(uid)
        pend = pending_withdrawal(uid)
        lines = [f"💸 Вывод средств\n",
                 f"Доступно: {fmt_rub(max(avail, 0))}",
                 f"Минимум для вывода: {REF_MIN_WITHDRAW_RUB} ₽. Выплаты — {PAYOUT_DAY}-го числа каждого месяца, "
                 f"через владельца ({OWNER_CONTACT})."]
        kb = None
        if ref_banned(uid):
            lines.append("\n⛔ Вывод для тебя недоступен. Вопросы — к владельцу.")
        elif pend:
            lines.append(f"\n⏳ Заявка #{pend[0]} на {fmt_rub(pend[1])} ждёт выплаты.")
        elif avail >= REF_MIN_WITHDRAW_RUB * 100:
            lines.append("\nМожешь запросить вывод всей доступной суммы.")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="💸 Запросить вывод", callback_data="wd:req")]])
        else:
            lines.append(f"\nДо вывода не хватает {fmt_rub(REF_MIN_WITHDRAW_RUB * 100 - max(avail, 0))}.")
        await message.answer("\n".join(lines), reply_markup=kb)
        return True
    if text == "➕ Пополнение":
        total, avail = ref_balance(uid)
        extra = f"\n\n💰 На твоём балансе {fmt_rub(max(avail, 0))} — им можно оплатить Premium." if avail > 0 else ""
        await message.answer(premium_info_text(uid) + extra, reply_markup=tariff_keyboard(uid))
        return True
    if text == "🎁 Бонус":
        await show_bonus(message, uid)
        return True
    if text == "🌐 Бесплатный прокси":
        url = proxy_url()
        if not url:
            await message.answer("🌐 Бесплатный прокси скоро появится.")
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Подключить прокси", url=url)]])
            await message.answer("🌐 Бесплатный прокси для Telegram. Нажми кнопку и подтверди подключение.\n"
                                 f"Если не открылось — ссылка: {url}", reply_markup=kb)
        return True
    return False

async def start_withdraw(callback, uid):
    total, avail = ref_balance_w(uid)
    if ref_banned(uid):
        await callback.answer("Вывод недоступен.", show_alert=True)
        return
    if pending_withdrawal(uid):
        await callback.answer("У тебя уже есть заявка.", show_alert=True)
        return
    if avail < REF_MIN_WITHDRAW_RUB * 100:
        await callback.answer(f"Минимум {REF_MIN_WITHDRAW_RUB} ₽.", show_alert=True)
        return
    set_setting(f"ref_wait_{uid}", "wd")
    await callback.message.answer(
        f"Напиши реквизиты для выплаты {fmt_rub(avail)} (банк и номер карты/телефона, или другой способ) "
        f"одним сообщением. Отмена — «← Назад».")
    await callback.answer()

async def handle_withdraw_details(message, uid, text):
    del_setting(f"ref_wait_{uid}")
    total, avail = ref_balance_w(uid)
    if len(text) < 5:
        await message.answer("Слишком коротко. Нажми «💸 Вывод» и попробуй снова.")
        return
    if ref_banned(uid) or pending_withdrawal(uid) or avail < REF_MIN_WITHDRAW_RUB * 100:
        await message.answer("Сейчас вывод недоступен.")
        return
    cur = db.execute("INSERT INTO withdrawals (user_id, amount_kop, details, status, created_at) "
                     "VALUES (?,?,?, 'pending', ?)", (uid, avail, text[:300], now_ts()))
    wid = cur.lastrowid
    db.commit()
    ledger_add(uid, "wd", -avail, note=f"заявка #{wid}")
    log_action(uid, "wd_request", uid, f"#{wid} {fmt_rub(avail)}")
    await message.answer(f"✅ Заявка #{wid} на {fmt_rub(avail)} принята. Выплата — {PAYOUT_DAY}-го числа.")
    try:
        await bot.send_message(ADMIN_ID, wd_text(wid), reply_markup=wd_keyboard(wid))
    except Exception:
        logging.exception("Не удалось уведомить владельца о выводе")

def wd_text(wid) -> str:
    r = db.execute("SELECT user_id, amount_kop, details, status FROM withdrawals WHERE id=?", (wid,)).fetchone()
    return (f"💸 Заявка на вывод #{wid}\nОт: {user_label(r[0])}\nСумма: {fmt_rub(r[1])}\n"
            f"Реквизиты: {r[2]}\nСтатус: {r[3]}")

def wd_keyboard(wid) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выплачено", callback_data=f"wdo:ok:{wid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wdo:no:{wid}")]])

async def owner_withdraw_action(callback, uid, data):
    if not is_owner(uid):
        await callback.answer("Только владелец.")
        return
    _, act, wid_s = data.split(":")
    r = db.execute("SELECT user_id, amount_kop, status FROM withdrawals WHERE id=?", (int(wid_s),)).fetchone()
    if not r:
        await callback.answer("Заявка не найдена.")
        return
    buyer, kop, status = r
    if status != "pending":
        await callback.answer("Уже обработана.")
        return
    wid = int(wid_s)
    if act == "ok":
        db.execute("UPDATE withdrawals SET status='paid', done_at=? WHERE id=?", (now_ts(), wid))
        db.commit()
        log_action(uid, "wd_paid", buyer, f"#{wid} {fmt_rub(kop)}")
        await callback.message.edit_text(f"✅ Заявка #{wid} выплачена: {user_label(buyer)}, {fmt_rub(kop)}")
        try:
            await bot.send_message(buyer, f"✅ Выплата {fmt_rub(kop)} по заявке #{wid} отправлена.")
        except Exception:
            pass
        await callback.answer("Отмечено")
    else:
        db.execute("UPDATE withdrawals SET status='rejected', done_at=? WHERE id=?", (now_ts(), wid))
        db.commit()
        ledger_add(buyer, "wd_back", kop, note=f"отказ по заявке #{wid}")
        log_action(uid, "wd_no", buyer, f"#{wid}")
        await callback.message.edit_text(f"❌ Заявка #{wid} отклонена ({user_label(buyer)}), деньги вернулись на баланс.")
        try:
            await bot.send_message(buyer, f"Заявка #{wid} отклонена, деньги вернулись на баланс. "
                                          f"Вопросы: {OWNER_CONTACT}")
        except Exception:
            pass
        await callback.answer("Отклонено")

async def send_pending_withdrawals(chat_id) -> int:
    rows = db.execute("SELECT id FROM withdrawals WHERE status='pending' ORDER BY id").fetchall()
    for (wid,) in rows:
        await bot.send_message(chat_id, wd_text(wid), reply_markup=wd_keyboard(wid))
    return len(rows)

async def payout_loop():
    while True:
        try:
            now = datetime.now(TZ)
            key = now.strftime("%Y-%m")
            if now.day == PAYOUT_DAY and now.hour >= 10 and get_setting("payout_notified") != key:
                set_setting("payout_notified", key)
                n = await send_pending_withdrawals(ADMIN_ID)
                if n:
                    await bot.send_message(ADMIN_ID, f"📅 Сегодня день выплат: заявок {n}.")
        except Exception:
            logging.exception("Ошибка payout_loop")
        await asyncio.sleep(1800)

async def pay_from_balance(callback, uid, key):
    t = tariff_by_key(key)
    if not t:
        await callback.answer("Тариф не найден.")
        return
    if key == "trial" and not trial_available(uid):
        await callback.answer("Пробный тариф можно взять один раз.", show_alert=True)
        return
    rub, pct = price_for(uid, key)
    total, avail = ref_balance(uid)
    if avail < rub * 100:
        await callback.answer("Не хватает доступных денег на балансе.", show_alert=True)
        return
    cur = db.execute(
        "INSERT INTO orders (user_id, tariff, days, price, discount_pct, status, created_at, paid_at, currency) "
        "VALUES (?,?,?,?,?, 'paid', ?, ?, 'BAL')", (uid, key, t[1], rub, pct, now_ts(), now_ts()))
    oid = cur.lastrowid
    if key == "trial":
        db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (uid,))
    if pct:
        db.execute("UPDATE users SET discount_pct=0, discount_until=0, discount_code=NULL WHERE user_id=?", (uid,))
    db.commit()
    ledger_add(uid, "spend", -rub * 100, order_id=oid, note="оплата Premium")
    log_action(uid, "bal_paid", uid, f"#{oid} {t[1]} дн {rub} ₽")
    until = await do_grant(ADMIN_ID, uid, t[1])
    await callback.message.answer(f"✅ Оплачено с баланса: {rub} ₽. Premium активен до {fmt_ts(until)}.")
    try:
        await bot.send_message(ADMIN_ID, f"💰 Оплата с реф. баланса: заказ #{oid}, {user_label(uid)}, {t[3]}, {rub} ₽.")
    except Exception:
        pass
    await callback.answer()

@dp.message(Command("setproxy"))
async def cmd_setproxy(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /setproxy ССЫЛКА (tg://proxy?... или https://t.me/proxy?...)\n"
                             "Выключить: /setproxy off")
        return
    if parts[1].strip().lower() == "off":
        del_setting("free_proxy_url")
        await message.answer("Прокси убран.")
        return
    link = normalize_proxy(parts[1])
    if not link.startswith("https://t.me/"):
        await message.answer("Ссылка должна начинаться с tg://proxy? или https://t.me/")
        return
    set_setting("free_proxy_url", link)
    await message.answer(f"✅ Прокси сохранён:\n{link}")

@dp.message(Command("payouts"))
async def cmd_payouts(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    n = await send_pending_withdrawals(ADMIN_ID)
    if not n:
        await message.answer("Заявок на вывод нет.")

@dp.message(Command("refban"))
async def cmd_refban(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /refban ID — заморозить реф. заработок и вывод (баланс обнуляется)\n"
                             "Вернуть: /refunban ID")
        return
    target = int(parts[1])
    set_setting(f"ref_ban_{target}", "1")
    total, _ = ref_balance(target)
    if total > 0:
        ledger_add(target, "clawback", -total, note="обнуление владельцем")
    log_action(ADMIN_ID, "refban", target, "")
    await message.answer(f"⛔ {user_label(target)}: реф. программа закрыта, баланс обнулён.")

@dp.message(Command("refunban"))
async def cmd_refunban(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /refunban ID")
        return
    del_setting(f"ref_ban_{int(parts[1])}")
    await message.answer("✅ Снято.")

# ---------- Уведомление о перезапуске ----------

HEARTBEAT_SEC = 20
RESTART_QUIET_SEC = 180   # перезапуск только бота короче этого — не беспокоим владельца

def fmt_dur(sec) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d} д")
    if h: parts.append(f"{h} ч")
    if m: parts.append(f"{m} мин")
    if not parts: parts.append(f"{s} сек")
    return " ".join(parts)

def read_uptime() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return -1.0

def restart_keyboard(minutes) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙏 Извиниться за технические неполадки", callback_data=f"rs:sorry:{minutes}")],
        [InlineKeyboardButton(text="🗑 Забыть", callback_data="rs:forget")]])

async def startup_notice():
    now = now_ts()
    prev = get_setting("last_alive")
    set_setting("last_alive", str(now))
    up = read_uptime()
    if not prev or not prev.isdigit():
        await bot.send_message(
            ADMIN_ID, f"🔄 Бот запущен {fmt_ts(now)}.\nСлежение за простоями только началось — "
                      f"сколько простоял до этого, неизвестно.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Забыть", callback_data="rs:forget")]]))
        return
    prev = int(prev)
    down = max(0, now - prev)
    rebooted = up >= 0 and (now - up) > prev - HEARTBEAT_SEC
    if not rebooted and down < RESTART_QUIET_SEC:
        return
    cause = (f"🖥 Сервер перезагружался (работает с {fmt_ts(now - up)})" if rebooted
             else "♻️ Перезапущен только бот (сервер не перезагружался)")
    await bot.send_message(
        ADMIN_ID,
        f"🔄 Бот снова в строю\n\n"
        f"{cause}\n"
        f"Последняя активность: {fmt_ts(prev)}\n"
        f"Запуск: {fmt_ts(now)}\n"
        f"⏱ Простой: {fmt_dur(down)}\n\n"
        f"Накопившиеся за это время сообщения бот сейчас обрабатывает — возможна небольшая задержка.",
        reply_markup=restart_keyboard(down // 60))

async def heartbeat_loop():
    while True:
        try:
            set_setting("last_alive", str(now_ts()))
        except Exception:
            logging.exception("Ошибка heartbeat")
        await asyncio.sleep(HEARTBEAT_SEC)


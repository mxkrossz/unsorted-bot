# ---------- Оплата звёздами Telegram ----------

TARIFF_STARS = {"trial": 25, "m1": 50, "m3": 75, "m6": 125, "m12": 200}
REFUND_WINDOW_DAYS = 7
STAR_USD = 0.013   # сколько USD получает разработчик за одну звезду (условия Telegram)

def stars_price_for(uid, key):
    base = TARIFF_STARS[key]
    _rub = rub_discount_amount(uid) if key != "trial" else 0
    if _rub:
        _t = tariff_by_key(key)
        _price = max(1, _t[2] - _rub)
        return max(1, round(base * _price / _t[2])), max(1, round(100 * (1 - _price / _t[2])))
    pct = discount_percent_for(uid) if key != "trial" else 0
    return max(1, round(base * (100 - pct) / 100)), pct

def create_star_order(uid, key):
    """Заказ под оплату звёздами (статус star_pending). -> (id, звёзды, скидка%)"""
    t = tariff_by_key(key)
    stars, pct = stars_price_for(uid, key)
    row = db.execute(
        "SELECT id FROM orders WHERE user_id=? AND tariff=? AND status='star_pending' AND currency='XTR' "
        "AND price=? AND created_at > ? ORDER BY id DESC LIMIT 1",
        (uid, key, stars, now_ts() - 3600)).fetchone()
    if row:
        return row[0], stars, pct
    code = (db.execute("SELECT discount_code FROM users WHERE user_id=?", (uid,)).fetchone() or [None])[0] if pct else None
    cur = db.execute(
        "INSERT INTO orders (user_id, tariff, days, price, discount_pct, promo, status, created_at, currency, stars) "
        "VALUES (?,?,?,?,?,?, 'star_pending', ?, 'XTR', ?)",
        (uid, key, t[1], stars, pct, code, now_ts(), stars))
    db.commit()
    return cur.lastrowid, stars, pct

def method_keyboard(uid, key) -> InlineKeyboardMarkup:
    rub, _ = price_for(uid, key)
    stars, _ = stars_price_for(uid, key)
    rows = [
        [InlineKeyboardButton(text=f"⭐ Звёздами — {stars} ⭐", callback_data=f"pay:star:{key}")],
        [InlineKeyboardButton(text=f"💳 Картой / переводом — {rub} ₽", callback_data=f"pay:card:{key}")],
    ]
    if ref_balance(uid)[1] >= rub * 100:
        rows.append([InlineKeyboardButton(text=f"💰 С баланса — {rub} ₽", callback_data=f"pay:bal:{key}")])
    rows.append([InlineKeyboardButton(text="← Другой тариф", callback_data="buy:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def method_text(uid, key) -> str:
    t = tariff_by_key(key)
    rub, _ = price_for(uid, key)
    stars, pct = stars_price_for(uid, key)
    disc = f" (скидка {pct}%)" if pct else ""
    return (f"🧾 {t[3]}{disc}\n\n"
            f"⭐ Звёздами Telegram: {stars} ⭐ — Premium включится сразу после платежа.\n"
            f"💳 Картой / переводом: {rub} ₽ — владелец подтверждает платёж вручную.\n\n"
            f"Оплачивая, ты соглашаешься с условиями: /terms")

def shorten_premium(uid, days):
    until = get_premium_until(uid) - int(days) * 86400
    db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (max(0, until) if until > now_ts() else 0, uid))
    db.commit()

async def refund_stars(uid, charge_id) -> bool:
    """Возврат звёзд. Работает и на старых версиях aiogram (через прямой запрос к Bot API)."""
    try:
        if hasattr(bot, "refund_star_payment"):
            return bool(await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id))
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API_BASE}/bot{BOT_TOKEN}/refundStarPayment",
                              json={"user_id": uid, "telegram_payment_charge_id": charge_id}) as r:
                data = await r.json()
                return bool(data.get("ok"))
    except Exception:
        logging.exception("Ошибка возврата звёзд")
        return False

def terms_text() -> str:
    return (f"📄 Условия\n\n"
            f"• Premium — цифровая услуга на оплаченный срок: полная история удалённых сообщений "
            f"(до {TEXT_RETENTION_DAYS} дн.), спасение одноразовых медиа без лимита, перевод сообщений "
            f"({PREMIUM_TRANSLATE_PER_DAY} в день), защита от фишинга ({PREMIUM_LINKS_PER_DAY} проверок ссылок в день), "
            f"расшифровка голосовых без лимита, проверка файлов без лимита (до {PREMIUM_SCAN_MAX_MB} МБ) и списки из сообщений без лимита.\n"
            f"• Автопродления нет. Перед окончанием бот напомнит, продлевать или нет.\n"
            f"• Оплата звёздами включает Premium сразу после платежа.\n"
            f"• Возврат: если Premium не заработал или оплата прошла по ошибке — напиши в течение "
            f"{REFUND_WINDOW_DAYS} дней через /paysupport, звёзды вернём.\n"
            f"• Пробный тариф — один раз на аккаунт.\n\n"
            f"Полный текст публичной оферты: /offer\n"
            f"Оплачивая, ты соглашаешься с этими условиями.")

def paysupport_text(uid) -> str:
    rows = db.execute(
        "SELECT id, tariff, status, COALESCE(currency,'RUB'), price FROM orders WHERE user_id=? "
        "ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    names = {"paid": "оплачен", "pending": "ждёт оплаты", "star_pending": "не оплачен",
             "rejected": "отклонён", "refunded": "возвращён"}
    lines = [f"#{i} · {tariff_by_key(k)[3] if tariff_by_key(k) else k} · "
             f"{p} {'⭐' if c == 'XTR' else '₽'} · {names.get(s, s)}" for i, k, s, c, p in rows]
    return ("💬 Проблема с оплатой?\n\nНапиши " + OWNER_CONTACT + " и укажи номер заказа. "
            "Telegram и его поддержка по покупкам в этом боте не помогут.\n\n"
            + ("Твои последние заказы:\n" + "\n".join(lines) if lines else "Заказов пока нет."))


# ---------- Промокоды ----------

STAFF_PROMO_MAX_DAYS = 30
STAFF_PROMO_MAX_PCT = 30
STAFF_PROMO_MAX_USES = 100
STAFF_PROMO_MAX_EXPIRY_DAYS = 30
DISCOUNT_VALID_DAYS = 7
_promo_fails: dict = {}

def promo_rate_ok(uid) -> bool:
    now = time.time()
    lst = [t for t in _promo_fails.get(uid, []) if now - t < 3600]
    _promo_fails[uid] = lst
    return len(lst) < 5

def promo_fail(uid):
    _promo_fails.setdefault(uid, []).append(time.time())

def gen_promo_code() -> str:
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if not db.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone():
            return code

def create_promo(code, kind, value, max_uses, expiry_days, by):
    exp = now_ts() + expiry_days * 86400 if expiry_days else 0
    db.execute(
        "INSERT INTO promo_codes (code, kind, value, max_uses, used, expires_at, active, created_by, created_at) "
        "VALUES (?,?,?,?,0,?,1,?,?)", (code, kind, value, max_uses, exp, by, now_ts()))
    db.commit()

STAFF_PROMO_MAX_RUB = 30
PROMO_NEW_DAYS = 14

PROMO_AUDIENCES = {
    "all":       "👥 Все",
    "new":       f"🆕 Новые (до {PROMO_NEW_DAYS} дней)",
    "free":      "🆓 Без Premium",
    "premium":   "💎 С активным Premium",
    "connected": "🔌 Подключили Business",
    "unpaid":    "🛒 Ещё не платили",
    "testers":   "🧪 Тестеры",
}

def promo_kind_text(kind, value) -> str:
    if kind == "days":
        return f"+{value} дн Premium"
    if kind == "rub":
        return f"скидка {value} ₽"
    if kind == "bonus":
        return f"+{value} ₽ на баланс"
    return f"скидка {value}%"

def promo_value_limit(kind, is_o) -> int:
    if kind == "days":
        return 365 if is_o else STAFF_PROMO_MAX_DAYS
    if kind == "rub":
        return 1000 if is_o else STAFF_PROMO_MAX_RUB
    if kind == "bonus":
        return 1000 if is_o else 0
    return 90 if is_o else STAFF_PROMO_MAX_PCT

def promo_audience_ok(uid, aud) -> bool:
    if not aud or aud == "all":
        return True
    if aud == "new":
        r = db.execute("SELECT 1 FROM users WHERE user_id=? AND first_seen >= datetime('now', ?)",
                       (uid, f"-{PROMO_NEW_DAYS} days")).fetchone()
        return r is not None
    if aud == "free":
        return not is_premium(uid)
    if aud == "premium":
        return is_premium(uid)
    if aud == "connected":
        return db.execute("SELECT 1 FROM biz_conn WHERE owner_id=? AND (enabled=1 OR "
                          "(enabled IS NULL AND COALESCE(last_msg_at,0) > ?))",
                          (uid, int(time.time()) - 7 * 86400)).fetchone() is not None
    if aud == "unpaid":
        return db.execute("SELECT 1 FROM orders WHERE user_id=? AND status='paid'", (uid,)).fetchone() is None
    if aud == "testers":
        return is_tester(uid)
    return True

def promo_audience_label(code) -> str:
    r = db.execute("SELECT audience FROM promo_codes WHERE code=?", (code,)).fetchone()
    return PROMO_AUDIENCES.get((r[0] if r else None) or "all", "👥 Все")

def pc_apply_audience(code, uid):
    aud = get_setting(f"pc_aud_{uid}") or "all"
    if aud not in PROMO_AUDIENCES:
        aud = "all"
    db.execute("UPDATE promo_codes SET audience=? WHERE code=?", (aud, code))
    db.commit()

def promo_stats_line(code) -> str:
    n, = db.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND "
                    "(promo=? OR promo LIKE ?)", (code, f"RUB%:{code}")).fetchone()
    return f"Оплат Premium с этим кодом: {n}"

def promo_stats_text() -> str:
    q1 = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    total = q1("SELECT COUNT(*) FROM promo_codes")
    alive = sum(1 for r in db.execute(
        "SELECT code, kind, value, max_uses, used, expires_at, active FROM promo_codes").fetchall()
        if promo_status(r)[0])
    uses = q1("SELECT COUNT(*) FROM promo_uses")
    paid = q1("SELECT COUNT(*) FROM orders WHERE status='paid' AND promo IS NOT NULL AND promo!=''")
    by_kind = db.execute("SELECT kind, COUNT(*), COALESCE(SUM(used),0) FROM promo_codes GROUP BY kind").fetchall()
    names = {"days": "дни Premium", "discount": "скидка %", "rub": "скидка ₽", "bonus": "бонус на баланс"}
    lines = [f"📊 Промокоды: всего {total}, активных {alive}", f"Активаций: {uses}",
             f"Оплат Premium по промокодам: {paid}", ""]
    for k, c, u in by_kind:
        lines.append(f"• {names.get(k, k)}: кодов {c}, активаций {u}")
    return "\n".join(lines)

def rub_discount_amount(uid) -> int:
    row = db.execute("SELECT discount_until, discount_code FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0] and row[0] > now_ts() and (row[1] or "").startswith("RUB"):
        try:
            return int(row[1][3:].split(":")[0])
        except ValueError:
            return 0
    return 0

def promo_locked_kop(uid) -> int:
    """Деньги от промокодов: тратить на Premium можно, выводить нельзя."""
    cred = db.execute("SELECT COALESCE(SUM(amount_kop),0) FROM ref_ledger WHERE user_id=? AND kind='promo'",
                      (uid,)).fetchone()[0]
    spent = -db.execute("SELECT COALESCE(SUM(amount_kop),0) FROM ref_ledger WHERE user_id=? AND kind='spend'",
                        (uid,)).fetchone()[0]
    return max(0, cred - max(0, spent))

def ref_balance_w(uid):
    """Баланс для вывода: без денег от промокодов."""
    total, avail = ref_balance(uid)
    return total, avail - promo_locked_kop(uid)

def promo_status(row):
    """row: code, kind, value, max_uses, used, expires_at, active -> (активен?, причина)"""
    code, kind, value, max_uses, used, exp, active = row[:7]
    if not active:
        return False, "отключён"
    if exp and exp < now_ts():
        return False, "срок истёк"
    if max_uses and used >= max_uses:
        return False, "лимит исчерпан"
    return True, "активен"

def redeem_promo(uid, raw):
    """-> (успех, текст, тип)"""
    code = (raw or "").strip().upper()
    row = db.execute(
        "SELECT code, kind, value, max_uses, used, expires_at, active FROM promo_codes WHERE code=?",
        (code,)).fetchone()
    if not row:
        return False, "Такого промокода нет. Проверь написание.", None
    alive, why = promo_status(row)
    if not alive:
        return False, {"отключён": "Этот промокод отключён.",
                       "срок истёк": "Срок действия промокода истёк.",
                       "лимит исчерпан": "Промокод уже использован максимальное число раз."}[why], None
    if db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
        return False, "Ты уже использовал этот промокод.", None
    _, kind, value = row[:3]
    _aud = db.execute("SELECT audience FROM promo_codes WHERE code=?", (code,)).fetchone()
    if not promo_audience_ok(uid, _aud[0] if _aud else "all"):
        return False, "Этот промокод недоступен для твоего аккаунта.", None
    if kind == "rub" and active_discount(uid):
        return False, ("У тебя уже есть действующая скидка. Сначала используй её на покупку, "
                       "потом введи этот промокод."), None
    db.execute("INSERT INTO promo_uses (code, user_id, used_at) VALUES (?,?,?)", (code, uid, now_ts()))
    db.execute("UPDATE promo_codes SET used = used + 1 WHERE code=?", (code,))
    db.commit()
    if kind == "bonus":
        ledger_add(uid, "promo", int(value) * 100, note=f"промокод {code}")
        return True, (f"✅ Промокод принят: +{value} ₽ на баланс. Их можно потратить на Premium "
                      f"(кнопка «💰 С баланса» при оплате), вывести нельзя."), kind
    if kind == "rub":
        until = now_ts() + DISCOUNT_VALID_DAYS * 86400
        db.execute("UPDATE users SET discount_pct=1, discount_until=?, discount_code=? WHERE user_id=?",
                   (until, f"RUB{int(value)}:{code}", uid))
        db.commit()
        return True, (f"✅ Промокод принят: скидка {value} ₽ на один заказ до {fmt_ts(until)}. "
                      f"Выбери тариф:"), kind
    if kind == "days":
        until = grant_premium(uid, value)
        return True, f"✅ Промокод принят: +{value} дн Premium, теперь до {fmt_ts(until)}.", kind
    cur = active_discount(uid)
    if cur and cur[0] > value:
        return True, (f"✅ Промокод принят, но у тебя уже есть скидка {cur[0]}% — она больше, "
                      f"оставляю её (до {fmt_ts(cur[1])})."), kind
    until = now_ts() + DISCOUNT_VALID_DAYS * 86400
    db.execute("UPDATE users SET discount_pct=?, discount_until=?, discount_code=? WHERE user_id=?",
               (value, until, code, uid))
    db.commit()
    return True, (f"✅ Промокод принят: скидка {value}% на один заказ до {fmt_ts(until)}. "
                  f"Выбери тариф:"), kind

def promo_list_view(uid):
    if is_owner(uid):
        rows = db.execute(
            "SELECT code, kind, value, max_uses, used, expires_at, active FROM promo_codes "
            "ORDER BY created_at DESC LIMIT 15").fetchall()
    else:
        rows = db.execute(
            "SELECT code, kind, value, max_uses, used, expires_at, active FROM promo_codes "
            "WHERE created_by=? ORDER BY created_at DESC LIMIT 15", (uid,)).fetchall()
    lines = []
    kb = []
    for r in rows:
        alive, why = promo_status(r)
        lim = f"{r[4]}/{r[3]}" if r[3] else f"{r[4]}/∞"
        until = f" · до {fmt_ts(r[5])}" if r[5] else ""
        lines.append(f"{'✅' if alive else '🚫'} {r[0]} — {promo_kind_text(r[1], r[2])} · {lim}{until}")
        kb.append([InlineKeyboardButton(text=f"{r[0]} · {promo_kind_text(r[1], r[2])}",
                                        callback_data=f"pc:show:{r[0]}")])
    kb.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="pc:new")])
    kb.append([InlineKeyboardButton(text="📊 Статистика", callback_data="pc:stats")])
    text = "🎟 Промокоды:\n\n" + ("\n".join(lines) if lines else "Пока нет ни одного.")
    return text, InlineKeyboardMarkup(inline_keyboard=kb)

def promo_detail_view(uid, code):
    row = db.execute(
        "SELECT code, kind, value, max_uses, used, expires_at, active, created_by FROM promo_codes WHERE code=?",
        (code,)).fetchone()
    if not row or (not is_owner(uid) and row[7] != uid):
        return None, None
    alive, why = promo_status(row)
    uses = db.execute(
        "SELECT p.user_id, u.username, p.used_at FROM promo_uses p LEFT JOIN users u ON u.user_id=p.user_id "
        "WHERE p.code=? ORDER BY p.used_at DESC LIMIT 20", (code,)).fetchall()
    lim = f"{row[4]}/{row[3]}" if row[3] else f"{row[4]}/∞"
    lines = [f"🎟 {row[0]}", f"Тип: {promo_kind_text(row[1], row[2])}", f"Использовано: {lim}",
             f"Действует до: {fmt_ts(row[5]) if row[5] else 'бессрочно'}",
             f"Статус: {why}", f"Аудитория: {promo_audience_label(row[0])}", promo_stats_line(row[0]),
             f"Создал: {user_label(row[7])}", "",
             "Активации:" if uses else "Активаций пока нет."]
    for u_id, un, ts in uses:
        lines.append(f"• {('@' + un) if un else u_id} — {fmt_ts(ts)}")
    kb = []
    if row[6]:
        kb.append([InlineKeyboardButton(text="🚫 Отключить", callback_data=f"pc:off:{row[0]}")])
    else:
        kb.append([InlineKeyboardButton(text="✅ Включить", callback_data=f"pc:on:{row[0]}")])
    kb.append([InlineKeyboardButton(text="← К списку", callback_data="pc:list")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


# ---------- Покупка Premium ----------

# (ключ, дней, цена ₽, название)
TARIFFS = [
    ("trial", 15, 49, "Пробный · 15 дней"),
    ("m1", 30, 99, "1 месяц"),
    ("m3", 90, 149, "3 месяца"),
    ("m6", 180, 249, "6 месяцев"),
    ("m12", 365, 399, "12 месяцев"),
]
BASE_MONTH_PRICE = 99

def tariff_by_key(key):
    for t in TARIFFS:
        if t[0] == key:
            return t
    return None

def trial_available(uid) -> bool:
    row = db.execute("SELECT trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0]:
        return False
    busy = db.execute(
        "SELECT 1 FROM orders WHERE user_id=? AND tariff='trial' AND status IN ('pending','paid')",
        (uid,)).fetchone()
    return busy is None

def active_discount(uid):
    """(процент, до когда) или None."""
    row = db.execute("SELECT discount_pct, discount_until FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0] and row[1] and row[1] > now_ts():
        return row[0], row[1]
    return None

def discount_percent_for(uid) -> int:
    d = active_discount(uid)
    return d[0] if d else 0

def price_for(uid, key):
    t = tariff_by_key(key)
    _rub = rub_discount_amount(uid) if key != "trial" else 0
    if _rub:
        price = max(1, t[2] - _rub)
        return price, max(1, round(100 * (1 - price / t[2])))
    pct = discount_percent_for(uid) if key != "trial" else 0
    price = max(1, round(t[2] * (100 - pct) / 100))
    return price, pct

def tariffs_text(uid) -> str:
    lines = ["Тарифы Premium:"]
    _d = active_discount(uid)
    if _d:
        _r = rub_discount_amount(uid)
        lines.append(f"🎟 Твоя скидка {str(_r) + ' ₽' if _r else str(_d[0]) + '%'} действует до "
                     f"{fmt_ts(_d[1])} (на один заказ)")
    for key, days, price, label in TARIFFS:
        if key == "trial":
            if not trial_available(uid):
                continue
            lines.append(f"• {label} — {price} ₽ (один раз)")
            continue
        p, pct = price_for(uid, key)
        save = BASE_MONTH_PRICE * round(days / 30) - price
        extra = f" · выгода {save} ₽" if save > 0 else ""
        if pct:
            lines.append(f"• {label} — {p} ₽ вместо {price} ₽ (скидка {pct}%){extra}")
        else:
            lines.append(f"• {label} — {p} ₽{extra}")
    return "\n".join(lines)

try:
    with open(os.path.join(BASE_DIR, "offer.txt"), encoding="utf-8") as _of:
        OFFER_TEXT = _of.read().strip()
except Exception:
    OFFER_TEXT = "Текст публичной оферты: положи его в файл offer.txt рядом с bot.py."

def offer_ok(uid) -> bool:
    """Нажал ли пользователь «Согласен с офертой»."""
    r = db.execute("SELECT COALESCE(offer_ok_at,0) FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0])

def offer_set_ok(uid):
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    db.execute("UPDATE users SET offer_ok_at=? WHERE user_id=?", (now_ts(), uid))
    db.commit()

def offer_current() -> str:
    """Оферта, которую владелец отредактировал в боте; если не редактировал — встроенная."""
    return get_setting("offer_custom", "") or OFFER_TEXT

def offer_chunks(limit=3800):
    parts, cur = [], ""
    for para in offer_current().split("\n\n"):
        if cur and len(cur) + len(para) + 2 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur:
        parts.append(cur)
    return parts

def offer_keyboard(uid, key=None) -> InlineKeyboardMarkup:
    rows = []
    if offer_ok(uid):
        rows.append([InlineKeyboardButton(text="✅ Вы согласились с офертой", callback_data="offer:done")])
    else:
        rows.append([InlineKeyboardButton(text="☑️ Согласен с офертой",
                                          callback_data=f"offer:ok:{key}" if key else "offer:okv")])
    rows.append([InlineKeyboardButton(text="← К тарифам", callback_data="buy:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def tariff_keyboard(uid) -> InlineKeyboardMarkup:
    rows = []
    for key, days, price, label in TARIFFS:
        if key == "trial" and not trial_available(uid):
            continue
        p, _ = price_for(uid, key)
        rows.append([InlineKeyboardButton(text=f"{label} — {p} ₽", callback_data=f"buy:{key}")])
    rows.append([InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo:enter")])
    rows.append([InlineKeyboardButton(text="📄 Публичная оферта", callback_data="offer:show")])
    if offer_ok(uid):
        rows.append([InlineKeyboardButton(text="✅ Вы согласились с офертой", callback_data="offer:done")])
    else:
        rows.append([InlineKeyboardButton(text="☑️ Согласен с офертой", callback_data="offer:ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def offer_gate(callback, uid, key) -> bool:
    """True — оплату нужно остановить: пользователь ещё не согласился с офертой."""
    if offer_ok(uid):
        return False
    await callback.message.answer(
        "Перед оплатой нужно согласиться с публичной офертой. Прочитай её и нажми «Согласен» — "
        "и сразу продолжим оплату.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Публичная оферта", callback_data="offer:show")],
            [InlineKeyboardButton(text="☑️ Согласен с офертой", callback_data=f"offer:ok:{key}")]]))
    await callback.answer()
    return True

def create_order(uid, key):
    """Создаёт заказ (или возвращает свежий ожидающий такой же). -> (id, цена, новый?)"""
    t = tariff_by_key(key)
    price, pct = price_for(uid, key)
    row = db.execute(
        "SELECT id, price FROM orders WHERE user_id=? AND tariff=? AND status='pending' "
        "AND price=? AND created_at > ? ORDER BY id DESC LIMIT 1",
        (uid, key, price, now_ts() - 3600)).fetchone()
    if row:
        return row[0], row[1], False
    cur = db.execute(
        "INSERT INTO orders (user_id, tariff, days, price, discount_pct, promo, status, created_at) "
        "VALUES (?,?,?,?,?,?, 'pending', ?)",
        (uid, key, t[1], price, pct,
         (db.execute("SELECT discount_code FROM users WHERE user_id=?", (uid,)).fetchone() or [None])[0]
         if pct else None, now_ts()))
    db.commit()
    return cur.lastrowid, price, True

def order_row(oid):
    return db.execute(
        "SELECT id, user_id, tariff, days, price, discount_pct, status FROM orders WHERE id=?",
        (oid,)).fetchone()

def pay_keyboard(oid, uid, key, price) -> InlineKeyboardMarkup:
    from urllib.parse import quote
    t = tariff_by_key(key)
    msg = f"Привет! Хочу оплатить Premium: заказ #{oid}, {t[3]}, {price} ₽. Мой ID: {uid}"
    url = f"https://t.me/{OWNER_CONTACT.lstrip('@')}?text={quote(msg)}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать для оплаты", url=url)],
        [InlineKeyboardButton(text="← Другой тариф", callback_data="buy:menu")]])

def order_text(oid, key, price, pct) -> str:
    t = tariff_by_key(key)
    disc = f" (скидка {pct}%)" if pct else ""
    return (f"🧾 Заказ #{oid}\nТариф: {t[3]}\nК оплате: {price} ₽{disc}\n\n"
            f"Как оплатить: нажми кнопку ниже и напиши {OWNER_CONTACT} — он пришлёт реквизиты "
            f"(в сообщении уже указан номер заказа). Когда платёж подтвердят, бот сам напишет "
            f"тебе, и Premium включится.")

def order_keyboard(oid) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выдать", callback_data=f"ord:ok:{oid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ord:no:{oid}")]])

def revenue_text() -> str:
    since = now_ts() - 30 * 86400
    q = ("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE status='paid' "
         "AND COALESCE(currency,'RUB') {op} 'XTR' AND COALESCE(currency,'RUB') != 'BAL'")
    s, n = db.execute(q.format(op="!=") + " AND paid_at>=?", (since,)).fetchone()
    s_all, n_all = db.execute(q.format(op="!=")).fetchone()
    st, nt = db.execute(q.format(op="=") + " AND paid_at>=?", (since,)).fetchone()
    st_all, nt_all = db.execute(q.format(op="=")).fetchone()
    return (f"💳 Картой за 30 дней: {s} ₽ ({n} шт.), всего {s_all} ₽ ({n_all} шт.)\n"
            f"⭐ Звёздами за 30 дней: {st} ⭐ ≈ {st * STAR_USD:.2f} USD ({nt} шт.), "
            f"всего {st_all} ⭐ ≈ {st_all * STAR_USD:.2f} USD ({nt_all} шт.)")

REMIND_TEXTS = {
    "3d": "⏳ Твой Premium закончится через несколько дней (до {until}). Продли, чтобы не потерять "
          "полную историю удалённых, одноразовые без лимита, переводы, защиту от фишинга, расшифровку голосовых, проверку файлов и списки.",
    "1d": "⏳ Premium заканчивается завтра ({until}). Успей продлить.",
    "end": "Premium закончился. Теперь история удалённых — только за {free} дн., одноразовых "
           "{once} в день. Вернуть всё можно в любой момент.",
}

def remind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Продлить Premium", callback_data="buy:menu")]])

async def check_reminders() -> int:
    """Рассылает напоминания об окончании Premium (каждое — один раз). Возвращает число отправленных."""
    now = now_ts()
    rows = db.execute(
        "SELECT user_id, premium_until FROM users WHERE premium_until > ? AND user_id != ?",
        (now - 2 * 86400, ADMIN_ID)).fetchall()
    sent = 0
    for uid, until in rows:
        remain = until - now
        if remain > 3 * 86400:
            continue
        if not (0 < remain <= 86400):
            continue        # 3d / end / после окончания — в check_retention (настраиваются владельцем)
        stage = "1d"
        if db.execute("SELECT 1 FROM premium_reminders WHERE user_id=? AND until=? AND stage=?",
                      (uid, until, stage)).fetchone():
            continue
        db.execute("INSERT OR IGNORE INTO premium_reminders (user_id, until, stage) VALUES (?,?,?)",
                   (uid, until, stage))
        db.commit()
        text = REMIND_TEXTS[stage].format(until=fmt_ts(until), free=FREE_HISTORY_DAYS,
                                          once=FREE_ONETIME_PER_DAY)
        try:
            await bot.send_message(uid, text, reply_markup=remind_keyboard())
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    try:
        sent += await check_retention()
    except Exception:
        logging.exception("check_retention")
    return sent

async def reminder_loop():
    while True:
        try:
            n = await check_reminders()
            if n:
                logging.info(f"Напоминания о Premium: отправлено {n}")
        except Exception:
            logging.exception("Ошибка reminder_loop")
        await asyncio.sleep(1800)



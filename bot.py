import asyncio
import logging
import sqlite3
import os
import time
import hashlib
from pathlib import Path
from datetime import timezone, timedelta, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection, Message, BusinessMessagesDeleted,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    BufferedInputFile, LabeledPrice, PreCheckoutQuery
)

BOT_TOKEN = "PUT_YOUR_TOKEN_HERE"
ADMIN_ID = 123456789  # твой Telegram ID (владелец)

TZ = timezone(timedelta(hours=3))
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.5
MAX_CACHE = 20000000

# --- Кэш файлов на диске (скачиваем медиа сразу при получении, как в vestige) ---
MEDIA_DIR = Path("/root/salesbot/media")
CACHE_MEDIA = True              # False — не сохранять файлы на диск
MAX_CACHE_FILE_MB = 20          # больше 20 МБ Bot API скачать не даёт
RETENTION_DAYS = 30             # файлы старше N дней удаляются (0 — не удалять)
MEDIA_MAX_GB = 30               # потолок размера папки с файлами (старые удаляются первыми)
TEXT_RETENTION_DAYS = 30        # тексты в кэше публичных пользователей хранятся N дней
FREE_HISTORY_DAYS = 5           # базовый тариф: удалённые не старше N дней
FREE_ONETIME_PER_DAY = 2        # базовый тариф: одноразовых в день
OWNER_CONTACT = "@your_contact"  # куда писать за покупкой Premium
PREMIUM_PRICE_TEXT = "99 ₽ / месяц"
CLEANUP_INTERVAL_MINUTES = 60   # как часто чистить
DB_PATH = "/root/salesbot/cache.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

business_owners: dict[str, int] = {}
business_owner_names: dict[str, str] = {}

db = sqlite3.connect(DB_PATH)
db.execute("""
    CREATE TABLE IF NOT EXISTS message_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conn_id TEXT, chat_id INTEGER, message_id INTEGER,
        body TEXT, tag TEXT, time_str TEXT, media_type TEXT, file_id TEXT,
        UNIQUE(conn_id, chat_id, message_id)
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS pending_forward (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        manager_label TEXT, tag TEXT, body TEXT, time_str TEXT,
        media_type TEXT, file_id TEXT,
        chat_id INTEGER DEFAULT 0, chat_name TEXT DEFAULT '',
        delivered INTEGER DEFAULT 0
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TEXT,
        monitoring INTEGER DEFAULT 1,
        notifications INTEGER DEFAULT 1,
        track_edits INTEGER DEFAULT 1,
        own_edit_notify INTEGER DEFAULT 0,
        banned_until TEXT DEFAULT NULL
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS bug_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        text TEXT,
        created_at TEXT,
        status TEXT DEFAULT 'open'
    )
""")
db.commit()

for col_def in ["chat_id INTEGER DEFAULT 0", "chat_name TEXT DEFAULT ''", "delivered INTEGER DEFAULT 0"]:
    try:
        db.execute(f"ALTER TABLE pending_forward ADD COLUMN {col_def}")
        db.commit()
    except Exception:
        pass
for col_def in ["media_type TEXT", "file_id TEXT", "file_path TEXT",
                "sent_at INTEGER", "owner_id INTEGER"]:
    try:
        db.execute(f"ALTER TABLE message_cache ADD COLUMN {col_def}")
        db.commit()
    except Exception:
        pass
for col_def in ["monitoring INTEGER DEFAULT 1", "notifications INTEGER DEFAULT 1",
                "track_edits INTEGER DEFAULT 1", "own_edit_notify INTEGER DEFAULT 0",
                "banned_until TEXT DEFAULT NULL", "capture_once INTEGER DEFAULT 1", "premium_until INTEGER DEFAULT 0",
                "trial_used INTEGER DEFAULT 0", "discount_pct INTEGER DEFAULT 0",
                "discount_until INTEGER DEFAULT 0", "discount_code TEXT", "offer_ok_at INTEGER DEFAULT 0"]:
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
        db.commit()
    except Exception:
        pass


db.execute("""
    CREATE TABLE IF NOT EXISTS onetime_usage (
        user_id INTEGER, day TEXT, n INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )
""")
db.execute("CREATE INDEX IF NOT EXISTS idx_mc_sent_at ON message_cache(sent_at)")
db.execute("""
    CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY, kind TEXT, value INTEGER, max_uses INTEGER DEFAULT 0,
        used INTEGER DEFAULT 0, expires_at INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
        created_by INTEGER, created_at INTEGER
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS promo_uses (
        code TEXT, user_id INTEGER, used_at INTEGER, PRIMARY KEY (code, user_id)
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, tariff TEXT,
        days INTEGER, price INTEGER, discount_pct INTEGER DEFAULT 0, promo TEXT,
        status TEXT DEFAULT 'pending', created_at INTEGER, paid_at INTEGER
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS premium_reminders (
        user_id INTEGER, until INTEGER, stage TEXT,
        PRIMARY KEY (user_id, until, stage)
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        user_id INTEGER PRIMARY KEY, referrer_id INTEGER, created_at INTEGER, rewarded INTEGER DEFAULT 0
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS ref_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, amount_kop INTEGER,
        src_user INTEGER, order_id INTEGER, created_at INTEGER, avail_at INTEGER, note TEXT
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_kop INTEGER, details TEXT,
        status TEXT DEFAULT 'pending', created_at INTEGER, done_at INTEGER
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS day3_offers (user_id INTEGER PRIMARY KEY, sent_at INTEGER)
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS faq (
        id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, answer TEXT, pos INTEGER
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS bonus_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'pending',
        created_at INTEGER, done_at INTEGER, days INTEGER
    )
""")
db.commit()
if db.execute("SELECT COUNT(*) FROM faq").fetchone()[0] == 0:
    _seed = [
        ("Как подключить бота?",
         "Настройки → Telegram Business → Чат-боты → выбери этого бота и включи.\n\n"
         "Если пункта нет или Telegram просит Premium: Настройки → «Изм.» (справа сверху) → "
         "«Автоматизация чатов» → впиши @kikitutkofffbot."),
        ("Что даёт Premium?",
         "Полная история удалённых сообщений (до 30 дней) и спасение одноразовых фото и видео без лимита. "
         "Без Premium история — 5 дней, одноразовых — 2 в день."),
        ("Как оплатить?",
         "Кнопка «💎 Premium» → выбери тариф → оплата звёздами Telegram или картой/переводом. "
         "Также можно оплатить с реферального баланса."),
        ("Как вернуть деньги?",
         "Условия возврата: /terms. Если что-то пошло не так — /paysupport или напиши владельцу."),
        ("Как работает реферальная программа?",
         "«👤 Личный кабинет» → «🔗 Реф. ссылка». За каждого нового человека по твоей ссылке — 50 ₽, "
         "плюс 2,5% с его оплат. Вывод от 1000 ₽, выплаты 10-го числа."),
        ("Бот не присылает удалённые сообщения",
         "Проверь: 1) бот подключён в Telegram Business (или через «Автоматизацию чатов»); "
         "2) в боте включён «📡 Мониторинг»; 3) сообщения были написаны уже после подключения. "
         "Не помогло — «🐛 Сообщить о баге»."),
    ]
    for _i, (_q, _a) in enumerate(_seed, 1):
        db.execute("INSERT INTO faq (question, answer, pos) VALUES (?,?,?)", (_q, _a, _i))
    db.commit()
db.execute("""
    CREATE TABLE IF NOT EXISTS testers (user_id INTEGER PRIMARY KEY, added_at INTEGER, added_by INTEGER)
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS export_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, partner TEXT,
        n INTEGER, status TEXT DEFAULT 'pending', created_at INTEGER, done_at INTEGER, file BLOB
    )
""")
db.commit()
for col_def in ["currency TEXT DEFAULT 'RUB'", "stars INTEGER", "charge_id TEXT"]:
    try:
        db.execute(f"ALTER TABLE orders ADD COLUMN {col_def}")
        db.commit()
    except Exception:
        pass
try:
    db.execute("ALTER TABLE promo_codes ADD COLUMN audience TEXT DEFAULT 'all'")
    db.commit()
except Exception:
    pass
db.execute("""
    CREATE TABLE IF NOT EXISTS retention_cfg (
        stage TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, delay_h INTEGER DEFAULT 0,
        pct INTEGER DEFAULT 0, valid_h INTEGER DEFAULT 72, text TEXT, sort INTEGER DEFAULT 0
    )
""")
db.commit()
db.execute("""
    CREATE TABLE IF NOT EXISTS achievements (
        code TEXT PRIMARY KEY, title TEXT, metric TEXT, threshold INTEGER,
        rtype TEXT, rvalue TEXT, active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS user_achievements (
        user_id INTEGER, code TEXT, earned_at INTEGER, reward TEXT,
        PRIMARY KEY (user_id, code)
    )
""")
db.commit()
db.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, kind TEXT,
        text TEXT, status TEXT, created_at INTEGER, updated_at INTEGER, handled_by INTEGER
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS ticket_msgs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, sender TEXT, sender_id INTEGER,
        text TEXT, ts INTEGER
    )
""")
db.commit()
db.execute("""
    CREATE TABLE IF NOT EXISTS biz_conn (
        conn_id TEXT PRIMARY KEY, owner_id INTEGER, enabled INTEGER,
        connected_at INTEGER, disconnected_at INTEGER, first_msg_at INTEGER, last_msg_at INTEGER
    )
""")
db.commit()
db.execute("""
    CREATE TABLE IF NOT EXISTS staff (
        user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at INTEGER
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS admin_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor_id INTEGER,
        action TEXT, target_id INTEGER, details TEXT
    )
""")
db.commit()


# ---------- Утилиты ----------

def get_setting(key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()

def del_setting(key):
    db.execute("DELETE FROM settings WHERE key=?", (key,))
    db.commit()

def get_user(user_id):
    return db.execute(
        "SELECT monitoring, notifications, track_edits, own_edit_notify, banned_until FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

def user_monitoring_on(user_id) -> bool:
    row = get_user(user_id)
    return row[0] == 1 if row else True

def user_notifications_on(user_id) -> bool:
    row = get_user(user_id)
    return row[1] == 1 if row else True

def user_track_edits_on(user_id) -> bool:
    row = get_user(user_id)
    return row[2] == 1 if row else True

def user_own_edit_notify_on(user_id) -> bool:
    row = get_user(user_id)
    return row[3] == 1 if row else False

# ---------- Тарифы: базовый / Premium ----------

_limit_notified: dict = {}

def now_ts() -> int:
    return int(time.time())

def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%y %H:%M")

def get_premium_until(user_id) -> int:
    row = db.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    return int(row[0]) if row and row[0] else 0

def is_premium(user_id) -> bool:
    if user_id == ADMIN_ID:
        return True
    if is_tester(user_id):
        return True
    return get_premium_until(user_id) > now_ts()

def grant_premium(user_id, days) -> int:
    ensure_user(user_id)
    base = max(now_ts(), get_premium_until(user_id))
    until = base + int(days) * 86400
    db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, user_id))
    db.commit()
    return until

def revoke_premium(user_id):
    db.execute("UPDATE users SET premium_until=0 WHERE user_id=?", (user_id,))
    db.commit()

def resolve_user_id(token):
    t = (token or "").strip()
    if t.lstrip("-").isdigit():
        return int(t)
    row = db.execute("SELECT user_id FROM users WHERE lower(username)=?",
                     (t.lstrip("@").lower(),)).fetchone()
    return row[0] if row else None

def msg_age_days(cached):
    """Возраст сообщения в днях (от времени отправки). None — неизвестно."""
    sent = cached.get("sent_at")
    if not sent:
        try:
            dt = datetime.strptime(cached.get("time") or "", "%H:%M. %d.%m.%y").replace(tzinfo=TZ)
            sent = int(dt.timestamp())
        except Exception:
            return None
    return (now_ts() - sent) / 86400

def history_allowed(user_id, cached) -> bool:
    """Можно ли показать это удалённое сообщение по тарифу пользователя."""
    if is_premium(user_id):
        return True
    age = msg_age_days(cached)
    return age is None or age <= FREE_HISTORY_DAYS

def history_teaser(cached, chat_label=None) -> str:
    where = f"В чате с {chat_label} удалено" if chat_label else "Удалено"
    return (f"🗑 {where} сообщение, но оно старше {FREE_HISTORY_DAYS} дн. ({cached.get('time')}).\n"
            f"Полная история удалённых — в 💎 Premium (кнопка в меню).")

def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def onetime_used_today(user_id) -> int:
    row = db.execute("SELECT n FROM onetime_usage WHERE user_id=? AND day=?",
                     (user_id, today_key())).fetchone()
    return row[0] if row else 0

def onetime_left(user_id):
    """None — безлимит (Premium); иначе сколько одноразовых осталось сегодня."""
    if is_premium(user_id):
        return None
    return max(0, FREE_ONETIME_PER_DAY - onetime_used_today(user_id))

def onetime_register(user_id):
    db.execute(
        "INSERT INTO onetime_usage (user_id, day, n) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET n = n + 1",
        (user_id, today_key()))
    db.commit()

def tariff_status_text(user_id) -> str:
    if user_id == ADMIN_ID:
        return "💎 Тариф: владелец (без ограничений)"
    if is_tester(user_id):
        return "🧪 Статус: тестер (все функции Premium, ранний доступ к новому)"
    if is_premium(user_id):
        return f"💎 Тариф: Premium до {fmt_ts(get_premium_until(user_id))}\n🔥 Одноразовые: без лимита"
    return (f"🆓 Тариф: базовый\n"
            f"🗑 История удалённых: до {FREE_HISTORY_DAYS} дн.\n"
            f"🔥 Одноразовые сегодня: {onetime_used_today(user_id)}/{FREE_ONETIME_PER_DAY}")

def premium_info_text(user_id) -> str:
    head = tariff_status_text(user_id)
    return (f"{head}\n\n"
            f"💎 Premium — {PREMIUM_PRICE_TEXT}\n"
            f"• удалённые сообщения за всё время хранения (до {TEXT_RETENTION_DAYS} дн.), "
            f"а не только за {FREE_HISTORY_DAYS} дн.\n"
            f"• спасение одноразовых без лимита (базовый: {FREE_ONETIME_PER_DAY} в день)\n\n"
            f"{tariffs_text(user_id)}\n\nВыбери тариф кнопкой ниже 👇\nВопросы по оплате: {OWNER_CONTACT}")

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
            async with s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/refundStarPayment",
                              json={"user_id": uid, "telegram_payment_charge_id": charge_id}) as r:
                data = await r.json()
                return bool(data.get("ok"))
    except Exception:
        logging.exception("Ошибка возврата звёзд")
        return False

def terms_text() -> str:
    return (f"📄 Условия\n\n"
            f"• Premium — цифровая услуга на оплаченный срок: полная история удалённых сообщений "
            f"(до {TEXT_RETENTION_DAYS} дн.) и спасение одноразовых медиа без лимита.\n"
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

OFFER_PATH = Path("/root/salesbot/offer.txt")  # текст оферты лежит вне репозитория
try:
    OFFER_TEXT = OFFER_PATH.read_text(encoding="utf-8").strip()
except Exception:
    OFFER_TEXT = "Текст публичной оферты: положи его в файл offer.txt."

def offer_ok(uid) -> bool:
    """Нажал ли пользователь «Согласен с офертой»."""
    r = db.execute("SELECT COALESCE(offer_ok_at,0) FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0])

def offer_set_ok(uid):
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    db.execute("UPDATE users SET offer_ok_at=? WHERE user_id=?", (now_ts(), uid))
    db.commit()

def offer_chunks(limit=3800):
    parts, cur = [], ""
    for para in OFFER_TEXT.split("\n\n"):
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
          "полную историю удалённых и одноразовые без лимита.",
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


# ---------- Роли: владелец / админ ----------

STAFF_MAX_GRANT_DAYS = 30   # админ может выдать Premium максимум на столько дней за раз

def is_owner(uid) -> bool:
    return uid == ADMIN_ID

def is_staff(uid) -> bool:
    return db.execute("SELECT 1 FROM staff WHERE user_id=?", (uid,)).fetchone() is not None

def has_panel(uid) -> bool:
    return is_owner(uid) or is_staff(uid)

def staff_add(uid, by):
    db.execute("INSERT OR REPLACE INTO staff (user_id, added_by, added_at) VALUES (?,?,?)",
               (uid, by, now_ts()))
    db.commit()

def staff_del(uid):
    db.execute("DELETE FROM staff WHERE user_id=?", (uid,))
    db.commit()

def log_action(actor, action, target=None, details=""):
    db.execute("INSERT INTO admin_log (ts, actor_id, action, target_id, details) VALUES (?,?,?,?,?)",
               (now_ts(), actor, action, target, details))
    db.commit()

def role_line(uid) -> str:
    return _role_line_base(uid) + badge_line(uid)

def _role_line_base(uid) -> str:
    if is_owner(uid):
        return "👑 Роль: владелец\n"
    if is_staff(uid):
        return "👮 Роль: админ\n"
    if is_tester(uid):
        return "🧪 Роль: тестер\n"
    return ""

def user_label(uid) -> str:
    row = db.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    return f"@{row[0]} ({uid})" if row and row[0] else f"id{uid}"

def user_card_text(uid) -> str:
    row = db.execute("SELECT username, first_seen FROM users WHERE user_id=?", (uid,)).fetchone()
    uname = f"@{row[0]}" if row and row[0] else "—"
    seen = row[1] if row else "—"
    if is_owner(uid):
        tariff = "владелец"
    elif is_premium(uid):
        tariff = f"Premium до {fmt_ts(get_premium_until(uid))}"
    else:
        tariff = "базовый"
    role = "владелец" if is_owner(uid) else ("админ" if is_staff(uid) else ("тестер" if is_tester(uid) else "пользователь"))
    banned, _ = is_user_banned(uid)
    return (f"👤 {uname}\nID: {uid}\nРоль: {role}\nТариф: {tariff}\n"
            f"В базе с: {seen}\nБан: {'да' if banned else 'нет'}")

GRANT_CHOICES = [(7, "7 дн"), (15, "15 дн"), (30, "30 дн"),
                 (90, "90 дн · 3 мес"), (180, "180 дн · полгода"), (365, "365 дн · год")]

def grant_keyboard(target, actor) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for days, label in GRANT_CHOICES:
        if days > STAFF_MAX_GRANT_DAYS and not is_owner(actor):
            continue
        row.append(InlineKeyboardButton(text=f"+{label}", callback_data=f"pg:{target}:{days}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if is_owner(actor):
        rows.append([InlineKeyboardButton(text="✏️ Своё число дней", callback_data=f"pg:{target}:custom")])
        if is_premium(target) and not is_owner(target):
            rows.append([InlineKeyboardButton(text="🚫 Забрать Premium", callback_data=f"pr:{target}")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="pg:close:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def owner_panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📢 Рассылка"),       KeyboardButton(text="🔴🟢 Техработы")],
        [KeyboardButton(text="📊 Статистика"),     KeyboardButton(text="📋 Заявки")],
        [KeyboardButton(text="💎 Выдать Premium"), KeyboardButton(text="💎 Список Premium")],
        [KeyboardButton(text="👥 Админы"),         KeyboardButton(text="📜 Журнал")],
        [KeyboardButton(text="🧾 Заказы"),         KeyboardButton(text="🎟 Промокоды")],
        [KeyboardButton(text="🩺 Состояние"),      KeyboardButton(text="🔎 Кабинет юзера")],
        [KeyboardButton(text="❓ Редактор FAQ"), KeyboardButton(text="🧪 Тестеры")],
        [KeyboardButton(text="💬 Обращения"),      KeyboardButton(text="💡 Идеи")],
        [KeyboardButton(text="🏆 Редактор достижений"), KeyboardButton(text="🔁 Удержание")],
        [KeyboardButton(text="📥 Экспорт"),        KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def staff_panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💎 Выдать Premium")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📋 Заявки")],
        [KeyboardButton(text="🎟 Промокоды"), KeyboardButton(text="🔎 Кабинет юзера")],
        [KeyboardButton(text="🩺 Состояние")],
        [KeyboardButton(text="💬 Обращения"), KeyboardButton(text="💡 Идеи")],
        [KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def panel_kb(uid) -> ReplyKeyboardMarkup:
    return owner_panel_keyboard() if is_owner(uid) else staff_panel_keyboard()

def premium_list_text() -> str:
    rows = db.execute(
        "SELECT user_id, username, premium_until FROM users WHERE premium_until > ? "
        "ORDER BY premium_until ASC LIMIT 60", (now_ts(),)).fetchall()
    if not rows:
        return "Активных Premium нет."
    lines = [f"{uid} {('@' + un) if un else ''} — до {fmt_ts(pu)}" for uid, un, pu in rows]
    return f"💎 Активных Premium: {len(rows)}\n\n" + "\n".join(lines)

def staff_panel():
    rows = db.execute(
        "SELECT s.user_id, u.username FROM staff s LEFT JOIN users u ON u.user_id=s.user_id "
        "ORDER BY s.added_at").fetchall()
    if rows:
        text = "👥 Админы:\n\n" + "\n".join(
            f"{uid} {('@' + un) if un else ''}" for uid, un in rows)
    else:
        text = "👥 Админов пока нет."
    kb = [[InlineKeyboardButton(text=f"❌ Снять {('@' + un) if un else uid}",
                                callback_data=f"st:del:{uid}")] for uid, un in rows]
    kb.append([InlineKeyboardButton(text="➕ Назначить админа", callback_data="st:add")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)

def journal_text() -> str:
    rows = db.execute(
        "SELECT ts, actor_id, action, target_id, details FROM admin_log ORDER BY id DESC LIMIT 30"
    ).fetchall()
    if not rows:
        return "Журнал пока пуст."
    names = {"grant": "выдал Premium", "revoke": "забрал Premium",
             "staff_add": "назначил админом", "staff_del": "снял админа",
             "order_paid": "подтвердил оплату", "order_no": "отклонил заказ",
             "promo_new": "создал промокод", "promo_off": "отключил промокод",
             "stars_paid": "оплатил звёздами", "refund": "вернул платёж",
             "ticket_reply": "ответил на обращение", "ticket_close": "закрыл обращение",
             "idea_acc": "принял идею", "idea_rej": "отклонил идею",
             "ach_edit": "изменил достижения", "promo_on": "включил промокод", "rt_edit": "изменил удержание"}
    lines = [f"{fmt_ts(ts)} · {user_label(a)} {names.get(act, act)} → "
             f"{user_label(t) if t else '—'} {d or ''}".strip()
             for ts, a, act, t, d in rows]
    return "📜 Последние действия:\n\n" + "\n".join(lines)

async def do_grant(actor, target, days):
    """Выдаёт Premium, пишет в журнал, уведомляет получателя и (если выдал админ) владельца."""
    until = grant_premium(target, days)
    log_action(actor, "grant", target, f"{days} дн")
    try:
        await bot.send_message(target, f"💎 Тебе выдан Premium до {fmt_ts(until)}. Спасибо!")
    except Exception:
        pass
    if not is_owner(actor):
        try:
            await bot.send_message(
                ADMIN_ID, f"🔔 Админ {user_label(actor)} выдал Premium {user_label(target)} "
                          f"на {days} дн (до {fmt_ts(until)}).")
        except Exception:
            pass
    return until

def user_capture_once_on(user_id) -> bool:
    row = db.execute("SELECT capture_once FROM users WHERE user_id=?", (user_id,)).fetchone()
    return (row[0] != 0) if row and row[0] is not None else True

def set_user_capture_once(user_id, val: int):
    db.execute("UPDATE users SET capture_once=? WHERE user_id=?", (val, user_id))
    db.commit()

def is_user_banned(user_id) -> tuple:
    """Возвращает (забанен: bool, сообщение: str)"""
    if user_id == ADMIN_ID:
        return False, ""
    row = get_user(user_id)
    if not row or row[4] is None:
        return False, ""
    banned_until = row[4]
    if banned_until == "forever":
        return True, "⛔ Ты заблокирован навсегда."
    try:
        ban_dt = datetime.fromisoformat(banned_until)
        if ban_dt.tzinfo is None:
            ban_dt = ban_dt.replace(tzinfo=TZ)
        now = datetime.now(TZ)
        if now < ban_dt:
            return True, f"⛔ Ты заблокирован до {ban_dt.strftime('%H:%M %d.%m.%y')}."
        else:
            db.execute("UPDATE users SET banned_until=NULL WHERE user_id=?", (user_id,))
            db.commit()
            return False, ""
    except Exception:
        return False, ""

def ban_user(user_id, hours: int = None):
    if hours is None:
        db.execute("UPDATE users SET banned_until='forever' WHERE user_id=?", (user_id,))
    else:
        ban_until = (datetime.now(TZ) + timedelta(hours=hours)).isoformat()
        db.execute("UPDATE users SET banned_until=? WHERE user_id=?", (ban_until, user_id))
    db.commit()

def unban_user(user_id):
    db.execute("UPDATE users SET banned_until=NULL WHERE user_id=?", (user_id,))
    db.commit()

def set_user_monitoring(user_id, val: int):
    db.execute("UPDATE users SET monitoring=? WHERE user_id=?", (val, user_id))
    db.commit()

def set_user_notifications(user_id, val: int):
    db.execute("UPDATE users SET notifications=? WHERE user_id=?", (val, user_id))
    db.commit()

def set_user_track_edits(user_id, val: int):
    db.execute("UPDATE users SET track_edits=? WHERE user_id=?", (val, user_id))
    db.commit()

def set_user_own_edit_notify(user_id, val: int):
    db.execute("UPDATE users SET own_edit_notify=? WHERE user_id=?", (val, user_id))
    db.commit()

def ensure_user(user_id, username="") -> bool:
    existing = db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
    if existing:
        return False
    _gift = int(globals().get("NEW_USER_PREMIUM_DAYS", 0))
    db.execute(
        "INSERT INTO users (user_id, username, first_seen, monitoring, notifications, track_edits, own_edit_notify, "
        "premium_until) VALUES (?, ?, datetime('now'), 1, 1, 1, 0, ?)",
        (user_id, username, (int(time.time()) + _gift * 86400) if _gift > 0 else 0)
    )
    db.commit()
    return True

def add_bug_report(user_id, username, text):
    db.execute(
        "INSERT INTO bug_reports (user_id, username, text, created_at, status) "
        "VALUES (?, ?, ?, datetime('now'), 'open')",
        (user_id, username, text)
    )
    db.commit()

def get_open_bug_reports():
    return db.execute(
        "SELECT id, user_id, username, text, created_at FROM bug_reports "
        "WHERE status='open' ORDER BY id DESC"
    ).fetchall()

def close_bug_report(report_id):
    db.execute("UPDATE bug_reports SET status='closed' WHERE id=?", (report_id,))
    db.commit()

def fmt_user(user) -> str:
    if user is None: return "неизвестно"
    if hasattr(user, "username") and user.username: return f"@{user.username}"
    if hasattr(user, "full_name") and user.full_name: return user.full_name
    if hasattr(user, "first_name") and user.first_name: return user.first_name
    return f"id{user.id}"

def fmt_time(dt) -> str:
    return dt.astimezone(TZ).strftime("%H:%M. %d.%m.%y")

def build_block(tag, body, time_str, manager_label=None) -> str:
    prefix = f"[{manager_label}]\n" if manager_label else ""
    return f"{prefix}{tag}\n{body}\n({time_str})"

def get_owner_name(conn_id) -> str:
    return business_owner_names.get(conn_id, "Неизвестно")

def cache_put(conn_id, chat_id, message_id, body, tag, time_str, media_type=None, file_id=None,
              file_path=None, sent_at=None, owner_id=None):
    db.execute(
        "INSERT OR REPLACE INTO message_cache "
        "(conn_id, chat_id, message_id, body, tag, time_str, media_type, file_id, file_path, "
        "sent_at, owner_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (conn_id, chat_id, message_id, body, tag, time_str, media_type, file_id, file_path,
         sent_at, owner_id),
    )
    db.commit()
    count = db.execute("SELECT COUNT(*) FROM message_cache").fetchone()[0]
    if count > MAX_CACHE:
        db.execute("DELETE FROM message_cache WHERE id IN "
                   "(SELECT id FROM message_cache ORDER BY id ASC LIMIT ?)", (count - MAX_CACHE,))
        db.commit()

def cache_get(conn_id, chat_id, message_id):
    row = db.execute(
        "SELECT body, tag, time_str, media_type, file_id, file_path, sent_at, owner_id "
        "FROM message_cache WHERE conn_id=? AND chat_id=? AND message_id=?",
        (conn_id, chat_id, message_id),
    ).fetchone()
    return {"body": row[0], "tag": row[1], "time": row[2],
            "media_type": row[3], "file_id": row[4], "file_path": row[5],
            "sent_at": row[6], "owner_id": row[7]} if row else None

def cache_set_path(conn_id, chat_id, message_id, path):
    db.execute(
        "UPDATE message_cache SET file_path=? WHERE conn_id=? AND chat_id=? AND message_id=?",
        (path, conn_id, chat_id, message_id),
    )
    db.commit()

def cache_clear_path_by_file(path):
    db.execute("UPDATE message_cache SET file_path=NULL WHERE file_path=?", (path,))
    db.commit()

def release_cached_file(conn_id, chat_id, message_id, cached):
    """Файл уже доставлен (или не нужен): удаляем его с диска и чистим путь в кэше."""
    if not cached or not cached.get("file_path"):
        return
    try:
        if os.path.exists(cached["file_path"]):
            os.remove(cached["file_path"])
    except Exception:
        logging.exception("Не удалось удалить файл из кэша")
    cache_set_path(conn_id, chat_id, message_id, None)

def cache_get_context(chat_id, before_id, limit):
    rows = db.execute(
        "SELECT tag, body, time_str FROM message_cache "
        "WHERE chat_id=? AND id < ? ORDER BY id DESC LIMIT ?",
        (chat_id, before_id, limit),
    ).fetchall()
    return list(reversed(rows))

def pending_add(manager_label, tag, body, time_str,
                media_type=None, file_id=None, chat_id=0, chat_name=""):
    db.execute(
        "INSERT INTO pending_forward "
        "(manager_label, tag, body, time_str, media_type, file_id, chat_id, chat_name, delivered) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (manager_label, tag, body, time_str, media_type, file_id, chat_id, chat_name),
    )
    db.commit()
    count = db.execute("SELECT COUNT(*) FROM pending_forward").fetchone()[0]
    if count > MAX_CACHE:
        db.execute("DELETE FROM pending_forward WHERE id IN "
                   "(SELECT id FROM pending_forward ORDER BY id ASC LIMIT ?)", (count - MAX_CACHE,))
        db.commit()

def pending_chats_all():
    return db.execute(
        "SELECT DISTINCT chat_id, chat_name, manager_label FROM pending_forward "
        "WHERE chat_id != 0 ORDER BY id ASC"
    ).fetchall()

def pending_count_unread_for_chat(chat_id):
    return db.execute(
        "SELECT COUNT(*) FROM pending_forward WHERE chat_id=? AND delivered=0", (chat_id,)
    ).fetchone()[0]

def pending_count_total_for_chat(chat_id):
    return db.execute(
        "SELECT COUNT(*) FROM pending_forward WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]

def pending_get_unread_for_chat(chat_id):
    return db.execute(
        "SELECT id, manager_label, tag, body, time_str, media_type, file_id "
        "FROM pending_forward WHERE chat_id=? AND delivered=0 ORDER BY id ASC",
        (chat_id,),
    ).fetchall()

def pending_get_last_for_chat(chat_id, limit):
    rows = db.execute(
        "SELECT id, manager_label, tag, body, time_str, media_type, file_id "
        "FROM pending_forward WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return list(reversed(rows))

def pending_mark_delivered_chat(chat_id):
    db.execute("UPDATE pending_forward SET delivered=1 WHERE chat_id=?", (chat_id,))
    db.commit()

def pending_mark_delivered_all():
    db.execute("UPDATE pending_forward SET delivered=1")
    db.commit()

def pending_count_total():
    return db.execute("SELECT COUNT(*) FROM pending_forward").fetchone()[0]


# ---------- Клавиатуры ----------

def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📡 Мониторинг"), KeyboardButton(text="🔔 Уведомления")],
        [KeyboardButton(text="ℹ️ Мой статус"), KeyboardButton(text="🔧 Админ панель")],
        [KeyboardButton(text="💬 Обратная связь"), KeyboardButton(text="💎 Premium")],
        [KeyboardButton(text="🎟 Промокод"), KeyboardButton(text="❓ FAQ")],
        [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="📥 Экспорт")],
    ], resize_keyboard=True)

def monitoring_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    mon_btn  = "❌ Выключить мониторинг" if user_monitoring_on(user_id) else "✅ Включить мониторинг"
    edit_btn = "✏️ Выключить отслеживание правок" if user_track_edits_on(user_id) else "✏️ Включить отслеживание правок"
    own_btn  = "🔕 Выкл. уведомления о своих правках" if user_own_edit_notify_on(user_id) else "🔔 Вкл. уведомления о своих правках"
    once_btn = "🔥 Выкл. спасение одноразовых" if user_capture_once_on(user_id) else "🔥 Вкл. спасение одноразовых"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=mon_btn)],
        [KeyboardButton(text=edit_btn)],
        [KeyboardButton(text=own_btn)],
        [KeyboardButton(text=once_btn)],
        [KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def notifications_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔔 Включить уведомления")],
        [KeyboardButton(text="🔕 Выключить уведомления")],
        [KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📢 Рассылка"),    KeyboardButton(text="🔴🟢 Техработы")],
        [KeyboardButton(text="📊 Статистика"),  KeyboardButton(text="📋 Заявки")],
        [KeyboardButton(text="📥 Экспорт"),     KeyboardButton(text="← Назад")],
    ], resize_keyboard=True)

def tech_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 НАЧАЛИСЬ ТЕХНИЧЕСКИЕ РАБОТЫ!", callback_data="broadcast:tech_start")],
        [InlineKeyboardButton(text="🟢 ТЕХНИЧЕСКИЕ РАБОТЫ ЗАВЕРШЕНЫ!", callback_data="broadcast:tech_end")],
    ])

def bug_report_keyboard(report_id: int, user_id: int, staff_only=False) -> InlineKeyboardMarkup:
    if staff_only:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Закрыть", callback_data=f"bug:close:{report_id}:{user_id}")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Закрыть",    callback_data=f"bug:close:{report_id}:{user_id}"),
            InlineKeyboardButton(text="⏱ Бан 1ч",    callback_data=f"bug:ban1:{report_id}:{user_id}"),
        ],
        [
            InlineKeyboardButton(text="⏱ Бан 24ч",   callback_data=f"bug:ban24:{report_id}:{user_id}"),
            InlineKeyboardButton(text="⏱ Бан 7д",    callback_data=f"bug:ban168:{report_id}:{user_id}"),
        ],
        [
            InlineKeyboardButton(text="💣 Перм. бан", callback_data=f"bug:banperm:{report_id}:{user_id}"),
            InlineKeyboardButton(text="🔓 Разбан",    callback_data=f"bug:unban:{report_id}:{user_id}"),
        ],
    ])

def export_keyboard() -> InlineKeyboardMarkup:
    chats = db.execute("""
        SELECT mc.chat_id,
               COALESCE(
                   (SELECT pf.chat_name FROM pending_forward pf
                    WHERE pf.chat_id = mc.chat_id AND pf.chat_name != '' LIMIT 1),
                   'id' || mc.chat_id
               ) as chat_name,
               COUNT(*) as msg_count
        FROM message_cache mc
        GROUP BY mc.chat_id
        ORDER BY msg_count DESC
        LIMIT 20
    """).fetchall()
    if not chats:
        return None
    buttons = []
    for chat_id, chat_name, msg_count in chats:
        buttons.append([InlineKeyboardButton(
            text=f"{chat_name} ({msg_count} сообщ.)",
            callback_data=f"exp:{chat_id}"
        )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="exp:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- Медиа ----------

KNOWN_MEDIA_TYPES = ("photo", "document", "video", "animation",
                     "sticker", "audio", "voice", "video_note")

def has_known_media(message):
    return any(getattr(message, t, None) for t in KNOWN_MEDIA_TYPES)

def get_media_type_and_file_id(message):
    if message.photo:      return "photo",      message.photo[-1].file_id
    if message.document:   return "document",   message.document.file_id
    if message.video:      return "video",       message.video.file_id
    if message.animation:  return "animation",   message.animation.file_id
    if message.sticker:    return "sticker",     message.sticker.file_id
    if message.audio:      return "audio",       message.audio.file_id
    if message.voice:      return "voice",       message.voice.file_id
    if message.video_note: return "video_note",  message.video_note.file_id
    return None, None

def get_body_and_tag(message, owner_id):
    sender = message.from_user
    is_outgoing = sender is not None and sender.id == owner_id
    tag = f"(❗❗ты → {fmt_user(message.chat)})" if is_outgoing else f"({fmt_user(sender)})"
    forward_mark = "📨 " if getattr(message, "forward_origin", None) else ""
    if message.voice:        body = forward_mark + "🎤 Голосовое"
    elif message.video_note: body = forward_mark + "⭕ Кружок"
    elif message.text or message.caption: body = forward_mark + (message.text or message.caption)
    elif has_known_media(message): body = forward_mark + "[медиа]"
    elif message.location:
        if getattr(message.location, 'live_period', None):
            body = forward_mark + "📍 Трансляция геопозиции"
        else:
            body = forward_mark + "📍 Геопозиция"
    elif message.venue:
        body = forward_mark + "📍 Место: " + message.venue.title
    else: body = "🙈 Скрытое медиа"
    return body, tag

async def send_media_by_file_id(message, target_chat_id, caption=None):
    if message.photo:        await bot.send_photo(target_chat_id, message.photo[-1].file_id, caption=caption)
    elif message.document:   await bot.send_document(target_chat_id, message.document.file_id, caption=caption)
    elif message.video:      await bot.send_video(target_chat_id, message.video.file_id, caption=caption)
    elif message.animation:  await bot.send_animation(target_chat_id, message.animation.file_id, caption=caption)
    elif message.sticker:    await bot.send_sticker(target_chat_id, message.sticker.file_id)
    elif message.audio:      await bot.send_audio(target_chat_id, message.audio.file_id, caption=caption)
    elif message.voice:      await bot.send_voice(target_chat_id, message.voice.file_id)
    elif message.video_note: await bot.send_video_note(target_chat_id, message.video_note.file_id)
    else: raise ValueError("Неизвестный тип медиа")

async def send_media_by_type(media_type, file_id, target_chat_id, caption=None):
    if media_type == "photo":        await bot.send_photo(target_chat_id, file_id, caption=caption)
    elif media_type == "document":   await bot.send_document(target_chat_id, file_id, caption=caption)
    elif media_type == "video":      await bot.send_video(target_chat_id, file_id, caption=caption)
    elif media_type == "animation":  await bot.send_animation(target_chat_id, file_id, caption=caption)
    elif media_type == "sticker":    await bot.send_sticker(target_chat_id, file_id)
    elif media_type == "audio":      await bot.send_audio(target_chat_id, file_id, caption=caption)
    elif media_type == "voice":      await bot.send_voice(target_chat_id, file_id)
    elif media_type == "video_note": await bot.send_video_note(target_chat_id, file_id)

MEDIA_EXT = {"voice": "ogg", "video_note": "mp4", "video": "mp4", "audio": "mp3",
             "photo": "jpg", "document": "bin", "animation": "mp4", "sticker": "webp"}

# Поля, которыми Telegram может помечать самоуничтожающиеся медиа (стабильного флага в Bot API нет)
ONE_TIME_FLAGS = ("view_once", "is_one_time", "one_time", "self_destruct", "ttl_seconds")

_bg_tasks: set = set()
_rescued: set = set()
_dl_sem = asyncio.Semaphore(4)


def get_media_size(message):
    obj = None
    if message.photo:
        obj = message.photo[-1]
    else:
        for t in ("document", "video", "animation", "sticker", "audio", "voice", "video_note"):
            obj = getattr(message, t, None)
            if obj:
                break
    return getattr(obj, "file_size", None) if obj else None

async def download_raw(file_id):
    """Скачивает файл из Telegram в память. None — если не вышло."""
    try:
        tg_file = await bot.get_file(file_id)
        data = await bot.download_file(tg_file.file_path)
        return data.read() if data else None
    except Exception as e:
        logging.warning(f"Не удалось скачать файл из Telegram: {e}")
        return None

async def send_buffered(media_type, target_chat_id, buf):
    if media_type == "voice":        await bot.send_voice(target_chat_id, buf)
    elif media_type == "video_note": await bot.send_video_note(target_chat_id, buf)
    elif media_type == "photo":      await bot.send_photo(target_chat_id, buf)
    elif media_type == "video":      await bot.send_video(target_chat_id, buf)
    elif media_type == "animation":  await bot.send_animation(target_chat_id, buf)
    elif media_type == "audio":      await bot.send_audio(target_chat_id, buf)
    else:                            await bot.send_document(target_chat_id, buf)

async def try_resend_media(media_type, file_id, target_chat_id, file_path=None) -> bool:
    """Отправляет файл получателю, пробуя по очереди:
    1) по file_id; 2) файл с диска; 3) скачать из Telegram и загрузить заново;
    4) как обычный документ — если у получателя запрет на голосовые/кружки
       (ошибка VOICE_MESSAGES_FORBIDDEN) или иная причина отказа."""
    forbidden = False
    if file_id:
        try:
            await send_media_by_type(media_type, file_id, target_chat_id)
            return True
        except Exception as e:
            forbidden = "VOICE_MESSAGES_FORBIDDEN" in str(e).upper()
            logging.warning(f"{media_type}: отправка по file_id не удалась ({e})")

    raw = None
    suffix = "." + MEDIA_EXT.get(media_type, "bin")
    if file_path and os.path.exists(file_path):
        try:
            raw = await asyncio.to_thread(Path(file_path).read_bytes)
            suffix = Path(file_path).suffix or suffix
        except Exception:
            logging.exception("Не удалось прочитать файл из кэша")
    if raw is None and file_id:
        raw = await download_raw(file_id)
    if raw is None:
        logging.warning(f"{media_type}: файла нет ни на диске, ни у Telegram")
        return False

    name = f"deleted{suffix}"
    if not forbidden:
        try:
            await send_buffered(media_type, target_chat_id, BufferedInputFile(raw, filename=name))
            return True
        except Exception as e:
            forbidden = "VOICE_MESSAGES_FORBIDDEN" in str(e).upper()
            logging.warning(f"{media_type}: повторная загрузка не удалась ({e}), шлю документом")
    try:
        await bot.send_document(target_chat_id, BufferedInputFile(raw, filename=name))
        return True
    except Exception:
        logging.exception(f"Не удалось отправить {media_type} даже документом")
        return False

async def deliver_deleted_media(cached, target_chat_id) -> str:
    """Присылает удалённый файл. Возвращает пустую строку или предупреждение для текста."""
    if not cached or not cached.get("media_type"):
        return ""
    if not (cached.get("file_id") or cached.get("file_path")):
        return ""
    ok = await try_resend_media(cached["media_type"], cached.get("file_id"),
                                target_chat_id, cached.get("file_path"))
    return "" if ok else "\n⚠️ Файл не удалось прислать (Telegram не отдал его боту)"

async def cache_media_on_arrival(conn_id, chat_id, message_id, media_type, file_id, file_size):
    """Скачивает файл на диск сразу при получении — переживёт инвалидацию file_id."""
    if file_size and file_size > MAX_CACHE_FILE_MB * 1024 * 1024:
        return
    try:
        async with _dl_sem:
            tg_file = await bot.get_file(file_id)
            if not tg_file.file_path:
                return
            suffix = Path(tg_file.file_path).suffix or "." + MEDIA_EXT.get(media_type, "bin")
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha1(f"{conn_id}:{chat_id}:{message_id}:{file_id}".encode()).hexdigest()[:16]
            target = MEDIA_DIR / f"{key}{suffix}"
            if not target.exists():
                await bot.download_file(tg_file.file_path, destination=target)
        cache_set_path(conn_id, chat_id, message_id, str(target))
    except Exception as e:
        logging.warning(f"Не удалось сохранить файл на диск ({media_type}): {e}")

def schedule_media_cache(conn_id, chat_id, message_id, media_type, file_id, file_size):
    if not (CACHE_MEDIA and media_type and file_id):
        return
    task = asyncio.create_task(
        cache_media_on_arrival(conn_id, chat_id, message_id, media_type, file_id, file_size))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

def cleanup_old_media() -> int:
    if RETENTION_DAYS <= 0 or not MEDIA_DIR.exists():
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for f in MEDIA_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                cache_clear_path_by_file(str(f))
                removed += 1
        except Exception:
            logging.exception("Ошибка очистки кэша файлов")
    return removed

def enforce_media_cap() -> int:
    """Если папка с файлами больше потолка — удаляем самые старые."""
    if not MEDIA_DIR.exists():
        return 0
    files = []
    for f in MEDIA_DIR.iterdir():
        try:
            if f.is_file():
                st = f.stat()
                files.append((st.st_mtime, st.st_size, f))
        except Exception:
            pass
    total = sum(s for _, s, _ in files)
    cap = MEDIA_MAX_GB * 1024 ** 3
    if total <= cap:
        return 0
    files.sort(key=lambda x: x[0])
    removed = 0
    for _, size, f in files:
        if total <= cap * 0.9:
            break
        try:
            f.unlink()
            cache_clear_path_by_file(str(f))
            total -= size
            removed += 1
        except Exception:
            logging.exception("Ошибка удаления по потолку размера")
    return removed

def cleanup_old_messages() -> int:
    """Удаляет тексты старше TEXT_RETENTION_DAYS у публичных пользователей.
    Данные владельца и менеджеров (журнал, экспорт) не трогаем."""
    if TEXT_RETENTION_DAYS <= 0:
        return 0
    cutoff = now_ts() - TEXT_RETENTION_DAYS * 86400
    protected = [ADMIN_ID]
    marks = ",".join("?" * len(protected))
    cur = db.execute(
        f"DELETE FROM message_cache WHERE sent_at > 0 AND sent_at < ? "
        f"AND owner_id IS NOT NULL AND owner_id NOT IN ({marks})",
        (cutoff, *protected))
    db.commit()
    return cur.rowcount

async def backfill_cache_meta():
    """Старым записям кэша проставляем sent_at (из строки времени) и owner_id (по подключению)."""
    while True:
        rows = db.execute(
            "SELECT id, time_str FROM message_cache WHERE sent_at IS NULL LIMIT 5000").fetchall()
        if not rows:
            break
        upd = []
        for rid, ts in rows:
            try:
                v = int(datetime.strptime(ts or "", "%H:%M. %d.%m.%y").replace(tzinfo=TZ).timestamp())
            except Exception:
                v = 0
            upd.append((v, rid))
        db.executemany("UPDATE message_cache SET sent_at=? WHERE id=?", upd)
        db.commit()
        await asyncio.sleep(0.05)
    conns = db.execute(
        "SELECT DISTINCT conn_id FROM message_cache WHERE owner_id IS NULL").fetchall()
    for (cid,) in conns:
        try:
            oid = await get_owner_id(cid)
        except Exception:
            oid = None
        if oid:
            db.execute("UPDATE message_cache SET owner_id=? WHERE conn_id=? AND owner_id IS NULL",
                       (oid, cid))
            db.commit()

async def janitor_loop():
    while True:
        try:
            n = cleanup_old_media()
            if n:
                logging.info(f"Очистка кэша файлов: удалено {n}")
            m = enforce_media_cap()
            if m:
                logging.info(f"Потолок размера файлов: удалено {m}")
            await backfill_cache_meta()
            k = cleanup_old_messages()
            if k:
                logging.info(f"Очистка кэша сообщений: удалено {k}")
        except Exception:
            logging.exception("Ошибка janitor")
        await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)

# ---------- Одноразовые (самоуничтожающиеся) медиа ----------

def _extra_flags(obj):
    extra = getattr(obj, "model_extra", None)
    return extra if isinstance(extra, dict) else {}

def is_one_time(message) -> bool:
    """Ищет признаки самоуничтожающегося медиа на сообщении и на самом файле."""
    candidates = [message]
    mt, _ = get_media_type_and_file_id(message)
    if mt:
        candidates.append(message.photo[-1] if mt == "photo" else getattr(message, mt, None))
    for cand in candidates:
        flags = _extra_flags(cand)
        for name in ONE_TIME_FLAGS:
            v = flags.get(name)
            if isinstance(v, bool) and v:
                return True
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return True
    return False

def is_hidden_message(message) -> bool:
    """Бот не получил ни текста, ни известного медиа (типично для одноразовых фото/видео)."""
    body, _ = get_body_and_tag(message, 0)
    return body == "🙈 Скрытое медиа"

def log_hidden(message):
    """В лог — только названия полей скрытого сообщения (для диагностики), без содержимого."""
    try:
        skip = {"message_id", "date", "chat", "from_user", "business_connection_id"}
        fields = sorted(k for k in message.model_dump(exclude_none=True) if k not in skip)
        logging.info(f"Скрытое медиа, поля сообщения: {fields}")
    except Exception:
        pass

# ---------- MTProto-запасной способ скачивания (под токеном БОТА, не аккаунта) ----------
# Декодер file_id одноразовых медиа взят из escobar877-ic/telegram-memory-bot (MIT).
# Работает только если создан /root/salesbot/mtproto.json и установлен telethon.
MTPROTO_CONF = Path("/root/salesbot/mtproto.json")
MTPROTO_SESSION = "/root/salesbot/mtproto_bot"
_mt_client = None
_mt_lock = asyncio.Lock()

def _rle_decode(value: bytes) -> bytes:
    result = bytearray()
    zero = False
    for byte in value:
        if byte == 0:
            zero = True
        elif zero:
            result.extend(b"\0" * byte)
            zero = False
        else:
            result.append(byte)
    return bytes(result)

def _read_tl_bytes(buffer) -> bytes:
    first = buffer.read(1)[0]
    if first < 254:
        length = first
        header = 1
    else:
        length = int.from_bytes(buffer.read(3), "little")
        header = 4
    value = buffer.read(length)
    padding = -(header + length) % 4
    if padding:
        buffer.read(padding)
    return value

def resolve_protected_file_id(file_id: str):
    """Расшифровывает file_id самоуничтожающихся фото/видео (типы 22 и 23)."""
    import base64
    import struct
    from io import BytesIO
    from telethon import types
    padded = file_id + "=" * (-len(file_id) % 4)
    decoded = _rle_decode(base64.urlsafe_b64decode(padded))
    if len(decoded) < 34:
        return None
    major = decoded[-1]
    body = decoded[:-2] if major >= 4 else decoded[:-1]
    buffer = BytesIO(body)
    encoded_type, dc_id = struct.unpack("<ii", buffer.read(8))
    has_file_reference = bool(encoded_type & (1 << 25))
    file_type = encoded_type & ~((1 << 24) | (1 << 25))
    if file_type not in (22, 23) or not 1 <= dc_id <= 5:
        return None
    file_reference = _read_tl_bytes(buffer) if has_file_reference else b""
    media_id, access_hash = struct.unpack("<qq", buffer.read(16))
    if file_type == 22:
        tail = buffer.read()
        size_type = chr(struct.unpack("<i", tail[-4:])[0]) if len(tail) >= 4 else "y"
        return types.Photo(
            id=media_id, access_hash=access_hash, file_reference=file_reference, date=None,
            sizes=[types.PhotoSize(type=size_type, w=0, h=0, size=0)],
            dc_id=dc_id, has_stickers=False,
        )
    return types.Document(
        id=media_id, access_hash=access_hash, file_reference=file_reference, date=None,
        mime_type="video/mp4", size=0, thumbs=None, dc_id=dc_id,
        attributes=[types.DocumentAttributeVideo(
            duration=0, w=0, h=0, round_message=False, supports_streaming=True)],
    )

async def mtproto_download(file_id):
    """Скачивает файл через MTProto (сессия бота). None — если не настроено или не вышло."""
    global _mt_client
    try:
        if not MTPROTO_CONF.exists():
            logging.info("MTProto не настроен (нет файла /root/salesbot/mtproto.json)")
            return None
        async with _mt_lock:
            if _mt_client is None:
                import json
                from telethon import TelegramClient
                conf = json.loads(MTPROTO_CONF.read_text(encoding="utf-8"))
                client = TelegramClient(MTPROTO_SESSION, int(conf["api_id"]), conf["api_hash"],
                                        receive_updates=False)
                await client.start(bot_token=BOT_TOKEN)
                _mt_client = client
        from telethon.utils import resolve_bot_file_id
        media = resolve_bot_file_id(file_id) or resolve_protected_file_id(file_id)
        if media is None:
            logging.warning("MTProto: не удалось расшифровать file_id")
            return None
        data = await _mt_client.download_media(media, file=bytes)
        return data or None
    except Exception as e:
        logging.warning(f"MTProto: не удалось скачать ({e})")
        return None

async def send_raw(media_type, target_chat_id, raw, suffix) -> bool:
    """Отправляет скачанные байты: как есть, а при отказе — документом."""
    name = f"saved{suffix}"
    try:
        await send_buffered(media_type, target_chat_id, BufferedInputFile(raw, filename=name))
        return True
    except Exception as e:
        logging.warning(f"{media_type}: отправка скачанного файла не удалась ({e}), шлю документом")
    try:
        await bot.send_document(target_chat_id, BufferedInputFile(raw, filename=name))
        return True
    except Exception:
        logging.exception("Не удалось отправить сохранённый файл даже документом")
        return False

def log_probe(original, mt, protected, hidden, one_time):
    """В лог — что именно Telegram показал в ответе (только названия полей, без содержимого)."""
    fields = []
    try:
        skip = {"message_id", "date", "chat", "from_user", "business_connection_id"}
        fields = sorted(k for k in original.model_dump(exclude_none=True) if k not in skip)
    except Exception:
        pass
    logging.info(f"Ответ на медиа: тип={mt} protected={protected} hidden={hidden} "
                 f"one_time={one_time} поля={fields}")

async def maybe_rescue_one_time(message, conn_id, owner_id):
    """Владелец ответил на сообщение собеседника с одноразовым/защищённым медиа:
    Telegram показывает такое медиа внутри reply_to_message — сохраняем файл владельцу."""
    original = message.reply_to_message
    if original is None:
        return
    sender = message.from_user
    if not sender or sender.id != owner_id:
        return
    if original.from_user and original.from_user.id == owner_id:
        return
    mt, fid = get_media_type_and_file_id(original)
    protected = bool(getattr(original, "has_protected_content", False))
    hidden = is_hidden_message(original)
    one_time = bool(mt) and is_one_time(original)
    if not (hidden or one_time or (protected and mt)):
        return
    log_probe(original, mt, protected, hidden, one_time)
    if not user_capture_once_on(owner_id):
        return
    key = (conn_id, message.chat.id, original.message_id)
    if key in _rescued:
        return
    if len(_rescued) > 5000:
        _rescued.clear()
    _rescued.add(key)

    who = fmt_user(original.from_user or original.chat)
    when = fmt_time(original.date) if getattr(original, "date", None) else ""
    head = f"🔥 Одноразовое/защищённое медиа от {who}\n({when})"
    if not (mt and fid):
        log_hidden(original)
        await bot.send_message(
            owner_id,
            head + "\n⚠️ Telegram не передал боту этот файл — сохранить его нельзя.")
        return

    left = onetime_left(owner_id)
    if left is not None and left <= 0:
        _rescued.discard(key)
        day = today_key()
        if _limit_notified.get(owner_id) != day:
            _limit_notified[owner_id] = day
            await bot.send_message(
                owner_id,
                f"🔥 Лимит одноразовых на сегодня исчерпан ({FREE_ONETIME_PER_DAY} в день).\n"
                f"Без лимита — в 💎 Premium. Купить: {OWNER_CONTACT}")
        return

    cached = cache_get(conn_id, message.chat.id, original.message_id)
    path = cached.get("file_path") if cached else None
    ok = await try_resend_media(mt, fid, owner_id, path)
    if not ok:
        raw = await mtproto_download(fid)
        if raw:
            ok = await send_raw(mt, owner_id, raw, "." + MEDIA_EXT.get(mt, "bin"))
    if not ok:
        _rescued.discard(key)   # дадим повторить ответом позже (например, после настройки MTProto)
    left_note = ""
    if ok and left is not None:
        onetime_register(owner_id)
        left_note = f"\nОсталось сегодня: {onetime_left(owner_id)} из {FREE_ONETIME_PER_DAY}"
    warn = "" if ok else "\n⚠️ Telegram показал медиа в ответе, но не разрешил скачать файл."
    await bot.send_message(owner_id, head + warn + left_note)

async def send_media_from_message(source_msg, target_chat_id) -> bool:
    try:
        if source_msg.photo:        await bot.send_photo(target_chat_id, source_msg.photo[-1].file_id); return True
        if source_msg.video:        await bot.send_video(target_chat_id, source_msg.video.file_id); return True
        if source_msg.document:     await bot.send_document(target_chat_id, source_msg.document.file_id); return True
        if source_msg.animation:    await bot.send_animation(target_chat_id, source_msg.animation.file_id); return True
        if source_msg.audio:        await bot.send_audio(target_chat_id, source_msg.audio.file_id); return True
        if source_msg.voice:        await bot.send_voice(target_chat_id, source_msg.voice.file_id); return True
        if source_msg.video_note:   await bot.send_video_note(target_chat_id, source_msg.video_note.file_id); return True
        if source_msg.sticker:      await bot.send_sticker(target_chat_id, source_msg.sticker.file_id); return True
    except Exception:
        logging.exception("Ошибка пересылки медиа по 👀")
    return False

async def with_retry(coro_func, *args, **kwargs):
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await coro_func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < RETRY_ATTEMPTS:
                logging.warning(f"Попытка {attempt}/{RETRY_ATTEMPTS} не удалась ({e}), повтор через {RETRY_DELAY_SECONDS} сек")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.exception(f"Все {RETRY_ATTEMPTS} попытки не удались")
    raise last_exc

def biz_upsert(conn_id, owner_id, enabled=None, connected_at=None):
    """Запоминает подключение Telegram Business. enabled=None — не менять."""
    try:
        now = int(time.time())
        row = db.execute("SELECT enabled FROM biz_conn WHERE conn_id=?", (conn_id,)).fetchone()
        if row is None:
            db.execute("INSERT INTO biz_conn (conn_id, owner_id, enabled, connected_at, disconnected_at) "
                       "VALUES (?,?,?,?,?)",
                       (conn_id, owner_id, 1 if enabled is None else int(bool(enabled)),
                        connected_at or now, None if (enabled is None or enabled) else now))
        else:
            if enabled is not None:
                if enabled:
                    db.execute("UPDATE biz_conn SET owner_id=?, enabled=1, disconnected_at=NULL WHERE conn_id=?",
                               (owner_id, conn_id))
                elif row[0] != 0:
                    db.execute("UPDATE biz_conn SET owner_id=?, enabled=0, disconnected_at=? WHERE conn_id=?",
                               (owner_id, now, conn_id))
            if connected_at:
                db.execute("UPDATE biz_conn SET connected_at=COALESCE(connected_at, ?) WHERE conn_id=?",
                           (connected_at, conn_id))
        db.commit()
    except Exception:
        logging.exception("biz_upsert")

def biz_touch(conn_id, owner_id):
    """Отмечает реальную активность по подключению (пришло сообщение)."""
    try:
        now = int(time.time())
        if db.execute("SELECT 1 FROM biz_conn WHERE conn_id=?", (conn_id,)).fetchone() is None:
            biz_upsert(conn_id, owner_id)
        db.execute("UPDATE biz_conn SET last_msg_at=?, first_msg_at=COALESCE(first_msg_at, ?), "
                   "enabled=CASE WHEN enabled=0 THEN 1 ELSE enabled END, "
                   "disconnected_at=CASE WHEN enabled=0 THEN NULL ELSE disconnected_at END "
                   "WHERE conn_id=?", (now, now, conn_id))
        db.commit()
    except Exception:
        logging.exception("biz_touch")

async def backfill_biz_conn():
    """Один раз при старте: восстанавливает подключения по кэшу сообщений и уточняет статус у Telegram."""
    try:
        rows = db.execute(
            "SELECT conn_id, owner_id, MIN(sent_at), MAX(sent_at) FROM message_cache "
            "WHERE owner_id IS NOT NULL GROUP BY conn_id, owner_id").fetchall()
        for conn_id, owner_id, mn, mx in rows:
            if db.execute("SELECT 1 FROM biz_conn WHERE conn_id=?", (conn_id,)).fetchone() is None:
                db.execute("INSERT INTO biz_conn (conn_id, owner_id, enabled, first_msg_at, last_msg_at) "
                           "VALUES (?,?,NULL,?,?)", (conn_id, owner_id, mn, mx))
        db.commit()
        pending = db.execute("SELECT conn_id, owner_id FROM biz_conn WHERE enabled IS NULL").fetchall()
        for conn_id, owner_id in pending:
            try:
                bc = await bot.get_business_connection(conn_id)
                en = 1 if getattr(bc, "is_enabled", True) else 0
                dt = getattr(bc, "date", None)
                db.execute("UPDATE biz_conn SET enabled=?, connected_at=COALESCE(connected_at, ?), "
                           "disconnected_at=CASE WHEN ?=0 THEN ? ELSE NULL END WHERE conn_id=?",
                           (en, int(dt.timestamp()) if dt else None, en, int(time.time()), conn_id))
                db.commit()
            except Exception:
                logging.info(f"backfill_biz_conn: статус {conn_id} не получен")
            await asyncio.sleep(0.2)
    except Exception:
        logging.exception("backfill_biz_conn")

def stats_business_text() -> str:
    """Реальная статистика: кто подключил Telegram Business и пользуется ботом."""
    now = int(time.time())
    q1 = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    total = q1("SELECT COUNT(*) FROM users")
    rows = db.execute(
        "SELECT owner_id, MAX(CASE WHEN enabled=1 OR (enabled IS NULL AND COALESCE(last_msg_at,0) > ?) "
        "THEN 1 ELSE 0 END), MAX(COALESCE(last_msg_at,0)) FROM biz_conn "
        "WHERE owner_id IS NOT NULL GROUP BY owner_id", (now - 7 * 86400,)).fetchall()
    owners = {r[0]: (r[1], r[2]) for r in rows}
    ever = len(owners)
    active = sum(1 for v in owners.values() if v[0] == 1)
    using = sum(1 for v in owners.values() if v[0] == 1 and v[1] > now - 30 * 86400)
    a24 = sum(1 for v in owners.values() if v[1] > now - 86400)
    a7 = sum(1 for v in owners.values() if v[1] > now - 7 * 86400)
    a30 = sum(1 for v in owners.values() if v[1] > now - 30 * 86400)
    uids = {r[0] for r in db.execute("SELECT user_id FROM users").fetchall()}
    never = len(uids - set(owners))
    left = ever - active
    prem = q1("SELECT COUNT(*) FROM users WHERE premium_until > ?", now)
    paid = q1("SELECT COUNT(DISTINCT u.user_id) FROM users u JOIN orders o ON o.user_id=u.user_id "
              "WHERE u.premium_until > ? AND o.status='paid'", now)
    unpaid = max(0, prem - paid)
    testers = q1("SELECT COUNT(*) FROM testers")
    pct = lambda a, b: f"{(100 * a / b):.0f}%" if b else "—"
    return (
        "📊 Реальная статистика\n\n"
        f"👥 Зарегистрировано (/start): {total}\n"
        f"🔌 Подключили Telegram Business: {ever}\n"
        f"✅ Действующее подключение: {active}\n"
        f"🔥 Реально используют: {using}\n"
        "   (подключение активно и сообщения за 30 дней)\n\n"
        "Активность по сообщениям:\n"
        f"• за 24 часа: {a24}\n"
        f"• за 7 дней: {a7}\n"
        f"• за 30 дней: {a30}\n\n"
        f"💤 Зарегистрированы, но не подключили: {never}\n"
        f"🔕 Подключали и отключили: {left}\n\n"
        f"💎 Premium сейчас: {prem}\n"
        f"   • оплатили: {paid}\n"
        f"   • без оплаты (подарок/бонус/выдан): {unpaid}\n"
        f"🧪 Тестеры: {testers}\n\n"
        "Конверсия:\n"
        f"• регистрация → подключение: {pct(ever, total)}\n"
        f"• регистрация → действующее: {pct(active, total)}\n"
        f"• регистрация → реально используют: {pct(using, total)}"
    )

async def get_owner_id(conn_id):
    owner_id = business_owners.get(conn_id)
    if owner_id is not None:
        return owner_id
    try:
        bc = await bot.get_business_connection(conn_id)
        business_owners[conn_id] = bc.user.id
        business_owner_names[conn_id] = fmt_user(bc.user)
        biz_upsert(conn_id, bc.user.id, bool(getattr(bc, "is_enabled", True)),
                   int(bc.date.timestamp()) if getattr(bc, "date", None) else None)
        return bc.user.id
    except Exception:
        logging.exception(f"Не удалось получить business_connection для {conn_id}")
        return None

def build_chat_list_keyboard():
    chats = pending_chats_all()
    if not chats:
        return None, []
    buttons = []
    for chat_id, chat_name, manager_label in chats:
        label = chat_name if chat_name else f"id{chat_id}"
        unread = pending_count_unread_for_chat(chat_id)
        total  = pending_count_total_for_chat(chat_id)
        unread_str = f" | 🆕 {unread}" if unread > 0 else " | ✅"
        buttons.append([InlineKeyboardButton(
            text=f"[{manager_label}] {label} ({total}{unread_str})",
            callback_data=f"fc:{chat_id}"
        )])
    buttons.append([InlineKeyboardButton(text="📬 Все непрочитанные сразу", callback_data="fc:all")])
    return InlineKeyboardMarkup(inline_keyboard=buttons), chats

async def do_send_unread_chat(chat_id, context_count):
    rows = pending_get_unread_for_chat(chat_id)
    if not rows:
        await bot.send_message(ADMIN_ID, "Нет новых непрочитанных сообщений в этом чате.")
        return
    if context_count > 0:
        first_db_id = rows[0][0]
        context_rows = cache_get_context(chat_id, first_db_id, context_count)
        if context_rows:
            await bot.send_message(ADMIN_ID, f"📋 Контекст ({len(context_rows)} сообщ.):")
            for ctx_tag, ctx_body, ctx_time in context_rows:
                try:
                    await with_retry(bot.send_message, ADMIN_ID, build_block(ctx_tag, ctx_body, ctx_time))
                except Exception:
                    logging.exception("Ошибка отправки контекста")
        await bot.send_message(ADMIN_ID, "▼ Пропущенные сообщения:")
    for row in rows:
        _id, manager_label, tag, body, time_str, media_type, file_id = row
        try:
            if media_type and media_type not in ("✏️изменение",):
                await with_retry(send_media_by_type, media_type, file_id, ADMIN_ID, None)
            await with_retry(bot.send_message, ADMIN_ID, build_block(tag, body, time_str, manager_label))
        except Exception:
            logging.exception(f"Ошибка отправки id={_id}")
    pending_mark_delivered_chat(chat_id)
    kb, chats = build_chat_list_keyboard()
    if kb:
        await bot.send_message(ADMIN_ID, "✅ Прочитано. Все чаты:", reply_markup=kb)
    else:
        await bot.send_message(ADMIN_ID, "✅ Все непрочитанные просмотрены.")

async def do_send_history_chat(chat_id, limit):
    rows = pending_get_last_for_chat(chat_id, limit)
    if not rows:
        await bot.send_message(ADMIN_ID, "В журнале нет сообщений для этого чата.")
        return
    await bot.send_message(ADMIN_ID, f"📖 Последние {len(rows)} сообщений из журнала:")
    for row in rows:
        _id, manager_label, tag, body, time_str, media_type, file_id = row
        try:
            if media_type and media_type not in ("✏️изменение",):
                await with_retry(send_media_by_type, media_type, file_id, ADMIN_ID, None)
            await with_retry(bot.send_message, ADMIN_ID, build_block(tag, body, time_str, manager_label))
        except Exception:
            logging.exception(f"Ошибка показа истории id={_id}")
    kb, _ = build_chat_list_keyboard()
    if kb:
        await bot.send_message(ADMIN_ID, "✅ Показано. Все чаты:", reply_markup=kb)

async def broadcast_to_users(text: str) -> tuple:
    users = db.execute("SELECT user_id FROM users WHERE notifications=1").fetchall()
    sent, failed = 0, 0
    for (uid,) in users:
        if uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed

async def export_chat_to_file(chat_id: int) -> bytes:
    """Генерирует текстовый файл с историей чата из кэша."""
    rows = db.execute(
        "SELECT tag, body, time_str FROM message_cache "
        "WHERE chat_id=? ORDER BY id ASC",
        (chat_id,)
    ).fetchall()

    chat_name = db.execute(
        "SELECT chat_name FROM pending_forward WHERE chat_id=? AND chat_name!='' LIMIT 1",
        (chat_id,)
    ).fetchone()
    name = chat_name[0] if chat_name else f"id{chat_id}"

    lines = [f"=== Экспорт чата: {name} ===\n"]
    lines.append(f"Сообщений: {len(rows)}\n")
    lines.append(f"Дата экспорта: {datetime.now(TZ).strftime('%H:%M %d.%m.%y')}\n")
    lines.append("=" * 40 + "\n\n")

    for tag, body, time_str in rows:
        lines.append(f"{tag}\n{body}\n({time_str})\n---\n")

    return "\n".join(lines).encode("utf-8")


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
        "впиши @kikitutkofffbot\n\n"
        "Дальше всё автоматически: кто-то удалил или изменил сообщение — бот пришлёт тебе "
        "уведомление.\n\n"
        "💎 Тарифы и лимиты — кнопка «💎 Premium». Создатель @your_contact",
        reply_markup=main_keyboard(message.from_user.id)
    )
    if is_new and NEW_USER_PREMIUM_DAYS > 0:
        await message.answer(
            f"🎁 Тебе в подарок {NEW_USER_PREMIUM_DAYS} дней Premium — до "
            f"{fmt_ts(get_premium_until(message.from_user.id))}: полная история удалённых, "
            f"одноразовые фото и видео без лимита, экспорт переписки.")

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
    journal_total  = pending_count_total()
    journal_unread = db.execute("SELECT COUNT(*) FROM pending_forward WHERE delivered=0").fetchone()[0]
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

@dp.message(Command("feels"))
async def cmd_feels(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    kb, chats = build_chat_list_keyboard()
    if not chats:
        await message.answer("Журнал пуст — сообщений ещё нет.")
        return
    await message.answer("Выберите чат:", reply_markup=kb)

@dp.message(Command("clear_feels"))
async def cmd_clear_feels(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    total  = pending_count_total()
    viewed = db.execute("SELECT COUNT(*) FROM pending_forward WHERE delivered=1").fetchone()[0]
    unread = total - viewed
    if total == 0:
        await message.answer("Журнал уже пуст.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🗑 Удалить просмотренные ({viewed:,} зап.)".replace(",", " "),
            callback_data="cf:viewed"
        )],
        [InlineKeyboardButton(text="🗑 Удалить 1 млн самых старых", callback_data="cf:1000000")],
        [InlineKeyboardButton(text="🗑 Удалить 5 млн самых старых", callback_data="cf:5000000")],
        [InlineKeyboardButton(text="💣 Очистить всё",              callback_data="cf:all")],
        [InlineKeyboardButton(text="❌ Отмена",                     callback_data="cf:cancel")],
    ])
    await message.answer(
        f"📊 Журнал: {total:,} записей\n"
        f"   • Просмотрено: {viewed:,}\n"
        f"   • Непросмотрено: {unread:,}\n\n"
        f"Что удалить?".replace(",", " "),
        reply_markup=kb
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
    del_setting("awaiting_count_for_chat")
    del_setting("awaiting_history_for_chat")
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

BOT_USERNAME = "kikitutkofffbot"
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

@dp.message(Command("offer"))
async def cmd_offer(message: Message):
    if not message.from_user:
        return
    parts = offer_chunks()
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await message.answer(part, reply_markup=offer_keyboard(message.from_user.id) if last else None)

@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(terms_text())

@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    if not message.from_user:
        return
    await message.answer(paysupport_text(message.from_user.id))

@dp.message(Command("support"))
async def cmd_support(message: Message):
    await message.answer(f"Вопросы и помощь: {OWNER_CONTACT}\nПо оплате: /paysupport\nУсловия: /terms")

@dp.message(Command("grant"))
async def cmd_grant(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) <= 0:
        await message.answer("Формат: /grant ID_или_@ник дни\nПример: /grant 123456789 30")
        return
    target = resolve_user_id(parts[1])
    if target is None:
        await message.answer("Не нашёл такого пользователя (по @нику ищу среди тех, кто писал боту). "
                             "Используй числовой ID.")
        return
    until = grant_premium(target, int(parts[2]))
    log_action(ADMIN_ID, "grant", target, f"{parts[2]} дн (команда)")
    await message.answer(f"✅ Premium выдан: {target} до {fmt_ts(until)}")
    try:
        await bot.send_message(target, f"💎 Тебе выдан Premium до {fmt_ts(until)}. Спасибо!")
    except Exception:
        await message.answer("⚠️ Пользователю сообщение не доставлено (он ещё не писал боту?).")

@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    target = resolve_user_id(parts[1]) if len(parts) == 2 else None
    if target is None:
        await message.answer("Формат: /revoke ID_или_@ник")
        return
    revoke_premium(target)
    log_action(ADMIN_ID, "revoke", target, "(команда)")
    await message.answer(f"✅ Premium у {target} отключён.")

@dp.message(Command("premiumlist"))
async def cmd_premiumlist(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    rows = db.execute(
        "SELECT user_id, username, premium_until FROM users WHERE premium_until > ? "
        "ORDER BY premium_until ASC LIMIT 60", (now_ts(),)).fetchall()
    if not rows:
        await message.answer("Активных Premium нет.")
        return
    lines = [f"{uid} {('@' + un) if un else ''} — до {fmt_ts(pu)}" for uid, un, pu in rows]
    await message.answer(f"💎 Активных Premium: {len(rows)}\n\n" + "\n".join(lines))

@dp.message(Command("gift_all"))
async def cmd_gift_all(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] != "confirm":
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        await message.answer(f"Формат: /gift_all дни confirm\n"
                             f"Подарит Premium всем {total} пользователям из базы (кроме тебя).\n"
                             f"Пример: /gift_all 30 confirm")
        return
    uids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE user_id != ?", (ADMIN_ID,))]
    for uid in uids:
        grant_premium(uid, int(parts[1]))
    await message.answer(f"✅ Подарено {parts[1]} дн. Premium: {len(uids)} пользователям.\n"
                         f"Сообщить им можно рассылкой (📢 Рассылка).")

@dp.message(Command("comm"))
async def cmd_comm(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📋 Команды бота:\n\n"
        "/feels — открыть журнал чатов\n"
        "/clear_feels — управление журналом\n"
        "/stats — статистика бота\n"
        "/upd — рассылка сообщения всем пользователям\n"
        "/r — быстрые кнопки технических работ\n"
        "/cancel — отменить рассылку\n"
        "/grant ID|@ник дни — выдать Premium\n"
        "/revoke ID|@ник — забрать Premium\n"
        "/premiumlist — список Premium\n"
        "/gift_all дни confirm — подарить Premium всем\n"
        "/comm — эта справка"
    )


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
        journal_total  = pending_count_total()
        journal_unread = db.execute("SELECT COUNT(*) FROM pending_forward WHERE delivered=0").fetchone()[0]
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

    pending_key = get_setting("awaiting_count_for_chat")
    if pending_key:
        if not text.isdigit():
            await message.answer("Введи просто число, например: 150")
            return
        chat_id = int(pending_key)
        del_setting("awaiting_count_for_chat")
        await message.answer(f"Отправляю пропущенные + {int(text)} для контекста...")
        await do_send_unread_chat(chat_id, int(text))
        return

    history_key = get_setting("awaiting_history_for_chat")
    if history_key:
        if not text.isdigit():
            await message.answer("Введи просто число, например: 150")
            return
        chat_id = int(history_key)
        del_setting("awaiting_history_for_chat")
        await message.answer(f"Показываю последние {int(text)} сообщений из журнала...")
        await do_send_history_chat(chat_id, int(text))
        return


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

    if data.startswith("fc:"):
        chat_key = data[3:]

        if chat_key == "all":
            rows = db.execute(
                "SELECT id, manager_label, tag, body, time_str, media_type, file_id "
                "FROM pending_forward WHERE delivered=0 ORDER BY id ASC"
            ).fetchall()
            if not rows:
                await callback.message.edit_text("Нет непрочитанных сообщений.")
                await callback.answer()
                return
            await callback.message.edit_text(f"Отправляю все непрочитанные ({len(rows)} сообщ.)...")
            for row in rows:
                _id, manager_label, tag, body, time_str, media_type, file_id = row
                try:
                    if media_type and media_type not in ("✏️изменение",):
                        await with_retry(send_media_by_type, media_type, file_id, ADMIN_ID, None)
                    await with_retry(bot.send_message, ADMIN_ID, build_block(tag, body, time_str, manager_label))
                except Exception:
                    logging.exception(f"Ошибка отправки id={_id}")
            pending_mark_delivered_all()
            kb, _ = build_chat_list_keyboard()
            if kb:
                await bot.send_message(ADMIN_ID, "✅ Готово. Все чаты:", reply_markup=kb)
            else:
                await bot.send_message(ADMIN_ID, "✅ Готово.")
            await callback.answer()
            return

        if chat_key == "back":
            kb, chats = build_chat_list_keyboard()
            if chats:
                await callback.message.edit_text("Выберите чат:", reply_markup=kb)
            else:
                await callback.message.edit_text("Журнал пуст.")
            await callback.answer()
            return

        chat_id = int(chat_key)
        unread = pending_count_unread_for_chat(chat_id)
        total  = pending_count_total_for_chat(chat_id)

        if unread > 0:
            buttons = [
                [InlineKeyboardButton(text=f"Только пропущенные ({unread})", callback_data=f"fs:{chat_key}:0")],
                [InlineKeyboardButton(text="+50 сообщений до",    callback_data=f"fs:{chat_key}:50")],
                [InlineKeyboardButton(text="+100 сообщений до",   callback_data=f"fs:{chat_key}:100")],
                [InlineKeyboardButton(text="+250 сообщений до",   callback_data=f"fs:{chat_key}:250")],
                [InlineKeyboardButton(text="+500 сообщений до",   callback_data=f"fs:{chat_key}:500")],
                [InlineKeyboardButton(text="✏️ Ввести своё число", callback_data=f"fs:{chat_key}:custom")],
                [InlineKeyboardButton(text="← Назад",             callback_data="fc:back")],
            ]
            await callback.message.edit_text(
                f"Непрочитанных: {unread}. Сколько показать контекста?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
        else:
            buttons = [
                [InlineKeyboardButton(text="Последние 50 из журнала",  callback_data=f"fh:{chat_key}:50")],
                [InlineKeyboardButton(text="Последние 100 из журнала", callback_data=f"fh:{chat_key}:100")],
                [InlineKeyboardButton(text="Последние 250 из журнала", callback_data=f"fh:{chat_key}:250")],
                [InlineKeyboardButton(text="Последние 500 из журнала", callback_data=f"fh:{chat_key}:500")],
                [InlineKeyboardButton(text="✏️ Ввести своё число",     callback_data=f"fh:{chat_key}:custom")],
                [InlineKeyboardButton(text="← Назад",                  callback_data="fc:back")],
            ]
            await callback.message.edit_text(
                f"Всё прочитано (всего в журнале: {total}). Показать из журнала?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
        await callback.answer()

    elif data.startswith("fs:"):
        parts = data.split(":")
        chat_id, count_str = int(parts[1]), parts[2]
        if count_str == "custom":
            set_setting("awaiting_count_for_chat", str(chat_id))
            await callback.message.edit_text("Напиши число сообщений для контекста (например: 150):")
            await callback.answer()
            return
        await callback.message.edit_text(
            "Отправляю пропущенные" +
            (f" + {count_str} для контекста" if count_str != "0" else "") + "..."
        )
        await do_send_unread_chat(chat_id, int(count_str))
        await callback.answer()

    elif data.startswith("fh:"):
        parts = data.split(":")
        chat_id, count_str = int(parts[1]), parts[2]
        if count_str == "custom":
            set_setting("awaiting_history_for_chat", str(chat_id))
            await callback.message.edit_text("Напиши число последних сообщений (например: 150):")
            await callback.answer()
            return
        await callback.message.edit_text(f"Показываю последние {count_str} из журнала...")
        await do_send_history_chat(chat_id, int(count_str))
        await callback.answer()

    elif data.startswith("cf:"):
        action = data[3:]
        if action == "cancel":
            await callback.message.edit_text("Отмена — журнал не тронут.")
        elif action == "viewed":
            db.execute("DELETE FROM pending_forward WHERE delivered=1")
            db.commit()
            await callback.message.edit_text(
                f"✅ Просмотренные удалены.\nОсталось: {pending_count_total():,}.".replace(",", " ")
            )
        elif action == "all":
            db.execute("DELETE FROM pending_forward")
            db.commit()
            await callback.message.edit_text("✅ Журнал полностью очищен.")
        else:
            n = int(action)
            db.execute(
                "DELETE FROM pending_forward WHERE id IN "
                "(SELECT id FROM pending_forward ORDER BY id ASC LIMIT ?)", (n,)
            )
            db.commit()
            await callback.message.edit_text(
                f"✅ Удалено {n:,} записей.\nОсталось: {pending_count_total():,}.".replace(",", " ")
            )
        await callback.answer()


# ---------- Основная логика business-сообщений ----------

@dp.business_connection()
async def on_business_connection(bc: BusinessConnection):
    business_owners[bc.id] = bc.user.id
    business_owner_names[bc.id] = fmt_user(bc.user)
    biz_upsert(bc.id, bc.user.id, bool(getattr(bc, "is_enabled", True)),
               int(bc.date.timestamp()) if getattr(bc, "date", None) else None)

@dp.business_message()
async def on_business_message(message: Message):
    conn_id  = message.business_connection_id
    owner_id = await get_owner_id(conn_id)
    if owner_id is None:
        return
    biz_touch(conn_id, owner_id)

    time_str = fmt_time(message.date)
    body, tag = get_body_and_tag(message, owner_id)
    _mt, _fi  = get_media_type_and_file_id(message)

    cache_put(conn_id, message.chat.id, message.message_id, body, tag, time_str, _mt, _fi,
              sent_at=int(message.date.timestamp()), owner_id=owner_id)
    schedule_media_cache(conn_id, message.chat.id, message.message_id, _mt, _fi,
                         get_media_size(message))
    if body == "🙈 Скрытое медиа":
        log_hidden(message)
    try:
        await maybe_rescue_one_time(message, conn_id, owner_id)
    except Exception:
        logging.exception("Ошибка спасения одноразового медиа")

    return

@dp.edited_business_message()
async def on_business_message_edited(message: Message):
    conn_id  = message.business_connection_id
    owner_id = await get_owner_id(conn_id)
    if owner_id is None:
        return
    biz_touch(conn_id, owner_id)

    if message.location and getattr(message.location, 'live_period', None):
        return

    old_entry     = cache_get(conn_id, message.chat.id, message.message_id)
    new_time_str  = fmt_time(message.date)
    new_body, tag = get_body_and_tag(message, owner_id)
    old_body = old_entry["body"] if old_entry else "(не сохранено в кэше)"

    sender = message.from_user
    is_own_edit = sender is not None and sender.id == owner_id

    if owner_id == ADMIN_ID:
        # Себе — только если own_edit_notify включён или это не своя правка
        if not is_own_edit or user_own_edit_notify_on(ADMIN_ID):
            try:
                await with_retry(bot.send_message, ADMIN_ID,
                    "✏️ Сообщение изменено (на твоём аккаунте):\n"
                    + build_block(tag, f"БЫЛО: {old_body}\nСТАЛО: {new_body}", new_time_str))
            except Exception:
                logging.exception("Ошибка уведомления об изменении своего сообщения")

    else:
        # Публичный пользователь
        if user_monitoring_on(owner_id) and user_track_edits_on(owner_id):
            if not is_own_edit or user_own_edit_notify_on(owner_id):
                try:
                    await with_retry(bot.send_message, owner_id,
                        f"✏️ В чате с {fmt_user(message.chat)} изменено сообщение:\n"
                        f"БЫЛО: {old_body}\nСТАЛО: {new_body}\n({new_time_str})")
                except Exception:
                    logging.exception("Ошибка отправки публичному пользователю об изменении")

    _new_mt, _new_fi = get_media_type_and_file_id(message)
    _keep_path = (old_entry.get("file_path")
                  if (old_entry and _new_fi and old_entry.get("file_id") == _new_fi) else None)
    cache_put(conn_id, message.chat.id, message.message_id,
              new_body, tag, new_time_str, _new_mt, _new_fi, _keep_path,
              sent_at=int(message.date.timestamp()), owner_id=owner_id)
    if _new_fi and not _keep_path:
        schedule_media_cache(conn_id, message.chat.id, message.message_id, _new_mt, _new_fi,
                             get_media_size(message))

@dp.deleted_business_messages()
async def on_business_messages_deleted(event: BusinessMessagesDeleted):
    conn_id  = event.business_connection_id
    chat_id  = event.chat.id
    owner_id = await get_owner_id(conn_id)
    if owner_id is None:
        return

    for msg_id in event.message_ids:
        cached = cache_get(conn_id, chat_id, msg_id)

        if owner_id == ADMIN_ID:
            if cached:
                warn = await deliver_deleted_media(cached, ADMIN_ID)
                text_out = "🗑 Удалено сообщение:\n" + build_block(
                    cached["tag"], cached["body"], cached["time"]) + warn
            else:
                text_out = f"🗑 Удалено сообщение (нет в кэше, id {msg_id})"
            try:
                await with_retry(bot.send_message, ADMIN_ID, text_out)
            except Exception:
                logging.exception("Ошибка уведомления об удалении своего сообщения")

        else:
            # Публичный пользователь — только если мониторинг включён
            if not user_monitoring_on(owner_id):
                release_cached_file(conn_id, chat_id, msg_id, cached)
                continue
            interlocutor = fmt_user(event.chat)
            if cached:
                if history_allowed(owner_id, cached):
                    warn = await deliver_deleted_media(cached, owner_id)
                    text_public = (f"🗑 В чате с {interlocutor} удалено сообщение:\n"
                                   f"{cached['body']}\n({cached['time']})" + warn)
                else:
                    text_public = history_teaser(cached, interlocutor)
            else:
                text_public = f"🗑 В чате с {interlocutor} удалено сообщение (нет в кэше)"
            try:
                await with_retry(bot.send_message, owner_id, text_public)
            except Exception:
                logging.exception("Ошибка отправки публичному пользователю об удалении")

        # файл уже доставлен (или получателю не нужен) — освобождаем место на диске
        release_cached_file(conn_id, chat_id, msg_id, cached)


async def main():
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await startup_notice()
    except Exception:
        logging.exception("Ошибка startup_notice")
    _ht = asyncio.create_task(heartbeat_loop())
    _bg_tasks.add(_ht)
    _jt = asyncio.create_task(janitor_loop())
    _bg_tasks.add(_jt)
    _rt = asyncio.create_task(reminder_loop())
    _bg_tasks.add(_rt)
    _pt = asyncio.create_task(payout_loop())
    _bg_tasks.add(_pt)
    _ot = asyncio.create_task(offer_loop())
    _bg_tasks.add(_ot)
    _bt = asyncio.create_task(backfill_biz_conn())
    _bg_tasks.add(_bt)
    _at = asyncio.create_task(achievements_loop())
    _bg_tasks.add(_at)
    _rtt = asyncio.create_task(retention_loop())
    _bg_tasks.add(_rtt)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

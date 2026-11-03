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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")  # токен от @BotFather
ADMIN_ID = 123456789  # твой Telegram ID (владелец)

TZ = timezone(timedelta(hours=3))
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.5
MAX_CACHE = 20000000

# --- Кэш файлов на диске (скачиваем медиа сразу при получении, как в vestige) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = Path(BASE_DIR) / "media"
CACHE_MEDIA = True              # False — не сохранять файлы на диск
MAX_CACHE_FILE_MB = 20          # больше 20 МБ Bot API скачать не даёт
RETENTION_DAYS = 30             # файлы старше N дней удаляются (0 — не удалять)
MEDIA_MAX_GB = 30               # потолок размера папки с файлами (старые удаляются первыми)
TEXT_RETENTION_DAYS = 30        # тексты в кэше публичных пользователей хранятся N дней
FREE_HISTORY_DAYS = 5           # базовый тариф: удалённые не старше N дней
FREE_ONETIME_PER_DAY = 2        # базовый тариф: одноразовых в день
FREE_TRANSLATE_PER_DAY = 2      # базовый тариф: переводов в день
PREMIUM_TRANSLATE_PER_DAY = 150 # Premium: переводов в день
FREE_LINKS_PER_DAY = 2          # базовый тариф: проверок ссылок в день
PREMIUM_LINKS_PER_DAY = 100     # Premium: проверок ссылок в день
OWNER_CONTACT = "@your_contact"  # куда писать за покупкой Premium
PREMIUM_PRICE_TEXT = "99 ₽ / месяц"
CLEANUP_INTERVAL_MINUTES = 60   # как часто чистить
DB_PATH = os.path.join(BASE_DIR, "cache.db")

logging.basicConfig(level=logging.INFO)

# --- локальный Bot API (файлы до 200 МБ). Включается файлом local_api.txt рядом с bot.py ---
def _read_local_api():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_api.txt"), encoding="utf-8") as _f:
            return _f.read().strip().rstrip("/")
    except Exception:
        return ""

LOCAL_API_URL = _read_local_api()
API_BASE = LOCAL_API_URL or "https://api.telegram.org"

def _make_bot():
    if LOCAL_API_URL:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer
        session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True), timeout=900)
        return Bot(token=BOT_TOKEN, session=session)
    return Bot(token=BOT_TOKEN)

bot = _make_bot()
dp = Dispatcher()

_orig_download_file = Bot.download_file

async def _download_file_local(self, file_path, destination=None, timeout=30, chunk_size=65536, seek=True):
    """Локальный сервер отдаёт абсолютный путь к уже скачанному файлу — просто копируем."""
    if LOCAL_API_URL and file_path and os.path.isabs(file_path):
        import io as _io, shutil as _sh
        def _cp():
            if destination is None:
                with open(file_path, "rb") as _f:
                    return _io.BytesIO(_f.read())
            if isinstance(destination, (str, os.PathLike)):
                _sh.copyfile(file_path, destination)
                return None
            with open(file_path, "rb") as _f:
                _sh.copyfileobj(_f, destination)
            if seek:
                destination.seek(0)
            return None
        return await asyncio.get_running_loop().run_in_executor(None, _cp)
    return await _orig_download_file(self, file_path, destination=destination, timeout=timeout,
                                     chunk_size=chunk_size, seek=seek)

Bot.download_file = _download_file_local

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
         "«Автоматизация чатов» → впиши @your_bot."),
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
    CREATE TABLE IF NOT EXISTS translations (
        conn_id TEXT, chat_id INTEGER, message_id INTEGER, user_id INTEGER,
        orig_text TEXT, translated TEXT, direction TEXT, status TEXT DEFAULT 'done',
        created_at INTEGER, PRIMARY KEY (conn_id, chat_id, message_id)
    )
""")
db.execute("""
    CREATE TABLE IF NOT EXISTS tr_usage (
        user_id INTEGER, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY (user_id, day)
    )
""")
db.commit()
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
        return (f"💎 Тариф: Premium до {fmt_ts(get_premium_until(user_id))}\n🔥 Одноразовые: без лимита\n"
                f"🌐 Переводы сегодня: {tr_used_today(user_id)}/{PREMIUM_TRANSLATE_PER_DAY}\n"
                f"🛡 Проверки ссылок сегодня: {lg_used_today(user_id)}/{PREMIUM_LINKS_PER_DAY}\n"
                f"🎙 Расшифровки: без лимита (сегодня {mj_used(user_id, 'rec')})\n"
                f"🔍 Проверки файлов (до {PREMIUM_SCAN_MAX_MB} МБ): без лимита (сегодня {mj_used(user_id, 'scan')})\n"
                f"📝 Списки: без лимита (сегодня {mj_used(user_id, 'list')})")
    return (f"🆓 Тариф: базовый\n"
            f"🗑 История удалённых: до {FREE_HISTORY_DAYS} дн.\n"
            f"🔥 Одноразовые сегодня: {onetime_used_today(user_id)}/{FREE_ONETIME_PER_DAY}\n"
            f"🌐 Переводы сегодня: {tr_used_today(user_id)}/{FREE_TRANSLATE_PER_DAY}\n"
            f"🛡 Проверки ссылок сегодня: {lg_used_today(user_id)}/{FREE_LINKS_PER_DAY}\n"
            f"🎙 Расшифровки сегодня: {mj_used(user_id, 'rec')}/{FREE_STT_PER_DAY}\n"
            f"🔍 Проверки файлов (до {FREE_SCAN_MAX_MB} МБ) сегодня: {mj_used(user_id, 'scan')}/{FREE_SCAN_PER_DAY}\n"
            f"📝 Списки сегодня: {mj_used(user_id, 'list')}/{FREE_LIST_PER_DAY}")

def premium_info_text(user_id) -> str:
    head = tariff_status_text(user_id)
    return (f"{head}\n\n"
            f"💎 Premium — {PREMIUM_PRICE_TEXT}\n"
            f"• удалённые сообщения за всё время хранения (до {TEXT_RETENTION_DAYS} дн.), "
            f"а не только за {FREE_HISTORY_DAYS} дн.\n"
            f"• спасение одноразовых без лимита (базовый: {FREE_ONETIME_PER_DAY} в день)\n"
            f"• перевод сообщений прямо в чате: {PREMIUM_TRANSLATE_PER_DAY} в день (базовый: {FREE_TRANSLATE_PER_DAY})\n"
            f"• защита от фишинга: {PREMIUM_LINKS_PER_DAY} проверок ссылок в день с расширенным анализом "
            f"(базовый: {FREE_LINKS_PER_DAY} в день, только известные угрозы)\n"
            f"• расшифровка голосовых и кружков без лимита (базовый: {FREE_STT_PER_DAY} в день)\n"
            f"• проверка файлов на вирусы без лимита, файлы до {PREMIUM_SCAN_MAX_MB} МБ (базовый: {FREE_SCAN_PER_DAY} в день, файлы до {FREE_SCAN_MAX_MB} МБ)\n"
            f"• списки из сообщений без лимита (базовый: {FREE_LIST_PER_DAY} в день)\n\n"
            f"{tariffs_text(user_id)}\n\nВыбери тариф кнопкой ниже 👇\nВопросы по оплате: {OWNER_CONTACT}")


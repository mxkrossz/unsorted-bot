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
    lg_btn   = "🛡 Выкл. защиту от ссылок" if get_setting(f"lg_off_{user_id}", "0") != "1" else "🛡 Вкл. защиту от ссылок"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=mon_btn)],
        [KeyboardButton(text=edit_btn)],
        [KeyboardButton(text=own_btn)],
        [KeyboardButton(text=once_btn)],
        [KeyboardButton(text=lg_btn)],
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
               'id' || mc.chat_id as chat_name,
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



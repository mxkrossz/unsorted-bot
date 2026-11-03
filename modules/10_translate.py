# ---------- Перевод сообщений в переписке ----------

TR_MAX_CHARS = 3500
TR_CHUNK = 1500
TR_UNDO_CMDS = ("+оригинал", "+отмена")
_tr_notified: dict = {}

def tr_parse_command(text):
    """'+перевод& текст' (ru→en) или '+&перевод текст' (en→ru). -> (язык результата, текст) или None."""
    t = (text or "").lstrip()
    low = t.lower()
    if low.startswith("+перевод&"):
        return "en", t[len("+перевод&"):].strip()
    if low.startswith("+&перевод"):
        return "ru", t[len("+&перевод"):].strip()
    return None

def tr_enabled() -> bool:
    return get_setting("tr_off", "0") != "1"

def tr_limit(uid) -> int:
    return PREMIUM_TRANSLATE_PER_DAY if is_premium(uid) else FREE_TRANSLATE_PER_DAY

def tr_used_today(uid) -> int:
    row = db.execute("SELECT n FROM tr_usage WHERE user_id=? AND day=?", (uid, today_key())).fetchone()
    return row[0] if row else 0

def tr_register(uid):
    db.execute("INSERT INTO tr_usage (user_id, day, n) VALUES (?, ?, 1) "
               "ON CONFLICT(user_id, day) DO UPDATE SET n = n + 1", (uid, today_key()))
    db.commit()

def tr_chunks(text, limit=TR_CHUNK):
    """Режет текст на куски не длиннее ~limit символов по строкам и пробелам. ''.join(куски) == text."""
    units = []
    lines = text.split("\n")
    for li, line in enumerate(lines):
        pieces = []
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(line[:cut])
            line = line[cut:]
        pieces.append(line)
        for pi, p in enumerate(pieces):
            last_piece = pi == len(pieces) - 1
            units.append(p + (("\n" if li < len(lines) - 1 else "") if last_piece else ""))
    out, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) > limit:
            out.append(cur)
            cur = ""
        cur += u
    if cur:
        out.append(cur)
    return out or [""]

async def tr_http(text, tl):
    """Бесплатный веб-переводчик Google (без ключа). -> (перевод, определённый язык)"""
    import aiohttp
    params = {"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": text}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get("https://translate.googleapis.com/translate_a/single", params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"переводчик ответил {r.status}")
            data = await r.json(content_type=None)
    out = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    return out, (data[2] if len(data) > 2 and isinstance(data[2], str) else "")

async def tr_http_fallback(text, tl):
    """Запасной переводчик MyMemory (короткие тексты до ~450 символов)."""
    import aiohttp
    params = {"q": text, "langpair": f"Autodetect|{tl}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get("https://api.mymemory.translated.net/get", params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"запасной переводчик ответил {r.status}")
            data = await r.json(content_type=None)
    out = (data.get("responseData") or {}).get("translatedText") or ""
    if not out:
        raise RuntimeError("пустой ответ запасного переводчика")
    return out, ""

async def tr_translate(text, tl):
    """Переводит текст кусками. -> (результат, определённый язык исходника)"""
    result, detected = "", ""
    for chunk in tr_chunks(text):
        core = chunk.rstrip()
        trail = chunk[len(core):]
        if not core.strip():
            result += chunk
            continue
        try:
            t, lang = await tr_http(core, tl)
        except Exception:
            if len(core) > 450:
                raise
            t, lang = await tr_http_fallback(core, tl)
        detected = detected or lang
        result += t + trail
    return result.strip(), detected

async def tr_notify(uid, text):
    """Сообщение владельцу аккаунта в личку бота (не чаще раза в 30 сек одинаковое)."""
    key = (uid, text)
    now = time.time()
    if now - _tr_notified.get(key, 0) < 30:
        return
    _tr_notified[key] = now
    try:
        await bot.send_message(uid, text)
    except Exception:
        logging.exception("Не удалось отправить уведомление о переводе")

async def tr_edit(conn_id, chat_id, mid, text):
    try:
        await bot.edit_message_text(text, business_connection_id=conn_id, chat_id=chat_id, message_id=mid)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        raise

async def tr_strip_command(conn_id, message, body, owner_id, reason):
    """Команда не сработала: убираем её из сообщения, оставляем исходный текст, сообщаем причину в личку."""
    try:
        if body:
            await tr_edit(conn_id, message.chat.id, message.message_id, body)
    except Exception:
        logging.exception("Не удалось убрать команду перевода")
        reason += "\nНе получилось отредактировать сообщение — проверь, что у бота есть право отвечать на сообщения (Настройки → Business → Чат-боты)."
    await tr_notify(owner_id, reason)

async def tr_undo(message, conn_id, owner_id):
    rep = getattr(message, "reply_to_message", None)
    if not rep:
        await tr_notify(owner_id, "↩️ Чтобы вернуть оригинал, ответь командой +оригинал на переведённое сообщение.")
        return True
    row = db.execute("SELECT orig_text, status FROM translations WHERE conn_id=? AND chat_id=? AND message_id=?",
                     (conn_id, message.chat.id, rep.message_id)).fetchone()
    if not row or row[1] != "done":
        await tr_notify(owner_id, "↩️ Это сообщение не было переведено ботом или перевод уже отменён.")
        return True
    try:
        await tr_edit(conn_id, message.chat.id, rep.message_id, row[0])
    except Exception:
        logging.exception("Не удалось вернуть оригинал")
        await tr_notify(owner_id, "⚠️ Не удалось вернуть оригинал. Попробуй ещё раз позже.")
        return True
    db.execute("UPDATE translations SET status='undone' WHERE conn_id=? AND chat_id=? AND message_id=?",
               (conn_id, message.chat.id, rep.message_id))
    db.commit()
    try:
        await bot.delete_business_messages(business_connection_id=conn_id, message_ids=[message.message_id])
    except Exception:
        await tr_notify(owner_id, "↩️ Оригинал возвращён. Саму команду +оригинал удали вручную.")
    return True

async def maybe_translate(message, conn_id, owner_id) -> bool:
    """Обрабатывает команды перевода/отмены в исходящих сообщениях владельца. True — это была команда."""
    if not message.from_user or message.from_user.id != owner_id or not tr_enabled():
        return False
    text = getattr(message, "text", None)
    if not text:
        return False
    if text.strip().lower() in TR_UNDO_CMDS:
        return await tr_undo(message, conn_id, owner_id)
    cmd = tr_parse_command(text)
    if not cmd:
        return False
    tl, body = cmd
    chat_id, mid = message.chat.id, message.message_id
    if db.execute("SELECT 1 FROM translations WHERE conn_id=? AND chat_id=? AND message_id=?",
                  (conn_id, chat_id, mid)).fetchone():
        return True
    if not body:
        await tr_notify(owner_id, "🌐 После команды нет текста. Пример: +перевод& привет, как дела?")
        return True
    if len(body) > TR_MAX_CHARS:
        await tr_strip_command(conn_id, message, body, owner_id,
                               f"🌐 Текст слишком длинный для перевода (больше {TR_MAX_CHARS} символов). Отправлен без перевода.")
        return True
    if tr_used_today(owner_id) >= tr_limit(owner_id):
        await tr_strip_command(
            conn_id, message, body, owner_id,
            f"🌐 Лимит переводов на сегодня исчерпан ({tr_used_today(owner_id)}/{tr_limit(owner_id)}). "
            + ("Завтра счётчик обнулится." if is_premium(owner_id) else
               f"В 💎 Premium — {PREMIUM_TRANSLATE_PER_DAY} в день. Сообщение отправлено без перевода."))
        return True
    try:
        result, detected = await tr_translate(body, tl)
    except Exception as e:
        logging.exception("Ошибка перевода")
        await tr_strip_command(conn_id, message, body, owner_id,
                               "🌐 Сервис перевода сейчас недоступен. Сообщение отправлено без перевода, счётчик не списан.")
        return True
    if not result or len(result) > 4096:
        await tr_strip_command(conn_id, message, body, owner_id,
                               "🌐 Не удалось получить корректный перевод. Сообщение отправлено без перевода, счётчик не списан.")
        return True
    if (detected or "").lower().split("-")[0] == tl:
        await tr_strip_command(conn_id, message, body, owner_id,
                               "🌐 Текст уже на нужном языке — оставил как есть, счётчик не списан.")
        return True
    try:
        await tr_edit(conn_id, chat_id, mid, result)
    except Exception:
        logging.exception("Не удалось отредактировать сообщение при переводе")
        await tr_notify(owner_id, "⚠️ Не удалось отредактировать сообщение. Проверь, что у бота есть право отвечать "
                                  "на сообщения (Настройки → Business → Чат-боты).")
        return True
    db.execute("INSERT OR IGNORE INTO translations (conn_id, chat_id, message_id, user_id, orig_text, translated, "
               "direction, status, created_at) VALUES (?,?,?,?,?,?,?, 'done', ?)",
               (conn_id, chat_id, mid, owner_id, body, result, "ru-en" if tl == "en" else "en-ru", now_ts()))
    db.commit()
    tr_register(owner_id)
    return True

def tr_is_own_edit(message, conn_id) -> bool:
    """Правка, которую сделал сам бот (перевод/отмена) — не считаем правкой пользователя."""
    if mj_is_own_edit(message, conn_id):
        return True
    row = db.execute("SELECT orig_text, translated FROM translations WHERE conn_id=? AND chat_id=? AND message_id=?",
                     (conn_id, message.chat.id, message.message_id)).fetchone()
    if not row:
        return False
    t = (getattr(message, "text", None) or "").strip()
    return t in ((row[0] or "").strip(), (row[1] or "").strip())


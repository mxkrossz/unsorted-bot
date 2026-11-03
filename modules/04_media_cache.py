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
    Данные владельца (экспорт) не трогаем."""
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
# Работает только если создан mtproto.json рядом с bot.py и установлен telethon.
MTPROTO_CONF = Path(BASE_DIR) / "mtproto.json"
MTPROTO_SESSION = os.path.join(BASE_DIR, "mtproto_bot")
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
            logging.info("MTProto не настроен (нет файла mtproto.json)")
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

    name = f"id{chat_id}"

    lines = [f"=== Экспорт чата: {name} ===\n"]
    lines.append(f"Сообщений: {len(rows)}\n")
    lines.append(f"Дата экспорта: {datetime.now(TZ).strftime('%H:%M %d.%m.%y')}\n")
    lines.append("=" * 40 + "\n\n")

    for tag, body, time_str in rows:
        lines.append(f"{tag}\n{body}\n({time_str})\n---\n")

    return "\n".join(lines).encode("utf-8")



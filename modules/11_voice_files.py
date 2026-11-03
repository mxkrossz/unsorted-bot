# ---------- Распознавание голосовых и проверка файлов ----------

import shutil as _shutil
import tempfile as _tempfile
import zipfile as _zipfile
import subprocess as _subprocess
from collections import OrderedDict as _OD

FREE_STT_PER_DAY = 5            # базовый тариф: расшифровок голосовых в день
FREE_SCAN_PER_DAY = 1           # базовый тариф: проверок файлов в день
FREE_SCAN_MAX_MB = 50           # базовый тариф: максимальный размер файла
PREMIUM_SCAN_MAX_MB = 200       # Premium: без лимита по количеству, файлы до 200 МБ
STT_MODEL = "small"             # модель faster-whisper (tiny/base/small/medium)
STT_MAX_SEC = 900               # самое длинное голосовое: 15 минут
STT_TIMEOUT = 1500
SCAN_TIMEOUT = 300
MJ_MAX_BYTES = (200 if LOCAL_API_URL else 20) * 1024 * 1024  # 20 МБ — лимит облачного Bot API, 200 МБ — через локальный сервер
MJ_TTL = {"rec": 30 * 86400, "scan": 86400}

for _t in ("mj_usage (user_id INTEGER, kind TEXT, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY (user_id, kind, day))",
           "mj_jobs (owner_id INTEGER, kind TEXT, fuid TEXT, result TEXT, created_at INTEGER, PRIMARY KEY (owner_id, kind, fuid))"):
    db.execute("CREATE TABLE IF NOT EXISTS " + _t)
db.commit()

_mj_done = _OD()
_mj_own = {}
_stt_model = None
_stt_lock = None

def mj_kind_of(text):
    return {"+распознать&": "rec", "+проверка&": "scan"}.get((text or "").strip().lower())

def mj_enabled() -> bool:
    return get_setting("mj_off", "0") != "1"

def mj_limit(uid, kind):
    if kind == "rec":
        return None if is_premium(uid) else FREE_STT_PER_DAY
    return None if is_premium(uid) else FREE_SCAN_PER_DAY

def mj_used(uid, kind) -> int:
    row = db.execute("SELECT n FROM mj_usage WHERE user_id=? AND kind=? AND day=?", (uid, kind, today_key())).fetchone()
    return row[0] if row else 0

def mj_register(uid, kind):
    db.execute("INSERT INTO mj_usage (user_id, kind, day, n) VALUES (?,?,?,1) "
               "ON CONFLICT(user_id, kind, day) DO UPDATE SET n = n + 1", (uid, kind, today_key()))
    db.commit()

def mj_is_own_edit(message, conn_id) -> bool:
    """Правка, которую сделал сам бот при распознавании/проверке."""
    ts = _mj_own.get((conn_id, message.chat.id, message.message_id))
    return bool(ts) and time.time() - ts < 3600

async def mj_edit(conn_id, chat_id, mid, text):
    _mj_own[(conn_id, chat_id, mid)] = time.time()
    if len(_mj_own) > 500:
        cut = time.time() - 3600
        for k in [k for k, v in _mj_own.items() if v < cut]:
            _mj_own.pop(k, None)
    try:
        await tr_edit(conn_id, chat_id, mid, text[:4096])
        return True
    except Exception:
        logging.exception("Не удалось отредактировать служебное сообщение")
        return False

async def mj_pre_fail(conn_id, message, owner_id, text):
    """Команда не выполнена до начала работы: убираем её из чата, причину пишем владельцу в личку."""
    try:
        await bot.delete_business_messages(business_connection_id=conn_id, message_ids=[message.message_id])
    except Exception:
        text += "\n(Саму команду из чата удали вручную.)"
    await tr_notify(owner_id, text)

def mj_type_name(m) -> str:
    for attr, name in (("voice", "голосовое"), ("video_note", "кружок"), ("document", "файл"), ("audio", "музыку/аудио"),
                       ("photo", "фото"), ("video", "видео"), ("sticker", "стикер"), ("animation", "GIF"),
                       ("text", "текстовое сообщение")):
        if getattr(m, attr, None):
            return name
    return "сообщение"

def mj_fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024

def mj_split(text, limit=3900):
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + len(para) + 2 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur:
        parts.append(cur)
    return parts or [""]

# --- запуск команд ---

async def maybe_media_cmd(message, conn_id, owner_id) -> bool:
    """+распознать& / +проверка& — только ответом на голосовое/кружок или на файл."""
    if not message.from_user or message.from_user.id != owner_id:
        return False
    kind = mj_kind_of(getattr(message, "text", None))
    if not kind:
        return False
    chat_id, mid = message.chat.id, message.message_id
    key = (conn_id, chat_id, mid)
    if key in _mj_done:
        return True
    _mj_done[key] = 1
    while len(_mj_done) > 2000:
        _mj_done.popitem(last=False)
    cmd = "+распознать&" if kind == "rec" else "+проверка&"
    if not mj_enabled():
        await mj_pre_fail(conn_id, message, owner_id, f"⚙️ Команда {cmd} сейчас отключена владельцем бота.")
        return True
    rep = getattr(message, "reply_to_message", None)
    if not rep:
        what = "голосовое сообщение или кружок" if kind == "rec" else "сообщение с файлом"
        await mj_pre_fail(conn_id, message, owner_id,
                          f"ℹ️ Команду {cmd} нужно отправлять ОТВЕТОМ на {what}. Свайпни нужное сообщение и напиши команду.")
        return True
    media = (getattr(rep, "voice", None) or getattr(rep, "video_note", None)) if kind == "rec" else getattr(rep, "document", None)
    if not media:
        need = "голосовые сообщения и кружки" if kind == "rec" else "файлы (документы)"
        await mj_pre_fail(conn_id, message, owner_id,
                          f"ℹ️ Команда {cmd} работает только на {need}, а ты ответил на: {mj_type_name(rep)}.")
        return True
    fuid = getattr(media, "file_unique_id", None) or ""
    if fuid:
        row = db.execute("SELECT result, created_at FROM mj_jobs WHERE owner_id=? AND kind=? AND fuid=?",
                         (owner_id, kind, fuid)).fetchone()
        if row and now_ts() - row[1] < MJ_TTL[kind]:
            if not await mj_edit(conn_id, chat_id, mid, row[0]):
                await tr_notify(owner_id, "⚠️ Не удалось отредактировать сообщение. Проверь право бота отвечать на сообщения.")
            return True
    if kind == "scan" and not is_premium(owner_id):
        fsz = int(getattr(media, "file_size", 0) or 0)
        if fsz > FREE_SCAN_MAX_MB * 1024 * 1024:
            await mj_pre_fail(conn_id, message, owner_id,
                              f"⚠️ Файл больше {FREE_SCAN_MAX_MB} МБ ({mj_fmt_size(fsz)}). Без Premium проверяются файлы до "
                              f"{FREE_SCAN_MAX_MB} МБ, в 💎 Premium — до {PREMIUM_SCAN_MAX_MB} МБ. Лимит не списан.")
            return True
    lim = mj_limit(owner_id, kind)
    if lim is not None and mj_used(owner_id, kind) >= lim:
        what = "расшифровок" if kind == "rec" else "проверок файлов"
        up = (f"В 💎 Premium — {'без лимита' if kind == 'rec' else 'без лимита и файлы до ' + str(PREMIUM_SCAN_MAX_MB) + ' МБ'}."
              if not is_premium(owner_id) else "Завтра счётчик обнулится.")
        await mj_pre_fail(conn_id, message, owner_id,
                          f"⚠️ Лимит {what} на сегодня исчерпан ({mj_used(owner_id, kind)}/{lim}). {up}")
        return True
    t = asyncio.create_task(mj_worker(kind, conn_id, message, media, owner_id, fuid, getattr(rep, "video_note", None) is media))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return True

async def mj_worker(kind, conn_id, message, media, owner_id, fuid, is_note):
    chat_id, mid = message.chat.id, message.message_id
    try:
        if kind == "rec":
            chunks, charge, cache = await asyncio.wait_for(mj_do_rec(conn_id, chat_id, mid, media, is_note), STT_TIMEOUT)
        else:
            chunks, charge, cache = await asyncio.wait_for(mj_do_scan(conn_id, chat_id, mid, media), 3600)
    except asyncio.TimeoutError:
        await mj_edit(conn_id, chat_id, mid, "❌ Не удалось завершить вовремя — обработка остановлена. Попробуй ещё раз позже. Лимит не списан.")
        return
    except ModuleNotFoundError:
        logging.exception("Не установлен компонент для распознавания")
        await mj_edit(conn_id, chat_id, mid, "❌ Сервис распознавания сейчас недоступен. Лимит не списан.")
        await tr_notify(owner_id, "⚠️ На сервере не установлен faster-whisper. Установи по инструкции и напиши /media.")
        return
    except Exception:
        logging.exception("Ошибка обработки команды")
        await mj_edit(conn_id, chat_id, mid, "❌ Не удалось обработать. Лимит не списан.")
        return
    ok = await mj_edit(conn_id, chat_id, mid, chunks[0])
    if not ok:
        await tr_notify(owner_id, "⚠️ Не удалось показать результат в чате. Проверь право бота отвечать на сообщения. Результат:\n\n" + chunks[0][:3500])
    for extra in chunks[1:]:
        try:
            await bot.send_message(chat_id=chat_id, text=extra, business_connection_id=conn_id)
        except Exception:
            logging.exception("Не удалось отправить продолжение")
            await tr_notify(owner_id, "⚠️ Результат длиннее одного сообщения, часть не отправилась в чат.")
            break
    if charge:
        mj_register(owner_id, kind)
    if cache and fuid:
        db.execute("INSERT OR REPLACE INTO mj_jobs (owner_id, kind, fuid, result, created_at) VALUES (?,?,?,?,?)",
                   (owner_id, kind, fuid, chunks[0] if len(chunks) == 1 else cache[:4000], now_ts()))
        db.execute("DELETE FROM mj_jobs WHERE created_at < ?", (now_ts() - 31 * 86400,))
        db.commit()

# --- распознавание речи (faster-whisper на сервере) ---

_STT_PHANTOM = ("продолжение следует", "субтитры", "спасибо за просмотр", "thanks for watching",
                "thank you for watching", "подписывайтесь", "редактор субтитров", "корректор")
_LANG_NAMES = {"ru": "русский", "en": "английский", "uk": "украинский", "be": "белорусский", "kk": "казахский",
               "de": "немецкий", "fr": "французский", "es": "испанский", "it": "итальянский", "pt": "португальский",
               "tr": "турецкий", "pl": "польский", "ar": "арабский", "zh": "китайский", "ja": "японский",
               "ko": "корейский", "uz": "узбекский", "az": "азербайджанский", "hy": "армянский", "ka": "грузинский"}

def _stt_sync(path):
    global _stt_model
    if _stt_model is None:
        from faster_whisper import WhisperModel
        _stt_model = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
    segs, info = _stt_model.transcribe(path, beam_size=1, vad_filter=True, condition_on_previous_text=False)
    out = [(float(s.start), float(s.end), (s.text or "").strip(), float(s.avg_logprob), float(s.no_speech_prob)) for s in segs]
    return out, info.language, float(info.language_probability)

async def stt_transcribe(path):
    global _stt_lock
    if _stt_lock is None:
        _stt_lock = asyncio.Lock()
    async with _stt_lock:
        return await asyncio.get_running_loop().run_in_executor(None, _stt_sync, path)

def stt_format(segs, lang_prob):
    """-> ('ok'|'nospeech'|'unclear', текст с абзацами)"""
    keep = [s for s in segs if s[2] and not (s[4] > 0.6 and s[3] < -0.8)]
    if not keep:
        return "nospeech", ""
    w = [max(0.1, s[1] - s[0]) for s in keep]
    mean_lp = sum(s[3] * x for s, x in zip(keep, w)) / sum(w)
    joined = " ".join(s[2] for s in keep).strip()
    if len(joined) < 80 and any(p in joined.lower() for p in _STT_PHANTOM):
        return "nospeech", ""
    if mean_lp < -1.1 or (lang_prob < 0.35 and mean_lp < -0.7):
        return "unclear", ""
    paras, cur, last_end = [], "", None
    for s in keep:
        gap = (s[0] - last_end) if last_end is not None else 0
        if cur and (gap > 1.8 or (len(cur) > 380 and cur[-1] in ".!?…")):
            paras.append(cur)
            cur = ""
        cur = (cur + " " + s[2]).strip()
        last_end = s[1]
    if cur:
        paras.append(cur)
    return "ok", "\n\n".join(paras)

async def mj_do_rec(conn_id, chat_id, mid, media, is_note):
    dur = int(getattr(media, "duration", 0) or 0)
    size = int(getattr(media, "file_size", 0) or 0)
    what = "Кружок" if is_note else "Голосовое"
    if dur > STT_MAX_SEC:
        return [f"⚠️ {what} слишком длинное ({dur // 60} мин). Максимум — {STT_MAX_SEC // 60} мин. Лимит не списан."], False, None
    if size > MJ_MAX_BYTES:
        return [f"⚠️ Запись больше {MJ_MAX_BYTES >> 20} МБ — слишком большая. Лимит не списан."], False, None
    await mj_edit(conn_id, chat_id, mid, "⏳ Получаю запись…")
    d = _tempfile.mkdtemp(prefix="mj_")
    try:
        path = os.path.join(d, "a.bin")
        f = await bot.get_file(media.file_id)
        await bot.download_file(f.file_path, destination=path, timeout=120)
        if _stt_lock is not None and _stt_lock.locked():
            await mj_edit(conn_id, chat_id, mid, "⏳ В очереди на распознавание…")
        else:
            await mj_edit(conn_id, chat_id, mid, f"🎙 Распознаю речь… ({dur // 60}:{dur % 60:02d})")
        segs, lang, lprob = await stt_transcribe(path)
    finally:
        _shutil.rmtree(d, ignore_errors=True)
    state, text = stt_format(segs, lprob)
    if state == "nospeech":
        return ["🎙 В записи не найдено распознаваемой речи."], True, None
    if state == "unclear":
        return ["🎙 Речь неразборчива — надёжно распознать не удалось."], True, None
    full = f"🎙 Расшифровка ({_LANG_NAMES.get(lang, lang)}):\n\n{text}"
    return mj_split(full), True, full

# --- проверка файлов ---

_EXEC_EXT = {"exe", "scr", "bat", "cmd", "com", "pif", "js", "jse", "vbs", "vbe", "wsf", "hta", "lnk", "ps1",
             "msi", "apk", "jar", "dll", "sh", "reg", "xapk", "apks"}
_SAFE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "pdf", "txt", "doc", "docx", "xls", "xlsx", "ppt",
             "pptx", "mp3", "mp4", "mov", "avi", "mkv", "wav", "ogg", "m4a", "csv", "rtf"}
_FMT_NAMES = {"exe": "программа Windows (exe/dll)", "elf": "программа Linux", "apk": "приложение Android (apk)",
              "jar": "Java-программа (jar)", "dex": "код Android (dex)", "lnk": "ярлык Windows", "script": "скрипт",
              "zip": "архив ZIP", "rar": "архив RAR", "7z": "архив 7z", "pdf": "документ PDF",
              "docx": "документ Word", "xlsx": "таблица Excel", "pptx": "презентация PowerPoint",
              "ole": "старый документ Office (doc/xls/ppt)", "rtf": "документ RTF", "png": "изображение PNG",
              "jpg": "изображение JPEG", "gif": "изображение GIF", "webp": "изображение WebP", "mp4": "видео/аудио MP4",
              "mp3": "аудио MP3", "ogg": "аудио OGG", "msi": "установщик Windows (msi)"}
_APK_PERMS = {
    "SEND_SMS": ("отправка SMS", 2), "READ_SMS": ("чтение SMS", 2), "RECEIVE_SMS": ("перехват входящих SMS", 2),
    "BIND_ACCESSIBILITY_SERVICE": ("управление экраном через «Специальные возможности»", 3),
    "BIND_DEVICE_ADMIN": ("права администратора устройства", 3),
    "SYSTEM_ALERT_WINDOW": ("показ окон поверх других приложений", 2),
    "REQUEST_INSTALL_PACKAGES": ("установка других приложений", 2),
    "BIND_NOTIFICATION_LISTENER_SERVICE": ("чтение всех уведомлений", 2),
    "READ_CALL_LOG": ("чтение журнала звонков", 2), "READ_CONTACTS": ("чтение контактов", 1),
    "RECORD_AUDIO": ("запись звука", 1), "CALL_PHONE": ("звонки без участия пользователя", 1),
    "MANAGE_EXTERNAL_STORAGE": ("доступ ко всем файлам", 1), "QUERY_ALL_PACKAGES": ("список всех приложений", 1),
    "DISABLE_KEYGUARD": ("отключение блокировки экрана", 1),
}
_SCRIPT_BAD = (b"-enc ", b"-encodedcommand", b"frombase64string", b"invoke-expression", b"downloadstring",
               b"downloadfile", b"certutil -urlcache", b"bitsadmin", b"mshta ", b"wscript.shell", b"createobject(",
               b"iex(", b"iex ", b"powershell -w hidden", b"-windowstyle hidden")

def mj_ext(name) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""

def mj_sniff(head):
    if head.startswith(b"MZ"):
        return "exe"
    if head.startswith(b"\x7fELF"):
        return "elf"
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    if head.startswith(b"dex\n"):
        return "dex"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"ID3"):
        return "mp3"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"L\x00\x00\x00\x01\x14\x02\x00"):
        return "lnk"
    if head.startswith(b"#!"):
        return "script"
    return None

def _mj_group(fmt) -> str:
    if fmt in ("exe", "elf", "apk", "jar", "dex", "lnk", "script", "msi"):
        return "exec"
    if fmt in ("zip", "rar", "7z"):
        return "archive"
    if fmt in ("pdf", "docx", "xlsx", "pptx", "ole", "rtf"):
        return "doc"
    return "media"

def _mj_apk_perms(manifest: bytes):
    found = set()
    pats = [manifest.decode("latin-1", "ignore"), manifest.decode("utf-16-le", "ignore"),
            manifest[1:].decode("utf-16-le", "ignore")]
    for s in pats:
        for m in _re.finditer(r"android\.permission\.([A-Z_]+)", s):
            found.add(m.group(1))
    return found

def _mj_an_zip(path, name, info):
    F = info["findings"]
    try:
        z = _zipfile.ZipFile(path)
    except Exception:
        info["error"] = "архив повреждён или имеет неподдерживаемую структуру"
        return
    with z:
        infos = z.infolist()[:20000]
        names = [i.filename for i in infos]
        low = [n.lower() for n in names]
        has_manifest = "androidmanifest.xml" in low
        has_dex = any(_re.fullmatch(r"classes\d*\.dex", n) for n in low)
        if has_manifest and has_dex:
            info["fmt"] = "apk"
        elif "[content_types].xml" in low:
            if any(n.startswith("word/") for n in low):
                info["fmt"] = "docx"
            elif any(n.startswith("xl/") for n in low):
                info["fmt"] = "xlsx"
            elif any(n.startswith("ppt/") for n in low):
                info["fmt"] = "pptx"
        elif "meta-inf/manifest.mf" in low:
            info["fmt"] = "jar"
        fmt = info["fmt"]
        if any(i.flag_bits & 1 for i in infos):
            F.append(("warn", "архив защищён паролем — содержимое проверить нельзя (так часто прячут вирусы от антивирусов)"))
        total = sum(i.file_size for i in infos)
        if total > 1 << 30 or (sum(i.compress_size for i in infos) and total / max(1, sum(i.compress_size for i in infos)) > 300 and total > 100 << 20):
            F.append(("warn", "подозрительно большой объём после распаковки (возможна «бомба» из архива)"))
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            F.append(("warn", "в архиве есть пути, выходящие за пределы папки распаковки"))
        if fmt in ("zip", "jar"):
            ex = [n for n in names if mj_ext(n) in _EXEC_EXT and not n.endswith("/")]
            if ex and fmt == "zip":
                F.append(("warn", "внутри архива исполняемые файлы: " + ", ".join(ex[:5]) + ("…" if len(ex) > 5 else "")))
        if fmt == "apk":
            _mj_an_apk(z, names, low, info)
        if fmt in ("docx", "xlsx", "pptx"):
            if any(n.endswith("vbaproject.bin") for n in low):
                F.append(("warn", "документ содержит макросы — программы, которые могут запускаться при открытии"))
            for n, l in zip(names, low):
                if l.endswith(".rels") and i_size_ok(z, n):
                    data = z.read(n)[:200000]
                    if b'TargetMode="External"' in data and (b"attachedTemplate" in data or b"oleObject" in data) and b"http" in data:
                        F.append(("warn", "документ подгружает внешний шаблон или объект из интернета"))
                        break

def i_size_ok(z, n) -> bool:
    try:
        return z.getinfo(n).file_size < 1 << 20
    except KeyError:
        return False

def _mj_an_apk(z, names, low, info):
    F = info["findings"]
    try:
        manifest = z.read("AndroidManifest.xml")[:5 << 20]
    except Exception:
        manifest = b""
    perms = _mj_apk_perms(manifest)
    hits = [(p, _APK_PERMS[p]) for p in sorted(perms) if p in _APK_PERMS]
    total = sum(h[1][1] for h in hits)
    sms = perms & {"SEND_SMS", "READ_SMS", "RECEIVE_SMS"}
    acc = "BIND_ACCESSIBILITY_SERVICE" in perms
    adm = "BIND_DEVICE_ADMIN" in perms
    info["perms"] = [h[1][0] for h in hits]
    strong = (acc and (sms or perms & {"SYSTEM_ALERT_WINDOW", "BIND_NOTIFICATION_LISTENER_SERVICE"})) or (adm and sms) or len(sms) == 3
    if strong or total >= 6:
        F.append(("warn", "приложение запрашивает опасное сочетание разрешений: " + ", ".join(h[1][0] for h in hits[:6]) +
                  " — так ведут себя банковские трояны"))
    elif hits:
        F.append(("info", "запрашивает разрешения: " + ", ".join(h[1][0] for h in hits[:6])))
    for n, l in zip(names, low):
        if (l.startswith("assets/") or l.startswith("res/raw/")) and mj_ext(l) in ("apk", "dex", "jar"):
            F.append(("warn", f"внутри приложения спрятан ещё один исполняемый файл ({n}) — так делают «дропперы»"))
            break
    sms_code = False
    for n, l in zip(names, low):
        if _re.fullmatch(r"classes\d*\.dex", l) and z.getinfo(n).file_size < (64 << 20):
            try:
                data = z.read(n)
            except Exception:
                continue
            if b"sendTextMessage" in data or b"abortBroadcast" in data:
                sms_code = True
                break
    if sms_code and sms:
        F.append(("warn", "в коде приложения есть отправка или перехват SMS"))

def _mj_list_7z(path):
    exe = _shutil.which("7z") or _shutil.which("7zz") or _shutil.which("7za")
    if not exe:
        return None, False
    try:
        p = _subprocess.run([exe, "l", "-slt", "-ba", "-p", path], capture_output=True, timeout=30, stdin=_subprocess.DEVNULL)
    except Exception:
        return None, False
    out = p.stdout.decode("utf-8", "ignore")
    names, enc = [], False
    for line in out.splitlines():
        if line.startswith("Path = "):
            names.append(line[7:].strip())
        elif line.startswith("Encrypted = +"):
            enc = True
    if p.returncode not in (0, 1) and not names:
        return None, "Wrong password" in out or "Enter password" in out
    return names, enc

def scan_analyze(path, name):
    """Разбор файла без запуска. -> dict(fmt, sha256, findings, perms, error)"""
    info = {"fmt": None, "sha256": "", "findings": [], "perms": [], "error": None}
    F = info["findings"]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        head = f.read(64)
        f.seek(0)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    info["sha256"] = h.hexdigest()
    ext = mj_ext(name)
    fmt = mj_sniff(head)
    if fmt is None and ext in ("bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "hta", "sh", "reg"):
        fmt = "script"
    if fmt == "ole" and ext == "msi":
        fmt = "msi"
    info["fmt"] = fmt
    if "‮" in name:
        F.append(("warn", "в названии файла есть скрытый символ, переворачивающий порядок букв (маскировка расширения)"))
    if _re.search(r"\.(pdf|docx?|xlsx?|pptx?|jpe?g|png|txt|mp[34]|zip|rar)\s*\.(exe|scr|bat|cmd|com|js|vbs|apk|lnk|ps1|jar|msi|hta)$", name.lower()):
        F.append(("warn", "у файла двойное расширение (например .pdf.exe) — типичная маскировка вредоносной программы"))
    if fmt == "zip":
        _mj_an_zip(path, name, info)
        fmt = info["fmt"]
    elif fmt in ("rar", "7z"):
        names, enc = _mj_list_7z(path)
        if enc:
            F.append(("warn", "архив защищён паролем — содержимое проверить нельзя (так часто прячут вирусы от антивирусов)"))
        if names:
            ex = [n for n in names if mj_ext(n) in _EXEC_EXT]
            if ex:
                F.append(("warn", "внутри архива исполняемые файлы: " + ", ".join(ex[:5]) + ("…" if len(ex) > 5 else "")))
        elif not enc:
            F.append(("info", "состав архива просмотреть не удалось (проверяет только антивирус)"))
    elif fmt == "pdf":
        with open(path, "rb") as f:
            d = f.read(8 << 20)
        js = b"/JavaScript" in d or b"/JS" in d
        if b"/Launch" in d:
            F.append(("warn", "PDF умеет запускать программы на компьютере (/Launch)"))
        if js and (b"/OpenAction" in d or b"/AA" in d):
            F.append(("warn", "в PDF есть скрипт, который запускается сразу при открытии"))
        elif js:
            F.append(("info", "PDF содержит скрипты JavaScript"))
        if b"/EmbeddedFile" in d:
            F.append(("info", "PDF содержит вложенные файлы"))
    elif fmt in ("ole", "msi"):
        with open(path, "rb") as f:
            d = f.read(20 << 20)
        if fmt == "ole" and (b"_VBA_PROJECT" in d or b"VBA" in d):
            F.append(("warn", "документ содержит макросы — программы, которые могут запускаться при открытии"))
            if any(x in d for x in (b"AutoOpen", b"Auto_Open", b"Document_Open", b"Workbook_Open")):
                F.append(("warn", "макрос запускается автоматически при открытии"))
    elif fmt == "rtf":
        with open(path, "rb") as f:
            d = f.read(8 << 20)
        if b"\\objdata" in d or b"\\objupdate" in d:
            F.append(("warn", "RTF содержит встроенный объект (так эксплуатируют уязвимости Word)"))
    elif fmt == "script":
        with open(path, "rb") as f:
            d = f.read(300000).lower()
        bad = [x.decode() for x in _SCRIPT_BAD if x in d]
        if bad:
            F.append(("warn", "скрипт скачивает или скрыто запускает код (" + ", ".join(bad[:3]).strip() + ")"))
    elif fmt == "exe":
        with open(path, "rb") as f:
            d = f.read(4 << 20)
        if b"UPX0" in d or b"UPX!" in d:
            F.append(("info", "программа упакована (UPX) — это скрывает её устройство от анализа"))
    info["fmt"] = fmt
    group = _mj_group(fmt) if fmt else None
    if fmt and ext in _SAFE_EXT and group in ("exec", "archive") and not (fmt == "zip" and ext in ("docx", "xlsx", "pptx", "zip")):
        what = "программа" if group == "exec" else "архив"
        F.append(("warn", f"файл выдаёт себя за .{ext}, но по содержимому это {what} ({_FMT_NAMES.get(fmt, fmt)})"))
    elif fmt and ext in ("exe", "apk", "jar", "msi") and fmt not in ("exe", "apk", "jar", "msi", "zip", "ole"):
        F.append(("info", f"расширение .{ext} не совпадает с содержимым ({_FMT_NAMES.get(fmt, fmt)})"))
    if fmt is None and ext not in _SAFE_EXT and not info["error"]:
        info["error"] = "формат файла не опознан, проверить структуру нельзя"
    if group == "exec" and fmt in ("exe", "apk", "jar", "lnk", "script", "elf", "msi") and not any(s == "warn" for s, _ in F):
        F.append(("info", "это исполняемый файл: запускай и устанавливай его, только если уверен в отправителе"))
    return info

def mj_clam_available() -> bool:
    return bool(_shutil.which("clamdscan") or _shutil.which("clamscan"))

async def mj_clam(path):
    """-> ('clean'|'found'|'unavailable', название угрозы)"""
    for cmd in (["clamdscan", "--no-summary", "--fdpass", path], ["clamscan", "--no-summary", path]):
        if not _shutil.which(cmd[0]):
            continue
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _err = await asyncio.wait_for(proc.communicate(), 600)
        except Exception:
            logging.exception("ClamAV")
            continue
        text = out.decode("utf-8", "ignore")
        if proc.returncode == 0:
            return "clean", None
        if proc.returncode == 1:
            m = _re.search(r": (.+) FOUND", text)
            return "found", (m.group(1).strip() if m else "неизвестная угроза")
    return "unavailable", None

async def mj_bazaar(sha256):
    key = get_setting("abuse_key", "")
    if not key:
        return None
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post("https://mb-api.abuse.ch/api/v1/", data={"query": "get_info", "hash": sha256},
                              headers={"Auth-Key": key}) as r:
                data = await r.json(content_type=None)
    except Exception:
        logging.exception("MalwareBazaar")
        return None
    if isinstance(data, dict) and data.get("query_status") == "ok" and data.get("data"):
        d = data["data"][0]
        return d.get("signature") or d.get("file_type") or "вредоносный файл"
    return None

def scan_report(name, size, info, clam, mb, used_bazaar):
    bad = []
    if clam[0] == "found":
        bad.append(f"антивирус опознал файл как «{clam[1]}»")
    if mb:
        bad.append(f"файл найден в базе вредоносных файлов MalwareBazaar ({mb})")
    warns = [t for s, t in info["findings"] if s == "warn"]
    infos = [t for s, t in info["findings"] if s == "info"]
    if bad:
        head = "🚨 Обнаружена вредоносная активность"
        expl = ("Файл опознан как вредоносный. Не открывай и не устанавливай его, удали и предупреди отправителя — "
                "возможно, его аккаунт взломан.")
        lines = bad + warns
    elif info["error"] and not warns:
        head = "❓ Не удалось проверить"
        expl = "Причина: " + info["error"] + "."
        lines = []
    elif warns:
        head = "⚠️ Обнаружена потенциальная угроза"
        expl = ("Это не доказательство заражения, но признаки подозрительные. Лучше не открывать файл, "
                "пока не убедишься в отправителе.")
        lines = warns
    else:
        head = "✅ Угроз не обнаружено"
        expl = ("Известные угрозы не найдены. Это не гарантия безопасности: новые и целевые вредоносные файлы "
                "могут не определяться.")
        lines = []
    safe = "".join(c for c in name.replace("\u202e", "⟨RLO⟩") if c.isprintable())[:80] or "файл"
    fmt = _FMT_NAMES.get(info["fmt"], info["fmt"] or ext_or_unknown(name))
    out = [head, "", f"📄 Файл: {safe}", f"🗂 Формат: {fmt}", f"📦 Размер: {mj_fmt_size(size)}"]
    if lines:
        out += ["", "Что нашли:"] + [f"• {x}" for x in lines[:6]]
    if infos and not bad:
        out += ["", "Заметки:"] + [f"• {x}" for x in infos[:4]]
    out += ["", expl]
    how = []
    if clam[0] != "unavailable":
        how.append("антивирус ClamAV")
    if used_bazaar:
        how.append("база MalwareBazaar")
    how.append("анализ структуры файла")
    out.append("Проверено: " + ", ".join(how) + ".")
    if clam[0] == "unavailable":
        out.append("⚠️ Антивирус на сервере сейчас недоступен — проверена только структура файла.")
    return "\n".join(out)

def ext_or_unknown(name) -> str:
    e = mj_ext(name)
    return f".{e}" if e else "не определён"

SCAN_QUEUE_MAX = 8      # сколько проверок максимум ждёт в очереди
SCAN_PER_OWNER = 2      # сколько проверок одного пользователя может быть в очереди и в работе
_scan_sem = None
_scan_waiting = 0
_scan_owner = {}
_scan_inflight = set()

async def mj_do_scan(conn_id, chat_id, mid, doc):
    """Проверки идут строго по одной; остальные ждут в очереди."""
    global _scan_sem, _scan_waiting
    if _scan_sem is None:
        _scan_sem = asyncio.Semaphore(1)
    size = int(getattr(doc, "file_size", 0) or 0)
    fuid = getattr(doc, "file_unique_id", None) or ""
    if fuid and (conn_id, fuid) in _scan_inflight:
        return ["⏳ Этот файл уже проверяется — результат появится в первом сообщении с командой. Лимит не списан."], False, None
    if _scan_owner.get(conn_id, 0) >= SCAN_PER_OWNER:
        return [f"⚠️ У тебя уже {SCAN_PER_OWNER} проверки в очереди. Дождись их завершения. Лимит не списан."], False, None
    if _scan_waiting >= SCAN_QUEUE_MAX:
        return ["⚠️ Очередь проверок сейчас заполнена. Попробуй через пару минут. Лимит не списан."], False, None
    _scan_owner[conn_id] = _scan_owner.get(conn_id, 0) + 1
    if fuid:
        _scan_inflight.add((conn_id, fuid))
    try:
        if _scan_sem.locked():
            ahead = _scan_waiting + 1
            _scan_waiting += 1
            try:
                await mj_edit(conn_id, chat_id, mid, f"⏳ Проверка в очереди. Перед тобой: {ahead}")
                await _scan_sem.acquire()
            finally:
                _scan_waiting -= 1
        else:
            await _scan_sem.acquire()
        try:
            return await asyncio.wait_for(mj_do_scan_inner(conn_id, chat_id, mid, doc), SCAN_TIMEOUT + (size >> 20) * 4)
        finally:
            _scan_sem.release()
    finally:
        _scan_owner[conn_id] = max(0, _scan_owner.get(conn_id, 1) - 1)
        _scan_inflight.discard((conn_id, fuid))

async def mj_do_scan_inner(conn_id, chat_id, mid, doc):
    name = (getattr(doc, "file_name", None) or "файл").strip()
    size = int(getattr(doc, "file_size", 0) or 0)
    await mj_edit(conn_id, chat_id, mid, "⏳ Получение файла…")
    if size > MJ_MAX_BYTES:
        return [f"❓ Не удалось проверить\n\n📄 Файл: {name[:80]}\n📦 Размер: {mj_fmt_size(size)}\n\n"
                f"Причина: файл больше {MJ_MAX_BYTES >> 20} МБ — это максимальный размер для проверки. "
                f"Проверка не выполнялась. Лимит не списан."], False, None
    d = _tempfile.mkdtemp(prefix="mj_")
    try:
        path = os.path.join(d, "f.bin")
        f = await bot.get_file(doc.file_id)
        await bot.download_file(f.file_path, destination=path, timeout=120)
        await mj_edit(conn_id, chat_id, mid, "🔍 Анализ структуры файла…")
        info = await asyncio.get_running_loop().run_in_executor(None, scan_analyze, path, name)
        clam = ("unavailable", None)
        if mj_clam_available():
            await mj_edit(conn_id, chat_id, mid, "🛡 Проверка на вредоносное содержимое…")
            clam = await mj_clam(path)
        mb, used_bazaar = None, bool(get_setting("abuse_key", ""))
        if used_bazaar:
            await mj_edit(conn_id, chat_id, mid, "🔎 Проверка по базам угроз…")
            mb = await mj_bazaar(info["sha256"])
        await mj_edit(conn_id, chat_id, mid, "📋 Формирование результата…")
    finally:
        _shutil.rmtree(d, ignore_errors=True)
    text = scan_report(name, size, info, clam, mb, used_bazaar)
    return [text], True, text

# --- команды владельца ---

@dp.message(Command("media", "media_on", "media_off", "abusekey", "abusekey_off"))
async def cmd_media(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    if cmd == "media_on":
        set_setting("mj_off", "0")
        await message.answer("✅ Распознавание и проверка файлов включены.")
    elif cmd == "media_off":
        set_setting("mj_off", "1")
        await message.answer("⏸ Распознавание и проверка файлов выключены для всех.")
    elif cmd == "abusekey":
        if len(parts) < 2:
            await message.answer("Формат: /abusekey КЛЮЧ")
            return
        set_setting("abuse_key", parts[1].strip())
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔑 Ключ abuse.ch сохранён (сообщение с ключом удалено). Хеши файлов теперь сверяются с MalwareBazaar.")
    elif cmd == "abusekey_off":
        del_setting("abuse_key")
        await message.answer("🔑 Ключ abuse.ch удалён.")
    else:
        import importlib.util as _iu
        stt = "установлен" if _iu.find_spec("faster_whisper") else "НЕ установлен"
        clam = ("clamdscan" if _shutil.which("clamdscan") else "clamscan" if _shutil.which("clamscan") else "НЕ установлен")
        rar = _shutil.which("7z") or _shutil.which("7zz") or _shutil.which("7za")
        await message.answer(
            "🎙 Распознавание и 🔍 проверка файлов\n"
            f"Статус: {'выключены' if get_setting('mj_off', '0') == '1' else 'включены'}\n"
            f"faster-whisper: {stt} (модель {STT_MODEL})\n"
            f"ClamAV: {clam}\n"
            f"7z (просмотр rar/7z): {'есть' if rar else 'нет'}\n"
            f"Ключ abuse.ch (MalwareBazaar): {'есть' if get_setting('abuse_key', '') else 'нет'}\n"
            f"Лимиты в день: расшифровки {FREE_STT_PER_DAY} / без лимита; файлы {FREE_SCAN_PER_DAY} (до {FREE_SCAN_MAX_MB} МБ) / без лимита (до {PREMIUM_SCAN_MAX_MB} МБ)\n\n"
            "/media_on, /media_off — включить или выключить\n/abusekey КЛЮЧ, /abusekey_off")


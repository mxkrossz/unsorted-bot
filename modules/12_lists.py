# ---------- Списки из сообщений (+список&) ----------

FREE_LIST_PER_DAY = 1           # базовый тариф: списков в день (Premium — без лимита)
LS_MAX_CHARS = 4000

_LS_SHOP = {"купи", "купите", "купить", "возьми", "возьмите", "взять", "закажи", "закажите", "заказать",
            "докупи", "докупите", "докупить", "прихвати", "прихватить", "захвати", "захватить", "прикупи", "прикупить"}
_LS_TASK = {"позвони", "позвоните", "напиши", "напишите", "отправь", "отправьте", "оплати", "оплатите", "проверь",
            "проверьте", "сделай", "сделайте", "запиши", "запишись", "забери", "заберите", "привези", "привезите",
            "принеси", "принесите", "отвези", "отнеси", "передай", "скажи", "узнай", "найди", "подготовь",
            "выбери", "поменяй", "почини", "убери", "помой", "приготовь", "сходи", "съезди", "заедь", "зайди",
            "встреть", "подпиши", "продли", "верни", "верните", "ответь", "договорись", "напомни", "включи",
            "выключи", "закрой", "открой", "отмени", "перенеси", "зарегистрируй", "оформи", "положи",
            "забеги", "заскочи", "загляни", "закинь", "сбегай", "выгуляй", "покорми", "полей", "постирай",
            "вынеси", "заправь", "погуляй", "съешь", "выпей", "заплати", "пополни", "уточни", "спроси"}
_LS_FILLER = _re.compile(
    r"^(?:(?:а|и|ну|ещё|еще|также|кстати|плюс|потом|затем|тоже|пожалуйста|вообще|короче|слушай|"
    r"не\s+забудь(?:те)?|не\s+забыть|нужно|надо|необходимо|нужен|нужна|нужны|ещё\s+нужно|еще\s+нужно)"
    r"(?=[\s,:—-]|$)[\s,:—-]*)+", _re.I)
_LS_GREET = _re.compile(r"^(?:привет|здравствуй(?:те)?|добрый\s+день|доброе\s+утро|добрый\s+вечер|хай)[\s,!.]*", _re.I)
_LS_ATTACH = ("если", "когда", "пока", "чтобы", "лучше", "желательно", "только", "но", "можно", "например",
              "главное", "примерно", "около", "не меньше", "не больше", "минимум", "максимум", "а лучше", "типа")
_LS_PROSE = {"я", "мы", "он", "она", "они", "это", "что", "как", "почему", "потому", "мне", "тебе", "тебя", "меня",
             "вы", "ты", "у", "где", "когда", "зачем", "привет"}
_LS_BULLET = _re.compile(r"^\s*(?:[-–—•*·▪●◦]|\d{1,2}[.)])\s+")

def _ls_words(s):
    return _re.findall(r"[\w'-]+", s.lower())

def _ls_isverb(w):
    w = w.lower()
    return w in _LS_SHOP or w in _LS_TASK or bool(_re.fullmatch(r"[а-яё]{3,}(?:ть|ти|чь|ться|тись)", w))

def _ls_split_top(text, seps):
    """Делит по разделителям вне скобок. seps — список подстрок-разделителей (запятая и т.п.)."""
    parts, cur, depth = [], "", 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and ch in seps:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts

def _ls_clean(s):
    s = _re.sub(r"\s+", " ", s).strip(" \t.,;:!?—–-«»\"'")
    return s

def _ls_cap(s):
    return s[:1].upper() + s[1:] if s else s

def _ls_clause_items(clause, mode_in):
    """Одна фраза -> (режим, [пункты]). Режим: 'shop', 'task' или None."""
    c = _ls_clean(_LS_FILLER.sub("", _ls_clean(clause)))
    if not c:
        return mode_in, []
    cond = None
    m = _re.match(r"^(если\s[^,]+),\s*(.+)$", c, _re.I)
    if m:
        cond, c = m.group(1), m.group(2)
        c = _ls_clean(_LS_FILLER.sub("", c))
    words = c.split()
    first = words[0].lower() if words else ""
    mode = mode_in
    strip_verb = False
    if first in _LS_SHOP:
        mode, strip_verb = "shop", True
    elif first in _LS_TASK or _ls_isverb(first):
        mode = "task"
    if strip_verb:
        c = " ".join(words[1:])
        c = _ls_clean(_LS_FILLER.sub("", c))
    if not c:
        return mode, []
    pieces = []
    for chunk in _ls_split_top(c, ",;"):
        chunk = _ls_clean(chunk)
        if not chunk:
            continue
        low = chunk.lower()
        if pieces and any(low == a or low.startswith(a + " ") for a in _LS_ATTACH):
            pieces[-1] += f" ({chunk})"
            continue
        pieces.append(chunk)
    items = []
    for p in pieces:
        parts = [p]
        if not _re.search(r"\bи\s+т\.?\s*[дп]|и\s+так\s+далее|и\s+т\.д", p, _re.I):
            segs = _re.split(r"\s+(?:и|а также|плюс|да)\s+", p)
            if len(segs) > 1:
                ok = True
                for i in range(len(segs) - 1):
                    left = segs[i].split()
                    if len(left) == 1 and _re.search(r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие)$", left[0].lower()):
                        ok = False
                    if mode == "task" and not strip_verb and _ls_isverb(segs[0].split()[0]) and not _ls_isverb(segs[i + 1].split()[0]):
                        ok = False
                if ok:
                    parts = segs
        items += [x for x in (_ls_clean(t) for t in parts) if x]
    if cond and items:
        items[-1] += f" ({cond})"
    return mode, items

def ls_build(text):
    """Текст -> (список групп [(заголовок|None, [пункты])], причина отказа|None). Ничего не придумывает."""
    text = (text or "").replace("\r", "").strip()
    if not text:
        return None, "empty"
    text = _LS_GREET.sub("", text).strip()
    lines = [l for l in text.split("\n") if l.strip()]
    bullets = sum(1 for l in lines if _LS_BULLET.match(l))
    groups = []
    def add(title, items):
        for g in groups:
            if g[0] == title:
                g[1].extend(items)
                return
        groups.append((title, list(items)))
    if len(lines) >= 2 and (bullets >= 2 or sum(1 for l in lines if len(l.split()) <= 6) >= len(lines) - 1):
        title = None
        for l in lines:
            raw = _LS_BULLET.sub("", l).strip()
            if raw.endswith(":") and len(raw.split()) <= 6 and not _LS_BULLET.match(l):
                title = _ls_cap(raw.rstrip(":").strip()) + ":"
                add(title, [])
                continue
            it = _ls_clean(raw)
            if it:
                add(title, [_ls_cap(it)])
        groups = [g for g in groups if g[1]]
        n = sum(len(g[1]) for g in groups)
        return (groups, None) if n >= 2 else (None, "few")
    if "?" in text:
        return None, "prose"
    mode, order, by_mode, free_titles, has_header = None, [], {}, [], False
    sentences = [s for s in _re.split(r"(?<=[.!;])\s+|\n+", text) if s.strip()]
    verbs_seen, prose_seen = 0, 0
    for sent in sentences:
        body = sent.strip()
        m = _re.match(r"^([^:]{1,60}):\s*(.+)$", body, _re.S)
        title = None
        if m and len(m.group(1).split()) <= 6:
            title, body = _ls_cap(_ls_clean(m.group(1))) + ":", m.group(2)
            has_header = True
        fw = _ls_words(_LS_FILLER.sub("", body))[:1]
        if fw and fw[0] in _LS_PROSE and not title:
            prose_seen += 1
        mode_n, items = _ls_clause_items(body, mode)
        if mode_n != mode or (fw and _ls_isverb(fw[0])):
            verbs_seen += 1 if (fw and _ls_isverb(fw[0])) else 0
        mode = mode_n
        if not items:
            continue
        if title:
            add(title, [_ls_cap(i) for i in items])
        else:
            key = {"shop": "Список покупок:", "task": "Отдельные поручения:"}.get(mode)
            by_mode.setdefault(key, []).extend(_ls_cap(i) for i in items)
            if key not in order:
                order.append(key)
    for k in order:
        add(k, by_mode[k])
    total = sum(len(g[1]) for g in groups)
    if total < 2:
        return None, "few"
    if prose_seen and not has_header:
        return None, "prose"
    allitems = [i for g in groups for i in g[1]]
    if not has_header and any((_ls_words(i)[:1] or [""])[0] in _LS_PROSE for i in allitems):
        return None, "prose"
    avg = sum(len(i.split()) for i in allitems) / max(1, total)
    if not has_header and verbs_seen == 0 and total < 3 and avg > 5:
        return None, "prose"
    named = [g for g in groups if g[0]]
    if len(groups) == 1 and not has_header:
        groups = [(None, groups[0][1])]
    elif len(named) < len(groups) and named:
        groups = [(g[0] or "Остальное:", g[1]) for g in groups]
    return groups, None

def ls_render(groups) -> str:
    out = []
    for title, items in groups:
        block = ([title] if title else []) + ["• " + i for i in items]
        out.append("\n".join(block))
    return "\n\n".join(out)

def ls_limit(uid):
    return None if is_premium(uid) else FREE_LIST_PER_DAY

async def maybe_list_cmd(message, conn_id, owner_id) -> bool:
    """+список& — ответом на текстовое сообщение с перечислением."""
    if not message.from_user or message.from_user.id != owner_id:
        return False
    if (getattr(message, "text", None) or "").strip().lower() != "+список&":
        return False
    chat_id, mid = message.chat.id, message.message_id
    key = (conn_id, chat_id, mid)
    if key in _mj_done:
        return True
    _mj_done[key] = 1
    while len(_mj_done) > 2000:
        _mj_done.popitem(last=False)
    if not mj_enabled():
        await mj_pre_fail(conn_id, message, owner_id, "⚙️ Команда +список& сейчас отключена владельцем бота.")
        return True
    rep = getattr(message, "reply_to_message", None)
    if not rep:
        await mj_pre_fail(conn_id, message, owner_id,
                          "ℹ️ Команду +список& нужно отправлять ОТВЕТОМ на сообщение с перечислением. Свайпни нужное сообщение и напиши команду.")
        return True
    text = getattr(rep, "text", None) or getattr(rep, "caption", None)
    if not text:
        await mj_pre_fail(conn_id, message, owner_id,
                          f"ℹ️ Команда +список& работает только на текстовые сообщения, а ты ответил на: {mj_type_name(rep)}.")
        return True
    if len(text) > LS_MAX_CHARS:
        await mj_pre_fail(conn_id, message, owner_id, f"ℹ️ Сообщение слишком длинное для списка (больше {LS_MAX_CHARS} символов).")
        return True
    fuid = "t" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    row = db.execute("SELECT result, created_at FROM mj_jobs WHERE owner_id=? AND kind='list' AND fuid=?", (owner_id, fuid)).fetchone()
    if row and now_ts() - row[1] < 30 * 86400:
        if not await mj_edit(conn_id, chat_id, mid, row[0]):
            await tr_notify(owner_id, "⚠️ Не удалось отредактировать сообщение. Проверь право бота отвечать на сообщения.")
        return True
    lim = ls_limit(owner_id)
    if lim is not None and mj_used(owner_id, "list") >= lim:
        await mj_pre_fail(conn_id, message, owner_id,
                          f"⚠️ Лимит списков на сегодня исчерпан ({mj_used(owner_id, 'list')}/{lim}). "
                          f"Без лимита — в 💎 Premium. Купить: {OWNER_CONTACT}")
        return True
    groups, why = ls_build(text)
    if not groups:
        await mj_pre_fail(conn_id, message, owner_id,
                          "ℹ️ В этом сообщении я не нашёл перечисления, из которого можно сделать список. Лимит не списан.")
        return True
    result = ls_render(groups)
    chunks = mj_split(result)
    if not await mj_edit(conn_id, chat_id, mid, chunks[0]):
        await tr_notify(owner_id, "⚠️ Не удалось показать список в чате. Проверь право бота отвечать на сообщения. Список:\n\n" + chunks[0][:3500])
        return True
    for extra in chunks[1:]:
        try:
            await bot.send_message(chat_id=chat_id, text=extra, business_connection_id=conn_id)
        except Exception:
            logging.exception("Не удалось отправить продолжение списка")
            break
    mj_register(owner_id, "list")
    db.execute("INSERT OR REPLACE INTO mj_jobs (owner_id, kind, fuid, result, created_at) VALUES (?,?,?,?,?)",
               (owner_id, "list", fuid, chunks[0], now_ts()))
    db.commit()
    return True


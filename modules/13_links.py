# ---------- Защита от мошеннических ссылок ----------

import ipaddress as _ipaddr
import socket as _socket
from urllib.parse import urlsplit as _urlsplit, urljoin as _urljoin

LG_FEEDS = {
    "urlhaus": ("urls", "https://urlhaus.abuse.ch/downloads/text_online/"),
    "phishing": ("hosts", "https://raw.githubusercontent.com/mitchellkrogza/Phishing.Database/master/phishing-domains-ACTIVE.txt"),
}
LG_CACHE_TTL = 24 * 3600
LG_ALERT_TTL = 30 * 86400
LG_MAX_URLS = 5
LG_MAX_FEED_BYTES = 80 * 1024 * 1024

LG_TRUSTED = (
    "telegram.org", "t.me", "telegram.me", "google.com", "youtube.com", "youtu.be", "wikipedia.org",
    "yandex.ru", "ya.ru", "vk.com", "mail.ru", "apple.com", "microsoft.com", "github.com", "gosuslugi.ru",
    "sberbank.ru", "sber.ru", "tinkoff.ru", "tbank.ru", "alfabank.ru", "vtb.ru", "ozon.ru", "wildberries.ru",
    "avito.ru", "amazon.com", "instagram.com", "facebook.com", "whatsapp.com", "paypal.com", "steampowered.com",
    "netflix.com", "spotify.com", "discord.com", "x.com", "twitter.com", "linkedin.com", "reddit.com",
)
LG_SHORTENERS = ("bit.ly", "tinyurl.com", "cutt.ly", "clck.ru", "goo.gl", "is.gd", "ow.ly", "rb.gy",
                 "vk.cc", "tiny.cc", "t.ly", "shorturl.at", "u.to", "clc.to")
LG_BAD_TLDS = ("zip", "mov", "top", "xyz", "click", "tk", "ml", "ga", "cf", "gq", "work", "support",
               "icu", "buzz", "rest", "monster", "cfd", "sbs", "cyou", "quest", "link", "country", "loan")
LG_KEYWORDS = ("login", "signin", "verify", "secure", "account", "update", "wallet", "bonus", "gift", "airdrop",
               "claim", "confirm", "password", "payment", "support", "unlock", "prize", "free", "gosuslugi",
               "подтверд", "выигр", "приз", "бонус", "вход", "пароль")
LG_BRANDS = {
    "sberbank": ("sberbank.ru", "sber.ru"), "sber": ("sberbank.ru", "sber.ru", "sber.com"),
    "tinkoff": ("tinkoff.ru", "tbank.ru"), "gosuslugi": ("gosuslugi.ru",),
    "alfabank": ("alfabank.ru",), "raiffeisen": ("raiffeisen.ru",), "yandex": ("yandex.ru", "ya.ru", "yandex.com"),
    "avito": ("avito.ru",), "ozon": ("ozon.ru",), "wildberries": ("wildberries.ru",),
    "paypal": ("paypal.com",), "apple": ("apple.com",), "icloud": ("icloud.com",), "microsoft": ("microsoft.com",),
    "google": ("google.com", "google.ru"), "facebook": ("facebook.com",), "instagram": ("instagram.com",),
    "whatsapp": ("whatsapp.com",), "telegram": ("telegram.org", "t.me", "telegram.me"),
    "binance": ("binance.com",), "coinbase": ("coinbase.com",), "metamask": ("metamask.io",),
    "steam": ("steampowered.com", "steamcommunity.com"), "netflix": ("netflix.com",), "amazon": ("amazon.com",),
    "discord": ("discord.com", "discord.gg"), "vkontakte": ("vk.com",),
}
_LG_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "4": "a", "7": "t", "$": "s",
                                "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
                                "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ı": "i", "ӏ": "l"})
_lg_limit_notified = set()

for _t in ("link_bad_hosts (host TEXT PRIMARY KEY, src TEXT)",
           "link_bad_urls (url TEXT PRIMARY KEY, src TEXT)",
           "link_cache (ckey TEXT PRIMARY KEY, level TEXT, score INTEGER, reasons TEXT, bl INTEGER, checked_at INTEGER)",
           "link_alerts (owner_id INTEGER, key TEXT, sender_id INTEGER, alerted_at INTEGER, PRIMARY KEY (owner_id, key))",
           "link_seen (owner_id INTEGER, day TEXT, key TEXT, PRIMARY KEY (owner_id, day, key))"):
    db.execute("CREATE TABLE IF NOT EXISTS " + _t)
db.commit()

def lg_enabled(uid) -> bool:
    return get_setting("lg_global_off", "0") != "1" and get_setting(f"lg_off_{uid}", "0") != "1"

def lg_limit(uid) -> int:
    return PREMIUM_LINKS_PER_DAY if is_premium(uid) else FREE_LINKS_PER_DAY

def lg_used_today(uid) -> int:
    return db.execute("SELECT COUNT(*) FROM link_seen WHERE owner_id=? AND day=?", (uid, today_key())).fetchone()[0]

def lg_host_in(host, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)

def lg_norm(raw):
    """Приводит адрес к единому виду. -> dict или None (если это не http/https-ссылка)."""
    u = (raw or "").strip().strip('<>"\'«»')
    u = u.rstrip(".,;:!?)]}»\"'")
    if not u or len(u) > 2048:
        return None
    if "://" not in u:
        u = "http://" + u
    try:
        sp = _urlsplit(u)
        host = (sp.hostname or "").lower().rstrip(".")
        port = sp.port
    except ValueError:
        return None
    if sp.scheme.lower() not in ("http", "https") or not host or " " in host:
        return None
    netloc = host + (f":{port}" if port and port not in (80, 443) else "")
    path = sp.path or "/"
    key = f"{sp.scheme.lower()}://{netloc}{path}" + (f"?{sp.query}" if sp.query else "")
    return {"key": key, "host": host, "port": port, "scheme": sp.scheme.lower(), "path": path,
            "query": sp.query, "userinfo": "@" in sp.netloc, "url": u}

def lg_extract(message):
    """Ссылки из сообщения: [(адрес, видимый текст или None)] — не больше LG_MAX_URLS."""
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    ents = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
    out, seen = [], set()
    def add(u, vis=None):
        if u and u not in seen and len(out) < LG_MAX_URLS:
            seen.add(u)
            out.append((u, vis))
    raw16 = text.encode("utf-16-le")
    for e in ents:
        try:
            visible = raw16[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
        except Exception:
            visible = ""
        if e.type == "url":
            add(visible)
        elif e.type == "text_link" and getattr(e, "url", None):
            add(e.url, visible)
    for m in _re.finditer(r'(?i)\b(?:https?://|www\.)[^\s<>"\']+', text):
        add(m.group(0))
    return out

def _lg_lev1(a, b) -> bool:
    """Отличаются ли строки не больше чем на одну правку."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(s) and s[i] == l[i]:
        i += 1
    return s[i:] == l[i + 1:]

def lg_heuristics(info, visible=None):
    """Признаки фишинга по самому адресу. -> (баллы, [причины])"""
    host, score, why = info["host"], 0, []
    labels = host.split(".")
    core_labels = labels[:-1] if len(labels) > 1 else labels
    tld = labels[-1]
    try:
        _ipaddr.ip_address(host)
        score += 40
        why.append("адрес указан цифрами (IP) вместо названия сайта")
        is_ip = True
    except ValueError:
        is_ip = False
    if info["userinfo"]:
        score += 50
        why.append("в адресе есть «@» — настоящий сайт скрыт после этого знака")
    if not is_ip:
        mixed = [l for l in core_labels if _re.search(r"[а-яёА-ЯЁ]", l) and _re.search(r"[a-zA-Z]", l)]
        if mixed:
            score += 50
            why.append("в названии смешаны русские и латинские буквы (подмена символов)")
        if any(l.startswith("xn--") for l in labels):
            score += 30
            try:
                shown = host.encode("ascii").decode("idna")
            except Exception:
                shown = host
            why.append(f"домен в кодировке punycode (может скрывать похожие символы): {shown}")
        official = False
        for brand, domains in LG_BRANDS.items():
            if lg_host_in(host, domains):
                official = True
                break
        if not official:
            tokens = [t for l in core_labels for t in _re.split(r"[-_]", l) if t]
            hit = None
            for brand in LG_BRANDS:
                for t in tokens:
                    sk = t.translate(_LG_CONFUSABLE).replace("rn", "m").replace("vv", "w")
                    if t == brand or (len(brand) >= 6 and brand in t):
                        hit = (brand, "в названии использовано имя «%s», но это не официальный сайт" % brand, 35)
                    elif sk == brand or (len(brand) >= 6 and _lg_lev1(sk, brand)):
                        hit = (brand, "название похоже на «%s» (подмена или опечатка в имени сайта)" % brand, 60)
                    if hit:
                        break
                if hit:
                    break
            if hit:
                score += hit[2]
                why.append(hit[1])
        if tld in LG_BAD_TLDS:
            score += 25
            why.append(f"зона .{tld} часто используется мошенниками")
        if len(labels) >= 5:
            score += 15
            why.append("слишком много поддоменов в адресе")
    if info["port"] and info["port"] not in (80, 443):
        score += 15
        why.append(f"нестандартный порт {info['port']}")
    blob = (host + " " + info["path"] + " " + info["query"]).lower()
    kw = [k for k in LG_KEYWORDS if k in blob]
    if len(kw) >= 2:
        score += 25
        why.append("в адресе слова, типичные для фишинга: " + ", ".join(kw[:3]))
    elif kw:
        score += 10
    if info["scheme"] == "http" and kw:
        score += 10
        why.append("нет шифрования (http) на странице с «входом» или «оплатой»")
    if visible:
        vi = lg_norm(visible) if _re.search(r"[a-zA-Zа-я0-9]\.[a-zA-Zа-я]{2,}", visible) else None
        if vi and vi["host"] != host and not vi["host"].endswith("." + host) and not host.endswith("." + vi["host"]):
            score += 50
            why.append(f"текст ссылки показывает {vi['host']}, а ведёт она на {host}")
    return score, why

def lg_ip_ok(ip) -> bool:
    try:
        a = _ipaddr.ip_address(ip)
    except ValueError:
        return False
    return a.is_global and not a.is_multicast

async def lg_url_allowed(url) -> bool:
    """Защита от SSRF: только http/https, порты 80/443, только публичные адреса."""
    try:
        sp = _urlsplit(url)
        host, port = sp.hostname, sp.port
    except ValueError:
        return False
    if sp.scheme not in ("http", "https") or not host or (port and port not in (80, 443)):
        return False
    try:
        _ipaddr.ip_address(host)
        return lg_ip_ok(host)
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port or 443, type=_socket.SOCK_STREAM)
    except Exception:
        return False
    ips = {i[4][0] for i in infos}
    return bool(ips) and all(lg_ip_ok(ip) for ip in ips)

async def lg_follow(url, hops=4):
    """Безопасно идёт по перенаправлениям (без скачивания страницы). -> [адреса цепочки]"""
    import aiohttp
    from aiohttp.abc import AbstractResolver

    class SafeResolver(AbstractResolver):
        async def resolve(self, host, port=0, family=_socket.AF_INET):
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=_socket.AF_UNSPEC,
                                                                 type=_socket.SOCK_STREAM)
            res = []
            for fam, _, _, _, sa in infos:
                if not lg_ip_ok(sa[0]):
                    raise OSError("blocked address")
                res.append({"hostname": host, "host": sa[0], "port": port, "family": fam,
                            "proto": 0, "flags": _socket.AI_NUMERICHOST})
            if not res:
                raise OSError("no address")
            return res
        async def close(self):
            pass

    chain = [url]
    conn = aiohttp.TCPConnector(resolver=SafeResolver(), limit=2, force_close=True)
    async with aiohttp.ClientSession(connector=conn, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=8),
                                     headers={"User-Agent": "Mozilla/5.0 (link-check)"}) as s:
        cur = url
        for _ in range(hops):
            if not await lg_url_allowed(cur):
                break
            try:
                async with s.head(cur, allow_redirects=False) as r:
                    status, loc = r.status, r.headers.get("Location")
                if status in (403, 405, 501):
                    async with s.get(cur, allow_redirects=False, headers={"Range": "bytes=0-0"}) as r:
                        status, loc = r.status, r.headers.get("Location")
            except Exception:
                break
            if status in (301, 302, 303, 307, 308) and loc:
                cur = _urljoin(cur, loc)
                chain.append(cur)
            else:
                break
    return chain

async def lg_gsb(url):
    """Google Safe Browsing — только если владелец сам добавил ключ (/linkkey). -> название угрозы или None"""
    key = get_setting("gsb_key", "")
    if not key:
        return None
    import aiohttp
    body = {"client": {"clientId": "unsorted-bot", "clientVersion": "1.0"},
            "threatInfo": {"threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE"],
                           "platformTypes": ["ANY_PLATFORM"], "threatEntryTypes": ["URL"],
                           "threatEntries": [{"url": url}]}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        async with s.post("https://safebrowsing.googleapis.com/v4/threatMatches:find",
                          params={"key": key}, json=body) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    m = (data or {}).get("matches")
    return m[0].get("threatType") if m else None

def lg_blocklist_hit(info):
    """Есть ли ссылка в базах известных вредоносных адресов. -> причина или None"""
    key = info["key"]
    alt = key.replace("https://", "http://", 1) if key.startswith("https://") else key.replace("http://", "https://", 1)
    if db.execute("SELECT 1 FROM link_bad_urls WHERE url IN (?, ?)", (key, alt)).fetchone():
        return "ссылка найдена в базе вредоносных адресов (URLhaus)"
    parts = info["host"].split(".")
    for i in range(0, max(1, len(parts) - 1)):
        h = ".".join(parts[i:])
        if db.execute("SELECT 1 FROM link_bad_hosts WHERE host=?", (h,)).fetchone():
            return "сайт найден в базе фишинговых ресурсов"
    return None

def lg_level(score, bl) -> str:
    if bl or score >= 60:
        return "high"
    return "medium" if score >= 30 else "low"

async def lg_analyze(info, visible, extended):
    """Проверка одной ссылки с кэшем. -> {level, score, reasons, bl}"""
    ckey = ("x|" if extended else "b|") + info["key"] + ("|" + visible if visible else "")
    row = db.execute("SELECT level, score, reasons, bl, checked_at FROM link_cache WHERE ckey=?", (ckey,)).fetchone()
    if row and now_ts() - row[4] < LG_CACHE_TTL:
        import json as _j
        return {"level": row[0], "score": row[1], "reasons": _j.loads(row[2]), "bl": bool(row[3])}
    reasons, score, bl = [], 0, False
    hit = lg_blocklist_hit(info)
    if hit:
        reasons.append(hit)
        bl = True
    else:
        try:
            g = await lg_gsb(info["key"])
        except Exception:
            g = None
        if g:
            reasons.append(f"Google Safe Browsing: угроза ({g})")
            bl = True
    if extended and not bl:
        s, why = lg_heuristics(info, visible)
        score += s
        reasons += why
        shortener = lg_host_in(info["host"], LG_SHORTENERS)
        if shortener or score >= 10:
            try:
                chain = await lg_follow(info["url"])
            except Exception:
                chain = [info["url"]]
            if len(chain) > 1:
                final = lg_norm(chain[-1])
                if final and final["host"] != info["host"]:
                    fh = lg_blocklist_hit(final)
                    if fh:
                        reasons.append("после перенаправления: " + fh)
                        bl = True
                    else:
                        fs, fwhy = lg_heuristics(final)
                        score += fs
                        reasons += ["после перенаправления: " + w for w in fwhy]
                    if shortener or fs or fh:
                        reasons.append(f"ссылка перенаправляет на {final['host']}")
                if len(chain) > 3:
                    score += 20
                    reasons.append("длинная цепочка перенаправлений")
    res = {"level": lg_level(score, bl), "score": score, "reasons": reasons, "bl": bl}
    import json as _j
    db.execute("INSERT OR REPLACE INTO link_cache (ckey, level, score, reasons, bl, checked_at) VALUES (?,?,?,?,?,?)",
               (ckey, res["level"], score, _j.dumps(reasons, ensure_ascii=False), int(bl), now_ts()))
    db.commit()
    return res

def lg_defang(url) -> str:
    return _re.sub(r"^http", "hxxp", url).replace(".", "[.]")

def lg_alert_text(sender, res, info, extended) -> str:
    link = lg_defang(info["url"])
    if res["level"] == "high":
        head = "🚨 Обнаружена угроза!"
        lead = f"В сообщении от {sender} обнаружена опасная ссылка."
        lvl = "🔴 высокий"
    else:
        head = "⚠️ Обнаружена подозрительная ссылка!"
        lead = f"В сообщении от {sender} обнаружена потенциально опасная ссылка."
        lvl = "🟠 средний"
    lines = [head, "", lead, "", f"Ссылка: {link}", f"Уровень риска: {lvl}"]
    if extended:
        lines.append("Причины:")
        lines += ["• " + r for r in res["reasons"][:6]]
    else:
        lines.append("Причина: ссылка найдена в базе вредоносных или фишинговых ресурсов.")
    lines += ["", "Рекомендуем не переходить по ней и не вводить личные данные.",
              "Отсутствие предупреждения не гарантирует безопасность ссылки."]
    if not extended:
        lines += ["", "💎 В Premium: расширенный анализ, подмена доменов и подробные предупреждения."]
    return "\n".join(lines)

async def maybe_check_links(message, conn_id, owner_id):
    """Проверяет ссылки во входящих сообщениях и предупреждает владельца аккаунта."""
    if not message.from_user or message.from_user.id == owner_id or not lg_enabled(owner_id):
        return
    urls = lg_extract(message)
    if not urls:
        return
    extended = is_premium(owner_id)
    for raw, vis in urls:
        info = lg_norm(raw)
        if not info:
            continue
        mismatch = bool(vis) and any("текст ссылки" in w for w in lg_heuristics(info, vis)[1])
        if lg_host_in(info["host"], LG_TRUSTED) and not mismatch and not info["userinfo"]:
            continue
        key = info["key"]
        row = db.execute("SELECT alerted_at FROM link_alerts WHERE owner_id=? AND key=?", (owner_id, key)).fetchone()
        if row and now_ts() - row[0] < LG_ALERT_TTL:
            continue
        seen = db.execute("SELECT 1 FROM link_seen WHERE owner_id=? AND day=? AND key=?",
                          (owner_id, today_key(), key)).fetchone()
        if not seen:
            if lg_used_today(owner_id) >= lg_limit(owner_id):
                nk = (owner_id, today_key())
                if nk not in _lg_limit_notified:
                    _lg_limit_notified.add(nk)
                    try:
                        await bot.send_message(
                            owner_id,
                            f"🛡 Лимит проверок ссылок на сегодня исчерпан ({lg_used_today(owner_id)}/{lg_limit(owner_id)}). "
                            + ("Завтра счётчик обнулится." if is_premium(owner_id) else
                               f"В 💎 Premium — {PREMIUM_LINKS_PER_DAY} проверок в день и расширенный анализ."))
                    except Exception:
                        logging.exception("Не удалось отправить уведомление о лимите проверок")
                break
            db.execute("INSERT OR IGNORE INTO link_seen (owner_id, day, key) VALUES (?,?,?)",
                       (owner_id, today_key(), key))
            db.commit()
        res = await lg_analyze(info, vis, extended)
        if res["level"] == "low" or (res["level"] == "medium" and not extended):
            continue
        text = lg_alert_text(fmt_user(message.from_user), res, info, extended)
        try:
            await bot.send_message(owner_id, text)
        except Exception:
            logging.exception("Не удалось отправить предупреждение о ссылке")
            continue
        db.execute("INSERT OR REPLACE INTO link_alerts (owner_id, key, sender_id, alerted_at) VALUES (?,?,?,?)",
                   (owner_id, key, message.from_user.id, now_ts()))
        db.commit()

# --- базы известных угроз (бесплатные, обновляются раз в 12 часов) ---

def lg_parse_feed(text, kind):
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()[-1]
        if kind == "urls":
            i = lg_norm(tok) if "://" in tok else None
            if i:
                out.add(i["key"])
        else:
            h = tok.lower().rstrip(".")
            if "://" in h:
                i = lg_norm(h)
                h = i["host"] if i else ""
            if h and _re.fullmatch(r"[a-z0-9._-]+\.[a-z0-9-]{2,}", h) and not lg_host_in(h, LG_TRUSTED):
                out.add(h)
    return out

async def lg_download(url):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            buf = bytearray()
            async for chunk in r.content.iter_chunked(1 << 16):
                buf += chunk
                if len(buf) > LG_MAX_FEED_BYTES:
                    raise RuntimeError("файл слишком большой")
    return buf.decode("utf-8", "ignore")

async def lg_update_feeds() -> dict:
    stats = {}
    for name, (kind, url) in LG_FEEDS.items():
        try:
            items = lg_parse_feed(await lg_download(url), kind)
            if not items:
                raise RuntimeError("пустой список")
            tbl, col = ("link_bad_urls", "url") if kind == "urls" else ("link_bad_hosts", "host")
            db.execute(f"DELETE FROM {tbl} WHERE src=?", (name,))
            batch = [(x, name) for x in items]
            for i in range(0, len(batch), 20000):
                db.executemany(f"INSERT OR IGNORE INTO {tbl} ({col}, src) VALUES (?,?)", batch[i:i + 20000])
            db.commit()
            stats[name] = len(items)
        except Exception as e:
            logging.exception(f"Не удалось обновить базу {name}")
            stats[name] = f"ошибка: {e}"
    set_setting("lg_feed_updated", str(now_ts()))
    return stats

async def links_feed_loop():
    await asyncio.sleep(90)
    while True:
        try:
            await lg_update_feeds()
        except Exception:
            logging.exception("links_feed_loop")
        await asyncio.sleep(12 * 3600)

def lg_status_text() -> str:
    bu = db.execute("SELECT COUNT(*) FROM link_bad_urls").fetchone()[0]
    bh = db.execute("SELECT COUNT(*) FROM link_bad_hosts").fetchone()[0]
    upd = get_setting("lg_feed_updated", "0")
    return ("🛡 Защита от ссылок\n"
            f"Базы: адресов URLhaus — {bu:,}, фишинговых доменов — {bh:,}".replace(",", " ") + "\n"
            f"Обновлено: {fmt_ts(int(upd)) if upd.isdigit() and int(upd) else 'ещё не обновлялись'}\n"
            f"Google Safe Browsing: {'подключён' if get_setting('gsb_key', '') else 'не подключён'}\n"
            f"Защита: {'выключена для всех' if get_setting('lg_global_off', '0') == '1' else 'включена'}\n"
            f"Лимиты в день: базовый {FREE_LINKS_PER_DAY}, Premium {PREMIUM_LINKS_PER_DAY}\n\n"
            "/links_update — обновить базы сейчас\n"
            "/links_off, /links_on — выключить или включить для всех\n"
            "/linkkey КЛЮЧ — подключить Google Safe Browsing (по желанию)\n"
            "/linkkey_off — отключить")

@dp.message(Command("links", "links_update", "links_on", "links_off", "linkkey", "linkkey_off"))
async def cmd_links(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    if cmd == "links_update":
        await message.answer("⏳ Обновляю базы, это может занять до пары минут…")
        st = await lg_update_feeds()
        await message.answer("Готово: " + ", ".join(f"{k}: {v}" for k, v in st.items()))
    elif cmd == "links_on":
        set_setting("lg_global_off", "0")
        await message.answer("🛡 Защита от ссылок включена.")
    elif cmd == "links_off":
        set_setting("lg_global_off", "1")
        await message.answer("🛡 Защита от ссылок выключена для всех.")
    elif cmd == "linkkey":
        if len(parts) < 2:
            await message.answer("Формат: /linkkey КЛЮЧ")
            return
        set_setting("gsb_key", parts[1].strip())
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔑 Ключ Google Safe Browsing сохранён (сообщение с ключом удалено).")
    elif cmd == "linkkey_off":
        del_setting("gsb_key")
        await message.answer("🔑 Google Safe Browsing отключён.")
    else:
        await message.answer(lg_status_text())

async def handle_offer_wait(message, user_id, text) -> bool:
    if user_id != ADMIN_ID or get_setting("offer_edit_on", "0") != "1":
        return False
    if text in MENU_TEXTS:
        set_setting("offer_edit_on", "0")
        return False
    doc = getattr(message, "document", None)
    if doc and not text:
        name = (getattr(doc, "file_name", "") or "").lower()
        if not name.endswith(".txt") or (getattr(doc, "file_size", 0) or 0) > 300000:
            await message.answer("Пришли файл .txt (до 300 КБ) или обычное сообщение с текстом.")
            return True
        try:
            fobj = await bot.get_file(doc.file_id)
            buf = await bot.download_file(fobj.file_path)
            text = buf.read().decode("utf-8-sig").strip()
        except Exception:
            logging.exception("Не удалось прочитать файл оферты")
            await message.answer("Не удалось прочитать файл. Пришли текст обычным сообщением.")
            return True
    if not text:
        await message.answer("Пришли текст оферты обычным сообщением или файлом .txt.")
        return True
    draft = get_setting("offer_draft", "")
    draft = (draft + "\n\n" + text) if draft else text
    set_setting("offer_draft", draft)
    await message.answer(f"✅ Принято. Сейчас в черновике {len(draft)} симв. Пришли следующую часть "
                         f"или /offer_save, чтобы сохранить, /offer_cancel, чтобы отменить.")
    return True

@dp.message(Command("offer_edit", "offer_save", "offer_cancel", "offer_reset"))
async def cmd_offer_edit(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    cmd = (message.text or "").split()[0].lstrip("/").split("@")[0].lower()
    if cmd == "offer_edit":
        set_setting("offer_edit_on", "1")
        set_setting("offer_draft", "")
        await message.answer(
            "✏️ Редактирование оферты.\n\n"
            "Пришли ПОЛНЫЙ новый текст оферты. Telegram режет сообщения на 4096 символов, поэтому "
            "разбей текст на 2 части ПО ПУСТОЙ СТРОКЕ (между пунктами) и отправь их по очереди. "
            "Когда всё отправлено — /offer_save.\n\n"
            "Отмена: /offer_cancel. Вернуть встроенный текст: /offer_reset.")
    elif cmd == "offer_save":
        draft = get_setting("offer_draft", "")
        if len(draft) < 300:
            await message.answer("Черновик пустой или слишком короткий. Начни с /offer_edit.")
            return
        set_setting("offer_custom", draft)
        set_setting("offer_edit_on", "0")
        set_setting("offer_draft", "")
        await message.answer(f"✅ Оферта сохранена ({len(draft)} симв.). Проверь: /offer")
    elif cmd == "offer_cancel":
        set_setting("offer_edit_on", "0")
        set_setting("offer_draft", "")
        await message.answer("Редактирование отменено, оферта не изменилась.")
    else:
        del_setting("offer_custom")
        set_setting("offer_edit_on", "0")
        set_setting("offer_draft", "")
        await message.answer("Оферта возвращена к встроенному тексту.")

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



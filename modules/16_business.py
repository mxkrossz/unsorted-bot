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
    try:
        await maybe_translate(message, conn_id, owner_id)
    except Exception:
        logging.exception("Ошибка обработки перевода")
    try:
        await maybe_media_cmd(message, conn_id, owner_id)
    except Exception:
        logging.exception("Ошибка команды распознавания/проверки")
    try:
        await maybe_list_cmd(message, conn_id, owner_id)
    except Exception:
        logging.exception("Ошибка команды списка")
    try:
        await asyncio.wait_for(maybe_check_links(message, conn_id, owner_id), 30)
    except Exception:
        logging.exception("Ошибка проверки ссылок")

    return

@dp.edited_business_message()
async def on_business_message_edited(message: Message):
    conn_id  = message.business_connection_id
    owner_id = await get_owner_id(conn_id)
    if owner_id is None:
        return
    biz_touch(conn_id, owner_id)

    if tr_is_own_edit(message, conn_id):
        return

    try:
        await asyncio.wait_for(maybe_check_links(message, conn_id, owner_id), 30)
    except Exception:
        logging.exception("Ошибка проверки ссылок")

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



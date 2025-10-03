# telegram_api.py
import os
import getpass
import logging
from typing import List

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

log = logging.getLogger("telegram_api")

async def authorize():
    api_id = int(input("Введите API ID: ").strip())
    api_hash = input("Введите API HASH: ").strip()
    phone = input("Введите номер телефона (с +7...): ").strip()

    # сессию делаем временной (без имени файла → хранится в памяти)
    client = TelegramClient(None, api_id, api_hash)

    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Введите код из Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pwd = getpass.getpass("Введите пароль 2FA: ")
            await client.sign_in(password=pwd)

    return client

async def list_user_dialogs(client) -> List:
    """
    Возвращает ВСЕ диалоги: users (личные), группы/супергруппы, каналы.
    """
    dialogs = await client.get_dialogs()
    # отфильтруем всё «нужно» и прикрутим человекочитаемый тип
    res = []
    for d in dialogs:
        t = (
            "user" if getattr(d, "is_user", False)
            else "group" if getattr(d, "is_group", False)
            else "channel" if getattr(d, "is_channel", False)
            else "other"
        )
        if t in {"user", "group", "channel"}:
            d._tgdl_kind = t  # пометим на будущее (UI)
            res.append(d)
    log.info("Найдено диалогов: %s (users/groups/channels)", len(res))
    return res

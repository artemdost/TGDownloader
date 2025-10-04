# telegram_api.py
import os
import getpass
import logging
import inspect
from typing import Awaitable, Callable, List, Optional, Union

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

log = logging.getLogger("telegram_api")

async def authorize(
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None,
    phone: Optional[str] = None,
    session_name: Optional[str] = None,
    code_callback: Optional[Callable[..., Union[str, Awaitable[str], None]]] = None,
    password_callback: Optional[Callable[..., Union[str, Awaitable[str], None]]] = None,
) -> TelegramClient:
    """Shared authorization helper for CLI (interactive) and GUI (callbacks)."""

    def _stringify(value, field_name: str) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
        else:
            cleaned = str(value).strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be empty")
        return cleaned

    async def _request(cb: Callable[..., Union[str, Awaitable[str], None]], prompt: str) -> Optional[str]:
        try:
            result = cb(prompt)
        except TypeError:
            result = cb()
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return None
        return str(result).strip() or None

    if api_id is None:
        api_id_str = input("Enter API ID: ").strip()
    else:
        api_id_str = str(api_id).strip()
    if not api_id_str:
        raise ValueError("API ID must not be empty")
    api_id = int(api_id_str)

    if api_hash is None:
        api_hash = input("Enter API HASH: ").strip()
    else:
        api_hash = _stringify(api_hash, "API HASH")

    if phone is None:
        phone = input("Enter phone number (e.g. +7...): ").strip()
    else:
        phone = _stringify(phone, "phone")

    client = TelegramClient(session_name, api_id, api_hash)

    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        if code_callback:
            code = await _request(code_callback, "Enter code from Telegram: ")
        else:
            code = input("Enter code from Telegram: ").strip()
        if not code:
            raise ValueError("Telegram code is required")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            if password_callback:
                pwd = await _request(password_callback, "Enter 2FA password: ")
            else:
                pwd = getpass.getpass("Enter 2FA password: ")
            if not pwd:
                raise ValueError("2FA password is required")
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

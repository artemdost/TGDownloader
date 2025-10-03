# channel_data.py
import os
import re
import json
import logging
from datetime import datetime
from typing import Tuple, Callable, Optional, Dict, Iterable

from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

log = logging.getLogger("channel_data")

# Расширения/мимы, которые считаем потенциально опасными для скачивания
DANGEROUS_EXT: set[str] = {
    ".exe", ".scr", ".pif", ".com", ".cmd", ".bat", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".ps1", ".psm1", ".psc1", ".msi", ".msp", ".mst", ".jar", ".lnk",
    ".reg", ".hta", ".cpl", ".dll", ".sys", ".apk", ".app", ".pkg", ".dmg"
}
DANGEROUS_MIME_PREFIX: tuple[str, ...] = (
    "application/x-dosexec",
    "application/x-msdownload",
    "application/x-ms-installer",
)

def _safe_name(s: str, fallback: str = "dialog") -> str:
    if not s:
        return fallback
    s = s.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s)
    return s or fallback

def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)
    return path

def _to_web_path(p: str) -> str:
    return p.replace("\\", "/")

def _is_dangerous(doc, filename: str | None) -> bool:
    """
    Эвристика: не качаем, если расширение/миме подозрительны.
    """
    fn_ext = (os.path.splitext(filename)[1].lower() if filename else "")
    if fn_ext in DANGEROUS_EXT:
        return True
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if any(mime.startswith(pref) for pref in DANGEROUS_MIME_PREFIX):
        return True
    # js/html как документы тоже не скачиваем (риски XSS/скриптов)
    if mime in {"text/javascript", "application/javascript", "text/html"}:
        return True
    return False

async def _download_one_message_media(
    client, msg: Message, media_dir_abs: str, export_dir_abs: str, skip_dangerous: bool
) -> list[dict]:
    """
    Скачивает медиа и возвращает список объектов:
      {"kind":"image"|"video"|"file"|"blocked", "path":"...", "thumb":"...", "name":"..."}
    Если файл «опасный» и skip_dangerous=True — не скачиваем, кладём заглушку kind="blocked".
    """
    items: list[dict] = []
    if not msg or not msg.media:
        return items

    try:
        filename_hint = None
        doc = None
        is_document = isinstance(msg.media, MessageMediaDocument) and msg.media.document
        if is_document:
            doc = msg.media.document
            for attr in doc.attributes or []:
                if isinstance(attr, DocumentAttributeFilename) and getattr(attr, "file_name", None):
                    filename_hint = attr.file_name
                    break

            if skip_dangerous and _is_dangerous(doc, filename_hint):
                # заглушка вместо скачивания
                items.append({
                    "kind": "blocked",
                    "name": filename_hint or "file",
                    "reason": "dangerous-file-blocked"
                })
                return items

        # Скачиваем основной бинарь
        real_path = await msg.download_media(file=media_dir_abs)
        if not real_path:
            return items

        rel_main = _to_web_path(os.path.relpath(real_path, start=export_dir_abs))
        name = filename_hint or os.path.basename(real_path)
        ext = os.path.splitext(real_path)[1].lower()

        kind = "file"
        if is_document and doc:
            mime = (doc.mime_type or "").lower()
            if mime.startswith("video/"):
                kind = "video"
            for attr in doc.attributes or []:
                if isinstance(attr, DocumentAttributeVideo):
                    kind = "video"
        else:
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                kind = "image"

        entry = {"kind": kind, "path": rel_main, "name": name}

        # превью для видео
        if kind == "video" and is_document and doc:
            try:
                stem = os.path.splitext(os.path.basename(real_path))[0]
                thumb_guess = os.path.join(media_dir_abs, f"{stem}.jpg")
                thumb_real = await client.download_file(doc, file=thumb_guess, thumb=-1)
                if thumb_real:
                    rel_thumb = _to_web_path(os.path.relpath(thumb_real, start=export_dir_abs))
                    entry["thumb"] = rel_thumb
            except Exception as e:
                log.debug("Не удалось скачать превью видео %s: %s", msg.id, e)

        items.append(entry)
    except Exception as e:
        log.warning("Не удалось скачать медиа из сообщения %s: %s", getattr(msg, "id", "?"), e)

    return items

def _make_sender_display(sender, alias_map: Dict[int, str], alias_counter: Dict[str, int]) -> dict:
    if not sender:
        return {"id": None, "username": None, "name": None, "display": "Channel"}
    uid = getattr(sender, "id", None)
    username = getattr(sender, "username", None)
    name = " ".join([x for x in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if x]).strip() \
           or getattr(sender, "first_name", None) or getattr(sender, "last_name", None)
    display = None
    if username:
        display = f"@{username}"
    elif name:
        display = name
    else:
        if uid is None:
            alias_counter.setdefault("noid", 0)
            alias_counter["noid"] += 1
            display = f"User{alias_counter['noid']}"
        else:
            if uid not in alias_map:
                alias_counter.setdefault("uid", 0)
                alias_counter["uid"] += 1
                alias_map[uid] = f"User{alias_counter['uid']}"
            display = alias_map[uid]
    return {"id": uid, "username": username, "name": name, "display": display}

async def dump_dialog_to_json_and_media(
    client,
    dialog,
    out_root: str = "export",
    progress_every: int = 50,
    on_progress: Optional[Callable[[str, str, int], None]] = None,
    skip_dangerous: bool = True,   # <<<
) -> Tuple[str, str]:
    """
    Универсальный экспорт любого диалога (личный, группа, канал).
    В JSON: 'from': {id, username, name, display}, media: список объектов (image/video/file/blocked).
    """
    entity = dialog.entity
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or getattr(entity, "last_name", None) or "Без названия"
    safe_title = _safe_name(title, "dialog")

    export_dir_abs = _ensure_dir(os.path.join(out_root, safe_title))
    media_dir_abs = _ensure_dir(os.path.join(export_dir_abs, "media"))
    json_path = os.path.join(export_dir_abs, "channel_messages.json")

    log.info("Начинаем выгрузку: %s", title)

    count = 0
    result_messages = []

    alias_map: Dict[int, str] = {}
    alias_counter: Dict[str, int] = {}

    # пустой JSON + первый HTML (если задан on_progress)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result_messages, f, ensure_ascii=False, indent=2)
    if on_progress:
        try:
            on_progress(json_path, media_dir_abs, count)
        except Exception as e:
            log.warning("on_progress (initial) исключение: %s", e)

    async for msg in client.iter_messages(entity, reverse=True):
        if not isinstance(msg, Message):
            continue

        sender = await msg.get_sender()
        sender_info = _make_sender_display(sender, alias_map, alias_counter)

        item = {
            "id": msg.id,
            "date": msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "",
            "from": sender_info,
            "text": msg.text or "",
            "media": [],
        }

        media_items = await _download_one_message_media(
            client, msg, media_dir_abs=media_dir_abs, export_dir_abs=export_dir_abs,
            skip_dangerous=skip_dangerous
        )
        item["media"] = media_items

        result_messages.append(item)
        count += 1

        if progress_every and count % progress_every == 0:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result_messages, f, ensure_ascii=False, indent=2)
            log.info("Промежуточное сохранение: %s сообщений...", count)
            if on_progress:
                try:
                    on_progress(json_path, media_dir_abs, count)
                except Exception as e:
                    log.warning("on_progress исключение: %s", e)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result_messages, f, ensure_ascii=False, indent=2)
    if on_progress:
        try:
            on_progress(json_path, media_dir_abs, count)
        except Exception as e:
            log.warning("on_progress (финал) исключение: %s", e)

    log.info("Готово. Сообщений: %s. JSON: %s. Медиа: %s", count, json_path, media_dir_abs)
    return json_path, media_dir_abs

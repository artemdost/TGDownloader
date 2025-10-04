# channel_data.py
import asyncio
import os
import re
import json
import logging
from datetime import datetime
from typing import Tuple, Callable, Optional, Dict, Iterable, Any

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
    client,
    msg: Message,
    media_dir_abs: str,
    export_dir_abs: str,
    skip_dangerous: bool,
    media_event_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> list[dict]:
    """
    Download media from a message and return descriptor dicts:
      {"kind": "image"|"video"|"file"|"blocked", "path": "...", "thumb": "...", "name": "..."}.
    If skip_dangerous=True and the payload looks risky, nothing is downloaded and a blocked placeholder is returned.
    """
    items: list[dict] = []
    if not msg or not msg.media:
        return items

    def _emit_media(stage: str, **details: Any) -> None:
        if not media_event_cb:
            return
        payload: Dict[str, Any] = {"stage": stage, "message_id": getattr(msg, "id", None)}
        payload.update(details)
        try:
            media_event_cb(payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("media_event callback failed: %s", exc)

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

        kind_guess = "file"
        if is_document and doc:
            mime = (doc.mime_type or "").lower()
            if mime.startswith("video/"):
                kind_guess = "video"
            elif mime.startswith("image/"):
                kind_guess = "image"
            for attr in doc.attributes or []:
                if isinstance(attr, DocumentAttributeVideo):
                    kind_guess = "video"
        elif getattr(msg, "photo", None):
            kind_guess = "image"

        if is_document and doc and skip_dangerous and _is_dangerous(doc, filename_hint):
            name_hint = filename_hint or "file"
            _emit_media("blocked", kind=kind_guess, name=name_hint, reason="dangerous")
            items.append({
                "kind": "blocked",
                "name": name_hint,
                "reason": "dangerous-file-blocked"
            })
            return items

        name_hint = filename_hint or f"message_{getattr(msg, 'id', 'media')}"
        size_hint = getattr(doc, "size", None) if doc else None
        _emit_media("start", kind=kind_guess, name=name_hint, size=size_hint)

        real_path = await msg.download_media(file=media_dir_abs)
        if not real_path:
            _emit_media("error", kind=kind_guess, name=name_hint)
            return items

        rel_main = _to_web_path(os.path.relpath(real_path, start=export_dir_abs))
        name = filename_hint or os.path.basename(real_path)
        ext = os.path.splitext(real_path)[1].lower()

        kind = kind_guess
        if is_document and doc:
            mime = (doc.mime_type or "").lower()
            if mime.startswith("video/"):
                kind = "video"
            elif mime.startswith("image/"):
                kind = "image"
            for attr in doc.attributes or []:
                if isinstance(attr, DocumentAttributeVideo):
                    kind = "video"
        else:
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                kind = "image"

        entry = {"kind": kind, "path": rel_main, "name": name}

        if kind == "video" and is_document and doc:
            try:
                stem = os.path.splitext(os.path.basename(real_path))[0]
                thumb_guess = os.path.join(media_dir_abs, f"{stem}.jpg")
                thumb_real = await client.download_file(doc, file=thumb_guess, thumb=-1)
                if thumb_real:
                    rel_thumb = _to_web_path(os.path.relpath(thumb_real, start=export_dir_abs))
                    entry["thumb"] = rel_thumb
            except Exception as e:
                log.debug("Failed to download video preview %s: %s", msg.id, e)

        items.append(entry)
    except Exception as e:
        log.warning("Failed to download media for message %s: %s", getattr(msg, "id", "?"), e)

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
    on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_media: Optional[Callable[[Dict[str, Any]], None]] = None,
    pause_event: Optional[asyncio.Event] = None,
    cancel_event: Optional[asyncio.Event] = None,
    skip_dangerous: bool = True,   # <<<
) -> Tuple[str, str]:
    """
    Export a dialog (users/groups/channels) into structured JSON plus media files.
    Each JSON entry contains sender info and an array of media descriptors (image/video/file/blocked).
    """
    entity = dialog.entity
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or getattr(entity, "last_name", None) or "Untitled dialog"
    safe_title = _safe_name(title, "dialog")

    export_dir_abs = _ensure_dir(os.path.join(out_root, safe_title))
    media_dir_abs = _ensure_dir(os.path.join(export_dir_abs, "media"))
    json_path = os.path.join(export_dir_abs, "channel_messages.json")

    log.info("Processing dialog: %s", title)

    existing_messages: list[Dict[str, Any]] = []
    processed_ids: set[int] = set()
    alias_map: Dict[int, str] = {}
    alias_counter: Dict[str, int] = {}

    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing_messages = [m for m in data if isinstance(m, dict)]
            else:
                log.warning("Existing JSON %s has unexpected structure, ignoring", json_path)
        except Exception as e:
            log.warning("Failed to load existing JSON %s: %s", json_path, e)

    for m in existing_messages:
        mid = m.get("id")
        if isinstance(mid, int):
            processed_ids.add(mid)
        sender = m.get("from") or {}
        uid = sender.get("id")
        display = sender.get("display")
        if isinstance(uid, int) and isinstance(display, str) and display.startswith("User"):
            alias_map[uid] = display
            try:
                num = int(display[4:])
                alias_counter["uid"] = max(alias_counter.get("uid", 0), num)
            except Exception:
                pass
        elif uid is None and isinstance(display, str) and display.startswith("User"):
            try:
                num = int(display[4:])
                alias_counter["noid"] = max(alias_counter.get("noid", 0), num)
            except Exception:
                pass

    result_messages = list(existing_messages)
    count = len(result_messages)

    if not os.path.exists(json_path):
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result_messages, f, ensure_ascii=False, indent=2)

    if on_progress:
        try:
            on_progress(json_path, media_dir_abs, count)
        except Exception as e:
            log.warning("on_progress (initial) callback failed: %s", e)

    cancelled = False

    async for msg in client.iter_messages(entity, reverse=True):
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break
        if pause_event:
            while not pause_event.is_set():
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break
                await asyncio.sleep(0.1)
            if cancelled:
                break

        if not isinstance(msg, Message):
            continue
        if getattr(msg, "id", None) in processed_ids:
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
            client,
            msg,
            media_dir_abs=media_dir_abs,
            export_dir_abs=export_dir_abs,
            skip_dangerous=skip_dangerous,
            media_event_cb=on_media,
        )
        item["media"] = media_items

        result_messages.append(item)
        msg_id = item.get("id")
        if isinstance(msg_id, int):
            processed_ids.add(msg_id)
        count += 1

        if on_message:
            try:
                on_message({
                    "id": item.get("id"),
                    "count": count,
                    "text": item.get("text", "") or "",
                    "date": item.get("date"),
                    "media": media_items,
                })
            except Exception as e:
                log.warning("on_message callback failed: %s", e)

        if progress_every and count % progress_every == 0:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result_messages, f, ensure_ascii=False, indent=2)
            log.info("Saved messages so far: %s", count)
            if on_progress:
                try:
                    on_progress(json_path, media_dir_abs, count)
                except Exception as e:
                    log.warning("on_progress callback failed: %s", e)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result_messages, f, ensure_ascii=False, indent=2)
    if on_progress:
        try:
            on_progress(json_path, media_dir_abs, count)
        except Exception as e:
            log.warning("on_progress (final) callback failed: %s", e)

    log.info("Done. Total messages: %s. JSON: %s. Media: %s", count, json_path, media_dir_abs)

    if cancel_event and cancel_event.is_set():
        raise asyncio.CancelledError()

    return json_path, media_dir_abs


# channel_data.py - SECURED VERSION
import asyncio
import os
import re
import json
import logging
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Tuple, Callable, Optional, Dict, Iterable, Any
from collections import deque

from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

log = logging.getLogger("channel_data")

# ═══════════════════════════════════════════════════
# RATE LIMITER CLASS
# ═══════════════════════════════════════════════════
class RateLimiter:
    """Download rate limiting"""
    
    def __init__(self, max_requests: int = 10, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
    
    async def acquire(self):
        """Wait until slot is available"""
        now = time.time()
        
        while self.requests and self.requests[0] < now - self.time_window:
            self.requests.popleft()
        
        if len(self.requests) >= self.max_requests:
            sleep_time = self.requests[0] + self.time_window - now
            if sleep_time > 0:
                log.debug(f"Rate limit reached, waiting {sleep_time:.1f}s")
                await asyncio.sleep(sleep_time)
                now = time.time()
                while self.requests and self.requests[0] < now - self.time_window:
                    self.requests.popleft()
        
        self.requests.append(now)

# Global rate limiter: max 5 downloads per 10 seconds
_media_download_limiter = RateLimiter(max_requests=5, time_window=10)

# ═══════════════════════════════════════════════════
# SECURITY: EXPANDED DANGEROUS EXTENSIONS LIST
# ═══════════════════════════════════════════════════
DANGEROUS_EXT: set[str] = {
    # Executables
    ".exe", ".scr", ".pif", ".com", ".cmd", ".bat", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".ps1", ".psm1", ".psc1", ".msi", ".msp", ".mst", ".jar",
    ".lnk", ".reg", ".hta", ".cpl", ".dll", ".sys", ".apk", ".app", ".pkg", ".dmg",
    ".deb", ".rpm", ".run", ".bin", ".sh", ".bash", ".zsh", ".fish",
    
    # Scripts and macros
    ".vb", ".vba", ".xlsm", ".xlsb", ".xltm", ".xla", ".xlam", ".pptm", ".potm",
    ".ppam", ".ppsm", ".sldm", ".docm", ".dotm",
    
    # Web and scripts
    ".html", ".htm", ".svg", ".xml", ".xsl", ".xslt", ".php", ".asp", ".aspx",
    ".jsp", ".jspx", ".py", ".pl", ".rb", ".lua", ".scpt",
    
    # Archives (potential zip bombs)
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".img",
    
    # Suspicious formats
    ".torrent", ".onion", ".i2p"
}

DANGEROUS_MIME_PREFIX: tuple[str, ...] = (
    "application/x-dosexec",
    "application/x-msdownload",
    "application/x-ms-installer",
    "application/x-executable",
    "application/x-mach-binary",
    "application/x-sh",
    "application/x-shellscript",
    "text/x-python",
    "text/x-php",
    "text/javascript",
    "application/javascript",
    "text/html",
    "application/x-httpd-php",
)

# Maximum file size: 500 MB (protection against zip bombs)
MAX_FILE_SIZE = 500 * 1024 * 1024

# ═══════════════════════════════════════════════════
# SECURITY: PATH VALIDATION
# ═══════════════════════════════════════════════════
def _is_safe_path(base_dir: str, target_path: str) -> bool:
    """
    Validate that target_path is within base_dir (防止 path traversal)
    """
    try:
        base = Path(base_dir).resolve()
        target = Path(target_path).resolve()
        return target.is_relative_to(base)
    except (ValueError, OSError):
        return False


def _safe_name(s: str, fallback: str = "dialog") -> str:
    """Sanitize filename - ENHANCED SECURITY"""
    if not s:
        return fallback
    
    s = s.strip()
    
    # Remove null bytes (path traversal vector)
    s = s.replace('\x00', '')
    
    # Remove path separators and dangerous characters
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', s)
    
    # Remove sequences that could be path traversal
    s = s.replace('..', '_')
    s = re.sub(r'\.{2,}', '_', s)
    
    # Collapse multiple spaces
    s = re.sub(r'\s+', ' ', s)
    
    # Prevent hidden files on Unix
    if s.startswith('.'):
        s = '_' + s[1:]
    
    # Limit length (255 chars is filesystem limit)
    if len(s) > 200:
        # Keep extension if present
        name, ext = os.path.splitext(s)
        s = name[:200-len(ext)] + ext
    
    return s or fallback


def _ensure_dir(path: str) -> str:
    """Create directory with security checks"""
    # Normalize path
    path = os.path.normpath(path)
    
    # Check for absolute path outside project
    if os.path.isabs(path):
        log.warning("Attempted to create absolute path: %s", path)
        raise ValueError("Absolute paths not allowed")
    
    # Check for path traversal
    if '..' in path.split(os.sep):
        log.warning("Path traversal attempt detected: %s", path)
        raise ValueError("Path traversal not allowed")
    
    # Create with restricted permissions
    os.makedirs(path, exist_ok=True)
    
    # Set restrictive permissions (owner only) on Unix
    if os.name != 'nt':
        try:
            os.chmod(path, 0o700)
        except Exception as e:
            log.warning("Failed to set permissions on %s: %s", path, e)
    
    return path


def _to_web_path(p: str) -> str:
    """Convert to web-safe path"""
    # Normalize separators
    p = p.replace("\\", "/")
    
    # Remove any remaining dangerous sequences
    p = re.sub(r'\.\./', '', p)
    p = re.sub(r'/\.\.', '', p)
    
    return p


def _is_dangerous(doc, filename: str | None) -> bool:
    """Enhanced dangerous file detection"""
    # Check extension
    fn_ext = (os.path.splitext(filename)[1].lower() if filename else "")
    if fn_ext in DANGEROUS_EXT:
        log.warning("Blocked dangerous extension: %s", fn_ext)
        return True
    
    # Check MIME type
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if any(mime.startswith(pref) for pref in DANGEROUS_MIME_PREFIX):
        log.warning("Blocked dangerous MIME type: %s", mime)
        return True
    
    # Check file size (protection against zip bombs)
    size = getattr(doc, "size", 0)
    if size > MAX_FILE_SIZE:
        log.warning("Blocked oversized file: %d bytes (max %d)", size, MAX_FILE_SIZE)
        return True
    
    return False


def _check_symlink_attack(filepath: str) -> bool:
    """Check if file is a symbolic link (security risk)"""
    try:
        if os.path.islink(filepath):
            log.warning("Symbolic link detected (blocked): %s", filepath)
            return True
        return False
    except Exception:
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
    Download media from a message with rate limiting and security checks.
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
        except Exception as exc:
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

        # SECURITY CHECK: Dangerous file detection
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

        last_percent = -1

        def _progress(current: int, total: int) -> None:
            nonlocal last_percent
            if total <= 0:
                percent = None
            else:
                percent = int((current / total) * 100)
            if percent is None:
                _emit_media("progress", kind=kind_guess, name=name_hint, current=current, total=total)
                return
            if percent != last_percent and (percent - last_percent >= 10 or percent in {0, 100} or last_percent < 0):
                last_percent = percent
                _emit_media(
                    "progress",
                    kind=kind_guess,
                    name=name_hint,
                    current=current,
                    total=total,
                    percent=percent,
                )

        # RATE LIMITING
        await _media_download_limiter.acquire()
        
        real_path = await msg.download_media(file=media_dir_abs, progress_callback=_progress)
        
        if not real_path:
            _emit_media("error", kind=kind_guess, name=name_hint)
            return items

        # SECURITY CHECK: Validate downloaded file path
        if not _is_safe_path(export_dir_abs, real_path):
            log.error("Downloaded file outside safe directory: %s", real_path)
            try:
                os.remove(real_path)
            except Exception:
                pass
            _emit_media("error", kind=kind_guess, name=name_hint)
            return items

        # SECURITY CHECK: Detect symlink attacks
        if _check_symlink_attack(real_path):
            try:
                os.remove(real_path)
            except Exception:
                pass
            _emit_media("blocked", kind=kind_guess, name=name_hint, reason="symlink")
            items.append({
                "kind": "blocked",
                "name": name_hint,
                "reason": "symlink-blocked"
            })
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

        # Download video thumbnail
        if kind == "video" and is_document and doc:
            try:
                await _media_download_limiter.acquire()
                
                stem = os.path.splitext(os.path.basename(real_path))[0]
                thumb_guess = os.path.join(media_dir_abs, f"{stem}.jpg")
                thumb_real = await client.download_file(doc, file=thumb_guess, thumb=-1)
                
                if thumb_real:
                    # SECURITY CHECK: Validate thumbnail path
                    if _is_safe_path(export_dir_abs, thumb_real) and not _check_symlink_attack(thumb_real):
                        rel_thumb = _to_web_path(os.path.relpath(thumb_real, start=export_dir_abs))
                        entry["thumb"] = rel_thumb
                    else:
                        log.warning("Thumbnail failed security check: %s", thumb_real)
                        try:
                            os.remove(thumb_real)
                        except Exception:
                            pass
            except Exception as e:
                log.debug("Failed to download video preview %s: %s", msg.id, e)

        _emit_media("complete", kind=kind, name=name_hint, path=rel_main)
        items.append(entry)
        
    except Exception as e:
        log.warning("Failed to download media for message %s: %s", getattr(msg, "id", "?"), e)

    return items


def _make_sender_display(sender, alias_map: Dict[int, str], alias_counter: Dict[str, int]) -> dict:
    """Create sender display info"""
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
    is_finish_requested: Optional[Callable[[], bool]] = None,
    skip_dangerous: bool = True,
) -> Tuple[str, str]:
    """
    Export a dialog with rate-limited media downloads and security checks.
    """
    entity = dialog.entity
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or getattr(entity, "last_name", None) or "Untitled dialog"
    safe_title = _safe_name(title, "dialog")

    export_dir_abs = _ensure_dir(os.path.join(out_root, safe_title))
    media_dir_abs = _ensure_dir(os.path.join(export_dir_abs, "media"))
    json_path = os.path.join(export_dir_abs, "channel_messages.json")

    log.info("Processing dialog: %s", _safe_name(title[:50], "dialog"))  # Sanitize log output

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
                log.warning("Existing JSON has unexpected structure, ignoring")
        except Exception as e:
            log.warning("Failed to load existing JSON: %s", e)

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
        if is_finish_requested and is_finish_requested():
            break
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break
        if pause_event:
            while not pause_event.is_set():
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break
                if is_finish_requested and is_finish_requested():
                    break
                await asyncio.sleep(0.1)
            if cancelled or (is_finish_requested and is_finish_requested()):
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

    log.info("Done. Total messages: %s", count)

    if cancel_event and cancel_event.is_set() and not (is_finish_requested and is_finish_requested()):
        raise asyncio.CancelledError()

    return json_path, media_dir_abs
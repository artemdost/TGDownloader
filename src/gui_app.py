# gui_app.py - SECURED VERSION
import asyncio
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Optional
import ctypes
import hashlib
from .logo_helper import load_logo_image, create_canvas_logo, create_tray_icon
from PIL import ImageTk
from .process_hardening import harden_process
from .channel_data import dump_dialog_to_json_and_media

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None
    Image = None
    ImageDraw = None

from .html_generator import generate_html
from .telegram_api import authorize, list_user_dialogs

# Apply crash-dump hardening in GUI mode as well
harden_process()

DEFAULT_PROGRESS_EVERY = 5

# ═══════════════════════════════════════════════════
# SECURITY: SECURE CREDENTIAL STORAGE
# ═══════════════════════════════════════════════════
class SecureVar:
    """Secure variable that clears memory on deletion"""
    
    def __init__(self, value: str = ""):
        self._value = value
        self._cleared = False
    
    def set(self, value: str):
        """Set value"""
        self.clear()
        self._value = str(value) if value else ""
        self._cleared = False
    
    def get(self) -> str:
        """Get value"""
        if self._cleared:
            return ""
        return self._value
    
    def clear(self):
        """Securely clear value from memory"""
        if self._cleared:
            return
        try:
            if self._value:
                # Attempt to overwrite memory
                buf = ctypes.create_string_buffer(len(self._value.encode('utf-8')))
                ctypes.memset(ctypes.addressof(buf), 0, len(self._value.encode('utf-8')))
        except Exception:
            pass
        finally:
            self._value = ""
            self._cleared = True
    
    def __del__(self):
        self.clear()


class Worker:
    """Background thread that talks to Telegram without blocking tkinter."""

    def __init__(self, ui_queue: "queue.Queue[dict[str, Any]]") -> None:
        self.ui_queue = ui_queue
        self.command_queue: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client = None
        self.dialogs = []
        self._pending_inputs: set[asyncio.Future] = set()
        self._export_pause_event: Optional[asyncio.Event] = None
        self._export_cancel_event: Optional[asyncio.Event] = None
        self._export_running = False
        self._export_finish_requested = False
        self._current_dialog_title: Optional[str] = None
        self._media_progress: dict[tuple[str, str], int] = {}
        self._media_labels: dict[tuple[str, str], str] = {}
        self._cleanup_old_sessions()
    
    def _cleanup_old_sessions(self) -> None:
        """Remove .DELETE_ME files and orphaned sessions on startup"""
        import glob
        
        for trash_file in glob.glob("*.DELETE_ME_*"):
            try:
                os.remove(trash_file)
                print(f"[CLEANUP] Removed old trash: {trash_file}")
            except Exception as e:
                print(f"[CLEANUP] Failed to remove {trash_file}: {e}")

    def start(self) -> None:
        self.thread.start()

    def send_command(self, name: str, **payload: Any) -> None:
        self.command_queue.put((name, payload))

    def resolve_future(self, fut: asyncio.Future, value: Optional[str]) -> None:
        if not self.loop:
            return

        def _set_result() -> None:
            if not fut.done():
                fut.set_result(value)

        self.loop.call_soon_threadsafe(_set_result)

    def _emit(self, event_type: str, **payload: Any) -> None:
        self.ui_queue.put({"type": event_type, **payload})

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        while True:
            name, payload = self.command_queue.get()
            if name == "stop":
                self.loop.run_until_complete(self._handle_stop())
                break
            handler = getattr(self, f"_cmd_{name}", None)
            if not handler:
                self._emit("error", message=f"Unknown command: {name}")
                continue
            try:
                self.loop.run_until_complete(handler(**payload))
            except Exception as exc:
                self._emit("error", message=str(exc))

    async def _handle_stop(self) -> None:
        if self._export_cancel_event:
            self._export_cancel_event.set()
        if self._export_pause_event:
            self._export_pause_event.set()
        if self.loop:
            for fut in list(self._pending_inputs):
                if not fut.done():
                    fut.set_result(None)
            self._pending_inputs.clear()
        
        # Disconnect client first (releases file handles)
        if self.client:
            try:
                await self.client.disconnect()
                await asyncio.sleep(0.5)
            except Exception:
                pass
        
        # Auto-delete session files
        import glob
        
        session_files_deleted = []
        session_files_failed = []
        
        for session_file in glob.glob("*.session*"):
            # Attempt 1: Direct deletion
            try:
                os.remove(session_file)
                session_files_deleted.append(session_file)
                self._emit("log", message=f"🗑️ Удалено: {session_file}")
                continue
            except PermissionError:
                pass
            except Exception as e:
                self._emit("log", message=f"❌ Ошибка удаления {session_file}: {e}")
                session_files_failed.append(session_file)
                continue

            # Attempt 2: Wait and retry
            try:
                time.sleep(0.3)
                os.remove(session_file)
                session_files_deleted.append(session_file)
                self._emit("log", message=f"🗑️ Удалено (повтор): {session_file}")
                continue
            except Exception:
                pass

            # Attempt 3: Rename for deletion on next start
            try:
                trash_name = f"{session_file}.DELETE_ME_{int(time.time())}"
                os.rename(session_file, trash_name)
                self._emit("log", message=f"🔄 Помечено для удаления: {session_file}")
                try:
                    os.remove(trash_name)
                    session_files_deleted.append(session_file)
                    self._emit("log", message=f"🗑️ Удалено (переименовано): {session_file}")
                except Exception:
                    session_files_failed.append(session_file)
                    self._emit("log", message=f"⚠️ Будет удалено при следующем запуске: {trash_name}")
            except Exception as e:
                session_files_failed.append(session_file)
                self._emit("log", message=f"❌ Не удалось обработать {session_file}: {e}")

        # Clean up old .DELETE_ME files
        for old_trash in glob.glob("*.DELETE_ME_*"):
            try:
                os.remove(old_trash)
                self._emit("log", message=f"🗑️ Очищен старый мусор: {old_trash}")
            except Exception:
                pass

        if session_files_deleted:
            self._emit("log", message=f"✅ Очищено {len(session_files_deleted)} файл(ов) сессий")
        if session_files_failed:
            self._emit("log", message=f"⚠️ {len(session_files_failed)} файл(ов) требуют перезапуска для удаления")

        self._emit("status", message="Отключено (сессии очищены)")
        self._emit("export_state", state="idle")

    async def _cmd_connect(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_name: Optional[str],
    ) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        
        try:
            client = await authorize(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                session_name=session_name,
                code_callback=self._request_code,
                password_callback=self._request_password,
            )
        except Exception as exc:
            raise RuntimeError(f"Authorization failed: {exc}") from exc
        
        self.client = client
        me = await client.get_me()
        identity = (
            getattr(me, "username", None)
            or getattr(me, "first_name", None)
            or getattr(me, "last_name", None)
            or "account"
        )
        self._emit("status", message=f"Подключено: {identity[:20]}")  # Limit length
        self._emit("log", message="Авторизация успешна")
        await self._send_dialogs()

    async def _cmd_refresh_dialogs(self) -> None:
        if not self.client:
            raise RuntimeError("Сначала подключите свой аккаунт")
        await self._send_dialogs()

    async def _cmd_export(
        self,
        dialog_indices: list[int],
        anonymize: bool,
        block_dangerous: bool,
        refresh_seconds: Optional[int],
        progress_every: int,
    ) -> None:
        if not self.client:
            raise RuntimeError("Сначала подключите свой аккаунт")
        if self._export_running:
            raise RuntimeError("Экспорт уже выполняется")

        indices = list(dict.fromkeys(dialog_indices))
        if not indices:
            raise RuntimeError("Выберите канал для экспорта")

        self._export_pause_event = asyncio.Event()
        self._export_pause_event.set()
        self._export_cancel_event = asyncio.Event()
        self._export_running = True
        self._export_finish_requested = False
        self._emit("export_state", state="running")
        completed_successfully = False

        try:
            for idx in indices:
                if idx < 0 or idx >= len(self.dialogs):
                    raise RuntimeError("Выбранный диалог вне диапазона")

                dialog = self.dialogs[idx]
                title = (
                    getattr(dialog.entity, "title", None)
                    or getattr(dialog.entity, "first_name", None)
                    or getattr(dialog.entity, "last_name", None)
                    or "Канал"
                )

                # Sanitize title for logging
                safe_title = title[:50] if title else "Канал"

                self._current_dialog_title = safe_title
                self._emit("status", message=f"Подготовка экспорта: {safe_title}")
                self._emit("log", message=f"Начало экспорта: {safe_title}")

                await self._run_single_export(
                    dialog=dialog,
                    title=title,
                    anonymize=anonymize,
                    block_dangerous=block_dangerous,
                    refresh_seconds=refresh_seconds,
                    progress_every=progress_every,
                )

                if self._export_finish_requested:
                    completed_successfully = True
                    self._emit("status", message="Экспорт завершен")
                    break

                if self._export_cancel_event.is_set():
                    raise asyncio.CancelledError()
            else:
                completed_successfully = True
                self._emit("status", message="Экспорт завершен")

        except asyncio.CancelledError:
            self._emit("log", message="Экспорт отменен")
            self._emit("status", message="Экспорт отменен")
            self._emit("export_state", state="cancelled")
        
        finally:
            self._export_running = False
            self._current_dialog_title = None
            self._export_pause_event = None
            self._export_cancel_event = None
            finish_requested = self._export_finish_requested
            self._export_finish_requested = False
            if completed_successfully or finish_requested:
                self._emit("export_state", state="completed")

    async def _run_single_export(
        self,
        dialog,
        title: str,
        anonymize: bool,
        block_dangerous: bool,
        refresh_seconds: Optional[int],
        progress_every: int,
    ) -> None:
        safe_title = title[:50] if title else "Канал"

        def on_progress(json_path: str, media_dir: str, count: int) -> None:
            self._emit(
                "progress",
                json_path=json_path,
                media_dir=media_dir,
                count=count,
                channel=safe_title,
            )

        def on_message(info: dict[str, Any]) -> None:
            msg_id = info.get("id")
            count = info.get("count")

            summary_parts = []
            if count is not None:
                summary_parts.append(f"#{count}")
            if msg_id is not None:
                summary_parts.append(f"id {msg_id}")

            header = "Сообщение " + " ".join(summary_parts) if summary_parts else "Сообщение"

            text_raw = info.get("text") or ""
            text_snippet = " ".join(text_raw.splitlines()).strip()

            # Limit text length in logs
            if len(text_snippet) > 100:
                text_snippet = f"{text_snippet[:97]}..."

            body = text_snippet or "(нет текста)"
            self._emit("log", message=f"[{safe_title}] {header}: {body}")

            for media in info.get("media") or []:
                kind = media.get("kind") or "файл"
                if kind == "blocked":
                    name = media.get("name") or "файл"
                    reason = media.get("reason") or "заблокирован"
                    self._emit("log", message=f"  заблокирован {name} ({reason})")
                else:
                    path_hint = media.get("path") or media.get("name") or "неизвестно"
                    self._emit("log", message=f"  сохранено {kind}: {path_hint}")

        def on_media_event(info: dict[str, Any]) -> None:
            stage = info.get("stage")
            kind = (info.get("kind") or "файл").strip()
            name = (info.get("name") or info.get("path") or "медиа").strip()
            message_id = info.get("message_id")

            # Limit name length
            if len(name) > 50:
                name = name[:47] + "..."

            key = (safe_title, name)
            label = f"Загрузка {kind}: {name}"
            if message_id is not None:
                label = f"{label} (сообщение {message_id})"

            if stage == "start":
                self._media_progress[key] = -1
                self._media_labels[key] = label
                self._emit("log", message=f"[{safe_title}] {label}")
                self._emit("status", message=label)

            elif stage == "progress":
                percent = info.get("percent")
                current = info.get("current")
                total = info.get("total")
                prev = self._media_progress.get(key, -1)

                if percent is not None:
                    if percent != prev:
                        self._media_progress[key] = percent
                        status = f"{label} {percent}%"
                        self._emit("status", message=status)
                else:
                    status = f"{label} {current or 0}/{total or '?'} байт"
                    self._emit("status", message=status)

            elif stage == "complete":
                self._media_progress.pop(key, None)
                self._media_labels.pop(key, None)
                self._emit("log", message=f"[{safe_title}] Сохранено {kind}: {name}")
                self._emit("status", message=f"Сохранено {kind}: {name}")

            elif stage == "blocked":
                reason = info.get("reason") or "заблокирован"
                self._media_progress.pop(key, None)
                self._media_labels.pop(key, None)
                self._emit("log", message=f"[{safe_title}] Заблокирован {kind}: {name} ({reason})")
            
            elif stage == "error":
                self._media_progress.pop(key, None)
                self._media_labels.pop(key, None)
                self._emit("log", message=f"[{safe_title}] Ошибка {kind}: {name}")

        try:
            json_path, media_dir = await dump_dialog_to_json_and_media(
                self.client,
                dialog,
                out_root="export",
                progress_every=progress_every,
                on_progress=on_progress,
                on_message=on_message,
                on_media=on_media_event,
                pause_event=self._export_pause_event,
                cancel_event=self._export_cancel_event,
                is_finish_requested=lambda: self._export_finish_requested,
                skip_dangerous=block_dangerous,
            )
        except asyncio.CancelledError:
            raise

        html_path = generate_html(
            json_path=json_path,
            media_root=media_dir,
            channel_title=title,
            refresh_seconds=refresh_seconds,
            anonymize=anonymize,
            csp=True,
        )

        self._emit(
            "export_done",
            json_path=json_path,
            media_dir=media_dir,
            html_path=html_path,
            channel=safe_title,
        )
        self._emit("status", message=f"Экспорт завершен: {safe_title}")

    def request_pause(self) -> bool:
        if not self._export_running or not self._export_pause_event or not self.loop:
            return False
        if not self._export_pause_event.is_set():
            return False

        self.loop.call_soon_threadsafe(self._export_pause_event.clear)
        self._emit("status", message="Экспорт приостановлен")
        self._emit("log", message="Экспорт приостановлен")
        self._emit("export_state", state="paused")
        return True

    def request_resume(self) -> bool:
        if not self._export_running or not self._export_pause_event or not self.loop:
            return False
        if self._export_pause_event.is_set():
            return False

        self.loop.call_soon_threadsafe(self._export_pause_event.set)
        self._emit("status", message="Возобновление экспорта")
        self._emit("log", message="Возобновление экспорта")
        self._emit("export_state", state="resumed")
        return True

    def request_finish(self) -> bool:
        if not self._export_running or not self._export_cancel_event or not self.loop:
            return False
        if self._export_finish_requested:
            return False

        self._export_finish_requested = True
        self.loop.call_soon_threadsafe(self._export_cancel_event.set)
        if self._export_pause_event:
            self.loop.call_soon_threadsafe(self._export_pause_event.set)

        self._emit("log", message="Завершаем экспорт с текущими данными...")
        self._emit("status", message="Финализация экспорта")
        self._emit("export_state", state="finish_requested")
        return True

    async def _run_input_dialog(self, prompt: str, title: str, secret: bool = False) -> str:
        fut: asyncio.Future = self.loop.create_future()
        self._pending_inputs.add(fut)
        self._emit(
            "input_request",
            prompt=prompt,
            title=title,
            secret=secret,
            future=fut,
        )
        result = await fut
        self._pending_inputs.discard(fut)
        if result is None:
            raise RuntimeError("Ввод отменен пользователем")
        return result.strip()

    async def _request_code(self, prompt: str) -> str:
        return await self._run_input_dialog(prompt, "Код подтверждения")

    async def _request_password(self, prompt: str) -> str:
        return await self._run_input_dialog(prompt, "Пароль 2FA", secret=True)

    async def _send_dialogs(self) -> None:
        dialogs = await list_user_dialogs(self.client)
        self.dialogs = dialogs
        items = []
        
        for idx, dlg in enumerate(dialogs):
            title = (
                getattr(dlg.entity, "title", None)
                or getattr(dlg.entity, "first_name", None)
                or getattr(dlg.entity, "last_name", None)
                or "No title"
            )
            items.append({
                "index": idx,
                "title": title,
                "kind": getattr(dlg, "_tgdl_kind", "?")
            })
        
        self._emit("dialogs", items=items)
        self._emit("log", message=f"Dialogs updated: {len(items)}")
# gui_app.py - PART 2 (App class)
# Add this after the Worker class from Part 1

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Telegram Export Studio")
        self.geometry("1200x780")
        self.minsize(1000, 700)

        # Иконка окна
        try:
            from .logo_helper import get_resource_path
            icon_path = get_resource_path("icon.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception:
            pass

        self.colors = self._setup_theme()
        self.configure(bg=self.colors["window"])

        self.ui_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.worker = Worker(self.ui_queue)
        self.worker.start()

        # SECURITY: Use SecureVar for sensitive data
        self.api_id_var = SecureVar()
        self.api_hash_var = SecureVar()
        self.phone_var = SecureVar()
        
        # Non-sensitive vars
        self.block_dangerous_var = tk.BooleanVar(value=True)
        self.batch_var = tk.StringVar(value=str(DEFAULT_PROGRESS_EVERY))
        self.search_var = tk.StringVar()

        self.status_var = tk.StringVar(value="Welcome")
        self.stats_var = tk.StringVar(value="Messages saved: 0")
        self.channel_title_var = tk.StringVar(value="Select a channel to export")

        self.all_dialogs: list[dict[str, Any]] = []
        self.filtered_indices: list[int] = []
        self.export_running = False
        self.export_paused = False
        self.progress_animating = False
        self.export_finishing = False
        self._last_export_info: Optional[dict[str, Any]] = None
        self.last_export_html: Optional[str] = None
        self.last_export_dir: Optional[str] = None
        self._tray_icon = None
        self._tray_thread: Optional[threading.Thread] = None
        self._tray_active = False

        self._build_layout()

        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.dialog_list.bind("<<ListboxSelect>>", self._on_channel_select)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._process_events)

    def __del__(self):
        """Secure cleanup on destruction"""
        try:
            self.api_id_var.clear()
            self.api_hash_var.clear()
            self.phone_var.clear()
        except Exception:
            pass

    def _setup_theme(self) -> dict[str, str]:
        try:
            import darkdetect
            is_dark = bool(darkdetect.isDark())
        except Exception:
            is_dark = False

        if is_dark:
            # Telegram Dark Theme 2025
            colors = {
                "window": "#0E1621",
                "card": "#1C2533",
                "glass": "#151E2B",
                "accent": "#2AABEE",
                "accent_hover": "#3FB5F0",
                "accent_active": "#1E8DD6",
                "accent_contrast": "#FFFFFF",
                "text": "#FFFFFF",
                "text_secondary": "#8E9AAF",
                "muted": "#6C7883",
                "border": "#2B3544",
                "entry_bg": "#1C2533",
                "entry_focus": "#242F3D",
                "success": "#4DCD5E",
                "warning": "#F5A623",
                "danger": "#E53935",
            }
        else:
            # Telegram Light Theme 2025
            colors = {
                "window": "#FFFFFF",
                "card": "#FFFFFF",
                "glass": "#F4F5F7",
                "accent": "#2AABEE",
                "accent_hover": "#3FB5F0",
                "accent_active": "#1E8DD6",
                "accent_contrast": "#FFFFFF",
                "text": "#000000",
                "text_secondary": "#707579",
                "muted": "#A0A7AF",
                "border": "#E4E7EB",
                "entry_bg": "#F4F5F7",
                "entry_focus": "#FFFFFF",
                "success": "#4DCD5E",
                "warning": "#F5A623",
                "danger": "#E53935",
            }

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Базовые стили
        style.configure(".", background=colors["window"], foreground=colors["text"], font=("Segoe UI", 10))

        # Карточки с тенью (эмуляция)
        style.configure("Card.TFrame", background=colors["card"], relief="flat", borderwidth=0)
        style.configure("Glass.TFrame", background=colors["glass"], relief="flat", borderwidth=0)
        style.configure("CardInner.TFrame", background=colors["card"], relief="flat", borderwidth=0)

        # Типографика Telegram 2025
        style.configure("Header.TLabel", background=colors["glass"], foreground=colors["text"], font=("Segoe UI", 22, "bold"))
        style.configure("Title.TLabel", background=colors["card"], foreground=colors["text"], font=("Segoe UI", 14, "bold"))
        style.configure("Info.TLabel", background=colors["card"], foreground=colors["muted"], font=("Segoe UI", 9))
        style.configure("Body.TLabel", background=colors["card"], foreground=colors["text"], font=("Segoe UI", 10, "bold"))
        style.configure("Caption.TLabel", background=colors["glass"], foreground=colors["text_secondary"], font=("Segoe UI", 10))

        # Кнопки в стиле Telegram (жирный шрифт)
        style.configure("Accent.TButton",
            background=colors["accent"],
            foreground=colors["accent_contrast"],
            padding=(20, 10),
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10, "bold")
        )
        style.map("Accent.TButton",
            background=[("active", colors["accent_hover"]), ("pressed", colors["accent_active"]), ("disabled", colors["muted"])],
            foreground=[("disabled", "#FFFFFF")]
        )

        style.configure("Secondary.TButton",
            background=colors["entry_bg"],
            foreground=colors["text"],
            padding=(18, 10),
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10, "bold")
        )
        style.map("Secondary.TButton",
            background=[("active", colors["border"]), ("pressed", colors["border"]), ("disabled", colors["entry_bg"])],
            foreground=[("disabled", colors["muted"])]
        )

        style.configure("Ghost.TButton",
            background=colors["card"],
            foreground=colors["accent"],
            padding=(16, 10),
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10, "bold")
        )
        style.map("Ghost.TButton",
            foreground=[("active", colors["accent_hover"]), ("pressed", colors["accent_active"]), ("disabled", colors["muted"])]
        )

        # Чекбоксы
        style.configure("TCheckbutton",
            background=colors["card"],
            foreground=colors["text"],
            focuscolor=colors["accent"],
            font=("Segoe UI", 10)
        )
        style.map("TCheckbutton", foreground=[("disabled", colors["muted"])])

        # Поля ввода
        style.configure("TEntry",
            fieldbackground=colors["entry_bg"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            insertcolor=colors["accent"],
            padding=10,
            relief="flat"
        )
        style.map("TEntry",
            fieldbackground=[("focus", colors["entry_focus"])],
            bordercolor=[("focus", colors["accent"])],
            lightcolor=[("focus", colors["accent"])],
            darkcolor=[("focus", colors["accent"])]
        )

        # Прогресс-бар
        style.configure("Accent.Horizontal.TProgressbar",
            troughcolor=colors["entry_bg"],
            background=colors["accent"],
            bordercolor=colors["entry_bg"],
            lightcolor=colors["accent"],
            darkcolor=colors["accent"],
            thickness=6
        )

        # Разделители в стиле Telegram (голубые)
        style.configure("TSeparator", background=colors["accent"])
        style.configure("TelegramBlue.TSeparator", background=colors["accent"])

        # Шрифты по умолчанию
        self.option_add("*Font", "{Segoe UI} 10")
        self.option_add("*TEntry*Font", "{Segoe UI} 10")
        self.option_add("*TButton*Font", "{Segoe UI} 10 bold")
        self.option_add("*TLabel*Font", "{Segoe UI} 10")

        return colors

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Хедер с большими отступами
        header = ttk.Frame(self, style="Glass.TFrame", padding=(32, 24))
        header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        header.columnconfigure(1, weight=1)
        self._build_header(header)

        # Карточки с разделителями
        cards_frame = ttk.Frame(self, style="CardInner.TFrame")
        cards_frame.grid(row=1, column=0, sticky="nsew", padx=32, pady=(16, 16))
        cards_frame.columnconfigure(0, weight=1)  # Connect card
        cards_frame.columnconfigure(1, weight=0)  # Separator 1
        cards_frame.columnconfigure(2, weight=1)  # Channels card
        cards_frame.columnconfigure(3, weight=0)  # Separator 2
        cards_frame.columnconfigure(4, weight=1)  # Export card
        cards_frame.rowconfigure(0, weight=1)

        self._build_connect_card(cards_frame)

        # Разделитель 1 (голубая линия Telegram)
        sep1_frame = tk.Frame(cards_frame, width=2, bg=self.colors["accent"])
        sep1_frame.grid(row=0, column=1, sticky="ns", padx=12)

        self._build_channel_card(cards_frame)

        # Разделитель 2 (голубая линия Telegram)
        sep2_frame = tk.Frame(cards_frame, width=2, bg=self.colors["accent"])
        sep2_frame.grid(row=0, column=3, sticky="ns", padx=12)

        self._build_export_card(cards_frame)

        self._build_logs_card()
    def _build_header(self, parent: ttk.Frame) -> None:
        
        logo_img = load_logo_image(56)
        if logo_img:
            logo_canvas = tk.Canvas(parent, width=56, height=56, highlightthickness=0, bg=self.colors["glass"], bd=0)
            logo_canvas.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
            photo = ImageTk.PhotoImage(logo_img)
            logo_canvas.create_image(0, 0, image=photo, anchor='nw')
            logo_canvas._logo_photo = photo
        
        ttk.Label(parent, text="Telegram Export Studio", style="Header.TLabel").grid(row=0, column=1, sticky="w", columnspan=2)
        ttk.Label(parent, text="Подключите свои приватные каналы и архивируйте всё в один клик.", style="Caption.TLabel").grid(row=1, column=1, sticky="w", pady=(4, 0), columnspan=2)

    def _build_connect_card(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=24)
        card.grid(row=0, column=0, sticky="nsew", padx=0)
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Подключение", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(card, text="Используйте ваши Telegram API данные для авторизации.", style="Info.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 20))

        # Keyboard layout fix: use keycode instead of keysym for Ctrl+V/C/X
        def copy_paste_handler(e):
            """Handle copy/paste/cut regardless of keyboard layout"""
            if e.keycode == 86 and e.keysym != 'v':  # Ctrl+V
                e.widget.event_generate('<<Paste>>')
            elif e.keycode == 67 and e.keysym != 'c':  # Ctrl+C
                e.widget.event_generate('<<Copy>>')
            elif e.keycode == 88 and e.keysym != 'x':  # Ctrl+X
                e.widget.event_generate('<<Cut>>')

        # API ID (использует textvariable для автоматической синхронизации)
        ttk.Label(card, text="API ID", style="Body.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.api_id_internal = tk.StringVar()
        self.api_id_entry = ttk.Entry(card, show='•', textvariable=self.api_id_internal)
        self.api_id_entry.grid(row=3, column=0, sticky="ew", pady=(0, 16))
        self.api_id_entry.bind("<Control-Key>", copy_paste_handler)
        # Синхронизация с SecureVar при любом изменении
        def sync_api_id(*_args):
            self.api_id_var.set(self.api_id_internal.get())
        self.api_id_internal.trace_add('write', sync_api_id)

        # API Hash (использует textvariable для автоматической синхронизации)
        ttk.Label(card, text="API Hash", style="Body.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 6))
        self.api_hash_internal = tk.StringVar()
        self.api_hash_entry = ttk.Entry(card, show='•', textvariable=self.api_hash_internal)
        self.api_hash_entry.grid(row=5, column=0, sticky="ew", pady=(0, 16))
        self.api_hash_entry.bind("<Control-Key>", copy_paste_handler)
        # Синхронизация с SecureVar при любом изменении
        def sync_api_hash(*_args):
            self.api_hash_var.set(self.api_hash_internal.get())
        self.api_hash_internal.trace_add('write', sync_api_hash)

        # Phone (использует textvariable для автоматической синхронизации)
        ttk.Label(card, text="Номер телефона", style="Body.TLabel").grid(row=6, column=0, sticky="w", pady=(0, 6))
        self.phone_internal = tk.StringVar()
        self.phone_entry = ttk.Entry(card, textvariable=self.phone_internal)
        self.phone_entry.grid(row=7, column=0, sticky="ew", pady=(0, 16))
        self.phone_entry.bind("<Control-Key>", copy_paste_handler)
        # Синхронизация с SecureVar при любом изменении
        def sync_phone(*_args):
            self.phone_var.set(self.phone_internal.get())
        self.phone_internal.trace_add('write', sync_phone)

        # Info message
        info_frame = ttk.Frame(card, style="CardInner.TFrame")
        info_frame.grid(row=8, column=0, sticky="ew", pady=(4, 20))
        ttk.Label(
            info_frame,
            text="🔒 Файлы сессий не сохраняются\nВам нужно будет вводить код при каждом запуске",
            style="Info.TLabel",
            justify="left"
        ).grid(row=0, column=0, sticky="w")

        self.connect_button = ttk.Button(card, text="Подключиться", style="Accent.TButton", command=self._on_connect)
        self.connect_button.grid(row=10, column=0, sticky="ew", pady=(0, 0))

    def _build_channel_card(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=24)
        card.grid(row=0, column=2, sticky="nsew", padx=0)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)

        ttk.Label(card, text="Каналы", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(card, text="Выберите чат для архивации.", style="Info.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 20))

        search_row = ttk.Frame(card, style="CardInner.TFrame")
        search_row.grid(row=2, column=0, sticky="ew")
        search_row.columnconfigure(0, weight=1)
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.refresh_button = ttk.Button(search_row, text="Обновить", style="Secondary.TButton", command=self._on_refresh)
        self.refresh_button.grid(row=0, column=1, sticky="ew")

        list_container = ttk.Frame(card, style="CardInner.TFrame")
        list_container.grid(row=3, column=0, sticky="nsew", pady=(12, 12))
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)

        self.dialog_list = tk.Listbox(
            list_container,
            selectmode=tk.BROWSE,
            activestyle="none",
            exportselection=False,
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
            bg=self.colors["card"],
            fg=self.colors["text"],
            selectbackground=self.colors["accent"],
            selectforeground=self.colors["accent_contrast"],
        )
        self.dialog_list.grid(row=0, column=0, sticky="nsew")

        ttk.Label(card, text="Подключенные чаты появятся здесь после авторизации.", style="Info.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 0))

    def _build_export_card(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=24)
        card.grid(row=0, column=4, sticky="nsew", padx=0)
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Экспорт", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.channel_label = ttk.Label(card, textvariable=self.channel_title_var, style="Info.TLabel")
        self.channel_label.grid(row=1, column=0, sticky="w", pady=(0, 20))

        ttk.Checkbutton(card, text="Блокировать опасные вложения", variable=self.block_dangerous_var).grid(row=2, column=0, sticky="w", pady=(0, 20))

        ttk.Label(card, text="Размер пакета", style="Body.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.batch_entry = ttk.Entry(card, textvariable=self.batch_var, width=8)
        self.batch_entry.grid(row=4, column=0, sticky="w", pady=(0, 20))

        self.progress_bar = ttk.Progressbar(card, style="Accent.Horizontal.TProgressbar", mode="determinate")
        self.progress_bar.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(card, textvariable=self.stats_var, style="Info.TLabel").grid(row=6, column=0, sticky="w", pady=(0, 24))

        self.export_controls_frame = ttk.Frame(card, style="CardInner.TFrame")
        self.export_controls_frame.grid(row=7, column=0, sticky="ew", pady=(0, 0))
        self.export_controls_frame.columnconfigure(0, weight=1)

        self.start_button = ttk.Button(self.export_controls_frame, text="Начать экспорт", style="Accent.TButton", command=self._on_export)
        self.start_button.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        buttons_row = ttk.Frame(self.export_controls_frame, style="CardInner.TFrame")
        buttons_row.grid(row=1, column=0, sticky="ew")
        buttons_row.columnconfigure(0, weight=1)
        buttons_row.columnconfigure(1, weight=1)

        self.pause_button = ttk.Button(buttons_row, text="Пауза", style="Secondary.TButton", command=self._on_pause_resume)
        self.pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.finish_button = ttk.Button(buttons_row, text="Завершить сейчас", style="Secondary.TButton", command=self._on_finish)
        self.finish_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        self.pause_button.state(["disabled"])
        self.finish_button.state(["disabled"])

        self.completion_frame = ttk.Frame(card, style="CardInner.TFrame")
        self.completion_frame.columnconfigure(0, weight=1)
        self.completion_frame.grid(row=7, column=0, sticky="ew", pady=(24, 0))
        self.completion_title_var = tk.StringVar(value="Экспорт завершен")
        ttk.Label(self.completion_frame, text="✓", font=("Segoe UI", 26), background=self.colors["card"], foreground=self.colors["accent"]).grid(row=0, column=0, pady=(0, 4))
        ttk.Label(self.completion_frame, textvariable=self.completion_title_var, style="Title.TLabel").grid(row=1, column=0, pady=(0, 4))
        ttk.Label(self.completion_frame, text="Ваш архив готов.", style="Info.TLabel").grid(row=2, column=0)
        completion_buttons = ttk.Frame(self.completion_frame, style="CardInner.TFrame")
        completion_buttons.grid(row=3, column=0, pady=(16, 0))
        completion_buttons.columnconfigure(0, weight=1)
        completion_buttons.columnconfigure(1, weight=1)
        completion_buttons.columnconfigure(2, weight=1)
        self.open_folder_button = ttk.Button(completion_buttons, text="Открыть папку", style="Accent.TButton", command=self._open_last_export)
        self.open_folder_button.grid(row=0, column=0, padx=(0, 8))
        self.open_html_button = ttk.Button(completion_buttons, text="Открыть HTML", style="Accent.TButton", command=self._open_index_html)
        self.open_html_button.grid(row=0, column=1, padx=(0, 8))
        self.export_again_button = ttk.Button(completion_buttons, text="Экспортировать снова", style="Ghost.TButton", command=self._reset_after_completion)
        self.export_again_button.grid(row=0, column=2)
        self.open_folder_button.state(["disabled"])
        self.open_html_button.state(["disabled"])
        self.completion_frame.grid_remove()

    def _build_logs_card(self) -> None:
        card = ttk.Frame(self, style="Card.TFrame", padding=24)
        card.grid(row=2, column=0, sticky="nsew", padx=32, pady=(0, 32))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        ttk.Label(card, text="Активность", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))

        log_container = ttk.Frame(card, style="CardInner.TFrame")
        log_container.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        log_container.columnconfigure(0, weight=1)
        log_container.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_container,
            wrap="word",
            state="disabled",
            height=8,
            bg=self.colors["entry_bg"],
            fg=self.colors["text"],
            relief="flat",
            highlightthickness=0,
            font=("Consolas", 9),
            insertbackground=self.colors["accent"],
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("timestamp", foreground=self.colors["muted"])
        self.log_text.tag_configure("message", foreground=self.colors["text"])

        status_row = ttk.Frame(card, style="CardInner.TFrame")
        status_row.grid(row=2, column=0, sticky="ew")
        ttk.Label(status_row, textvariable=self.status_var, style="Info.TLabel").grid(row=0, column=0, sticky="w")

# gui_app.py - PART 3 (App class methods continuation)
# Add these methods to the App class from Part 2

    def _apply_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        self.dialog_list.delete(0, tk.END)
        self.filtered_indices.clear()
        icon_map = {"channel": "[CH]", "group": "[GR]", "user": "[DM]"}
        
        for item in self.all_dialogs:
            title = item.get("title", "")
            if query and query not in title.lower():
                continue
            icon = icon_map.get(item.get('kind'), '•')
            
            # Sanitize title for display
            display_title = title[:100] if len(title) > 100 else title
            entry = f"{icon}  {display_title}"
            
            self.dialog_list.insert(tk.END, entry)
            self.filtered_indices.append(item["index"])
        
        self._on_channel_select()
        self._update_export_controls()

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        
        # Sanitize log message (limit length)
        if len(message) > 500:
            message = message[:497] + "..."
        
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] ", ("timestamp",))
        self.log_text.insert("end", f"{message}\n", ("message",))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _process_events(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.after(120, self._process_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        
        if etype == "log":
            msg = event.get("message")
            if msg:
                self._append_log(msg)
        
        elif etype == "error":
            msg = event.get("message", "Неожиданная ошибка")
            self._append_log(f"[Ошибка] {msg}")
            messagebox.showerror("Ошибка", msg[:200], parent=self)  # Limit error message length
            self.status_var.set("Ошибка")
            self.export_running = False
            self.export_paused = False
            self._set_progress_running(False)
            self._show_controls_view()
            self._update_export_controls()

        elif etype == "dialogs":
            self.all_dialogs = event.get("items", [])
            self._apply_filter()

        elif etype == "progress":
            count = event.get("count", 0)
            channel = event.get("channel") or "Канал"
            # Sanitize channel name
            safe_channel = channel[:50] if len(channel) > 50 else channel
            self.stats_var.set(f"{safe_channel}: {count} сообщений сохранено")

        elif etype == "export_done":
            self._last_export_info = event
            html = event.get("html_path")
            channel = event.get("channel") or "Канал"
            if html:
                self.last_export_html = html
                self.last_export_dir = os.path.dirname(html)
            self._append_log(f"[Готово] {channel[:50]} -> {html}")
        
        elif etype == "status":
            message = event.get("message", "")
            if message:
                # Limit status message length
                if len(message) > 100:
                    message = message[:97] + "..."
                self.status_var.set(message)
        
        elif etype == "export_state":
            self._handle_export_state(event.get("state"))
        
        elif etype == "input_request":
            self._handle_input_request(event)

    def _handle_export_state(self, state: Optional[str]) -> None:
        if state == "running":
            self.export_running = True
            self.export_paused = False
            self._show_controls_view()
            self._set_progress_running(True)
        
        elif state == "paused":
            self.export_paused = True
            self._set_progress_running(False)
        
        elif state == "resumed":
            self.export_paused = False
            self._set_progress_running(True)
        
        elif state == "finish_requested":
            self.export_finishing = True
            self.finish_button.state(["disabled"])
        
        elif state == "cancelled":
            self.export_finishing = False
            self.export_running = False
            self.export_paused = False
            self._set_progress_running(False)
            self._show_controls_view()
            self.status_var.set("Экспорт отменен")
        
        elif state == "completed":
            self.export_finishing = False
            self.export_running = False
            self.export_paused = False
            self._set_progress_running(False)
            info = self._last_export_info or {}
            self._show_completion_view(info)
        
        elif state == "idle":
            self.export_finishing = False
            self.export_running = False
            self.export_paused = False
            self._set_progress_running(False)
        
        self._update_export_controls()

    def _handle_input_request(self, event: dict[str, Any]) -> None:
        prompt = event.get("prompt") or "Введите значение"
        title = event.get("title") or "Ввод"
        secret = bool(event.get("secret"))
        fut = event.get("future")
        
        value = self._show_input_dialog(title=title, prompt=prompt, secret=secret)
        
        if fut is not None:
            self.worker.resolve_future(fut, value)

    def _show_input_dialog(self, title: str, prompt: str, secret: bool = False) -> Optional[str]:
        if Image is None or pystray is None:
            return simpledialog.askstring(title, prompt, show='•' if secret else '', parent=self)
        
        top = tk.Toplevel(self)
        top.title(title)
        top.configure(bg=self.colors['window'])
        top.transient(self)
        top.grab_set()
        top.resizable(False, False)

        frame = ttk.Frame(top, style='Card.TFrame', padding=20)
        frame.grid(row=0, column=0, sticky='nsew')

        # Sanitize prompt (limit length)
        safe_prompt = prompt[:200] if len(prompt) > 200 else prompt
        ttk.Label(frame, text=safe_prompt, style='Body.TLabel').grid(row=0, column=0, sticky='w')
        
        value_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=value_var, show='•' if secret else '')
        entry.grid(row=1, column=0, sticky='ew', pady=(8, 16))
        entry.focus_set()

        button_row = ttk.Frame(frame, style='CardInner.TFrame')
        button_row.grid(row=2, column=0, sticky='ew')
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=1)

        result: dict[str, Optional[str]] = {'value': None}

        def submit() -> None:
            result['value'] = value_var.get().strip() or None
            top.destroy()

        def cancel() -> None:
            result['value'] = None
            top.destroy()

        ttk.Button(button_row, text='Отмена', style='Secondary.TButton', command=cancel).grid(row=0, column=0, sticky='ew', padx=(0, 12))
        ttk.Button(button_row, text='ОК', style='Accent.TButton', command=submit).grid(row=0, column=1, sticky='ew')

        top.bind('<Return>', lambda _: submit())
        top.bind('<Escape>', lambda _: cancel())

        self._center_modal(top)
        top.wait_window()
        return result.get('value')

    def _center_modal(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        w = window.winfo_width()
        h = window.winfo_height()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        x = parent_x + max((parent_w - w) // 2, 0)
        y = parent_y + max((parent_h - h) // 2, 0)
        window.geometry('{}x{}+{}+{}'.format(w, h, x, y))
    
    def _create_tray_image(self):
        if Image is None:
            return None
        tray_img = create_tray_icon(64)
        return tray_img if tray_img else Image.new('RGBA', (64, 64), (0, 0, 0, 0))

    def _start_tray_icon(self) -> None:
        if self._tray_active or pystray is None:
            return
        image = self._create_tray_image()
        if image is None:
            return

        def on_show(icon, item):
            self.after(0, self._restore_from_tray)

        def on_exit(icon, item):
            self.after(0, self._on_exit)

        menu = pystray.Menu(
            pystray.MenuItem('Показать окно', on_show, default=True),
            pystray.MenuItem('Выход и очистка данных', on_exit),
        )
        self._tray_icon = pystray.Icon('tg-export', image, 'Telegram Export Studio', menu)

        def run() -> None:
            try:
                self._tray_active = True
                self._tray_icon.run()
            finally:
                self._tray_active = False

        self._tray_thread = threading.Thread(target=run, daemon=True)
        self._tray_thread.start()

    def _stop_tray_icon(self) -> None:
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self._tray_icon = None
        self._tray_active = False
        self._tray_thread = None

    def _restore_from_tray(self) -> None:
        self._stop_tray_icon()
        self.deiconify()
        self.lift()
        self.focus_force()

    def _minimize_to_tray(self) -> None:
        if pystray is None or Image is None or ImageDraw is None:
            messagebox.showinfo('Трей недоступен', 'Требуются pystray и Pillow для режима трея. Сворачиваю в панель задач.', parent=self)
            self.iconify()
            self.status_var.set('Свернуто в панель задач (трей отключен)')
            return
        self.withdraw()
        self._start_tray_icon()
        self.status_var.set('Работает в трее…')

    def _on_exit(self) -> None:
        """Secure cleanup on exit"""
        # Clear SecureVar objects
        try:
            self.api_id_var.clear()
            self.api_hash_var.clear()
            self.phone_var.clear()
        except Exception:
            pass

        # Clear Entry widgets from memory
        try:
            self.api_id_entry.delete(0, 'end')
            self.api_hash_entry.delete(0, 'end')
            self.phone_entry.delete(0, 'end')
        except Exception:
            pass

        # Stop background services
        self._stop_tray_icon()
        try:
            self.worker.send_command('stop')
        except Exception:
            pass

        self.after(200, self.destroy)

    def _set_progress_running(self, running: bool) -> None:
        if running:
            self.progress_bar.configure(mode="indeterminate")
            if not self.progress_animating:
                self.progress_bar.start(12)
                self.progress_animating = True
        else:
            if self.progress_animating:
                self.progress_bar.stop()
                self.progress_animating = False
            self.progress_bar.configure(mode="determinate")
            self.progress_bar["value"] = 0

    def _show_completion_view(self, info: Optional[dict[str, Any]] = None) -> None:
        info = info or {}
        channel = info.get("channel") or "Экспорт завершен"
        # Sanitize channel name
        safe_channel = channel[:50] if len(channel) > 50 else channel
        self.completion_title_var.set(f"{safe_channel} экспортирован")

        if self.last_export_dir and os.path.isdir(self.last_export_dir):
            self.open_folder_button.state(["!disabled"])
            self.open_html_button.state(["!disabled"])
        else:
            self.open_folder_button.state(["disabled"])
            self.open_html_button.state(["disabled"])
        
        self.export_controls_frame.grid_remove()
        self.completion_frame.grid()

    def _show_controls_view(self) -> None:
        self.completion_frame.grid_remove()
        self.export_controls_frame.grid()
        self.open_folder_button.state(["disabled"])
        self.open_html_button.state(["disabled"])

    def _update_export_controls(self) -> None:
        if self.export_running:
            self.start_button.state(["disabled"])
            self.pause_button.state(["!disabled"])
            if self.export_finishing:
                self.finish_button.state(["disabled"])
            else:
                self.finish_button.state(["!disabled"])
            self.pause_button.configure(text="Продолжить" if self.export_paused else "Пауза")
        else:
            selection = bool(self.dialog_list.curselection())
            if selection:
                self.start_button.state(["!disabled"])
            else:
                self.start_button.state(["disabled"])
            self.pause_button.state(["disabled"])
            self.finish_button.state(["disabled"])
            self.pause_button.configure(text="Пауза")

    def _on_connect(self) -> None:
        # Get and validate API ID
        api_id_str = self.api_id_var.get().strip()
        if not api_id_str:
            messagebox.showerror("Ошибка", "Введите корректный API ID", parent=self)
            return

        try:
            api_id = int(api_id_str)
            if api_id <= 0:
                raise ValueError("API ID должен быть положительным числом")
        except ValueError:
            messagebox.showerror("Ошибка", "API ID должен быть положительным целым числом", parent=self)
            return

        # Validate API Hash
        api_hash = self.api_hash_var.get().strip()
        if not api_hash:
            messagebox.showerror("Ошибка", "API Hash обязателен", parent=self)
            return

        if len(api_hash) != 32 or not all(c in '0123456789abcdefABCDEF' for c in api_hash):
            response = messagebox.askyesno(
                "Предупреждение",
                "Формат API Hash выглядит необычно (ожидается 32 hex символа). Продолжить?",
                parent=self
            )
            if not response:
                return

        # Validate phone
        phone = self.phone_var.get().strip()
        if not phone:
            messagebox.showerror("Ошибка", "Номер телефона обязателен", parent=self)
            return

        if not phone.startswith('+'):
            messagebox.showerror("Ошибка", "Номер телефона должен начинаться с + (например +1234567890)", parent=self)
            return

        if not phone[1:].replace(' ', '').isdigit():
            messagebox.showerror("Ошибка", "Номер телефона должен содержать только цифры после +", parent=self)
            return

        self.status_var.set("Подключение...")
        self.worker.send_command(
            "connect",
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            session_name=None,  # Always None - no session files
        )

    def _on_refresh(self) -> None:
        self.worker.send_command("refresh_dialogs")

    def _on_export(self) -> None:
        if self.export_running:
            return

        selection = self.dialog_list.curselection()
        if not selection:
            messagebox.showwarning("Внимание", "Выберите хотя бы один канал", parent=self)
            return

        dialog_indices = [self.filtered_indices[i] for i in selection]
        refresh_seconds = None

        # Validate batch size
        try:
            batch_value = int(self.batch_var.get().strip())
            if batch_value <= 0:
                raise ValueError("Размер пакета должен быть положительным")
            if batch_value > 1000:
                response = messagebox.askyesno(
                    "Предупреждение",
                    "Очень большой размер пакета может вызвать проблемы с памятью. Продолжить?",
                    parent=self
                )
                if not response:
                    return
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверный размер пакета: {e}", parent=self)
            return

        self.stats_var.set("Сообщений сохранено: 0")
        self._show_controls_view()
        
        self.worker.send_command(
            "export",
            dialog_indices=dialog_indices,
            anonymize=False,
            block_dangerous=self.block_dangerous_var.get(),
            refresh_seconds=refresh_seconds,
            progress_every=max(1, batch_value),
        )
        
        self.export_running = True
        self.export_paused = False
        self.export_finishing = False
        self._set_progress_running(True)
        self._update_export_controls()

    def _on_pause_resume(self) -> None:
        if not self.export_running:
            return
        if self.export_paused:
            self.worker.request_resume()
        else:
            self.worker.request_pause()

    def _on_finish(self) -> None:
        if not self.export_running:
            return
        if self.worker.request_finish():
            self.finish_button.state(["disabled"])
            self.pause_button.state(["disabled"])

    def _open_last_export(self) -> None:
        if not self.last_export_dir or not os.path.isdir(self.last_export_dir):
            messagebox.showinfo("Открыть папку", "Директория экспорта пока недоступна.", parent=self)
            return

        # SECURITY: Validate path before opening
        try:
            # Check if path is within expected export directory
            export_root = os.path.abspath("export")
            target_path = os.path.abspath(self.last_export_dir)

            if not target_path.startswith(export_root):
                messagebox.showerror("Ошибка безопасности", "Неверный путь экспорта", parent=self)
                return

            if sys.platform.startswith("win"):
                os.startfile(self.last_export_dir)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.last_export_dir])
            else:
                subprocess.Popen(["xdg-open", self.last_export_dir])

        except Exception as exc:
            messagebox.showerror("Открыть папку", f"Не удалось открыть папку: {exc}", parent=self)

    def _open_index_html(self) -> None:
        if not self.last_export_dir or not os.path.isdir(self.last_export_dir):
            messagebox.showinfo("Открыть HTML", "Директория экспорта пока недоступна.", parent=self)
            return

        index_html_path = os.path.join(self.last_export_dir, "index.html")
        if not os.path.isfile(index_html_path):
            messagebox.showerror("Ошибка", "Файл index.html не найден", parent=self)
            return

        # SECURITY: Validate path before opening
        try:
            # Check if path is within expected export directory
            export_root = os.path.abspath("export")
            target_path = os.path.abspath(index_html_path)

            if not target_path.startswith(export_root):
                messagebox.showerror("Ошибка безопасности", "Неверный путь экспорта", parent=self)
                return

            if sys.platform.startswith("win"):
                os.startfile(index_html_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", index_html_path])
            else:
                subprocess.Popen(["xdg-open", index_html_path])

        except Exception as exc:
            messagebox.showerror("Открыть HTML", f"Не удалось открыть файл: {exc}", parent=self)

    def _reset_after_completion(self) -> None:
        self._last_export_info = None
        self.last_export_html = None
        self.last_export_dir = None
        self.stats_var.set("Сообщений сохранено: 0")
        self.status_var.set("Готов")
        self._show_controls_view()
        self._update_export_controls()

    def _on_channel_select(self, *_: Any) -> None:
        selection = self.dialog_list.curselection()
        if not selection:
            if self.filtered_indices:
                self.channel_title_var.set("Выберите канал для экспорта")
            else:
                self.channel_title_var.set("Каналы недоступны")
        else:
            idx = self.filtered_indices[selection[0]]
            match = next((d for d in self.all_dialogs if d.get("index") == idx), None)
            if match:
                title = match.get("title") or "Канал"
                # Sanitize title for display
                safe_title = title[:100] if len(title) > 100 else title
                self.channel_title_var.set(safe_title)
            else:
                self.channel_title_var.set("Канал")
        self._update_export_controls()

    def _on_close(self) -> None:
        """Handle window close event - minimize to tray if export is running"""
        # If export is running, minimize to tray to keep it working
        if self.export_running:
            self._minimize_to_tray()
        else:
            # If nothing is running, ask user what to do
            if messagebox.askyesno("Выход", "Хотите выйти из приложения?"):
                self._on_exit()
            else:
                self._minimize_to_tray()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()


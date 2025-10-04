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

from channel_data import dump_dialog_to_json_and_media

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None
    Image = None
    ImageDraw = None

from html_generator import generate_html
from telegram_api import authorize, list_user_dialogs

DEFAULT_PROGRESS_EVERY = 50

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
                self._emit("log", message=f"🗑️ Deleted: {session_file}")
                continue
            except PermissionError:
                pass
            except Exception as e:
                self._emit("log", message=f"❌ Error deleting {session_file}: {e}")
                session_files_failed.append(session_file)
                continue
            
            # Attempt 2: Wait and retry
            try:
                time.sleep(0.3)
                os.remove(session_file)
                session_files_deleted.append(session_file)
                self._emit("log", message=f"🗑️ Deleted (retry): {session_file}")
                continue
            except Exception:
                pass
            
            # Attempt 3: Rename for deletion on next start
            try:
                trash_name = f"{session_file}.DELETE_ME_{int(time.time())}"
                os.rename(session_file, trash_name)
                self._emit("log", message=f"🔄 Marked for deletion: {session_file}")
                try:
                    os.remove(trash_name)
                    session_files_deleted.append(session_file)
                    self._emit("log", message=f"🗑️ Deleted (renamed): {session_file}")
                except Exception:
                    session_files_failed.append(session_file)
                    self._emit("log", message=f"⚠️ Will be deleted on next start: {trash_name}")
            except Exception as e:
                session_files_failed.append(session_file)
                self._emit("log", message=f"❌ Cannot process {session_file}: {e}")
        
        # Clean up old .DELETE_ME files
        for old_trash in glob.glob("*.DELETE_ME_*"):
            try:
                os.remove(old_trash)
                self._emit("log", message=f"🗑️ Cleaned old trash: {old_trash}")
            except Exception:
                pass
        
        if session_files_deleted:
            self._emit("log", message=f"✅ Cleaned up {len(session_files_deleted)} session file(s)")
        if session_files_failed:
            self._emit("log", message=f"⚠️ {len(session_files_failed)} file(s) require restart to delete")
        
        self._emit("status", message="Disconnected (sessions cleaned)")
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
        self._emit("status", message=f"Connected: {identity[:20]}")  # Limit length
        self._emit("log", message="Authorization successful")
        await self._send_dialogs()

    async def _cmd_refresh_dialogs(self) -> None:
        if not self.client:
            raise RuntimeError("Connect your account first")
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
            raise RuntimeError("Connect your account first")
        if self._export_running:
            raise RuntimeError("Export already in progress")
        
        indices = list(dict.fromkeys(dialog_indices))
        if not indices:
            raise RuntimeError("Select a channel to export")

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
                    raise RuntimeError("Selected dialog is out of range")
                
                dialog = self.dialogs[idx]
                title = (
                    getattr(dialog.entity, "title", None)
                    or getattr(dialog.entity, "first_name", None)
                    or getattr(dialog.entity, "last_name", None)
                    or "Channel"
                )
                
                # Sanitize title for logging
                safe_title = title[:50] if title else "Channel"
                
                self._current_dialog_title = safe_title
                self._emit("status", message=f"Preparing export: {safe_title}")
                self._emit("log", message=f"Starting export: {safe_title}")
                
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
                    self._emit("status", message="Export finalized")
                    break
                
                if self._export_cancel_event.is_set():
                    raise asyncio.CancelledError()
            else:
                completed_successfully = True
                self._emit("status", message="Export completed")
        
        except asyncio.CancelledError:
            self._emit("log", message="Export cancelled")
            self._emit("status", message="Export cancelled")
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
        safe_title = title[:50] if title else "Channel"
        
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
            
            header = "Message " + " ".join(summary_parts) if summary_parts else "Message"
            
            text_raw = info.get("text") or ""
            text_snippet = " ".join(text_raw.splitlines()).strip()
            
            # Limit text length in logs
            if len(text_snippet) > 100:
                text_snippet = f"{text_snippet[:97]}..."
            
            body = text_snippet or "(no text)"
            self._emit("log", message=f"[{safe_title}] {header}: {body}")
            
            for media in info.get("media") or []:
                kind = media.get("kind") or "file"
                if kind == "blocked":
                    name = media.get("name") or "file"
                    reason = media.get("reason") or "blocked"
                    self._emit("log", message=f"  blocked {name} ({reason})")
                else:
                    path_hint = media.get("path") or media.get("name") or "unknown"
                    self._emit("log", message=f"  saved {kind}: {path_hint}")

        def on_media_event(info: dict[str, Any]) -> None:
            stage = info.get("stage")
            kind = (info.get("kind") or "file").strip()
            name = (info.get("name") or info.get("path") or "media").strip()
            message_id = info.get("message_id")
            
            # Limit name length
            if len(name) > 50:
                name = name[:47] + "..."
            
            key = (safe_title, name)
            label = f"Downloading {kind}: {name}"
            if message_id is not None:
                label = f"{label} (msg {message_id})"

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
                    status = f"{label} {current or 0}/{total or '?'} bytes"
                    self._emit("status", message=status)
            
            elif stage == "complete":
                self._media_progress.pop(key, None)
                self._media_labels.pop(key, None)
                self._emit("log", message=f"[{safe_title}] Saved {kind}: {name}")
                self._emit("status", message=f"Saved {kind}: {name}")
            
            elif stage == "blocked":
                reason = info.get("reason") or "blocked"
                self._media_progress.pop(key, None)
                self._media_labels.pop(key, None)
                self._emit("log", message=f"[{safe_title}] Blocked {kind}: {name} ({reason})")
            
            elif stage == "error":
                self._media_progress.pop(key, None)
                self._media_labels.pop(key, None)
                self._emit("log", message=f"[{safe_title}] Failed {kind}: {name}")

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
        self._emit("status", message=f"Export finished: {safe_title}")

    def request_pause(self) -> bool:
        if not self._export_running or not self._export_pause_event or not self.loop:
            return False
        if not self._export_pause_event.is_set():
            return False
        
        self.loop.call_soon_threadsafe(self._export_pause_event.clear)
        self._emit("status", message="Export paused")
        self._emit("log", message="Export paused")
        self._emit("export_state", state="paused")
        return True

    def request_resume(self) -> bool:
        if not self._export_running or not self._export_pause_event or not self.loop:
            return False
        if self._export_pause_event.is_set():
            return False
        
        self.loop.call_soon_threadsafe(self._export_pause_event.set)
        self._emit("status", message="Resuming export")
        self._emit("log", message="Resuming export")
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
        
        self._emit("log", message="Finishing export with current data...")
        self._emit("status", message="Finalizing export")
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
            raise RuntimeError("Input cancelled by user")
        return result.strip()

    async def _request_code(self, prompt: str) -> str:
        return await self._run_input_dialog(prompt, "Verification code")

    async def _request_password(self, prompt: str) -> str:
        return await self._run_input_dialog(prompt, "2FA password", secret=True)

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
        self.geometry("1180x720")
        self.minsize(1100, 700)

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
        self.anonymize_var = tk.BooleanVar(value=False)
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
            colors = {
                "window": "#172B4D",
                "card": "#1F355A",
                "glass": "#243C63",
                "accent": "#27B0FF",
                "accent_hover": "#3BC3FF",
                "accent_active": "#1993D6",
                "accent_contrast": "#071522",
                "text": "#E6F0FF",
                "muted": "#94A3B8",
                "border": "#26446B",
                "entry_bg": "#1F355A",
                "entry_focus": "#274972",
            }
        else:
            colors = {
                "window": "#F4F8FB",
                "card": "#FFFFFF",
                "glass": "#FFFFFF",
                "accent": "#229ED9",
                "accent_hover": "#33B8EE",
                "accent_active": "#1B86B8",
                "accent_contrast": "#FFFFFF",
                "text": "#1F2D3D",
                "muted": "#6E7C87",
                "border": "#DBE4EA",
                "entry_bg": "#FFFFFF",
                "entry_focus": "#E6F5FF",
            }

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=colors["window"], foreground=colors["text"], font=("Inter", 11))
        style.configure("Card.TFrame", background=colors["card"], relief="flat", borderwidth=0)
        style.configure("Glass.TFrame", background=colors["glass"], relief="flat", borderwidth=0)
        style.configure("CardInner.TFrame", background=colors["card"], relief="flat", borderwidth=0)
        style.configure("Header.TLabel", background=colors["glass"], foreground=colors["text"], font=("Inter", 20, "bold"))
        style.configure("Title.TLabel", background=colors["card"], foreground=colors["text"], font=("Inter", 15, "semibold"))
        style.configure("Info.TLabel", background=colors["card"], foreground=colors["muted"], font=("Inter", 10))
        style.configure("Body.TLabel", background=colors["card"], foreground=colors["text"], font=("Inter", 11))
        style.configure("Caption.TLabel", background=colors["glass"], foreground=colors["muted"], font=("Inter", 11))
        style.configure("Accent.TButton", background=colors["accent"], foreground=colors["accent_contrast"], padding=(16, 8), borderwidth=0)
        style.map(
            "Accent.TButton",
            background=[("active", colors["accent_hover"]), ("pressed", colors["accent_active"]), ("disabled", colors["border"])],
            foreground=[("disabled", colors["muted"])]
        )
        style.configure("Secondary.TButton", background=colors["card"], foreground=colors["text"], padding=(14, 8), borderwidth=1, relief="solid", bordercolor=colors["border"])
        style.map(
            "Secondary.TButton",
            background=[("active", colors["entry_focus"]), ("pressed", colors["entry_focus"]), ("disabled", colors["card"])],
            foreground=[("disabled", colors["muted"])]
        )
        style.configure("Ghost.TButton", background=colors["card"], foreground=colors["accent"], padding=(12, 8), borderwidth=0)
        style.map("Ghost.TButton", foreground=[("active", colors["accent_hover"]), ("pressed", colors["accent_active"]), ("disabled", colors["muted"])])
        style.configure("TCheckbutton", background=colors["card"], foreground=colors["text"], focuscolor=colors["accent"])
        style.map("TCheckbutton", foreground=[("disabled", colors["muted"])])
        style.configure("TEntry", fieldbackground=colors["entry_bg"], bordercolor=colors["border"], lightcolor=colors["accent"], darkcolor=colors["border"], insertcolor=colors["accent"], padding=6)
        style.map("TEntry", fieldbackground=[("focus", colors["entry_focus"])], bordercolor=[("focus", colors["accent"])])
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=colors["card"], background=colors["accent"], bordercolor=colors["card"], lightcolor=colors["accent"], darkcolor=colors["accent"])
        style.configure("TSeparator", background=colors["border"])

        self.option_add("*Font", "Inter 11")
        self.option_add("*TEntry*Font", "Inter 11")
        self.option_add("*TButton*Font", "Inter 11")
        self.option_add("*TLabel*Font", "Inter 11")

        return colors

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ttk.Frame(self, style="Glass.TFrame", padding=(24, 20))
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 12))
        header.columnconfigure(1, weight=1)
        header.columnconfigure(2, weight=0)
        self._build_header(header)

        cards_frame = ttk.Frame(self, style="CardInner.TFrame")
        cards_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 12))
        cards_frame.columnconfigure(0, weight=1)
        cards_frame.columnconfigure(1, weight=1)
        cards_frame.columnconfigure(2, weight=1)
        cards_frame.rowconfigure(0, weight=1)

        self._build_connect_card(cards_frame)
        self._build_channel_card(cards_frame)
        self._build_export_card(cards_frame)

        self._build_logs_card()

    def _build_header(self, parent: ttk.Frame) -> None:
        logo = tk.Canvas(parent, width=56, height=56, highlightthickness=0, bg=self.colors["glass"], bd=0)
        logo.grid(row=0, column=0, rowspan=2, sticky="w")
        logo.create_oval(4, 4, 52, 52, fill=self.colors["accent"], outline="")
        logo.create_polygon(20, 18, 40, 26, 24, 30, 20, 46, 16, 32, outline="", fill=self.colors["accent_contrast"])

        title = ttk.Label(parent, text="Telegram Export Studio", style="Header.TLabel")
        title.grid(row=0, column=1, sticky="w")
        subtitle = ttk.Label(parent, text="Connect your private channels and archive everything with a single click.", style="Caption.TLabel")
        subtitle.grid(row=1, column=1, sticky="w", pady=(4, 0))

        action_row = ttk.Frame(parent, style="Glass.TFrame")
        action_row.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(action_row, text="Hide to tray", style="Secondary.TButton", command=self._minimize_to_tray).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(action_row, text="Exit", style="Accent.TButton", command=self._on_exit).grid(row=0, column=1)

    def _build_connect_card(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Connect", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="Use your Telegram API credentials to authorize.", style="Info.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 16))

        # API ID
        ttk.Label(card, text="API ID", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        self.api_id_entry = ttk.Entry(card, show='•')
        self.api_id_entry.grid(row=3, column=0, sticky="ew", pady=(4, 12))
        
        # Bind to SecureVar
        def on_api_id_change(*args):
            self.api_id_var.set(self.api_id_entry.get())
        self.api_id_entry.bind('<KeyRelease>', on_api_id_change)

        # API Hash
        ttk.Label(card, text="API Hash", style="Body.TLabel").grid(row=4, column=0, sticky="w")
        self.api_hash_entry = ttk.Entry(card, show='•')
        self.api_hash_entry.grid(row=5, column=0, sticky="ew", pady=(4, 12))
        
        def on_api_hash_change(*args):
            self.api_hash_var.set(self.api_hash_entry.get())
        self.api_hash_entry.bind('<KeyRelease>', on_api_hash_change)

        # Phone
        ttk.Label(card, text="Phone number", style="Body.TLabel").grid(row=6, column=0, sticky="w")
        self.phone_entry = ttk.Entry(card)
        self.phone_entry.grid(row=7, column=0, sticky="ew", pady=(4, 12))
        
        def on_phone_change(*args):
            self.phone_var.set(self.phone_entry.get())
        self.phone_entry.bind('<KeyRelease>', on_phone_change)

        # Info message
        info_frame = ttk.Frame(card, style="CardInner.TFrame")
        info_frame.grid(row=8, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(
            info_frame,
            text="🔒 No session files will be saved\nYou'll need to enter code on each launch",
            style="Info.TLabel",
            justify="left"
        ).grid(row=0, column=0, sticky="w")

        self.connect_button = ttk.Button(card, text="Connect", style="Accent.TButton", command=self._on_connect)
        self.connect_button.grid(row=10, column=0, sticky="ew", pady=(18, 0))

    def _build_channel_card(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        card.grid(row=0, column=1, sticky="nsew", padx=12)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)

        ttk.Label(card, text="Channels", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="Select a chat to archive.", style="Info.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 16))

        search_row = ttk.Frame(card, style="CardInner.TFrame")
        search_row.grid(row=2, column=0, sticky="ew")
        search_row.columnconfigure(0, weight=1)
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.refresh_button = ttk.Button(search_row, text="Refresh", style="Secondary.TButton", command=self._on_refresh)
        self.refresh_button.grid(row=0, column=1, sticky="w", padx=(12, 0))

        list_container = ttk.Frame(card, style="CardInner.TFrame")
        list_container.grid(row=3, column=0, sticky="nsew", pady=(16, 0))
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)

        self.dialog_list = tk.Listbox(
            list_container,
            selectmode=tk.BROWSE,
            activestyle="none",
            exportselection=False,
            borderwidth=0,
            highlightthickness=0,
            font=("Inter", 11),
            bg=self.colors["card"],
            fg=self.colors["text"],
            selectbackground=self.colors["accent"],
            selectforeground=self.colors["accent_contrast"],
        )
        self.dialog_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_container, orient="vertical", command=self.dialog_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.dialog_list.configure(yscrollcommand=scroll.set)

        ttk.Label(card, text="Connected chats appear here once authorized.", style="Info.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 0))

    def _build_export_card(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        card.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Export", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.channel_label = ttk.Label(card, textvariable=self.channel_title_var, style="Info.TLabel")
        self.channel_label.grid(row=1, column=0, sticky="w", pady=(4, 12))

        options = ttk.Frame(card, style="CardInner.TFrame")
        options.grid(row=2, column=0, sticky="ew")
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)

        ttk.Checkbutton(card, text="Anonymize sender names", variable=self.anonymize_var).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(card, text="Block risky attachments", variable=self.block_dangerous_var).grid(row=4, column=0, sticky="w", pady=(4, 16))

        ttk.Label(card, text="Batch size", style="Body.TLabel").grid(row=5, column=0, sticky="w")
        self.batch_entry = ttk.Entry(card, textvariable=self.batch_var, width=8)
        self.batch_entry.grid(row=6, column=0, sticky="w", pady=(4, 0))

        self.progress_bar = ttk.Progressbar(card, style="Accent.Horizontal.TProgressbar", mode="determinate")
        self.progress_bar.grid(row=7, column=0, sticky="ew", pady=(24, 8))
        ttk.Label(card, textvariable=self.stats_var, style="Info.TLabel").grid(row=8, column=0, sticky="w")

        self.export_controls_frame = ttk.Frame(card, style="CardInner.TFrame")
        self.export_controls_frame.grid(row=9, column=0, sticky="ew", pady=(24, 0))
        self.export_controls_frame.columnconfigure(0, weight=1)
        self.export_controls_frame.columnconfigure(1, weight=1)
        self.export_controls_frame.columnconfigure(2, weight=1)

        self.start_button = ttk.Button(self.export_controls_frame, text="Start export", style="Accent.TButton", command=self._on_export)
        self.start_button.grid(row=0, column=0, sticky="ew")
        self.pause_button = ttk.Button(self.export_controls_frame, text="Pause", style="Secondary.TButton", command=self._on_pause_resume)
        self.pause_button.grid(row=0, column=1, sticky="ew", padx=12)
        self.finish_button = ttk.Button(self.export_controls_frame, text="Finish now", style="Secondary.TButton", command=self._on_finish)
        self.finish_button.grid(row=0, column=2, sticky="ew")

        self.pause_button.state(["disabled"])
        self.finish_button.state(["disabled"])

        self.completion_frame = ttk.Frame(card, style="CardInner.TFrame")
        self.completion_frame.columnconfigure(0, weight=1)
        self.completion_frame.grid(row=9, column=0, sticky="ew", pady=(24, 0))
        self.completion_title_var = tk.StringVar(value="Export complete")
        ttk.Label(self.completion_frame, text="✓", font=("Inter", 26), background=self.colors["card"], foreground=self.colors["accent"]).grid(row=0, column=0, pady=(0, 4))
        ttk.Label(self.completion_frame, textvariable=self.completion_title_var, style="Title.TLabel").grid(row=1, column=0, pady=(0, 4))
        ttk.Label(self.completion_frame, text="Your archive is ready.", style="Info.TLabel").grid(row=2, column=0)
        completion_buttons = ttk.Frame(self.completion_frame, style="CardInner.TFrame")
        completion_buttons.grid(row=3, column=0, pady=(16, 0))
        self.open_folder_button = ttk.Button(completion_buttons, text="Open folder", style="Accent.TButton", command=self._open_last_export)
        self.open_folder_button.grid(row=0, column=0, padx=(0, 12))
        self.export_again_button = ttk.Button(completion_buttons, text="Export again", style="Ghost.TButton", command=self._reset_after_completion)
        self.export_again_button.grid(row=0, column=1)
        self.open_folder_button.state(["disabled"])
        self.completion_frame.grid_remove()

    def _build_logs_card(self) -> None:
        card = ttk.Frame(self, style="Card.TFrame", padding=20)
        card.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 24))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        ttk.Label(card, text="Activity", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        log_container = ttk.Frame(card, style="CardInner.TFrame")
        log_container.grid(row=1, column=0, sticky="nsew", pady=(12, 12))
        log_container.columnconfigure(0, weight=1)
        log_container.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_container,
            wrap="word",
            state="disabled",
            height=10,
            bg=self.colors["card"],
            fg=self.colors["text"],
            relief="flat",
            highlightthickness=0,
            font=("Inter", 10),
            insertbackground=self.colors["accent"],
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_container, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
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
            msg = event.get("message", "Unexpected error")
            self._append_log(f"[Error] {msg}")
            messagebox.showerror("Error", msg[:200], parent=self)  # Limit error message length
            self.status_var.set("Error")
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
            channel = event.get("channel") or "Channel"
            # Sanitize channel name
            safe_channel = channel[:50] if len(channel) > 50 else channel
            self.stats_var.set(f"{safe_channel}: {count} messages saved")
        
        elif etype == "export_done":
            self._last_export_info = event
            html = event.get("html_path")
            channel = event.get("channel") or "Channel"
            if html:
                self.last_export_html = html
                self.last_export_dir = os.path.dirname(html)
            self._append_log(f"[Done] {channel[:50]} -> {html}")
        
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
            self.status_var.set("Export cancelled")
        
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
        prompt = event.get("prompt") or "Enter value"
        title = event.get("title") or "Input"
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

        ttk.Button(button_row, text='Cancel', style='Secondary.TButton', command=cancel).grid(row=0, column=0, sticky='ew', padx=(0, 12))
        ttk.Button(button_row, text='OK', style='Accent.TButton', command=submit).grid(row=0, column=1, sticky='ew')

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
        if Image is None or ImageDraw is None:
            return None
        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=self.colors.get('accent', '#229ED9'))
        draw.polygon((26, 22, 44, 30, 30, 34, 26, 50, 22, 36), fill=self.colors.get('accent_contrast', '#FFFFFF'))
        return image

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
            pystray.MenuItem('Show window', on_show),
            pystray.MenuItem('Exit', on_exit),
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
            messagebox.showinfo('Tray unavailable', 'pystray and Pillow are required for tray mode. Exiting instead.', parent=self)
            self._on_exit()
            return
        self.withdraw()
        self._start_tray_icon()
        self.status_var.set('Running in tray…')

    def _on_exit(self) -> None:
        # Secure cleanup
        try:
            self.api_id_var.clear()
            self.api_hash_var.clear()
            self.phone_var.clear()
        except Exception:
            pass
        
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
        channel = info.get("channel") or "Export complete"
        # Sanitize channel name
        safe_channel = channel[:50] if len(channel) > 50 else channel
        self.completion_title_var.set(f"{safe_channel} exported")
        
        if self.last_export_dir and os.path.isdir(self.last_export_dir):
            self.open_folder_button.state(["!disabled"])
        else:
            self.open_folder_button.state(["disabled"])
        
        self.export_controls_frame.grid_remove()
        self.completion_frame.grid()

    def _show_controls_view(self) -> None:
        self.completion_frame.grid_remove()
        self.export_controls_frame.grid()
        self.open_folder_button.state(["disabled"])

    def _update_export_controls(self) -> None:
        if self.export_running:
            self.start_button.state(["disabled"])
            self.pause_button.state(["!disabled"])
            if self.export_finishing:
                self.finish_button.state(["disabled"])
            else:
                self.finish_button.state(["!disabled"])
            self.pause_button.configure(text="Resume" if self.export_paused else "Pause")
        else:
            selection = bool(self.dialog_list.curselection())
            if selection:
                self.start_button.state(["!disabled"])
            else:
                self.start_button.state(["disabled"])
            self.pause_button.state(["disabled"])
            self.finish_button.state(["disabled"])
            self.pause_button.configure(text="Pause")

    def _on_connect(self) -> None:
        # Get and validate API ID
        api_id_str = self.api_id_var.get().strip()
        if not api_id_str:
            messagebox.showerror("Error", "Enter a valid API ID", parent=self)
            return
        
        try:
            api_id = int(api_id_str)
            if api_id <= 0:
                raise ValueError("API ID must be positive")
        except ValueError:
            messagebox.showerror("Error", "API ID must be a valid positive integer", parent=self)
            return
        
        # Validate API Hash
        api_hash = self.api_hash_var.get().strip()
        if not api_hash:
            messagebox.showerror("Error", "API Hash is required", parent=self)
            return
        
        if len(api_hash) != 32 or not all(c in '0123456789abcdefABCDEF' for c in api_hash):
            response = messagebox.askyesno(
                "Warning",
                "API Hash format looks unusual (expected 32 hex characters). Continue anyway?",
                parent=self
            )
            if not response:
                return
        
        # Validate phone
        phone = self.phone_var.get().strip()
        if not phone:
            messagebox.showerror("Error", "Phone number is required", parent=self)
            return
        
        if not phone.startswith('+'):
            messagebox.showerror("Error", "Phone number must start with + (e.g. +1234567890)", parent=self)
            return
        
        if not phone[1:].replace(' ', '').isdigit():
            messagebox.showerror("Error", "Phone number must contain only digits after +", parent=self)
            return
        
        self.status_var.set("Connecting...")
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
            messagebox.showwarning("Attention", "Select at least one channel", parent=self)
            return
        
        dialog_indices = [self.filtered_indices[i] for i in selection]
        refresh_seconds = None
        
        # Validate batch size
        try:
            batch_value = int(self.batch_var.get().strip())
            if batch_value <= 0:
                raise ValueError("Batch size must be positive")
            if batch_value > 1000:
                response = messagebox.askyesno(
                    "Warning",
                    "Very large batch size may cause memory issues. Continue?",
                    parent=self
                )
                if not response:
                    return
        except ValueError as e:
            messagebox.showerror("Error", f"Invalid batch size: {e}", parent=self)
            return
        
        self.stats_var.set("Messages saved: 0")
        self._show_controls_view()
        
        self.worker.send_command(
            "export",
            dialog_indices=dialog_indices,
            anonymize=self.anonymize_var.get(),
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
            messagebox.showinfo("Open folder", "Export directory is not available yet.", parent=self)
            return
        
        # SECURITY: Validate path before opening
        try:
            # Check if path is within expected export directory
            export_root = os.path.abspath("export")
            target_path = os.path.abspath(self.last_export_dir)
            
            if not target_path.startswith(export_root):
                messagebox.showerror("Security Error", "Invalid export path", parent=self)
                return
            
            if sys.platform.startswith("win"):
                os.startfile(self.last_export_dir)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.last_export_dir])
            else:
                subprocess.Popen(["xdg-open", self.last_export_dir])
        
        except Exception as exc:
            messagebox.showerror("Open folder", f"Cannot open folder: {exc}", parent=self)

    def _reset_after_completion(self) -> None:
        self._last_export_info = None
        self.last_export_html = None
        self.last_export_dir = None
        self.stats_var.set("Messages saved: 0")
        self.status_var.set("Ready")
        self._show_controls_view()
        self._update_export_controls()

    def _on_channel_select(self, *_: Any) -> None:
        selection = self.dialog_list.curselection()
        if not selection:
            if self.filtered_indices:
                self.channel_title_var.set("Select a channel to export")
            else:
                self.channel_title_var.set("No channels available")
        else:
            idx = self.filtered_indices[selection[0]]
            match = next((d for d in self.all_dialogs if d.get("index") == idx), None)
            if match:
                title = match.get("title") or "Channel"
                # Sanitize title for display
                safe_title = title[:100] if len(title) > 100 else title
                self.channel_title_var.set(safe_title)
            else:
                self.channel_title_var.set("Channel")
        self._update_export_controls()

    def _on_close(self) -> None:
        self._minimize_to_tray()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
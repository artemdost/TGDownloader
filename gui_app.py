# gui_app.py
import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Optional

from channel_data import dump_dialog_to_json_and_media
from html_generator import generate_html
from telegram_api import authorize, list_user_dialogs

DEFAULT_PROGRESS_EVERY = 50


class Worker:
    """Background thread that talks to Telegram without blocking tkinter."""

    def __init__(self, ui_queue: queue.Queue[dict[str, Any]]) -> None:
        self.ui_queue = ui_queue
        self.command_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client = None
        self.dialogs = []
        self._pending_inputs: set[asyncio.Future] = set()

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
            except Exception as exc:  # noqa: BLE001
                self._emit("error", message=str(exc))

    async def _handle_stop(self) -> None:
        if self.loop:
            for fut in list(self._pending_inputs):
                if not fut.done():
                    fut.set_result(None)
            self._pending_inputs.clear()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._emit("status", message="Disconnected")

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
            except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Authorization failed: {exc}") from exc
        self.client = client
        me = await client.get_me()
        identity = (
            getattr(me, "username", None)
            or getattr(me, "first_name", None)
            or getattr(me, "last_name", None)
            or "account"
        )
        self._emit("status", message=f"Connected: {identity}")
        self._emit("log", message="Authorization successful")
        await self._send_dialogs()

    async def _cmd_refresh_dialogs(self) -> None:
        if not self.client:
            raise RuntimeError("Connect your account first")
        await self._send_dialogs()

    async def _cmd_export(
        self,
        dialog_index: int,
        anonymize: bool,
        block_dangerous: bool,
        refresh_seconds: Optional[int],
        progress_every: int,
    ) -> None:
        if not self.client:
            raise RuntimeError("Connect your account first")
        if dialog_index < 0 or dialog_index >= len(self.dialogs):
            raise RuntimeError("Select a dialog to export")
        dialog = self.dialogs[dialog_index]
        title = (
            getattr(dialog.entity, "title", None)
            or getattr(dialog.entity, "first_name", None)
            or getattr(dialog.entity, "last_name", None)
            or "No title"
        )
        self._emit("status", message=f"Exporting: {title}")
        self._emit("log", message=f"Starting export: {title}")

        def on_progress(json_path: str, media_dir: str, count: int) -> None:
            self._emit(
                "progress",
                json_path=json_path,
                media_dir=media_dir,
                count=count,
            )
            self._emit("log", message=f"Saved {count} messages...")

        def on_message(info: dict[str, Any]) -> None:
            msg_id = info.get("id")
            count = info.get("count")
            text_raw = info.get("text") or ""
            text_snippet = text_raw.replace("\r", " ").replace("\n", " ").strip()
            if len(text_snippet) > 120:
                text_snippet = text_snippet[:117] + "..."
            media_items = info.get("media") or []
            summary_parts = []
            if count is not None:
                summary_parts.append(f"#{count}")
            if msg_id is not None:
                summary_parts.append(f"id {msg_id}")
            header = "Message " + " ".join(summary_parts) if summary_parts else "Message"
            body = text_snippet or "(no text)"
            self._emit("log", message=f"{header}: {body}")
            for media in media_items:
                kind = media.get("kind") or "file"
                if kind == "blocked":
                    name = media.get("name") or "file"
                    reason = media.get("reason") or "blocked"
                    self._emit("log", message=f"  blocked {name} ({reason})")
                else:
                    path_hint = media.get("path") or media.get("name") or "unknown"
                    self._emit("log", message=f"  saved {kind}: {path_hint}")

        try:
            json_path, media_dir = await dump_dialog_to_json_and_media(
                self.client,
                dialog,
                out_root="export",
                progress_every=progress_every,
                on_progress=on_progress,
                on_message=on_message,
                skip_dangerous=block_dangerous,
            )
            html_path = generate_html(
                json_path=json_path,
                media_root=media_dir,
                channel_title=title,
                refresh_seconds=refresh_seconds,
                anonymize=anonymize,
                csp=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Export failed: {exc}") from exc

        self._emit(
            "export_done",
            json_path=json_path,
            media_dir=media_dir,
            html_path=html_path,
        )
        self._emit("status", message=f"Export finished: {title}")

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
            items.append({"index": idx, "title": title, "kind": getattr(dlg, "_tgdl_kind", "?")})
        self._emit("dialogs", items=items)
        self._emit("log", message=f"Dialogs updated: {len(items)}")

    async def _request_input(self, prompt: str, title: str, secret: bool = False) -> str:
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
        return await self._request_input(prompt, "Verification code")

    async def _request_password(self, prompt: str) -> str:
        return await self._request_input(prompt, "2FA password", secret=True)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Telegram Export GUI")
        self.geometry("1100x650")

        self.ui_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker = Worker(self.ui_queue)
        self.worker.start()

        self.api_id_var = tk.StringVar()
        self.api_hash_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        self.session_var = tk.StringVar(value="tg_gui")
        self.no_session_var = tk.BooleanVar(value=False)
        self.anonymize_var = tk.BooleanVar(value=True)
        self.block_dangerous_var = tk.BooleanVar(value=True)
        self.refresh_var = tk.StringVar()
        self.batch_var = tk.StringVar(value=str(DEFAULT_PROGRESS_EVERY))

        self.status_var = tk.StringVar(value="Ready")
        self.search_var = tk.StringVar()

        self.all_dialogs: list[dict[str, Any]] = []
        self.filtered_indices: list[int] = []

        self._build_layout()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_events)

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        params = ttk.LabelFrame(self, text="Parameters")
        params.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        for col in range(6):
            params.columnconfigure(col, weight=1)

        ttk.Label(params, text="API ID").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(params, textvariable=self.api_id_var).grid(row=0, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(params, text="API HASH").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(params, textvariable=self.api_hash_var).grid(row=0, column=3, sticky="ew", padx=4, pady=4)

        ttk.Label(params, text="Phone").grid(row=0, column=4, sticky="w", padx=4, pady=4)
        ttk.Entry(params, textvariable=self.phone_var).grid(row=0, column=5, sticky="ew", padx=4, pady=4)

        ttk.Label(params, text="Session").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(params, textvariable=self.session_var).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Checkbutton(params, text="Skip session file", variable=self.no_session_var).grid(row=1, column=2, sticky="w", padx=4, pady=4)

        ttk.Checkbutton(params, text="Anonymize names", variable=self.anonymize_var).grid(row=1, column=3, sticky="w", padx=4, pady=4)
        ttk.Checkbutton(params, text="Block dangerous files", variable=self.block_dangerous_var).grid(row=1, column=4, sticky="w", padx=4, pady=4)

        ttk.Label(params, text="Auto-refresh (s)").grid(row=1, column=5, sticky="w", padx=4, pady=4)
        ttk.Entry(params, textvariable=self.refresh_var, width=6).grid(row=1, column=5, sticky="e", padx=(4, 12), pady=4)

        ttk.Label(params, text="Batch size").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(params, textvariable=self.batch_var, width=6).grid(row=2, column=1, sticky="w", padx=4, pady=4)

        ttk.Button(params, text="Connect", command=self._on_connect).grid(row=2, column=3, sticky="ew", padx=4, pady=6)
        ttk.Button(params, text="Refresh dialogs", command=self._on_refresh).grid(row=2, column=4, sticky="ew", padx=4, pady=6)
        ttk.Button(params, text="Export", command=self._on_export).grid(row=2, column=5, sticky="ew", padx=4, pady=6)

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        ttk.Label(body, text="Search").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        search_entry = ttk.Entry(body, textvariable=self.search_var)
        search_entry.grid(row=0, column=0, sticky="ew", padx=4, pady=4)

        list_frame = ttk.Frame(body)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self.dialog_list = tk.Listbox(list_frame, exportselection=False)
        self.dialog_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.dialog_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.dialog_list.configure(yscrollcommand=scroll.set)

        log_frame = ttk.Frame(body)
        log_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0), pady=4)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        status = ttk.Label(self, textvariable=self.status_var, anchor="w")
        status.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

    def _on_connect(self) -> None:
        try:
            api_id = int(self.api_id_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Enter a valid API ID", parent=self)
            return
        api_hash = self.api_hash_var.get().strip()
        if not api_hash:
            messagebox.showerror("Error", "API HASH is required", parent=self)
            return
        phone = self.phone_var.get().strip()
        if not phone:
            messagebox.showerror("Error", "Phone number is required", parent=self)
            return
        session_name = None if self.no_session_var.get() else self.session_var.get().strip() or None
        self.status_var.set("Connecting...")
        self.worker.send_command(
            "connect",
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            session_name=session_name,
        )

    def _on_refresh(self) -> None:
        self.worker.send_command("refresh_dialogs")

    def _on_export(self) -> None:
        selection = self.dialog_list.curselection()
        if not selection:
            messagebox.showwarning("Attention", "Select a dialog to export", parent=self)
            return
        dialog_index = self.filtered_indices[selection[0]]
        try:
            refresh_value = self.refresh_var.get().strip()
            refresh_seconds = int(refresh_value) if refresh_value else None
        except ValueError:
            messagebox.showerror("Error", "Auto-refresh must be numeric", parent=self)
            return
        try:
            batch_value = int(self.batch_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Batch size must be numeric", parent=self)
            return
        self.worker.send_command(
            "export",
            dialog_index=dialog_index,
            anonymize=self.anonymize_var.get(),
            block_dangerous=self.block_dangerous_var.get(),
            refresh_seconds=refresh_seconds,
            progress_every=max(1, batch_value),
        )

    def _apply_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        self.dialog_list.delete(0, tk.END)
        self.filtered_indices.clear()
        for item in self.all_dialogs:
            text = f"[{item.get('kind', '?')}] {item.get('title', '')}".strip()
            if query and query not in text.lower():
                continue
            self.dialog_list.insert(tk.END, text)
            self.filtered_indices.append(item["index"])

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _process_events(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.after(100, self._process_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "log":
            msg = event.get("message", "")
            if msg:
                self._append_log(msg)
        elif etype == "error":
            msg = event.get("message", "Unknown error")
            self._append_log(f"[Error] {msg}")
            messagebox.showerror("Error", msg, parent=self)
            self.status_var.set("Error")
        elif etype == "dialogs":
            self.all_dialogs = event.get("items", [])
            self._apply_filter()
        elif etype == "progress":
            count = event.get("count", 0)
            self.status_var.set(f"Messages saved: {count}")
        elif etype == "export_done":
            html = event.get("html_path")
            self._append_log(f"Done. HTML: {html}")
            messagebox.showinfo("Export", f"Export complete\nHTML: {html}", parent=self)
        elif etype == "status":
            self.status_var.set(event.get("message", ""))
        elif etype == "input_request":
            self._handle_input_request(event)

    def _handle_input_request(self, event: dict[str, Any]) -> None:
        prompt = event.get("prompt") or "Enter a value"
        title = event.get("title") or "Input"
        secret = bool(event.get("secret"))
        fut = event.get("future")
        show = "*" if secret else None
        value = simpledialog.askstring(title, prompt, show=show, parent=self)
        if fut is not None:
            self.worker.resolve_future(fut, value)

    def _on_close(self) -> None:
        self.worker.send_command("stop")
        self.after(200, self.destroy)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

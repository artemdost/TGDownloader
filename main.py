# main.py
import os
import sys
import asyncio
import logging
from dotenv import load_dotenv

from telegram_api import authorize, list_user_dialogs
from channel_data import dump_dialog_to_json_and_media
from html_generator import generate_html

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")

LIVE_REFRESH_SECONDS = None     # вручную F5
BATCH_SAVE_EVERY = 50           # как часто обновлять index.html по ходу

def _yesno(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        ans = input(prompt + suffix + ": ").strip().lower()
        if not ans:
            return default
        if ans in ("y","yes","д","да"):
            return True
        if ans in ("n","no","н","нет"):
            return False
        print("Ответьте Y/N")

def _pick_dialog(dials):
    if not dials:
        raise SystemExit("Нет доступных диалогов (users/groups/channels).")
    print("\nДоступные диалоги:")
    for i, d in enumerate(dials, 1):
        kind = getattr(d, "_tgdl_kind", "?")
        title = getattr(d.entity, "title", None) or getattr(d.entity, "first_name", None) or "без названия"
        print(f"{i}. [{kind}] {title}")
    while True:
        raw = input("Выберите номер диалога: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(dials):
                return dials[idx]
        print("Неверный номер. Попробуйте снова.")

async def async_main():
    load_dotenv()

    # security toggles (интерактивно)
    use_anon = _yesno("Анонимизировать авторов сообщений (User1, User2...)?", default=True)
    block_danger = _yesno("Не скачивать потенциально опасные файлы (exe/js/vbs/...)?", default=True)

    client = await authorize()
    try:
        dialogs = await list_user_dialogs(client)
        chosen = _pick_dialog(dialogs)
        dialog_title = getattr(chosen.entity, "title", None) or getattr(chosen.entity, "first_name", None) or "Без названия"
        print(f"Выбран диалог: {dialog_title}")

        # промежуточное обновление HTML по ходу
        def on_progress(json_path, media_dir, count):
            generate_html(
                json_path=json_path,
                media_root=media_dir,
                channel_title=dialog_title,
                refresh_seconds=LIVE_REFRESH_SECONDS,
                total_count=count,
                anonymize=use_anon,   # <<<
                csp=True,             # <<<
            )
            log.info("HTML обновлён (промежуточно), сообщений: %s", count)

        json_path, media_dir = await dump_dialog_to_json_and_media(
            client, chosen,
            out_root="export",
            progress_every=BATCH_SAVE_EVERY,
            on_progress=on_progress,
            skip_dangerous=block_danger,   # <<<
        )

        html_path = generate_html(
            json_path=json_path,
            media_root=media_dir,
            channel_title=dialog_title,
            refresh_seconds=LIVE_REFRESH_SECONDS,
            anonymize=use_anon,  # <<<
            csp=True,            # <<<
        )

        print("\n=== Готово! ===")
        print(f"JSON: {json_path}")
        print(f"Медиа: {media_dir}")
        print(f"HTML: {html_path}")
        print("Открой index.html. Обновляй вручную (F5), авто-рефреш отключён.")

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except Exception as e:
        log.exception("Критическая ошибка: %s", e)
        print("\nНажмите Enter, чтобы выйти...")
        try:
            input()
        except Exception:
            pass
        sys.exit(1)

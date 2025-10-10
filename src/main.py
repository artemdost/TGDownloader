# main.py - SECURED VERSION
import os
import sys
import asyncio
import logging
from dotenv import load_dotenv
from .process_hardening import harden_process
from .telegram_api import authorize, list_user_dialogs
from .channel_data import dump_dialog_to_json_and_media
from .html_generator import generate_html

# Apply process-level hardening before bootstrapping the app
harden_process()

# Configure logging with security considerations

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("main")

# Disable debug logging for sensitive libraries
logging.getLogger("telethon").setLevel(logging.WARNING)

LIVE_REFRESH_SECONDS = None  # Manual F5 refresh
BATCH_SAVE_EVERY = 50  # How often to update index.html during export


def _yesno(prompt: str, default: bool = False) -> bool:
    """Safe yes/no input"""
    suffix = " [Y/n]" if default else " [y/N]"
    max_attempts = 3
    
    for attempt in range(max_attempts):
        try:
            ans = input(prompt + suffix + ": ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled by user")
            return False
        
        if not ans:
            return default
        if ans in ("y", "yes", "д", "да"):
            return True
        if ans in ("n", "no", "н", "нет"):
            return False
        
        if attempt < max_attempts - 1:
            print("Please answer Y/N")
        else:
            print(f"Too many invalid attempts. Using default: {'Yes' if default else 'No'}")
            return default
    
    return default


def _pick_dialog(dials):
    """Safe dialog selection"""
    if not dials:
        raise SystemExit("No available dialogs (users/groups/channels).")
    
    print("\nAvailable dialogs:")
    for i, d in enumerate(dials, 1):
        kind = getattr(d, "_tgdl_kind", "?")
        title = (
            getattr(d.entity, "title", None) 
            or getattr(d.entity, "first_name", None) 
            or "Untitled"
        )
        
        # Sanitize title for display
        safe_title = title[:80] if len(title) > 80 else title
        print(f"{i}. [{kind}] {safe_title}")
    
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            raw = input("Select dialog number: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled by user")
            raise SystemExit(0)
        
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(dials):
                return dials[idx]
        
        if attempt < max_attempts - 1:
            print(f"Invalid number. Please enter 1-{len(dials)}")
        else:
            raise SystemExit("Too many invalid attempts. Exiting.")
    
    raise SystemExit("Dialog selection failed.")


async def async_main():
    """Main async function with security enhancements"""
    try:
        load_dotenv()

        print("=" * 60)
        print("  TELEGRAM EXPORT STUDIO - SECURE VERSION")
        print("=" * 60)
        print()

        # Security toggles (interactive)
        print("Security settings:")
        use_anon = _yesno("  Anonymize sender names (User1, User2...)?", default=True)
        block_danger = _yesno("  Block potentially dangerous files (exe/js/vbs...)?", default=True)
        print()

        # Authorization
        print("Connecting to Telegram...")
        client = await authorize()
        
        try:
            # Get dialogs
            dialogs = await list_user_dialogs(client)
            chosen = _pick_dialog(dialogs)
            
            dialog_title = (
                getattr(chosen.entity, "title", None) 
                or getattr(chosen.entity, "first_name", None) 
                or "Untitled"
            )
            
            # Sanitize dialog title for logging
            safe_title = dialog_title[:80] if len(dialog_title) > 80 else dialog_title
            print(f"\nSelected dialog: {safe_title}")
            print()

            # Progress callback
            def on_progress(json_path, media_dir, count):
                try:
                    generate_html(
                        json_path=json_path,
                        media_root=media_dir,
                        channel_title=dialog_title,
                        refresh_seconds=LIVE_REFRESH_SECONDS,
                        total_count=count,
                        anonymize=use_anon,
                        csp=True,  # Always use CSP
                    )
                    log.info("HTML updated (intermediate), messages: %s", count)
                except Exception as e:
                    log.error("Failed to generate HTML during progress: %s", e)

            # Export dialog
            print("Starting export...")
            json_path, media_dir = await dump_dialog_to_json_and_media(
                client, 
                chosen,
                out_root="export",
                progress_every=BATCH_SAVE_EVERY,
                on_progress=on_progress,
                skip_dangerous=block_danger,
            )

            # Generate final HTML
            print("\nGenerating final HTML...")
            html_path = generate_html(
                json_path=json_path,
                media_root=media_dir,
                channel_title=dialog_title,
                refresh_seconds=LIVE_REFRESH_SECONDS,
                anonymize=use_anon,
                csp=True,  # Always use CSP
            )

            print("\n" + "=" * 60)
            print("  EXPORT COMPLETE!")
            print("=" * 60)
            print(f"JSON:  {json_path}")
            print(f"Media: {media_dir}")
            print(f"HTML:  {html_path}")
            print()
            print("Open index.html in your browser.")
            print("Auto-refresh is disabled, refresh manually (F5).")
            print("=" * 60)

        finally:
            # Always disconnect client
            try:
                await client.disconnect()
                log.info("Disconnected from Telegram")
            except Exception as e:
                log.warning("Error during disconnect: %s", e)
    
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user (Ctrl+C)")
        log.info("Export cancelled by user")
        sys.exit(0)
    
    except Exception as e:
        log.exception("Critical error: %s", e)
        print(f"\nError: {e}")
        print("\nPress Enter to exit...")
        try:
            input()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    try:
        # Check Python version
        if sys.version_info < (3, 10):
            print("Error: Python 3.10 or higher is required")
            sys.exit(1)
        
        asyncio.run(async_main())
    
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
    
    except Exception as e:
        log.exception("Fatal error: %s", e)
        print("\nPress Enter to exit...")
        try:
            input()
        except Exception:
            pass
        sys.exit(1)

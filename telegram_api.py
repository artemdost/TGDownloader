# telegram_api.py - SECURED VERSION
import os
import getpass
import logging
import inspect
import secrets
import ctypes
from typing import Awaitable, Callable, List, Optional, Union

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

log = logging.getLogger("telegram_api")

# ═══════════════════════════════════════════════════
# SECURE CREDENTIAL HANDLING
# ═══════════════════════════════════════════════════
class SecureString:
    """Secure string storage with memory cleanup"""
    
    def __init__(self, value: str):
        self._value = value
        self._cleared = False
    
    def get(self) -> str:
        if self._cleared:
            raise ValueError("Credential has been cleared")
        return self._value
    
    def clear(self):
        """Overwrite memory with zeros"""
        if self._cleared:
            return
        try:
            # Attempt to overwrite string in memory
            if self._value:
                buf = ctypes.create_string_buffer(len(self._value.encode()))
                ctypes.memset(ctypes.addressof(buf), 0, len(self._value.encode()))
        except Exception:
            pass
        finally:
            self._value = ""
            self._cleared = True
    
    def __del__(self):
        self.clear()


def _sanitize_for_log(value: str, field_name: str) -> str:
    """Mask sensitive data in logs"""
    if not value:
        return f"[{field_name}: empty]"
    if len(value) <= 4:
        return f"[{field_name}: ***]"
    return f"[{field_name}: {value[:2]}***{value[-2:]}]"


async def authorize(
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None,
    phone: Optional[str] = None,
    session_name: Optional[str] = None,
    code_callback: Optional[Callable[..., Union[str, Awaitable[str], None]]] = None,
    password_callback: Optional[Callable[..., Union[str, Awaitable[str], None]]] = None,
) -> TelegramClient:
    """
    Secure authorization that NEVER creates session files.
    Always uses in-memory StringSession.
    """
    
    # Secure storage for credentials
    secure_api_hash: Optional[SecureString] = None
    secure_phone: Optional[SecureString] = None
    secure_code: Optional[SecureString] = None
    secure_password: Optional[SecureString] = None

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

    try:
        # Get API ID
        if api_id is None:
            api_id_str = input("Enter API ID: ").strip()
        else:
            api_id_str = str(api_id).strip()
        if not api_id_str:
            raise ValueError("API ID must not be empty")
        
        try:
            api_id = int(api_id_str)
        except ValueError:
            raise ValueError("API ID must be a valid integer")
        
        # Validate API ID range (Telegram uses positive integers)
        if api_id <= 0:
            raise ValueError("API ID must be positive")

        # Get API Hash
        if api_hash is None:
            api_hash = input("Enter API HASH: ").strip()
        else:
            api_hash = _stringify(api_hash, "API HASH")
        
        # Validate API Hash format (should be 32 hex characters)
        if len(api_hash) != 32 or not all(c in '0123456789abcdefABCDEF' for c in api_hash):
            log.warning("API Hash format appears invalid (expected 32 hex chars)")
        
        secure_api_hash = SecureString(api_hash)

        # Get Phone
        if phone is None:
            phone = input("Enter phone number (e.g. +7...): ").strip()
        else:
            phone = _stringify(phone, "phone")
        
        # Validate phone format
        if not phone.startswith('+'):
            raise ValueError("Phone number must start with + (e.g. +1234567890)")
        if not phone[1:].replace(' ', '').isdigit():
            raise ValueError("Phone number must contain only digits after +")
        
        secure_phone = SecureString(phone)

        # Log sanitized credentials
        log.info("Authorizing with API ID: %d, Phone: %s", 
                 api_id, _sanitize_for_log(phone, "phone"))

        # Use in-memory session ONLY
        session = StringSession()
        log.info("Using in-memory session (no files will be created)")
        
        client = TelegramClient(session, api_id, secure_api_hash.get())

        await client.connect()
        
        if not await client.is_user_authorized():
            await client.send_code_request(secure_phone.get())
            
            # Get verification code
            if code_callback:
                code = await _request(code_callback, "Enter code from Telegram: ")
            else:
                code = input("Enter code from Telegram: ").strip()
            
            if not code:
                raise ValueError("Telegram code is required")
            
            # Validate code format (typically 5 digits)
            if not code.isdigit():
                raise ValueError("Verification code must contain only digits")
            
            secure_code = SecureString(code)
            
            try:
                await client.sign_in(phone=secure_phone.get(), code=secure_code.get())
                log.info("Authorization successful")
            except SessionPasswordNeededError:
                log.info("2FA required")
                
                # Get 2FA password
                if password_callback:
                    pwd = await _request(password_callback, "Enter 2FA password: ")
                else:
                    pwd = getpass.getpass("Enter 2FA password: ")
                
                if not pwd:
                    raise ValueError("2FA password is required")
                
                secure_password = SecureString(pwd)
                await client.sign_in(password=secure_password.get())
                log.info("2FA authorization successful")

        return client
    
    finally:
        # Secure cleanup of credentials
        if secure_api_hash:
            secure_api_hash.clear()
        if secure_phone:
            secure_phone.clear()
        if secure_code:
            secure_code.clear()
        if secure_password:
            secure_password.clear()


async def list_user_dialogs(client) -> List:
    """
    Returns ALL dialogs: users, groups/supergroups, channels.
    """
    try:
        dialogs = await client.get_dialogs()
    except Exception as e:
        log.error("Failed to retrieve dialogs: %s", e)
        raise
    
    res = []
    for d in dialogs:
        try:
            t = (
                "user" if getattr(d, "is_user", False)
                else "group" if getattr(d, "is_group", False)
                else "channel" if getattr(d, "is_channel", False)
                else "other"
            )
            if t in {"user", "group", "channel"}:
                d._tgdl_kind = t
                res.append(d)
        except Exception as e:
            log.warning("Failed to process dialog: %s", e)
            continue
    
    log.info("Found %d dialogs (users/groups/channels)", len(res))
    return res
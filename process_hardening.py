"""Utilities for hardening the process to reduce credential exposure."""
from __future__ import annotations

import ctypes
import logging
import os
import threading

log = logging.getLogger("process_hardening")

_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_WER_FAULT_REPORTING_FLAG_DISABLE = 0x0004

_lock = threading.Lock()
_hardened = False


def _set_error_mode() -> bool:
    """Prevent Windows from showing error dialogs that spawn WER."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False

    try:
        kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
        kernel32.SetErrorMode.restype = ctypes.c_uint
        current = kernel32.SetErrorMode(0)
        kernel32.SetErrorMode(current | _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX)
        return True
    except Exception as exc:  # Defensive: best-effort only
        log.debug("SetErrorMode failed: %s", exc)
        return False


def _disable_wer_reports() -> bool:
    """Disable Windows Error Reporting crash dumps for this process."""
    try:
        wer = ctypes.WinDLL("wer.dll", use_last_error=True)
    except OSError:
        return False

    try:
        WerSetFlags = wer.WerSetFlags
    except AttributeError:
        return False

    try:
        WerSetFlags.argtypes = [ctypes.c_uint]
        WerSetFlags.restype = ctypes.c_int
        hr = WerSetFlags(_WER_FAULT_REPORTING_FLAG_DISABLE)
        # WerSetFlags returns S_OK (0) on success, or E_ACCESSDENIED (0x80070005)
        if hr == 0 or hr == -2147024891:  # HRESULT for E_ACCESSDENIED
            return True
        log.debug("WerSetFlags returned %#x", hr & 0xFFFFFFFF)
        return False
    except Exception as exc:  # Defensive: best-effort only
        log.debug("WerSetFlags failed: %s", exc)
        return False


def harden_process() -> None:
    """Apply best-effort hardening against crash-dump leaks."""
    global _hardened
    if _hardened:
        return

    with _lock:
        if _hardened:
            return

        if os.name == "nt":
            error_mode_set = _set_error_mode()
            wer_disabled = _disable_wer_reports()
            log.debug(
                "Process hardening applied (error_mode=%s, wer_disabled=%s)",
                error_mode_set,
                wer_disabled,
            )
        else:
            log.debug("Process hardening not required on non-Windows platforms")

        _hardened = True

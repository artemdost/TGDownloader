#!/usr/bin/env python3
"""
Telegram Export Studio - Console Launcher
"""

import sys
import asyncio
from src.main import async_main

if __name__ == "__main__":
    try:
        if sys.version_info < (3, 10):
            print("Error: Python 3.10 or higher is required")
            sys.exit(1)

        asyncio.run(async_main())

    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)

    except Exception as e:
        print(f"\nFatal error: {e}")
        print("\nPress Enter to exit...")
        try:
            input()
        except Exception:
            pass
        sys.exit(1)

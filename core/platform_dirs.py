"""
Per-platform user directories — single source of truth for where SAVA writes
config/state and logs outside the install tree.

Stdlib-only (no project imports): `main.py` needs this before its own
sys.path setup runs, and `config/settings.py` builds its singleton at import
time, so this module must be safe to import first, unconditionally.
"""

import os
import sys
from pathlib import Path


def user_data_dir(app_name: str = "SAVA") -> Path:
    """Return the per-user directory SAVA should write config/state into.

    win32   -> %APPDATA%\\<app_name>          (unchanged — the shipping platform)
    darwin  -> ~/Library/Application Support/<app_name>
    other   -> ~/<app_name>                    (today's fallback, e.g. Linux/dev)
    """
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    return Path.home() / app_name


def user_log_dir(app_name: str = "SAVA") -> Path:
    """Return the per-user directory SAVA should write logs into.

    win32   -> same as user_data_dir() (logs sit alongside config there)
    darwin  -> ~/Library/Logs/<app_name>
    other   -> same as user_data_dir()
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / app_name
    return user_data_dir(app_name)

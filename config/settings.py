"""
Settings — JSON file for user preferences only.
ArtNet config now lives in a separate INI file (see core/artnet_config.py).
"""

import json
import os
import threading
import tempfile
import copy
from pathlib import Path

from core.platform_dirs import user_data_dir

BASE_DIR    = Path(__file__).resolve().parent.parent
ASSETS_DIR  = BASE_DIR / "assets"
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "sava_settings.json"

# User-writable config directory (works in installed builds)
USER_CONFIG_DIR = user_data_dir()


DEFAULT_SETTINGS = {
    "master_volume":    80,
    "shuffle":          False,
    "repeat":           "none",
    "always_on_top":    False,
    "last_playlist":    [],
    "track_volumes":    {},
    "cue_points":       {},
    "track_colors":     {},
    "track_end_actions": {},
    "window_x":         100,
    "window_y":         100,
    "theme":            "orange",
    "crossfade_layout": {},
    "toplo_unlocked":   False,  # dp-264: Easter egg, About-logo click-6x unlock
}


class Settings:

    def __init__(self):
        self._lock  = threading.RLock()
        self._data  = {}
        self._path  = self._resolve_path()
        self.load()

    def _resolve_path(self) -> Path:
        """Use APPDATA in production, project folder in dev."""
        # If running from PyInstaller bundle, use APPDATA
        import sys
        if getattr(sys, "frozen", False):
            USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            return USER_CONFIG_DIR / "sava_settings.json"
        return CONFIG_FILE

    @property
    def file_path(self) -> Path:
        return self._path

    def get(self, key: str, default=None):
        with self._lock:
            val = self._data.get(key, default)
            if isinstance(val, (dict, list)):
                return copy.deepcopy(val)
            return val

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value

    def save(self):
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                snapshot = copy.deepcopy(self._data)
            except Exception as e:
                print(f"[Settings] Snapshot error: {e}")
                return
        try:
            text = json.dumps(snapshot, indent=2)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".sava_settings_", suffix=".tmp",
                dir=str(self._path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, str(self._path))
            except Exception:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            print(f"[Settings] Save error: {e}")

    def load(self):
        with self._lock:
            if self._path.exists():
                try:
                    with open(self._path, "r", encoding="utf-8") as fh:
                        saved = json.load(fh)
                    self._data = _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), saved)
                    return
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[Settings] Load error, using defaults: {e}")
            self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self.save()


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


settings = Settings()
"""
ArtNet config — plain INI file.
Network section supports universe, subnet and port.
enabled accepts + (true) or - (false).
"""

import configparser
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

from core.platform_dirs import user_data_dir


def _config_path() -> Path:
    if getattr(sys, "frozen", False):
        appdata = user_data_dir()
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata / "artnet_config.ini"
    return Path(__file__).resolve().parent.parent / "config" / "artnet_config.ini"


CONFIG_PATH = _config_path()


DEFAULT_INI = """\
# SAVA ArtNet / DMX configuration
# ----------------------------------------------------------------------
# Each section is one function. Edit channel numbers to match your console.
#
#   enabled = +     this function/listener is ON
#   enabled = -     this function/listener is OFF
#
# Threshold is the DMX value (0-255) at which trigger functions fire.
# After editing, save the file and use ArtNet -> Reload config in SAVA.

[network]
# Port the listener binds to. 6454 is the ArtNet standard.
port     = 6454
# ArtNet subnet (0-15). Most consoles call this just "subnet".
subnet   = 0
# ArtNet universe within the subnet (0-15).
universe = 0
# Master enable for incoming ArtNet
enabled  = +

# ── Transport ────────────────────────────────────────────────────────
[play]
enabled   = +
channel   = 1
threshold = 128

[pause]
enabled   = +
channel   = 2
threshold = 128

[stop]
enabled   = +
channel   = 3
threshold = 128

[next_track]
enabled   = +
channel   = 4
threshold = 128

[prev_track]
enabled   = +
channel   = 5
threshold = 128

# ── Continuous controls ─────────────────────────────────────────────
# These ignore threshold; they map DMX 0-255 to their full range.
# Disabled by default (v3.0 release default) -- most rigs drive volume,
# seek, and track select from the console's own controls, not DMX; leaving
# these off out of the box avoids a fader left at a nonzero value silently
# fighting the operator the first time SAVA is connected.

[master_volume]
enabled = -
channel = 6

[seek]
enabled = -
channel = 7

# ── Track select ────────────────────────────────────────────────────
# Two channels: an enable toggle, and the track number.
# DMX value on track_select is mapped 0-255 -> 0-100 (matches consoles
# that display percent but output decimal).
# track_select only fires when track_select_enable >= threshold.

[track_select_enable]
enabled   = -
channel   = 8
threshold = 128

[track_select]
enabled = +
channel = 9

# ── Loop A to B ─────────────────────────────────────────────────────
[loop_ab]
enabled   = +
channel   = 10
threshold = 128

# ── Cue points 1-8 ──────────────────────────────────────────────────
[cue_1]
enabled   = +
channel   = 11
threshold = 128

[cue_2]
enabled   = +
channel   = 12
threshold = 128

[cue_3]
enabled   = +
channel   = 13
threshold = 128

[cue_4]
enabled   = +
channel   = 14
threshold = 128

[cue_5]
enabled   = +
channel   = 15
threshold = 128

[cue_6]
enabled   = +
channel   = 16
threshold = 128

[cue_7]
enabled   = +
channel   = 17
threshold = 128

[cue_8]
enabled   = +
channel   = 18
threshold = 128
"""

FUNCTION_NAMES = [
    "play", "pause", "stop", "next_track", "prev_track",
    "master_volume", "seek",
    "track_select_enable", "track_select",
    "loop_ab",
    "cue_1", "cue_2", "cue_3", "cue_4",
    "cue_5", "cue_6", "cue_7", "cue_8",
]

# Human-readable labels for the map key window
FUNCTION_LABELS = {
    "play":                "Play",
    "pause":               "Pause",
    "stop":                "Stop",
    "next_track":          "Next track",
    "prev_track":          "Previous track",
    "master_volume":       "Master volume",
    "seek":                "Seek position",
    "track_select_enable": "Track select: enable gate",
    "track_select":        "Track select: track number",
    "loop_ab":             "Loop A-B toggle",
    "cue_1":               "Cue 1 jump",
    "cue_2":               "Cue 2 jump",
    "cue_3":               "Cue 3 jump",
    "cue_4":               "Cue 4 jump",
    "cue_5":               "Cue 5 jump",
    "cue_6":               "Cue 6 jump",
    "cue_7":               "Cue 7 jump",
    "cue_8":               "Cue 8 jump",
}

# Functions that use threshold (trigger functions, not continuous ones)
TRIGGER_FUNCTIONS = {
    "play", "pause", "stop", "next_track", "prev_track",
    "track_select_enable", "loop_ab",
    "cue_1", "cue_2", "cue_3", "cue_4",
    "cue_5", "cue_6", "cue_7", "cue_8",
}


def _set_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Replace one `key = value` line inside `[section]`, preserving comments
    and formatting elsewhere in the file. Appends the section/key if either
    is missing (e.g. the user hand-edited the file and dropped a section)."""
    lines = text.splitlines(keepends=True)
    out = []
    section_pattern = re.compile(r"^\s*\[(.+?)\]\s*$")
    key_pattern = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(.*)$", re.IGNORECASE)

    in_section = False
    section_found = False
    replaced = False

    for line in lines:
        sm = section_pattern.match(line)
        if sm:
            in_section = sm.group(1).strip().lower() == section.lower()
            if in_section:
                section_found = True
            out.append(line)
            continue
        if in_section and not replaced:
            km = key_pattern.match(line)
            if km:
                val_part = km.group(3)
                comment = ""
                for prefix in ("#", ";"):
                    idx = val_part.find(prefix)
                    if idx != -1:
                        comment = val_part[idx:]
                        break
                new_line = f"{km.group(1)}{key}{km.group(2)}{value}"
                if comment:
                    new_line += f"  {comment}"
                out.append(new_line + ("\n" if line.endswith("\n") else ""))
                replaced = True
                continue
        out.append(line)

    if not replaced:
        if section_found:
            for i, line in enumerate(out):
                sm = section_pattern.match(line)
                if sm and sm.group(1).strip().lower() == section.lower():
                    out.insert(i + 1, f"{key} = {value}\n")
                    replaced = True
                    break
        if not replaced:
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(f"\n[{section}]\n{key} = {value}\n")

    return "".join(out)


def _atomic_write(path: Path, text: str):
    """Write `text` to `path` via a temp file + os.replace, the same
    durability pattern config/settings.py uses for the JSON prefs.

    A plain `write_text` truncates the real file and then writes into it, so
    the config is briefly EMPTY or half-written on disk. Two things read it in
    that window: MainWindow's `_check_artnet_config_changed` polls this file's
    mtime every 100ms and reloads on any change, and a crash or power loss
    mid-write leaves the user's whole DMX mapping truncated with no backup.
    os.replace is atomic on both Windows and POSIX, so a reader sees either
    the entire old file or the entire new one, never a partial mapping.
    """
    fd, tmp_path = tempfile.mkstemp(
        prefix=".artnet_config_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def _parse_enabled(raw_value: str, fallback: bool = True) -> bool:
    """Accept '+' / '-' as primary, also true/false/yes/no for compatibility."""
    if raw_value is None:
        return fallback
    v = str(raw_value).strip().lower()
    if v in ("+", "true",  "yes", "on",  "1"):
        return True
    if v in ("-", "false", "no",  "off", "0"):
        return False
    return fallback


class ArtNetConfig:

    def __init__(self):
        self._lock = threading.RLock()
        self._map  = {}

        # Network settings
        self._port           = 6454
        self._universe       = 0
        self._subnet         = 0
        self._listen_enabled = True

        self.ensure_file_exists()
        self.reload()

    def ensure_file_exists(self):
        if not CONFIG_PATH.exists():
            try:
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                CONFIG_PATH.write_text(DEFAULT_INI, encoding="utf-8")
                print(f"[ArtNetConfig] Created default config at {CONFIG_PATH}")
            except Exception as e:
                print(f"[ArtNetConfig] Could not create config: {e}")

    def reload(self) -> bool:
        with self._lock:
            parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
            try:
                parser.read(CONFIG_PATH, encoding="utf-8")
            except Exception as e:
                print(f"[ArtNetConfig] Read error: {e}")
                return False

            # ── Network section ──
            if parser.has_section("network"):
                try:
                    self._port = parser.getint("network", "port", fallback=6454)
                    self._port = max(1, min(self._port, 65535))
                except Exception:
                    self._port = 6454
                try:
                    self._subnet = parser.getint("network", "subnet", fallback=0)
                    self._subnet = max(0, min(self._subnet, 15))
                except Exception:
                    self._subnet = 0
                try:
                    self._universe = parser.getint("network", "universe", fallback=0)
                    self._universe = max(0, min(self._universe, 15))
                except Exception:
                    self._universe = 0
                self._listen_enabled = _parse_enabled(
                    parser.get("network", "enabled", fallback="+"), fallback=True
                )
            else:
                self._port = 6454
                self._subnet = 0
                self._universe = 0
                self._listen_enabled = True

            # ── Function sections ──
            new_map = {}
            for fn in FUNCTION_NAMES:
                if not parser.has_section(fn):
                    continue
                try:
                    enabled = _parse_enabled(
                        parser.get(fn, "enabled", fallback="+"), fallback=True
                    )
                    channel   = parser.getint(fn, "channel",   fallback=1)
                    threshold = parser.getint(fn, "threshold", fallback=128)
                    new_map[fn] = {
                        "enabled":   enabled,
                        "channel":   max(1, min(channel,   512)),
                        "threshold": max(0, min(threshold, 255)),
                    }
                except Exception as e:
                    print(f"[ArtNetConfig] Parse error for [{fn}]: {e}")
                    continue

            self._map = new_map
            print(
                f"[ArtNetConfig] Loaded {len(new_map)} mappings, "
                f"port={self._port}, subnet={self._subnet}, universe={self._universe}"
            )
            return True

    @property
    def file_path(self) -> Path:
        return CONFIG_PATH

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    @property
    def universe(self) -> int:
        with self._lock:
            return self._universe

    @property
    def subnet(self) -> int:
        with self._lock:
            return self._subnet

    @property
    def full_universe(self) -> int:
        """The 15-bit ArtNet 'Port-Address': (subnet << 4) | universe."""
        with self._lock:
            return ((self._subnet & 0x0F) << 4) | (self._universe & 0x0F)

    @property
    def listen_enabled(self) -> bool:
        with self._lock:
            return self._listen_enabled

    def save_network(self, port=None, subnet=None, universe=None, enabled=None):
        """Update the [network] section in the INI file and reload."""
        with self._lock:
            text = CONFIG_PATH.read_text(encoding="utf-8")
            if port is not None:
                text = _set_ini_value(text, "network", "port", str(port))
            if subnet is not None:
                text = _set_ini_value(text, "network", "subnet", str(subnet))
            if universe is not None:
                text = _set_ini_value(text, "network", "universe", str(universe))
            if enabled is not None:
                text = _set_ini_value(
                    text, "network", "enabled", "+" if enabled else "-"
                )
            _atomic_write(CONFIG_PATH, text)
            self.reload()

    def save_mapping(self, function_name, enabled=None, channel=None, threshold=None):
        """Update one function's section in the INI file and reload."""
        if function_name not in FUNCTION_NAMES:
            return
        with self._lock:
            text = CONFIG_PATH.read_text(encoding="utf-8")
            if enabled is not None:
                text = _set_ini_value(
                    text, function_name, "enabled", "+" if enabled else "-"
                )
            if channel is not None:
                text = _set_ini_value(text, function_name, "channel", str(channel))
            if threshold is not None:
                text = _set_ini_value(
                    text, function_name, "threshold", str(threshold)
                )
            _atomic_write(CONFIG_PATH, text)
            self.reload()

    def get_mapping(self, function_name: str) -> dict:
        with self._lock:
            return dict(self._map.get(function_name, {}))

    def all_mappings(self) -> dict:
        with self._lock:
            return {fn: dict(m) for fn, m in self._map.items()}


artnet_config = ArtNetConfig()

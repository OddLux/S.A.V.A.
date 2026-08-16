"""
SAVA — Synchronizing Audio Via Art-net
Entry point with crash-safe logging for packaged builds.
"""

import sys
import os
import traceback
import faulthandler
from pathlib import Path
from datetime import datetime

from core.platform_dirs import user_log_dir


# ── Determine if we're frozen (packaged) or running from source ──────────────
IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    # Running from PyInstaller bundle
    APP_ROOT = Path(sys.executable).parent
else:
    APP_ROOT = Path(__file__).resolve().parent

# ── Set up a log file in a writable user location ─────────────────────────────
def _get_log_dir() -> Path:
    """Return a writable directory for the log file."""
    if IS_FROZEN and sys.platform in ("win32", "darwin"):
        # On installed builds, write to the platform's per-user log location
        # (%APPDATA%\SAVA on Windows, ~/Library/Logs/SAVA on macOS).
        d = user_log_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass
    # Fallback: alongside the app
    d = APP_ROOT / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path.home()
    return d

LOG_DIR  = _get_log_dir()
LOG_FILE = LOG_DIR / "sava.log"

# Every print() in SAVA is a diagnostic that lands in this file, and the file
# is opened in APPEND mode -- nothing ever truncated it. A long-running show
# machine accumulates decode/ArtNet log lines into a single file that only
# grows, and each write is fsync'd (see _LogStream.write), so a
# multi-hundred-MB log both fills the disk and makes every print progressively
# more expensive. Rotate once at startup: one previous run is kept as
# sava.log.1 for post-mortem, anything older is discarded.
_LOG_MAX_BYTES = 5 * 1024 * 1024


def _rotate_log(path: Path):
    """Roll `path` to `path.1` when it has grown past _LOG_MAX_BYTES. Best
    effort -- a failure here must never stop the app starting, since the only
    consequence of not rotating is a larger log."""
    try:
        if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
            backup = path.with_name(path.name + ".1")
            if backup.exists():
                backup.unlink()
            path.replace(backup)
    except Exception:
        pass


_rotate_log(LOG_FILE)


# ── Redirect stdout / stderr to the log file ──────────────────────────────────
# In a --windowed build, stdout/stderr point to nothing. Any print() call
# eventually crashes the app once the OS buffer fills. We replace them with
# a real file so prints become harmless and crashes get captured.
class _LogStream:
    def __init__(self, path):
        self._path = path
        self._fh   = None
        try:
            # buffering=0 not allowed in text mode, but we flush after every write
            self._fh = open(path, "a", encoding="utf-8", buffering=1)
            self._fh.write(f"\n=== SAVA started {datetime.now().isoformat()} ===\n")
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except Exception:
                pass
        except Exception:
            self._fh = None

    def write(self, msg):
        if self._fh:
            try:
                self._fh.write(msg)
                self._fh.flush()
                # fsync after every write so the log survives a hard crash
                try:
                    os.fsync(self._fh.fileno())
                except Exception:
                    pass
            except Exception:
                pass

    def flush(self):
        if self._fh:
            try:
                self._fh.flush()
            except Exception:
                pass

    def isatty(self):
        return False


# Always redirect — works in both dev and frozen modes
sys.stdout = _LogStream(LOG_FILE)
sys.stderr = _LogStream(LOG_FILE)
# Enable native crash dumps to the log file
try:
    _fh_log = open(LOG_FILE, "a", encoding="utf-8")
    faulthandler.enable(file=_fh_log, all_threads=True)
except Exception:
    pass

# ── Make sure project root is on the path ────────────────────────────────────
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

def _find_binary(name: str) -> Path:
    """Locate an executable bundled with the app (ffmpeg, ffprobe, etc.)."""
    candidates = [
        APP_ROOT / "assets" / name,
        APP_ROOT / name,
        APP_ROOT / "_internal" / "assets" / name,
    ]
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "assets" / name)
    for c in candidates:
        if c.exists():
            return c
    return None

try:
    from pydub import AudioSegment
    from pydub import utils as pydub_utils

    # dp-244: same platform-aware suffix rule core/deck_engine.py applies --
    # "ffmpeg.exe" on Windows (unchanged), bare "ffmpeg" elsewhere.
    _exe = ".exe" if os.name == "nt" else ""
    _ffmpeg  = _find_binary(f"ffmpeg{_exe}")
    _ffprobe = _find_binary(f"ffprobe{_exe}")

    if _ffmpeg:
        AudioSegment.converter = str(_ffmpeg)
        print(f"[SAVA] ffmpeg found at:  {_ffmpeg}")
    else:
        print(f"[SAVA] WARNING: ffmpeg.exe not found")

    if _ffprobe:
        # pydub uses ffprobe to read metadata and duration
        AudioSegment.ffprobe = str(_ffprobe)
        pydub_utils.get_prober_name = lambda: str(_ffprobe)
        # Also set on os.environ so subprocess calls find it
        os.environ["PATH"] = str(_ffprobe.parent) + os.pathsep + os.environ.get("PATH", "")
        print(f"[SAVA] ffprobe found at: {_ffprobe}")
    else:
        print(f"[SAVA] WARNING: ffprobe.exe not found — track loading may fail")
except Exception as e:
    print(f"[SAVA] Audio tool setup error: {e}")


# ── Crash handler ────────────────────────────────────────────────────────────
def _excepthook(exc_type, exc_value, exc_traceback):
    """Log any uncaught exception so the app doesn't die silently."""
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print("=== UNCAUGHT EXCEPTION ===")
    print(msg)
    print("=== END EXCEPTION ===")
    try:
        sys.stderr.flush()
    except Exception:
        pass

sys.excepthook = _excepthook


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        from PyQt6.QtGui     import QIcon
        from ui.skin         import apply_skin
        from ui.main_window  import MainWindow

        app = QApplication(sys.argv)
        app.setApplicationName("SAVA")
        app.setApplicationDisplayName("Synchronizing Audio Via Art-net")
        # dp-227: the VERSION file is the single source of truth. This was
        # hardcoded "1.0.0", so Qt reported a version that had no relationship
        # to the one in the About dialog or the installer filename.
        from core.version import __version__ as _sava_version
        app.setApplicationVersion(_sava_version)
        app.setOrganizationName("SAVA")

        # App-wide icon
        icon_path = APP_ROOT / "assets" / "sava.ico"
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))

        apply_skin(app)

        # Create the ArtNet bridge AFTER QApplication exists
        # (it's a QObject and needs a Qt event loop)
        from core import artnet_bridge as ab_module
        ab_module.artnet_bridge = ab_module.ArtNetBridge()

        window = MainWindow()
        window.show()

        sys.exit(app.exec())

    except Exception as e:
        # Last-resort error handler — show a message box if Qt is available,
        # otherwise just log and exit
        msg = traceback.format_exc()
        print(f"[SAVA] Fatal startup error:\n{msg}")
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            _app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(
                None, "SAVA — Fatal Error",
                f"A fatal error occurred:\n\n{e}\n\n"
                f"Full details have been written to:\n{LOG_FILE}"
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
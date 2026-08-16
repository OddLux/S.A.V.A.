"""dp-227: SAVA's single source of version truth.

The version lives in the plain-text `VERSION` file at the repo root, NOT in
this module, because two different toolchains have to read it:

- Python (this module, for the About dialog)
- Inno Setup's preprocessor (`installer.iss`, for `AppVersion`)

A plain one-line file is the only format both can read without a build step
or a code generator, so there is exactly one place to edit when cutting a
release and no possibility of the installer and the app disagreeing.

SAVA has no `pyproject.toml` and no packaging metadata -- it is a PyInstaller
app, not a distribution -- so the usual `importlib.metadata` route the dev
protocol assumes is not available here. See the release section of CLAUDE.md.
"""

import sys
from pathlib import Path

# Only reached if VERSION is missing or unreadable, which in a frozen build
# means it was not bundled. Deliberately an obviously-wrong sentinel rather
# than a plausible number: a wrong version reported confidently is worse than
# one that is visibly broken.
_FALLBACK = "0.0.0-unknown"


def _version_file_candidates():
    """VERSION's possible locations, frozen build first.

    PyInstaller unpacks bundled data under `sys._MEIPASS`; from source the
    file sits next to this package. `SAVA.spec` must keep `VERSION` in its
    `datas` or the frozen build falls back to the sentinel."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass) / "VERSION"
    yield Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    """The version string, or `_FALLBACK` if VERSION cannot be read.

    Never raises: this is called while building the About dialog, and a
    missing data file must not take the window down."""
    for candidate in _version_file_candidates():
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return _FALLBACK


__version__ = get_version()

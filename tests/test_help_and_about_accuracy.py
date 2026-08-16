"""dp-250: the in-app Help and About text must describe the app that exists.

Both had drifted badly. Help still documented a playlist context-menu item
removed by dp-232 ("Set Cue 1-4 here"), a crossfade Preview transport that no
longer exists, and a "Presets tab" that never existed at all -- while saying
nothing about show files, audio-device selection, the Start marker, or the
crossfade scrub slider. About credited a library the app does not use and
attributed authorship to initials.

Docs rot silently because nothing fails when they do. These tests are the
thing that fails. They are deliberately written against the CODE (menu labels
pulled from the widget, the real import graph) rather than against a second
copy of the expected text -- a test that just compares the help to a frozen
string would pass forever while the app changed underneath it.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from ui.main_window import _HELP_HTML


# -- Help: no stale content -------------------------------------------------


@pytest.mark.parametrize(
    "phrase, removed_by",
    [
        ("Set Cue 1-4", "dp-232 removed the playlist context-menu cue items"),
        ("Stop Preview", "the crossfade dialog's preview transport was removed"),
        ("Presets tab", "never existed in any version"),
        ("pygame", "dp-216 replaced the playback engine"),
    ],
)
def test_help_does_not_document_things_that_do_not_exist(phrase, removed_by):
    assert phrase not in _HELP_HTML, f"stale help text: {phrase!r} -- {removed_by}"


def test_help_headings_follow_the_active_theme():
    """Headings were hardcoded green (#00cc00), which clashed with every
    theme except one. They must use the live accent colour."""
    from ui.skin import C_ACCENT

    assert "#00cc00" not in _HELP_HTML
    assert C_ACCENT in _HELP_HTML


def test_help_has_no_broken_escape_artifacts():
    """_HELP_HTML is an f-string, so a single backslash in a Windows path is
    an ESCAPE: `\\a` in `_internal\\assets` silently became the BEL control
    character (0x07). Any C0 control character in the rendered help means a
    path got mangled."""
    controls = [c for c in _HELP_HTML if ord(c) < 32 and c not in "\n\r\t"]
    assert controls == [], f"control characters in help text: {controls!r}"
    assert r"_internal\assets" in _HELP_HTML
    assert r"%APPDATA%\SAVA\sava.log" in _HELP_HTML


# -- Help: current features are covered -------------------------------------


@pytest.mark.parametrize(
    "feature, phrase",
    [
        ("show files (dp-246)", "Export Show"),
        ("show files (dp-246)", ".savashow"),
        ("audio device selection (dp-223)", "Audio Output"),
        ("start marker (dp-232)", "Playback begins here"),
        ("fin marker (dp-199)", "Playback ends here"),
        ("learn is a toggle (dp-249)", "click <b>Cancel</b> to stop"),
        ("scrub commits on release (dp-247)", "jumps when you release"),
        ("timecode modes (dp-213)", "elapsed / remaining / both"),
        ("preview waveform (dp-218)", "queued next"),
        ("crossfade scrub slider (dp-219)", "retime it live"),
        ("theme selection", "Theme"),
    ],
)
def test_help_documents_current_features(feature, phrase):
    assert phrase in _HELP_HTML, f"help does not cover {feature}"


def test_help_lists_every_supported_audio_format():
    """The format list in Help must match SUPPORTED_EXTENSIONS, not a
    hand-maintained second copy of it."""
    from core.playlist import SUPPORTED_EXTENSIONS

    formats_section = _HELP_HTML[_HELP_HTML.index("<b>Formats:</b>"):][:300].upper()
    # .aif is covered by the AIFF entry; every other extension must appear.
    for ext in SUPPORTED_EXTENSIONS:
        name = ext.lstrip(".").upper()
        if name == "AIF":
            continue
        assert name in formats_section, f"Help omits supported format {ext}"


def test_help_documents_every_mappable_artnet_function():
    """Every function a user can map in the DMX dialog needs an explanation,
    or they are left guessing what a channel does."""
    from core.artnet_config import FUNCTION_NAMES

    text = _HELP_HTML.lower()
    # The eight cue functions are documented as one "Cue 1-8" row.
    required = {
        fn for fn in FUNCTION_NAMES if not re.fullmatch(r"cue_[1-8]", fn)
    }
    human = {
        "play": "play", "pause": "pause", "stop": "stop",
        "next_track": "next", "prev_track": "prev",
        "master_volume": "master volume", "seek": "seek",
        "track_select_enable": "track select enable",
        "track_select": "track select",
        "loop_ab": "loop a to b",
    }
    assert set(human) == required, "FUNCTION_NAMES changed -- update this map"
    for fn, phrase in human.items():
        assert phrase in text, f"Help does not document ArtNet function {fn}"
    assert "cue 1-8" in text


# -- About ------------------------------------------------------------------


def _about_text():
    """The text the About dialog actually DISPLAYS, read out of the source
    rather than by opening the modal dialog (which would block the run).

    Comment lines are stripped. Without that, this function's own explanatory
    comment -- which names the libraries that were wrongly credited -- counted
    as a credit and failed the very test it explains. The dialog shows string
    literals, so only those are the subject here.
    """
    source = Path(__file__).resolve().parent.parent / "ui" / "main_window.py"
    body = source.read_text(encoding="utf-8")
    start = body.index("def _on_about")
    block = body[start:body.index("def _on_help_instructions", start)]
    return "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )


def test_about_credits_the_developer_and_company():
    about = _about_text()
    assert "Massimo - Sava Kisiov" in about
    assert "OddLux" in about
    assert "MSK" not in about, "the old initials-only credit is still there"


@pytest.mark.parametrize("library", ["PyQt6", "sounddevice", "numpy", "mutagen", "ffmpeg"])
def test_about_credits_each_library_actually_used(library):
    assert library in _about_text()


@pytest.mark.parametrize("library", ["pygame", "pydub"])
def test_about_does_not_credit_unused_libraries(library):
    """pygame is gone entirely; pydub survives only as a never-executed
    fallback inside core/analyzer.py. Neither belongs in 'Built with'."""
    assert library not in _about_text()


def test_credited_libraries_are_genuinely_imported():
    """The real check: every credited PYTHON library must actually appear in
    the import graph. Asserting against the source, not against memory, is
    what stops this drifting again the next time the engine is replaced.
    (ffmpeg is excluded -- it is a bundled binary, not an import.)"""
    root = Path(__file__).resolve().parent.parent
    sources = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for d in ("core", "ui", "config")
        for p in (root / d).glob("*.py")
    ) + (root / "main.py").read_text(encoding="utf-8", errors="replace")

    for library in ("PyQt6", "sounddevice", "numpy", "mutagen"):
        assert re.search(rf"^\s*(import|from)\s+{library}\b", sources, re.M), (
            f"About credits {library} but nothing imports it"
        )

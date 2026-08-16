"""dp-253: "Clear markers" on a playlist row must invalidate the IDLE deck's
cached start/end marker too, not just the active deck's -- otherwise the
queued-next track still starts from the old Start marker when it becomes
active, even though settings and the preview widget show it cleared.

Discriminating assertion (per the ticket): the idle DECK's actual
`read_idx`/`start_marker`/`end_marker` after the clear -- not the settings
dict, not the widget's drawn markers. Those two already passed before the
fix; only the deck-level assertion tells old and new code apart.

Uses the real `core.engine.engine` singleton (a headless DeckEngine, no
sounddevice stream) and a real rendered tone file so `preload()` actually
arms with a nonzero frontier (W4's guard).

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m pytest tests -q
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from config.settings import settings
from core.engine import engine
from core.playlist import playlist
from ui.main_window import MainWindow

_ROOT = Path(__file__).resolve().parent.parent
_FFMPEG_BIN = str(_ROOT / "assets" / "ffmpeg.exe")

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _make_tone_file(directory, seconds, name):
    path = os.path.join(directory, name)
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}",
            path,
        ],
        check=True, timeout=20,
    )
    return path


def _wait_for(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return False


def _meta(filepath, duration):
    return {
        "title": Path(filepath).stem,
        "artist": "Unknown",
        "album": "Unknown",
        "duration": duration,
        "filepath": filepath,
        "color": None,
        "end_action": "next",
    }


class TestClearMarkersInvalidatesIdleDeck(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="dp253_test_")
        self.track_a = _make_tone_file(self._tmpdir.name, 0.3, "a.wav")
        self.track_b = _make_tone_file(self._tmpdir.name, 0.3, "b.wav")

        self._saved_tracks = list(playlist._tracks)
        playlist._tracks = [
            _meta(self.track_a, 0.3),
            _meta(self.track_b, 0.3),
        ]
        playlist._current_index = 0

        settings.set("track_start_markers", {self.track_b: 0.2})
        settings.set("track_end_markers", {})

        self.window = MainWindow()

        # Load track A active, then arm the idle deck via the same path the
        # app uses on every track (re)start -- picks up B as the successor
        # and preloads it with the stale (about-to-be-cleared) start marker.
        engine.load(self.track_a, self.track_a)
        self.window._rearm_preload()
        armed = _wait_for(lambda: engine.preloaded_file == self.track_b)
        self.assertTrue(armed, "idle deck never armed for track_b")
        # Confirm the stale marker actually landed on the idle deck before
        # clearing -- otherwise this test wouldn't be exercising anything.
        self.assertGreater(engine._idle.read_idx, 0)
        self.assertEqual(engine._idle.start_marker, 0.2)

    def tearDown(self):
        settings.set("track_start_markers", {})
        settings.set("track_end_markers", {})
        playlist._tracks = self._saved_tracks
        self.window.close()
        self._tmpdir.cleanup()

    def test_clearing_idle_rows_markers_resets_the_idle_decks_read_idx(self):
        # Row 1 == track_b == the currently-preloaded idle deck's track.
        self.window._on_clear_track_markers(1)

        rebuilt = _wait_for(
            lambda: engine.preloaded_file == self.track_b
            and engine._idle.start_marker is None
        )
        self.assertTrue(rebuilt, "idle deck never rebuilt after marker clear")
        # The discriminating assertion: the DECK's actual playback start
        # position, not settings or the widget.
        self.assertEqual(engine._idle.read_idx, 0)
        self.assertIsNone(engine._idle.end_marker)

    def test_clearing_the_active_rows_markers_is_unaffected(self):
        """Regression guard: the pre-existing is_current branch still works,
        and clearing the ACTIVE row must not disturb the idle deck at all."""
        settings.set(
            "track_start_markers", {self.track_a: 0.1, self.track_b: 0.2}
        )
        engine._active.start_marker = 0.1  # mirror what load() would have cached

        self.window._on_clear_track_markers(0)  # row 0 == track_a == active

        self.assertIsNone(engine._active.start_marker)
        # Idle deck (track_b) must be untouched by clearing the active row.
        self.assertEqual(engine._idle.start_marker, 0.2)
        self.assertGreater(engine._idle.read_idx, 0)

    def test_clearing_an_unrelated_row_does_not_reload_the_idle_deck(self):
        """Requirement 3: a row that is neither active nor idle must not
        force a needless idle-deck re-decode. Adds a third, unrelated track
        and clears ITS markers -- the idle deck (still track_b) must be the
        exact same Deck object, not retired/reloaded."""
        track_c = _make_tone_file(self._tmpdir.name, 0.3, "c.wav")
        settings.set("track_start_markers", {self.track_b: 0.2, track_c: 0.1})
        tracks = list(playlist._tracks)
        tracks.append(_meta(track_c, 0.3))
        playlist._tracks = tracks

        idle_deck_before = engine._idle
        gen_before = engine._preload_gen

        self.window._on_clear_track_markers(2)  # row 2 == track_c, unrelated

        # No invalidate/re-preload should have been triggered: same Deck
        # object, same generation, idle deck's own marker cache untouched.
        self.assertIs(engine._idle, idle_deck_before)
        self.assertEqual(engine._preload_gen, gen_before)
        self.assertEqual(engine._idle.start_marker, 0.2)
        self.assertGreater(engine._idle.read_idx, 0)
        # The unrelated row's own setting was still cleared, though.
        self.assertNotIn(track_c, settings.get("track_start_markers", {}))


if __name__ == "__main__":
    unittest.main()

"""
Regression tests for MainWindow's dp-216 Phase 5 deck-model wiring --
specifically how `_on_engine_track_changed` advances the playlist after a
DeckEngine deck swap.

The engine swaps decks autonomously inside its audio callback (natural
gapless advance / crossfade finalize) AND on an explicit
`swap_to_preloaded()` (manual Next). Both routes fire the same
`on_track_changed(filepath)`, but the playlist has ALREADY been advanced in
the manual case and NOT in the natural one -- so the handler needs an
unambiguous way to tell them apart. `_pending_manual_swap` is that signal.

No pytest dependency in this project's venv -- plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from core.playlist import playlist
from ui.main_window import MainWindow

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _meta(filepath):
    return {
        "title": Path(filepath).stem,
        "artist": "Unknown",
        "album": "Unknown",
        "duration": 100.0,
        "filepath": filepath,
        "color": None,
        "end_action": "next",
    }


class TestTrackChangedPlaylistAdvance(unittest.TestCase):

    def setUp(self):
        self._saved_tracks = list(playlist._tracks)
        self._saved_index = playlist._current_index
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()
        playlist._tracks = self._saved_tracks
        playlist._current_index = self._saved_index

    def _load(self, filepaths, current=0):
        playlist._tracks = [_meta(fp) for fp in filepaths]
        playlist._current_index = current
        playlist._shuffle_order = []

    def test_natural_swap_advances_index(self):
        self._load(["/music/a.mp3", "/music/b.mp3"], current=0)

        self.window._on_engine_track_changed("/music/b.mp3")

        self.assertEqual(playlist.current_index, 1)

    def test_natural_swap_advances_past_adjacent_duplicate_track(self):
        # The bug this guards: comparing playlist.current["filepath"] against
        # the swapped-to filepath reads a natural swap as "already advanced"
        # when the SAME FILE sits at both index 0 and index 1, pinning
        # current_index at 0 forever -- the UI never leaves "Track 1 of 2" and
        # peek_next() keeps re-preloading index 1, so the pair loops instead
        # of ending. Index math (playlist.next()) is what makes duplicates
        # work; _pending_manual_swap is what makes it safe to always call it.
        self._load(["/music/dup.mp3", "/music/dup.mp3"], current=0)

        self.window._on_engine_track_changed("/music/dup.mp3")

        self.assertEqual(playlist.current_index, 1)

    def test_manual_swap_does_not_double_advance(self):
        # _advance_to already called playlist.next() to produce the meta it
        # handed to swap_to_preloaded(); advancing again here would skip a
        # track on every manual Next.
        self._load(["/music/a.mp3", "/music/b.mp3", "/music/c.mp3"], current=1)
        self.window._pending_manual_swap = True

        self.window._on_engine_track_changed("/music/b.mp3")

        self.assertEqual(playlist.current_index, 1)
        self.assertFalse(self.window._pending_manual_swap)  # consumed, one-shot

    def test_manual_swap_flag_is_not_sticky(self):
        # A consumed flag must not suppress the NEXT natural advance.
        self._load(["/music/a.mp3", "/music/b.mp3", "/music/c.mp3"], current=1)
        self.window._pending_manual_swap = True
        self.window._on_engine_track_changed("/music/b.mp3")

        self.window._on_engine_track_changed("/music/c.mp3")

        self.assertEqual(playlist.current_index, 2)

    def test_diverged_prediction_is_force_corrected(self):
        # R11: playlist.next() reshuffles at a shuffle+repeat=all wrap, so it
        # can return a different track than the one the engine already
        # swapped to (preloaded via the non-reshuffling peek_next()). The
        # engine's choice is what is AUDIBLE, so the selection follows it.
        self._load(["/music/a.mp3", "/music/b.mp3", "/music/c.mp3"], current=0)

        self.window._on_engine_track_changed("/music/c.mp3")  # not next() 's b

        self.assertEqual(playlist.current_index, 2)


if __name__ == "__main__":
    unittest.main()

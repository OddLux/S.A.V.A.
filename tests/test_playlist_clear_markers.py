"""dp-232: the playlist context menu's "Clear markers" acts on the SELECTED row.

The transport's own Clear buttons already act on the active deck. This menu
item exists specifically to reach a track that is NOT playing, so the thing
worth pinning is that it keys off the right-clicked row's filepath and leaves
every other track's markers -- including the playing track's -- untouched.

Plain unittest, no pytest in this project's venv:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from config.settings import settings
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


class TestClearMarkersFromPlaylist(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow()
        # `playlist.tracks` is a property returning a COPY -- mutating it does
        # nothing. `track_at()` reads `_tracks`, so populate that directly,
        # the same way test_main_window_deck_wiring does.
        self._saved_tracks = list(playlist._tracks)
        playlist._tracks = [_meta("a.wav"), _meta("b.wav")]

        settings.set("track_start_markers", {"a.wav": 5.0, "b.wav": 7.0})
        settings.set("track_end_markers", {"a.wav": 50.0, "b.wav": 70.0})

    def tearDown(self):
        settings.set("track_start_markers", {})
        settings.set("track_end_markers", {})
        playlist._tracks = self._saved_tracks
        self.window.close()

    def test_clears_only_the_selected_rows_markers(self):
        self.window._on_clear_track_markers(0)  # row 0 == a.wav

        starts = settings.get("track_start_markers", {})
        ends = settings.get("track_end_markers", {})

        self.assertNotIn("a.wav", starts)
        self.assertNotIn("a.wav", ends)
        # The other track must be completely undisturbed.
        self.assertEqual(starts.get("b.wav"), 7.0)
        self.assertEqual(ends.get("b.wav"), 70.0)

    def test_clears_the_second_row_not_just_the_first(self):
        """Discrimination check: a handler that ignored its argument and
        always cleared row 0 would pass the test above."""
        self.window._on_clear_track_markers(1)  # row 1 == b.wav

        starts = settings.get("track_start_markers", {})
        ends = settings.get("track_end_markers", {})

        self.assertNotIn("b.wav", starts)
        self.assertNotIn("b.wav", ends)
        self.assertEqual(starts.get("a.wav"), 5.0)
        self.assertEqual(ends.get("a.wav"), 50.0)

    def test_out_of_range_row_is_a_no_op(self):
        self.window._on_clear_track_markers(99)

        self.assertEqual(settings.get("track_start_markers", {}).get("a.wav"), 5.0)
        self.assertEqual(settings.get("track_end_markers", {}).get("b.wav"), 70.0)

    def test_menu_no_longer_offers_set_cue_items(self):
        """dp-232 removed Set Cue 1-4 from the playlist context menu; cues are
        set from the transport's Cue group instead."""
        source = Path(__file__).resolve().parent.parent / "ui" / "playlist_widget.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("Set Cue 1 here", text)
        self.assertIn("Clear markers", text)


if __name__ == "__main__":
    unittest.main()

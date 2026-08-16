"""dp-242: the read-only next-track preview must stop showing the outgoing
track's waveform the instant the preview re-targets to a new track, rather
than holding the old waveform on screen for the whole 200ms-deferred decode
window.

`_refresh_preview` (ui/main_window.py) already calls
`self._preview_waveform.set_loading(title)` synchronously, before the
decode's QTimer.singleShot(200, ...) defer -- so this file's job is to
pin that behavior down as a regression test, not to add new production
code. See DISCRIMINATION CHECK below for how it was verified this test
would actually catch the described bug if that call were ever removed.

Plain unittest, no pytest dependency:
    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PyQt6.QtWidgets import QApplication

from core.playlist import playlist
from ui.main_window import MainWindow
from ui.preview_waveform_widget import LOADING, READY

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _meta(filepath, track_id):
    return {
        "id": track_id,
        "title": Path(filepath).stem,
        "artist": "Unknown",
        "album": "Unknown",
        "duration": 100.0,
        "filepath": filepath,
        "color": None,
        "end_action": "next",
    }


class TestPreviewRetargetClearsStaleWaveform(unittest.TestCase):
    def setUp(self):
        self._saved_tracks = list(playlist._tracks)
        self._saved_index = playlist._current_index
        playlist._tracks = [
            _meta("/music/a.wav", "row-a"),
            _meta("/music/b.wav", "row-b"),
        ]
        playlist._current_index = 0
        playlist._shuffle_order = []
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()
        playlist._tracks = self._saved_tracks
        playlist._current_index = self._saved_index

    def test_retarget_clears_previous_waveform_before_decode_lands(self):
        # Preview A: land its waveform, so the widget is genuinely READY
        # and holding real data -- the state the reported bug leaks through.
        self.window._refresh_preview("/music/a.wav", "row-a")
        waveform_a = np.ones(4, dtype=np.float32)
        self.window._on_preview_waveform_ready("/music/a.wav", waveform_a, 100.0)
        self.assertEqual(self.window._preview_waveform._state, READY)

        # Re-target to B. B's own decode has NOT landed yet (no
        # _on_preview_waveform_ready call for it below) -- this is exactly
        # the decode window dp-242 reports as showing the wrong waveform.
        self.window._refresh_preview("/music/b.wav", "row-b")

        # DISCRIMINATION CHECK: fails if `self._preview_waveform.set_loading
        # (title)` is removed from `_refresh_preview` (verified by
        # commenting it out, running this test -- it then stays READY with
        # `waveform_a` still attached, reproducing dp-242's report -- then
        # restoring the call).
        self.assertEqual(self.window._preview_waveform._state, LOADING)
        self.assertIsNone(self.window._preview_waveform._waveform)

    def test_stale_decode_after_second_retarget_is_still_dropped(self):
        """dp-218's staleness guard (preserved, not the bug) -- a decode for
        A that lands after the target has moved to B must not paint."""
        self.window._refresh_preview("/music/a.wav", "row-a")
        self.window._refresh_preview("/music/b.wav", "row-b")

        waveform_a = np.ones(4, dtype=np.float32)
        self.window._on_preview_waveform_ready("/music/a.wav", waveform_a, 100.0)

        self.assertEqual(self.window._preview_waveform._state, LOADING)
        self.assertIsNone(self.window._preview_waveform._waveform)


if __name__ == "__main__":
    unittest.main()

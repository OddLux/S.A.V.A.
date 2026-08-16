"""dp-234: a primary-waveform decode that finishes for a track the user has
since replayed/moved away from must never write its duration/waveform onto
whatever row happens to be selected at that instant.

Mirrors dp-218's fix for the preview waveform (_kick_preview_analysis):
bind the filepath to the decode's own closure and drop the result in the
slot if it no longer matches `self._primary_target`.

No pytest dependency in this project's venv -- plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
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


class TestPrimaryWaveformStaleAnalysis(unittest.TestCase):
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

    def test_stale_result_does_not_write_to_current_row(self):
        self._load(["/music/a.wav", "/music/b.wav"], current=0)

        # Track A's decode was kicked and is still in flight when a newer
        # decode (e.g. track B, kicked by a deck swap or a replay) becomes
        # the primary target -- A's in-flight decode is now stale.
        self.window._primary_target = "/music/b.wav"

        waveform = np.zeros(4, dtype=np.float32)
        self.window._on_waveform_ready("/music/a.wav", waveform, 262.583)

        # A's stale result must land on NEITHER row.
        self.assertEqual(playlist.tracks[0]["duration"], 100.0)
        self.assertEqual(playlist.tracks[1]["duration"], 100.0)

    def test_current_result_writes_to_its_own_row_by_filepath(self):
        self._load(["/music/a.wav", "/music/b.wav"], current=1)
        self.window._primary_target = "/music/b.wav"

        waveform = np.zeros(4, dtype=np.float32)
        self.window._on_waveform_ready("/music/b.wav", waveform, 274.182)

        self.assertEqual(playlist.tracks[1]["duration"], 274.182)
        self.assertEqual(playlist.tracks[0]["duration"], 100.0)


if __name__ == "__main__":
    unittest.main()

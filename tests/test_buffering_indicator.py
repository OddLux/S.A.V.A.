"""dp-217: headless tests for the info-bar loading indicator.

Two halves:
  - DeckEngine side: `on_buffering` fires from the poll thread exactly on
    is_buffering() transitions, never per-tick while unchanged. This is
    still correct and still tested, even though the UI no longer wires it
    up (see the repurpose note in ui/main_window.py's _connect_engine) --
    DeckEngine itself is untouched.
  - UI side (repurposed 2026-08-02): `_lbl_buffering` now indicates a
    primary WAVEFORM decode in flight, not a DeckEngine re-buffer pause.
    Set by `_kick_primary_analysis`, cleared by `_on_waveform_ready` -- on
    BOTH the accepted path and the dp-234 stale-drop path, so it can never
    stick on after a crossfade-window replay.

Plain unittest, no pytest dependency:
    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.deck_engine as deck_engine_module

from tests.test_deck_engine import _make_bare_engine, _make_deck


class TestOnBufferingTransitions(unittest.TestCase):
    """DeckEngine's poll thread must fire on_buffering only on change (D7/R8)."""

    def setUp(self):
        self._orig_interval = deck_engine_module._POLL_INTERVAL
        deck_engine_module._POLL_INTERVAL = 0.01

    def tearDown(self):
        deck_engine_module._POLL_INTERVAL = self._orig_interval

    def test_fires_exactly_on_transition(self):
        engine = _make_bare_engine()
        engine._active = _make_deck()  # decode_complete=True -> not buffering
        calls = []
        engine.on_buffering = lambda flag: calls.append(flag)

        engine._poll_running = True
        engine._poll_thread = deck_engine_module.threading.Thread(
            target=engine._poll_position, daemon=True
        )
        engine._poll_thread.start()
        try:
            # Let a few idle ticks pass -- no transition yet, no callback.
            time.sleep(0.05)
            self.assertEqual(calls, [])

            # Simulate a streamed deck falling behind its decode frontier.
            with engine._lock:
                engine._active.decode_complete = False
                engine._active.read_idx = engine._active._frontier
            time.sleep(0.05)
            self.assertEqual(calls, [True])

            # Stay buffering across several more ticks -- no repeat callback.
            time.sleep(0.05)
            self.assertEqual(calls, [True])

            # Decode catches back up -- transitions back to False once.
            with engine._lock:
                engine._active.decode_complete = True
            time.sleep(0.05)
            self.assertEqual(calls, [True, False])
        finally:
            engine._poll_running = False
            engine._poll_thread.join(timeout=1.0)

    def test_resident_deck_can_report_buffering(self):
        """AC #3 in dp-217 claims buffering can 'never' be shown for a
        short/resident deck, citing `and not self.decode_complete`. That
        guard is not streamed-deck-specific -- a resident deck whose
        read_idx catches its frontier before decode_complete is set (e.g.
        mid-decode, or a stalled ffmpeg) reports buffering too. Confirmed
        behaviorally here rather than trusting the docstring."""
        deck = _make_deck()
        self.assertFalse(deck.is_buffering())  # decode_complete=True -> False

        deck.decode_complete = False
        deck.read_idx = deck._frontier
        self.assertTrue(deck.is_buffering())  # resident deck, still True


_app = None


def setUpModule():
    global _app
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)


import numpy as np

from core.playlist import playlist


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


class TestWaveformLoadingIndicatorUI(unittest.TestCase):
    """dp-217 repurpose: `_lbl_buffering` now tracks the primary waveform
    decode, driven directly by _kick_primary_analysis/_on_waveform_ready
    (no signal indirection needed -- both already run on the Qt thread)."""

    def setUp(self):
        from ui.main_window import MainWindow

        self._saved_tracks = list(playlist._tracks)
        self._saved_index = playlist._current_index
        playlist._tracks = [_meta("/music/a.wav"), _meta("/music/b.wav")]
        playlist._current_index = 0
        playlist._shuffle_order = []
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()
        playlist._tracks = self._saved_tracks
        playlist._current_index = self._saved_index

    def test_kick_sets_loading_text(self):
        # Discrimination check: fails against current master, where kicking
        # a decode never touches _lbl_buffering at all (it was still wired
        # to engine.on_buffering, which never fires in practice).
        self.assertEqual(self.window._lbl_buffering.text(), "")
        self.window._kick_primary_analysis("/music/a.wav")
        self.assertEqual(self.window._lbl_buffering.text(), "Buffering waveform")

    def test_accepted_result_clears_indicator(self):
        self.window._kick_primary_analysis("/music/a.wav")
        self.assertEqual(self.window._lbl_buffering.text(), "Buffering waveform")
        waveform = np.zeros(4, dtype=np.float32)
        self.window._on_waveform_ready("/music/a.wav", waveform, 123.0)
        self.assertEqual(self.window._lbl_buffering.text(), "")

    def test_stale_dropped_result_still_clears_indicator(self):
        """The sticking failure mode from the dp-217 repurpose note: a
        decode for the OLD target arrives after the target has already
        moved on (dp-234's guard drops it), and no further decode for the
        old target will ever be kicked. If the indicator only cleared on
        the accepted path, it would stay "Buffering waveform" forever.

        Discrimination check: fails against current master, where
        _on_waveform_ready's early return (filepath != _primary_target)
        skips the label entirely."""
        self.window._kick_primary_analysis("/music/a.wav")
        self.window._primary_target = "/music/b.wav"  # target moved on
        self.assertEqual(self.window._lbl_buffering.text(), "Buffering waveform")

        waveform = np.zeros(4, dtype=np.float32)
        self.window._on_waveform_ready("/music/a.wav", waveform, 123.0)  # stale

        self.assertEqual(self.window._lbl_buffering.text(), "")


if __name__ == "__main__":
    unittest.main()

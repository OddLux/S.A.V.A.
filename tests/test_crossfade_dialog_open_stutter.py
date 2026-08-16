"""
Regression test for dp-241 - opening the crossfade dialog stuttered
playback because every per-track waveform decode completion triggered an
immediate full CrossfadeTimelineWidget._render() (scene.clear() + rebuild,
including QPainterPath.intersected() per overlap) on the Qt main thread.
Several tracks' decodes finishing close together (the common case right
after set_layout()) meant a burst of N consecutive full rebuilds landing on
the main thread, contending the GIL against the sounddevice audio callback
thread.

The decode itself was already off-thread (core/analyzer.py's
WaveformAnalyzer._run runs in a daemon threading.Thread) - that part was
never the bug. The fix debounces _on_waveform_ready's render call via
ui/crossfade_timeline_widget.py's _waveform_render_timer, coalescing a
burst of near-simultaneous completions into a single rebuild.

No pytest dependency in this project's venv - plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from core.crossfade_model import CrossfadeLayout
from ui.crossfade_timeline_widget import CrossfadeTimelineWidget

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def make_layout(count):
    tracks = [{"filepath": f"t{i}.mp3", "duration": 180.0} for i in range(count)]
    return CrossfadeLayout.from_playlist_tracks(tracks)


class TestWaveformReadyRenderCoalescing(unittest.TestCase):
    def setUp(self):
        # analyze() is never actually invoked in this test - waveform-ready
        # is simulated directly via _on_waveform_ready - but patching it
        # keeps set_layout() from spinning real background decode threads
        # against nonexistent files.
        self._analyze_patcher = patch(
            "core.analyzer.WaveformAnalyzer.analyze", lambda self, filepath, points=2000: None
        )
        self._analyze_patcher.start()
        self.addCleanup(self._analyze_patcher.stop)

    def test_burst_of_waveform_ready_calls_collapses_to_one_render(self):
        widget = CrossfadeTimelineWidget()
        widget.set_layout(make_layout(5))
        QApplication.instance().processEvents()

        render_calls = []
        widget._render_impl = lambda: render_calls.append(1) or None

        # Simulate 5 tracks' decodes completing in a tight burst, as they
        # would right after dialog open.
        for i in range(5):
            widget._on_waveform_ready(i, [0.1, 0.2, 0.3])

        # No render before the debounce window elapses.
        QApplication.instance().processEvents()
        self.assertEqual(render_calls, [])

        # Let the debounce timer fire.
        deadline = widget._waveform_render_timer.interval() + 50
        QApplication.instance().processEvents()
        import time
        time.sleep(deadline / 1000.0)
        QApplication.instance().processEvents()

        self.assertEqual(
            len(render_calls), 1,
            f"expected exactly one coalesced render for a burst of 5 "
            f"waveform-ready calls, got {len(render_calls)}",
        )

    def test_single_waveform_ready_still_renders_eventually(self):
        widget = CrossfadeTimelineWidget()
        widget.set_layout(make_layout(1))
        QApplication.instance().processEvents()

        render_calls = []
        widget._render_impl = lambda: render_calls.append(1) or None

        widget._on_waveform_ready(0, [0.1, 0.2, 0.3])

        deadline = widget._waveform_render_timer.interval() + 50
        import time
        time.sleep(deadline / 1000.0)
        QApplication.instance().processEvents()

        self.assertEqual(len(render_calls), 1)


if __name__ == "__main__":
    unittest.main()

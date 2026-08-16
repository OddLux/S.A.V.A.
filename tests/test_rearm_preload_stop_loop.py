"""dp-254: `_rearm_preload` must preload the successor track under a `stop`
or `loop` end-action (zero-latency manual Next) while keeping auto-advance
OFF, so a natural end still halts/loops instead of stitching into it.

Before dp-254, `_idle_armed` served both "idle deck ready" and "auto-advance
permitted" -- so `_rearm_preload` had to skip preloading entirely for
`stop`/`loop` to avoid an unwanted auto-advance. DeckEngine now splits those
into `_idle_armed` and `_auto_advance_armed`; this test locks in the new
`_rearm_preload` contract at the UI layer, mocking `engine` so no real audio
stream/decode is involved.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _meta(filepath, end_action="next"):
    return {
        "title": Path(filepath).stem,
        "artist": "Unknown",
        "album": "Unknown",
        "duration": 100.0,
        "filepath": filepath,
        "id": filepath,
        "color": None,
        "end_action": end_action,
    }


class TestRearmPreloadUnderStopLoop(unittest.TestCase):

    def _rearm(self, current_action, next_meta):
        """Runs MainWindow._rearm_preload against a mocked engine + playlist,
        returning the mock so callers can assert on it. Bypasses building a
        full MainWindow (heavy, opens real widgets) -- only the attributes
        `_rearm_preload` itself touches are needed."""
        from ui.main_window import MainWindow

        fake_self = mock.Mock()
        fake_self._overlap_for_transition = mock.Mock(return_value="OVERLAP")

        with mock.patch("ui.main_window.engine") as fake_engine, \
             mock.patch("ui.main_window.playlist") as fake_playlist:
            fake_engine.current_file = "/music/current.mp3"
            fake_playlist.current_index = 0
            fake_playlist.get_track_end_action = mock.Mock(return_value=current_action)
            fake_playlist.peek_next = mock.Mock(return_value=next_meta)

            MainWindow._rearm_preload(fake_self)

            return fake_engine

    def test_stop_action_preloads_successor(self):
        next_meta = _meta("/music/next.mp3")
        fake_engine = self._rearm("stop", next_meta)

        fake_engine.preload.assert_called_once_with("/music/next.mp3", "/music/next.mp3")

    def test_stop_action_disarms_auto_advance(self):
        next_meta = _meta("/music/next.mp3")
        fake_engine = self._rearm("stop", next_meta)

        fake_engine.set_auto_advance.assert_called_once_with(False)

    def test_stop_action_never_arms_a_crossfade(self):
        next_meta = _meta("/music/next.mp3")
        fake_engine = self._rearm("stop", next_meta)

        fake_engine.arm_crossfade.assert_called_once_with(None)

    def test_loop_action_preloads_successor_and_disarms_auto_advance(self):
        next_meta = _meta("/music/next.mp3")
        fake_engine = self._rearm("loop", next_meta)

        fake_engine.preload.assert_called_once_with("/music/next.mp3", "/music/next.mp3")
        fake_engine.set_auto_advance.assert_called_once_with(False)
        fake_engine.arm_crossfade.assert_called_once_with(None)

    def test_next_action_still_preloads_and_arms_auto_advance(self):
        next_meta = _meta("/music/next.mp3")
        fake_engine = self._rearm("next", next_meta)

        fake_engine.preload.assert_called_once_with("/music/next.mp3", "/music/next.mp3")
        fake_engine.set_auto_advance.assert_called_once_with(True)
        fake_engine.arm_crossfade.assert_called_once_with("OVERLAP")


if __name__ == "__main__":
    unittest.main()

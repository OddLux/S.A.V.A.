"""UI review fixes (2026-08-01). Four defects found reading the UI layer:

1. Right-clicking a playlist track acted on the SELECTED row, not the row
   under the cursor -- a right-click does not move Qt's selection.
2. The per-track volume slider displayed the PLAYING track's volume but
   wrote to the SELECTED row.
3. `DeckEngine.set_track_volume` persisted under the requested filepath but
   applied the gain to `self._active` unconditionally, so adjusting a
   non-playing track's volume changed the volume of whatever was playing.
4. Transport cue-button highlights were never reset on a track change, so
   the previous track's lit cues persisted onto a track with no cues.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from test_deck_engine import _make_bare_engine, _make_deck  # noqa: E402
from ui.playlist_widget import PlaylistWidget  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

TRACKS = [
    {"filepath": f"t{i}.wav", "title": f"Track {i}", "duration": 100.0}
    for i in range(3)
]


class TestPlaylistContextMenuTargetsTheClickedRow(unittest.TestCase):

    def setUp(self):
        self.widget = PlaylistWidget()
        self.widget.populate([dict(t) for t in TRACKS])
        self.removed = []
        self.widget.remove_requested.connect(self.removed.append)

    def test_right_click_acts_on_the_row_under_the_cursor(self):
        # Select row 0, then right-click row 2. A right-click does not move
        # the selection, so the old currentRow() lookup returned 0.
        self.widget._list.setCurrentRow(0)
        item2 = self.widget._list.item(2)
        pos = self.widget._list.visualItemRect(item2).center()

        with mock.patch("ui.playlist_widget.QMenu") as fake_menu_cls:
            menu = fake_menu_cls.return_value
            actions = {}

            def _add_action(label):
                action = mock.MagicMock(name=label)
                actions[label] = action
                return action

            menu.addAction.side_effect = _add_action
            menu.addMenu.return_value.addAction.side_effect = _add_action
            menu.exec.side_effect = lambda _p: actions["Remove from playlist"]

            self.widget._show_context_menu(pos)

        self.assertEqual(self.removed, [2])

    def test_right_click_on_empty_space_does_nothing(self):
        self.widget._list.setCurrentRow(0)
        far_below = QPoint(5, self.widget._list.height() + 500)

        with mock.patch("ui.playlist_widget.QMenu") as fake_menu_cls:
            self.widget._show_context_menu(far_below)
            fake_menu_cls.assert_not_called()

        self.assertEqual(self.removed, [])


class TestTrackVolumeSliderTargetsThePlayingTrack(unittest.TestCase):

    def setUp(self):
        self.widget = PlaylistWidget()
        self.widget.populate([dict(t) for t in TRACKS])
        self.emitted = []
        self.widget.track_volume_changed.connect(
            lambda idx, vol: self.emitted.append((idx, vol))
        )

    def test_slider_writes_to_the_playing_track_not_the_selected_row(self):
        self.widget.set_current(1)          # track 1 is playing
        self.widget._list.setCurrentRow(2)  # user clicks a different row

        self.widget._track_vol_slider.setValue(42)

        self.assertEqual(self.emitted[-1], (1, 42))

    def test_no_emit_when_nothing_is_playing(self):
        self.widget._track_vol_slider.setValue(30)
        self.assertEqual(self.emitted, [])


class TestSetTrackVolumeOnlyTouchesMatchingDecks(unittest.TestCase):

    def _engine(self):
        engine = _make_bare_engine()
        engine._active = _make_deck()
        engine._active.filepath = "playing.wav"
        engine._active.track_volume = 1.0
        engine._idle = _make_deck()
        engine._idle.filepath = "queued.wav"
        engine._idle.track_volume = 1.0
        return engine

    def test_other_tracks_volume_does_not_change_the_playing_deck(self):
        engine = self._engine()
        with mock.patch("core.deck_engine.settings") as fake_settings:
            fake_settings.get.return_value = {}
            engine.set_track_volume(20, "some_other_track.wav")

        self.assertEqual(engine._active.track_volume, 1.0)
        self.assertEqual(engine._idle.track_volume, 1.0)

    def test_playing_deck_still_updates_for_its_own_file(self):
        engine = self._engine()
        with mock.patch("core.deck_engine.settings") as fake_settings:
            fake_settings.get.return_value = {}
            engine.set_track_volume(50, "playing.wav")

        self.assertAlmostEqual(engine._active.track_volume, 0.5)
        self.assertEqual(engine._idle.track_volume, 1.0)

    def test_preloaded_idle_deck_updates_so_it_swaps_in_at_the_right_gain(self):
        engine = self._engine()
        with mock.patch("core.deck_engine.settings") as fake_settings:
            fake_settings.get.return_value = {}
            engine.set_track_volume(25, "queued.wav")

        self.assertAlmostEqual(engine._idle.track_volume, 0.25)
        self.assertEqual(engine._active.track_volume, 1.0)

    def test_value_is_still_persisted_for_a_non_loaded_track(self):
        engine = self._engine()
        store = {}
        with mock.patch("core.deck_engine.settings") as fake_settings:
            fake_settings.get.return_value = store
            engine.set_track_volume(10, "not_loaded.wav")
            fake_settings.set.assert_called_once()
            key, value = fake_settings.set.call_args[0]

        self.assertEqual(key, "track_volumes")
        self.assertEqual(value["not_loaded.wav"], 10)


if __name__ == "__main__":
    unittest.main()

"""
Regression tests for ui/crossfade_dialog.py's per-track jump shortcut row
(dp-175).

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QGraphicsRectItem

from core.crossfade_model import CrossfadeLayout
from ui.crossfade_dialog import CrossfadeDialog
from ui.crossfade_timeline_widget import track_color, MARKER_PEN_WIDTH

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def make_layout(count: int) -> CrossfadeLayout:
    tracks = [
        {"filepath": f"/music/track{i}.mp3", "duration": 100.0 + i}
        for i in range(count)
    ]
    return CrossfadeLayout.from_playlist_tracks(tracks)


class TestCrossfadeDialogShortcutRow(unittest.TestCase):

    def setUp(self):
        self.dialog = CrossfadeDialog(layout=make_layout(3))
        _app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        _app.processEvents()

    def _shortcut_buttons(self):
        # dp-204: buttons live nested one QHBoxLayout row per capacity band
        # inside the outer _shortcut_rows QVBoxLayout, not flat in a single
        # row — flatten across rows preserving track order.
        rows = self.dialog._shortcut_rows
        buttons = []
        for i in range(rows.count()):
            sub = rows.itemAt(i).layout()
            if sub is None:
                continue
            for j in range(sub.count()):
                widget = sub.itemAt(j).widget()
                if widget is not None:
                    buttons.append(widget)
        return buttons

    def test_one_button_per_track_labeled_with_filename_stem(self):
        buttons = self._shortcut_buttons()
        self.assertEqual(len(buttons), 3)
        for idx, btn in enumerate(buttons):
            self.assertEqual(btn.text(), f"track{idx}")

    def test_clicking_button_scrolls_timeline_to_that_track(self):
        # End-to-end: clicking button idx=1 must actually center the
        # timeline view's viewport on track 1's *start* (dp-187 -- not its
        # midpoint), via the real scroll_to_track() call through the
        # button's connected signal, not a mocked stand-in.
        timeline = self.dialog._timeline
        positions = timeline.layout_model.track_positions()
        expected_start_x = positions[1] * timeline._pps

        buttons = self._shortcut_buttons()
        buttons[1].click()
        _app.processEvents()

        visible = timeline._view.mapToScene(timeline._view.viewport().rect()).boundingRect()
        self.assertAlmostEqual(visible.center().x(), expected_start_x, delta=2.0)

    def test_row_rebuilds_with_fewer_tracks_no_stale_buttons(self):
        self.dialog._timeline.set_layout(make_layout(1))
        _app.processEvents()
        buttons = self._shortcut_buttons()
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text(), "track0")

    def test_row_rebuilds_with_more_tracks_none_missing(self):
        self.dialog._timeline.set_layout(make_layout(5))
        _app.processEvents()
        buttons = self._shortcut_buttons()
        self.assertEqual(len(buttons), 5)
        self.assertEqual([b.text() for b in buttons], [f"track{i}" for i in range(5)])

    def test_row_starts_empty_when_layout_has_no_tracks(self):
        self.dialog._timeline.set_layout(CrossfadeLayout([]))
        _app.processEvents()
        self.assertEqual(self._shortcut_buttons(), [])


def _marker_for_track(timeline, index):
    # dp-205: marker switched from a QGraphicsLineItem to a filled
    # QGraphicsRectItem; the lane background rect also carries data(0) ==
    # index (dp-176), so disambiguate by the marker's fixed height.
    for item in timeline._scene.items():
        if (
            isinstance(item, QGraphicsRectItem)
            and item.data(0) == index
            and item.rect().height() == MARKER_PEN_WIDTH
        ):
            return item
    return None


class TestCrossfadeDialogShortcutButtonColors(unittest.TestCase):
    """dp-176: each shortcut button must be styled with the same per-track
    identity color as its track's marker line in the timeline, so a user
    can match a button to a lane at a glance."""

    def setUp(self):
        self.dialog = CrossfadeDialog(layout=make_layout(6))
        _app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        _app.processEvents()

    def _shortcut_buttons(self):
        rows = self.dialog._shortcut_rows
        buttons = []
        for i in range(rows.count()):
            sub = rows.itemAt(i).layout()
            if sub is None:
                continue
            for j in range(sub.count()):
                widget = sub.itemAt(j).widget()
                if widget is not None:
                    buttons.append(widget)
        return buttons

    def test_button_stylesheet_contains_its_track_color(self):
        buttons = self._shortcut_buttons()
        for idx, btn in enumerate(buttons):
            expected = track_color(idx)
            self.assertIn(expected, btn.styleSheet().lower())

    def test_button_color_matches_marker_line_color(self):
        timeline = self.dialog._timeline
        buttons = self._shortcut_buttons()
        for idx, btn in enumerate(buttons):
            marker = _marker_for_track(timeline, idx)
            self.assertIsNotNone(marker)
            # dp-205: marker is now a gradient-filled rect, not a flat-pen
            # line, so there is no single "marker color" to string-match —
            # instead confirm the gradient's top stop is derived from the
            # same identity color (track_color(idx)) the button uses.
            expected = track_color(idx)
            top_stop_color = marker.brush().gradient().stops()[0][1].name()
            self.assertEqual(top_stop_color, QColor(expected).lighter(130).name())
            self.assertIn(expected, btn.styleSheet().lower())

    def test_button_colors_stable_across_repeated_set_layout(self):
        self.dialog._timeline.set_layout(make_layout(6))
        _app.processEvents()
        first = [btn.styleSheet() for btn in self._shortcut_buttons()]

        self.dialog._timeline.set_layout(make_layout(6))
        _app.processEvents()
        second = [btn.styleSheet() for btn in self._shortcut_buttons()]

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

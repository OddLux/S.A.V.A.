"""
Regression tests for the dp-224 per-track time rulers and the relocated
global ruler on ui/crossfade_timeline_widget.py.

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QGraphicsLineItem, QGraphicsSimpleTextItem

from core.crossfade_model import CrossfadeLayout
from ui.crossfade_timeline_widget import (
    CrossfadeTimelineWidget, MARKER_PEN_WIDTH, PER_TRACK_RULER_Z, RULER_HEIGHT,
    RULER_Z, TRACK_HEIGHT, _fmt_time, _nice_interval,
)

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def make_layout(durations):
    tracks = [{"filepath": f"t{i}.mp3", "duration": d} for i, d in enumerate(durations)]
    return CrossfadeLayout.from_playlist_tracks(tracks)


def _global_ruler_items(widget):
    return [item for item in widget._scene.items() if item.zValue() == RULER_Z]


def _per_track_ruler_items(widget):
    return [item for item in widget._scene.items() if item.zValue() == PER_TRACK_RULER_Z]


def _per_track_labels(widget):
    return [
        item for item in _per_track_ruler_items(widget)
        if isinstance(item, QGraphicsSimpleTextItem)
    ]


def _per_track_lines(widget):
    return [
        item for item in _per_track_ruler_items(widget)
        if isinstance(item, QGraphicsLineItem)
    ]


class TestGlobalRulerRelocated(unittest.TestCase):
    def test_global_ruler_still_present_and_spans_full_timeline(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        widget.set_layout(make_layout([180.0, 220.0]))

        # dp-243: interval selection is now label-width-aware (duration-
        # dependent) - match the total duration the widget's own render used.
        interval = _nice_interval(widget._pps, 400.0)
        lines = [i for i in _global_ruler_items(widget) if isinstance(i, QGraphicsLineItem)]
        xs = sorted({round(item.line().x1(), 3) for item in lines})
        expected_first_tick = round(interval * widget._pps, 3)
        self.assertIn(expected_first_tick, xs)
        # Whole-timeline span: a tick exists near the end of the total
        # duration (last interval multiple <= total).
        total = 400.0
        last_multiple = (total // interval) * interval
        self.assertIn(round(last_multiple * widget._pps, 3), xs)

    def test_global_ruler_moved_above_the_per_track_upper_band(self):
        # dp-224: global ruler now occupies the band further from the
        # tracks than the upper per-track ruler band.
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        widget.set_layout(make_layout([180.0, 220.0]))
        global_lines = [i for i in _global_ruler_items(widget) if isinstance(i, QGraphicsLineItem)]
        self.assertTrue(global_lines)
        for item in global_lines:
            y1, y2 = item.line().y1(), item.line().y2()
            self.assertLessEqual(max(y1, y2), -(MARKER_PEN_WIDTH + RULER_HEIGHT))


class TestPerTrackRulers(unittest.TestCase):
    def test_ruler_exists_per_track_with_zero_first_label(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        widget.set_layout(make_layout([180.0, 220.0, 150.0]))

        for i in range(3):
            labels_at_zero = [
                lbl for lbl in _per_track_labels(widget)
                if lbl.text() == "0:00"
            ]
            self.assertTrue(labels_at_zero, f"no 0:00 label found (track {i})")

    def test_ticks_use_local_track_time_not_global_offset(self):
        # Track 1 starts at track 0's duration (180s). Its own ruler must
        # place ticks at (180 + local_t) * pps, not at local_t * pps alone.
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        layout = make_layout([180.0, 220.0])
        widget.set_layout(layout)
        positions = layout.track_positions()

        # dp-243: per-track rulers gate on that track's own duration.
        interval = _nice_interval(widget._pps, layout.tracks[1].duration)
        expected_x = round((positions[1] + interval) * widget._pps, 3)
        xs = {round(item.line().x1(), 3) for item in _per_track_lines(widget)}
        self.assertIn(expected_x, xs)

    def test_even_index_ruler_above_lane_odd_index_below(self):
        # Match each track's first non-zero tick (at its own local
        # `interval` offset) by exact x, rather than an x-range filter -
        # adjacent tracks can butt up against each other (overlaps), so a
        # range filter can pick up a neighboring track's ticks near the
        # boundary.
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        layout = make_layout([180.0, 220.0, 150.0, 200.0])
        widget.set_layout(layout)
        positions = layout.track_positions()
        tracks = layout.tracks

        for i, track in enumerate(tracks):
            # dp-243: per-track rulers gate on that track's own duration.
            interval = _nice_interval(widget._pps, track.duration)
            expected_x = round((positions[i] + interval) * widget._pps, 3)
            matches = [
                item for item in _per_track_lines(widget)
                if round(item.line().x1(), 3) == expected_x
            ]
            self.assertTrue(matches, f"no ruler tick at expected x for track {i}")
            ys = [y for item in matches for y in (item.line().y1(), item.line().y2())]
            if i % 2 == 0:
                self.assertTrue(all(y <= -MARKER_PEN_WIDTH for y in ys))
            else:
                self.assertTrue(all(y >= TRACK_HEIGHT + MARKER_PEN_WIDTH for y in ys))

    def test_interval_adapts_to_zoom(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 40.0  # MAX_PPS - tight interval
        widget.set_layout(make_layout([60.0, 60.0]))
        high_zoom_count = len(_per_track_lines(widget))

        widget._pps = 1.0  # zoomed way out - wide interval
        widget._render()
        low_zoom_count = len(_per_track_lines(widget))

        self.assertGreater(high_zoom_count, low_zoom_count)

    def test_zero_duration_layout_renders_no_per_track_ruler_items(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        widget.set_layout(make_layout([0.0]))
        self.assertEqual(_per_track_ruler_items(widget), [])

    def test_empty_layout_renders_without_exception(self):
        widget = CrossfadeTimelineWidget()
        widget.set_layout(make_layout([]))
        self.assertEqual(_per_track_ruler_items(widget), [])
        self.assertEqual(_global_ruler_items(widget), [])

    def test_non_positive_pps_renders_no_per_track_ruler_items(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 0.0
        widget.set_layout(make_layout([120.0, 120.0]))
        self.assertEqual(_per_track_ruler_items(widget), [])

    def test_item_count_bounded_at_low_zoom_with_five_tracks(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 0.05  # heavily zoomed out
        widget.set_layout(make_layout([180.0, 220.0, 150.0, 200.0, 190.0]))
        self.assertLess(len(_per_track_ruler_items(widget)), 100)


if __name__ == "__main__":
    unittest.main()


class TestGlobalRulerIsVisuallySubordinate(unittest.TestCase):
    """dp-224 follow-up (user request): the global whole-playlist ruler and
    the even-track rulers sat in adjacent bands with identical styling, so
    they read as one continuous strip of numbers with no cue as to which row
    meant what. The global row is now lifted clear by GLOBAL_RULER_GAP and
    drawn dimmer, so the per-track times -- the ones actually being read
    while editing an overlap -- dominate."""

    def setUp(self):
        self.widget = CrossfadeTimelineWidget()
        self.widget.set_layout(make_layout([120.0, 100.0, 140.0]))
        self.widget._pps = 5.0
        self.widget._render()
        _app.processEvents()

    def _items_at_z(self, z):
        return [it for it in self.widget._scene.items() if it.zValue() == z]

    def test_global_ruler_is_dimmer_than_the_per_track_rulers(self):
        global_items = self._items_at_z(RULER_Z)
        per_track_items = self._items_at_z(PER_TRACK_RULER_Z)
        self.assertTrue(global_items and per_track_items, "premise: both rulers drawn")

        self.assertLess(max(it.opacity() for it in global_items), 1.0)
        self.assertEqual(min(it.opacity() for it in per_track_items), 1.0)

    def test_global_ruler_sits_clear_above_the_per_track_band(self):
        global_bottom = max(
            it.sceneBoundingRect().bottom() for it in self._items_at_z(RULER_Z)
        )
        per_track_top = min(
            it.sceneBoundingRect().top()
            for it in self._items_at_z(PER_TRACK_RULER_Z)
            if it.sceneBoundingRect().top() < 0  # upper band only
        )
        self.assertLessEqual(
            global_bottom,
            per_track_top,
            "global ruler overlaps the even-track ruler band",
        )

    def test_scene_rect_covers_the_lifted_global_ruler(self):
        top = min(
            it.sceneBoundingRect().top() for it in self._items_at_z(RULER_Z)
        )
        self.assertLessEqual(self.widget._scene.sceneRect().y(), top)

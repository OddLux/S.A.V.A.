"""
Regression tests for the dp-214 time ruler on ui/crossfade_timeline_widget.py.

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import QApplication, QGraphicsLineItem, QGraphicsSimpleTextItem

from core.crossfade_model import CrossfadeLayout
from ui.crossfade_timeline_widget import (
    CrossfadeTimelineWidget, RULER_Z, _NICE_INTERVALS, _fmt_time, _nice_interval,
)

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def make_layout(durations):
    tracks = [{"filepath": f"t{i}.mp3", "duration": d} for i, d in enumerate(durations)]
    return CrossfadeLayout.from_playlist_tracks(tracks)


def _ruler_items(widget):
    return [item for item in widget._scene.items() if item.zValue() == RULER_Z]


class TestFmtTime(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(_fmt_time(0), "0:00")

    def test_over_a_minute(self):
        self.assertEqual(_fmt_time(65), "1:05")


class TestNiceInterval(unittest.TestCase):
    def test_high_zoom_uses_smallest_interval(self):
        self.assertEqual(_nice_interval(40), 1)

    def test_low_zoom_falls_back_to_largest_interval(self):
        # dp-239: the ladder's largest candidate is now 1800s (30min), not
        # 300s (5min) - pps small enough that even 1800*pps < MIN_TICK_PX
        # must fall through to the new top.
        self.assertEqual(_nice_interval(0.02), 1800)

    def test_dp239_no_op_for_short_and_medium_zooms(self):
        # dp-239 is additive-only at the TOP of the ladder. Prove the no-op
        # claim by computation, not eye: for a spread of pps values that
        # were already served by the pre-dp-239 ladder (max candidate 300),
        # the selected interval must match what the OLD ladder would have
        # picked - i.e. must never be one of the new large candidates
        # (600/900/1800), and must equal the same result computed against
        # the original tuple directly.
        old_ladder = (1, 2, 5, 10, 15, 30, 60, 120, 300)

        def _old_nice_interval(pps):
            for candidate in old_ladder:
                if candidate * pps >= 40:
                    return candidate
            return old_ladder[-1]

        # pps must stay high enough that the OLD ladder's top entry (300)
        # itself satisfies the MIN_TICK_PX gate (300*pps >= 40) - below that
        # the old algorithm was already falling back to an inadequate 300,
        # and the new ladder legitimately finds a better candidate there.
        # That is the fix working as intended, not a no-op violation.
        for pps in (40.0, 20.0, 5.0, 1.0, 0.5, 0.2, 0.15):
            with self.subTest(pps=pps):
                self.assertEqual(_nice_interval(pps), _old_nice_interval(pps))

    def test_dp239_long_timeline_selects_new_large_interval(self):
        # Full zoom-out on a long (~5h) timeline: the previous ladder topped
        # out at 300s and would still pick 300 here, which is the exact bug
        # (a tick every 5 minutes on a multi-hour timeline). This must now
        # pick one of the newly added large intervals.
        pps = 0.04  # matches the label-overlap test's full-zoom-out regime
        interval = _nice_interval(pps)
        self.assertIn(interval, (600, 900, 1800))

    def test_dp239_discrimination_against_old_ladder(self):
        # Fails outright if _NICE_INTERVALS is reverted to the pre-dp-239
        # tuple - proves this suite actually discriminates old vs new.
        old_ladder = (1, 2, 5, 10, 15, 30, 60, 120, 300)
        self.assertNotEqual(tuple(_NICE_INTERVALS), old_ladder)
        self.assertEqual(_NICE_INTERVALS[: len(old_ladder)], old_ladder)
        self.assertGreater(max(_NICE_INTERVALS), 300)


class TestRulerRendering(unittest.TestCase):
    def test_ticks_at_expected_x_positions(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        layout = make_layout([180.0, 220.0])
        widget.set_layout(layout)

        # dp-243: the ruler's actual interval choice is now label-width-aware
        # (duration-dependent) - pass the same total duration the widget's
        # own render used, or this test drifts from what's actually drawn.
        interval = _nice_interval(widget._pps, layout.total_duration())
        lines = [item for item in _ruler_items(widget) if isinstance(item, QGraphicsLineItem)]
        xs = sorted({round(item.line().x1(), 3) for item in lines})
        expected = round(interval * widget._pps, 3)
        self.assertIn(expected, xs)

    def test_zero_duration_layout_renders_no_ruler_items(self):
        widget = CrossfadeTimelineWidget()
        widget._pps = 20.0
        widget.set_layout(make_layout([0.0]))
        self.assertEqual(_ruler_items(widget), [])

    def test_empty_layout_renders_without_exception(self):
        widget = CrossfadeTimelineWidget()
        widget.set_layout(make_layout([]))
        self.assertEqual(_ruler_items(widget), [])

    def test_item_count_bounded_at_low_zoom(self):
        # At low zoom (small pps over a long total duration), _nice_interval
        # must fall back to a large candidate so tick count stays bounded -
        # this is the "don't draw a tick per second at low zoom" guard.
        widget = CrossfadeTimelineWidget()
        layout = make_layout([180.0, 220.0, 150.0, 200.0, 190.0])
        widget._pps = 0.05  # heavily zoomed out
        widget.set_layout(layout)
        self.assertLess(len(_ruler_items(widget)), 100)

    def test_item_count_matches_expected_tick_count_at_high_zoom(self):
        # At MAX_PPS (most zoomed in), _nice_interval legitimately picks a
        # tight 1s interval - full-duration item count scaling with duration
        # at that zoom is the known, separately-tracked dp-222 perf issue,
        # not something this ticket fixes. Just verify the count matches the
        # chosen interval's math (no runaway/off-by-one item generation).
        widget = CrossfadeTimelineWidget()
        layout = make_layout([18.0, 22.0, 15.0, 20.0, 19.0])
        widget._pps = 40.0  # MAX_PPS, most zoomed in
        widget.set_layout(layout)
        # dp-243: duration-aware gate - match what the render actually used.
        interval = _nice_interval(widget._pps, layout.total_duration())
        expected_ticks = int(94.0 // interval) + 1
        lines = [item for item in _ruler_items(widget) if isinstance(item, QGraphicsLineItem)]
        self.assertEqual(len(lines), expected_ticks)


    def test_dp239_labels_do_not_overlap_at_full_zoom_out_on_long_timeline(self):
        # The user complaint: on a long timeline zoomed all the way out,
        # tick labels bunch into a solid band. Measure actual label widths
        # against actual tick spacing rather than trusting the interval
        # number alone.
        widget = CrossfadeTimelineWidget()
        five_hours = 5 * 3600.0
        widget.set_layout(make_layout([five_hours]))
        # pps small enough that the ladder's new top candidate (1800s) is
        # the only one that clears MIN_TICK_PX - the regime a real 5h
        # timeline lands in once fit-to-view shrinks pps for a long
        # duration (dp-207 zoom mapping, _fit_to_view_pps).
        widget._pps = 0.04
        widget._render()

        interval = _nice_interval(widget._pps)
        tick_spacing_px = interval * widget._pps
        labels = [
            item for item in _ruler_items(widget)
            if isinstance(item, QGraphicsSimpleTextItem)
        ]
        self.assertGreater(len(labels), 1)
        metrics = QFontMetrics(labels[0].font())
        for label in labels:
            label_width_px = metrics.horizontalAdvance(label.text())
            self.assertLess(
                label_width_px, tick_spacing_px,
                f"label {label.text()!r} ({label_width_px}px) does not fit "
                f"in tick spacing ({tick_spacing_px}px) - labels overlap",
            )


    def test_dp243_no_label_overlap_swept_across_zoom_levels(self):
        # dp-243: dp-239 only fixed full zoom-out. Sweep a range of pps
        # values on a ~5h timeline (mid-zoom included, e.g. 0.05/0.08/0.2 -
        # the exact values the ticket measured as overlapping) and assert no
        # two adjacent labels overlap at any of them. A sweep, not spot
        # checks - spot checks are what let dp-239 through with the bug
        # still present.
        widget = CrossfadeTimelineWidget()
        five_hours = 5 * 3600.0
        widget.set_layout(make_layout([five_hours]))

        for pps in (0.0444, 0.05, 0.06, 0.08, 0.1, 0.12, 0.15, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0, 20.0, 40.0):
            with self.subTest(pps=pps):
                widget._pps = pps
                widget._render()

                labels = [
                    item for item in _ruler_items(widget)
                    if isinstance(item, QGraphicsSimpleTextItem)
                ]
                self.assertGreater(len(labels), 0)
                metrics = QFontMetrics(labels[0].font())
                xs = sorted(item.x() for item in labels)
                for prev_x, next_x in zip(xs, xs[1:]):
                    # Each label's own width must fit before the next
                    # label's x position - i.e. adjacent labels don't
                    # overlap. (Labels are drawn left-aligned at their tick
                    # x, per _draw_ruler.)
                    prev_label = next(itm for itm in labels if itm.x() == prev_x)
                    prev_w = metrics.horizontalAdvance(prev_label.text())
                    self.assertLessEqual(
                        prev_x + prev_w, next_x,
                        f"label {prev_label.text()!r} at x={prev_x} "
                        f"(width {prev_w}px) overlaps next label at x={next_x} "
                        f"(pps={pps})",
                    )


if __name__ == "__main__":
    unittest.main()

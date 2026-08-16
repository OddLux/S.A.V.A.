"""
Regression tests for ui/crossfade_timeline_widget.py's zoom/scrollbar
behavior (dp-165, dp-169).

dp-165 closed with a headless verification that was never persisted as an
actual test — it only checked `horizontalScrollBar().pageStep()`/`.maximum()`
after calling `slider.setValue(...)` directly, and never asserted the
scrollbar was actually *visible* on screen. That gap is exactly why the
live regression in dp-169 (scrollbar handle invisible at fit-to-view zoom
because of the implicit `ScrollBarAsNeeded` policy, rather than the
Premiere/DaVinci-style "always visible, full-width at min zoom" bar the
user expects) went undetected: the logical numbers were correct even while
the widget was invisible. This file makes that verification permanent and
covers both the logical metrics and the visibility/policy regression.

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication, QGraphicsRectItem, QGraphicsSimpleTextItem,
)

from core.crossfade_model import CrossfadeLayout
from ui.crossfade_timeline_widget import (
    CrossfadeTimelineWidget, _CurveHandleItem, TRACK_HEIGHT, MARKER_PEN_WIDTH,
    track_color,
)
from ui.skin import C_TIMELINE_PER_TRACK_PALETTE

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def make_layout():
    tracks = [
        {"filepath": "a.mp3", "duration": 180.0},
        {"filepath": "b.mp3", "duration": 220.0},
        {"filepath": "c.mp3", "duration": 150.0},
        {"filepath": "d.mp3", "duration": 200.0},
    ]
    return CrossfadeLayout.from_playlist_tracks(tracks)


def make_layout_n(count: int):
    tracks = [
        {"filepath": f"track{i}.mp3", "duration": 100.0 + i}
        for i in range(count)
    ]
    return CrossfadeLayout.from_playlist_tracks(tracks)


class TestCrossfadeTimelineScrollbar(unittest.TestCase):

    def setUp(self):
        self.widget = CrossfadeTimelineWidget()
        self.widget.resize(900, 400)
        self.widget.show()
        self.widget.set_layout(make_layout())
        _app.processEvents()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def _frac(self):
        hbar = self.widget._view.horizontalScrollBar()
        total = hbar.maximum() + hbar.pageStep()
        return (hbar.pageStep() / total) if total else 1.0

    def test_scrollbar_policy_is_always_on(self):
        # dp-169: ScrollBarAsNeeded hides the handle entirely at
        # fit-to-view zoom (maximum() == 0) instead of showing a
        # full-width bar, which is what the live regression report was
        # actually about. AlwaysOn is required so the handle is visible
        # (and correctly sized) across the whole zoom range.
        self.assertEqual(
            self.widget._view.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn,
        )

    def test_scrollbar_visible_at_fit_to_view_maximum(self):
        # dp-165's headless check only asserted maximum()==0 at fit-to-view
        # zoom and treated "hidden" as an acceptable pass. dp-169: the live
        # app must actually show a full-width bar here, not hide it.
        # dp-207: fit-to-view is now the slider's MAXIMUM (value 1000), not
        # the minimum - direction was flipped so left = zoomed in.
        self.widget._zoom_slider.setValue(1000)
        _app.processEvents()
        hbar = self.widget._view.horizontalScrollBar()
        self.assertEqual(hbar.maximum(), 0)
        self.assertTrue(hbar.isVisible())
        self.assertAlmostEqual(self._frac(), 1.0, places=3)

    def test_scrollbar_narrows_monotonically_across_live_drag_ticks(self):
        # Simulate a live drag by driving valueChanged the same way a real
        # mouse drag with tracking enabled does — one emit per intermediate
        # tick, not just a single jump to the final value — and assert the
        # handle fraction narrows (or holds) as the view zooms IN, staying
        # visible throughout.
        # dp-207: zoom-in is now the DECREASING slider direction (value 0 =
        # max zoom-in), so walk the ticks from fit-to-view (1000) down.
        prev_frac = None
        for value in (1000, 700, 500, 300, 150, 50):
            self.widget._zoom_slider.setValue(value)
            _app.processEvents()
            hbar = self.widget._view.horizontalScrollBar()
            self.assertTrue(hbar.isVisible())
            frac = self._frac()
            if prev_frac is not None:
                self.assertLessEqual(frac, prev_frac)
            prev_frac = frac

    def test_no_large_pps_jump_near_fit_to_view_end(self):
        # dp-207 regression: previously any slider value > 0 used a constant
        # MIN_PPS floor instead of fit-to-view, so stepping one tick off the
        # zoomed-out end jumped the PPS abruptly (the user's "huge jump
        # between full zoom out and the next step"). Under the exponential
        # curve anchored on fit_pps at value 1000, an adjacent tick must be
        # only marginally more zoomed-in, not a cliff.
        self.widget._zoom_slider.setValue(1000)
        _app.processEvents()
        fit_pps = self.widget._pps

        self.widget._zoom_slider.setValue(990)
        _app.processEvents()
        near_pps = self.widget._pps

        # One 1%-of-travel tick off the fit end stays within a small ratio
        # of the fit PPS (not the old jump straight to ~MIN_PPS).
        self.assertLess(near_pps, fit_pps * 1.2)
        self.assertGreaterEqual(near_pps, fit_pps)

    def test_scrollbar_widens_again_when_zooming_back_out(self):
        # Mirrors a live user zooming in then back out — the handle must
        # track _pps live on every tick, not just on the final value/on
        # release, per dp-169's "stale _pps during drag" hypothesis.
        # dp-207: value 0 is now the zoomed-IN end, value 1000 the
        # zoomed-OUT (fit) end - swapped from the pre-flip mapping.
        self.widget._zoom_slider.setValue(0)
        _app.processEvents()
        zoomed_in_frac = self._frac()

        self.widget._zoom_slider.setValue(950)
        _app.processEvents()
        zoomed_out_frac = self._frac()

        self.assertGreater(zoomed_out_frac, zoomed_in_frac)

    def test_fit_to_view_refits_on_live_resize(self):
        # dp-207: fit-to-view now sits at slider value 1000 (was 0).
        self.widget._zoom_slider.setValue(1000)
        _app.processEvents()
        pps_before = self.widget._pps

        self.widget.resize(1400, 400)
        _app.processEvents()
        pps_after = self.widget._pps

        self.assertNotAlmostEqual(pps_before, pps_after, places=3)
        hbar = self.widget._view.horizontalScrollBar()
        self.assertEqual(hbar.maximum(), 0)
        self.assertTrue(hbar.isVisible())

    def test_interior_zoom_reacts_to_resize(self):
        # dp-207: under the exponential curve, fit_pps anchors every slider
        # position, so an interior (non-endpoint) value must also re-fit on
        # resize - not just the fit-to-view endpoint.
        self.widget._zoom_slider.setValue(600)
        _app.processEvents()
        pps_before = self.widget._pps

        self.widget.resize(1400, 400)
        _app.processEvents()
        pps_after = self.widget._pps

        self.assertNotAlmostEqual(pps_before, pps_after, places=3)

    def test_zoom_direction_and_nonlinear_response(self):
        # dp-207 acceptance criteria 1, 4: slider value 0 renders at the
        # (reduced) max zoom-in PPS, value 1000 at fit-to-view, and an
        # interior value is NOT the linear midpoint of the two ends (proves
        # the response curve is non-linear, not the old flat interpolation).
        from ui.crossfade_timeline_widget import MAX_PPS

        self.widget._zoom_slider.setValue(0)
        _app.processEvents()
        self.assertAlmostEqual(self.widget._pps, MAX_PPS, places=3)

        self.widget._zoom_slider.setValue(1000)
        _app.processEvents()
        fit_pps = self.widget._pps
        self.assertAlmostEqual(fit_pps, self.widget._fit_to_view_pps(), places=3)

        self.widget._zoom_slider.setValue(500)
        _app.processEvents()
        linear_mid = MAX_PPS + 0.5 * (fit_pps - MAX_PPS)
        self.assertNotAlmostEqual(self.widget._pps, linear_mid, places=2)


class TestScrollToTrack(unittest.TestCase):
    """dp-175: scroll_to_track() must center the requested track's lane in
    the viewport without touching zoom (_pps)."""

    def setUp(self):
        self.widget = CrossfadeTimelineWidget()
        self.widget.resize(900, 400)
        self.widget.show()
        self.widget.set_layout(make_layout())
        _app.processEvents()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def _visible_scene_rect(self):
        return self.widget._view.mapToScene(
            self.widget._view.viewport().rect()
        ).boundingRect()

    def test_scroll_to_track_centers_each_track_in_viewport(self):
        # dp-187: scroll_to_track() centers the viewport on the track's
        # *start* (left edge), not the midpoint of its full duration span —
        # clicking a track's jump shortcut should land the user at the
        # beginning of that track, not somewhere in its middle.
        positions = self.widget._layout.track_positions()
        tracks = self.widget._layout.tracks
        pps = self.widget._pps
        for idx in range(len(tracks)):
            self.widget.scroll_to_track(idx)
            _app.processEvents()
            visible = self._visible_scene_rect()
            expected_start_x = positions[idx] * pps
            # centerOn() clamps to the scene's bounds, so the very first
            # track (whose start is the scene's left edge) can't actually
            # land its center there — allow the clamp for idx == 0.
            if idx == 0:
                self.assertLessEqual(visible.center().x(), expected_start_x + visible.width() / 2.0 + 2.0)
            else:
                self.assertAlmostEqual(visible.center().x(), expected_start_x, delta=2.0)

    def test_scroll_to_track_does_not_change_zoom(self):
        pps_before = self.widget._pps
        self.widget.scroll_to_track(1)
        _app.processEvents()
        self.assertEqual(self.widget._pps, pps_before)

    def test_scroll_to_track_out_of_range_index_is_noop(self):
        # Must not raise for an invalid index.
        self.widget.scroll_to_track(-1)
        self.widget.scroll_to_track(99)
        _app.processEvents()

    def test_scroll_to_track_with_no_layout_is_noop(self):
        widget = CrossfadeTimelineWidget()
        try:
            widget.scroll_to_track(0)  # no layout set — must not raise
        finally:
            widget.deleteLater()
            _app.processEvents()


def _find_handle(widget, overlap_index, which, curve_role="in"):
    # dp-190: each overlap now has two handle pairs (fade-in and the
    # independent fade-out curve) sharing the same z-value, so ov_index +
    # which alone is ambiguous — curve_role disambiguates which pair.
    for item in widget._scene.items():
        if (
            isinstance(item, _CurveHandleItem)
            and item._ov_index == overlap_index
            and item._which == which
            and item._curve_role == curve_role
        ):
            return item
    return None


def _send_mouse_event(vp, event_type, view_pos, buttons, button=Qt.MouseButton.NoButton):
    global_pos = vp.mapToGlobal(view_pos)
    event = QMouseEvent(
        event_type, view_pos.toPointF(), global_pos.toPointF(),
        button, buttons, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.instance().sendEvent(vp, event)


class TestCrossfadeCurveHandleDrag(unittest.TestCase):
    """dp-180: regression coverage for the curve-handle drag bug — dragging
    used to delete the handle item mid-drag (see widget module docstring
    on _on_handle_moved) because each itemChange scheduled a full
    scene.clear()+rebuild via QTimer, destroying the very item Qt was
    still tracking as the active mouse grabber."""

    def setUp(self):
        # dp-197: these tests use fake, non-existent track paths ("a.mp3",
        # "b.mp3"), so WaveformAnalyzer.analyze() always fails to decode.
        # core/analyzer.py's failure path still calls on_ready (with a
        # zeroed waveform) from its background thread, asynchronously and
        # with real subprocess-spawn jitter (30-50ms+ typical). That signal
        # crosses threads via _waveform_ready_signal, so Qt auto-queues it
        # onto the main thread - it can land during, or right after, a
        # simulated drag's own processEvents() calls, triggering a real
        # _render() that legitimately adds 2 new waveform QGraphicsPathItems
        # (one per track) that weren't present when a test's "before" scene
        # item count was captured. That's not a scene-item leak in the
        # drag-handling code - it's an unrelated, always-pending background
        # decode racing an assertion that never accounted for it. None of
        # this class's tests exercise waveform rendering, so patch analyze()
        # to a no-op for the whole class: removes the nondeterminism at its
        # source instead of tolerating/working around the race.
        self._analyze_patcher = patch(
            "core.analyzer.WaveformAnalyzer.analyze", lambda self, filepath, points=2000: None
        )
        self._analyze_patcher.start()

        self.widget = CrossfadeTimelineWidget()
        self.widget.resize(900, 400)
        self.widget.show()
        tracks = [
            {"filepath": "a.mp3", "duration": 180.0},
            {"filepath": "b.mp3", "duration": 220.0},
        ]
        self.layout = CrossfadeLayout.from_playlist_tracks(tracks)
        self.layout.set_overlap_duration(0, 60.0)
        self.widget.set_layout(self.layout)
        QApplication.instance().processEvents()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        QApplication.instance().processEvents()
        self._analyze_patcher.stop()

    def test_dragging_handle_survives_multiple_move_ticks(self):
        app = QApplication.instance()
        handle = _find_handle(self.widget, 0, 0)
        self.assertIsNotNone(handle)

        vp = self.widget._view.viewport()
        start = self.widget._view.mapFromScene(handle.scenePos())

        _send_mouse_event(
            vp, QMouseEvent.Type.MouseButtonPress, start,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        )
        app.processEvents()

        for dx in range(1, 30, 3):
            pos = QPoint(start.x() + dx, start.y())
            _send_mouse_event(
                vp, QMouseEvent.Type.MouseMove, pos,
                Qt.MouseButton.LeftButton,
            )
            app.processEvents()
            # The handle item must still be a live C++ object after every
            # tick — this raises RuntimeError if it was deleted mid-drag.
            handle.pos()

        _send_mouse_event(
            vp, QMouseEvent.Type.MouseButtonRelease, QPoint(start.x() + 29, start.y()),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        )
        app.processEvents()

        # The drag actually moved the model's control point, proving the
        # drag wasn't silently a no-op either.
        self.assertNotEqual(self.layout.overlaps[0].curve_p1[0], 0.34)

    def test_drag_does_not_accumulate_duplicate_scene_items(self):
        app = QApplication.instance()
        before_count = len(self.widget._scene.items())
        handle = _find_handle(self.widget, 0, 0)
        vp = self.widget._view.viewport()
        start = self.widget._view.mapFromScene(handle.scenePos())

        _send_mouse_event(
            vp, QMouseEvent.Type.MouseButtonPress, start,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        )
        app.processEvents()
        for dx in (1, 5, 10, 15, 20):
            _send_mouse_event(
                vp, QMouseEvent.Type.MouseMove, QPoint(start.x() + dx, start.y()),
                Qt.MouseButton.LeftButton,
            )
            app.processEvents()
        _send_mouse_event(
            vp, QMouseEvent.Type.MouseButtonRelease, QPoint(start.x() + 20, start.y()),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        )
        app.processEvents()

        self.assertEqual(len(self.widget._scene.items()), before_count)

    def test_double_click_reset_still_works(self):
        # dp-166 regression: reset-to-default via double-click must
        # continue to work after the dp-180 drag fix.
        from core.crossfade_model import DEFAULT_CURVE_P1, DEFAULT_CURVE_P2

        self.widget._layout.set_overlap_curve(0, (0.1, 0.2), (0.8, 0.9))
        self.widget._reset_overlap_curve(0)
        QApplication.instance().processEvents()
        self.assertEqual(self.widget._layout.overlaps[0].curve_p1, DEFAULT_CURVE_P1)
        self.assertEqual(self.widget._layout.overlaps[0].curve_p2, DEFAULT_CURVE_P2)

    def test_set_linear_action_sets_collinear_control_points(self):
        self.widget._layout.set_overlap_curve(0, (0.1, 0.2), (0.8, 0.9))
        self.widget._set_overlap_curve_linear(0)
        QApplication.instance().processEvents()
        ov = self.widget._layout.overlaps[0]
        self.assertAlmostEqual(ov.curve_p1[0], 1 / 3)
        self.assertAlmostEqual(ov.curve_p1[1], 1 / 3)
        self.assertAlmostEqual(ov.curve_p2[0], 2 / 3)
        self.assertAlmostEqual(ov.curve_p2[1], 2 / 3)
        for i in range(11):
            t = i / 10.0
            self.assertAlmostEqual(ov.evaluate_in(t), t, places=4)


def _marker_for_track(widget, index):
    """dp-205: find the marker QGraphicsRectItem for a given track index,
    via the role-0 data set alongside the rect item's own track-index tag —
    stacking order in scene.items() is not insertion order, so identity
    must be looked up by data, not position in the returned list. The lane
    background rect also carries data(0) == index (dp-176), so disambiguate
    by the marker's fixed height (MARKER_PEN_WIDTH, unlike the background's
    TRACK_HEIGHT)."""
    for item in widget._scene.items():
        if (
            isinstance(item, QGraphicsRectItem)
            and item.data(0) == index
            and item.rect().height() == MARKER_PEN_WIDTH
        ):
            return item
    return None


class TestPerTrackColorCoding(unittest.TestCase):
    """dp-176: each track's marker line gets its own color from
    C_TIMELINE_PER_TRACK_PALETTE (indexed by track position, wrapping past
    the palette's length), replacing dp-170's flat odd/even 2-color split."""

    def setUp(self):
        self.widget = CrossfadeTimelineWidget()
        self.widget.resize(900, 400)
        self.widget.show()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def test_marker_line_color_matches_palette_index(self):
        self.widget.set_layout(make_layout_n(5))
        _app.processEvents()
        for idx in range(5):
            marker = _marker_for_track(self.widget, idx)
            self.assertIsNotNone(marker)
            expected = C_TIMELINE_PER_TRACK_PALETTE[idx % len(C_TIMELINE_PER_TRACK_PALETTE)]
            # dp-205: marker paints a lighter(130)/darker(130) gradient of
            # the identity color, not a flat pen — assert the gradient
            # stops instead of a single pen color.
            stops = marker.brush().gradient().stops()
            self.assertEqual(stops[0][1].name(), QColor(expected).lighter(130).name())
            self.assertEqual(stops[1][1].name(), QColor(expected).darker(130).name())

    def test_track_color_helper_matches_marker_color(self):
        # The helper CrossfadeDialog reuses to colorize shortcut buttons
        # must return exactly what the marker gradient was actually built
        # from — not just the same formula independently re-derived.
        self.widget.set_layout(make_layout_n(6))
        _app.processEvents()
        for idx in range(6):
            marker = _marker_for_track(self.widget, idx)
            stops = marker.brush().gradient().stops()
            expected = track_color(idx)
            self.assertEqual(stops[0][1].name(), QColor(expected).lighter(130).name())
            self.assertEqual(stops[1][1].name(), QColor(expected).darker(130).name())

    def test_palette_wraps_gracefully_beyond_palette_size(self):
        # 8-entry palette; 11 tracks forces wraparound (index 8 == index 0,
        # index 9 == index 1, index 10 == index 2).
        n = len(C_TIMELINE_PER_TRACK_PALETTE)
        self.widget.set_layout(make_layout_n(n + 3))
        _app.processEvents()
        for idx in range(n + 3):
            marker = _marker_for_track(self.widget, idx)
            self.assertIsNotNone(marker)
            expected = C_TIMELINE_PER_TRACK_PALETTE[idx % n]
            stops = marker.brush().gradient().stops()
            self.assertEqual(stops[0][1].name(), QColor(expected).lighter(130).name())
            self.assertEqual(stops[1][1].name(), QColor(expected).darker(130).name())
        # Explicit wraparound pairing check.
        first_marker = _marker_for_track(self.widget, 0)
        wrapped_marker = _marker_for_track(self.widget, n)
        self.assertEqual(
            first_marker.brush().gradient().stops()[0][1].name(),
            wrapped_marker.brush().gradient().stops()[0][1].name(),
        )

    def test_colors_stay_stable_across_repeated_set_layout_calls(self):
        # Same tracks, same order, called twice — colors must not
        # re-randomize or shift on redraw.
        layout1 = make_layout_n(6)
        self.widget.set_layout(layout1)
        _app.processEvents()
        first_colors = [
            _marker_for_track(self.widget, i).brush().gradient().stops()[0][1].name()
            for i in range(6)
        ]

        layout2 = make_layout_n(6)
        self.widget.set_layout(layout2)
        _app.processEvents()
        second_colors = [
            _marker_for_track(self.widget, i).brush().gradient().stops()[0][1].name()
            for i in range(6)
        ]

        self.assertEqual(first_colors, second_colors)

    def test_palette_has_at_least_six_distinct_colors(self):
        # Acceptance criteria: 6-8 visually distinguishable colors.
        self.assertGreaterEqual(len(C_TIMELINE_PER_TRACK_PALETTE), 6)
        self.assertEqual(
            len(set(C_TIMELINE_PER_TRACK_PALETTE)), len(C_TIMELINE_PER_TRACK_PALETTE)
        )

    def test_marker_top_bottom_placement_still_keyed_off_parity(self):
        # dp-176 must not disturb dp-170's placement logic — only the color
        # source changed. dp-208 later moved both markers off the waveform
        # into the lane gutters: top markers now occupy
        # [-MARKER_PEN_WIDTH, 0], bottom markers [TRACK_HEIGHT,
        # TRACK_HEIGHT + MARKER_PEN_WIDTH] (see ui/crossfade_timeline_widget
        # ._render_impl). Still keyed off i % 2, just at the gutter y.
        self.widget.set_layout(make_layout_n(5))
        _app.processEvents()
        for idx in range(5):
            marker = _marker_for_track(self.widget, idx)
            expected_y = -MARKER_PEN_WIDTH if idx % 2 == 0 else TRACK_HEIGHT
            self.assertAlmostEqual(marker.rect().y(), expected_y)


def _marker_rect_for_track(widget, index):
    """dp-208: find the marker's QGraphicsRectItem for a track. Both the
    lane background rect and the marker rect carry data(0) == track index
    (see _render_impl) -- the marker is the one sized MARKER_PEN_WIDTH tall,
    not TRACK_HEIGHT tall."""
    for item in widget._scene.items():
        if (
            isinstance(item, QGraphicsRectItem)
            and item.data(0) == index
            and item.rect().height() == MARKER_PEN_WIDTH
        ):
            return item
    return None


def _label_item_for_track(widget, index):
    """dp-208: find the track-name label item by matching its x position
    against the track's lane bounds (label items carry no track-index
    data(0))."""
    positions = widget._layout.track_positions()
    x = positions[index] * widget._pps
    w = max(1.0, widget._layout.tracks[index].duration * widget._pps)
    for item in widget._scene.items():
        if isinstance(item, QGraphicsSimpleTextItem):
            pos = item.pos()
            if x - 0.5 <= pos.x() <= x + w + 0.5:
                return item
    return None


class TestMarkerGutterPlacement(unittest.TestCase):
    """dp-208 (amended approach): marker bars moved off the waveform into
    the lane gutters instead of moving the dp-168 label."""

    def setUp(self):
        self.widget = CrossfadeTimelineWidget()
        self.widget.resize(900, 400)
        self.widget.show()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def test_marker_rects_sit_in_gutters_not_on_waveform(self):
        self.widget.set_layout(make_layout_n(5))
        _app.processEvents()
        for idx in range(5):
            marker = _marker_rect_for_track(self.widget, idx)
            self.assertIsNotNone(marker)
            rect = marker.rect()
            if idx % 2 == 0:
                self.assertLess(rect.y(), 0)
                self.assertAlmostEqual(rect.y() + rect.height(), 0)
            else:
                self.assertGreaterEqual(rect.y(), TRACK_HEIGHT)
                self.assertAlmostEqual(rect.y(), TRACK_HEIGHT)

    def test_scene_rect_expanded_to_cover_both_gutters(self):
        # dp-214: scene rect's y-origin/height further extended upward by
        # RULER_HEIGHT for the time ruler band above the top gutter.
        # dp-224: the global ruler moved up one more RULER_HEIGHT (closer to
        # the zoom bar) and a per-track ruler band was added below the
        # bottom gutter for odd-index tracks - scene rect grows by
        # RULER_HEIGHT on both ends.
        # dp-224 follow-up: plus GLOBAL_RULER_GAP, the extra separation that
        # lifts the global ruler clear of the even-track rulers below it.
        from ui.crossfade_timeline_widget import (
            GLOBAL_RULER_GAP, RULER_HEIGHT, _RULER_PEN_PAD,
        )
        self.widget.set_layout(make_layout_n(3))
        _app.processEvents()
        scene_rect = self.widget._scene.sceneRect()
        self.assertAlmostEqual(
            scene_rect.y(),
            -(MARKER_PEN_WIDTH + 2 * RULER_HEIGHT + GLOBAL_RULER_GAP
              + _RULER_PEN_PAD),
        )
        self.assertAlmostEqual(
            scene_rect.height(),
            TRACK_HEIGHT + 2 * MARKER_PEN_WIDTH + 3 * RULER_HEIGHT
            + GLOBAL_RULER_GAP + 2 * _RULER_PEN_PAD,
        )

    def test_labels_no_longer_intersect_markers(self):
        self.widget.set_layout(make_layout_n(6))
        _app.processEvents()
        for idx in range(6):
            marker = _marker_rect_for_track(self.widget, idx)
            label = _label_item_for_track(self.widget, idx)
            self.assertIsNotNone(marker)
            self.assertIsNotNone(label)
            marker_bounds = marker.sceneBoundingRect()
            label_bounds = label.sceneBoundingRect()
            self.assertFalse(marker_bounds.intersects(label_bounds))


if __name__ == "__main__":
    unittest.main()

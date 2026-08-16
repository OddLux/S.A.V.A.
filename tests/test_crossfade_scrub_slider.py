"""dp-219: CrossfadeScrubSlider thumb geometry + drag gating.

Covers the two review findings that made the as-built widget wrong:

- D2: paint placed the thumb at `t * (w - _THUMB_WIDTH)` while the click
  handler read `x / w`. Grabbing the thumb where it was drawn jumped the
  fade, and `t == 1.0` -- the engine's finalize condition, which this whole
  feature depends on -- was unreachable by dragging to the right edge.
- D5: any left-click anywhere on the widget started a drag, so a stray click
  instantly warped a live, audible A/B mix.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.crossfade_scrub_slider import CrossfadeScrubSlider, _THUMB_WIDTH  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

WIDTH = 200
HEIGHT = 18


def _press(widget, x):
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, HEIGHT / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _move(widget, x):
    return QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(x, HEIGHT / 2),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


class _SliderCase(unittest.TestCase):
    def setUp(self):
        self.widget = CrossfadeScrubSlider()
        # setFixedSize, not resize(): the widget is horizontally Expanding and
        # never shown here, so a plain resize() does not pin width() and the
        # geometry assertions below would compare against a layout-chosen width.
        self.widget.setFixedSize(WIDTH, HEIGHT)
        self.seen = []
        self.widget.gain_seek_requested.connect(self.seen.append)


class TestThumbGeometryRoundTrips(_SliderCase):

    def test_thumb_travel_excludes_its_own_width(self):
        self.widget.set_progress(True, 1.0)
        # Left edge of the thumb at full progress, not off the end.
        self.assertEqual(self.widget._thumb_x(), WIDTH - _THUMB_WIDTH)
        self.assertLessEqual(self.widget._thumb_rect().right(), WIDTH)

    def test_grabbing_the_thumb_does_not_move_the_fade(self):
        """The D2 bug: press on the drawn thumb emitted a different t than
        the one the thumb was drawn at, so merely grabbing it jumped the mix."""
        self.widget.set_progress(True, 0.5)
        grab_x = self.widget._thumb_x() + _THUMB_WIDTH / 2

        self.widget.mousePressEvent(_press(self.widget, grab_x))

        self.assertEqual(len(self.seen), 1)
        self.assertAlmostEqual(self.seen[0], 0.5, places=2)

    def test_dragging_to_the_right_edge_reaches_exactly_one(self):
        """t == 1.0 is the engine's existing finalize condition. Under the
        old `x / width()` mapping this was unreachable from the thumb."""
        self.widget.set_progress(True, 0.5)
        self.widget.mousePressEvent(
            _press(self.widget, self.widget._thumb_x() + _THUMB_WIDTH / 2)
        )
        self.widget.mouseMoveEvent(_move(self.widget, WIDTH + 50))  # overshoot

        self.assertEqual(self.seen[-1], 1.0)

    def test_dragging_left_rewinds_bidirectionally(self):
        self.widget.set_progress(True, 0.8)
        self.widget.mousePressEvent(
            _press(self.widget, self.widget._thumb_x() + _THUMB_WIDTH / 2)
        )
        self.widget.mouseMoveEvent(_move(self.widget, -50))  # overshoot left

        self.assertEqual(self.seen[-1], 0.0)


class TestDragGating(_SliderCase):

    def test_click_on_the_groove_is_ignored(self):
        """D5: a crossfade is an audible, committed, seconds-long transition.
        A stray click must not warp the A/B mix to wherever the pointer hit."""
        self.widget.set_progress(True, 0.5)
        thumb_right = self.widget._thumb_x() + _THUMB_WIDTH

        self.widget.mousePressEvent(_press(self.widget, thumb_right + 40))

        self.assertEqual(self.seen, [])
        self.assertFalse(self.widget._dragging)

    def test_press_ignored_entirely_when_no_crossfade_is_running(self):
        self.widget.set_progress(False, 0.0)

        self.widget.mousePressEvent(_press(self.widget, WIDTH // 2))

        self.assertEqual(self.seen, [])
        self.assertFalse(self.widget._dragging)

    def test_live_progress_is_ignored_while_dragging(self):
        self.widget.set_progress(True, 0.2)
        self.widget.mousePressEvent(
            _press(self.widget, self.widget._thumb_x() + _THUMB_WIDTH / 2)
        )
        self.widget.mouseMoveEvent(_move(self.widget, WIDTH * 0.75))
        dragged_x = self.widget._thumb_x()

        # A poll tick lands mid-drag: the user's own position must win.
        self.widget.set_progress(True, 0.05)

        self.assertEqual(self.widget._thumb_x(), dragged_x)


if __name__ == "__main__":
    unittest.main()

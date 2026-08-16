"""
Regression tests for the timecode click-to-cycle display modes (dp-213).

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from core.engine import engine
from ui.main_window import MainWindow, _format_timecode

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _left_click_event():
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(1, 1),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _right_click_event():
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(1, 1),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )


class TestFormatTimecode(unittest.TestCase):

    def test_elapsed_mode(self):
        self.assertEqual(_format_timecode(83, 296, 0), "1:23 / 4:56")

    def test_remaining_mode(self):
        self.assertEqual(_format_timecode(83, 296, 1), "-3:33 / 4:56")

    def test_both_mode(self):
        self.assertEqual(_format_timecode(83, 296, 2), "1:23 / -3:33")

    def test_position_zero(self):
        self.assertEqual(_format_timecode(0, 296, 0), "0:00 / 4:56")
        self.assertEqual(_format_timecode(0, 296, 1), "-4:56 / 4:56")
        self.assertEqual(_format_timecode(0, 296, 2), "0:00 / -4:56")

    def test_position_equals_duration_no_negative_zero(self):
        self.assertEqual(_format_timecode(296, 296, 1), "0:00 / 4:56")
        self.assertEqual(_format_timecode(296, 296, 2), "4:56 / 0:00")

    def test_sub_second_remainder_is_not_negative_zero(self):
        """_fmt truncates, so a remainder under a second formats as "0:00".
        Guarding on `remaining == 0` therefore still produced "-0:00" for the
        whole final second of every track -- i.e. once per play, not a corner
        case. The guard has to be on the FORMATTED value, not the raw float."""
        for remaining in (0.4, 0.9, 0.99):
            with self.subTest(remaining=remaining):
                pos = 296 - remaining
                self.assertEqual(_format_timecode(pos, 296, 1), "0:00 / 4:56")
                self.assertNotIn("-0:00", _format_timecode(pos, 296, 2))

    def test_duration_zero_no_track_loaded(self):
        self.assertEqual(_format_timecode(0, 0, 0), "0:00 / 0:00")
        self.assertEqual(_format_timecode(0, 0, 1), "0:00 / 0:00")
        self.assertEqual(_format_timecode(0, 0, 2), "0:00 / 0:00")

    def test_both_mode_halves_tick_on_same_step(self):
        """dp-233: elapsed and remaining are independently truncated floats
        that cross their integer boundaries at different instants -- the
        skew is `(1 - frac(duration)) mod 1`, worst at x.4 durations. Walk
        each track at sub-second steps and assert both halves of "both"
        mode change on the SAME step, never staggered."""
        for dur in (200.0, 200.4, 200.9):
            with self.subTest(dur=dur):
                pos = 0.0
                prev_text = _format_timecode(pos, dur, 2)
                pos += 0.05
                while pos < dur:
                    text = _format_timecode(pos, dur, 2)
                    if text != prev_text:
                        prev_elapsed, prev_remaining = prev_text.split(" / ")
                        elapsed, remaining = text.split(" / ")
                        changed = (elapsed != prev_elapsed, remaining != prev_remaining)
                        self.assertEqual(
                            changed,
                            (True, True),
                            f"dur={dur} pos={pos:.2f}: {prev_text!r} -> {text!r} "
                            "- only one half changed, halves are skewed",
                        )
                    prev_text = text
                    pos += 0.05

    def test_both_mode_never_shows_negative_zero(self):
        for dur in (200.0, 200.4, 200.9):
            with self.subTest(dur=dur):
                pos = 0.0
                while pos < dur:
                    self.assertNotIn("-0:00", _format_timecode(pos, dur, 2))
                    pos += 0.05


class TestTimecodeClickCycle(unittest.TestCase):

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()

    def test_cycle_order_wraps_after_third_click(self):
        self.assertEqual(self.window._timecode_mode, 0)
        self.window._lbl_time.sig_clicked.emit()
        self.assertEqual(self.window._timecode_mode, 1)
        self.window._lbl_time.sig_clicked.emit()
        self.assertEqual(self.window._timecode_mode, 2)
        self.window._lbl_time.sig_clicked.emit()
        self.assertEqual(self.window._timecode_mode, 0)

    def test_left_click_advances_mode_and_updates_label(self):
        orig_position = type(engine).position.fget
        orig_duration = type(engine).duration.fget
        try:
            type(engine).position = property(lambda self: 83.0)
            type(engine).duration = property(lambda self: 296.0)
            self.window._refresh_ui()
            before = self.window._lbl_time.text()
            self.window._lbl_time.mousePressEvent(_left_click_event())
            self.assertEqual(self.window._timecode_mode, 1)
            self.assertNotEqual(self.window._lbl_time.text(), before)
        finally:
            type(engine).position = property(orig_position)
            type(engine).duration = property(orig_duration)

    def test_right_click_does_not_advance_mode(self):
        self.window._lbl_time.mousePressEvent(_right_click_event())
        self.assertEqual(self.window._timecode_mode, 0)

    def test_magnifier_popup_matches_label_in_each_mode(self):
        self.window._lbl_time.sig_hover_enter.emit()
        for _ in range(3):
            self.window._lbl_time.sig_clicked.emit()
            self.assertEqual(
                self.window._timecode_popup.text(), self.window._lbl_time.text()
            )


if __name__ == "__main__":
    unittest.main()

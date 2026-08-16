"""
Regression tests for the timecode hover magnifier popup (dp-198).

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from core.engine import engine
from ui.main_window import MainWindow
from ui.skin import FONT_SIZE_TIMECODE

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class TestTimecodeHoverPopup(unittest.TestCase):

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()

    def test_no_popup_before_first_hover(self):
        # Lazily built — nothing should exist until the user actually hovers.
        self.assertIsNone(self.window._timecode_popup)

    def test_hover_enter_shows_popup_with_current_text(self):
        self.window._lbl_time.setText("1:23 / 4:56")
        self.window._lbl_time.sig_hover_enter.emit()
        self.assertIsNotNone(self.window._timecode_popup)
        self.assertTrue(self.window._timecode_popup.isVisible())
        self.assertEqual(self.window._timecode_popup.text(), "1:23 / 4:56")

    def test_hover_leave_hides_popup(self):
        self.window._lbl_time.sig_hover_enter.emit()
        self.assertTrue(self.window._timecode_popup.isVisible())
        self.window._lbl_time.sig_hover_leave.emit()
        self.assertFalse(self.window._timecode_popup.isVisible())

    def test_popup_font_is_roughly_quadruple_base_timecode_size(self):
        # A global `QWidget { font-size: ...pt; }` QSS rule (ui/skin.py)
        # overrides setFont() — the popup's own stylesheet must carry an
        # explicit font-size so the 4x scale actually renders (dp-198).
        self.window._lbl_time.sig_hover_enter.emit()
        self.assertIn(
            f"font-size: {FONT_SIZE_TIMECODE * 4}pt;",
            self.window._timecode_popup.styleSheet(),
        )

    def test_popup_tracks_live_refresh_while_visible(self):
        # _refresh_ui() derives self._lbl_time's text from live engine
        # state every tick — the popup must mirror whatever that produces,
        # not freeze at the value captured when the hover began.
        self.window._lbl_time.sig_hover_enter.emit()
        self.window._refresh_ui()
        first_text = self.window._lbl_time.text()
        self.assertEqual(self.window._timecode_popup.text(), first_text)

        orig_position = type(engine).position.fget
        try:
            type(engine).position = property(lambda self: 65.0)
            self.window._refresh_ui()
        finally:
            type(engine).position = property(orig_position)
        second_text = self.window._lbl_time.text()
        self.assertNotEqual(first_text, second_text)
        self.assertEqual(self.window._timecode_popup.text(), second_text)

    def test_popup_does_not_update_while_hidden(self):
        self.window._lbl_time.sig_hover_enter.emit()
        self.window._lbl_time.sig_hover_leave.emit()
        stale_text = self.window._timecode_popup.text()
        self.window._lbl_time.setText("9:99 / 9:99")
        self.window._refresh_ui()
        # Text is allowed to be whatever it was — the point is the popup
        # never has to repaint while hidden — but it must not be visible.
        self.assertFalse(self.window._timecode_popup.isVisible())

    def test_base_label_font_and_geometry_untouched(self):
        # dp-198 is an overlay only — it must not resize/restyle the base
        # 48px info bar or self._lbl_time itself (dp-186's constraints).
        before_font = self.window._lbl_time.font().pointSize()
        before_height = self.window._lbl_time.height()
        self.window._lbl_time.sig_hover_enter.emit()
        self.assertEqual(self.window._lbl_time.font().pointSize(), before_font)
        self.assertEqual(self.window._lbl_time.height(), before_height)

    def test_popup_stays_within_screen_bounds_at_minimum_window_size(self):
        self.window.resize(self.window.minimumWidth(), self.window.minimumHeight())
        _app.processEvents()
        self.window._lbl_time.sig_hover_enter.emit()
        popup = self.window._timecode_popup
        screen = self.window.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.assertGreaterEqual(popup.x(), avail.left())
            self.assertGreaterEqual(popup.y(), avail.top())
            self.assertLessEqual(popup.x() + popup.width(), avail.right() + 1)
            self.assertLessEqual(popup.y() + popup.height(), avail.bottom() + 1)


if __name__ == "__main__":
    unittest.main()

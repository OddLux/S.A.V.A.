"""dp-240: the info bar's widget order and the window's resize floor.

Target order: [title] <stretch> [buffering] [gap] [Track x of y] [gap] [time]
-- buffering moved left of the track counter, counter moved right next to
the timecode with a visibly tighter gap than the gap before it.

Plain unittest, no pytest dependency:
    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QLayout, QWidget

from ui.main_window import MainWindow

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class TestInfoBarLayoutOrder(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()

    def _info_lay(self):
        # The info bar is the QFrame directly wrapping _lbl_title.
        return self.window._lbl_title.parentWidget().layout()

    def test_widget_order_matches_target(self):
        # DISCRIMINATION CHECK: fails against pre-dp-240 master, where the
        # order is title/stretch/counter/buffering/time (buffering sits
        # BETWEEN counter and timecode instead of left of it). Verified by
        # reverting the addWidget/addSpacing block in main_window.py and
        # watching this fail, then restoring it.
        lay = self._info_lay()
        widgets = []
        for i in range(lay.count()):
            item = lay.itemAt(i)
            w = item.widget()
            if w is not None:
                widgets.append(w)
        self.assertEqual(
            widgets,
            [
                self.window._lbl_title,
                self.window._lbl_buffering,
                self.window._lbl_info,
                self.window._lbl_time,
            ],
        )

    def test_gap_before_counter_wider_than_gap_before_timecode(self):
        lay = self._info_lay()
        spacer_widths = []
        for i in range(lay.count()):
            item = lay.itemAt(i)
            if item.widget() is None and item.spacerItem() is not None:
                spacer_widths.append(item.spacerItem().sizeHint().width())
        # Excludes the stretch spacer (reports width 0 -- it expands via
        # QSizePolicy, not a fixed sizeHint). The remaining two are the
        # addSpacing(16) before the counter and addSpacing(6) before the
        # timecode, in that order.
        fixed_spacings = [w for w in spacer_widths if w > 0]
        self.assertEqual(len(fixed_spacings), 2)
        gap_before_counter, gap_before_time = fixed_spacings
        self.assertGreater(gap_before_counter, gap_before_time)

    def test_buffering_indicator_still_fixed_width_and_borderless(self):
        self.assertEqual(self.window._lbl_buffering.width(), 200)
        self.assertIn("border: none", self.window._lbl_buffering.styleSheet())

    def test_resize_floor_unchanged(self):
        # dp-185: floor is set explicitly via setMinimumSize(max(640,
        # transport.minimumWidth()), 520) -- reordering info-bar widgets
        # (which don't set a minimum width themselves) must not move it.
        expected_w = max(640, self.window._transport.minimumWidth())
        self.assertEqual(self.window.minimumWidth(), expected_w)
        self.assertEqual(self.window.minimumHeight(), 520)


if __name__ == "__main__":
    unittest.main()

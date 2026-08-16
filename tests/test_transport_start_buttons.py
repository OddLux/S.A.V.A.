"""dp-232: Track Start marker Set/Clear buttons, mirroring the existing
Track End (Fin) marker's Set/Clear buttons in the same "Track End" group.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from ui.transport_widget import TransportWidget  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

# dp-185's pinned resize floor, re-asserted here because dp-232 adds two
# buttons to a group whose width the floor depends on. The scheme chosen
# (Start row reuses row 0's exact "Set"/"Clear" column widths) keeps the
# group's width -- and therefore this ceiling -- unchanged.
_RESIZE_FLOOR_CEILING = 1093


class TestStartMarkerButtons(unittest.TestCase):

    def setUp(self):
        self.widget = TransportWidget()

    def _find_start_buttons(self):
        return [
            b for b in self.widget.findChildren(QPushButton)
            if b.toolTip().startswith("Set the start marker")
            or b.toolTip().startswith("Clear the start marker")
        ]

    def test_start_buttons_exist_and_are_not_cropped(self):
        btns = self._find_start_buttons()
        self.assertEqual(len(btns), 2)
        for btn in btns:
            self.assertGreaterEqual(btn.width(), btn.sizeHint().width())

    def test_start_set_signal_emits(self):
        seen = []
        self.widget.sig_start_set.connect(lambda: seen.append(True))
        for btn in self._find_start_buttons():
            if btn.toolTip().startswith("Set the start marker"):
                btn.click()
        self.assertTrue(seen)

    def test_start_clear_signal_emits(self):
        seen = []
        self.widget.sig_start_clear.connect(lambda: seen.append(True))
        for btn in self._find_start_buttons():
            if btn.toolTip().startswith("Clear the start marker"):
                btn.click()
        self.assertTrue(seen)

    def test_transport_resize_floor_did_not_grow(self):
        # This is the headline width guard for dp-232: the Start row reuses
        # row 0's column widths (same "Set"/"Clear" text), so the group and
        # therefore the whole transport row's sizeHint must not have grown
        # past the accepted 1093px ceiling.
        self.assertLessEqual(
            self.widget.sizeHint().width(), _RESIZE_FLOOR_CEILING
        )


if __name__ == "__main__":
    unittest.main()


class TestMarkerRowOrder(unittest.TestCase):
    """dp-254: Start sits ABOVE End.

    A track's start point precedes its end point, so the group should read
    top-to-bottom in the order the markers actually occur. dp-232 had them
    reversed.

    Asserted on GEOMETRY (actual y positions of the real buttons), not on the
    order widgets were added to the layout -- addWidget order and rendered
    position are different things, and only the second one is what the user
    sees.
    """

    def setUp(self):
        from PyQt6.QtWidgets import QGroupBox

        self.widget = TransportWidget()
        # show() is required, not cosmetic: Qt does not run layout until the
        # widget is shown, so every child's y() stays 0 until then -- the
        # geometry assertions below would compare 0 against 0 and pass
        # vacuously in BOTH orderings, testing nothing. Safe here: the suite
        # runs under QT_QPA_PLATFORM=offscreen, so nothing hits a screen.
        self.widget.show()
        _app.processEvents()
        self.groups = {g.title(): g for g in self.widget.findChildren(QGroupBox)}

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def _marker_group(self):
        self.assertIn("Track Start", self.groups)
        return self.groups["Track Start"]

    def _by_tooltip(self, prefix):
        return [
            b for b in self._marker_group().findChildren(QPushButton)
            if b.toolTip().startswith(prefix)
        ]

    def test_start_buttons_are_above_end_buttons(self):
        start = self._by_tooltip("Set the start marker")
        end = self._by_tooltip("Set the end (Fin) marker")
        self.assertEqual(len(start), 1)
        self.assertEqual(len(end), 1)
        self.assertLess(
            start[0].y(), end[0].y(),
            "Track Start must render above Track End",
        )

    def test_the_group_title_captions_the_start_row(self):
        """The title and the inline caption must each stay attached to the
        pair they name -- swapping the rows without swapping the captions
        would silently mislabel both."""
        group = self._marker_group()
        start_set = self._by_tooltip("Set the start marker")[0]
        end_set = self._by_tooltip("Set the end (Fin) marker")[0]

        captions = {
            lbl.text(): lbl for lbl in group.findChildren(QLabel)
            if lbl.text() in ("Track Start", "Track End")
        }
        self.assertIn("Track End", captions, "inline caption should name the End row")
        self.assertNotIn(
            "Track Start", captions,
            "Track Start is the group TITLE, not an inline label",
        )
        # The inline "Track End" caption sits between the two button rows.
        self.assertLess(start_set.y(), captions["Track End"].y())
        self.assertLess(captions["Track End"].y(), end_set.y())

    def test_all_four_marker_buttons_still_present_and_uncropped(self):
        """Regression guard: the swap must not drop or crop a button."""
        group = self._marker_group()
        buttons = group.findChildren(QPushButton)
        self.assertEqual(len(buttons), 4)
        for btn in buttons:
            with self.subTest(tooltip=btn.toolTip()):
                self.assertGreaterEqual(btn.width(), btn.sizeHint().width())

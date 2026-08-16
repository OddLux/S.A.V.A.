"""dp-230: every Loop A to B button was narrower than its own label needed.

`_build_loop_group` fixed all four at `BW = 72`. Measured against Qt's own
`QPushButton.sizeHint()` -- which uses the real style padding rather than an
estimate -- "Set A"/"Set B"/"Clear" each needed 82px and the Enable/Disable
toggle needed 94px. Same defect class as dp-228's Fin buttons: a width chosen
once for one set of labels and kept after the labels changed.

The toggle is the important case: it rewrites its own text at runtime
(`_on_loop_toggle`), so the label it must fit is the WIDEST it will ever show,
not the one it happens to start with.

User decision 2026-08-01, option (a): widen the loop group and accept that it
no longer matches the Track End group's width. dp-229 had deliberately sized
those two as a matched pair; correctness of the labels wins over the pairing.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGroupBox, QPushButton,
)

from ui.transport_widget import TransportWidget  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

# dp-229 raised the transport row's accepted resize floor to this. dp-230 must
# not push it further: the loop group grows, but Cue Points carries the row's
# stretch and gives the width back.
_ACCEPTED_SIZE_HINT_CEILING = 1093


class TestLoopButtonsFitTheirLabels(unittest.TestCase):

    def setUp(self):
        self.widget = TransportWidget()
        self.widget.resize(1400, 260)
        self.widget.show()
        _app.processEvents()
        self.group = next(
            g for g in self.widget.findChildren(QGroupBox)
            if g.title() == "Loop A to B"
        )

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def test_no_loop_button_is_narrower_than_qt_says_it_needs(self):
        """Asserts against Qt's own sizeHint, not a padding estimate -- the
        estimate is what let this slip through in the first place."""
        for btn in self.group.findChildren(QPushButton):
            with self.subTest(label=btn.text()):
                self.assertGreaterEqual(btn.width(), btn.sizeHint().width())

    def test_toggle_fits_its_widest_label_not_just_its_initial_one(self):
        """The regression that mattered: the button starts as "Enable" (94px
        needed) and becomes "Disable" (106px) the moment a loop is turned on.
        Sizing for the initial label alone crops the other state."""
        toggle = self.widget._btn_loop_tog
        self.assertEqual(toggle.text(), "Enable")

        toggle.setText("Disable")
        _app.processEvents()

        self.assertGreaterEqual(toggle.width(), toggle.sizeHint().width())

    def test_toggle_does_not_resize_when_its_label_changes(self):
        """A control that changes width under the cursor as you click it is
        its own bug -- pre-sizing for the widest label prevents that."""
        toggle = self.widget._btn_loop_tog
        before = toggle.width()
        toggle.setText("Disable")
        _app.processEvents()
        self.assertEqual(toggle.width(), before)

    def test_widths_are_measured_not_hardcoded(self):
        """Bumping the font must move the widths. This fails against the old
        `setFixedSize(72, 28)`, which never re-measures."""
        for btn in self.group.findChildren(QPushButton):
            with self.subTest(label=btn.text()):
                before = btn.minimumWidth()
                font = btn.font()
                font.setPointSize(font.pointSize() + 8)
                btn.setFont(font)
                _app.processEvents()
                self.assertGreater(btn.sizeHint().width(), before)


class TestLoopWideningDidNotCostRowWidth(unittest.TestCase):

    def test_transport_min_width_did_not_grow_past_the_dp229_ceiling(self):
        """The loop group widens, but Cue Points holds the row's stretch and
        surrenders the difference, so the window's resize floor is unchanged."""
        widget = TransportWidget()
        try:
            self.assertLessEqual(
                widget.sizeHint().width(), _ACCEPTED_SIZE_HINT_CEILING
            )
        finally:
            widget.deleteLater()
            _app.processEvents()


if __name__ == "__main__":
    unittest.main()

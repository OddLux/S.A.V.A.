"""dp-228: Set Fin / Clear Fin buttons cropped their labels and wasted
horizontal space.

`_build_cue_group`'s right-hand zone sized both Fin buttons with
`setFixedSize(58, 28)` -- 58px was chosen for the "Cue N" buttons and did
not fit "Clear Fin" at FONT_SIZE_SMALL. They also sat under a trailing
addStretch(1) that padded the zone taller than the two buttons need.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtGui import QFontMetrics  # noqa: E402
from PyQt6.QtWidgets import QApplication, QGroupBox, QPushButton  # noqa: E402

from ui.transport_widget import TransportWidget  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

# Pre-dp-228 baseline. The old code fixed both buttons at 58px regardless
# of label -- which is what cropped "Clear Fin". To compare footprints
# honestly, this is the column width a *non-cropping* version of the old
# labels ("Set Fin" / "Clear Fin") would have needed, measured the same
# way _mk_btn measures (QFontMetrics + _BTN_PAD_W): max(105, 127) = 127.
# TransportWidget().sizeHint().width() was 1075 on this build -- measured
# directly against the pre-dp-228 commit (afd4c16) -- and must not grow.
_PRE_FIX_UNCROPPED_ZONE_WIDTH = 127
_PRE_FIX_SIZE_HINT_WIDTH = 1075
# dp-229: +18px for Fin's own group frame plus a row-2 spacing. See
# test_transport_min_width_is_within_the_accepted_ceiling for why this moved.
_DP229_SIZE_HINT_CEILING = _PRE_FIX_SIZE_HINT_WIDTH + 18


class TestFinButtonsDoNotCropAndShrinkFootprint(unittest.TestCase):

    def setUp(self):
        self.widget = TransportWidget()

    def test_fin_buttons_are_not_cropped(self):
        for btn in self._find_fin_buttons():
            fm = QFontMetrics(btn.font())
            needed = fm.horizontalAdvance(btn.text()) + self.widget._BTN_PAD_W
            self.assertGreaterEqual(btn.width(), needed)

    def test_fin_buttons_stay_uncropped_at_larger_font(self):
        # Proves width comes from measurement, not a hardcoded constant --
        # this would fail against the old setFixedSize(58, 28) code, which
        # never re-measures for a bigger font.
        for btn in self._find_fin_buttons():
            font = btn.font()
            font.setPointSize(font.pointSize() + 6)
            btn.setFont(font)
            fm = QFontMetrics(font)
            needed = fm.horizontalAdvance(btn.text()) + self.widget._BTN_PAD_W
            self.assertGreaterEqual(btn.width(), needed)

    def test_fin_zone_footprint_shrunk(self):
        # dp-229 moved Fin into its own "Track End" group box, so the two
        # buttons now sit side by side in a QGridLayout rather than stacked.
        # The zone's width is therefore the sum of both, and it must still
        # beat what an uncropped version of the ORIGINAL stacked layout
        # ("Set Fin" over "Clear Fin", 127px column) would have cost twice.
        btns = self._find_fin_buttons()
        self.assertEqual(len(btns), 2)
        zone_width = sum(b.sizeHint().width() for b in btns)
        self.assertLess(zone_width, _PRE_FIX_UNCROPPED_ZONE_WIDTH * 2)

    def test_transport_min_width_is_within_the_accepted_ceiling(self):
        # dp-228 asserted <= 1075 (the pre-dp-228 value) because that change
        # was a pure shrink. dp-229 then promoted Fin from a column inside
        # the Cue Points box to its own sibling group box, at the user's
        # request ("make the Fin section roughly the same size as the AB loop
        # section and make the Cues section bigger"). That costs one extra
        # group frame plus a row-2 spacing: +18px on the row's minimum.
        #
        # Deliberate, accepted trade -- not drift. The dp-185 invariant this
        # test protects is that the transport row must not creep the window's
        # resize floor UNNOTICED. Raising the ceiling with this note keeps the
        # guard meaningful; deleting the assertion would not.
        self.assertLessEqual(
            self.widget.sizeHint().width(), _DP229_SIZE_HINT_CEILING
        )

    def test_fin_set_signal_emits(self):
        seen = []
        self.widget.sig_fin_set.connect(lambda: seen.append(True))
        for btn in self._find_fin_buttons():
            if btn.toolTip().startswith("Set the end"):
                btn.click()
        self.assertTrue(seen)

    def test_fin_clear_signal_emits(self):
        seen = []
        self.widget.sig_fin_clear.connect(lambda: seen.append(True))
        for btn in self._find_fin_buttons():
            if btn.toolTip().startswith("Clear the end"):
                btn.click()
        self.assertTrue(seen)

    def _find_fin_buttons(self):
        return [
            b for b in self.widget.findChildren(QPushButton)
            if b.toolTip().startswith("Set the end (Fin)")
            or b.toolTip().startswith("Clear the end (Fin)")
        ]




class TestFinIsItsOwnGroupSizedLikeLoop(unittest.TestCase):
    """dp-229 (user request): "make the Fin section roughly the same size as
    the AB loop section and make the Cues section bigger". Fin moved out of
    the Cue Points box into its own sibling group, built with the same
    geometry as the loop group so the two read as a matched pair."""

    def setUp(self):
        self.widget = TransportWidget()
        self.widget.resize(1400, 260)
        self.widget.show()
        _app.processEvents()
        self.groups = {g.title(): g for g in self.widget.findChildren(QGroupBox)}

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        _app.processEvents()

    def test_fin_has_its_own_group_box(self):
        # dp-254: Start took over the group title when the rows swapped.
        self.assertIn("Track Start", self.groups)

    def test_fin_group_is_sized_to_its_own_content(self):
        """dp-229 asserted Track End and Loop A to B were within 20px of each
        other -- a matched pair, which is what the user asked for at the time.
        dp-230 then found every loop button was narrower than its label needed
        ("Disable" wanted 106px and had 72), and the user chose option (a):
        widen the loop group and accept the pair no longer matches. Labels
        that fit beat two boxes being the same width.

        So the surviving invariant is not "these two match" but "each group is
        sized by its own content, and nothing crops". The matched-pair
        assertion is deliberately gone, not accidentally lost.

        dp-232: the group now holds 4 buttons (Fin Set/Clear on row 0, Start
        Set/Clear on row 1) -- the previously-empty stretch row is now real
        content, same column widths as row 0, so the group's width is
        unchanged even though its button count doubled.
        """
        fin_group = self.groups["Track Start"]
        buttons = fin_group.findChildren(QPushButton)
        self.assertEqual(len(buttons), 4)
        for btn in buttons:
            with self.subTest(label=btn.text()):
                self.assertGreaterEqual(btn.width(), btn.sizeHint().width())

    def test_cue_group_is_much_larger_than_both(self):
        cues = self.groups["Cue Points"].width()
        self.assertGreater(cues, self.groups["Loop A to B"].width() * 3)
        self.assertGreater(cues, self.groups["Track Start"].width() * 3)

    def test_fin_buttons_are_no_longer_inside_the_cue_group(self):
        """The ambiguity this also fixes: a bare "Clear" used to sit in the
        same box as "Clear All Cues"."""
        cue_group = self.groups["Cue Points"]
        fin_texts = {
            b.text() for b in cue_group.findChildren(QPushButton)
            if b.toolTip().startswith(("Set the end (Fin)", "Clear the end (Fin)"))
        }
        self.assertEqual(fin_texts, set())

    def test_cue_buttons_expand_into_the_reclaimed_width(self):
        """Previously fixed at 58px, so the group stretched while its buttons
        stayed pinned in the top-left corner."""
        widest = max(b.width() for b in self.widget._cue_btns)
        self.assertGreater(widest, 58)


if __name__ == "__main__":
    unittest.main()

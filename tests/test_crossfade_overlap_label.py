"""dp-215: the M:SS duration label drawn inside each crossfade overlap band.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QGraphicsSimpleTextItem  # noqa: E402

from core.crossfade_model import CrossfadeLayout, LayoutTrack  # noqa: E402
from ui.crossfade_timeline_widget import (  # noqa: E402
    OVERLAP_LABEL_Z,
    CrossfadeTimelineWidget,
)

_app = QApplication.instance() or QApplication(sys.argv)


def _make_layout(durations, overlaps):
    layout = CrossfadeLayout(
        [LayoutTrack(filepath=f"t{i}.wav", duration=d) for i, d in enumerate(durations)]
    )
    for i, ov_dur in enumerate(overlaps):
        layout.set_overlap_duration(i, ov_dur)
    return layout


class _LabelCase(unittest.TestCase):
    def setUp(self):
        self.widget = CrossfadeTimelineWidget()

    def _render(self, layout, pps=20.0):
        self.widget.set_layout(layout)
        self.widget._pps = pps
        self.widget._render()
        _app.processEvents()

    def _duration_labels(self):
        """Text items at the overlap-label z -- distinguishes them from
        dp-168's track-name labels and dp-214's ruler labels, which share the
        QGraphicsSimpleTextItem type but sit at different z-values."""
        return [
            item
            for item in self.widget._scene.items()
            if isinstance(item, QGraphicsSimpleTextItem)
            and item.zValue() == OVERLAP_LABEL_Z
        ]


class TestOverlapDurationLabel(_LabelCase):

    def test_label_shows_overlap_duration_as_m_ss(self):
        self._render(_make_layout([200.0, 200.0], [95.0]))

        labels = self._duration_labels()
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].text(), "1:35")

    def test_label_sits_inside_its_overlap_rect(self):
        self._render(_make_layout([200.0, 200.0], [95.0]))

        rect = self.widget._overlap_rect(0)
        label = self._duration_labels()[0]
        bounds = label.sceneBoundingRect()
        self.assertGreaterEqual(bounds.left(), rect.left())
        self.assertLessEqual(bounds.right(), rect.right())
        self.assertLessEqual(bounds.bottom(), rect.bottom())

    def test_label_follows_a_drag_resize(self):
        """The live-update AC: the label must come from _render_impl's own
        rebuild, not a separately-triggered refresh that can go stale."""
        layout = _make_layout([200.0, 200.0], [95.0])
        self._render(layout)
        self.assertEqual(self._duration_labels()[0].text(), "1:35")

        layout.set_overlap_duration(0, 30.0)
        self.widget._render()
        _app.processEvents()

        self.assertEqual(self._duration_labels()[0].text(), "0:30")

    def test_one_label_per_nonzero_overlap(self):
        self._render(_make_layout([200.0, 200.0, 200.0], [95.0, 60.0]))

        self.assertEqual(
            sorted(item.text() for item in self._duration_labels()),
            ["1:00", "1:35"],
        )


class TestOverlapDurationLabelOmission(_LabelCase):

    def test_no_label_for_a_zero_overlap(self):
        self._render(_make_layout([200.0, 200.0], [0.0]))

        self.assertEqual(self._duration_labels(), [])

    def test_no_label_when_the_band_is_too_narrow_for_the_text(self):
        """Omitted rather than elided: half a duration is worse than none."""
        self._render(_make_layout([200.0, 200.0], [2.0]), pps=1.0)

        self.assertEqual(self._duration_labels(), [])

    def test_nothing_escapes_the_band_at_low_zoom(self):
        layout = _make_layout([600.0, 600.0, 600.0], [5.0, 240.0])
        self._render(layout, pps=0.5)

        for label in self._duration_labels():
            bounds = label.sceneBoundingRect()
            inside_any = any(
                (rect := self.widget._overlap_rect(i)) is not None
                and bounds.left() >= rect.left()
                and bounds.right() <= rect.right()
                for i in range(len(layout.overlaps))
            )
            self.assertTrue(inside_any, f"{label.text()} escaped its overlap band")


if __name__ == "__main__":
    unittest.main()

"""dp-235: the played/unplayed overlay must align with the waveform beneath it.

`_draw_waveform` blits the bright envelope with `drawPixmap(0, 0, pm)` and then
draws the darkened copy over the played span. It used to draw that copy with the
`drawPixmap(target, pixmap, source)` overload, whose `source` rect is read in
different units than the plain blit uses. On a display with
devicePixelRatio != 1 the two therefore landed at DIFFERENT scales, and the
darkened copy appeared as a visibly mis-sized second waveform on top of the
first (user report, 2026-08-02, on a 1.5x display).

The invariant under test is alignment: whatever scale the base is drawn at, the
overlay must be drawn at exactly the same one. The stripe pattern varies along
the vertical axis so a scale mismatch is detectable -- a flat pattern renders
identically whether or not the bug is present, which is how an earlier version
of this test passed against the bug.
"""

import unittest

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

W, H = 200, 50
NEEDLE_X = 100


def _striped(dpr, top_color, bottom_color):
    """Pixmap split horizontally: top QUARTER `top_color`, rest `bottom_color`.

    A quarter rather than a half so the colour boundary stays inside the
    canvas even when the content ends up scaled by the largest dpr tested --
    at a half boundary and dpr 2 it lands exactly on the bottom edge and
    becomes undetectable, which makes the test look aligned for the wrong
    reason."""
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor(bottom_color))
    painter = QPainter(pm)
    painter.fillRect(
        QRect(0, 0, int(W * dpr), int(H * dpr / 4)), QColor(top_color)
    )
    painter.end()
    return pm


def _canvas(dpr):
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor("white"))
    return pm


def _boundary_y(img, x):
    """Physical y of the first row at `x` that differs from the top row."""
    top = img.pixelColor(x, 0).rgb()
    for y in range(img.height()):
        if img.pixelColor(x, y).rgb() != top:
            return y
    return -1


def _render(dpr, overlay_with_source_rect):
    """Draw base full-width, then the overlay across the played span."""
    canvas = _canvas(dpr)
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, _striped(dpr, "red", "blue"))
    overlay = _striped(dpr, "green", "black")
    if overlay_with_source_rect:
        rect = QRect(0, 0, NEEDLE_X, H)
        painter.drawPixmap(rect, overlay, rect)
    else:
        painter.save()
        painter.setClipRect(QRect(0, 0, NEEDLE_X, H))
        painter.drawPixmap(0, 0, overlay)
        painter.restore()
    painter.end()
    return canvas.toImage()


class TestWaveformOverlayAlignment(unittest.TestCase):
    def _assert_aligned(self, dpr):
        img = _render(dpr, overlay_with_source_rect=False)
        overlay_edge = _boundary_y(img, int(NEEDLE_X * dpr) // 2)
        base_edge = _boundary_y(img, img.width() - 5)
        self.assertNotEqual(overlay_edge, -1, "overlay never changed color")
        self.assertNotEqual(base_edge, -1, "base never changed color")
        self.assertEqual(
            overlay_edge, base_edge,
            f"dpr={dpr}: overlay split at y={overlay_edge}, "
            f"base at y={base_edge} -- overlay is drawn at a different scale",
        )

    def test_overlay_aligns_with_base_at_dpr_1(self):
        self._assert_aligned(1.0)

    def test_overlay_aligns_with_base_at_dpr_1_5(self):
        self._assert_aligned(1.5)

    def test_overlay_aligns_with_base_at_dpr_2(self):
        self._assert_aligned(2.0)

    def test_overlay_stops_at_the_needle(self):
        """The base must remain untouched to the right of the playhead."""
        for dpr in (1.0, 1.5, 2.0):
            img = _render(dpr, overlay_with_source_rect=False)
            right = img.pixelColor(img.width() - 5, 2).name()
            self.assertEqual(right, "#ff0000", f"dpr={dpr}: base was overpainted")

    def test_source_rect_overload_really_does_misalign(self):
        """Discrimination check: the old code must actually fail this.

        If this starts passing, Qt changed the overload's semantics and the
        alignment assertions above would no longer be guarding anything."""
        img = _render(1.5, overlay_with_source_rect=True)
        overlay_edge = _boundary_y(img, int(1.5 * NEEDLE_X) // 2)
        base_edge = _boundary_y(img, img.width() - 5)
        self.assertNotEqual(
            overlay_edge, base_edge,
            "old overload unexpectedly aligned -- guard is no longer meaningful",
        )


if __name__ == "__main__":
    unittest.main()

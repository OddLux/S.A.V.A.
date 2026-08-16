"""dp-218: pure waveform/marker paint helpers, ported verbatim-in-behavior
from WaveformWidget's private draw methods (ui/waveform_widget.py) so the
new read-only PreviewWaveformWidget can reuse the same visuals without
dragging along WaveformWidget's interactive (click/drag-to-seek) machinery.

No Qt widget state here -- every function takes a QPainter plus the data it
needs to draw. WaveformWidget itself is left untouched (no regression risk
on the interactive widget the user stares at all day).
"""

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QPixmap, QPolygon

from ui.skin import make_font


def time_to_x(pos: float, duration: float, width: int) -> int:
    """Map a position in seconds to a pixel x-coordinate."""
    if duration <= 0:
        return 0
    return int(pos / duration * width)


def render_envelope_pixmap(data, w: int, h: int, color, dpr: float) -> QPixmap:
    """dp-225: rasterize the waveform envelope ONCE into a QPixmap instead of
    replaying the per-pixel drawLine loop on every repaint. `dpr` is the
    caller's devicePixelRatioF() -- the pixmap is allocated at physical
    resolution and tagged with it so a plain `drawPixmap(0, 0, pm)` blits it
    back at the correct logical size, sharp on HiDPI displays.

    Callers own their own cache key (waveform generation + size + dpr +
    color) and only call this when that key changes -- this function itself
    does no caching.
    """
    pw = max(1, int(round(w * dpr)))
    ph = max(1, int(round(h * dpr)))
    pm = QPixmap(pw, ph)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    draw_waveform(painter, data, w, h, color)
    painter.end()
    return pm


def draw_waveform(painter, data, w: int, h: int, color: str):
    """Draw the RMS bar waveform. `color` is a skin C_* hex string."""
    n = len(data)
    if n == 0:
        return
    center = h // 2
    half = center - 2
    base = QColor(color)
    pen = QPen(base, 1)
    painter.setPen(pen)
    for px in range(w):
        idx = max(0, min(int(px / w * n), n - 1))
        amplitude = data[idx]
        bar_h = max(1, int(amplitude * half))
        painter.drawLine(px, center - bar_h, px, center + bar_h)


def draw_cue_ticks(painter, cues: dict, duration: float, w: int, h: int, color: str):
    """Draw cue point tick marks + index labels. `cues` is {index: pos}."""
    if duration <= 0:
        return
    col = QColor(color)
    for idx, pos in cues.items():
        if pos is None:
            continue
        x = time_to_x(pos, duration, w)
        painter.setPen(QPen(col, 1))
        painter.drawLine(x, 0, x, h)
        painter.setFont(make_font(7))
        painter.setPen(col)
        painter.drawText(x + 2, 10, str(idx + 1))


def draw_end_marker(painter, position, duration: float, w: int, h: int, color: str):
    """Draw the custom Fin end-marker, or nothing if unset."""
    if position is None or duration <= 0:
        return
    col = QColor(color)
    x = time_to_x(position, duration, w)
    painter.setPen(QPen(col, 2))
    painter.drawLine(x, 0, x, h)
    painter.setFont(make_font(7))
    painter.setPen(col)
    painter.drawText(x + 2, 10, "Fin")


def draw_start_marker(painter, position, duration: float, w: int, h: int, color: str):
    """Draw the custom Track Start marker, or nothing if unset. Mirrors
    `draw_end_marker`."""
    if position is None or duration <= 0:
        return
    col = QColor(color)
    x = time_to_x(position, duration, w)
    painter.setPen(QPen(col, 2))
    painter.drawLine(x, 0, x, h)
    painter.setFont(make_font(7))
    painter.setPen(col)
    painter.drawText(x + 2, 10, "Start")


def draw_crossfade_markers(
    painter, fade_in_end, fade_out_start, duration: float, w: int, h: int, color: str
):
    """Draw the dashed fade-in-end / fade-out-start markers with a
    bottom-edge triangle, distinct from cue/end markers."""
    if duration <= 0:
        return
    col = QColor(color)
    pen = QPen(col, 1, Qt.PenStyle.DashLine)
    for pos in (fade_in_end, fade_out_start):
        if pos is None:
            continue
        x = time_to_x(pos, duration, w)
        painter.setPen(pen)
        painter.drawLine(x, 0, x, h)
        painter.setBrush(QBrush(col))
        painter.setPen(Qt.PenStyle.NoPen)
        pts = [QPoint(x - 5, h), QPoint(x + 5, h), QPoint(x, h - 8)]
        painter.drawPolygon(QPolygon(pts))

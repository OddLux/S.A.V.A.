"""
Waveform visualizer widget.
- Draws a downsampled RMS waveform
- Shows a moving playhead needle
- Click or drag to seek
- Highlights the A→B loop region with A and B markers
- Shows cue point markers
"""

import numpy as np
from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore    import Qt, QRect, QPoint, pyqtSignal
from PyQt6.QtGui     import QPainter, QColor, QPen, QBrush, QPolygon

from ui.skin import (
    C_WAVEFORM_BG, C_WAVEFORM_FG, C_WAVEFORM_POS,
    C_WAVEFORM_LOOP, C_ACCENT_ORANGE, C_ACCENT_BLUE,
    C_TEXT_DIM, C_END_MARKER, C_START_MARKER, C_TIMELINE_OVERLAP,
    make_font
)
from ui.waveform_draw import render_envelope_pixmap


class WaveformWidget(QWidget):

    seek_requested = pyqtSignal(float)   # seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(80)
        self.setMaximumHeight(120)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._waveform   = None
        self._duration   = 0.0
        self._position   = 0.0
        self._loop_a     = None
        self._loop_b     = None
        self._cue_points = {}
        self._end_marker = None
        self._start_marker = None
        self._dragging   = False
        # dp-216 Phase 3 Part B: static crossfade fade-in/fade-out markers,
        # sourced from the saved CrossfadeLayout (core/crossfade_markers.py)
        # -- purely visual, no seek/interaction.
        self._fade_in_end = None
        self._fade_out_start = None

        # dp-225: envelope pixmap cache. `_env_gen` bumps on every
        # set_waveform() call (a fresh array may reuse a prior id(), so we
        # don't key on identity) -- the cache key also carries widget size,
        # devicePixelRatio, and the active waveform color, so a resize or a
        # HiDPI screen change rebuilds it too. Theme colors are process-
        # lifetime constants (theme switch requires an app restart per
        # _on_theme_selected in main_window.py), so no separate theme-change
        # hook is needed -- including the color in the key is just cheap
        # insurance against that assumption ever changing.
        self._env_gen        = 0
        self._env_cache_key  = None
        self._env_bright_pm  = None
        self._env_dark_pm    = None
        # dp-235: the cache key carries devicePixelRatio, but nothing forces
        # a repaint when the window is dragged between screens of different
        # DPR (this machine has a 1.5x and a 1.0x display). Without this the
        # envelope keeps its old scale until some other event repaints --
        # visible while paused, when the 10 Hz position tick isn't running.
        self._screen_hooked = False

        self._bg       = QColor(C_WAVEFORM_BG)
        self._fg       = QColor(C_WAVEFORM_FG)
        self._needle   = QColor(C_WAVEFORM_POS)
        self._loop_col = QColor(C_WAVEFORM_LOOP)
        self._loop_col.setAlpha(60)
        self._cue_col  = QColor(C_ACCENT_ORANGE)
        self._end_col  = QColor(C_END_MARKER)
        self._start_col = QColor(C_START_MARKER)
        self._loop_marker_col = QColor(C_ACCENT_BLUE)
        self._crossfade_marker_col = QColor(C_TIMELINE_OVERLAP)

    # ── Public setters ────────────────────────────────────────────────────────

    def set_waveform(self, data: np.ndarray, duration: float):
        self._waveform = data
        self._duration = duration
        self._position = 0.0
        self._env_gen += 1
        self.update()

    def set_position(self, position_sec: float):
        self._position = position_sec
        self.update()

    def set_loop_points(self, a, b):
        self._loop_a = a
        self._loop_b = b
        self.update()

    def set_cue_points(self, cue_dict: dict):
        self._cue_points = dict(cue_dict)
        self.update()

    def set_end_marker(self, position):
        self._end_marker = position
        self.update()

    def clear_end_marker(self):
        self._end_marker = None
        self.update()

    def set_start_marker(self, position):
        self._start_marker = position
        self.update()

    def clear_start_marker(self):
        self._start_marker = None
        self.update()

    def set_crossfade_markers(self, fade_in_end, fade_out_start):
        self._fade_in_end = fade_in_end
        self._fade_out_start = fade_out_start
        self.update()

    def clear(self):
        self._waveform   = None
        self._duration   = 0.0
        self._position   = 0.0
        self._loop_a     = None
        self._loop_b     = None
        self._cue_points = {}
        self._fade_in_end = None
        self._fade_out_start = None
        self._env_gen += 1
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()

        painter.fillRect(0, 0, w, h, self._bg)

        if self._waveform is None or self._duration == 0:
            self._draw_empty(painter, w, h)
            painter.end()
            return

        # Loop region fill
        if self._loop_a is not None and self._loop_b is not None:
            xa = int(self._loop_a / self._duration * w)
            xb = int(self._loop_b / self._duration * w)
            painter.fillRect(xa, 0, xb - xa, h, self._loop_col)

        self._draw_waveform(painter, w, h)
        self._draw_loop_markers(painter, w, h)
        self._draw_cues(painter, w, h)
        self._draw_end_marker(painter, w, h)
        self._draw_track_start_marker(painter, w, h)
        self._draw_crossfade_markers(painter, w, h)

        # Playhead
        needle_x = int(self._position / self._duration * w)
        painter.setPen(QPen(self._needle, 2))
        painter.drawLine(needle_x, 0, needle_x, h)

        self._draw_time_labels(painter, w, h)
        painter.end()

    def _draw_empty(self, painter, w, h):
        painter.setPen(QColor(C_TEXT_DIM))
        painter.setFont(make_font(8))
        painter.drawText(QRect(0, 0, w, h),
                         Qt.AlignmentFlag.AlignCenter, "No track loaded")

    def _ensure_envelope_cache(self, w, h):
        """dp-225: rebuild the cached envelope pixmaps only when the
        waveform, widget size, devicePixelRatio, or color actually changed
        -- not on every repaint. Two pixmaps (bright/dark) reproduce the
        exact played/unplayed color split the old per-pixel loop drew, at
        the cost of one rebuild instead of ~1200 drawLine calls."""
        dpr = self.devicePixelRatioF()
        key = (self._env_gen, w, h, dpr, C_WAVEFORM_FG)
        if key == self._env_cache_key:
            return
        self._env_cache_key = key
        if self._waveform is None or w <= 0 or h <= 0:
            self._env_bright_pm = None
            self._env_dark_pm = None
            return
        dark_color = QColor(C_WAVEFORM_FG).darker(140)
        self._env_bright_pm = render_envelope_pixmap(
            self._waveform, w, h, C_WAVEFORM_FG, dpr
        )
        self._env_dark_pm = render_envelope_pixmap(
            self._waveform, w, h, dark_color, dpr
        )

    def showEvent(self, event):
        """dp-235: hook screenChanged once the native window exists.

        `windowHandle()` is None until the widget is shown, so this cannot be
        done in __init__. Dragging the window between displays of different
        devicePixelRatio otherwise leaves the cached envelope at the old
        scale until something else happens to repaint."""
        super().showEvent(event)
        if not self._screen_hooked:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.screenChanged.connect(self._on_screen_changed)
                self._screen_hooked = True

    def _on_screen_changed(self, _screen):
        """dp-235: force an envelope rebuild at the new screen's DPR."""
        self._env_cache_key = None
        self.update()

    def _draw_waveform(self, painter, w, h):
        self._ensure_envelope_cache(w, h)
        if self._env_bright_pm is None:
            return
        painter.drawPixmap(0, 0, self._env_bright_pm)
        needle_x = max(0, min(w, int(self._position / self._duration * w)))
        if needle_x > 0:
            # dp-235: clip, do NOT use the drawPixmap(target, pm, source)
            # overload here. That overload reads `source` in the pixmap's
            # PHYSICAL pixels, while `w`/`h` are logical - so on a display
            # with devicePixelRatio != 1 it grabs a fraction of the pixmap
            # and stretches it over the target, painting a visibly
            # mis-sized second waveform on top of the first. Clipping keeps
            # everything in logical coordinates and is DPR-agnostic.
            painter.save()
            painter.setClipRect(QRect(0, 0, needle_x, h))
            painter.drawPixmap(0, 0, self._env_dark_pm)
            painter.restore()

    def _draw_loop_markers(self, painter, w, h):
        """Draw [A and B] markers with labels on the waveform."""
        marker_col = self._loop_marker_col
        pen = QPen(marker_col, 2)
        painter.setPen(pen)
        painter.setFont(make_font(7))

        if self._loop_a is not None:
            xa = int(self._loop_a / self._duration * w)
            painter.setPen(QPen(marker_col, 2))
            painter.drawLine(xa, 0, xa, h)
            # Triangle marker pointing right at top
            painter.setBrush(QBrush(marker_col))
            painter.setPen(Qt.PenStyle.NoPen)
            pts = [QPoint(xa, 0), QPoint(xa + 8, 6), QPoint(xa, 12)]
            painter.drawPolygon(QPolygon(pts))
            painter.setPen(QPen(marker_col, 1))
            painter.drawText(xa + 2, 22, "A")

        if self._loop_b is not None:
            xb = int(self._loop_b / self._duration * w)
            painter.setPen(QPen(marker_col, 2))
            painter.drawLine(xb, 0, xb, h)
            # Triangle marker pointing left at top
            painter.setBrush(QBrush(marker_col))
            painter.setPen(Qt.PenStyle.NoPen)
            pts = [QPoint(xb, 0), QPoint(xb - 8, 6), QPoint(xb, 12)]
            painter.drawPolygon(QPolygon(pts))
            painter.setPen(QPen(marker_col, 1))
            painter.drawText(xb - 12, 22, "B")

    def _draw_cues(self, painter, w, h):
        for idx, pos in self._cue_points.items():
            if pos is None:
                continue
            x = int(pos / self._duration * w)
            painter.setPen(QPen(self._cue_col, 1))
            painter.drawLine(x, 0, x, h)
            painter.setFont(make_font(7))
            painter.setPen(self._cue_col)
            painter.drawText(x + 2, 10, str(idx + 1))

    def _draw_end_marker(self, painter, w, h):
        if self._end_marker is None or self._duration <= 0:
            return
        x = int(self._end_marker / self._duration * w)
        painter.setPen(QPen(self._end_col, 2))
        painter.drawLine(x, 0, x, h)
        painter.setFont(make_font(7))
        painter.setPen(self._end_col)
        painter.drawText(x + 2, 10, "Fin")

    def _draw_track_start_marker(self, painter, w, h):
        """dp-232: drawn every paint, outside the cached envelope pixmap
        (dp-225) -- the cache does not invalidate when a marker moves, so
        baking it in would go stale instantly. Mirrors `_draw_end_marker`."""
        if self._start_marker is None or self._duration <= 0:
            return
        x = int(self._start_marker / self._duration * w)
        painter.setPen(QPen(self._start_col, 2))
        painter.drawLine(x, 0, x, h)
        painter.setFont(make_font(7))
        painter.setPen(self._start_col)
        painter.drawText(x + 2, 10, "Start")

    def _draw_crossfade_markers(self, painter, w, h):
        """dp-216 Phase 3 Part B: thin dashed lines + a bottom-edge triangle
        for the crossfade fade-in-end / fade-out-start points, distinct from
        the loop markers (top triangle) and cue markers (mid label) so all
        three read separately. Non-interactive -- no mouse handling here."""
        if self._duration <= 0:
            return
        col = self._crossfade_marker_col
        pen = QPen(col, 1, Qt.PenStyle.DashLine)
        for pos in (self._fade_in_end, self._fade_out_start):
            if pos is None:
                continue
            x = int(pos / self._duration * w)
            painter.setPen(pen)
            painter.drawLine(x, 0, x, h)
            painter.setBrush(QBrush(col))
            painter.setPen(Qt.PenStyle.NoPen)
            pts = [QPoint(x - 5, h), QPoint(x + 5, h), QPoint(x, h - 8)]
            painter.drawPolygon(QPolygon(pts))

    def _draw_time_labels(self, painter, w, h):
        painter.setFont(make_font(7))
        painter.setPen(QColor(C_TEXT_DIM))
        painter.drawText(QRect(w - 45, 2, 43, 12),
                         Qt.AlignmentFlag.AlignRight, _fmt_time(self._duration))
        painter.drawText(QRect(2, 2, 43, 12),
                         Qt.AlignmentFlag.AlignLeft,  _fmt_time(self._position))

    # ── Mouse ─────────────────────────────────────────────────────────────────

    # dp-247: a drag SCRUBS THE NEEDLE ONLY -- `seek_requested` is emitted
    # once, on release, at the position the user let go at. Previously every
    # single mouse-move emitted a seek, and each one cost a real
    # `engine.seek()` (which blocks the Qt main thread waiting on the decode
    # frontier) plus a full `_rearm_preload()` (which can spawn a preload
    # worker thread and an ffprobe process). One drag across a long track was
    # therefore hundreds of seeks, hundreds of queued audio-thread commands,
    # and dozens of throwaway decoder threads.
    #
    # Committing on release also matches the requested behaviour: audio does
    # not chase the cursor while dragging, it jumps once when you let go.

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._scrub_to_x(event.position().x())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._scrub_to_x(event.position().x())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._commit_seek(event.position().x())

    @property
    def is_scrubbing(self) -> bool:
        """True while a drag is in progress. `MainWindow._on_engine_position`
        consults this so the engine's 10 Hz position updates don't yank the
        needle back out from under the user's cursor mid-drag."""
        return self._dragging

    def _position_from_x(self, x: float):
        """Seconds at pixel `x`, or None when there is no track to seek in."""
        if self._duration <= 0:
            return None
        frac = max(0.0, min(1.0, x / self.width()))
        return frac * self._duration

    def _scrub_to_x(self, x: float):
        """Move the needle only -- no signal, so no audio seek."""
        pos = self._position_from_x(x)
        if pos is not None:
            self.set_position(pos)

    def _commit_seek(self, x: float):
        pos = self._position_from_x(x)
        if pos is not None:
            self.seek_requested.emit(pos)


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds)//60}:{int(seconds)%60:02d}"
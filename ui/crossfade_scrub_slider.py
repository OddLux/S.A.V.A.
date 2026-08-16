"""dp-219: manual scrub control for a LIVE crossfade's gain schedule.

Seats between the primary (interactive) waveform and dp-218's
`PreviewWaveformWidget`. Inert until a real crossfade is running
(`DeckEngine._crossfade_running`); while running, the thumb tracks live
progress unless the user is dragging it, in which case dragging emits
`gain_seek_requested(t)` -- a normalized `[0.0, 1.0]` fraction of the
crossfade's fixed frame length, which `MainWindow` converts to frames and
feeds to `engine.seek_crossfade_gain()`.

Gain-schedule warp only (per dp-219's chosen scrub model): this widget never
touches deck read-position/seek in any way. Fixed pixel width regardless of
the configured overlap duration -- the full `[0, crossfade_len]` range always
maps end-to-end onto the same physical slider extent.
"""

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

from ui.skin import C_ACCENT, C_SLIDER_GROOVE, C_SLIDER_HANDLE, C_TEXT_DIM, make_font

_THUMB_WIDTH = 8


class CrossfadeScrubSlider(QWidget):
    """Read/write during an active crossfade, otherwise a disabled-looking
    groove. No seek/cue interaction of any kind -- purely a gain-schedule
    scrubber."""

    gain_seek_requested = pyqtSignal(float)  # normalized t in [0.0, 1.0]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(18)

        self._running = False
        self._t = 0.0          # live progress (from the engine's poll signal)
        self._dragging = False
        self._drag_t = 0.0     # local override while the user is dragging
        self._grab_dx = 0.0    # pointer offset within the thumb at grab time,
        # so the thumb doesn't jump to center itself under the cursor

    # ── Public API ────────────────────────────────────────────────────────

    def set_progress(self, running: bool, t: float):
        """Called from MainWindow's crossfade-progress signal (10 Hz, off
        the engine's poll thread). While dragging, the live `t` is ignored --
        the user's own drag position is authoritative until release."""
        self._running = running
        if not self._dragging:
            self._t = max(0.0, min(1.0, t))
        if not running:
            self._dragging = False
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()
        mid = h // 2

        groove_col = QColor(C_SLIDER_GROOVE)
        if not self._running:
            groove_col.setAlpha(90)  # dimmed/inert appearance
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(groove_col)
        painter.drawRect(QRect(0, mid - 2, w, 4))

        if not self._running:
            painter.setPen(QColor(C_TEXT_DIM))
            painter.setFont(make_font(7))
            painter.drawText(
                QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "-- no live crossfade --"
            )
            painter.end()
            return

        thumb_x = self._thumb_x()
        painter.setBrush(QColor(C_ACCENT))
        painter.drawRect(0, mid - 2, thumb_x, 4)  # filled progress to the left of the thumb
        painter.setBrush(QColor(C_SLIDER_HANDLE))
        painter.drawRect(self._thumb_rect())

        painter.end()

    # ── Mouse (active only while a crossfade is running) ─────────────────

    def mousePressEvent(self, event):
        # Only a press ON THE THUMB starts a drag. A crossfade is an audible,
        # committed, seconds-long transition -- a stray click on the groove
        # must not instantly warp the A/B mix to wherever the pointer landed.
        if not self._running or event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._thumb_rect().contains(event.position().toPoint()):
            return
        # Order matters: _thumb_x() switches to reading _drag_t the moment
        # _dragging goes True, so seed _drag_t from the live position and
        # measure the grab offset BEFORE flipping the flag. Doing it the
        # other way measures against a stale _drag_t of 0.0, which makes
        # merely grabbing the thumb slam the fade back to the start.
        self._grab_dx = event.position().x() - self._thumb_x()
        self._drag_t = self._t
        self._dragging = True
        self._seek_from_x(event.position().x())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._seek_from_x(event.position().x())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._t = self._drag_t

    # ── Thumb geometry (single source of truth for paint AND hit-test) ────
    #
    # The thumb travels over `width() - _THUMB_WIDTH`, not `width()`: it is
    # drawn as a _THUMB_WIDTH-wide rect whose LEFT edge sits at the mapped
    # position, so the rightmost drawable left-edge is `w - _THUMB_WIDTH`.
    # Paint and hit-test MUST share this, or grabbing the thumb where it is
    # drawn jumps the fade, and t == 1.0 -- the engine's finalize condition,
    # which this whole feature depends on -- becomes unreachable by dragging
    # to the right edge.

    def _travel(self) -> int:
        return max(1, self.width() - _THUMB_WIDTH)

    def _thumb_x(self) -> int:
        t = self._drag_t if self._dragging else self._t
        return int(t * self._travel())

    def _thumb_rect(self) -> QRect:
        return QRect(self._thumb_x(), 0, _THUMB_WIDTH, self.height())

    def _seek_from_x(self, x: float):
        frac = max(0.0, min(1.0, (x - self._grab_dx) / self._travel()))
        self._drag_t = frac
        self.update()
        self.gain_seek_requested.emit(frac)

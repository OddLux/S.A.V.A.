"""dp-218: read-only preview of the next-up track -- the one already
pre-loaded on DeckEngine's idle deck (Playlist.peek_next()). Purely
observational feedback: no seek, no cue editing, no click interaction of
any kind. Marker draw math is shared with WaveformWidget via
ui/waveform_draw.py; the interactive widget itself is untouched.
"""

from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

from config.settings import settings
from core.crossfade_markers import crossfade_marker_positions
from ui.skin import (
    C_ACCENT_ORANGE,
    C_END_MARKER,
    C_START_MARKER,
    C_TEXT_DIM,
    C_TIMELINE_OVERLAP,
    C_WAVEFORM_BG,
    C_WAVEFORM_FG,
    make_font,
)
from ui.waveform_draw import (
    draw_crossfade_markers,
    draw_cue_ticks,
    draw_end_marker,
    draw_start_marker,
    render_envelope_pixmap,
)

EMPTY = "empty"
LOADING = "loading"
READY = "ready"


def resolve_preview_markers(track_id, filepath: str, layout, index: int) -> dict:
    """Pure lookup of the marker set a preview should show for the row
    identified by `track_id` (at playlist position `index`, filepath
    `filepath`), given the current crossfade `layout`. Sources match
    exactly what WaveformWidget shows for the active track: the dp-237/238
    row-keyed maps (settings["row_cue_points"]/["row_end_markers"]/
    ["row_start_markers"], keyed by track_id -- NOT filepath, so two rows
    of the same file show independent markers) and
    core.crossfade_markers.crossfade_marker_positions for the fade points
    (that one stays filepath-keyed -- it's a layout-staleness check, not
    per-track state).

    Returns {"cues": {int: float}, "end_marker": float|None,
             "fade_in_end": float|None, "fade_out_start": float|None}.
    """
    raw_cues = settings.get("row_cue_points", {}).get(track_id, [])
    cues = {i: pos for i, pos in enumerate(raw_cues)}
    end_marker = settings.get("row_end_markers", {}).get(track_id, None)
    start_marker = settings.get("row_start_markers", {}).get(track_id, None)
    fade_in_end, fade_out_start = crossfade_marker_positions(layout, index, filepath)
    return {
        "cues": cues,
        "end_marker": end_marker,
        "start_marker": start_marker,
        "fade_in_end": fade_in_end,
        "fade_out_start": fade_out_start,
    }


class PreviewWaveformWidget(QWidget):
    """Read-only. No mousePressEvent/mouseMoveEvent/mouseReleaseEvent, no
    magnifier, no drag state -- strictly a display of DeckEngine's idle
    deck target."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(46)

        self._state = EMPTY
        self._title = None
        self._waveform = None
        self._duration = 0.0
        self._markers = {
            "cues": {},
            "end_marker": None,
            "start_marker": None,
            "fade_in_end": None,
            "fade_out_start": None,
        }

        self._bg = QColor(C_WAVEFORM_BG)

        # dp-225: envelope pixmap cache -- same rationale as WaveformWidget.
        # This widget has no needle/position, so a single cached pixmap
        # (no bright/dark split) suffices.
        self._env_gen       = 0
        self._env_cache_key = None
        self._env_pm        = None

    # ── Public setters ────────────────────────────────────────────────────

    def set_empty(self):
        self._state = EMPTY
        self._title = None
        self._waveform = None
        self._duration = 0.0
        self.update()

    def set_loading(self, title: str | None = None):
        self._state = LOADING
        self._title = title
        self._waveform = None
        self._duration = 0.0
        self.update()

    def set_waveform(self, data, duration: float):
        self._state = READY
        self._waveform = data
        self._duration = duration
        self._env_gen += 1
        self.update()

    def set_markers(self, markers: dict):
        self._markers = markers
        self.update()

    def clear(self):
        self.set_empty()

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()
        painter.fillRect(0, 0, w, h, self._bg)

        if self._state == EMPTY:
            self._draw_placeholder(painter, w, h, "-- no next track --")
        elif self._state == LOADING:
            # dp-242/dp-217: same "Loading…" wording as the primary info
            # bar's waveform-decode indicator -- one idiom for "a waveform
            # is being decoded" across both widgets.
            text = "Loading…"
            if self._title:
                text = f"Loading… ({self._title})"
            self._draw_placeholder(painter, w, h, text)
        else:
            self._draw_ready(painter, w, h)

        painter.end()

    def _draw_placeholder(self, painter, w, h, text):
        painter.setPen(QColor(C_TEXT_DIM))
        painter.setFont(make_font(8))
        painter.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text)

    def _ensure_envelope_cache(self, w, h):
        dpr = self.devicePixelRatioF()
        key = (self._env_gen, w, h, dpr, C_WAVEFORM_FG)
        if key == self._env_cache_key:
            return
        self._env_cache_key = key
        if self._waveform is None or w <= 0 or h <= 0:
            self._env_pm = None
            return
        self._env_pm = render_envelope_pixmap(self._waveform, w, h, C_WAVEFORM_FG, dpr)

    def _draw_ready(self, painter, w, h):
        if self._waveform is None or self._duration <= 0:
            self._draw_placeholder(painter, w, h, "-- no next track --")
            return
        self._ensure_envelope_cache(w, h)
        if self._env_pm is not None:
            painter.drawPixmap(0, 0, self._env_pm)
        draw_cue_ticks(
            painter, self._markers["cues"], self._duration, w, h, C_ACCENT_ORANGE
        )
        draw_end_marker(
            painter, self._markers["end_marker"], self._duration, w, h, C_END_MARKER
        )
        draw_start_marker(
            painter, self._markers.get("start_marker"), self._duration, w, h,
            C_START_MARKER,
        )
        draw_crossfade_markers(
            painter,
            self._markers["fade_in_end"],
            self._markers["fade_out_start"],
            self._duration,
            w,
            h,
            C_TIMELINE_OVERLAP,
        )

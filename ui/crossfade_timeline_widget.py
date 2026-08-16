"""
Crossfade timeline widget (dp-159) - QGraphicsView/QGraphicsScene rendering
of a core/crossfade_model.py layout: track blocks sized by duration with
odd/even shading, a zoom slider, overlap regions with draggable bezier
curve handles, and left-only/rigid-chain drag on each track.

Standalone/embeddable - not hosted in a dialog here (that's dp-160). All
invariant enforcement (no gaps, no rightward drag past default) is
delegated to CrossfadeLayout; this widget only calls into the model and
re-renders whatever state it returns, per the ticket's instruction not to
duplicate constraint math here.
"""

from pathlib import Path

from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFontMetrics, QLinearGradient, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QSizePolicy,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsEllipseItem,
    QGraphicsSimpleTextItem, QGraphicsLineItem,
)

from core.analyzer import WaveformAnalyzer
from core.crossfade_model import CrossfadeLayout, DEFAULT_CURVE_OUT_P1, DEFAULT_CURVE_OUT_P2
from ui.skin import (
    C_TIMELINE_TRACK_ODD, C_TIMELINE_TRACK_EVEN, C_TIMELINE_OVERLAP,
    C_TIMELINE_CURVE, C_TIMELINE_HANDLE, C_TIMELINE_PER_TRACK_PALETTE,
    C_TEXT_PRIMARY, C_TEXT_DIM, C_BORDER, C_ACCENT_BLUE,
    make_font, FONT_SIZE_SMALL,
)

# dp-190: the independent fade-out curve gets its own color (existing
# C_ACCENT_BLUE, already used elsewhere for pause/loop contrast) plus a
# dashed line, so it's visually distinguishable from the fade-in curve
# (C_TIMELINE_CURVE, solid) without introducing any new colors.
C_TIMELINE_CURVE_OUT  = C_ACCENT_BLUE
C_TIMELINE_HANDLE_OUT = C_ACCENT_BLUE


class _TimelineGraphicsView(QGraphicsView):
    """QGraphicsView that notifies its parent widget on resize so the
    fit-to-view zoom level (dp-161) can be recomputed against the new
    viewport width."""

    def __init__(self, scene, widget: "CrossfadeTimelineWidget"):
        super().__init__(scene)
        self._widget = widget

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._widget._on_view_resized()

TRACK_HEIGHT   = 90   # dp-167: bumped from 60 for legibility (labels/handles)
HANDLE_RADIUS  = 5
MIN_PPS        = 2.0     # pixels per second floor for the fit-to-view edge
                         # case (near-zero-duration layout); see
                         # _fit_to_view_pps. Not a slider endpoint anymore.
MAX_PPS        = 40.0    # dp-207: pixels per second at max zoom-in (slider
                         # value 0). Reduced from 200.0 (1/5) - the old max
                         # was more zoomed-in than useful.
LABEL_PADDING  = 4    # dp-168: gap between lane edge and its track-name label
LABEL_Z        = 6    # dp-168: above the waveform (1) and overlap tint (5) so
                       # the name stays readable over both; below the curve
                       # line/handles (8/10), which live lower in the lane.
MARKER_PEN_WIDTH = 15  # dp-200: widened 3x from 5px for at-a-glance readability
                       # dp-188: widened from 2px so the marker is a reliable
                       # drag handle (dp-170's original 2px was too thin to
                       # click/drag) while still reading as a thin boundary
                       # line at TRACK_HEIGHT = 90
MARKER_Z         = 7   # dp-170: above the overlap tint (5) and track label
                        # (6) so the marker reads through overlap regions and
                        # doesn't get an ambiguous same-z paint order against
                        # the label; below the curve line/handles (8/10).
OVERLAP_WAVEFORM_Z     = 5.5   # dp-171: above the opaque overlap tint (5),
                                # which would otherwise fully hide both
                                # tracks' waveforms in the overlap band;
                                # below the label/marker/curve/handles
                                # (6/7/8/10) so those stay unaffected.
OVERLAP_WAVEFORM_OPACITY = 0.5  # dp-171: reduced-alpha so both tracks'
                                 # waveform segments blend and stay
                                 # distinguishable through the overlap tint
                                 # instead of one occluding the other.

# dp-214: time ruler band, drawn in a dedicated strip above the top gutter
# (which itself occupies [-MARKER_PEN_WIDTH, 0]) so ticks/labels never
# collide with dp-208's marker bars or dp-168's track-name labels.
# dp-215: the overlap's own duration label, drawn inside the overlap band.
# z sits between the opaque overlap tint (5) and the curve lines (8) -- above
# the tint so it reads, below the curves so it never occludes one.
OVERLAP_LABEL_Z = 6.5

RULER_HEIGHT  = 20
RULER_Z       = 9  # above the curve/handles' 8/10 band is fine since the
                    # ruler lives in its own y-band, never overlapping them;
                    # kept off LABEL_Z (6) so ruler items are distinguishable
                    # from dp-168's track-name labels by z alone.
# dp-224: per-track rulers get their own z, distinct from the global ruler's
# RULER_Z, so tests (and any future hit-testing) can tell the two apart
# without depending on scene insertion order. They never share a y-band with
# RULER_Z items, so the numeric ordering relative to it is arbitrary.
PER_TRACK_RULER_Z = 9.5
# dp-224 follow-up: extra vertical separation between the global (whole
# playlist) ruler and the even-track rulers directly beneath it. Without it
# the two bands abut and read as one continuous strip of numbers, and it is
# not obvious which row is whole-playlist time and which is per-track.
GLOBAL_RULER_GAP = 10
# Ruler ticks are stroked lines, and a QPen is centred on its path -- so the
# topmost tick's bounding box overhangs its band by half the pen width. Pad
# the scene rect by a whole pixel so that hairline is not clipped (dp-208 hit
# the same thing with the marker bars).
_RULER_PEN_PAD = 1
# ...and the global ruler is drawn dimmer than the per-track rulers for the
# same reason: the per-track times are the ones being read while editing an
# overlap, so the whole-playlist row recedes rather than competing.
GLOBAL_RULER_OPACITY = 0.55
MIN_TICK_PX   = 40  # minimum readable pixel spacing between ruler ticks -
                     # legacy fallback gate, used only when _nice_interval is
                     # called without a duration (see dp-243).
MIN_LABEL_GUTTER = 8  # dp-243: minimum readable gap, in px, between the edge
                       # of one label and the start of the next - added on
                       # top of the measured label width so labels don't
                       # merely abut.
# dp-239: ladder topped out at 300s (5min), so a multi-hour timeline still
# drew a tick every 5 minutes and adjacent M:SS labels overlapped into a
# solid band. Extended additively at the top only - 600 (10min) and 900
# (15min) keep the x2/x1.5 progression sane on the way to 1800 (30min).
# Existing entries (1..300) are untouched so short/medium timelines render
# identically to before (see _nice_interval's no-op guarantee, tested in
# tests/test_crossfade_timeline_ruler.py).
_NICE_INTERVALS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800)


def _fmt_time(seconds: float) -> str:
    """M:SS formatting, matching ui/waveform_widget.py's convention."""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _nice_interval(pps: float, duration: float | None = None) -> float:
    """Smallest candidate interval (seconds) whose pixel spacing at the
    given pps keeps adjacent labels legible. Falls back to the largest
    candidate if even that isn't wide enough (very low zoom).

    dp-243: when `duration` is given, the gate is label-width-aware. The
    widest label actually drawn at any interval is the one nearest
    `duration` (the largest tick value on that ruler - "300:00" is wider
    than "30:00"), measured with QFontMetrics against the ruler's own font.
    A candidate interval is accepted once its pixel spacing clears that
    measured width plus MIN_LABEL_GUTTER.

    When `duration` is omitted, falls back to the legacy fixed MIN_TICK_PX
    gate - kept for callers that only care about interval selection in the
    abstract (dp-239's no-op guarantee is expressed against this form)."""
    if duration is None:
        for candidate in _NICE_INTERVALS:
            if candidate * pps >= MIN_TICK_PX:
                return candidate
        return _NICE_INTERVALS[-1]

    metrics = QFontMetrics(make_font(FONT_SIZE_SMALL))
    min_spacing = metrics.horizontalAdvance(_fmt_time(duration)) + MIN_LABEL_GUTTER
    for candidate in _NICE_INTERVALS:
        if candidate * pps >= min_spacing:
            return candidate
    return _NICE_INTERVALS[-1]


def track_color(index: int) -> str:
    """dp-176: the per-track identity color for a given track position -
    single source of truth for the palette[index % len] lookup, shared by
    this widget's marker-line draw (_render_impl) and CrossfadeDialog's
    shortcut button row, so the two never drift out of sync."""
    return C_TIMELINE_PER_TRACK_PALETTE[index % len(C_TIMELINE_PER_TRACK_PALETTE)]


class _CurveHandleItem(QGraphicsEllipseItem):
    """Draggable control point for one overlap's bezier curve. Position is
    constrained to the overlap's rect; dragging writes back into the model
    (normalized 0..1 within the overlap) and asks the parent widget to
    re-render so the curve line updates live."""

    def __init__(
        self, widget: "CrossfadeTimelineWidget", overlap_index: int, which: int,
        curve_role: str = "in",
    ):
        super().__init__(-HANDLE_RADIUS, -HANDLE_RADIUS, HANDLE_RADIUS * 2, HANDLE_RADIUS * 2)
        self._widget     = widget
        self._ov_index   = overlap_index
        self._which      = which        # 0 = p1, 1 = p2
        self._curve_role = curve_role   # dp-190: "in" (fade-in) or "out" (fade-out)
        handle_color = C_TIMELINE_HANDLE if curve_role == "in" else C_TIMELINE_HANDLE_OUT
        self.setBrush(QBrush(QColor(handle_color)))
        self.setPen(QPen(QColor(C_BORDER)))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(10)

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionChange:
            rect = self._widget._overlap_rect(self._ov_index)
            if rect is None:
                return value
            x = min(max(value.x(), rect.left()), rect.right())
            y = min(max(value.y(), rect.top()), rect.bottom())
            clamped = QPointF(x, y)
            # Building the scene calls setPos() programmatically, which
            # fires this same itemChange re-entrantly. Writing back to the
            # model / triggering a full re-render from inside that callback
            # would clear() the scene (deleting this item's C++ object)
            # while it's still mid-construction - skip while building.
            if not self._widget._building:
                norm_x = (x - rect.left()) / rect.width() if rect.width() else 0.0
                norm_y = 1.0 - ((y - rect.top()) / rect.height() if rect.height() else 0.0)
                self._widget._on_handle_moved(
                    self._ov_index, self._which, norm_x, norm_y, self._curve_role
                )
            return clamped
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        # Forward-fix for dp-180/dp-168 interaction: mark a live left-button
        # drag so any *other* trigger of a full self._render() (waveform
        # decode completing, a view resize, etc.) - not just this item's own
        # itemChange() - defers instead of clearing the scene mid-drag. See
        # CrossfadeTimelineWidget._render()/._handle_drag_active.
        if event.button() == Qt.MouseButton.LeftButton:
            self._widget._handle_drag_active = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._widget._handle_drag_active = False
            self._widget._flush_pending_render()

    def mouseDoubleClickEvent(self, event):
        # dp-166: reset this overlap's curve to the default equal-power
        # bezier - the ticket's "reset-to-default action".
        # dp-190: resets only the curve this handle belongs to (in or out),
        # not both at once.
        self._widget._reset_overlap_curve(self._ov_index, self._curve_role)
        event.accept()

    def contextMenuEvent(self, event):
        # dp-180: right-click menu hosting the linear-curve action,
        # alongside the existing double-click reset-to-default.
        from PyQt6.QtWidgets import QMenu

        menu = QMenu()
        reset_action = menu.addAction("Reset to Default")
        linear_action = menu.addAction("Set Linear")
        chosen = menu.exec(event.screenPos())
        if chosen is reset_action:
            self._widget._reset_overlap_curve(self._ov_index, self._curve_role)
        elif chosen is linear_action:
            self._widget._set_overlap_curve_linear(self._ov_index, self._curve_role)
        event.accept()


class CrossfadeTimelineWidget(QWidget):
    """Renders a CrossfadeLayout on a zoomable horizontal timeline."""

    sig_overlap_changed = pyqtSignal(int)  # overlap index that changed
    sig_layout_changed = pyqtSignal(object)  # dp-175: emitted at end of
    # set_layout() with the new CrossfadeLayout, so hosts (CrossfadeDialog)
    # can rebuild anything derived from the track list (e.g. per-track
    # shortcut buttons) without duplicating layout-change detection.
    _waveform_ready_signal = pyqtSignal(int, object)  # track index, RMS array

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout: CrossfadeLayout | None = None
        self._pps = 20.0  # pixels per second
        self._drag_index  = None   # track index currently being dragged
        self._drag_start_x = 0.0
        self._drag_start_overlap = 0.0
        self._building = False   # True while _render() is (re)building the scene
        # Forward-fix for dp-180/dp-168 interaction (see _render()): true
        # while a _CurveHandleItem is the active left-button mouse grabber.
        # dp-222: track drags (below, driven by the viewport event filter,
        # not item-move) are NOT guarded by this flag. That is safe today
        # only because track rects are not ItemIsMovable, so they never
        # become the Qt mouse grabber the way a _CurveHandleItem does - if
        # that ever changes, this guard must be extended to cover track
        # drags too, or dp-180's RuntimeError (scene.clear() deleting the
        # actively-grabbed item mid-drag) returns for track dragging.
        self._handle_drag_active = False
        self._render_pending = False  # a _render() call was deferred mid-drag
        # dp-222: track-drag render throttle. _update_track_drag used to call
        # self._render() unconditionally on every mouse-move, forcing a full
        # scene.clear() + rebuild (waveform paths + overlap intersected()
        # calls) tens of times a second. Coalesce those into one render per
        # timer tick instead - multiple mouse-moves inside one interval
        # collapse into a single rebuild.
        self._track_drag_active = False
        self._drag_render_timer = QTimer(self)
        self._drag_render_timer.setSingleShot(True)
        self._drag_render_timer.setInterval(16)  # ~60Hz ceiling
        self._drag_render_timer.timeout.connect(self._render)
        # dp-241: waveform-ready render coalescing. Each track's decode runs
        # off-thread (core/analyzer.py's WaveformAnalyzer._run) - that part
        # was already correct - but every completion called self._render()
        # immediately, one full scene.clear()+rebuild (waveform paths +
        # per-overlap QPainterPath.intersected() calls, dp-222's own comment
        # calls this the single most expensive render step) per track. On
        # dialog open with several tracks whose decodes finish close
        # together, that's N consecutive full rebuilds landing on the Qt
        # main thread in a burst, and CPU-bound Python there contends the
        # GIL with the sounddevice audio callback thread - the audible
        # stutter. Debouncing collapses a burst of near-simultaneous
        # completions into one rebuild, the same coalescing pattern already
        # used for track-drag renders above.
        self._waveform_render_timer = QTimer(self)
        self._waveform_render_timer.setSingleShot(True)
        self._waveform_render_timer.setInterval(50)
        self._waveform_render_timer.timeout.connect(self._render)
        # Per-track decoded waveforms (dp-163). core/analyzer.py's shared
        # `analyzer` singleton decodes one track at a time, so a short-lived
        # WaveformAnalyzer() instance is spun up per track here instead -
        # each fires its own on_ready off the analysis thread, marshalled
        # back onto the Qt thread via _waveform_ready_signal per the
        # threading-model rule in CLAUDE.md.
        self._waveforms: dict[int, "object"] = {}   # track index -> np.ndarray
        self._analyzers: list[WaveformAnalyzer] = [] # keep instances alive
        # (overlap index, curve role "in"/"out") -> curve QGraphicsPathItem (dp-190)
        self._curve_items: dict[tuple, "object"] = {}
        self._waveform_ready_signal.connect(self._on_waveform_ready)
        self._setup_ui()

    # ── UI ────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        zoom_row = QHBoxLayout()
        lbl = QLabel("Zoom")
        lbl.setFont(make_font(FONT_SIZE_SMALL))
        lbl.setStyleSheet(f"color: {C_TEXT_PRIMARY}; background: transparent;")
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(0, 1000)
        # dp-207: slider value 0 = max zoom-in (MAX_PPS), value 1000 = fit
        # the whole timeline in view. Default sits moderately zoomed in
        # (near the left/zoomed-in end) on the exponential response curve.
        self._zoom_slider.setValue(250)
        self._zoom_slider.valueChanged.connect(self._on_zoom_changed)
        zoom_row.addWidget(lbl)
        zoom_row.addWidget(self._zoom_slider)
        root.addLayout(zoom_row)

        self._scene = QGraphicsScene(self)
        self._view  = _TimelineGraphicsView(self._scene, self)
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        # dp-165 set this to ScrollBarAsNeeded, which is logically correct
        # (pageStep()/maximum() narrow monotonically with zoom, verified
        # headlessly) but is invisible in the live app: at dp-161's
        # fit-to-view minimum, maximum() is 0 and Qt's "as needed" policy
        # hides the scrollbar entirely rather than drawing a full-width
        # handle. dp-165's headless check only ever asserted on the logical
        # metrics, never on isVisible()/actually painting the widget, so it
        # could not have caught this - a hidden bar still reports correct
        # numbers. The Premiere/DaVinci-style zoom bar this is meant to
        # emulate (dp-169) is always visible, spanning the full track at
        # min zoom and narrowing in place as you zoom in - so it must stay
        # on screen at all times.
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._view.viewport().installEventFilter(self)
        # dp-167: expand to fill the hosting dialog on resize, with a floor
        # so the track lane never compresses below a legibly-readable height.
        self._view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._view.setMinimumHeight(TRACK_HEIGHT + 40)
        root.addWidget(self._view)

    def _fit_to_view_pps(self) -> float:
        """PPS that fits the entire layout's timeline inside the current
        viewport width. Falls back to MIN_PPS when there's no layout or the
        duration is zero/near-zero (dp-161)."""
        if self._layout is None:
            return MIN_PPS
        total = self._layout.total_duration()
        if total <= 1e-6:
            return MIN_PPS
        return max(self._view.viewport().width() / total, 1e-6)

    def _zoom_pps_for_frac(self, frac: float, fit_pps: float) -> float:
        """dp-207: map a slider fraction (0.0 = zoomed in, 1.0 = fit whole
        timeline) to pixels-per-second on an exponential response curve
        between MAX_PPS (frac 0) and fit_pps (frac 1).

        Exponential (log-scale) interpolation makes equal slider travel
        equal a constant *ratio* of PPS change, so mid-range positions read
        as evenly spaced instead of the linear curve's cliff where most of
        the travel looked the same as one end. Falls back to linear when
        fit_pps is outside the (0, MAX_PPS) range the exponential form
        needs - e.g. a very short timeline whose fit-to-view is already
        more zoomed-in than MAX_PPS - keeping the mapping monotonic."""
        if 0.0 < fit_pps < MAX_PPS:
            return MAX_PPS * (fit_pps / MAX_PPS) ** frac
        return MAX_PPS + frac * (fit_pps - MAX_PPS)

    def _on_zoom_changed(self, value: int):
        frac = value / 1000.0
        self._pps = self._zoom_pps_for_frac(frac, self._fit_to_view_pps())
        self._render()

    def _on_view_resized(self):
        # dp-207: fit_pps is now an anchor of the whole exponential curve,
        # not just one endpoint, so every slider position's PPS depends on
        # viewport width - recompute unconditionally on resize.
        frac = self._zoom_slider.value() / 1000.0
        self._pps = self._zoom_pps_for_frac(frac, self._fit_to_view_pps())
        self._render()

    # ── Public API ────────────────────────────────────────────────────────

    def set_layout(self, layout: CrossfadeLayout):
        self._layout = layout
        self._waveforms = {}
        self._analyzers = []
        self._render()
        self._start_waveform_decodes()
        self.sig_layout_changed.emit(layout)

    @property
    def layout_model(self) -> "CrossfadeLayout | None":
        return self._layout

    def scroll_to_track(self, index: int):
        """dp-175: scroll the timeline view to center the given track's
        lane, without changing the current zoom level. Reuses the same
        position/width scene-rect math as _render_impl's track layout
        (positions[i] * pps, duration * pps) so this stays in sync with
        whatever the tracks actually render at."""
        if self._layout is None:
            return
        tracks = self._layout.tracks
        if not (0 <= index < len(tracks)):
            return
        positions = self._layout.track_positions()
        x = positions[index] * self._pps
        self._view.centerOn(QPointF(x, TRACK_HEIGHT / 2))

    def _start_waveform_decodes(self):
        if self._layout is None:
            return
        for idx, track in enumerate(self._layout.tracks):
            wa = WaveformAnalyzer()
            wa.on_ready = lambda waveform, i=idx: self._waveform_ready_signal.emit(i, waveform)
            self._analyzers.append(wa)
            wa.analyze(track.filepath)

    def _on_waveform_ready(self, track_index: int, waveform):
        self._waveforms[track_index] = waveform
        # dp-241: debounce - see _waveform_render_timer's construction
        # comment. Restarting on every call means a burst of completions
        # (the common case right after set_layout()) collapses to one
        # render fired 50ms after the last one lands.
        self._waveform_render_timer.start()

    # ── Rendering ─────────────────────────────────────────────────────────

    def _overlap_rect(self, overlap_index: int) -> "QRectF | None":
        if self._layout is None:
            return None
        positions = self._layout.track_positions()
        tracks    = self._layout.tracks
        if not (0 <= overlap_index < len(self._layout.overlaps)):
            return None
        overlap_start = positions[overlap_index + 1]
        overlap_end   = positions[overlap_index] + tracks[overlap_index].duration
        x0 = overlap_start * self._pps
        x1 = overlap_end * self._pps
        return QRectF(min(x0, x1), 0, abs(x1 - x0), TRACK_HEIGHT)

    def _render(self):
        # Forward-fix for a dp-180/dp-168 interaction: dp-180 stopped
        # _on_handle_moved() from ever calling _render() mid-drag, but it
        # only closed that one call site. _on_waveform_ready() - present
        # since dp-163, untouched by dp-168 or dp-180 - still called the
        # full self._render() unconditionally. A background waveform decode
        # completing while a user is mid-drag on a _CurveHandleItem (very
        # plausible: dragging right after opening the dialog, before decode
        # finishes) reaches this same self._scene.clear() and deletes the
        # actively-grabbed handle's C++ object, reproducing dp-180's exact
        # RuntimeError through a different call path. Guard the single
        # rebuild entry point itself instead of chasing every caller: defer
        # the rebuild until the drag's mouseReleaseEvent flushes it.
        if self._handle_drag_active:
            self._render_pending = True
            return
        self._building = True
        try:
            self._render_impl()
        finally:
            self._building = False

    def _flush_pending_render(self):
        if self._render_pending:
            self._render_pending = False
            self._render()

    def _render_impl(self):
        self._scene.clear()
        self._curve_items = {}
        if self._layout is None or not self._layout.tracks:
            return

        positions = self._layout.track_positions()
        tracks    = self._layout.tracks

        for i, track in enumerate(tracks):
            x = positions[i] * self._pps
            w = max(1.0, track.duration * self._pps)
            color = C_TIMELINE_TRACK_ODD if i % 2 == 0 else C_TIMELINE_TRACK_EVEN
            rect = QGraphicsRectItem(x, 0, w, TRACK_HEIGHT)
            rect.setBrush(QBrush(QColor(color)))
            rect.setPen(QPen(QColor(C_BORDER)))
            rect.setData(0, i)  # track index, used by drag hit-testing
            self._scene.addItem(rect)

            # dp-170: odd/even boundary marker - top edge for odd tracks
            # (i % 2 == 0), bottom edge for even tracks (i % 2 == 1) - gives
            # each track a distinct visual anchor independent of fill color,
            # so its extent stays traceable through overlap regions.
            # dp-176: colored per-track (palette[i % len], stable/deterministic
            # by track position) instead of dp-170's original flat odd/even
            # 2-color split, so the marker also carries track *identity*, not
            # just alternating-lane contrast. Top/bottom placement is
            # unchanged - still keyed off i % 2.
            # dp-194: playlist-assigned color wins when set; dp-176's
            # position-derived auto-palette is the fallback for tracks
            # with no playlist color (track.color is None).
            marker_color = track.color or track_color(i)
            # dp-205: a filled rect (not a stroked line) so the marker stays
            # exactly cropped to [x, x+w] and [0, TRACK_HEIGHT] -- a QPen
            # stroke defaults to Qt::SquareCap (overshoots the endpoints
            # horizontally) and is centered on its y (overshoots vertically
            # into the track label above/below). Top-edge markers sit flush
            # at y=0; bottom-edge markers sit flush at TRACK_HEIGHT.
            # dp-208 (amended approach): markers moved off the waveform into
            # the lane gutters instead of moving the label. Top markers now
            # sit in [-MARKER_PEN_WIDTH, 0] (bar above the waveform, lower
            # edge flush with the y=0 border); bottom markers sit in
            # [TRACK_HEIGHT, TRACK_HEIGHT + MARKER_PEN_WIDTH] (bar below,
            # upper edge flush with the TRACK_HEIGHT border). Frees the
            # on-waveform corners so the dp-168 label never collides.
            marker_y = -MARKER_PEN_WIDTH if i % 2 == 0 else TRACK_HEIGHT
            marker = QGraphicsRectItem(x, marker_y, w, MARKER_PEN_WIDTH)
            gradient = QLinearGradient(0, marker_y, 0, marker_y + MARKER_PEN_WIDTH)
            gradient.setColorAt(0.0, QColor(marker_color).lighter(130))
            gradient.setColorAt(1.0, QColor(marker_color).darker(130))
            marker.setBrush(QBrush(gradient))
            marker.setPen(QPen(Qt.PenStyle.NoPen))
            marker.setZValue(MARKER_Z)
            marker.setData(0, i)  # track index, mirrors the rect item's
            # role-0 data (dp-176: lets callers/tests identify a marker's
            # owning track without depending on scene stacking order).
            self._scene.addItem(marker)

            waveform = self._waveforms.get(i)
            if waveform is not None and len(waveform) > 0:
                self._draw_waveform(x, w, waveform)

            self._draw_track_label(x, w, track)

        for i, ov in enumerate(self._layout.overlaps):
            rect = self._overlap_rect(i)
            if rect is None or rect.width() <= 0:
                continue
            region = QGraphicsRectItem(rect)
            region.setBrush(QBrush(QColor(C_TIMELINE_OVERLAP)))
            region.setPen(QPen(Qt.PenStyle.NoPen))
            region.setZValue(5)
            self._scene.addItem(region)

            # dp-171: the tint above is an opaque fill sitting above both
            # tracks' waveforms (z=1), which otherwise hides them completely
            # within the overlap band. Re-draw each bordering track's
            # waveform, clipped to this overlap rect, above the tint at
            # reduced opacity so both stay visible/distinguishable - the
            # curve line (drawn next, z=8) stays fully opaque on top.
            # dp-222: this is the single most expensive step per rebuild
            # (QPainterPath.intersected() per overlap) - skip it during a
            # throttled mid-drag render and let it come back at full
            # fidelity on the release-triggered final render.
            if not self._track_drag_active:
                self._draw_overlap_waveforms(rect, i)

            self._draw_overlap_duration_label(rect, ov)

            self._draw_curve(rect, ov, i)
            self._draw_curve_out(rect, ov, i)

            for which, (nx, ny) in enumerate((ov.curve_p1, ov.curve_p2)):
                handle = _CurveHandleItem(self, i, which, curve_role="in")
                hx = rect.left() + nx * rect.width()
                hy = rect.top() + (1.0 - ny) * rect.height()
                handle.setPos(hx, hy)
                self._scene.addItem(handle)

            # dp-190: independent fade-out curve's own handle pair. Legacy
            # layouts (curve_out_p1/p2 still None) show it seeded at the
            # mirrored default rather than at no position at all, so there's
            # always something to drag - editing it is what "adopts" the
            # independent curve for that overlap going forward.
            out_p1 = ov.curve_out_p1 if ov.curve_out_p1 is not None else DEFAULT_CURVE_OUT_P1
            out_p2 = ov.curve_out_p2 if ov.curve_out_p2 is not None else DEFAULT_CURVE_OUT_P2
            for which, (nx, ny) in enumerate((out_p1, out_p2)):
                handle = _CurveHandleItem(self, i, which, curve_role="out")
                hx = rect.left() + nx * rect.width()
                hy = rect.top() + (1.0 - ny) * rect.height()
                handle.setPos(hx, hy)
                self._scene.addItem(handle)

        self._draw_time_ruler()
        self._draw_per_track_rulers()

        total_w = self._layout.total_duration() * self._pps
        # dp-208: expand the scene rect to cover both gutter bands so the
        # relocated marker bars (now outside [0, TRACK_HEIGHT]) aren't
        # clipped by scroll/fit-in-view math.
        # dp-214: further extended upward by RULER_HEIGHT for the time ruler
        # band, which sits above the top gutter.
        # dp-224: the global ruler moved up by one more RULER_HEIGHT (to sit
        # closer to the zoom bar) and a per-track ruler band was added below
        # the bottom gutter (for odd-index tracks) - the scene rect grows by
        # RULER_HEIGHT on both ends to cover them. Top-to-bottom band stack
        # is now: global ruler, even-track rulers, top marker gutter, track
        # lanes, bottom marker gutter, odd-track rulers.
        # dp-224 follow-up: plus GLOBAL_RULER_GAP, the extra separation
        # pushing the global ruler clear of the even-track rulers below it.
        self._scene.setSceneRect(
            0,
            -(MARKER_PEN_WIDTH + 2 * RULER_HEIGHT + GLOBAL_RULER_GAP
              + _RULER_PEN_PAD),
            max(total_w, 1),
            TRACK_HEIGHT + 2 * MARKER_PEN_WIDTH + 3 * RULER_HEIGHT
            + GLOBAL_RULER_GAP + 2 * _RULER_PEN_PAD,
        )

    def _draw_ruler(self, x_offset: float, duration: float, y0: float, y1: float,
                    z: float, opacity: float = 1.0):
        """Shared tick/label drawing for both the global ruler (dp-214) and
        the per-track rulers (dp-224). `x_offset` (seconds) shifts every tick
        so a per-track ruler reads local time from its own 0:00 while a
        scene-x-mapped tick still lands at `(x_offset + tick_seconds) *
        self._pps`. Ticks are drawn between y0 and y1; labels sit at
        whichever of the two is visually topmost (smallest y)."""
        if duration <= 0 or self._pps <= 0:
            return

        interval = _nice_interval(self._pps, duration)
        font = make_font(FONT_SIZE_SMALL)
        pen = QPen(QColor(C_TEXT_DIM))
        label_y = min(y0, y1)

        tick_count = int(duration // interval) + 1
        for step in range(tick_count + 1):
            tick_seconds = step * interval
            if tick_seconds > duration:
                break
            x = (x_offset + tick_seconds) * self._pps

            tick = QGraphicsLineItem(x, y0, x, y1)
            tick.setPen(pen)
            tick.setZValue(z)
            tick.setOpacity(opacity)
            self._scene.addItem(tick)

            label = QGraphicsSimpleTextItem(_fmt_time(tick_seconds))
            label.setFont(font)
            label.setBrush(QBrush(QColor(C_TEXT_PRIMARY)))
            label.setPos(x + 2, label_y)
            label.setZValue(z)
            label.setOpacity(opacity)
            self._scene.addItem(label)

    def _draw_time_ruler(self):
        """dp-214: draw tick marks + M:SS labels spanning [0,
        total_duration()]. dp-224: relocated one band further up (closer to
        the zoom bar), vacating its old band for the per-track rulers of
        even-index tracks. Interval adapts to zoom (self._pps) via
        _nice_interval so labels stay legible at any zoom level."""
        if self._layout is None or not self._layout.tracks:
            return
        total = self._layout.total_duration()
        band_bottom = -(MARKER_PEN_WIDTH + RULER_HEIGHT + GLOBAL_RULER_GAP)
        band_top = band_bottom - RULER_HEIGHT
        self._draw_ruler(
            0.0, total, band_bottom, band_top, RULER_Z, GLOBAL_RULER_OPACITY
        )

    def _draw_per_track_rulers(self):
        """dp-224: one ruler per track, each starting at 0:00 at that
        track's own start. Placement mirrors dp-208's marker-bar parity:
        even-index tracks (marker bar above) get their ruler in the upper
        band the global ruler just vacated; odd-index tracks (marker bar
        below) get their ruler under the bottom marker gutter.

        Perf guard (dp-222's _render_impl-on-every-drag-move constraint):
        tracks narrower than one zoom-adaptive interval are skipped
        entirely rather than drawn with just a lone 0:00 tick - keeps
        per-track item count bounded the same way dp-214 bounds the global
        ruler's tick count."""
        if self._layout is None or not self._layout.tracks or self._pps <= 0:
            return
        interval = _nice_interval(self._pps)
        positions = self._layout.track_positions()
        for i, track in enumerate(self._layout.tracks):
            duration = track.duration
            if duration <= 0 or duration < interval:
                continue
            if i % 2 == 0:
                y0, y1 = -MARKER_PEN_WIDTH, -(MARKER_PEN_WIDTH + RULER_HEIGHT)
            else:
                y0 = TRACK_HEIGHT + MARKER_PEN_WIDTH
                y1 = TRACK_HEIGHT + MARKER_PEN_WIDTH + RULER_HEIGHT
            self._draw_ruler(positions[i], duration, y0, y1, PER_TRACK_RULER_Z)

    def _draw_overlap_duration_label(self, rect: QRectF, overlap):
        """dp-215: write the overlap's own length (M:SS) inside its region, so
        the crossfade duration can be read straight off the timeline instead
        of inferred from the band's width.

        Reads `overlap.duration` directly -- deriving it back from the rect's
        pixel width would just be `_overlap_rect`'s arithmetic run backwards
        through `self._pps`, and would drift from the model the moment either
        changes.

        Sits at OVERLAP_LABEL_Z (6.5), between the opaque tint (5) and the
        curve lines (8): high enough to read over the tint, low enough never
        to occlude a curve. Placed low in the lane, where both bezier curves
        spend the least of their vertical travel.

        Omitted entirely -- not elided -- when the text does not fit the
        band. A half-word of a duration is worse than no duration, and
        `_draw_track_label`'s elision only makes sense because a truncated
        filename is still recognizable.
        """
        if overlap.duration <= 0:
            return
        text = _fmt_time(overlap.duration)
        font = make_font(FONT_SIZE_SMALL)
        metrics = QFontMetrics(font)
        text_w = metrics.horizontalAdvance(text)
        if text_w + 2 * LABEL_PADDING > rect.width():
            return

        item = QGraphicsSimpleTextItem(text)
        item.setFont(font)
        item.setBrush(QBrush(QColor(C_TEXT_PRIMARY)))
        item.setPos(
            rect.left() + (rect.width() - text_w) / 2.0,
            TRACK_HEIGHT - metrics.height() - LABEL_PADDING,
        )
        item.setZValue(OVERLAP_LABEL_Z)
        self._scene.addItem(item)

    def _draw_track_label(self, x: float, w: float, track):
        """dp-168: draw the track's filename (stem, no extension) at the
        lane's top-left corner, elided to the lane width so long names don't
        spill into the neighboring lane."""
        name = Path(track.filepath).stem
        font = make_font(FONT_SIZE_SMALL)
        metrics = QFontMetrics(font)
        available = max(0.0, w - 2 * LABEL_PADDING)
        elided = metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(available))

        item = QGraphicsSimpleTextItem(elided)
        item.setFont(font)
        item.setBrush(QBrush(QColor(C_TEXT_PRIMARY)))
        item.setPos(x + LABEL_PADDING, LABEL_PADDING)
        item.setZValue(LABEL_Z)
        self._scene.addItem(item)

    def _build_waveform_path(self, x: float, w: float, waveform) -> "QPainterPath":
        """Build the closed envelope path for one track's decoded RMS
        waveform, scaled to TRACK_HEIGHT. Pure path construction - no scene
        mutation - so it can be reused both for the full-lane draw and for
        the overlap-clipped redraw (dp-171)."""
        from PyQt6.QtGui import QPainterPath

        n = len(waveform)
        mid = TRACK_HEIGHT / 2.0
        path = QPainterPath()
        path.moveTo(x, mid)
        for step in range(n):
            px = x + (step / max(1, n - 1)) * w
            amp = float(waveform[step]) * mid
            path.lineTo(px, mid - amp)
        for step in range(n - 1, -1, -1):
            px = x + (step / max(1, n - 1)) * w
            amp = float(waveform[step]) * mid
            path.lineTo(px, mid + amp)
        path.closeSubpath()
        return path

    def _draw_waveform(self, x: float, w: float, waveform):
        """Draw a track's decoded RMS envelope inside its rect, scaled to
        TRACK_HEIGHT, behind the overlap/curve-handle rendering (z-value 0,
        default below the overlap region's z-value 5 - dp-163). Unchanged by
        dp-171 - the overlap band gets a separate, additional redraw (see
        _draw_overlap_waveforms) so non-overlapping regions have zero visual
        regression."""
        from PyQt6.QtWidgets import QGraphicsPathItem

        path = self._build_waveform_path(x, w, waveform)

        item = QGraphicsPathItem(path)
        item.setBrush(QBrush(QColor(C_TIMELINE_CURVE)))
        item.setPen(QPen(Qt.PenStyle.NoPen))
        item.setOpacity(0.35)
        item.setZValue(1)
        self._scene.addItem(item)

    def _draw_overlap_waveforms(self, rect: QRectF, overlap_index: int):
        """dp-171: redraw the two tracks bordering this overlap, clipped to
        the overlap rect, above the opaque overlap tint (z=5) at reduced
        opacity - so both waveforms stay visible/distinguishable through the
        overlap band instead of the tint fully occluding them. Purely
        additive: the base per-track waveform draw (_draw_waveform) is
        untouched, so regions outside the overlap are unaffected."""
        from PyQt6.QtGui import QPainterPath
        from PyQt6.QtWidgets import QGraphicsPathItem

        if self._layout is None:
            return
        positions = self._layout.track_positions()
        tracks = self._layout.tracks
        clip = QPainterPath()
        clip.addRect(rect)

        for track_index in (overlap_index, overlap_index + 1):
            if not (0 <= track_index < len(tracks)):
                continue
            waveform = self._waveforms.get(track_index)
            if waveform is None or len(waveform) == 0:
                continue
            x = positions[track_index] * self._pps
            w = max(1.0, tracks[track_index].duration * self._pps)
            full_path = self._build_waveform_path(x, w, waveform)
            clipped = full_path.intersected(clip)
            if clipped.isEmpty():
                continue

            item = QGraphicsPathItem(clipped)
            item.setBrush(QBrush(QColor(C_TIMELINE_CURVE)))
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setOpacity(OVERLAP_WAVEFORM_OPACITY)
            item.setZValue(OVERLAP_WAVEFORM_Z)
            self._scene.addItem(item)

    def _build_curve_path(self, rect: QRectF, gain_fn) -> "QPainterPath":
        """Build a curve line's path from a gain(t) callable - shared by the
        fade-in curve (overlap.evaluate_in) and the independent fade-out
        curve (overlap.evaluate_out, dp-190) so the sampling loop isn't
        duplicated."""
        from PyQt6.QtGui import QPainterPath

        path = QPainterPath()
        steps = 24
        for step in range(steps + 1):
            t = step / steps
            gain = gain_fn(t)
            x = rect.left() + t * rect.width()
            y = rect.top() + (1.0 - gain) * rect.height()
            if step == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        return path

    def _draw_curve(self, rect: QRectF, overlap, overlap_index: int):
        from PyQt6.QtWidgets import QGraphicsPathItem

        item = QGraphicsPathItem(self._build_curve_path(rect, overlap.evaluate_in))
        item.setPen(QPen(QColor(C_TIMELINE_CURVE), 2))
        item.setZValue(8)
        self._scene.addItem(item)
        self._curve_items[(overlap_index, "in")] = item

    def _draw_curve_out(self, rect: QRectF, overlap, overlap_index: int):
        """dp-190: draw the independent fade-out curve as a dashed line in
        a distinct color, so it reads as a separate, secondary curve rather
        than a duplicate of the fade-in line."""
        from PyQt6.QtWidgets import QGraphicsPathItem

        pen = QPen(QColor(C_TIMELINE_CURVE_OUT), 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        item = QGraphicsPathItem(self._build_curve_path(rect, overlap.evaluate_out))
        item.setPen(pen)
        item.setZValue(8)
        self._scene.addItem(item)
        self._curve_items[(overlap_index, "out")] = item

    # ── Handle drag callback ─────────────────────────────────────────────

    def _reset_overlap_curve(self, overlap_index: int, curve_role: str = "in"):
        """dp-166: reset an overlap's curve to its default bezier
        (double-click on one of its handles). dp-190: resets only the
        curve_role ("in"/"out") the double-clicked handle belongs to, not
        both curves at once."""
        from core.crossfade_model import DEFAULT_CURVE_P1, DEFAULT_CURVE_P2
        if self._layout is None:
            return
        if curve_role == "out":
            self._layout.set_overlap_curve_out(overlap_index, DEFAULT_CURVE_OUT_P1, DEFAULT_CURVE_OUT_P2)
        else:
            self._layout.set_overlap_curve(overlap_index, DEFAULT_CURVE_P1, DEFAULT_CURVE_P2)
        self.sig_overlap_changed.emit(overlap_index)
        QTimer.singleShot(0, self._render)

    def _set_overlap_curve_linear(self, overlap_index: int, curve_role: str = "in"):
        """dp-180: set an overlap's curve to a straight P0-P3 line. Placing
        both control points on the diagonal makes all four bezier points
        collinear, which reduces the cubic bezier to the identity ramp -
        a true linear fade, not an approximation.

        dp-190: the fade-out curve's P0-P3 line runs from (0,1) to (1,0)
        instead of (0,0) to (1,1), so its collinear control points are
        (1/3, 2/3) and (2/3, 1/3) - the same line, expressed for the
        fade-out's own endpoints. Only affects curve_role's own curve."""
        if self._layout is None:
            return
        if curve_role == "out":
            self._layout.set_overlap_curve_out(overlap_index, (1 / 3, 2 / 3), (2 / 3, 1 / 3))
        else:
            self._layout.set_overlap_curve(overlap_index, (1 / 3, 1 / 3), (2 / 3, 2 / 3))
        self.sig_overlap_changed.emit(overlap_index)
        QTimer.singleShot(0, self._render)

    def _on_handle_moved(
        self, overlap_index: int, which: int, norm_x: float, norm_y: float,
        curve_role: str = "in",
    ):
        if self._layout is None:
            return
        ov = self._layout.overlaps[overlap_index]
        point = (norm_x, norm_y)
        if curve_role == "out":
            out_p1 = ov.curve_out_p1 if ov.curve_out_p1 is not None else DEFAULT_CURVE_OUT_P1
            out_p2 = ov.curve_out_p2 if ov.curve_out_p2 is not None else DEFAULT_CURVE_OUT_P2
            if which == 0:
                self._layout.set_overlap_curve_out(overlap_index, point, out_p2)
            else:
                self._layout.set_overlap_curve_out(overlap_index, out_p1, point)
        else:
            if which == 0:
                self._layout.set_overlap_curve(overlap_index, point, ov.curve_p2)
            else:
                self._layout.set_overlap_curve(overlap_index, ov.curve_p1, point)
        self.sig_overlap_changed.emit(overlap_index)
        # dp-180: this callback runs synchronously from inside Qt's own
        # itemChange() during a live drag (ItemIsMovable) - the handle item
        # is the active mouse grabber at this point. A full self._render()
        # (immediate or deferred via QTimer) calls scene.clear(), which
        # deletes that same handle item mid-drag and silently breaks/ends
        # the drag after the very first pixel of movement. Update only the
        # curve line in place instead - the handle's own position is
        # already being driven live by Qt's item-move machinery, so it
        # needs no rebuild.
        self._update_curve_visual(overlap_index, curve_role)

    def _update_curve_visual(self, overlap_index: int, curve_role: str = "in"):
        """Redraw one overlap's curve line without touching the scene's
        other items - safe to call mid-drag (see _on_handle_moved). dp-190:
        only touches the curve_role ("in"/"out") that actually moved."""
        item = self._curve_items.get((overlap_index, curve_role))
        if item is None or self._layout is None:
            return
        rect = self._overlap_rect(overlap_index)
        if rect is None:
            return
        overlap = self._layout.overlaps[overlap_index]
        gain_fn = overlap.evaluate_out if curve_role == "out" else overlap.evaluate_in
        item.setPath(self._build_curve_path(rect, gain_fn))

    # ── Track drag (left-only, rigid chain, delegated to the model) ─────

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self._view.viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                self._start_track_drag(event)
            elif event.type() == QEvent.Type.MouseMove and self._drag_index is not None:
                self._update_track_drag(event)
            elif event.type() == QEvent.Type.MouseButtonRelease and self._drag_index is not None:
                self._drag_index = None
                # dp-222: force one final full-fidelity render immediately on
                # release rather than leaving the last frame to the throttle
                # timer - a released drag must look identical to a
                # non-throttled render (overlap waveforms restored).
                self._drag_render_timer.stop()
                self._track_drag_active = False
                self._render()
        return super().eventFilter(obj, event)

    def _track_at(self, scene_pos: QPointF):
        item = self._scene.itemAt(scene_pos, self._view.transform())
        if isinstance(item, (QGraphicsRectItem, QGraphicsLineItem)):
            idx = item.data(0)
            if idx is not None:
                return int(idx)
        return None

    def _start_track_drag(self, event):
        if self._layout is None:
            return
        # dp-222: a right-click (or any non-left button) must not start a
        # drag a subsequent mouse-move would then act on.
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # dp-222: belt-and-braces. _track_drag_active is normally cleared by
        # the MouseButtonRelease branch of eventFilter(), but a release that
        # never reaches us (released outside the viewport, grab broken by a
        # modal/focus change) would leave it stuck True - and every later
        # render would then silently skip the overlap-waveform pass, making
        # overlaps invisible with no error. Clearing it here guarantees a
        # fresh drag always starts from a known state.
        self._track_drag_active = False
        scene_pos = self._view.mapToScene(event.pos())
        idx = self._track_at(scene_pos)
        # Only tracks with a predecessor (idx >= 1) can be dragged - the
        # first track has no overlap to its left.
        if idx is None or idx < 1:
            return
        self._drag_index = idx
        self._drag_start_x = scene_pos.x()
        self._drag_start_overlap = self._layout.overlaps[idx - 1].duration

    def _update_track_drag(self, event):
        if self._layout is None or self._drag_index is None:
            return
        scene_pos = self._view.mapToScene(event.pos())
        delta_px  = scene_pos.x() - self._drag_start_x
        delta_sec = delta_px / self._pps
        # Dragging left (negative delta_px) increases overlap.
        new_overlap = self._drag_start_overlap - delta_sec
        self._layout.set_overlap_duration(self._drag_index - 1, new_overlap)
        self.sig_overlap_changed.emit(self._drag_index - 1)
        # dp-222: coalesce renders to a max ~60Hz cadence instead of a full
        # scene.clear() + rebuild on every mouse-move. Multiple moves inside
        # one timer interval collapse into a single render when it fires.
        self._track_drag_active = True
        if not self._drag_render_timer.isActive():
            self._drag_render_timer.start()

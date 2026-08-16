"""
Main application window - Winamp-style layout.
ArtNet bridge is lazily resolved through _bridge() so it can be
instantiated after QApplication is created.
"""

import functools
import os
import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFileDialog, QFrame, QSizePolicy, QMessageBox
)
from PyQt6.QtCore  import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui   import QAction, QActionGroup, QKeySequence, QIcon
from PyQt6           import sip

from core.engine        import engine, STATE_PLAYING, STATE_PAUSED, STATE_STOPPED
from core.deck_engine   import FOLLOW_SYSTEM_DEFAULT, list_output_devices
from core.version       import __version__
from core.playlist      import playlist, SHOW_EXTENSION, ShowFileError
from core.analyzer      import WaveformAnalyzer
from core             import artnet_bridge as _ab_module
from core.artnet_config import artnet_config
from core.crossfade_model   import CrossfadeLayout, Overlap
from core.crossfade_markers import crossfade_marker_positions

from ui.waveform_widget      import WaveformWidget
from ui.preview_waveform_widget import PreviewWaveformWidget, resolve_preview_markers
from ui.crossfade_scrub_slider import CrossfadeScrubSlider
from ui.playlist_widget      import PlaylistWidget
from ui.transport_widget     import TransportWidget

from ui.artnet_map_window    import ArtNetMapWindow
from ui.crossfade_dialog     import CrossfadeDialog

from config.settings import settings
import ui.skin as skin
from ui.skin import (
    C_ACCENT, C_TEXT_DIM, make_font, make_display_font,
    FONT_SIZE_SMALL, FONT_SIZE_DISPLAY, FONT_SIZE_TIMECODE
)


def _bridge():
    """Return the ArtNet bridge singleton (lazily created in main.py)."""
    return _ab_module.artnet_bridge


class _HoverTimecodeLabel(QLabel):
    """dp-198: QLabel that emits hover enter/leave signals. A plain QLabel
    doesn't expose hover callbacks on its own — enterEvent/leaveEvent are
    QWidget primitives available without any extra flags/mouse tracking,
    so a thin subclass is the smallest way to surface them."""

    sig_hover_enter = pyqtSignal()
    sig_hover_leave = pyqtSignal()
    sig_clicked     = pyqtSignal()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.sig_hover_enter.emit()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.sig_hover_leave.emit()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.sig_clicked.emit()


class MainWindow(QMainWindow):

    # ── Thread-safe signals ───────────────────────────────────────────────────
    _artnet_action_signal    = pyqtSignal(str, int, int)
    _track_ended_signal      = pyqtSignal()
    _position_signal         = pyqtSignal(float)
    _waveform_ready_signal   = pyqtSignal(str, object, float)
    _preview_waveform_ready_signal = pyqtSignal(str, object, float)
    _playlist_changed_signal = pyqtSignal()
    _track_changed_signal    = pyqtSignal(int, object)
    _files_scanned_signal    = pyqtSignal(list)
    _engine_track_changed_signal = pyqtSignal(str)
    _crossfade_progress_signal   = pyqtSignal(bool, float, int)

    def __init__(self):
        super().__init__()
        # No version number in the title bar. The running version already
        # lives in Help -> About, sourced from the VERSION file (dp-227); a
        # hardcoded one here is just a second place to forget to update --
        # which is exactly what happened: "v2" survived into the v3.0.0
        # release.
        self.setWindowTitle("SAVA - Synchronizing Audio Via Art-net")
        self.resize(800, 600)
        self.setAcceptDrops(True)

        self.move(settings.get("window_x", 100), settings.get("window_y", 100))

        if settings.get("always_on_top", False):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Window icon
        icon_path = Path(__file__).resolve().parent.parent / "assets" / "sava.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._shortcuts_enabled = True
        self._keyboard_actions  = []
        self._about_logo_clicks = 0  # dp-264: About-logo Easter egg counter

        # Crossfade state (dp-160/dp-216 Phase 5): persisted layout (may be
        # None). The actual overlap ramp + gapless rotation both now live
        # inside DeckEngine (engine.preload / engine.arm_crossfade) -- this
        # window only decides WHAT to preload/arm (see _rearm_preload) and
        # reacts to engine.on_track_changed once a swap has happened.
        self._crossfade_layout = CrossfadeLayout.load()
        self._crossfade_dialog = None

        # dp-216 Phase 5: set by _advance_to when a MANUAL swap_to_preloaded()
        # succeeds, consumed by _on_engine_track_changed. Distinguishes "the
        # caller already advanced the playlist before requesting this swap"
        # (manual Next) from "the engine swapped on its own and the playlist
        # still points at the finished track" (natural end / crossfade
        # finalize). Comparing filepaths instead would misread a playlist with
        # two ADJACENT DUPLICATE tracks as already-advanced and stick the
        # index forever. Both writer and reader run on the Qt main thread and
        # the single idle deck serializes swaps, so this cannot be consumed by
        # the wrong event.
        self._pending_manual_swap = False

        # dp-218: read-only preview of the next-up track (the idle deck's
        # target). Each decode gets its OWN WaveformAnalyzer instance (see
        # _kick_preview_analysis) rather than a shared singleton, which would
        # let a stale decode's result race a re-target and paint the wrong
        # waveform.
        self._preview_target = None
        self._preview_target_id = None  # dp-238: the queued row's track_id

        # dp-234: the primary (now-playing) waveform decode gets the same
        # treatment as the preview above. A crossfade/replay can start a
        # second decode while the first is still in flight (deck swap fires
        # analyze() via _on_engine_track_changed, then the user replays a
        # track before it finishes); with a shared analyzer singleton and no
        # filepath check, the finishing decode wrote its duration onto
        # whatever track happened to be selected at that instant, not the
        # track it was decoding. `_primary_target` mirrors `_preview_target`.
        self._primary_target = None

        self._setup_ui()
        self._setup_menu()
        self._setup_status_bar()

        # dp-185: the window's interactive resize floor is set explicitly
        # here rather than left to layout auto-propagation, because a
        # top-level QMainWindow does not reliably clamp interactive resizes
        # to its central layout's computed minimum. Derive the width floor
        # from the transport row's own minimum (set in TransportWidget)
        # instead of guessing, so it can never again drift narrower than
        # what that row actually needs to render without overlap/crop.
        self.setMinimumSize(max(640, self._transport.minimumWidth()), 520)

        # Connect internal thread-safe signals
        self._artnet_action_signal.connect(self._on_artnet_action)
        self._track_ended_signal.connect(self._on_track_ended)
        self._position_signal.connect(self._on_engine_position)
        self._waveform_ready_signal.connect(self._on_waveform_ready)
        self._preview_waveform_ready_signal.connect(self._on_preview_waveform_ready)
        self._playlist_changed_signal.connect(self._refresh_playlist_widget)
        self._track_changed_signal.connect(self._on_playlist_track_changed)
        self._files_scanned_signal.connect(self._on_files_scanned)
        self._engine_track_changed_signal.connect(self._on_engine_track_changed)
        self._crossfade_progress_signal.connect(self._on_crossfade_progress)

        self._connect_engine()
        self._connect_playlist()
        self._connect_artnet()
        self._connect_transport()
        self._connect_waveform()
        self._connect_playlist_widget()

        # ArtNet config auto-reload state (dp-179)
        self._artnet_status_hold_until = 0.0
        self._artnet_config_mtime = self._artnet_config_file_mtime()

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start(100)

        # dp-247: the INI's `[network] enabled` is the SINGLE source of truth
        # for whether the listener runs -- see _on_toggle_artnet.
        if artnet_config.listen_enabled:
            b = _bridge()
            if b:
                b.on_action = lambda fn, val, thr: self._on_artnet_action(fn, val, thr)
                b.start()

        self._refresh_playlist_widget()
        if playlist.count > 0:
            self._playlist_widget.set_current(playlist.current_index)

    # ── Drag and drop ─────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        files   = []
        folders = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not path:
                continue
            p = Path(path)
            if p.is_dir():
                folders.append(str(p))
            elif p.is_file():
                files.append(str(p))

        # dp-245 D8: route through the async scan, same as the File menu
        # (_on_add_files / _on_add_folder) -- the synchronous add_files/
        # add_folder used to run right here on the Qt main thread, so
        # dropping a large folder froze the window while the menu path
        # (already async) did not. commit_scanned() and the status text are
        # handled by _on_files_scanned via the existing on_files_scanned
        # wiring; each drop item just kicks its own background scan.
        if files:
            self._status_left.setText(f"Scanning {len(files)} files…")
            playlist.add_files_async(filepaths=files)
        for folder in folders:
            self._status_left.setText("Scanning folder…")
            playlist.add_files_async(folder=folder)
        event.acceptProposedAction()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        info_frame = QFrame()
        info_frame.setFixedHeight(48)
        info_lay = QHBoxLayout(info_frame)
        info_lay.setContentsMargins(8, 2, 8, 2)

        self._lbl_title = QLabel("No track loaded")
        self._lbl_title.setFont(make_display_font(FONT_SIZE_DISPLAY))
        self._lbl_title.setStyleSheet(f"color: {C_ACCENT}; background: transparent;")
        self._lbl_title.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._lbl_time = _HoverTimecodeLabel("0:00 / 0:00")
        self._lbl_time.setFont(make_display_font(FONT_SIZE_TIMECODE))
        self._lbl_time.setStyleSheet(f"color: {C_ACCENT}; background: transparent;")
        self._lbl_time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # dp-198: hover-triggered magnifier popup showing the same live
        # timecode at ~2x size. Built lazily in _show_timecode_popup so
        # nothing extra happens on startup if the user never hovers.
        self._timecode_popup = None
        self._lbl_time.sig_hover_enter.connect(self._show_timecode_popup)
        self._lbl_time.sig_hover_leave.connect(self._hide_timecode_popup)
        # dp-213: click cycles elapsed -> remaining -> both -> elapsed.
        # Session-only state, not persisted.
        self._timecode_mode = 0
        self._lbl_time.sig_clicked.connect(self._on_timecode_clicked)
        self._lbl_info = QLabel("")
        self._lbl_info.setFont(make_font(FONT_SIZE_SMALL))
        self._lbl_info.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")

        # dp-217, repurposed 2026-08-02: originally a DeckEngine re-buffer
        # indicator (D7), but live testing showed that condition never
        # actually fires -- playback starts before a re-buffer pause is ever
        # needed. What really lags with no feedback is the PRIMARY WAVEFORM
        # decode, which can take seconds on a long track. Repointed at that
        # instead: shown while _kick_primary_analysis has a decode in
        # flight, cleared by _on_waveform_ready (both the accepted AND the
        # dp-234 stale-drop paths -- see that method's comment for why the
        # drop path must clear too). Fixed width reserved up front and text
        # toggled empty/set, rather than adding/removing the widget, so it
        # never reflows the info bar when it appears.
        #
        # `border: none` is load-bearing, not cosmetic. Qt stylesheet
        # selectors match SUBCLASSES, and QLabel derives from QFrame -- so
        # ui/skin.py's `QFrame { border: 1px solid ...; border-radius: 8px }`
        # applies to every QLabel in the app. That is what gives the title,
        # track-counter and timecode labels their panel look, but on a label
        # whose text is empty it renders as a bare, unexplained box sitting
        # in the info bar (reported by the user as "an additional UI element
        # that I don't know what it's for"). Dropping the border makes the
        # indicator genuinely invisible until it has something to say.
        self._lbl_buffering = QLabel("")
        self._lbl_buffering.setFont(make_font(FONT_SIZE_SMALL))
        self._lbl_buffering.setStyleSheet(
            f"color: {C_ACCENT}; background: transparent; border: none;"
        )
        self._lbl_buffering.setToolTip(
            "Shows 'Buffering waveform' while the waveform for the current "
            "track is being decoded"
        )
        # dp-217: widened 80 -> 200px for "Buffering waveform" (measured
        # 198px at FONT_SIZE_SMALL). The old 80px already clipped the
        # shorter "Loading…" by 8px -- this keeps the whole label readable
        # without letting it grow unbounded and reflow the bar.
        self._lbl_buffering.setFixedWidth(200)
        self._lbl_buffering.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        # dp-240: buffering indicator moved left of the track counter, and
        # the counter moved right next to the timecode (tighter gap there
        # than the gap before it) so the two numeric readouts read as one
        # cluster.
        info_lay.addWidget(self._lbl_title)
        info_lay.addStretch()
        info_lay.addWidget(self._lbl_buffering)
        info_lay.addSpacing(16)
        info_lay.addWidget(self._lbl_info)
        info_lay.addSpacing(6)
        info_lay.addWidget(self._lbl_time)
        root.addWidget(info_frame)

        self._waveform_widget = WaveformWidget()
        root.addWidget(self._waveform_widget)

        # dp-219: manual scrub control for a live crossfade's gain schedule,
        # seated between the interactive waveform and dp-218's preview
        # waveform per that ticket's layout note.
        self._crossfade_scrub_slider = CrossfadeScrubSlider()
        self._crossfade_scrub_slider.gain_seek_requested.connect(
            self._on_crossfade_gain_seek_requested
        )
        root.addWidget(self._crossfade_scrub_slider)

        # dp-218: read-only preview of the next-up track, stacked below the
        # interactive waveform (and dp-219's scrub slider above it).
        self._preview_waveform = PreviewWaveformWidget()
        root.addWidget(self._preview_waveform)

        self._transport = TransportWidget()
        self._transport.set_master_volume(settings.get("master_volume", 80))
        self._transport.set_shuffle(settings.get("shuffle", False))
        self._transport.set_repeat(settings.get("repeat", "none"))
        root.addWidget(self._transport)

        self._playlist_widget = PlaylistWidget()
        root.addWidget(self._playlist_widget, stretch=1)

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _setup_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("File")
        self._add_action(file_menu, "Add files…",     self._on_add_files,     "Ctrl+O",       kb=True)
        self._add_action(file_menu, "Add folder…",    self._on_add_folder,    "Ctrl+Shift+O", kb=True)
        file_menu.addSeparator()
        self._add_action(file_menu, "Import M3U…",    self._on_import_m3u)
        self._add_action(file_menu, "Export M3U…",    self._on_export_m3u)
        self._add_action(file_menu, "Import PLS…",    self._on_import_pls)
        self._add_action(file_menu, "Export PLS…",    self._on_export_pls)
        file_menu.addSeparator()
        self._add_action(file_menu, "Import Show…",   self._on_import_show)
        self._add_action(file_menu, "Export Show…",   self._on_export_show)
        file_menu.addSeparator()
        self._add_action(file_menu, "Clear playlist", self._on_clear_playlist)
        file_menu.addSeparator()
        self._add_action(file_menu, "Exit",           self.close,             "Ctrl+Q",       kb=True)

        pb_menu = mb.addMenu("Playback")
        shortcuts_menu = pb_menu.addMenu("Shortcuts")
        self._add_action(shortcuts_menu, "Play / Pause",   self._on_play_pause,  "Space",        kb=True)
        self._add_action(shortcuts_menu, "Stop",           self._on_stop,        ".",            kb=True)
        self._add_action(shortcuts_menu, "Next track",     self._on_next,        "Ctrl+Right",   kb=True)
        self._add_action(shortcuts_menu, "Previous track", self._on_prev,        "Ctrl+Left",    kb=True)
        shortcuts_menu.addSeparator()
        self._add_action(shortcuts_menu, "Set Loop A",     lambda: engine.set_loop_a(),    "Ctrl+[",       kb=True)
        self._add_action(shortcuts_menu, "Set Loop B",     lambda: engine.set_loop_b(),    "Ctrl+]",       kb=True)
        self._add_action(shortcuts_menu, "Toggle Loop",    lambda: self._on_loop_toggle(), "Ctrl+L",       kb=True)
        self._add_action(shortcuts_menu, "Clear Loop",     lambda: self._on_loop_clear(),  "Ctrl+Shift+L", kb=True)
        pb_menu.addSeparator()
        self._add_action(pb_menu, "Crossfade…",     self._on_show_crossfade_dialog)
        pb_menu.addSeparator()
        # dp-223: rebuilt every time it opens, so a device plugged in after
        # launch shows up without restarting SAVA.
        self._audio_menu = pb_menu.addMenu("Audio Output")
        self._audio_menu.aboutToShow.connect(self._rebuild_audio_device_menu)

        view_menu = mb.addMenu("View")
        self._act_on_top = QAction("Always on top", self, checkable=True)
        self._act_on_top.setChecked(settings.get("always_on_top", False))
        self._act_on_top.triggered.connect(self._on_always_on_top)
        view_menu.addAction(self._act_on_top)
        view_menu.addSeparator()
        self._act_shortcuts = QAction("Keyboard shortcuts enabled", self, checkable=True)
        self._act_shortcuts.setChecked(True)
        self._act_shortcuts.triggered.connect(self._on_toggle_shortcuts)
        view_menu.addAction(self._act_shortcuts)
        view_menu.addSeparator()

        theme_menu = view_menu.addMenu("Theme")
        active = skin.current_theme()
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        # dp-264: "toplo" is an Easter-egg theme -- hidden from the submenu
        # until unlocked (click the About logo 6x, then restart). The menu is
        # built once at startup from the persisted flag, same as every other
        # restart-applied theme choice, so an unlock only takes effect after
        # the next launch.
        #
        # `or active == "toplo"` closes a dangling state: the theme actually
        # in force comes from settings["theme"], which is independent of the
        # unlock flag. If a settings file carries theme=toplo with the flag
        # false (hand-edited JSON, or a partial settings migration), hiding
        # the entry leaves the app RUNNING in Toplo with nothing checked in
        # the submenu and no indication of what's active. Never hide the
        # theme that is currently applied -- it reveals nothing the user
        # doesn't already have on screen.
        toplo_unlocked = settings.get("toplo_unlocked", False) or active == "toplo"
        for name in skin.THEME_NAMES:
            if name == "toplo" and not toplo_unlocked:
                continue
            act = QAction(skin.THEME_LABELS[name], self, checkable=True)
            act.setChecked(name == active)
            act.triggered.connect(lambda _checked, n=name: self._on_theme_selected(n))
            self._theme_group.addAction(act)
            theme_menu.addAction(act)

        artnet_menu = mb.addMenu("ArtNet")
        self._add_action(artnet_menu, "Configure DMX mapping…", self._on_show_map_window)
        artnet_menu.addSeparator()
        artnet_advanced_menu = artnet_menu.addMenu("Advanced")
        self._add_action(artnet_advanced_menu, "Open config file… (advanced)", self._on_open_artnet_config_file)
        self._add_action(artnet_advanced_menu, "Reload config",         self._on_reload_artnet_config)
        artnet_menu.addSeparator()
        self._act_artnet = QAction("Enable ArtNet listener", self, checkable=True)
        self._act_artnet.setChecked(artnet_config.listen_enabled)
        self._act_artnet.triggered.connect(self._on_toggle_artnet)
        artnet_menu.addAction(self._act_artnet)

        help_menu = mb.addMenu("Help")
        self._add_action(help_menu, "Instructions / How to use…", self._on_help_instructions)
        help_menu.addSeparator()
        self._add_action(help_menu, "About SAVA", self._on_about)

    def _add_action(self, menu, label, slot, shortcut=None, kb=False):
        act = QAction(label, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        menu.addAction(act)
        if kb and shortcut:
            self._keyboard_actions.append(act)
        return act

    # ── Status bar ────────────────────────────────────────────────────────────

    def _setup_status_bar(self):
        sb = self.statusBar()
        self._status_left  = QLabel("Stopped")
        self._status_right = QLabel("ArtNet: off")
        for lbl in (self._status_left, self._status_right):
            lbl.setFont(make_font(FONT_SIZE_SMALL))
            lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        sb.addWidget(self._status_left)
        sb.addPermanentWidget(self._status_right)

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _connect_engine(self):
        engine.on_track_end     = lambda: self._track_ended_signal.emit()
        engine.on_position      = lambda pos: self._position_signal.emit(pos)
        engine.on_track_changed = lambda fp: self._engine_track_changed_signal.emit(fp)
        # dp-217 repurpose (2026-08-02): engine.on_buffering deliberately left
        # unwired. Live testing showed DeckEngine's re-buffer pause never
        # actually fires in practice (playback starts before a re-buffer
        # would ever be needed) -- the real, observed lag is the primary
        # waveform decode, which _lbl_buffering now indicates instead (see
        # _kick_primary_analysis / _on_waveform_ready). Wiring both sources
        # to the same label would let them fight over one text field for no
        # practical benefit; DeckEngine itself is untouched and still fires
        # the callback if anything ever calls it.
        engine.on_crossfade_progress = (
            lambda running, t, length: self._crossfade_progress_signal.emit(
                running, t, length
            )
        )

    def _on_crossfade_progress(self, running: bool, t: float, length: int):
        self._crossfade_scrub_slider.set_progress(running, t)

    def _on_crossfade_gain_seek_requested(self, t: float):
        """Convert the slider's normalized drag fraction to the frame count
        engine.seek_crossfade_gain() expects.

        The length is read from the engine at DRAG TIME, not cached from the
        last on_crossfade_progress signal. That signal only fires at the poll
        thread's 10 Hz, so for up to ~100ms after a crossfade begins the
        cached length is still 0 -- and `int(t * 0)` is 0, which would slam
        the gain schedule back to the very start of the fade instead of where
        the user dragged it.
        """
        _running, _t, length = engine.crossfade_progress
        if length <= 0:
            return
        engine.seek_crossfade_gain(int(t * length))

    def _connect_playlist(self):
        playlist.on_track_changed    = lambda idx, meta: self._track_changed_signal.emit(idx, meta)
        playlist.on_playlist_changed = lambda: self._playlist_changed_signal.emit()
        playlist.on_files_scanned    = lambda metas: self._files_scanned_signal.emit(metas)

    def _connect_artnet(self):
        b = _bridge()
        if b:
            b.on_action = lambda fn, val, thr: self._on_artnet_action(fn, val, thr)

    def _connect_transport(self):
        t = self._transport
        t.sig_play.connect(self._on_play)
        t.sig_pause.connect(self._on_pause)
        t.sig_stop.connect(self._on_stop)
        t.sig_next.connect(self._on_next)
        t.sig_prev.connect(self._on_prev)
        t.sig_master_vol.connect(engine.set_master_volume)
        t.sig_shuffle.connect(playlist.set_shuffle)
        t.sig_repeat.connect(playlist.set_repeat)
        t.sig_loop_a.connect(self._on_loop_a)
        t.sig_loop_b.connect(self._on_loop_b)
        t.sig_loop_toggle.connect(self._on_loop_toggle)
        t.sig_loop_clear.connect(self._on_loop_clear)
        t.sig_cue_set.connect(self._on_cue_set)
        t.sig_cue_jump.connect(self._on_cue_jump_requested)
        t.sig_cue_clear_all.connect(self._on_cue_clear_all)
        t.sig_fin_set.connect(self._on_fin_set)
        t.sig_fin_clear.connect(self._on_fin_clear)
        t.sig_start_set.connect(self._on_start_set)
        t.sig_start_clear.connect(self._on_start_clear)

    def _connect_waveform(self):
        self._waveform_widget.seek_requested.connect(self._on_seek_requested)

    def _kick_primary_analysis(self, filepath):
        """dp-234: start a primary-waveform decode bound to `filepath`, the
        same pattern _kick_preview_analysis uses (dp-218) -- a fresh
        WaveformAnalyzer per decode, filepath captured in the closure, so a
        later decode for a different track can never be mistaken for this
        one when its result lands."""
        self._primary_target = filepath
        self._lbl_buffering.setText("Buffering waveform")  # dp-217 repurpose
        decoder = WaveformAnalyzer()
        decoder.on_ready = (
            lambda wf: self._waveform_ready_signal.emit(
                filepath, wf, decoder.duration
            )
        )
        decoder.analyze(filepath)

    def _connect_playlist_widget(self):
        pw = self._playlist_widget
        pw.track_activated.connect(self._on_playlist_activated)
        pw.track_volume_changed.connect(self._on_track_volume_changed)
        pw.track_volume_committed.connect(self._on_track_volume_committed)
        pw.remove_requested.connect(self._on_remove_track)
        pw.color_changed.connect(self._on_track_color_changed)
        pw.reordered.connect(playlist.move)
        pw.end_action_changed.connect(self._on_end_action_changed)
        pw.clear_markers_requested.connect(self._on_clear_track_markers)

    # ── Engine callbacks ──────────────────────────────────────────────────────

    def _on_track_ended(self):
        current_idx = playlist.current_index
        action = playlist.get_track_end_action(current_idx) if current_idx >= 0 else "next"

        if action == "loop":
            current_meta = playlist.current
            if current_meta:
                self._load_and_play(current_meta["filepath"])
            return

        if action == "stop":
            self._set_stopped_ui()
            return

        meta = playlist.next()
        if meta:
            self._advance_to(meta)
        else:
            self._set_stopped_ui()

    def _on_engine_position(self, pos: float):
        # dp-247: while the user is dragging the waveform, the needle belongs
        # to them. The engine keeps playing (and keeps reporting position) all
        # through the drag, so without this the 10 Hz updates fight the cursor
        # and the needle visibly snaps back and forth.
        if self._waveform_widget.is_scrubbing:
            return
        self._waveform_widget.set_position(pos)

    def _on_engine_track_changed(self, filepath: str):
        """dp-216 Phase 5: fired by DeckEngine (via on_track_changed) the
        instant it swaps active<->idle decks -- either a gapless auto-advance
        or a crossfade finalize, both triggered sample-accurately INSIDE the
        engine with no Python decision point beforehand. Also fires for a
        manual swap_to_preloaded() (see _advance_to), where the playlist was
        already advanced by the caller before requesting the swap.

        `_pending_manual_swap` -- not a filepath comparison -- is what tells
        those two cases apart. A playlist holding two ADJACENT DUPLICATE
        tracks (same file twice in a row) would make a filepath check read a
        natural swap as "already advanced", pinning current_index forever and
        looping that file instead of moving on.

        On the natural path, playlist.next() reshuffles at a shuffle+repeat=all
        wrap (R11), so it can diverge from the track the engine actually
        swapped to (preloaded via the non-reshuffling peek_next()); if so,
        force-correct the selection to what's actually sounding rather than
        leave the UI pointing at the wrong track. next() is also a harmless
        no-op under repeat=one, which returns the current track unchanged."""
        if self._pending_manual_swap:
            self._pending_manual_swap = False  # playlist already advanced
        else:
            meta = playlist.next()
            if not meta or meta["filepath"] != filepath:
                idx = next(
                    (i for i, t in enumerate(playlist.tracks) if t["filepath"] == filepath),
                    -1,
                )
                if idx >= 0:
                    playlist.select(idx)
        self._set_playing_ui()
        self._waveform_widget.clear()
        self._waveform_widget.set_loop_points(None, None)
        self._waveform_widget.set_cue_points(engine.cue_points)
        self._sync_cue_buttons()
        if engine.end_marker is not None:
            self._waveform_widget.set_end_marker(engine.end_marker)
        else:
            self._waveform_widget.clear_end_marker()
        if engine.start_marker is not None:
            self._waveform_widget.set_start_marker(engine.start_marker)
        else:
            self._waveform_widget.clear_start_marker()
        tv = engine.track_volume  # dp-237: row-keyed, engine already resolved it at load
        self._playlist_widget.update_track_volume_display(tv)
        # dp-245 D7: a bare `QTimer.singleShot(200, lambda: ...)` closes over
        # self with no lifetime tie to it -- if the window is destroyed
        # inside that 200ms window the lambda fires against a dead C++
        # widget and crashes. PyQt6's singleShot has no context-object
        # overload (only (msec, slot) and (msec, timerType, slot)) to give
        # Qt itself that tie, so the callable checks `sip.isdeleted(self)`
        # before touching any widget -- the same effect (drop the call once
        # the receiver is gone) without it.
        QTimer.singleShot(200, functools.partial(self._deferred_primary_analysis, filepath))
        self._rearm_preload()  # arm the track after this one

    def _deferred_primary_analysis(self, filepath: str):
        """dp-245 D7: guarded target for the 200ms primary-analysis defer in
        _on_engine_track_changed -- see the comment there."""
        if sip.isdeleted(self):
            return
        self._kick_primary_analysis(filepath)

    def _sync_cue_buttons(self):
        """Repaint the transport's 8 cue buttons to match the CURRENT track's
        cue points.

        Cue-button highlights were only ever set (on cue-set) or wiped (on
        Clear All Cues) -- nothing reset them on a track change, so the
        previous track's lit cue buttons persisted onto a track that had no
        cues there at all. Same class of stale-highlight bug dp-199 fixed for
        the Fin marker; this is the cue-button half."""
        cues = engine.cue_points
        self._transport.clear_all_cue_buttons()
        for idx, pos in cues.items():
            if pos is not None:
                self._transport.set_cue_active(idx, True)

    # ── Audio output device (dp-223) ──────────────────────────────────────

    def _rebuild_audio_device_menu(self):
        """Populate the Audio Output submenu from a LIVE device enumeration.

        Rebuilt on every open rather than once at startup: output devices
        come and go (dock plugged in, monitor woken, driver restarted), and a
        menu built at launch would list stale entries and hide new ones.
        """
        self._audio_menu.clear()
        group = QActionGroup(self)
        group.setExclusive(True)
        current = settings.get("output_device_name", FOLLOW_SYSTEM_DEFAULT)

        act_default = QAction("Follow Windows default", self, checkable=True)
        act_default.setChecked(current == FOLLOW_SYSTEM_DEFAULT)
        act_default.triggered.connect(
            lambda: self._on_audio_device_chosen(FOLLOW_SYSTEM_DEFAULT)
        )
        group.addAction(act_default)
        self._audio_menu.addAction(act_default)
        self._audio_menu.addSeparator()

        devices = list_output_devices()
        if not devices:
            act_none = QAction("(no output devices found)", self)
            act_none.setEnabled(False)
            self._audio_menu.addAction(act_none)
        for _index, name, api in devices:
            act = QAction(f"{name}  [{api}]", self, checkable=True)
            act.setChecked(current == name)
            act.triggered.connect(
                lambda _checked=False, n=name: self._on_audio_device_chosen(n)
            )
            group.addAction(act)
            self._audio_menu.addAction(act)

        self._audio_menu.addSeparator()
        active = getattr(engine, "output_device_name", None)
        act_active = QAction(f"Currently playing on: {active or 'none'}", self)
        act_active.setEnabled(False)
        self._audio_menu.addAction(act_active)

    def _on_audio_device_chosen(self, name: str):
        """Switch output device. Playback stops -- see
        DeckEngine.set_output_device for why re-decoding is unavoidable when
        the sample rate can change under us."""
        actual = engine.set_output_device(name)
        self._set_stopped_ui()
        self._waveform_widget.clear()
        self._preview_waveform.set_empty()
        self._preview_target = None
        self._preview_target_id = None
        if actual:
            self._status_left.setText(f"Audio output: {actual} — press Play")
        else:
            self._status_left.setText("No usable audio output device")

    # ── Crossfade (dp-160) ────────────────────────────────────────────────

    def _on_show_crossfade_dialog(self):
        if self._crossfade_dialog is None:
            self._crossfade_dialog = CrossfadeDialog(self._crossfade_layout)
            self._crossfade_dialog.sig_layout_saved.connect(self._on_crossfade_layout_saved)
            self._crossfade_dialog.finished.connect(self._on_crossfade_dialog_closed)
            # dp-194: a layout restored from disk (CrossfadeLayout.load(),
            # via to_dict/from_dict) never carries playlist colors - sync
            # them from the current playlist so a fresh open reflects any
            # color assigned since the layout was last saved.
            self._crossfade_dialog.refresh_track_colors()
        self._crossfade_dialog.show()
        self._crossfade_dialog.raise_()
        self._crossfade_dialog.activateWindow()

    def _on_crossfade_dialog_closed(self, *_args):
        self._crossfade_dialog = None

    def _on_crossfade_layout_saved(self, layout: CrossfadeLayout):
        self._crossfade_layout = layout
        self._update_crossfade_markers()
        self._refresh_preview_markers()  # dp-218: queued track's fade markers moved too

    def _update_crossfade_markers(self):
        """dp-216 Phase 3 Part B: resolve and push the current track's static
        crossfade fade-in/fade-out marker positions to the waveform. Reads
        only the saved layout + playlist index -- decoupled from the live
        crossfade ramp (engine.arm_crossfade, wired in by Phase 5's
        _rearm_preload), which is armed separately."""
        idx = playlist.current_index
        cur = playlist.current
        fade_in_end, fade_out_start = crossfade_marker_positions(
            self._crossfade_layout,
            idx,
            cur["filepath"] if cur else None,
            # dp-221: same gate _overlap_for_transition arms on, so the
            # marker can never promise a fade that will not fire.
            next_matches=self._next_is_layout_successor(idx),
        )
        self._waveform_widget.set_crossfade_markers(fade_in_end, fade_out_start)

    def _overlap_for_transition(self, idx: int):
        """The configured Overlap for the transition OUT of track idx, or
        None if no crossfade applies (no layout, layout stale vs the live
        playlist, or overlap duration 0 -> plain gapless). Duck-typed to
        DeckEngine.arm_crossfade's expectation (.duration/.evaluate_in/
        .evaluate_out) since it hands back a core.crossfade_model.Overlap
        directly.

        dp-221: also requires that the track ACTUALLY coming up is the
        layout's linear successor. The crossfade layout is authored as a
        linear timeline -- overlap[i] means "the fade between track i and
        track i+1", with its own duration and its own pair of bezier curves.
        But `_rearm_preload` predicts the incoming track with
        `playlist.peek_next()`, which is shuffle-aware and, under
        `repeat == "one"`, returns the CURRENT track. So without this check
        SAVA applied a curve authored for one pair of tracks to a completely
        different pair -- and under repeat=one crossfaded a track into a
        fresh copy of itself using its neighbour's curve.

        When the incoming track is not the authored successor there is no
        authored answer for that pair, so no crossfade is armed and the
        transition falls back to a plain gapless swap. Refusing is the only
        provably-correct option; inventing a curve for an unauthored pair
        would just be a different wrong answer. (A global default crossfade
        duration for shuffle would be a reasonable FEATURE, but that is a
        product decision, not a bug fix, and belongs in its own ticket.)
        """
        layout = self._crossfade_layout
        if layout is None or not layout.tracks:
            return None
        if idx < 0 or idx >= len(layout.overlaps):
            return None
        cur = playlist.current
        if not cur or layout.tracks[idx].filepath != cur["filepath"]:
            return None  # layout stale vs live playlist
        if not self._next_is_layout_successor(idx):
            return None  # shuffle / repeat=one -> no authored curve for this pair
        ov = layout.overlaps[idx]
        return ov if ov.duration > 0 else None

    def _next_is_layout_successor(self, idx: int) -> bool:
        """True when the track actually coming up (`playlist.peek_next()` --
        the same call `_rearm_preload` uses to choose what to preload) is the
        crossfade layout's linear successor of track `idx`.

        Shared by `_overlap_for_transition` and `_update_crossfade_markers`
        so the armed ramp and the fade markers drawn on the waveform can
        never disagree about whether a crossfade is going to happen."""
        layout = self._crossfade_layout
        if layout is None or not (0 <= idx < len(layout.overlaps)):
            return False
        nxt = playlist.peek_next()
        if not nxt:
            return False
        return nxt["filepath"] == layout.tracks[idx + 1].filepath

    def _rearm_preload(self):
        """dp-216 Phase 5: decide what the idle deck should hold next and
        whether the transition into it should crossfade. Called after every
        track (re)start, engine-driven swap, and playlist change, so the
        prediction always tracks the live playlist. engine.preload()/
        engine.arm_crossfade() are cheap no-ops when already correct
        (see their docstrings) -- both are safe to call unconditionally.

        dp-254: a track whose own end-action is "loop" or "stop" STILL gets
        its successor preloaded (zero-latency manual Next), but must NOT
        auto-advance at natural end -- DeckEngine's audio callback auto-swaps
        to an armed idle deck the instant the active deck truly ends, with no
        Python decision point in between (unlike the old pygame engine, which
        always fired on_track_end and let _on_track_ended decide), so simply
        preloading would make a stop/loop track auto-advance too. DeckEngine
        splits this into two flags (`_idle_armed` = idle deck ready,
        `_auto_advance_armed` = natural-end auto-stitch permitted); this is
        the one caller that ever passes `set_auto_advance(False)`, via
        `engine.preload()` + `engine.set_auto_advance(auto)` below. Keeping
        auto-advance off is what makes _on_track_ended's loop/stop branches
        still reachable."""
        if engine.current_file is None:
            engine.invalidate_preload()
            self._refresh_preview(None)
            return
        idx = playlist.current_index
        action = playlist.get_track_end_action(idx) if idx >= 0 else "next"
        auto = action not in ("loop", "stop")
        next_meta = playlist.peek_next()
        if not next_meta:
            engine.invalidate_preload()
            engine.arm_crossfade(None)
            self._refresh_preview(None)
            return
        engine.preload(next_meta["filepath"], next_meta.get("id"))
        engine.set_auto_advance(auto)
        engine.arm_crossfade(self._overlap_for_transition(idx) if auto else None)
        self._refresh_preview(next_meta["filepath"], next_meta.get("id"))

    def _refresh_preview(self, filepath, track_id=None):
        """dp-218: re-target the read-only preview waveform to `filepath`
        (the idle deck's target, or None if nothing is queued next).
        `track_id` (dp-238) is the queued row's id -- needed so
        _refresh_preview_markers reads that ROW's markers, not just
        whichever row happens to match the filepath first (two rows of the
        same file must show different markers). No-op if the target hasn't
        actually changed -- called from every exit branch of
        _rearm_preload, which itself runs on every track (re)start/swap/
        playlist edit."""
        if filepath == self._preview_target and track_id == self._preview_target_id:
            return
        self._preview_target = filepath
        self._preview_target_id = track_id

        if filepath is None:
            self._preview_waveform.set_empty()
            return

        self._refresh_preview_markers()
        meta = next((t for t in playlist.tracks if t["filepath"] == filepath), None)
        title = meta.get("title") if meta else None
        self._preview_waveform.set_loading(title)
        # Mirror the primary waveform's deferral (_on_engine_track_changed)
        # so a rapid Next chain doesn't spawn a decode per press. dp-245 D7:
        # guarded callable, not a bare lambda -- see the comment on the
        # primary waveform's deferral for why.
        QTimer.singleShot(
            200, functools.partial(self._deferred_preview_analysis, filepath)
        )

    def _deferred_preview_analysis(self, filepath: str):
        """dp-245 D7: guarded target for the 200ms preview-analysis defer in
        _refresh_preview -- see the comment on the primary waveform's
        deferral (_on_engine_track_changed) for why this guard exists."""
        if sip.isdeleted(self):
            return
        self._kick_preview_analysis(filepath)

    def _refresh_preview_markers(self):
        """Re-resolve the preview's marker set for whatever track is
        currently queued. Split out of _refresh_preview so the marker set can
        be refreshed WITHOUT re-targeting (and without restarting the decode):
        the markers come from the row-keyed settings maps (dp-238) and the
        saved crossfade layout, all of which the user can edit while the
        queued track just sits there waiting. Resolving them once at re-target
        time left the preview showing a stale marker set until the next track
        advance."""
        filepath = self._preview_target
        if filepath is None:
            return
        track_id = self._preview_target_id
        # dp-238: match on track_id first -- filepath alone can't tell two
        # rows of the same file apart. Falls back to a filepath match only
        # when no id was carried (defensive; every playlist row has one).
        if track_id is not None:
            idx = next(
                (i for i, t in enumerate(playlist.tracks) if t.get("id") == track_id),
                -1,
            )
        else:
            idx = next(
                (i for i, t in enumerate(playlist.tracks) if t["filepath"] == filepath),
                -1,
            )
        self._preview_waveform.set_markers(
            resolve_preview_markers(track_id, filepath, self._crossfade_layout, idx)
        )

    def _kick_preview_analysis(self, filepath):
        # The target may have moved on again during the 200ms defer.
        if filepath != self._preview_target:
            return
        # dp-218 fix: bind filepath AND a fresh analyzer instance to this one
        # decode (not self._preview_target / self._preview_analyzer, which
        # are shared mutable state a later re-target can overwrite before
        # this decode's on_ready fires). Reading shared state at emit time
        # was the bug: a decode finishing after a re-target would emit the
        # NEW target's filepath, so the staleness check in the slot passed
        # and painted the wrong waveform onto the wrong track.
        decoder = WaveformAnalyzer()
        decoder.on_ready = (
            lambda wf: self._preview_waveform_ready_signal.emit(
                filepath, wf, decoder.duration
            )
        )
        decoder.analyze(filepath)

    def _on_preview_waveform_ready(self, filepath, waveform, duration):
        # Stale decode from a fast Next -- the preview has already
        # re-targeted to something else.
        if filepath != self._preview_target:
            return
        self._preview_waveform.set_waveform(waveform, duration)

    def _advance_to(self, meta: dict):
        """Advance to meta's track: an instant deck swap if the idle deck is
        already armed for it, else a normal (gapped) load. Shared by
        natural-end's fallthrough (idle wasn't armed in time) and the Next
        button -- the two transitions dp-216 Phase 2 makes gap-free.

        Every caller has ALREADY advanced the playlist (via playlist.next())
        to produce `meta`, so flag the swap as manual before requesting it --
        _on_engine_track_changed must not advance a second time."""
        filepath = meta["filepath"]
        if engine.preloaded_file == filepath:
            self._pending_manual_swap = True
            if engine.swap_to_preloaded(filepath):
                return  # on_track_changed (already fired or about to) does the rest
            self._pending_manual_swap = False  # rejected -> no swap event coming
        # Idle deck vanished/wasn't ready between preloaded_file and
        # swap_to_preloaded (race), or wasn't armed at all -> fall back.
        self._load_and_play(filepath, meta.get("id"))

    def _on_cue_jump_requested(self, index: int):
        engine.jump_to_cue(index)

    def _on_seek_requested(self, position: float):
        engine.seek(position)
        # dp-202: on_position is suppressed while stopped (engine.py poll
        # thread), so redraw the needle directly or it won't move until Play.
        self._waveform_widget.set_position(position)
        # engine.seek() queues a cancel_crossfade (A8). If a crossfade was
        # actually running, the callback had already cleared _idle_armed when
        # the ramp triggered, and nothing else re-arms it -- so without this
        # the upcoming transition silently degrades from gapless/crossfaded
        # to a fully gapped load(). Re-arm against the current prediction.
        self._rearm_preload()

    def _on_waveform_ready(self, filepath, waveform, dur):
        # dp-217 repurpose: clear the loading indicator unconditionally here,
        # BEFORE the staleness check below -- not only on the accepted path.
        # A decode this stale (dropped because the target moved on) is still
        # the answer to "is anything loading for the CURRENT target", and if
        # the current target's own decode result was already consumed by an
        # earlier duplicate (dp-234 matches on filepath, not decode
        # instance), no further callback will ever arrive to clear it.
        # Clearing on every arrival, accepted or not, is what stops it
        # sticking on forever after a crossfade-window replay.
        self._lbl_buffering.setText("")
        # dp-234: a stale decode from a track that isn't the one currently
        # playing/selected (replay during a crossfade's overlap window can
        # start a second decode before the first's callback fires). Drop it
        # rather than writing its duration/waveform onto whatever row
        # happens to be selected right now -- the live track's own decode
        # (kicked when IT started) will still deliver its own result.
        if filepath != self._primary_target:
            return
        self._waveform_widget.set_waveform(waveform, dur)
        idx = next(
            (i for i, t in enumerate(playlist.tracks) if t["filepath"] == filepath),
            -1,
        )
        if idx >= 0:
            playlist.update_duration(idx, dur)
            self._playlist_widget.update_duration(idx, dur)
        self._update_crossfade_markers()

    # ── Playlist callbacks ────────────────────────────────────────────────────

    def _on_playlist_track_changed(self, index, meta):
        self._playlist_widget.set_current(index)
        self._update_track_info(meta)

    def _refresh_playlist_widget(self):
        self._playlist_widget.populate(playlist.tracks)
        if playlist.current_index >= 0:
            self._playlist_widget.set_current(playlist.current_index)
        self._invalidate_crossfade_layout_if_stale()
        # dp-192/dp-216: a playlist edit can change which track comes next,
        # so the idle deck may be stale. Re-arm against the new prediction
        # (no-op if the predicted next track is unchanged).
        self._rearm_preload()

    def _invalidate_crossfade_layout_if_stale(self):
        """dp-236: reverses dp-160's blanket "any edit resets everything"
        rule. Re-read dp-160's closing notes first (`.tickets/closed/`) —
        the always-reset rule was never guarding a real failure mode, it
        was just the cheapest correct thing dp-160 could ship: a structural
        filepath-list comparison that either matches exactly or wipes the
        layout. It was not protecting against anything a smarter rebuild
        can't also protect against.

        The layout is always rebuilt from scratch from the live playlist
        (never patched in place), so it is structurally IMPOSSIBLE for it
        to disagree with the playlist afterwards — that's the hard
        consistency constraint from dp-236 (a stale overlap could arm a
        crossfade against the wrong track, which is worse than losing one).
        Preservation works by pair identity, keyed on dp-237's stable
        per-row track id (never filepath — the same file can appear twice):
        an overlap survives iff the same ordered pair of ids is STILL
        adjacent, in the same order, after the edit. Any pair that is new
        — the moved track's own two edges, its old neighbours' edges once
        they meet each other, its new neighbours' edges, an added/removed
        track's edges — has no match and comes back as the layout's normal
        zero-overlap default. This single rule implements all of move/add/
        remove's per-operation neighbour semantics without needing to know
        which edit happened or its indices; the diff over id-pairs is the
        answer either way. A pre-dp-236 persisted layout has no track_id on
        any of its tracks, so no pair can ever match — that legacy case
        degrades to exactly dp-160's original full reset.
        """
        layout = self._crossfade_layout
        if layout is None:
            return
        playlist_tracks = playlist.tracks
        # Compare (filepath, id) pairs, not filepath alone -- two rows can
        # share a filepath (dp-237), so a filepath-only list can be
        # unchanged while the actual row order (by id) has not.
        layout_key   = [(t.filepath, t.track_id) for t in layout.tracks]
        playlist_key = [(t["filepath"], t.get("id")) for t in playlist_tracks]
        if layout_key == playlist_key:
            return  # nothing changed structurally

        if not playlist_tracks:
            self._crossfade_layout = None
            CrossfadeLayout.clear_persisted()
            if self._crossfade_dialog is not None:
                self._crossfade_dialog.close()
            return

        old_tracks = layout.tracks
        old_pairs = {}
        for i, ov in enumerate(layout.overlaps):
            a, b = old_tracks[i].track_id, old_tracks[i + 1].track_id
            if a is not None and b is not None:
                old_pairs[(a, b)] = ov

        new_layout = CrossfadeLayout.from_playlist_tracks(playlist_tracks)
        new_ids = [t.get("id") for t in playlist_tracks]
        for i in range(len(new_ids) - 1):
            key = (new_ids[i], new_ids[i + 1])
            if key in old_pairs:
                new_layout.replace_overlap(i, old_pairs[key])

        self._crossfade_layout = new_layout
        new_layout.save()
        if self._crossfade_dialog is not None:
            self._crossfade_dialog.set_layout(new_layout)

    # ── Transport slots ───────────────────────────────────────────────────────

    def _on_play(self):
        if engine.state == STATE_PAUSED:
            engine.resume()
            self._set_playing_ui()
        elif engine.state == STATE_STOPPED:
            if engine.current_file:
                engine.play()
                self._set_playing_ui()
            else:
                meta = playlist.current or playlist.select(0)
                if meta:
                    self._load_and_play(meta["filepath"], meta.get("id"))

    def _on_play_pause(self):
        if engine.state == STATE_PLAYING:
            self._on_pause()
        else:
            self._on_play()

    def _on_pause(self):
        if engine.state == STATE_PLAYING:
            engine.pause()
            self._set_paused_ui()
        elif engine.state == STATE_PAUSED:
            engine.resume()
            self._set_playing_ui()

    def _on_stop(self):
        # engine.stop() cancels any in-flight crossfade ramp internally
        # (dp-216 Phase 5, _stop_internal), same as load()/seek() do.
        engine.stop()
        self._set_stopped_ui()

    def _on_next(self):
        meta = playlist.next()
        if meta:
            self._advance_to(meta)

    def _on_prev(self):
        meta = playlist.previous()
        if meta:
            self._load_and_play(meta["filepath"], meta.get("id"))

    def _on_loop_a(self):
        engine.set_loop_a()
        a, b, _ = engine.loop_points
        self._waveform_widget.set_loop_points(a, b)

    def _on_loop_b(self):
        engine.set_loop_b()
        a, b, _ = engine.loop_points
        self._waveform_widget.set_loop_points(a, b)

    def _on_loop_toggle(self):
        engine.toggle_loop_ab()
        a, b, active = engine.loop_points
        self._transport.set_loop_active(active)
        self._waveform_widget.set_loop_points(a, b)

    def _on_loop_clear(self):
        engine.clear_loop()
        self._transport.set_loop_active(False)
        self._waveform_widget.set_loop_points(None, None)

    # dp-218: these four mutate settings["cue_points"]/["track_end_markers"]
    # for the ACTIVE track, which is normally not the queued one -- but it is
    # whenever the same file sits in both slots (repeat=one, or the same file
    # twice in a row in the playlist). _refresh_preview_markers() is a cheap
    # dict rebuild and a no-op when nothing is queued, so call it
    # unconditionally rather than trying to detect that aliasing here.

    def _on_cue_set(self, index: int):
        engine.set_cue(index)
        self._transport.set_cue_active(index, True)
        self._waveform_widget.set_cue_points(engine.cue_points)
        self._refresh_preview_markers()

    def _on_fin_set(self):
        engine.set_end_marker()
        self._waveform_widget.set_end_marker(engine.end_marker)
        self._refresh_preview_markers()

    def _on_fin_clear(self):
        engine.clear_end_marker()
        self._waveform_widget.clear_end_marker()
        self._refresh_preview_markers()

    def _on_start_set(self):
        engine.set_start_marker()
        self._waveform_widget.set_start_marker(engine.start_marker)
        self._refresh_preview_markers()

    def _on_start_clear(self):
        engine.clear_start_marker()
        self._waveform_widget.clear_start_marker()
        self._refresh_preview_markers()

    def _on_cue_clear_all(self):
        fp = engine.current_file
        if fp:
            # dp-237: row-keyed when the active deck carries a track_id,
            # legacy file-keyed fallback otherwise (mirrors engine's own
            # cue/marker methods).
            key = "row_cue_points" if engine.current_track_id else "cue_points"
            lookup = engine.current_track_id or fp
            cue_dict = settings.get(key, {})
            if lookup in cue_dict:
                del cue_dict[lookup]
                settings.set(key, cue_dict)
                settings.save()
            engine.clear_all_cues()
        self._transport.clear_all_cue_buttons()
        self._waveform_widget.set_cue_points({})
        self._refresh_preview_markers()

    # ── Playlist widget slots ─────────────────────────────────────────────────

    def _on_playlist_activated(self, index: int):
        # dp-232: the negative-index encoding that used to smuggle "set cue N"
        # through this signal is gone along with the playlist context menu's
        # Set Cue 1-4 items; cues are set from the transport's Cue group.
        meta = playlist.select(index)
        if meta:
            self._load_and_play(meta["filepath"], meta.get("id"))

    def _on_clear_track_markers(self, index: int):
        """dp-232: clear the Start and Fin markers of the SELECTED row.

        Deliberately keyed off the right-clicked row's filepath rather than
        the active deck -- the transport's Clear buttons already cover the
        playing track, and this exists precisely to reach a track that is
        not playing. Cue points and the colour label are left alone."""
        meta = playlist.track_at(index)
        if not meta:
            return
        filepath = meta["filepath"]
        track_id = meta.get("id")
        # dp-237: row-keyed when this row has an id (the normal case, always
        # true via the playlist); legacy file-keyed fallback for a row built
        # without one (defensive -- unchanged pre-dp-237 behaviour).
        for legacy_key, row_key in (
            ("track_start_markers", "row_start_markers"),
            ("track_end_markers", "row_end_markers"),
        ):
            key, lookup = (row_key, track_id) if track_id else (legacy_key, filepath)
            stored = settings.get(key, {})
            if lookup in stored:
                del stored[lookup]
                settings.set(key, stored)
        settings.save()

        # Only touch live deck/waveform state if the cleared row is the track
        # currently loaded -- otherwise this must not disturb playback at
        # all. dp-237: keyed on track_id when we have one (so clearing one
        # duplicate row never disturbs another row of the same file that
        # happens to be playing), else falls back to the filepath check.
        is_current = (
            engine.current_track_id == track_id
            if track_id else engine.current_file == filepath
        )
        if is_current:
            engine.clear_start_marker()
            engine.clear_end_marker()
            self._waveform_widget.clear_end_marker()
            self._waveform_widget.clear_start_marker()
        else:
            # dp-253: the cleared row may be the IDLE deck's preloaded track
            # (the normal case -- every track queues its successor). preload()
            # reads markers from settings ONCE and caches them on the Deck, so
            # deleting the settings entry above does not retroactively touch
            # an already-loaded idle deck -- invalidate_preload() + a fresh
            # _rearm_preload() is required to make it re-read the now-cleared
            # settings; preload()'s own idempotency check (`_idle_armed and
            # _idle.filepath == filepath`) would otherwise no-op on a bare
            # re-preload since the filepath hasn't changed. Only fires when
            # the cleared row is actually the queued one, so an unrelated row
            # further down the playlist never forces a needless re-decode.
            is_idle = (
                engine.preloaded_track_id == track_id
                if track_id else engine.preloaded_file == filepath
            )
            if is_idle:
                engine.invalidate_preload()
                self._rearm_preload()
        self._refresh_preview_markers()

    def _on_track_volume_changed(self, track_index: int, volume: int):
        meta = playlist.track_at(track_index)
        if meta:
            engine.set_track_volume(volume, meta["filepath"], meta.get("id"))
            if track_index == playlist.current_index:
                engine.set_track_volume(volume)

    def _on_track_volume_committed(self):
        """dp-245 D1: per-track volume is set continuously during a slider
        drag (see `_on_track_volume_changed`), so persist once here on
        release instead of fsyncing the whole settings file on every tick.
        """
        settings.save()

    def _on_remove_track(self, index: int):
        playlist.remove(index)
        self._refresh_playlist_widget()

    def _on_track_color_changed(self, index: int, color: str):
        playlist.set_track_color(index, color or None)
        # dp-194: live-refresh an open crossfade dialog's marker line and
        # shortcut button for this track, without waiting for it to be
        # closed and reopened.
        if self._crossfade_dialog is not None:
            self._crossfade_dialog.refresh_track_colors()

    def _on_end_action_changed(self, index: int, action: str):
        playlist.set_track_end_action(index, action)
        self._refresh_playlist_widget()

    # ── File menu slots ───────────────────────────────────────────────────────

    def _on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add audio files", "",
            "Audio files (*.mp3 *.flac *.wav *.ogg *.aiff *.aac *.wma *.opus *.m4a)"
            ";;All files (*)"
        )
        if files:
            self._status_left.setText(f"Scanning {len(files)} files…")
            playlist.add_files_async(filepaths=files)

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder")
        if folder:
            self._status_left.setText("Scanning folder…")
            playlist.add_files_async(folder=folder)

    def _on_files_scanned(self, metas: list):
        added = playlist.commit_scanned(metas)
        self._status_left.setText(f"Added {added} tracks")
        self._refresh_playlist_widget()

    def _on_import_m3u(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import M3U", "", "M3U (*.m3u *.m3u8)"
        )
        if path:
            playlist.import_m3u(path)
            self._refresh_playlist_widget()

    def _on_export_m3u(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export M3U", "", "M3U (*.m3u)"
        )
        if path:
            playlist.export_m3u(path)

    def _on_import_pls(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import PLS", "", "PLS (*.pls)"
        )
        if path:
            playlist.import_pls(path)
            self._refresh_playlist_widget()

    def _on_export_pls(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PLS", "", "PLS (*.pls)"
        )
        if path:
            playlist.export_pls(path)

    def _on_import_show(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Show", "", f"SAVA Show (*{SHOW_EXTENSION})"
        )
        if not path:
            return
        try:
            missing = self._apply_imported_show(path)
        except ShowFileError as e:
            # Picking the wrong file is an ordinary mistake, not a crash.
            # Without this the JSONDecodeError unwound out of the Qt slot into
            # main.py's excepthook, which logs to a file the user never sees --
            # so the menu item appeared to do nothing at all.
            QMessageBox.warning(self, "Import Show", str(e))
            return
        if missing:
            QMessageBox.warning(
                self, "Import Show",
                "These tracks could not be located and were skipped:\n\n"
                + "\n".join(missing),
            )

    def _apply_imported_show(self, path: str) -> list:
        """Everything `_on_import_show` does once it has a path: import the
        rows, rebuild the crossfade layout, refresh the UI. Returns the list
        of skipped tracks for the caller to warn about.

        Split from the slot deliberately so this is TESTABLE. The slot itself
        opens a modal file dialog, which cannot run in a test -- and a test
        that re-implements the slot's body instead is not testing the shipped
        code at all: the copy can stay green while the real slot rots. This
        method is the whole behaviour; the slot is only a dialog plus a
        message box.
        """
        missing, id_map, layout_dict = playlist.import_show(path)
        if layout_dict and layout_dict.get("tracks"):
            # Remap the file's track ids the same way import_show remapped
            # the rows themselves. An id with no entry in id_map belonged
            # to a track that was skipped (missing) -- left unremapped, it
            # can never match anything in the live playlist below, which
            # is exactly what makes a crossfade spanning it come back at
            # zero overlap (dp-246 decision 3) with no special-case code.
            for t in layout_dict["tracks"]:
                old_id = t.get("track_id")
                if old_id in id_map:
                    t["track_id"] = id_map[old_id]
            imported_layout = CrossfadeLayout.from_dict(layout_dict)
            if self._crossfade_layout is not None:
                # dp-246: import is additive (mirrors add_files/import_m3u),
                # so a show imported into a playlist that already has its
                # own authored overlaps must not blow those away.
                # Concatenating tracks/overlaps preserves BOTH groups'
                # internal ordered-id pairs for the invalidate pass below;
                # only the seam between them is a genuinely new pair,
                # which correctly defaults to zero overlap the same way
                # any other never-before-adjacent pair does.
                merged_tracks = self._crossfade_layout.tracks + imported_layout.tracks
                merged_overlaps = list(self._crossfade_layout.overlaps)
                if self._crossfade_layout.tracks and imported_layout.tracks:
                    merged_overlaps.append(Overlap())
                merged_overlaps.extend(imported_layout.overlaps)
                merged = CrossfadeLayout(merged_tracks)
                for i, ov in enumerate(merged_overlaps):
                    merged.replace_overlap(i, ov)
                self._crossfade_layout = merged
            else:
                self._crossfade_layout = imported_layout
        # _refresh_playlist_widget's _invalidate_crossfade_layout_if_stale
        # call does the actual rebuild: it diffs self._crossfade_layout
        # (now the imported/merged one, above) against the live playlist
        # and preserves only the ordered id-pairs still adjacent -- the
        # same mechanism dp-236 already built, reused verbatim here.
        self._refresh_playlist_widget()
        return missing

    def _on_export_show(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Show", "", f"SAVA Show (*{SHOW_EXTENSION})"
        )
        if not path:
            return
        # getSaveFileName does NOT append the filter's extension, so a user who
        # simply types "opening night" gets an extensionless file -- which the
        # Import dialog's *.savashow filter then hides from them.
        if not path.lower().endswith(SHOW_EXTENSION):
            path += SHOW_EXTENSION
        try:
            missing = playlist.export_show(path)
        except OSError as e:
            QMessageBox.warning(
                self, "Export Show", f"Could not write the show file:\n\n{e}"
            )
            return
        if missing:
            QMessageBox.warning(
                self, "Export Show",
                "These tracks are missing, or live outside the show folder, "
                "and will not load when this show is opened:\n\n"
                + "\n".join(missing)
                + "\n\nThe show file was still written. Move these tracks "
                "into the show folder and export again to fix it.",
            )

    def _on_clear_playlist(self):
        engine.stop()
        playlist.clear()
        self._waveform_widget.clear()
        self._lbl_title.setText("No track loaded")
        self._lbl_time.setText("0:00 / 0:00")
        self._refresh_playlist_widget()

    # ── ArtNet slots ──────────────────────────────────────────────────────────

    def _on_open_artnet_config_file(self):
        """Open the artnet_config.ini in the user's default text editor."""
        path = artnet_config.file_path
        try:
            # dp-244: os.startfile exists ONLY on Windows -- referencing it on
            # macOS/Linux is an AttributeError, not a fallback. Qt's
            # QDesktopServices is the portable equivalent and already a
            # dependency; keep startfile as the Windows path since it is the
            # behaviour that has been shipping and tested.
            if hasattr(os, "startfile"):
                os.startfile(str(path))
            else:
                from PyQt6.QtGui import QDesktopServices
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    raise OSError("no handler registered for this file type")
            self._status_right.setText(f"ArtNet: opened {path.name}")
        except Exception as e:
            QMessageBox.warning(
                self, "ArtNet",
                f"Could not open config file:\n{path}\n\n{e}"
            )

    def _on_reload_artnet_config(self):
        """Manual reload from the menu - retained as a fallback; always
        confirms with a dialog since the user explicitly asked for it."""
        if self._reload_artnet_config_impl():
            QMessageBox.information(
                self, "ArtNet",
                "Config reloaded successfully."
            )

    def _reload_artnet_config_impl(self) -> bool:
        """Re-read the INI file and refresh dependent UI. Shared by the
        manual 'Reload config' menu action and the automatic on-disk-change
        watcher (dp-179) - no duplicated reload logic between the two.

        Returns True if a bridge was available and the reload ran.
        """
        b = _bridge()
        if not b:
            return False
        b.reload_config()
        # dp-247: `[network] enabled` is the authoritative listener switch, so
        # a reload must ACT on it -- editing the INI to `enabled = -` in a text
        # editor now actually stops the listener (and re-ticks the menu), where
        # before the reloaded value was parsed and then ignored.
        self._apply_listener_state()
        self._set_artnet_status("ArtNet: config reloaded")
        # Refresh the map window if it's open
        if hasattr(self, "_map_window") and self._map_window is not None:
            try:
                self._map_window.refresh()
            except Exception:
                pass
        return True

    def _set_artnet_status(self, text, hold_ms=1500):
        """Show a transient ArtNet status message. `_refresh_ui` overwrites
        `_status_right` with the listening/off state every 100ms; the hold
        window keeps a transient message like "config reloaded" actually
        visible instead of being stomped on the very next tick."""
        self._status_right.setText(text)
        self._artnet_status_hold_until = time.monotonic() + hold_ms / 1000

    def _artnet_config_file_mtime(self):
        try:
            return artnet_config.file_path.stat().st_mtime
        except OSError:
            return None

    def _check_artnet_config_changed(self):
        """Auto-reload artnet_config.ini when it changes on disk (dp-179).

        Polls mtime off the existing 100ms `_ui_timer` - already on the Qt
        main thread - instead of adding a second timer/thread or a
        QFileSystemWatcher (some editors save via delete+rename, which can
        silently drop a watch; mtime polling is the more robust fallback,
        see ticket implementation notes).
        """
        mtime = self._artnet_config_file_mtime()
        if mtime is None or mtime == self._artnet_config_mtime:
            return
        self._artnet_config_mtime = mtime
        self._reload_artnet_config_impl()

    def _on_show_map_window(self):
        """Open the editable ArtNet/DMX configuration dialog."""
        # Keep one persistent instance so the user can leave it open
        if not hasattr(self, "_map_window") or self._map_window is None:
            self._map_window = ArtNetMapWindow(self)
        else:
            # Refresh contents in case the config was edited
            self._map_window.refresh()
        self._map_window.show()
        self._map_window.raise_()
        self._map_window.activateWindow()

    def _on_toggle_artnet(self, checked: bool):
        """Enable/disable the listener.

        dp-247: this persists to the INI's `[network] enabled`, NOT to a
        separate `listen_mode` settings key. There used to be two independent
        flags for one piece of state -- this menu action wrote
        `settings["listen_mode"]` (which alone decided whether the bridge ran),
        while the DMX dialog's "Listener enabled" checkbox wrote the INI's
        `enabled` (which NOTHING read, making the checkbox purely decorative).
        The two could therefore disagree: an INI saying `enabled = -` while
        SAVA listened anyway.

        The INI wins, because it is the file a show operator hand-edits and
        the one `Advanced -> Open config file…` exposes. `_apply_listener_state`
        is now the only place that starts/stops the bridge, so the menu action,
        the dialog checkbox and a hand-edit of the file all converge on the
        same behaviour.
        """
        try:
            artnet_config.save_network(enabled=checked)
            # save_network() rewrote the file; adopt the new mtime so the
            # on-disk watcher doesn't treat our own write as an external edit
            # and reload a second time on the next tick.
            self._artnet_config_mtime = self._artnet_config_file_mtime()
            self._apply_listener_state()
        except Exception:
            import traceback
            print(f"[ArtNet toggle] FAILED: {traceback.format_exc()}")

    def _apply_listener_state(self):
        """Start or stop the bridge to match `artnet_config.listen_enabled`,
        and re-sync the menu checkbox to it.

        Single choke point (dp-247) so every route to the setting behaves
        identically: the View menu toggle, the DMX dialog's Save, and editing
        `artnet_config.ini` in a text editor (picked up by the mtime watcher in
        `_check_artnet_config_changed`) all end up here. Idempotent --
        `ArtNetBridge.start()/stop()` both no-op when already in that state.
        """
        enabled = artnet_config.listen_enabled
        self._act_artnet.setChecked(enabled)
        b = _bridge()
        if not b:
            return
        if enabled:
            b.on_action = lambda fn, val, thr: self._on_artnet_action(fn, val, thr)
            b.start()
            self._status_right.setText("ArtNet: listening")
        else:
            b.stop()
            self._status_right.setText("ArtNet: off")

    def _on_artnet_action(self, function_name: str, value: int, threshold: int):
        fn = function_name

        if fn == "track_select_enable":
            return

        if fn == "track_select":
            b = _bridge()
            if not b or not b.track_select_active:
                return
            if value == 0:
                return
            track_number = round(value / 255 * 100)
            if track_number == 0:
                return
            idx = track_number - 1
            if idx < 0 or idx >= playlist.count:
                return
            if idx == playlist.current_index and engine.state == STATE_PLAYING:
                return
            meta = playlist.select(idx)
            if meta:
                self._load_and_play(meta["filepath"], meta.get("id"))
            return

        if fn == "play"         and value >= threshold: self._on_play()
        elif fn == "pause"      and value >= threshold: self._on_pause()
        elif fn == "stop"       and value >= threshold: self._on_stop()
        elif fn == "next_track" and value >= threshold: self._on_next()
        elif fn == "prev_track" and value >= threshold: self._on_prev()
        elif fn == "loop_ab"    and value >= threshold: self._on_loop_toggle()
        elif fn.startswith("cue_") and value >= threshold:
            try:
                self._on_cue_jump_requested(int(fn.split("_")[1]) - 1)
            except (IndexError, ValueError):
                pass
        elif fn == "master_volume":
            vol = int(value / 255 * 100)
            engine.set_master_volume(vol)
            self._transport.set_master_volume(vol)
        elif fn == "seek":
            engine.seek_percent(value / 255)

    # ── View slots ────────────────────────────────────────────────────────────

    def _on_always_on_top(self, checked: bool):
        settings.set("always_on_top", checked)
        settings.save()
        # dp-255: WindowStaysOnTopHint only takes effect on a re-show, and
        # re-showing resets window position on some platforms - so capture
        # and restore geometry around the flag flip to apply it live
        # instead of requiring a restart.
        geo = self.geometry()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.setGeometry(geo)
        self.show()

    def _on_theme_selected(self, name: str):
        if name == skin.current_theme():
            return
        settings.set("theme", name)
        settings.save()
        QMessageBox.information(
            self, "Theme",
            f"The {skin.THEME_LABELS[name]} theme will take effect the next "
            "time SAVA is started."
        )

    def _on_toggle_shortcuts(self, checked: bool):
        self._shortcuts_enabled = checked
        for act in self._keyboard_actions:
            act.setEnabled(checked)
        self._status_left.setText(
            "Keyboard shortcuts enabled" if checked else "Keyboard shortcuts disabled"
        )

    def _on_about(self):
        # dp-227: version comes from core.version, which reads the VERSION
        # file -- the same single source installer.iss reads for AppVersion,
        # so the installed app and its installer can never disagree.
        #
        # The credits list is the set of libraries SAVA actually imports and
        # runs on, verified against the import graph rather than remembered:
        # PyQt6 (UI), sounddevice (PortAudio output), numpy (the mixer's
        # buffers), mutagen (tags), plus the bundled ffmpeg binary that does
        # all decoding. `pygame` and `pydub` were both credited here long
        # after dp-216 Phase 5 replaced the playback path; pygame is gone
        # entirely, and pydub survives only as a never-executed fallback
        # inside core/analyzer.py, so neither belongs in a "built with" list.
        #
        # dp-264: custom dialog (not QMessageBox.about) so the logo can be a
        # clickable QLabel -- 6 clicks in this app session unlocks the
        # hidden "Toplo" theme (persisted, takes effect next launch, see the
        # Theme submenu build in _build_menus). The counter lives on self so
        # it survives closing and reopening the dialog within the same run,
        # but resets on restart -- there is no reason to persist partial
        # progress toward an Easter egg.
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDialogButtonBox,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("About SAVA")

        layout = QVBoxLayout(dlg)
        row = QHBoxLayout()

        icon_path = Path(__file__).resolve().parent.parent / "assets" / "sava.ico"
        logo = QLabel()
        if icon_path.exists():
            # QIcon.pixmap() rather than QPixmap(...).scaled(): sava.ico ships
            # a native 64x64 frame (alongside 16/32/48/128/256), so this picks
            # that exact frame instead of downscaling the 256 one, and it also
            # honours the device pixel ratio on HiDPI displays.
            logo.setPixmap(QIcon(str(icon_path)).pixmap(64, 64))
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        logo.setToolTip("SAVA")
        logo.mousePressEvent = lambda _event: self._on_about_logo_clicked()
        row.addWidget(logo)

        text = QLabel(
            f"<b>SAVA - Synchronizing Audio Via Art-net</b><br>"
            f"Version {__version__}<br><br>"
            "A professional audio player with full ArtNet / DMX control.<br><br>"
            "Developed by Massimo - Sava Kisiov<br>"
            "for OddLux<br><br>"
            "Built with:<br>"
            "PyQt6<br>"
            "sounddevice<br>"
            "numpy<br>"
            "mutagen<br>"
            "ffmpeg"
        )
        row.addWidget(text)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()

    def _on_help_instructions(self):
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QPushButton

        dlg = QDialog(self)
        dlg.setWindowTitle("SAVA - Instructions")
        dlg.setMinimumSize(720, 600)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(8, 8, 8, 8)

        text = QTextBrowser()
        text.setOpenExternalLinks(True)
        text.setFont(make_font(FONT_SIZE_SMALL))
        text.setHtml(_HELP_HTML)
        layout.addWidget(text)

        btn = QPushButton("Close")
        btn.setFixedHeight(28)
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)

        dlg.exec()

    def _on_about_logo_clicked(self):
        """dp-264: 6 clicks on the About logo (this app session) unlocks the
        hidden Toplo theme. Once unlocked, further clicks are a no-op --
        there's nothing left to unlock and no reason to keep re-saving.

        Deliberately defined AFTER _on_help_instructions, not next to
        _on_about where it belongs by topic: tests/test_help_and_about_
        accuracy.py reads the About dialog's displayed text by slicing the
        source from `def _on_about` to `def _on_help_instructions`. A method
        sitting between those two is treated as About text, so a string in
        here naming (say) a retired library would fail a credits test for a
        reason that has nothing to do with the credits.
        """
        if settings.get("toplo_unlocked", False):
            return
        self._about_logo_clicks += 1
        if self._about_logo_clicks < 6:
            return
        settings.set("toplo_unlocked", True)
        settings.save()
        QMessageBox.information(
            self, "SAVA",
            "Congrats! You are so contemporary now!",
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_and_play(self, filepath: str, track_id: str = None):
        # engine.load() cancels any in-flight/armed crossfade and retires the
        # idle deck internally (dp-216 Phase 5) -- no separate cancel needed.
        engine.load(filepath, track_id)
        engine.play()
        # Give the decode-ahead worker a brief head start before spending CPU
        # on the waveform decode thread - avoids the two competing for CPU
        # in the first moments of playback on lower-end hardware (dp-155).
        # dp-245 D7: guarded callable, not a bare lambda. This is the THIRD
        # site of that 200ms deferral and the most reachable one -- it fires
        # on every manual track load, so quitting within 200ms of a
        # double-clicked row is an ordinary thing a user can do, not a race
        # they have to hunt for. See _deferred_primary_analysis.
        QTimer.singleShot(200, functools.partial(self._deferred_primary_analysis, filepath))
        # dp-178: speculatively warm the OS disk cache + mutagen metadata
        # for whichever tracks a Next/Prev press would land on next, while
        # this track plays. engine.prefetch() only touches its own separate
        # cache - never engine's current-file state - and returns
        # immediately (the actual read happens on a background thread).
        next_meta = playlist.peek_next()
        if next_meta:
            engine.prefetch(next_meta["filepath"])
        prev_meta = playlist.peek_previous()
        if prev_meta:
            engine.prefetch(prev_meta["filepath"])
        # dp-192/dp-216: arm the idle deck for the predicted next track so
        # the next natural-end / Next transition is gap-free (or arms the
        # crossfade ramp instead, if that transition is configured to
        # overlap -- see _rearm_preload).
        self._rearm_preload()
        self._waveform_widget.clear()
        self._waveform_widget.set_loop_points(None, None)
        self._set_playing_ui()
        meta = playlist.current
        if meta:
            self._update_track_info(meta)
        tv = engine.track_volume  # dp-237: row-keyed, engine already resolved it at load
        self._playlist_widget.update_track_volume_display(tv)
        self._waveform_widget.set_cue_points(engine.cue_points)
        self._sync_cue_buttons()
        if engine.end_marker is not None:
            self._waveform_widget.set_end_marker(engine.end_marker)
        else:
            self._waveform_widget.clear_end_marker()
        if engine.start_marker is not None:
            self._waveform_widget.set_start_marker(engine.start_marker)
        else:
            self._waveform_widget.clear_start_marker()

    def _update_track_info(self, meta: dict):
        title  = meta.get("title",  "Unknown")
        artist = meta.get("artist", "")
        album  = meta.get("album",  "")
        if artist and artist != "Unknown":
            self._lbl_title.setText(f"{artist} - {title}")
        else:
            self._lbl_title.setText(title)
        parts = []
        if album and album != "Unknown":
            parts.append(album)
        parts.append(f"Track {playlist.current_index + 1} of {playlist.count}")
        self._lbl_info.setText("  |  ".join(parts))

    def _set_playing_ui(self):
        self._transport.set_playing(True)
        self._transport.set_paused(False)
        self._transport.set_stopped(False)
        self._status_left.setText("Playing")

    def _set_paused_ui(self):
        self._transport.set_playing(False)
        self._transport.set_paused(True)
        self._transport.set_stopped(False)
        self._status_left.setText("Paused")

    def _set_stopped_ui(self):
        self._transport.set_playing(False)
        self._transport.set_paused(False)
        self._transport.set_stopped(True)
        self._status_left.setText("Stopped")
        # dp-206: engine.stop() zeroes position internally, but nothing
        # else redraws the needle -- do it here so it covers every path
        # that transitions to stopped (Stop button, natural track-end).
        self._waveform_widget.set_position(0.0)

    # ── Timecode hover magnifier (dp-198) ───────────────────────────────────

    def _show_timecode_popup(self):
        if self._timecode_popup is None:
            popup = QLabel(self)
            popup.setWindowFlags(
                Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
            )
            popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            popup.setFont(make_display_font(FONT_SIZE_TIMECODE * 4))
            popup.setStyleSheet(
                f"color: {C_ACCENT}; background: {skin.C_BG_DARK};"
                f"border: 1px solid {skin.C_BORDER}; padding: 6px 10px;"
                f"font-size: {FONT_SIZE_TIMECODE * 4}pt;"
            )
            self._timecode_popup = popup
        self._timecode_popup.setText(self._lbl_time.text())
        self._position_timecode_popup()
        self._timecode_popup.show()

    def _hide_timecode_popup(self):
        if self._timecode_popup is not None:
            self._timecode_popup.hide()

    def _position_timecode_popup(self):
        """Anchor just below the label, clamped to the current screen so it
        never renders off-screen near edges or at the window's minimum
        size."""
        popup = self._timecode_popup
        popup.adjustSize()
        anchor = self._lbl_time.mapToGlobal(
            self._lbl_time.rect().bottomRight()
        )
        x = anchor.x() - popup.width()
        y = anchor.y() + 4
        screen = self.screen() or popup.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - popup.width()))
            y = max(avail.top(), min(y, avail.bottom() - popup.height()))
        popup.move(x, y)

    def _on_timecode_clicked(self):
        self._timecode_mode = (self._timecode_mode + 1) % 3
        self._refresh_ui()

    def _refresh_ui(self):
        pos = engine.position
        dur = engine.duration
        self._lbl_time.setText(_format_timecode(pos, dur, self._timecode_mode))
        if self._timecode_popup is not None and self._timecode_popup.isVisible():
            self._timecode_popup.setText(self._lbl_time.text())
        _, _, loop_active = engine.loop_points
        self._transport.set_loop_active(loop_active)
        try:
            b = _bridge()
            running = b.is_running if b else False
        except Exception:
            running = False
        if time.monotonic() >= self._artnet_status_hold_until:
            self._status_right.setText("ArtNet: listening" if running else "ArtNet: off")
        self._check_artnet_config_changed()

    # ── Window events ─────────────────────────────────────────────────────────

    def closeEvent(self, event):
        settings.set("window_x", self.x())
        settings.set("window_y", self.y())
        playlist.save()
        settings.save()
        b = _bridge()
        if b:
            try:
                b.stop()
            except Exception:
                pass
        engine.shutdown()
        event.accept()


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _format_timecode(pos: float, dur: float, mode: int) -> str:
    """dp-213: build the timecode label string for the given display mode.

    mode 0 = elapsed (pos / dur), 1 = remaining (-remaining / dur),
    2 = both (elapsed on the left, remaining on the right).
    """
    pos = max(0.0, pos)
    dur = max(0.0, dur)
    remaining = max(0.0, dur - pos)
    # `int(remaining) == 0`, not `remaining == 0`: _fmt truncates, so any
    # sub-second remainder (the last second of every track, once per play)
    # still formats as "0:00" and would render as "-0:00".
    remaining_str = (
        "0:00" if dur == 0 or int(remaining) == 0 else f"-{_fmt(remaining)}"
    )
    if mode == 1:
        return f"{remaining_str} / {_fmt(dur)}"
    if mode == 2:
        # dp-233: elapsed = int(pos) and remaining = int(dur - pos) are two
        # independently truncated floats - they cross their integer
        # boundaries at different instants, causing visible tick skew.
        # Derive both halves from the SAME integer second instead.
        elapsed_i = int(pos)
        remaining_i = int(round(dur)) - elapsed_i
        both_remaining_str = (
            "0:00" if dur == 0 or remaining_i <= 0 else f"-{_fmt(remaining_i)}"
        )
        return f"{_fmt(pos)} / {both_remaining_str}"
    return f"{_fmt(pos)} / {_fmt(dur)}"


# ── Help text ─────────────────────────────────────────────────────────────────
# Headings use the ACTIVE theme's accent (C_ACCENT) rather than a hardcoded
# green -- the theme is user-selectable (View -> Theme), and a fixed colour
# clashed with every theme except one.
_HELP_HTML = f"""
<h2 style="color:{C_ACCENT};">SAVA - Synchronizing Audio Via Art-net</h2>
<p><i>Audio player driven by ArtNet / DMX.</i></p>

<h3 style="color:{C_ACCENT};">1. Getting started</h3>
<ul>
<li><b>Add tracks:</b> drag files or folders onto the window, or <b>File &rarr; Add files / Add folder</b>.</li>
<li><b>Formats:</b> MP3, FLAC, WAV, OGG, AIFF, AAC, WMA, OPUS, M4A.</li>
<li><b>Play:</b> double-click a track, or select it and press Play.</li>
</ul>

<h3 style="color:{C_ACCENT};">2. Transport</h3>
<ul>
<li><b>Prev / Play / Pause / Stop / Next.</b></li>
<li><b>VOL</b> - master volume.</li>
<li><b>Shuffle</b> - random order. <b>Repeat</b> - cycles none / one / all.</li>
</ul>

<h3 style="color:{C_ACCENT};">3. Displays</h3>
<ul>
<li><b>Main waveform</b> - the playing track. Click to seek. Drag to scrub: the needle follows, playback jumps when you release.</li>
<li><b>Preview waveform</b> (below) - the track queued next.</li>
<li><b>Timecode</b> - click to cycle elapsed / remaining / both. Hover to magnify.</li>
<li><b>"Buffering waveform"</b> - the waveform is still decoding. Playback is unaffected.</li>
</ul>

<h3 style="color:{C_ACCENT};">4. Markers</h3>
<p>All markers are per playlist row and persist between sessions. Two rows of the same file keep separate markers.</p>
<ul>
<li><b>Start</b> - Set / Clear. Playback begins here instead of 0:00.</li>
<li><b>Fin</b> - Set / Clear. Playback ends here instead of the true end.</li>
<li><b>Loop A to B</b> - Set A, Set B, then <b>Enable</b> (label becomes Disable). <b>Clear</b> removes both.</li>
<li><b>Cues 1-8</b> - right-click a Cue button to set at the current position, left-click to jump. <b>Clear All Cues</b> wipes them for the current track.</li>
</ul>
<p>Start cannot be placed at or after Fin, and Fin cannot be placed at or before Start - the offending set is ignored.</p>

<h3 style="color:{C_ACCENT};">5. Playlist</h3>
<ul>
<li><b>Drag</b> rows to reorder. <b>Track volume</b> slider applies to the selected row only.</li>
<li><b>Right-click a row:</b> Play this track, Remove from playlist, <b>When this track ends</b> (play next <code>&gt;</code> / loop <code>@</code> / stop <code>|</code>), Set or Clear colour label, Clear markers.</li>
</ul>

<h3 style="color:{C_ACCENT};">6. Crossfade</h3>
<p><b>Playback &rarr; Crossfade…</b> lays every playlist track on a timeline with an overlap between each adjacent pair.</p>
<ul>
<li><b>Zoom</b> slider sets the scale.</li>
<li><b>Drag a track block</b> left to overlap it with the one before. The first track cannot be dragged.</li>
<li>Each overlap has a <b>fade curve</b> with draggable handles. Double-click a handle to reset that curve.</li>
<li><b>Reset to Default</b>, <b>Save</b>, <b>Close</b>.</li>
</ul>
<p>Crossfades apply only to a track's authored next track. Under Shuffle or Repeat-one the transition falls back to a plain gapless swap. Editing the playlist keeps every overlap whose two tracks are still adjacent; new pairings start at zero.</p>
<p>While a crossfade runs, the slider between the two waveforms shows its progress and can be dragged to retime it live.</p>

<h3 style="color:{C_ACCENT};">7. Show files</h3>
<p>M3U and PLS store only the track list. A <b>show file</b> stores the work: cues, Start and Fin markers, track volumes, colour labels, end actions and the crossfade layout.</p>
<ul>
<li><b>One folder per show</b> - put the audio and the <code>.savashow</code> in it together.</li>
<li><b>File &rarr; Export Show…</b> / <b>Import Show…</b>. Paths are stored relative to the show file, so the folder can be copied to another drive or machine.</li>
<li>SAVA never copies audio. If a track is missing, the rest of the show still loads and the transition across the gap loses its crossfade.</li>
<li>Both export and import warn you by name about tracks that will not resolve. Neither is blocked.</li>
</ul>

<h3 style="color:{C_ACCENT};">8. Audio output</h3>
<p><b>Playback &rarr; Audio Output</b> - follow the Windows default, or pin a specific device. The list is rebuilt each time it opens, so a device plugged in after launch appears. Switching stops playback: tracks must be re-decoded at the new device's sample rate.</p>

<h3 style="color:{C_ACCENT};">9. ArtNet / DMX</h3>
<p>SAVA listens for ArtNet on UDP port 6454. Any console with ArtNet output can drive it. SAVA only receives - it sends nothing back.</p>
<ul>
<li><b>ArtNet &rarr; Configure DMX mapping…</b> - per function: Enabled, Channel, Threshold, and <b>Learn</b>.</li>
<li><b>Learn</b> assigns the next channel that changes. Click Learn to arm, click <b>Cancel</b> to stop.</li>
<li><b>Network addressing</b> - Port, Subnet, Universe, Listener enabled. <b>Save</b> writes the file, <b>Revert</b> re-reads it.</li>
<li><b>Advanced</b> - open <code>artnet_config.ini</code> in a text editor, or force a reload. SAVA reloads it automatically when it changes on disk, including the listener on/off state.</li>
</ul>
<h4>Functions</h4>
<table cellpadding="4" cellspacing="0">
<tr><td><b>Play / Pause / Stop / Next / Prev</b></td><td>Fire when the value reaches the threshold.</td></tr>
<tr><td><b>Master volume</b></td><td>0-255 maps to 0-100%.</td></tr>
<tr><td><b>Seek</b></td><td>0-255 maps to 0-100% of the track.</td></tr>
<tr><td><b>Loop A to B</b></td><td>Toggles the loop.</td></tr>
<tr><td><b>Cue 1-8</b></td><td>Jump to that cue.</td></tr>
<tr><td><b>Track Select Enable</b></td><td>Gate. Track Select only works while this is at or above its threshold.</td></tr>
<tr><td><b>Track Select</b></td><td>Track number. 0-255 maps to 0-100 (matches consoles that display percent). Value 0 does nothing.</td></tr>
</table>

<h3 style="color:{C_ACCENT};">10. Keyboard shortcuts</h3>
<table cellpadding="4">
<tr><td><b>Space</b></td><td>Play / Pause</td></tr>
<tr><td><b>.</b></td><td>Stop</td></tr>
<tr><td><b>Ctrl+Right / Ctrl+Left</b></td><td>Next / Previous track</td></tr>
<tr><td><b>Ctrl+[ / Ctrl+]</b></td><td>Set Loop A / Set Loop B</td></tr>
<tr><td><b>Ctrl+L / Ctrl+Shift+L</b></td><td>Toggle loop / Clear loop</td></tr>
<tr><td><b>Ctrl+O / Ctrl+Shift+O</b></td><td>Add files / Add folder</td></tr>
<tr><td><b>Ctrl+Q</b></td><td>Exit</td></tr>
</table>
<p>Turn them all off with <b>View &rarr; Keyboard shortcuts enabled</b>.</p>

<h3 style="color:{C_ACCENT};">11. View</h3>
<ul>
<li><b>Always on top</b> - keeps SAVA above other windows. Applies on next launch.</li>
<li><b>Theme</b> - colour scheme. Applies on next launch.</li>
</ul>

<h3 style="color:{C_ACCENT};">12. Troubleshooting</h3>
<ul>
<li><b>A track will not load:</b> check <code>ffmpeg.exe</code> is in the install folder under <code>_internal\\assets\\</code>.</li>
<li><b>No sound:</b> check <b>Playback &rarr; Audio Output</b> - the saved device may be unplugged. "Follow Windows default" is the safe choice.</li>
<li><b>No ArtNet:</b> confirm nothing else is bound to UDP 6454, the console targets this machine's IP, and Subnet/Universe match.</li>
<li><b>Log file:</b> <code>%APPDATA%\\SAVA\\sava.log</code></li>
</ul>
"""

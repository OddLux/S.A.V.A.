"""
Crossfade dialog (dp-160) - hosts the dp-159 timeline widget plus
Reset/Save/Close, opened via Playback -> Crossfade.... dp-216 Phase 3
removed the preview transport (Preview/Stop Preview); CrossfadeSession still
backs the live engine until the Phase 5 swap, but this dialog no longer
drives it.
"""

from functools import partial
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
)

from core.crossfade_model import CrossfadeLayout
from core.playlist import playlist
from ui.crossfade_timeline_widget import CrossfadeTimelineWidget, track_color
from ui.skin import C_TEXT_DIM, FONT_SIZE_SMALL, make_font, track_button_style


class CrossfadeDialog(QDialog):
    """Opened from the Playback menu. Not modal - preview playback keeps
    running while the user edits, same as the rest of SAVA's non-blocking
    dialogs (e.g. ArtNetMapWindow)."""

    sig_layout_saved = pyqtSignal(object)  # emits the saved CrossfadeLayout

    def __init__(self, layout: "CrossfadeLayout | None" = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crossfade")
        # dp-167: taller default so 3-4 track lanes are visible without
        # compression; dialog stays freely resizable (no fixed min/max).
        self.resize(900, 420)
        self.setMinimumSize(600, 300)
        self._layout  = layout or CrossfadeLayout.from_playlist_tracks(playlist.tracks)
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)

        self._timeline = CrossfadeTimelineWidget()
        self._timeline.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._timeline.sig_layout_changed.connect(self._rebuild_shortcut_row)
        root.addWidget(self._timeline, stretch=1)

        # dp-175: per-track jump shortcuts - one button per track, rebuilt
        # from scratch on every set_layout() (via sig_layout_changed) so
        # stale buttons never linger and new tracks always get one.
        # dp-204: buttons wrap into multiple rows (alternating 4/5 caps)
        # instead of a single unbounded row.
        self._shortcut_rows = QVBoxLayout()
        root.addLayout(self._shortcut_rows)

        self._timeline.set_layout(self._layout)

        btn_row = QHBoxLayout()
        self._status_lbl = QLabel("")
        self._status_lbl.setFont(make_font(FONT_SIZE_SMALL))
        self._status_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        btn_row.addWidget(self._status_lbl)
        btn_row.addStretch()

        self._btn_reset = QPushButton("Reset to Default")
        self._btn_reset.setToolTip(
            "Rebuild this layout from the playlist with zero overlap and "
            "default curves on every track. Does not save until you click "
            "Save."
        )
        self._btn_reset.clicked.connect(self._on_reset)
        btn_row.addWidget(self._btn_reset)

        self._btn_save = QPushButton("Save")
        self._btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(self._btn_save)

        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self._on_close)
        btn_row.addWidget(self._btn_close)

        root.addLayout(btn_row)

    # ── Live layout refresh (dp-236) ─────────────────────────────────────

    def set_layout(self, layout: "CrossfadeLayout"):
        """Swap in a layout rebuilt underneath this dialog by a playlist
        edit (selective overlap preservation, dp-236) and re-render, rather
        than closing the dialog on every edit as dp-160 did."""
        self._layout = layout
        self._timeline.set_layout(self._layout)
        self._rebuild_shortcut_row(self._layout)

    # ── Live color refresh (dp-194) ──────────────────────────────────────

    def refresh_track_colors(self):
        """Sync each track's color field from the current playlist state
        (by list order, same join key from_playlist_tracks uses) and
        re-render - without rebuilding the layout, so overlaps/curves stay
        untouched. Called by MainWindow when a playlist track's color
        changes while this dialog is open."""
        playlist_tracks = playlist.tracks
        for i, track in enumerate(self._layout.tracks):
            if i < len(playlist_tracks):
                track.color = playlist_tracks[i].get("color")
        self._timeline.set_layout(self._layout)

    # ── Track jump shortcuts (dp-175) ────────────────────────────────────

    # dp-204: row capacity alternates 4/5/4/5... by row index (0-based).
    _SHORTCUT_ROW_CAPS = (4, 5)
    _SHORTCUT_BUTTON_WIDTH = 130
    # dp-209: fill-width sizing. Each row's buttons split the row's
    # available width evenly across its *cap* (not its actual button
    # count) - that's what keeps a short final row from stretching a
    # lone button across the whole width, since a partial row still
    # divides by the full cap and simply leaves the remainder blank.
    _SHORTCUT_ROW_SPACING = 4
    _SHORTCUT_BUTTON_MIN_WIDTH = 90
    _SHORTCUT_BUTTON_MAX_WIDTH = 220
    _SHORTCUT_ROW_MARGIN = 24  # dp-209: rough allowance for dialog/layout margins

    def _rebuild_shortcut_row(self, layout: "CrossfadeLayout"):
        """Rebuild the per-track shortcut button rows from scratch. Track
        lists here are small (a handful of tracks), so a full clear +
        rebuild on every layout change is cheap and avoids stale-button
        bugs from trying to diff/patch the rows in place.

        dp-204: buttons wrap into multiple rows once the current row hits
        its cap (alternating 4/5), and each button's label is elided from
        the end (never the middle) so the start of the track name always
        stays visible.

        dp-209: instead of a fixed button width plus a trailing stretch,
        each row's buttons are sized to split the row's available width
        (clamped to a sane min/max), so a fully-capped row fills its width
        without leaving one large trailing gap."""
        while self._shortcut_rows.count():
            item = self._shortcut_rows.takeAt(0)
            sub_layout = item.layout()
            if sub_layout is not None:
                while sub_layout.count():
                    sub_item = sub_layout.takeAt(0)
                    widget = sub_item.widget()
                    if widget is not None:
                        widget.deleteLater()

        avail_width = max(self.width() - self._SHORTCUT_ROW_MARGIN, 100)

        row = None
        row_idx = -1
        cap = 0
        count_in_row = 0
        btn_width = self._SHORTCUT_BUTTON_WIDTH
        for idx, track in enumerate(layout.tracks):
            if row is None or count_in_row >= cap:
                row_idx += 1
                cap = self._SHORTCUT_ROW_CAPS[row_idx % len(self._SHORTCUT_ROW_CAPS)]
                count_in_row = 0
                row = QHBoxLayout()
                row.setSpacing(self._SHORTCUT_ROW_SPACING)
                self._shortcut_rows.addLayout(row)
                btn_width = int(avail_width / cap) - self._SHORTCUT_ROW_SPACING
                btn_width = max(
                    self._SHORTCUT_BUTTON_MIN_WIDTH,
                    min(btn_width, self._SHORTCUT_BUTTON_MAX_WIDTH),
                )

            # dp-168's track-name derivation, reused here so the label on
            # this button always matches the label drawn on the track's
            # lane (see CrossfadeTimelineWidget._draw_track_label).
            name = Path(track.filepath).stem
            btn = QPushButton()
            btn.setFixedWidth(btn_width)
            metrics = QFontMetrics(btn.font())
            elided = metrics.elidedText(
                name, Qt.TextElideMode.ElideRight, btn_width - 16
            )
            btn.setText(elided)
            btn.setToolTip(f"Scroll the timeline to \"{name}\"")
            # dp-176: colorize with this track's identity color (same
            # palette[i % len] lookup the timeline uses for the marker
            # line) so the button visually matches its track at a glance.
            # dp-194: playlist-assigned color wins when set; falls back to
            # dp-176's auto-palette when the track has no playlist color.
            btn.setStyleSheet(track_button_style(track.color or track_color(idx)))
            btn.clicked.connect(partial(self._timeline.scroll_to_track, idx))
            row.addWidget(btn)
            count_in_row += 1

    # ── Reset ─────────────────────────────────────────────────────────────

    def _on_reset(self):
        self._layout = CrossfadeLayout.from_playlist_tracks(playlist.tracks)
        self._timeline.set_layout(self._layout)
        self._status_lbl.setText("Reset to default (not saved)")

    # ── Save / close ──────────────────────────────────────────────────────

    def _on_save(self):
        self._layout.save()
        self.sig_layout_saved.emit(self._layout)
        self._status_lbl.setText("Saved")

    def _on_close(self):
        self.reject()

    def closeEvent(self, event):
        super().closeEvent(event)

    def resizeEvent(self, event):
        # dp-209: shortcut button widths are computed from self.width(), so
        # a dialog resize must re-trigger the rebuild to re-fill the row -
        # sig_layout_changed alone only fires on layout/track changes, not
        # on window resize.
        super().resizeEvent(event)
        self._rebuild_shortcut_row(self._layout)

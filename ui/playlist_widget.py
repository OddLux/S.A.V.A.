"""
Playlist widget — shows all tracks with end-action indicators,
context menu, drag-and-drop reordering, per-track volume.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QSlider, QMenu, QColorDialog, QAbstractItemView
)
from PyQt6.QtCore    import Qt, pyqtSignal, QPoint
from PyQt6.QtGui     import QColor, QBrush

from ui.skin import (
    C_ACCENT, C_TEXT_DIM, C_TEXT_PRIMARY,
    make_font, FONT_SIZE_SMALL
)

# Symbols shown next to track name in the list
END_ACTION_SYMBOLS = {
    "next": ">",     # play next track
    "loop": "@",     # loop this track
    "stop": "|",     # stop after this track
}

END_ACTION_LABELS = {
    "next": "Play next track",
    "loop": "Loop this track",
    "stop": "Stop after this track",
}


class PlaylistWidget(QWidget):

    track_activated         = pyqtSignal(int)
    track_volume_changed    = pyqtSignal(int, int)
    track_volume_committed  = pyqtSignal()   # dp-245 D1: fires once on release
    remove_requested        = pyqtSignal(int)
    color_changed           = pyqtSignal(int, str)
    reordered               = pyqtSignal(int, int)
    end_action_changed      = pyqtSignal(int, str)   # (index, "next"|"loop"|"stop")
    clear_markers_requested = pyqtSignal(int)        # dp-232: row index, not the playing track

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracks        = []
        self._current_index = -1
        self._setup_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(4, 2, 4, 2)
        lbl_title = QLabel("PLAYLIST")
        lbl_title.setFont(make_font(FONT_SIZE_SMALL, bold=True))
        lbl_title.setStyleSheet(f"color: {C_ACCENT}; background: transparent;")

        # Legend
        legend = QLabel("Legend:  >  next   @  loop   |  stop")
        legend.setFont(make_font(FONT_SIZE_SMALL))
        legend.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")

        self._lbl_total = QLabel("")
        self._lbl_total.setFont(make_font(FONT_SIZE_SMALL))
        self._lbl_total.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        self._lbl_total.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        hdr.addWidget(lbl_title)
        hdr.addSpacing(20)
        hdr.addWidget(legend)
        hdr.addStretch()
        hdr.addWidget(self._lbl_total)
        layout.addLayout(hdr)

        # List
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setFont(make_font(FONT_SIZE_SMALL))
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self._list)

        # Per-track volume row
        vol_row = QHBoxLayout()
        vol_row.setContentsMargins(4, 2, 4, 2)
        lbl_vol = QLabel("Track vol:")
        lbl_vol.setFont(make_font(FONT_SIZE_SMALL))
        lbl_vol.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        self._track_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._track_vol_slider.setRange(0, 100)
        self._track_vol_slider.setValue(100)
        self._track_vol_slider.setFixedHeight(16)
        self._track_vol_slider.valueChanged.connect(self._on_track_vol_changed)
        # dp-245 D1: valueChanged fires continuously during a drag -- only
        # persist once, on release, not on every intermediate tick.
        self._track_vol_slider.sliderReleased.connect(
            self.track_volume_committed.emit
        )
        self._lbl_track_vol = QLabel("100")
        self._lbl_track_vol.setFixedWidth(28)
        self._lbl_track_vol.setFont(make_font(FONT_SIZE_SMALL))
        self._lbl_track_vol.setStyleSheet(f"color: {C_ACCENT}; background: transparent;")
        vol_row.addWidget(lbl_vol)
        vol_row.addWidget(self._track_vol_slider)
        vol_row.addWidget(self._lbl_track_vol)
        layout.addLayout(vol_row)

    # ── Public API ────────────────────────────────────────────────────────────

    def populate(self, tracks):
        self._tracks = tracks
        self._list.clear()
        for i, t in enumerate(tracks):
            item = self._make_item(i, t)
            self._list.addItem(item)
        self._update_total()

    def set_current(self, index: int):
        if 0 <= self._current_index < self._list.count():
            old = self._list.item(self._current_index)
            if old:
                old.setFont(make_font(FONT_SIZE_SMALL))
                old_color = self._tracks[self._current_index].get("color")
                old.setForeground(QBrush(QColor(old_color or C_TEXT_PRIMARY)))
        self._current_index = index
        if 0 <= index < self._list.count():
            item = self._list.item(index)
            if item:
                f = make_font(FONT_SIZE_SMALL, bold=True)
                item.setFont(f)
                item.setForeground(QBrush(QColor(C_ACCENT)))
                self._list.scrollToItem(item)
                self._list.setCurrentItem(item)

    def update_track_volume_display(self, volume: int):
        self._track_vol_slider.blockSignals(True)
        self._track_vol_slider.setValue(volume)
        self._track_vol_slider.blockSignals(False)
        self._lbl_track_vol.setText(str(volume))

    def update_duration(self, index: int, duration: float):
        if 0 <= index < len(self._tracks):
            self._tracks[index]["duration"] = duration
            item = self._list.item(index)
            if item:
                item.setText(self._format_item(index, self._tracks[index]))
        self._update_total()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _make_item(self, index: int, meta: dict) -> QListWidgetItem:
        item = QListWidgetItem(self._format_item(index, meta))
        item.setFont(make_font(FONT_SIZE_SMALL))
        item.setData(Qt.ItemDataRole.UserRole, index)
        color = meta.get("color")
        if color:
            item.setForeground(QBrush(QColor(color)))
        return item

    def _format_item(self, index: int, meta: dict) -> str:
        dur     = meta.get("duration", 0)
        dur_str = _fmt_time(dur) if dur else "--:--"
        title   = meta.get("title",  "Unknown")
        artist  = meta.get("artist", "")
        action  = meta.get("end_action", "next")
        symbol  = END_ACTION_SYMBOLS.get(action, ">")
        if artist and artist != "Unknown":
            label = f"{artist} - {title}"
        else:
            label = title
        return f"{index + 1:>3}. [{symbol}]  {label}  [{dur_str}]"

    def _update_total(self):
        total = sum(t.get("duration", 0) for t in self._tracks)
        count = len(self._tracks)
        m = int(total) // 60
        s = int(total) % 60
        self._lbl_total.setText(f"{count} tracks  {m}:{s:02d}")

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_double_click(self, item: QListWidgetItem):
        row = self._list.row(item)
        self.track_activated.emit(row)

    def _on_track_vol_changed(self, value: int):
        """Apply to the PLAYING track, not the selected row.

        `update_track_volume_display` is fed the playing track's volume (see
        MainWindow._on_engine_track_changed), so the slider always SHOWS the
        playing track. Reading `currentRow()` here meant that after clicking
        any other row in the list, the slider displayed one track's volume
        while writing to a different one."""
        self._lbl_track_vol.setText(str(value))
        if self._current_index >= 0:
            self.track_volume_changed.emit(self._current_index, value)

    def _on_rows_moved(self, parent, start, end, dest_parent, dest_row):
        if start != dest_row:
            self.reordered.emit(start, dest_row if dest_row < start else dest_row - 1)

    def _show_context_menu(self, pos: QPoint):
        # Act on the row UNDER THE CURSOR, not the selected row. A
        # right-click does not change the selection in Qt, so using
        # currentRow() here meant right-clicking any track you had not first
        # left-clicked silently applied Remove / colour / end-action to
        # whichever track happened to be selected instead.
        item = self._list.itemAt(pos)
        if item is None:
            return
        row = self._list.row(item)
        if row < 0:
            return

        current_action = self._tracks[row].get("end_action", "next") if row < len(self._tracks) else "next"

        menu = QMenu(self)
        act_play   = menu.addAction("Play this track")
        act_remove = menu.addAction("Remove from playlist")
        menu.addSeparator()

        # End action submenu
        ea_menu = menu.addMenu("When this track ends:")
        act_next = ea_menu.addAction("Play next track  ( > )")
        act_loop = ea_menu.addAction("Loop this track  ( @ )")
        act_stop = ea_menu.addAction("Stop after this  ( | )")
        for act, key in ((act_next, "next"),
                         (act_loop, "loop"),
                         (act_stop, "stop")):
            act.setCheckable(True)
            act.setChecked(current_action == key)

        menu.addSeparator()
        act_color = menu.addAction("Set colour label")
        act_clear_color = menu.addAction("Clear colour label")
        menu.addSeparator()
        # dp-232: clears the Start and Fin markers of the RIGHT-CLICKED row,
        # which is not necessarily the playing track -- the transport's own
        # Clear buttons act on the active deck, this acts on the selection.
        # Cue points and the colour label are deliberately left alone.
        act_clear_markers = menu.addAction("Clear markers")

        chosen = menu.exec(self._list.mapToGlobal(pos))

        if chosen is None:
            return

        if chosen == act_play:
            self.track_activated.emit(row)
        elif chosen == act_remove:
            self.remove_requested.emit(row)
        elif chosen == act_next:
            self.end_action_changed.emit(row, "next")
        elif chosen == act_loop:
            self.end_action_changed.emit(row, "loop")
        elif chosen == act_stop:
            self.end_action_changed.emit(row, "stop")
        elif chosen == act_color:
            color = QColorDialog.getColor(
                QColor(C_ACCENT), self, "Choose track colour"
            )
            if color.isValid():
                hex_color = color.name()
                item = self._list.item(row)
                if item:
                    item.setForeground(QBrush(color))
                self.color_changed.emit(row, hex_color)
        elif chosen == act_clear_color:
            item = self._list.item(row)
            if item:
                item.setForeground(QBrush(QColor(C_TEXT_PRIMARY)))
            self.color_changed.emit(row, "")
        elif chosen == act_clear_markers:
            self.clear_markers_requested.emit(row)


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"
"""
ArtNet configuration dialog.
Editable channel mapping, Learn-assisted channel assignment, and editable
network addressing settings — all persisted through ArtNetConfig's INI file
(core/artnet_config.py). Replaces manual editing of the config file.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton, QHeaderView,
    QCheckBox, QSpinBox, QWidget, QMessageBox
)
from PyQt6.QtCore import Qt

from core.artnet_config import (
    artnet_config, FUNCTION_NAMES, FUNCTION_LABELS, TRIGGER_FUNCTIONS
)
from ui.skin import (
    C_ACCENT, C_ACCENT_ORANGE,
    make_font, make_display_font, FONT_SIZE_SMALL, FONT_SIZE_NORMAL
)


def _bridge():
    from core import artnet_bridge as ab_module
    return ab_module.artnet_bridge


class ArtNetMapWindow(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SAVA — ArtNet / DMX Configuration")
        self.setMinimumSize(700, 640)
        self.setModal(False)   # non-modal so user can keep it open during a show

        self._learning_row = None
        self._row_widgets  = []

        self._setup_ui()
        self.refresh()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QLabel("ArtNet / DMX configuration")
        title.setFont(make_display_font(14))
        title.setStyleSheet(f"color: {C_ACCENT}; background: transparent;")
        root.addWidget(title)

        # ── Network addressing group ──
        net_group = QGroupBox("Network addressing")
        net_form = QFormLayout(net_group)

        self._sb_port = QSpinBox()
        self._sb_port.setRange(1, 65535)
        self._sb_subnet = QSpinBox()
        self._sb_subnet.setRange(0, 15)
        self._sb_universe = QSpinBox()
        self._sb_universe.setRange(0, 15)
        self._cb_listener = QCheckBox("Listener enabled")

        net_form.addRow("Port", self._sb_port)
        net_form.addRow("Subnet", self._sb_subnet)
        net_form.addRow("Universe", self._sb_universe)
        net_form.addRow("", self._cb_listener)
        root.addWidget(net_group)

        # ── Mapping table ──
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            ["Function", "Enabled", "Channel", "Threshold", "Learn"]
        )
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setFont(make_font(FONT_SIZE_NORMAL))
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        root.addWidget(self._table, stretch=1)

        self._lbl_status = QLabel("")
        self._lbl_status.setFont(make_font(FONT_SIZE_SMALL))
        self._lbl_status.setStyleSheet(
            f"color: {C_ACCENT_ORANGE}; background: transparent;"
        )
        root.addWidget(self._lbl_status)

        # ── Buttons ──
        btn_row = QHBoxLayout()
        btn_revert = QPushButton("Revert")
        btn_revert.setToolTip("Discard edits and re-read the config file")
        btn_revert.clicked.connect(self._on_revert)

        btn_save = QPushButton("Save")
        btn_save.setToolTip("Write these settings to the config file")
        btn_save.clicked.connect(self._on_save)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)

        for b in (btn_revert, btn_save, btn_close):
            b.setFixedHeight(28)
            b.setFont(make_font(FONT_SIZE_SMALL))

        btn_row.addWidget(btn_revert)
        btn_row.addStretch()
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    # ── Refresh (discard edits, reload from disk) ───────────────────────────────

    def _on_revert(self):
        artnet_config.reload()
        self._cancel_learn()
        self.refresh()

    def refresh(self):
        self._cancel_learn()

        self._sb_port.setValue(artnet_config.port)
        self._sb_subnet.setValue(artnet_config.subnet)
        self._sb_universe.setValue(artnet_config.universe)
        self._cb_listener.setChecked(artnet_config.listen_enabled)

        mappings = artnet_config.all_mappings()
        rows = [(fn, mappings[fn]) for fn in FUNCTION_NAMES if fn in mappings]

        self._table.setRowCount(len(rows))
        self._row_widgets = []

        for row_idx, (fn, m) in enumerate(rows):
            label = FUNCTION_LABELS.get(fn, fn)
            it_fn = QTableWidgetItem(label)
            it_fn.setFont(make_font(FONT_SIZE_NORMAL))
            self._table.setItem(row_idx, 0, it_fn)

            cb_enabled = QCheckBox()
            cb_enabled.setChecked(m.get("enabled", True))
            self._table.setCellWidget(row_idx, 1, self._centered(cb_enabled))

            sb_channel = QSpinBox()
            sb_channel.setRange(1, 512)
            sb_channel.setValue(m.get("channel", 1))
            self._table.setCellWidget(row_idx, 2, sb_channel)

            sb_threshold = QSpinBox()
            sb_threshold.setRange(0, 255)
            sb_threshold.setValue(m.get("threshold", 128))
            sb_threshold.setEnabled(fn in TRIGGER_FUNCTIONS)
            self._table.setCellWidget(row_idx, 3, sb_threshold)

            btn_learn = QPushButton("Learn")
            btn_learn.setFixedWidth(64)
            btn_learn.setFont(make_font(FONT_SIZE_SMALL))
            btn_learn.clicked.connect(
                lambda _checked, r=row_idx: self._on_learn_clicked(r)
            )
            self._table.setCellWidget(row_idx, 4, btn_learn)

            self._row_widgets.append({
                "fn":         fn,
                "enabled":    cb_enabled,
                "channel":    sb_channel,
                "threshold":  sb_threshold,
                "learn_btn":  btn_learn,
            })

        self._table.resizeRowsToContents()
        self._lbl_status.setText("")

    @staticmethod
    def _centered(widget):
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch()
        lay.addWidget(widget)
        lay.addStretch()
        return wrap

    # ── Learn ─────────────────────────────────────────────────────────────────

    def _on_learn_clicked(self, row_idx):
        """Arm Learn on this row, or DISARM it if this row is already armed.

        dp-249: Learn used to be one-way. Arming a row set its button to "..."
        and then called `setEnabled(False)` on it, so the only ways back out
        were to send a DMX change (assigning a channel the user may not have
        wanted) or to Save/Revert/close the whole dialog. Arming a row by
        mistake, or changing your mind about which function to learn, left it
        stuck listening with no visible way to stop.

        It is now a toggle: click to arm, click the same button again to
        cancel. The button stays ENABLED while armed -- that is the fix; a
        disabled button cannot be clicked to cancel itself.
        """
        # Toggle OFF -- this row is the one currently listening.
        if self._learning_row == row_idx:
            self._cancel_learn()
            self._lbl_status.setText("Learn cancelled.")
            return

        bridge = _bridge()
        if bridge is None or not bridge.is_running:
            QMessageBox.warning(
                self, "ArtNet",
                "ArtNet listener is not running. Enable the listener and "
                "save before using Learn."
            )
            return
        # A DIFFERENT row is armed -- disarm it first, so exactly one row is
        # ever listening.
        if self._learning_row is not None:
            self._cancel_learn()

        self._learning_row = row_idx
        row = self._row_widgets[row_idx]
        row["learn_btn"].setText("Cancel")
        self._lbl_status.setText(
            f"Listening for the next DMX change to assign to "
            f"\"{FUNCTION_LABELS.get(row['fn'], row['fn'])}\"… "
            f"Click Cancel to stop."
        )
        bridge.arm_learn(lambda channel: self._on_learned(row_idx, channel))

    def _on_learned(self, row_idx, channel):
        # Invoked from ArtNetBridge's QTimer poll on the Qt main thread —
        # safe to touch widgets directly (see CLAUDE.md threading model).
        if self._learning_row != row_idx:
            return
        row = self._row_widgets[row_idx]
        row["channel"].setValue(channel)
        row["learn_btn"].setText("Learn")
        row["learn_btn"].setEnabled(True)
        self._learning_row = None
        self._lbl_status.setText(f"Assigned channel {channel}.")

    def _cancel_learn(self):
        if self._learning_row is not None:
            row = self._row_widgets[self._learning_row]
            row["learn_btn"].setText("Learn")
            row["learn_btn"].setEnabled(True)
            self._learning_row = None
        bridge = _bridge()
        if bridge is not None:
            bridge.disarm_learn()

    # ── Save ─────────────────────────────────────────────────────────────────

    def _on_save(self):
        self._cancel_learn()

        artnet_config.save_network(
            port=self._sb_port.value(),
            subnet=self._sb_subnet.value(),
            universe=self._sb_universe.value(),
            enabled=self._cb_listener.isChecked(),
        )
        for row in self._row_widgets:
            artnet_config.save_mapping(
                row["fn"],
                enabled=row["enabled"].isChecked(),
                channel=row["channel"].value(),
                threshold=row["threshold"].value(),
            )

        bridge = _bridge()
        if bridge is not None:
            bridge.reload_config()

        self.refresh()
        self._lbl_status.setText("Saved.")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._cancel_learn()
        super().closeEvent(event)

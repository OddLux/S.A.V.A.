"""
Transport widget — no fade, full text buttons, proper spacing.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QSlider, QGroupBox, QGridLayout, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFontMetrics

from ui.skin import (
    C_ACCENT, C_ACCENT_ORANGE, C_ACCENT_BLUE, C_TEXT_DIM,
    make_font, FONT_SIZE_SMALL, FONT_SIZE_NORMAL
)


class TransportWidget(QWidget):

    sig_play          = pyqtSignal()
    sig_pause         = pyqtSignal()
    sig_stop          = pyqtSignal()
    sig_next          = pyqtSignal()
    sig_prev          = pyqtSignal()

    sig_master_vol    = pyqtSignal(int)

    sig_shuffle       = pyqtSignal(bool)
    sig_repeat        = pyqtSignal(str)

    sig_loop_a        = pyqtSignal()
    sig_loop_b        = pyqtSignal()
    sig_loop_toggle   = pyqtSignal()
    sig_loop_clear    = pyqtSignal()

    sig_cue_set       = pyqtSignal(int)
    sig_cue_jump      = pyqtSignal(int)
    sig_cue_clear_all = pyqtSignal()

    sig_fin_set       = pyqtSignal()
    sig_fin_clear     = pyqtSignal()

    sig_start_set     = pyqtSignal()
    sig_start_clear   = pyqtSignal()

    # Extra width added on top of raw QFontMetrics text width when sizing
    # row-1 buttons (dp-174): covers the QSS padding (4px + 10px per side)
    # plus the 1px border on each side, with a small safety buffer so text
    # never crops at FONT_SIZE_NORMAL/FONT_SIZE_SMALL on any Windows DPI/font
    # rendering combo.
    _BTN_PAD_W = 28

    # dp-193: how long Prev/Next hold their "just triggered" flash style.
    # dp-183 originally shipped 150ms, verified only headlessly. Live
    # on-screen verification (dp-193) showed the fill itself renders
    # correctly (solid, high-contrast, matches Play/Pause/Stop's persistent
    # fill) - the actual problem is 150ms is too brief for a human to
    # reliably register against Play/Pause/Stop's *persistent* state fills.
    # Bumped to 380ms - still snappy, comfortably perceptible.
    _NAV_FLASH_MS = 380

    def __init__(self, parent=None):
        super().__init__(parent)
        self._repeat_mode = "none"
        self._shuffle_on  = False
        self._loop_active = False
        self._setup_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # ── Row 1: transport + volume + shuffle/repeat ─────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(4)

        H = 32

        # dp-195: the previous icons for Prev/Pause/Stop/Next used
        # U+23EE/U+23F8/U+23F9/U+23ED — all in the "Miscellaneous Technical"
        # block, which Windows resolves through the Segoe UI Emoji fallback
        # and paints as boxed blue/white color chips. Play's U+25B6
        # ("Geometric Shapes") has no emoji presentation and renders as a
        # plain monochrome triangle in the app's text color, which is the
        # look the user asked the others to match.
        #
        # So the four broken icons are replaced with non-emoji code points
        # chosen to sit as close to U+25B6 as possible: U+25C0 is its exact
        # mirror (same block), Next simply reuses U+25B6 itself (provably
        # safe — it already renders plain here), U+25A0 is the plain
        # Geometric Shapes square (NOT U+23F9, the emoji stop that was
        # breaking), and U+275A is a Dingbats bar with no emoji variant.
        # Each was confirmed by live window capture, not by code reading:
        # an offscreen/headless probe reports every one of these glyphs as
        # monochrome even when it is demonstrably an emoji chip on screen.
        self._btn_prev  = self._mk_btn("◀◀ Prev",  "Previous track", self.sig_prev,  H)
        self._btn_play  = self._mk_btn("▶ Play",   "Play",            self.sig_play,  H)
        self._btn_pause = self._mk_btn("❚❚ Pause", "Pause",           self.sig_pause, H)
        self._btn_stop  = self._mk_btn("■ Stop",   "Stop",            self.sig_stop,  H)
        self._btn_next  = self._mk_btn("Next ▶▶",  "Next track",      self.sig_next,  H)

        # Full QPushButton blocks (not partial color/border-color overrides)
        # so hover/pressed states and padding/radius stay consistent with
        # the rest of the row — Prev/Next read as dimmed/secondary but
        # still clearly bordered buttons (dp-162, fixes dp-151/152 regression).
        self._nav_base_style = (
            f"QPushButton {{ color: {C_TEXT_DIM}; border: 1px solid {C_TEXT_DIM}; }}"
            f"QPushButton:hover {{ background-color: #222222; border-color: {C_ACCENT}; }}"
            f"QPushButton:pressed {{ background-color: #111111; }}"
        )
        self._btn_prev.setStyleSheet(self._nav_base_style)
        self._btn_next.setStyleSheet(self._nav_base_style)

        self._play_base_style = (
            f"QPushButton {{ border-color: {C_ACCENT}; }}"
            f"QPushButton:hover {{ background-color: #003300; }}"
        )
        self._btn_play.setStyleSheet(self._play_base_style)

        self._stop_base_style = (
            f"QPushButton {{ border-color: {C_ACCENT_ORANGE}; color: {C_ACCENT_ORANGE}; }}"
            f"QPushButton:hover {{ background-color: #3a1500; border-color: {C_ACCENT_ORANGE}; }}"
            f"QPushButton:pressed {{ background-color: #1a0800; }}"
        )
        self._btn_stop.setStyleSheet(self._stop_base_style)

        # dp-183: Prev/Next have no persistent "state" the way Play/Pause/Stop
        # do (they're one-shot triggers, not a mode), so they get a brief
        # accent-filled flash on click instead - same visual weight as Play's
        # persistent active fill, just time-boxed rather than state-tied.
        self._btn_prev.clicked.connect(lambda: self._flash_nav_button(self._btn_prev))
        self._btn_next.clicked.connect(lambda: self._flash_nav_button(self._btn_next))

        row1.addWidget(self._btn_prev)
        row1.addSpacing(16)
        row1.addWidget(self._btn_play)
        row1.addWidget(self._btn_pause)
        row1.addWidget(self._btn_stop)
        row1.addSpacing(16)
        row1.addWidget(self._btn_next)

        row1.addSpacing(16)

        lbl_vol = QLabel("VOL")
        lbl_vol.setFont(make_font(FONT_SIZE_SMALL))
        lbl_vol.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")

        self._master_vol = QSlider(Qt.Orientation.Horizontal)
        self._master_vol.setRange(0, 100)
        self._master_vol.setValue(80)
        self._master_vol.setMinimumWidth(120)
        self._master_vol.setFixedHeight(18)
        self._master_vol.setToolTip("Master volume")
        self._master_vol.valueChanged.connect(self.sig_master_vol)

        self._lbl_vol_val = QLabel("80")
        self._lbl_vol_val.setFixedWidth(28)
        self._lbl_vol_val.setFont(make_font(FONT_SIZE_SMALL))
        self._lbl_vol_val.setStyleSheet(f"color: {C_ACCENT}; background: transparent;")
        self._master_vol.valueChanged.connect(
            lambda v: self._lbl_vol_val.setText(str(v))
        )

        row1.addWidget(lbl_vol)
        row1.addWidget(self._master_vol)
        row1.addWidget(self._lbl_vol_val)
        row1.addStretch()

        self._btn_shuffle = self._mk_btn(
            "Shuffle", "Shuffle on/off", self._on_shuffle, H,
            font_size=FONT_SIZE_SMALL,
        )
        self._btn_shuffle.setCheckable(True)

        # Pre-sized for the longest label it will ever show ("Repeat: All")
        # so cycling repeat modes never grows/shrinks the button or crops
        # its text mid-interaction.
        self._btn_repeat = self._mk_btn(
            "Repeat", "Repeat: none -> one -> all", self._on_repeat, H,
            font_size=FONT_SIZE_SMALL,
            extra_labels=["Repeat: 1", "Repeat: All"],
        )

        row1.addWidget(self._btn_shuffle)
        row1.addWidget(self._btn_repeat)
        root.addLayout(row1)

        # ── Row 2: Loop | Fin | Cues ───────────────────────────────────────
        # dp-229: Fin is now its own group box, sized to match "Loop A to B",
        # instead of a stretch-padded column tucked inside the Cue Points
        # box. Cue Points keeps the stretch and absorbs the space Fin used to
        # take from it. Giving Fin its own titled box also disambiguates its
        # "Clear" button, which previously sat inches from "Clear All Cues"
        # inside the same group and read as clearing cues.
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(self._build_loop_group())
        row2.addWidget(self._build_fin_group())
        row2.addWidget(self._build_cue_group(), stretch=1)
        root.addLayout(row2)

        # dp-185: row1's addStretch() means the layout's own minimumSizeHint
        # is the only thing standing between the volume slider and the
        # shuffle/repeat buttons overlapping once the window is squeezed —
        # but a top-level window's interactive resize floor is governed by
        # setMinimumSize(), not by a nested layout's computed minimum. Pin
        # it explicitly so this widget always enforces its own floor.
        self.setMinimumWidth(self.sizeHint().width())

    # ── Group builders ────────────────────────────────────────────────────────

    def _build_loop_group(self) -> QGroupBox:
        grp = QGroupBox("Loop A to B")
        grp.setFont(make_font(FONT_SIZE_SMALL))
        lay = QGridLayout(grp)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        BH = 28

        # dp-230: measured, not the old hardcoded BW = 72. Every button in
        # this group was narrower than the label it had to draw -- checked
        # against Qt's own QPushButton.sizeHint(), which uses the real style
        # padding rather than an estimate: "Set A"/"Set B"/"Clear" each needed
        # 82px, and the toggle needed 94px. Same defect class as dp-228's Fin
        # buttons: a width picked once for one set of labels, kept after the
        # labels changed.
        btn_la = self._mk_btn("Set A", "Set loop start point", self.sig_loop_a,
                              BH, font_size=FONT_SIZE_SMALL)
        btn_lb = self._mk_btn("Set B", "Set loop end point", self.sig_loop_b,
                              BH, font_size=FONT_SIZE_SMALL)
        btn_lc = self._mk_btn("Clear", "Clear loop points", self.sig_loop_clear,
                              BH, font_size=FONT_SIZE_SMALL)

        # The toggle swaps its own text at runtime (_on_loop_toggle), so it
        # must be pre-sized for the WIDEST label it will ever show -- that is
        # exactly what `extra_labels` is for, and "Disable" (94px) was the
        # worst offender at the old fixed 72px.
        self._btn_loop_tog = self._mk_btn(
            "Enable", "Toggle A to B loop on/off", self._on_loop_toggle, BH,
            font_size=FONT_SIZE_SMALL, extra_labels=["Disable"],
        )
        self._btn_loop_tog.setCheckable(True)
        # _mk_btn's `_BTN_PAD_W` estimate lands ~1px under what Qt's own style
        # asks for on the longer "Disable" label. Defer to Qt: set the text to
        # the widest label, take its real sizeHint, and floor the button at
        # that. Cheap, and it removes any argument about whether a 1px
        # shortfall elides on some DPI setting we have not tried.
        self._btn_loop_tog.setText("Disable")
        self._btn_loop_tog.setMinimumWidth(
            max(self._btn_loop_tog.minimumWidth(),
                self._btn_loop_tog.sizeHint().width())
        )
        self._btn_loop_tog.setText("Enable")

        lay.addWidget(btn_la,             0, 0)
        lay.addWidget(btn_lb,             0, 1)
        lay.addWidget(self._btn_loop_tog, 1, 0)
        lay.addWidget(btn_lc,             1, 1)
        return grp

    def _build_fin_group(self) -> QGroupBox:
        """dp-229: the Fin (custom track-end marker) controls as their own
        group, deliberately built with the SAME geometry as
        `_build_loop_group` -- identical `BW`/`BH`, margins, spacing and
        `QGridLayout` -- so the two boxes read as a matched pair rather than
        one being an afterthought beside the other.

        dp-232: row 1, previously an empty stretch row reserved only to
        match the loop group's height, now holds the Track Start marker's
        Set/Clear buttons -- same column widths as row 0, so the group's
        width is UNCHANGED (a `QGridLayout` column's width is the max across
        all rows sharing it; the Start row's "Set"/"Clear" text is identical
        to the End row's, so it does not widen either column). This is why
        the width stays pinned without needing a leading label column (which
        measured ~86px wider and blew the 1093px resize floor -- see the
        ticket for the rejected alternatives).

        Row identity is carried by an inline caption between the two button
        rows, mirroring how the group title captions the row above it. The
        caption spans both columns, so it only has to fit the group's
        existing ~168px width, not add to it. A `C_START_MARKER` tint on the
        Start buttons was tried first and dropped: colour alone is a weak
        carrier of meaning (crimson/chartreuse is the worst pairing for a
        red-green deficiency), and it made those two buttons the only ones
        in the transport not using the shared style.

        The extra row costs ~20px of height, which is free: "Cue Points" is
        124px tall and already sets row 2's height, while this group was 94px
        -- so it grows into existing headroom without moving anything.

        dp-254: START is the top row and owns the group TITLE; END is the
        bottom row and owns the inline caption. A track's start precedes its
        end, so top-to-bottom now matches the order the markers occur in
        (dp-232 had them the other way round, which read backwards).

        Neither title is suffixed ("Track End (Fin)" measures 165px, wider
        than the two 72px buttons plus margins, so it -- not the content --
        would set the group's minimum width and push the whole transport
        row's resize floor out; dp-185 pins that floor to sizeHint). Both
        bare forms are comfortably under the ~168px the buttons already set:
        "Track End" 99px, "Track Start" ~110px. Swapping which one is the
        QGroupBox title therefore cannot change the group's width, and the
        1093px floor asserted in tests/test_transport_start_buttons.py holds.
        The wording also matches the playlist context menu ("When this track
        ends")."""
        grp = QGroupBox("Track Start")
        grp.setFont(make_font(FONT_SIZE_SMALL))
        lay = QGridLayout(grp)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        BH = 28  # matches _build_loop_group's button height

        # _mk_btn (measured), NOT _mk_plain (fixed 72px like the loop group's
        # buttons). Copying the loop group's fixed width verbatim would have
        # reintroduced dp-228's exact bug: "Clear" needs 83px at
        # FONT_SIZE_SMALL and would crop at 72. Measured widths still land the
        # group at ~166px, i.e. the matched pair with "Loop A to B" the user
        # asked for -- the sizes agree because the content agrees, not because
        # a number was copied across.
        btn_set = self._mk_btn(
            "Set", "Set the end (Fin) marker at the current position",
            self.sig_fin_set, BH, font_size=FONT_SIZE_SMALL,
        )
        btn_clear = self._mk_btn(
            "Clear", "Clear the end (Fin) marker",
            self.sig_fin_clear, BH, font_size=FONT_SIZE_SMALL,
        )

        # dp-232: Track Start marker, same label text ("Set"/"Clear") so the
        # columns stay the same width -- see the docstring above.
        btn_start_set = self._mk_btn(
            "Set", "Set the start marker at the current position -- "
            "playback will begin here", self.sig_start_set, BH,
            font_size=FONT_SIZE_SMALL,
        )
        btn_start_clear = self._mk_btn(
            "Clear", "Clear the start marker",
            self.sig_start_clear, BH, font_size=FONT_SIZE_SMALL,
        )
        # dp-232: caption for the Start row, mirroring how the group title
        # captions the End row above. Spans both columns so it never widens
        # them. Deliberately no tint on the buttons themselves -- they use
        # the shared transport style like every other button.
        # Styled to match a QGroupBox title rather than a plain QLabel: the
        # QSS gives group titles `color: C_ACCENT` while bare QLabels get
        # `C_TEXT_PRIMARY` (white), which made this the only white heading in
        # the transport. `padding: 0 4px` is QGroupBox::title's own padding,
        # so the caption lines up with "Track End" above it.
        # dp-254: this inline caption now names the END row, since Start took
        # over row 0 and with it the group title. "Track End" is also the
        # NARROWER of the two strings, so moving it here (and "Track Start"
        # up to the group title) cannot widen the group -- see the width
        # reasoning in this method's docstring.
        lbl_end = QLabel("Track End")
        lbl_end.setFont(make_font(FONT_SIZE_SMALL))
        lbl_end.setStyleSheet(
            f"QLabel {{ color: {C_ACCENT}; background: transparent;"
            f" border: none; padding: 0; margin: 0; }}"
        )

        # dp-254: Start ABOVE End. A track's start point precedes its end
        # point, so reading the group top-to-bottom now follows the order the
        # markers actually occur in -- the previous layout asked the user to
        # read it backwards. The swap is purely which row each pair sits on:
        # the group TITLE captions row 0 and the inline QLabel captions row 2,
        # so the titles swap with the rows to keep each caption attached to
        # the pair it names.
        lay.addWidget(btn_start_set,   0, 0)
        lay.addWidget(btn_start_clear, 0, 1)
        lay.addWidget(lbl_end,         1, 0, 1, 2)
        lay.addWidget(btn_set,         2, 0)
        lay.addWidget(btn_clear,       2, 1)
        return grp

    def _build_cue_group(self) -> QGroupBox:
        grp = QGroupBox("Cue Points")
        grp.setFont(make_font(FONT_SIZE_SMALL))
        outer = QHBoxLayout(grp)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        CW, CH = 58, 28

        # ── Left zone: 8 cue buttons + Clear All Cues ───────────────────────
        left = QGridLayout()
        left.setSpacing(4)

        self._cue_btns = []
        for i in range(8):
            b = QPushButton(f"Cue {i + 1}")
            # dp-229: minimum + Expanding rather than a fixed width, so the
            # cue grid actually GROWS into the row's spare width (this group
            # carries the row's stretch). Fixed-size buttons left the group
            # box stretching while its contents stayed pinned at 58px in the
            # top-left corner.
            b.setMinimumSize(CW, CH)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setFont(make_font(FONT_SIZE_SMALL))
            b.setToolTip(f"Cue {i + 1}  |  Left-click: jump   Right-click: set")
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            n = i
            b.clicked.connect(lambda checked, idx=n: self.sig_cue_jump.emit(idx))
            b.customContextMenuRequested.connect(
                lambda pos, idx=n: self.sig_cue_set.emit(idx)
            )
            self._cue_btns.append(b)
            left.addWidget(b, i // 4, i % 4)

        btn_clr = QPushButton("Clear All Cues")
        btn_clr.setFixedHeight(CH)
        btn_clr.setFont(make_font(FONT_SIZE_SMALL))
        btn_clr.setToolTip("Clear all cue points for the current track")
        btn_clr.clicked.connect(self.sig_cue_clear_all)
        left.addWidget(btn_clr, 2, 0, 1, 4)

        outer.addLayout(left)

        # dp-229: the Fin controls and their separator used to live here.
        # They are now `_build_fin_group()`, a sibling box in row 2, and this
        # group keeps the row's stretch to itself.
        return grp

    # ── Factories ─────────────────────────────────────────────────────────────

    def _mk_btn(
        self, label, tip, signal, h, font_size=FONT_SIZE_NORMAL, extra_labels=None
    ) -> QPushButton:
        """Build a row-1 transport button sized to fit its label text.

        Width comes from `QFontMetrics` against the button's actual font,
        not a hardcoded pixel value, so the label never crops regardless of
        font size or Windows DPI/font-rendering config (dp-174). Pass
        `extra_labels` for buttons whose text changes at runtime (e.g. the
        repeat-mode cycle button) to reserve width for the longest label
        up front, so the button never resizes mid-interaction.
        """
        b = QPushButton(label)
        b.setToolTip(tip)
        font = make_font(font_size)
        b.setFont(font)
        fm = QFontMetrics(font)
        widest = max(
            fm.horizontalAdvance(text) for text in (label, *(extra_labels or ()))
        )
        b.setMinimumWidth(widest + self._BTN_PAD_W)
        b.setMinimumHeight(h)
        b.clicked.connect(signal)
        return b

    def _mk_plain(self, label, tip, signal, w, h) -> QPushButton:
        b = QPushButton(label)
        b.setToolTip(tip)
        b.setFont(make_font(FONT_SIZE_SMALL))
        b.setFixedSize(w, h)
        b.clicked.connect(signal)
        return b

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_shuffle(self, checked: bool):
        self._shuffle_on = checked
        self.sig_shuffle.emit(checked)

    def _on_repeat(self):
        order  = ["none", "one", "all"]
        labels = {"none": "Repeat", "one": "Repeat: 1", "all": "Repeat: All"}
        self._repeat_mode = order[(order.index(self._repeat_mode) + 1) % 3]
        self._btn_repeat.setText(labels[self._repeat_mode])
        self.sig_repeat.emit(self._repeat_mode)

    def _on_loop_toggle(self, checked: bool):
        self._loop_active = checked
        self._btn_loop_tog.setText("Disable" if checked else "Enable")
        self.sig_loop_toggle.emit()

    # ── Public updaters ───────────────────────────────────────────────────────

    def set_master_volume(self, value: int):
        self._master_vol.blockSignals(True)
        self._master_vol.setValue(value)
        self._master_vol.blockSignals(False)
        self._lbl_vol_val.setText(str(value))

    def set_shuffle(self, enabled: bool):
        self._btn_shuffle.setChecked(enabled)
        self._shuffle_on = enabled

    def set_repeat(self, mode: str):
        labels = {"none": "Repeat", "one": "Repeat: 1", "all": "Repeat: All"}
        self._repeat_mode = mode
        self._btn_repeat.setText(labels.get(mode, "Repeat"))

    def set_loop_active(self, active: bool):
        self._btn_loop_tog.setChecked(active)
        self._btn_loop_tog.setText("Disable" if active else "Enable")

    def set_cue_active(self, index: int, active: bool):
        if 0 <= index < len(self._cue_btns):
            self._cue_btns[index].setStyleSheet(
                f"background-color: {C_ACCENT_ORANGE}; color: #000;" if active else ""
            )

    def clear_all_cue_buttons(self):
        for b in self._cue_btns:
            b.setStyleSheet("")

    def set_playing(self, playing: bool):
        self._btn_play.setStyleSheet(
            f"background-color: {C_ACCENT}; color: #000; border-color: {C_ACCENT};"
            if playing else self._play_base_style
        )

    def set_paused(self, paused: bool):
        self._btn_pause.setStyleSheet(
            f"background-color: {C_ACCENT_BLUE}; color: #000;" if paused else ""
        )

    def set_stopped(self, stopped: bool):
        """dp-183: give Stop the same persistent active-state feedback as
        Play/Pause instead of only its static orange border - filled with
        its own accent color while playback is actually in the stopped
        state."""
        self._btn_stop.setStyleSheet(
            f"QPushButton {{ background-color: {C_ACCENT_ORANGE}; color: #000; "
            f"border-color: {C_ACCENT_ORANGE}; }}"
            if stopped else self._stop_base_style
        )

    def _flash_nav_button(self, btn: QPushButton):
        """dp-183: Prev/Next are one-shot triggers, not persistent states,
        so they get a brief accent-filled flash on click - same visual
        weight as Play's active fill, timed out instead of state-tied.

        dp-193: the fill itself was already rendering correctly (confirmed
        live), but a 1px border (inherited from the base app QSS - this
        rule only overrode border-color, not width) read as too subtle next
        to Play/Pause/Stop's persistent fills. Bumped to an explicit 2px
        border for extra visual weight, on top of the longer
        _NAV_FLASH_MS duration."""
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {C_ACCENT}; color: #000; "
            f"border: 2px solid {C_ACCENT}; }}"
        )
        QTimer.singleShot(self._NAV_FLASH_MS, lambda: btn.setStyleSheet(self._nav_base_style))
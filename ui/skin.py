"""
2000s rounded skin definitions with selectable colour themes.

All colours, fonts and QSS stylesheets live here so the rest of the UI
just imports what it needs. Five charcoal themes are available (orange,
green, blue, purple, toplo); orange is the reference design. The active
theme is chosen from settings at import time and applied on next launch
(see ui.main_window View -> Theme).

3D depth is faked with vertical qlineargradient fills (light top -> dark
bottom) plus 1px bevel borders, because Qt Style Sheets have no box-shadow.
"""

from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QApplication

from config.settings import settings

# ── Theme registry ─────────────────────────────────────────────────────────────
# Each palette defines the same keys; orange is the reference. Keys map 1:1 to
# the module-level C_* constants resolved below, so adding a theme is just adding
# a dict here. The bright/cool accents keep legacy names (ORANGE/BLUE) but mean
# "bright accent" and "contrast accent" — their hue follows the theme.
PALETTES = {
    "orange": {
        "bg_dark": "#241a14", "bg_mid": "#2e2017", "bg_light": "#3a2a1e",
        "accent": "#ff7a2e", "accent_dim": "#b5531c",
        "accent_bright": "#ffae5c", "accent_cool": "#5cc6ff",
        "text_primary": "#f5ece4", "text_dim": "#b89a86", "text_dark": "#6e5a4c",
        "border": "#15100b", "border_light": "#5a4030", "highlight": "#5a3010",
        "wave_bg": "#1a120c", "wave_fg": "#ff8a3d",
        "wave_pos": "#ffd9a0", "wave_loop": "#ffae5c",
        "slider_groove": "#15100b", "slider_handle": "#ff7a2e",
        "btn_bg": "#3a2a1e", "btn_hover": "#4a3526",
        "btn_pressed": "#1f160f", "btn_text": "#ffd9a0",
        "btn_grad_top": "#4a3526", "btn_grad_bot": "#2a1d13",
        "btn_hgrad_top": "#5c4330", "btn_hgrad_bot": "#382617",
        "accent_grad_top": "#ff9a52", "accent_grad_bot": "#e85f1c",
        "panel_grad_top": "#33241a", "panel_grad_bot": "#281c13",
        "vu_low": "#ffa54a", "vu_mid": "#ffcf3d", "vu_high": "#e8431f",
    },
    "green": {
        "bg_dark": "#16241a", "bg_mid": "#1d2e22", "bg_light": "#283a2e",
        "accent": "#2ec16a", "accent_dim": "#1c7a45",
        "accent_bright": "#6cf0a0", "accent_cool": "#5cc6ff",
        "text_primary": "#e8f5ec", "text_dim": "#93b89e", "text_dark": "#506e5a",
        "border": "#0b150f", "border_light": "#305a40", "highlight": "#14502a",
        "wave_bg": "#0c1a10", "wave_fg": "#3dd47a",
        "wave_pos": "#b8ffd0", "wave_loop": "#6cf0a0",
        "slider_groove": "#0b150f", "slider_handle": "#2ec16a",
        "btn_bg": "#1e3a28", "btn_hover": "#264a33",
        "btn_pressed": "#0f1f16", "btn_text": "#b8ffd0",
        "btn_grad_top": "#264a33", "btn_grad_bot": "#16291d",
        "btn_hgrad_top": "#2f5c40", "btn_hgrad_bot": "#1d3826",
        "accent_grad_top": "#4cd886", "accent_grad_bot": "#1ba556",
        "panel_grad_top": "#1a3324", "panel_grad_bot": "#13281b",
        "vu_low": "#3dd47a", "vu_mid": "#b6e83d", "vu_high": "#e8431f",
    },
    "blue": {
        "bg_dark": "#141a28", "bg_mid": "#1a2230", "bg_light": "#25303f",
        "accent": "#2e8bff", "accent_dim": "#1c5ab5",
        "accent_bright": "#5cb0ff", "accent_cool": "#ffb15c",
        "text_primary": "#e6effa", "text_dim": "#93a4b8", "text_dark": "#505e6e",
        "border": "#0b0f15", "border_light": "#30405a", "highlight": "#143a5a",
        "wave_bg": "#0c121a", "wave_fg": "#3d9bff",
        "wave_pos": "#b8dcff", "wave_loop": "#5cb0ff",
        "slider_groove": "#0b0f15", "slider_handle": "#2e8bff",
        "btn_bg": "#1e2a3a", "btn_hover": "#26354a",
        "btn_pressed": "#0f141f", "btn_text": "#b8dcff",
        "btn_grad_top": "#26354a", "btn_grad_bot": "#161f29",
        "btn_hgrad_top": "#2f405c", "btn_hgrad_bot": "#1d2838",
        "accent_grad_top": "#4ca0ff", "accent_grad_bot": "#1b6ae8",
        "panel_grad_top": "#1a2433", "panel_grad_bot": "#131c28",
        "vu_low": "#3d9bff", "vu_mid": "#3dd4d4", "vu_high": "#e8431f",
    },
    "purple": {
        "bg_dark": "#1e1428", "bg_mid": "#261a30", "bg_light": "#32253f",
        "accent": "#a64cff", "accent_dim": "#6f2cb5",
        "accent_bright": "#c98cff", "accent_cool": "#ffd24a",
        "text_primary": "#f0e8fa", "text_dim": "#a893b8", "text_dark": "#60506e",
        "border": "#120b18", "border_light": "#4a305a", "highlight": "#401455",
        "wave_bg": "#140c1a", "wave_fg": "#b45cff",
        "wave_pos": "#e0c8ff", "wave_loop": "#c98cff",
        "slider_groove": "#120b18", "slider_handle": "#a64cff",
        "btn_bg": "#2e1e3a", "btn_hover": "#3a264a",
        "btn_pressed": "#170f1f", "btn_text": "#e0c8ff",
        "btn_grad_top": "#3a264a", "btn_grad_bot": "#211629",
        "btn_hgrad_top": "#472f5c", "btn_hgrad_bot": "#2c1d38",
        "accent_grad_top": "#b86cff", "accent_grad_bot": "#8a2ce8",
        "panel_grad_top": "#2a1a33", "panel_grad_bot": "#1f1328",
        "vu_low": "#b45cff", "vu_mid": "#ff6bd6", "vu_high": "#e8431f",
    },
    # dp-231: brand palette supplied by the user. The three brand colours are
    # Warm Lemon #fedd31, Off black #373737 and Concrete grey #f1f2f2 — used as
    # accent, raised surface and primary text respectively. The backgrounds step
    # down from the brand off-black so the lemon keeps its contrast against
    # them; accent_cool stays blue because the brand set has no usable
    # complement and pause/loop must not read as the primary accent.
    "toplo": {
        "bg_dark": "#232323", "bg_mid": "#2d2d2d", "bg_light": "#373737",
        "accent": "#fedd31", "accent_dim": "#a8901a",
        "accent_bright": "#fff08a", "accent_cool": "#5cc6ff",
        "text_primary": "#f1f2f2", "text_dim": "#a0a0a0", "text_dark": "#6a6a6a",
        "border": "#141414", "border_light": "#4f4f4f", "highlight": "#5a4d0c",
        "wave_bg": "#1b1b1b", "wave_fg": "#fedd31",
        "wave_pos": "#fffbe0", "wave_loop": "#fff08a",
        "slider_groove": "#141414", "slider_handle": "#fedd31",
        "btn_bg": "#373737", "btn_hover": "#454545",
        "btn_pressed": "#1e1e1e", "btn_text": "#ffee8f",
        "btn_grad_top": "#454545", "btn_grad_bot": "#2b2b2b",
        "btn_hgrad_top": "#545454", "btn_hgrad_bot": "#383838",
        "accent_grad_top": "#ffe863", "accent_grad_bot": "#e5c40e",
        "panel_grad_top": "#333333", "panel_grad_bot": "#262626",
        "vu_low": "#fedd31", "vu_mid": "#ffab2e", "vu_high": "#e8431f",
    },
}

# Ordered list + display labels for the UI theme picker.
THEME_NAMES = ("orange", "green", "blue", "purple", "toplo")
THEME_LABELS = {
    "orange": "Orange (default)",
    "green":  "Green",
    "blue":   "Blue",
    "purple": "Purple",
    "toplo":  "Toplo",
}
DEFAULT_THEME = "orange"


def current_theme() -> str:
    """Return the active theme name from settings (falls back to default)."""
    name = settings.get("theme", DEFAULT_THEME)
    return name if name in PALETTES else DEFAULT_THEME


# ── Active palette -> module-level colour constants ─────────────────────────────
# Resolved once at import. Theme is restart-applied, so every widget that imports
# a C_* constant (including the custom painters) sees the chosen theme's value.
_pal = PALETTES[current_theme()]

C_BG_DARK       = _pal["bg_dark"]        # main window background
C_BG_MID        = _pal["bg_mid"]         # panel / widget background
C_BG_LIGHT      = _pal["bg_light"]       # slightly raised surfaces
C_ACCENT        = _pal["accent"]         # primary accent
C_ACCENT_DIM    = _pal["accent_dim"]     # dimmed accent (inactive elements)
C_ACCENT_ORANGE = _pal["accent_bright"]  # bright accent (hot-cue) — legacy name
C_ACCENT_BLUE   = _pal["accent_cool"]    # contrast accent (pause / loop) — legacy name
C_END_MARKER = "#dc2f3c"  # dp-199: crimson end ("Fin") marker, distinct from cue orange + pause/loop blue
# dp-232: fixed chartreuse start marker (user's choice). A fixed constant, not
# palette-derived, is what keeps it legible across every theme. Checked
# against and stays distinct from: green-theme accent #2ec16a, green-theme
# accent_bright #6cf0a0, and C_TIMELINE_PER_TRACK_PALETTE's #4cd97a -- all
# three lean cyan/teal-green, while #3beb00 sits at the yellow-green end of
# the spectrum, so it doesn't wash out against the green theme's accent glow.
C_START_MARKER = "#3beb00"
C_TEXT_PRIMARY  = _pal["text_primary"]   # main text
C_TEXT_DIM      = _pal["text_dim"]       # secondary / inactive text
C_TEXT_DARK     = _pal["text_dark"]      # disabled text
C_BORDER        = _pal["border"]         # widget borders (dark bevel bottom)
C_BORDER_LIGHT  = _pal["border_light"]   # bevel highlight (light top edge)
C_HIGHLIGHT     = _pal["highlight"]      # selected row in playlist
C_WAVEFORM_BG   = _pal["wave_bg"]        # waveform widget background
C_WAVEFORM_FG   = _pal["wave_fg"]        # waveform bars
C_WAVEFORM_POS  = _pal["wave_pos"]       # playhead needle
C_WAVEFORM_LOOP = _pal["wave_loop"]      # A->B loop region tint
C_SLIDER_GROOVE = _pal["slider_groove"]
C_SLIDER_HANDLE = _pal["slider_handle"]
C_BTN_BG        = _pal["btn_bg"]
C_BTN_HOVER     = _pal["btn_hover"]
C_BTN_PRESSED   = _pal["btn_pressed"]
C_BTN_TEXT      = _pal["btn_text"]

# Gradient stops (for qlineargradient 3D depth)
C_BTN_GRAD_TOP    = _pal["btn_grad_top"]
C_BTN_GRAD_BOT    = _pal["btn_grad_bot"]
C_BTN_HGRAD_TOP   = _pal["btn_hgrad_top"]
C_BTN_HGRAD_BOT   = _pal["btn_hgrad_bot"]
C_ACCENT_GRAD_TOP = _pal["accent_grad_top"]
C_ACCENT_GRAD_BOT = _pal["accent_grad_bot"]
C_PANEL_GRAD_TOP  = _pal["panel_grad_top"]
C_PANEL_GRAD_BOT  = _pal["panel_grad_bot"]

# VU-meter zone colours (low / mid safe zones themed, high stays red = clipping)
C_VU_LOW  = _pal["vu_low"]
C_VU_MID  = _pal["vu_mid"]
C_VU_HIGH = _pal["vu_high"]

# Crossfade timeline widget (dp-159) — derived from the existing per-theme
# constants above rather than new PALETTES keys, so all four themes get a
# consistent, correctly-contrasted timeline automatically without hand
# tuning four more hex values per theme.
C_TIMELINE_TRACK_ODD  = C_BG_MID
C_TIMELINE_TRACK_EVEN = C_BG_LIGHT
C_TIMELINE_OVERLAP    = C_ACCENT_DIM
C_TIMELINE_CURVE      = C_ACCENT_ORANGE
C_TIMELINE_HANDLE     = C_ACCENT

# dp-176: per-track color palette — each track gets its own distinct color
# from this list (index wraps with modulo past the palette's length), shared
# between its dp-170 marker line and its dp-175 jump-shortcut button so a
# user can match one to the other at a glance. Deliberately a fixed,
# theme-independent set of saturated hues (like C_VU_HIGH's constant red)
# rather than derived from the active theme's palette dict: the theme
# palettes only vary a couple of hues per theme (mostly shades of the same
# accent), which isn't enough distinct hue range for 6-8 clearly separable
# per-track colors, and C_TIMELINE_OVERLAP (=C_ACCENT_DIM) itself already
# spans red/green/blue/purple across the four themes, so no themed subset
# could reliably avoid blending into the overlap tint in every theme anyway.
# A fixed rainbow spread stays legible against C_TIMELINE_OVERLAP and against
# each other regardless of the active theme.
C_TIMELINE_PER_TRACK_PALETTE = (
    "#ff5c5c",  # red
    "#ff9a3d",  # orange
    "#f0d43d",  # yellow
    "#4cd97a",  # green
    "#3dd4c8",  # teal
    "#4c9aff",  # blue
    "#b46cff",  # purple
    "#ff6bc4",  # pink
)

# ── Fonts ─────────────────────────────────────────────────────────────────────
# dp-150: switched from "Courier New" (monospace/retro Winamp feel) to the
# 2000s Windows-native UI font. Qt's own font matching falls back to Tahoma
# (or the platform default) automatically when Segoe UI is unavailable, so a
# single family name is sufficient here -- no QFont.setFamilies() list needed.
FONT_FAMILY_MAIN  = "Segoe UI"
FONT_FAMILY_TITLE = "Arial"
FONT_SIZE_NORMAL  = 9
FONT_SIZE_SMALL   = 8
FONT_SIZE_LARGE   = 11
FONT_SIZE_DISPLAY  = 14             # track title / time display
FONT_SIZE_TIMECODE = 20             # dp-186: elapsed/total timecode - larger
                                     # than the track title so it reads as
                                     # the info bar's primary readout, still
                                     # fits the 48px-tall info_frame


def make_font(size: int = FONT_SIZE_NORMAL, bold: bool = False) -> QFont:
    f = QFont(FONT_FAMILY_MAIN, size)
    f.setBold(bold)
    return f


def make_display_font(size: int = FONT_SIZE_DISPLAY) -> QFont:
    f = QFont(FONT_FAMILY_MAIN, size, QFont.Weight.Bold)
    return f


def track_button_style(color: str) -> str:
    """dp-176: per-track colorized QSS for CrossfadeDialog's jump-shortcut
    buttons. Mirrors the QPushButton rule in STYLESHEET below (gradient
    fill, bevel border, hover/pressed states) so a colorized button still
    reads as "one of this app's buttons" rather than something hand-rolled
    — only the hue changes, driven by the same per-track color as that
    track's marker line (see track_color() in
    ui/crossfade_timeline_widget.py).

    The exact, unmodified `color` string is kept as the gradient's bottom
    stop (rather than only ever appearing lightened/darkened) so callers —
    including tests — can verify a button's identity color with a plain
    substring check against the returned QSS."""
    base = QColor(color)
    top    = base.lighter(115).name()
    hover_top    = base.lighter(130).name()
    hover_bottom = base.name()
    border       = base.darker(160).name()
    return f"""
        QPushButton {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {top}, stop:1 {color});
            color: {C_BG_DARK};
            border: 1px solid {border};
            border-top: 1px solid {top};
            border-radius: 7px;
            padding: 4px 10px;
            font-family: "{FONT_FAMILY_MAIN}";
            font-size: {FONT_SIZE_NORMAL}pt;
        }}
        QPushButton:hover {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {hover_top}, stop:1 {hover_bottom});
            border-color: {border};
        }}
        QPushButton:pressed {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {border}, stop:1 {top});
            border-top: 1px solid {border};
        }}
    """


# ── Master QSS stylesheet ─────────────────────────────────────────────────────
# Built once from the active palette's C_* constants. 3D depth is faked with
# vertical qlineargradient fills (light top -> dark bottom) plus 1px bevel
# borders, because Qt Style Sheets do not support box-shadow.
STYLESHEET = f"""
/* ── Global ── */
QWidget {{
    background-color: {C_BG_DARK};
    color: {C_TEXT_PRIMARY};
    font-family: "{FONT_FAMILY_MAIN}";
    font-size: {FONT_SIZE_NORMAL}pt;
    border: none;
    outline: none;
}}

QMainWindow, QDialog {{
    background-color: {C_BG_DARK};
}}

/* ── Panels / frames ── */
QFrame {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_PANEL_GRAD_TOP}, stop:1 {C_PANEL_GRAD_BOT});
    border: 1px solid {C_BORDER};
    border-top: 1px solid {C_BORDER_LIGHT};
    border-radius: 8px;
}}

QGroupBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_PANEL_GRAD_TOP}, stop:1 {C_PANEL_GRAD_BOT});
    border: 1px solid {C_ACCENT_DIM};
    border-radius: 8px;
    margin-top: 8px;
    padding-top: 6px;
    color: {C_ACCENT};
    font-size: {FONT_SIZE_SMALL}pt;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 4px;
    color: {C_ACCENT};
}}

/* ── Buttons ── */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BTN_GRAD_TOP}, stop:1 {C_BTN_GRAD_BOT});
    color: {C_BTN_TEXT};
    border: 1px solid {C_BORDER};
    border-top: 1px solid {C_BORDER_LIGHT};
    border-radius: 7px;
    padding: 4px 10px;
    font-family: "{FONT_FAMILY_MAIN}";
    font-size: {FONT_SIZE_NORMAL}pt;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BTN_HGRAD_TOP}, stop:1 {C_BTN_HGRAD_BOT});
    border-color: {C_ACCENT_DIM};
    border-top-color: {C_ACCENT};
    color: {C_TEXT_PRIMARY};
}}
QPushButton:pressed {{
    /* invert the gradient (dark top) to fake a pushed-in 3D state */
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BTN_PRESSED}, stop:1 {C_BTN_GRAD_TOP});
    border-top: 1px solid {C_BORDER};
    color: {C_ACCENT};
}}
QPushButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT_GRAD_TOP}, stop:1 {C_ACCENT_GRAD_BOT});
    border: 1px solid {C_ACCENT_DIM};
    color: {C_BG_DARK};
}}
QPushButton:disabled {{
    background: {C_BG_MID};
    color: {C_TEXT_DARK};
    border-color: {C_BORDER};
}}

/* ── Sliders ── */
QSlider::groove:horizontal {{
    background: {C_SLIDER_GROOVE};
    height: 6px;
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT_GRAD_TOP}, stop:1 {C_ACCENT_GRAD_BOT});
    border: 1px solid {C_ACCENT_DIM};
    width: 13px;
    height: 13px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {C_ACCENT_DIM}, stop:1 {C_ACCENT});
    border-radius: 3px;
}}
QSlider::groove:vertical {{
    background: {C_SLIDER_GROOVE};
    width: 6px;
    border-radius: 3px;
}}
QSlider::handle:vertical {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT_GRAD_TOP}, stop:1 {C_ACCENT_GRAD_BOT});
    border: 1px solid {C_ACCENT_DIM};
    width: 13px;
    height: 13px;
    margin: 0 -5px;
    border-radius: 7px;
}}
QSlider::sub-page:vertical {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT_DIM}, stop:1 {C_ACCENT});
    border-radius: 3px;
}}

/* ── List / playlist ── */
QListWidget {{
    background-color: {C_BG_MID};
    color: {C_TEXT_PRIMARY};
    border: 1px solid {C_BORDER};
    border-radius: 8px;
    alternate-background-color: {C_BG_LIGHT};
    selection-background-color: {C_HIGHLIGHT};
    selection-color: {C_ACCENT};
    padding: 2px;
}}
QListWidget::item {{
    padding: 3px 5px;
    border-radius: 4px;
}}
QListWidget::item:hover {{
    background-color: {C_BG_LIGHT};
}}
QListWidget::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_HIGHLIGHT}, stop:1 {C_BG_MID});
    color: {C_ACCENT};
}}

/* ── Scroll bars ── */
QScrollBar:vertical {{
    background: {C_BG_DARK};
    width: 10px;
    margin: 0;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {C_ACCENT_DIM}, stop:1 {C_ACCENT});
    min-height: 24px;
    border-radius: 5px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {C_BG_DARK};
    height: 10px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT}, stop:1 {C_ACCENT_DIM});
    min-width: 24px;
    border-radius: 5px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── Labels ── */
QLabel {{
    background: transparent;
    color: {C_TEXT_PRIMARY};
}}

/* ── Line edits / spin boxes ── */
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {C_BG_MID};
    color: {C_TEXT_PRIMARY};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 3px 6px;
    selection-background-color: {C_ACCENT_DIM};
}}
QLineEdit:focus, QSpinBox:focus {{
    border-color: {C_ACCENT};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: {C_BTN_BG};
    border: none;
    width: 14px;
}}

/* ── Combo boxes ── */
QComboBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BTN_GRAD_TOP}, stop:1 {C_BTN_GRAD_BOT});
    color: {C_TEXT_PRIMARY};
    border: 1px solid {C_BORDER};
    border-top: 1px solid {C_BORDER_LIGHT};
    border-radius: 6px;
    padding: 3px 6px;
}}
QComboBox QAbstractItemView {{
    background-color: {C_BG_MID};
    color: {C_TEXT_PRIMARY};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    selection-background-color: {C_HIGHLIGHT};
}}
QComboBox::drop-down {{
    border: none;
    background: transparent;
    width: 18px;
}}

/* ── Tab widget ── */
QTabWidget::pane {{
    border: 1px solid {C_BORDER};
    border-radius: 8px;
    background: {C_BG_MID};
}}
QTabBar::tab {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BG_LIGHT}, stop:1 {C_BG_DARK});
    color: {C_TEXT_DIM};
    padding: 5px 12px;
    border: 1px solid {C_BORDER};
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT_GRAD_TOP}, stop:1 {C_ACCENT_GRAD_BOT});
    color: {C_BG_DARK};
}}
QTabBar::tab:hover:!selected {{
    color: {C_TEXT_PRIMARY};
}}

/* ── Tooltips ── */
QToolTip {{
    background-color: {C_BG_MID};
    color: {C_ACCENT};
    border: 1px solid {C_ACCENT_DIM};
    border-radius: 6px;
    padding: 3px 6px;
    font-size: {FONT_SIZE_SMALL}pt;
}}

/* ── Menu bar / menus ── */
QMenuBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BG_LIGHT}, stop:1 {C_BG_DARK});
    color: {C_TEXT_PRIMARY};
    border-bottom: 1px solid {C_BORDER};
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
    border-radius: 6px;
}}
QMenuBar::item:selected {{
    background-color: {C_HIGHLIGHT};
    color: {C_ACCENT};
}}
QMenu {{
    background-color: {C_BG_MID};
    color: {C_TEXT_PRIMARY};
    border: 1px solid {C_BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 4px 18px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background-color: {C_HIGHLIGHT};
    color: {C_ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background: {C_BORDER};
    margin: 4px 6px;
}}

/* ── Progress bar ── */
QProgressBar {{
    background-color: {C_BG_MID};
    border: 1px solid {C_BORDER};
    border-radius: 7px;
    text-align: center;
    color: {C_TEXT_PRIMARY};
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {C_ACCENT_GRAD_BOT}, stop:1 {C_ACCENT_GRAD_TOP});
    border-radius: 7px;
}}

/* ── Check boxes ── */
QCheckBox {{
    color: {C_TEXT_PRIMARY};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 13px;
    height: 13px;
    border: 1px solid {C_ACCENT_DIM};
    background: {C_BG_MID};
    border-radius: 4px;
}}
QCheckBox::indicator:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_ACCENT_GRAD_TOP}, stop:1 {C_ACCENT_GRAD_BOT});
    border-color: {C_ACCENT};
}}

/* ── Table widget (DMX config) ── */
QTableWidget {{
    background-color: {C_BG_MID};
    gridline-color: {C_BORDER};
    color: {C_TEXT_PRIMARY};
    selection-background-color: {C_HIGHLIGHT};
    selection-color: {C_ACCENT};
    border: 1px solid {C_BORDER};
    border-radius: 8px;
}}
QHeaderView::section {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {C_BG_LIGHT}, stop:1 {C_BG_DARK});
    color: {C_ACCENT};
    border: 1px solid {C_BORDER};
    padding: 4px;
    font-size: {FONT_SIZE_SMALL}pt;
}}
QTableWidget::item {{
    padding: 2px 4px;
}}
"""


def apply_skin(app: QApplication):
    """Call once at startup to apply the active theme app-wide."""
    app.setStyleSheet(STYLESHEET)
    app.setFont(make_font())

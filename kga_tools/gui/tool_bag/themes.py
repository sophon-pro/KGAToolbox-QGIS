"""Bag designs (themes), their stylesheet, tinted icons, previews and the style picker."""

import os

from qgis.PyQt.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel, QSlider,
                                 QToolButton, QVBoxLayout)

from .compat import OWN_PREFIX

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
# Ways the bag can be hung / fastened at the top: key -> (label, room needed above the bag in px)
FIXTURE_SCALE = 0.75
HANDLE_STYLES = {
    "handle": ("Handle (carry loop)", 21),
    "strap": ("Strap (flat band)", 18),
    "pin": ("Pin (push-pin)", 17),
    "clip": ("Clip (binder clip)", 17),
    "hook": ("Hook (hanger)", 21),
    "ring": ("Ring (key ring)", 21),
    "tape": ("Tape (washi tape)", 14),
    "none": ("None", 10),
}
CHEVRON = os.path.join(ICON_DIR, "chevron_down.svg").replace("\\", "/")
PASTEL = ["#8ecae6", "#ffb4a2", "#b5e48c", "#cdb4db", "#ffd166"]


def _theme(label, top, bottom, handle, **kw):
    t = dict(
        label=label, top=top, bottom=bottom, handle=handle,
        title="#ffffff", body="#ffffff", border="#d6e2f3", ink="#24324a",
        btn="#ffffff", btn_border="#e3e9f2", hover="#f2f7ff", hover_border="#b9d3fb",
        checked="#e3eeff", checked_border="#6fa3f5", checked_ink="#1d4ed8",
        disabled="#a7b1bf", disabled_bg="#fbfcfe",
        bubble="rgba(255,255,255,0.88)", bubble_hover="#ffffff", bubble_icon="#3a4756",
        accents=PASTEL, zip_band="#e4ecf8", zip_teeth="#c3d3ea", pull="#ffd166", pull_border="#d4a72c",
        hint="#6b7a90", scroll="#c9d8ee", scroll_hover="#9fbbe4", indicator=bottom,
    )
    t.update(kw)
    return t


THEMES = {
    "sky": _theme("Sky Blue", "#7cb8ff", "#5b95f5", "#4f86e6"),
    "peach": _theme("Peach", "#ffb59e", "#ff8a7a", "#f06b5e",
                    border="#f4d6cf", hover="#fff4f1", hover_border="#ffc7b8", checked="#ffe9e3",
                    checked_border="#ff9c8a", checked_ink="#c2410c", zip_band="#fbe9e4", zip_teeth="#f1c9bf",
                    scroll="#f3cfc6", scroll_hover="#eaa898"),
    "mint": _theme("Mint", "#7fdcb5", "#3fbf8f", "#2fa577",
                   border="#cdeee0", hover="#effbf6", hover_border="#a7e4c9", checked="#dcf5ea",
                   checked_border="#4cc596", checked_ink="#047857", zip_band="#e3f6ee", zip_teeth="#b9e6d3",
                   scroll="#bfe8d6", scroll_hover="#8fd6b8"),
    "lavender": _theme("Lavender", "#c3a6ff", "#9b7bf2", "#8360e0",
                       border="#e3d9fb", hover="#f7f3ff", hover_border="#d2c1fb", checked="#ede5ff",
                       checked_border="#a78bfa", checked_ink="#6d28d9", zip_band="#efe9fd", zip_teeth="#d6c8f8",
                       scroll="#dccff9", scroll_hover="#bda6f3"),
    "sakura": _theme("Sakura", "#ffb6d0", "#ff86b0", "#ec5f93",
                     border="#f9d6e3", hover="#fff3f8", hover_border="#ffc2d8", checked="#ffe4ef",
                     checked_border="#f989b2", checked_ink="#be185d", zip_band="#fde8f0", zip_teeth="#f6c4d7",
                     scroll="#f8cadb", scroll_hover="#f19bbc"),
    "lemon": _theme("Lemon", "#ffe27a", "#ffc93c", "#e0a800", title="#5b4300",
                    border="#f5e3a8", hover="#fffbea", hover_border="#f6dc84", checked="#fff3c4",
                    checked_border="#f0c23b", checked_ink="#92400e", zip_band="#fbf2d2", zip_teeth="#efdc9a",
                    pull="#ff8fab", pull_border="#e06c8c", scroll="#f3e2a6", scroll_hover="#e8cc6a"),
    "kraft": _theme("Kraft", "#c99c6e", "#a87b4f", "#8a5f38", body="#fffaf3",
                    border="#e8d8c3", btn="#fffdf9", btn_border="#eadfce", hover="#fbf3e7",
                    hover_border="#dcc3a1", checked="#f5e6d1", checked_border="#c99c6e", checked_ink="#7c4a1e",
                    accents=["#c99c6e", "#9bb37a", "#d98c6a", "#8fa9c4", "#e2b75c"],
                    zip_band="#f1e6d6", zip_teeth="#dcc6a8", pull="#e2b75c", pull_border="#a98330",
                    scroll="#e5d3bb", scroll_hover="#cfb08a", disabled_bg="#fdf8f1"),
    "classic": _theme("Classic White", "#ffffff", "#f3f6fa", "#b8c4d6", title="#24324a",
                      bubble="#eef2f7", bubble_hover="#e2e8f0",
                      accents=["#dbe3ee"] * 5, zip_band="#eef2f7", zip_teeth="#d5dde8",
                      pull="#cbd5e1", pull_border="#94a3b8", indicator="#5b95f5"),
    "midnight": _theme("Midnight", "#3a4660", "#2b3447", "#55658a", body="#1f2633", border="#39445a",
                       ink="#e6ecf5", btn="#283041", btn_border="#36415a", hover="#323d55",
                       hover_border="#4f6086", checked="#2f4f86", checked_border="#6fa3f5",
                       checked_ink="#ffffff", disabled="#6b7690", disabled_bg="#232a38",
                       bubble="rgba(255,255,255,0.14)", bubble_hover="rgba(255,255,255,0.26)",
                       bubble_icon="#e6ecf5", zip_band="#2a3345", zip_teeth="#45526e", pull="#ffd166",
                       hint="#9aa6bd", scroll="#46536f", scroll_hover="#5d6d90", indicator="#6fa3f5"),
}
DEFAULT_THEME = "sky"

QSS = """
QFrame#ctwBag { background: transparent; border: none; }
QWidget#ctwHeader { background: transparent; border: none; }
QLabel#ctwTitle { color: %(title)s; font-weight: 700; background: transparent; border: none; }
QToolButton#ctwHdrBtn { background: %(bubble)s; border: 1px solid rgba(255,255,255,0.55); border-radius: 11px;
                        padding: 3px; }
QToolButton#ctwHdrBtn:hover { background: %(bubble_hover)s; }
QToolButton#ctwHdrBtn:checked { background: #ffe08a; }
QScrollArea, QWidget#ctwBody { background: transparent; border: none; }
QToolButton#ctwTool { background: %(btn)s; border: 1px solid %(btn_border)s; border-left: 4px solid %(a0)s;
                      border-radius: %(br)spx; padding: %(pad)s; color: %(ink)s; }
QToolButton#ctwTool:hover { background: %(hover)s; border-top-color: %(hover_border)s;
                            border-right-color: %(hover_border)s; border-bottom-color: %(hover_border)s; }
QToolButton#ctwTool:pressed { background: %(checked)s; }
QToolButton#ctwTool:checked { background: %(checked)s; border-top-color: %(checked_border)s;
                              border-right-color: %(checked_border)s; border-bottom-color: %(checked_border)s;
                              color: %(checked_ink)s; font-weight: 600; }
QToolButton#ctwTool:disabled { color: %(disabled)s; background: %(disabled_bg)s; }
%(accent_rules)s
QToolButton#ctwMissing { background: #fffaf0; border: 1px dashed #e0b25a; border-radius: %(br)spx;
                         padding: %(pad)s; color: #8a6116; }
QLabel#ctwHint { color: %(hint)s; background: transparent; border: none; }
QFrame#ctwIndicator { background: %(indicator)s; border: none; border-radius: 1px; }
QSizeGrip { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 2px 18px 0; }
QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 3px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: %(scroll_hover)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


def scrollbar_stylesheet(t):
    """The slim, rounded scroll bar. Set directly on the bar itself (not only on the panel) so that
    a global QGIS / OS theme cannot swap it for the classic arrow scroll bar."""
    return """
        QScrollBar:vertical { background: transparent; border: none; width: 8px; margin: 4px 2px 18px 0; }
        QScrollBar::handle:vertical { background: %(scroll)s; border: none; border-radius: 3px; min-height: 24px; }
        QScrollBar::handle:vertical:hover { background: %(scroll_hover)s; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; width: 0; border: none; background: none; }
        QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical { width: 0; height: 0; background: none; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
    """ % t


def theme(key):
    return THEMES.get(key) or THEMES[DEFAULT_THEME]


def stylesheet(t, radius, compact):
    values = dict(t)
    values["r"] = radius
    values["br"] = 7 if compact else 9
    values["pad"] = "2px 6px" if compact else "5px 8px"
    values["a0"] = t["accents"][0]
    values["accent_rules"] = "\n".join(
        f'QToolButton#ctwTool[accent="{i}"] {{ border-left-color: {c}; }}' for i, c in enumerate(t["accents"]))
    return QSS % values


_icon_cache = {}


def tinted_icon(name, color):
    """Load one of the plugin's line icons recoloured for the current theme."""
    key = (name, color)
    if key not in _icon_cache:
        path = os.path.join(ICON_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                svg = fh.read().replace("#3a4756", color).replace("#ffffff", color if name == "bag_white.svg" else "#ffffff")
            pix = QPixmap()
            if pix.loadFromData(QByteArray(svg.encode("utf-8")), "SVG"):
                _icon_cache[key] = QIcon(pix)
            else:
                _icon_cache[key] = QIcon(path)
        except OSError:
            _icon_cache[key] = QIcon(path)
    return _icon_cache[key]


def preview_pixmap(t, w=70, h=92):
    """A tiny drawing of the bag in this design (used in the style picker)."""
    pix = QPixmap(w, h)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # handle
    path = QPainterPath(QPointF(w * 0.32, 20))
    path.cubicTo(QPointF(w * 0.32, 2), QPointF(w * 0.68, 2), QPointF(w * 0.68, 20))
    pen = QPen(QColor(t["handle"]), 4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawPath(path)
    # body
    body = QRectF(4, 14, w - 8, h - 18)
    p.setPen(QPen(QColor(t["border"]), 1.2))
    p.setBrush(QColor(t["body"]))
    p.drawRoundedRect(body, 9, 9)
    # header
    hdr = QPainterPath()
    hdr.addRoundedRect(QRectF(body.left(), body.top(), body.width(), 18), 9, 9)
    hdr.addRect(QRectF(body.left(), body.top() + 9, body.width(), 9))
    g = QLinearGradient(0, body.top(), 0, body.top() + 18)
    g.setColorAt(0, QColor(t["top"]))
    g.setColorAt(1, QColor(t["bottom"]))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawPath(hdr.simplified())
    # zipper
    p.setBrush(QColor(t["zip_teeth"]))
    x = body.left() + 5
    while x < body.right() - 12:
        p.drawRect(QRectF(x, body.top() + 20, 2, 3))
        x += 4
    p.setBrush(QColor(t["pull"]))
    p.drawRoundedRect(QRectF(body.right() - 11, body.top() + 19, 7, 5), 2, 2)
    # stitched seam
    pen = QPen(QColor(t["handle"]), 0.8)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setOpacity(0.5)
    p.drawRoundedRect(QRectF(body.left() + 3, body.top() + 26, body.width() - 6, body.height() - 29), 6, 6)
    p.setOpacity(1.0)
    # three little tool chips
    for i in range(3):
        r = QRectF(body.left() + 7, body.top() + 29 + i * 15, body.width() - 14, 12)
        p.setPen(QPen(QColor(t["btn_border"]), 1))
        p.setBrush(QColor(t["checked"] if i == 2 else t["btn"]))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(Qt.PenStyle.NoPen)
        c = QColor(t["accents"][i % len(t["accents"])])
        c.setAlphaF(0.95 if i == 2 else 0.55)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(r.left() + 2, r.top() + 2, 8, 8), 2.5, 2.5)
        p.setBrush(QColor(t["ink"]))
        p.setOpacity(0.35)
        p.drawRoundedRect(QRectF(r.left() + 13, r.center().y() - 1, r.width() * 0.45, 2), 1, 1)
        p.setOpacity(1.0)
    p.end()
    return pix


class StylePicker(QFrame):
    """Popup gallery: click a design to apply it immediately."""

    def __init__(self, controller, parent):
        # Frameless + translucent window so the rounded card below is really round
        # (a plain Popup is a square window with a square OS shadow behind it).
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.ctrl = controller
        self.setObjectName(OWN_PREFIX + "_stylepicker")
        card = QFrame(self)
        card.setObjectName(OWN_PREFIX + "_stylecard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 70))
        card.setGraphicsEffect(shadow)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 14)
        outer.addWidget(card)
        self.setStyleSheet("""
            QFrame#%s { background: #ffffff; border: 1px solid #d6e2f3; border-radius: 12px; }
            QLabel { color: #24324a; background: transparent; }
            QToolButton { background: #ffffff; border: 2px solid transparent; border-radius: 10px;
                          padding: 4px; color: #24324a; }
            QToolButton:hover { background: #f2f7ff; }
            QToolButton:checked { border-color: #5b95f5; background: #eef5ff; }
            QCheckBox { color: #24324a; }
            QComboBox { background: #ffffff; color: #24324a; border: 1px solid #d6e2f3;
                        border-radius: 6px; padding: 2px 26px 2px 8px; min-width: 120px; }
            QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
                        width: 22px; border: none; background: transparent; }
            QComboBox::down-arrow { image: url(%s); width: 12px; height: 12px; }
            QComboBox QAbstractItemView { background: #ffffff; color: #24324a;
                        selection-background-color: #eef5ff; selection-color: #24324a; }
        """ % (card.objectName(), CHEVRON))
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        title = QLabel("<b>Choose your bag</b>")
        lay.addWidget(title)
        grid = QGridLayout()
        grid.setSpacing(4)
        self.buttons = {}
        for i, (key, t) in enumerate(THEMES.items()):
            b = QToolButton()
            b.setCheckable(True)
            b.setChecked(key == controller.theme)
            b.setIcon(QIcon(preview_pixmap(t)))
            b.setIconSize(QSize(70, 92))
            b.setText(t["label"])
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setToolTip(f"{t['label']} bag")
            b.clicked.connect(lambda *_, k=key: self._choose(k))
            grid.addWidget(b, i // 3, i % 3)
            self.buttons[key] = b
        lay.addLayout(grid)

        sliders = QGridLayout()
        sliders.setHorizontalSpacing(8)
        sliders.addWidget(QLabel("Background"), 0, 0)
        self.background = QSlider(Qt.Orientation.Horizontal)
        self.background.setRange(0, 90)
        self.background.setValue(int(round(controller.bg_transparency * 100)))
        self.background.setToolTip("See-through bag fabric: the map shows behind your tools,\n"
                                   "while the tools themselves stay clear and readable")
        self.bg_value = QLabel()
        self.background.valueChanged.connect(self._background_changed)
        sliders.addWidget(self.background, 0, 1)
        sliders.addWidget(self.bg_value, 0, 2)
        sliders.addWidget(QLabel("Fade all"), 1, 0)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(40, 100)
        self.opacity.setValue(int(round(controller.opacity * 100)))
        self.opacity.setToolTip("Fade the whole bag, tools included")
        self.op_value = QLabel()
        self.opacity.valueChanged.connect(self._opacity_changed)
        sliders.addWidget(self.opacity, 1, 1)
        sliders.addWidget(self.op_value, 1, 2)
        for lbl in (self.bg_value, self.op_value):
            lbl.setMinimumWidth(34)
            lbl.setStyleSheet("color:#6b7a90")
        self._update_values()
        if not controller.panel_supports_transparency():
            self.background.setEnabled(False)
            self.background.setToolTip("Your desktop does not support see-through windows")
        lay.addLayout(sliders)
        row = QHBoxLayout()
        self.handle = QComboBox()
        for key, (label, _room) in HANDLE_STYLES.items():
            self.handle.addItem(label, key)
        self.handle.setCurrentIndex(max(0, self.handle.findData(controller.handle_style)))
        self.handle.setToolTip("How the bag hangs: carry loop, pin, clip, hook, ...")
        self.handle.currentIndexChanged.connect(
            lambda _i: controller.set_style(handle_style=self.handle.currentData()))
        self.compact = QCheckBox("Compact")
        self.compact.setChecked(controller.compact)
        self.compact.toggled.connect(lambda on: controller.set_style(compact=on))
        row.addWidget(QLabel("Top"))
        row.addWidget(self.handle)
        row.addWidget(self.compact)
        row.addStretch(1)
        lay.addLayout(row)

        # how the tools are laid out in the bag
        from .panel import MAX_COLUMNS, VIEW_MODES
        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("Tools"))
        self.view_combo = QComboBox()
        for key, label in VIEW_MODES.items():
            self.view_combo.addItem(label, key)
        self.view_combo.setCurrentIndex(max(0, self.view_combo.findData(controller.view_mode)))
        self.view_combo.setToolTip("List (one tool per row) or a grid of tiles")
        vrow.addWidget(self.view_combo, 1)
        vrow.addWidget(QLabel("Columns"))
        self.cols_combo = QComboBox()
        for n in range(1, MAX_COLUMNS + 1):
            self.cols_combo.addItem(str(n), n)
        self.cols_combo.setCurrentIndex(max(0, self.cols_combo.findData(controller.columns)))
        self.cols_combo.setToolTip("Columns of tools, in the list or the grid (up to 3)")
        vrow.addWidget(self.cols_combo)
        self.view_combo.currentIndexChanged.connect(self._view_changed)
        self.cols_combo.currentIndexChanged.connect(
            lambda _i: controller.set_view(columns=self.cols_combo.currentData()))
        lay.addLayout(vrow)

        # bubble on the map: show it, and pick one of nine places for it
        from .bubble import BUBBLE_POSITIONS
        brow = QHBoxLayout()
        left = QVBoxLayout()
        self.bubble_show = QCheckBox("Minimize to a bubble")
        self.bubble_show.setChecked(controller.bubble_visible)
        self.bubble_show.toggled.connect(lambda on: controller.set_bubble(visible=on))
        left.addWidget(self.bubble_show)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Size"))
        from .bubble import MAX_BUBBLE, MIN_BUBBLE
        self.bubble_size = QSlider(Qt.Orientation.Horizontal)
        self.bubble_size.setRange(MIN_BUBBLE, MAX_BUBBLE)
        self.bubble_size.setValue(controller.bubble_size)
        self.bubble_size.setToolTip("Size of the bubble on the map")
        self.bubble_size_value = QLabel(f"{controller.bubble_size} px")
        self.bubble_size_value.setMinimumWidth(36)
        self.bubble_size_value.setStyleSheet("color:#6b7a90")
        self.bubble_size.valueChanged.connect(self._bubble_size_changed)
        size_row.addWidget(self.bubble_size, 1)
        size_row.addWidget(self.bubble_size_value)
        left.addLayout(size_row)
        hint = QLabel("The bag hides in the bubble.\nPlace it on the map:")
        hint.setStyleSheet("color:#6b7a90")
        left.addWidget(hint)
        left.addStretch(1)
        brow.addLayout(left, 1)
        pad = QFrame()
        pad.setObjectName("tbBubblePad")
        pad.setStyleSheet("QFrame#tbBubblePad { background:#eef3fb; border:1px solid #d6e2f3; border-radius:6px; }")
        pg = QGridLayout(pad)
        pg.setContentsMargins(4, 4, 4, 4)
        pg.setSpacing(3)
        self.bubble_buttons = {}
        for key, (label, h, v) in BUBBLE_POSITIONS.items():
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFixedSize(16, 12)
            b.setToolTip(label)
            b.setStyleSheet("""
                QToolButton { background:#ffffff; border:1px solid #cfdaec; border-radius:6px; }
                QToolButton:hover { border-color:#5b95f5; }
                QToolButton:checked { background:#5b95f5; border-color:#3f78dd; }""")
            b.setChecked(key == controller.bubble_position)
            b.clicked.connect(lambda *_, k=key: controller.set_bubble(position=k))
            pg.addWidget(b, v, h)
            self.bubble_buttons[key] = b
        brow.addWidget(pad)
        lay.addLayout(brow)

    def _view_changed(self, _i):
        self.ctrl.set_view(mode=self.view_combo.currentData())
        self.cols_combo.blockSignals(True)          # the bag may have picked a sensible column count
        self.cols_combo.setCurrentIndex(max(0, self.cols_combo.findData(self.ctrl.columns)))
        self.cols_combo.blockSignals(False)

    def _bubble_size_changed(self, v):
        self.bubble_size_value.setText(f"{v} px")
        self.ctrl.set_bubble(size=v)

    def _update_values(self):
        self.bg_value.setText(f"{self.background.value()}%")
        self.op_value.setText(f"{self.opacity.value()}%")

    def _background_changed(self, v):
        self._update_values()
        self.ctrl.set_style(bg_transparency=v / 100.0)

    def _opacity_changed(self, v):
        self._update_values()
        self.ctrl.set_style(opacity=v / 100.0)

    def _choose(self, key):
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        self.ctrl.set_style(theme=key)

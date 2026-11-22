# -*- coding: utf-8 -*-
"""Shared look: icons (inline SVG tinted to the current palette), buttons and stylesheet."""

from qgis.PyQt.QtCore import QByteArray, QEvent, QRectF, QSize
from qgis.PyQt.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QLabel, QToolButton

from . import compat as C

# 24x24 stroke icons; "currentColor" is replaced by the palette text colour
_PATHS = {
    "attributes": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14h18M9 9v11"/>',
    "labels": '<path d="M5 6h14M12 6v13M9 19h6"/>',
    "symbols": '<circle cx="7" cy="7" r="3"/><path d="M14 4l3 6h-6z"/><rect x="4" y="14" width="6" height="6" rx="1"/>'
               '<path d="M14 17c2-3 4 3 6 0"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14h18M9 4v16M15 4v16"/>',
    "first": '<path d="M7 6v12M17 6l-7 6 7 6"/>',
    "prev": '<path d="M15 6l-6 6 6 6"/>',
    "next": '<path d="M9 6l6 6-6 6"/>',
    "last": '<path d="M17 6v12M7 6l7 6-7 6"/>',
    "zoom": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5M11 8v6M8 11h6"/>',
    "flash": '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    "switch": '<path d="M4 8h13M14 4.5L17.5 8 14 11.5M20 16H7M10 12.5L6.5 16 10 19.5"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    # the same gear as the Tool Bag / Symbol Bag (symbol_bag/icons/settings.svg)
    "settings": '<polygon points="19.25,10.53 21.52,10.76 21.52,13.24 19.25,13.47 18.17,16.09 19.61,17.85 '
                '17.85,19.61 16.09,18.17 13.47,19.25 13.24,21.52 10.76,21.52 10.53,19.25 7.91,18.17 6.15,19.61 '
                '4.39,17.85 5.83,16.09 4.75,13.47 2.48,13.24 2.48,10.76 4.75,10.53 5.83,7.91 4.39,6.15 6.15,4.39 '
                '7.91,5.83 10.53,4.75 10.76,2.48 13.24,2.48 13.47,4.75 16.09,5.83 17.85,4.39 19.61,6.15 '
                '18.17,7.91"/><circle cx="12" cy="12" r="3"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "bag": '<path d="M5 9h14l-1.2 10.2a2 2 0 0 1-2 1.8H8.2a2 2 0 0 1-2-1.8z"/><path d="M9 9V7a3 3 0 0 1 6 0v2"/>',
    "apply": '<path d="M5 12l5 5 9-10"/>',
    "undo": '<path d="M9 7L4 12l5 5"/><path d="M4 12h10a6 6 0 0 1 0 12h-2" transform="translate(0 -6)"/>',
    "point": '<circle cx="12" cy="12" r="4" fill="currentColor"/>',
    "line": '<path d="M4 18c4-10 8 2 16-12"/>',
    "polygon": '<path d="M5 8l7-4 7 5-2 10H7z"/>',
    "table_none": '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 10h16"/>',
    "warning": '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
    "star": '<path d="M12 4l2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6-4.9 2.6.9-5.5-4-3.9 5.5-.8z"/>',
    "panel": '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M14 4v16M16.5 8h2M16.5 11h2M16.5 14h2"/>'
             '<circle cx="8.5" cy="10" r="2"/><path d="M6 16h5"/>',
}

_cache = {}


def _text_color():
    pal = QGuiApplication.palette()
    return pal.color(C.enum_value(QPalette, "ColorRole", "WindowText")).name()


def icon(name, color=None, size=24):
    color = color or _text_color()
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    body = _PATHS.get(name, _PATHS["table_none"]).replace("currentColor", color)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" '
           'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{b}</svg>').format(c=color, b=body)
    ico = QIcon()
    for px in (size, size * 2):
        pm = QPixmap(px, px)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, px, px))
        p.end()
        ico.addPixmap(pm)
    _cache[key] = ico
    return ico


def plugin_icon():
    import os
    path = os.path.join(os.path.dirname(__file__), "icons", "icon.svg")
    return QIcon(path) if os.path.exists(path) else icon("panel", color="#2f74d8", size=32)


def small_button(ico, tip, slot=None, checkable=False, text=None):
    b = QToolButton()
    b.setIcon(ico)
    b.setIconSize(QSize(16, 16))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setCheckable(checkable)
    if text:
        b.setText(text)
        b.setToolButtonStyle(C.ToolButtonTextBesideIcon)
    if slot is not None:
        b.clicked.connect(lambda *_: slot())
    return b


class ElidedLabel(QLabel):
    """Single-line label that elides to its real width (never pushes the layout wider).

    hug=True: the label asks for the width of its whole text (measured with its current, stylesheet-applied
    font) and shrinks, eliding, only when there is not enough room - so a button can sit right after it."""

    def __init__(self, text="", mode=None, parent=None, hug=False):
        super().__init__(parent)
        self._full = text
        self._hug = hug
        self._mode = mode if mode is not None else C.ElideRight
        from qgis.PyQt.QtWidgets import QSizePolicy
        self.setSizePolicy(C.enum_value(QSizePolicy, "Policy", "Preferred" if hug else "Ignored"),
                           C.enum_value(QSizePolicy, "Policy", "Preferred"))
        self.setMinimumWidth(30)
        self.setText(text)

    def setText(self, text):  # noqa: N802
        self._full = text or ""
        self._elide()
        if self._hug:
            self.updateGeometry()

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        if self._hug:
            hint.setWidth(self.fontMetrics().horizontalAdvance(self._full) + 4)
        return hint

    def changeEvent(self, e):  # noqa: N802
        super().changeEvent(e)
        if self._hug and e.type() in (C.enum_value(QEvent, "Type", "FontChange"),
                                      C.enum_value(QEvent, "Type", "StyleChange")):
            self.updateGeometry()
            self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        QLabel.setText(self, self.fontMetrics().elidedText(self._full, self._mode, max(10, self.width())))


def section_label(text):
    lbl = QLabel(text)
    lbl.setObjectName("sectionTitle")
    return lbl

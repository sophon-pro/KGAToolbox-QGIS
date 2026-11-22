# -*- coding: utf-8 -*-
"""Symbol Bag – bag styles (colourways), stylesheet and themed icons."""

import os
from contextlib import suppress

from qgis.PyQt.QtCore import QByteArray, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")

# header top / header bottom / accent / body / card / text / sub / header text
BAG_STYLES = {
    "sky":      dict(label="Sky Blue", h1="#79b4f5", h2="#4f8fe3", accent="#2f74d8", body="#eef5fd",
                     card="#ffffff", text="#1f2d3d", sub="#647487", htext="#ffffff", dark=False),
    "peach":    dict(label="Peach", h1="#f8b09d", h2="#ef8a72", accent="#e2694c", body="#fdf2ee",
                     card="#ffffff", text="#3a2621", sub="#8a6c64", htext="#ffffff", dark=False),
    "mint":     dict(label="Mint", h1="#86d4ae", h2="#56b88c", accent="#2f9a68", body="#eef8f3",
                     card="#ffffff", text="#1c3328", sub="#5e7a6c", htext="#ffffff", dark=False),
    "lavender": dict(label="Lavender", h1="#bea8e6", h2="#9a7fd3", accent="#7b55c7", body="#f4f0fb",
                     card="#ffffff", text="#2a2140", sub="#6f6488", htext="#ffffff", dark=False),
    "sakura":   dict(label="Sakura", h1="#f6b2c8", h2="#ea86a6", accent="#d55a82", body="#fdf1f5",
                     card="#ffffff", text="#3b2230", sub="#8c6676", htext="#ffffff", dark=False),
    "lemon":    dict(label="Lemon", h1="#f8dc7c", h2="#f0c64c", accent="#b98a00", body="#fdf9ea",
                     card="#ffffff", text="#3a3214", sub="#7f7350", htext="#4a3c08", dark=False),
    "kraft":    dict(label="Kraft", h1="#c49a6c", h2="#a87b4f", accent="#8a5f35", body="#f7efe3",
                     card="#fffaf2", text="#3a2a1a", sub="#7d6650", htext="#ffffff", dark=False),
    "white":    dict(label="Classic White", h1="#ffffff", h2="#eef1f5", accent="#4b6584", body="#f6f7f9",
                     card="#ffffff", text="#1f2933", sub="#6b7785", htext="#1f2933", dark=False),
    "midnight": dict(label="Midnight", h1="#34405a", h2="#252e42", accent="#6ea8ff", body="#1b2231",
                     card="#263043", text="#e6ebf2", sub="#9aa6b8", htext="#e6ebf2", dark=True),
}
STYLE_ORDER = ["sky", "peach", "mint", "lavender", "sakura", "lemon", "kraft", "white", "midnight"]
DEFAULT_STYLE = "mint"

# pastel tiles behind previews, per symbol kind (light, dark)
TILES = {
    "marker": ("#e3f0fc", "#2c3d58"),
    "line": ("#fdeee4", "#45372f"),
    "fill": ("#e6f6ec", "#2b4436"),
    "raster": ("#efe9fb", "#383250"),
}

ZIPPER_PULL = "#f5c542"


class Theme(object):
    def __init__(self, style=DEFAULT_STYLE, background=0.86, compact=False):
        self.name = style if style in BAG_STYLES else DEFAULT_STYLE
        self.p = dict(BAG_STYLES[self.name])
        self.background = max(0.2, min(1.0, background))
        self.compact = compact
        self.accent = QColor(self.p["accent"])
        self.p["radius"] = 12
        self._icon_cache = {}

    @property
    def dark(self):
        return self.p["dark"]

    @property
    def key(self):
        return "dark" if self.dark else "light"

    def color(self, key):
        return QColor(self.p[key])

    def accent_soft(self, alpha=40):
        c = QColor(self.accent)
        c.setAlpha(alpha)
        return c

    def tile(self, kind):
        light, dark = TILES.get(kind, ("#eef1f5", "#30384a"))
        return QColor(dark if self.dark else light)

    def body_color(self):
        c = QColor(self.p["body"])
        c.setAlphaF(self.background)
        return c

    # ------------------------------------------------------------------
    def icon(self, name, color=None, size=20):
        """Theme-coloured icon from icons/<name>.svg."""
        color = QColor(color) if color is not None else self.color("sub")
        key = (name, color.name(QColor.NameFormat.HexArgb), size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        path = os.path.join(ICON_DIR, name + ".svg")
        icon = QIcon()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                svg = fh.read().replace("currentColor", color.name())
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            for scale in (1, 2):
                pm = QPixmap(size * scale, size * scale)
                pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                renderer.render(p, QRectF(0, 0, size * scale, size * scale))
                p.end()
                pm.setDevicePixelRatio(scale)
                icon.addPixmap(pm)
        except OSError:
            pass
        self._icon_cache[key] = icon
        return icon

    # ------------------------------------------------------------------
    @staticmethod
    def _chevron_file(color):
        """A small chevron SVG in the accent colour for combo boxes (QSS needs a file)."""
        import tempfile
        folder = os.path.join(tempfile.gettempdir(), "symbol_bag_icons")
        path = os.path.join(folder, "chevron_{}.svg".format(color.lstrip("#")))
        if not os.path.isfile(path):
            with suppress(OSError):
                os.makedirs(folder, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M2.5 4.5 6 8l3.5-3.5" '
                             'fill="none" stroke="{}" stroke-width="1.8" stroke-linecap="round" '
                             'stroke-linejoin="round"/></svg>'.format(color))
        return path.replace(chr(92), "/")

    def stylesheet(self):
        p = self.p
        a = self.accent.name()
        a_soft = "rgba({},{},{},{})".format(self.accent.red(), self.accent.green(), self.accent.blue(), 40)
        card = QColor(p["card"])
        field = "rgba({},{},{},235)".format(card.red(), card.green(), card.blue())
        border = "rgba(0,0,0,30)" if not self.dark else "rgba(255,255,255,30)"
        hbtn = "rgba(255,255,255,235)" if not self.dark else "rgba(255,255,255,40)"
        hbtn_icon_hover = "rgba(255,255,255,255)" if not self.dark else "rgba(255,255,255,70)"
        chevron = self._chevron_file(a)
        return """
QWidget#SBContent, QWidget#SBHeader, QWidget#SBFooter, QStackedWidget, QLabel {{ background: transparent; }}
QLabel#SBTitleLabel {{ font-weight: 700; font-size: 10pt; color: {htext}; }}
QToolButton#SBHeaderBtn {{
    background: {hbtn}; border: none; border-radius: 12px; padding: 3px;
}}
QToolButton#SBHeaderBtn:hover {{ background: {hbtn_hover}; }}
QToolButton#SBHeaderBtn::menu-indicator {{ image: none; width: 0; }}
QToolButton#SBTool {{ background: transparent; border: none; border-radius: 6px; padding: 2px; }}
QToolButton#SBTool:hover {{ background: {a_soft}; }}
QToolButton#SBTool::menu-indicator {{ image: none; width: 0; }}
QToolButton#SBApplyBtn {{ background: transparent; border: none; border-radius: 6px; padding: 2px 8px 2px 4px; color: {a}; font-weight: 700; }}
QToolButton#SBApplyBtn:hover {{ background: {a_soft}; }}
QToolButton#SBApplyBtn:disabled {{ color: {sub}; font-weight: 400; }}
QToolButton#SBChip {{
    border: none; border-radius: 10px; padding: 3px 7px; background: transparent; color: {text};
}}
QToolButton#SBChip:hover {{ background: {a_soft}; }}
QToolButton#SBChip:checked, QToolButton#SBChip:checked:hover {{ background: transparent; color: white; }}
QLineEdit#SBSearch {{
    border: 1px solid {border}; border-radius: 13px; padding: 4px 10px;
    background: {field}; color: {text}; selection-background-color: {a};
}}
QLineEdit#SBSearch:focus {{ border: 1px solid {a}; }}
QComboBox#SBLayerCombo {{
    border: 1px solid {border}; border-radius: 13px; padding: 3px 30px 3px 10px; min-height: 20px;
    background: {field}; color: {text}; selection-background-color: {a_soft}; selection-color: {text};
}}
QComboBox#SBLayerCombo:hover, QComboBox#SBLayerCombo:on {{ border: 1px solid {a}; }}
QComboBox#SBLayerCombo::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right; width: 26px; border: none;
    border-top-right-radius: 13px; border-bottom-right-radius: 13px;
}}
QComboBox#SBLayerCombo::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
QComboBox#SBLayerCombo QAbstractItemView {{
    background: {card}; color: {text}; border: 1px solid {border}; border-radius: 10px; padding: 3px;
    outline: 0; selection-background-color: {a_soft}; selection-color: {text};
}}
QComboBox#SBLayerCombo QAbstractItemView::item {{ min-height: 24px; padding: 2px 8px; border-radius: 7px; }}
QComboBox#SBLayerCombo QAbstractItemView::item:selected {{ background: {a_soft}; color: {text}; }}
QListView#SBView {{ background: transparent; border: none; outline: 0; }}
QListView#SBView > QWidget {{ background: transparent; }}
QLabel#SBSmart {{
    background: {a_soft}; border-radius: 10px; padding: 3px 8px; color: {text};
}}
QLabel#SBSmart a {{ color: {a}; }}
QLabel#SBFooterLabel {{ color: {sub}; font-size: 8pt; }}
QLabel#SBEmpty {{ color: {sub}; font-size: 9.5pt; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
""".format(chevron=chevron, a=a, a_soft=a_soft, field=field, border=border, hbtn=hbtn, hbtn_hover=hbtn_icon_hover,
           border_strong="rgba(0,0,0,60)" if not self.dark else "rgba(255,255,255,60)", **p)


# ----------------------------------------------------------------------
# painting helpers shared by the bag window, the bubble and the style picker
# ----------------------------------------------------------------------
def header_gradient(rect, p):
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0, QColor(p["h1"]))
    g.setColorAt(1, QColor(p["h2"]))
    return g


def paint_zipper(painter, rect, p, dark=False):
    """Stitched zipper band with a pull tab on the right."""
    painter.save()
    tint = QColor(p["h2"])
    tint.setAlpha(55 if not dark else 90)
    painter.fillRect(rect, tint)
    pen = QPen(QColor(p["h2"]).darker(115), 1.2, Qt.PenStyle.CustomDashLine)
    pen.setDashPattern([2.5, 2.5])
    painter.setPen(pen)
    y = rect.center().y()
    painter.drawLine(QPointF(rect.left() + 6, y), QPointF(rect.right() - 30, y))
    # pull tab
    pull = QRectF(rect.right() - 26, rect.top() + 1.5, 15, rect.height() - 3)
    painter.setPen(QPen(QColor(ZIPPER_PULL).darker(130), 1))
    painter.setBrush(QColor(ZIPPER_PULL))
    painter.drawRoundedRect(pull, pull.height() / 2, pull.height() / 2)
    painter.setBrush(QColor("#ffffff"))
    painter.drawEllipse(QPointF(pull.right() + 4.5, pull.center().y()), 2.6, 2.6)
    painter.restore()


def paint_bag_thumbnail(painter, rect, style_key, selected=False):
    """Little bag illustration for the style picker (handle, header, stitched rows)."""
    p = BAG_STYLES[style_key]
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = rect.width(), rect.height()
    bw, bh = w * 0.62, h * 0.70
    bx, by = rect.center().x() - bw / 2, rect.top() + h * 0.24
    # handle
    painter.setPen(QPen(QColor(p["h2"]).darker(108), max(3.0, w * 0.045)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(QRectF(rect.center().x() - bw * 0.22, rect.top() + h * 0.07, bw * 0.44, h * 0.34), 0, 180 * 16)
    body = QRectF(bx, by, bw, bh)
    painter.setPen(QPen(QColor(p["h2"]).lighter(110) if not p["dark"] else QColor(p["h1"]), 1.2))
    painter.setBrush(QColor(p["body"]) if not p["dark"] else QColor(p["h2"]))
    painter.drawRoundedRect(body, 9, 9)
    head = QRectF(bx, by, bw, bh * 0.16)
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    path.addRoundedRect(head, 9, 9)
    path.addRect(QRectF(bx, by + head.height() / 2, bw, head.height() / 2))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(header_gradient(head, p))
    painter.drawPath(path.simplified())
    paint_zipper(painter, QRectF(bx, by + head.height(), bw, bh * 0.08), p, p["dark"])
    # rows
    row_colors = ["#a9d1f5", "#f7b9b0", "#b7e3a1"]
    for i, rc in enumerate(row_colors):
        ry = by + bh * (0.33 + i * 0.21)
        row = QRectF(bx + bw * 0.1, ry, bw * 0.8, bh * 0.15)
        painter.setBrush(QColor(p["card"]) if not p["dark"] else QColor(p["h1"]))
        painter.drawRoundedRect(row, 4, 4)
        painter.setBrush(QColor(rc))
        painter.drawRoundedRect(QRectF(row.left() + 4, row.top() + row.height() * 0.22,
                                       row.height() * 0.56, row.height() * 0.56), 2, 2)
        painter.setBrush(QColor(p["sub"]))
        painter.drawRoundedRect(QRectF(row.left() + row.height() * 0.9, row.center().y() - 1.2,
                                       row.width() * 0.5, 2.4), 1, 1)
    painter.restore()

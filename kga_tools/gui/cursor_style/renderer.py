"""Cursor renderer: turns a CursorStyle into a QPixmap / QCursor.

Precision rules
---------------
* Everything is drawn in *physical* device pixels on a square image of odd
  size N = 2c + 1, so the hotspot is the exact centre pixel (index c).
* Straight lines are crisp filled rectangles whose width is an odd number of
  physical pixels, so they are symmetric around the hotspot pixel.
* The pixmap carries devicePixelRatio = display factor so Qt does not
  resample it.  The hotspot passed to QCursor is chosen per platform so that,
  after Qt's own conversion, it lands exactly on pixel c:
    - Windows / macOS / Wayland: Qt multiplies the (logical) hotspot by the
      screen scale factor  -> we pick logical h and set c = round(h * f).
    - X11 (xcb): Qt uses the hotspot as given against the physical pixmap
      -> we pass c directly.
  (Verified against qtbase 5.15 qwindowscursor.cpp / qxcbcursor.cpp.)
"""

import math
from collections import OrderedDict

from qgis.PyQt.QtCore import Qt, QRect, QRectF, QPointF
from qgis.PyQt.QtGui import (QImage, QPixmap, QPainter, QPen, QColor, QCursor,
                             QPainterPath, QGuiApplication, QFontMetrics)

from .style import (BASE_NATIVE, BASE_SYSTEM_ARROW, BASE_SYSTEM_CROSS,
                    MAX_HALF_EXTENT)

HOTSPOT_LOGICAL = "logical"    # Qt scales hotspot by screen factor
HOTSPOT_PHYSICAL = "physical"  # Qt uses hotspot in pixmap pixels


def hotspot_mode(platform_name=None):
    if platform_name is None:
        platform_name = QGuiApplication.platformName() if QGuiApplication.instance() else ""
    return HOTSPOT_PHYSICAL if platform_name in ("xcb", "offscreen", "minimal") else HOTSPOT_LOGICAL


def ui_scale_factor():
    """QGIS scales its own theme cursors with the UI font (see
    QgsApplication::getThemeCursor). We follow the same idea so cursors grow
    with Windows display scaling when Qt high-DPI scaling is off. Never < 1."""
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    try:
        from qgis.core import Qgis
        ui = float(getattr(Qgis, "UI_SCALE_FACTOR", 1.0))
    except Exception:  # pragma: no cover
        ui = 1.0
    h = QFontMetrics(QGuiApplication.font()).height()
    f = ui * h / 16.0
    return max(1.0, min(4.0, round(f * 4) / 4.0))


def _odd(v):
    v = max(1, int(round(v)))
    return v if v % 2 else v + 1


class Geometry:
    """Physical-pixel geometry of a style at a given display factor."""

    def __init__(self, style, dpr, ui_scale, mode):
        self.dpr = dpr = float(dpr) if dpr and dpr > 0 else 1.0
        k = style.scale / 100.0 * (ui_scale if style.match_ui_scale else 1.0)
        need = self._compute(style, k * dpr)
        max_phys = MAX_HALF_EXTENT * dpr
        self.clamped = False
        if need > max_phys:  # shrink proportionally instead of clipping
            k *= max_phys / need
            need = self._compute(style, k * dpr)
            self.clamped = True
        self.k = k
        if mode == HOTSPOT_PHYSICAL:
            self.c = int(math.ceil(need))
            self.hotspot = self.c
        else:
            h = int(math.ceil(need / dpr))
            c = int(round(h * dpr))
            while c < need:  # guarantee no clipping after rounding
                h += 1
                c = int(round(h * dpr))
            self.c, self.hotspot = c, h
        self.n = 2 * self.c + 1

    def _compute(self, s, p):
        self.p = p
        self.lw = _odd(s.line_width * p)
        self.ow = max(1, int(round(s.outline_width * p))) if s.outline else 0
        self.arm = max(2, int(round(s.arm_length * p)))
        self.gap = int(round(s.gap * p))
        self.circle_r = s.circle_diameter * p / 2.0
        self.inner_r = s.inner_ring_diameter * p / 2.0
        d = s.dot_size * p
        self.dot_crisp = d <= 3.5
        self.dot = _odd(d) if self.dot_crisp else d
        self.dash = max(2, int(round(3 * p)))
        r = 1.0
        if s.crosshair:
            r = max(r, self.arm, self.lw / 2.0 + 0.5)
        if s.circle:
            r = max(r, self.circle_r + self.lw / 2.0)
        if s.inner_ring:
            r = max(r, self.inner_r + self.lw / 2.0)
        if s.dot:
            r = max(r, self.dot / 2.0)
        return r + self.ow + 1.0  # +1: antialiasing margin


class RenderResult:
    __slots__ = ("cursor", "pixmap", "hotspot", "center", "size", "dpr",
                 "clamped", "logical_size")

    def __init__(self, cursor, pixmap=None, hotspot=None, center=None,
                 size=0, dpr=1.0, clamped=False):
        self.cursor, self.pixmap, self.hotspot = cursor, pixmap, hotspot
        self.center, self.size, self.dpr, self.clamped = center, size, dpr, clamped
        self.logical_size = size / dpr if dpr else size


def render_image(style, geo):
    """Draw the style into an ARGB image of geo.n x geo.n physical pixels."""
    n = geo.n
    img = QImage(n, n, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    try:
        if geo.ow:
            _draw_layer(p, style, geo, QColor(style.outline_color),
                        QColor(style.outline_color), geo.ow)
        _draw_layer(p, style, geo, QColor(style.main_color),
                    QColor(style.dot_color), 0)
    finally:
        p.end()
    if style.opacity < 100:
        out = QImage(n, n, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setOpacity(style.opacity / 100.0)
        p.drawImage(0, 0, img)
        p.end()
        img = out
    return img


def _draw_layer(p, s, g, color, dot_color, grow):
    c = g.c
    half = g.lw // 2
    ctr = QPointF(c + 0.5, c + 0.5)

    if s.crosshair:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for a, b in _arm_spans(s, g):
            # horizontal pieces (left/right) and vertical pieces (up/down)
            for x0, x1 in ((c + a, c + b), (c - b, c - a)):
                p.fillRect(QRect(x0 - grow, c - half - grow,
                                 x1 - x0 + 1 + 2 * grow, g.lw + 2 * grow), color)
                p.fillRect(QRect(c - half - grow, x0 - grow,
                                 g.lw + 2 * grow, x1 - x0 + 1 + 2 * grow), color)

    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    for enabled, r in ((s.circle, g.circle_r), (s.inner_ring, g.inner_r)):
        if not enabled:
            continue
        pen = QPen(color, g.lw + 2 * grow)
        if s.dashed:
            pen.setCapStyle(Qt.PenCapStyle.RoundCap if grow else Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(_dashed_circle(ctr, r, g.dash))
        else:
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ctr, r, r)

    if s.dot:
        col = color if grow else dot_color
        if g.dot_crisp:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            d = g.dot + 2 * grow
            p.fillRect(QRect(c - d // 2, c - d // 2, d, d), col)
        else:
            rr = g.dot / 2.0 + grow
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(ctr, rr, rr)


def _arm_spans(s, g):
    """Pixel offsets [a, b] (inclusive, measured from the hotspot pixel) of the
    painted crosshair pieces on one side. Solid arms with no gap are drawn
    through the centre."""
    start = g.gap if g.gap > 0 else 0
    end = g.arm
    if not s.dashed:
        return [(start, end)]
    spans, pos, d = [], max(start, 1), g.dash
    if start == 0:
        spans.append((0, 0))  # always mark the hotspot pixel
    while pos <= end:
        spans.append((pos, min(end, pos + d - 1)))
        pos += 2 * d
    return spans


def _dashed_circle(ctr, r, dash):
    path = QPainterPath()
    circ = 2 * math.pi * r
    segs = max(4, int(circ / (2 * dash)))
    step = 360.0 / segs
    rect = QRectF(ctr.x() - r, ctr.y() - r, 2 * r, 2 * r)
    for i in range(segs):
        start = i * step
        path.arcMoveTo(rect, start)
        path.arcTo(rect, start, step / 2.0)
    return path


def build(style, dpr=1.0, ui_scale=1.0, mode=None):
    """Create a RenderResult. Never raises for a validated style."""
    if style.base == BASE_SYSTEM_ARROW:
        return RenderResult(QCursor(Qt.CursorShape.ArrowCursor))
    if style.base == BASE_SYSTEM_CROSS:
        return RenderResult(QCursor(Qt.CursorShape.CrossCursor))
    if style.base == BASE_NATIVE:
        return RenderResult(None)
    mode = mode or hotspot_mode()
    geo = Geometry(style, dpr, ui_scale, mode)
    img = render_image(style, geo)
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(geo.dpr)
    cur = QCursor(pm, geo.hotspot, geo.hotspot)
    return RenderResult(cur, pm, geo.hotspot, geo.c, geo.n, geo.dpr, geo.clamped)


class CursorCache:
    """Small LRU cache: cursors are regenerated only when the style or the
    display factor changes. Bounded so edited styles never accumulate."""

    def __init__(self, maxsize=12):
        self._d = OrderedDict()
        self.maxsize = maxsize
        self.renders = 0  # number of real renders (for tests / diagnostics)
        self.hits = 0

    def get(self, style, dpr=1.0, ui_scale=None, mode=None):
        if ui_scale is None:
            ui_scale = ui_scale_factor()
        mode = mode or hotspot_mode()
        key = (style.key(), round(float(dpr or 1.0), 4), ui_scale, mode)
        res = self._d.get(key)
        if res is not None:
            self._d.move_to_end(key)
            self.hits += 1
            return res
        res = build(style, dpr, ui_scale, mode)
        self.renders += 1
        self._d[key] = res
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)
        return res

    def clear(self):
        self._d.clear()

    def __len__(self):
        return len(self._d)

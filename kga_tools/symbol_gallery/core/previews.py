# -*- coding: utf-8 -*-
"""Thumbnail rendering for gallery items (symbols and colour ramps)."""
from contextlib import suppress
from qgis.PyQt.QtCore import QSize, QRectF
from qgis.PyQt.QtGui import QImage, QPixmap, QPainter, QColor, QPen, QBrush, QLinearGradient
from qgis.core import (
    QgsSymbolLayerUtils,
    QgsLegendPatchShape,
    QgsGeometry,
    QgsRenderContext,
)

from .compat import SYMBOL_MARKER, SYMBOL_LINE, SYMBOL_FILL, VANCHOR_VCENTER, RC_ANTIALIASING, RC_HQ_IMAGES

BACKGROUNDS = {
    "light": QColor("#ffffff"),
    "paper": QColor("#f4efe6"),
    "dark": QColor("#1b1f27"),
}



# A gentle, smooth wave for lines and a plain swatch rectangle for fills give
# the clean "legend swatch" look of engineering software.
_LINE_WKT = "LineString (0.000 1.000, 0.400 1.104, 0.800 1.202, 1.200 1.288, 1.600 1.355, 2.000 1.399, 2.400 1.419, 2.800 1.413, 3.200 1.380, 3.600 1.324, 4.000 1.247, 4.400 1.155, 4.800 1.053, 5.200 0.947, 5.600 0.845, 6.000 0.753, 6.400 0.676, 6.800 0.620, 7.200 0.587, 7.600 0.581, 8.000 0.601, 8.400 0.645, 8.800 0.712, 9.200 0.798, 9.600 0.896, 10.000 1.000)"
_FILL_WKT = "Polygon ((0 0, 10 0, 10 6.6, 0 6.6, 0 0))"

_shape_cache = {}


def _shape(sym_type):
    if sym_type not in _shape_cache:
        if sym_type == SYMBOL_LINE:
            g = QgsGeometry.fromWkt(_LINE_WKT)
        elif sym_type == SYMBOL_FILL:
            g = QgsGeometry.fromWkt(_FILL_WKT)
        else:
            return None
        # lines keep their proportions (a gentle wave); fills stretch to the swatch
        _shape_cache[sym_type] = QgsLegendPatchShape(sym_type, g, sym_type == SYMBOL_LINE)
    return _shape_cache[sym_type]


def _canvas(size, background):
    pm = QPixmap(size)
    bg = BACKGROUNDS.get(background, BACKGROUNDS["light"])
    pm.fill(bg)
    return pm


def _context(px_per_mm):
    ctx = QgsRenderContext()
    ctx.setScaleFactor(px_per_mm)
    ctx.setFlag(RC_ANTIALIASING, True)
    if RC_HQ_IMAGES is not None:
        ctx.setFlag(RC_HQ_IMAGES, True)
    return ctx


# symbol size unit -> millimetres (Qgis.RenderUnit: mm, map units, pixels,
# percentage, points, inches)
_TO_MM = {0: 1.0, 1: 1.0, 2: 25.4 / 96.0, 3: 1.0, 4: 25.4 / 72.0, 5: 25.4}


def _unit_code(unit):
    try:
        return int(unit)
    except (TypeError, ValueError):
        return int(getattr(unit, "value", 0))


def _size_mm(symbol):
    """Largest marker-layer size in millimetres, whatever unit each layer uses
    (style packs converted from ArcGIS store sizes in points)."""
    best = 0.0
    for i in range(symbol.symbolLayerCount()):
        lyr = symbol.symbolLayer(i)
        if not hasattr(lyr, "size"):
            continue
        with suppress(Exception):
            mm = lyr.size() * _TO_MM.get(_unit_code(lyr.sizeUnit()), 1.0)
            off = lyr.offset()
            mm += 2 * max(abs(off.x()), abs(off.y())) * _TO_MM.get(
                _unit_code(lyr.offsetUnit()), 1.0)
            best = max(best, mm)
    return best or symbol.size()


def _content_extent(pixmap):
    """Largest of width/height of the painted (non-transparent) part, in px."""
    img = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    try:
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes() if hasattr(img, "sizeInBytes") else img.byteCount())
        alpha = bytes(ptr)[3::4]           # ARGB32 is BGRA in memory (little endian)
    except Exception:  # pragma: no cover
        return 0
    bpl = img.bytesPerLine() // 4
    rows = [y for y in range(h) if any(a > 24 for a in alpha[y * bpl:y * bpl + w])]
    if not rows:
        return 0
    cols = [x for x in range(w) if any(alpha[y * bpl + x] > 24 for y in rows)]
    return max(rows[-1] - rows[0] + 1, (cols[-1] - cols[0] + 1) if cols else 0)


def symbol_pixmap(symbol, size, background="light", padding=None, zoom=1.6):
    """Render *symbol* centred on a coloured canvas of *size* (QSize).

    *zoom* magnifies millimetre sizes relative to a 96 dpi screen, so small
    markers, hatches and dash patterns stay legible in thumbnails while all
    symbols keep the same relative scale.
    """
    pm = _canvas(size, background)
    if padding is None:
        padding = max(4, int(min(size.width(), size.height()) * 0.12))
    inner = QSize(size.width() - 2 * padding, size.height() - 2 * padding)
    if symbol.type() == SYMBOL_MARKER:
        # anchored markers (e.g. map pins) are centred for the thumbnail only
        symbol = symbol.clone()
        for i in range(symbol.symbolLayerCount()):
            lyr = symbol.symbolLayer(i)
            if hasattr(lyr, "setVerticalAnchorPoint"):
                lyr.setVerticalAnchorPoint(VANCHOR_VCENTER)
        zoom *= 1.8   # point symbols are only a few mm wide
        # ...but never let a large marker overflow the thumbnail
        with suppress(Exception):  # pragma: no cover
            mm = max(_size_mm(symbol), 0.1)
            fit = min(inner.width(), inner.height()) / (mm * 96 / 25.4 * 1.15)
            zoom = min(zoom, fit)
    ctx = _context(96 / 25.4 * zoom)
    icon = QgsSymbolLayerUtils.symbolPreviewPixmap(
        symbol, inner, 0, ctx, False, None, _shape(symbol.type()))
    if symbol.type() == SYMBOL_MARKER:
        # icons whose drawing fills only part of their frame (common in imported
        # styles) are enlarged so every point thumbnail reads at the same size
        ext = _content_extent(icon)
        side = min(inner.width(), inner.height())
        if 0 < ext < 0.55 * side:
            zoom *= min(0.72 * side / ext, 4.0)
            icon = QgsSymbolLayerUtils.symbolPreviewPixmap(
                symbol, inner, 0, _context(96 / 25.4 * zoom), False, None, None)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.drawPixmap(padding, padding, icon)
    p.end()
    return pm


def ramp_pixmap(color_ramp, size, background="light", padding=None):
    pm = _canvas(size, background)
    if padding is None:
        padding = max(4, int(min(size.width(), size.height()) * 0.12))
    rect = QRectF(padding, size.height() * 0.3, size.width() - 2 * padding,
                  size.height() * 0.4)
    grad = QLinearGradient(rect.topLeft(), rect.topRight())
    steps = 64
    for i in range(steps + 1):
        t = i / float(steps)
        grad.setColorAt(t, color_ramp.color(t))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(0, 0, 0, 60), 1))
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(rect, 4, 4)
    p.end()
    return pm


def entry_pixmap(entry, size, background="light", zoom=1.6):
    obj = entry.build()
    if entry.is_ramp:
        return ramp_pixmap(obj, size, background)
    return symbol_pixmap(obj, size, background, zoom=zoom)


def error_pixmap(size, background="light"):
    """Neutral placeholder for a symbol that failed to render."""
    pm = _canvas(size, background)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(220, 38, 38, 160), 2))
    w, h = size.width(), size.height()
    p.drawLine(int(w * .4), int(h * .35), int(w * .6), int(h * .65))
    p.drawLine(int(w * .6), int(h * .35), int(w * .4), int(h * .65))
    p.end()
    return pm

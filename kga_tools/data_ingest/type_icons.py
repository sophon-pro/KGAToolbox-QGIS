# -*- coding: utf-8 -*-
"""Distinct list icons for Data Explorer, so a file's kind reads at a glance.

Point, Line and Polygon layers, rasters, tables and the multi-layer containers
(File Geodatabase, GeoPackage, KML/KMZ, ...) each get their own glyph and colour
instead of QGIS's one generic vector icon.  The icons are drawn once with QPainter
at 64 px and cached, so they stay crisp at any list size and need no files.
"""

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

from . import inspector

SIZE = 64

POINT = "point"
LINE = "line"
POLYGON = "polygon"

RED = QColor("#d9534f")
BLUE = QColor("#2f6db5")
GREEN = QColor("#3f9d4f")
TEAL = QColor("#17a2b8")
ORANGE = QColor("#f0932b")
PURPLE = QColor("#8e5bb5")
GREY = QColor("#7a838c")
DARK = QColor("#2d3640")

_CACHE = {}

# Containers: extension -> (shape, colour)
_CONTAINERS = {
    ".gdb": ("db", BLUE),
    ".gpkg": ("db", TEAL),
    ".sqlite": ("db", GREY),
    ".mbtiles": ("db", PURPLE),
    ".kml": ("pin", ORANGE),
    ".kmz": ("pin", ORANGE),
    ".zip": ("zip", ORANGE),
    ".xlsx": ("sheet", GREEN),
    ".xls": ("sheet", GREEN),
    ".ods": ("sheet", GREEN),
    ".nc": ("raster", PURPLE),
    ".h5": ("raster", PURPLE),
    ".hdf": ("raster", PURPLE),
}


def geometry_category(geom):
    """'point' / 'line' / 'polygon' from a QGIS geometry type, WKB type or a name like
    'Multi Polygon'; None when unknown or there is no geometry."""
    if geom is None:
        return None
    text = str(getattr(geom, "name", geom)).lower().replace(" ", "")
    for needle, cat in (("point", POINT), ("line", LINE), ("curve", LINE),
                        ("polygon", POLYGON), ("surface", POLYGON)):
        if needle in text:
            return cat
    return None


def details_category(details):
    """Geometry category of a QgsProviderSublayerDetails (None if unknown)."""
    try:
        from qgis.core import QgsWkbTypes
        return geometry_category(QgsWkbTypes.geometryType(details.wkbType()))
    except (AttributeError, TypeError, RuntimeError):
        return None


def path_category(path):
    """Geometry category of a single-layer file whose layers were already read (else None)."""
    cached = inspector.cached_sublayers(path)
    if cached and len(cached) == 1:
        return details_category(cached[0])
    return None


def type_label(kind, category=None, path=""):
    """Short text for the Type column."""
    ext = os.path.splitext(path.rstrip("/\\"))[1].lower() if path else ""
    if kind == inspector.KIND_VECTOR and category:
        return {POINT: "Point", LINE: "Line", POLYGON: "Polygon"}[category]
    if ext in (".kml", ".kmz") and not category:
        return "KML" if ext == ".kml" else "KMZ"
    return inspector.KIND_LABELS.get(kind, "Other").rstrip("s")


# ---------------------------------------------------------------- drawing

def _painter(pix):
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return p


def _pen(color, width=4):
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw_point(p, small=False):
    p.setPen(_pen(DARK, 3))
    p.setBrush(QBrush(RED))
    for x, y, r in ((20, 22, 9), (44, 18, 7), (34, 44, 11)) if not small else ((32, 32, 14),):
        p.drawEllipse(QPointF(x, y), r, r)


def _draw_line(p):
    pts = [QPointF(8, 48), QPointF(24, 20), QPointF(40, 40), QPointF(56, 14)]
    p.setPen(_pen(BLUE, 6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for a, b in zip(pts, pts[1:]):
        p.drawLine(a, b)
    p.setPen(_pen(DARK, 3))
    p.setBrush(QBrush(QColor("white")))
    for pt in pts:
        p.drawEllipse(pt, 5, 5)


def _draw_polygon(p):
    poly = QPolygonF([QPointF(10, 24), QPointF(30, 8), QPointF(56, 18), QPointF(52, 50), QPointF(20, 56)])
    fill = QColor(GREEN)
    fill.setAlpha(190)
    p.setPen(_pen(QColor("#2a6f36"), 4))
    p.setBrush(QBrush(fill))
    p.drawPolygon(poly)


def _draw_generic_vector(p):
    """Geometry not known yet: a small polygon, line and point together."""
    p.save()
    p.scale(0.62, 0.62)
    p.translate(2, 2)
    _draw_polygon(p)
    p.restore()
    p.save()
    p.translate(24, 28)
    p.scale(0.5, 0.5)
    _draw_line(p)
    p.restore()
    p.setPen(_pen(DARK, 3))
    p.setBrush(QBrush(RED))
    p.drawEllipse(QPointF(14, 48), 7, 7)


def _draw_raster(p, color=PURPLE):
    cell = 14
    colors = [color.lighter(170), color.lighter(130), color, color.darker(130)]
    p.setPen(_pen(QColor("white"), 1))
    for r in range(4):
        for c in range(4):
            p.setBrush(QBrush(colors[(r + c * 2) % 4]))
            p.drawRect(QRectF(4 + c * cell, 4 + r * cell, cell, cell))


def _draw_table(p, color=GREY, header=None):
    p.setPen(_pen(color.darker(130), 3))
    p.setBrush(QBrush(QColor("white")))
    p.drawRoundedRect(QRectF(6, 8, 52, 48), 4, 4)
    p.setBrush(QBrush(header or color))
    p.drawRect(QRectF(6, 8, 52, 12))
    p.setPen(_pen(color.darker(130), 2))
    for y in (32, 44):
        p.drawLine(QPointF(6, y), QPointF(58, y))
    p.drawLine(QPointF(24, 20), QPointF(24, 56))
    p.drawLine(QPointF(41, 20), QPointF(41, 56))


def _draw_db(p, color):
    """The classic database cylinder."""
    body = QColor(color)
    p.setPen(_pen(color.darker(150), 3))
    p.setBrush(QBrush(body))
    p.drawRect(QRectF(10, 14, 44, 36))
    p.drawEllipse(QRectF(10, 40, 44, 16))
    p.setBrush(QBrush(body.lighter(140)))
    p.drawEllipse(QRectF(10, 6, 44, 16))
    p.setPen(_pen(color.darker(150), 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(10, 20, 44, 14), 0, -180 * 16)
    p.drawArc(QRectF(10, 31, 44, 14), 0, -180 * 16)


def _draw_pin(p, color):
    """A map pin (KML is Google Earth's placemark format)."""
    path = QPolygonF([QPointF(32, 60), QPointF(14, 32), QPointF(18, 14), QPointF(32, 6),
                      QPointF(46, 14), QPointF(50, 32)])
    p.setPen(_pen(color.darker(150), 3))
    p.setBrush(QBrush(color))
    p.drawPolygon(path)
    p.drawEllipse(QRectF(14, 4, 36, 36))
    p.setBrush(QBrush(QColor("white")))
    p.drawEllipse(QPointF(32, 22), 8, 8)


def _draw_zip(p, color):
    p.setPen(_pen(color.darker(150), 3))
    p.setBrush(QBrush(color.lighter(120)))
    p.drawRoundedRect(QRectF(10, 6, 44, 52), 4, 4)
    p.setBrush(QBrush(QColor("white")))
    for i, y in enumerate(range(8, 42, 8)):
        p.drawRect(QRectF(28 + (i % 2) * 6, y, 6, 6))
    p.setBrush(QBrush(color.darker(150)))
    p.drawRoundedRect(QRectF(24, 42, 16, 12), 3, 3)


def _draw_sheet(p, color):
    _draw_table(p, color, header=color)
    p.setPen(_pen(QColor("white"), 4))
    p.drawLine(QPointF(14, 11), QPointF(20, 18))
    p.drawLine(QPointF(20, 11), QPointF(14, 18))


def _themed(name):
    return QgsApplication.getThemeIcon(name)


def _make(shape, color=None):
    pix = QPixmap(SIZE, SIZE)
    pix.fill(Qt.GlobalColor.transparent)
    p = _painter(pix)
    try:
        if shape == POINT:
            _draw_point(p)
        elif shape == LINE:
            _draw_line(p)
        elif shape == POLYGON:
            _draw_polygon(p)
        elif shape == "vector":
            _draw_generic_vector(p)
        elif shape == "raster":
            _draw_raster(p, color or PURPLE)
        elif shape == "table":
            _draw_table(p)
        elif shape == "db":
            _draw_db(p, color)
        elif shape == "pin":
            _draw_pin(p, color)
        elif shape == "zip":
            _draw_zip(p, color)
        elif shape == "sheet":
            _draw_sheet(p, color)
    finally:
        p.end()
    return QIcon(pix)


def _cached(shape, color=None):
    key = (shape, color.name() if color is not None else "")
    if key not in _CACHE:
        _CACHE[key] = _make(shape, color)
    return _CACHE[key]


def file_icon(kind, path="", category=None, top=False):
    """Icon for a list row. `top` rows are files/folders (containers get their own glyph);
    other rows are layers and are told apart by kind and geometry."""
    ext = os.path.splitext(path.rstrip("/\\"))[1].lower() if path else ""
    if top and ext in _CONTAINERS:
        shape, color = _CONTAINERS[ext]
        return _cached(shape, color)
    if kind == inspector.KIND_VECTOR:
        return _cached(category or "vector")
    if kind == inspector.KIND_RASTER:
        return _cached("raster")
    if kind == inspector.KIND_TABLE:
        return _cached("table")
    if kind == inspector.KIND_POINTCLOUD:
        return _themed("/mIconPointCloudLayer.svg")
    if kind == inspector.KIND_MESH:
        return _themed("/mIconMeshLayer.svg")
    return _themed("/mIconFile.svg")


def star_icon(filled):
    """The favourite-folder star: solid gold when the folder is a favourite, an outline when not."""
    key = ("star", filled)
    if key not in _CACHE:
        import math
        pix = QPixmap(SIZE, SIZE)
        pix.fill(Qt.GlobalColor.transparent)
        p = _painter(pix)
        pts = []
        for i in range(10):
            r = 28 if i % 2 == 0 else 12
            a = -math.pi / 2 + i * math.pi / 5
            pts.append(QPointF(32 + r * math.cos(a), 34 + r * math.sin(a)))
        p.setPen(_pen(QColor("#c98a00") if filled else GREY, 4))
        p.setBrush(QBrush(QColor("#ffc107")) if filled else Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF(pts))
        p.end()
        _CACHE[key] = QIcon(pix)
    return _CACHE[key]

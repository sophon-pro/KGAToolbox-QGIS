# -*- coding: utf-8 -*-
"""Strip lines and point grids over an area: the engine (no GUI).

Strip lines are parallel lines across an area, evenly spaced or following a
repeating pattern of gaps (10, 20, 30, 10, 20, 30 ...). A point grid is points
at a fixed spacing. Both are cut to the area, so a polygon gives lines and
points that stop at its edge.

Distances are metres. Latitude/longitude and Web Mercator are never measured
directly (a "50 m" Web Mercator line is 40 m long at 50 degrees north), so the
area is moved to the UTM zone under its centre first; any other projected CRS
is used as it is, with its units converted. The layers come back in that CRS.

Strips run at a *bearing*: 0 is north-south (vertical on the map), 90 is
east-west. The area is turned so the strips are vertical, the lines are made
there, and everything is turned back, so any angle works the same way.
"""

import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsFeature, QgsFields,
                       QgsGeometry, QgsPointXY, QgsProject,
                       QgsUnitTypes, QgsVectorLayer, QgsWkbTypes)

from .compat import make_field

#: Refuse jobs bigger than this; a layer of millions of points helps nobody.
MAX_FEATURES = 1000000

START_ONE = 'one'           # first line / point one spacing in from the edge
START_HALF = 'half'         # half a spacing in
START_EDGE = 'edge'         # on the edge

START_CHOICES = (
    (START_ONE, 'One spacing in from the edge'),
    (START_HALF, 'Half a spacing in from the edge'),
    (START_EDGE, 'On the edge'),
)

_EPS = 1e-9

#: Holes in the area smaller than this (metres squared) are filled in.
MIN_HOLE_M2 = 5.0


class StripGridError(Exception):
    """A message fit to show the user."""


# ------------------------------------------------------------------ helpers

def parse_spacings(text):
    """'10, 20; 30' -> [10.0, 20.0, 30.0]. Raises StripGridError."""
    pieces = [p for p in
              text.replace(';', ' ').replace(',', ' ').replace('\n', ' ').split()
              if p]
    if not pieces:
        raise StripGridError('Type at least one spacing, e.g. 10, 20, 30.')
    values = []
    for piece in pieces:
        try:
            value = float(piece)
        except ValueError:
            raise StripGridError(
                '"{0}" is not a number. Separate the spacings with commas, '
                'e.g. 10, 20, 30.'.format(piece))
        if not math.isfinite(value) or value <= 0:
            raise StripGridError('Every spacing must be more than zero.')
        values.append(value)
    return values


def utm_crs(lon, lat):
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    return QgsCoordinateReferenceSystem(
        'EPSG:{0}'.format((32600 if lat >= 0 else 32700) + zone))


def working_crs(geometry, crs):
    """The CRS to measure in for an area given in `crs`."""
    if not crs.isValid():
        raise StripGridError('The area has no coordinate system.')
    if crs.isGeographic() or crs.authid() == 'EPSG:3857':
        to_wgs = QgsCoordinateTransform(
            crs, QgsCoordinateReferenceSystem('EPSG:4326'),
            QgsProject.instance().transformContext())
        centre = to_wgs.transform(geometry.boundingBox().center())
        return utm_crs(centre.x(), centre.y())
    return crs


def metres_to_units(crs):
    """How many CRS units are one metre."""
    return QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters,
                                             crs.mapUnits())


class Area(object):
    """The area in the measuring CRS, plus what is needed to read it."""

    def __init__(self, geometry, crs, label=''):
        if geometry is None or geometry.isEmpty():
            raise StripGridError('The area is empty.')
        work = working_crs(geometry, crs)
        shape = QgsGeometry(geometry)
        if work != crs:
            transform = QgsCoordinateTransform(
                crs, work, QgsProject.instance().transformContext())
            if shape.transform(transform) != Qgis.GeometryOperationResult.Success:
                raise StripGridError('The area could not be moved to {0}.'
                                     .format(work.authid()))
        if not shape.isGeosValid():
            shape = shape.makeValid()
        if shape.isEmpty() or shape.area() <= 0:
            raise StripGridError('The area has no extent.')
        self.unit = metres_to_units(work)
        # Hairline holes (slivers left where parcels were merged, spikes that
        # were cleaned badly) are not real holes. Left in, every cell that
        # crosses one is cut around it and draws as a stray line.
        healed = shape.removeInteriorRings(MIN_HOLE_M2 * self.unit * self.unit)
        if healed is not None and not healed.isEmpty():
            shape = healed
        self.geometry = shape
        self.crs = work
        self.label = label
        self.crs_note = '{0} ({1})'.format(work.authid(), work.description())

    def shrunk(self, metres):
        """The same area pulled in by `metres` from its edge.

        Raises StripGridError when nothing is left.
        """
        if not metres or metres <= 0:
            return self
        inner = self.geometry.buffer(-metres * self.unit, 8)
        if inner is None or inner.isEmpty() or inner.area() <= 0:
            raise StripGridError(
                'An offset of {0:g} m from the edge leaves nothing of the '
                'area. Use a smaller offset.'.format(metres))
        other = Area.__new__(Area)
        other.__dict__.update(self.__dict__)
        other.geometry = inner
        return other


def bearing_between(start, end, crs):
    """The bearing (0 is north-south, clockwise, 0 to <180) of a line.

    `start` and `end` are points in `crs`. The direction is read in the CRS the
    strips will be made in, so a line picked on a Web Mercator map is not
    skewed by it. Raises StripGridError for two points in the same place.
    """
    line = QgsGeometry.fromPolylineXY([QgsPointXY(start), QgsPointXY(end)])
    work = working_crs(line, crs)
    if work != crs:
        transform = QgsCoordinateTransform(
            crs, work, QgsProject.instance().transformContext())
        line.transform(transform)
    points = line.asPolyline()
    dx = points[-1].x() - points[0].x()
    dy = points[-1].y() - points[0].y()
    if dx == 0 and dy == 0:
        raise StripGridError('Those two points are in the same place.')
    bearing = math.degrees(math.atan2(dx, dy)) % 180.0
    return 0.0 if bearing >= 179.99995 else bearing


def _frame(area, bearing):
    """The area turned so strips are vertical: (shape, centre)."""
    centre = area.geometry.boundingBox().center()
    turned = QgsGeometry(area.geometry)
    if bearing:
        turned.rotate(-bearing, centre)
    return turned, centre


def _turn_back(geometry, bearing, centre):
    if bearing:
        geometry.rotate(bearing, centre)
    return geometry


def positions(low, high, pattern, start):
    """Where lines fall between `low` and `high`.

    Returns (position, gap) pairs. `pattern` is the gaps in order, repeated.
    The first gap is the start offset.
    """
    first = pattern[0]
    if start == START_EDGE:
        offset = 0.0
    elif start == START_HALF:
        offset = first / 2.0
    else:
        offset = first
    out = []
    here = low + offset
    gap = offset
    index = 0
    # Stay inside by a hair so a line exactly on the far edge is kept once.
    while here <= high + _EPS * max(1.0, abs(high)):
        out.append((here, gap))
        if len(out) > MAX_FEATURES:
            raise StripGridError(
                'That is more than {0:,} lines. Use a bigger spacing or a '
                'smaller area.'.format(MAX_FEATURES))
        gap = pattern[index % len(pattern)]
        index += 1
        here += gap
    return out


# ------------------------------------------------------------------- fields

def _field(name, kind):
    # 2 and 6 are Qt's Int and Double type ids on both Qt5 and Qt6.
    return make_field(name, {'int': 2, 'double': 6}[kind])


def line_fields():
    fields = QgsFields()
    for name, kind in (('id', 'int'), ('strip_no', 'int'),
                       ('gap_m', 'double'), ('length_m', 'double'),
                       ('bearing', 'double')):
        fields.append(_field(name, kind))
    return fields


def point_fields():
    fields = QgsFields()
    for name, kind in (('id', 'int'), ('row_no', 'int'), ('col_no', 'int'),
                       ('x', 'double'), ('y', 'double'),
                       ('lon', 'double'), ('lat', 'double')):
        fields.append(_field(name, kind))
    return fields


# ------------------------------------------------------------------- strips

def count_strips(area, pattern, start, bearing, inset=0.0):
    """How many strips fit (not how many survive clipping)."""
    area = area.shrunk(inset)
    turned, _centre = _frame(area, bearing)
    box = turned.boundingBox()
    u = area.unit
    return len(positions(box.xMinimum(), box.xMaximum(),
                         [p * u for p in pattern], start))


def make_strips(area, pattern, start=START_ONE, bearing=0.0, inset=0.0):
    """A list of (geometry, attributes) for the strip lines, in `area.crs`.

    `pattern` is the gaps in metres, repeated as needed. `inset` keeps the
    lines that far (metres) inside the edge of the area.
    """
    area = area.shrunk(inset)
    u = area.unit
    gaps = [p * u for p in pattern]
    turned, centre = _frame(area, bearing)
    box = turned.boundingBox()
    engine = QgsGeometry.createGeometryEngine(turned.constGet())
    engine.prepareGeometry()
    pad = max(box.height(), 1.0) * 0.01
    out = []
    ident = 0
    for number, (x, gap) in enumerate(
            positions(box.xMinimum(), box.xMaximum(), gaps, start), 1):
        line = QgsGeometry.fromPolylineXY([
            QgsPointXY(x, box.yMinimum() - pad),
            QgsPointXY(x, box.yMaximum() + pad)])
        cut = line.intersection(turned)
        if cut is None or cut.isEmpty():
            continue
        for part in _lines_of(cut):
            ident += 1
            _turn_back(part, bearing, centre)
            out.append((part, [ident, number, gap / u,
                               part.length() / u, float(bearing)]))
            if len(out) > MAX_FEATURES:
                raise StripGridError('That is too many lines.')
    del engine
    return out


def _lines_of(geometry):
    """The line pieces of a clip result (it can hold points or polygons)."""
    if QgsWkbTypes.geometryType(geometry.wkbType()) != \
            QgsWkbTypes.GeometryType.LineGeometry:
        geometry = QgsGeometry.fromWkt(geometry.asWkt())
        pieces = []
        for part in geometry.asGeometryCollection():
            if QgsWkbTypes.geometryType(part.wkbType()) == \
                    QgsWkbTypes.GeometryType.LineGeometry:
                pieces.extend(_lines_of(part))
        return pieces
    if geometry.isMultipart():
        return [QgsGeometry.fromPolylineXY(line)
                for line in geometry.asMultiPolyline() if len(line) > 1]
    line = geometry.asPolyline()
    return [QgsGeometry.fromPolylineXY(line)] if len(line) > 1 else []


# ------------------------------------------------------------------- points

def count_points_estimate(area, across, along, start, bearing, inset=0.0):
    """An upper bound on the points (the bounding box's count)."""
    area = area.shrunk(inset)
    turned, _centre = _frame(area, bearing)
    box = turned.boundingBox()
    u = area.unit
    cols = len(positions(box.xMinimum(), box.xMaximum(), [across * u], start))
    rows = len(positions(box.yMinimum(), box.yMaximum(), [along * u], start))
    return cols * rows


def make_points(area, across, along=None, start=START_ONE, bearing=0.0,
                inset=0.0):
    """A list of (geometry, attributes) for a point grid inside the area.

    `across` is the distance between columns and `along` between rows, both in
    metres (`along` defaults to `across`). Points are numbered from the top
    left, row by row, in the turned frame. `inset` keeps the points that far
    (metres) inside the edge of the area.
    """
    area = area.shrunk(inset)
    along = across if along is None else along
    u = area.unit
    turned, centre = _frame(area, bearing)
    box = turned.boundingBox()
    cols = positions(box.xMinimum(), box.xMaximum(), [across * u], start)
    rows = positions(box.yMinimum(), box.yMaximum(), [along * u], start)
    if len(cols) * len(rows) > MAX_FEATURES * 4:
        raise StripGridError(
            'That is about {0:,} points. Use a bigger spacing or a smaller '
            'area.'.format(len(cols) * len(rows)))
    engine = QgsGeometry.createGeometryEngine(turned.constGet())
    engine.prepareGeometry()
    to_wgs = QgsCoordinateTransform(
        area.crs, QgsCoordinateReferenceSystem('EPSG:4326'),
        QgsProject.instance().transformContext())
    out = []
    ident = 0
    # Top row first, so numbering reads like a page.
    for row_no, (y, _g) in enumerate(reversed(rows), 1):
        for col_no, (x, _g2) in enumerate(cols, 1):
            point = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            if not engine.intersects(point.constGet()):
                continue
            _turn_back(point, bearing, centre)
            p = point.asPoint()
            try:
                ll = to_wgs.transform(p)
                lon, lat = ll.x(), ll.y()
            except Exception:
                lon = lat = None
            ident += 1
            out.append((point, [ident, row_no, col_no, p.x(), p.y(),
                                lon, lat]))
            if len(out) > MAX_FEATURES:
                raise StripGridError(
                    'That is more than {0:,} points. Use a bigger spacing or '
                    'a smaller area.'.format(MAX_FEATURES))
    del engine
    return out


# ------------------------------------------------------------------- shapes

SHAPE_SQUARE = 'square'
SHAPE_RECTANGLE = 'rectangle'
SHAPE_HEXAGON = 'hexagon'
SHAPE_TRIANGLE = 'triangle'
SHAPE_POLYGON = 'polygon'

SHAPE_CHOICES = (
    (SHAPE_SQUARE, 'Square'),
    (SHAPE_RECTANGLE, 'Rectangle'),
    (SHAPE_HEXAGON, 'Hexagon'),
    (SHAPE_TRIANGLE, 'Triangle'),
    (SHAPE_POLYGON, 'Regular polygon (any number of sides)'),
)

KEEP_TOUCH = 'touch'        # every cell that touches the area
KEEP_INSIDE = 'inside'      # only cells wholly inside the area
KEEP_CUT = 'cut'            # every cell, trimmed at the area's edge

KEEP_CHOICES = (
    (KEEP_TOUCH, 'Cells that touch the area'),
    (KEEP_INSIDE, 'Only cells wholly inside the area'),
    (KEEP_CUT, 'Cells cut at the edge of the area'),
)

_SQRT3 = math.sqrt(3.0)


def shape_fields():
    fields = point_fields()
    fields.append(_field('area_m2', 'double'))
    return fields


def _cell_area(shape, width, height, spacing):
    """Area (metres squared) of one cell, for the estimate and the limit."""
    if shape == SHAPE_SQUARE:
        return width * width
    if shape == SHAPE_RECTANGLE:
        return width * height
    if shape == SHAPE_HEXAGON:
        return 1.5 * _SQRT3 * width * width
    if shape == SHAPE_TRIANGLE:
        return _SQRT3 / 4.0 * width * width
    return spacing[0] * spacing[1]


def count_shapes_estimate(area, shape, width, height=None, spacing=None,
                          bearing=0.0, inset=0.0):
    """About how many cells: the bounding box's area over a cell's."""
    area = area.shrunk(inset)
    turned, _centre = _frame(area, bearing)
    box = turned.boundingBox()
    u = area.unit
    height = width if height is None else height
    spacing = spacing or (width, width)
    cell = _cell_area(shape, width, height, spacing) * u * u
    return int(math.ceil(box.width() * box.height() / cell)) + 1


def _ring(points):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(x, y)
                                       for x, y in points + points[:1]]])


def _regular(cx, cy, radius, sides):
    """A regular polygon with a flat bottom (a point at the top for a triangle)."""
    start = -math.pi / 2.0 + math.pi / sides
    return [(cx + radius * math.cos(start + 2.0 * math.pi * k / sides),
             cy + radius * math.sin(start + 2.0 * math.pi * k / sides))
            for k in range(sides)]


def _cells(shape, box, w, h, sides, spacing):
    """Yield (row, col, outline points, centre) in the turned frame.

    Rows count down from the top of the box, columns across from its left.
    Cells start at the top-left corner, so the grid is anchored to the area.
    """
    left, right = box.xMinimum(), box.xMaximum()
    top, bottom = box.yMaximum(), box.yMinimum()
    if shape in (SHAPE_SQUARE, SHAPE_RECTANGLE):
        cw, ch = w, (w if shape == SHAPE_SQUARE else h)
        r = 0
        while top - r * ch > bottom:
            y1 = top - r * ch
            c = 0
            while left + c * cw < right:
                x0 = left + c * cw
                yield (r, c, [(x0, y1 - ch), (x0 + cw, y1 - ch),
                              (x0 + cw, y1), (x0, y1)],
                       (x0 + cw / 2.0, y1 - ch / 2.0))
                c += 1
            r += 1
    elif shape == SHAPE_HEXAGON:
        a = w
        row_h = _SQRT3 * a
        c = -1          # the column left of the box fills the zigzag edge
        while left + c * 1.5 * a < right:
            cx = left + a + c * 1.5 * a
            # Odd columns sit half a cell lower, so they start one row higher
            # to cover the half-cell gap along the top edge.
            odd = c % 2 != 0
            r = -1 if odd else 0
            drop = row_h / 2.0 if odd else 0.0
            while top - drop - r * row_h > bottom:
                cy = top - row_h / 2.0 - drop - r * row_h
                yield r, c, _regular(cx, cy, a, 6), (cx, cy)
                r += 1
            c += 1
    elif shape == SHAPE_TRIANGLE:
        a = w
        th = _SQRT3 / 2.0 * a
        r = 0
        while top - r * th > bottom:
            y1 = top - r * th
            y0 = y1 - th
            c = -1      # starts half a triangle left, to fill the slanted edge
            while left + c * a / 2.0 < right:
                x = left + c * a / 2.0
                if (r + c) % 2 == 0:
                    pts = [(x, y0), (x + a, y0), (x + a / 2.0, y1)]
                else:
                    pts = [(x, y1), (x + a, y1), (x + a / 2.0, y0)]
                yield (r, c, pts,
                       (sum(p[0] for p in pts) / 3.0,
                        sum(p[1] for p in pts) / 3.0))
                c += 1
            r += 1
    else:
        sx, sy = spacing
        radius = w / 2.0
        r = 0
        while top - r * sy > bottom:
            cy = top - sy / 2.0 - r * sy
            c = 0
            while left + c * sx < right:
                cx = left + sx / 2.0 + c * sx
                yield r, c, _regular(cx, cy, radius, sides), (cx, cy)
                c += 1
            r += 1


def make_shapes(area, shape, width, height=None, sides=6, spacing=None,
                bearing=0.0, inset=0.0, keep=KEEP_TOUCH):
    """A grid of regular shapes over the area.

    Returns (cells, centres): lists of (geometry, attributes) for the polygons
    and for their centre points. `width` is the side of a square, hexagon or
    triangle, the width of a rectangle, or the diameter of a regular polygon;
    `spacing` is the (column, row) distance between the centres of regular
    polygons. Everything is in metres.
    """
    if shape == SHAPE_POLYGON and not 3 <= int(sides) <= 64:
        raise StripGridError('A regular polygon needs 3 to 64 sides.')
    area = area.shrunk(inset)
    u = area.unit
    height = width if height is None else height
    spacing = spacing or (width, width)
    turned, centre = _frame(area, bearing)
    box = turned.boundingBox()
    cell_m2 = _cell_area(shape, width, height, spacing)
    if box.width() * box.height() / (u * u) / cell_m2 > MAX_FEATURES:
        raise StripGridError(
            'That is more than {0:,} cells. Use bigger cells or a smaller '
            'area.'.format(MAX_FEATURES))
    engine = QgsGeometry.createGeometryEngine(turned.constGet())
    engine.prepareGeometry()
    to_wgs = QgsCoordinateTransform(
        area.crs, QgsCoordinateReferenceSystem('EPSG:4326'),
        QgsProject.instance().transformContext())
    kept = []
    for row, col, outline, middle in _cells(
            shape, box, width * u, height * u, int(sides),
            (spacing[0] * u, spacing[1] * u)):
        cell = _ring(outline)
        if keep == KEEP_INSIDE:
            if not engine.contains(cell.constGet()):
                continue
        elif not engine.intersects(cell.constGet()):
            continue
        elif keep == KEEP_CUT:
            cell = _polygons_of(cell.intersection(turned))
            if cell is None:
                continue
            # A trimmed cell's own centre can lie outside what is left of
            # it, so the point goes inside the trimmed shape instead.
            middle = _inside_point(cell, middle)
        kept.append((row, col, cell, middle))
    del engine
    # Count rows and columns from 1 over the cells that survived.
    cols = {c: i for i, c in enumerate(sorted({k[1] for k in kept}), 1)}
    if shape == SHAPE_HEXAGON:
        # Neighbouring columns are half a cell apart, so a row is counted
        # down each column on its own.
        rows = {}
        by_col = {}
        for k in kept:
            by_col.setdefault(k[1], []).append(k[0])
        for c, found in by_col.items():
            for i, r in enumerate(sorted(found), 1):
                rows[(r, c)] = i
    else:
        rows = {(r, c): i for i, r in enumerate(
            sorted({k[0] for k in kept}), 1) for c in cols}
    cells, centres = [], []
    ordered = sorted(kept, key=lambda k: (k[0], k[1]))
    for ident, (row, col, cell, middle) in enumerate(ordered, 1):
        square_m = cell.area() / (u * u)
        _turn_back(cell, bearing, centre)
        point = QgsGeometry.fromPointXY(QgsPointXY(*middle))
        _turn_back(point, bearing, centre)
        p = point.asPoint()
        try:
            ll = to_wgs.transform(p)
            lon, lat = ll.x(), ll.y()
        except Exception:
            lon = lat = None
        base = [ident, rows[(row, col)], cols[col], p.x(), p.y(), lon, lat]
        cells.append((cell, base + [square_m]))
        centres.append((point, base))
    return cells, centres


def _inside_point(cell, middle):
    """The cell's centre if it is still inside, else the centroid, else a
    point guaranteed to be on the surface."""
    here = QgsGeometry.fromPointXY(QgsPointXY(*middle))
    if cell.contains(here):
        return middle
    centroid = cell.centroid()
    if centroid is not None and not centroid.isEmpty() \
            and cell.contains(centroid):
        point = centroid.asPoint()
        return (point.x(), point.y())
    point = cell.pointOnSurface().asPoint()
    return (point.x(), point.y())


def _polygons_of(geometry):
    """The polygon part of a clip result as one multipolygon, or None."""
    if geometry is None or geometry.isEmpty():
        return None
    parts = [part for part in geometry.asGeometryCollection()
             if QgsWkbTypes.geometryType(part.wkbType())
             == QgsWkbTypes.GeometryType.PolygonGeometry and part.area() > 0]
    if not parts:
        return None
    merged = QgsGeometry.collectGeometry(parts)
    return merged if merged is not None and not merged.isEmpty() else None


# -------------------------------------------------------------------- layers

def build_layer(name, kind, crs, fields, items):
    """A memory layer holding `items` ((geometry, attributes) pairs)."""
    geom = {'lines': 'LineString',
            'polygons': 'MultiPolygon'}.get(kind, 'Point')
    layer = QgsVectorLayer('{0}?crs={1}'.format(geom, crs.authid()),
                           name, 'memory')
    provider = layer.dataProvider()
    provider.addAttributes(list(fields))
    layer.updateFields()
    features = []
    for geometry, attributes in items:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        feature.setAttributes(attributes)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def layer_name(kind, pattern, across=None, along=None):
    def fmt(v):
        return ('{0:g}'.format(v))
    if kind == 'lines':
        if len(set(pattern)) == 1:
            return 'Strip lines {0} m'.format(fmt(pattern[0]))
        return 'Strip lines {0} m'.format('-'.join(fmt(p) for p in pattern[:4])
                                          + ('...' if len(pattern) > 4 else ''))
    if along is None or along == across:
        return 'Point grid {0} m'.format(fmt(across))
    return 'Point grid {0} x {1} m'.format(fmt(across), fmt(along))


def shape_layer_name(shape, width, height=None, sides=6):
    def fmt(v):
        return '{0:g}'.format(v)
    if shape == SHAPE_SQUARE:
        return 'Square grid {0} m'.format(fmt(width))
    if shape == SHAPE_RECTANGLE:
        return 'Rectangle grid {0} x {1} m'.format(fmt(width), fmt(height))
    if shape == SHAPE_HEXAGON:
        return 'Hexagon grid {0} m'.format(fmt(width))
    if shape == SHAPE_TRIANGLE:
        return 'Triangle grid {0} m'.format(fmt(width))
    return 'Regular {0}-gon grid {1} m'.format(int(sides), fmt(width))

# -*- coding: utf-8 -*-
"""
Seven tools in `algorithms/` share this module - Copy Parallel, Buffer, Split
into COGO Lines, Merge, Divide, Clip and Construct Polygon. Everything here is
deliberately free of Qt widgets so the arithmetic can be read, reasoned
about and tested without a canvas; `gui/modify_base.py` holds the pane
scaffolding and the rubber bands, and the modules in `algorithms/` hold one
pane each.

Three things this module exists to get right, because they are what makes a
QGIS result differ from the ArcGIS one:

* **Units.** ArcGIS offsets and buffers by a real-world distance whatever the
  layer is stored in. `Workspace` does the metric work in a projected CRS -
  the layer's own when it has one, an appropriate UTM zone when the layer is
  in degrees - and puts the geometry back afterwards. Asking for *Map units*
  opts out of that and works in the layer CRS as typed.
* **Curves.** A File Geodatabase happily holds true circular arcs, and
  segmenting them on the way through would quietly lengthen every parcel
  boundary. Arcs survive Copy Parallel and Split into COGO Lines, and their
  radius and arc length come out as COGO values rather than as a chain of
  short chords.
* **Direction conventions.** A COGO direction means nothing without saying
  which way zero points and which way the angle runs, so the four ArcGIS
  direction types and four direction units are all here rather than assumed.
"""

import math

from qgis.core import (
    Qgis,
    QgsCircularString,
    QgsCompoundCurve,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsGeometryUtils,
    QgsLineString,
    QgsMapLayerProxyModel,
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
    QgsWkbTypes,
)


# --------------------------------------------------------------------------- #
#  enum shims
#
#  The buffer/offset style enums moved onto the scoped `Qgis` namespace in
#  3.24 and the old `QgsGeometry.JoinStyleRound` spelling warns from 3.40, so
#  neither can be written on its own in a plugin that declares 3.28.
# --------------------------------------------------------------------------- #

def _scoped(holder_name, name, legacy_prefix, legacy_holder=QgsGeometry):
    holder = getattr(Qgis, holder_name, None)
    if holder is not None:
        value = getattr(holder, name, None)
        if value is not None:
            return value
    return getattr(legacy_holder, legacy_prefix + name)


def join_style(name):
    """`Qgis.JoinStyle.Round` / `.Miter` / `.Bevel`."""
    return _scoped('JoinStyle', name, 'JoinStyle')


def cap_style(name):
    """`Qgis.EndCapStyle.Round` / `.Flat` / `.Square`."""
    return _scoped('EndCapStyle', name, 'EndCap')


def buffer_side(name):
    """`Qgis.BufferSide.Left` / `.Right`."""
    return _scoped('BufferSide', name, 'Side')


def distance_unit(name):
    """`Qgis.DistanceUnit.Meters` etc., falling back to `QgsUnitTypes`."""
    holder = getattr(Qgis, 'DistanceUnit', None)
    if holder is not None:
        value = getattr(holder, name, None)
        if value is not None:
            return value
    return getattr(QgsUnitTypes, 'Distance' + name)


def area_unit(name):
    """`Qgis.AreaUnit.Hectares` etc., falling back to `QgsUnitTypes`."""
    holder = getattr(Qgis, 'AreaUnit', None)
    if holder is not None:
        value = getattr(holder, name, None)
        if value is not None:
            return value
    return getattr(QgsUnitTypes, 'AreaUnit' + name)


def geometry_type(name):
    """`Qgis.GeometryType.Polygon` etc. Same shim as in copy_paste_feature."""
    try:
        return getattr(Qgis.GeometryType, name)
    except AttributeError:                  # pragma: no cover - QGIS < 3.30
        return getattr(QgsWkbTypes, name + 'Geometry')


def layer_filter(*names):
    """`QgsMapLayerComboBox.setFilters` value for 'LineLayer', 'PolygonLayer'...

    Two things have to be got right at once. `QgsMapLayerProxyModel.Filters`
    warns from 3.34 and `Qgis.LayerFilter` does not exist before it, so the
    holder is looked up rather than written. And OR-ing two flags in Python
    gives a plain `int`, which lands on the deprecated integer overload even on
    a build that has the new enum - so the result is put back into the flags
    type before it is handed over.
    """
    holder = getattr(Qgis, 'LayerFilter', None)
    flags = getattr(Qgis, 'LayerFilters', None)
    if holder is None or flags is None:     # pragma: no cover - QGIS < 3.34
        holder, flags = QgsMapLayerProxyModel, QgsMapLayerProxyModel.Filters
    value = 0
    for name in names:
        value |= int(getattr(holder, name))
    return flags(value)


# --------------------------------------------------------------------------- #
#  units
# --------------------------------------------------------------------------- #

#: Offered wherever a distance is typed. `None` means "the layer's own map
#: units", which is also the only choice that skips the reprojection in
#: `Workspace`.
DISTANCE_UNITS = (
    ('Map units', None),
    ('Meters', 'Meters'),
    ('Kilometers', 'Kilometers'),
    ('Feet', 'Feet'),
    ('US survey feet', 'FeetUSSurvey'),
    ('Yards', 'Yards'),
    ('Miles', 'Miles'),
    ('Nautical miles', 'NauticalMiles'),
)

#: Offered wherever an area is typed. Hectares and acres lead because they are
#: what land parcels are actually divided in.
AREA_UNITS = (
    ('Square map units', None),
    ('Hectares', 'Hectares'),
    ('Acres', 'Acres'),
    ('Square meters', 'SquareMeters'),
    ('Square kilometers', 'SquareKilometers'),
    ('Square feet', 'SquareFeet'),
    ('Square yards', 'SquareYards'),
    ('Square miles', 'SquareMiles'),
)


def _unit_or_none(name):
    if name is None:
        return None
    try:
        return distance_unit(name)
    except AttributeError:                  # pragma: no cover - odd build
        return None


def _area_unit_or_none(name):
    if name is None:
        return None
    try:
        return area_unit(name)
    except AttributeError:                  # pragma: no cover
        return None


def utm_crs_for(point, source_crs):
    """The UTM zone `point` falls in, as a CRS.

    Only used to give a layer stored in degrees somewhere sensible to be
    offset, buffered or divided in. One zone is right for anything a single
    editing session covers; none of this is meant for a continent.
    """
    if source_crs is None or not source_crs.isValid():
        return QgsCoordinateReferenceSystem()
    wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
    try:
        if source_crs != wgs84:
            transform = QgsCoordinateTransform(
                source_crs, wgs84, QgsProject.instance())
            point = transform.transform(point)
    except Exception:                       # pragma: no cover - unprojectable
        return QgsCoordinateReferenceSystem()

    # A point outside the lon/lat domain is not a place - it is a hint that
    # arrived in the wrong CRS. Clamping it would quietly pick zone 60 for data
    # in Cambodia and make every distance wrong by the scale factor between
    # them, so refuse instead and let the caller fall back.
    longitude, latitude = point.x(), point.y()
    if not (-180.0 <= longitude <= 180.0) or not (-90.0 <= latitude <= 90.0):
        return QgsCoordinateReferenceSystem()

    zone = min(60, int((longitude + 180.0) / 6.0) + 1)
    code = (32600 if latitude >= 0 else 32700) + zone
    return QgsCoordinateReferenceSystem('EPSG:{}'.format(code))


class Workspace(object):
    """Where a metric operation is carried out, and how to get back.

    Built once per apply. When the layer is already projected - or the user
    asked for map units - this is the identity and costs nothing. When the
    layer is in degrees and a real distance was typed, geometries make a round
    trip through a UTM zone, so that "10 meters" means ten meters rather than
    ten degrees.
    """

    def __init__(self, layer, unit_name=None, extent_hint=None):
        self.layer_crs = layer.crs()
        self.unit_name = unit_name
        self.unit = _unit_or_none(unit_name)

        self.crs = self.layer_crs
        self._to = None
        self._back = None
        #: True when a real-world unit was asked for on a layer stored in
        #: degrees and no projected CRS could be found to honour it. The typed
        #: number is then read as degrees, which is never what was meant, so
        #: the caller is expected to say so rather than write the result.
        self.fell_back = False

        # Map units were asked for: whatever the layer is in *is* the unit,
        # and reprojecting would change what the typed number means.
        if unit_name is None:
            return
        if not self.layer_crs.isValid() or not self.layer_crs.isGeographic():
            return

        centre = extent_hint if extent_hint is not None else layer.extent().center()
        projected = utm_crs_for(centre, self.layer_crs)
        if not projected.isValid():         # pragma: no cover - no zone found
            self.fell_back = True
            return

        self.crs = projected
        project = QgsProject.instance()
        self._to = QgsCoordinateTransform(self.layer_crs, projected, project)
        self._back = QgsCoordinateTransform(projected, self.layer_crs, project)

    # ------------------------------------------------------------ geometry --

    @property
    def reprojects(self):
        return self._to is not None

    def to_work(self, geometry):
        if self._to is None or geometry is None or geometry.isNull():
            return geometry
        clone = QgsGeometry(geometry)
        if clone.transform(self._to) != 0:  # pragma: no cover - out of domain
            raise ValueError('Could not reproject the geometry for the '
                             'calculation.')
        return clone

    def from_work(self, geometry):
        if self._back is None or geometry is None or geometry.isNull():
            return geometry
        clone = QgsGeometry(geometry)
        if clone.transform(self._back) != 0:  # pragma: no cover
            raise ValueError('Could not reproject the result back to the '
                             'layer CRS.')
        return clone

    # --------------------------------------------------------------- units --

    def distance(self, value):
        """`value`, in the unit the user picked, as a number in work CRS units."""
        if self.unit is None:
            return float(value)
        if not self.crs.isValid():          # pragma: no cover - broken CRS
            return float(value)
        try:
            factor = QgsUnitTypes.fromUnitToUnitFactor(
                self.unit, self.crs.mapUnits())
        except Exception:                   # pragma: no cover
            return float(value)
        return float(value) * factor

    def to_distance_unit(self, value, unit_name=None):
        """The inverse of `distance`, for reporting what a result came out at."""
        unit = _unit_or_none(unit_name if unit_name is not None else self.unit_name)
        if unit is None or not self.crs.isValid():
            return float(value)
        try:
            factor = QgsUnitTypes.fromUnitToUnitFactor(self.crs.mapUnits(), unit)
        except Exception:                   # pragma: no cover
            return float(value)
        return float(value) * factor

    def area(self, value, unit_name):
        """`value`, in the given area unit, as a number in squared work units."""
        unit = _area_unit_or_none(unit_name)
        if unit is None or not self.crs.isValid():
            return float(value)
        try:
            target = QgsUnitTypes.distanceToAreaUnit(self.crs.mapUnits())
            factor = QgsUnitTypes.fromUnitToUnitFactor(unit, target)
        except Exception:                   # pragma: no cover
            return float(value)
        return float(value) * factor

    def to_area_unit(self, value, unit_name):
        """The inverse of `area`."""
        unit = _area_unit_or_none(unit_name)
        if unit is None or not self.crs.isValid():
            return float(value)
        try:
            source = QgsUnitTypes.distanceToAreaUnit(self.crs.mapUnits())
            factor = QgsUnitTypes.fromUnitToUnitFactor(source, unit)
        except Exception:                   # pragma: no cover
            return float(value)
        return float(value) * factor


# --------------------------------------------------------------------------- #
#  geometry plumbing shared by more than one tool
# --------------------------------------------------------------------------- #

def curves_of(geometry):
    """Every curve in `geometry`, flattened: rings included, curves preserved.

    A polygon contributes its exterior ring and then each interior ring, which
    is what makes Copy Parallel and Split into COGO Lines work on a parcel
    boundary the way they do in ArcGIS.
    """
    if geometry is None or geometry.isNull():
        return []

    abstract = geometry.constGet()
    curves = []

    def walk(part):
        if part is None:
            return
        # A collection: multi-line, multi-polygon or a mixed collection.
        if hasattr(part, 'numGeometries') and not isinstance(part, QgsCompoundCurve):
            for index in range(part.numGeometries()):
                walk(part.geometryN(index))
            return
        # A polygon, curve polygon included.
        exterior = getattr(part, 'exteriorRing', None)
        if callable(exterior):
            ring = part.exteriorRing()
            if ring is not None:
                curves.append(ring.clone())
            for index in range(part.numInteriorRings()):
                interior = part.interiorRing(index)
                if interior is not None:
                    curves.append(interior.clone())
            return
        # A curve: line string, circular string or compound curve.
        if hasattr(part, 'numPoints'):
            curves.append(part.clone())

    walk(abstract)
    return curves


def is_closed(curve):
    try:
        return bool(curve.isClosed())
    except Exception:                       # pragma: no cover
        return False


def combine(geometries):
    """Collect a list of geometries into one, dropping the empties."""
    parts = [g for g in geometries if g is not None and not g.isNull()
             and not g.isEmpty()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return QgsGeometry.collectGeometry(parts)


def fit_to_layer(geometry, layer):
    """Promote to multipart, or explode, so `geometry` fits `layer`'s WKB type.

    Returns a list: one geometry normally, several when a multipart result has
    to go into a single-part layer, and an empty list when it cannot fit at all.
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []
    if layer is None:
        return [geometry]

    target = layer.wkbType()
    wants_multi = QgsWkbTypes.isMultiType(target)

    if wants_multi and not geometry.isMultipart():
        clone = QgsGeometry(geometry)
        if clone.convertToMultiType():
            geometry = clone
    elif not wants_multi and geometry.isMultipart():
        parts = geometry.asGeometryCollection()
        return [p for p in parts if p is not None and not p.isEmpty()]

    return [geometry]


# --------------------------------------------------------------------------- #
#  Copy Parallel
# --------------------------------------------------------------------------- #

#: The corner treatments ArcGIS offers, in its order. "Patched" is not one of
#: them: GEOS has no patched join, and faking it with a rounded corner would
#: put vertices where a surveyor did not ask for them.
CORNER_STYLES = (
    ('Mitered', 'Miter'),
    ('Beveled', 'Bevel'),
    ('Rounded', 'Round'),
)

#: Which side of the line the copies land on. Left and right are relative to
#: the direction the line was digitized in, exactly as in ArcGIS.
SIDE_LEFT, SIDE_RIGHT, SIDE_BOTH = range(3)

SIDE_LABELS = ('Left', 'Right', 'Both')


def reverse_curve(geometry):
    """`geometry` walked the other way round. Returns it unchanged if it cannot."""
    try:
        reversed_curve = geometry.constGet().reversed()
    except Exception:                       # pragma: no cover - not a curve
        return geometry
    if reversed_curve is None:              # pragma: no cover
        return geometry
    return QgsGeometry(reversed_curve.clone())


def _match_direction(result, source):
    """Turn `result` round if it runs against `source`.

    GEOS hands back a left-hand offset walking the other way. Nothing about the
    shape changes, but the direction a line was digitized in is what "left" and
    "right" mean here, and it is what a COGO description or a one-way arrow
    symbol reads off afterwards - so put it back.
    """
    try:
        start = source.constGet().startPoint()
        first = result.constGet().startPoint()
        last = result.constGet().endPoint()
    except Exception:                       # pragma: no cover
        return result

    to_first = QgsPointXY(start).sqrDist(QgsPointXY(first))
    to_last = QgsPointXY(start).sqrDist(QgsPointXY(last))
    return reverse_curve(result) if to_last < to_first else result


def offset_curve(geometry, distance, segments=8, corner='Miter', miter_limit=5.0):
    """One parallel copy of a line geometry, `distance` to its left when positive.

    Multipart input is offset part by part and collected back up, because
    `QgsGeometry.offsetCurve` gives up on a multi-line string. A part that
    collapses - which is what a tight inside corner does when the offset is
    wider than the bend - is dropped rather than allowed to fold back on
    itself.
    """
    if geometry is None or geometry.isNull():
        return None

    style = join_style(corner)

    def offset_one(part):
        single = QgsGeometry(part)
        try:
            result = single.offsetCurve(float(distance), int(segments),
                                        style, float(miter_limit))
        except Exception:
            return None
        if result is None or result.isNull() or result.isEmpty():
            return None
        return _match_direction(result, single)

    if geometry.isMultipart():
        pieces = [offset_one(part) for part in geometry.asGeometryCollection()]
        return combine([p for p in pieces if p is not None])
    return offset_one(geometry)


def copy_parallel(geometry, distance, side=SIDE_BOTH, copies=1, segments=8,
                  corner='Miter', miter_limit=5.0):
    """Every parallel copy of one feature, as a list of geometries.

    With more than one copy the n-th sits at n x distance, so "3 copies at 5 m"
    lands lines at 5, 10 and 15 m - the same as pressing Copy Parallel three
    times in ArcGIS and typing a bigger number each time.
    """
    results = []
    sides = {SIDE_LEFT: (1.0,), SIDE_RIGHT: (-1.0,),
             SIDE_BOTH: (1.0, -1.0)}[side]
    for step in range(1, max(1, int(copies)) + 1):
        for sign in sides:
            offset = offset_curve(geometry, sign * float(distance) * step,
                                  segments, corner, miter_limit)
            if offset is not None:
                results.append(offset)
    return results


# --------------------------------------------------------------------------- #
#  Buffer
# --------------------------------------------------------------------------- #

CAP_STYLES = (
    ('Round', 'Round'),
    ('Flat', 'Flat'),
    ('Square', 'Square'),
)

BUFFER_FULL, BUFFER_LEFT, BUFFER_RIGHT = range(3)

BUFFER_SIDE_LABELS = ('Both sides', 'Left side only', 'Right side only')


def buffer_geometry(geometry, distance, segments=8, cap='Round', corner='Round',
                    miter_limit=5.0, side=BUFFER_FULL):
    """A buffer polygon around one feature.

    A one-sided buffer only means anything for a line, so it quietly falls back
    to a full buffer for points and polygons rather than returning nothing.
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None

    is_line = geometry.type() == geometry_type('Line')
    try:
        if side != BUFFER_FULL and is_line:
            result = geometry.singleSidedBuffer(
                abs(float(distance)), int(segments),
                buffer_side('Left' if side == BUFFER_LEFT else 'Right'),
                join_style(corner), float(miter_limit))
        else:
            result = geometry.buffer(
                float(distance), int(segments), cap_style(cap),
                join_style(corner), float(miter_limit))
    except Exception:
        return None

    if result is None or result.isNull() or result.isEmpty():
        return None
    return result


def dissolve(geometries):
    """One geometry covering all of `geometries`, merging what overlaps."""
    parts = [g for g in geometries if g is not None and not g.isNull()
             and not g.isEmpty()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    merged = QgsGeometry.unaryUnion(parts)
    if merged is None or merged.isNull() or merged.isEmpty():
        return combine(parts)
    return merged


# --------------------------------------------------------------------------- #
#  Split into COGO Lines
# --------------------------------------------------------------------------- #

#: The COGO field names ArcGIS uses. Matched case-insensitively against the
#: target layer, and created on request when the layer has none of them.
COGO_DIRECTION = 'Direction'
COGO_DISTANCE = 'Distance'
COGO_RADIUS = 'Radius'
COGO_ARCLENGTH = 'ArcLength'

COGO_FIELDS = (COGO_DIRECTION, COGO_DISTANCE, COGO_RADIUS, COGO_ARCLENGTH)

#: Where zero points and which way the angle runs.
DIR_NORTH_AZIMUTH, DIR_SOUTH_AZIMUTH, DIR_POLAR, DIR_QUADRANT = range(4)

DIRECTION_TYPES = (
    'North azimuth',
    'South azimuth',
    'Polar',
    'Quadrant bearing',
)

#: How the angle is written down. The packed form is what ArcGIS stores in a
#: numeric Direction field when the direction unit is degrees-minutes-seconds:
#: 45 degrees 30 minutes 15 seconds is the number 45.3015.
DIR_DECIMAL, DIR_DMS, DIR_GRADIANS, DIR_RADIANS = range(4)

DIRECTION_UNITS = (
    'Decimal degrees',
    'Degrees minutes seconds',
    'Gradians',
    'Radians',
)


def segment_curves(curve, keep_arcs=True):
    """One curve split into the pieces a COGO line each describes.

    A line string gives two-point segments. A circular string gives one arc per
    three points, kept as an arc so its radius and arc length survive. A
    compound curve is walked in order, so a boundary that runs line-arc-line
    comes out as three COGO lines rather than as a fan of chords.
    """
    pieces = []

    def walk(part):
        if isinstance(part, QgsCompoundCurve):
            for index in range(part.nCurves()):
                walk(part.curveAt(index))
            return

        if isinstance(part, QgsCircularString) and keep_arcs:
            points = [part.pointN(i) for i in range(part.numPoints())]
            # A circular string stores start / mid / end, start / mid / end...
            # sharing the end point between consecutive arcs.
            for index in range(0, len(points) - 2, 2):
                arc = QgsCircularString()
                arc.setPoints(points[index:index + 3])
                pieces.append(arc)
            return

        if isinstance(part, QgsCircularString):     # arcs not wanted: chord it
            part = part.curveToLine()

        try:
            points = [part.pointN(i) for i in range(part.numPoints())]
        except Exception:                   # pragma: no cover - not a curve
            return
        for index in range(len(points) - 1):
            first, second = points[index], points[index + 1]
            if QgsPointXY(first) == QgsPointXY(second):
                continue                    # a repeated vertex is not a segment
            line = QgsLineString()
            line.setPoints([first, second])
            pieces.append(line)

    walk(curve)
    return pieces


def azimuth_between(first, second):
    """North azimuth in degrees, 0 at north and running clockwise."""
    dx = second.x() - first.x()
    dy = second.y() - first.y()
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.degrees(math.atan2(dx, dy)) % 360.0


def arc_measures(arc):
    """(signed radius, arc length) for a circular arc.

    The sign follows the ArcGIS convention: a positive radius turns clockwise
    from the start point, a negative one counter-clockwise. Reading a parcel
    description back without that sign gives a mirror image of the curve.
    """
    start = arc.pointN(0)
    middle = arc.pointN(1)
    end = arc.pointN(2)
    try:
        radius, centre_x, centre_y = QgsGeometryUtils.circleCenterRadius(
            start, middle, end)
    except Exception:                       # pragma: no cover
        return (0.0, arc.length())
    if not radius or math.isinf(radius) or math.isnan(radius):
        return (0.0, arc.length())

    # Cross product of start->middle and middle->end: positive turns left.
    cross = ((middle.x() - start.x()) * (end.y() - middle.y())
             - (middle.y() - start.y()) * (end.x() - middle.x()))
    sign = -1.0 if cross > 0 else 1.0
    return (sign * float(radius), float(arc.length()))


def cogo_of(piece):
    """The four COGO values for one segment, in work-CRS units and degrees.

    Direction and distance describe the chord for an arc, which is what a
    surveyor's chord bearing and chord distance mean, with the curve itself
    carried by the radius and arc length beside them.
    """
    start = piece.pointN(0)
    end = piece.pointN(piece.numPoints() - 1)
    direction = azimuth_between(start, end)
    chord = math.hypot(end.x() - start.x(), end.y() - start.y())

    if isinstance(piece, QgsCircularString):
        radius, arc_length = arc_measures(piece)
    else:
        radius, arc_length = (0.0, chord)

    return {
        'direction': direction,             # north azimuth, degrees
        'distance': chord,
        'radius': radius,
        'arclength': arc_length,
    }


def convert_direction(north_azimuth, direction_type):
    """A north azimuth expressed in one of the four ArcGIS direction types.

    Quadrant bearing comes back as `(quadrant, angle)` because it needs two
    letters as well as a number; the other three come back as a plain angle.
    """
    azimuth = float(north_azimuth) % 360.0

    if direction_type == DIR_NORTH_AZIMUTH:
        return azimuth
    if direction_type == DIR_SOUTH_AZIMUTH:
        return (azimuth + 180.0) % 360.0
    if direction_type == DIR_POLAR:
        # Counter-clockwise from east, which is the mathematical convention.
        return (90.0 - azimuth) % 360.0

    # Quadrant bearing: the angle away from the north-south meridian, with the
    # quadrant named by the two letters around it.
    if azimuth <= 90.0:
        return ('NE', azimuth)
    if azimuth <= 180.0:
        return ('SE', 180.0 - azimuth)
    if azimuth <= 270.0:
        return ('SW', azimuth - 180.0)
    return ('NW', 360.0 - azimuth)


def to_dms(angle):
    """(degrees, minutes, seconds) for a positive angle in decimal degrees."""
    angle = abs(float(angle))
    degrees = int(angle)
    rest = (angle - degrees) * 60.0
    minutes = int(rest)
    seconds = (rest - minutes) * 60.0
    # Rounding seconds to two places can carry: 59.999" is a minute, not 60".
    if round(seconds, 2) >= 60.0:
        seconds = 0.0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        degrees += 1
    return (degrees, minutes, seconds)


def pack_dms(angle):
    """DDD.MMSSss, the packed form ArcGIS stores in a numeric COGO field."""
    degrees, minutes, seconds = to_dms(angle)
    return degrees + minutes / 100.0 + seconds / 10000.0


def direction_number(north_azimuth, direction_type, direction_unit):
    """What goes in a numeric Direction field.

    A quadrant bearing has no faithful numeric form - the two letters are part
    of the value - so the angle away from the meridian is stored and the
    quadrant is left to the text form. `direction_text` is what a quadrant
    layer should be writing.
    """
    converted = convert_direction(north_azimuth, direction_type)
    angle = converted[1] if isinstance(converted, tuple) else converted

    if direction_unit == DIR_DECIMAL:
        return round(angle, 8)
    if direction_unit == DIR_DMS:
        return round(pack_dms(angle), 6)
    if direction_unit == DIR_GRADIANS:
        return round(angle * 400.0 / 360.0, 8)
    return round(math.radians(angle), 10)


def direction_text(north_azimuth, direction_type, direction_unit):
    """The written form, the way a deed or a Pro attribute table shows it."""
    converted = convert_direction(north_azimuth, direction_type)
    quadrant = None
    if isinstance(converted, tuple):
        quadrant, angle = converted
    else:
        angle = converted

    if direction_unit == DIR_DMS:
        degrees, minutes, seconds = to_dms(angle)
        body = '{:d}-{:02d}-{:05.2f}'.format(degrees, minutes, seconds)
    elif direction_unit == DIR_GRADIANS:
        body = '{:.4f}g'.format(angle * 400.0 / 360.0)
    elif direction_unit == DIR_RADIANS:
        body = '{:.6f}r'.format(math.radians(angle))
    else:
        body = '{:.4f}'.format(angle)

    if quadrant is None:
        return body
    return '{}{}{}'.format(quadrant[0], body, quadrant[1])


# --------------------------------------------------------------------------- #
#  Merge
# --------------------------------------------------------------------------- #

def merge_geometries(geometries):
    """One geometry from several, the way ArcGIS Merge combines features.

    Parts that touch dissolve into one; parts that do not stay as a multipart
    feature. That is `unaryUnion`, and the fallback keeps the parts rather than
    losing the merge outright when GEOS refuses an invalid input.
    """
    parts = [g for g in geometries if g is not None and not g.isNull()
             and not g.isEmpty()]
    if not parts:
        return None
    if len(parts) == 1:
        return QgsGeometry(parts[0])

    merged = QgsGeometry.unaryUnion(parts)
    if merged is None or merged.isNull() or merged.isEmpty():
        return combine(parts)
    return merged


# --------------------------------------------------------------------------- #
#  Divide
# --------------------------------------------------------------------------- #

#: How the pieces are sized. The same three read differently for a line and a
#: polygon, which is why the labels are per geometry type below.
DIVIDE_EQUAL_PARTS, DIVIDE_MEASURE, DIVIDE_PERCENT = range(3)

LINE_METHODS = (
    'Equal parts',
    'Specified length',
    'Percentage of length',
)

POLYGON_METHODS = (
    'Equal areas',
    'Specified area',
    'Percentage of area',
)

#: Where a piece too short or too small to be a part of its own ends up.
REMAINDER_END, REMAINDER_START, REMAINDER_SPREAD = range(3)

REMAINDER_LABELS = (
    'Leave it as a final part',
    'Leave it as a first part',
    'Spread it across the parts',
)


# ---------------------------------------------------------------- direction --
#
#  A polygon is divided along a direction, and typing that direction as a
#  number is only ever the third-best way to say it. The two better ways -
#  "parallel to this boundary" and "along this line" - both come down to two
#  points on the map, so both end here.

def division_angle(first, second):
    """The direction from `first` to `second`, as `divide_polygon` reads one.

    Degrees counter-clockwise from east, folded into 0-180: a direction and its
    reverse describe the same family of parallel cuts, and the direction an
    edge comes out at depends on which way the ring it belongs to happens to
    have been digitized - which is not something the user can see, and not
    something they would expect to change the answer.

    None for two points in the same place, which describe no direction at all.
    """
    dx = second.x() - first.x()
    dy = second.y() - first.y()
    if dx == 0.0 and dy == 0.0:
        return None
    return math.degrees(math.atan2(dy, dx)) % 180.0


def nearest_segment(geometry, point):
    """The segment of `geometry` nearest `point`, as `(start, end)`.

    Both in `geometry`'s own CRS, so the caller has to have brought `point`
    into it first. None when there is no segment to be had: an empty geometry,
    a point, or a pair of vertices in the same place.

    An arc answers with the chord between two of its three defining points
    rather than a tangent, which is the honest reading - a curved boundary has
    no one direction, and the nearest half of it is the best available answer
    to "parallel to this edge".
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    try:
        result = geometry.closestSegmentWithContext(QgsPointXY(point))
    except Exception:                       # pragma: no cover - defensive
        return None
    if not result or len(result) < 3:       # pragma: no cover - old build
        return None

    # The third value is the vertex *after* the closest segment, so the segment
    # itself is the pair ending there. Index 0 would mean the point came before
    # the first vertex, which is not a segment.
    after = int(result[2])
    if after < 1:
        return None
    start = geometry.vertexAt(after - 1)
    end = geometry.vertexAt(after)
    if start.isEmpty() or end.isEmpty():    # pragma: no cover - bad index
        return None
    start, end = QgsPointXY(start), QgsPointXY(end)
    if start == end:                        # a repeated vertex is not a segment
        return None
    return start, end


def _cut_positions(total, method, count, measure, percent, remainder):
    """Cumulative cut positions along a total, shared by lines and polygons.

    Returns the running totals at which to cut - not including 0 or `total` -
    so both dividers can stay about geometry and leave the arithmetic here.
    """
    total = float(total)
    if total <= 0:
        return []

    if method == DIVIDE_EQUAL_PARTS:
        count = max(1, int(count))
        return [total * index / count for index in range(1, count)]

    if method == DIVIDE_PERCENT:
        percent = float(percent)
        if percent <= 0:
            return []
        step = total * percent / 100.0
    else:
        step = float(measure)
        if step <= 0:
            return []

    if step >= total:
        return []

    whole = int(math.floor(total / step + 1e-9))
    leftover = total - whole * step
    # A leftover under a millimetre is floating-point noise, not a part.
    if leftover < 1e-6:
        whole = max(1, whole)
        return [step * index for index in range(1, whole)]

    if remainder == REMAINDER_SPREAD:
        parts = whole
        return [total * index / parts for index in range(1, parts)]

    cuts = [step * index for index in range(1, whole + 1)]
    if remainder == REMAINDER_START:
        # Push everything along so the short piece comes first.
        cuts = [leftover] + [leftover + step * index
                             for index in range(1, whole)]
    return cuts


def divide_line(geometry, method=DIVIDE_EQUAL_PARTS, count=2, measure=0.0,
                percent=0.0, remainder=REMAINDER_END):
    """One line feature cut into parts, in order along the line.

    Multipart input is treated as one run: the parts are measured end to end,
    which is how ArcGIS divides a multipart line, so a cut can fall inside any
    of them.
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []

    curves = curves_of(geometry)
    if not curves:
        return []

    lengths = [curve.length() for curve in curves]
    total = sum(lengths)
    cuts = _cut_positions(total, method, count, measure, percent, remainder)
    if not cuts:
        return [QgsGeometry(geometry)]

    bounds = [0.0] + cuts + [total]
    parts = []
    for index in range(len(bounds) - 1):
        piece = _line_between(curves, lengths, bounds[index], bounds[index + 1])
        if piece is not None:
            parts.append(piece)
    return parts


def _line_between(curves, lengths, start, end):
    """The stretch of a run of curves between two distances along it."""
    pieces = []
    offset = 0.0
    for curve, length in zip(curves, lengths):
        first = max(start, offset)
        last = min(end, offset + length)
        if last - first > 1e-9:
            try:
                piece = curve.curveSubstring(first - offset, last - offset)
            except Exception:               # pragma: no cover - old build
                piece = None
            if piece is not None and piece.numPoints() > 1:
                pieces.append(QgsGeometry(piece.clone()))
        offset += length
    return combine(pieces)


def divide_polygon(geometry, method=DIVIDE_EQUAL_PARTS, count=2, measure=0.0,
                   percent=0.0, angle=0.0, remainder=REMAINDER_END,
                   tolerance=1e-7, iterations=60):
    """One polygon cut into parallel strips of the asked-for area.

    ArcGIS cuts along a direction; so does this. The polygon is rotated so that
    direction runs up the page, the cut positions are found by bisection - the
    area to the left of a vertical line only ever grows as the line moves right,
    so bisection always converges - and the strips are rotated back.

    Bisection rather than a closed form because a real parcel is not convex:
    a polygon with a notch or a hole has no formula for "the line that leaves
    exactly one hectare behind it".
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []

    total_area = geometry.area()
    if total_area <= 0:
        return []

    cuts = _cut_positions(total_area, method, count, measure, percent, remainder)
    if not cuts:
        return [QgsGeometry(geometry)]

    # Rotate so the division lines stand vertical. `rotate` turns clockwise,
    # and a direction at `angle` counter-clockwise from east has to land at 90.
    centre = geometry.centroid().asPoint()
    working = QgsGeometry(geometry)
    spin = float(angle) - 90.0
    if spin:
        working.rotate(spin, centre)

    box = working.boundingBox()
    left, right = box.xMinimum(), box.xMaximum()
    if right - left <= 0:                   # pragma: no cover - degenerate
        return [QgsGeometry(geometry)]

    pad = max(box.width(), box.height()) * 0.5 + 1.0
    low, high = box.yMinimum() - pad, box.yMaximum() + pad

    def strip(x_start, x_end):
        rect = QgsGeometry.fromPolygonXY([[
            QgsPointXY(x_start, low), QgsPointXY(x_end, low),
            QgsPointXY(x_end, high), QgsPointXY(x_start, high),
            QgsPointXY(x_start, low),
        ]])
        try:
            return working.intersection(rect)
        except Exception:                   # pragma: no cover
            return None

    def area_left_of(x):
        piece = strip(left - 1.0, x)
        return piece.area() if piece is not None and not piece.isNull() else 0.0

    edges = [left]
    for target in cuts:
        low_x, high_x = edges[-1], right
        for _ in range(iterations):
            middle = (low_x + high_x) / 2.0
            if area_left_of(middle) < target:
                low_x = middle
            else:
                high_x = middle
            if high_x - low_x < tolerance * max(1.0, abs(right - left)):
                break
        edges.append((low_x + high_x) / 2.0)
    edges.append(right)

    parts = []
    for index in range(len(edges) - 1):
        # Widen the outermost strips so nothing is lost to the bounding box.
        start = edges[index] - (1.0 if index == 0 else 0.0)
        end = edges[index + 1] + (1.0 if index == len(edges) - 2 else 0.0)
        piece = strip(start, end)
        if piece is None or piece.isNull() or piece.isEmpty():
            continue
        if spin:
            piece.rotate(-spin, centre)
        parts.append(piece)
    return parts


# --------------------------------------------------------------------------- #
#  Clip
# --------------------------------------------------------------------------- #

CLIP_DISCARD, CLIP_PRESERVE = range(2)

CLIP_LABELS = (
    'Discard the area that intersects',
    'Preserve the area that intersects',
)


def clip_geometry(geometry, clip, mode=CLIP_DISCARD):
    """What is left of `geometry` after clipping it with `clip`.

    `None` means nothing is left, and the caller should delete the feature -
    which is what ArcGIS does when a clip swallows a feature whole.
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    if clip is None or clip.isNull() or clip.isEmpty():
        return QgsGeometry(geometry)

    try:
        if mode == CLIP_PRESERVE:
            result = geometry.intersection(clip)
        else:
            result = geometry.difference(clip)
    except Exception:
        return QgsGeometry(geometry)

    if result is None or result.isNull() or result.isEmpty():
        return None

    # An intersection can drop to a lower dimension - two polygons that only
    # touch share a line, not an area. That is not a clipped feature.
    if result.type() != geometry.type():
        parts = [part for part in result.asGeometryCollection()
                 if part.type() == geometry.type()]
        result = combine(parts)
        if result is None or result.isEmpty():
            return None
    return result


# --------------------------------------------------------------------------- #
#  Construct Polygon
# --------------------------------------------------------------------------- #

def explode_lines(geometries):
    """Every line in `geometries` as a single-part geometry of its own.

    Polygonizing does not care which feature a course came from, and a
    multipart line whose parts sit on different sides of a parcel is one
    feature holding several courses. Splitting first means the ends can be
    counted, and snapped, one course at a time.
    """
    lines = []
    for geometry in geometries:
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            continue
        parts = (geometry.asGeometryCollection() if geometry.isMultipart()
                 else [QgsGeometry(geometry)])
        for part in parts:
            if part is None or part.isNull() or part.isEmpty():
                continue
            if part.type() != geometry_type('Line'):
                continue
            lines.append(part)
    return lines


def line_ends(geometry):
    """The two ends of a single-part line, or `[]` when it is already a ring.

    A closed line meets itself, so it has nothing loose about it and is left
    out of the count in `loose_ends`.
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []
    curve = geometry.constGet()
    if curve is None or not hasattr(curve, 'startPoint'):
        return []
    if is_closed(curve):
        return []
    try:
        return [QgsPointXY(curve.startPoint()), QgsPointXY(curve.endPoint())]
    except Exception:                       # pragma: no cover - not a curve
        return []


def loose_ends(lines, tolerance=0.0):
    """The ends of a line network that meet nothing else.

    Both ends of every course are counted, and an end only one course reaches
    is where the boundary is open - which is the whole difference between a
    set of lines that makes a parcel and a set that makes nothing.

    Run this on *noded* lines (`node_lines`). A course that stops halfway
    along another one is a perfectly good junction, and only noding turns that
    meeting into a shared end that this can see.
    """
    nodes = []                              # [[point, times reached]]
    for line in lines:
        for end in line_ends(line):
            for node in nodes:
                if node[0].distance(end) <= tolerance:
                    node[1] += 1
                    break
            else:
                nodes.append([end, 1])

    candidates = [point for point, reached in nodes if reached < 2]
    if not candidates:
        return []

    # Noding splits an open course wherever something meets it, so every
    # meeting is an end meeting an end - except on a closed ring, which is
    # never split at its own start point. A spur running off a ring corner
    # would otherwise be reported as loose at a corner that is not open at
    # all.
    rings = [line for line in lines if line_ends(line) == []]
    if not rings:
        return candidates
    return [point for point in candidates
            if not any(ring.distance(QgsGeometry.fromPointXY(point)) <= tolerance
                       for ring in rings)]


def snap_line_ends(lines, tolerance):
    """Pull course ends within `tolerance` of one another onto a single point.

    A boundary digitized course by course, or brought in from a survey, is
    routinely a millimetre short of closing - and a millimetre is as open as a
    metre to GEOS. The tolerance closes those gaps by moving the *ends*, which
    is the one edit that cannot change the shape of a course between them.
    """
    if tolerance <= 0:
        return list(lines)

    clusters = []                           # [[representative point, ...]]

    def anchor(point):
        for cluster in clusters:
            if cluster[0].distance(point) <= tolerance:
                return cluster[0]
        clusters.append([point])
        return point

    snapped = []
    for line in lines:
        ends = line_ends(line)
        if not ends:
            snapped.append(line)            # a ring: nothing to pull together
            continue
        curve = line.constGet()
        try:
            last = curve.numPoints() - 1
        except Exception:                   # pragma: no cover - not a curve
            snapped.append(line)
            continue
        clone = QgsGeometry(line)
        for index, end in ((0, ends[0]), (last, ends[1])):
            target = anchor(end)
            if target.distance(end) > 0:
                clone.moveVertex(target.x(), target.y(), index)
        snapped.append(clone)
    return snapped


def node_lines(lines):
    """`lines` split at every crossing, as a list of single-part lines.

    Two courses that cross without a vertex where they meet enclose nothing
    until they are noded: GEOS polygonizes what is handed to it and does not
    invent the intersection. `unaryUnion` is what puts the node in.
    """
    parts = [line for line in lines
             if line is not None and not line.isNull() and not line.isEmpty()]
    if not parts:
        return []
    noded = QgsGeometry.unaryUnion(parts)
    if noded is None or noded.isNull() or noded.isEmpty():
        return list(parts)                  # GEOS refused; use what we have
    return explode_lines([noded])


def construct_polygons(geometries, tolerance=0.0):
    """The polygons `geometries` enclose, and the ends that close nothing.

    Answers `(polygons, loose)`. An empty `polygons` with points in `loose`
    is the case the caller has to report: the courses do not make a boundary,
    and those are the places where they stop.

    Circular arcs come back segmented. Polygonizing is GEOS work and GEOS has
    no arcs, so a curved boundary is built from the chords it was drawn with -
    unlike Copy Parallel and Split into COGO Lines, which keep them.
    """
    lines = snap_line_ends(explode_lines(geometries), tolerance)
    noded = node_lines(lines)
    if not noded:
        return [], []

    loose = loose_ends(noded, tolerance)
    try:
        built = QgsGeometry.polygonize(noded)
    except Exception:                       # pragma: no cover - defensive
        return [], loose
    if built is None or built.isNull() or built.isEmpty():
        return [], loose

    polygons = [part for part in built.asGeometryCollection()
                if part is not None and not part.isNull() and not part.isEmpty()]
    return polygons, loose

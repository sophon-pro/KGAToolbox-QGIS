"""
COGO helpers of the Modify Features pane (the Traverse, Update COGO and Cul-de-sac tools live in
traverse.py, update_cogo.py and culdesac.py).

COGO = coordinate geometry: a line is described by a direction and a distance (and a radius for a curve),
stored in the Direction, Distance, Radius and ArcLength fields. The conventions (direction types and units,
field matching, curve sign) are the ones Split into COGO Lines already uses (core/modify_features.py).

    Traverse     the engine: courses (direction, distance, optional radius) become lines carrying COGO values
    Update COGO  the engine: rewrite the COGO fields of lines from their geometry
"""

import math
import re

from qgis.core import (
    Qgis,
    QgsCircularString,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsSettings,
)
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QWidget,
)

from ...core import compat
from ...core.modify_features import (
    COGO_ARCLENGTH,
    COGO_DIRECTION,
    COGO_DISTANCE,
    COGO_FIELDS,
    COGO_RADIUS,
    DIR_DECIMAL,
    DIR_DMS,
    DIR_GRADIANS,
    DIR_NORTH_AZIMUTH,
    DIR_POLAR,
    DIR_QUADRANT,
    DIR_SOUTH_AZIMUTH,
    DIRECTION_TYPES,
    DIRECTION_UNITS,
    DISTANCE_UNITS,
    Workspace,
    cogo_of,
    curves_of,
    direction_number,
    direction_text,
    find_cogo_field,
    fit_to_layer,
    segment_curves,
)
from ...gui.modify_base import make_feature
from ..selection import _transform, all_vector_layers

LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
SETTINGS = "kga_editor/modify_cogo_"


# ---------------------------------------------------------------------------------------- directions
def parse_angle(text, unit):
    """An angle typed in the given unit (decimal degrees, D-M-S, gradians, radians) as decimal degrees."""
    text = text.strip()
    if not text:
        raise ValueError("the direction is empty")
    try:
        if unit == DIR_DMS:
            parts = [p for p in re.split(r"[-\s°'\"]+", text) if p]
            if len(parts) == 1 and "." in parts[0]:                  # packed DDD.MMSSss, as stored in a number
                degrees, _dot, rest = parts[0].partition(".")
                rest = rest.ljust(4, "0")
                parts = [degrees, rest[:2], rest[2:4] + ("." + rest[4:] if rest[4:] else "")]
            numbers = [float(p) for p in parts[:3]] + [0.0] * (3 - len(parts[:3]))
            degrees, minutes, seconds = numbers
            if not (0 <= minutes < 60 and 0 <= seconds < 60):
                raise ValueError("minutes and seconds must be under 60")
            return degrees + minutes / 60.0 + seconds / 3600.0
        value = float(text.lower().rstrip("gr"))
    except ValueError as exc:
        raise ValueError(str(exc) if "must be" in str(exc) else f"'{text}' is not an angle") from None
    if unit == DIR_GRADIANS:
        return value * 360.0 / 400.0
    if unit == DIR_DECIMAL:
        return value
    return math.degrees(value)                                       # radians


def parse_direction(text, direction_type, direction_unit):
    """A typed direction as a north azimuth in degrees (0 = north, clockwise)."""
    if direction_type == DIR_QUADRANT:
        m = re.match(r"^\s*([NSns])\s*(.+?)\s*([EWew])\s*$", text)
        if m is None:
            raise ValueError(f"'{text}' is not a quadrant bearing such as N45-30-00E")
        angle = parse_angle(m.group(2), direction_unit)
        if not 0 <= angle <= 90:
            raise ValueError("a quadrant bearing is between 0 and 90 degrees")
        ns, ew = m.group(1).upper(), m.group(3).upper()
        if ns == "N":
            return angle if ew == "E" else (360.0 - angle) % 360.0
        return 180.0 - angle if ew == "E" else 180.0 + angle
    angle = parse_angle(text, direction_unit)
    if direction_type == DIR_NORTH_AZIMUTH:
        return angle % 360.0
    if direction_type == DIR_SOUTH_AZIMUTH:
        return (angle - 180.0) % 360.0
    if direction_type == DIR_POLAR:
        return (90.0 - angle) % 360.0
    raise ValueError("unknown direction type")


# ---------------------------------------------------------------------------------------- courses
def course_piece(start, azimuth, distance, radius=0.0):
    """
    One course from `start`: (piece, end point). A straight course is a two-point line; with a radius the
    course is an arc whose chord is `distance` long (a positive radius turns clockwise, as in ArcGIS).
    """
    if distance <= 0:
        raise ValueError("a distance must be more than zero")
    rad = math.radians(azimuth)
    d = (math.sin(rad), math.cos(rad))
    end = QgsPointXY(start.x() + d[0] * distance, start.y() + d[1] * distance)
    if not radius:
        return QgsLineString([QgsPoint(start), QgsPoint(end)]), end
    r, half = abs(radius), distance / 2.0
    if r < half - 1e-9:
        raise ValueError(f"a radius of {r:g} is too small for a chord of {distance:g}")
    sag = r - math.sqrt(max(r * r - half * half, 0.0))
    left = (-d[1], d[0])
    side = 1.0 if radius > 0 else -1.0                               # clockwise arcs bulge to the left of the chord
    mid = QgsPointXY((start.x() + end.x()) / 2 + left[0] * sag * side, (start.y() + end.y()) / 2 + left[1] * sag * side)
    return QgsCircularString(QgsPoint(start), QgsPoint(mid), QgsPoint(end)), end


def linear(piece):
    """The piece as a plain polyline geometry (an arc becomes short straight segments)."""
    if isinstance(piece, QgsCircularString):
        return QgsGeometry(piece.curveToLine())
    return QgsGeometry(piece.clone())


# ---------------------------------------------------------------------------------------- settings
class CogoSettings:
    """How COGO values are read and written: direction convention, direction unit, distance unit."""

    def __init__(self, direction_type=DIR_NORTH_AZIMUTH, direction_unit=DIR_DMS, distance_unit=None,
                 create_fields=True):
        self.direction_type = direction_type
        self.direction_unit = direction_unit
        self.distance_unit = distance_unit          # a name from DISTANCE_UNITS, None = the layer's map units
        self.create_fields = create_fields

    @classmethod
    def load(cls):
        s = QgsSettings()
        unit = s.value(SETTINGS + "distance_unit", "")
        return cls(int(s.value(SETTINGS + "direction_type", DIR_NORTH_AZIMUTH)),
                   int(s.value(SETTINGS + "direction_unit", DIR_DMS)), unit or None,
                   str(s.value(SETTINGS + "create_fields", "true")).lower() != "false")

    def save(self):
        s = QgsSettings()
        s.setValue(SETTINGS + "direction_type", self.direction_type)
        s.setValue(SETTINGS + "direction_unit", self.direction_unit)
        s.setValue(SETTINGS + "distance_unit", self.distance_unit or "")
        s.setValue(SETTINGS + "create_fields", self.create_fields)


class SettingsForm(QWidget):
    """The shared direction type / unit / distance unit choosers."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.type_box = QComboBox(self)
        self.type_box.addItems(DIRECTION_TYPES)
        self.type_box.setCurrentIndex(settings.direction_type)
        self.unit_box = QComboBox(self)
        self.unit_box.addItems(DIRECTION_UNITS)
        self.unit_box.setCurrentIndex(settings.direction_unit)
        self.distance_box = QComboBox(self)
        for label, name in DISTANCE_UNITS:
            self.distance_box.addItem(label, name)
        self.distance_box.setCurrentIndex(max(0, self.distance_box.findData(settings.distance_unit)))
        self.create_box = QCheckBox("Create the COGO fields if they are missing", self)
        self.create_box.setChecked(settings.create_fields)
        form.addRow("Direction type", self.type_box)
        form.addRow("Direction units", self.unit_box)
        form.addRow("Distance units", self.distance_box)
        form.addRow(self.create_box)

    def settings(self):
        return CogoSettings(self.type_box.currentIndex(), self.unit_box.currentIndex(),
                            self.distance_box.currentData(), self.create_box.isChecked())


# ---------------------------------------------------------------------------------------- COGO fields
def ensure_cogo_fields(layer, create):
    """({COGO name: field index (-1 = missing)}, names added). New fields go through the edit buffer."""
    added = []
    if create:
        missing = [n for n in COGO_FIELDS if find_cogo_field(layer.fields(), n) < 0]
        if missing:
            fields = [compat.make_field(n, compat.T_DOUBLE, 0, 8) for n in missing]
            try:
                if layer.isEditable():
                    if all(layer.addAttribute(f) for f in fields):
                        added = list(missing)
                elif layer.dataProvider().addAttributes(fields):
                    layer.updateFields()
                    added = list(missing)
            except Exception:               # a provider that will not take a field: the values just are not written
                added = []
    return {n: find_cogo_field(layer.fields(), n) for n in COGO_FIELDS}, added


def cogo_attributes(layer, values, settings, indices):
    """{field index: value} for the COGO fields the layer has; text fields get the written form."""
    out = {}
    index = indices.get(COGO_DIRECTION, -1)
    if index >= 0:
        if layer.fields().at(index).type() == compat.T_STRING:
            out[index] = direction_text(values["direction"], settings.direction_type, settings.direction_unit)
        else:
            out[index] = direction_number(values["direction"], settings.direction_type, settings.direction_unit)
    for name, key in ((COGO_DISTANCE, "distance"), (COGO_RADIUS, "radius"), (COGO_ARCLENGTH, "arclength")):
        index = indices.get(name, -1)
        if index >= 0:
            number = values[key]
            out[index] = "{:.4f}".format(number) if layer.fields().at(index).type() == compat.T_STRING else round(number, 6)
    return out


def _unit_values(workspace, values):
    out = dict(values)
    for key in ("distance", "radius", "arclength"):
        out[key] = workspace.to_distance_unit(values[key])
    return out


# ---------------------------------------------------------------------------------------- traverse
def traverse_shapes(canvas, layer, start, courses, settings, per_course=True):
    """
    The lines of a traverse, not yet written: [(geometry in the layer's CRS, COGO values or None)]. start: a point in
    the map's CRS. courses: [(north azimuth, distance, radius or 0)] with distances in settings.distance_unit. One
    shape per course carrying its COGO values, or one connected line.
    """
    canvas_crs = canvas.mapSettings().destinationCrs()
    point = QgsGeometry.fromPointXY(QgsPointXY(start))
    if layer.crs() != canvas_crs:
        point.transform(_transform(canvas_crs, layer.crs()))
    workspace = Workspace(layer, settings.distance_unit, extent_hint=point.asPoint())
    if workspace.fell_back:
        raise ValueError("the layer is in degrees and no projected CRS fits it - pick 'Map units' or reproject")
    current = workspace.to_work(point).asPoint()
    pieces = []
    for azimuth, distance, radius in courses:
        piece, current = course_piece(current, azimuth, workspace.distance(distance), workspace.distance(radius))
        pieces.append(piece)
    if per_course:
        return [(workspace.from_work(linear(p)), _unit_values(workspace, cogo_of(p))) for p in pieces]
    vertices = []
    for p in pieces:
        points = linear(p).asPolyline()
        vertices.extend(points if not vertices else points[1:])
    return [(workspace.from_work(QgsGeometry.fromPolylineXY(vertices)), None)]


def traverse(canvas, layer, start, courses, settings, per_course=True, created=None):
    """
    Add the lines of a traverse to `layer` (editable): one feature per course carrying its COGO values, or one
    connected line. `created`, if given, collects (layer, feature id) of the new lines. Returns (features added, COGO
    field names added).
    """
    shapes = traverse_shapes(canvas, layer, start, courses, settings, per_course)
    indices, added = ensure_cogo_fields(layer, settings.create_fields) if per_course else ({}, [])
    layer.beginEditCommand("Traverse")
    count = 0
    try:
        for shape, values in shapes:
            attrs = cogo_attributes(layer, values, settings, indices) if values is not None else {}
            for part in fit_to_layer(shape, layer):
                new = make_feature(layer, part, attrs)
                if layer.addFeature(new):
                    count += 1
                    if created is not None:
                        created.append((layer, new.id()))
    except Exception:
        layer.destroyEditCommand()
        raise
    layer.endEditCommand()
    layer.triggerRepaint()
    return count, added


def editable_line_layers():
    return [layer for layer in all_vector_layers() if layer.isEditable() and layer.geometryType() == LINE]


# ---------------------------------------------------------------------------------------- update COGO
def update_cogo_values(layer, ids, settings):
    """
    Rewrite the COGO fields of the given line features from their geometry. A one-segment line gives its
    direction and distance (an arc also its radius and arc length); a longer line gives the chord from its first
    to its last point and its full length. Returns (features updated, names of fields added, fields missing).
    """
    indices, added = ensure_cogo_fields(layer, settings.create_fields)
    missing = [n for n in COGO_FIELDS if indices[n] < 0]
    if all(indices[n] < 0 for n in COGO_FIELDS):
        return 0, added, missing
    workspace = Workspace(layer, settings.distance_unit)
    if workspace.fell_back:
        raise ValueError("the layer is in degrees and no projected CRS fits it - pick 'Map units' or reproject")
    layer.beginEditCommand("Update COGO")
    updated, ok = 0, True
    for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
        geometry = f.geometry()
        if geometry.isEmpty():
            continue
        curves = curves_of(workspace.to_work(geometry))
        if len(curves) != 1:
            continue                                    # a multi-part line has no single direction
        pieces = segment_curves(curves[0], True)
        if not pieces:
            continue
        if len(pieces) == 1:
            values = cogo_of(pieces[0])
        else:
            first, last = curves[0].pointN(0), curves[0].pointN(curves[0].numPoints() - 1)
            values = cogo_of(QgsLineString([first, last]))
            values["arclength"] = curves[0].length()
        attrs = cogo_attributes(layer, _unit_values(workspace, values), settings, indices)
        for index, value in attrs.items():
            ok = layer.changeAttributeValue(f.id(), index, value) and ok
        updated += 1
    if ok:
        layer.endEditCommand()
    else:
        layer.destroyEditCommand()
        updated = 0
    layer.triggerRepaint()
    return updated, added, missing


TOOL_CLASSES = {}
COMMAND_PAGES = {}

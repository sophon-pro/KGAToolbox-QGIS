"""Geometry construction and validation using native QGIS geometry classes.

Vertices are tuples ``(x, y, z, m, fid)``. Geometries are built directly from
coordinate arrays (no WKT round trips) so Z and M values are preserved.
"""

from qgis.core import QgsGeometry, QgsLineString, QgsPolygon, QgsWkbTypes

from .compat import GEOMTYPE_POLYGON, geos_validation_engine

STATUS_VALID = "valid"
STATUS_REPAIRED = "repaired"
STATUS_INVALID = "invalid"


def _arrays(vertices, has_z, has_m):
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices] if has_z else []
    ms = [v[3] for v in vertices] if has_m else []
    return xs, ys, zs, ms


def make_linestring(vertices, has_z, has_m):
    xs, ys, zs, ms = _arrays(vertices, has_z, has_m)
    return QgsLineString(xs, ys, zs, ms)


def build_line(vertices, has_z, has_m):
    return QgsGeometry(make_linestring(vertices, has_z, has_m))


def build_segment(a, b, has_z, has_m):
    """Two-vertex line; a dedicated fast path for individual-segment mode."""
    return QgsGeometry(QgsLineString(
        [a[0], b[0]], [a[1], b[1]],
        [a[2], b[2]] if has_z else [],
        [a[3], b[3]] if has_m else []))


def build_polygon(open_ring_vertices, has_z, has_m):
    """Build a polygon from an *open* ring; it is closed here (last -> first)."""
    ring_vertices = list(open_ring_vertices) + [open_ring_vertices[0]]
    ring = make_linestring(ring_vertices, has_z, has_m)
    poly = QgsPolygon()
    poly.setExteriorRing(ring)
    return QgsGeometry(poly)


def _geos_errors(geom):
    try:
        errors = geom.validateGeometry(geos_validation_engine())
    except TypeError:  # pragma: no cover - very old signature
        errors = geom.validateGeometry()
    msgs = []
    for e in errors:
        text = e.what()
        if e.hasWhere():
            text += " at ({:.6g}, {:.6g})".format(e.where().x(), e.where().y())
        msgs.append(text)
    return "; ".join(msgs) if msgs else "invalid geometry"


def _polygonal_only(geom):
    """Keep only polygonal parts of a (possibly mixed) repair result."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    if geom.type() == GEOMTYPE_POLYGON:
        return geom
    if QgsWkbTypes.isMultiType(geom.wkbType()) or "Collection" in QgsWkbTypes.displayString(
        geom.wkbType()
    ):
        extracted = QgsGeometry(geom)
        # converts in place; drops non-polygonal children (e.g. dangling lines)
        if (extracted.convertGeometryCollectionToSubclass(GEOMTYPE_POLYGON)
                and not extracted.isNull() and not extracted.isEmpty()):
            return extracted
    return None


def check_polygon(geom, validate, repair):
    """Return (status, geometry_or_None, message).

    Note: a self-intersecting ring (e.g. a bow-tie) can have a signed area of
    exactly 0, so area alone is never used to reject a polygon before repair.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return STATUS_INVALID, None, "empty or null geometry"
    if not validate:
        return STATUS_VALID, geom, ""
    if geom.isGeosValid() and geom.area() > 0:
        return STATUS_VALID, geom, ""
    reason = _geos_errors(geom)
    fixed = _polygonal_only(geom.makeValid())
    degenerate = fixed is None or fixed.area() == 0
    if degenerate:
        return STATUS_INVALID, None, (
            "zero area: vertices are collinear or coincident ({}); cannot be "
            "repaired".format(reason))
    if not repair:
        return STATUS_INVALID, None, reason + " (repair disabled)"
    if not fixed.isGeosValid():
        return STATUS_INVALID, None, reason + " (could not be repaired)"
    # Single-part results are stored as plain polygons
    if fixed.isMultipart() and fixed.constGet().numGeometries() == 1:
        fixed = QgsGeometry(fixed.constGet().geometryN(0).clone())
    parts = fixed.constGet().numGeometries() if fixed.isMultipart() else 1
    note = "repaired ({}) -> {} part(s)".format(reason, parts)
    if QgsWkbTypes.hasM(geom.wkbType()) and not QgsWkbTypes.hasM(fixed.wkbType()):
        note += "; M values could not be kept by the repair"
    return STATUS_REPAIRED, fixed, note


def check_line(geom, validate):
    if geom is None or geom.isNull() or geom.isEmpty():
        return STATUS_INVALID, None, "empty or null geometry"
    if not validate:
        return STATUS_VALID, geom, ""
    if geom.length() == 0:
        return STATUS_INVALID, None, "zero length (all vertices share the same X/Y)"
    return STATUS_VALID, geom, ""


def polygon_boundary(geom):
    """Boundary of a (multi)polygon as a (multi)linestring geometry."""
    boundary = geom.constGet().boundary()
    return QgsGeometry(boundary) if boundary is not None else None

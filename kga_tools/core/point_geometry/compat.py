"""Compatibility helpers so the converter runs on QGIS 3.28+ (Qt5) and QGIS 4.x (Qt6).

Only small, well-defined shims live here. Everything else in the plugin uses
these helpers rather than version-specific enums directly.
"""

from qgis.core import Qgis, QgsWkbTypes

from ..compat import T_INT, T_LONGLONG, T_STRING
from ..compat import make_field as _make_field

_KINDS = {"int": T_INT, "long": T_LONGLONG, "string": T_STRING}


def make_field(name, kind):
    """Create a QgsField of kind 'int', 'long' or 'string'."""
    return _make_field(name, _KINDS[kind])


# --- WKB types -----------------------------------------------------------
def _wkb(name):
    try:
        return getattr(Qgis.WkbType, name)
    except AttributeError:  # QGIS < 3.30
        return getattr(QgsWkbTypes, name)


WKB_LINESTRING = _wkb("LineString")
WKB_MULTILINESTRING = _wkb("MultiLineString")
WKB_POLYGON = _wkb("Polygon")
WKB_MULTIPOLYGON = _wkb("MultiPolygon")
WKB_NOGEOMETRY = _wkb("NoGeometry")


def _geom_type(name):
    try:
        return getattr(Qgis.GeometryType, name)
    except AttributeError:  # QGIS < 3.30
        return getattr(QgsWkbTypes, name + "Geometry")


GEOMTYPE_POINT = _geom_type("Point")
GEOMTYPE_POLYGON = _geom_type("Polygon")
GEOMTYPE_LINE = _geom_type("Line")


def output_wkb_type(base, has_z, has_m):
    """Add Z/M dimensions to a base WKB type."""
    t = base
    if has_z:
        t = QgsWkbTypes.addZ(t)
    if has_m:
        t = QgsWkbTypes.addM(t)
    return t


def wkb_display_string(wkb_type):
    return QgsWkbTypes.displayString(wkb_type)


def is_point_layer(layer):
    try:
        return layer is not None and layer.geometryType() == GEOMTYPE_POINT
    except Exception:  # pragma: no cover
        return False


def geos_validation_engine():
    try:
        return Qgis.GeometryValidationEngine.Geos
    except AttributeError:
        from qgis.core import QgsGeometry

        return QgsGeometry.ValidatorGeos


def message_level(name):
    try:
        return getattr(Qgis.MessageLevel, name)
    except AttributeError:
        return getattr(Qgis, name)


def task_can_cancel_flag():
    from qgis.core import QgsTask

    try:
        return QgsTask.Flag.CanCancel
    except AttributeError:
        return QgsTask.CanCancel


def sink_fast_insert_flag():
    from qgis.core import QgsFeatureSink

    try:
        return QgsFeatureSink.Flag.FastInsert
    except AttributeError:
        return QgsFeatureSink.FastInsert

# -*- coding: utf-8 -*-
"""Small helpers that keep the plugin working on QGIS 3 (Qt5) and QGIS 4 (Qt6)."""

from qgis.core import Qgis, QgsWkbTypes
from qgis.PyQt.QtCore import Qt


PLUGIN_NAME = "Layer Bag"
SETTINGS_KEY = "LayerMiniPanel/"


def enum_value(owner, scope, name, default=None):
    """Return owner.scope.name (scoped enum, Qt6/new QGIS) or owner.name (old style)."""
    scoped = getattr(owner, scope, None) if scope else None
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(owner, name, default)


def as_int(value):
    """Integer value of an enum member, whatever binding generation produced it."""
    try:
        return int(value)
    except TypeError:
        return int(getattr(value, "value", 0))


# ---------------------------------------------------------------- geometry
GEOM_POINT, GEOM_LINE, GEOM_POLYGON, GEOM_NONE, GEOM_UNKNOWN = "point", "line", "polygon", "none", "unknown"


def geometry_family(layer):
    """'point' | 'line' | 'polygon' | 'none' | 'unknown' for a vector layer."""
    try:
        gt = layer.geometryType()
    except Exception:
        return GEOM_UNKNOWN
    pairs = (
        (GEOM_POINT, enum_value(Qgis, "GeometryType", "Point") if hasattr(Qgis, "GeometryType") else None,
         getattr(QgsWkbTypes, "PointGeometry", None)),
        (GEOM_LINE, enum_value(Qgis, "GeometryType", "Line") if hasattr(Qgis, "GeometryType") else None,
         getattr(QgsWkbTypes, "LineGeometry", None)),
        (GEOM_POLYGON, enum_value(Qgis, "GeometryType", "Polygon") if hasattr(Qgis, "GeometryType") else None,
         getattr(QgsWkbTypes, "PolygonGeometry", None)),
        (GEOM_NONE, enum_value(Qgis, "GeometryType", "Null") if hasattr(Qgis, "GeometryType") else None,
         getattr(QgsWkbTypes, "NullGeometry", None)),
    )
    for fam, a, b in pairs:
        if (a is not None and gt == a) or (b is not None and gt == b):
            return fam
    return GEOM_UNKNOWN


def wkb_label(layer):
    """Human WKB type, e.g. 'MultiPolygon' or 'PointZ'."""
    try:
        return QgsWkbTypes.displayString(layer.wkbType()) or "Unknown"
    except Exception:
        return "Unknown"


def is_multipart(layer):
    try:
        return QgsWkbTypes.isMultiType(layer.wkbType())
    except Exception:
        return False


# ---------------------------------------------------------------- Qt enum shortcuts
AlignLeft = enum_value(Qt, "AlignmentFlag", "AlignLeft")
AlignCenter = enum_value(Qt, "AlignmentFlag", "AlignCenter")
AlignVCenter = enum_value(Qt, "AlignmentFlag", "AlignVCenter")
DisplayRole = enum_value(Qt, "ItemDataRole", "DisplayRole")
ToolTipRole = enum_value(Qt, "ItemDataRole", "ToolTipRole")
UserRole = enum_value(Qt, "ItemDataRole", "UserRole")
DecorationRole = enum_value(Qt, "ItemDataRole", "DecorationRole")
ForegroundRole = enum_value(Qt, "ItemDataRole", "ForegroundRole")
FontRole = enum_value(Qt, "ItemDataRole", "FontRole")
Horizontal = enum_value(Qt, "Orientation", "Horizontal")
CaseInsensitive = enum_value(Qt, "CaseSensitivity", "CaseInsensitive")
ElideMiddle = enum_value(Qt, "TextElideMode", "ElideMiddle")
ElideRight = enum_value(Qt, "TextElideMode", "ElideRight")
RightDockWidgetArea = enum_value(Qt, "DockWidgetArea", "RightDockWidgetArea")
LeftDockWidgetArea = enum_value(Qt, "DockWidgetArea", "LeftDockWidgetArea")
CustomContextMenu = enum_value(Qt, "ContextMenuPolicy", "CustomContextMenu")
ItemIsEnabled = enum_value(Qt, "ItemFlag", "ItemIsEnabled")
ItemIsSelectable = enum_value(Qt, "ItemFlag", "ItemIsSelectable")
ToolButtonTextBesideIcon = enum_value(Qt, "ToolButtonStyle", "ToolButtonTextBesideIcon")
ToolButtonIconOnly = enum_value(Qt, "ToolButtonStyle", "ToolButtonIconOnly")
ScrollBarAlwaysOff = enum_value(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff")

MSG_INFO = enum_value(Qgis, "MessageLevel", "Info")
MSG_WARNING = enum_value(Qgis, "MessageLevel", "Warning")
MSG_CRITICAL = enum_value(Qgis, "MessageLevel", "Critical")
MSG_SUCCESS = enum_value(Qgis, "MessageLevel", "Success")


def no_geometry_flag():
    from qgis.core import QgsFeatureRequest
    return enum_value(QgsFeatureRequest, "Flag", "NoGeometry")


def qt_exec(obj):
    """dialog.exec() on Qt6, exec_() on old Qt5 bindings."""
    return obj.exec() if hasattr(obj, "exec") else getattr(obj, "exec_")()

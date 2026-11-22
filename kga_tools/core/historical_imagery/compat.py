# -*- coding: utf-8 -*-
"""Small helpers that keep the plugin working on QGIS 3.22+ (Qt5) and QGIS 4 (Qt6)."""

from qgis.core import Qgis, QgsTask, QgsWkbTypes

try:  # Qt6 moved QAction to QtGui
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401


def enum(owner, enum_name, member):
    """Return ``owner.enum_name.member`` (scoped, Qt6/QGIS 4) or ``owner.member`` (legacy)."""
    scoped = getattr(owner, enum_name, None)
    if scoped is not None and hasattr(scoped, member):
        return getattr(scoped, member)
    return getattr(owner, member)


def polygon_geometry_type():
    if hasattr(Qgis, "GeometryType") and hasattr(Qgis.GeometryType, "Polygon"):
        return Qgis.GeometryType.Polygon  # QGIS >= 3.30
    return QgsWkbTypes.PolygonGeometry


TASK_CAN_CANCEL = enum(QgsTask, "Flag", "CanCancel")
MSG_INFO = enum(Qgis, "MessageLevel", "Info")
MSG_WARNING = enum(Qgis, "MessageLevel", "Warning")
MSG_CRITICAL = enum(Qgis, "MessageLevel", "Critical")
MSG_SUCCESS = enum(Qgis, "MessageLevel", "Success")

LOG_TAG = "Satellite Imagery Archive"


def log(message, level=None):
    from qgis.core import QgsMessageLog
    QgsMessageLog.logMessage(str(message), LOG_TAG, MSG_INFO if level is None else level)

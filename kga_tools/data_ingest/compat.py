"""Qt5 / Qt6 (QGIS 3.x / 4.x) compatibility helpers.

All enums elsewhere in the plugin are written in their fully-qualified
form (e.g. Qt.ItemDataRole.UserRole) which works on both PyQt5 and PyQt6.
Only classes that moved between modules are handled here.
"""

try:  # Qt6: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

try:  # Qt6: QFileSystemModel lives in QtGui
    from qgis.PyQt.QtGui import QFileSystemModel  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QFileSystemModel  # noqa: F401

try:  # Qt6: QShortcut lives in QtGui
    from qgis.PyQt.QtGui import QShortcut  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QShortcut  # noqa: F401


# --------------------------------------------------------------------------
# Enum helpers. QGIS 3.30-3.36 moved many enums into the Qgis namespace and
# QGIS 4 removes several old aliases, so look up the new name first.
# --------------------------------------------------------------------------
def _resolve(candidates):
    import qgis.core as core
    for path in candidates:
        obj = core
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    raise AttributeError("none of %s exist in this QGIS version" % (candidates,))


def request_flag(name):
    return _resolve(("Qgis.FeatureRequestFlag." + name, "QgsFeatureRequest.Flag." + name,
                     "QgsFeatureRequest." + name))


def select_behavior(name):
    return _resolve(("Qgis.SelectBehavior." + name, "QgsVectorLayer.SelectBehavior." + name,
                     "QgsVectorLayer." + name))


def geometry_type(name):
    old = {"Polygon": "PolygonGeometry", "Line": "LineGeometry", "Point": "PointGeometry"}
    return _resolve(("Qgis.GeometryType." + name, "QgsWkbTypes.GeometryType." + old.get(name, name),
                     "QgsWkbTypes." + old.get(name, name)))


def layer_type(name):
    old = {"Vector": "VectorLayer", "Raster": "RasterLayer"}
    return _resolve(("Qgis.LayerType." + name, "QgsMapLayerType." + old.get(name, name),
                     "QgsMapLayer." + old.get(name, name)))

def ignore(_exc):
    """Deliberately swallow an expected, harmless failure (best-effort calls)."""
    return None


try:  # Qt6: QActionGroup lives in QtGui
    from qgis.PyQt.QtGui import QActionGroup  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QActionGroup  # noqa: F401


def identify_mode(name):
    from qgis.gui import QgsMapToolIdentify
    for path in ("IdentifyMode." + name, name):
        obj = QgsMapToolIdentify
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    try:
        from qgis.core import Qgis
        return getattr(Qgis.IdentifyMode, name)
    except AttributeError:
        raise AttributeError("Identify mode %s not found" % name)


def enum_int(value):
    """Integer value of a sip/Python enum (Qt5 and Qt6)."""
    try:
        return int(getattr(value, "value", value))
    except (TypeError, ValueError):
        return value


def writer_action(name):
    return _resolve(("QgsVectorFileWriter.ActionOnExistingFile." + name, "QgsVectorFileWriter." + name))

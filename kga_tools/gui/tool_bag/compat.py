"""Small helpers that keep the plugin working on both Qt5 (QGIS 3.x) and Qt6 (QGIS 4.x).

All enums elsewhere in the plugin are written in their fully scoped form
(e.g. ``Qt.Orientation.Horizontal``), which PyQt5 >= 5.11 and PyQt6 both accept.
"""

from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint

try:  # Qt5: QAction lives in QtWidgets
    from qgis.PyQt.QtWidgets import QAction, QActionGroup  # noqa: F401
except ImportError:  # Qt6: QAction moved to QtGui
    from qgis.PyQt.QtGui import QAction, QActionGroup  # noqa: F401

from qgis.PyQt.QtWidgets import QWidgetAction  # noqa: F401  (QtWidgets in both)

OWN_PREFIX = "ToolBag"
PLUGIN_NAME = "Tool Bag"


def qexec(obj, *args):
    """Call exec() / exec_() whichever the binding provides."""
    fn = getattr(obj, "exec", None) or getattr(obj, "exec_")
    return fn(*args)


def event_pos(event) -> QPoint:
    """Local position of a mouse/drop/context event as a QPoint (Qt5 + Qt6)."""
    if hasattr(event, "position"):
        with suppress(Exception):  # pragma: no cover
            return event.position().toPoint()
    return event.pos()


def event_global_pos(event) -> QPoint:
    if hasattr(event, "globalPosition"):
        with suppress(Exception):  # pragma: no cover
            return event.globalPosition().toPoint()
    return event.globalPos()


def alive(obj) -> bool:
    """True if the wrapped C++ object still exists."""
    if obj is None:
        return False
    try:
        return not sip.isdeleted(obj)
    except TypeError:
        return True


def ptr(obj) -> int:
    """Stable integer key for a live QObject (its C++ address)."""
    return int(sip.unwrapinstance(obj))


def is_own_object(obj) -> bool:
    """True if obj (or one of its parents) belongs to this plugin."""
    depth = 0
    while obj is not None and depth < 40:
        if not alive(obj):
            return False
        try:
            name = obj.objectName() or ""
        except Exception:
            return False
        if name.startswith(OWN_PREFIX):
            return True
        obj = obj.parent()
        depth += 1
    return False


def message_level(name: str):
    """Return Qgis.MessageLevel.<name> for both old and new QGIS enum styles."""
    from qgis.core import Qgis

    scoped = getattr(Qgis, "MessageLevel", None)
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(Qgis, name)


def to_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")

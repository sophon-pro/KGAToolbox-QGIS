"""
Construction Sketch map tool (precision digitizing with a right-click construction menu).

Part of the "Construction Sketch" QGIS plugin. See README.md.
Requires QGIS 3.44 or newer (QGIS 4 / Qt6 supported).
"""

import math
from contextlib import suppress
import re

from qgis.core import Qgis
from qgis.utils import iface

TITLE = "Construction Sketch"

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsSettingsTree,
    QgsTolerance,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsVectorLayerUtils,
)
from qgis.gui import QgsAttributeEditorContext, QgsMapTool, QgsRubberBand, QgsSnapIndicator
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor
from .templates import category_attributes
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMenu,
)

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

SKETCH_COLOR = QColor(230, 0, 0)
WEB_MERCATOR = {"EPSG:3857", "EPSG:900913", "EPSG:3395", "ESRI:102100", "EPSG:102100"}
CIRCLE_SEGMENTS = 72                # corners of a drawn circle / ellipse
FREEHAND_SPACING_PX = 4             # freehand adds a vertex after the mouse moved this far
SHAPE_POINTS = {"rectangle": 2, "circle": 2, "ellipse": 3}    # shape tool -> clicks that define it
DUPLICATE_TOLERANCE = 1e-6          # working-CRS units; closer clicks are ignored
PICK_TOLERANCE_PX = 20              # how close (pixels) a click must be to pick an edge

GEOM_POINT = Qgis.GeometryType.Point
GEOM_LINE = Qgis.GeometryType.Line
GEOM_POLYGON = Qgis.GeometryType.Polygon
SUPPRESS_ON = Qgis.AttributeFormSuppression.On
SUPPRESS_DEFAULT = Qgis.AttributeFormSuppression.Default
CAP_ADD_FEATURES = Qgis.VectorProviderCapability.AddFeatures
try:                                # QGIS 4.x
    ICON_BOX = Qgis.RubberBandIconType.Box
    ICON_CIRCLE = Qgis.RubberBandIconType.Circle
    ICON_X = Qgis.RubberBandIconType.CrossX
except AttributeError:              # QGIS 3.44
    ICON_BOX = QgsRubberBand.IconType.ICON_BOX
    ICON_CIRCLE = QgsRubberBand.IconType.ICON_CIRCLE
    ICON_X = QgsRubberBand.IconType.ICON_X

SNAP_VERTEX = Qgis.SnappingType.Vertex
SNAP_SEGMENT = Qgis.SnappingType.Segment
SNAP_MIDDLE = Qgis.SnappingType.MiddleOfSegment
SNAP_ENDPOINT = Qgis.SnappingType.LineEndpoint


DOUBLE_CLICK_KEY = "construction_sketch/double_click_finish"


def double_click_finish_enabled():
    """Option in the right-click menu; on by default, remembered between QGIS sessions."""
    return QgsSettings().value(DOUBLE_CLICK_KEY, True, type=bool)


def set_double_click_finish(enabled):
    QgsSettings().setValue(DOUBLE_CLICK_KEY, bool(enabled))


def _attribute_form_disabled_globally():
    """QGIS option: Settings > Options > Digitizing > 'Suppress attribute form pop-up'."""
    entry = QgsSettingsTree.node("digitizing").childSetting("disable-enter-attribute-values-dialog")
    return bool(entry.valueAsVariant()) if entry is not None else False


# --------------------------------------------------------------------------
# Pure geometry / parsing helpers (no QGIS objects)
# --------------------------------------------------------------------------
def parse_dms(text):
    """'45', '45.5', '45 30 15', '45-30-15', '-12 15' -> decimal degrees."""
    t = text.strip().replace("\u2212", "-").replace("\u00b0", " ").replace("'", " ").replace('"', " ")
    if not t:
        raise ValueError("Please enter an angle.")
    negative = t.startswith("-")
    parts = [p for p in re.split(r"[\s:\-]+", t.lstrip("+-")) if p]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Cannot read angle '{text}'.")
    try:
        values = [float(p) for p in parts] + [0.0, 0.0]
    except ValueError:
        raise ValueError(f"Cannot read angle '{text}'.")
    degrees = values[0] + values[1] / 60.0 + values[2] / 3600.0
    return -degrees if negative else degrees


def parse_azimuth(text):
    """Azimuth (clockwise from north) or quadrant bearing (N45E) -> 0..360."""
    t = text.strip().upper()
    m = re.fullmatch(r"([NS])\s*(.+?)\s*([EW])", t)
    if not m:
        return parse_dms(t) % 360.0
    a = parse_dms(m.group(2))
    if not 0.0 <= a <= 90.0:
        raise ValueError("A quadrant bearing must be between 0 and 90 degrees.")
    quadrant = m.group(1) + m.group(3)
    return {"NE": a, "SE": 180.0 - a, "SW": 180.0 + a, "NW": 360.0 - a}[quadrant] % 360.0


def parse_number(text, name="value"):
    try:
        value = float(text.strip())
    except ValueError:
        raise ValueError(f"Please enter a number for {name}.")
    if not math.isfinite(value):
        raise ValueError(f"Please enter a number for {name}.")
    return value


def azimuth_deg(x1, y1, x2, y2):
    """Azimuth from point 1 to point 2, clockwise from north, 0..360."""
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360.0


def offset_xy(x, y, azimuth, distance):
    a = math.radians(azimuth)
    return x + distance * math.sin(a), y + distance * math.cos(a)


def square_corner(p0, p1, pn1, pn):
    """
    Corner point that closes a polygon with right angles: the new point lies on
    the line through pn perpendicular to the last segment (pn1 -> pn) AND on the
    line through p0 perpendicular to the first segment (p0 -> p1).
    Returns None if the two lines are parallel.
    """
    ax, ay = -(pn[1] - pn1[1]), pn[0] - pn1[0]
    bx, by = -(p1[1] - p0[1]), p1[0] - p0[0]
    denom = ax * by - ay * bx
    if abs(denom) <= 1e-12 * (math.hypot(ax, ay) * math.hypot(bx, by) or 1.0):
        return None
    wx, wy = p0[0] - pn[0], p0[1] - pn[1]
    s = (wx * by - wy * bx) / denom
    return pn[0] + s * ax, pn[1] + s * ay


def rectangle_ring(a, b):
    """Corners of the axis-aligned rectangle with opposite corners a and b (tuples)."""
    return [(a[0], a[1]), (b[0], a[1]), (b[0], b[1]), (a[0], b[1])]


def circle_ring(center, edge, segments=CIRCLE_SEGMENTS):
    """Points of the circle round center through edge (tuples); the first point is the edge point."""
    r = math.hypot(edge[0] - center[0], edge[1] - center[1])
    a0 = math.atan2(edge[1] - center[1], edge[0] - center[0])
    return [(center[0] + r * math.cos(a0 + 2 * math.pi * i / segments),
             center[1] + r * math.sin(a0 + 2 * math.pi * i / segments)) for i in range(segments)]


def ellipse_ring(center, major_end, minor_pt, segments=CIRCLE_SEGMENTS):
    """
    Points of the ellipse round center: semi-major axis to major_end, semi-minor axis = how far
    minor_pt is from the major axis line.
    """
    dx, dy = major_end[0] - center[0], major_end[1] - center[1]
    a = math.hypot(dx, dy)
    if a == 0:
        return []
    ux, uy = dx / a, dy / a
    b = abs((minor_pt[0] - center[0]) * -uy + (minor_pt[1] - center[1]) * ux)
    return [(center[0] + a * math.cos(t) * ux - b * math.sin(t) * uy,
             center[1] + a * math.cos(t) * uy + b * math.sin(t) * ux)
            for t in (2 * math.pi * i / segments for i in range(segments))]


def closest_point_on_segment(p, a, b):
    """Closest point to p on segment a-b (tuples)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return a[0] + t * dx, a[1] + t * dy


def segment_distance2(p, a, b):
    """Squared distance from point p to segment a-b (tuples)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    cx, cy = a[0] + t * dx, a[1] + t * dy
    return (p[0] - cx) ** 2 + (p[1] - cy) ** 2


# --------------------------------------------------------------------------
# Small input dialog
# --------------------------------------------------------------------------
def _exec(widget, *args):
    """Run a modal dialog/menu (exec() exists in both PyQt5 and PyQt6)."""
    return widget.exec(*args)


class _InputDialog(QDialog):
    def __init__(self, title, fields, combo=None):
        super().__init__(iface.mainWindow())
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self.edits = []
        for label, default in fields:
            edit = QLineEdit(str(default))
            form.addRow(label, edit)
            self.edits.append(edit)
        self.combo = None
        if combo:
            self.combo = QComboBox()
            self.combo.addItems(combo[1])
            form.addRow(combo[0], self.combo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if self.edits:
            self.edits[0].selectAll()
            self.edits[0].setFocus()


def _ask(title, fields, combo=None):
    """Show a dialog; return (list_of_texts, combo_index) or None if cancelled."""
    dlg = _InputDialog(title, fields, combo)
    try:
        if not _exec(dlg):                       # 0 = Rejected, 1 = Accepted
            return None
        return [e.text() for e in dlg.edits], (dlg.combo.currentIndex() if dlg.combo else None)
    finally:
        dlg.deleteLater()


# --------------------------------------------------------------------------
# The map tool
# --------------------------------------------------------------------------
class ConstructionSketchTool(QgsMapTool):

    featureAdded = pyqtSignal(object, object)       # layer, the new QgsFeature

    def __init__(self, canvas):
        super().__init__(canvas)
        self.layer = None
        self.fixed = False          # True: draws on the template's layer, whatever the active layer is
        self.mode = ""              # construction tool: "" / polygon / line / rectangle / circle / ...
        self.target_attrs = {}      # {field index: value} the template gives every new feature
        self.target_label = ""
        self._freehand_down = False
        self.work_crs = None
        self.geom_type = GEOM_LINE
        self.vertices = []          # QgsPointXY in working CRS
        self.preview = None         # cursor position (working CRS), constrained
        self.dir_lock = None        # azimuth in degrees, or None
        self.dist_lock = None       # distance in working-CRS units, or None
        self.picking = None         # "parallel" / "perpendicular" / None
        self.last = {"az": "0", "defl": "90", "dist": "10", "dx": "0", "dy": "0"}
        self._ct_cache = None       # (canvas->work, work->canvas) transforms
        self._status_shown = False
        self._hint = ""
        self._last_point = None
        self._finish_on_release = False
        self._ignore_next_release = False
        self._last_click_added_vertex = False
        self._pick_missed = False
        self._connected = False
        self._disposed = False

        self.sketch_band = QgsRubberBand(canvas, GEOM_LINE)
        self.vertex_band = QgsRubberBand(canvas, GEOM_POINT)
        self.guide_band = QgsRubberBand(canvas, GEOM_LINE)
        self.ref_band = QgsRubberBand(canvas, GEOM_LINE)
        self.self_snap_band = QgsRubberBand(canvas, GEOM_POINT)
        self._style_bands()
        self.snap_indicator = QgsSnapIndicator(canvas)
        try:
            self._tool_cursor = QgsApplication.getThemeCursor(QgsApplication.Cursor.CapturePoint)
        except Exception:
            self._tool_cursor = QCursor(Qt.CursorShape.CrossCursor)
        self.setCursor(self._tool_cursor)

    # ---------------------------------------------------------------- setup
    def _style_bands(self):
        self.sketch_band.setColor(SKETCH_COLOR)
        self.sketch_band.setFillColor(QColor(230, 0, 0, 50))
        self.sketch_band.setWidth(2)
        self.vertex_band.setColor(SKETCH_COLOR)
        self.vertex_band.setIcon(ICON_BOX)
        self.vertex_band.setIconSize(7)
        self.guide_band.setColor(QColor(0, 120, 255))
        self.guide_band.setWidth(1)
        self.guide_band.setLineStyle(Qt.PenStyle.DashLine)
        self.ref_band.setColor(QColor(0, 200, 255))
        self.ref_band.setWidth(4)
        self.self_snap_band.setColor(QColor(255, 0, 255))
        self.self_snap_band.setIcon(ICON_CIRCLE)
        self.self_snap_band.setIconSize(14)
        self.self_snap_band.setWidth(2)

    def activate(self):
        super().activate()
        if self._disposed:
            return
        self._connect_signals()
        self._set_bands_visible(True)
        layer = self.layer if self.fixed else iface.activeLayer()
        if (self.vertices and self._layer_alive() and isinstance(layer, QgsVectorLayer)
                and layer.id() == self.layer.id()):
            # Coming back from Pan / Zoom / another tool: continue the unfinished sketch
            self._ct_cache = None
            self._set_bands_visible(True)
            self._redraw()
        else:
            self._set_layer(layer if not self.fixed or self._layer_alive() else None, announce=True)

    def deactivate(self):
        """Switching to another tool hides the sketch but keeps it, so you can come back to it."""
        self._disconnect_signals()
        if not self._disposed:
            self.picking = None
            self._freehand_down = False
            self.dir_lock = self.dist_lock = None
            self.preview = None
            self.ref_band.reset(GEOM_LINE)
            self.guide_band.reset(GEOM_LINE)
            self._clear_snap_markers()
            self._set_bands_visible(False)
        self._clear_status()
        super().deactivate()

    def _set_bands_visible(self, visible):
        for band in (self.sketch_band, self.vertex_band, self.guide_band, self.ref_band, self.self_snap_band):
            band.setVisible(visible)

    def _connect_signals(self):
        if self._connected:
            return
        iface.currentLayerChanged.connect(self._on_current_layer_changed)
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_removed)
        self.canvas().destinationCrsChanged.connect(self._on_canvas_crs_changed)
        self._tree_selection = None
        try:                                    # show the chosen category as soon as it is clicked
            self._tree_selection = iface.layerTreeView().selectionModel()
            self._tree_selection.currentChanged.connect(self._on_tree_current_changed)
        except Exception:
            self._tree_selection = None
        self._connected = True

    def _disconnect_signals(self):
        if not self._connected:
            return
        for signal, slot in ((iface.currentLayerChanged, self._on_current_layer_changed),
                             (QgsProject.instance().layersWillBeRemoved, self._on_layers_removed),
                             (self.canvas().destinationCrsChanged, self._on_canvas_crs_changed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if getattr(self, "_tree_selection", None) is not None:
            try:
                self._tree_selection.currentChanged.disconnect(self._on_tree_current_changed)
            except (TypeError, RuntimeError):
                pass
            self._tree_selection = None
        self._connected = False

    def _on_tree_current_changed(self, *args):
        if self._layer_alive() and not self._disposed:
            self._show_status()

    def dispose(self):
        """Remove everything this tool drew on the canvas (used by uninstall)."""
        self._disconnect_signals()
        self._reset()
        scene = self.canvas().scene()
        for band in (self.sketch_band, self.vertex_band, self.guide_band, self.ref_band, self.self_snap_band):
            scene.removeItem(band)
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self._clear_status()
        self.layer = None
        self._disposed = True

    # ------------------------------------------------------- layer handling
    def _layer_alive(self):
        return self.layer is not None and not sip.isdeleted(self.layer)

    def _on_current_layer_changed(self, layer):
        if self.fixed:                      # a template picked in the Create Features pane owns the layer
            return
        if layer is not None and self._layer_alive() and layer.id() == self.layer.id():
            return
        had_sketch = bool(self.vertices)
        self._set_layer(layer, announce=True)
        if had_sketch:
            iface.messageBar().pushInfo(TITLE, "Active layer changed: the unfinished sketch was discarded.")

    def _on_layers_removed(self, layer_ids):
        if self.layer is not None and (sip.isdeleted(self.layer) or self.layer.id() in layer_ids):
            self._reset()
            self.layer = None
            self._clear_status()

    def _on_canvas_crs_changed(self):
        self._ct_cache = None
        self.ref_band.reset(GEOM_LINE)
        self._redraw()

    def _set_layer(self, layer, announce=False):
        """Remember the target layer. Does NOT start editing (that happens on the first click)."""
        self._reset()
        self.layer = None
        self._ct_cache = None
        self._last_point = None
        if not isinstance(layer, QgsVectorLayer) or layer.geometryType() not in (GEOM_POINT, GEOM_LINE, GEOM_POLYGON):
            return
        self.layer = layer
        self.geom_type = layer.geometryType()
        self.work_crs = self._pick_work_crs(layer)
        self.sketch_band.reset(self.geom_type)
        units = QgsUnitTypes.toString(self.work_crs.mapUnits())
        if self.geom_type == GEOM_POINT:
            self._hint = (f"Construction Sketch on '{layer.name()}' - distances in {units} "
                          f"({self.work_crs.authid()}). Left-click to add a point; right-click for "
                          f"Absolute X,Y, or Direction/Distance and Delta X,Y from the last point.")
        else:
            self._hint = (f"Construction Sketch on '{layer.name()}' - distances in {units} "
                          f"({self.work_crs.authid()}). Left-click to add vertices, right-click for options.")
        if self.fixed:
            self._hint = self._mode_hint(layer, self.mode)
        if announce:
            self._show_status()      # status bar, not the message bar: the map must not move

    def set_template(self, layer, attrs, mode, label=""):
        """Draw on `layer` with construction tool `mode`; new features get `attrs` ({field index: value})."""
        same = self.fixed and self._layer_alive() and layer is self.layer and mode == self.mode
        self.fixed = True
        self.mode = mode
        self.target_attrs = dict(attrs or {})
        self.target_label = label
        if not same:
            self._set_layer(layer, announce=False)
        self._hint = self._mode_hint(layer, mode)
        self._show_status()

    def clear_template(self):
        """Back to following the active layer (the old stand-alone behaviour)."""
        self.fixed = False
        self.mode = ""
        self.target_attrs = {}
        self.target_label = ""

    def _mode_hint(self, layer, mode):
        units = QgsUnitTypes.toString(self.work_crs.mapUnits()) if self.work_crs else ""
        head = f"{self.target_label or layer.name()} - distances in {units}. "
        tips = {
            "point": "Left-click to add a point; right-click for Absolute X,Y, Direction/Distance, Delta X,Y.",
            "rectangle": "Click one corner, then the opposite corner. Right-click for Delta X,Y.",
            "circle": "Click the centre, then a point on the edge. Right-click > Distance sets the radius.",
            "ellipse": "Click the centre, the end of the major axis, then the minor axis.",
            "freehand": "Press and drag to draw; release to finish.",
            "right_angle": "Click the first two vertices; every later segment turns exactly 90 degrees.",
        }
        return head + tips.get(mode, "Left-click to add vertices; right-click for options.")

    def _pick_work_crs(self, layer):
        crs = layer.crs()
        if crs.isValid() and not crs.isGeographic() and crs.authid() not in WEB_MERCATOR:
            return crs
        # Use the UTM zone at the map centre
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        try:
            c = self._transform(self._canvas_crs(), wgs84).transform(self.canvas().extent().center())
            lon, lat = c.x(), c.y()
        except Exception:
            lon, lat = 0.0, 0.0
        if not (math.isfinite(lon) and math.isfinite(lat)):
            lon, lat = 0.0, 0.0
        zone = min(60, max(1, int((lon + 180.0) // 6) + 1))
        return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")

    def _ensure_editable(self):
        """Start editing on the target layer if needed. Returns False if it cannot be edited."""
        layer = self.layer
        try:
            can_add = bool(layer.dataProvider().capabilities() & CAP_ADD_FEATURES)
        except Exception:
            can_add = True
        if layer.readOnly() or not can_add:
            iface.messageBar().pushWarning(TITLE, f"'{layer.name()}' is read-only; features cannot be added.")
            return False
        if layer.isEditable():
            return True
        if not layer.startEditing():
            iface.messageBar().pushWarning(TITLE, f"Could not start editing '{layer.name()}'.")
            return False
        return True                  # no pop-up here: it would shift the map right after a click

    def _ready(self, warn=False):
        if self._disposed:
            return False
        if not self._layer_alive():
            self.layer = None
            if warn:
                iface.messageBar().pushWarning(TITLE, "Select a line or polygon layer in the Layers panel.")
            return False
        return True

    # ------------------------------------------------------- CRS helpers
    @staticmethod
    def _transform(src, dst):
        return QgsCoordinateTransform(src, dst, QgsProject.instance())

    def _canvas_crs(self):
        return self.canvas().mapSettings().destinationCrs()

    def _transforms(self):
        if self._ct_cache is None:
            c = self._canvas_crs()
            self._ct_cache = (self._transform(c, self.work_crs), self._transform(self.work_crs, c))
        return self._ct_cache

    def _c2w(self, pt):
        return self._transforms()[0].transform(QgsPointXY(pt))

    def _w2c(self, pt):
        return self._transforms()[1].transform(QgsPointXY(pt))

    def _view_size(self):
        try:
            r = self._transforms()[0].transformBoundingBox(self.canvas().extent())
            return max(r.width(), r.height())
        except Exception:
            return 1000.0

    def _units(self):
        return QgsUnitTypes.toAbbreviatedString(self.work_crs.mapUnits()) if self.work_crs else ""

    # ------------------------------------------------------- constraints
    def _apply_constraints(self, p):
        if not self.vertices:
            return p
        if self.mode == "ellipse" and len(self.vertices) >= 2:
            return p                                  # the minor axis is measured from the centre
        o = self.vertices[-1]
        dx, dy = p.x() - o.x(), p.y() - o.y()
        direction = self._effective_direction()
        if direction is not None:
            ux, uy = math.sin(math.radians(direction)), math.cos(math.radians(direction))
            t = dx * ux + dy * uy
            if self.dist_lock is not None:
                t = self.dist_lock if t >= 0 else -self.dist_lock
            return QgsPointXY(o.x() + t * ux, o.y() + t * uy)
        if self.dist_lock is not None:
            length = math.hypot(dx, dy)
            if length == 0:
                return QgsPointXY(o.x(), o.y() + self.dist_lock)
            return QgsPointXY(o.x() + dx / length * self.dist_lock, o.y() + dy / length * self.dist_lock)
        return p

    def _effective_direction(self):
        """The locked direction, or - for Right Angle - 90 degrees to the last segment."""
        if self.dir_lock is not None:
            return self.dir_lock
        if self.mode == "right_angle" and len(self.vertices) >= 2:
            return (self._last_segment_azimuth() + 90.0) % 360.0
        return None

    def _clear_constraints(self):
        self.dir_lock = None
        self.dist_lock = None
        if self.picking:
            self._end_pick_cursor()
        self.picking = None
        self.ref_band.reset(GEOM_LINE)
        self._redraw()

    def _has_constraint(self):
        return self.dir_lock is not None or self.dist_lock is not None or self.picking is not None

    def _last_segment_azimuth(self):
        a, b = self.vertices[-2], self.vertices[-1]
        return azimuth_deg(a.x(), a.y(), b.x(), b.y())

    # ------------------------------------------------------- drawing
    def _set_band(self, band, points_work, geom_type):
        band.reset(geom_type)
        n = len(points_work)
        for i, p in enumerate(points_work):
            band.addPoint(self._w2c(p), i == n - 1)

    def _redraw(self):
        if not self.work_crs or self._disposed:
            return
        with suppress(Exception):
            pts = list(self.vertices)
            if self.preview is not None and not self.picking and self.vertices:
                pts.append(self.preview)
            ring = self._shape_ring(with_preview=True)
            self._set_band(self.sketch_band, pts if ring is None else ring, self.geom_type)
            markers = self.vertices
            if self.geom_type == GEOM_POINT and self._last_point is not None:
                markers = [self._last_point]          # reference for Direction/Distance and Delta X,Y
            self._set_band(self.vertex_band, markers, GEOM_POINT)
            direction = self._effective_direction()
            if direction is not None and self.vertices:
                o, size = self.vertices[-1], self._view_size()
                a = QgsPointXY(*offset_xy(o.x(), o.y(), direction, size))
                b = QgsPointXY(*offset_xy(o.x(), o.y(), direction, -size))
                self._set_band(self.guide_band, [b, a], GEOM_LINE)
            else:
                self.guide_band.reset(GEOM_LINE)
        self._show_status()

    def _shape_ring(self, with_preview=False):
        """
        Corner points (working CRS) of the rectangle / circle / ellipse being drawn; [] while it is
        not defined yet; None when the tool is not a shape tool.
        """
        if self.mode not in SHAPE_POINTS:
            return None
        v = [(p.x(), p.y()) for p in self.vertices]
        if with_preview and self.preview is not None and len(v) < SHAPE_POINTS[self.mode]:
            v.append((self.preview.x(), self.preview.y()))
        if len(v) < 2:
            return []
        if self.mode == "rectangle":
            pts = rectangle_ring(v[0], v[1])
        elif self.mode == "circle" or len(v) == 2:
            pts = circle_ring(v[0], v[1])
        else:
            pts = ellipse_ring(v[0], v[1], v[2])
        return [QgsPointXY(x, y) for x, y in pts]

    def _show_status(self):
        if not self.vertices and not self._has_constraint():
            if self._hint and self._layer_alive() and self.canvas().mapTool() is self:
                iface.mainWindow().statusBar().showMessage(self._hint_text())
                self._status_shown = True
            else:
                self._clear_status()
            return
        parts = [f"Vertices: {len(self.vertices)}"]
        if self.vertices and self.preview is not None:
            o, p = self.vertices[-1], self.preview
            parts.append(f"Length: {math.hypot(p.x() - o.x(), p.y() - o.y()):.3f} {self._units()}")
            parts.append(f"Direction: {azimuth_deg(o.x(), o.y(), p.x(), p.y()):.2f}\u00b0")
        if self.dir_lock is not None:
            parts.append(f"[Direction locked {self.dir_lock:.2f}\u00b0]")
        if self.dist_lock is not None:
            parts.append(f"[Distance locked {self.dist_lock:g} {self._units()}]")
        if self.picking:
            if getattr(self, "_pick_missed", False):
                parts.append(f"{self.picking.upper()}: no edge there. Move onto an edge or into a shape "
                             f"until an edge turns BLUE, then click. Right-click or Esc = cancel.")
            else:
                parts.append(f"{self.picking.upper()}: click the edge to copy (it turns BLUE under the mouse; "
                             f"clicking inside a shape uses its nearest edge). Right-click or Esc = cancel.")
        iface.mainWindow().statusBar().showMessage("   ".join(parts))
        self._status_shown = True

    def _clear_status(self):
        if self._status_shown:            # only clear messages this tool put there
            iface.mainWindow().statusBar().clearMessage()
            self._status_shown = False

    def _reset(self):
        self._finish_on_release = self._ignore_next_release = self._last_click_added_vertex = False
        self._freehand_down = False
        self.vertices = []
        self.preview = None
        self.dir_lock = self.dist_lock = self.picking = None
        if self._disposed:
            return
        for band, gtype in ((self.sketch_band, self.geom_type), (self.vertex_band, GEOM_POINT),
                            (self.guide_band, GEOM_LINE), (self.ref_band, GEOM_LINE),
                            (self.self_snap_band, GEOM_POINT)):
            band.reset(gtype)

    # ------------------------------------------------------- snapping
    def _clear_snap_markers(self):
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self.self_snap_band.reset(GEOM_POINT)

    def _snap(self, e):
        """
        Snapped position (canvas CRS) for a mouse event. Uses the project's snapping
        settings for other layers (QGIS engine) AND for the sketch being drawn
        (its own vertices, segment midpoints and edges). The better of the two wins:
        vertex > midpoint > edge, then the closer one.
        Returns (point in WORKING CRS, sketch_vertex_index or None).
        """
        point = e.snapPoint()                 # must run BEFORE mapPointMatch()
        match = e.mapPointMatch()
        own = self._snap_to_sketch(e.originalMapPoint())
        self.self_snap_band.reset(GEOM_POINT)

        if match.isValid() and own is not None:
            qgis_rank = 0 if match.hasVertex() else (2 if match.hasEdge() else 1)
            if (qgis_rank, match.distance()) <= (own[0], own[3]):
                own = None
        if own is None:
            self.snap_indicator.setMatch(match)
            return self._c2w(point), None

        rank, hit_work, index, _dist = own
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self.self_snap_band.setIcon(ICON_CIRCLE if rank == 0 else (ICON_BOX if rank == 1 else ICON_X))
        self.self_snap_band.addPoint(self._w2c(hit_work), True)
        return hit_work, index

    def _sketch_snap_settings(self):
        """(type flags, tolerance in canvas units) that apply to the sketch, or None if off."""
        config = QgsProject.instance().snappingConfig()
        if not config.enabled() or not self.vertices:
            return None
        flags, tolerance, units = config.typeFlag(), config.tolerance(), config.units()
        if config.mode() == Qgis.SnappingMode.AdvancedConfiguration:
            layer_settings = config.individualLayerSettings(self.layer)
            if not layer_settings.valid() or not layer_settings.enabled():
                return None
            flags, tolerance, units = layer_settings.typeFlag(), layer_settings.tolerance(), layer_settings.units()
        try:
            tol = QgsTolerance.toleranceInProjectUnits(tolerance, self.layer, self.canvas().mapSettings(), units)
        except Exception:
            tol = 12 * self.canvas().mapUnitsPerPixel()
        return flags, tol

    def _snap_to_sketch(self, canvas_pt):
        """
        Best snap on this sketch: (rank, point in WORKING CRS, vertex index or None, distance) or None.
        rank 0 = vertex, 1 = segment midpoint, 2 = edge.
        """
        settings = self._sketch_snap_settings()
        if settings is None:
            return None
        flags, tol = settings
        pts = [self._w2c(v) for v in self.vertices]
        p = (canvas_pt.x(), canvas_pt.y())
        candidates = []

        want_vertex = bool(flags & SNAP_VERTEX)
        want_ends = bool(flags & SNAP_ENDPOINT)
        for i, c in enumerate(pts):
            if want_vertex or (want_ends and i in (0, len(pts) - 1)):
                d = math.hypot(c.x() - p[0], c.y() - p[1])
                if d <= tol:
                    candidates.append((0, d, QgsPointXY(self.vertices[i]), i))

        segments = list(zip(pts, pts[1:]))
        if self.geom_type == GEOM_POLYGON and len(pts) >= 3:
            segments.append((pts[-1], pts[0]))            # closing edge
        # Tolerance is checked on screen (canvas CRS); the snapped point itself is computed
        # in the working CRS so it lies exactly on the straight edge / exact midpoint.
        work_segments = list(zip(self.vertices, self.vertices[1:]))
        if self.geom_type == GEOM_POLYGON and len(self.vertices) >= 3:
            work_segments.append((self.vertices[-1], self.vertices[0]))
        if flags & SNAP_MIDDLE:
            for (a, b), (wa, wb) in zip(segments, work_segments):
                m = QgsPointXY((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
                d = math.hypot(m.x() - p[0], m.y() - p[1])
                if d <= tol:
                    exact = QgsPointXY((wa.x() + wb.x()) / 2.0, (wa.y() + wb.y()) / 2.0)
                    candidates.append((1, d, exact, None))
        if flags & SNAP_SEGMENT:
            cursor_work = self._c2w(canvas_pt)
            for (a, b), (wa, wb) in zip(segments, work_segments):
                cx, cy = closest_point_on_segment(p, (a.x(), a.y()), (b.x(), b.y()))
                d = math.hypot(cx - p[0], cy - p[1])
                if d <= tol:
                    ex, ey = closest_point_on_segment((cursor_work.x(), cursor_work.y()),
                                                      (wa.x(), wa.y()), (wb.x(), wb.y()))
                    candidates.append((2, d, QgsPointXY(ex, ey), None))

        if not candidates:
            return None
        rank, d, hit, index = min(candidates, key=lambda c: (c[0], c[1]))
        return rank, hit, index, d

    # ------------------------------------------------------- mouse / keys
    def canvasPressEvent(self, e):
        """Freehand only: pressing starts the line / outline."""
        if (self.mode != "freehand" or e.button() != Qt.MouseButton.LeftButton or self.picking
                or not self._ready(warn=True)):
            return
        try:
            point, _index = self._snap(e)
            self._add_vertex(point)
            self._freehand_down = bool(self.vertices)
            self._freehand_px = (e.pixelPoint().x(), e.pixelPoint().y())
        except Exception as exc:
            iface.messageBar().pushCritical(TITLE, str(exc))

    def canvasMoveEvent(self, e):
        if not self._ready():
            return
        if self._freehand_down:
            with suppress(Exception):
                px = (e.pixelPoint().x(), e.pixelPoint().y())
                if math.hypot(px[0] - self._freehand_px[0], px[1] - self._freehand_px[1]) >= FREEHAND_SPACING_PX:
                    self._freehand_px = px
                    self._add_vertex(self._c2w(e.mapPoint()))
            return
        with suppress(Exception):
            if self.picking:
                # Highlight the segment that a click would pick (own sketch or other layers)
                self._clear_snap_markers()
                seg = self._find_segment(e.originalMapPoint()) or self._segment_of_shape_under(e.originalMapPoint())
                self.ref_band.reset(GEOM_LINE)
                if seg is not None:
                    self.ref_band.addPoint(seg[0], False)
                    self.ref_band.addPoint(seg[1], True)
                return
            point, _index = self._snap(e)
            if self.vertices:
                self.preview = self._apply_constraints(point)
                self._redraw()

    def canvasReleaseEvent(self, e):
        if e.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        if not self._ready(warn=True):
            return
        if e.button() == Qt.MouseButton.RightButton:
            if self.picking:                         # right-click cancels "click an edge"
                self._cancel_pick()
                return
            self._show_menu(e)
            return
        added_vertex = False
        if self._freehand_down:                      # releasing the mouse ends a freehand stroke
            self._freehand_down = False
            if len(self.vertices) >= (3 if self.geom_type == GEOM_POLYGON else 2):
                self._run(self._finish)
            if self.vertices:
                self._reset()
                self._show_status()
            return
        try:
            # Second release of a double-click: the first click already placed the vertex -> finish
            if self._finish_on_release:
                self._finish_on_release = False
                self._finish_by_double_click()
                return
            if self._ignore_next_release:
                self._ignore_next_release = False
                return
            if self.picking:
                self._pick_reference(e.originalMapPoint())
                return
            point, index = self._snap(e)
            if self.geom_type == GEOM_POINT:
                self._create_point(point)
                return
            # Clicking the first vertex of a polygon (with snapping on) closes and finishes it
            if (index == 0 and self.geom_type == GEOM_POLYGON and len(self.vertices) >= 3
                    and not self._has_constraint() and self.mode not in SHAPE_POINTS):
                self._clear_snap_markers()
                self._run(self._finish)
                return
            count = len(self.vertices)
            self._add_vertex(self._apply_constraints(point))
            added_vertex = len(self.vertices) > count
            if self.mode in SHAPE_POINTS and len(self.vertices) >= SHAPE_POINTS[self.mode]:
                self._clear_snap_markers()                # a rectangle / circle / ellipse finishes itself
                self._run(self._finish)
                added_vertex = False
        except Exception as exc:
            iface.messageBar().pushCritical(TITLE, str(exc))
        finally:
            self._last_click_added_vertex = added_vertex

    def canvasDoubleClickEvent(self, e):
        """
        Double-click = place the last vertex and finish (Qt sends: press, release, double-click,
        release). Only when the first click of the pair actually added a vertex, so a double-click
        used to pick an edge or to close a polygon never finishes by accident.
        """
        if e.button() != Qt.MouseButton.LeftButton or not self._ready():
            return
        if (double_click_finish_enabled() and self._last_click_added_vertex and not self.picking
                and self.mode not in SHAPE_POINTS and self.mode != "freehand"):
            self._finish_on_release = True
        else:
            self._ignore_next_release = True     # never turn the 2nd click into a stray vertex

    def _finish_by_double_click(self):
        need = 3 if self.geom_type == GEOM_POLYGON else 2
        if self.mode == "freehand":
            return
        if len(self.vertices) < need:
            iface.mainWindow().statusBar().showMessage(
                f"Construction Sketch: a {'polygon needs 3' if need == 3 else 'line needs 2'} vertices "
                f"to finish - keep clicking.", 5000)
            return
        self._clear_snap_markers()
        self._run(self._finish)

    def keyPressEvent(self, e):
        """
        NOTE: QGIS's convention is reversed - a map tool calls e.ignore() to say "I handled this
        key" (QgsMapCanvas::keyPressEvent). Keys left accepted get QGIS's default behaviour
        (arrow-key / space panning, and Delete/Backspace = Delete Selected Features!).
        """
        if not self._ready() or (not self.vertices and not self._has_constraint()):
            return                                    # not ours: QGIS handles it as usual
        key = e.key()
        if key == Qt.Key.Key_Escape:
            if self._has_constraint():
                self._clear_constraints()
            else:
                self._reset()
                self._show_status()
        elif key == Qt.Key.Key_Backspace:
            self._undo()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            self._run(self._finish)
        else:
            return                                    # e.g. arrows / space: QGIS pans as usual
        e.ignore()                                    # consumed (stops QGIS's Delete Selected on Backspace)

    # ------------------------------------------------------- right-click menu
    def _show_menu(self, e):
        n = len(self.vertices)
        is_polygon = self.geom_type == GEOM_POLYGON
        menu = QMenu(self.canvas())

        def add(text, slot, enabled=True):
            action = menu.addAction(text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda checked=False, f=slot: self._run(f))

        if self.geom_type == GEOM_POINT:
            has_ref = self._last_point is not None
            add("Absolute X, Y...", self._cmd_absolute_xy)
            add("Direction/Distance from Last Point...", self._cmd_direction_distance, has_ref)
            add("Delta X, Y from Last Point...", self._cmd_delta_xy, has_ref)
            try:
                _exec(menu, self.canvas().mapToGlobal(e.pixelPoint()))
            finally:
                menu.deleteLater()
            return

        if self.mode == "freehand":                        # nothing to construct: right-click drops the stroke
            if n:
                self._reset()
                self._show_status()
            return
        if self.mode in SHAPE_POINTS:
            add("Absolute X, Y...", self._cmd_absolute_xy)
            add("Delta X, Y...", self._cmd_delta_xy, n >= 1)
            add("Direction/Distance...", self._cmd_direction_distance, n >= 1)
            if self.mode == "circle":
                add("Distance (radius)...", self._cmd_distance, n >= 1)
            menu.addSeparator()
            add("Clear Constraints	Esc", self._clear_constraints, self._has_constraint())
            add("Undo Last Vertex	Backspace", self._undo, n >= 1)
            add("Delete Sketch", lambda: (self._reset(), self._show_status()), n >= 1)
            try:
                _exec(menu, self.canvas().mapToGlobal(e.pixelPoint()))
            finally:
                menu.deleteLater()
            return

        add("Parallel...", lambda: self._start_pick("parallel"), n >= 1)
        add("Perpendicular...", lambda: self._start_pick("perpendicular"), n >= 1)
        add("Perpendicular to Last Segment", self._cmd_perpendicular_last, n >= 2)
        menu.addSeparator()
        add("Direction...", self._cmd_direction, n >= 1)
        add("Deflection...", self._cmd_deflection, n >= 2)
        add("Distance...", self._cmd_distance, n >= 1)
        add("Length...", self._cmd_length, n >= 2)
        add("Direction/Distance...", self._cmd_direction_distance, n >= 1)
        menu.addSeparator()
        add("Absolute X, Y...", self._cmd_absolute_xy)
        add("Delta X, Y...", self._cmd_delta_xy, n >= 1)
        menu.addSeparator()
        add("Clear Constraints\tEsc", self._clear_constraints, self._has_constraint())
        add("Undo Last Vertex\tBackspace", self._undo, n >= 1)
        menu.addSeparator()
        if is_polygon:
            add("Square and Finish", self._square_and_finish, n >= 3)
        add("Finish Sketch\tF2", self._finish, n >= (3 if is_polygon else 2))
        add("Delete Sketch", lambda: (self._reset(), self._show_status()), n >= 1)
        menu.addSeparator()
        option = menu.addAction("Double-click to Finish")
        option.setCheckable(True)
        option.setChecked(double_click_finish_enabled())
        option.toggled.connect(set_double_click_finish)

        try:
            _exec(menu, self.canvas().mapToGlobal(e.pixelPoint()))
        finally:
            menu.deleteLater()

    def _run(self, func):
        if not self._ready(warn=True):
            return
        try:
            func()
        except ValueError as exc:
            iface.messageBar().pushWarning(TITLE, str(exc))
        except Exception as exc:
            iface.messageBar().pushCritical(TITLE, str(exc))
        if not self._disposed:
            self.canvas().setFocus()

    # ------------------------------------------------------- vertex operations
    def _add_vertex(self, p):
        if not self.vertices and not self._ensure_editable():
            return
        p = QgsPointXY(p)
        if not (math.isfinite(p.x()) and math.isfinite(p.y())):
            raise ValueError("That position cannot be converted to the working CRS.")
        if self.vertices:
            last = self.vertices[-1]
            if math.hypot(p.x() - last.x(), p.y() - last.y()) < DUPLICATE_TOLERANCE:
                return        # double-click or repeated click on the same spot
        self.vertices.append(p)
        self.dir_lock = self.dist_lock = self.picking = None   # constraints apply to one segment
        self.ref_band.reset(GEOM_LINE)
        self.preview = None
        self._redraw()

    def _undo(self):
        if self.vertices:
            self.vertices.pop()
            self.preview = None
            self._clear_constraints()

    # ------------------------------------------------------- parallel / perpendicular
    def _start_pick(self, kind):
        self.dir_lock = None
        self.picking = kind
        self._pick_missed = False
        self._clear_snap_markers()
        try:
            self.canvas().setCursor(QgsApplication.getThemeCursor(QgsApplication.Cursor.Select))
        except Exception:
            self.canvas().setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._redraw()

    def _end_pick_cursor(self):
        self.canvas().setCursor(self._tool_cursor)

    def _cancel_pick(self):
        self.picking = None
        self.ref_band.reset(GEOM_LINE)
        self._end_pick_cursor()
        self._redraw()

    def _pick_reference(self, canvas_pt):
        seg = self._find_segment(canvas_pt) or self._segment_of_shape_under(canvas_pt)
        if seg is None:
            self._pick_missed = True          # status bar explains; no pop-up (it would move the map)
            self._show_status()
            return
        a, b = (self._c2w(p) for p in seg)
        if math.hypot(b.x() - a.x(), b.y() - a.y()) < DUPLICATE_TOLERANCE:
            self._pick_missed = True
            self._show_status()
            return
        az = azimuth_deg(a.x(), a.y(), b.x(), b.y())
        if self.picking == "perpendicular":
            az += 90.0
        self.dir_lock = az % 360.0
        self.picking = None
        self._end_pick_cursor()
        self.ref_band.reset(GEOM_LINE)
        self.ref_band.addPoint(seg[0], False)
        self.ref_band.addPoint(seg[1], True)
        self._redraw()
        # The status bar now shows "[Direction locked ...]"

    def _find_segment(self, cpt):
        """Nearest segment (two QgsPointXY in canvas CRS) within PICK_TOLERANCE_PX pixels. Read-only."""
        canvas = self.canvas()
        canvas_crs = self._canvas_crs()
        tol = PICK_TOLERANCE_PX * canvas.mapUnitsPerPixel()
        best_d2, best = tol * tol, None
        p = (cpt.x(), cpt.y())

        sketch = [self._w2c(v) for v in self.vertices]
        if self.geom_type == GEOM_POLYGON and len(sketch) >= 3:
            sketch.append(sketch[0])
        for a, b in zip(sketch, sketch[1:]):
            d2 = segment_distance2(p, (a.x(), a.y()), (b.x(), b.y()))
            if d2 < best_d2:
                best_d2, best = d2, (a, b)

        rect = QgsRectangle(cpt.x() - tol, cpt.y() - tol, cpt.x() + tol, cpt.y() + tol)
        for lyr in canvas.layers():
            if not isinstance(lyr, QgsVectorLayer) or lyr.geometryType() not in (GEOM_LINE, GEOM_POLYGON):
                continue
            with suppress(Exception):
                layer_rect = self._transform(canvas_crs, lyr.crs()).transformBoundingBox(rect)
                to_canvas = self._transform(lyr.crs(), canvas_crs)
                request = QgsFeatureRequest().setFilterRect(layer_rect).setNoAttributes().setLimit(200)
                for feat in lyr.getFeatures(request):
                    geom = QgsGeometry(feat.geometry())     # copy: the layer's geometry is untouched
                    if geom.isEmpty():
                        continue
                    geom.transform(to_canvas)
                    d2, _pt, after, _left = geom.closestSegmentWithContext(QgsPointXY(cpt))
                    if 0 <= d2 < best_d2 and after > 0:
                        va, vb = geom.vertexAt(after - 1), geom.vertexAt(after)
                        best_d2, best = d2, (QgsPointXY(va.x(), va.y()), QgsPointXY(vb.x(), vb.y()))
        return best

    def _segment_of_shape_under(self, cpt):
        """
        Clicked INSIDE a polygon (the sketch itself, or a polygon feature)? Then use that
        polygon's edge nearest to the click. Returns two canvas-CRS points or None.
        """
        p = QgsPointXY(cpt)
        point_geom = QgsGeometry.fromPointXY(p)
        if self.geom_type == GEOM_POLYGON and len(self.vertices) >= 3:
            ring = [self._w2c(v) for v in self.vertices]
            if QgsGeometry.fromPolygonXY([ring + [ring[0]]]).contains(point_geom):
                edges = list(zip(ring, ring[1:] + [ring[0]]))
                return min(edges, key=lambda e: segment_distance2((p.x(), p.y()), (e[0].x(), e[0].y()),
                                                                  (e[1].x(), e[1].y())))
        canvas_crs = self._canvas_crs()
        for lyr in self.canvas().layers():
            if not isinstance(lyr, QgsVectorLayer) or lyr.geometryType() != GEOM_POLYGON:
                continue
            with suppress(Exception):
                to_layer = self._transform(canvas_crs, lyr.crs())
                lp = to_layer.transform(p)
                request = QgsFeatureRequest().setFilterRect(QgsRectangle(lp.x(), lp.y(), lp.x(), lp.y()))
                request.setNoAttributes().setLimit(20)
                for feat in lyr.getFeatures(request):
                    geom = QgsGeometry(feat.geometry())
                    if geom.isEmpty() or not geom.contains(QgsGeometry.fromPointXY(lp)):
                        continue
                    geom.transform(self._transform(lyr.crs(), canvas_crs))
                    _d2, _pt, after, _left = geom.closestSegmentWithContext(p)
                    if after > 0:
                        va, vb = geom.vertexAt(after - 1), geom.vertexAt(after)
                        return QgsPointXY(va.x(), va.y()), QgsPointXY(vb.x(), vb.y())
        return None

    # ------------------------------------------------------- dialog commands
    def _cmd_perpendicular_last(self):
        """Lock the next segment at 90 degrees to the previous one (no edge click needed)."""
        self.dir_lock = (self._last_segment_azimuth() + 90.0) % 360.0
        a, b = self.vertices[-2], self.vertices[-1]
        self.ref_band.reset(GEOM_LINE)
        self.ref_band.addPoint(self._w2c(a), False)
        self.ref_band.addPoint(self._w2c(b), True)
        self._redraw()

    def _cmd_direction(self):
        res = _ask("Direction", [("Azimuth (\u00b0 clockwise from north)\ne.g. 45, 45 30 00, N45E",
                                  self.last["az"])])
        if res:
            self.dir_lock = parse_azimuth(res[0][0])
            self.last["az"] = res[0][0]
            self._redraw()

    def _cmd_deflection(self):
        res = _ask("Deflection", [("Deflection angle (+ right, \u2212 left)", self.last["defl"]),
                                  (f"Distance ({self._units()}) - optional", "")])
        if not res:
            return
        defl_text, dist_text = res[0]
        az = (self._last_segment_azimuth() + parse_dms(defl_text)) % 360.0
        self.last["defl"] = defl_text
        if dist_text.strip():
            o = self.vertices[-1]
            self._add_vertex(QgsPointXY(*offset_xy(o.x(), o.y(), az, self._positive(dist_text, "distance"))))
        else:
            self.dir_lock = az
            self._redraw()

    def _cmd_distance(self):
        res = _ask("Distance", [(f"Distance ({self._units()})", self.last["dist"])])
        if res:
            self.dist_lock = self._positive(res[0][0], "distance")
            self.last["dist"] = res[0][0]
            self._redraw()

    def _cmd_length(self):
        res = _ask("Length", [(f"Length ({self._units()}) in the previous direction", self.last["dist"])])
        if res:
            length = self._positive(res[0][0], "length")
            self.last["dist"] = res[0][0]
            o = self.vertices[-1]
            self._add_vertex(QgsPointXY(*offset_xy(o.x(), o.y(), self._last_segment_azimuth(), length)))

    def _cmd_direction_distance(self):
        res = _ask("Direction/Distance", [("Azimuth (\u00b0 clockwise from north)", self.last["az"]),
                                          (f"Distance ({self._units()})", self.last["dist"])])
        if res:
            az_text, dist_text = res[0]
            az, dist = parse_azimuth(az_text), self._positive(dist_text, "distance")
            self.last.update(az=az_text, dist=dist_text)
            o = self._origin()
            self._place(QgsPointXY(*offset_xy(o.x(), o.y(), az, dist)))

    def _cmd_absolute_xy(self):
        crs_options = [
            (self.work_crs, f"Working CRS ({self.work_crs.authid()})"),
            (self.layer.crs(), f"Layer CRS ({self.layer.crs().authid()})"),
            (self._canvas_crs(), f"Map CRS ({self._canvas_crs().authid()})"),
            (QgsCoordinateReferenceSystem("EPSG:4326"), "WGS 84 longitude/latitude (EPSG:4326)"),
        ]
        default = self._origin() or self._c2w(self.canvas().extent().center())
        res = _ask("Absolute X, Y",
                   [("X / Easting / Longitude", f"{default.x():.3f}"),
                    ("Y / Northing / Latitude", f"{default.y():.3f}")],
                   ("Coordinates in", [label for _crs, label in crs_options]))
        if res:
            (x_text, y_text), idx = res
            pt = QgsPointXY(parse_number(x_text, "X"), parse_number(y_text, "Y"))
            src = crs_options[idx][0]
            if src.isGeographic() and not (-180 <= pt.x() <= 180 and -90 <= pt.y() <= 90):
                raise ValueError("Longitude must be -180..180 and latitude -90..90.")
            self._place(self._transform(src, self.work_crs).transform(pt))

    def _cmd_delta_xy(self):
        res = _ask("Delta X, Y", [(f"Delta X ({self._units()})", self.last["dx"]),
                                  (f"Delta Y ({self._units()})", self.last["dy"])])
        if res:
            dx_text, dy_text = res[0]
            dx, dy = parse_number(dx_text, "Delta X"), parse_number(dy_text, "Delta Y")
            self.last.update(dx=dx_text, dy=dy_text)
            o = self._origin()
            self._place(QgsPointXY(o.x() + dx, o.y() + dy))

    # ------------------------------------------------------- points
    def _origin(self):
        """Reference for relative input: last vertex (lines/polygons) or last point placed (points)."""
        if self.geom_type == GEOM_POINT:
            return self._last_point
        return self.vertices[-1] if self.vertices else None

    def _place(self, p):
        """Result of a dialog command: a new point feature, or the next vertex of the sketch."""
        if self.geom_type == GEOM_POINT:
            self._create_point(p)
        else:
            self._add_vertex(p)

    def _create_point(self, p):
        """Add one point feature at p (working CRS)."""
        p = QgsPointXY(p)
        if not (math.isfinite(p.x()) and math.isfinite(p.y())):
            raise ValueError("That position cannot be converted to the working CRS.")
        if not self._ensure_editable():
            return
        layer = self.layer
        geom = QgsGeometry.fromPointXY(p)
        geom.transform(self._transform(self.work_crs, layer.crs()))
        coerced = geom.coerceToType(layer.wkbType())          # handles MultiPoint and Z/M layers
        if not coerced or coerced[0].isEmpty():
            iface.messageBar().pushCritical(TITLE, "The point does not match the layer's geometry type.")
            return
        feature = self._new_feature(layer, coerced[0])
        if self._add_feature(layer, feature):
            layer.triggerRepaint()
            self._last_point = p
            self.featureAdded.emit(layer, feature)
            self._clear_snap_markers()
            self._redraw()
            iface.mainWindow().statusBar().showMessage(
                f"Point added at {p.x():.3f}, {p.y():.3f} ({self.work_crs.authid()}). "
                f"Right-click for Direction/Distance or Delta X,Y from this point.", 8000)

    @staticmethod
    def _positive(text, name):
        value = parse_number(text, name)
        if value <= 0:
            raise ValueError(f"The {name} must be greater than 0.")
        return value

    # ------------------------------------------------------- finishing
    def _square_and_finish(self):
        v = [(p.x(), p.y()) for p in self.vertices]
        corner = square_corner(v[0], v[1], v[-2], v[-1])
        if corner is None:
            raise ValueError("Cannot square this shape: the first and last segments are parallel.")
        self.vertices.append(QgsPointXY(*corner))
        if not self._finish():
            self.vertices.pop()           # keep the sketch as it was if finishing failed/cancelled
            self._redraw()

    def _finish(self):
        """Create the feature. Returns True if a feature was added."""
        layer = self.layer
        is_polygon = self.geom_type == GEOM_POLYGON
        if self.mode in SHAPE_POINTS:
            if len(self.vertices) < SHAPE_POINTS[self.mode]:
                return False
            pts = self._shape_ring()
        elif len(self.vertices) < (3 if is_polygon else 2):
            iface.mainWindow().statusBar().showMessage(
                f"Construction Sketch: a {'polygon needs 3' if is_polygon else 'line needs 2'} vertices to finish.", 5000)
            return False
        else:
            pts = list(self.vertices)
        if not self._ensure_editable():
            return False
        geom = QgsGeometry.fromPolygonXY([pts + [pts[0]]]) if is_polygon else QgsGeometry.fromPolylineXY(pts)
        geom.transform(self._transform(self.work_crs, layer.crs()))
        coerced = geom.coerceToType(layer.wkbType())
        if not coerced or coerced[0].isEmpty():
            iface.messageBar().pushCritical(TITLE, "The sketch does not match the layer's geometry type.")
            return False
        if not coerced[0].isGeosValid():
            iface.messageBar().pushWarning(TITLE, "Note: the new feature's geometry is not valid "
                                                  "(e.g. it crosses itself).")
        feature = self._new_feature(layer, coerced[0])
        added = self._add_feature(layer, feature)
        if added:
            layer.triggerRepaint()
            self._reset()
            self._show_status()
            self.featureAdded.emit(layer, feature)
        return added

    def _new_feature(self, layer, geometry):
        """New feature with default values + the template's values (or the category chosen in the Layers panel)."""
        attrs = self.target_attrs if self.fixed else category_attributes(iface, layer)[0]
        return QgsVectorLayerUtils.createFeature(layer, geometry, attrs, layer.createExpressionContext())

    def _hint_text(self):
        """Status-bar hint, including the category new features will get."""
        if self.fixed:
            return self._hint
        try:
            attrs, text = category_attributes(iface, self.layer)
        except Exception:
            attrs, text = {}, None
        if attrs:
            return f"{self._hint}  |  New features: category {text}"
        if text:
            return f"{self._hint}  |  {text}"
        return self._hint

    def _add_feature(self, layer, feature):
        """Add the feature like QGIS does: via the attribute form unless it is suppressed."""
        suppress = layer.editFormConfig().suppress()
        globally_off = _attribute_form_disabled_globally()
        show_form = layer.fields().count() > 0 and not (
            suppress == SUPPRESS_ON or (suppress == SUPPRESS_DEFAULT and globally_off))

        if show_form:
            before = set(layer.editBuffer().addedFeatures().keys()) if layer.editBuffer() else set()
            form = iface.getFeatureForm(layer, feature)
            form.setMode(QgsAttributeEditorContext.Mode.AddFeatureMode)
            try:
                if not _exec(form):
                    iface.messageBar().pushInfo(TITLE, "Cancelled: no feature added. Your sketch is kept.")
                    return False
                after = set(layer.editBuffer().addedFeatures().keys()) if layer.editBuffer() else set()
                if after - before:
                    return True               # the form added the feature itself
                edited = form.feature()       # otherwise add it with the attributes from the form
                if edited is not None:
                    feature = QgsFeature(edited)
            finally:
                form.deleteLater()

        layer.beginEditCommand("Construction sketch")
        if not layer.addFeature(feature):
            layer.destroyEditCommand()
            iface.messageBar().pushCritical(TITLE, "Could not add the feature to the layer.")
            return False
        layer.endEditCommand()
        return True

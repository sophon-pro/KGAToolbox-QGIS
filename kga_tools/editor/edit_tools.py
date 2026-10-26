"""
Editing tools that work on EVERY visible, editable layer - no need to make a layer active.

* SplitTool - draw a line (click vertices; finish with double-click, right-click or Enter).
  Every line/polygon feature it crosses, in every visible editable layer, is split.
  If features are selected, only the selected features are split (the same rule as
  Delete Selected).
  Backspace = remove last vertex, Esc = cancel.
"""

from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsLineString,
    QgsPoint,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor

GEOM_LINE = Qgis.GeometryType.Line
GEOM_POLYGON = Qgis.GeometryType.Polygon
SUCCESS = Qgis.GeometryOperationResult.Success


def visible_editable_layers(geometry_types=(GEOM_LINE, GEOM_POLYGON)):
    """Layers in edit mode, visible in the Layers panel, of the given geometry types (top first)."""
    result = []
    for node in QgsProject.instance().layerTreeRoot().findLayers():
        layer = node.layer()
        if (isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer) and node.isVisible()
                and layer.isEditable() and layer.geometryType() in geometry_types):
            result.append(layer)
    return result


def split_with_line(canvas_points, canvas_crs, iface=None):
    """
    Split features with a line given in canvas coordinates.
    Returns (new_features, layers_changed).
    """
    targets = visible_editable_layers()
    use_selection = any(lyr.selectedFeatureCount() for lyr in targets)
    topological = QgsProject.instance().topologicalEditing()
    new_features, changed = 0, []
    for layer in targets:
        if use_selection and not layer.selectedFeatureCount():
            continue                     # a selection exists elsewhere: only selected features are split
        ct = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
        line = None
        with suppress(Exception):
            line = QgsLineString([QgsPoint(ct.transform(QgsPointXY(p))) for p in canvas_points])
        if line is None:
            continue
        before = len(layer.editBuffer().addedFeatures()) if layer.editBuffer() else 0
        layer.beginEditCommand("Split features")
        result, topo_points = layer.splitFeatures(line, True, topological)
        if result == SUCCESS:                       # QGIS reports NothingHappened when nothing was split
            if topological:
                for p in topo_points:
                    layer.addTopologicalPoints(p)
            layer.endEditCommand()
            after = len(layer.editBuffer().addedFeatures()) if layer.editBuffer() else before
            new_features += max(after - before, 1)
            changed.append(layer.name())
            layer.triggerRepaint()
        else:
            layer.destroyEditCommand()
    if iface is not None:
        if changed:
            text = (f"Split: {new_features} new feature(s) in {len(changed)} layer(s) ({', '.join(changed)})."
                    + (" Only selected features were split." if use_selection else ""))
        else:
            text = ("Split: nothing was split - draw the line right across the feature(s)."
                    + (" (Only selected features are split; clear the selection to split everything.)"
                       if use_selection else ""))
        iface.mainWindow().statusBar().showMessage("Edit Session " + text, 8000)
    return new_features, changed


class SplitTool(QgsMapTool):

    def __init__(self, canvas, iface):
        super().__init__(canvas)
        self.iface = iface
        self.points = []
        self._ignore_next_release = False
        self.band = QgsRubberBand(canvas, GEOM_LINE)
        self.band.setColor(QColor(220, 0, 0, 230))
        self.band.setWidth(2)
        self.band.setLineStyle(Qt.PenStyle.DashLine)
        self.snap_indicator = QgsSnapIndicator(canvas)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def dispose(self):
        self._reset()
        self.canvas().scene().removeItem(self.band)
        self.snap_indicator.setMatch(QgsPointLocator.Match())

    def activate(self):
        super().activate()
        self.iface.mainWindow().statusBar().showMessage(
            "Split (all layers): click to draw a line across the features; double-click, right-click or "
            "Enter to split. Backspace = undo vertex, Esc = cancel.", 10000)

    def deactivate(self):
        self._reset()
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()

    def _reset(self):
        self.points = []
        self.band.reset(GEOM_LINE)

    def _draw(self, cursor=None):
        self.band.reset(GEOM_LINE)
        pts = self.points + ([cursor] if cursor is not None else [])
        for i, p in enumerate(pts):
            self.band.addPoint(p, i == len(pts) - 1)

    def canvasMoveEvent(self, e):
        point = e.snapPoint()
        self.snap_indicator.setMatch(e.mapPointMatch())
        if self.points:
            self._draw(QgsPointXY(point))

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.finish()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._ignore_next_release:          # second click of a double-click
            self._ignore_next_release = False
            return
        p = QgsPointXY(e.snapPoint())
        if not self.points or p != self.points[-1]:
            self.points.append(p)
        self._draw()

    def canvasDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._ignore_next_release = True
            self.finish()

    def keyPressEvent(self, e):
        # QGIS convention: e.ignore() = "this tool handled the key"
        key = e.key()
        if key == Qt.Key.Key_Escape and self.points:
            self._reset()
            e.ignore()
        elif key == Qt.Key.Key_Backspace and self.points:
            self.points.pop()
            self._draw()
            e.ignore()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.points:
            self.finish()
            e.ignore()

    def finish(self):
        pts = list(self.points)
        self._reset()
        if len(pts) < 2:
            return 0, []
        return split_with_line(pts, self.canvas().mapSettings().destinationCrs(), self.iface)

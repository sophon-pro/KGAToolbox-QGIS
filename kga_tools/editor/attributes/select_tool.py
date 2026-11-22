"""
"Change the selection." - the Attributes pane's own map tool.

Builds on the Edit Session select tool (click or drag a rectangle; every visible, selectable layer;
Shift adds, Ctrl removes, N = next overlapping feature) and adds the other Pro selection shapes:
Polygon, Freehand, Circle and Line. It never deletes anything.
"""

import math
from contextlib import suppress

from qgis.core import QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsGeometry, QgsPointXY
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

from ..selection import (
    CLICK_TOLERANCE_PX,
    GEOM_LINE,
    GEOM_POLYGON,
    SelectTool,
    _status,
    _transform,
    behavior_from_modifiers,
    selection_summary,
)

MODES = ("rectangle", "polygon", "freehand", "circle", "line")
FREEHAND_STEP_PX = 3
CIRCLE_SEGMENTS = 48


class AttributesSelectTool(SelectTool):

    def __init__(self, canvas, manager, iface):
        super().__init__(canvas, manager, iface)
        self.mode = "rectangle"
        self._points = []              # polygon / line corners (map coordinates)
        self._cursor = None
        self._drawing = False          # freehand / circle gesture in progress
        self._center = None
        self.line_band = QgsRubberBand(canvas, GEOM_LINE)
        self.line_band.setColor(QColor(255, 140, 0, 220))
        self.line_band.setWidth(2)

    def right_click_menu_allowed(self):
        return self.mode == "rectangle"        # polygon / line: right-click finishes the shape

    def dispose(self):
        with suppress(Exception):
            self.line_band.reset(GEOM_LINE)
            self.canvas().scene().removeItem(self.line_band)
        super().dispose()

    def set_mode(self, mode):
        if mode in MODES:
            self._reset()
            self.mode = mode

    def deactivate(self):
        self._reset()
        super().deactivate()

    def _reset(self):
        self._points = []
        self._cursor = None
        self._drawing = False
        self._center = None
        self.band.reset(GEOM_POLYGON)
        self.line_band.reset(GEOM_LINE)
        self._cancel_drag()

    # ----------------------------------------------------------------------------- mouse
    def canvasPressEvent(self, e):
        if self.mode == "rectangle":
            return super().canvasPressEvent(e)
        button = e.button()
        point = QgsPointXY(e.mapPoint())
        if self.mode in ("polygon", "line"):
            if button == Qt.MouseButton.LeftButton:
                if not self._points or self._points[-1] != point:
                    self._points.append(point)
                self._draw()
            elif button == Qt.MouseButton.RightButton:
                self.finish(behavior_from_modifiers(e.modifiers()))
        elif button == Qt.MouseButton.LeftButton:
            self._drawing = True
            self._center = point
            self._points = [point]
            self._press_px = e.pixelPoint()
        return None

    def canvasMoveEvent(self, e):
        if self.mode == "rectangle":
            return super().canvasMoveEvent(e)
        point = QgsPointXY(e.mapPoint())
        if self.mode in ("polygon", "line"):
            self._cursor = point
            if self._points:
                self._draw()
        elif self._drawing:
            if self.mode == "freehand":
                last = self.toCanvasCoordinates(self._points[-1])
                now = e.pixelPoint()
                if abs(now.x() - last.x()) + abs(now.y() - last.y()) >= FREEHAND_STEP_PX:
                    self._points.append(point)
            else:
                self._cursor = point
            self._draw()
        return None

    def canvasReleaseEvent(self, e):
        if self.mode == "rectangle":
            return super().canvasReleaseEvent(e)
        if self.mode in ("freehand", "circle") and self._drawing and e.button() == Qt.MouseButton.LeftButton:
            self._cursor = QgsPointXY(e.mapPoint())
            self.finish(behavior_from_modifiers(e.modifiers()))
        return None

    def canvasDoubleClickEvent(self, e):
        if self.mode in ("polygon", "line") and e.button() == Qt.MouseButton.LeftButton:
            self.finish(behavior_from_modifiers(e.modifiers()))

    def keyPressEvent(self, e):
        key = e.key()
        if self.mode != "rectangle":
            if key == Qt.Key.Key_Escape:
                self._reset()
                e.ignore()
                return
            if key == Qt.Key.Key_Backspace and self._points and self.mode in ("polygon", "line"):
                self._points.pop()
                self._draw()
                e.ignore()
                return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            return                         # this tool never deletes features
        super().keyPressEvent(e)

    # ----------------------------------------------------------------------------- drawing
    def _shape(self):
        """The shape being drawn as a geometry in map coordinates, or None."""
        pts = list(self._points)
        if self.mode == "circle":
            if not pts or self._cursor is None:
                return None
            radius = math.hypot(self._cursor.x() - pts[0].x(), self._cursor.y() - pts[0].y())
            if radius <= 0:
                return None
            return QgsGeometry.fromPointXY(pts[0]).buffer(radius, CIRCLE_SEGMENTS)
        if self.mode in ("polygon", "freehand"):
            if self.mode == "polygon" and self._cursor is not None:
                pts.append(self._cursor)
            if len(pts) < 3:
                return QgsGeometry.fromPolylineXY(pts) if len(pts) == 2 else None
            return QgsGeometry.fromPolygonXY([pts + [pts[0]]])
        if self.mode == "line":
            if self._cursor is not None:
                pts.append(self._cursor)
            return QgsGeometry.fromPolylineXY(pts) if len(pts) >= 2 else None
        return None

    def _draw(self):
        self.band.reset(GEOM_POLYGON)
        self.line_band.reset(GEOM_LINE)
        shape = self._shape()
        if shape is None or shape.isEmpty():
            return
        none = QgsCoordinateReferenceSystem()
        if shape.type() == GEOM_POLYGON:
            self.band.addGeometry(shape, none)
        else:
            self.line_band.addGeometry(shape, none)

    # ----------------------------------------------------------------------------- selecting
    def finish(self, behavior):
        """Select what the finished shape touches."""
        if self.mode in ("freehand", "circle"):
            if self.mode == "freehand":
                self._cursor = None
            shape = self._shape()
        else:
            self._cursor = None
            shape = self._shape()
        if shape is not None and self.mode == "line":
            tol = CLICK_TOLERANCE_PX * self.canvas().mapUnitsPerPixel()
            shape = shape.buffer(tol, 4)
        self._reset()
        if shape is None or shape.isEmpty() or shape.type() != GEOM_POLYGON:
            return
        self._candidates = []
        self.switcher.hide()
        self.apply(self.features_in_geometry(shape), behavior)
        top = next((lyr for lyr in self._layers() if lyr.selectedFeatureCount()), None)
        if top is not None:
            self._follow(top)
        self._report()

    def features_in_geometry(self, shape):
        """{layer: [fid]} of features that intersect a polygon given in canvas CRS."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        result = {}
        for layer in self._layers():
            with suppress(Exception):
                poly = QgsGeometry(shape)
                poly.transform(_transform(canvas_crs, layer.crs()))
                request = QgsFeatureRequest().setFilterRect(poly.boundingBox()).setNoAttributes()
                engine = QgsGeometry.createGeometryEngine(poly.constGet())
                engine.prepareGeometry()
                result[layer] = [f.id() for f in layer.getFeatures(request)
                                 if not f.geometry().isEmpty() and engine.intersects(f.geometry().constGet())]
        return result

    def _report(self, overlapping=0):
        n, layers = selection_summary()
        text = "Selected: {} feature(s) in {} layer(s).".format(n, len(layers))
        if overlapping > 1:
            text += "  {} features here ({}/{}) - press N for the next one.".format(
                overlapping, self._cand_index + 1, overlapping)
        text += "  Shift = add, Ctrl = remove."
        _status(self.iface, text, 10000)

# -*- coding: utf-8 -*-
"""Drawing a region of interest on the map canvas.

One map tool, two shapes. **Rectangle**: press, drag, release. **Polygon**:
click each corner, then double-click, right-click or press Enter to close it;
Backspace takes the last corner back and Escape starts over.

The tool owns its gestures instead of borrowing a QGIS action, like every other
interactive tool in this plugin: an action's result has to be fished back out
of a layer, and in practice that did not reliably arrive. What the tool hands
back is a polygon in the *canvas* CRS through the `drawn` signal; converting it
to latitude and longitude is the caller's job, because only the caller knows
which CRS it needs.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

from contextlib import suppress

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

SHAPE_RECTANGLE = 'rectangle'
SHAPE_POLYGON = 'polygon'

#: The band colour while drawing and for the finished ROI.
ROI_COLOUR = QColor(214, 39, 40)


def _polygon_type():
    try:
        from qgis.core import Qgis

        return Qgis.GeometryType.Polygon
    except AttributeError:                  # pragma: no cover - QGIS < 3.30
        return QgsWkbTypes.GeometryType.PolygonGeometry


def make_band(canvas, fill_alpha=40):
    """A dashed red polygon band, the look every ROI has on this canvas."""
    band = QgsRubberBand(canvas, _polygon_type())
    band.setColor(ROI_COLOUR)
    band.setFillColor(QColor(ROI_COLOUR.red(), ROI_COLOUR.green(),
                             ROI_COLOUR.blue(), fill_alpha))
    band.setWidth(2)
    with suppress(AttributeError):          # pragma: no cover - older Qt
        band.setLineStyle(Qt.PenStyle.DashLine)
    return band


def drop_band(canvas, band):
    """Take a rubber band off the canvas for good.

    `reset()` only empties a band: the item stays in the canvas scene, which
    owns it, and would outlive the dialog that drew it.
    """
    if band is None:
        return
    with suppress(Exception):               # pragma: no cover - already gone
        band.reset()
    with suppress(Exception):               # pragma: no cover - already gone
        canvas.scene().removeItem(band)


class RoiMapTool(QgsMapTool):
    """Draw one rectangle or polygon, then hand it back and stay armed."""

    #: The finished shape, as a polygon QgsGeometry in the canvas CRS.
    drawn = pyqtSignal(object)
    message = pyqtSignal(str)

    #: A press-and-release closer than this is a click, not a rectangle.
    DRAG_THRESHOLD_PX = 4

    def __init__(self, canvas, shape=SHAPE_RECTANGLE):
        super().__init__(canvas)
        self.canvas = canvas
        self.shape = shape
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._band = make_band(canvas)
        self._points = []
        self._start = None
        self._press_pos = None
        #: Set by a double-click, whose trailing release must not start a
        #: new polygon with the corner it lands on.
        self._swallow_release = False

    def set_shape(self, shape):
        self._reset()
        self.shape = shape

    # ------------------------------------------------------------- events --

    def canvasPressEvent(self, event):
        if self.shape != SHAPE_RECTANGLE:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            self._reset()
            return
        self._press_pos = event.pos()
        self._start = self.toMapCoordinates(event.pos())

    def canvasMoveEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        if self.shape == SHAPE_RECTANGLE:
            if self._start is not None:
                self._show(self._rectangle(self._start, point))
        elif self._points:
            self._show(self._polygon(self._points + [point]))

    def canvasReleaseEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        if self.shape == SHAPE_RECTANGLE:
            if self._start is None or \
                    event.button() != Qt.MouseButton.LeftButton:
                return
            delta = event.pos() - self._press_pos
            start = self._start
            self._reset()
            if max(abs(delta.x()), abs(delta.y())) < self.DRAG_THRESHOLD_PX:
                self.message.emit('Drag across the map to draw the rectangle.')
                return
            self._finish(self._rectangle(start, point))
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._close_polygon()
        elif self._swallow_release:
            self._swallow_release = False
        elif event.button() == Qt.MouseButton.LeftButton:
            self._points.append(point)
            self._show(self._polygon(self._points))

    def canvasDoubleClickEvent(self, event):
        if self.shape != SHAPE_POLYGON:
            return
        # The double-click's first click already added this corner; the release
        # that follows this event is the second click's, and is swallowed.
        self._swallow_release = True
        self._close_polygon()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._reset()
        elif self.shape == SHAPE_POLYGON and key in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._close_polygon()
        elif self.shape == SHAPE_POLYGON and key in (
                Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self._points:
            self._points.pop()
            self._show(self._polygon(self._points))
        else:
            return
        event.accept()

    def deactivate(self):
        self._reset()
        super().deactivate()

    def remove(self):
        """Take the drawing band off the canvas; the tool is done with."""
        drop_band(self.canvas, self._band)
        self._band = None

    # ------------------------------------------------------------ helpers --

    def _close_polygon(self):
        points = self._dedupe(self._points)
        self._reset()
        if len(points) < 3:
            self.message.emit('A polygon needs at least three corners. Click '
                              'each corner, then double-click to finish.')
            return
        self._finish(self._polygon(points))

    def _finish(self, geometry):
        if geometry is None or geometry.isEmpty() or geometry.area() <= 0:
            self.message.emit('That shape has no area.')
            return
        if not geometry.isGeosValid():
            geometry = geometry.makeValid()
        self.drawn.emit(geometry)

    def _show(self, geometry):
        if self._band is None:
            return
        if geometry is None:
            self._band.reset(_polygon_type())
            return
        self._band.setToGeometry(geometry, None)

    def _reset(self):
        self._points = []
        self._start = None
        self._press_pos = None
        if self._band is not None:
            self._band.reset(_polygon_type())

    @staticmethod
    def _rectangle(start, end):
        return QgsGeometry.fromRect(QgsRectangle(start, end))

    @staticmethod
    def _polygon(points):
        if len(points) < 2:
            return None
        return QgsGeometry.fromPolygonXY([[QgsPointXY(p) for p in points]])

    @staticmethod
    def _dedupe(points):
        """Drop the repeated corner a double-click leaves behind."""
        kept = []
        for point in points:
            if not kept or point.distance(kept[-1]) > 0:
                kept.append(point)
        return kept

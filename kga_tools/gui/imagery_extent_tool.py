# -*- coding: utf-8 -*-
"""Draw-a-box map tool for the Imagery Downloader.

A QgsMapTool of its own (not an iface action): the rectangle press-drag-release
gestures come from RoiMapTool; this adds a `cancelled` signal for Escape so the
dialog can be brought back, and a blue band for the finished box.
"""

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsProject, QgsRectangle)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from .roi_tools import SHAPE_RECTANGLE, RoiMapTool, _polygon_type

BLUE = QColor(0x17, 0x45, 0x92)


class DrawBoxMapTool(RoiMapTool):
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas, SHAPE_RECTANGLE)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            super().keyPressEvent(event)
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


def make_box_band(canvas):
    band = QgsRubberBand(canvas, _polygon_type())
    band.setColor(BLUE)
    band.setFillColor(QColor(BLUE.red(), BLUE.green(), BLUE.blue(), 60))
    band.setWidth(2)
    return band


def show_box(canvas, band, bbox4326):
    """Paint `bbox4326` (w, s, e, n) on the canvas whatever its CRS."""
    if band is None:
        return
    if bbox4326 is None:
        band.reset(_polygon_type())
        return
    rect = QgsRectangle(*bbox4326)
    try:
        tr = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem('EPSG:4326'),
            canvas.mapSettings().destinationCrs(),
            QgsProject.instance().transformContext())
        rect = tr.transformBoundingBox(rect)
    except Exception:
        band.reset(_polygon_type())
        return
    band.setToGeometry(QgsGeometry.fromRect(rect), None)

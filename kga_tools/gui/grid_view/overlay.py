# -*- coding: utf-8 -*-
"""The canvas item that draws the grid lines (a couple of hundred lines at most: dynamic scaling keeps them
at least MIN_PX pixels apart, so drawing them directly each time is cheap)."""

import traceback

import numpy as np
from qgis.core import Qgis, QgsMessageLog
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QLineF, QRectF
from qgis.PyQt.QtGui import QColor, QPainter, QPen

from ...branding import LOG_TAG
from ..node_view.overlay import pixel_transform
from . import engine

#: Just above the map image (-10), below the Pro selection outline (-5) and the vertex markers (-4).
OVERLAY_Z = -7
MINOR = QColor(70, 70, 70, 90)
MAJOR = QColor(40, 40, 40, 160)


class GridOverlay(QgsMapCanvasItem):

    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller
        self.setZValue(OVERLAY_Z)

    def boundingRect(self):                 # noqa: N802
        return QRectF(0, 0, self.canvas.width(), self.canvas.height())

    def updatePosition(self):               # noqa: N802
        self.setPos(0, 0)

    def paint(self, painter, option=None, widget=None):
        try:
            self._paint(painter)
        except Exception:                    # never let a paint error reach Qt
            QgsMessageLog.logMessage('Grid failed to paint:\n{}'.format(traceback.format_exc()),
                                     LOG_TAG, Qgis.MessageLevel.Warning)

    def _paint(self, painter):
        settings = self.canvas.mapSettings()
        spacing = self.controller.spacing_map_units()
        mupp = settings.mapUnitsPerPixel()
        _factor, spacing = engine.effective_scale(spacing, mupp)
        if spacing <= 0 or mupp <= 0:
            return
        extent = settings.visibleExtent()
        corners = [(extent.xMinimum(), extent.yMinimum()), (extent.xMaximum(), extent.yMinimum()),
                   (extent.xMaximum(), extent.yMaximum()), (extent.xMinimum(), extent.yMaximum())]
        centre = ((extent.xMinimum() + extent.xMaximum()) / 2.0, (extent.yMinimum() + extent.yMaximum()) / 2.0)
        (ox, oy), (ax, ay), (bx, by) = pixel_transform(settings, centre)
        straight = abs(self.controller.rotation()) < 1e-9 and abs(settings.rotation()) < 1e-9
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, not straight)
        minor, major = [], []
        for p0, p1, k in engine.grid_lines(corners, spacing, self.controller.rotation()):
            if not len(k):
                continue
            q0, q1 = self._pixels(p0, centre, ox, oy, ax, ay, bx, by), self._pixels(p1, centre, ox, oy, ax, ay, bx, by)
            if straight:
                q0, q1 = np.floor(q0) + 0.5, np.floor(q1) + 0.5      # crisp one-pixel lines
            is_major = (k % engine.MAJOR_EVERY) == 0
            for (a, b), target in ((self._lines(q0, q1, ~is_major), minor), (self._lines(q0, q1, is_major), major)):
                target.extend(QLineF(float(x0), float(y0), float(x1), float(y1)) for x0, y0, x1, y1 in
                              np.column_stack([a, b]))
        for lines, colour in ((minor, MINOR), (major, MAJOR)):
            if lines:
                pen = QPen(colour, 0)
                painter.setPen(pen)
                painter.drawLines(lines)

    @staticmethod
    def _pixels(points, centre, ox, oy, ax, ay, bx, by):
        dx, dy = points[:, 0] - centre[0], points[:, 1] - centre[1]
        return np.column_stack([ox + dx * ax + dy * bx, oy + dx * ay + dy * by])

    @staticmethod
    def _lines(q0, q1, mask):
        return q0[mask], q1[mask]

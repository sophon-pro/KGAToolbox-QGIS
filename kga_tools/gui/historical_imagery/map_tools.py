# -*- coding: utf-8 -*-
"""Rectangle AOI drawing tool and helpers that turn canvas/layers into an AOI."""

import json

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                       QgsPointXY, QgsProject, QgsRectangle)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QColor

from ...core.historical_imagery.compat import polygon_geometry_type
from ...core.historical_imagery.models import AOI

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MAX_POLYGON_VERTICES = 500


def make_rubber_band(canvas, stroke="#e8590c", fill=(232, 89, 12, 40), width=2):
    rb = QgsRubberBand(canvas, polygon_geometry_type())
    rb.setStrokeColor(QColor(stroke))
    rb.setFillColor(QColor(*fill))
    rb.setWidth(width)
    return rb


class RectangleMapTool(QgsMapToolEmitPoint):
    """Click-and-drag (or click, click) to draw a rectangle; emits it in canvas CRS."""

    rectangleCreated = pyqtSignal(QgsRectangle)
    toolDeactivated = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rubber = make_rubber_band(canvas)
        self.start_point = None
        self.dragging = False

    def canvasPressEvent(self, event):  # noqa: N802
        self.start_point = event.mapPoint()
        self.dragging = True
        self._show(self.start_point, self.start_point)

    def canvasMoveEvent(self, event):  # noqa: N802
        if self.dragging and self.start_point is not None:
            self._show(self.start_point, event.mapPoint())

    def canvasReleaseEvent(self, event):  # noqa: N802
        if not self.dragging or self.start_point is None:
            return
        self.dragging = False
        end = event.mapPoint()
        rect = QgsRectangle(self.start_point, end)
        self.start_point = None
        self.rubber.reset(polygon_geometry_type())
        if rect.width() > 0 and rect.height() > 0:
            self.rectangleCreated.emit(rect)

    def _show(self, p1, p2):
        self.rubber.reset(polygon_geometry_type())
        for pt in (QgsPointXY(p1.x(), p1.y()), QgsPointXY(p2.x(), p1.y()),
                   QgsPointXY(p2.x(), p2.y()), QgsPointXY(p1.x(), p2.y())):
            self.rubber.addPoint(pt, False)
        self.rubber.closePoints(True)
        self.rubber.show()

    def deactivate(self):
        self.rubber.reset(polygon_geometry_type())
        super().deactivate()
        self.toolDeactivated.emit()


def _to_wgs84(geom, src_crs):
    if src_crs != WGS84:
        ct = QgsCoordinateTransform(src_crs, WGS84, QgsProject.instance())
        geom.transform(ct)
    return geom


def aoi_from_rectangle(rect, crs, source="extent"):
    """Canvas/drawn rectangle (in ``crs``) -> AOI in EPSG:4326."""
    if crs != WGS84:
        ct = QgsCoordinateTransform(crs, WGS84, QgsProject.instance())
        rect = ct.transformBoundingBox(rect)
    x0 = max(-180.0, rect.xMinimum())
    y0 = max(-85.0, rect.yMinimum())
    x1 = min(180.0, rect.xMaximum())
    y1 = min(85.0, rect.yMaximum())
    if x1 <= x0 or y1 <= y0:
        raise ValueError("The area of interest is empty or outside valid coordinates.")
    return AOI(bbox=(x0, y0, x1, y1), geometry=None, source=source)


def aoi_bbox_from_layer(layer, selected_only=False):
    """Bounding box of a polygon layer (or its selection) as an AOI without a shape - instant.

    The panel's area label and the size estimate only need the box. Building the real shape
    merges every feature, which takes seconds on a detailed province or district layer.
    """
    if layer is None:
        raise ValueError("Choose a polygon layer.")
    box = layer.boundingBoxOfSelected() if selected_only else layer.extent()
    if selected_only and layer.selectedFeatureCount() == 0:
        raise ValueError("The polygon layer has no selected features.")
    if box.isNull() or box.isEmpty():
        raise ValueError("The polygon layer has no features.")
    if layer.crs() != WGS84:
        box = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance()).transformBoundingBox(box)
    return AOI(bbox=(box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()), source="polygon")


def aoi_from_layer(layer, selected_only=False):
    """Union of polygon features -> AOI (geometry kept for STAC 'intersects' and clipping)."""
    if layer is None:
        raise ValueError("Choose a polygon layer.")
    feats = layer.getSelectedFeatures() if selected_only else layer.getFeatures()
    # Transform and thin each feature on its own: merging first would run the union on
    # hundreds of thousands of vertices.
    to_wgs84 = None
    if layer.crs() != WGS84:
        to_wgs84 = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
    geoms = []
    for f in feats:
        if not f.hasGeometry():
            continue
        g = QgsGeometry(f.geometry())
        if to_wgs84 is not None:
            g.transform(to_wgs84)
        tolerance = 0.0001
        while _vertex_count(g) > MAX_POLYGON_VERTICES * 2 and tolerance < 0.1:
            thin = g.simplify(tolerance)
            if thin and not thin.isEmpty():
                g = thin
            tolerance *= 3
        geoms.append(g)
    if not geoms:
        raise ValueError("The polygon layer has no {}features.".format(
            "selected " if selected_only else ""))
    geom = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]
    try:  # APIs and cutlines expect 2-D coordinates
        geom.get().dropZValue()
        geom.get().dropMValue()
    except AttributeError:
        pass
    if not geom.isGeosValid():
        geom = geom.makeValid()
    # Keep API payloads small: simplify very detailed boundaries.
    tolerance = 0.0001
    while _vertex_count(geom) > MAX_POLYGON_VERTICES and tolerance < 0.1:
        simplified = geom.simplify(tolerance)
        if simplified and not simplified.isEmpty():
            geom = simplified
        tolerance *= 3
    if _vertex_count(geom) > MAX_POLYGON_VERTICES:
        geom = geom.convexHull()
    bb = geom.boundingBox()
    return AOI(bbox=(bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum()),
               geometry=json.loads(geom.asJson(6)), source="polygon")


def _vertex_count(geom):
    try:
        return geom.constGet().nCoordinates()
    except AttributeError:
        return len(list(geom.vertices()))


def aoi_to_canvas_geometry(aoi, canvas):
    """AOI -> QgsGeometry in the canvas CRS (for drawing the AOI rubber band)."""
    if aoi.geometry:
        geom = QgsGeometry.fromWkt(geojson_to_wkt(aoi.geometry))
    else:
        geom = QgsGeometry.fromRect(QgsRectangle(*aoi.bbox))
    dest = canvas.mapSettings().destinationCrs()
    if dest != WGS84:
        geom.transform(QgsCoordinateTransform(WGS84, dest, QgsProject.instance()))
    return geom


def footprint_to_canvas_geometry(footprint, canvas):
    if not footprint:
        return None
    geom = QgsGeometry.fromWkt(geojson_to_wkt(footprint))
    if geom.isNull():
        return None
    dest = canvas.mapSettings().destinationCrs()
    if dest != WGS84:
        geom.transform(QgsCoordinateTransform(WGS84, dest, QgsProject.instance()))
    return geom


def geojson_to_wkt(geom):
    try:
        from osgeo import ogr
        g = ogr.CreateGeometryFromJson(json.dumps(geom))
        return g.ExportToWkt() if g else ""
    except ImportError:
        return ""

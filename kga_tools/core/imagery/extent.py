# -*- coding: utf-8 -*-
"""Turn what the user picked into a lon/lat box and, optionally, a mask.

Every extent mode resolves to an `Area`: a (west, south, east, north) box in
EPSG:4326 plus, for the clip mode, the polygon in EPSG:4326 as WKT.
"""

from dataclasses import dataclass

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsDistanceArea, QgsGeometry, QgsProject, QgsRectangle,
                       QgsWkbTypes)

WGS84 = 'EPSG:4326'


def _polygon_type():
    """The polygon geometry-type value `QgsWkbTypes.geometryType` returns.

    ``Qgis.GeometryType`` is the spelling that exists on QGIS 4; the old
    ``QgsWkbTypes.GeometryType`` enum was removed with it.
    """
    try:
        from qgis.core import Qgis

        return Qgis.GeometryType.Polygon
    except AttributeError:                  # pragma: no cover - QGIS < 3.30
        return QgsWkbTypes.GeometryType.PolygonGeometry


class AreaError(Exception):
    """A sentence for the details panel."""


@dataclass
class Area:
    bbox: tuple
    mask_wkt: str = None
    label: str = ''


def _wgs84():
    return QgsCoordinateReferenceSystem(WGS84)


def _transform(crs):
    if crs is None or not crs.isValid():
        raise AreaError('The layer has no coordinate system.')
    return QgsCoordinateTransform(crs, _wgs84(),
                                  QgsProject.instance().transformContext())


def _rect_bbox(rect, crs):
    if rect is None or rect.isNull() or rect.isEmpty():
        raise AreaError('The extent is empty.')
    if crs.authid() != WGS84:
        try:
            rect = _transform(crs).transformBoundingBox(rect)
        except Exception:
            raise AreaError('The extent cannot be converted to lon/lat.')
    return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())


def rect_area(rect, crs, label=''):
    return Area(_rect_bbox(rect, crs), None, label)


def canvas_area(canvas):
    if canvas is None:
        raise AreaError('There is no map canvas.')
    return rect_area(canvas.extent(), canvas.mapSettings().destinationCrs(),
                     'Map canvas')


def layer_area(layer):
    if layer is None:
        raise AreaError('Choose a layer.')
    return rect_area(layer.extent(), layer.crs(), layer.name())


def polygon_area(layer, selected_only=False):
    if layer is None:
        raise AreaError('Choose a polygon layer.')
    if selected_only:
        features = list(layer.getSelectedFeatures())
        if not features:
            raise AreaError('No features are selected in "%s".' % layer.name())
    else:
        features = list(layer.getFeatures())
    geoms = [f.geometry() for f in features
             if f.hasGeometry() and not f.geometry().isEmpty()]
    if not geoms:
        raise AreaError('"%s" has no polygon geometry.' % layer.name())
    tr = _transform(layer.crs()) if layer.crs().authid() != WGS84 else None
    fixed = []
    for geom in geoms:
        geom = QgsGeometry(geom)
        if tr is not None:
            try:
                geom.transform(tr)
            except Exception:
                raise AreaError('The polygon cannot be converted to lon/lat.')
        if not geom.isGeosValid():
            geom = geom.makeValid()
            if geom is None or geom.isEmpty():
                raise AreaError('A polygon has invalid geometry that could '
                                'not be repaired.')
        fixed.append(geom)
    merged = QgsGeometry.unaryUnion(fixed) if len(fixed) > 1 else fixed[0]
    if merged is not None and not merged.isGeosValid():
        merged = merged.makeValid()
    if merged is None or merged.isEmpty():
        raise AreaError('The polygon could not be repaired.')
    # makeValid can return a collection; keep only the polygon parts.
    polygon = _polygon_type()
    if QgsWkbTypes.geometryType(merged.wkbType()) != polygon:
        parts = [p for p in merged.asGeometryCollection()
                 if QgsWkbTypes.geometryType(p.wkbType()) == polygon]
        if not parts:
            raise AreaError('The layer does not contain polygons.')
        merged = QgsGeometry.unaryUnion(parts)
    if merged.area() <= 0:
        raise AreaError('The polygon has no area.')
    box = merged.boundingBox()
    return Area((box.xMinimum(), box.yMinimum(), box.xMaximum(),
                 box.yMaximum()), merged.asWkt(9), layer.name())


def geodesic_area_km2(area):
    """Area in km2: the polygon when there is one, else the box."""
    calc = QgsDistanceArea()
    calc.setSourceCrs(_wgs84(), QgsProject.instance().transformContext())
    ellipsoid = QgsProject.instance().ellipsoid()
    calc.setEllipsoid(ellipsoid if ellipsoid and ellipsoid != 'NONE'
                      else 'WGS84')
    if area.mask_wkt:
        geom = QgsGeometry.fromWkt(area.mask_wkt)
    else:
        w, s, e, n = area.bbox
        geom = QgsGeometry.fromRect(QgsRectangle(w, s, e, n))
    return abs(calc.measureArea(geom)) / 1e6


def bounds_transformer(output_crs):
    """A callable (bbox4326 -> bounds in output_crs), or None for EPSG:3857."""
    if output_crs.authid() in ('EPSG:3857', 'EPSG:900913'):
        return None
    tr = QgsCoordinateTransform(_wgs84(), output_crs,
                                QgsProject.instance().transformContext())

    def fn(bbox):
        try:
            r = tr.transformBoundingBox(QgsRectangle(*bbox))
        except Exception:
            return None
        return (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
    return fn

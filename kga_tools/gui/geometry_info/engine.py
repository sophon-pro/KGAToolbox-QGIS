# -*- coding: utf-8 -*-
"""Measurements of the selected features: length (m) of lines, perimeter (m) and area (m2) of polygons.

Measured on the project's ellipsoid (planar when the project has none and the layer CRS is projected), so the
numbers match the Pro-style "geodesic" values rather than the distorted map ones.
"""

from qgis.core import (
    Qgis,
    QgsDistanceArea,
    QgsFeatureRequest,
    QgsProject,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)

LINE = QgsWkbTypes.GeometryType.LineGeometry
POLYGON = QgsWkbTypes.GeometryType.PolygonGeometry


def selected_layer(project=None):
    """The one vector layer that has selected features, or None when there are none or more than one layer."""
    project = project or QgsProject.instance()
    found = None
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        if layer.selectedFeatureCount() > 0:
            if found is not None:
                return None
            found = layer
    return found


def _measurer(layer, project):
    area = QgsDistanceArea()
    area.setSourceCrs(layer.crs(), project.transformContext())
    ellipsoid = project.ellipsoid()
    if (not ellipsoid or ellipsoid == 'NONE') and layer.crs().isGeographic():
        ellipsoid = layer.crs().ellipsoidAcronym() or 'EPSG:7030'      # degrees are no distance: use the CRS's own
    area.setEllipsoid(ellipsoid or 'NONE')
    return area


def _factor(unit_from, unit_to):
    try:
        return QgsUnitTypes.fromUnitToUnitFactor(unit_from, unit_to)
    except Exception:
        return 1.0


def vertex_count(geometry, polygon):
    """Vertices of a geometry; a polygon ring's closing vertex is the same vertex as its first, so it counts once."""
    total = geometry.constGet().nCoordinates()
    if polygon:
        for part in geometry.constParts():
            total -= 1 + part.numInteriorRings()
    return max(total, 0)


def measure(layer, project=None):
    """``(kind, count, length_m, area_m2, vertices)`` for the selected features of *layer* (kind 'line' / 'polygon'),
    or None for point layers and empty selections. For polygons *length_m* is the perimeter."""
    project = project or QgsProject.instance()
    kind = layer.geometryType()
    if kind not in (LINE, POLYGON) or layer.selectedFeatureCount() == 0:
        return None
    da = _measurer(layer, project)
    to_m = _factor(da.lengthUnits(), Qgis.DistanceUnit.Meters)
    to_m2 = _factor(da.areaUnits(), Qgis.AreaUnit.SquareMeters)
    request = QgsFeatureRequest().setFilterFids(list(layer.selectedFeatureIds()))
    request.setNoAttributes()
    length = area = 0.0
    count = vertices = 0
    for feature in layer.getFeatures(request):
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            continue
        count += 1
        vertices += vertex_count(geometry, kind == POLYGON)
        if kind == LINE:
            length += da.measureLength(geometry)
        else:
            length += da.measurePerimeter(geometry)
            area += da.measureArea(geometry)
    if count == 0:
        return None
    return ('line' if kind == LINE else 'polygon'), count, length * to_m, area * to_m2, vertices


def _number(value):
    return '{:,.2f}'.format(value)


def describe(result):
    """The status-bar text and tooltip for a `measure` result."""
    kind, count, length, area, vertices = result
    many = ' ({} features)'.format(count) if count > 1 else ''
    if kind == 'line':
        text = 'Length: {} m'.format(_number(length))
        tip = 'Length and number of vertices of the selected line feature{}'.format('s (total)' if count > 1 else '')
    else:
        text = 'Perimeter: {} m   Area: {} m²'.format(_number(length), _number(area))
        tip = 'Perimeter, area and number of vertices of the selected polygon feature{}'.format('s (total)' if count > 1 else '')
    text += '   Vertices: {:,}'.format(vertices)
    return text + many, tip

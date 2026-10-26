"""
Picking and writing features for the Modify Features map tools.

pick_features() finds the features under a click in every layer shown on the map; commit() writes new
geometries (and new features) back, converting from the map's CRS to each layer's CRS, as one undoable
edit command per layer.
"""

from contextlib import suppress

from qgis.core import (
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayerUtils,
)

from ..selection import CLICK_TOLERANCE_PX, _transform, all_vector_layers, map_layers


def pick_features(canvas, point, geometry_types=None, editable_only=False):
    """
    [(layer, fid, geometry in the map's CRS)] within a few pixels of `point`, nearest first. Layers that are
    not shown on the map are never picked.
    """
    shown = map_layers(canvas)
    canvas_crs = canvas.mapSettings().destinationCrs()
    tol = CLICK_TOLERANCE_PX * canvas.mapUnitsPerPixel()
    p = QgsPointXY(point)
    rect = QgsRectangle(p.x() - tol, p.y() - tol, p.x() + tol, p.y() + tol)
    found = []
    for layer in all_vector_layers():
        if (shown is not None and layer.id() not in shown) or not layer.isSpatial():
            continue
        if editable_only and not layer.isEditable():
            continue
        if geometry_types is not None and layer.geometryType() not in geometry_types:
            continue
        with suppress(Exception):
            same = layer.crs() == canvas_crs
            to_layer = None if same else _transform(canvas_crs, layer.crs())
            to_map = None if same else _transform(layer.crs(), canvas_crs)
            lrect = rect if same else to_layer.transformBoundingBox(rect)
            lpt = QgsGeometry.fromPointXY(p if same else to_layer.transform(p))
            ltol = max(lrect.width(), lrect.height()) / 2.0
            request = QgsFeatureRequest().setFilterRect(lrect).setNoAttributes()
            for f in layer.getFeatures(request):
                g = f.geometry()
                if g.isEmpty():
                    continue
                d = g.distance(lpt)
                if d <= ltol:
                    gm = QgsGeometry(g)
                    if to_map is not None:
                        gm.transform(to_map)
                    found.append((d, layer, f.id(), gm))
    found.sort(key=lambda item: item[0])
    return [(layer, fid, g) for _d, layer, fid, g in found]


def _to_layer(canvas, layer, geom):
    g = QgsGeometry(geom)
    canvas_crs = canvas.mapSettings().destinationCrs()
    if layer.crs() != canvas_crs:
        g.transform(_transform(canvas_crs, layer.crs()))
    return g


def commit(canvas, changes, adds, text, created=None):
    """
    Write geometries and add features, one edit command per layer so one Ctrl+Z undoes the layer's part.
    changes: [(layer, fid, geometry in map CRS)]; adds: [(layer, geometry in map CRS)] or
    [(layer, geometry, {field index: value})] (new features get the layer's default values, then those attributes).
    `created`, if given, collects (layer, feature id) of the new features. Returns the names of the layers where
    something failed.
    """
    layers = {}
    for layer, fid, g in changes:
        layers.setdefault(layer.id(), (layer, [], []))[1].append((fid, g))
    for item in adds:
        layer, g = item[0], item[1]
        layers.setdefault(layer.id(), (layer, [], []))[2].append((g, item[2] if len(item) > 2 else None))
    failed = []
    for layer, edits, new in layers.values():
        layer.beginEditCommand(text)
        ok = all(layer.changeGeometry(fid, _to_layer(canvas, layer, g)) for fid, g in edits)
        made = []
        if ok:
            for g, attrs in new:
                feature = QgsVectorLayerUtils.createFeature(layer, _to_layer(canvas, layer, g), attrs or {})
                ok = layer.addFeature(feature) and ok
                made.append(feature.id())
        if ok:
            layer.endEditCommand()
            if created is not None:
                created.extend((layer, fid) for fid in made)
        else:
            layer.destroyEditCommand()
            failed.append(layer.name())
        layer.triggerRepaint()
    return failed

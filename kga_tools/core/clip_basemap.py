# -*- coding: utf-8 -*-
"""Clip Basemap by Boundary: the logic behind the window.

A tile basemap cannot be cut by a vector layer in the canvas, so the clip is a
*mask*: a copy of the boundary drawn with an inverted-polygon renderer, filled
with the project's map background colour, and placed directly above the
basemap in the layer tree. Everything the basemap would show outside the boundary is covered; layers above the mask are
untouched, and nothing is downloaded or written to disk.
"""

from qgis.core import (QgsFeature, QgsFillSymbol, QgsGeometry,
                       QgsInvertedPolygonRenderer, QgsProject,
                       QgsSingleSymbolRenderer, QgsVectorLayer)
from qgis.PyQt.QtGui import QColor

from . import data_sources as DS
from . import layer_loader as LL

#: Custom property stamped on a mask layer; its value is the basemap layer id.
MASK_PROPERTY = 'kga/clip_mask_for'


class ClipError(Exception):
    pass


def _fill_symbol(project):
    """Solid fill in the project's map background colour (white by default)."""
    color = QColor(project.backgroundColor())
    color.setAlpha(255)
    return QgsFillSymbol.createSimple({
        'color': '{},{},{},255'.format(color.red(), color.green(),
                                       color.blue()),
        'style': 'solid', 'outline_style': 'no'})


def _mask_layer(boundary, selected_only, name, project):
    """A style-free copy of the boundary geometries, ready to be inverted."""
    if selected_only and boundary.selectedFeatureCount() == 0:
        raise ClipError('No features are selected on the boundary layer.')
    if boundary.providerType() != 'memory' and not selected_only:
        # Same data source, so the mask follows edits to the boundary.
        mask = boundary.clone()
        mask.setName(name)
        mask.setSubsetString(boundary.subsetString())
    else:
        uri = 'MultiPolygon?crs={}'.format(boundary.crs().authid()
                                          or boundary.crs().toWkt())
        mask = QgsVectorLayer(uri, name, 'memory')
        features = (boundary.selectedFeatures() if selected_only
                    else boundary.getFeatures())
        copies = []
        for feature in features:
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            geom.convertToMultiType()
            new = QgsFeature()
            new.setGeometry(geom)
            copies.append(new)
        mask.dataProvider().addFeatures(copies)
        mask.updateExtents()
    if not mask.isValid():
        raise ClipError('The boundary layer could not be used as a mask.')
    mask.setRenderer(QgsInvertedPolygonRenderer(
        QgsSingleSymbolRenderer(_fill_symbol(project))))
    mask.setOpacity(1.0)
    return mask


def _geometry_mask(geometry, crs, name, project):
    """A one-feature mask from a drawn shape or an extent, in its own CRS."""
    if geometry is None or geometry.isEmpty():
        raise ClipError('The area is empty.')
    mask = QgsVectorLayer('MultiPolygon?crs={}'.format(
        crs.authid() or crs.toWkt()), name, 'memory')
    geom = QgsGeometry(geometry)
    geom.convertToMultiType()
    feature = QgsFeature()
    feature.setGeometry(geom)
    mask.dataProvider().addFeatures([feature])
    mask.updateExtents()
    mask.setRenderer(QgsInvertedPolygonRenderer(
        QgsSingleSymbolRenderer(_fill_symbol(project))))
    return mask


def find_or_add_basemap(basemap_id, project=None):
    """The project's layer for a preset basemap, adding it at the bottom."""
    project = project or QgsProject.instance()
    basemap = DS.basemap_by_id(basemap_id)
    if basemap is None:
        raise ClipError('Unknown basemap.')
    uri = LL.xyz_uri(basemap.url, basemap.zmin, basemap.zmax)
    for layer in project.mapLayers().values():
        if layer.source() == uri:
            return layer
    try:
        layer = LL.build_basemap(basemap.url, LL.unique_name(
            project, basemap.label), basemap.zmin, basemap.zmax)
    except LL.LoadError as exc:
        raise ClipError(str(exc))
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    root.insertLayer(len(root.children()), layer)
    return layer


def remove_masks(basemap_layer, project=None):
    """Remove masks made earlier for this basemap. Returns how many."""
    project = project or QgsProject.instance()
    stale = [lyr.id() for lyr in project.mapLayers().values()
             if lyr.customProperty(MASK_PROPERTY) == basemap_layer.id()]
    if stale:
        project.removeMapLayers(stale)
    return len(stale)


def clip(basemap_layer, boundary=None, selected_only=False, project=None,
         geometry=None, crs=None):
    """Mask `basemap_layer` outside the boundary; returns the mask layer.

    The boundary is a polygon layer, or a `geometry` in `crs` (a drawn shape
    or an extent).
    """
    project = project or QgsProject.instance()
    name = '{} - clip mask'.format(basemap_layer.name())
    if geometry is not None:
        mask = _geometry_mask(geometry, crs, name, project)
    else:
        if boundary is None or boundary.geometryType() != 2:  # 2 = polygon
            raise ClipError('Choose a polygon layer as the boundary.')
        mask = _mask_layer(boundary, selected_only, name, project)
    remove_masks(basemap_layer, project)
    mask.setCustomProperty(MASK_PROPERTY, basemap_layer.id())
    project.addMapLayer(mask, False)
    root = project.layerTreeRoot()
    node = root.findLayer(basemap_layer.id())
    if node is None:                    # basemap not in the tree: add it
        root.insertLayer(len(root.children()), basemap_layer)
        node = root.findLayer(basemap_layer.id())
    parent = node.parent()
    if parent is root or parent.name() != basemap_layer.name():
        # Group the basemap with its mask, named like the basemap, at the
        # basemap's place in the panel. A re-clip finds the group and reuses it.
        index = parent.children().index(node)
        group = parent.insertGroup(index, basemap_layer.name())
        group.insertLayer(0, basemap_layer)
        parent.removeChildNode(node)
        parent = group
        node = group.findLayer(basemap_layer.id())
    parent.insertLayer(parent.children().index(node), mask)
    parent.setExpanded(False)
    return mask

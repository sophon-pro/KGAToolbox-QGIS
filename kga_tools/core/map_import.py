# -*- coding: utf-8 -*-
"""Bring a map from another QGIS project into the open project.

A "map" is a top-level group of the layer tree that Map Tabs marks with custom
properties. A project that was never organised into maps counts as one map
holding everything it has, named after its file. The layers are copied (the
other project is only read), keep their data sources and styles, and land in a
new group of the open project.
"""

import os
import uuid

from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject

from . import layout_import

PROP_MAP = 'maptabs/is_map'
PROP_ID = 'maptabs/id'
PROP_THEME = 'maptabs/theme'
PROP_OPEN = 'maptabs/open'


def _is_map(node):
    return (isinstance(node, QgsLayerTreeGroup)
            and str(node.customProperty(PROP_MAP, '')).lower() in ('1', 'true'))


def read_project(path):
    """The project at `path` as a separate QgsProject, or None if unreadable."""
    if not path or not os.path.isfile(path):
        return None
    project = QgsProject()
    return project if project.read(path) else None


def list_maps(project, path=''):
    """[(name, group)] of the maps in `project`; the whole tree if it has none."""
    root = project.layerTreeRoot()
    groups = [(node.name(), node) for node in root.children() if _is_map(node)]
    if groups:
        return groups
    if not root.children():
        return []
    name = os.path.splitext(os.path.basename(path))[0] or 'Map'
    whole = QgsLayerTreeGroup(name)
    for node in root.children():
        whole.addChildNode(node.clone())
    return [(name, whole)]


def import_group(source, group, destination, name, database='', cache=None):
    """Copy `group` (from project `source`) into `destination`.

    Vector layers are written to new tables of `database` (when there is one)
    with their styles; other layers are copied as they are. `cache` maps a
    source layer id to (copy, table) so one layer is copied once per import.
    Returns (the new, not yet inserted, map group or None when none of its
    layers could be copied, [(layer name, what happened)]).
    """
    cache = {} if cache is None else cache
    notes = []
    clone = group.clone()
    copied = 0
    for node in list(clone.findLayers()):
        layer = source.mapLayer(node.layerId())
        parent = node.parent()
        if parent is None:
            continue
        new_layer = None
        if layer is not None:
            if layer.id() not in cache:
                taken = [table for _lyr, table in cache.values() if table]
                made, note, table = layout_import.bring_layer(
                    destination, layer, database, taken, in_tree=False)
                notes.append((layer.name(), note))
                if made is not None:
                    cache[layer.id()] = (made, table)
            if layer.id() in cache:
                new_layer = cache[layer.id()][0]
        if new_layer is None:
            parent.removeChildNode(node)
            continue
        replacement = QgsLayerTreeLayer(new_layer)
        replacement.setName(node.name())
        replacement.setItemVisibilityChecked(node.itemVisibilityChecked())
        replacement.setExpanded(node.isExpanded())
        position = parent.children().index(node)
        parent.insertChildNode(position, replacement)
        parent.removeChildNode(node)
        copied += 1
    if not copied:
        return None, notes
    clone.setName(name)
    clone.setCustomProperty(PROP_MAP, '1')
    clone.setCustomProperty(PROP_ID, uuid.uuid4().hex)
    clone.removeCustomProperty(PROP_THEME)
    clone.removeCustomProperty(PROP_OPEN)
    return clone, notes


def read_shortcut(path):
    """(project file, map id, map name) from a `.kgamap` shortcut."""
    from . import map_shortcuts
    name, project_file = map_shortcuts.read(path)
    return project_file, map_shortcuts.read_id(path), name

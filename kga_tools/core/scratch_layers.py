# -*- coding: utf-8 -*-
"""Scratch (memory) layers: find them in the project and keep them in a GeoPackage.

A scratch layer lives only in QGIS's memory. Closing the project, opening
another one or quitting QGIS throws its features away. `save_layers` writes
each chosen layer into a GeoPackage table and points the layer at that table
(`setDataSource`, so the style, the name and its place in the layer tree are
kept), so the project that is saved afterwards refers to real data.

No widgets here; `gui/scratch_close_guard.py` asks the user and calls this.
"""

import os
from contextlib import suppress

from qgis.core import (
    QgsDataProvider,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import output_defaults as od

SKIP_CHECK_PROPERTY = 'skipMemoryLayersCheck'     # QGIS's own "do not warn" flag


def is_scratch(layer):
    return isinstance(layer, QgsVectorLayer) and layer.isValid() \
        and layer.providerType() == 'memory'


def scratch_layers(project=None):
    """The project's memory layers, in layer-tree order where known."""
    project = project or QgsProject.instance()
    found = [layer for layer in project.mapLayers().values() if is_scratch(layer)]
    order = {}
    with suppress(Exception):
        root = project.layerTreeRoot()
        for index, node in enumerate(root.findLayers()):
            order[node.layerId()] = index
    found.sort(key=lambda layer: order.get(layer.id(), len(order)))
    return found


def has_content(layer):
    """True when the layer holds features (an empty scratch layer loses little)."""
    return layer.featureCount() > 0


def mark_discarded(layers):
    """Tell QGIS's own memory-layer warning that the user already decided."""
    for layer in layers:
        with suppress(Exception):
            layer.setCustomProperty(SKIP_CHECK_PROPERTY, 1)


class Result(object):
    """What happened to each layer: `saved` {layer_id: (table, path)}, `failed` {name: why}."""

    def __init__(self, path):
        self.path = path
        self.saved = {}
        self.failed = {}

    @property
    def ok(self):
        return not self.failed


def save_layers(layers, path, project=None):
    """Write `layers` into the GeoPackage `path` and repoint each one at its table."""
    project = project or QgsProject.instance()
    result = Result(path)
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    taken = set()
    for layer in layers:
        name = layer.name()
        try:
            _commit(layer)
            table = od.unique_table(path, od.safe_stem(name, 'layer'), taken)
            taken.add(table)
            _write(layer, path, table, project)
            _repoint(layer, path, table)
            result.saved[layer.id()] = (table, path)
        except Exception as error:      # noqa: BLE001 - one layer must not stop the rest
            result.failed[name] = str(error) or error.__class__.__name__
    return result


def _commit(layer):
    """A memory layer being edited has to be committed first or its edits are lost."""
    if not layer.isEditable():
        return
    if not layer.commitChanges():
        raise RuntimeError('Its edits could not be saved: {}'.format(
            '; '.join(layer.commitErrors())))


def _write(layer, path, table, project):
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.layerName = table
    options.fileEncoding = 'UTF-8'
    options.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        if os.path.isfile(path)
        else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    outcome = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, project.transformContext(), options)
    if outcome[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(outcome[1] or 'The GeoPackage could not be written.')


def _repoint(layer, path, table):
    """Point the layer at its new table; the style and name stay."""
    uri = '{}|layername={}'.format(path, table)
    name = layer.name()
    options = QgsDataProvider.ProviderOptions()
    layer.setDataSource(uri, name, 'ogr', options)
    if not layer.isValid():
        raise RuntimeError('The saved table could not be opened again.')


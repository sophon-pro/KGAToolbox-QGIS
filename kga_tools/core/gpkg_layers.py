# -*- coding: utf-8 -*-
"""Small GeoPackage layer operations the Catalog needs (no widgets here)."""

import os

from qgis.core import QgsProject, QgsProviderRegistry

SUFFIX = '.gpkg'


class LayerError(Exception):
    """The layer could not be deleted; the message is for the user."""


def split_uri(uri):
    """(gpkg path, layer name) of an OGR layer uri, or None when it is not one."""
    parts = (uri or '').split('|')
    path = parts[0]
    if not path.lower().endswith(SUFFIX):
        return None
    name = next((p[len('layername='):] for p in parts[1:]
                 if p.startswith('layername=')), '')
    if not name:
        return None
    return path, name


def layers_using(path, name):
    """The project's layers that read table `name` of the GeoPackage `path`."""
    wanted = os.path.normcase(os.path.normpath(path))
    found = []
    for layer in QgsProject.instance().mapLayers().values():
        parts = split_uri(layer.source())
        if parts is None:
            continue
        if os.path.normcase(os.path.normpath(parts[0])) == wanted \
                and parts[1] == name:
            found.append(layer)
    return found


def delete_layer(path, name):
    """Drop table `name` from the GeoPackage `path`.

    Refuses while the project has the layer open: removing a table under a
    live layer leaves it pointing at nothing.
    """
    open_layers = layers_using(path, name)
    if open_layers:
        raise LayerError(
            'The layer "{}" is open in the project ({}). Remove it from the '
            'project first.'.format(name, ', '.join(
                sorted({layer.name() for layer in open_layers}))))
    try:
        metadata = QgsProviderRegistry.instance().providerMetadata('ogr')
        connection = metadata.createConnection(path, {})
        connection.dropVectorTable('', name)
    except Exception as error:
        raise LayerError('Could not delete "{}": {}'.format(name, error))

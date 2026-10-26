# -*- coding: utf-8 -*-
"""Shortcuts to a project's maps, kept in the workspace's Maps folder.

A map is a group in the project file, so the Maps folder of a workspace would
be empty on disk. For each map this keeps a small `<map name>.kgamap` file
there (JSON: map name, map id and project file), which the Catalog and the
Browser list and open like a shortcut. It is the same scheme as the layouts'
`.kgalayout` files and shares their code.
"""

from . import layout_shortcuts as _shared

EXTENSION = '.kgamap'
KEY = 'map'


def file_name(map_name, taken=()):
    return _shared.file_name(map_name, taken, EXTENSION, 'Map')


def read(path):
    """(map name, project file) from a shortcut, or ('', '') if unreadable."""
    return _shared.read(path, KEY)


def read_id(path):
    return str(_shared.read_data(path).get('id', ''))


def read_layers(path):
    """The layer names stored in a shortcut, in tree order."""
    layers = _shared.read_data(path).get('layers', [])
    return [str(name) for name in layers] if isinstance(layers, list) else []


def sync(folder, project_file, maps):
    """One shortcut per map; True if anything changed.

    `maps` is [(id, name)] or [(id, name, [layer names])]; the layer names are
    stored so the Catalog can list what a map uses without opening the project.
    """
    entries = [(m[0], m[1], list(m[2]) if len(m) > 2 else []) for m in maps]
    return _shared.sync(
        folder, project_file, [name for _id, name, _layers in entries],
        EXTENSION, KEY,
        {name: {'id': mid, 'layers': layers} for mid, name, layers in entries},
        'Map')

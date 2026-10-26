# -*- coding: utf-8 -*-
"""Where a tool puts its output when the user has not said otherwise.

ArcGIS Pro sends every tool's output to the project's default geodatabase, and
every file or folder to the project folder, with a name made from the tool and
its input. This module is the QGIS side of that: it knows the current project's
default database and folder (the catalog controller tells it), and builds
unused names for them. Nothing here touches widgets; the dialogs and the
Processing hook ask it for a value and put the value in the field, where the
user can still change it.

With no project (the catalog is not loaded) every function answers '' / None,
and callers leave their own defaults alone.
"""

import os
import re

from . import kga_settings

_workspace = None

_UNSAFE = re.compile(r'[^0-9A-Za-z_]+')
_MAX_STEM = 60


def set_workspace(workspace):
    """The catalog controller calls this whenever the current project changes."""
    global _workspace
    _workspace = workspace


def workspace():
    return _workspace


def project_folder():
    """The default folder for files, or '' without a project."""
    if _workspace is None or not kga_settings.output_to_project():
        return ''
    folder = _workspace.default_folder
    return folder if folder and os.path.isdir(folder) else ''


def project_database():
    """The default database (a .gpkg), or '' without a project."""
    if _workspace is None or not kga_settings.output_to_project():
        return ''
    database = _workspace.default_database
    return database if database and os.path.isfile(database) else ''


# ------------------------------------------------------------------- names

def safe_stem(text, fallback='Output'):
    """`text` as a name that is safe as a file stem and as a table name."""
    stem = _UNSAFE.sub('_', (text or '').strip()).strip('_')
    stem = re.sub(r'_{2,}', '_', stem)[:_MAX_STEM].strip('_')
    if not stem:
        return fallback
    if stem[0].isdigit():
        stem = '_' + stem
    return stem


def make_stem(tool, source=''):
    """`Tool_Source`, the way Pro names an output (e.g. Buffer_Roads)."""
    tool = safe_stem(tool)
    source = safe_stem(source, '') if source else ''
    if source and source.lower() != tool.lower():
        return safe_stem('{}_{}'.format(tool, source))
    return tool


def _table_names(database):
    """The layer names already in `database`, lower-cased; None if unreadable."""
    try:
        from osgeo import gdal, ogr
        gdal.UseExceptions()
        source = ogr.Open(database)
        if source is None:
            return None
        names = {source.GetLayerByIndex(i).GetName().lower()
                 for i in range(source.GetLayerCount())}
        source = None
        return names
    except Exception:       # noqa: BLE001 - a locked / odd file just means "no check"
        return None


def unique_table(database, stem, taken=()):
    """`stem`, or `stem_1`, `stem_2`... whichever is not in `database` yet."""
    used = set(name.lower() for name in taken)
    used |= (_table_names(database) or set())
    name, n = stem, 0
    while name.lower() in used:
        n += 1
        name = '{}_{}'.format(stem, n)
    return name


def unique_file(folder, stem, extension, taken=()):
    """A path in `folder` that does not exist yet (`stem.ext`, `stem_1.ext`...)."""
    extension = (extension or '').lstrip('.')
    suffix = '.' + extension if extension else ''
    used = set(os.path.normcase(os.path.abspath(p)) for p in taken)
    name, n = stem, 0
    while True:
        path = os.path.join(folder, name + suffix)
        if not os.path.exists(path) \
                and os.path.normcase(os.path.abspath(path)) not in used:
            return path
        n += 1
        name = '{}_{}'.format(stem, n)


def table_uri(database, table, geometry=True):
    """The destination string QGIS itself writes for "Save to GeoPackage"."""
    uri = "ogr:dbname='{}' table=\"{}\"".format(
        database.replace('\\', '/'), table)
    return uri + ' (geom)' if geometry else uri


def is_default_table_uri(value):
    """True for a string `table_uri` could have produced."""
    return isinstance(value, str) and value.startswith('ogr:dbname=')


# --------------------------------------------------------- for custom dialogs

def default_file(stem, extension, taken=()):
    """A new file path in the project folder, or '' without a project."""
    folder = project_folder()
    if not folder:
        return ''
    return unique_file(folder, safe_stem(stem), extension, taken)


def default_database_target(stem, taken=()):
    """(database, unused table name) in the project database, or ('', '')."""
    database = project_database()
    if not database:
        return '', ''
    return database, unique_table(database, safe_stem(stem), taken)


# --------------------------------------------------------- for Processing

# Output parameter types the hook fills in, by QgsProcessingParameterDefinition.type()
VECTOR_TYPES = ('sink', 'vectorDestination')
FILE_TYPES = ('fileDestination', 'rasterDestination')
FOLDER_TYPES = ('folderDestination',)
OUTPUT_TYPES = VECTOR_TYPES + FILE_TYPES + FOLDER_TYPES


def processing_default(param_type, stem, extension='', geometry=True, taken=()):
    """The value for a Processing output of `param_type`, or None.

    `taken` is what the same dialog already claimed, so two outputs of one tool
    do not get the same name.
    """
    if param_type in VECTOR_TYPES:
        database = project_database()
        if not database:
            return None
        table = unique_table(database, stem, taken_tables(taken))
        return table_uri(database, table, geometry)
    if param_type in FILE_TYPES:
        folder = project_folder()
        if not folder:
            return None
        return unique_file(folder, stem, extension or 'tif', taken)
    if param_type in FOLDER_TYPES:
        return project_folder() or None
    return None


def taken_tables(taken):
    """Table names out of the values a dialog has claimed so far."""
    names = []
    for value in taken:
        match = re.search(r'table="([^"]+)"', str(value))
        if match:
            names.append(match.group(1))
    return names

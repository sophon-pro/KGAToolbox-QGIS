# -*- coding: utf-8 -*-
"""Look at a project's layers before sharing it: what is each, can it travel?

ArcGIS Pro analyses a project package before writing it and lists what cannot be
packaged. This is the same step: every layer in the project is classified by where
its data lives, flagged when it cannot be shared, and given a reason it belongs in
the package. Nothing is written or changed here.

**Where the data lives** (`category`) decides what the packager does (`action`):

=================  ===========================================  ==========
category           example                                      action
=================  ===========================================  ==========
project_db         a table in a GeoPackage in the project       keep
project_file       a shapefile / GeoTIFF in the project folder  keep
external_vector    a shapefile or GeoPackage elsewhere          copy_db
external_file      a GeoTIFF elsewhere, a mesh, point cloud     copy_file
temporary          a scratch (memory) layer                     copy_db
delimited          a CSV added as a layer                       copy_db
remote_vector      PostGIS, WFS, ArcGIS feature service         service
                                                                (copy_db if
                                                                asked)
service            WMS, XYZ, vector tiles, WCS                  service
virtual            a virtual layer (a query over other layers)  keep
lost               a source that no longer exists               none
embedded           a layer embedded from another project        none
unsupported        a raster sub-dataset (GPKG:, NETCDF:)        none
=================  ===========================================  ==========

**Relevance.** "Map data only" shares the layers in the map and whatever they
*need*. A layer is relevant when something that is shared depends on it: a join,
a relation, a value-relation lookup, a layout's atlas or table, a virtual layer's
input, or a layer named inside an expression. `find_relevant` follows those links
until nothing new turns up, because a joined table can itself be joined.
"""

import os
import re
from contextlib import suppress
from html import escape

from qgis.core import QgsProviderRegistry, QgsRasterLayer, QgsVectorLayer

from . import projectpkg as pp

# Categories
CAT_PROJECT_DB = 'project_db'
CAT_PROJECT_FILE = 'project_file'
CAT_EXTERNAL_VECTOR = 'external_vector'
CAT_EXTERNAL_FILE = 'external_file'
CAT_TEMPORARY = 'temporary'
CAT_DELIMITED = 'delimited'
CAT_REMOTE_VECTOR = 'remote_vector'
CAT_SERVICE = 'service'
CAT_VIRTUAL = 'virtual'
CAT_LOST = 'lost'
CAT_EMBEDDED = 'embedded'
CAT_UNSUPPORTED = 'unsupported'

# Actions
ACTION_KEEP = 'keep'
ACTION_COPY_DB = 'copy_db'
ACTION_COPY_FILE = 'copy_file'
ACTION_SERVICE = 'service'
ACTION_NONE = 'none'

CATEGORY_LABEL = {
    CAT_PROJECT_DB: 'Project database',
    CAT_PROJECT_FILE: 'Project folder file',
    CAT_EXTERNAL_VECTOR: 'External vector data',
    CAT_EXTERNAL_FILE: 'External file',
    CAT_TEMPORARY: 'Scratch layer',
    CAT_DELIMITED: 'Delimited text',
    CAT_REMOTE_VECTOR: 'Remote database / service',
    CAT_SERVICE: 'Web service',
    CAT_VIRTUAL: 'Virtual layer',
    CAT_LOST: 'Source not found',
    CAT_EMBEDDED: 'Embedded layer',
    CAT_UNSUPPORTED: 'Sub-dataset',
}

ACTION_LABEL = {
    ACTION_KEEP: 'In the project',
    ACTION_COPY_DB: 'Copy into Default.gpkg',
    ACTION_COPY_FILE: 'Copy into commondata',
    ACTION_SERVICE: 'Keep as a service',
    ACTION_NONE: 'Not shared',
}

REMOTE_VECTOR_PROVIDERS = {'postgres', 'mssql', 'oracle', 'hana', 'WFS', 'wfs',
                           'arcgisfeatureserver', 'afs'}
FILE_VECTOR_PROVIDERS = {'ogr', 'spatialite'}

REASON_IN_MAP = 'in the map'
REASON_NOT_IN_MAP = 'not in the map'

#: How a file's sidecars are recognised: same stem, these suffixes.
SIDECAR_SUFFIXES = ('.aux.xml', '.ovr', '.prj', '.tfw', '.wld', '.jgw', '.pgw',
                    '.cpg', '.qix', '.sbn', '.sbx', '.shx', '.dbf', '.idx',
                    '.qpj', '.xml', '.msk', '.rrd')

_CREDENTIAL = re.compile(r"(password|pwd)\s*=\s*('[^']*'|\"[^\"]*\"|[^\s&]+)",
                         re.IGNORECASE)


# ----------------------------------------------------------------- the model

class LayerInfo(object):
    """What the packager needs to know about one layer."""

    def __init__(self, layer):
        self.layer = layer
        self.id = layer.id()
        self.name = layer.name()
        self.provider = ''
        self.kind = 'other'             # vector | raster | other
        self.category = CAT_SERVICE
        self.action = ACTION_SERVICE
        self.path = ''                  # the file behind it, when it is one
        self.table = ''                 # layer name inside a database file
        self.in_map = False
        self.relevant_reasons = []      # why it is needed when it is not in the map
        self.warnings = []
        self.notes = []
        self.size = 0                   # estimated bytes it adds to the package
        self.include = True

    # -- derived ------------------------------------------------------------

    @property
    def sharable(self):
        return self.action != ACTION_NONE

    @property
    def relevant(self):
        return bool(self.relevant_reasons)

    @property
    def reason(self):
        """Why this layer is in the package, for the table."""
        if self.in_map:
            return REASON_IN_MAP
        if self.relevant_reasons:
            return 'needed: ' + '; '.join(self.relevant_reasons)
        return REASON_NOT_IN_MAP

    @property
    def status(self):
        """ok | service | warning | not_sharable"""
        if not self.sharable:
            return pp.STATUS_NOT_SHARABLE
        if self.action == ACTION_SERVICE:
            return pp.STATUS_SERVICE
        return pp.STATUS_PACKAGED

    @property
    def category_label(self):
        return CATEGORY_LABEL.get(self.category, self.category)

    @property
    def action_label(self):
        return ACTION_LABEL.get(self.action, self.action)

    def __repr__(self):
        return 'LayerInfo({!r}, {}, {})'.format(
            self.name, self.category, self.action)


class Analysis(object):
    """The result of `analyze`: one `LayerInfo` per layer, plus project facts."""

    def __init__(self, home):
        self.home = home
        self.layers = []
        self.layouts = []
        self.warnings = []

    def by_id(self):
        return {info.id: info for info in self.layers}

    def apply_mode(self, mode):
        """Tick the layers `mode` shares by default."""
        for info in self.layers:
            if mode == pp.MODE_EVERYTHING:
                info.include = True
            else:
                info.include = info.in_map or info.relevant
        return self

    @property
    def not_sharable(self):
        return [i for i in self.layers if not i.sharable]

    def included(self):
        return [i for i in self.layers if i.include]

    def estimated_size(self):
        return sum(i.size for i in self.layers if i.include)


# ---------------------------------------------------------------- file helpers

def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == \
        os.path.normcase(os.path.normpath(b))


def sibling_files(path):
    """`path` and the files that belong to it (shapefile parts, world files ...)."""
    if not path or not os.path.isfile(path):
        return []
    found = [path]
    folder = os.path.dirname(path)
    stem, extension = os.path.splitext(os.path.basename(path))
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        full = os.path.join(folder, name)
        if full == path or not os.path.isfile(full):
            continue
        lowered = name.lower()
        if lowered.startswith(stem.lower() + '.') and \
                any(lowered.endswith(s) for s in SIDECAR_SUFFIXES):
            found.append(full)
        elif lowered in (os.path.basename(path).lower() + '.aux.xml',
                         os.path.basename(path).lower() + '.ovr'):
            found.append(full)
    return sorted(set(found))


def files_size(paths):
    total = 0
    for path in paths:
        with suppress(OSError):
            total += os.path.getsize(path)
    return total


def table_size(gpkg, table):
    """Approximate bytes of one table of a GeoPackage (incl. its spatial index)."""
    import sqlite3
    try:
        connection = sqlite3.connect('file:{}?mode=ro'.format(
            gpkg.replace('\\', '/')), uri=True)
    except sqlite3.Error:
        return os.path.getsize(gpkg) if os.path.isfile(gpkg) else 0
    try:
        names = (table, 'rtree_{}_geom'.format(table))
        row = connection.execute(
            'SELECT SUM(pgsize) FROM dbstat WHERE name IN (?, ?, ?, ?, ?)',
            names + ('rtree_{}_geom_node'.format(table),
                     'rtree_{}_geom_rowid'.format(table),
                     'rtree_{}_geom_parent'.format(table))).fetchone()
        if row and row[0]:
            return int(row[0])
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    # dbstat is not always compiled in: share the file out by table count.
    try:
        count = max(1, len(_gpkg_tables(gpkg)))
    except Exception:
        count = 1
    return os.path.getsize(gpkg) // count if os.path.isfile(gpkg) else 0


def _gpkg_tables(gpkg):
    import sqlite3
    connection = sqlite3.connect('file:{}?mode=ro'.format(
        gpkg.replace('\\', '/')), uri=True)
    try:
        return [r[0] for r in connection.execute(
            'SELECT table_name FROM gpkg_contents')]
    finally:
        connection.close()


def strip_credentials(source):
    """`source` with passwords blanked, so they are never packaged."""
    return _CREDENTIAL.sub(lambda m: m.group(1) + "=''", source or '')


def has_credentials(source):
    text = (source or '').lower()
    return bool(_CREDENTIAL.search(source or '')) or 'authcfg=' in text \
        or 'username=' in text or 'user=' in text


def _decode(provider, source):
    with suppress(Exception):
        return QgsProviderRegistry.instance().decodeUri(provider, source) or {}
    return {}


def _is_remote_path(path):
    lowered = (path or '').lower()
    return lowered.startswith(('http:', 'https:', 'ftp:', '/vsicurl', '/vsis3',
                               '/vsigs', '/vsiaz', '/vsiadls', 'ws:', 'wss:'))


# ----------------------------------------------------------- classification

def classify(layer, home, copy_remote=False, project=None):
    """A `LayerInfo` for `layer`, classified against the project folder `home`."""
    info = LayerInfo(layer)
    info.provider = layer.providerType() or ''
    if isinstance(layer, QgsVectorLayer):
        info.kind = 'vector'
    elif isinstance(layer, QgsRasterLayer):
        info.kind = 'raster'
    source = layer.source() or ''
    parts = _decode(info.provider, source)

    if project is not None and project.layerIsEmbedded(layer.id()):
        _set(info, CAT_EMBEDDED, ACTION_NONE)
        info.warnings.append(
            'Embedded from another project; its data cannot be packaged. Add '
            'the layer to this project directly.')
        return info

    if info.provider in ('memory',):
        _set(info, CAT_TEMPORARY, ACTION_COPY_DB)
        info.notes.append('Scratch layer: its features are saved into '
                          'Default.gpkg.')
        return _finish(info, layer)
    if info.provider == 'virtual':
        _set(info, CAT_VIRTUAL, ACTION_KEEP)
        info.notes.append('Virtual layer: a query over other layers.')
        return _finish(info, layer)
    if info.provider == 'delimitedtext':
        return _delimited(info, layer, parts)

    path = str(parts.get('path') or '')
    info.table = str(parts.get('layerName') or '')

    if info.kind == 'vector' and info.provider in REMOTE_VECTOR_PROVIDERS:
        _set(info, CAT_REMOTE_VECTOR,
             ACTION_COPY_DB if copy_remote else ACTION_SERVICE)
        _credential_notes(info, source)
        if not copy_remote:
            info.notes.append('Stays a live connection; whoever opens the '
                              'package needs access to it.')
        return _finish(info, layer)

    file_based = info.provider in FILE_VECTOR_PROVIDERS or \
        info.provider in ('gdal', 'mdal', 'pdal', 'copc', 'ept',
                          'vectortile', 'pointcloud') or \
        (info.kind != 'vector' and bool(path) and not _is_remote_path(path))

    if not path or _is_remote_path(path) or not file_based:
        _set(info, CAT_SERVICE, ACTION_SERVICE)
        _credential_notes(info, source)
        if not layer.isValid():
            info.warnings.append(
                'This service could not be reached just now; it is kept as '
                'it is.')
        return _finish(info, layer)

    info.path = os.path.normpath(path)
    if not os.path.isfile(info.path):
        if _looks_like_subdataset(path):
            _set(info, CAT_UNSUPPORTED, ACTION_NONE)
            info.warnings.append(
                'A sub-dataset of a container file ({}); it is not '
                'repackaged. Keep the layer or export it to a GeoPackage '
                'first.'.format(path.split(':')[0]))
        else:
            _set(info, CAT_LOST, ACTION_NONE)
            info.warnings.append(
                'Data source not found: {}. This layer is kept in the '
                'project but its data cannot be shared.'.format(
                    os.path.basename(info.path) or info.path))
        return info
    if not layer.isValid():
        _set(info, CAT_LOST, ACTION_NONE)
        info.warnings.append(
            'The layer could not be opened ({}). Its data cannot be '
            'shared.'.format(os.path.basename(info.path)))
        return info

    inside = bool(home) and _within(home, info.path)
    if info.provider in ('ogr', 'spatialite') and info.kind == 'vector':
        if inside:
            _set(info, CAT_PROJECT_DB if info.path.lower().endswith('.gpkg')
                 else CAT_PROJECT_FILE, ACTION_KEEP)
        else:
            _set(info, CAT_EXTERNAL_VECTOR, ACTION_COPY_DB)
    else:
        if inside:
            _set(info, CAT_PROJECT_FILE, ACTION_KEEP)
        else:
            _set(info, CAT_EXTERNAL_FILE, ACTION_COPY_FILE)
    return _finish(info, layer)


def _delimited(info, layer, parts):
    path = str(parts.get('path') or '')
    _set(info, CAT_DELIMITED, ACTION_COPY_DB)
    info.path = os.path.normpath(path) if path else ''
    if info.path and not os.path.isfile(info.path):
        _set(info, CAT_LOST, ACTION_NONE)
        info.warnings.append('Data source not found: {}.'.format(
            os.path.basename(info.path)))
        return info
    if not layer.isValid():
        _set(info, CAT_LOST, ACTION_NONE)
        info.warnings.append('The text file could not be read as a layer.')
        return info
    info.notes.append('Saved into Default.gpkg as a table.')
    return _finish(info, layer)


def _looks_like_subdataset(path):
    head = path.split(':')[0].upper()
    return ':' in path[2:] and head in (
        'GPKG', 'NETCDF', 'HDF4_EOS', 'HDF5', 'SQLITE', 'PGEO', 'OCI',
        'ZARR', 'GRIB', 'MBTILES', 'OPENFILEGDB')


def _within(home, path):
    return pp.is_within(home, path)


def _set(info, category, action):
    info.category = category
    info.action = action


def _credential_notes(info, source):
    if has_credentials(source):
        info.warnings.append(
            'The connection uses a user name, password or stored '
            'authentication; credentials are never packaged, so the recipient '
            'must sign in themselves.')


def _finish(info, layer):
    """Size estimate, now the category is known."""
    info.size = estimate_size(info)
    if info.warnings and info.action == ACTION_SERVICE:
        pass
    return info


def estimate_size(info):
    if info.action == ACTION_COPY_FILE or (
            info.action == ACTION_KEEP and info.category == CAT_PROJECT_FILE):
        return files_size(sibling_files(info.path)) if info.path else 0
    if info.category in (CAT_PROJECT_DB, CAT_EXTERNAL_VECTOR) and info.path:
        if info.path.lower().endswith('.gpkg') and info.table:
            return table_size(info.path, info.table)
        return files_size(sibling_files(info.path))
    if info.action == ACTION_COPY_DB:
        # No file to measure (scratch, text, remote): a rough 120 bytes a feature.
        with suppress(Exception):
            return max(0, int(info.layer.featureCount())) * 120
    return 0


# -------------------------------------------------------------- relevance

def layers_in_map(project):
    """Ids of the layers that appear in the layer tree (the map's table of contents)."""
    return {node.layerId()
            for node in project.layerTreeRoot().findLayers()}


def _layer_ref(project, value):
    """A layer id for `value` (an id, or a layer name when it is unambiguous)."""
    if not value:
        return ''
    if project.mapLayer(value) is not None:
        return value
    named = project.mapLayersByName(value)
    return named[0].id() if len(named) == 1 else ''


def direct_dependencies(project, info):
    """[(layer id, reason)] the layer `info` needs in order to work."""
    layer = info.layer
    needs = []
    if isinstance(layer, QgsVectorLayer):
        with suppress(Exception):
            for join in layer.vectorJoins():
                needs.append((join.joinLayerId(),
                              'joined to {}'.format(layer.name())))
        with suppress(Exception):
            fields = layer.fields()
            for index in range(fields.count()):
                setup = layer.editorWidgetSetup(index)
                config = setup.config() or {}
                if setup.type() == 'ValueRelation':
                    target = _layer_ref(project, config.get('Layer'))
                    if target:
                        needs.append((target, 'lookup for {}.{}'.format(
                            layer.name(), fields.at(index).name())))
        if info.provider == 'virtual':
            needs.extend(_virtual_inputs(project, layer))
    return [(i, r) for i, r in needs if i]


def _virtual_inputs(project, layer):
    found = []
    with suppress(Exception):
        from qgis.core import QgsVirtualLayerDefinition
        from qgis.PyQt.QtCore import QUrl
        definition = QgsVirtualLayerDefinition.fromUrl(QUrl.fromEncoded(
            layer.source().encode()))
        for source in definition.sourceLayers():
            target = ''
            if source.isReferenced():
                target = _layer_ref(project, source.reference())
            if not target:
                target = _layer_ref(project, source.name())
            if target:
                found.append((target, 'input of the virtual layer {}'.format(
                    layer.name())))
    return found


def relation_links(project):
    """[(layer id, layer id, label)] for every relation in the project."""
    links = []
    with suppress(Exception):
        for relation in project.relationManager().relations().values():
            links.append((relation.referencingLayerId(),
                          relation.referencedLayerId(),
                          relation.name() or relation.id()))
    return links


def layout_dependencies(project):
    """[(layout name, [layer ids])] - atlas coverage, locked map layers, tables."""
    found = []
    with suppress(Exception):
        for layout in project.layoutManager().layouts():
            ids = []
            with suppress(Exception):
                atlas = layout.atlas()
                if atlas is not None and atlas.enabled() \
                        and atlas.coverageLayer() is not None:
                    ids.append(atlas.coverageLayer().id())
            # Tables are multi-frames, which `items()` does not list.
            members = list(layout.items())
            with suppress(Exception):
                members.extend(layout.multiFrames())
            for item in members:
                with suppress(Exception):
                    if hasattr(item, 'keepLayerSet') and item.keepLayerSet():
                        ids.extend(layer.id() for layer in item.layers())
                with suppress(Exception):
                    if hasattr(item, 'sourceLayer'):
                        layer = item.sourceLayer()
                        if layer is not None:
                            ids.append(layer.id())
            found.append((layout.name(), [i for i in dict.fromkeys(ids) if i]))
    return found


def scan_expressions(text, infos):
    """Ids of the layers whose id or name is quoted somewhere in project XML `text`.

    A layer used only inside an expression - `get_feature('roads', ...)`,
    `aggregate('layer_id', ...)` - is invisible to every API, so the saved
    project is searched for the id or the name written as a string literal.
    Names are matched exactly and only when quoted, which keeps ordinary
    labels from matching by accident. It errs towards including a layer.
    """
    found = set()
    for info in infos:
        for token in {info.id, info.name}:
            if not token:
                continue
            escaped = escape(token, quote=False)
            patterns = ("'{}'".format(escaped), '&apos;{}&apos;'.format(escaped),
                        '&quot;{}&quot;'.format(escaped))
            if any(p in text for p in patterns):
                found.add(info.id)
                break
    return found


def read_project_text(project_file):
    """The XML of a saved project (`.qgz` or `.qgs`), or ''."""
    if not project_file or not os.path.isfile(project_file):
        return ''
    try:
        if project_file.lower().endswith('.qgz'):
            import zipfile
            with zipfile.ZipFile(project_file) as archive:
                for name in archive.namelist():
                    if name.lower().endswith('.qgs'):
                        return archive.read(name).decode('utf-8', 'replace')
            return ''
        with open(project_file, encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except (OSError, ValueError):
        return ''


def find_relevant(project, analysis, project_text=''):
    """Fill `relevant_reasons` on layers that shared layers depend on.

    Starts from the layers in the map and follows joins, relations, value
    lookups, layout sources, virtual-layer inputs and expression references
    until no new layer turns up.
    """
    by_id = analysis.by_id()
    needed = {i.id for i in analysis.layers if i.in_map}
    links = relation_links(project)
    layouts = layout_dependencies(project)
    expression_ids = scan_expressions(project_text, analysis.layers) \
        if project_text else set()

    def need(layer_id, reason):
        info = by_id.get(layer_id)
        if info is None:
            return False
        if reason not in info.relevant_reasons and not info.in_map:
            info.relevant_reasons.append(reason)
        if layer_id in needed:
            return False
        needed.add(layer_id)
        return True

    # Layers a layout cannot render without are needed whatever the map says.
    for layout_name, ids in layouts:
        for layer_id in ids:
            need(layer_id, 'used by the layout {}'.format(layout_name))
    for layer_id in expression_ids:
        need(layer_id, 'named in an expression')

    changed = True
    while changed:
        changed = False
        for info in analysis.layers:
            if info.id not in needed:
                continue
            for target, reason in direct_dependencies(project, info):
                if need(target, reason):
                    changed = True
        for referencing, referenced, label in links:
            if referencing in needed and need(
                    referenced, 'related to {} ({})'.format(
                        by_id[referencing].name if referencing in by_id else '?',
                        label)):
                changed = True
            if referenced in needed and need(
                    referencing, 'related to {} ({})'.format(
                        by_id[referenced].name if referenced in by_id else '?',
                        label)):
                changed = True
    return analysis


# ------------------------------------------------------------------- driver

def analyze(project, home, project_file='', copy_remote=False):
    """Classify every layer of `project` and work out relevance.

    `home` is the project folder (data inside it is "in the project").
    """
    analysis = Analysis(home)
    in_map = layers_in_map(project)
    for layer in project.mapLayers().values():
        info = classify(layer, home, copy_remote, project)
        info.in_map = layer.id() in in_map
        analysis.layers.append(info)
    analysis.layers.sort(key=lambda i: (not i.in_map, i.name.lower()))
    with suppress(Exception):
        analysis.layouts = [layout.name()
                            for layout in project.layoutManager().layouts()]
    find_relevant(project, analysis, read_project_text(project_file))
    # Font markers cannot travel; say so per layer.
    for info in analysis.layers:
        if info.kind == 'vector':
            for font in _font_markers(info.layer):
                info.warnings.append(
                    'Uses the font marker "{}"; fonts cannot be packaged, so '
                    'install it on the receiving computer.'.format(font))
    return analysis


def _font_markers(layer):
    fonts = set()
    with suppress(Exception):
        from .packaging import iter_symbol_layers, iter_symbols
        for symbol in iter_symbols(layer):
            for symbol_layer in iter_symbol_layers(symbol):
                if 'FontMarker' in type(symbol_layer).__name__:
                    fonts.add(symbol_layer.fontFamily())
    return sorted(fonts)

# -*- coding: utf-8 -*-
"""Copy data in the QGIS Browser, paste it somewhere else.

The Browser panel has no Copy and Paste, so moving a layer from a GeoPackage
into a folder as a shapefile, or from a File Geodatabase into a GeoPackage, means
an Export dialog per layer. This module is the engine behind the Copy / Paste
entries the plugin adds to the Browser's right-click menu. It knows nothing
about the Browser itself: `gui/browser_paste.py` turns the selected items into
`Source` objects and the right-clicked item into a `Target`.

* **OGR-readable sources are translated by GDAL**, through the same machinery
  as the GeoPackage <-> File Geodatabase tools, so field domains, aliases and
  OBJECTID come across. Anything GDAL cannot read as it stands (a PostGIS or
  WFS layer, a layer with a filter or a geometry-type split) is first written
  to a scratch GeoPackage by QGIS and translated from that.
* **Names are made legal for the target**, and the original is kept as the
  ArcGIS alias when the target is a File Geodatabase.
* **A name clash is a decision, not an accident**: keep both (``roads_2``),
  replace, or skip.
* **Rasters** can be pasted into a folder, where they become GeoTIFFs.
"""

import os
import re
import shutil
import tempfile
import time
from contextlib import suppress

from qgis.core import (
    QgsFeedback,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import gdb_convert as gc

# Formats a folder can be pasted into.
FMT_SHAPEFILE = 'ESRI Shapefile'
FMT_GEOJSON = 'GeoJSON'
FMT_GPKG = gc.GPKG_DRIVER
FMT_GDB = gc.GDB_DRIVER

FORMATS = (
    (FMT_SHAPEFILE, 'Shapefile (.shp)', '.shp'),
    (FMT_GPKG, 'GeoPackage (.gpkg)', '.gpkg'),
    (FMT_GDB, 'File Geodatabase (.gdb)', '.gdb'),
    (FMT_GEOJSON, 'GeoJSON (.geojson)', '.geojson'),
)
#: One file per layer, as opposed to one container holding them all.
FILE_FORMATS = (FMT_SHAPEFILE, FMT_GEOJSON)
EXTENSION = {code: ext for code, _label, ext in FORMATS}

# What to do when the target already holds something of that name.
CLASH_RENAME = 'rename'
CLASH_REPLACE = 'replace'
CLASH_SKIP = 'skip'

KIND_VECTOR = 'vector'
KIND_RASTER = 'raster'

TARGET_FOLDER = 'folder'
TARGET_GPKG = 'gpkg'
TARGET_GDB = 'gdb'

_ILLEGAL_FILE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SHAPEFILE_SIDECARS = ('.shp', '.shx', '.dbf', '.prj', '.cpg', '.qix', '.sbn',
                       '.sbx', '.qpj', '.shp.xml', '.fix', '.idx', '.ids',
                       '.atx', '.ixs', '.mxs', '.qmd')


class PasteError(Exception):
    """Something that stops a paste before any data is written."""


# ------------------------------------------------------------------ clipboard

class Source(object):
    """One thing that has been copied: a vector layer or a raster."""

    def __init__(self, name, uri, provider, kind=KIND_VECTOR):
        self.name = name
        self.uri = uri
        self.provider = provider
        self.kind = kind

    @property
    def key(self):
        return (self.provider, self.uri)

    def __repr__(self):
        return 'Source({!r}, {!r})'.format(self.name, self.uri)


class Target(object):
    """Where a paste lands: a folder, or an existing GeoPackage / File GDB."""

    def __init__(self, kind, path):
        self.kind = kind
        self.path = path

    @property
    def label(self):
        return os.path.basename(self.path.rstrip('/\\')) or self.path


class Clipboard(object):
    """The plugin's own clipboard. Nothing here touches the system one."""

    def __init__(self):
        self._sources = []

    def set(self, sources):
        seen, unique = set(), []
        for source in sources:
            if source.key not in seen:
                seen.add(source.key)
                unique.append(source)
        self._sources = unique

    def sources(self):
        return list(self._sources)

    def clear(self):
        self._sources = []

    def __len__(self):
        return len(self._sources)


CLIPBOARD = Clipboard()


class Options(object):
    """What the user chose in the paste dialog."""

    def __init__(self, **overrides):
        self.format = FMT_SHAPEFILE          # folder targets only
        self.container_name = ''             # folder -> .gpkg / .gdb only
        self.on_clash = CLASH_RENAME
        self.carry_styles = True
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise TypeError('Unknown paste option: {}'.format(key))
            setattr(self, key, value)


class Feedback(QgsFeedback):
    """A QgsFeedback that also keeps a log, so the caller can show what happened.

    `on_progress(percent, text)` is called whenever the work moves on, and
    `on_tick()` (at most ten times a second) from inside a long GDAL call; the
    GUI uses them to drive a progress dialog and to let a click on Cancel in.
    """

    def __init__(self, on_progress=None, on_tick=None):
        super().__init__()
        self.lines = []
        self.warnings = []
        self.errors = []
        self._on_progress = on_progress
        self._on_tick = on_tick
        self._last_tick = 0.0

    def tick(self):
        """Called from GDAL's progress callback."""
        if self._on_tick is None:
            return
        now = time.monotonic()
        if now - self._last_tick >= 0.1:
            self._last_tick = now
            self._on_tick()

    def pushInfo(self, text):           # noqa: N802 - Qt/Processing naming
        self.lines.append(text)

    def pushWarning(self, text):        # noqa: N802
        self.lines.append('Warning: ' + text)
        self.warnings.append(text)

    def reportError(self, text):        # noqa: N802
        self.lines.append('Error: ' + text)
        self.errors.append(text)

    def step(self, percent, text=''):
        self.setProgress(percent)
        if self._on_progress is not None:
            self._on_progress(percent, text)


class PasteResult(object):

    def __init__(self):
        self.written = []         # [(source name, where it went), ...]
        self.skipped = []         # [(source name, reason), ...]
        self.failed = []          # [(source name, message), ...]
        self.renamed_fields = []  # [(layer, source field, target field), ...]
        self.styles = 0
        self.canceled = False
        self.container = ''       # the .gpkg / .gdb that was written to, if any

    @property
    def count(self):
        return len(self.written)

    @property
    def clean(self):
        return not (self.skipped or self.failed or self.renamed_fields
                    or self.canceled)


# --------------------------------------------------------------- target paths

def target_for_path(path):
    """A `Target` for a Browser item's path, or None when it cannot be pasted into.

    The Browser hands over plain paths for items under a folder, and
    ``gpkg:/<path>`` for the entries of its GeoPackage node.
    """
    if not path:
        return None
    if path.lower().startswith('gpkg:'):
        path = path[5:].lstrip('/') if re.match(r'^gpkg:/+[A-Za-z]:', path) \
            else path[5:]
    path = os.path.normpath(path)
    low = path.lower()
    if low.endswith(gc.GPKG_SUFFIX) and os.path.isfile(path):
        return Target(TARGET_GPKG, path)
    if low.endswith(gc.GDB_SUFFIX) and os.path.isdir(path) \
            and gc.looks_like_filegdb(path):
        return Target(TARGET_GDB, path)
    if os.path.isdir(path):
        return Target(TARGET_FOLDER, path)
    return None


def container_path(path):
    """The .gpkg / .gdb behind a Browser item's path, or ''."""
    target = target_for_path(path)
    if target is not None and target.kind in (TARGET_GPKG, TARGET_GDB):
        return target.path
    return ''


def safe_file_name(name):
    """A file name Windows accepts, keeping Khmer and other scripts intact."""
    cleaned = _ILLEGAL_FILE_CHARS.sub('_', name or '').strip(' ._')
    return cleaned or 'layer'


def can_write_gdb():
    return gc.has_filegdb_write()


# ------------------------------------------------------------------- reading

class _Reader(object):
    """A source opened for translating: a GDAL dataset and the layer inside it."""

    def __init__(self, dataset, layer_name, path, temp_dir='', container=''):
        self.dataset = dataset
        self.layer_name = layer_name
        self.path = path              # the file the dataset came from
        self.temp_dir = temp_dir      # scratch folder to delete afterwards
        #: The .gpkg / .gdb the layer lives in, when its styles can be carried.
        self.container = container

    def close(self):
        self.dataset = None
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = ''


def _direct_reader(source):
    """A reader straight onto the source file, or None when that is not possible."""
    if source.provider != 'ogr':
        return None
    try:
        parts = QgsProviderRegistry.instance().decodeUri('ogr', source.uri)
    except Exception:
        return None
    path = parts.get('path') or ''
    # A filter, a geometry-type split or any open option is something only
    # QGIS applies, so those go through the scratch copy instead.
    if _has_extra_options(source.uri):
        return None
    dataset = gc.open_dataset(path)
    if dataset is None:
        return None
    name = parts.get('layerName') or ''
    layer = dataset.GetLayerByName(name) if name else None
    if layer is None and not name and dataset.GetLayerCount() == 1:
        layer = dataset.GetLayer(0)
    if layer is None:
        return None
    container = path if path.lower().endswith(
        (gc.GPKG_SUFFIX, gc.GDB_SUFFIX)) else ''
    return _Reader(dataset, layer.GetName(), path, container=container)


def _has_extra_options(uri):
    """True when a URI carries anything but a path and a layer name."""
    for chunk in uri.split('|')[1:]:
        if not chunk.lower().startswith('layername='):
            return True
    return False


def _scratch_reader(source, feedback):
    """Write the source to a scratch GeoPackage with QGIS and read that."""
    layer = QgsVectorLayer(source.uri, source.name, source.provider)
    if not layer.isValid():
        raise PasteError('"{}" could not be opened.'.format(source.name))
    temp_dir = tempfile.mkdtemp(prefix='kga_paste_')
    out = os.path.join(temp_dir, 'scratch.gpkg')
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    # Named after the source, because that name is what the pasted file or
    # table ends up called.
    name = _source_layer_name(source)
    options.layerName = name
    options.fileEncoding = 'UTF-8'
    options.feedback = feedback
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, out, QgsProject.instance().transformContext(), options)
    if result[0] != QgsVectorFileWriter.WriterError.NoError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if feedback.isCanceled():
            raise PasteError('Canceled.')
        raise PasteError('Could not read "{}": {}'.format(source.name, result[1]))
    dataset = gc.open_dataset(out)
    if dataset is None:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise PasteError('Could not read "{}".'.format(source.name))
    return _Reader(dataset, name, out, temp_dir)


def _open_reader(source, feedback):
    reader = _direct_reader(source)
    return reader if reader is not None else _scratch_reader(source, feedback)


# ------------------------------------------------------------------- writing

def _existing_layers(path):
    """Lower-cased layer names already in a container, or an empty set."""
    dataset = gc.open_dataset(path)
    if dataset is None:
        return set()
    try:
        return {dataset.GetLayer(i).GetName().lower()
                for i in range(dataset.GetLayerCount())}
    finally:
        dataset = None


def _delete_layer(path, name):
    dataset = gc.open_dataset(path, update=True)
    if dataset is None:
        return
    try:
        for index in range(dataset.GetLayerCount()):
            if dataset.GetLayer(index).GetName().lower() == name.lower():
                dataset.DeleteLayer(index)
                break
    finally:
        dataset = None


def _delete_vector_file(path):
    """Remove a shapefile with its sidecars, or a single GeoJSON file."""
    if path.lower().endswith('.shp'):
        stem = path[:-4]
        for extension in _SHAPEFILE_SIDECARS:
            side = stem + extension
            if os.path.exists(side):
                os.remove(side)
    elif os.path.exists(path):
        os.remove(path)


def _unique_file(folder, base, ext, taken):
    """folder/base.ext, or base_2.ext ... when that is taken on disk or in `taken`."""
    candidate, index = base, 2
    while (os.path.exists(os.path.join(folder, candidate + ext))
           or candidate.lower() in taken):
        candidate = '{}_{}'.format(base, index)
        index += 1
    taken.add(candidate.lower())
    return candidate


def _lco(fmt, job):
    """The layer-creation options for one layer in format `fmt`."""
    if fmt == FMT_SHAPEFILE:
        # Without this the .dbf is written in the system code page, and Khmer
        # text comes out as question marks.
        return ['ENCODING=UTF-8']
    if fmt == FMT_GEOJSON:
        return ['RFC7946=NO', 'WRITE_BBOX=NO']
    settings = gc.Settings(target_format=fmt)
    options = gc._layer_creation_options(settings, job)
    fid = getattr(job, 'fid', None)
    if fid:
        options = [('FID=' + fid) if option.startswith('FID=') else option
                   for option in options]
    return options


def _free_key_name(default, field_names):
    """The primary-key column name, avoiding every attribute field's name.

    ArcGIS shapefiles carry an ordinary attribute called ``FID``, and a
    geodatabase layer may carry ``OBJECTID`` as data. Writing either into a
    GeoPackage (key ``fid``) or geodatabase (key ``OBJECTID``) makes GDAL
    fail with "Wrong field type", so the key steps aside: ``fid_1`` /
    ``OBJECTID_1``, which is also what ArcGIS itself would call it.
    """
    taken = {name.lower() for name in field_names}
    if default.lower() not in taken:
        return default
    index = 1
    while '{}_{}'.format(default, index).lower() in taken:
        index += 1
    return '{}_{}'.format(default, index)


def _geometry_option(fmt):
    return gc._geometry_type(gc.Settings(target_format=fmt))


def _translate_once(reader, dest, fmt, layer_name, job, access, preserve_fid,
                    feedback):
    gdal = gc._gdal()

    def progress(_fraction, _message, _data):
        feedback.tick()
        return 0 if feedback.isCanceled() else 1

    options = gdal.VectorTranslateOptions(
        format=fmt,
        layers=[reader.layer_name],
        layerName=layer_name,
        accessMode=access,
        layerCreationOptions=_lco(fmt, job),
        geometryType=_geometry_option(fmt),
        preserveFID=preserve_fid,
        resolveDomains=False,
        callback=progress,
    )
    written = gdal.VectorTranslate(dest, reader.dataset, options=options)
    ok = written is not None
    written = None
    return ok


def _translate(reader, dest, fmt, layer_name, job, access, feedback):
    """Translate one layer. Feature ids are kept for the database formats.

    Duplicate or non-positive ids in the source (a shapefile counts from 0, a
    geodatabase from 1) make that fail, and the copy is perfectly good with
    fresh ones, so it is retried without them.
    """
    keep = fmt in (FMT_GPKG, FMT_GDB)
    try:
        return _translate_once(reader, dest, fmt, layer_name, job, access,
                               keep, feedback)
    except Exception:
        if not keep or feedback.isCanceled():
            raise
    return _translate_once(reader, dest, fmt, layer_name, job, 'overwrite',
                           False, feedback)


def _paste_vector(source, target, options, plan, result, feedback):
    """One vector source into the target. Returns True when something was written."""
    reader = _open_reader(source, feedback)
    try:
        fmt = plan.format
        if fmt in FILE_FORMATS:
            return _write_file_format(source, reader, target, plan, result,
                                      feedback)
        return _write_container(source, reader, plan, options, result, feedback)
    finally:
        reader.close()


def _write_file_format(source, reader, target, plan, result, feedback):
    folder = target.path
    base = safe_file_name(reader.layer_name or source.name)
    ext = EXTENSION[plan.format]
    path = os.path.join(folder, base + ext)

    if os.path.exists(path):
        if plan.on_clash == CLASH_SKIP:
            result.skipped.append((source.name, 'already exists'))
            return False
        if plan.on_clash == CLASH_REPLACE:
            if os.path.abspath(path) == os.path.abspath(reader.path):
                result.skipped.append(
                    (source.name, 'cannot replace a layer with itself'))
                return False
            _delete_vector_file(path)
        else:
            base = _unique_file(folder, base, ext, plan.taken_files)
            path = os.path.join(folder, base + ext)
    plan.taken_files.add(base.lower())

    before = gc._field_names(reader.dataset, reader.layer_name)
    job = gc._Job(reader.layer_name, base, '', '')
    try:
        ok = _translate(reader, path, plan.format, base, job, None, feedback)
    except Exception as error:
        _delete_vector_file(path)
        raise PasteError(gc._first_line(error))
    if not ok:
        _delete_vector_file(path)
        raise PasteError('the driver refused the layer')

    target_ds = gc.open_dataset(path)
    try:
        gc._record_renames(result, base, before,
                           gc._field_names(target_ds, _only_layer(target_ds)))
    finally:
        target_ds = None
    result.written.append((source.name, os.path.basename(path)))
    return True


def _only_layer(dataset):
    return dataset.GetLayer(0).GetName() if dataset is not None \
        and dataset.GetLayerCount() else ''


def _write_container(source, reader, plan, options, result, feedback):
    path = plan.container
    to_gdb = plan.format == FMT_GDB

    base = gc.sanitize_gdb_name(reader.layer_name) if to_gdb \
        else gc.sanitize_gpkg_name(reader.layer_name)
    # `taken` holds what is already in the container plus what this paste has
    # added, so two sources called `roads` land as roads and roads_2.
    clashes = base.lower() in plan.taken_layers
    if clashes:
        same_place = bool(reader.path) and os.path.abspath(reader.path) == \
            os.path.abspath(path) and reader.layer_name.lower() == base.lower()
        if plan.on_clash == CLASH_SKIP:
            result.skipped.append((source.name, 'already exists'))
            return False
        if plan.on_clash == CLASH_REPLACE:
            if same_place:
                result.skipped.append(
                    (source.name, 'cannot replace a layer with itself'))
                return False
            if base.lower() in plan.added_layers:
                # Two sources with one name in a single paste: replacing the
                # first with the second would silently drop data.
                base = gc.unique_name(base, plan.taken_layers)
        else:
            base = gc.unique_name(base, plan.taken_layers)
    plan.taken_layers.add(base.lower())
    plan.added_layers.add(base.lower())

    alias = reader.layer_name if (to_gdb and base != reader.layer_name) else ''
    # source == target on purpose: for a GeoPackage that stops an IDENTIFIER
    # option being added, and two layers cannot share one identifier.
    job = gc._Job(base, base, alias, '')
    job.fid = _free_key_name(
        'OBJECTID' if to_gdb else 'fid',
        gc._field_names(reader.dataset, reader.layer_name))
    if job.fid not in ('fid', 'OBJECTID'):
        feedback.pushInfo('{}: the source already has a field named like the '
                          'key column, so the key is called {}.'.format(
                              source.name, job.fid))
    exists = os.path.exists(path) and gc.open_dataset(path) is not None
    access = 'overwrite' if exists else None

    before = gc._field_names(reader.dataset, reader.layer_name)
    try:
        ok = _translate(reader, path, plan.format, base, job, access, feedback)
    except Exception as error:
        if feedback.isCanceled():
            raise PasteError('Canceled.')
        if not exists:
            # A half-written new container is worse than none.
            _discard_new_container(path, to_gdb)
        raise PasteError(gc._first_line(error))
    if not ok:
        raise PasteError('the driver refused the layer')

    target_ds = gc.open_dataset(path)
    try:
        gc._record_renames(result, base, before,
                           gc._field_names(target_ds, base))
    finally:
        target_ds = None

    if plan.carry_styles and getattr(reader, 'container', ''):
        _carry_style(reader, path, to_gdb, reader.layer_name, base, result)

    result.written.append((source.name, base))
    result.container = path
    return True


def _carry_style(reader, path, to_gdb, source_name, target_name, result):
    # Styles are a courtesy; the data has already been written.
    with suppress(Exception):
        source_is_gdb = reader.container.lower().endswith(gc.GDB_SUFFIX)
        styles = [row for row in gc._collect_styles(reader.container,
                                                    source_is_gdb)
                  if row.get('f_table_name') == source_name]
        if styles:
            result.styles += gc._write_styles(
                path, to_gdb, styles, {source_name: target_name})


def _discard_new_container(path, is_folder):
    with suppress(Exception):
        gc._remove_container(path, is_folder)


# -------------------------------------------------------------------- rasters

def _paste_raster(source, target, plan, result, feedback):
    if target.kind != TARGET_FOLDER:
        result.skipped.append(
            (source.name, 'rasters can only be pasted into a folder'))
        return False
    gdal = gc._gdal()
    layer_path = source.uri.split('|', 1)[0]
    if not os.path.exists(layer_path):
        result.skipped.append(
            (source.name, 'only file-based rasters can be copied'))
        return False
    base = safe_file_name(os.path.splitext(os.path.basename(layer_path))[0]
                          or source.name)
    path = os.path.join(target.path, base + '.tif')
    if os.path.exists(path):
        if plan.on_clash == CLASH_SKIP:
            result.skipped.append((source.name, 'already exists'))
            return False
        if plan.on_clash == CLASH_REPLACE:
            if os.path.abspath(path) == os.path.abspath(layer_path):
                result.skipped.append(
                    (source.name, 'cannot replace a raster with itself'))
                return False
            os.remove(path)
        else:
            base = _unique_file(target.path, base, '.tif', plan.taken_files)
            path = os.path.join(target.path, base + '.tif')
    plan.taken_files.add(base.lower())

    def progress(_fraction, _message, _data):
        feedback.tick()
        return 0 if feedback.isCanceled() else 1

    try:
        gdal.Translate(path, layer_path, format='GTiff',
                       creationOptions=['COMPRESS=LZW', 'TILED=YES',
                                        'BIGTIFF=IF_SAFER'],
                       callback=progress)
    except Exception as error:
        if os.path.exists(path):
            os.remove(path)
        if feedback.isCanceled():
            raise PasteError('Canceled.')
        raise PasteError(gc._first_line(error))
    result.written.append((source.name, os.path.basename(path)))
    return True


# --------------------------------------------------------------------- paste

class _Plan(object):
    """Decisions made once per paste and shared by every layer in it."""

    def __init__(self, options, target):
        self.format = options.format if target.kind == TARGET_FOLDER \
            else (FMT_GPKG if target.kind == TARGET_GPKG else FMT_GDB)
        self.on_clash = options.on_clash
        self.carry_styles = options.carry_styles
        self.container = ''
        self.taken_layers = set()
        self.added_layers = set()
        self.taken_files = set()


def clashes(sources, target, options):
    """Names in `sources` the target already has, for the dialog to warn about.

    Matches what `paste` will actually do, so what the dialog promises is what
    happens.
    """
    found = []
    if target.kind == TARGET_FOLDER:
        fmt = options.format
        if fmt in FILE_FORMATS:
            for source in sources:
                ext = EXTENSION[fmt] if source.kind == KIND_VECTOR else '.tif'
                base = safe_file_name(_source_layer_name(source))
                if os.path.exists(os.path.join(target.path, base + ext)):
                    found.append(source.name)
            return found
        container = _container_in_folder(target, options, sources)
        if not os.path.exists(container):
            return found
        existing = _existing_layers(container)
        return _name_clashes(sources, existing, fmt)

    existing = _existing_layers(target.path)
    fmt = FMT_GPKG if target.kind == TARGET_GPKG else FMT_GDB
    return _name_clashes(sources, existing, fmt)


def _name_clashes(sources, existing, fmt):
    found = []
    for source in sources:
        if source.kind != KIND_VECTOR:
            continue
        name = _source_layer_name(source)
        base = gc.sanitize_gdb_name(name) if fmt == FMT_GDB \
            else gc.sanitize_gpkg_name(name)
        if base.lower() in existing:
            found.append(source.name)
    return found


def _source_layer_name(source):
    """The name a source's data will be saved under, before legalising it."""
    with suppress(Exception):
        parts = QgsProviderRegistry.instance().decodeUri(
            source.provider, source.uri)
        name = parts.get('layerName')
        if name:
            return str(name)
        path = parts.get('path') or ''
        if path:
            return os.path.splitext(os.path.basename(path))[0]
    return source.name


def default_container_name(sources):
    """A starting point for the .gpkg / .gdb a folder paste creates."""
    paths = set()
    for source in sources:
        if source.provider == 'ogr':
            try:
                path = QgsProviderRegistry.instance().decodeUri(
                    'ogr', source.uri).get('path') or ''
            except Exception:
                path = ''
            paths.add(path)
    if len(paths) == 1:
        only = next(iter(paths))
        if only:
            return safe_file_name(os.path.splitext(os.path.basename(only))[0])
    return 'pasted'


def _container_in_folder(target, options, sources):
    name = safe_file_name(options.container_name
                          or default_container_name(sources))
    ext = EXTENSION[options.format]
    if not name.lower().endswith(ext):
        name += ext
    return os.path.join(target.path, name)


def problem(sources, target, options=None):
    """Why this paste cannot go ahead, or '' when it can."""
    if not sources:
        return 'There is nothing to paste. Copy something first.'
    if target is None:
        return 'That item cannot be pasted into.'
    fmt = None
    if options is not None and target.kind == TARGET_FOLDER:
        fmt = options.format
    if target.kind == TARGET_GDB or fmt == FMT_GDB:
        if not gc.has_filegdb_write():
            return ('This QGIS build cannot write a File Geodatabase. '
                    'GDAL 3.6 or newer is required.')
        if target.kind == TARGET_GDB and gc.gdb_locks(target.path):
            return ('"{}" is open in ArcGIS (it has lock files). Close it '
                    'there first.'.format(target.label))
    return ''


def paste(sources, target, options, feedback):
    """Write every source into the target and return a `PasteResult`.

    `PasteError` is raised only for problems found before the first write;
    anything that goes wrong afterwards is recorded against that source and the
    paste carries on, so one bad layer in a long list does not cost the rest.
    """
    why = problem(sources, target, options)
    if why:
        raise PasteError(why)

    result = PasteResult()
    plan = _Plan(options, target)

    if target.kind == TARGET_FOLDER:
        if not os.path.isdir(target.path):
            raise PasteError('The folder "{}" no longer exists.'.format(
                target.path))
        if plan.format in (FMT_GPKG, FMT_GDB):
            plan.container = _container_in_folder(target, options, sources)
    else:
        plan.container = target.path

    if plan.container:
        existing = _existing_layers(plan.container) \
            if os.path.exists(plan.container) else set()
        plan.taken_layers = set(existing)
        if plan.format == FMT_GDB and os.path.isdir(plan.container) \
                and not gc.looks_like_filegdb(plan.container) \
                and os.listdir(plan.container):
            raise PasteError(
                '"{}" is a folder that is not a File Geodatabase.'.format(
                    plan.container))
        if plan.format == FMT_GDB and os.path.isdir(plan.container) \
                and not gc.looks_like_filegdb(plan.container):
            # An empty folder standing where the .gdb should go.
            os.rmdir(plan.container)

    total = len(sources)
    for index, source in enumerate(sources):
        if feedback.isCanceled():
            result.canceled = True
            break
        feedback.step(index * 100.0 / total, 'Pasting {} ({} of {})'.format(
            source.name, index + 1, total))
        try:
            if source.kind == KIND_RASTER:
                _paste_raster(source, target, plan, result, feedback)
            else:
                _paste_vector(source, target, options, plan, result, feedback)
        except PasteError as error:
            if feedback.isCanceled():
                result.canceled = True
                break
            result.failed.append((source.name, str(error)))
            feedback.reportError('{}: {}'.format(source.name, error))
        except Exception as error:
            result.failed.append((source.name, gc._first_line(error)))
            feedback.reportError('{}: {}'.format(source.name,
                                                 gc._first_line(error)))

    feedback.step(100, 'Done')
    return result

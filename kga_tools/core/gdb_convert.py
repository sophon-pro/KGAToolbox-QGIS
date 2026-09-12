# -*- coding: utf-8 -*-
"""GeoPackage <-> File Geodatabase, a whole container at a time.

QGIS reads a File Geodatabase well enough but cannot edit one, so work that
starts life in ArcGIS has to be copied into something writable before anything
can happen, and copied back afterwards. This module is that copy, in both
directions, with the things a layer-by-layer export loses put back:

* **Field domains travel.** ``gdal.VectorTranslate`` copies every domain a
  copied field references, so coded and range domains survive both ways.
* **Names are made legal, and the original is kept as the ArcGIS alias.**
  A GeoPackage layer called ``2024 roads-final`` becomes the table
  ``T_2024_roads_final`` with alias ``2024 roads-final``; converting back reads
  the alias and restores the original name.
* **The output looks like ArcGIS made it.** ``OBJECTID`` as the OID column,
  optional ``Shape_Length`` / ``Shape_Area``, optional feature dataset.
* **QGIS styles survive the round trip**, parked in a ``KGA_layer_styles``
  table inside the geodatabase and unpacked back into ``layer_styles`` on the
  way home.

Everything below was probed against GDAL 3.13 rather than assumed. The five
findings that shaped the code:

1. ``accessMode='overwrite'`` is per *layer*, not per container, and works both
   for a layer that already exists and for one that does not, so every write
   after the first can use it unconditionally.
2. OpenFileGDB defaults its OID column to ``OBJECTID``, but VectorTranslate
   passes the source FID column name straight through, so a GeoPackage source
   produces a geodatabase whose OID column is called ``fid`` unless
   ``FID=OBJECTID`` is forced.
3. 64-bit integers silently become doubles unless
   ``TARGET_ARCGIS_VERSION=ARCGIS_PRO_3_2_OR_LATER`` is set - which in turn
   makes the output unreadable by ArcMap. It has to be the user's choice.
4. OGR does not list ``layer_styles`` among a GeoPackage's layers, so styles
   are invisible to VectorTranslate and have to be moved by hand.
5. Returning 0 from the GDAL progress callback aborts the translation by
   raising, so cancelling has to be caught rather than tested for.
"""

import copy
import os
import re
import shutil
import sqlite3

GPKG_DRIVER = 'GPKG'
GDB_DRIVER = 'OpenFileGDB'

GPKG_SUFFIX = '.gpkg'
GDB_SUFFIX = '.gdb'

#: Where QGIS keeps styles in a GeoPackage, and where we park them in a .gdb.
STYLE_TABLE_GPKG = 'layer_styles'
STYLE_TABLE_GDB = 'KGA_layer_styles'

STYLE_COLUMNS = ('f_table_name', 'f_geometry_column', 'styleName',
                 'styleQML', 'styleSLD', 'useAsDefault', 'description')

# Spelled out rather than built from STYLE_COLUMNS so that no SQL statement in
# this module is assembled at run time. Keep the three in step: the column list
# below is STYLE_COLUMNS in order, and the placeholder count matches it.
STYLE_SELECT_SQL = ('SELECT f_table_name, f_geometry_column, styleName, '
                    'styleQML, styleSLD, useAsDefault, description '
                    'FROM layer_styles')
STYLE_INSERT_SQL = ('INSERT INTO layer_styles '
                    '(f_table_name, f_geometry_column, styleName, '
                    'styleQML, styleSLD, useAsDefault, description) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)')

# Tables OGR may list that are not user data.
GPKG_SKIP_PREFIXES = ('gpkg_', 'rtree_', 'sqlite_')
GPKG_SKIP_NAMES = frozenset((STYLE_TABLE_GPKG.lower(), STYLE_TABLE_GDB.lower()))
GDB_SKIP_PREFIXES = ('gdb_',)
GDB_SKIP_NAMES = frozenset((STYLE_TABLE_GDB.lower(),))

GDB_MAX_NAME = 160

#: Written by OpenFileGDB itself; never report them as a renamed source field.
GDB_ADDED_FIELDS = frozenset(('shape_area', 'shape_length'))

ARCGIS_ALL = ''
ARCGIS_PRO_32 = 'ARCGIS_PRO_3_2_OR_LATER'


class ConversionError(Exception):
    """Something that stops a conversion before any data is written."""


# --------------------------------------------------------------- GDAL access

def _gdal():
    try:
        from osgeo import gdal
    except ImportError:                     # pragma: no cover - broken install
        raise ConversionError(
            'The GDAL Python bindings are not available in this QGIS build, '
            'so this tool cannot run.')
    return gdal


def _ogr():
    try:
        from osgeo import ogr
    except ImportError:                     # pragma: no cover - broken install
        raise ConversionError(
            'The GDAL Python bindings are not available in this QGIS build, '
            'so this tool cannot run.')
    return ogr


def has_filegdb_write():
    """True when this GDAL can *create* a File Geodatabase (needs GDAL 3.6+)."""
    try:
        driver = _gdal().GetDriverByName(GDB_DRIVER)
    except ConversionError:
        return False
    return bool(driver) and driver.GetMetadataItem('DCAP_CREATE') == 'YES'


def open_dataset(path, update=False):
    """A vector dataset for `path`, or None. Never raises.

    QGIS turns GDAL's Python exceptions on, so an OpenEx that would once have
    returned None now throws; every open in the plugin is guarded the same way.
    """
    gdal = _gdal()
    if not path or not os.path.exists(path):
        return None
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else 0)
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    try:
        try:
            return gdal.OpenEx(path, flags)
        except Exception:
            return None
    finally:
        gdal.PopErrorHandler()


# ------------------------------------------------------------- path checking

def looks_like_filegdb(path):
    """True when `path` is a directory holding File Geodatabase tables."""
    if not path or not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(name.lower().endswith(('.gdbtable', '.gdbindexes')) or
               name.lower() in ('gdb', 'timestamps') for name in names)


def gdb_locks(path):
    """Lock files ArcGIS leaves behind while it has the geodatabase open."""
    if not path or not os.path.isdir(path):
        return []
    try:
        return sorted(n for n in os.listdir(path) if n.lower().endswith('.lock'))
    except OSError:
        return []


def ensure_suffix(path, suffix):
    return path if path.lower().endswith(suffix) else path + suffix


def _remove_container(path, is_folder):
    """Delete an output container, refusing anything that is not one."""
    if is_folder:
        if not os.path.isdir(path):
            return
        if os.listdir(path) and not looks_like_filegdb(path):
            raise ConversionError(
                'Refusing to replace "{}": it is a folder with files in it, '
                'but not a File Geodatabase. Choose another output path, or '
                'empty the folder yourself.'.format(path))
        try:
            shutil.rmtree(path)
        except OSError as error:
            raise ConversionError(
                'Could not replace "{}": {}. Close it in ArcGIS and in the '
                'QGIS browser first.'.format(path, error))
        return

    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        raise ConversionError(
            'Refusing to replace "{}": a folder is standing where the '
            'GeoPackage should go.'.format(path))
    try:
        os.remove(path)
    except OSError as error:
        raise ConversionError(
            'Could not replace "{}": {}. Something still has it open - QGIS '
            'itself, ArcGIS, or another program.'.format(path, error))
    # SQLite leaves these behind after a crash, and a stale one corrupts the
    # new database that reuses the name.
    for extra in ('-wal', '-shm', '-journal'):
        side = path + extra
        if os.path.exists(side):
            try:
                os.remove(side)
            except OSError:
                pass


# ------------------------------------------------------------------ describe

class LayerInfo(object):
    """One layer in a source container, before anything is decided about it."""

    def __init__(self, name, alias='', feature_dataset='', geometry='',
                 crs='', feature_count=0):
        self.name = name
        self.alias = alias or ''
        self.feature_dataset = feature_dataset or ''
        self.geometry = geometry or ''
        self.crs = crs or ''
        self.feature_count = feature_count

    @property
    def is_spatial(self):
        return bool(self.geometry)

    def label(self):
        """'Multi Polygon, EPSG:32648, 240 features', for the run log."""
        bits = [self.geometry or 'table']
        if self.crs:
            bits.append(self.crs)
        bits.append('{} feature{}'.format(
            self.feature_count, '' if self.feature_count == 1 else 's'))
        return ', '.join(bits)


def _feature_dataset_map(dataset):
    """``{layer name: feature dataset}`` for a File Geodatabase.

    A .gdb's feature datasets appear as sub-groups of the dataset's root group.
    A GeoPackage has no root group at all, so this is empty for one.
    """
    result = {}
    try:
        root = dataset.GetRootGroup()
    except Exception:                       # pragma: no cover - GDAL < 3.4
        return result
    if root is None:
        return result
    try:
        for group_name in (root.GetGroupNames() or []):
            group = root.OpenGroup(group_name)
            if group is None:
                continue
            for layer_name in (group.GetVectorLayerNames() or []):
                result[layer_name] = group_name
    except Exception:
        return {}
    return result


def _is_system_layer(name, is_gdb):
    low = (name or '').lower()
    if is_gdb:
        return low.startswith(GDB_SKIP_PREFIXES) or low in GDB_SKIP_NAMES
    return low.startswith(GPKG_SKIP_PREFIXES) or low in GPKG_SKIP_NAMES


def describe(path, is_gdb=None):
    """``([LayerInfo, ...], [domain name, ...])`` for a container.

    Counting features is what makes "skip empty layers" and the preview in the
    log possible. Both drivers answer from a header rather than by scanning, so
    it stays cheap even on a large geodatabase.
    """
    ogr = _ogr()
    if is_gdb is None:
        is_gdb = os.path.isdir(path)

    dataset = open_dataset(path)
    if dataset is None:
        raise ConversionError('Could not open "{}" as a {}.'.format(
            path, 'File Geodatabase' if is_gdb else 'GeoPackage'))

    try:
        datasets = _feature_dataset_map(dataset)
        layers = []
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayer(index)
            name = layer.GetName()
            if _is_system_layer(name, is_gdb):
                continue

            crs = ''
            reference = layer.GetSpatialRef()
            if reference is not None:
                authority = reference.GetAuthorityName(None)
                code = reference.GetAuthorityCode(None)
                crs = '{}:{}'.format(authority, code) if authority and code \
                    else (reference.GetName() or '')

            geometry_type = layer.GetGeomType()
            try:
                geometry = '' if geometry_type == ogr.wkbNone \
                    else ogr.GeometryTypeToName(geometry_type)
            except Exception:
                geometry = ''

            layers.append(LayerInfo(
                name=name,
                alias=layer.GetMetadataItem('ALIAS_NAME') or '',
                feature_dataset=datasets.get(name, ''),
                geometry=geometry,
                crs=crs,
                feature_count=layer.GetFeatureCount(),
            ))

        try:
            domains = sorted(dataset.GetFieldDomainNames() or [])
        except Exception:                   # pragma: no cover - GDAL < 3.5
            domains = []
    finally:
        dataset = None

    return layers, domains


# -------------------------------------------------------------------- naming

def sanitize_gdb_name(name):
    """A File Geodatabase table name: letters, digits and underscores only."""
    clean = re.sub(r'[^0-9A-Za-z_]', '_', name or '')
    clean = re.sub(r'_+', '_', clean).strip('_')
    if not clean:
        clean = 'layer'
    if clean[0].isdigit():
        clean = 'T_' + clean
    if clean.lower().startswith(GDB_SKIP_PREFIXES):
        clean = 'T_' + clean
    return clean[:GDB_MAX_NAME]


def sanitize_gpkg_name(name):
    """A GeoPackage table name. Far more permissive: keeps spaces and Khmer."""
    clean = re.sub(r'[\x00-\x1f"\'`\\]', '_', name or '')
    clean = re.sub(r'\s+', ' ', clean).strip()
    if not clean:
        clean = 'layer'
    low = clean.lower()
    if low.startswith(GPKG_SKIP_PREFIXES) or low in GPKG_SKIP_NAMES:
        clean = 'x_' + clean
    return clean


def unique_name(name, taken):
    """`name`, suffixed until it is free. Records the result in `taken`."""
    if name.lower() not in taken:
        taken.add(name.lower())
        return name
    index = 2
    while '{}_{}'.format(name, index).lower() in taken:
        index += 1
    result = '{}_{}'.format(name, index)
    taken.add(result.lower())
    return result


# ------------------------------------------------------------------ settings

class Settings(object):
    """Everything the two algorithms can vary, with working defaults."""

    def __init__(self, **overrides):
        self.target_format = GPKG_DRIVER
        #: Source layer names to convert; empty means every one of them.
        self.only = ()
        self.append = False
        self.skip_empty = False
        self.linearize = False
        self.preserve_fid = True
        self.carry_styles = True

        # File Geodatabase -> GeoPackage
        self.use_aliases = True
        self.dataset_prefix = False

        # GeoPackage -> File Geodatabase
        self.feature_dataset = ''
        self.arcgis_version = ARCGIS_ALL
        self.shape_fields = True
        self.set_aliases = True

        for key, value in overrides.items():
            if not hasattr(self, key):
                raise TypeError('Unknown conversion setting: {}'.format(key))
            setattr(self, key, value)

    @property
    def to_gdb(self):
        return self.target_format == GDB_DRIVER


class Result(object):
    """What a conversion did, for the summary and for the caller."""

    def __init__(self):
        self.written = []          # [(source name, target name), ...]
        self.skipped = []          # [(source name, reason), ...]
        self.failed = []           # [(source name, message), ...]
        self.renamed_fields = []   # [(layer, source field, target field), ...]
        self.domains = []
        self.styles = 0
        self.canceled = False

    @property
    def count(self):
        return len(self.written)


# ------------------------------------------------------------------ planning

class _Job(object):
    def __init__(self, source, target, alias, feature_dataset):
        self.source = source
        self.target = target
        self.alias = alias
        self.feature_dataset = feature_dataset


def plan(layers, settings, feedback=None):
    """Decide every layer's target name before anything is written.

    Doing it up front is what makes the duplicate handling predictable: a
    geodatabase can hold ``Roads`` in two different feature datasets, and both
    have to land in one flat GeoPackage.
    """
    wanted = {n.strip().lower() for n in (settings.only or ()) if n.strip()}
    jobs, skipped = [], []
    taken = set()

    for info in layers:
        if wanted and info.name.lower() not in wanted:
            continue
        if settings.skip_empty and info.feature_count == 0:
            skipped.append((info.name, 'no features'))
            continue

        if settings.to_gdb:
            base = sanitize_gdb_name(info.name)
            alias = info.name if (settings.set_aliases and base != info.name) \
                else ''
            dataset = settings.feature_dataset if info.is_spatial else ''
        else:
            base = info.alias if (settings.use_aliases and info.alias) \
                else info.name
            if settings.dataset_prefix and info.feature_dataset:
                base = '{} - {}'.format(info.feature_dataset, base)
            base = sanitize_gpkg_name(base)
            alias, dataset = '', ''

        jobs.append(_Job(info.name, unique_name(base, taken), alias, dataset))

    missing = wanted - {job.source.lower() for job in jobs} \
        - {name.lower() for name, _ in skipped}
    if missing and feedback is not None:
        feedback.pushWarning('Not found in the source: {}'.format(
            ', '.join(sorted(missing))))

    return jobs, skipped


# ---------------------------------------------------------------- converting

def _layer_creation_options(settings, job):
    """The ``-lco`` list for one layer."""
    if not settings.to_gdb:
        options = ['FID=fid', 'SPATIAL_INDEX=YES']
        if job.source != job.target:
            # Keeps the geodatabase's own table name visible in the browser.
            options.append('IDENTIFIER={}'.format(job.source))
        return options

    options = ['FID=OBJECTID']
    if settings.arcgis_version:
        options.append('TARGET_ARCGIS_VERSION={}'.format(settings.arcgis_version))
    if settings.shape_fields:
        options.append('CREATE_SHAPE_AREA_AND_LENGTH_FIELDS=YES')
    if job.alias:
        options.append('LAYER_ALIAS={}'.format(job.alias))
    if job.feature_dataset:
        options.append('FEATURE_DATASET={}'.format(job.feature_dataset))
    return options


def _geometry_type(settings):
    types = []
    if settings.linearize:
        types.append('CONVERT_TO_LINEAR')
    if settings.to_gdb:
        # Every ArcGIS line and polygon class is multi-part. Writing singles
        # into one works, but makes the first edit in ArcGIS a type change.
        types.append('PROMOTE_TO_MULTI')
    return types or None


def _field_names(dataset, name):
    if dataset is None:
        return []
    layer = dataset.GetLayerByName(name)
    if layer is None:
        return []
    definition = layer.GetLayerDefn()
    return [definition.GetFieldDefn(i).GetName()
            for i in range(definition.GetFieldCount())]


def _record_renames(result, layer_name, before, after):
    """Report fields GDAL had to launder, e.g. '1st field' -> '_1st_field'.

    Matched by name rather than by position: OpenFileGDB puts the Shape_Area
    and Shape_Length fields it adds itself in front of the copied ones, so the
    positions do not line up. Relative order is preserved, so zipping the
    leftovers pairs each renamed field with the name it ended up with.
    """
    kept = {name.lower() for name in after}
    lost = [name for name in before if name.lower() not in kept]
    if not lost:
        return
    known = {name.lower() for name in before} | GDB_ADDED_FIELDS
    gained = [name for name in after if name.lower() not in known]
    for old, new in zip(lost, gained):
        result.renamed_fields.append((layer_name, old, new))


def _first_line(error):
    text = str(error).strip()
    return text.splitlines()[-1] if text else error.__class__.__name__


def _translate(target, source_ds, job, settings, access, feedback):
    """One VectorTranslate call, with cancellation wired into the callback."""
    gdal = _gdal()

    def progress(_fraction, _message, _data):
        return 0 if feedback.isCanceled() else 1

    options = gdal.VectorTranslateOptions(
        format=settings.target_format,
        layers=[job.source],
        layerName=job.target,
        accessMode=access,
        layerCreationOptions=_layer_creation_options(settings, job),
        geometryType=_geometry_type(settings),
        preserveFID=settings.preserve_fid,
        resolveDomains=False,
        callback=progress,
    )
    written = gdal.VectorTranslate(target, source_ds, options=options)
    ok = written is not None
    written = None
    return ok


def convert(source_path, target_path, settings, feedback):
    """Copy every requested layer from one container into the other.

    Returns a `Result`. `ConversionError` is raised only for problems found
    before the first layer is written; anything that goes wrong afterwards is
    recorded against that layer and the run continues, so one bad layer in a
    sixty-layer geodatabase does not cost the other fifty-nine.
    """
    result = Result()
    to_gdb = settings.to_gdb
    source_is_gdb = os.path.isdir(source_path)

    if to_gdb and not has_filegdb_write():
        raise ConversionError(
            'This QGIS build has no OpenFileGDB write driver, so it cannot '
            'create a File Geodatabase. GDAL 3.6 or newer is required.')

    if not source_path or not os.path.exists(source_path):
        raise ConversionError('Input not found: {}'.format(source_path))
    if source_is_gdb and not looks_like_filegdb(source_path):
        raise ConversionError(
            '"{}" is a folder, but not a File Geodatabase - there are no '
            '.gdbtable files in it.'.format(source_path))

    locks = gdb_locks(source_path) if source_is_gdb else []
    if locks:
        feedback.pushWarning(
            'The geodatabase has {} lock file(s); it is open in ArcGIS. Close '
            'it there first, or you may copy a half-saved edit.'.format(len(locks)))

    target_path = ensure_suffix(target_path,
                                GDB_SUFFIX if to_gdb else GPKG_SUFFIX)
    if os.path.abspath(target_path) == os.path.abspath(source_path):
        raise ConversionError('The input and the output are the same path.')

    parent = os.path.dirname(target_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    layers, domains = describe(source_path, source_is_gdb)
    if not layers:
        raise ConversionError(
            'There are no layers to convert in {}.'.format(source_path))

    jobs, skipped = plan(layers, settings, feedback)
    result.skipped.extend(skipped)
    result.domains = domains
    if not jobs:
        raise ConversionError(
            'Every layer was filtered out; there is nothing left to convert.')

    styles = _collect_styles(source_path, source_is_gdb) \
        if settings.carry_styles else []

    present = os.path.isdir(target_path) if to_gdb else os.path.isfile(target_path)
    if present and not settings.append:
        feedback.pushInfo('Replacing the existing output.')
        _remove_container(target_path, to_gdb)
        present = False

    # `exists` drives accessMode, so it has to mean "there is a dataset here to
    # add a layer to", not merely "the path is taken". Processing creates the
    # destination folder for a .gdb before the algorithm runs, and an empty
    # folder is a path with no dataset in it.
    exists = present and open_dataset(target_path) is not None
    if settings.append and not exists:
        feedback.pushInfo('Nothing to add to yet; creating the output.')

    source_ds = open_dataset(source_path)
    if source_ds is None:
        raise ConversionError('Could not open {}'.format(source_path))

    feedback.pushInfo('{} layer(s) to convert into {}'.format(
        len(jobs), target_path))
    if domains:
        feedback.pushInfo('Field domains travelling with them: {}'.format(
            ', '.join(domains)))
    feedback.pushInfo('')

    by_name = {info.name: info for info in layers}
    dataset_crs = _feature_dataset_crs(layers, jobs) if to_gdb else None
    total = len(jobs)

    try:
        for index, job in enumerate(jobs):
            if feedback.isCanceled():
                result.canceled = True
                break
            feedback.setProgress(index * 100.0 / total)

            if job.feature_dataset and dataset_crs is not None:
                info = by_name.get(job.source)
                if info is not None and info.crs != dataset_crs:
                    feedback.pushWarning(
                        '  {}: its CRS ({}) does not match the feature '
                        'dataset ({}), so it goes outside the feature '
                        'dataset.'.format(job.source, info.crs or 'none',
                                          dataset_crs))
                    job.feature_dataset = ''

            before = _field_names(source_ds, job.source)
            access = 'overwrite' if exists else None
            ok, message = False, ''

            try:
                ok = _translate(target_path, source_ds, job, settings,
                                access, feedback)
            except Exception as error:
                if feedback.isCanceled():
                    result.canceled = True
                    break
                message = _first_line(error)
                if settings.preserve_fid:
                    # Duplicate or non-positive ids in the source are the usual
                    # cause, and the copy is perfectly good without them.
                    feedback.pushWarning(
                        '  {}: could not keep the original feature ids; '
                        'retrying with fresh ones.'.format(job.source))
                    relaxed = copy.copy(settings)
                    relaxed.preserve_fid = False
                    try:
                        ok = _translate(target_path, source_ds, job, relaxed,
                                        access, feedback)
                    except Exception as retry_error:
                        message = _first_line(retry_error)

            if not ok:
                feedback.reportError('  FAILED: {} -> {}'.format(
                    job.source, message or 'the driver refused the layer'))
                result.failed.append((job.source, message))
                continue

            exists = True
            result.written.append((job.source, job.target))

            target_ds = open_dataset(target_path)
            try:
                _record_renames(result, job.target, before,
                                _field_names(target_ds, job.target))
            finally:
                target_ds = None

            note = '' if job.source == job.target \
                else '  (was "{}")'.format(job.source)
            info = by_name.get(job.source)
            feedback.pushInfo('  ok: {}  [{}]{}'.format(
                job.target, info.label() if info else '', note))
    finally:
        source_ds = None

    if styles and result.written and not result.canceled:
        renames = dict(result.written)
        try:
            result.styles = _write_styles(target_path, to_gdb, styles, renames)
        except Exception as error:
            feedback.pushWarning(
                'The layer styles could not be carried across: {}'.format(error))

    feedback.setProgress(100)
    _summarize(result, feedback)
    return result


def _feature_dataset_crs(layers, jobs):
    """The CRS the feature dataset gets pinned to: the first spatial layer's.

    A File Geodatabase feature dataset holds one CRS for everything inside it,
    so mixing them has to be caught rather than attempted.
    """
    wanted = {job.source for job in jobs if job.feature_dataset}
    for info in layers:
        if info.name in wanted and info.is_spatial:
            return info.crs
    return None


def _summarize(result, feedback):
    feedback.pushInfo('')
    if result.renamed_fields:
        feedback.pushInfo('Field names changed to fit the target format:')
        for layer, old, new in result.renamed_fields:
            feedback.pushInfo('  {}: "{}" -> "{}"'.format(layer, old, new))
    for name, reason in result.skipped:
        feedback.pushInfo('Skipped {} ({})'.format(name, reason))
    if result.styles:
        feedback.pushInfo('Carried {} layer style(s) across.'.format(result.styles))
    feedback.pushInfo('Converted {} | Skipped {} | Failed {}'.format(
        result.count, len(result.skipped), len(result.failed)))
    if result.canceled:
        feedback.pushWarning(
            'Canceled; the output holds only what is listed above.')


# --------------------------------------------------------------------- styles

def _gpkg_connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _read_gpkg_styles(path):
    """QGIS style rows out of a GeoPackage, or [] when it keeps none."""
    if not path or not os.path.isfile(path):
        return []
    try:
        connection = _gpkg_connect(path)
    except sqlite3.Error:
        return []
    try:
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (STYLE_TABLE_GPKG,))
        if cursor.fetchone() is None:
            return []
        rows = connection.execute(STYLE_SELECT_SQL).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [{key: row[key] for key in STYLE_COLUMNS} for row in rows]


def _read_gdb_styles(path):
    """Style rows out of the KGA_layer_styles table a previous run parked."""
    dataset = open_dataset(path)
    if dataset is None:
        return []
    try:
        layer = dataset.GetLayerByName(STYLE_TABLE_GDB)
        if layer is None:
            return []
        definition = layer.GetLayerDefn()
        present = {definition.GetFieldDefn(i).GetName()
                   for i in range(definition.GetFieldCount())}
        rows = []
        layer.ResetReading()
        feature = layer.GetNextFeature()
        while feature is not None:
            rows.append({key: (feature.GetField(key) if key in present else None)
                         for key in STYLE_COLUMNS})
            feature = layer.GetNextFeature()
        return rows
    except Exception:
        return []
    finally:
        dataset = None


def _collect_styles(path, is_gdb):
    return _read_gdb_styles(path) if is_gdb else _read_gpkg_styles(path)


def _write_styles(path, to_gdb, styles, renames):
    """Re-point the style rows at their new layer names, then store them."""
    moved = []
    for row in styles:
        table = row.get('f_table_name')
        if table not in renames:
            continue
        row = dict(row)
        row['f_table_name'] = renames[table]
        moved.append(row)
    if not moved:
        return 0
    return _write_gdb_styles(path, moved) if to_gdb \
        else _write_gpkg_styles(path, moved)


def _write_gdb_styles(path, styles):
    ogr = _ogr()
    dataset = open_dataset(path, update=True)
    if dataset is None:
        raise ConversionError(
            'Could not reopen {} to store the styles.'.format(path))
    try:
        if dataset.GetLayerByName(STYLE_TABLE_GDB) is not None:
            dataset.DeleteLayer(STYLE_TABLE_GDB)
        layer = dataset.CreateLayer(STYLE_TABLE_GDB, None, ogr.wkbNone,
                                    options=['FID=OBJECTID'])
        for column in STYLE_COLUMNS:
            if column == 'useAsDefault':
                field = ogr.FieldDefn(column, ogr.OFTInteger)
                field.SetSubType(ogr.OFSTBoolean)
            else:
                field = ogr.FieldDefn(column, ogr.OFTString)
                # A QML document runs to hundreds of kilobytes. Width 0 is the
                # unlimited text type, and round-trips one intact.
                field.SetWidth(0 if column in ('styleQML', 'styleSLD') else 255)
            layer.CreateField(field)

        definition = layer.GetLayerDefn()
        for row in styles:
            feature = ogr.Feature(definition)
            for column in STYLE_COLUMNS:
                value = row.get(column)
                if value is None:
                    continue
                if column == 'useAsDefault':
                    feature.SetField(column, 1 if value else 0)
                else:
                    feature.SetField(column, str(value))
            layer.CreateFeature(feature)
        return len(styles)
    finally:
        dataset = None


def _write_gpkg_styles(path, styles):
    connection = _gpkg_connect(path)
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS layer_styles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                f_table_catalog TEXT, f_table_schema TEXT,
                f_table_name TEXT, f_geometry_column TEXT,
                styleName TEXT, styleQML TEXT, styleSLD TEXT,
                useAsDefault BOOLEAN, description TEXT, owner TEXT,
                ui TEXT, update_time DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for row in styles:
            connection.execute(
                'DELETE FROM layer_styles '
                'WHERE f_table_name = ? AND styleName IS ?',
                (row.get('f_table_name'), row.get('styleName')))
            connection.execute(
                STYLE_INSERT_SQL,
                [row.get(column) for column in STYLE_COLUMNS])
        connection.commit()
        return len(styles)
    finally:
        connection.close()

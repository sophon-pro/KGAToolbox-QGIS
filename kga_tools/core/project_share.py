# -*- coding: utf-8 -*-
"""Share Workspace: write a `.kgapp` project package.

The method is the one ArcGIS Pro uses for a `.ppkx`: build the package contents as
an ordinary project folder in a staging area, with every data source pointing at
a copy *inside* that folder and every path stored relative, then zip the folder
with a manifest. Opening the package is then just unpacking it.

How the pieces are chosen (see `project_analysis` for the classification):

* **Everything** copies the whole project folder (so files no layer uses come
  too), every database in it, the toolbox and the layouts folder. **Map data
  only** copies just the tables and files that layers use, plus the relevant
  data layers depend on.
* A layer's data that lives **outside** the project is consolidated: vector data
  into the package's `Default.gpkg`, rasters and other files into `commondata/`.
  Scratch layers, delimited text and (when asked) remote databases are copied
  into `Default.gpkg` as tables.
* The packaged project is rewritten on an **independent `QgsProject`** read from the
  saved file, so the open project is never touched. `setDataSource` re-points a
  layer without losing its style, joins or settings (verified on QGIS 3.44), and
  `Qgis.FilePathType.Relative` stores every path relative to the new `.qgz`.
* Symbol SVGs and layout pictures are collected into `resources/`.
* Layers that cannot be shared stay in the project, flagged, and are listed in the
  manifest and the report. Credentials are never packaged.
* Afterwards the written project is re-opened and searched for paths that still
  point at this computer, and the result goes in the warnings.

Everything is written to a staging folder next to the output and the finished
package is moved into place last, so a failure leaves no half-written `.kgapp`.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsDataProvider,
    QgsMapSettings,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import data_paste as dp
from . import gdb_convert as gc
from . import project_analysis as pa
from . import projectpkg as pp
from . import workspace as ws

SKIP_DIRS = {'__pycache__', '.git', '.svn', '.idea', '.vscode', '.vs'}
SKIP_NAMES = {ws.MARKER.lower(), 'thumbs.db', 'desktop.ini', '.ds_store'}
SKIP_SUFFIXES = ('.qgz', '.qgs', '.qgz~', '.qgs~', '.kgapp', '.kgapp.part',
                 '.bak', '.tmp', '-wal', '-shm', '-journal', '.lock', '.crdownload')
#: Already compressed: storing them deflated again only costs time.
STORED_SUFFIXES = ('.jp2', '.ecw', '.png', '.jpg', '.jpeg', '.zip', '.gz', '.laz',
                   '.copc', '.mbtiles', '.kgalp', '.kgapp', '.7z', '.mp4', '.webp')

DEFAULT_DB = ws.DEFAULT_DB
COMMONDATA = 'commondata'
RESOURCES = 'resources'
CHUNK = pp.CHUNK


class ShareError(pp.ProjectPackageError):
    """Sharing could not be completed; the message is for the user."""


# ------------------------------------------------------------------- options

class ShareOptions(object):
    """What the user chose in the Share Workspace dialog."""

    def __init__(self, output, **overrides):
        self.output = output
        self.mode = pp.MODE_EVERYTHING
        self.info = pp.PackageInfo()
        self.include_toolbox = True         # Toolbox scripts
        self.include_layouts_folder = True  # the project's Layouts folder
        self.copy_remote = False            # snapshot PostGIS / WFS ... layers
        self.thumbnail = None               # a QImage, or None
        self.attached_databases = []        # extra databases of the workspace
        self.default_database = ''          # the workspace's default, if chosen
        self.destination = ''               # build a workspace folder here, not a zip
        self.default_folder = ''
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise TypeError('Unknown share option: {}'.format(key))
            setattr(self, key, value)


class ShareResult(object):

    def __init__(self, path):
        self.path = path
        self.size = 0
        self.manifest = {}
        self.warnings = []
        self.layers = []                    # manifest layer entries
        self.invalid_after = []             # layers that do not load from the package

    def count(self, status):
        return sum(1 for layer in self.layers if layer['status'] == status)


# ------------------------------------------------------------ small utilities

def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _posix(path):
    return path.replace('\\', '/')


def safe_name(name, fallback='project'):
    cleaned = dp.safe_file_name(name or '')
    return cleaned if cleaned and cleaned != 'layer' else fallback


def default_output_path(name):
    """Where a package called `name` goes unless the user says otherwise."""
    folder = os.path.join(ws.default_location(), 'Packages')
    return os.path.join(folder, safe_name(name) + pp.EXTENSION)


def copy_sqlite(source, target):
    """Copy a GeoPackage / SQLite file consistently, even while QGIS has it open.

    A plain file copy can miss what is still in the write-ahead log. SQLite's
    online-backup API reads a consistent snapshot instead.
    """
    os.makedirs(os.path.dirname(target), exist_ok=True)
    reader = sqlite3.connect('file:{}?mode=ro'.format(_posix(source)), uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def copy_file(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if source.lower().endswith(('.gpkg', '.sqlite')):
        copy_sqlite(source, target)
    else:
        shutil.copy2(source, target)


def remove_tree(path):
    """Delete a folder tree, retrying briefly for files that are still closing."""
    import time
    for attempt in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(0.1 * (attempt + 1))
    return False


def unique_path(path):
    """`path`, or `path_2` ... when it already exists."""
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    counter = 2
    while os.path.exists('{}_{}{}'.format(stem, counter, extension)):
        counter += 1
    return '{}_{}{}'.format(stem, counter, extension)


def gpkg_table_names(path):
    """Lower-cased table names in a GeoPackage (empty when it has none)."""
    if not os.path.isfile(path):
        return set()
    try:
        connection = sqlite3.connect('file:{}?mode=ro'.format(_posix(path)),
                                     uri=True)
    except sqlite3.Error:
        return set()
    try:
        return {row[0].lower() for row in connection.execute(
            'SELECT name FROM sqlite_master WHERE type IN (?, ?)',
            ('table', 'view'))}
    except sqlite3.Error:
        return set()
    finally:
        connection.close()


def write_layer_to_gpkg(layer, gpkg, table):
    """Write the features of a live `layer` into `gpkg` as `table`."""
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.layerName = table
    options.fileEncoding = 'UTF-8'
    options.actionOnExistingFile = \
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, gpkg, QgsProject.instance().transformContext(), options)
    code = result[0] if isinstance(result, (tuple, list)) else result
    if code != QgsVectorFileWriter.WriterError.NoError:
        message = result[1] if isinstance(result, (tuple, list)) \
            and len(result) > 1 else ''
        raise ShareError('could not copy the layer "{}": {}'.format(
            layer.name(), message or 'writer error {}'.format(code)))


def plugin_version():
    """The KGA Toolbox version from metadata.txt, or ''."""
    with suppress(OSError):
        folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(folder, 'metadata.txt'), encoding='utf-8') as handle:
            for line in handle:
                if line.lower().startswith('version='):
                    return line.split('=', 1)[1].strip()
    return ''


def encode_ogr(path, table='', **extra):
    parts = {'path': path}
    if table:
        parts['layerName'] = table
    parts.update(extra)
    return QgsProviderRegistry.instance().encodeUri('ogr', parts)


# ----------------------------------------------------------------- thumbnail

def render_thumbnail(project, size=(480, 320), extent=None):
    """A QImage of the project's local layers, or None if nothing can be drawn.

    Web layers are left out: a thumbnail must not wait on the network.
    """
    with suppress(Exception):
        from qgis.core import QgsMapRendererSequentialJob
        from qgis.PyQt.QtCore import QSize
        from qgis.PyQt.QtGui import QColor
        layers = [layer for layer in project.layerTreeRoot().checkedLayers()
                  if layer.providerType() not in ('wms', 'wcs', 'arcgismapserver',
                                                  'vectortile', 'xyzvectortiles',
                                                  'tiledscene')
                  and layer.isValid()]
        if not layers:
            return None
        settings = QgsMapSettings()
        settings.setLayers(layers)
        settings.setOutputSize(QSize(*size))
        settings.setBackgroundColor(QColor('white'))
        settings.setDestinationCrs(project.crs() if project.crs().isValid()
                                   else layers[0].crs())
        box = extent
        if box is None or box.isEmpty():
            box = settings.fullExtent() if hasattr(settings, 'fullExtent') \
                else layers[0].extent()
        if box.isEmpty():
            return None
        box = box.buffered(max(box.width(), box.height()) * 0.05 or 1)
        settings.setExtent(box)
        job = QgsMapRendererSequentialJob(settings)
        job.start()
        job.waitForFinished()
        return job.renderedImage()
    return None


# ------------------------------------------------------------------ the build

class _Builder(object):
    """One run of `share`. Kept as an object because the steps share a lot."""

    def __init__(self, project, project_file, home, analysis, options, feedback):
        self.project = project
        self.project_file = project_file
        self.home = home
        self.analysis = analysis
        self.options = options
        self.feedback = feedback
        self.warnings = []
        self.entries = {}               # layer id -> manifest entry
        self.moved = {}                 # normalised original path -> new absolute path
        self.table_of = {}              # (normalised path, table) -> (new path, new table)
        self.commondata = {}            # normalised source file -> new absolute path
        self.counter = 0
        self.staging = ''
        self.root = ''
        self.name = ''
        self.pkg = None
        self.resource_names = {}        # sha256 -> relative name
        self.resource_paths = {}        # original path -> relative name
        self.copied_bytes = 0

    # -- feedback ----------------------------------------------------------

    def step(self, percent, text):
        if self.feedback is not None:
            self.feedback.step(percent, text)
            if self.feedback.isCanceled():
                raise ShareError('Canceled.')

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)
        if self.feedback is not None and hasattr(self.feedback, 'pushWarning'):
            self.feedback.pushWarning(message)

    def check_canceled(self):
        if self.feedback is not None and self.feedback.isCanceled():
            raise ShareError('Canceled.')

    # -- run ---------------------------------------------------------------

    def run(self):
        output = self.options.output
        folder = os.path.dirname(os.path.abspath(output))
        os.makedirs(folder, exist_ok=True)
        self.staging = tempfile.mkdtemp(prefix='kgapp_', dir=folder)
        try:
            self.name = safe_name(self.options.info.name or os.path.splitext(
                os.path.basename(self.project_file))[0])
            self.root = os.path.join(self.staging, 'project')
            os.makedirs(self.root)
            self.step(2, 'Reading the project')
            self.load_project()
            self.step(6, 'Copying data')
            self.place_data()
            self.step(60, 'Preparing the project')
            self.repoint_layers()
            self.collect_resources()
            self.step(72, 'Writing the project')
            project_path = self.write_project()
            self.write_marker()
            self.step(76, 'Checking the package')
            self.check_portability(project_path)
            if self.options.destination:
                return self.finish_folder(project_path)
            return self.finish(project_path)
        finally:
            self.release()
            remove_tree(self.staging)

    def release(self):
        """Let go of the staged files; Windows will not delete an open GeoPackage."""
        if self.pkg is not None:
            with suppress(Exception):
                self.pkg.clear()
        self.pkg = None
        import gc
        gc.collect()

    # -- 1. an independent copy of the project -------------------------------

    def load_project(self):
        pkg = QgsProject()
        if not pkg.read(self.project_file):
            raise ShareError('The project file could not be read: {}'.format(
                self.project_file))
        self.pkg = pkg
        for info in self.analysis.layers:
            self.entries[info.id] = {
                'id': info.id, 'name': info.name, 'provider': info.provider,
                'category': info.category, 'status': pp.STATUS_EXCLUDED,
                'reason': info.reason, 'source': self.describe_source(info),
                'packaged_as': '', 'size': 0,
                'warnings': list(info.warnings), 'notes': list(info.notes)}
            if pkg.mapLayer(info.id) is None and info.include and info.sharable:
                # Scratch layers are not always written to the project file.
                clone = info.layer.clone()
                pkg.addMapLayer(clone)
                self.warn('The layer "{}" was not in the saved project and was '
                          'added from the open one.'.format(info.name))

    @staticmethod
    def describe_source(info):
        if info.path:
            return os.path.basename(info.path) + \
                ('|' + info.table if info.table else '')
        return pa.strip_credentials(info.layer.source())[:160]

    # -- 2. the data -----------------------------------------------------------

    def included(self):
        return [i for i in self.analysis.layers if i.include]

    def place_data(self):
        os.makedirs(os.path.join(self.root, 'Toolbox'), exist_ok=True)
        os.makedirs(os.path.join(self.root, 'Layouts'), exist_ok=True)
        if self.options.mode == pp.MODE_EVERYTHING:
            self.copy_project_folder()
            self.copy_attached_databases()
        else:
            self.copy_selected_folders()
        self.step(30, 'Copying layer data')
        layers = self.included()
        total = max(1, len(layers))
        for index, info in enumerate(layers):
            self.check_canceled()
            self.step(30 + 30.0 * index / total,
                      'Copying {}'.format(info.name))
            try:
                self.place_layer(info)
            except pp.ProjectPackageError:
                raise
            except Exception as error:
                self.fail(info, str(error) or error.__class__.__name__)

    def fail(self, info, why):
        """A layer whose data could not be copied stays in the project, flagged."""
        info.action = pa.ACTION_NONE
        text = 'The data could not be copied ({}). The layer is kept in the ' \
            'project but its data is not shared.'.format(why)
        info.warnings.append(text)
        self.entries[info.id]['warnings'].append(text)
        self.entries[info.id]['status'] = pp.STATUS_NOT_SHARABLE
        self.warn('{}: {}'.format(info.name, text))

    # whole-folder copy (everything mode) ------------------------------------

    def iter_home_files(self):
        for folder, dirs, names in os.walk(self.home):
            dirs[:] = sorted(
                d for d in dirs
                if d not in SKIP_DIRS
                and not os.path.islink(os.path.join(folder, d))
                and not (d.lower() == 'toolbox'
                         and _norm(folder) == _norm(self.home)
                         and not self.options.include_toolbox)
                and not (d.lower() == 'layouts'
                         and _norm(folder) == _norm(self.home)
                         and not self.options.include_layouts_folder))
            for name in sorted(names):
                lowered = name.lower()
                if lowered in SKIP_NAMES or lowered.endswith(SKIP_SUFFIXES):
                    continue
                full = os.path.join(folder, name)
                if os.path.islink(full):
                    continue
                yield full

    def copy_project_folder(self):
        files = list(self.iter_home_files())
        total = sum(self.size_of(f) for f in files) or 1
        done = 0
        for index, source in enumerate(files):
            self.check_canceled()
            relative = os.path.relpath(source, self.home)
            target = os.path.join(self.root, relative)
            try:
                copy_file(source, target)
            except (OSError, sqlite3.Error) as error:
                self.warn('Could not copy {}: {}'.format(relative, error))
                continue
            self.moved[_norm(source)] = target
            done += self.size_of(source)
            self.step(6 + 24.0 * done / total, 'Copying {}'.format(relative))

    def copy_attached_databases(self):
        for path in self.options.attached_databases:
            if not os.path.isfile(path) or pp.is_within(self.home, path):
                continue
            target = unique_path(os.path.join(
                self.root, os.path.basename(path)))
            try:
                copy_file(path, target)
            except (OSError, sqlite3.Error) as error:
                self.warn('Could not copy the database {}: {}'.format(
                    os.path.basename(path), error))
                continue
            self.moved[_norm(path)] = target

    @staticmethod
    def size_of(path):
        with suppress(OSError):
            return os.path.getsize(path)
        return 0

    # selective copy (map mode) -----------------------------------------------

    def copy_selected_folders(self):
        """Toolbox and Layouts, when asked for; the rest comes in with layers."""
        for folder, wanted in (('Toolbox', self.options.include_toolbox),
                               ('Layouts', self.options.include_layouts_folder)):
            source = os.path.join(self.home, folder)
            if not wanted or not os.path.isdir(source):
                continue
            for current, dirs, names in os.walk(source):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in names:
                    lowered = name.lower()
                    if lowered in SKIP_NAMES or lowered.endswith(SKIP_SUFFIXES):
                        continue
                    full = os.path.join(current, name)
                    target = os.path.join(self.root, os.path.relpath(full, self.home))
                    with suppress(OSError):
                        copy_file(full, target)

    # per layer -----------------------------------------------------------------

    def place_layer(self, info):
        entry = self.entries[info.id]
        if not info.sharable:
            entry['status'] = pp.STATUS_NOT_SHARABLE
            self.warn('{}: {}'.format(info.name, info.warnings[0]
                                      if info.warnings else 'not shared'))
            return
        for message in info.warnings:
            self.warn('{}: {}'.format(info.name, message))
        if info.action == pa.ACTION_SERVICE:
            entry['status'] = pp.STATUS_SERVICE
            return
        if info.category == pa.CAT_VIRTUAL:
            entry['status'] = pp.STATUS_PACKAGED
            entry['packaged_as'] = 'virtual layer (query)'
            return

        if info.path and _norm(info.path) in self.moved:
            entry['status'] = pp.STATUS_PACKAGED
            entry['packaged_as'] = _posix(os.path.relpath(
                self.moved[_norm(info.path)], self.root))
            if info.table:
                entry['packaged_as'] += '|' + info.table
            return                      # the folder / database copy covered it
        if info.category in (pa.CAT_PROJECT_DB, pa.CAT_PROJECT_FILE):
            self.place_project_data(info)
        elif info.action == pa.ACTION_COPY_FILE:
            self.place_file(info)
        elif info.action == pa.ACTION_COPY_DB:
            self.place_in_database(info)
        entry['status'] = pp.STATUS_PACKAGED

    def place_project_data(self, info):
        """Map mode: copy just the table / file this layer uses."""
        source = info.path
        relative = os.path.relpath(source, self.home)
        target = os.path.join(self.root, relative)
        if source.lower().endswith('.gpkg') and info.table:
            self.copy_table(info, source, info.table, target)
            return
        for member in pa.sibling_files(source):
            destination = os.path.join(self.root, os.path.relpath(member, self.home))
            if not os.path.exists(destination):
                copy_file(member, destination)
        self.moved[_norm(source)] = target
        self.entries[info.id]['packaged_as'] = _posix(os.path.relpath(target, self.root))

    def copy_table(self, info, source_gpkg, table, target_gpkg):
        key = (_norm(source_gpkg), table)
        if key in self.table_of:
            return self.table_of[key]
        if not os.path.exists(target_gpkg):
            os.makedirs(os.path.dirname(target_gpkg), exist_ok=True)
            ws.create_empty_geopackage(target_gpkg)
        uri = encode_ogr(source_gpkg, table)
        source = dp.Source(info.id, uri, 'ogr', dp.KIND_VECTOR)
        result = self.paste([source], target_gpkg)
        if result.failed:
            raise ShareError(result.failed[0][1])
        if not result.written:
            raise ShareError('nothing was written')
        new_table = result.written[0][1]
        self.table_of[key] = (target_gpkg, new_table)
        self.entries[info.id]['packaged_as'] = '{}|{}'.format(
            _posix(os.path.relpath(target_gpkg, self.root)), new_table)
        return self.table_of[key]

    def paste(self, sources, gpkg):
        sub = dp.Feedback()
        if self.feedback is not None:
            self.feedback.canceled.connect(sub.cancel)
        options = dp.Options(on_clash=dp.CLASH_RENAME, carry_styles=True)
        return dp.paste(sources, dp.Target(dp.TARGET_GPKG, gpkg), options, sub)

    def default_gpkg(self):
        path = os.path.join(self.root, DEFAULT_DB)
        if not os.path.exists(path):
            ws.create_empty_geopackage(path)
        return path

    def place_in_database(self, info):
        """External vector data, scratch and delimited-text layers -> Default.gpkg."""
        gpkg = self.default_gpkg()
        if info.provider == 'ogr' and info.path and info.category == pa.CAT_EXTERNAL_VECTOR:
            key = (_norm(info.path), info.table or '', info.layer.subsetString())
            if key in self.table_of:
                return
            result = self.paste([dp.Source(info.id, info.layer.source(),
                                           'ogr', dp.KIND_VECTOR)], gpkg)
            if result.failed:
                raise ShareError(result.failed[0][1])
            if not result.written:
                raise ShareError('nothing was written')
            table = result.written[0][1]
            self.table_of[key] = (gpkg, table)
            self.table_of[('id', info.id)] = (gpkg, table)
        else:
            base = gc.sanitize_gpkg_name(info.name) or 'layer'
            table = gc.unique_name(base, gpkg_table_names(gpkg))
            write_layer_to_gpkg(info.layer, gpkg, table)
            self.table_of[('id', info.id)] = (gpkg, table)
        self.entries[info.id]['packaged_as'] = '{}|{}'.format(
            DEFAULT_DB, self.table_of[('id', info.id)][1])

    def place_file(self, info):
        """An external raster (or other file) -> commondata/NN_name/."""
        source = _norm(info.path)
        if source in self.commondata:
            return
        self.counter += 1
        folder = os.path.join(self.root, COMMONDATA, '{:02d}_{}'.format(
            self.counter, safe_name(os.path.splitext(
                os.path.basename(info.path))[0], 'data')))
        os.makedirs(folder, exist_ok=True)
        main = os.path.join(folder, os.path.basename(info.path))
        for member in pa.sibling_files(info.path):
            copy_file(member, os.path.join(folder, os.path.basename(member)))
        self.commondata[source] = main
        self.entries[info.id]['packaged_as'] = _posix(os.path.relpath(main, self.root))

    # -- 3. point the layers at the copies -----------------------------------------

    def repoint_layers(self):
        options = QgsDataProvider.ProviderOptions()
        options.transformContext = self.pkg.transformContext()
        for info in self.analysis.layers:
            layer = self.pkg.mapLayer(info.id)
            if layer is None:
                continue
            entry = self.entries[info.id]
            if not info.include:
                self.pkg.removeMapLayer(info.id)
                entry['status'] = pp.STATUS_EXCLUDED
                continue
            if entry['status'] != pp.STATUS_PACKAGED:
                self.strip_service_credentials(layer, info)
                continue
            new_source, provider = self.new_source(info, layer)
            if not new_source:
                continue
            try:
                layer.setDataSource(new_source, layer.name(), provider, options)
            except Exception as error:
                self.fail(info, str(error))
                continue
            if not layer.isValid():
                self.warn('{}: the packaged copy did not load ({}).'.format(
                    info.name, new_source))
                self.entries[info.id]['warnings'].append(
                    'The packaged copy of the data did not load.')

    def strip_service_credentials(self, layer, info):
        if info.action != pa.ACTION_SERVICE:
            return
        cleaned = pa.strip_credentials(layer.source())
        if cleaned != layer.source():
            options = QgsDataProvider.ProviderOptions()
            options.transformContext = self.pkg.transformContext()
            with suppress(Exception):
                layer.setDataSource(cleaned, layer.name(), info.provider, options)
            self.warn('{}: the saved password was removed from the '
                      'connection.'.format(info.name))

    def new_source(self, info, layer):
        """(source string, provider key) of the packaged copy of this layer."""
        provider = info.provider
        if info.category == pa.CAT_VIRTUAL:
            return '', ''
        if ('id', info.id) in self.table_of:
            gpkg, table = self.table_of[('id', info.id)]
            return encode_ogr(gpkg, table), 'ogr'
        if info.path and info.category == pa.CAT_EXTERNAL_VECTOR:
            key = (_norm(info.path), info.table or '', info.layer.subsetString())
            if key in self.table_of:
                gpkg, table = self.table_of[key]
                return encode_ogr(gpkg, table), 'ogr'
        if info.path and info.table and \
                (_norm(info.path), info.table) in self.table_of:
            gpkg, table = self.table_of[(_norm(info.path), info.table)]
            return self.replace_in_source(layer, provider, gpkg, table), provider
        if info.path and _norm(info.path) in self.moved:
            return self.replace_in_source(
                layer, provider, self.moved[_norm(info.path)], info.table), provider
        if info.path and _norm(info.path) in self.commondata:
            return self.replace_in_source(
                layer, provider, self.commondata[_norm(info.path)], ''), provider
        return '', ''

    @staticmethod
    def replace_in_source(layer, provider, new_path, new_table):
        """The layer's own source with the file (and table) swapped, rest kept."""
        registry = QgsProviderRegistry.instance()
        parts = registry.decodeUri(provider, layer.source())
        parts['path'] = new_path
        if new_table and 'layerName' in parts:
            parts['layerName'] = new_table
        return registry.encodeUri(provider, parts)

    # -- 4. symbol and layout resources -----------------------------------------------

    def add_resource(self, source_path):
        """Copy one symbol / picture file into `resources/`; return its name."""
        if source_path in self.resource_paths:
            return self.resource_paths[source_path]
        from .packaging import _resolve_resource
        resolved = _resolve_resource(source_path)
        if resolved is None:
            self.resource_paths[source_path] = None
            return None
        with suppress(OSError):
            digest = pp.sha256_of(resolved)
            if digest in self.resource_names:
                self.resource_paths[source_path] = self.resource_names[digest]
                return self.resource_names[digest]
            folder = 'svg' if resolved.lower().endswith('.svg') else 'img'
            name = '{}/{}/{}'.format(RESOURCES, folder, os.path.basename(resolved))
            target = unique_path(os.path.join(self.root, name))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(resolved, target)
            name = _posix(os.path.relpath(target, self.root))
            self.resource_names[digest] = name
            self.resource_paths[source_path] = name
            return name
        self.resource_paths[source_path] = None
        return None

    def warn_resource(self, message):
        self.warn(message)

    def collect_resources(self):
        from .packaging import collect_resources, rewrite_resources
        collector = _ResourceCollector(self)
        for layer in self.pkg.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            with suppress(Exception):
                mapping = collect_resources(layer, collector)
                rewrite_resources(layer, mapping, self.root)
        self.collect_layout_pictures()

    def collect_layout_pictures(self):
        with suppress(Exception):
            from qgis.core import QgsLayoutItemPicture
            for layout in self.pkg.layoutManager().layouts():
                for item in layout.items():
                    if not isinstance(item, QgsLayoutItemPicture):
                        continue
                    path = item.picturePath()
                    if not path or path.lower().startswith(('http', 'base64:')):
                        continue
                    name = self.add_resource(path)
                    if name:
                        item.setPicturePath(os.path.join(self.root, name))
                    else:
                        self.warn('The picture "{}" in the layout "{}" was '
                                  'not found and could not be packaged.'.format(
                                      os.path.basename(path), layout.name()))

    # -- 5. the project file ---------------------------------------------------------------

    def write_project(self):
        pkg = self.pkg
        pkg.setFilePathStorage(Qgis.FilePathType.Relative)
        with suppress(Exception):
            pkg.setPresetHomePath('')
        if not pkg.title():
            pkg.setTitle(self.options.info.name or self.name)
        self.scrub_machine_paths()
        path = os.path.join(self.root, self.name + '.qgz')
        if not pkg.write(path):
            raise ShareError('The packaged project could not be written.')
        self.relativize_saved_sources(path)
        return path

    def scrub_machine_paths(self):
        """Forget the per-computer paths QGIS keeps beside the repointed layers.

        The last layout export folder is a plain project entry; a layout map's locked
        layer list and the GPS destination layer still carry the layers' old sources
        until they are set again from the repointed layers.
        """
        pkg = self.pkg
        with suppress(Exception):
            pkg.removeEntry('Layouts', '/lastLayoutExportDir')
        with suppress(Exception):
            from qgis.core import QgsLayoutItemMap
            for layout in pkg.layoutManager().layouts():
                for item in layout.items():
                    if isinstance(item, QgsLayoutItemMap) and item.keepLayerSet():
                        item.setLayers(item.layers())
        with suppress(Exception):
            gps = pkg.gpsSettings()
            gps.setDestinationLayer(gps.destinationLayer())
            pkg.setGpsSettings(gps)

    def relativize_saved_sources(self, path):
        """Rewrite the staging folder inside the written project as ``.``.

        QGIS writes the old source of a layer into a layout map's locked layer list
        and the GPS destination layer as an absolute path that ``setDataSource`` and
        relative storage both leave alone. It is only a lookup hint, so pointing it
        at the project folder keeps it valid wherever the package is opened.
        """
        root = os.path.abspath(self.root)
        forms = {root, root.replace(os.sep, '/')}
        pattern = re.compile('|'.join(re.escape(f) for f in sorted(forms, reverse=True)),
                             re.IGNORECASE)
        temporary = path + '.part'
        with suppress(Exception):
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(
                    temporary, 'w', zipfile.ZIP_DEFLATED) as target:
                for item in source.infolist():
                    data = source.read(item.filename)
                    if item.filename.lower().endswith('.qgs'):
                        data = pattern.sub('.', data.decode('utf-8')).encode('utf-8')
                    target.writestr(item, data)
            os.replace(temporary, path)
        with suppress(OSError):
            os.remove(temporary)

    def write_marker(self):
        workspace = ws.Workspace(self.root, self.name)
        if self.options.destination:
            workspace.scaffold()            # a real workspace needs Default.gpkg too
        workspace.save()

    def default_relative(self, path, base):
        if not path:
            return ''
        resolved = self.moved.get(_norm(path))
        if resolved is None and pp.is_within(base, path):
            resolved = os.path.join(self.root, os.path.relpath(path, base))
        if resolved is None or not os.path.exists(resolved):
            return ''
        return _posix(os.path.relpath(resolved, self.root))

    # -- 6. portability check ---------------------------------------------------------------------

    def check_portability(self, project_path):
        text = pa.read_project_text(project_path).replace('\\', '/').lower()
        wanted = {_posix(os.path.abspath(self.home)).lower(),
                  _posix(os.path.expanduser('~')).lower()}
        for info in self.analysis.layers:
            if info.path and info.include and info.sharable \
                    and info.action in (pa.ACTION_COPY_DB, pa.ACTION_COPY_FILE):
                wanted.add(_posix(os.path.dirname(info.path)).lower())
        allowed = [_posix(i.path).lower() for i in self.analysis.layers if i.path
                   and (not i.sharable or not i.include)]
        leftovers = []
        for path in sorted(p for p in wanted if len(p) > 3):
            index = text.find(path)
            while index != -1:
                line_end = text.find('<', index)
                snippet = text[index:line_end if line_end != -1 else index + 200]
                if not any(snippet.startswith(a) or a in snippet for a in allowed):
                    leftovers.append(path)
                    break
                index = text.find(path, index + len(path))
        for path in leftovers[:5]:
            self.warn('The project still mentions a path on this computer ({}). '
                      'Check settings such as plugin paths or print-layout '
                      'files.'.format(path))

    # -- 7. zip + manifest ----------------------------------------

    def finish(self, project_path):
        records = {}
        output = self.options.output
        part = output + '.part'
        manifest_layers = []
        for info in self.analysis.layers:
            entry = self.entries[info.id]
            if not info.include:
                entry['status'] = pp.STATUS_EXCLUDED
            elif not info.sharable:
                entry['status'] = pp.STATUS_NOT_SHARABLE
            manifest_layers.append(entry)

        thumbnail = self.save_thumbnail()
        try:
            with zipfile.ZipFile(part, 'w', allowZip64=True) as archive:
                files = []
                for folder, dirs, names in os.walk(self.root):
                    dirs.sort()
                    for name in sorted(names):
                        files.append(os.path.join(folder, name))
                total = sum(self.size_of(f) for f in files) or 1
                done = 0
                for full in files:
                    self.check_canceled()
                    arcname = pp.ROOT + _posix(os.path.relpath(full, self.root))
                    records[arcname] = self.add_member(archive, full, arcname)
                    done += records[arcname]['size']
                    self.step(76 + 22.0 * done / total,
                              'Compressing {}'.format(os.path.basename(full)))
                if thumbnail:
                    archive.write(thumbnail, pp.THUMBNAIL)
                manifest = self.build_manifest(project_path, manifest_layers,
                                               records)
                archive.writestr(pp.MANIFEST, json.dumps(
                    manifest, indent=2, ensure_ascii=False))
            os.replace(part, output)
        except BaseException:
            with suppress(OSError):
                os.remove(part)
            raise
        result = ShareResult(output)
        result.size = os.path.getsize(output)
        result.manifest = manifest
        result.warnings = list(self.warnings)
        result.layers = manifest_layers
        return result

    def finish_folder(self, project_path):
        """Conversion: the staged project folder *is* the result; move it into place."""
        destination = self.options.destination
        layers = []
        for info in self.analysis.layers:
            entry = self.entries[info.id]
            if not info.include:
                entry['status'] = pp.STATUS_EXCLUDED
            elif not info.sharable:
                entry['status'] = pp.STATUS_NOT_SHARABLE
            layers.append(entry)
        manifest = self.build_manifest(project_path, layers, {})
        # Windows will not move a folder whose GeoPackage a project still has open.
        self.release()
        self.step(96, 'Moving the workspace into place')
        if os.path.isdir(destination) and not os.listdir(destination):
            os.rmdir(destination)
        if os.path.exists(destination):
            raise ShareError('"{}" already exists.'.format(destination))
        os.replace(self.root, destination)
        result = ShareResult(destination)
        result.size = sum(self.size_of(os.path.join(folder, name))
                          for folder, _dirs, names in os.walk(destination)
                          for name in names)
        result.manifest = manifest
        result.warnings = list(self.warnings)
        result.layers = layers
        return result

    @staticmethod
    def add_member(archive, full, arcname):
        compression = zipfile.ZIP_STORED if full.lower().endswith(STORED_SUFFIXES) \
            else zipfile.ZIP_DEFLATED
        info = zipfile.ZipInfo.from_file(full, arcname)
        info.compress_type = compression
        digest = hashlib.sha256()
        size = 0
        with open(full, 'rb') as source, archive.open(info, 'w',
                                                      force_zip64=True) as out:
            for block in iter(lambda: source.read(CHUNK), b''):
                out.write(block)
                digest.update(block)
                size += len(block)
        return {'size': size, 'sha256': digest.hexdigest()}

    def save_thumbnail(self):
        image = self.options.thumbnail
        if image is None:
            return ''
        path = os.path.join(self.staging, pp.THUMBNAIL)
        return path if image.save(path, 'PNG') else ''

    def build_manifest(self, project_path, layers, records):
        info = self.options.info
        pkg = self.pkg
        extent = None
        with suppress(Exception):
            box = pkg.viewSettings().fullExtent() \
                if hasattr(pkg, 'viewSettings') else None
            if box is not None and not box.isEmpty():
                extent = [box.xMinimum(), box.yMinimum(),
                          box.xMaximum(), box.yMaximum()]
        crs = pkg.crs().authid() if pkg.crs().isValid() else ''
        defaults = {
            'database': self.default_relative(self.options.default_database,
                                              self.home),
            'folder': self.default_relative(self.options.default_folder,
                                            self.home)}
        version = plugin_version()
        return {
            'format': pp.PACKAGE_FORMAT,
            'kind': pp.KIND,
            'generator': 'KGA Toolbox {}'.format(version).strip(),
            'created': datetime.datetime.now().isoformat(timespec='seconds'),
            'qgis': Qgis.QGIS_VERSION.split('-')[0],
            'mode': self.options.mode,
            'info': dict(info.to_dict(), name=info.name or self.name),
            'project': {
                'name': self.name,
                'file': pp.ROOT + os.path.basename(project_path),
                'crs': crs, 'extent': extent,
                'layer_count': len(pkg.mapLayers()),
                'layouts': [layout.name()
                            for layout in pkg.layoutManager().layouts()],
            },
            'defaults': {k: v for k, v in defaults.items() if v},
            'layers': layers,
            'files': records,
            'warnings': list(self.warnings),
            'stats': {'bytes': sum(r['size'] for r in records.values()),
                      'files': len(records)},
        }


class _ResourceCollector(object):
    """What `packaging.collect_resources` needs from a writer, minus the zip."""

    def __init__(self, builder):
        self.builder = builder

    def add_resource(self, source_path):
        return self.builder.add_resource(source_path)

    def warn(self, message):
        self.builder.warn(message)


# ------------------------------------------------------------------- public API

def convert_problem(project_file, location, name):
    """Why a plain project cannot be converted to `location/name`, or ''."""
    if not project_file or not os.path.isfile(project_file):
        return 'Save the project first (Project > Save), then convert it.'
    if ws.open_workspace(project_file) is not None:
        return 'This project is already in a KGA workspace.'
    return pp.target_problem(location, name)


def convert(project, project_file, analysis, location, name, copy_remote=False,
            feedback=None):
    """Turn a traditional `.qgz` into a KGA workspace in `location/name`.

    The same engine as sharing, ending in a folder instead of a zip: data the
    project uses is copied in (what is already beside the project keeps its
    relative place, everything else is consolidated into `Default.gpkg` and
    `commondata/`), the project is rewritten with relative paths, and the folder
    becomes a workspace. The original project and its data are left alone.
    """
    why = convert_problem(project_file, location, name)
    if why:
        raise ShareError(why)
    destination = os.path.normpath(os.path.join(location.strip(), name.strip()))
    options = ShareOptions(
        destination, mode=pp.MODE_MAP, info=pp.PackageInfo(name=name.strip()),
        include_toolbox=False, include_layouts_folder=False,
        copy_remote=copy_remote, destination=destination)
    home = os.path.dirname(os.path.abspath(project_file))
    return _Builder(project, project_file, home, analysis, options, feedback).run()


def problem(project, project_file, options):
    """Why sharing cannot start, or '' when it can."""
    if not project_file or not os.path.isfile(project_file):
        return ('Save the project first (Project > Set Workspace), then '
                'share it.')
    if not options.output:
        return 'Choose where to save the package.'
    if not options.output.lower().endswith(pp.EXTENSION):
        return 'The package file name must end in {}.'.format(pp.EXTENSION)
    if ws.name_problem(options.info.name or 'x'):
        return ws.name_problem(options.info.name)
    return ''


def share(project, project_file, home, analysis, options, feedback=None):
    """Write the package and return a `ShareResult`.

    `analysis` is the (possibly user-edited) result of `project_analysis.analyze`;
    its `include` flags decide which layers go in. `home` is the project folder.
    """
    why = problem(project, project_file, options)
    if why:
        raise ShareError(why)
    return _Builder(project, project_file, home, analysis, options, feedback).run()


# ------------------------------------------------------------------------ report

def report_html(result, info=None):
    """A small stand-alone HTML report of what was shared, for the user to keep."""
    from html import escape
    manifest = result.manifest
    info = info or pp.PackageInfo(**(manifest.get('info') or {}))
    rows = []
    for layer in result.layers:
        css = {'packaged': 'ok', 'service': 'svc', 'not_sharable': 'bad',
               'excluded': 'off'}.get(layer['status'], '')
        rows.append(
            '<tr class="{}"><td>{}</td><td>{}</td><td>{}</td><td>{}</td>'
            '<td>{}</td></tr>'.format(
                css, escape(layer['name']), escape(layer['status'].replace('_', ' ')),
                escape(layer.get('reason', '')), escape(layer.get('packaged_as', '')),
                escape(' '.join(layer.get('warnings', [])))))
    warnings = ''.join('<li>{}</li>'.format(escape(w)) for w in result.warnings)
    return (
        '<!doctype html><meta charset="utf-8"><title>{title}</title>'
        '<style>body{{font:14px sans-serif;margin:24px}}table{{border-collapse:'
        'collapse}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left}}'
        '.bad{{background:#fde8e8}}.svc{{background:#eef4fb}}.off{{color:#888}}'
        '</style><h1>{title}</h1><p>{summary}</p>'
        '<p>Mode: <b>{mode}</b> &middot; Size: <b>{size:.1f} MB</b> &middot; '
        'Created: {created} &middot; QGIS {qgis}</p>'
        '<h2>Layers</h2><table><tr><th>Layer</th><th>Result</th><th>Why</th>'
        '<th>Packaged as</th><th>Warnings</th></tr>{rows}</table>'
        '<h2>Warnings</h2><ul>{warnings}</ul>'.format(
            title=escape(info.name or 'Project package'), summary=escape(info.summary),
            mode=escape(manifest.get('mode', '')), size=result.size / 1048576.0,
            created=escape(manifest.get('created', '')),
            qgis=escape(manifest.get('qgis', '')), rows=''.join(rows),
            warnings=warnings or '<li>None</li>'))

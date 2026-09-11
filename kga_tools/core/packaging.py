# -*- coding: utf-8 -*-
"""The `.kgalp` layer package: read, write, and collect what a style needs.

A QLR references data rather than containing it, so handing a styled layer to a
client means a folder of files and an explanation. There is no QGIS equivalent
of `.lpkx`, and this is it.

The package is a zip rather than a bare GeoPackage. A GPKG can hold styles in
its `layer_styles` table, but SVG symbols, raster marker images and the layer
tree have nowhere to live in one, and those are exactly the parts that break
when a styled layer changes machines.

    manifest.json      format version, created, creator, layer list, tree
    data.gpkg          all vector data, one table per layer
    rasters/           raster layers, copied as their original files
    styles/<name>.qml  one per layer
    resources/svg/     SVG symbols collected from symbol layers
    resources/img/     raster fill and marker images
    thumbnail.png      optional preview

**Resource collection is the hard part.** Symbol layers nest — a symbol layer
can carry a subsymbol — so the walk has to recurse, and the properties holding
a path differ per symbol layer type. `collect_resources` handles that, and
`rewrite_resources` puts the extracted paths back on the way in.

Field domains survive because the vector data is copied with
`QgsVectorFileWriter`, which was verified on GDAL 3.13 to carry
`gpkg_data_column_constraints` across. Font markers cannot be packaged at all
and are reported as a warning rather than silently rendering wrong.
"""

import hashlib
import json
import os
import shutil
import zipfile

from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

LOG_TAG = 'KGA Toolbox'

EXTENSION = '.kgalp'
MANIFEST = 'manifest.json'
DATA_GPKG = 'data.gpkg'
STYLE_DIR = 'styles'
RASTER_DIR = 'rasters'
RESOURCE_DIR = 'resources'
SVG_DIR = RESOURCE_DIR + '/svg'
IMG_DIR = RESOURCE_DIR + '/img'
THUMBNAIL = 'thumbnail.png'

#: Written into every package. A reader refuses anything newer than it knows.
PACKAGE_FORMAT = 1

#: Symbol layer properties that hold a file path, by the key they use.
SVG_KEYS = ('name', 'svgFile')
IMAGE_KEYS = ('imageFile', 'image_file', 'raster_file')


class PackageError(Exception):
    """Anything that stops a package being written or read, phrased for a user."""


# ------------------------------------------------------------------- writing

class PackageWriter(object):
    """Builds one `.kgalp`.

    The manifest is written last, after everything else has succeeded, so a
    truncated package is detectable by its absence rather than by loading
    half a project and wondering why.
    """

    def __init__(self, path, feedback=None):
        self.path = path
        self.feedback = feedback
        self.layers = []                # manifest entries
        self.groups = []                # layer tree
        self.warnings = []
        self._resources = {}            # original path -> archive name
        self._hashes = {}               # content hash -> archive name
        self._zip = None
        self._staging = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self):
        import tempfile
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        self._staging = tempfile.mkdtemp(prefix='kga_pkg_')
        self._zip = zipfile.ZipFile(self.path, 'w', zipfile.ZIP_DEFLATED)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._zip is not None:
                self._zip.close()
        finally:
            self._zip = None
            if self._staging:
                shutil.rmtree(self._staging, ignore_errors=True)
                self._staging = None
        if exc_type is not None and os.path.exists(self.path):
            # A half-written archive is worse than none: it would open and look
            # plausible right up to the missing manifest.
            try:
                os.remove(self.path)
            except OSError:                 # pragma: no cover
                pass
        return False

    # -- content ------------------------------------------------------------

    def staging_path(self, name):
        return os.path.join(self._staging, name)

    def add_vector(self, layer, selected_only=False):
        """Copy one vector layer into `data.gpkg`, returning its table name.

        `QgsVectorFileWriter` is used rather than a hand-rolled copy because it
        carries field domains across — checked against GDAL 3.13 — which is the
        whole reason the Domain Manager work has to be finished before this.
        """
        table = self._unique_table_name(layer.name())
        gpkg = self.staging_path(DATA_GPKG)

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = 'GPKG'
        options.layerName = table
        options.fileEncoding = 'UTF-8'
        if os.path.exists(gpkg):
            options.actionOnExistingFile = \
                QgsVectorFileWriter.CreateOrOverwriteLayer
        if selected_only and layer.selectedFeatureCount():
            options.onlySelectedFeatures = True

        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, gpkg, QgsProject.instance().transformContext(), options)
        code = result[0] if isinstance(result, (tuple, list)) else result
        if code != QgsVectorFileWriter.NoError:
            message = result[1] if isinstance(result, (tuple, list)) \
                and len(result) > 1 else ''
            raise PackageError('Could not package the layer "{}": {}'.format(
                layer.name(), message or 'writer error {}'.format(code)))
        return table

    def add_raster(self, layer):
        """Copy a raster's own file into `rasters/`, returning the archive name.

        Kept as the original file rather than converted: a COG stays a COG, and
        converting somebody's data behind their back is not this tool's job.
        """
        source = layer.source().split('|')[0]
        if not source or not os.path.exists(source):
            self.warn('The raster "{}" has no file on disk and was not '
                      'packaged.'.format(layer.name()))
            return None
        name = '{}/{}'.format(RASTER_DIR, os.path.basename(source))
        name = self._unique_archive_name(name)
        self._zip.write(source, name)

        # A raster is rarely one file. Sidecars carry the world file, the
        # pyramids and the projection, and a package without them renders
        # wrong or not at all.
        stem = os.path.splitext(source)[0]
        for suffix in ('.aux.xml', '.ovr', '.prj', '.tfw', '.wld', '.jgw',
                       '.pgw', '.vrt'):
            for candidate in (source + suffix, stem + suffix):
                if os.path.exists(candidate):
                    self._zip.write(
                        candidate,
                        '{}/{}'.format(RASTER_DIR,
                                       os.path.basename(candidate)))
        return name

    def add_style(self, layer, key):
        """Save the layer's style as a QML inside `styles/`."""
        name = '{}/{}.qml'.format(STYLE_DIR, _safe_name(key))
        temp = self.staging_path(_safe_name(key) + '.qml')
        message, ok = layer.saveNamedStyle(temp)
        if not ok:
            self.warn('Could not save the style for "{}": {}'.format(
                layer.name(), message))
            return None
        self._zip.write(temp, name)
        return name

    def add_resource(self, source_path):
        """Copy one symbol resource in, deduplicated by content hash.

        Returns the archive name, or None when the file cannot be found — a
        broken SVG path in the source project is worth reporting, not worth
        failing the whole package for.
        """
        if source_path in self._resources:
            return self._resources[source_path]

        resolved = _resolve_resource(source_path)
        if resolved is None:
            self.warn('Could not find the symbol resource "{}"; the package '
                      'will fall back to a default symbol for it.'.format(
                          source_path))
            self._resources[source_path] = None
            return None

        try:
            with open(resolved, 'rb') as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError as exc:
            self.warn('Could not read "{}": {}'.format(resolved, exc))
            self._resources[source_path] = None
            return None

        if digest in self._hashes:
            # The same SVG referenced from five layers is stored once.
            self._resources[source_path] = self._hashes[digest]
            return self._hashes[digest]

        folder = SVG_DIR if resolved.lower().endswith('.svg') else IMG_DIR
        name = self._unique_archive_name('{}/{}'.format(
            folder, os.path.basename(resolved)))
        self._zip.write(resolved, name)
        self._hashes[digest] = name
        self._resources[source_path] = name
        return name

    def add_thumbnail(self, image_path):
        if image_path and os.path.exists(image_path):
            self._zip.write(image_path, THUMBNAIL)
            return True
        return False

    def write_manifest(self, extra=None):
        """Written last, so an incomplete package has no manifest at all."""
        import datetime
        manifest = {
            'format': PACKAGE_FORMAT,
            'generator': 'KGA Toolbox',
            'created': datetime.datetime.now().isoformat(timespec='seconds'),
            'project': QgsProject.instance().fileName(),
            'layers': self.layers,
            'tree': self.groups,
            'resources': {k: v for k, v in self._resources.items() if v},
            'warnings': self.warnings,
        }
        if extra:
            manifest.update(extra)
        self._zip.writestr(
            MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
        return manifest

    # -- helpers ------------------------------------------------------------

    def flush_data(self):
        """Put the staged GeoPackage into the archive.

        Written in one go at the end rather than per layer, because a GPKG has
        to be closed before it can be copied and reopening it per layer would
        multiply the work by the layer count.
        """
        gpkg = self.staging_path(DATA_GPKG)
        if os.path.exists(gpkg):
            self._zip.write(gpkg, DATA_GPKG)
            return True
        return False

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)
        if self.feedback is not None:
            self.feedback.pushWarning(message) \
                if hasattr(self.feedback, 'pushWarning') \
                else self.feedback.pushInfo(message)

    def _unique_table_name(self, name):
        base = _safe_name(name) or 'layer'
        taken = {entry.get('table') for entry in self.layers}
        candidate = base
        counter = 2
        while candidate in taken:
            candidate = '{}_{}'.format(base, counter)
            counter += 1
        return candidate

    def _unique_archive_name(self, name):
        existing = set(self._zip.namelist())
        if name not in existing:
            return name
        stem, extension = os.path.splitext(name)
        counter = 2
        while '{}_{}{}'.format(stem, counter, extension) in existing:
            counter += 1
        return '{}_{}{}'.format(stem, counter, extension)


# ------------------------------------------------------------------- reading

def read_manifest(path):
    """The manifest of a package, with its version checked."""
    if not os.path.exists(path):
        raise PackageError('{} does not exist.'.format(path))
    if not zipfile.is_zipfile(path):
        raise PackageError('{} is not a KGA layer package.'.format(path))

    with zipfile.ZipFile(path) as archive:
        if MANIFEST not in archive.namelist():
            raise PackageError(
                '{} has no manifest, which means it was not finished being '
                'written. It cannot be opened.'.format(path))
        try:
            manifest = json.loads(archive.read(MANIFEST).decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PackageError('The manifest in {} is unreadable: {}'.format(
                path, exc))

    version = manifest.get('format', 1)
    if version > PACKAGE_FORMAT:
        raise PackageError(
            'That package was written by a newer KGA Toolbox (format {}, this '
            'build reads {}). Update the plugin to open it.'.format(
                version, PACKAGE_FORMAT))
    return manifest


def extract(path, destination, feedback=None):
    """Unpack safely, refusing any member that escapes `destination`.

    Zip path traversal is a real vulnerability the moment packages are shared
    between organisations, and it costs five lines to close.
    """
    root = os.path.abspath(destination)
    if not os.path.isdir(root):
        os.makedirs(root)

    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.endswith('/'):
                continue
            target = os.path.abspath(os.path.join(root, name))
            if not _is_within(root, target):
                raise PackageError(
                    'The package contains an entry that would be written '
                    'outside the extraction folder ("{}"). Refusing to open '
                    'it.'.format(name))
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with archive.open(member) as source, open(target, 'wb') as sink:
                shutil.copyfileobj(source, sink)
            if feedback is not None and feedback.isCanceled():
                raise PackageError('Cancelled while extracting.')
    return root


def _is_within(root, target):
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:                      # different drives on Windows
        return False


# ------------------------------------------------------- symbol resource walk

def iter_symbol_layers(symbol):
    """Every symbol layer in `symbol`, including those inside subsymbols.

    Symbol layers nest, so a flat pass over `symbolLayers()` misses the SVG
    inside a marker line's marker — which is exactly the case that renders
    wrong on the receiving machine.
    """
    if symbol is None:
        return
    try:
        layers = symbol.symbolLayers()
    except Exception:                       # pragma: no cover - defensive
        return
    for symbol_layer in layers:
        yield symbol_layer
        try:
            child = symbol_layer.subSymbol()
        except Exception:                   # pragma: no cover
            child = None
        if child is not None:
            for nested in iter_symbol_layers(child):
                yield nested


def iter_symbols(layer):
    """Every symbol a vector layer's renderer uses."""
    from qgis.core import QgsRenderContext

    renderer = layer.renderer() if hasattr(layer, 'renderer') else None
    if renderer is None:
        return []
    try:
        return list(renderer.symbols(QgsRenderContext())) or []
    except Exception:                       # pragma: no cover - defensive
        return []


def collect_resources(layer, writer):
    """Copy every file a layer's symbology references into the package.

    Returns `{original path: archive name}` for the ones that were found. Font
    markers are reported: a font cannot be packaged, and a receiving machine
    without it renders the wrong glyph rather than failing visibly.
    """
    mapping = {}
    fonts = set()

    for symbol in iter_symbols(layer):
        for symbol_layer in iter_symbol_layers(symbol):
            class_name = type(symbol_layer).__name__
            if 'FontMarker' in class_name:
                try:
                    fonts.add(symbol_layer.fontFamily())
                except Exception:           # pragma: no cover
                    fonts.add('?')
                continue
            try:
                properties = symbol_layer.properties()
            except Exception:               # pragma: no cover
                continue
            for key in SVG_KEYS + IMAGE_KEYS:
                value = properties.get(key)
                if not value or not _looks_like_a_path(value, key):
                    continue
                name = writer.add_resource(value)
                if name:
                    mapping[value] = name

    for font in sorted(fonts):
        writer.warn(
            'The layer "{}" uses the font marker "{}". Fonts cannot be '
            'packaged; install it on the receiving machine or the symbol will '
            'render with the wrong glyph.'.format(layer.name(), font))
    return mapping


def _looks_like_a_path(value, key):
    """Distinguish an SVG path from a plain symbol name.

    `name` is the SVG path on an SvgMarker but the marker shape ("circle") on a
    SimpleMarker, so the key alone is not enough to tell.
    """
    if key in IMAGE_KEYS:
        return True
    text = str(value)
    lowered = text.lower()
    return (lowered.endswith('.svg')
            or '/' in text or '\\' in text
            or lowered.startswith('base64:'))


def rewrite_resources(layer, mapping, root):
    """Point a loaded layer's symbols at the extracted resource files.

    Called after the style loads, walking the symbol layers the same way the
    writer did. Without this every packaged SVG resolves to the path it had on
    the machine that built the package.
    """
    if not mapping:
        return 0

    lookup = {}
    for original, archive_name in mapping.items():
        extracted = os.path.join(root, archive_name.replace('/', os.sep))
        if not os.path.exists(extracted):
            continue
        lookup[original] = extracted
        # QGIS normalises separators when it stores a path, so the style may
        # spell the same file differently than the manifest recorded it.
        lookup.setdefault(str(original).replace('\\', '/'), extracted)
        lookup.setdefault(os.path.basename(str(original)), extracted)

    changed = 0
    for symbol in iter_symbols(layer):
        for symbol_layer in iter_symbol_layers(symbol):
            if _repoint(symbol_layer, lookup):
                changed += 1
    if changed:
        layer.triggerRepaint()
    return changed


#: How to read and write the file path on each kind of symbol layer.
#: There is no generic `setProperties` on `QgsSymbolLayer` — `properties()` is
#: read-only and the setter differs per class — so writing a path back has to
#: go through the right pair. Verified against QGIS 3.44:
#: SvgMarker/RasterMarker/RasterLine use path/setPath, SVGFill uses
#: svgFilePath/setSvgFilePath, RasterFill uses imageFilePath/setImageFilePath.
PATH_ACCESSORS = (
    ('path', 'setPath'),
    ('svgFilePath', 'setSvgFilePath'),
    ('imageFilePath', 'setImageFilePath'),
)


def _repoint(symbol_layer, lookup):
    """Point one symbol layer at its packaged resource. True when it moved."""
    updated = False
    for getter_name, setter_name in PATH_ACCESSORS:
        getter = getattr(symbol_layer, getter_name, None)
        setter = getattr(symbol_layer, setter_name, None)
        if getter is None or setter is None:
            continue
        try:
            current = getter()
        except Exception:                   # pragma: no cover - defensive
            continue
        if not current:
            continue
        replacement = (lookup.get(current)
                       or lookup.get(str(current).replace('\\', '/'))
                       or lookup.get(os.path.basename(str(current))))
        if not replacement or replacement == current:
            continue
        try:
            setter(replacement)
            updated = True
        except Exception as exc:            # pragma: no cover - defensive
            QgsMessageLog.logMessage(
                'Could not repoint a symbol at {}: {}'.format(replacement, exc),
                LOG_TAG, Qgis.MessageLevel.Warning)
    return updated


def _resolve_resource(path):
    """An absolute path for a symbol resource, or None.

    A style may store an SVG as a bare name resolved against the QGIS SVG
    search paths, so those are tried before giving up.
    """
    text = str(path)
    if text.lower().startswith('base64:'):
        return None                         # already embedded in the style
    if os.path.isabs(text) and os.path.exists(text):
        return text

    from qgis.core import QgsApplication
    candidates = [text]
    project_home = QgsProject.instance().homePath()
    if project_home:
        candidates.append(os.path.join(project_home, text))
    try:
        for directory in QgsApplication.svgPaths():
            candidates.append(os.path.join(directory, text))
    except Exception:                       # pragma: no cover
        pass

    for candidate in candidates:
        if candidate and os.path.exists(candidate) and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


# --------------------------------------------------------------- layer tree

def describe_tree(layers):
    """The group structure of the chosen layers, as nested manifest entries.

    Only the groups that actually contain a chosen layer are recorded, so
    packaging two layers out of a forty-group project does not rebuild the
    whole tree on the far side.
    """
    root = QgsProject.instance().layerTreeRoot()
    wanted = {layer.id() for layer in layers}
    return _describe_group(root, wanted) or []


def _describe_group(group, wanted):
    entries = []
    for child in group.children():
        if hasattr(child, 'layerId'):
            if child.layerId() in wanted:
                entries.append({'type': 'layer', 'id': child.layerId()})
        else:
            nested = _describe_group(child, wanted)
            if nested:
                entries.append({'type': 'group', 'name': child.name(),
                                'children': nested})
    return entries


def _safe_name(name):
    """A file/table name safe on every platform and legal as a GPKG table."""
    cleaned = ''.join(
        character if character.isalnum() or character in '-_' else '_'
        for character in (name or ''))
    cleaned = cleaned.strip('_')
    if cleaned and cleaned[0].isdigit():
        cleaned = 'l_' + cleaned
    return cleaned[:60] or 'layer'

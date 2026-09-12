# -*- coding: utf-8 -*-

import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import LOG_TAG, docs_url
from ..core import packaging as P
from ..core.changelog import ChangeReport
from ..core.compat import no_threading, source_type


class CreateLayerPackageAlgorithm(QgsProcessingAlgorithm):
    """Write a self-contained `.kgalp`."""

    LAYERS = 'LAYERS'
    INCLUDE_SELECTED_ONLY = 'INCLUDE_SELECTED_ONLY'
    INCLUDE_RESOURCES = 'INCLUDE_RESOURCES'
    INCLUDE_METADATA = 'INCLUDE_METADATA'
    OUTPUT = 'OUTPUT'
    OUTPUT_HTML = 'OUTPUT_HTML'
    LAYER_COUNT = 'LAYER_COUNT'

    def tr(self, string):
        return QCoreApplication.translate('KgaLayerPackage', string)

    def createInstance(self):
        return CreateLayerPackageAlgorithm()

    def name(self):
        return 'create_layer_package'

    def displayName(self):
        return self.tr('Create Layer Package')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('create_layer_package')

    def shortHelpString(self):
        return self.tr(
            'Package layers, their styles and everything those styles need '
            'into one <b>.kgalp</b> file that opens anywhere.\n\n'
            'A QLR points at data that has to travel beside it; this contains '
            'the data. Vector layers go into one GeoPackage — <b>field domains '
            'included</b> — rasters are copied as their own files with their '
            'sidecars, each style is saved as a QML, and every SVG symbol and '
            'raster marker image the symbology references is collected and '
            'de-duplicated. The group structure is recorded so the far side '
            'rebuilds it.\n\n'
            'Fonts are the one thing that cannot be packaged. If a layer uses a '
            'font marker you get a warning naming the font, so you can tell the '
            'recipient what to install.\n\n'
            'The manifest is written last, so a package interrupted halfway '
            'through has no manifest and is refused rather than opening '
            'half-loaded.'
        )

    def flags(self):
        # Reads the project's layer tree and renderers.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LAYERS, self.tr('Layers to package'),
            source_type('MapLayer')))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_SELECTED_ONLY,
            self.tr('Package only the selected features'), defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_RESOURCES,
            self.tr('Collect SVG symbols and marker images'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_METADATA, self.tr('Include layer metadata'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr('Layer package'),
            self.tr('KGA layer package (*.kgalp)')))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_HTML, self.tr('Package report'),
            self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False))

        self.addOutput(QgsProcessingOutputNumber(
            self.LAYER_COUNT, self.tr('Layers packaged')))

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.LAYERS, context)
        if not layers:
            raise QgsProcessingException(self.tr('No layers were chosen.'))

        selected_only = self.parameterAsBool(
            parameters, self.INCLUDE_SELECTED_ONLY, context)
        include_resources = self.parameterAsBool(
            parameters, self.INCLUDE_RESOURCES, context)
        include_metadata = self.parameterAsBool(
            parameters, self.INCLUDE_METADATA, context)
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        report_path = self.parameterAsFileOutput(parameters, self.OUTPUT_HTML,
                                                 context)

        if not path.lower().endswith(P.EXTENSION):
            path += P.EXTENSION

        report = ChangeReport(
            self.tr('Create Layer Package'),
            os.path.basename(path), dry_run=False)

        vectors = rasters = skipped = 0
        rows = []

        try:
            with P.PackageWriter(path, feedback) as writer:
                total = len(layers)
                for index, layer in enumerate(layers):
                    if feedback.isCanceled():
                        raise P.PackageError(self.tr('Cancelled.'))
                    feedback.setProgress(index * 90.0 / total)
                    feedback.pushInfo(self.tr('Packaging {name}…').format(
                        name=layer.name()))

                    entry = self._add_layer(writer, layer, selected_only,
                                            include_resources,
                                            include_metadata)
                    if entry is None:
                        skipped += 1
                        rows.append([layer.name(), self.tr('skipped'), '', ''])
                        continue

                    writer.layers.append(entry)
                    if entry['kind'] == 'vector':
                        vectors += 1
                    else:
                        rasters += 1
                    rows.append([
                        layer.name(), entry['kind'],
                        entry.get('table') or entry.get('file') or '',
                        self.tr('yes') if entry.get('style') else self.tr('no')])

                writer.flush_data()
                writer.groups = P.describe_tree(layers)
                manifest = writer.write_manifest()
                feedback.setProgress(100)
        except P.PackageError as exc:
            raise QgsProcessingException(str(exc))

        report.set_count(self.tr('vector layers'), vectors)
        report.set_count(self.tr('raster layers'), rasters)
        report.set_count(self.tr('layers skipped'), skipped)
        report.set_count(self.tr('resources collected'),
                         len(manifest.get('resources') or {}))
        report.set_count(self.tr('package size (KB)'),
                         int(os.path.getsize(path) / 1024))
        report.add_table(
            self.tr('Layers'),
            [self.tr('Layer'), self.tr('Kind'), self.tr('Stored as'),
             self.tr('Style')], rows)
        for warning in writer.warnings:
            report.warn(warning)

        report.push_to(feedback)
        feedback.pushInfo(self.tr('Package written to {path}').format(path=path))

        return {self.OUTPUT: path,
                self.OUTPUT_HTML: report.write_html(report_path),
                self.LAYER_COUNT: vectors + rasters}

    def _add_layer(self, writer, layer, selected_only, include_resources,
                   include_metadata):
        """One manifest entry, or None when the layer cannot be packaged."""
        if not layer.isValid():
            writer.warn(self.tr('"{name}" is not valid and was skipped.').format(
                name=layer.name()))
            return None

        entry = {'name': layer.name(), 'id': layer.id(),
                 'crs': layer.crs().authid()}

        if isinstance(layer, QgsVectorLayer):
            entry['kind'] = 'vector'
            entry['table'] = writer.add_vector(layer, selected_only)
            if include_resources:
                entry['resources'] = P.collect_resources(layer, writer)
        elif isinstance(layer, QgsRasterLayer):
            entry['kind'] = 'raster'
            stored = writer.add_raster(layer)
            if stored is None:
                return None
            entry['file'] = stored
        else:
            writer.warn(self.tr(
                '"{name}" is neither a vector nor a raster layer and was '
                'skipped.').format(name=layer.name()))
            return None

        key = entry.get('table') or os.path.splitext(
            os.path.basename(entry.get('file', layer.name())))[0]
        entry['style'] = writer.add_style(layer, key)

        if include_metadata:
            with suppress(Exception):       # pragma: no cover - defensive
                metadata = layer.metadata()
                entry['metadata'] = {
                    'title': metadata.title(),
                    'abstract': metadata.abstract(),
                    'identifier': metadata.identifier(),
                }
        return entry


class OpenLayerPackageAlgorithm(QgsProcessingAlgorithm):
    """Unpack a `.kgalp` and add its layers to the project."""

    INPUT = 'INPUT'
    EXTRACT_TO = 'EXTRACT_TO'
    ADD_TO_PROJECT = 'ADD_TO_PROJECT'
    GROUP_NAME = 'GROUP_NAME'
    OUTPUT_FOLDER = 'OUTPUT_FOLDER'
    LAYER_COUNT = 'LAYER_COUNT'

    def tr(self, string):
        return QCoreApplication.translate('KgaLayerPackage', string)

    def createInstance(self):
        return OpenLayerPackageAlgorithm()

    def name(self):
        return 'open_layer_package'

    def displayName(self):
        return self.tr('Open Layer Package')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('open_layer_package')

    def shortHelpString(self):
        return self.tr(
            'Unpack a <b>.kgalp</b> and add its layers, styles and group '
            'structure to the current project.\n\n'
            'The package is extracted to a real folder, never to a temporary '
            'one — the layers point at the extracted files, so a temp folder '
            'would leave a project full of broken layers after the next '
            'restart. If you do not choose a folder, a <i>KGA Packages</i> '
            'folder is created beside the project file and the log says where '
            'everything went.\n\n'
            'SVG and image paths inside each style are rewritten to the '
            'extracted copies, so the symbology renders the same as it did on '
            'the machine that built the package.\n\n'
            'Archive entries that would be written outside the extraction '
            'folder are refused.'
        )

    def flags(self):
        # Adds layers to the project.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, self.tr('Layer package'), extension='kgalp'))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.EXTRACT_TO, self.tr('Extract to'), optional=True,
            createByDefault=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ADD_TO_PROJECT, self.tr('Add the layers to the project'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterString(
            self.GROUP_NAME, self.tr('Group name (the package name if empty)'),
            optional=True))

        self.addOutput(QgsProcessingOutputString(
            self.OUTPUT_FOLDER, self.tr('Extraction folder')))
        self.addOutput(QgsProcessingOutputNumber(
            self.LAYER_COUNT, self.tr('Layers added')))

    def processAlgorithm(self, parameters, context, feedback):
        path = self.parameterAsFile(parameters, self.INPUT, context)
        destination = self.parameterAsString(parameters, self.EXTRACT_TO,
                                             context)
        add_to_project = self.parameterAsBool(parameters, self.ADD_TO_PROJECT,
                                              context)
        group_name = self.parameterAsString(parameters, self.GROUP_NAME,
                                            context)

        try:
            manifest = P.read_manifest(path)
        except P.PackageError as exc:
            raise QgsProcessingException(str(exc))

        stem = os.path.splitext(os.path.basename(path))[0]
        if not destination:
            destination = self._default_folder(stem)
            feedback.pushInfo(self.tr(
                'No folder was chosen, so the package was extracted to '
                '{path}.').format(path=destination))

        try:
            root = P.extract(path, destination, feedback)
        except P.PackageError as exc:
            raise QgsProcessingException(str(exc))

        for warning in manifest.get('warnings') or []:
            feedback.pushWarning(self.tr(
                'From the package: {message}').format(message=warning))

        if not add_to_project:
            return {self.OUTPUT_FOLDER: root, self.LAYER_COUNT: 0}

        added = self._add_layers(manifest, root, group_name or stem, feedback)
        return {self.OUTPUT_FOLDER: root, self.LAYER_COUNT: added}

    def _default_folder(self, stem):
        """A `KGA Packages` folder beside the project file, or the home path."""
        project_file = QgsProject.instance().fileName()
        if project_file:
            base = os.path.join(os.path.dirname(project_file), 'KGA Packages')
        else:
            base = os.path.join(os.path.expanduser('~'), 'KGA Packages')
        return os.path.join(base, stem)

    def _add_layers(self, manifest, root, group_name, feedback):
        project = QgsProject.instance()
        group = project.layerTreeRoot().addGroup(group_name)

        gpkg = os.path.join(root, P.DATA_GPKG)
        by_id = {}
        entries = manifest.get('layers') or []

        for index, entry in enumerate(entries):
            if feedback.isCanceled():
                break
            feedback.setProgress(index * 100.0 / max(1, len(entries)))
            layer = self._load_layer(entry, root, gpkg, feedback)
            if layer is None:
                continue
            project.addMapLayer(layer, False)
            by_id[entry.get('id')] = layer

        # Rebuild the recorded tree, then hang anything the tree did not
        # mention off the group directly, so no layer is silently dropped.
        placed = self._rebuild_tree(manifest.get('tree') or [], by_id, group)
        for layer_id, layer in by_id.items():
            if layer_id not in placed:
                group.addLayer(layer)

        feedback.pushInfo(self.tr('Added {n} layer(s) to the group "{group}".')
                          .format(n=len(by_id), group=group_name))
        return len(by_id)

    def _load_layer(self, entry, root, gpkg, feedback):
        name = entry.get('name') or 'layer'

        if entry.get('kind') == 'vector':
            table = entry.get('table')
            if not table or not os.path.exists(gpkg):
                feedback.pushWarning(self.tr(
                    'The package has no data for "{name}".').format(name=name))
                return None
            layer = QgsVectorLayer('{}|layername={}'.format(gpkg, table), name,
                                   'ogr')
        else:
            relative = (entry.get('file') or '').replace('/', os.sep)
            source = os.path.join(root, relative)
            if not relative or not os.path.exists(source):
                feedback.pushWarning(self.tr(
                    'The package has no file for "{name}".').format(name=name))
                return None
            layer = QgsRasterLayer(source, name)

        if not layer.isValid():
            feedback.pushWarning(self.tr(
                'Could not load "{name}" from the package.').format(name=name))
            return None

        style = entry.get('style')
        if style:
            style_path = os.path.join(root, style.replace('/', os.sep))
            if os.path.exists(style_path):
                message, ok = layer.loadNamedStyle(style_path)
                if not ok:
                    feedback.pushWarning(self.tr(
                        'Could not apply the style for "{name}": {err}').format(
                            name=name, err=message))
                else:
                    # Only after the style has loaded do the symbol layers
                    # exist to be rewritten.
                    rewritten = P.rewrite_resources(
                        layer, entry.get('resources') or {}, root)
                    if rewritten:
                        feedback.pushInfo(self.tr(
                            'Repointed {n} symbol(s) of "{name}" at the '
                            'packaged resources.').format(n=rewritten,
                                                          name=name))
        return layer

    def _rebuild_tree(self, entries, by_id, parent):
        """Recreate the recorded groups. Returns the ids actually placed."""
        placed = set()
        for entry in entries:
            if entry.get('type') == 'group':
                child = parent.addGroup(entry.get('name') or 'Group')
                placed |= self._rebuild_tree(entry.get('children') or [],
                                             by_id, child)
            else:
                layer = by_id.get(entry.get('id'))
                if layer is not None:
                    parent.addLayer(layer)
                    placed.add(entry.get('id'))
        return placed

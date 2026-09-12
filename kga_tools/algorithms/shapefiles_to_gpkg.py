# -*- coding: utf-8 -*-

import os
import re

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterString,
    QgsProcessingParameterCrs,
    QgsCoordinateTransform,
    QgsVectorLayer,
    QgsVectorFileWriter,
)
from ..branding import docs_url


class ShapefilesToGpkg(QgsProcessingAlgorithm):

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'
    EXTENSIONS = 'EXTENSIONS'
    ENCODING = 'ENCODING'
    RECURSIVE = 'RECURSIVE'
    APPEND = 'APPEND'
    SKIP_EMPTY = 'SKIP_EMPTY'
    TARGET_CRS = 'TARGET_CRS'

    # ------------------------------------------------------------------ meta
    def createInstance(self):
        return ShapefilesToGpkg()

    def name(self):
        return 'shapefilestogpkg'

    def displayName(self):
        return self.tr('Shapefiles to GeoPackage')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('shapefilestogpkg')

    def shortHelpString(self):
        return self.tr(
            'Collects every shapefile in a folder and writes them into a '
            'single GeoPackage, using each file name as the layer name.\n\n'
            'Set the encoding to match the source files - QGIS falls back to '
            'the system encoding when a .cpg sidecar is missing, which is the '
            'usual cause of unreadable Khmer attributes.\n\n'
            'Optionally reprojects everything to a common CRS on the way in.'
        )

    def tr(self, string):
        return QCoreApplication.translate('ShapefilesToGpkg', string)

    # ------------------------------------------------------------------ params
    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                self.tr('Input folder'),
                behavior=QgsProcessingParameterFile.Behavior.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                self.tr('Output GeoPackage'),
                fileFilter='GeoPackage (*.gpkg)'
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.EXTENSIONS,
                self.tr('File extensions (comma separated)'),
                defaultValue='shp',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.ENCODING,
                self.tr('Source encoding'),
                defaultValue='UTF-8',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterCrs(
                self.TARGET_CRS,
                self.tr('Reproject all layers to (optional)'),
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.RECURSIVE,
                self.tr('Search subfolders'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.APPEND,
                self.tr('Append to existing GeoPackage (keep current layers)'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SKIP_EMPTY,
                self.tr('Skip layers with no features'),
                defaultValue=False
            )
        )

    # ------------------------------------------------------------------ helper
    @staticmethod
    def safe_name(name):
        """Make a file name safe to use as a GeoPackage layer name."""
        clean = re.sub(r'[^A-Za-z0-9_\-]', '_', name).strip('_')
        clean = re.sub(r'_+', '_', clean)
        if clean and clean[0].isdigit():
            clean = 'L_' + clean
        return clean or 'layer'

    def collect_files(self, folder, exts, recursive):
        found = []
        if recursive:
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    if os.path.splitext(f)[1].lower().lstrip('.') in exts:
                        found.append(os.path.join(root, f))
        else:
            for f in sorted(os.listdir(folder)):
                full = os.path.join(folder, f)
                if os.path.isfile(full) and \
                        os.path.splitext(f)[1].lower().lstrip('.') in exts:
                    found.append(full)
        return sorted(found)

    # ------------------------------------------------------------------ run
    def processAlgorithm(self, parameters, context, feedback):

        folder = self.parameterAsFile(parameters, self.INPUT, context)
        out_gpkg = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        ext_str = self.parameterAsString(parameters, self.EXTENSIONS, context) or 'shp'
        encoding = self.parameterAsString(parameters, self.ENCODING, context) or 'UTF-8'
        recursive = self.parameterAsBool(parameters, self.RECURSIVE, context)
        append = self.parameterAsBool(parameters, self.APPEND, context)
        skip_empty = self.parameterAsBool(parameters, self.SKIP_EMPTY, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)

        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException('Input folder not found: {}'.format(folder))

        if not out_gpkg.lower().endswith('.gpkg'):
            out_gpkg += '.gpkg'

        out_parent = os.path.dirname(out_gpkg)
        if out_parent and not os.path.isdir(out_parent):
            os.makedirs(out_parent, exist_ok=True)

        exts = {e.strip().lower().lstrip('.')
                for e in ext_str.split(',') if e.strip()}

        files = self.collect_files(folder, exts, recursive)
        if not files:
            raise QgsProcessingException(
                'No files matching [{}] found in {}'.format(
                    ', '.join(sorted(exts)), folder))

        transform_context = context.transformContext()

        feedback.pushInfo('Found {} file(s) to pack'.format(len(files)))
        feedback.pushInfo('Target: {}'.format(out_gpkg))
        if target_crs.isValid():
            feedback.pushInfo('Reprojecting all layers to {}'.format(
                target_crs.authid()))
        feedback.pushInfo('')

        # The first successful write creates (or replaces) the container;
        # everything after that is added as an extra layer inside it.
        gpkg_exists = os.path.exists(out_gpkg)
        first_write = not (append and gpkg_exists)

        used_names = set()
        total = len(files)
        packed, skipped, failed = 0, 0, 0

        for i, path in enumerate(files):

            if feedback.isCanceled():
                break

            feedback.setProgress(int(i / total * 100))

            stem = os.path.splitext(os.path.basename(path))[0]
            layer = QgsVectorLayer(path, stem, 'ogr')

            if not layer.isValid():
                feedback.pushWarning('  skip (invalid): {}'.format(stem))
                skipped += 1
                continue

            # Apply the source encoding BEFORE any features are read
            layer.setProviderEncoding(encoding)
            layer.dataProvider().setEncoding(encoding)

            if skip_empty and layer.featureCount() == 0:
                feedback.pushInfo('  skip (empty): {}'.format(stem))
                skipped += 1
                continue

            name = self.safe_name(stem)
            if name.lower() in used_names:
                suffix = 2
                while '{}_{}'.format(name, suffix).lower() in used_names:
                    suffix += 1
                name = '{}_{}'.format(name, suffix)
                feedback.pushInfo('  renamed duplicate to {}'.format(name))
            used_names.add(name.lower())

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = 'GPKG'
            options.layerName = name
            options.fileEncoding = 'UTF-8'   # GeoPackage is UTF-8 by definition

            if first_write:
                options.actionOnExistingFile = \
                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
            else:
                options.actionOnExistingFile = \
                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer

            if target_crs.isValid() and layer.crs() != target_crs:
                options.ct = QgsCoordinateTransform(
                    layer.crs(), target_crs, transform_context)

            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, out_gpkg, transform_context, options
            )

            if result[0] == QgsVectorFileWriter.WriterError.NoError:
                feedback.pushInfo('  ok: {}  ({} features, {})'.format(
                    name, layer.featureCount(),
                    layer.crs().authid() or 'no CRS'))
                packed += 1
                first_write = False
            else:
                feedback.reportError('  FAILED: {} -> {}'.format(stem, result[1]))
                failed += 1

        feedback.setProgress(100)
        feedback.pushInfo('')
        feedback.pushInfo('Packed {} | Skipped {} | Failed {}'.format(
            packed, skipped, failed))

        if packed == 0:
            raise QgsProcessingException(
                'Nothing was written - check the log above.')

        return {self.OUTPUT: out_gpkg}

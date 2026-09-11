# -*- coding: utf-8 -*-
"""
GeoPackage -> Shapefiles
Exports every spatial layer inside a .gpkg to individual ESRI Shapefiles.

Install:
    Processing Toolbox > Scripts (python icon) > Add Script to Toolbox...
    It then appears under  Scripts > KGA Toolbox > GeoPackage to Shapefiles

Notes:
    - Shapefile field names are truncated to 10 characters (DBF limit).
    - A .cpg sidecar is written so UTF-8 / Khmer attributes survive.
"""

import os
import re

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterString,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsVectorFileWriter,
    QgsCoordinateReferenceSystem,
)


class GpkgToShapefiles(QgsProcessingAlgorithm):

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'
    ENCODING = 'ENCODING'
    SKIP_EMPTY = 'SKIP_EMPTY'
    OVERWRITE = 'OVERWRITE'
    PREFIX = 'PREFIX'

    # ------------------------------------------------------------------ meta
    def createInstance(self):
        return GpkgToShapefiles()

    def name(self):
        return 'gpkgtoshapefiles'

    def displayName(self):
        return self.tr('GeoPackage to Shapefiles')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/gpkgtoshapefiles'

    def shortHelpString(self):
        return self.tr(
            'Exports all spatial layers from a GeoPackage into a folder as '
            'individual ESRI Shapefiles.\n\n'
            'Layer names are sanitised for the file system (non-alphanumeric '
            'characters become underscores).\n\n'
            'Warning: the Shapefile format truncates attribute field names to '
            '10 characters and cannot store multiple geometry types or '
            'date-time values with full precision. Keep the GeoPackage as the '
            'master copy.'
        )

    def tr(self, string):
        return QCoreApplication.translate('GpkgToShapefiles', string)

    # ------------------------------------------------------------------ params
    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                self.tr('Input GeoPackage'),
                behavior=QgsProcessingParameterFile.File,
                fileFilter='GeoPackage (*.gpkg *.GPKG)'
            )
        )

        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT,
                self.tr('Output folder')
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.ENCODING,
                self.tr('Field encoding'),
                defaultValue='UTF-8',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.PREFIX,
                self.tr('Filename prefix (optional)'),
                defaultValue='',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SKIP_EMPTY,
                self.tr('Skip layers with no features'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.OVERWRITE,
                self.tr('Overwrite existing shapefiles'),
                defaultValue=True
            )
        )

    # ------------------------------------------------------------------ helper
    @staticmethod
    def safe_name(name):
        """Make a layer name safe for a file name."""
        clean = re.sub(r'[^A-Za-z0-9_\-]', '_', name).strip('_')
        clean = re.sub(r'_+', '_', clean)
        return clean or 'layer'

    # ------------------------------------------------------------------ run
    def processAlgorithm(self, parameters, context, feedback):

        gpkg = self.parameterAsFile(parameters, self.INPUT, context)
        out_dir = self.parameterAsString(parameters, self.OUTPUT, context)
        encoding = self.parameterAsString(parameters, self.ENCODING, context) or 'UTF-8'
        prefix = self.parameterAsString(parameters, self.PREFIX, context) or ''
        skip_empty = self.parameterAsBool(parameters, self.SKIP_EMPTY, context)
        overwrite = self.parameterAsBool(parameters, self.OVERWRITE, context)

        if not gpkg or not os.path.exists(gpkg):
            raise QgsProcessingException('Input GeoPackage not found: {}'.format(gpkg))

        if not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        transform_context = context.transformContext()

        # Discover the sublayers inside the GeoPackage
        meta = QgsProviderRegistry.instance().providerMetadata('ogr')
        sublayers = meta.querySublayers(gpkg)

        if not sublayers:
            raise QgsProcessingException('No layers found inside {}'.format(gpkg))

        feedback.pushInfo('Found {} sublayer(s) in {}'.format(
            len(sublayers), os.path.basename(gpkg)))

        total = len(sublayers)
        exported, skipped, failed = 0, 0, 0

        layer_opts = QgsProviderSublayerDetails.LayerOptions(transform_context)

        for i, sub in enumerate(sublayers):

            if feedback.isCanceled():
                break

            feedback.setProgress(int(i / total * 100))

            layer = sub.toLayer(layer_opts)

            if layer is None or not layer.isValid():
                feedback.pushWarning('  skip (invalid): {}'.format(sub.name()))
                skipped += 1
                continue

            # Non-spatial tables cannot become shapefiles
            if not layer.isSpatial():
                feedback.pushInfo('  skip (no geometry): {}'.format(sub.name()))
                skipped += 1
                continue

            if skip_empty and layer.featureCount() == 0:
                feedback.pushInfo('  skip (empty): {}'.format(sub.name()))
                skipped += 1
                continue

            base = prefix + self.safe_name(sub.name())
            target = os.path.join(out_dir, base + '.shp')

            if os.path.exists(target) and not overwrite:
                feedback.pushInfo('  skip (exists): {}'.format(base + '.shp'))
                skipped += 1
                continue

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = 'ESRI Shapefile'
            options.fileEncoding = encoding
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
            )

            # Fall back to the layer CRS; keeps output identical to source
            crs = layer.crs()
            if not crs.isValid():
                crs = QgsCoordinateReferenceSystem()

            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, target, transform_context, options
            )

            if result[0] == QgsVectorFileWriter.NoError:
                feedback.pushInfo('  ok: {}  ({} features)'.format(
                    base + '.shp', layer.featureCount()))
                exported += 1
            else:
                feedback.reportError('  FAILED: {} -> {}'.format(
                    sub.name(), result[1]))
                failed += 1

        feedback.setProgress(100)
        feedback.pushInfo('')
        feedback.pushInfo('Exported {} | Skipped {} | Failed {}'.format(
            exported, skipped, failed))
        feedback.pushInfo('Output folder: {}'.format(out_dir))

        return {self.OUTPUT: out_dir}

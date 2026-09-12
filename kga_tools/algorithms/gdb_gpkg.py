# -*- coding: utf-8 -*-

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterString,
    QgsProject,
)

from ..core import gdb_convert as C
from ..core.compat import mark_advanced
from ..branding import docs_url


def _split_names(text):
    """'Roads, Canals' -> ('Roads', 'Canals'). Blank means every layer."""
    return tuple(part.strip() for part in (text or '').split(',') if part.strip())


def _project_layers_using(path):
    """Names of project layers whose data lives in `path`.

    Replacing a container that the project still has open fails on Windows with
    a bare permission error, and leaves the project full of broken layers when
    it does not. Better to say so before deleting anything.
    """
    if not path:
        return []
    target = os.path.normcase(os.path.abspath(path))
    names = []
    for layer in QgsProject.instance().mapLayers().values():
        source = layer.source() or ''
        # 'C:/data/x.gpkg|layername=roads', or a plain folder for a .gdb.
        candidate = source.split('|', 1)[0]
        if not candidate:
            continue
        if os.path.normcase(os.path.abspath(candidate)) == target:
            names.append(layer.name())
    return sorted(set(names))


class _ConvertBase(QgsProcessingAlgorithm):
    """The parts both directions share."""

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'
    LAYERS = 'LAYERS'
    APPEND = 'APPEND'
    SKIP_EMPTY = 'SKIP_EMPTY'
    LINEARIZE = 'LINEARIZE'
    PRESERVE_FID = 'PRESERVE_FID'
    STYLES = 'STYLES'
    LAYER_COUNT = 'LAYER_COUNT'

    def tr(self, string):
        return QCoreApplication.translate('KgaGdbGpkg', string)

    def group(self):
        return self.tr('KGA Data Conversion')

    def groupId(self):
        return 'kgadataconversion'

    # ------------------------------------------------------------ parameters
    def _add_shared_parameters(self, style_label):
        self.addParameter(mark_advanced(QgsProcessingParameterString(
            self.LAYERS, self.tr('Only these layers (comma separated)'),
            optional=True)))
        self.addParameter(QgsProcessingParameterBoolean(
            self.STYLES, style_label, defaultValue=True))
        self.addParameter(mark_advanced(QgsProcessingParameterBoolean(
            self.SKIP_EMPTY, self.tr('Skip layers with no features'),
            defaultValue=False)))
        self.addParameter(mark_advanced(QgsProcessingParameterBoolean(
            self.LINEARIZE,
            self.tr('Convert curved geometries to straight segments'),
            defaultValue=False)))
        self.addParameter(mark_advanced(QgsProcessingParameterBoolean(
            self.APPEND,
            self.tr('Add to the existing output instead of replacing it'),
            defaultValue=False)))
        self.addOutput(QgsProcessingOutputNumber(
            self.LAYER_COUNT, self.tr('Layers converted')))

    def _shared_settings(self, parameters, context):
        return {
            'only': _split_names(
                self.parameterAsString(parameters, self.LAYERS, context)),
            'append': self.parameterAsBool(parameters, self.APPEND, context),
            'skip_empty': self.parameterAsBool(
                parameters, self.SKIP_EMPTY, context),
            'linearize': self.parameterAsBool(
                parameters, self.LINEARIZE, context),
            'preserve_fid': self.parameterAsBool(
                parameters, self.PRESERVE_FID, context),
            'carry_styles': self.parameterAsBool(
                parameters, self.STYLES, context),
        }

    # ----------------------------------------------------------------- run
    def _run(self, source, target, settings, feedback, replacing):
        if replacing:
            open_layers = _project_layers_using(target)
            if open_layers:
                raise QgsProcessingException(self.tr(
                    'The output is already open in this project ({layers}). '
                    'Remove those layers first, or tick "Add to the existing '
                    'output instead of replacing it".').format(
                        layers=', '.join(open_layers[:6])))

        try:
            result = C.convert(source, target, settings, feedback)
        except C.ConversionError as error:
            raise QgsProcessingException(str(error))

        if not result.count:
            raise QgsProcessingException(self.tr(
                'Nothing was converted - see the log above.'))
        return result


class FileGdbToGeoPackage(_ConvertBase):
    """A whole .gdb, layer for layer, into one editable .gpkg."""

    USE_ALIASES = 'USE_ALIASES'
    DATASET_PREFIX = 'DATASET_PREFIX'
    LOAD = 'LOAD'

    def createInstance(self):
        return FileGdbToGeoPackage()

    def name(self):
        return 'filegdb_to_geopackage'

    def displayName(self):
        return self.tr('File Geodatabase to GeoPackage')

    def helpUrl(self):
        return docs_url('filegdb_to_geopackage')

    def shortHelpString(self):
        return self.tr(
            'Copies <b>every layer and table</b> of an ArcGIS File Geodatabase '
            'into one GeoPackage you can actually edit in QGIS.\n\n'
            '<b>What travels with the data</b>\n'
            '• Coded and range <b>field domains</b>, so the drop-down lists '
            'still work.\n'
            '• Non-spatial tables, Z and M values, and true curves.\n'
            '• Layer <b>aliases</b>: an ArcGIS class called '
            '<i>Canal_Alignment</i> with the alias <i>Canal Alignment</i> '
            'arrives under the readable name. Untick the alias option to keep '
            'the raw table names instead.\n'
            '• QGIS styles, if this geodatabase came out of the matching '
            '<i>GeoPackage to File Geodatabase</i> tool.\n\n'
            '<b>What changes</b>\n'
            'A GeoPackage is flat, so feature datasets disappear. Tick '
            '<i>Prefix layer names with their feature dataset</i> to keep the '
            'grouping visible in the layer names, which also keeps two classes '
            'of the same name in different feature datasets apart.\n\n'
            'ObjectID values become the GeoPackage feature ids, so a feature '
            'keeps its identity through the round trip.\n\n'
            'Close the geodatabase in ArcGIS first. A .lock file in the folder '
            'means it is still open there, and the log will say so.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, self.tr('File Geodatabase (.gdb folder)'),
            behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr('Output GeoPackage'),
            fileFilter='GeoPackage (*.gpkg)'))
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_ALIASES, self.tr('Use layer aliases as layer names'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DATASET_PREFIX,
            self.tr('Prefix layer names with their feature dataset'),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD, self.tr('Add the converted layers to the project'),
            defaultValue=False))
        self._add_shared_parameters(
            self.tr('Restore QGIS layer styles the geodatabase carries'))
        self.addParameter(mark_advanced(QgsProcessingParameterBoolean(
            self.PRESERVE_FID,
            self.tr('Keep ObjectID values as GeoPackage feature ids'),
            defaultValue=True)))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsFile(parameters, self.INPUT, context)
        target = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        settings = C.Settings(
            target_format=C.GPKG_DRIVER,
            use_aliases=self.parameterAsBool(
                parameters, self.USE_ALIASES, context),
            dataset_prefix=self.parameterAsBool(
                parameters, self.DATASET_PREFIX, context),
            **self._shared_settings(parameters, context))

        target = C.ensure_suffix(target, C.GPKG_SUFFIX)
        result = self._run(source, target, settings, feedback,
                           replacing=not settings.append)

        if self.parameterAsBool(parameters, self.LOAD, context):
            self._queue_layers(target, result, context, feedback)

        return {self.OUTPUT: target, self.LAYER_COUNT: result.count}

    def _queue_layers(self, gpkg, result, context, feedback):
        """Hand the written layers to Processing to add when the run ends.

        Going through the context rather than `QgsProject.addMapLayer` is what
        lets this algorithm keep running on a worker thread; adding layers
        directly would force it onto the GUI thread and freeze QGIS for the
        length of a large conversion.
        """
        project = context.project() or QgsProject.instance()
        for _source, name in result.written:
            uri = '{}|layername={}'.format(gpkg, name)
            try:
                details = QgsProcessingContext.LayerDetails(
                    name, project, name)
                context.addLayerToLoadOnCompletion(uri, details)
            except Exception as error:      # pragma: no cover - API drift
                feedback.pushWarning(self.tr(
                    'Could not queue "{name}" for loading: {error}').format(
                        name=name, error=error))
                return


class GeoPackageToFileGdb(_ConvertBase):
    """A whole .gpkg back into a .gdb the ArcGIS side can open."""

    FEATURE_DATASET = 'FEATURE_DATASET'
    ARCGIS_VERSION = 'ARCGIS_VERSION'
    SHAPE_FIELDS = 'SHAPE_FIELDS'
    SET_ALIASES = 'SET_ALIASES'

    #: Index order must match ARCGIS_VERSION_VALUES.
    ARCGIS_VERSION_VALUES = (C.ARCGIS_ALL, C.ARCGIS_PRO_32)

    def createInstance(self):
        return GeoPackageToFileGdb()

    def name(self):
        return 'geopackage_to_filegdb'

    def displayName(self):
        return self.tr('GeoPackage to File Geodatabase')

    def helpUrl(self):
        return docs_url('geopackage_to_filegdb')

    def shortHelpString(self):
        return self.tr(
            'Writes a whole GeoPackage back out as an ArcGIS File Geodatabase, '
            'ready to hand to someone working in ArcMap or ArcGIS Pro.\n\n'
            '<b>What the geodatabase gets</b>\n'
            '• An <b>OBJECTID</b> column, and optional <b>Shape_Length</b> and '
            '<b>Shape_Area</b> fields, the way ArcGIS makes them itself.\n'
            '• Coded and range <b>field domains</b>, carried over intact.\n'
            '• Lines and polygons stored as multi-part, which is what every '
            'ArcGIS feature class is.\n\n'
            '<b>Names</b>\n'
            'A geodatabase table name may only hold letters, digits and '
            'underscores, and may not start with a digit, so '
            '<i>2024 roads-final</i> becomes <i>T_2024_roads_final</i>. The '
            'name you started with is kept as the ArcGIS <b>alias</b>, so it '
            'is what the coworker sees in the table of contents — and it is '
            'what the reverse tool restores when the data comes back. Every '
            'rename is listed in the log.\n\n'
            '<b>Compatibility</b>\n'
            'Leave the ArcGIS version on <i>every ArcGIS version</i> unless '
            'you know the recipient is on Pro 3.2 or newer. The Pro 3.2 '
            'setting is only needed to keep 64-bit integer fields exact; on '
            'the compatible setting those become decimals, which is a problem '
            'for very large ID numbers and for nothing else.\n\n'
            'Ticking the styles option adds a small <b>KGA_layer_styles</b> '
            'table to the geodatabase. ArcGIS ignores it, and the reverse '
            'tool uses it to give you your symbology back.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, self.tr('GeoPackage'), extension='gpkg'))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT, self.tr('Output File Geodatabase (.gdb folder)')))
        self.addParameter(QgsProcessingParameterEnum(
            self.ARCGIS_VERSION, self.tr('Readable by'),
            options=[
                self.tr('Every ArcGIS version (64-bit integers become '
                        'decimals)'),
                self.tr('ArcGIS Pro 3.2 and later (keeps 64-bit integers)'),
            ],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.FEATURE_DATASET,
            self.tr('Put the layers in this feature dataset'), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SHAPE_FIELDS,
            self.tr('Create Shape_Length and Shape_Area fields'),
            defaultValue=True))
        self._add_shared_parameters(
            self.tr('Carry QGIS layer styles across '
                    '(adds a KGA_layer_styles table)'))
        self.addParameter(mark_advanced(QgsProcessingParameterBoolean(
            self.SET_ALIASES,
            self.tr('Keep the original layer name as the ArcGIS alias'),
            defaultValue=True)))
        self.addParameter(mark_advanced(QgsProcessingParameterBoolean(
            self.PRESERVE_FID,
            self.tr('Keep GeoPackage feature ids as ObjectID values'),
            defaultValue=True)))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsFile(parameters, self.INPUT, context)
        target = self.parameterAsString(parameters, self.OUTPUT, context)
        version = self.parameterAsEnum(parameters, self.ARCGIS_VERSION, context)

        settings = C.Settings(
            target_format=C.GDB_DRIVER,
            feature_dataset=(self.parameterAsString(
                parameters, self.FEATURE_DATASET, context) or '').strip(),
            arcgis_version=self.ARCGIS_VERSION_VALUES[version],
            shape_fields=self.parameterAsBool(
                parameters, self.SHAPE_FIELDS, context),
            set_aliases=self.parameterAsBool(
                parameters, self.SET_ALIASES, context),
            **self._shared_settings(parameters, context))

        target = C.ensure_suffix(target, C.GDB_SUFFIX)
        result = self._run(source, target, settings, feedback,
                           replacing=not settings.append)

        feedback.pushInfo('')
        feedback.pushInfo(self.tr(
            'Open {path} in ArcGIS Catalog to check it before sending it '
            'on.').format(path=target))
        return {self.OUTPUT: target, self.LAYER_COUNT: result.count}

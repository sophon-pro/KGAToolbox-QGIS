# -*- coding: utf-8 -*-
import json
import math

from qgis.PyQt.QtCore import QCoreApplication, QTimer
from qgis.PyQt.QtWidgets import QMessageBox

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsMarkerSymbol,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingFeatureSourceDefinition,
    QgsProcessingOutputNumber,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingUtils,
    QgsProject,
    QgsProperty,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
)

from ..core.compat import T_LONGLONG, T_STRING, make_field, source_type

try:
    from qgis.utils import iface
except ImportError:
    iface = None


class PointOnBoundaryChecker(QgsProcessingAlgorithm):
    """Check if points align with boundary vertices."""

    INPUT_BOUNDARY = 'INPUT_BOUNDARY'
    INPUT_POINTS = 'INPUT_POINTS'
    TOLERANCE = 'TOLERANCE'
    ERROR_COUNT = 'ERROR_COUNT'

    ERROR_LAYER_NAME = 'Topology Errors - Points Not on Boundary'
    PROP_KEY = 'kga_tools/point_boundary_error_layer'

    def tr(self, string):
        return QCoreApplication.translate('PointOnBoundaryChecker', string)

    def createInstance(self):
        return PointOnBoundaryChecker()

    def name(self):
        return 'checkpointsonboundary'

    def displayName(self):
        return self.tr('Check Points on Boundary Vertices')

    def group(self):
        return self.tr('KGA Topology')

    def groupId(self):
        return 'kgatopology'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/checkpointsonboundary'

    def shortHelpString(self):
        return self.tr(
            'Flags points that do not sit on a vertex of the boundary layer.\n\n'
            'Every point is tested against the vertices of the polygon or line '
            'boundary; anything further away than the snapping tolerance is '
            'written to a "Topology Errors - Points Not on Boundary" memory '
            'layer.\n\n'
            'The tolerance is in the layer\'s map units, so a projected CRS '
            'means metres and a geographic CRS means degrees.\n\n'
            'Review the results with Open Error Inspector, which can zoom to '
            'each error and re-run this check once the points are moved.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_BOUNDARY,
                self.tr('Boundary layer (Polygon or Line)'),
                [source_type('VectorPolygon'), source_type('VectorLine')]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_POINTS,
                self.tr('Point layer to check'),
                [source_type('VectorPoint')]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOLERANCE,
                self.tr('Snapping tolerance (map units)'),
                QgsProcessingParameterNumber.Double,
                defaultValue=0.001,
                minValue=0.0,
            )
        )
        self.addOutput(QgsProcessingOutputNumber(self.ERROR_COUNT, self.tr('Errors found')))

    @staticmethod
    def _source_layer(parameters, name, context):
        """The project layer behind a feature-source parameter, and its selection flag.

        parameterAsVectorLayer returns None for the
        QgsProcessingFeatureSourceDefinition that the "Selected features only"
        checkbox produces, so unwrap that case before resolving the layer.
        """
        value = parameters.get(name)
        selected_only = False
        if isinstance(value, QgsProcessingFeatureSourceDefinition):
            selected_only = bool(value.selectedFeaturesOnly)
            value = value.source
        if isinstance(value, QgsProperty):
            value = value.staticValue()
        if isinstance(value, QgsVectorLayer):
            return value, selected_only
        if isinstance(value, str) and value:
            layer = QgsProcessingUtils.mapLayerFromString(value, context)
            if isinstance(layer, QgsVectorLayer):
                return layer, selected_only
        return None, selected_only

    def processAlgorithm(self, parameters, context, feedback):
        boundary_source = self.parameterAsSource(parameters, self.INPUT_BOUNDARY, context)
        point_source = self.parameterAsSource(parameters, self.INPUT_POINTS, context)
        tolerance = self.parameterAsDouble(parameters, self.TOLERANCE, context)

        if not boundary_source or not point_source:
            raise QgsProcessingException(self.tr('Invalid input layers.'))

        self._target_crs = boundary_source.sourceCrs()
        boundary_layer, boundary_selected = self._source_layer(
            parameters, self.INPUT_BOUNDARY, context)
        point_layer, points_selected = self._source_layer(
            parameters, self.INPUT_POINTS, context)
        self._input_ids = {
            layer.id() for layer in (boundary_layer, point_layer) if layer is not None
        }
        # QgsFeatureSource instances cannot be stored in a layer property.  For
        # project layers, their IDs are stable Processing parameter values.
        # Anything else - a file picked straight off disk - is loaded into the
        # run's own temporary layer store, whose IDs die with the context, so
        # write no metadata rather than metadata that fails in Validate All.
        project = context.project() or QgsProject.instance()
        in_project = [
            layer for layer in (boundary_layer, point_layer)
            if layer is not None and project.mapLayer(layer.id()) is not None
        ]
        self._validation_params = {}
        self._validation_selected = []
        if len(in_project) == 2:
            self._validation_params = {
                self.INPUT_BOUNDARY: boundary_layer.id(),
                self.INPUT_POINTS: point_layer.id(),
                self.TOLERANCE: tolerance,
            }
            # "Selected features only" arrives as a
            # QgsProcessingFeatureSourceDefinition, which will not survive JSON.
            # Record the flag so the inspector can rebuild the definition.
            if boundary_selected:
                self._validation_selected.append(self.INPUT_BOUNDARY)
            if points_selected:
                self._validation_selected.append(self.INPUT_POINTS)

        # Reproject points if necessary
        xform = None
        if point_source.sourceCrs() != self._target_crs:
            xform = QgsCoordinateTransform(point_source.sourceCrs(), self._target_crs, context.transformContext())

        # 1. Extract Boundary Vertices into Spatial Index
        feedback.pushInfo(self.tr('Extracting boundary vertices...'))
        boundary_index = QgsSpatialIndex()
        # Plain coordinates, not a QgsGeometry per vertex: holding one geometry
        # object for every boundary vertex is what made this run out of memory
        # on real cadastral layers.
        vertices = []

        for feat in boundary_source.getFeatures():
            if feedback.isCanceled(): return {}
            geom = feat.geometry()
            if geom.isNull(): continue
            for v in geom.vertices():
                point = QgsPointXY(v)
                f = QgsFeature()
                f.setId(len(vertices))
                f.setGeometry(QgsGeometry.fromPointXY(point))
                boundary_index.addFeature(f)
                vertices.append(point)

        if not vertices:
            feedback.pushWarning(self.tr(
                'The boundary layer has no vertices, so every point is reported '
                'as an error.'
            ))

        # 2. Check Points against Vertices
        feedback.pushInfo(self.tr('Checking points against vertices...'))
        self._errors = []
        total_points = point_source.featureCount()
        step = 100.0 / total_points if total_points > 0 else 1

        skipped = 0
        for current, feat in enumerate(point_source.getFeatures()):
            if feedback.isCanceled(): return {}
            geom = feat.geometry()
            if geom.isNull(): continue

            if xform:
                try:
                    geom.transform(xform)
                except Exception:
                    skipped += 1
                    continue

            # Every part on its own: testing a multipoint as one geometry let a
            # single part landing on a vertex clear the whole feature, and the
            # error layer is single-part anyway.
            for part in (QgsPointXY(v) for v in geom.vertices()):
                # Growing the bounding box is free; buffering built a 5-segment
                # circle for every point only to read its extent straight back.
                search_bbox = QgsRectangle(part.x(), part.y(), part.x(), part.y())
                search_bbox.grow(tolerance)

                is_on_boundary = False
                for cid in boundary_index.intersects(search_bbox):
                    vertex = vertices[cid]
                    if math.dist((part.x(), part.y()),
                                 (vertex.x(), vertex.y())) <= tolerance:
                        is_on_boundary = True
                        break

                if not is_on_boundary:
                    self._errors.append(
                        {'fid': feat.id(), 'geom': QgsGeometry.fromPointXY(part)})

            feedback.setProgress(min(100, int(current * step)))

        if skipped:
            feedback.pushWarning(self.tr(
                '{} point(s) could not be reprojected and were skipped.'
            ).format(skipped))

        feedback.pushInfo(self.tr('Result: {} point(s) not on boundary.').format(len(self._errors)))
        return {self.ERROR_COUNT: len(self._errors)}

    def postProcessAlgorithm(self, context, feedback):
        errors = getattr(self, '_errors', [])

        # Remove old error layers tagged with custom property
        self._remove_previous_error_layers()

        if errors:
            self._add_layer(self._build_error_layer(errors))

        QTimer.singleShot(0, lambda: self._report(len(errors)))
        return {self.ERROR_COUNT: len(errors)}

    def _remove_previous_error_layers(self):
        project = QgsProject.instance()
        keep = getattr(self, '_input_ids', set())
        doomed = []
        for layer_id, layer in project.mapLayers().items():
            if layer_id in keep:
                continue
            if layer.customProperty(self.PROP_KEY):
                doomed.append(layer_id)
                continue
            # Error layers written before the property existed are recognised
            # by name, but only in-memory ones: a file-backed layer the user
            # happens to have named this is theirs, not ours.
            if layer.name() == self.ERROR_LAYER_NAME and \
                    layer.providerType() == 'memory':
                doomed.append(layer_id)
        if doomed:
            project.removeMapLayers(doomed)

    def _add_layer(self, layer):
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(0, layer)

    def _build_error_layer(self, errors):
        # The geometries are in the boundary layer's coordinates.  Naming a CRS
        # the input never had would put the errors somewhere else on the map, so
        # an input with no CRS gives an error layer with no CRS.
        crs = getattr(self, '_target_crs', None)
        # An empty "crs=" is what leaves a memory layer unprojected; drop the
        # parameter entirely and QGIS assumes EPSG:4326.
        authid = crs.authid() if crs is not None and crs.isValid() else ''
        layer = QgsVectorLayer(f'Point?crs={authid}', self.ERROR_LAYER_NAME, 'memory')
        if crs is not None and crs.isValid():
            layer.setCrs(crs)

        fields = [
            make_field('error', T_STRING, 30),
            make_field('point_fid', T_LONGLONG),
        ]
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()

        features = []
        for item in errors:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(item['geom'])
            feat.setAttributes(['Point not on boundary', int(item['fid'])])
            features.append(feat)

        layer.dataProvider().addFeatures(features)
        layer.updateExtents()

        # Updated symbol properties for cyan square
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'square',
            'color': '0,255,255,255',
            'outline_color': '0,255,255,255',
            'size': '4',
            'size_unit': 'MM'
        })

        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setCustomProperty(self.PROP_KEY, 'point_error')
        layer.setCustomProperty('showFeatureCount', True)
        params = getattr(self, '_validation_params', None)
        if params:
            layer.setCustomProperty('kga_tools/algo_id', self.id())
            layer.setCustomProperty('kga_tools/algo_params', json.dumps(params))
            layer.setCustomProperty('kga_tools/algo_params_selected', json.dumps(
                getattr(self, '_validation_selected', [])
            ))

        return layer

    def _report(self, error_count):
        parent = iface.mainWindow() if iface is not None else None
        title = self.tr('Topology Check')

        if not error_count:
            QMessageBox.information(
                parent, title,
                self.tr('No errors found.\n\nAll points align with boundary vertices.')
            )
            if iface:
                iface.messageBar().pushSuccess(title, self.tr('No topology errors found.'))
            return

        QMessageBox.warning(
            parent, title,
            self.tr('{} point(s) not on boundary vertices.\n\nThe error layer has been added to '
                    'the top of the layer panel.').format(error_count)
        )
        if iface:
            iface.messageBar().pushWarning(title, self.tr('{} error(s) found.').format(error_count))

# -*- coding: utf-8 -*-
"""
Topology Checker - Overlaps & Gaps
==================================

A QGIS Processing algorithm that checks polygon layers for:
  * overlaps  - within a single layer and/or between several layers
  * gaps      - enclosed holes between polygons (slivers, unfilled voids)

Behaviour
---------
* No errors found  -> a message box tells the user, and any error layers left
                      over from a previous run are removed from the layer panel.
* Errors found     -> memory layers "Topology Errors - Overlaps" / "... - Gaps"
                      are added to the top of the layer panel with the agreed
                      symbology:
                          overlap : fill #FF8080, outline #FF0000
                          gap     : no fill, outline #FF8080

Install
-------
Processing Toolbox > Scripts (python icon) > "Add Script to Toolbox..."
or drop this file in:
    <profile>/processing/scripts/

Author: KGA Tools
"""

from qgis.PyQt.QtCore import QCoreApplication, QTimer
from qgis.PyQt.QtWidgets import QMessageBox

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.compat import (
    T_DOUBLE, T_LONGLONG, T_STRING, make_field, no_threading, source_type,
)

try:
    from qgis.utils import iface
except ImportError:  # running head-less
    iface = None


class TopologyCheckerAlgorithm(QgsProcessingAlgorithm):
    """Check polygon layers for overlaps and gaps."""

    INPUT = 'INPUT'
    SELECTED_ONLY = 'SELECTED_ONLY'
    CHECK_OVERLAPS = 'CHECK_OVERLAPS'
    CHECK_GAPS = 'CHECK_GAPS'
    MIN_AREA = 'MIN_AREA'
    MAX_GAP_AREA = 'MAX_GAP_AREA'

    OVERLAP_COUNT = 'OVERLAP_COUNT'
    GAP_COUNT = 'GAP_COUNT'

    OVERLAP_LAYER_NAME = 'Topology Errors - Overlaps'
    GAP_LAYER_NAME = 'Topology Errors - Gaps'

    #: custom property used to recognise our own error layers on a re-run
    PROP_KEY = 'kga_tools/topology_error_layer'

    # ------------------------------------------------------------------ #
    # Boilerplate
    # ------------------------------------------------------------------ #
    def tr(self, string):
        return QCoreApplication.translate('TopologyChecker', string)

    def createInstance(self):
        return TopologyCheckerAlgorithm()

    def flags(self):
        # _collect() iterates the project's QgsVectorLayer objects directly
        # rather than a Processing feature-source snapshot, and map layers may
        # only be touched on the GUI thread.
        return no_threading(super().flags())

    def name(self):
        return 'checkoverlapsandgaps'

    def displayName(self):
        return self.tr('Check Overlaps and Gaps')

    def group(self):
        return self.tr('KGA Topology')

    def groupId(self):
        return 'kgatopology'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/checkoverlapsandgaps'

    def shortHelpString(self):
        return self.tr(
            'Checks polygon layers for topological errors.\n\n'
            '<b>Overlaps</b> are reported for every pair of polygons whose '
            'intersection has an area. If more than one layer is supplied, both '
            'within-layer and between-layer overlaps are reported.\n\n'
            '<b>Gaps</b> are the enclosed holes left between polygons once all '
            'input features are dissolved together. Voids that open onto the '
            'outer edge of the dataset are not enclosed and therefore cannot be '
            'detected this way.\n\n'
            'If no errors are found, a message is shown and any error layers '
            'from a previous run are removed from the layer panel. Otherwise '
            'the error layers are (re)created and placed at the top of the '
            'layer panel.\n\n'
            'Areas are expressed in the square map units of the first input '
            'layer, which also defines the CRS used for the check.'
        )

    # ------------------------------------------------------------------ #
    # Parameters
    # ------------------------------------------------------------------ #
    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT,
                self.tr('Input polygon layer(s)'),
                source_type('VectorPolygon'),
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SELECTED_ONLY,
                self.tr('Use selected features only (when a selection exists)'),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CHECK_OVERLAPS, self.tr('Check overlaps'), defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CHECK_GAPS, self.tr('Check gaps'), defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_AREA,
                self.tr('Ignore errors smaller than (square map units)'),
                QgsProcessingParameterNumber.Double,
                defaultValue=0.000001,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_GAP_AREA,
                self.tr('Ignore gaps larger than (square map units, 0 = no limit)'),
                QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
            )
        )

        self.addOutput(QgsProcessingOutputNumber(self.OVERLAP_COUNT, self.tr('Overlaps found')))
        self.addOutput(QgsProcessingOutputNumber(self.GAP_COUNT, self.tr('Gaps found')))

    # ------------------------------------------------------------------ #
    # Geometry helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _polygons_only(geom):
        """Return the polygonal part of a geometry, or None."""
        if geom is None or geom.isEmpty():
            return None
        if geom.type() == QgsWkbTypes.PolygonGeometry:
            return geom
        parts = [
            p for p in geom.asGeometryCollection()
            if p and not p.isEmpty() and p.type() == QgsWkbTypes.PolygonGeometry
        ]
        if not parts:
            return None
        return QgsGeometry.collectGeometry(parts)

    @classmethod
    def _repair(cls, geom):
        """Make a geometry GEOS-valid and keep only its polygonal parts."""
        if geom is None or geom.isEmpty():
            return None
        if not geom.isGeosValid():
            try:
                geom = geom.makeValid()
            except Exception:
                geom = geom.buffer(0, 8)
        return cls._polygons_only(geom)

    def _collect(self, layers, target_crs, selected_only, context, feedback):
        """Read every input feature into a flat list of repaired geometries."""
        entries = []
        for layer in layers:
            if layer is None or not layer.isValid():
                continue
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                feedback.pushWarning(
                    self.tr('Skipping "{}" - not a polygon layer.').format(layer.name())
                )
                continue

            xform = None
            if layer.crs().isValid() and layer.crs() != target_crs:
                xform = QgsCoordinateTransform(layer.crs(), target_crs, context.transformContext())
                feedback.pushInfo(
                    self.tr('Reprojecting "{}" to {} for the check.').format(
                        layer.name(), target_crs.authid() or target_crs.description()
                    )
                )

            use_selection = selected_only and layer.selectedFeatureCount() > 0
            iterator = layer.getSelectedFeatures() if use_selection else layer.getFeatures()

            skipped = 0
            for feat in iterator:
                if feedback.isCanceled():
                    return entries
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                geom = QgsGeometry(geom)
                if xform is not None:
                    try:
                        geom.transform(xform)
                    except Exception:
                        skipped += 1
                        continue
                geom = self._repair(geom)
                if geom is None:
                    skipped += 1
                    continue
                entries.append({'layer': layer.name(), 'fid': feat.id(), 'geom': geom})

            if skipped:
                feedback.pushWarning(
                    self.tr('{} feature(s) of "{}" could not be used.').format(skipped, layer.name())
                )
        return entries

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    def _find_overlaps(self, entries, min_area, feedback):
        index = QgsSpatialIndex()
        for i, entry in enumerate(entries):
            ft = QgsFeature()
            ft.setId(i)
            ft.setGeometry(entry['geom'])
            index.addFeature(ft)

        overlaps = []
        total = len(entries) or 1
        for i, entry in enumerate(entries):
            if feedback.isCanceled():
                break
            geom_a = entry['geom']
            for j in index.intersects(geom_a.boundingBox()):
                if j <= i:
                    continue
                geom_b = entries[j]['geom']
                if not geom_a.intersects(geom_b):
                    continue
                inter = self._polygons_only(geom_a.intersection(geom_b))
                if inter is None:
                    continue
                area = inter.area()
                if area <= min_area:
                    continue
                overlaps.append({
                    'geom': inter,
                    'layer_a': entry['layer'], 'fid_a': entry['fid'],
                    'layer_b': entries[j]['layer'], 'fid_b': entries[j]['fid'],
                    'area': area,
                })
            feedback.setProgress(int(i * 100.0 / total))
        return overlaps

    def _find_gaps(self, entries, min_area, max_area, feedback):
        feedback.pushInfo(self.tr('Dissolving {} feature(s) to locate gaps...').format(len(entries)))
        union = QgsGeometry.unaryUnion([e['geom'] for e in entries])
        if union is None or union.isEmpty():
            return []

        if QgsWkbTypes.isCurvedType(union.wkbType()):
            union = QgsGeometry(union.constGet().segmentize())

        try:
            polygons = union.asMultiPolygon() if union.isMultipart() else [union.asPolygon()]
        except Exception:
            feedback.pushWarning(self.tr('The dissolved geometry could not be read for gap analysis.'))
            return []

        gaps = []
        for polygon in polygons:
            if feedback.isCanceled():
                break
            if not polygon:
                continue
            for ring in polygon[1:]:          # ring 0 is the exterior
                gap = QgsGeometry.fromPolygonXY([ring])
                gap = self._repair(gap)
                if gap is None:
                    continue
                area = gap.area()
                if area <= min_area:
                    continue
                if max_area > 0 and area > max_area:
                    continue
                gaps.append({'geom': gap, 'area': area})
        return gaps

    # ------------------------------------------------------------------ #
    # Processing
    # ------------------------------------------------------------------ #
    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT, context)
        selected_only = self.parameterAsBool(parameters, self.SELECTED_ONLY, context)
        do_overlaps = self.parameterAsBool(parameters, self.CHECK_OVERLAPS, context)
        do_gaps = self.parameterAsBool(parameters, self.CHECK_GAPS, context)
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context)
        max_gap_area = self.parameterAsDouble(parameters, self.MAX_GAP_AREA, context)

        if not layers:
            raise QgsProcessingException(self.tr('Please select at least one polygon layer.'))
        if not do_overlaps and not do_gaps:
            raise QgsProcessingException(self.tr('Enable at least one check.'))

        # remember the inputs so a re-run never deletes a layer we just read
        self._input_ids = {lyr.id() for lyr in layers if lyr is not None}
        # Error layers retain enough information for Error Inspector to repeat
        # this exact check.  Store layer IDs, rather than QgsVectorLayer
        # objects, because custom properties must be serialisable.  An ID only
        # resolves again if the layer is in the project, so a run over a file
        # picked straight off disk records nothing at all - better than metadata
        # that makes Validate All fail later.
        project = context.project() or QgsProject.instance()
        replayable = sorted(
            lyr.id() for lyr in layers
            if lyr is not None and project.mapLayer(lyr.id()) is not None
        )
        self._validation_params = {}
        if len(replayable) == len(self._input_ids):
            self._validation_params = {
                self.INPUT: replayable,
                self.SELECTED_ONLY: selected_only,
                self.CHECK_OVERLAPS: do_overlaps,
                self.CHECK_GAPS: do_gaps,
                self.MIN_AREA: min_area,
                self.MAX_GAP_AREA: max_gap_area,
            }

        target_crs = layers[0].crs()
        self._target_crs = target_crs

        entries = self._collect(layers, target_crs, selected_only, context, feedback)
        if feedback.isCanceled():
            return {}
        if not entries:
            if selected_only:
                raise QgsProcessingException(self.tr(
                    'No usable polygon features in the selection. Select the '
                    'polygons to check, or turn off "Use selected features only".'
                ))
            raise QgsProcessingException(self.tr('No usable polygon features in the input.'))
        feedback.pushInfo(self.tr('Checking {} feature(s).').format(len(entries)))

        self._overlaps = self._find_overlaps(entries, min_area, feedback) if do_overlaps else []
        if feedback.isCanceled():
            return {}
        self._gaps = self._find_gaps(entries, min_area, max_gap_area, feedback) if do_gaps else []

        feedback.setProgress(100)
        feedback.pushInfo(
            self.tr('Result: {} overlap(s), {} gap(s).').format(len(self._overlaps), len(self._gaps))
        )
        return {self.OVERLAP_COUNT: len(self._overlaps), self.GAP_COUNT: len(self._gaps)}

    # ------------------------------------------------------------------ #
    # Layer panel handling (main thread)
    # ------------------------------------------------------------------ #
    def postProcessAlgorithm(self, context, feedback):
        overlaps = getattr(self, '_overlaps', [])
        gaps = getattr(self, '_gaps', [])

        self._remove_previous_error_layers()

        if overlaps:
            self._add_layer(self._build_overlap_layer(overlaps))
        if gaps:
            self._add_layer(self._build_gap_layer(gaps))

        QTimer.singleShot(0, lambda: self._report(len(overlaps), len(gaps)))
        return {self.OVERLAP_COUNT: len(overlaps), self.GAP_COUNT: len(gaps)}

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
            if layer.name() in (self.OVERLAP_LAYER_NAME, self.GAP_LAYER_NAME) and \
                    layer.providerType() == 'memory':
                doomed.append(layer_id)
        if doomed:
            project.removeMapLayers(doomed)

    def _add_layer(self, layer):
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(0, layer)

    def _set_validation_metadata(self, layer):
        """Make a generated layer re-runnable from Error Inspector."""
        import json
        params = getattr(self, '_validation_params', None)
        if not params:
            return
        layer.setCustomProperty('kga_tools/algo_id', self.id())
        layer.setCustomProperty('kga_tools/algo_params', json.dumps(params))

    def _new_memory_layer(self, name, fields):
        # The geometries are in the first input layer's coordinates.  Naming a
        # CRS the input never had would put the errors somewhere else on the
        # map, so an input with no CRS gives an error layer with no CRS.
        crs = getattr(self, '_target_crs', None)
        # An empty "crs=" is what leaves a memory layer unprojected; drop the
        # parameter entirely and QGIS assumes EPSG:4326.
        authid = crs.authid() if crs is not None and crs.isValid() else ''
        layer = QgsVectorLayer('MultiPolygon?crs={}'.format(authid), name, 'memory')
        if crs is not None and crs.isValid():
            layer.setCrs(crs)
        provider = layer.dataProvider()
        provider.addAttributes(fields)
        layer.updateFields()
        return layer

    def _build_overlap_layer(self, overlaps):
        fields = [
            make_field('error', T_STRING, 20),
            make_field('layer_a', T_STRING, 254),
            make_field('fid_a', T_LONGLONG),
            make_field('layer_b', T_STRING, 254),
            make_field('fid_b', T_LONGLONG),
            make_field('area', T_DOUBLE, 20, 6),
        ]
        layer = self._new_memory_layer(self.OVERLAP_LAYER_NAME, fields)

        features = []
        for item in overlaps:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(item['geom'])
            feat.setAttributes([
                'overlap', item['layer_a'], int(item['fid_a']),
                item['layer_b'], int(item['fid_b']), float(item['area']),
            ])
            features.append(feat)
        layer.dataProvider().addFeatures(features)
        layer.updateExtents()

        symbol = QgsFillSymbol.createSimple({
            'color': '255,128,128,255',
            'style': 'solid',
            'outline_color': '255,0,0,255',
            'outline_style': 'solid',
            'outline_width': '0.46',
            'outline_width_unit': 'MM',
            'joinstyle': 'bevel',
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setCustomProperty(self.PROP_KEY, 'overlap')
        layer.setCustomProperty('showFeatureCount', True)
        self._set_validation_metadata(layer)
        return layer

    def _build_gap_layer(self, gaps):
        fields = [
            make_field('error', T_STRING, 20),
            make_field('gap_id', T_LONGLONG),
            make_field('area', T_DOUBLE, 20, 6),
        ]
        layer = self._new_memory_layer(self.GAP_LAYER_NAME, fields)

        features = []
        for i, item in enumerate(gaps, start=1):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(item['geom'])
            feat.setAttributes(['gap', i, float(item['area'])])
            features.append(feat)
        layer.dataProvider().addFeatures(features)
        layer.updateExtents()

        symbol = QgsFillSymbol.createSimple({
            'style': 'no',                       # outline only - the gap stays see-through
            'outline_color': '255,128,128,255',
            'outline_style': 'solid',
            'outline_width': '0.66',
            'outline_width_unit': 'MM',
            'joinstyle': 'bevel',
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setCustomProperty(self.PROP_KEY, 'gap')
        layer.setCustomProperty('showFeatureCount', True)
        self._set_validation_metadata(layer)
        return layer

    # ------------------------------------------------------------------ #
    # User feedback
    # ------------------------------------------------------------------ #
    def _report(self, overlap_count, gap_count):
        parent = iface.mainWindow() if iface is not None else None
        title = self.tr('Topology Check')

        if not overlap_count and not gap_count:
            QMessageBox.information(
                parent, title,
                self.tr('No topology errors found.\n\nNo overlaps and no gaps were '
                        'detected in the input data.')
            )
            if iface is not None:
                iface.messageBar().pushSuccess(title, self.tr('No topology errors found.'))
            return

        parts = []
        if overlap_count:
            parts.append(self.tr('{} overlap(s)').format(overlap_count))
        if gap_count:
            parts.append(self.tr('{} gap(s)').format(gap_count))
        summary = self.tr(' and ').join(parts)

        QMessageBox.warning(
            parent, title,
            self.tr('{} found.\n\nThe error layer(s) have been added to the top of '
                    'the layer panel. Fix the geometry and run the check again.').format(summary)
        )
        if iface is not None:
            iface.messageBar().pushWarning(title, self.tr('{} found.').format(summary))

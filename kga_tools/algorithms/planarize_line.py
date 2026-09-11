from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (Qgis,
                       QgsFeature,
                       QgsFeatureRequest,
                       QgsProcessing,
                       QgsProcessingException,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterBoolean,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingUtils)
import processing

from ..core.compat import no_threading


def _provider_capability(name):
    """`Qgis.VectorProviderCapability.<name>`, or the pre-3.30 spelling."""
    holder = getattr(Qgis, 'VectorProviderCapability', None)
    if holder is not None and hasattr(holder, name):
        return getattr(holder, name)
    from qgis.core import QgsVectorDataProvider   # pragma: no cover - old builds
    return getattr(QgsVectorDataProvider, name)


def _no_geometry_flag():
    """The "don't fetch geometry" request flag, whichever enum holds it."""
    holder = getattr(Qgis, 'FeatureRequestFlag', None)
    if holder is not None and hasattr(holder, 'NoGeometry'):
        return holder.NoGeometry
    return QgsFeatureRequest.NoGeometry     # pragma: no cover - QGIS < 3.36


class PlanarizeLinesAlgorithm(QgsProcessingAlgorithm):
    """
    Splits intersecting lines at their intersections to create planarized single-part lines.
    """
    INPUT = 'INPUT'
    MODIFY_INPUT = 'MODIFY_INPUT'
    OUTPUT = 'OUTPUT'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return PlanarizeLinesAlgorithm()

    def name(self):
        return 'planarizelines'

    def displayName(self):
        return self.tr('Planarize Lines')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/planarizelines'

    def shortHelpString(self):
        return self.tr(
            "Splits lines where they intersect, creating planarized single-part "
            "lines with no cross-overs. Attributes from the original lines are "
            "retained.\n\n"
            "By default the result is written to a new layer and the input is "
            "left alone. Turn on <b>Planarize the input layer in place</b> to "
            "rewrite the input layer instead: its features are replaced by the "
            "planarized ones and no output layer is produced.\n\n"
            "In place needs a vector layer loaded in the project whose provider "
            "can add and delete features — not a file path or a read-only "
            "source. If the layer is already in edit mode the change is left in "
            "the edit buffer so you can undo it; otherwise it is committed. "
            "Feature ids are not preserved either way, because one input line "
            "becomes several output segments."
        )

    def flags(self):
        # The in-place branch writes into a layer loaded in the project, and
        # flags() is answered before the parameters are known.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Input Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MODIFY_INPUT,
                self.tr('Planarize the input layer in place (no output layer)'),
                defaultValue=False
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Planarized Output'),
                QgsProcessing.TypeVectorLine,
                optional=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))

        in_place = self.parameterAsBool(parameters, self.MODIFY_INPUT, context)
        destination = parameters.get(self.OUTPUT)
        if not in_place and destination in (None, ''):
            raise QgsProcessingException(self.tr(
                'Choose an output layer, or turn on "Planarize the input layer '
                'in place" to rewrite the input instead.'))

        layer = None
        if in_place:
            # Resolve and vet the target before doing any work, so a layer that
            # cannot be written to fails immediately rather than after the run.
            layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
            if layer is None:
                raise QgsProcessingException(self.tr(
                    'Planarizing in place needs a vector layer loaded in the '
                    'project. Pick a layer rather than a file, or turn the '
                    'option off and write to a new layer.'))
            capabilities = layer.dataProvider().capabilities()
            required = (_provider_capability('AddFeatures')
                        | _provider_capability('DeleteFeatures'))
            if not capabilities & required:
                raise QgsProcessingException(self.tr(
                    'The provider behind "{name}" cannot add and delete '
                    'features, so it cannot be planarized in place.').format(
                        name=layer.name()))

        # Step 1: Split lines at all intersections
        feedback.pushInfo("1/3: Splitting lines at intersections...")
        split_result = processing.run("native:splitwithlines", {
            'INPUT': parameters[self.INPUT],
            'LINES': parameters[self.INPUT],
            'OUTPUT': 'memory:'
        }, context=context, feedback=feedback, is_child_algorithm=True)

        if feedback.isCanceled():
            return {}

        # Step 2: Ensure all features are single-part
        feedback.pushInfo("2/3: Converting to single parts...")
        single_result = processing.run("native:multiparttosingleparts", {
            'INPUT': split_result['OUTPUT'],
            'OUTPUT': 'memory:'
        }, context=context, feedback=feedback, is_child_algorithm=True)

        if feedback.isCanceled():
            return {}

        # Step 3: Remove duplicate geometries created by overlapping segments
        feedback.pushInfo("3/3: Removing duplicate overlapping segments...")
        dedup_result = processing.run("native:deleteduplicategeometries", {
            'INPUT': single_result['OUTPUT'],
            'OUTPUT': 'memory:' if in_place else destination
        }, context=context, feedback=feedback, is_child_algorithm=True)

        if feedback.isCanceled():
            return {}

        if in_place:
            self._write_back(layer, source, dedup_result['OUTPUT'], context,
                             feedback)
            return {self.OUTPUT: layer.id()}

        return {self.OUTPUT: dedup_result['OUTPUT']}

    # ------------------------------------------------------------- in place --

    def _write_back(self, layer, source, result_id, context, feedback):
        """Replace the features the source covered with the planarized ones."""
        planarized = QgsProcessingUtils.mapLayerFromString(result_id, context)
        if planarized is None:
            raise QgsProcessingException(self.tr(
                'The planarized result could not be read back, so "{name}" was '
                'left unchanged.').format(name=layer.name()))

        # One input line becomes several segments, so its primary key cannot be
        # carried over: copying "fid" onto every segment would fail a GeoPackage
        # unique constraint at commit. Left null, the provider assigns new ones.
        fields = layer.fields()
        keys = {fields.at(index).name()
                for index in layer.dataProvider().pkAttributeIndexes()
                if 0 <= index < fields.count()}

        # Read everything out first: the features are built against the input
        # layer's fields, and nothing is deleted until they are all in hand.
        new_features = []
        for feature in planarized.getFeatures():
            replacement = QgsFeature(fields)
            replacement.setGeometry(feature.geometry())
            for field in fields:
                if field.name() in keys:
                    continue
                index = feature.fields().lookupField(field.name())
                if index >= 0:
                    replacement.setAttribute(field.name(),
                                             feature.attribute(index))
            new_features.append(replacement)

        # Only what the source covered, which is the selection when "selected
        # features only" is on — deleting the whole layer would be wrong there.
        request = QgsFeatureRequest().setNoAttributes()
        request.setFlags(_no_geometry_flag())
        old_ids = [feature.id() for feature in source.getFeatures(request)]

        feedback.pushInfo(
            'Replacing {old} feature(s) in "{name}" with {new} planarized '
            'one(s)...'.format(old=len(old_ids), name=layer.name(),
                               new=len(new_features)))

        started_here = not layer.isEditable()
        if started_here and not layer.startEditing():
            raise QgsProcessingException(self.tr(
                'Could not start editing "{name}".').format(name=layer.name()))

        layer.beginEditCommand('Planarize Lines')
        try:
            if old_ids and not layer.deleteFeatures(old_ids):
                raise QgsProcessingException(self.tr(
                    'Could not delete the original features from "{name}".'
                ).format(name=layer.name()))
            if new_features and not layer.addFeatures(new_features):
                raise QgsProcessingException(self.tr(
                    'Could not add the planarized features to "{name}".'
                ).format(name=layer.name()))
        except Exception:
            layer.destroyEditCommand()
            if started_here:
                layer.rollBack()
            raise
        layer.endEditCommand()

        if started_here:
            if not layer.commitChanges():
                errors = '; '.join(layer.commitErrors())
                layer.rollBack()
                raise QgsProcessingException(self.tr(
                    'Commit failed, so "{name}" is unchanged: {err}').format(
                        name=layer.name(), err=errors))
        else:
            feedback.pushInfo(
                'The layer was already in edit mode, so the change is in your '
                'edit buffer and not committed. Ctrl+Z undoes it.')

        layer.triggerRepaint()

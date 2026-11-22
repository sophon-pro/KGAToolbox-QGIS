# -*- coding: utf-8 -*-
"""Planarize Lines.

Splits lines where they cross, so no two lines run across each other without
a vertex at the crossing. Opens the same kind of window as the Modify Features
tools: pick the layer, say whether to change it or write a new one, press
Planarize.
"""
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QApplication,
                                 QCheckBox,
                                 QDialog,
                                 QFormLayout,
                                 QHBoxLayout,
                                 QLabel,
                                 QPushButton,
                                 QSizePolicy,
                                 QVBoxLayout,
                                 QWidget)
from qgis.core import (Qgis,
                       QgsFeature,
                       QgsFeatureRequest,
                       QgsProcessingAlgorithm,
                       QgsProcessingContext,
                       QgsProcessingException,
                       QgsProcessingFeatureSourceDefinition,
                       QgsProcessingFeedback,
                       QgsProcessingUtils,
                       QgsProject,
                       QgsVectorLayer)
from qgis.gui import QgsMapLayerComboBox
import processing

from ..gui.window_reveal import revealed_preview
from ..core.compat import no_threading
from ..core.modify_features import layer_filter
from ..gui.modify_dialog import close_dialog, show_dialog
from ..branding import docs_url, help_button

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None


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
    return QgsFeatureRequest.Flag.NoGeometry     # pragma: no cover - QGIS < 3.36


def plural(count, word):
    return '{} {}{}'.format(count, word, '' if count == 1 else 's')


# --------------------------------------------------------------------------- #
#  the work
# --------------------------------------------------------------------------- #

def planarize_layer(layer, selected_only, in_place, context, feedback):
    """Split `layer`'s lines at their intersections.

    Returns the planarized features as a memory layer, or None if cancelled.
    With `in_place` the layer's own features are replaced by them first.
    """
    if in_place:
        # Vet the target before doing any work, so a layer that cannot be
        # written to fails immediately rather than after the run.
        capabilities = layer.dataProvider().capabilities()
        required = (_provider_capability('AddFeatures')
                    | _provider_capability('DeleteFeatures'))
        if not capabilities & required:
            raise QgsProcessingException(
                'The provider behind "{}" cannot add and delete features, so '
                'it cannot be planarized in place.'.format(layer.name()))

    source = QgsProcessingFeatureSourceDefinition(layer.id(), selected_only)

    feedback.pushInfo('1/3: Splitting lines at intersections...')
    split_result = processing.run('native:splitwithlines', {
        'INPUT': source,
        'LINES': source,
        'OUTPUT': 'memory:'
    }, context=context, feedback=feedback, is_child_algorithm=True)
    if feedback.isCanceled():
        return None

    feedback.pushInfo('2/3: Converting to single parts...')
    single_result = processing.run('native:multiparttosingleparts', {
        'INPUT': split_result['OUTPUT'],
        'OUTPUT': 'memory:'
    }, context=context, feedback=feedback, is_child_algorithm=True)
    if feedback.isCanceled():
        return None

    feedback.pushInfo('3/3: Removing duplicate overlapping segments...')
    dedup_result = processing.run('native:deleteduplicategeometries', {
        'INPUT': single_result['OUTPUT'],
        'OUTPUT': 'memory:'
    }, context=context, feedback=feedback, is_child_algorithm=True)
    if feedback.isCanceled():
        return None

    result = QgsProcessingUtils.mapLayerFromString(
        dedup_result['OUTPUT'], context)
    if result is None:
        raise QgsProcessingException(
            'The planarized result could not be read back, so "{}" was left '
            'unchanged.'.format(layer.name()))
    if in_place:
        _write_back(layer, selected_only, result, feedback)
    return result


def _write_back(layer, selected_only, planarized, feedback):
    """Replace the features that were planarized with the planarized ones."""
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

    # Only what was planarized, which is the selection when "selected features
    # only" is on - deleting the whole layer would be wrong there.
    if selected_only:
        old_ids = list(layer.selectedFeatureIds())
    else:
        request = QgsFeatureRequest().setNoAttributes()
        request.setFlags(_no_geometry_flag())
        old_ids = [feature.id() for feature in layer.getFeatures(request)]

    feedback.pushInfo(
        'Replacing {} in "{}" with {} planarized.'.format(
            plural(len(old_ids), 'feature'), layer.name(), len(new_features)))

    started_here = not layer.isEditable()
    if started_here and not layer.startEditing():
        raise QgsProcessingException(
            'Could not start editing "{}".'.format(layer.name()))

    layer.beginEditCommand('Planarize Lines')
    try:
        if old_ids and not layer.deleteFeatures(old_ids):
            raise QgsProcessingException(
                'Could not delete the original features from "{}".'.format(
                    layer.name()))
        if new_features and not layer.addFeatures(new_features):
            raise QgsProcessingException(
                'Could not add the planarized features to "{}".'.format(
                    layer.name()))
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
            raise QgsProcessingException(
                'Commit failed, so "{}" is unchanged: {}'.format(
                    layer.name(), errors))
    else:
        feedback.pushInfo(
            'The layer was already in edit mode, so the change is in your '
            'edit buffer and not committed. Ctrl+Z undoes it.')

    layer.triggerRepaint()


# --------------------------------------------------------------------------- #
#  the window
# --------------------------------------------------------------------------- #

@revealed_preview
class PlanarizeDialog(QDialog):
    """The Planarize Lines window, laid out like the Modify Features tools."""

    TITLE = 'Planarize Lines'
    ALG_NAME = 'planarizelines'
    HINT = ('Choose the line layer, then press Planarize. Every place two '
            'lines cross or overlap becomes a split, so each segment runs '
            'from one junction to the next. Tick Selected features only to '
            'leave the rest alone, or Replace the features in this layer to '
            'rewrite it instead of making a new layer.')

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle(self.TITLE)
        self.setMinimumWidth(380)
        self._build_ui()
        self._connect()
        self._preselect_active_layer()
        self._refresh()

    # ------------------------------------------------------------------ ui --

    def _build_ui(self):
        outer = QVBoxLayout(self)

        top = QFormLayout()
        top.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(layer_filter('LineLayer'))
        top.addRow('Layer', self.layer_combo)
        outer.addLayout(top)

        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.selected_check = QCheckBox('Selected features only')
        self.selected_check.setToolTip(
            'Planarize only the lines selected on the layer. Lines that are '
            'not selected are neither split nor used to split the others. '
            'Available when the layer has a selection.')
        form.addRow('', self.selected_check)

        self.in_place_check = QCheckBox('Replace the features in this layer')
        self.in_place_check.setToolTip(
            'Off by default: the result goes into a new layer and the input '
            'is left alone. On, the layer\'s features are replaced by the '
            'planarized ones - one undo step if the layer is being edited, '
            'otherwise committed at once. Feature ids are not kept, because '
            'one line becomes several segments.')
        form.addRow('', self.in_place_check)

        heading = QLabel('How to use')
        bold = QFont(heading.font())
        bold.setBold(True)
        heading.setFont(bold)
        form.addRow(heading)

        hint = QLabel(self.HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #666;')
        hint.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Minimum)
        form.addRow(hint)

        self.verdict_label = QLabel('')
        self.verdict_label.setWordWrap(True)
        verdict_font = QFont(self.verdict_label.font())
        verdict_font.setBold(True)
        self.verdict_label.setFont(verdict_font)
        form.addRow(self.verdict_label)

        body.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Expanding)
        outer.addWidget(body, 1)

        buttons = QHBoxLayout()
        self.run_button = QPushButton('Planarize')
        self.run_button.setToolTip(
            'Split the lines of the chosen layer where they cross.')
        buttons.addWidget(self.run_button)
        buttons.addWidget(help_button(self.ALG_NAME, self))
        self.close_button = QPushButton('Close')
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('color: #666;')
        outer.addWidget(self.status_label)

    def _connect(self):
        self.layer_combo.layerChanged.connect(self._layer_changed)
        self.selected_check.toggled.connect(self._refresh)
        self.run_button.clicked.connect(self.run)
        self.close_button.clicked.connect(self.close)
        self._watched = None

    def _preselect_active_layer(self):
        active = self.iface.activeLayer()
        if (isinstance(active, QgsVectorLayer)
                and active.geometryType() == Qgis.GeometryType.Line):
            self.layer_combo.setLayer(active)
        self._layer_changed(self.layer_combo.currentLayer())

    def _layer_changed(self, layer):
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._refresh)
            except (TypeError, RuntimeError):
                pass
        self._watched = layer
        if layer is not None:
            layer.selectionChanged.connect(self._refresh)
        self._refresh()

    # --------------------------------------------------------------- state --

    def layer(self):
        return self.layer_combo.currentLayer()

    def _refresh(self, *_args):
        layer = self.layer()
        picked = layer.selectedFeatureCount() if layer is not None else 0
        self.selected_check.setEnabled(picked > 0)
        if picked == 0:
            self.selected_check.setChecked(False)

        if layer is None:
            self.verdict_label.setText('Choose a line layer.')
        elif self.selected_check.isChecked():
            self.verdict_label.setText(
                '{} selected to planarize.'.format(plural(picked, 'line')))
        else:
            self.verdict_label.setText(
                '{} to planarize.'.format(
                    plural(layer.featureCount(), 'line')))
        self.run_button.setEnabled(layer is not None)

    def say(self, text):
        """Show `text` under the buttons, growing the window to fit it."""
        self.status_label.setText(text)
        if self.isVisible():
            self.layout().activate()
            width = self.width()
            needed = self.layout().totalHeightForWidth(width)
            if needed > self.height():
                self.resize(width, needed)

    # ----------------------------------------------------------------- run --

    def run(self):
        layer = self.layer()
        if layer is None:
            self.say('Choose a line layer first.')
            return
        selected_only = self.selected_check.isChecked()
        in_place = self.in_place_check.isChecked()

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = planarize_layer(layer, selected_only, in_place, context,
                                     feedback)
        except QgsProcessingException as exc:
            self.say(str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        if result is None:
            self.say('Cancelled. Nothing was changed.')
            return

        count = result.featureCount()
        if in_place:
            self.say('{} now in "{}".'.format(
                plural(count, 'segment'), layer.name()))
        else:
            result.setName('{} planarized'.format(layer.name()))
            result.setCrs(layer.crs())
            QgsProject.instance().addMapLayer(result)
            self.say('{} written to the new layer "{}".'.format(
                plural(count, 'segment'), result.name()))
        self._refresh()


def close_planarize_lines():
    """Close the window. Called by `KgaToolsPlugin.unload`."""
    close_dialog(PlanarizeDialog)


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

class PlanarizeLinesAlgorithm(QgsProcessingAlgorithm):
    """Show the Planarize Lines window."""

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return PlanarizeLinesAlgorithm()

    def name(self):
        return 'planarizelines'

    def displayName(self):
        return self.tr('Planarize Lines')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('planarizelines')

    def shortHelpString(self):
        return self.tr(
            "Splits lines where they intersect, creating planarized single-part "
            "lines with no cross-overs. Attributes from the original lines are "
            "retained.\n\n"
            "By default the result is written to a new layer and the input is "
            "left alone. Tick <b>Replace the features in this layer</b> to "
            "rewrite the input layer instead: its features are replaced by the "
            "planarized ones and no new layer is made.\n\n"
            "Tick <b>Selected features only</b> to planarize just the selected "
            "lines; it is available when the layer has a selection.\n\n"
            "Replacing needs a vector layer loaded in the project whose "
            "provider can add and delete features. If the layer is already in "
            "edit mode the change is left in the edit buffer so you can undo "
            "it; otherwise it is committed. Feature ids are not preserved "
            "either way, because one input line becomes several output "
            "segments.\n\n"
            "The window stays open while you work - close this Processing "
            "window once it appears."
        )

    def flags(self):
        # Opens a window and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Planarize Lines is an interactive tool and needs the QGIS '
                'window, so it cannot run from a head-less Processing '
                'session.'))
        show_dialog(iface, PlanarizeDialog)
        feedback.pushInfo(self.tr(
            'Planarize Lines opened. You can close this Processing dialog '
            'and keep working on the map.'))
        return {}

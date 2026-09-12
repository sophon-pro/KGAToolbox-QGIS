# -*- coding: utf-8 -*-

from contextlib import suppress

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTableWidget,
    QTableWidgetItem,
)

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsProcessingAlgorithm,
    QgsProcessingException,
)

from ..core import schema
from ..core.compat import no_threading
from ..core.modify_features import fit_to_layer, layer_filter, merge_geometries
from ..gui.modify_dialog import (
    ModifyFeaturesDialog,
    close_dialog,
    show_dialog,
)
from ..branding import docs_url

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None

#: Beyond this many selected features the per-field table stops being a table
#: anyone can read, so it is filled from the preserved feature and left alone.
TABLE_LIMIT = 25


def label_for(layer, feature):
    """What a feature is called in the list: its display expression, then its id."""
    expression = layer.displayExpression()
    if expression:
        with suppress(Exception):           # pragma: no cover
            context = QgsExpressionContext()
            context.appendScopes(
                QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            context.setFeature(feature)
            value = QgsExpression(expression).evaluate(context)
            if value not in (None, ''):
                return '{} [{}]'.format(value, feature.id())
    return 'Feature {}'.format(feature.id())


def value_label(value):
    if schema.is_null(value):
        return '<NULL>'
    text = str(value)
    return text if len(text) <= 60 else text[:57] + '...'


class MergeDialog(ModifyFeaturesDialog):
    """The Merge window.

    The one tool here that needs more than one feature at a time, so it is the
    one that collects: press Merge, click each feature to take in - clicking it
    again puts it back - and press Enter. Dragging a path across them all does
    it in one gesture. Everything in hand is outlined on the map, and the list
    below fills as you click, so what will be merged is never a guess.
    """

    ALG_NAME = 'merge_features'
    TITLE = 'Merge'
    ACTION_LABEL = 'Merge'
    HINT = ('Press Merge, then click the features to combine - Enter merges '
            'them, Esc starts over, and dragging a path across them does it '
            'in one go. Pick which one keeps its attributes below.')

    LAYER_FILTER = layer_filter('VectorLayer')
    MIN_FEATURES = 2

    def __init__(self, iface, parent=None):
        self._features = []
        self._combos = {}
        super().__init__(iface, parent)

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        heading = QLabel('Preserve the attributes of')
        bold = QFont(heading.font())
        bold.setBold(True)
        heading.setFont(bold)
        form.addRow(heading)

        self.feature_list = QListWidget()
        self.feature_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.feature_list.setMaximumHeight(140)
        self.feature_list.setToolTip(
            'The features clicked so far. The highlighted one is where the '
            'merged feature keeps its identity and its starting values from; '
            'clicking an entry flashes it on the map.')
        form.addRow(self.feature_list)

        self.field_table = QTableWidget(0, 2)
        self.field_table.setHorizontalHeaderLabels(['Field', 'Value'])
        self.field_table.verticalHeader().setVisible(False)
        self.field_table.horizontalHeader().setStretchLastSection(True)
        self.field_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.field_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.field_table.setToolTip(
            'Each field, with every distinct value the features in hand hold. '
            'Pick one to override what the preserved feature had.')
        form.addRow(self.field_table)

        self.feature_list.currentRowChanged.connect(self._primary_changed)

    # -------------------------------------------------------------- tables --

    def on_held_changed(self, features):
        """The set in hand changed: relist it and rebuild the field table."""
        self._features = list(features)
        self._rebuild()

    def on_layer_changed(self, layer):
        self._features = []
        self._rebuild()

    def _rebuild(self):
        layer = self.current_layer()

        self.feature_list.blockSignals(True)
        self.feature_list.clear()
        for feature in self._features:
            self.feature_list.addItem(QListWidgetItem(label_for(layer, feature)))
        if self._features:
            self.feature_list.setCurrentRow(0)
        self.feature_list.blockSignals(False)

        self._mark_primary()
        self._build_field_table()

    def _mark_primary(self):
        """Say in the list itself which feature is the one being preserved."""
        current = self.feature_list.currentRow()
        layer = self.current_layer()
        for row, feature in enumerate(self._features):
            item = self.feature_list.item(row)
            if item is None:
                continue
            text = label_for(layer, feature)
            item.setText(text + ('  (preserved)' if row == current else ''))

    def _primary_changed(self, row):
        self._mark_primary()
        self._build_field_table()
        feature = self.primary_feature()
        layer = self.current_layer()
        if feature is not None and layer is not None:
            with suppress(Exception):       # pragma: no cover - old build
                self.canvas.flashFeatureIds(layer, [feature.id()])

    def primary_feature(self):
        row = self.feature_list.currentRow()
        if 0 <= row < len(self._features):
            return self._features[row]
        return self._features[0] if self._features else None

    def _build_field_table(self):
        self._combos = {}
        self.field_table.setRowCount(0)

        layer = self.current_layer()
        primary = self.primary_feature()
        if layer is None or primary is None:
            return
        if len(self._features) > TABLE_LIMIT:
            self.report(
                '{} features selected: the merged feature takes the preserved '
                'feature\'s values as they are.'.format(len(self._features)))
            return

        fields = layer.fields()
        try:
            owned = set(layer.primaryKeyAttributes() or [])
        except Exception:                   # pragma: no cover
            owned = set()

        rows = [index for index in range(fields.count()) if index not in owned]
        self.field_table.setRowCount(len(rows))

        for row, index in enumerate(rows):
            field = fields.at(index)
            name = QTableWidgetItem(field.name())
            name.setToolTip('{} ({})'.format(field.name(), field.typeName()))
            self.field_table.setItem(row, 0, name)

            combo = QComboBox()
            # One entry per distinct value, so merging twenty parcels that
            # share a district code offers that code once, not twenty times.
            seen = []
            for feature in self._features:
                value = feature.attribute(index)
                key = value_label(value)
                if key not in [text for text, _v in seen]:
                    seen.append((key, value))
            for text, value in seen:
                combo.addItem(text, value)

            primary_label = value_label(primary.attribute(index))
            position = combo.findText(primary_label)
            combo.setCurrentIndex(max(0, position))
            combo.setEnabled(combo.count() > 1)
            if combo.count() > 1:
                combo.setToolTip('{} different values in the selection.'.format(
                    combo.count()))
            self.field_table.setCellWidget(row, 1, combo)
            self._combos[index] = combo

    # ------------------------------------------------------------- preview --

    def result_for(self, feature):
        """Hovering shows what this feature would merge with what is in hand.

        With nothing in hand yet there is nothing to show: one feature merged
        with itself is the shape it already has.
        """
        if not self._features:
            return []
        geometries = [f.geometry() for f in self._features
                      if f.id() != feature.id()]
        if not geometries:
            return []
        merged = merge_geometries(geometries + [feature.geometry()])
        return [] if merged is None else [merged]

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        # A drag hands its features straight here without going through the
        # list, so make the table agree with what is about to be merged.
        if [f.id() for f in features] != [f.id() for f in self._features]:
            self._features = list(features)
            self._rebuild()

        primary = self.primary_feature()
        merged = merge_geometries([f.geometry() for f in features])
        if merged is None:
            return 'Those features could not be combined.'

        parts = fit_to_layer(merged, layer)
        if not parts:
            return 'The merged geometry does not fit "{}".'.format(layer.name())
        if len(parts) > 1:
            # A multipart result into a single-part layer would have to become
            # several features, which is the opposite of a merge.
            return ('They do not touch, and "{}" is a single-part layer, so '
                    'they cannot become one feature.'.format(layer.name()))

        values = {index: combo.currentData()
                  for index, combo in self._combos.items()}
        doomed = [f.id() for f in features if f.id() != primary.id()]
        # Read the count out now: the delete and the reselect below both change
        # what the dialog is holding, and the status line would otherwise
        # report "1 features merged" whatever it had just done.
        merged_count = len(features)

        layer.beginEditCommand('KGA Merge')
        try:
            layer.changeGeometry(primary.id(), parts[0])
            for index, value in values.items():
                if not schema.is_null(value) or not schema.is_null(
                        primary.attribute(index)):
                    layer.changeAttributeValue(primary.id(), index, value)
            for fid in doomed:
                layer.deleteFeature(fid)
        except Exception:
            layer.destroyEditCommand()
            raise
        layer.endEditCommand()

        layer.selectByIds([primary.id()])
        layer.triggerRepaint()
        self._features = []
        self._rebuild()
        self.count(merged_count)
        return '{} feature{} merged into feature {}.'.format(
            merged_count, '' if merged_count == 1 else 's', primary.id())


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_merge_features():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(MergeDialog)


class MergeFeaturesAlgorithm(QgsProcessingAlgorithm):
    """Show the Merge pane."""

    def tr(self, string):
        return QCoreApplication.translate('KgaMergeFeatures', string)

    def createInstance(self):
        return MergeFeaturesAlgorithm()

    def name(self):
        return 'merge_features'

    def displayName(self):
        return self.tr('Merge')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('merge_features')

    def shortHelpString(self):
        return self.tr(
            'Combine two or more selected features of one layer into a single '
            'feature, the way ArcGIS Pro\'s <i>Modify Features > Merge</i> '
            'does.\n\n'
            'Features that touch dissolve into one; features that do not stay '
            'on as a multipart feature.\n\n'
            'The list shows the selection. The highlighted entry is marked '
            '<b>(preserved)</b>: the merged feature keeps that feature\'s '
            'identity and starts from its attribute values, and clicking an '
            'entry flashes it on the map. The table below lists every field '
            'with each distinct value the selected features hold, so any one '
            'field can be taken from any one of them.\n\n'
            'Keeping the preserved feature\'s id rather than making a new '
            'feature means joins and relates still point at something '
            'afterwards.\n\n'
            'One press is one undo step. The pane stays open while you work — '
            'close this Processing window once it appears.'
        )

    def flags(self):
        # Opens a dock and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Merge is an interactive tool and needs the QGIS map canvas, '
                'so it cannot run from a head-less Processing session.'))
        show_dialog(iface, MergeDialog)
        feedback.pushInfo(self.tr(
            'Merge opened. You can close this Processing dialog and keep '
            'working on the map.'))
        return {}

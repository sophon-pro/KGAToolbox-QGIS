# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QCheckBox, QSpinBox

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
)

from ..core.compat import no_threading
from ..core.modify_features import (
    CORNER_STYLES,
    SIDE_BOTH,
    SIDE_LABELS,
    copy_parallel,
    fit_to_layer,
    layer_filter,
)
from ..gui.modify_base import carry_attributes, make_feature
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


class CopyParallelDialog(ModifyFeaturesDialog):
    """The Copy Parallel window."""

    ALG_NAME = 'copy_parallel'
    TITLE = 'Copy Parallel'
    ACTION_LABEL = 'Copy Parallel'
    HINT = ('Set the distance, press Copy Parallel, then hover a line to see '
            'the copies and click to write them. Drag a path across several '
            'lines to copy them all. Left and right are relative to the '
            'direction each line was drawn in.')

    LAYER_FILTER = layer_filter('LineLayer')
    ACCEPTS = ('Line',)
    EDITS_SOURCE = False                    # the template layer is written

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        self.template_combo = self.add_template_combo(
            'Template', layer_filter('LineLayer'),
            'Where the copies are created. Defaults to the source layer, '
            'which is what Copy Parallel does in ArcGIS.')

        self.distance_spin, self.unit_combo = self.add_distance_row(
            'Distance', value=10.0, minimum=0.0)

        self.side_combo = self.add_choice('Side', SIDE_LABELS, SIDE_BOTH)

        self.corner_combo = self.add_unit_combo('Corners', CORNER_STYLES,
                                                'Miter')
        self.corner_combo.setToolTip(
            'How the copy turns a corner. Mitered keeps the corner sharp, '
            'beveled cuts it off, rounded arcs it.')

        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 100)
        self.copies_spin.setValue(1)
        self.copies_spin.setToolTip(
            'More than one copy steps outwards: the second sits at twice the '
            'distance, the third at three times it.')
        form.addRow('Number of copies', self.copies_spin)

        self.attributes_check = QCheckBox('Copy the source attributes')
        self.attributes_check.setChecked(True)
        form.addRow('', self.attributes_check)

    def on_layer_changed(self, layer):
        self.follow_source(layer)

    def blocker(self):
        if self.target_layer() is None:
            return 'Choose a template layer for the copies.'
        if self.distance_spin.value() <= 0:
            return 'Set a distance greater than zero.'
        return ''

    # ------------------------------------------------------------- offsets --

    def offsets_for(self, feature, layer):
        """Every parallel copy of one feature, in layer CRS."""
        distance = self.distance_spin.value()
        geometry = feature.geometry()
        if distance <= 0 or geometry is None or geometry.isNull() \
                or geometry.isEmpty():
            return []

        workspace = self.make_workspace(layer, self.unit_combo.currentData())
        try:
            working = workspace.to_work(geometry)
        except ValueError:
            return []

        results = []
        for parallel in copy_parallel(working,
                                      workspace.distance(distance),
                                      self.side_combo.currentIndex(),
                                      self.copies_spin.value(),
                                      corner=self.corner_combo.currentData()):
            try:
                results.append(workspace.from_work(parallel))
            except ValueError:              # pragma: no cover
                continue
        return results

    def result_for(self, feature):
        layer = self.current_layer()
        return [] if layer is None else self.offsets_for(feature, layer)

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        target = self.target_layer()
        carry = self.attributes_check.isChecked()
        added = 0
        dropped = 0
        skipped = 0

        target.beginEditCommand('KGA Copy Parallel')
        try:
            for feature in features:
                geometries = self.offsets_for(feature, layer)
                if not geometries:
                    # A line whose bends are tighter than the offset collapses
                    # instead of copying. Counted, because it used to be
                    # indistinguishable from "nothing happened".
                    skipped += 1
                    continue
                values = (carry_attributes(feature, layer, target)
                          if carry else {})
                for geometry in geometries:
                    parts = fit_to_layer(geometry, target)
                    if not parts:
                        dropped += 1
                        continue
                    for part in parts:
                        if target.addFeature(
                                make_feature(target, part, values)):
                            added += 1
                        else:
                            dropped += 1
        except Exception:
            target.destroyEditCommand()
            raise
        target.endEditCommand()

        if added:
            target.triggerRepaint()
        self.count(added)

        notes = []
        if dropped:
            notes.append('{} could not be created'.format(dropped))
        if skipped:
            notes.append('{} line{} collapsed at that distance'.format(
                skipped, '' if skipped == 1 else 's'))
        note = '' if not notes else ' ({}.)'.format('; '.join(notes))
        return '{} parallel cop{} from {} line{} added to "{}".{}'.format(
            added, 'y' if added == 1 else 'ies',
            len(features), '' if len(features) == 1 else 's',
            target.name(), note)


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_copy_parallel():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(CopyParallelDialog)


class CopyParallelAlgorithm(QgsProcessingAlgorithm):
    """Show the Copy Parallel dialog."""

    def tr(self, string):
        return QCoreApplication.translate('KgaCopyParallel', string)

    def createInstance(self):
        return CopyParallelAlgorithm()

    def name(self):
        return 'copy_parallel'

    def displayName(self):
        return self.tr('Copy Parallel')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('copy_parallel')

    def shortHelpString(self):
        return self.tr(
            'Copy line features to one side or the other, the way ArcGIS '
            'Pro\'s <i>Modify Features > Copy Parallel</i> does.\n\n'
            'Set the <b>Distance</b> and its unit, choose the <b>Side</b> and '
            'the <b>Corners</b>, then press <b>Copy Parallel</b> and work on '
            'the map:\n\n'
            '• hover a line — its copies are drawn in cyan\n'
            '• click it — they are written\n'
            '• press, drag a path across several lines, release — every line '
            'the path crosses is copied\n\n'
            '<b>Left</b> and <b>Right</b> are relative to the direction each '
            'line was digitized in, as in ArcGIS. <b>Number of copies</b> '
            'steps outwards: the second copy sits at twice the distance.\n\n'
            'The copies go to the <b>Template</b> layer, which starts as the '
            'source layer; sending them elsewhere matches the attributes by '
            'field name. On a layer stored in degrees the offset is computed '
            'in the matching UTM zone, so a distance in meters means meters.\n\n'
            'Each click or drag is one undo step. The dialog stays open while '
            'you work — close this Processing window once it appears.'
        )

    def flags(self):
        # Opens a dialog and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Copy Parallel is an interactive tool and needs the QGIS map '
                'canvas, so it cannot run from a head-less Processing session.'))
        show_dialog(iface, CopyParallelDialog)
        feedback.pushInfo(self.tr(
            'Copy Parallel opened. You can close this Processing dialog and '
            'keep working on the map.'))
        return {}

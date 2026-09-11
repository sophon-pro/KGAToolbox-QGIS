# -*- coding: utf-8 -*-
"""
Buffer (interactive)
====================

Replica of the ArcGIS Pro *Modify Features > Buffer* pane.

Select features of any geometry type, type a distance, and buffer polygons
appear around them in a polygon layer of your choosing. A well becomes its
protection zone; a canal becomes its right of way; a village point becomes its
service area.

Press **Buffer**, then hover a feature to see its buffer in cyan and click to
write it; drag a path across several to buffer them all.

Pro's pane is short - a template, a distance and a unit - and so is the top of
this one. The end-cap and corner controls below it are QGIS's own and are what
decide whether a buffered canal ends in a semicircle or squarely at the last
vertex; they are on the pane rather than buried because a right of way that
overshoots its channel by the buffer radius is a real error, not a cosmetic one.

Distances are honest on a layer stored in degrees: the buffer is computed in
the matching UTM zone and brought back, so a 30 m setback is 30 m at the
equator and 30 m in the north.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QCheckBox, QSpinBox

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
)

from ..core.compat import no_threading
from ..core.modify_features import (
    BUFFER_FULL,
    BUFFER_SIDE_LABELS,
    CAP_STYLES,
    CORNER_STYLES,
    buffer_geometry,
    dissolve,
    fit_to_layer,
    geometry_type,
    layer_filter,
)
from ..gui.modify_base import carry_attributes, make_feature
from ..gui.modify_dialog import (
    ModifyFeaturesDialog,
    close_dialog,
    show_dialog,
)

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None


class BufferDialog(ModifyFeaturesDialog):
    """The Buffer window."""

    TITLE = 'Buffer'
    ACTION_LABEL = 'Buffer'
    HINT = ('Set the distance and the polygon layer the buffers go in, press '
            'Buffer, then hover a feature to see its buffer and click to write '
            'it. Drag a path across several to buffer them all.')

    LAYER_FILTER = layer_filter('VectorLayer')
    EDITS_SOURCE = False                    # the template layer is written
    # A buffer contains the feature it came from, so when the buffers go into
    # the layer they are read from - which is the default for a polygon layer -
    # the last one written sits under the cursor that wrote it. Without this
    # the next hover buffered that buffer, and the click after it buffered
    # *that*: one ring wider every time, for as long as the user kept clicking.
    SKIPS_OWN_OUTPUT = True

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        self.template_combo = self.add_template_combo(
            'Template', layer_filter('PolygonLayer'),
            'The polygon layer the buffers are created in. ArcGIS calls this '
            'the template.')

        self.distance_spin, self.unit_combo = self.add_distance_row(
            'Distance', value=10.0, minimum=0.0)

        self.side_combo = self.add_choice('Side', BUFFER_SIDE_LABELS,
                                          BUFFER_FULL)
        self.side_combo.setToolTip(
            'One-sided buffers only mean anything for a line; a point or a '
            'polygon is always buffered all round.')

        self.cap_combo = self.add_unit_combo('End caps', CAP_STYLES, 'Round')
        self.cap_combo.setToolTip(
            'How a buffered line ends. Round overshoots the last vertex by the '
            'buffer distance; flat stops on it.')

        self.corner_combo = self.add_unit_combo('Corners', CORNER_STYLES,
                                                'Round')

        self.segments_spin = QSpinBox()
        self.segments_spin.setRange(1, 64)
        self.segments_spin.setValue(8)
        self.segments_spin.setToolTip(
            'Straight segments used to draw a quarter circle. Higher is '
            'smoother and heavier.')
        form.addRow('Segments per quarter', self.segments_spin)

        self.dissolve_check = QCheckBox('Combine the buffers into one feature')
        self.dissolve_check.setToolTip(
            'Only means anything when a drag catches several features at '
            'once: their buffers are merged, so a chain gives one corridor '
            'rather than a stack of overlapping polygons.')
        form.addRow('', self.dissolve_check)

        self.attributes_check = QCheckBox('Copy the source attributes')
        self.attributes_check.setChecked(True)
        form.addRow('', self.attributes_check)

    def on_layer_changed(self, layer):
        # Buffering a polygon layer into itself is a legitimate thing to want,
        # so offer the source as the template when it is a polygon layer.
        if layer is not None and layer.geometryType() == geometry_type('Polygon'):
            self.follow_source(layer)

    def target_layer(self):
        # No falling back to the source: buffers are polygons and the source
        # may well be lines or points.
        return self.template_combo.currentLayer()

    def blocker(self):
        if self.target_layer() is None:
            return 'Choose a polygon layer for the buffers to go in.'
        if self.distance_spin.value() <= 0:
            return 'Set a distance greater than zero.'
        return ''

    # ------------------------------------------------------------- buffers --

    def buffer_for(self, feature, layer):
        """The buffer of one feature, in layer CRS, or None."""
        distance = self.distance_spin.value()
        geometry = feature.geometry()
        if distance <= 0 or geometry is None or geometry.isNull() \
                or geometry.isEmpty():
            return None

        workspace = self.make_workspace(layer, self.unit_combo.currentData())
        try:
            working = workspace.to_work(geometry)
        except ValueError:
            return None
        buffered = buffer_geometry(working, workspace.distance(distance),
                                   self.segments_spin.value(),
                                   self.cap_combo.currentData(),
                                   self.corner_combo.currentData(),
                                   side=self.side_combo.currentIndex())
        if buffered is None:
            return None
        try:
            return workspace.from_work(buffered)
        except ValueError:                  # pragma: no cover
            return None

    def result_for(self, feature):
        layer = self.current_layer()
        if layer is None:
            return []
        buffered = self.buffer_for(feature, layer)
        return [] if buffered is None else [buffered]

    def _pairs_for(self, features, layer):
        """`(source feature, buffer)` for each, combined when asked for.

        A combined run collapses to one pair whose source feature is the first
        one - which is where its attributes come from, the same rule ArcGIS
        uses for a dissolved buffer.
        """
        pairs = []
        for feature in features:
            buffered = self.buffer_for(feature, layer)
            if buffered is not None:
                pairs.append((feature, buffered))
        if self.dissolve_check.isChecked() and len(pairs) > 1:
            merged = dissolve([geometry for _feature, geometry in pairs])
            if merged is not None:
                pairs = [(pairs[0][0], merged)]
        return pairs

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        target = self.target_layer()
        pairs = self._pairs_for(features, layer)
        if not pairs:
            return 'None of the {} feature{} could be buffered by that ' \
                   'distance.'.format(len(features),
                                      '' if len(features) == 1 else 's')

        carry = self.attributes_check.isChecked()
        added = 0
        dropped = 0

        target.beginEditCommand('KGA Buffer')
        try:
            for feature, geometry in pairs:
                values = (carry_attributes(feature, layer, target)
                          if carry else {})
                parts = fit_to_layer(geometry, target)
                if not parts:
                    dropped += 1
                    continue
                for part in parts:
                    if target.addFeature(make_feature(target, part, values)):
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
        if self.dissolve_check.isChecked() and len(pairs) == 1 and added > 1:
            # The buffers were combined, and the combination still came out as
            # several features - which only happens when they do not all touch
            # and the target cannot hold a multipart one. Saying so beats
            # leaving the tick box looking like it did nothing.
            notes.append(
                'they do not all overlap and "{}" is a single-part layer, so '
                'the combined buffer had to be split'.format(target.name()))
        note = '' if not notes else ' ({}.)'.format('; '.join(notes))
        return '{} buffer{} from {} feature{} added to "{}".{}'.format(
            added, '' if added == 1 else 's',
            len(features), '' if len(features) == 1 else 's',
            target.name(), note)


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_buffer_features():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(BufferDialog)


class BufferFeaturesAlgorithm(QgsProcessingAlgorithm):
    """Show the Buffer pane."""

    def tr(self, string):
        return QCoreApplication.translate('KgaBufferFeatures', string)

    def createInstance(self):
        return BufferFeaturesAlgorithm()

    def name(self):
        return 'buffer_features'

    def displayName(self):
        return self.tr('Buffer')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/buffer_features'

    def shortHelpString(self):
        return self.tr(
            'Buffer the selected features into a polygon layer, the way '
            'ArcGIS Pro\'s <i>Modify Features > Buffer</i> does.\n\n'
            'Select the features, choose the <b>Template</b> - the polygon '
            'layer the buffers are created in - and set the <b>Distance</b> '
            'and its unit. The result is drawn in cyan before anything is '
            'written.\n\n'
            '<b>End caps</b> decide whether a buffered line finishes in a '
            'semicircle or squarely on its last vertex, and <b>Corners</b> how '
            'it turns. <b>Combine the buffers into one feature</b> merges what '
            'overlaps, so a chain of features gives one corridor.\n\n'
            'On a layer stored in degrees the buffer is computed in the '
            'matching UTM zone, so a distance in meters means meters.\n\n'
            'One press is one undo step. The pane stays open while you work - '
            'close this Processing window once it appears.'
        )

    def flags(self):
        # Opens a dialog and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Buffer is an interactive tool and needs the QGIS map canvas, '
                'so it cannot run from a head-less Processing session.'))
        show_dialog(iface, BufferDialog)
        feedback.pushInfo(self.tr(
            'Buffer opened. You can close this Processing dialog and keep '
            'working on the map.'))
        return {}

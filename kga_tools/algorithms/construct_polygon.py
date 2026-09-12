# -*- coding: utf-8 -*-
from contextlib import suppress

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import QCheckBox, QLabel

from qgis.core import (
    QgsGeometry,
    QgsProcessingAlgorithm,
    QgsProcessingException,
)

from ..core.compat import no_threading
from ..core.modify_features import (
    construct_polygons,
    dissolve,
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

#: Loose ends listed by coordinate in the message. Beyond this the list is
#: longer than the status line and the flash on the map is the better answer.
NAMED_ENDS = 3


def plural(count, word, ending='s'):
    return '{} {}{}'.format(count, word, '' if count == 1 else ending)


class ConstructPolygonDialog(ModifyFeaturesDialog):
    """The Construct Polygon window."""

    ALG_NAME = 'construct_polygon'
    TITLE = 'Construct Polygon'
    ACTION_LABEL = 'Construct'
    HINT = ('Choose the polygon layer the result goes in, press Construct, '
            'then click each line of the boundary and press Enter. Dragging a '
            'path across them does it in one go, and lines already selected '
            'are in hand as soon as the tool is switched on.')

    LAYER_FILTER = layer_filter('LineLayer')
    ACCEPTS = ('Line',)
    EDITS_SOURCE = False                    # the lines are read, not changed
    # One closed line is a boundary on its own, so this collects without
    # needing two: click the courses, press Enter.
    COLLECTS = True
    MIN_FEATURES = 1

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        self.template_combo = self.add_template_combo(
            'Template', layer_filter('PolygonLayer'),
            'The polygon layer the constructed polygons are created in. '
            'ArcGIS calls this the template.')

        self.tolerance_spin, self.unit_combo = self.add_distance_row(
            'Tolerance', value=0.0, minimum=0.0)
        self.tolerance_spin.setToolTip(
            'How far apart two line ends may be and still count as meeting. '
            'Zero demands that they touch exactly, which is what a boundary '
            'built by snapping does. Raise it to close the hairline gaps a '
            'survey import leaves behind - and no further, or corners that '
            'are genuinely apart are pulled together.')

        self.combine_check = QCheckBox('Combine the polygons into one feature')
        self.combine_check.setToolTip(
            'Only means anything when the lines enclose more than one area - '
            'a block of parcels sharing walls, say. Ticked, they become one '
            'feature; unticked, each enclosed area is a feature of its own, '
            'which is what ArcGIS does.')
        form.addRow('', self.combine_check)

        self.attributes_check = QCheckBox('Copy the attributes of the first line')
        self.attributes_check.setToolTip(
            'Off by default, as in ArcGIS: a boundary is made of several '
            'courses and no one of them owns the parcel, so the new polygon '
            'takes the template layer\'s own defaults. Tick it to carry the '
            'values of the first line clicked across by field name.')
        form.addRow('', self.attributes_check)

        verdict = QLabel('')
        verdict.setWordWrap(True)
        bold = QFont(verdict.font())
        bold.setBold(True)
        verdict.setFont(bold)
        verdict.setToolTip(
            'Whether the lines in hand close, worked out as you click them.')
        form.addRow(verdict)
        self.verdict_label = verdict
        self._say_verdict([])

        # The tolerance is the one setting that changes the answer without
        # anything being clicked, so the verdict has to follow it: raise it
        # over the gap and the line goes green there and then, which is how
        # you find the tolerance a boundary actually needs.
        self.tolerance_spin.valueChanged.connect(self._retest)
        self.unit_combo.currentIndexChanged.connect(self._retest)

    def _retest(self, *_args):
        self._say_verdict(self._held)

    def target_layer(self):
        # No falling back to the source: the result is a polygon and the
        # source is always a line layer.
        return self.template_combo.currentLayer()

    def blocker(self):
        if self.target_layer() is None:
            return 'Choose a polygon layer for the constructed polygons to go in.'
        return ''

    # -------------------------------------------------------- the verdict --

    def on_activated(self):
        """Take the layer's existing selection in hand.

        ArcGIS constructs polygons from what is selected, and a boundary is
        often selected already - by an attribute query, by a rubber band with
        the QGIS select tool, or because it was just digitized. Starting from
        nothing would mean clicking every course a second time.
        """
        layer = self.current_layer()
        if layer is None or self._held:
            return
        selected = list(layer.selectedFeatures())
        if selected:
            self.hold(selected)

    def on_layer_changed(self, layer):
        self._say_verdict([])

    def on_held_changed(self, features):
        self._say_verdict(features)

    def _say_verdict(self, features):
        """Say, above the buttons, whether what is in hand closes.

        The one thing this tool refuses on is the one thing a user cannot see
        by looking at the map - a gap of a millimetre draws the same as no gap
        at all - so it is answered as the courses are clicked rather than when
        Enter is pressed.
        """
        label = getattr(self, 'verdict_label', None)
        if label is None:                   # pragma: no cover - still building
            return
        if not features:
            label.setText('Nothing in hand.')
            label.setStyleSheet('color: #666;')
            return

        polygons, loose = self.build_from(features)
        count = len(features)
        if polygons:
            label.setStyleSheet('color: #1a7f37;')
            note = ('' if not loose else
                    ', and {} that closes nothing'.format(
                        plural(len(loose), 'loose end')))
            label.setText('{} in hand: {} to construct{}.'.format(
                plural(count, 'line'), plural(len(polygons), 'polygon'), note))
        else:
            label.setStyleSheet('color: #b3261e;')
            label.setText('{} in hand, enclosing nothing: {}.'.format(
                plural(count, 'line'), self._gap_text(loose)))

    def _gap_text(self, loose):
        if not loose:
            return 'the courses overlap or double back rather than closing'
        if len(loose) <= NAMED_ENDS:
            return '{} at {}'.format(
                plural(len(loose), 'loose end'),
                ', '.join(self._point_text(point) for point in loose))
        return plural(len(loose), 'loose end')

    def _point_text(self, point):
        """A coordinate the user can find the gap by.

        Three decimals is a millimetre in metres and a hundred metres in
        degrees, so the two corners of an unclosed parcel in Cambodia printed
        as the same place. The layer says which it is.
        """
        layer = self.current_layer()
        geographic = (layer is not None and layer.crs().isValid()
                      and layer.crs().isGeographic())
        places = 8 if geographic else 3
        return '({0:.{2}f}, {1:.{2}f})'.format(point.x(), point.y(), places)

    # ------------------------------------------------------------ geometry --

    def tolerance(self, workspace):
        return workspace.distance(self.tolerance_spin.value()) \
            if workspace is not None else 0.0

    def workspace(self, layer):
        """Where the tolerance is measured, or None when there is no tolerance.

        A tolerance of zero needs no working CRS, and building one anyway
        would send a boundary through a reprojection round trip it did not ask
        for - which moves every vertex a little and leaves a polygon that no
        longer sits exactly on the lines it was built from.
        """
        if self.tolerance_spin.value() <= 0:
            return None
        return self.make_workspace(layer, self.unit_combo.currentData())

    def build_from(self, features):
        """`(polygons, loose ends)` for these features, in layer CRS."""
        layer = self.current_layer()
        if layer is None:
            return [], []
        try:
            workspace = self.workspace(layer)
            geometries = [f.geometry() for f in features]
            if workspace is not None:
                geometries = [workspace.to_work(g) for g in geometries]
            polygons, loose = construct_polygons(geometries,
                                                 self.tolerance(workspace))
            if workspace is not None:
                polygons = [workspace.from_work(p) for p in polygons]
                loose = [workspace.from_work(
                    QgsGeometry.fromPointXY(point)).asPoint()
                    for point in loose]
        except ValueError:                  # out of the working CRS domain
            return [], []
        return polygons, loose

    def result_for(self, feature):
        """Hovering a line shows what it would close, together with what is held.

        A single closed line already encloses something, so this answers even
        with nothing in hand - which is the difference from Merge, where one
        feature merged with itself is the shape it already has.
        """
        held = [f for f in self._held if f.id() != feature.id()]
        polygons, _loose = self.build_from(held + [feature])
        return polygons

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        target = self.target_layer()

        # Built here rather than read off the verdict: a drag hands its
        # features straight to `run_on` without ever passing through the set
        # in hand, so the verdict may be about something else entirely.
        polygons, loose = self.build_from(features)
        if not polygons:
            # Pressing Enter empties the hand, and a refusal that also threw
            # the courses away would mean clicking the whole boundary again to
            # try a larger tolerance. Put them back, then say why.
            self.drop_held()
            self.hold(features)
            self.warn('Those {} do not close, so there is nothing to '
                      'construct: {}. Snap the ends together, or raise the '
                      'tolerance above the gap and press Enter again.'.format(
                          plural(len(features), 'line'), self._gap_text(loose)))
            self.flash(loose)
            return ''                       # the warning is the report

        if self.combine_check.isChecked() and len(polygons) > 1:
            merged = dissolve(polygons)
            if merged is not None:
                polygons = [merged]

        values = (carry_attributes(features[0], layer, target)
                  if self.attributes_check.isChecked() else {})

        added = 0
        dropped = 0
        target.beginEditCommand('KGA Construct Polygon')
        try:
            for polygon in polygons:
                parts = fit_to_layer(polygon, target)
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
        self._say_verdict([])

        notes = []
        if dropped:
            notes.append('{} could not be created'.format(dropped))
        if loose:
            notes.append('{} closed nothing and {} left out'.format(
                plural(len(loose), 'loose end'),
                'was' if len(loose) == 1 else 'were'))
        note = '' if not notes else ' ({}.)'.format('; '.join(notes))
        return '{} from {} added to "{}".{}'.format(
            plural(added, 'polygon'), plural(len(features), 'line'),
            target.name(), note)

    def flash(self, points):
        """Blink the gaps on the map, so the message points at somewhere real."""
        layer = self.current_layer()
        if not points or layer is None:
            return
        with suppress(Exception):           # pragma: no cover - older build
            self.canvas.flashGeometries(
                [QgsGeometry.fromPointXY(point) for point in points],
                layer.crs())


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_construct_polygon():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(ConstructPolygonDialog)


class ConstructPolygonAlgorithm(QgsProcessingAlgorithm):
    """Show the Construct Polygon pane."""

    def tr(self, string):
        return QCoreApplication.translate('KgaConstructPolygon', string)

    def createInstance(self):
        return ConstructPolygonAlgorithm()

    def name(self):
        return 'construct_polygon'

    def displayName(self):
        return self.tr('Construct Polygon')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('construct_polygon')

    def shortHelpString(self):
        return self.tr(
            'Build a polygon from the lines that bound it, the way ArcGIS '
            'Pro\'s <i>Modify Features > Construct Polygons</i> does.\n\n'
            'Choose the <b>Template</b> - the polygon layer the result is '
            'created in - press <b>Construct</b>, then click each line of the '
            'boundary and press Enter. Dragging a path across them all does '
            'it in one gesture, and lines already selected in the layer are '
            'taken in hand as soon as the tool is switched on. Hovering a '
            'line draws in cyan what it would close together with the lines '
            'already in hand.\n\n'
            '<b>The lines have to close.</b> Courses that stop short of one '
            'another enclose nothing, and the tool refuses rather than '
            'writing a polygon that is not the boundary drawn: it names the '
            'loose ends, flashes them on the map, and leaves the lines in '
            'hand so the gap can be fixed and Enter pressed again. The line '
            'count and whether it currently closes are shown above the '
            'buttons as you click.\n\n'
            '<b>Tolerance</b> is how far apart two ends may be and still '
            'count as meeting; ends within it are pulled onto one point '
            'before the polygon is built. Zero - the default - demands that '
            'they touch exactly. On a layer stored in degrees the tolerance '
            'is measured in the matching UTM zone, so meters mean meters.\n\n'
            'Lines that cross are noded first, so a boundary that encloses '
            'several areas gives one polygon per area. <b>Combine the '
            'polygons into one feature</b> makes them a single feature '
            'instead. Attributes come from the template layer\'s own '
            'defaults unless the box below it is ticked.\n\n'
            'Circular arcs come back segmented: the construction is GEOS '
            'work and GEOS has no arcs.\n\n'
            'The lines themselves are not changed. One press is one undo '
            'step. The pane stays open while you work - close this Processing '
            'window once it appears.'
        )

    def flags(self):
        # Opens a dialog and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Construct Polygon is an interactive tool and needs the QGIS '
                'map canvas, so it cannot run from a head-less Processing '
                'session.'))
        show_dialog(iface, ConstructPolygonDialog)
        feedback.pushInfo(self.tr(
            'Construct Polygon opened. You can close this Processing dialog '
            'and keep working on the map.'))
        return {}

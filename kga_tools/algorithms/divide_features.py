# -*- coding: utf-8 -*-

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsVectorLayer,
)

from ..core.compat import no_threading
from ..core.modify_features import (
    AREA_UNITS,
    DISTANCE_UNITS,
    DIVIDE_EQUAL_PARTS,
    DIVIDE_MEASURE,
    DIVIDE_PERCENT,
    LINE_METHODS,
    POLYGON_METHODS,
    REMAINDER_END,
    REMAINDER_LABELS,
    divide_line,
    divide_polygon,
    division_angle,
    fit_to_layer,
    geometry_type,
    layer_filter,
)
from ..gui.modify_base import (
    canvas_to_layer,
    carry_attributes,
    is_deleted,
    make_feature,
    workspace_for,
)
from ..gui.modify_dialog import (
    ANGLE_DRAW,
    ANGLE_EDGE,
    AngleMapTool,
    ModifyFeaturesDialog,
    close_dialog,
    show_dialog,
)
from ..branding import docs_url

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None


class DivideDialog(ModifyFeaturesDialog):
    """The Divide window."""

    ALG_NAME = 'divide_features'
    TITLE = 'Divide'
    ACTION_LABEL = 'Divide'
    HINT = ('Choose how the parts are sized, press Divide, then hover a '
            'feature to see the cuts and click to make them. Drag a path '
            'across several to divide them all. A polygon is cut at the '
            'division angle: type it, click an edge to run parallel to it, '
            'or draw it.')

    LAYER_FILTER = layer_filter('LineLayer', 'PolygonLayer')
    ACCEPTS = ('Line', 'Polygon')

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        # Set up before any widget exists, because building the rows below
        # connects signals that can reach the angle handlers straight away -
        # a combo emits `currentIndexChanged` as its first item goes in.
        self._angle_tool = None             # the pick in progress, if any
        self._angle_mode = ANGLE_EDGE
        self._angle_points = ()             # what a picked angle was read off
        self._angle_prompt = ''
        self._resume_tool = False           # was Divide in hand before the pick
        self._pick_previous = None          # what held the canvas before it
        self._setting_angle = False

        self.method_combo = self.add_choice('Divide into', POLYGON_METHODS,
                                            DIVIDE_EQUAL_PARTS)

        self.parts_spin = QSpinBox()
        self.parts_spin.setRange(2, 1000)
        self.parts_spin.setValue(2)
        form.addRow('Number of parts', self.parts_spin)

        # One measure row that means length on a line and area on a polygon;
        # only one of the two unit combos is ever on screen.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.measure_spin = QDoubleSpinBox()
        self.measure_spin.setDecimals(4)
        self.measure_spin.setRange(0.0001, 1e12)
        self.measure_spin.setValue(1.0)
        row.addWidget(self.measure_spin, 1)

        self.area_unit_combo = QComboBox()
        for text, name in AREA_UNITS:
            self.area_unit_combo.addItem(text, name)
        self.area_unit_combo.setCurrentIndex(1)     # hectares
        row.addWidget(self.area_unit_combo)

        self.length_unit_combo = QComboBox()
        for text, name in DISTANCE_UNITS:
            self.length_unit_combo.addItem(text, name)
        self.length_unit_combo.setCurrentIndex(1)   # meters
        row.addWidget(self.length_unit_combo)

        self.measure_holder = QWidget()
        self.measure_holder.setLayout(row)
        form.addRow('Size of each part', self.measure_holder)

        self.percent_spin = QDoubleSpinBox()
        self.percent_spin.setDecimals(4)
        self.percent_spin.setRange(0.0001, 100.0)
        self.percent_spin.setValue(25.0)
        self.percent_spin.setSuffix(' %')
        form.addRow('Percentage', self.percent_spin)

        self.remainder_combo = self.add_choice('Leftover', REMAINDER_LABELS,
                                               REMAINDER_END)
        self.remainder_combo.setToolTip(
            'What happens to the piece that is too small to be a part of its '
            'own. Spreading it makes every part slightly larger than asked '
            'for, but equal.')

        # The angle, and the two ways of taking it off the map rather than
        # typing it. Both buttons only fill this box in, so a picked angle can
        # still be nudged by hand and there is one number to read at the end.
        angle_row = QHBoxLayout()
        angle_row.setContentsMargins(0, 0, 0, 0)
        self.angle_spin = QDoubleSpinBox()
        # Four decimals, like the measure and percentage boxes above: an angle
        # taken off the map is a measurement rather than a round number, and
        # rounding it to 0.01 degrees moves the far end of a 500 m parcel by
        # nine centimetres.
        self.angle_spin.setDecimals(4)
        self.angle_spin.setRange(-360.0, 360.0)
        self.angle_spin.setValue(0.0)
        self.angle_spin.setSuffix(' deg')
        self.angle_spin.setToolTip(
            'The direction the cuts run in, counter-clockwise from east. 0 '
            'cuts along horizontal lines, 90 along vertical ones.')
        angle_row.addWidget(self.angle_spin, 1)

        self.edge_button = QPushButton('Edge')
        self.edge_button.setCheckable(True)
        self.edge_button.setToolTip(
            'Take the angle off a boundary: click an edge on the map - this '
            'parcel\'s, the road it fronts, the neighbour it abuts - and the '
            'cuts run parallel to it.')
        angle_row.addWidget(self.edge_button)

        self.draw_button = QPushButton('Draw')
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip(
            'Draw the angle: click the two ends of a line on the map and the '
            'cuts run along it. Snapping applies, so it can be drawn corner '
            'to corner exactly.')
        angle_row.addWidget(self.draw_button)

        for button in (self.edge_button, self.draw_button):
            button.setMaximumWidth(64)

        self.angle_holder = QWidget()
        self.angle_holder.setLayout(angle_row)
        form.addRow('Division angle', self.angle_holder)

        self.angle_note = QLabel('Typed.')
        self.angle_note.setWordWrap(True)
        self.angle_note.setStyleSheet('color: #666;')
        form.addRow('', self.angle_note)

        self.attributes_check = QCheckBox('Copy the source attributes to every part')
        self.attributes_check.setChecked(True)
        form.addRow('', self.attributes_check)

        self.method_combo.currentIndexChanged.connect(self._update_rows)
        self.angle_spin.valueChanged.connect(self._angle_typed)
        self.edge_button.toggled.connect(self._edge_toggled)
        self.draw_button.toggled.connect(self._draw_toggled)
        # Switching between a real area unit and map units switches the CRS
        # the cut is worked out in, on a layer stored in degrees - and with it
        # the number that describes a picked edge.
        self.area_unit_combo.currentIndexChanged.connect(
            self._refresh_picked_angle)

        self._update_rows()

    def on_layer_changed(self, layer):
        """Re-label the method combo: a line divides by length, a polygon by area."""
        methods = POLYGON_METHODS if self.is_polygon() else LINE_METHODS
        current = self.method_combo.currentIndex()
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        for text in methods:
            self.method_combo.addItem(text)
        self.method_combo.setCurrentIndex(max(0, min(current, len(methods) - 1)))
        self.method_combo.blockSignals(False)
        self._update_rows()
        self._refresh_picked_angle()

    def is_polygon(self):
        layer = self.current_layer()
        return (layer is not None
                and layer.geometryType() == geometry_type('Polygon'))

    def _update_rows(self):
        method = self.method_combo.currentIndex()
        polygon = self.is_polygon()

        self.set_row_visible(self.parts_spin, method == DIVIDE_EQUAL_PARTS)
        self.set_row_visible(self.measure_holder, method == DIVIDE_MEASURE)
        self.set_row_visible(self.percent_spin, method == DIVIDE_PERCENT)
        # A leftover only exists when the part size was given, not the count.
        self.set_row_visible(self.remainder_combo, method != DIVIDE_EQUAL_PARTS)
        self.set_row_visible(self.angle_holder, polygon)
        self.set_row_visible(self.angle_note, polygon)
        if not polygon:
            # A line divides along itself and has no angle to pick, so a pick
            # left running would hold the canvas for a row nobody can see.
            self._end_pick()

        self.area_unit_combo.setVisible(polygon)
        self.length_unit_combo.setVisible(not polygon)
        label = self.parameters.labelForField(self.measure_holder)
        if label is not None:
            label.setText('Area of each part' if polygon else 'Length of each part')

    # ------------------------------------------------------ division angle --
    #
    #  Three ways to say which way the cuts run, one number at the end of them.
    #  Typing it is the one ArcGIS offers first and the one people reach for
    #  last, because a parcel's direction is known as "parallel to the road",
    #  not as 34.27 degrees.

    @staticmethod
    def _angle_text(value):
        """An angle for a status line: four decimals, without the trailing zeros.

        So that a picked 44.7936 keeps its precision and a typed 30 still reads
        as 30 rather than 30.0000.
        """
        text = '{:.4f}'.format(float(value)).rstrip('0').rstrip('.')
        return text or '0'

    def _angle_typed(self, _value=None):
        """The user turned the box themselves, so it is no longer off the map."""
        if self._setting_angle:
            return
        self._angle_points = ()
        self.angle_note.setText('Typed.')

    def _set_angle(self, value, note):
        self._setting_angle = True
        try:
            self.angle_spin.setValue(value)
        finally:
            self._setting_angle = False
        self.angle_note.setText(note)

    def _angle_between(self, start, end):
        """The division angle two canvas-CRS points describe, or None.

        Read in the CRS the cut is made in rather than the one on screen. A
        layer stored in degrees, drawn in a Web Mercator project and divided
        in hectares has three CRSs in play at once, and an edge that looks
        like 34 degrees on the map is not at 34 degrees in the UTM zone the
        areas are worked out in - so the strips would come out square to
        nothing.
        """
        layer = self.current_layer()
        if layer is None or start is None or end is None:
            return None

        transform = canvas_to_layer(self.canvas, layer)
        points = []
        for point in (start, end):
            point = QgsPointXY(point)
            if transform is not None:
                try:
                    point = transform.transform(point)
                except Exception:           # pragma: no cover - out of domain
                    return None
            points.append(point)

        # Built the same way `parts_of` builds it, so the angle is read in the
        # CRS the strips are actually cut in.
        workspace = workspace_for(self.canvas, layer,
                                  self.area_unit_combo.currentData())
        if workspace.reprojects:
            try:
                line = workspace.to_work(QgsGeometry.fromPolylineXY(points))
            except ValueError:              # pragma: no cover - out of domain
                return None
            points = line.asPolyline()
            if len(points) < 2:             # pragma: no cover
                return None
        return division_angle(points[0], points[-1])

    def _refresh_picked_angle(self, *_args):
        """Re-read a picked angle when the CRS it is read in changes.

        The edge has not moved; the number that describes it has - which is
        what changing the area unit on a layer stored in degrees does, and
        what changing the layer does.
        """
        if not self._angle_points:
            return
        angle = self._angle_between(*self._angle_points)
        if angle is not None:
            self._set_angle(angle, self.angle_note.text())

    # --------------------------------------------------------- picking it --

    def _edge_toggled(self, checked):
        self._pick_toggled(checked, ANGLE_EDGE, self.draw_button)

    def _draw_toggled(self, checked):
        self._pick_toggled(checked, ANGLE_DRAW, self.edge_button)

    def _pick_toggled(self, checked, mode, other):
        if not checked:
            self._end_pick()
            return
        self._uncheck_quietly(other)
        self._begin_pick(mode)

    @staticmethod
    def _uncheck_quietly(button):
        """Pop a button up without it calling back in and ending the pick."""
        button.blockSignals(True)
        button.setChecked(False)
        button.blockSignals(False)

    def _begin_pick(self, mode):
        """Hand the canvas to the angle tool, remembering what had it."""
        if self.current_layer() is None:
            self._uncheck_quietly(self.edge_button)
            self._uncheck_quietly(self.draw_button)
            self.warn('Choose a layer first.')
            return

        # Captured before the canvas changes hands: setting a new map tool
        # pops the Divide button up, and this is what puts it back down. The
        # tool it is taken from is kept too, so that picking an angle without
        # Divide armed does not leave the canvas with nothing in hand.
        self._resume_tool = self.activate_button.isChecked()
        self._angle_mode = mode
        self._drop_angle_tool()
        previous = self.canvas.mapTool()
        self._pick_previous = None if previous is self._tool else previous

        tool = AngleMapTool(self.canvas, self, mode)
        tool.message.connect(self.report)
        self._angle_tool = tool             # set before the canvas is told
        self.canvas.setMapTool(tool)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

        self._angle_prompt = (
            'Click an edge on the map and the cuts will run parallel to it. '
            'Esc goes back.' if mode == ANGLE_EDGE else
            'Click the two ends of the line the cuts should run along. Esc '
            'goes back.')
        self.report(self._angle_prompt)

    def _drop_angle_tool(self):
        """Take the angle tool off the canvas, and its band with it."""
        tool, self._angle_tool = self._angle_tool, None
        if tool is None or is_deleted(tool):
            return
        if self.canvas.mapTool() is tool:
            self.canvas.unsetMapTool(tool)
        tool.dispose()
        tool.deleteLater()

    def _end_pick(self):
        """Put the canvas back as the pick found it; True if Divide resumed."""
        self._uncheck_quietly(self.edge_button)
        self._uncheck_quietly(self.draw_button)
        picking = self._angle_tool is not None
        self._drop_angle_tool()

        resume, self._resume_tool = self._resume_tool, False
        previous, self._pick_previous = self._pick_previous, None
        if picking and resume:
            self.activate_button.setChecked(True)
        elif picking and previous is not None and not is_deleted(previous):
            self.canvas.setMapTool(previous)
        return bool(picking and resume)

    def _map_tool_set(self, new_tool, _old_tool=None):
        """Something else took the canvas, so the pick - if any - is over.

        The pan tool, another plugin, the identify button: whatever took the
        canvas keeps it, and the buttons must stop saying a pick is running.
        """
        if self._angle_tool is not None and new_tool is not self._angle_tool:
            # Nothing is restored here: whatever took the canvas is the user's
            # choice, and putting the old tool back would take it off them.
            self._resume_tool = False
            self._pick_previous = None
            self._end_pick()
        super()._map_tool_set(new_tool, _old_tool)

    # -------------------------------------------- what the angle tool asks --

    def angle_layers(self):
        """Layers whose edges may be clicked for an angle.

        The layer being divided first, then everything else drawn on the map
        with an edge to offer. A parcel is very often split parallel to the
        road it fronts or the boundary it abuts, and those are usually
        somebody else's layer.
        """
        layers = []
        current = self.current_layer()
        if current is not None:
            layers.append(current)
        wanted = (geometry_type('Line'), geometry_type('Polygon'))
        for layer in self.canvas.layers():
            if (isinstance(layer, QgsVectorLayer)
                    and layer not in layers
                    and layer.geometryType() in wanted):
                layers.append(layer)
        return layers

    def angle_hovered(self, start, end):
        """An edge - or nothing - is under the cursor. Say what it would give."""
        angle = self._angle_between(start, end)
        if angle is None:
            self.report(self._angle_prompt)
            return
        self.report('{} deg. Click to use it.'.format(self._angle_text(angle)))

    def angle_chosen(self, start, end):
        angle = self._angle_between(start, end)
        if angle is None:
            self.warn('That gives no direction - the two points are in the '
                      'same place, or that part of the map will not project '
                      'into the layer.')
            return
        self._angle_points = (QgsPointXY(start), QgsPointXY(end))
        self._set_angle(angle, 'Taken from an edge on the map.'
                        if self._angle_mode == ANGLE_EDGE
                        else 'Drawn on the map.')
        resumed = self._end_pick()
        # Read back off the box rather than from `angle`, so the number the
        # status line quotes is the one the cut will actually be made at.
        self.report('Division angle set to {} deg.{}'.format(
            self._angle_text(self.angle_spin.value()),
            ' Hover a feature to see the cuts.' if resumed else ''))

    def angle_cancelled(self):
        resumed = self._end_pick()
        if not resumed:
            self.report('Angle left at {} deg.'.format(
                self._angle_text(self.angle_spin.value())))

    # --------------------------------------------------------------- close --

    def cleanup(self):
        # Before the base class, which is where the map tool and the bands go.
        self._drop_angle_tool()
        super().cleanup()

    # -------------------------------------------------------------- divide --

    def parts_of(self, feature, layer):
        """The pieces one feature divides into, in layer CRS.

        Fewer than two means it did not divide - a part size larger than the
        feature, or an angle that misses it - and the caller reports that
        rather than writing a single "part" back over the original.
        """
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return []

        method = self.method_combo.currentIndex()
        polygon = self.is_polygon()
        unit_name = (self.area_unit_combo.currentData() if polygon
                     else self.length_unit_combo.currentData())
        workspace = self.make_workspace(layer, unit_name)
        try:
            working = workspace.to_work(geometry)
        except ValueError:
            return []

        if polygon:
            measure = workspace.area(self.measure_spin.value(), unit_name)
            pieces = divide_polygon(
                working, method, self.parts_spin.value(), measure,
                self.percent_spin.value(), self.angle_spin.value(),
                self.remainder_combo.currentIndex())
        else:
            measure = workspace.distance(self.measure_spin.value())
            pieces = divide_line(
                working, method, self.parts_spin.value(), measure,
                self.percent_spin.value(),
                self.remainder_combo.currentIndex())

        if len(pieces) < 2:
            return []
        try:
            return [workspace.from_work(piece) for piece in pieces]
        except ValueError:                  # pragma: no cover
            return []

    def result_for(self, feature):
        layer = self.current_layer()
        return [] if layer is None else self.parts_of(feature, layer)

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        groups = [(feature, self.parts_of(feature, layer))
                  for feature in features]
        groups = [pair for pair in groups if pair[1]]
        if not groups:
            return 'None of the {} feature{} divided at that size - the parts ' \
                   'asked for are larger than the feature.'.format(
                       len(features), '' if len(features) == 1 else 's')

        carry = self.attributes_check.isChecked()
        divided = 0
        created = 0
        dropped = 0
        kept_ids = []

        layer.beginEditCommand('KGA Divide')
        try:
            for feature, parts in groups:
                fitted = []
                for part in parts:
                    fitted.extend(fit_to_layer(part, layer))
                if len(fitted) < 2:
                    dropped += 1
                    continue

                # The first part stays on the original feature, so whatever is
                # keyed on its id still resolves. The rest are new features.
                layer.changeGeometry(feature.id(), fitted[0])
                kept_ids.append(feature.id())
                divided += 1

                values = (carry_attributes(feature, layer, layer)
                          if carry else {})
                for part in fitted[1:]:
                    new = make_feature(layer, part, values)
                    if layer.addFeature(new):
                        created += 1
                        kept_ids.append(new.id())
                    else:
                        dropped += 1
        except Exception:
            layer.destroyEditCommand()
            raise
        layer.endEditCommand()

        if kept_ids:
            layer.selectByIds(kept_ids)
        layer.triggerRepaint()
        self.count(divided + created)

        note = ('' if not dropped else
                ' ({} could not be divided.)'.format(dropped))
        return '{} feature{} divided into {} parts.{}'.format(
            divided, '' if divided == 1 else 's', divided + created, note)


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_divide_features():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(DivideDialog)


class DivideFeaturesAlgorithm(QgsProcessingAlgorithm):
    """Show the Divide pane."""

    def tr(self, string):
        return QCoreApplication.translate('KgaDivideFeatures', string)

    def createInstance(self):
        return DivideFeaturesAlgorithm()

    def name(self):
        return 'divide_features'

    def displayName(self):
        return self.tr('Divide')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('divide_features')

    def shortHelpString(self):
        return self.tr(
            'Cut the selected features into parts, the way ArcGIS Pro\'s '
            '<i>Modify Features > Divide</i> does. The pane reads differently '
            'for a line and for a polygon.\n\n'
            '<b>Lines</b> divide into equal parts, into parts of a specified '
            'length, or into parts of a percentage of the total. A multipart '
            'line is measured end to end as one run.\n\n'
            '<b>Polygons</b> divide into parallel strips of the asked-for '
            'area, along the <b>Division angle</b> — the direction the cuts '
            'run in, counter-clockwise from east. Notches and holes are '
            'handled: the cut positions are searched for, not calculated from '
            'a formula that assumes a rectangle.\n\n'
            'That angle can be typed, but it is rarely known as a number. '
            '<b>Edge</b> takes it off a boundary — click any edge on the map, '
            'this parcel\'s or a neighbour\'s, and the cuts run parallel to '
            'it. <b>Draw</b> takes it off a line you click the two ends of, '
            'with snapping, so it can be set corner to corner exactly. Either '
            'way the number lands in the angle box and can still be nudged by '
            'hand, and it is read in the CRS the cut is made in rather than '
            'the one on screen.\n\n'
            '<b>Leftover</b> decides what happens to the piece too small to be '
            'a part of its own: leave it at the end, leave it at the start, or '
            'spread it so every part comes out equal and slightly larger than '
            'asked for.\n\n'
            'The first part keeps the original feature\'s identity and the '
            'rest are added beside it, so joins and relates still resolve. The '
            'parts end up selected, ready for the next tool.\n\n'
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
                'Divide is an interactive tool and needs the QGIS map canvas, '
                'so it cannot run from a head-less Processing session.'))
        show_dialog(iface, DivideDialog)
        feedback.pushInfo(self.tr(
            'Divide opened. You can close this Processing dialog and keep '
            'working on the map.'))
        return {}

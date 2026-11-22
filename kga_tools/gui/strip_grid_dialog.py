# -*- coding: utf-8 -*-
"""Strip Lines and Point Grid: the window.

Pick an area (the shared `RoiPicker`: map extent, a rectangle or polygon drawn
on the map, a layer's extent, or a polygon layer's boundary), say what to make
and how far apart, and press Create. The maths is in `core/strip_grid.py`.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
import traceback

from qgis.core import (Qgis, QgsCoordinateTransformContext,
                       QgsFillSymbol, QgsMessageLog, QgsProject, QgsSymbol,
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                                 QComboBox, QDialog, QDoubleSpinBox,
                                 QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QRadioButton, QSpinBox,
                                 QVBoxLayout, QWidget)

from ..branding import LOG_TAG, open_docs
from ..core import dem_sources as DEM
from ..core import output_defaults as od
from ..core import strip_grid as SG
from .modify_dialog import ANGLE_DRAW, ANGLE_EDGE, AngleMapTool
from .roi_picker import RoiPicker
from .tight_form import TightForm
from .window_reveal import revealed

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

TITLE = 'Strip Lines and Point Grid'
BLUE = '#174592'
STRIP_COLOUR = '#E8702A'
POINT_COLOUR = '#FF0000'
SHAPE_COLOUR = '#174592'
CENTRE_COLOUR = '#0B2A5B'

STYLE = """
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 22px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaHint { color: palette(mid); }
""" % {'blue': BLUE}

DIRECTIONS = (
    ('vertical', 'Vertical (north-south)', 0.0),
    ('horizontal', 'Horizontal (east-west)', 90.0),
    ('custom', 'Custom angle', None),
)


def tr(text):
    return text


def save_layer(layer, path, layer_name):
    """Write `layer` to a GeoPackage layer and load it back."""
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.fileEncoding = 'UTF-8'
    options.layerName = layer_name
    if os.path.exists(path):
        options.actionOnExistingFile = \
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), options)
    if result[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(result[1])
    return QgsVectorLayer('{0}|layername={1}'.format(path, layer_name),
                          layer_name, 'ogr')


@revealed
class StripGridDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaStripGridDialog')
        self.setMinimumWidth(460)
        self.setStyleSheet(STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._torn_down = False
        self._area = None
        self._area_error = ''

        canvas = iface.mapCanvas() if iface is not None else None
        outer = QVBoxLayout(self)

        self.area_picker = RoiPicker(canvas, self, title='Area')
        self.area_picker.message.connect(self._say)
        self.area_picker.changed.connect(self._area_changed)
        outer.addWidget(self.area_picker)

        form = TightForm()
        self.form = form

        # -- what to make --
        kind_row = QHBoxLayout()
        self.lines_radio = QRadioButton(tr('Strip lines'), self)
        self.points_radio = QRadioButton(tr('Point grid'), self)
        self.shapes_radio = QRadioButton(tr('Shape grid'), self)
        self._kind_group = QButtonGroup(self)
        self._kind_group.addButton(self.lines_radio)
        self._kind_group.addButton(self.points_radio)
        self._kind_group.addButton(self.shapes_radio)
        self.lines_radio.setChecked(True)
        kind_row.addWidget(self.lines_radio)
        kind_row.addWidget(self.points_radio)
        kind_row.addWidget(self.shapes_radio)
        kind_row.addStretch(1)
        form.addRow(tr('Make'), kind_row)

        # -- strip spacing --
        self.spacing_mode = QComboBox(self)
        self.spacing_mode.addItem(tr('Equal spacing'), 'equal')
        self.spacing_mode.addItem(tr('Varying spacing'), 'varying')
        form.addRow(tr('Strips'), self.spacing_mode)

        self.spacing_spin = self._spin(50.0)
        self.spacing_spin.setToolTip(tr('Distance between neighbouring '
                                        'lines, in metres.'))
        form.addRow(tr('Spacing'), self.spacing_spin)

        self.pattern_edit = QLineEdit(self)
        self.pattern_edit.setPlaceholderText('10, 20, 30')
        self.pattern_edit.setToolTip(tr(
            'The gaps between lines in metres, in order. They repeat until '
            'the area is covered: 10, 20, 30 gives gaps of 10, 20, 30, 10, '
            '20, 30 ...'))
        form.addRow(tr('Gaps (m)'), self.pattern_edit)

        # -- shape grid --
        self.shape_combo = QComboBox(self)
        for key, label in SG.SHAPE_CHOICES:
            self.shape_combo.addItem(tr(label), key)
        form.addRow(tr('Shape'), self.shape_combo)
        self.size_spin = self._spin(50.0)
        form.addRow(tr('Size'), self.size_spin)
        self.height_spin = self._spin(50.0)
        self.height_spin.setToolTip(tr('Height of each rectangle, in metres.'))
        form.addRow(tr('Height'), self.height_spin)
        self.sides_spin = QSpinBox(self)
        self.sides_spin.setRange(3, 64)
        self.sides_spin.setValue(8)
        self.sides_spin.setToolTip(tr('Number of sides of each shape.'))
        form.addRow(tr('Sides'), self.sides_spin)
        self.keep_combo = QComboBox(self)
        for key, label in SG.KEEP_CHOICES:
            self.keep_combo.addItem(tr(label), key)
        self.keep_combo.setToolTip(tr(
            'Which cells to keep at the edge of the area.'))
        form.addRow(tr('Keep'), self.keep_combo)
        self.centres_check = QCheckBox(tr('Also make centre points'), self)
        self.centres_check.setToolTip(tr(
            'Adds a point layer with the centre of every cell, numbered the '
            'same way as the cells.'))
        form.addRow('', self.centres_check)

        # -- point spacing --
        self.column_spin = self._spin(50.0)
        self.column_spin.setToolTip(tr('Distance between points along a '
                                       'row, in metres.'))
        form.addRow(tr('Spacing'), self.column_spin)
        self.same_check = QCheckBox(tr('Same distance between rows'), self)
        self.same_check.setChecked(True)
        form.addRow('', self.same_check)
        self.row_spin = self._spin(50.0)
        self.row_spin.setToolTip(tr('Distance between rows of points, in '
                                    'metres.'))
        form.addRow(tr('Row spacing'), self.row_spin)

        # -- direction and start --
        self.direction_combo = QComboBox(self)
        for key, label, _angle in DIRECTIONS:
            self.direction_combo.addItem(tr(label), key)
        form.addRow(tr('Direction'), self.direction_combo)
        self.angle_spin = QDoubleSpinBox(self)
        self.angle_spin.setRange(0.0, 179.9999)
        self.angle_spin.setDecimals(4)
        self.angle_spin.setSuffix(' \u00b0')
        self.angle_spin.setToolTip(tr(
            'Bearing of the lines: 0 is north-south, 90 is east-west, 45 '
            'runs north-east. A point grid is turned by the same angle.'))
        angle_row = QHBoxLayout()
        angle_row.setContentsMargins(0, 0, 0, 0)
        angle_row.addWidget(self.angle_spin, 1)
        self.edge_button = QPushButton(tr('Edge'), self)
        self.edge_button.setCheckable(True)
        self.edge_button.setToolTip(tr(
            'Take the direction off a boundary: click an edge on the map and '
            'the lines run parallel to it.'))
        self.draw_button = QPushButton(tr('Draw'), self)
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip(tr(
            'Draw the direction: click the two ends of a line on the map. '
            'Snapping applies.'))
        for button in (self.edge_button, self.draw_button):
            button.setMaximumWidth(64)
            angle_row.addWidget(button)
        self.angle_holder = QWidget(self)
        self.angle_holder.setLayout(angle_row)
        form.addRow(tr('Angle'), self.angle_holder)
        self._angle_tool = None
        self._pick_previous = None
        self._angle_prompt = ''

        self.start_combo = QComboBox(self)
        for key, label in SG.START_CHOICES:
            self.start_combo.addItem(tr(label), key)
        self.start_combo.setToolTip(tr(
            'Where the first line or point goes. Lines are counted from the '
            'left edge (vertical) or the top edge (horizontal).'))
        form.addRow(tr('First one'), self.start_combo)

        self.offset_spin = QDoubleSpinBox(self)
        self.offset_spin.setRange(0.0, 1000000.0)
        self.offset_spin.setDecimals(2)
        self.offset_spin.setSuffix(' m')
        self.offset_spin.setToolTip(tr(
            'Keep the lines or points this far inside the edge of the area. '
            '0 uses the whole area. "First one" is then counted from the '
            'inner edge.'))
        form.addRow(tr('Offset from edge'), self.offset_spin)

        # -- output --
        out_row = QHBoxLayout()
        self.temp_radio = QRadioButton(tr('Temporary layer'), self)
        self.file_radio = QRadioButton(tr('Save to file'), self)
        self._out_group = QButtonGroup(self)
        self._out_group.addButton(self.temp_radio)
        self._out_group.addButton(self.file_radio)
        self.temp_radio.setChecked(True)
        out_row.addWidget(self.temp_radio)
        out_row.addWidget(self.file_radio)
        out_row.addStretch(1)
        form.addRow(tr('Output'), out_row)
        self.file_widget = QgsFileWidget(self)
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.file_widget.setFilter('GeoPackage (*.gpkg)')
        form.addRow(tr('File'), self.file_widget)
        outer.addLayout(form)

        self.status = QLabel('', self)
        self.status.setObjectName('kgaHint')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        buttons = QHBoxLayout()
        help_button = QPushButton(tr('Help'), self)
        help_button.clicked.connect(lambda: open_docs('strip_point_grid'))
        buttons.addWidget(help_button)
        buttons.addStretch(1)
        self.create_button = QPushButton(tr('Create'), self)
        self.create_button.setObjectName('kgaPrimary')
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self.create)
        buttons.addWidget(self.create_button)
        close = QPushButton(tr('Close'), self)
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        outer.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._estimate)

        for signal in (self.lines_radio.toggled, self.points_radio.toggled,
                       self.shape_combo.currentIndexChanged,
                       self.spacing_mode.currentIndexChanged,
                       self.same_check.toggled, self.direction_combo.currentIndexChanged,
                       self.temp_radio.toggled):
            signal.connect(self._layout_changed)
        for signal in (self.spacing_spin.valueChanged,
                       self.size_spin.valueChanged,
                       self.height_spin.valueChanged,
                       self.sides_spin.valueChanged,
                       self.keep_combo.currentIndexChanged,
                       self.column_spin.valueChanged,
                       self.row_spin.valueChanged,
                       self.angle_spin.valueChanged,
                       self.offset_spin.valueChanged,
                       self.pattern_edit.textChanged,
                       self.start_combo.currentIndexChanged):
            signal.connect(self._schedule)
        self.column_spin.valueChanged.connect(self._mirror_rows)
        self.edge_button.toggled.connect(
            lambda on: self._pick_toggled(on, ANGLE_EDGE, self.draw_button))
        self.draw_button.toggled.connect(
            lambda on: self._pick_toggled(on, ANGLE_DRAW, self.edge_button))
        self.canvas = canvas
        if canvas is not None:
            canvas.mapToolSet.connect(self._map_tool_set)
        self._layout_changed()

    # ------------------------------------------------------------- building

    def _spin(self, value):
        spin = QDoubleSpinBox(self)
        spin.setRange(0.01, 1000000.0)
        spin.setDecimals(2)
        spin.setSuffix(' m')
        spin.setValue(value)
        return spin

    def _show(self, widget, visible):
        label = self.form.labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    def _kind(self):
        if self.shapes_radio.isChecked():
            return 'shapes'
        return 'points' if self.points_radio.isChecked() else 'lines'

    def _layout_changed(self, *_args):
        kind = self._kind()
        lines = kind == 'lines'
        shapes = kind == 'shapes'
        shape = self.shape_combo.currentData()
        # Regular polygons are placed on a grid of centres, so they take a
        # spacing like a point grid; the other shapes tile the area.
        spaced = kind == 'points' or (shapes and shape == SG.SHAPE_POLYGON)
        varying = self.spacing_mode.currentData() == 'varying'
        self._show(self.spacing_mode, lines)
        self._show(self.spacing_spin, lines and not varying)
        self._show(self.pattern_edit, lines and varying)
        self._show(self.shape_combo, shapes)
        self._show(self.size_spin, shapes)
        self._show(self.height_spin, shapes and shape == SG.SHAPE_RECTANGLE)
        self._show(self.sides_spin, shapes and shape == SG.SHAPE_POLYGON)
        self._show(self.keep_combo, shapes)
        self._show(self.centres_check, shapes)
        self._size_label(shape)
        self._show(self.column_spin, spaced)
        self._show(self.same_check, spaced)
        self._show(self.row_spin, spaced and not self.same_check.isChecked())
        self._show(self.start_combo, not shapes)
        self.row_spin.setEnabled(not self.same_check.isChecked())
        self._mirror_rows()
        self._show(self.angle_holder,
                   self.direction_combo.currentData() == 'custom')
        for key, _label, angle in DIRECTIONS:
            if key == self.direction_combo.currentData() and angle is not None:
                self.angle_spin.setValue(angle)
        self.file_widget.setEnabled(self.file_radio.isChecked())
        if self.file_radio.isChecked():
            self._suggest_file()
        self._schedule()
        self.adjustSize()

    def _size_label(self, shape):
        label = self.form.labelForField(self.size_spin)
        if label is None:
            return
        text = {SG.SHAPE_SQUARE: 'Side', SG.SHAPE_RECTANGLE: 'Width',
                SG.SHAPE_HEXAGON: 'Side', SG.SHAPE_TRIANGLE: 'Side',
                SG.SHAPE_POLYGON: 'Diameter'}.get(shape, 'Size')
        label.setText(tr(text))
        self.size_spin.setToolTip(tr(
            'Distance in metres: {0}.').format({
                SG.SHAPE_SQUARE: 'the side of each square',
                SG.SHAPE_RECTANGLE: 'the width of each rectangle',
                SG.SHAPE_HEXAGON: 'the side of each hexagon',
                SG.SHAPE_TRIANGLE: 'the side of each triangle',
                SG.SHAPE_POLYGON: 'the width of each shape across its '
                                  'corners'}.get(shape, '')))

    def _mirror_rows(self, *_args):
        if self.same_check.isChecked():
            self.row_spin.setValue(self.column_spin.value())

    def _suggest_file(self):
        if not self.file_widget.filePath().strip():
            self.file_widget.setFilePath(
                od.default_file(self._name().replace(' ', '_'), 'gpkg'))

    # ---------------------------------------------------------------- state

    def _say(self, text, error=False):
        self.status.setStyleSheet('color: #B3261E;' if error else '')
        self.status.setText(text)

    def _area_changed(self, *_args):
        self._area = None
        self._area_error = ''
        self._schedule()

    def _schedule(self, *_args):
        if not self._torn_down:
            self._timer.start()

    def _get_area(self, fresh=False):
        """The Area in its measuring CRS. Raises StripGridError."""
        if fresh:
            self.area_picker.forget_cached_boundary()
            self._area = None
        if self._area is not None:
            return self._area
        try:
            region = self.area_picker.region()
        except DEM.DemError as exc:
            raise SG.StripGridError(str(exc))
        if region is None:
            raise SG.StripGridError('Choose an area.')
        self._area = SG.Area(region.geometry, region.crs, region.label)
        return self._area

    def _bearing(self):
        key = self.direction_combo.currentData()
        for name, _label, angle in DIRECTIONS:
            if name == key and angle is not None:
                return angle
        return self.angle_spin.value()

    def _pattern(self):
        if self.spacing_mode.currentData() == 'varying':
            return SG.parse_spacings(self.pattern_edit.text())
        return [self.spacing_spin.value()]

    def _name(self):
        kind = self._kind()
        try:
            if kind == 'lines':
                return SG.layer_name('lines', self._pattern())
        except SG.StripGridError:
            return 'Strip lines'
        if kind == 'shapes':
            return SG.shape_layer_name(
                self.shape_combo.currentData(), self.size_spin.value(),
                self.height_spin.value(), self.sides_spin.value())
        return SG.layer_name('points', None, self.column_spin.value(),
                             self.row_spin.value())

    def _spacing(self):
        return (self.column_spin.value(), self.row_spin.value())

    def _estimate(self):
        if self._torn_down:
            return
        try:
            area = self._get_area()
            start = self.start_combo.currentData()
            kind = self._kind()
            if kind == 'shapes':
                n = SG.count_shapes_estimate(
                    area, self.shape_combo.currentData(),
                    self.size_spin.value(), self.height_spin.value(),
                    self._spacing(), self._bearing(),
                    self.offset_spin.value())
                text = tr('About {0:,} cells.').format(n)
            elif kind == 'lines':
                n = SG.count_strips(area, self._pattern(), start,
                                    self._bearing(),
                                    self.offset_spin.value())
                text = tr('About {0:,} strip lines.').format(n)
            else:
                n = SG.count_points_estimate(
                    area, self.column_spin.value(), self.row_spin.value(),
                    start, self._bearing(), self.offset_spin.value())
                text = tr('Up to {0:,} points.').format(n)
            if n > SG.MAX_FEATURES:
                self._say(tr('That is too many. Use a bigger spacing or a '
                             'smaller area.'), True)
            else:
                self._say(text + ' ' + tr('Made in {0}.').format(
                    area.crs.authid()))
        except SG.StripGridError as exc:
            self._say(str(exc), True)
        except Exception as exc:                # never break the form
            self._say(str(exc), True)

    # --------------------------------------------------------------- create

    def create(self):
        try:
            area = self._get_area(fresh=True)
            kind = self._kind()
            start = self.start_combo.currentData()
            bearing = self._bearing()
            inset = self.offset_spin.value()
            name = self._name()
            out_path = ''
            if self.file_radio.isChecked():
                out_path = self.file_widget.filePath().strip()
                if not out_path:
                    raise SG.StripGridError('Choose a file to save to, or '
                                            'pick Temporary layer.')
                if not out_path.lower().endswith('.gpkg'):
                    out_path += '.gpkg'
            made = []                       # (layer, kind) in the order to add
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                centres = []
                if kind == 'lines':
                    items = SG.make_strips(area, self._pattern(), start,
                                           bearing, inset)
                    layer_kind, fields = 'lines', SG.line_fields()
                elif kind == 'points':
                    items = SG.make_points(
                        area, self.column_spin.value(),
                        self.row_spin.value(), start, bearing, inset)
                    layer_kind, fields = 'points', SG.point_fields()
                else:
                    items, centres = SG.make_shapes(
                        area, self.shape_combo.currentData(),
                        self.size_spin.value(), self.height_spin.value(),
                        self.sides_spin.value(), self._spacing(), bearing,
                        inset, self.keep_combo.currentData())
                    layer_kind, fields = 'polygons', SG.shape_fields()
                if not items:
                    raise SG.StripGridError(
                        'Nothing fits in that area. Use a smaller spacing.')
                layer = SG.build_layer(name, layer_kind, area.crs, fields,
                                       items)
                if out_path:
                    layer = self._saved(layer, out_path, name)
                made.append((layer, layer_kind))
                if kind == 'shapes' and self.centres_check.isChecked():
                    centre_name = name + ' centres'
                    points = SG.build_layer(centre_name, 'points', area.crs,
                                            SG.point_fields(), centres)
                    if out_path:
                        points = self._saved(points, out_path, centre_name)
                    made.append((points, 'centres'))
            finally:
                QApplication.restoreOverrideCursor()
            for layer, layer_kind in made:
                self._style(layer, layer_kind)
                QgsProject.instance().addMapLayer(layer)
            noun = {'lines': 'strip lines', 'points': 'points',
                    'polygons': 'cells'}[made[0][1]]
            text = tr('Made {0:,} {1} in "{2}" ({3}).').format(
                len(items), noun, name, area.crs.authid())
            if len(made) > 1:
                text += ' ' + tr('Centre points added.')
            self._say(text)
        except SG.StripGridError as exc:
            self._say(str(exc), True)
        except Exception as exc:
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            self._say(tr('Failed: {0}').format(exc), True)

    @staticmethod
    def _saved(layer, path, name):
        saved = save_layer(layer, path, name)
        if not saved.isValid():
            raise SG.StripGridError(
                'The file was written but could not be opened: ' + path)
        return saved

    @staticmethod
    def _style(layer, kind):
        if kind == 'polygons':
            symbol = QgsFillSymbol.createSimple({
                'color': '0,0,0,0', 'outline_color': SHAPE_COLOUR,
                'outline_width': '0.4'})
        else:
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            if kind == 'lines':
                symbol.setColor(QColor(STRIP_COLOUR))
                symbol.setWidth(0.6)
            elif kind == 'centres':
                symbol.setColor(QColor(CENTRE_COLOUR))
                symbol.setSize(2.0)
            else:
                symbol.setColor(QColor(POINT_COLOUR))
                symbol.setSize(2.4)
        layer.renderer().setSymbol(symbol)

    # ---------------------------------------------------- direction from map

    def _pick_toggled(self, checked, mode, other):
        if not checked:
            self._end_pick()
            return
        other.blockSignals(True)
        other.setChecked(False)
        other.blockSignals(False)
        self._begin_pick(mode)

    def _begin_pick(self, mode):
        if self.canvas is None:
            self._end_pick()
            return
        self._drop_angle_tool()
        self._pick_previous = self.canvas.mapTool()
        tool = AngleMapTool(self.canvas, self, mode)
        tool.message.connect(self._say)
        self._angle_tool = tool
        self.canvas.setMapTool(tool)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        self._angle_prompt = (
            tr('Click an edge on the map and the lines will run parallel to '
               'it. Esc goes back.') if mode == ANGLE_EDGE else
            tr('Click the two ends of the line the strips should run along. '
               'Esc goes back.'))
        self._say(self._angle_prompt)

    def _drop_angle_tool(self):
        tool, self._angle_tool = self._angle_tool, None
        if tool is None:
            return
        try:
            if self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
            tool.dispose()
            tool.deleteLater()
        except RuntimeError:                # already destroyed by Qt
            pass

    def _end_pick(self, restore=True):
        for button in (self.edge_button, self.draw_button):
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
        picking = self._angle_tool is not None
        self._drop_angle_tool()
        previous, self._pick_previous = self._pick_previous, None
        if picking and restore and previous is not None:
            try:
                self.canvas.setMapTool(previous)
            except RuntimeError:            # the old tool was deleted
                pass

    def _map_tool_set(self, new_tool, _old=None):
        """Something else took the canvas: the pick is over."""
        if self._angle_tool is not None and new_tool is not self._angle_tool:
            self._end_pick(restore=False)

    def _bearing_of(self, start, end):
        try:
            return SG.bearing_between(
                start, end, self.canvas.mapSettings().destinationCrs())
        except Exception:
            return None

    # What AngleMapTool asks of its controller.

    def angle_layers(self):
        wanted = (Qgis.GeometryType.Line, Qgis.GeometryType.Polygon)
        layers = []
        current = self.area_picker.layer_combo.currentLayer()
        if isinstance(current, QgsVectorLayer) and \
                current.geometryType() in wanted:
            layers.append(current)
        for layer in self.canvas.layers():
            if isinstance(layer, QgsVectorLayer) and layer not in layers \
                    and layer.geometryType() in wanted:
                layers.append(layer)
        return layers

    def angle_hovered(self, start, end):
        bearing = None if start is None else self._bearing_of(start, end)
        if bearing is None:
            self._say(self._angle_prompt)
        else:
            self._say(tr('{0:.2f} \u00b0. Click to use it.').format(bearing))

    def angle_chosen(self, start, end):
        bearing = self._bearing_of(start, end)
        if bearing is None:
            self._say(tr('That gives no direction.'), True)
            return
        self.direction_combo.setCurrentIndex(
            self.direction_combo.findData('custom'))
        self.angle_spin.setValue(bearing)
        self._end_pick()
        self._say(tr('Direction set to {0:.2f} \u00b0 from the map.')
                  .format(bearing))

    def angle_cancelled(self):
        self._end_pick()
        self._say('')

    # ------------------------------------------------------------- teardown

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self):
        """Give the canvas back and drop the drawn outline."""
        if self._torn_down:
            return
        self._torn_down = True
        self._timer.stop()
        self._end_pick(restore=False)
        if self.canvas is not None:
            try:
                self.canvas.mapToolSet.disconnect(self._map_tool_set)
            except (TypeError, RuntimeError):
                pass
        try:
            self.area_picker.teardown()
        except RuntimeError:                # already destroyed by Qt
            pass

"""
The small Quick Labeling window: field (or expression), font, style, size, colour, halo and drop shadow.

Every change is applied to the layer straight away; there is no OK button. The window belongs to
one layer at a time (the active layer) and is moved or closed by the controller when that changes.
"""

from qgis.core import QgsProject, QgsSettings
from qgis.gui import QgsColorButton, QgsFieldExpressionWidget
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFontComboBox,
    QGridLayout,
    QLabel,
    QWidget,
)

from ...gui.window_reveal import revealed_preview
from ..layer_list import CurrentMapLayerComboBox
from ..quicksymbol.widgets import ToggleSwitch, switch_label
from .engine import (
    SETTINGS,
    DECIMALS,
    DEFAULT_HALO_COLOR,
    DEFAULT_HALO_SIZE,
    AREA_UNITS,
    DEFAULT_AREA_UNIT,
    DEFAULT_LENGTH_UNIT,
    DEFAULT_STYLE,
    geometry_kind,
    LENGTH_IDS,
    LENGTH_UNITS,
    is_numeric,
    label_expression,
    offset_applies,
    xy_applies,
    positions,
    units_allowed,
)

TITLE = "Quick Labeling"
LABEL_COLUMN = 84          # the Layer box and the labels of the body line up on this width
SHOW_LAYER_KEY = SETTINGS + "show_layer"


def font_styles(family):
    """The style names of a font family ('Regular', 'Bold', ...), always at least 'Regular'."""
    try:
        styles = list(QFontDatabase.styles(family))          # Qt6: static
    except TypeError:
        styles = list(QFontDatabase().styles(family))        # Qt5: instance method
    return styles or [DEFAULT_STYLE]


@revealed_preview
class QuickLabelDialog(QDialog):
    """Field | Font Name | Font Style | Font Size + Colour | Halo + Size + Colour | Position, applied live."""

    changed = pyqtSignal(str, bool, dict)          # field or expression, is expression, style
    layer_chosen = pyqtSignal(object)              # the user picked a layer in the Layer list

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("KgaQuickLabelDialog")
        self.setWindowTitle(TITLE)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.layer = None
        self._building = False
        self._source = None            # the plain numeric field the round / unit boxes act on (None: no boxes)

        self.layer_box = CurrentMapLayerComboBox(self)     # follows the layer panel; any layer of the map can be picked
        self.layer_box.setToolTip("The layer to label. It follows the layer selected in the Layers panel; pick "
                                  "another layer here to quick-label it without selecting it there")
        self.layer_check = ToggleSwitch(self)               # shows / hides the Layer list
        self.layer_check.setText("Layer")
        self.layer_check.setToolTip("Show the Layer list. Turn it off to hide it: the window still follows the layer "
                                    "selected in the Layers panel")
        self.layer_check.setChecked(QgsSettings().value(SHOW_LAYER_KEY, True, type=bool))
        self.layer_box.setVisible(self.layer_check.isChecked())
        self.hint = QLabel(self)                           # why the window below is greyed out
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(mid);")
        self.hint.hide()
        self.body = QWidget(self)                          # everything under the Layer row
        self.field = QgsFieldExpressionWidget(self)
        self.field.setToolTip("Attribute field to label with, or click the expression button to write one")
        self.number_row = QWidget(self)                    # only while the chosen field is numeric
        self.round_check = QCheckBox("Round to {} decimals".format(DECIMALS), self.number_row)
        self.round_check.setToolTip("Show the number rounded to {} decimals".format(DECIMALS))
        self.thousands_check = QCheckBox("Thousand separator", self.number_row)
        self.thousands_check.setToolTip("Group the digits with commas: 9999.00 becomes 9,999.00")
        self.length_check = QCheckBox("Add length unit", self.number_row)
        self.length_check.setToolTip("Convert the number from metres to a length unit and put it after the number")
        self.length_box = QComboBox(self.number_row)
        for unit_id, text in LENGTH_UNITS:
            self.length_box.addItem(text, unit_id)
        self.length_box.setToolTip("Length unit to add (m by default)")
        self.length_box.setEnabled(False)                  # only while 'Add length unit' is ticked
        self.area_check = QCheckBox("Add area unit", self.number_row)
        self.area_check.setToolTip("Convert the number from square metres to an area unit and put it after the number")
        self.area_box = QComboBox(self.number_row)
        for unit_id, text in AREA_UNITS:
            self.area_box.addItem(text, unit_id)
        self.area_box.setToolTip("Area unit to add (m\u00b2 by default)")
        self.area_box.setEnabled(False)                    # only while 'Add area unit' is ticked
        numbers = QGridLayout(self.number_row)
        numbers.setContentsMargins(0, 0, 0, 0)
        numbers.setHorizontalSpacing(14)
        numbers.setVerticalSpacing(4)
        numbers.addWidget(self.round_check, 0, 0, 1, 2)
        numbers.addWidget(self.thousands_check, 1, 0, 1, 2)
        numbers.addWidget(self.length_check, 2, 0)
        numbers.addWidget(self.length_box, 2, 1)
        numbers.addWidget(self.area_check, 3, 0)
        numbers.addWidget(self.area_box, 3, 1)
        self.number_row.hide()
        self.font_box = QFontComboBox(self)
        self.font_box.setToolTip("Font name")
        self.style_box = QComboBox(self)
        self.style_box.setToolTip("Font style (default: Regular)")
        self.size_box = QDoubleSpinBox(self)
        self.size_box.setRange(1.0, 200.0)
        self.size_box.setDecimals(1)
        self.size_box.setSingleStep(0.5)
        self.size_box.setSuffix(" pt")
        self.size_box.setToolTip("Font size")
        self.color_button = QgsColorButton(self)
        self.color_button.setAllowOpacity(True)
        self.color_button.setToolTip("Label colour")
        self.halo_check = QCheckBox("Halo", self)
        self.halo_check.setToolTip("Draw an outline around the text so it stays readable on a busy map")
        self.halo_size = QDoubleSpinBox(self)
        self.halo_size.setRange(0.1, 20.0)
        self.halo_size.setDecimals(1)
        self.halo_size.setSingleStep(0.5)
        self.halo_size.setSuffix(" pt")
        self.halo_size.setToolTip("Halo size")
        self.halo_color = QgsColorButton(self)
        self.halo_color.setAllowOpacity(True)
        self.halo_color.setColor(QColor(DEFAULT_HALO_COLOR))
        self.halo_color.setToolTip("Halo colour (white by default)")

        self.position_label = QLabel("Position", self)
        self.position_box = QComboBox(self)                # the choices depend on point / line / polygon
        self.position_box.setToolTip("Where the label sits relative to the feature")

        self.offset_label = QLabel("Offset", self)
        self.offset_box = QDoubleSpinBox(self)
        self.offset_box.setRange(0.0, 200.0)
        self.offset_box.setDecimals(1)
        self.offset_box.setSingleStep(0.5)
        self.offset_box.setSuffix(" pt")
        self.offset_box.setToolTip("Distance between the label and the feature (not used by every position)")

        self.offset_x_label = QLabel("Offset X", self)       # point layers only
        self.offset_x_box = QDoubleSpinBox(self)
        self.offset_y_label = QLabel("Y", self)
        self.offset_y_box = QDoubleSpinBox(self)
        for box, tip in ((self.offset_x_box, "Move the label right (positive) or left (negative)"),
                         (self.offset_y_box, "Move the label up (positive) or down (negative)")):
            box.setRange(-200.0, 200.0)
            box.setDecimals(1)
            box.setSingleStep(0.5)
            box.setSuffix(" pt")
            box.setToolTip(tip)

        self.shadow_row = QWidget(self)
        shadow_lay = QHBoxLayout(self.shadow_row)
        shadow_lay.setContentsMargins(0, 0, 0, 0)
        self.shadow_check = ToggleSwitch(self.shadow_row)
        self.shadow_check.setText("Drop shadow")
        self.shadow_check.setToolTip("Draw a soft shadow under the label")
        shadow_lay.addWidget(self.shadow_check)
        shadow_lay.addWidget(switch_label("Drop shadow", self.shadow_check, self.shadow_row))
        shadow_lay.addStretch(1)
        self.layer_row = QWidget(self)
        layer_lay = QHBoxLayout(self.layer_row)
        layer_lay.setContentsMargins(0, 0, 0, 0)
        layer_lay.addWidget(self.layer_check)
        layer_lay.addWidget(switch_label("Layer", self.layer_check, self.layer_row))
        layer_lay.addStretch(1)

        grid = QGridLayout(self.body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnMinimumWidth(0, LABEL_COLUMN)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.addWidget(QLabel("Field", self), 0, 0)
        grid.addWidget(self.field, 0, 1, 1, 3)
        grid.addWidget(self.number_row, 1, 1, 1, 3)
        grid.addWidget(QLabel("Font", self), 2, 0)
        grid.addWidget(self.font_box, 2, 1, 1, 3)
        grid.addWidget(QLabel("Style", self), 3, 0)
        grid.addWidget(self.style_box, 3, 1, 1, 3)
        grid.addWidget(QLabel("Size", self), 4, 0)
        grid.addWidget(self.size_box, 4, 1)
        grid.addWidget(QLabel("Color", self), 4, 2)
        grid.addWidget(self.color_button, 4, 3)
        grid.addWidget(self.halo_check, 5, 0)
        grid.addWidget(self.halo_size, 5, 1)
        grid.addWidget(QLabel("Color", self), 5, 2)
        grid.addWidget(self.halo_color, 5, 3)
        grid.addWidget(self.position_label, 6, 0)
        grid.addWidget(self.position_box, 6, 1, 1, 3)
        grid.addWidget(self.offset_label, 7, 0)
        grid.addWidget(self.offset_box, 7, 1)
        grid.addWidget(self.offset_x_label, 8, 0)
        grid.addWidget(self.offset_x_box, 8, 1)
        grid.addWidget(self.offset_y_label, 8, 2)
        grid.addWidget(self.offset_y_box, 8, 3)
        grid.addWidget(self.shadow_row, 9, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        outer = QGridLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setHorizontalSpacing(8)
        outer.setVerticalSpacing(6)
        outer.setColumnMinimumWidth(0, LABEL_COLUMN)
        outer.setColumnStretch(1, 1)
        outer.addWidget(self.layer_row, 0, 0)
        outer.addWidget(self.layer_box, 0, 1)
        outer.addWidget(self.hint, 1, 0, 1, 2)
        outer.addWidget(self.body, 2, 0, 1, 2)
        self.setMinimumWidth(330)
        self.layer_box.activated.connect(self._layer_picked)
        self.layer_check.toggled.connect(self._layer_list_toggled)

        self.field.fieldChanged.connect(self._field_changed)
        self.round_check.toggled.connect(self._options_changed)
        self.thousands_check.toggled.connect(self._options_changed)
        self.length_check.toggled.connect(lambda on: self._unit_toggled(self.length_check, on))
        self.length_box.currentIndexChanged.connect(self._options_changed)
        self.area_check.toggled.connect(lambda on: self._unit_toggled(self.area_check, on))
        self.area_box.currentIndexChanged.connect(self._options_changed)
        self.font_box.currentFontChanged.connect(self._font_changed)
        self.style_box.currentIndexChanged.connect(self._emit)
        self.size_box.valueChanged.connect(self._emit)
        self.color_button.colorChanged.connect(self._emit)
        self.position_box.currentIndexChanged.connect(self._position_changed)
        self.offset_box.valueChanged.connect(self._emit)
        self.offset_x_box.valueChanged.connect(self._emit)
        self.offset_y_box.valueChanged.connect(self._emit)
        self.shadow_check.toggled.connect(self._emit)
        self.halo_check.toggled.connect(self._halo_toggled)
        self.halo_size.valueChanged.connect(self._emit)
        self.halo_color.colorChanged.connect(self._emit)
        self._halo_enabled(False)
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_removed)

    # ------------------------------------------------------------------ binding
    def _show_layer(self, layer):
        """Put `layer` in the Layer list without that counting as the user picking it."""
        self.layer_box.show_layer(layer)

    def _resize(self):
        """Fit the window to what is shown. Widgets hidden inside the body only reach the outer layout on the next
        pass of the event loop, so both layouts are brought up to date first."""
        self.body.layout().invalidate()
        self.body.layout().activate()
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

    def _layer_list_toggled(self, on):
        self.layer_box.setVisible(on)
        QgsSettings().setValue(SHOW_LAYER_KEY, bool(on))
        self._resize()

    def _layer_picked(self, _index):
        layer = self.layer_box.currentLayer()
        if layer is not None:
            self.layer_chosen.emit(layer)

    def bind_empty(self, layer, message):
        """Show `layer` with nothing to edit: the settings are greyed out and `message` says why."""
        self._building = True
        try:
            self.layer = layer
            self.field.setLayer(layer)
        finally:
            self._building = False
        self._show_layer(layer)
        self.body.setEnabled(False)
        self.hint.setText(message)
        self.hint.show()
        self._resize()

    def bind(self, layer, field, is_expression, style):
        """Show the quick label of `layer` (no change is written to the layer by this)."""
        self.body.setEnabled(True)
        if not self.hint.isHidden():
            self.hint.hide()
            self._resize()
        self._show_layer(layer)
        self._building = True
        try:
            self.layer = layer
            self.field.setLayer(layer)
            self.field.setField(field)
            self.font_box.setCurrentFont(QFont(style["family"]))
            self._fill_styles(style["style"])
            self.size_box.setValue(float(style["size"]))
            self.color_button.setColor(QColor(style["color"]))
            self.shadow_check.setChecked(bool(style.get("shadow", False)))
            self.halo_check.setChecked(bool(style.get("halo", False)))
            self.halo_size.setValue(float(style.get("halo_size", DEFAULT_HALO_SIZE)))
            self.halo_color.setColor(QColor(style.get("halo_color", DEFAULT_HALO_COLOR)))
            self._halo_enabled(self.halo_check.isChecked())
            self._fill_positions(layer, style.get("position", ""))
            self.offset_box.setValue(float(style.get("offset", 0.0) or 0.0))
            self.offset_x_box.setValue(float(style.get("offset_x", 0.0) or 0.0))
            self.offset_y_box.setValue(float(style.get("offset_y", 0.0) or 0.0))
            self._offset_enabled()
            unit = style.get("unit", "")
            self.round_check.setChecked(bool(style.get("round", False)))
            self.thousands_check.setChecked(bool(style.get("thousands", False)))
            is_length = unit in LENGTH_IDS
            self.length_box.setCurrentIndex(max(0, self.length_box.findData(unit if is_length else DEFAULT_LENGTH_UNIT)))
            self.length_check.setChecked(is_length)
            self.area_box.setCurrentIndex(max(0, self.area_box.findData(DEFAULT_AREA_UNIT if is_length else unit)))
            self.area_check.setChecked(bool(unit) and not is_length)
            self._sync_unit_boxes()
            self._source = field if self._numeric_field(field, is_expression) else None
            if self._source is not None and self._options_on():
                self._show_expression()                  # the Field box (and the expression button) hold it
            self._update_number_row()
        finally:
            self._building = False

    def style_now(self):
        return {"family": self.font_box.currentFont().family(),
                "style": self.style_box.currentText() or DEFAULT_STYLE,
                "size": self.size_box.value(),
                "color": self.color_button.color().name(QColor.NameFormat.HexArgb),
                "shadow": self.shadow_check.isChecked(),
                "halo": self.halo_check.isChecked(),
                "halo_size": self.halo_size.value(),
                "halo_color": self.halo_color.color().name(QColor.NameFormat.HexArgb),
                "position": self.position_box.currentData() or "",
                "offset": self.offset_box.value(),
                "offset_x": self.offset_x_box.value(),
                "offset_y": self.offset_y_box.value(),
                "round": self._options()[0],
                "unit": self._options()[1],
                "thousands": self._options()[2]}

    def values_now(self):
        """(field or expression, is expression, style) as shown."""
        text, is_expression, _valid = self.field.currentField()
        return text, bool(is_expression), self.style_now()

    # ------------------------------------------------------------------ internals
    def _fill_styles(self, wanted):
        family = self.font_box.currentFont().family()
        names = font_styles(family)
        self.style_box.blockSignals(True)
        self.style_box.clear()
        self.style_box.addItems(names)
        pick = names.index(wanted) if wanted in names else (names.index(DEFAULT_STYLE) if DEFAULT_STYLE in names else 0)
        self.style_box.setCurrentIndex(pick)
        self.style_box.blockSignals(False)

    def _fill_positions(self, layer, wanted):
        """The Position choices of this layer's geometry (the row is hidden when it has none)."""
        choices = positions(layer)
        self.position_box.blockSignals(True)
        self.position_box.clear()
        for pid, text in choices:
            self.position_box.addItem(text, pid)
        index = self.position_box.findData(wanted)
        self.position_box.setCurrentIndex(index if index >= 0 else 0)
        self.position_box.blockSignals(False)
        show = bool(choices)
        if show == self.position_box.isHidden():
            for widget in (self.position_label, self.position_box, self.offset_label, self.offset_box):
                widget.setVisible(show)
            self._resize()
        point = geometry_kind(layer) == "point"
        shown = (self.offset_x_label, self.offset_x_box, self.offset_y_label, self.offset_y_box)
        if point == shown[0].isHidden():
            for widget in shown:
                widget.setVisible(point)
            self._resize()

    def _offset_enabled(self):
        """The offset only matters for positions that have a distance from the feature."""
        position = self.position_box.currentData() or ""
        self.offset_box.setEnabled(offset_applies(self.layer, position))
        for widget in (self.offset_x_box, self.offset_y_box):
            widget.setEnabled(xy_applies(self.layer, position))

    def _position_changed(self, *_args):
        self._offset_enabled()
        self._emit()

    # ------------------------------------------------------------------ number options
    def _options(self):
        """(round, unit id, thousand separator) as ticked."""
        unit = (self.length_box.currentData() if self.length_check.isChecked()
                else (self.area_box.currentData() if self.area_check.isChecked() else ""))
        return self.round_check.isChecked(), unit, self.thousands_check.isChecked()

    def _options_on(self):
        return any(self._options())

    def _numeric_field(self, text, is_expression):
        return bool(text and not is_expression and self.layer is not None and not sip.isdeleted(self.layer)
                    and is_numeric(self.layer, text))

    def _set_field_box(self, text):
        """Write into the Field box without that counting as the user picking something."""
        was, self._building = self._building, True
        try:
            self.field.setField(text)
        finally:
            self._building = was

    def _show_expression(self):
        """Put the expression the ticked boxes stand for into the Field box, so the expression button opens
        with it; with nothing ticked the plain field goes back."""
        if self._source is None:
            return
        options = self._options()
        self._set_field_box(label_expression(self._source, *options) if any(options) else self._source)

    def _clear_options(self):
        for box in (self.round_check, self.thousands_check, self.length_check, self.area_check):
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)
        self._sync_unit_boxes()

    def _sync_unit_boxes(self):
        """A unit drop-down is enabled only while its own box is ticked, and a box only fits some layers: a line
        has no area and a polygon no length. A box that is ticked stays enabled so it can always be unticked."""
        length_ok, area_ok = units_allowed(self.layer)
        self.length_check.setEnabled(length_ok or self.length_check.isChecked())
        self.area_check.setEnabled(area_ok or self.area_check.isChecked())
        self.length_box.setEnabled(self.length_check.isChecked())
        self.area_box.setEnabled(self.area_check.isChecked())
        self.length_check.setToolTip("Convert the number from metres to a length unit and put it after the number"
                                     if length_ok else "A polygon or point layer has no length to add")
        self.area_check.setToolTip("Convert the number from square metres to an area unit and put it after the number"
                                   if area_ok else "A line or point layer has no area to add")

    def _update_number_row(self):
        """The round / unit boxes show while a plain numeric field is the source."""
        show = self._source is not None
        if show == self.number_row.isHidden():
            self.number_row.setVisible(show)
            self._resize()

    def _options_changed(self, *_args):
        """A box was ticked or unticked: the expression goes into (or out of) the Field box."""
        if self._building:
            return
        self._show_expression()
        self._update_number_row()
        self._emit()

    def _unit_toggled(self, box, on):
        """A length unit and an area unit exclude each other."""
        if on and not self._building:
            other = self.area_check if box is self.length_check else self.length_check
            other.blockSignals(True)
            other.setChecked(False)
            other.blockSignals(False)
        self._sync_unit_boxes()
        self._options_changed()

    def _field_changed(self, *_args):
        """The user picked a field or edited the expression in the Field box."""
        if self._building:
            return
        text, is_expression, _valid = self.field.currentField()
        options = self._options()
        if self._numeric_field(text, is_expression):           # a plain numeric field: the boxes follow it
            self._source = text
            if any(options):
                self._show_expression()
        elif (is_expression and self._source is not None and any(options)
              and text == label_expression(self._source, *options)):
            pass                                               # the expression builder gave our own expression back
        else:
            if is_expression:                                  # an expression of their own: the boxes let go of it
                self._clear_options()
            self._source = None
        self._update_number_row()
        self._emit()

    def _halo_enabled(self, on):
        """The halo size and colour only matter while the Halo box is ticked."""
        self.halo_size.setEnabled(on)
        self.halo_color.setEnabled(on)

    def _halo_toggled(self, on):
        self._halo_enabled(on)
        self._emit()

    def _font_changed(self, _font):
        if self._building:
            return
        self._fill_styles(self.style_box.currentText())        # keep the style if the new font has it
        self._emit()

    def _emit(self, *_args):
        if self._building or self.layer is None or sip.isdeleted(self.layer):
            return
        text, is_expression, valid = self.field.currentField()
        if not text or not valid:                          # half-typed expression: wait until it is valid
            return
        if self._source is not None and self._options_on():
            text, is_expression = self._source, False       # the engine builds (and remembers) the expression
        self.changed.emit(text, bool(is_expression), self.style_now())

    def _layers_removed(self, removed):
        ids = [item if isinstance(item, str) else item.id() for item in removed]
        if self.layer is not None and not sip.isdeleted(self.layer) and self.layer.id() in ids:
            self.layer = None
            self.hide()

    def disconnect_all(self):
        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self._layers_removed)
        except (TypeError, RuntimeError):
            pass

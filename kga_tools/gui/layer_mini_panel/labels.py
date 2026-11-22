# -*- coding: utf-8 -*-
"""Labels section.

Every edit follows the same safe path:

    copy = QgsPalLayerSettings(current simple settings)   # full copy
    change exactly one property on the copy
    layer.setLabeling(QgsVectorLayerSimpleLabeling(copy))

so data-defined overrides, callouts, background, shadow, masks, rendering rules
and anything else configured in Layer Properties stay untouched.

* "Labels off" only calls layer.setLabelsEnabled(False); the configuration stays.
* Rule-based (or any non-simple) labeling is never converted: the section shows
  its state, lets you switch it on/off, and offers the native editor.
* Rapid changes (spin boxes, colour dragging) are coalesced into one commit.
"""

import json
from contextlib import suppress

from qgis.core import (QgsProperty, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeature,
                       QgsFeatureRequest, QgsPalLayerSettings, QgsProject, QgsTextFormat, QgsUnitTypes,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, Qgis)
from qgis.gui import QgsColorButton, QgsFieldExpressionWidget
from qgis.PyQt.QtCore import QSize, QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFontComboBox, QFormLayout, QFrame,
                                 QGroupBox, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                                 QToolButton, QVBoxLayout, QWidget)

from ...core.compat import scoped
from ...editor.quicklabel import engine as QE
from . import compat as C
from .ui_common import section_label

MODE_NONE, MODE_SIMPLE, MODE_OTHER = "none", "simple", "other"

# (label, enum name) per geometry family. Enum resolved at runtime for old/new QGIS.
PLACEMENTS = {
    C.GEOM_POINT: [("Around point", "AroundPoint"), ("Offset from point", "OverPoint"),
                   ("Cartographic", "OrderedPositionsAroundPoint")],
    C.GEOM_LINE: [("Parallel", "Line"), ("Curved", "Curved"), ("Horizontal", "Horizontal")],
    C.GEOM_POLYGON: [("Offset from centroid", "OverPoint"), ("Around centroid", "AroundPoint"),
                     ("Horizontal", "Horizontal"), ("Free (angled)", "Free"), ("Using perimeter", "Line"),
                     ("Curved perimeter", "PerimeterCurved"), ("Outside polygons", "OutsidePolygons")],
}
DEFAULT_PLACEMENT = {C.GEOM_POINT: "AroundPoint", C.GEOM_LINE: "Line", C.GEOM_POLYGON: "OverPoint"}

COMMIT_DELAY_MS = 120
SELECTED_EXPR = "is_selected()"
PREV_SHOW_KEY = "LayerMiniPanel/previous_show_property"
SHOW_OFF, SHOW_ALL, SHOW_SELECTED = "off", "all", "selected"


def _show_prop_key():
    return C.enum_value(QgsPalLayerSettings, "Property", "Show")


def shows_selected_only(settings):
    """True when the labels are limited to selected features by this panel's rule."""
    try:
        props = settings.dataDefinedProperties()
        key = _show_prop_key()
        if not props.isActive(key):
            return False
        expr = props.property(key).expressionString()
        return expr.replace(" ", "").lower() == SELECTED_EXPR
    except Exception:
        return False


def show_mode(layer):
    if layer is None or layer.labeling() is None or not layer.labelsEnabled():
        return SHOW_OFF
    if layer.labeling().type() == "simple":
        s = QgsPalLayerSettings(layer.labeling().settings())
        if shows_selected_only(s):
            return SHOW_SELECTED
    return SHOW_ALL


def placement_value(name):
    v = C.enum_value(Qgis, "LabelPlacement", name) if hasattr(Qgis, "LabelPlacement") else None
    if v is None:
        v = getattr(QgsPalLayerSettings, name, None)
    return v


def labeling_mode(layer):
    lab = layer.labeling() if layer is not None else None
    if lab is None:
        return MODE_NONE
    return MODE_SIMPLE if lab.type() == "simple" else MODE_OTHER


def unit_abbrev(unit):
    try:
        return QgsUnitTypes.toAbbreviatedString(unit) or ""
    except Exception:
        return ""


def default_label_field(layer):
    """Display expression field if any, else the first text field, else the first field."""
    fields = layer.fields()
    with suppress(Exception):
        disp = layer.displayField()
        if disp and fields.indexOf(disp) >= 0:
            return disp
    for i in range(fields.count()):
        f = fields.at(i)
        if f.type() == 10 or "char" in f.typeName().lower() or "text" in f.typeName().lower() \
                or "string" in f.typeName().lower():
            return f.name()
    return fields.at(0).name() if fields.count() else ""


class LabelsSection(QWidget):
    """``extended`` adds what Quick Labeling offers beyond the Layer Bag's basics (the Label Bag turns it
    on): drop shadow, Quick Labeling's positions with an offset and X / Y, and number options (round,
    thousands separator, unit)."""

    def __init__(self, iface, watcher, parent=None, extended=False):
        super().__init__(parent)
        self.extended = bool(extended)
        self.on_layout_changed = None    # the dock's request_fit: rows came or went
        self._source = None      # the plain numeric field the number options act on
        self.iface = iface
        self.watcher = watcher
        self.layer = None
        self.mode = MODE_NONE
        self.family = C.GEOM_UNKNOWN
        self._loading = False
        self._pending = []       # queued mutators (callables taking QgsPalLayerSettings)
        self._sample = None      # (expression text, sample label text)
        self.stats = dict(commits=0)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(COMMIT_DELAY_MS)
        self._timer.timeout.connect(self.flush)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        body.setObjectName("labelsBody")
        body.setMinimumWidth(0)
        if self.extended:                  # the Label Bag grows and shrinks to its content: nothing scrolls
            outer.addWidget(body)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(C.ScrollBarAlwaysOff)
            outer.addWidget(scroll)
            scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(8)

        # Off / All / Selected
        top = QHBoxLayout()
        top.setSpacing(4)
        self.enable = QCheckBox("Show labels")   # internal on/off state (driven by the switch)
        self.enable.hide()
        show_lbl = QLabel("Labels")
        show_lbl.setObjectName("sectionTitle")
        top.addWidget(show_lbl)
        self.show_buttons = {}
        for key, text, tip in ((SHOW_OFF, "Off", "No labels (the settings are kept)"),
                               (SHOW_ALL, "All", "Label every feature"),
                               (SHOW_SELECTED, "Selected", "Label only the selected features")):
            b = QToolButton()
            b.setObjectName("pill")
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, k=key: self.set_show_mode(k))
            self.show_buttons[key] = b
            top.addWidget(b)
        top.addStretch(1)
        self.native_btn = QToolButton()
        self.native_btn.setText("More…")
        self.native_btn.setToolTip("Open this layer's labeling page in Layer Properties")
        self.native_btn.setAutoRaise(True)
        self.native_btn.clicked.connect(self.open_native)
        top.addWidget(self.native_btn)
        lay.addLayout(top)

        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        lay.addWidget(self.notice)

        # preview
        self.preview = QLabel()
        self.preview.setObjectName("card")
        self.preview.setMinimumHeight(44)
        self.preview.setAlignment(C.AlignCenter)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored,
                                   QSizePolicy.Policy.Fixed)
        lay.addWidget(self.preview)

        # controls container (disabled for non-simple labeling)
        self.controls = QWidget()
        cl = QVBoxLayout(self.controls)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        lay.addWidget(self.controls)

        cl.addWidget(section_label("TEXT"))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.field = QgsFieldExpressionWidget()
        self.field.setAllowEmptyFieldName(False)
        self.field.setToolTip("Field or expression used for the label text (ε opens the expression builder)")
        form.addRow("Value", self.field)

        self.font = QFontComboBox()
        self.font.setMinimumContentsLength(8)
        self.font.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        form.addRow("Font", self.font)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.size = QDoubleSpinBox()
        self.size.setRange(0.1, 999)
        self.size.setDecimals(1)
        self.size.setSingleStep(0.5)
        self.size.setKeyboardTracking(False)
        self.size.setMinimumWidth(58)
        row.addWidget(self.size, 1)
        self.bold = QToolButton()
        self.bold.setText("B")
        self.bold.setCheckable(True)
        self.bold.setToolTip("Bold")
        f = self.bold.font()
        f.setBold(True)
        self.bold.setFont(f)
        self.italic = QToolButton()
        self.italic.setText("I")
        self.italic.setCheckable(True)
        self.italic.setToolTip("Italic")
        f = self.italic.font()
        f.setItalic(True)
        self.italic.setFont(f)
        self.color = QgsColorButton()
        self.color.setColorDialogTitle("Label text colour")
        self.color.setShowNoColor(False)
        self.color.setMinimumWidth(34)
        self.color.setToolTip("Text colour")
        for w in (self.bold, self.italic, self.color):
            row.addWidget(w)
        form.addRow("Size", row)
        cl.addLayout(form)

        # buffer
        self.buffer = QGroupBox("Text buffer (halo)")
        self.buffer.setCheckable(True)
        bl = QHBoxLayout(self.buffer)
        bl.setContentsMargins(8, 4, 8, 6)
        self.buf_size = QDoubleSpinBox()
        self.buf_size.setRange(0.0, 100)
        self.buf_size.setDecimals(2)
        self.buf_size.setSingleStep(0.2)
        self.buf_size.setKeyboardTracking(False)
        self.buf_color = QgsColorButton()
        self.buf_color.setColorDialogTitle("Buffer colour")
        self.buf_color.setShowNoColor(False)
        self.buf_color.setMinimumWidth(34)
        self.buf_size.setMinimumWidth(58)
        bl.addWidget(QLabel("Size"))
        bl.addWidget(self.buf_size, 1)
        bl.addWidget(self.buf_color)
        cl.addWidget(self.buffer)

        if self.extended:
            self._build_extended(cl)
        else:
            cl.addWidget(section_label("PLACEMENT"))
        self.placement = QComboBox()
        cl.addWidget(self.placement)
        if self.extended:
            self._build_offset(cl)

        if not self.extended:
            lay.addStretch(1)

        # signals
        self.enable.toggled.connect(self._on_enable)
        self.field.fieldChanged.connect(self._on_field)
        self.font.currentFontChanged.connect(lambda f: self._queue(self._set_family(f.family())))
        self.size.valueChanged.connect(lambda v: self._queue(self._set_size(v)))
        self.bold.toggled.connect(lambda b: self._queue(self._set_bold(b), now=True))
        self.italic.toggled.connect(lambda b: self._queue(self._set_italic(b), now=True))
        self.color.colorChanged.connect(lambda c: self._queue(self._set_color(c)))
        self.buffer.toggled.connect(lambda b: self._queue(self._set_buffer(enabled=b), now=True))
        self.buf_size.valueChanged.connect(lambda v: self._queue(self._set_buffer(size=v)))
        self.buf_color.colorChanged.connect(lambda c: self._queue(self._set_buffer(color=c)))
        self.placement.activated.connect(self._on_placement)
        if self.extended:
            self._connect_extended()

        self.set_layer(None)

    # ================================================================== extended controls (Label Bag)
    def _build_extended(self, cl):
        self.shadow = QCheckBox("Drop shadow")
        self.shadow.setToolTip("A soft black shadow behind the text")
        cl.addWidget(self.shadow)
        self.number_box = QGroupBox("Number")
        self.number_box.setToolTip("Only for a numeric field: round, thousands separator and a unit after it")
        nl = QVBoxLayout(self.number_box)
        nl.setContentsMargins(8, 4, 8, 6)
        nl.setSpacing(4)
        r1 = QHBoxLayout()
        self.round_check = QCheckBox("Round to {} decimals".format(QE.DECIMALS))
        self.thousands_check = QCheckBox("1,000 separator")
        r1.addWidget(self.round_check)
        r1.addWidget(self.thousands_check)
        r1.addStretch(1)
        nl.addLayout(r1)
        r2 = QHBoxLayout()
        self.unit_check = QCheckBox("Add unit")
        self.unit_check.setToolTip("Converts the number (a length field is taken to be in metres, an area in "
                                   "square metres) and writes the unit after it")
        self.unit_box = QComboBox()
        r2.addWidget(self.unit_check)
        r2.addWidget(self.unit_box, 1)
        nl.addLayout(r2)
        cl.addWidget(self.number_box)
        cl.addWidget(section_label("PLACEMENT"))

    def _build_offset(self, cl):
        self.offset_row = QWidget()
        ol = QHBoxLayout(self.offset_row)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(4)

        def spin(lo, hi, tip):
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setDecimals(1)
            sp.setSingleStep(0.5)
            sp.setKeyboardTracking(False)
            sp.setToolTip(tip)
            sp.setMinimumWidth(52)
            return sp
        self.offset_box = spin(0.0, 500.0, "Distance from the feature (points)")
        self.offset_x = spin(-500.0, 500.0, "Shift to the right (points)")
        self.offset_y = spin(-500.0, 500.0, "Shift up (points)")
        self.offset_x_label = QLabel("X")
        self.offset_y_label = QLabel("Y")
        ol.addWidget(QLabel("Offset"))
        ol.addWidget(self.offset_box, 1)
        ol.addWidget(self.offset_x_label)
        ol.addWidget(self.offset_x, 1)
        ol.addWidget(self.offset_y_label)
        ol.addWidget(self.offset_y, 1)
        cl.addWidget(self.offset_row)

    def _connect_extended(self):
        self.shadow.toggled.connect(lambda on: self._queue(self._set_shadow(on), now=True))
        for sp in (self.offset_box, self.offset_x, self.offset_y):
            sp.valueChanged.connect(self._on_offset)
        for cb in (self.round_check, self.thousands_check):
            cb.toggled.connect(self._on_number)
        self.unit_check.toggled.connect(self._on_number)
        self.unit_box.activated.connect(self._on_number)

    @staticmethod
    def _set_shadow(on):
        def m(s):
            fmt = s.format()
            fmt.setShadow(QE.shadow_settings(on, fmt.shadow()))
            s.setFormat(fmt)
        return m

    def _position_id(self):
        pid = self.placement.currentData()
        return pid if pid and pid != "__keep__" else ""

    def _sync_offset_state(self):
        if self.on_layout_changed is not None:
            self.on_layout_changed()
        """Offset / X / Y only where the chosen position can use them."""
        kind = QE.geometry_kind(self.layer)
        pid = self._position_id()
        ok = self.layer is not None and bool(pid) and QE.offset_applies(kind, pid)
        xy = self.layer is not None and bool(pid) and QE.xy_applies(kind, pid)
        self.offset_box.setEnabled(ok)
        for w in (self.offset_x, self.offset_y, self.offset_x_label, self.offset_y_label):
            w.setVisible(kind == "point")
            w.setEnabled(xy)

    def _write_position(self, s, pid):
        kind = QE.geometry_kind(self.layer)
        QE._set_position(s, kind, pid)
        QE._set_offset(s, kind, pid, self.offset_box.value(), self.offset_x.value(), self.offset_y.value())

    def _on_offset(self, _v=None):
        if self._loading or not self._position_id():
            return
        pid = self._position_id()
        self._queue(lambda s: QE._set_offset(s, QE.geometry_kind(self.layer), pid, self.offset_box.value(),
                                             self.offset_x.value(), self.offset_y.value()))

    # --- number options
    def _unit_choices(self):
        line, area = QE.units_allowed(self.layer) if self.layer is not None else (False, False)
        return (QE.LENGTH_UNITS if line else ()) + (QE.AREA_UNITS if area else ())

    def _fill_units(self, selected=""):
        self.unit_box.blockSignals(True)
        self.unit_box.clear()
        for uid, text in self._unit_choices():
            self.unit_box.addItem(text, uid)
        i = self.unit_box.findData(selected)
        self.unit_box.setCurrentIndex(max(0, i))
        self.unit_box.blockSignals(False)
        has = self.unit_box.count() > 0
        self.unit_check.setEnabled(has and self._source is not None)
        self.unit_box.setEnabled(has and self._source is not None and self.unit_check.isChecked())

    def _read_number(self, s):
        """Show the number options of the layer's label (the expression Quick Labeling / this bag built)."""
        saved = QE._saved_format(self.layer, s.fieldName, bool(s.isExpression)) if s is not None else None
        round_it, unit, thousands = False, "", False
        if saved is not None:
            self._source, round_it, unit, thousands = saved
        elif s is not None and not s.isExpression and QE.is_numeric(self.layer, s.fieldName):
            self._source = s.fieldName
        elif s is None and self.field.currentField()[0] and QE.is_numeric(self.layer, self.field.currentField()[0]):
            self._source = self.field.currentField()[0]
        else:
            self._source = None
        for w in (self.round_check, self.thousands_check, self.unit_check):
            w.blockSignals(True)
        self.round_check.setChecked(round_it)
        self.thousands_check.setChecked(thousands)
        self.unit_check.setChecked(bool(unit))
        for w in (self.round_check, self.thousands_check, self.unit_check):
            w.blockSignals(False)
        self._fill_units(unit)
        self.number_box.setVisible(self._source is not None)
        if saved is not None:                      # the box shows the expression, like Quick Labeling's window
            self.field.setExpression(s.fieldName)
        if self.on_layout_changed is not None:
            self.on_layout_changed()

    def _on_number(self, *_a):
        if self._loading or self.layer is None or self._source is None or self.mode == MODE_OTHER:
            return
        self.unit_box.setEnabled(self.unit_check.isChecked() and self.unit_box.count() > 0)
        round_it, thousands = self.round_check.isChecked(), self.thousands_check.isChecked()
        unit = self.unit_box.currentData() if self.unit_check.isChecked() else ""
        source, lyr = self._source, self.layer
        formatted = bool(round_it or thousands or unit)
        if formatted:
            lyr.setCustomProperty(QE.FORMAT_PROP, json.dumps({"field": source, "round": round_it, "unit": unit or "",
                                                              "thousands": thousands}))
            name, is_expr = QE.label_expression(source, round_it, unit or "", thousands), True
        else:
            lyr.removeCustomProperty(QE.FORMAT_PROP)
            name, is_expr = source, False
        self._loading = True
        try:
            self.field.setExpression(name) if is_expr else self.field.setField(name)
        finally:
            self._loading = False

        def m(s):
            s.fieldName = name
            s.isExpression = is_expr
        self._queue(m, now=True)

    # ================================================================== reading
    def set_layer(self, layer):
        self.flush()  # never lose a pending edit to the previous layer
        self.layer = layer if isinstance(layer, QgsVectorLayer) and layer.isSpatial() else None
        self.family = C.geometry_family(self.layer) if self.layer is not None else C.GEOM_UNKNOWN
        self._sample = None
        self.refresh()

    def refresh(self):
        """Read the layer's labeling into the controls (no writes)."""
        self._loading = True
        try:
            self._read()
        finally:
            self._loading = False

    def _read(self):
        lyr = self.layer
        self.native_btn.setEnabled(lyr is not None)
        if lyr is None:
            self.mode = MODE_NONE
            self.enable.setEnabled(False)
            self.enable.setChecked(False)
            self._show_buttons_state(SHOW_OFF, enabled=False)
            self.controls.setEnabled(False)
            self.notice.setText("Select a vector layer with geometry to manage its labels.")
            self.notice.show()
            self.preview.clear()
            return
        self.enable.setEnabled(True)
        self.mode = labeling_mode(lyr)
        self.enable.setChecked(bool(lyr.labelsEnabled()) and self.mode != MODE_NONE)
        self._show_buttons_state(show_mode(lyr) if self.mode != MODE_NONE else SHOW_OFF, enabled=True)
        self.field.setLayer(lyr)
        self._fill_placements()

        if self.mode == MODE_OTHER:
            kind = lyr.labeling().type()
            label = {"rule-based": "rule-based"}.get(kind, "“{}”".format(kind))
            self.notice.setText("This layer uses <b>{}</b> labeling. To keep its rules intact, edit them in the "
                                "full labeling settings. You can still switch labels on or off here.".format(label))
            self.notice.show()
            self.controls.setEnabled(False)
            self.preview.clear()
            return

        self.notice.hide()
        self.controls.setEnabled(True)
        if self.mode == MODE_SIMPLE:
            s = QgsPalLayerSettings(lyr.labeling().settings())
            fmt = s.format()
            self.field.setField(s.fieldName) if not s.isExpression else self.field.setExpression(s.fieldName)
            pl = s.placement
        else:  # no labeling yet: show the defaults that "Show labels" will create
            s = None
            fmt = self._default_format()
            self.field.setField(default_label_field(lyr))
            pl = placement_value(DEFAULT_PLACEMENT.get(self.family, "AroundPoint"))
        font = fmt.font()
        self.font.setCurrentFont(font)
        self.size.setValue(fmt.size())
        self.size.setSuffix(" " + unit_abbrev(fmt.sizeUnit()))
        self.bold.setChecked(self._is_bold(fmt))
        self.italic.setChecked(self._is_italic(fmt))
        self.color.setColor(fmt.color())
        buf = fmt.buffer()
        self.buffer.setChecked(buf.enabled())
        self.buf_size.setValue(buf.size())
        self.buf_size.setSuffix(" " + unit_abbrev(buf.sizeUnit()))
        self.buf_color.setColor(buf.color())
        if self.extended:
            self._read_extended(s, fmt)
        else:
            self._select_placement(pl)
        self._update_preview(fmt)

    def _read_extended(self, s, fmt):
        self.shadow.setChecked(fmt.shadow().enabled())
        pid = QE._position_of(self.layer, s) if s is not None else (QE.positions(self.layer) or [("", "")])[0][0]
        i = self.placement.findData(pid)
        self.placement.setCurrentIndex(max(0, i))
        off, x, y = QE._offset_of(s, pid) if s is not None else (0.0, 0.0, 0.0)
        for sp, v in ((self.offset_box, off), (self.offset_x, x), (self.offset_y, y)):
            sp.setValue(v)
        self._sync_offset_state()
        self._read_number(s)

    def _fill_placements(self):
        self.placement.clear()
        if self.extended:
            for pid, text in QE.positions(self.layer):
                self.placement.addItem(text, pid)
            return
        for label, name in PLACEMENTS.get(self.family, []):
            v = placement_value(name)
            if v is not None:
                self.placement.addItem(label, name)

    def _select_placement(self, value):
        for i in range(self.placement.count()):
            if placement_value(self.placement.itemData(i)) == value:
                self.placement.setCurrentIndex(i)
                return
        # a placement not offered for this geometry: show it, never change it silently
        name = None
        for cand in ("AroundPoint", "OverPoint", "Line", "Curved", "Horizontal", "Free",
                     "OrderedPositionsAroundPoint", "PerimeterCurved", "OutsidePolygons"):
            if placement_value(cand) == value:
                name = cand
        self.placement.addItem("Current: {}".format(name or "custom"), "__keep__")
        self.placement.setCurrentIndex(self.placement.count() - 1)

    @staticmethod
    def _is_bold(fmt):
        if hasattr(fmt, "forcedBold") and fmt.forcedBold():
            return True
        return fmt.font().bold() or "bold" in (fmt.namedStyle() or "").lower()

    @staticmethod
    def _is_italic(fmt):
        if hasattr(fmt, "forcedItalic") and fmt.forcedItalic():
            return True
        ns = (fmt.namedStyle() or "").lower()
        return fmt.font().italic() or "italic" in ns or "oblique" in ns

    def _default_format(self):
        fmt = QgsTextFormat()
        f = QFont()
        fmt.setFont(f)
        fmt.setSize(10)
        with suppress(Exception):
            fmt.setSizeUnit(C.enum_value(Qgis, "RenderUnit", "Points") or scoped(QgsUnitTypes, 'RenderUnit', 'RenderPoints'))
        from qgis.PyQt.QtGui import QColor
        fmt.setColor(QColor(30, 30, 30))
        buf = fmt.buffer()
        buf.setEnabled(True)
        buf.setSize(1.0)
        buf.setColor(QColor(255, 255, 255))
        fmt.setBuffer(buf)
        return fmt

    # ================================================================== preview
    def _sample_text(self, s_field, is_expr):
        """Label text of one feature (selected first, else first in layer) – reads one feature."""
        key = (s_field, is_expr)
        if self._sample and self._sample[0] == key:
            return self._sample[1]
        lyr = self.layer
        text = None
        try:
            req = QgsFeatureRequest()
            req.setLimit(1)
            req.setFlags(C.no_geometry_flag())
            exp = QgsExpression(s_field if is_expr else QgsExpression.quotedColumnRef(s_field))
            ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
            exp.prepare(ctx)
            if not exp.needsGeometry():
                ref = exp.referencedColumns()
                if ref and QgsFeatureRequest.ALL_ATTRIBUTES not in ref:
                    req.setSubsetOfAttributes(list(ref), lyr.fields())
            else:
                req.setFlags(C.enum_value(QgsFeatureRequest, "Flag", "NoFlags") or 0)
            if lyr.selectedFeatureCount():
                fid = next(iter(lyr.selectedFeatureIds()))
                req.setFilterFid(fid)
            f = QgsFeature()
            it = lyr.getFeatures(req)
            if it.nextFeature(f):
                ctx.setFeature(f)
                val = exp.evaluate(ctx)
                if val is not None and str(val) not in ("", "NULL"):
                    text = str(val)
        except Exception:
            text = None
        text = (text or "Label")[:40]
        self._sample = (key, text)
        return text

    def _update_preview(self, fmt=None):
        lyr = self.layer
        if lyr is None or self.mode == MODE_OTHER:
            self.preview.clear()
            return
        if fmt is None:
            if self.mode != MODE_SIMPLE:
                return
            fmt = QgsPalLayerSettings(lyr.labeling().settings()).format()
        name, is_expr, _ok = self._field_state()
        text = self._sample_text(name, is_expr) if name else "Label"
        w = max(120, self.preview.width() - 4)
        try:
            pm = QgsTextFormat.textFormatPreviewPixmap(fmt, QSize(w, 40), text)
            self.preview.setPixmap(pm)
        except Exception:
            self.preview.setText(text)
        self.preview.setToolTip("Preview of the label for one feature" if text != "Label" else "Preview")

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.layer is not None and self.mode == MODE_SIMPLE and not self._loading:
            self._update_preview()

    # ================================================================== writing
    def _field_state(self):
        res = self.field.currentField()
        if isinstance(res, tuple):
            name = res[0]
            is_expr = bool(res[1]) if len(res) > 1 else self.field.isExpression()
            ok = bool(res[2]) if len(res) > 2 else True
        else:
            name, is_expr, ok = res, self.field.isExpression(), True
        return name, is_expr, ok

    def _queue(self, mutator, now=False):
        if self._loading or self.layer is None or self.mode == MODE_OTHER:
            return
        if self.mode == MODE_NONE:
            # first change on an unlabeled layer creates labeling from the panel defaults
            self._create_labeling(enable=self.enable.isChecked())
        self._pending.append(mutator)
        if now:
            self.flush()
        else:
            self._timer.start()

    def flush(self):
        self._timer.stop()
        if not self._pending:
            return
        muts, self._pending = self._pending, []
        lyr = self.layer
        try:
            if lyr is None or labeling_mode(lyr) != MODE_SIMPLE:
                return
        except RuntimeError:  # layer already deleted: nothing to write to
            self.layer = None
            return
        s = QgsPalLayerSettings(lyr.labeling().settings())  # full copy
        for m in muts:
            m(s)
        self._write(s)
        self._update_preview(s.format())

    def _write(self, settings, enable=None):
        lyr = self.layer
        with self.watcher.own_change():
            lyr.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            if enable is not None:
                lyr.setLabelsEnabled(enable)
            lyr.triggerRepaint()
            lyr.emitStyleChanged()
        QgsProject.instance().setDirty(True)
        self.stats["commits"] += 1

    def _create_labeling(self, enable):
        lyr = self.layer
        s = QgsPalLayerSettings()
        name, is_expr, _ok = self._field_state()
        s.fieldName = name or default_label_field(lyr)
        s.isExpression = bool(is_expr)
        fmt = self._default_format()
        f = fmt.font()
        f.setFamily(self.font.currentFont().family())
        fmt.setFont(f)
        fmt.setSize(self.size.value())
        fmt.setColor(self.color.color())
        buf = fmt.buffer()
        buf.setEnabled(self.buffer.isChecked())
        buf.setSize(self.buf_size.value())
        buf.setColor(self.buf_color.color())
        fmt.setBuffer(buf)
        s.setFormat(fmt)
        data = self.placement.currentData()
        pl = placement_value(data) if data and data != "__keep__" else \
            placement_value(DEFAULT_PLACEMENT.get(self.family, "AroundPoint"))
        if pl is not None:
            s.placement = pl
        if self.extended:
            fmt = s.format()
            fmt.setShadow(QE.shadow_settings(self.shadow.isChecked()))
            s.setFormat(fmt)
            if self._position_id():
                self._write_position(s, self._position_id())
        self._write(s, enable=enable)
        self.mode = MODE_SIMPLE

    # --- mutators (each changes one property only)
    @staticmethod
    def _fmt(s, fn):
        fmt = s.format()
        fn(fmt)
        s.setFormat(fmt)

    def _set_family(self, family):
        def m(s):
            def f(fmt):
                font = fmt.font()
                font.setFamily(family)
                fmt.setFont(font)
                fmt.setNamedStyle("")  # a named style of the old family would not exist
                if self.bold.isChecked() and hasattr(fmt, "setForcedBold"):
                    fmt.setForcedBold(True)
                if self.italic.isChecked() and hasattr(fmt, "setForcedItalic"):
                    fmt.setForcedItalic(True)
            self._fmt(s, f)
        return m

    def _set_size(self, value):
        return lambda s: self._fmt(s, lambda fmt: fmt.setSize(value))

    def _set_bold(self, on):
        def f(fmt):
            font = fmt.font()
            font.setBold(on)
            fmt.setFont(font)
            if hasattr(fmt, "setForcedBold"):
                fmt.setForcedBold(on)
            ns = fmt.namedStyle() or ""
            if not on and "bold" in ns.lower():
                fmt.setNamedStyle("")
        return lambda s: self._fmt(s, f)

    def _set_italic(self, on):
        def f(fmt):
            font = fmt.font()
            font.setItalic(on)
            fmt.setFont(font)
            if hasattr(fmt, "setForcedItalic"):
                fmt.setForcedItalic(on)
            ns = (fmt.namedStyle() or "").lower()
            if not on and ("italic" in ns or "oblique" in ns):
                fmt.setNamedStyle("")
        return lambda s: self._fmt(s, f)

    def _set_color(self, color):
        return lambda s: self._fmt(s, lambda fmt: fmt.setColor(color))

    def _set_buffer(self, enabled=None, size=None, color=None):
        def f(fmt):
            b = fmt.buffer()
            if enabled is not None:
                b.setEnabled(enabled)
            if size is not None:
                b.setSize(size)
            if color is not None:
                b.setColor(color)
            fmt.setBuffer(b)
        return lambda s: self._fmt(s, f)

    # --- Off / All / Selected
    def _show_buttons_state(self, mode, enabled=True):
        for key, b in self.show_buttons.items():
            b.setChecked(key == mode)
            b.setEnabled(enabled and not (key == SHOW_SELECTED and self.mode == MODE_OTHER))
        sel = self.show_buttons[SHOW_SELECTED]
        sel.setToolTip("Label only the selected features" if self.mode != MODE_OTHER
                       else "Rule-based labeling: use the rules in Layer Properties to limit labels")

    def set_show_mode(self, mode):
        lyr = self.layer
        if lyr is None:
            return
        self.flush()
        if mode == SHOW_OFF:
            self.enable.setChecked(False)
        else:
            if mode == SHOW_SELECTED and self.mode == MODE_OTHER:
                mode = SHOW_ALL
            if self.mode == MODE_NONE:
                self._create_labeling(enable=True)
                self.enable.blockSignals(True)
                self.enable.setChecked(True)
                self.enable.blockSignals(False)
            elif not self.enable.isChecked():
                self.enable.setChecked(True)
            if self.mode == MODE_SIMPLE:
                self._apply_selected_rule(mode == SHOW_SELECTED)
        self._show_buttons_state(show_mode(lyr))
        self._update_preview()

    def _apply_selected_rule(self, on):
        """Add / remove the data-defined 'Show label = is_selected()' rule.
        A Show rule the layer already had is kept aside and put back later."""
        lyr = self.layer
        s = QgsPalLayerSettings(lyr.labeling().settings())
        props = s.dataDefinedProperties()
        key = _show_prop_key()
        ours = shows_selected_only(s)
        if on and not ours:
            if props.isActive(key):
                lyr.setCustomProperty(PREV_SHOW_KEY, props.property(key).toVariant())
            props.setProperty(key, QgsProperty.fromExpression(SELECTED_EXPR))
        elif not on and ours:
            prev = lyr.customProperty(PREV_SHOW_KEY)
            restored = QgsProperty()
            if prev:
                restored.loadVariant(prev)
            props.setProperty(key, restored)
            lyr.removeCustomProperty(PREV_SHOW_KEY)
        else:
            return
        s.setDataDefinedProperties(props)
        self._write(s)

    # --- control handlers
    def _on_enable(self, on):
        if self._loading or self.layer is None:
            return
        self.flush()
        lyr = self.layer
        if on and self.mode == MODE_NONE:
            self._create_labeling(enable=True)
            self._update_preview()
            return
        with self.watcher.own_change():
            lyr.setLabelsEnabled(on)  # keeps the whole configuration
            lyr.triggerRepaint()
            lyr.emitStyleChanged()
        QgsProject.instance().setDirty(True)

    def _on_field(self, *_args):
        if self._loading or self.layer is None:
            return
        name, is_expr, ok = self._field_state()
        if not name or not ok:
            return
        if self.extended:                  # a new field: the number options start afresh
            self.layer.removeCustomProperty(QE.FORMAT_PROP)
            self._loading = True
            try:
                self._read_number(None)
                if self._source is None and not is_expr and QE.is_numeric(self.layer, name):
                    self._source = name
                    self._fill_units()
                    self.number_box.setVisible(True)
            finally:
                self._loading = False
            if self.on_layout_changed is not None:
                self.on_layout_changed()

        def m(s):
            s.fieldName = name
            s.isExpression = bool(is_expr)
        self._queue(m, now=True)

    def _on_placement(self, _index):
        name = self.placement.currentData()
        if not name or name == "__keep__":
            return
        if self.extended:
            self._sync_offset_state()
            self._queue(lambda s: self._write_position(s, name), now=True)
            return
        value = placement_value(name)

        def m(s):
            s.placement = value
        self._queue(m, now=True)

    def use_field(self, name):
        """Set the label field from elsewhere (Attributes context menu)."""
        if self.layer is None or self.mode == MODE_OTHER:
            return
        self.field.setField(name)
        self._on_field()
        if not self.enable.isChecked():
            self.set_show_mode(SHOW_ALL)

    def open_native(self):
        if self.layer is None:
            return
        try:
            self.iface.showLayerProperties(self.layer, "mOptsPage_Labels")
        except TypeError:
            self.iface.showLayerProperties(self.layer)

"""
Change Line Length, laid out like the Change Line Length pane of ArcGIS Pro: "Change the selection." and the selected
features, "Change Length to:" / "Change Length by:" with a value and unit, "Preserve location of" the First or the
Last Vertex, "Proportion geometry using existing COGO", and at the bottom Show Preview and the Change Length button.

The vertex that is preserved stays where it is; the other end is cut back along the line (shorter) or continued
straight on in the direction of its last segment (longer). With "Proportion geometry" the whole line is scaled about
the preserved vertex instead, so every segment grows or shrinks by the same factor. The preview draws the new lines
dashed over the selected ones; only single-part lines change. Every run is one undoable edit command per layer.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsGeometry, QgsProject, QgsSettings, QgsUnitTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QButtonGroup, QComboBox, QHBoxLayout, QLabel, QRadioButton, QWidget

from ...core.modify_features import COGO_DISTANCE, find_cogo_field
from . import geometry_ops as G
from .generalize import UNITS, selected_geometries
from .ground import LengthMeasure
from .page import ActionPage, button, check, spin
from .transform import apply_to_selection, editable_selection

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
PREVIEW_MAX = 100


class ChangeLineLengthPage(ActionPage):
    LABEL = "Change Line Length"
    HINT = "set the length of the selected lines, keeping the first or the last vertex where it is."
    ACTION = "Change Length"
    SHOW_HINT = False
    SHOW_STATUS = False

    def __init__(self, ctl):
        super().__init__(ctl)
        settings = QgsSettings()
        self.mode = settings.value(SETTINGS + "length_mode", "to")
        if self.mode not in ("to", "by"):
            self.mode = "to"
        self.preserve = settings.value(SETTINGS + "length_preserve", "last")
        if self.preserve not in ("first", "last"):
            self.preserve = "last"
        self.proportion = settings.value(SETTINGS + "length_proportion", False, type=bool)
        self.show_preview = settings.value(SETTINGS + "length_preview", False, type=bool)
        self.spins = {}
        self.unit_combos = {}
        self.mode_radios = {}
        self.preserve_radios = {}
        self.result_label = None
        self.proportion_check = None
        self._band = None

    # ------------------------------------------------------------------ page
    def _value_row(self, key, label, default, low):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        radio = QRadioButton(label)
        radio.setChecked(self.mode == key)
        radio.toggled.connect(lambda on, k=key: on and self.set_mode(k))
        self.mode_radios[key] = radio
        lay.addWidget(radio, 1)
        box = spin(default, low, 1e12, 3, 1.0)
        box.valueChanged.connect(lambda _v: self.refresh_preview())
        self.spins[key] = box
        lay.addWidget(box)
        combo = QComboBox()
        current = 0
        for i, unit in enumerate(UNITS):
            combo.addItem(QgsUnitTypes.toAbbreviatedString(unit), unit)
            if unit == QgsProject.instance().distanceUnits():
                current = i
        combo.setCurrentIndex(current)
        combo.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        self.unit_combos[key] = combo
        lay.addWidget(combo)
        return row

    def build_options(self, box):
        group = QButtonGroup(self.page())
        box.addWidget(self._value_row("to", "Change Length to:", 0.0, 0.0))
        box.addWidget(self._value_row("by", "Change Length by:", 0.0, -1e12))
        for radio in self.mode_radios.values():
            group.addButton(radio)
        self.length_spin = self.spins["to"]
        box.addWidget(QLabel("Preserve location of:"))
        keep = QButtonGroup(self.page())
        for key, text in (("first", "First Vertex"), ("last", "Last Vertex")):
            radio = QRadioButton(text)
            radio.setChecked(self.preserve == key)
            radio.toggled.connect(lambda on, k=key: on and self.set_preserve(k))
            keep.addButton(radio)
            self.preserve_radios[key] = radio
            box.addWidget(radio)
        self.proportion_check = check("Proportion geometry using existing COGO", self.proportion, self.set_proportion)
        self.proportion_check.setToolTip("Scale the whole line about the preserved vertex, so every segment changes by the "
                                         "same factor. Needs the layer's COGO Distance field.")
        box.addWidget(self.proportion_check)
        self.result_label = QLabel()
        self.result_label.setObjectName("KgaModifyHint")
        self.result_label.setWordWrap(True)
        box.addWidget(self.result_label)
        self.update_page()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        row.addWidget(button(self.ACTION, self.run, "Apply to the selected lines (Ctrl+Z undoes it)"))
        box.addLayout(row)

    def build_page(self, box):
        self.build_options(box)

    def set_mode(self, key):
        self.mode = key
        QgsSettings().setValue(SETTINGS + "length_mode", key)
        self.refresh_preview()

    def set_preserve(self, key):
        self.preserve = key
        QgsSettings().setValue(SETTINGS + "length_preserve", key)
        self.refresh_preview()

    def set_proportion(self, on):
        self.proportion = bool(on)
        QgsSettings().setValue(SETTINGS + "length_proportion", self.proportion)
        self.refresh_preview()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "length_preview", self.show_preview)
        if getattr(self, "preview_check", None) is not None and self.preview_check.isChecked() != self.show_preview:
            self.preview_check.setChecked(self.show_preview)
        self.refresh_preview()

    def has_cogo(self):
        """Do the selected lines' layers have a COGO Distance field?"""
        selected = editable_selection(self.canvas())
        return bool(selected) and all(find_cogo_field(layer.fields(), COGO_DISTANCE) >= 0 for layer, _ids in selected)

    # ------------------------------------------------------------------ what a run does
    def measure(self):
        """How lengths are measured: on the project's ellipsoid when it has one (what $length reports), else flat."""
        return LengthMeasure(self.canvas())

    def _map_units(self, key):
        """The typed value in the measure's unit (metres on the ground, or the map's units when it is flat)."""
        return self.spins[key].value() * QgsUnitTypes.fromUnitToUnitFactor(self.unit_combos[key].currentData(),
                                                                           self.measure().unit)

    def transform(self):
        """fn(geometry in the map's CRS) -> the line with its new length, or None."""
        value = self._map_units(self.mode)
        first = self.preserve == "first"
        proportional = self.proportion and self.proportion_check.isEnabled()
        measure = self.measure()

        def fn(g):
            pts = G.single_line(g)
            if pts is None or len(pts) < 2:
                return None
            flat = QgsGeometry.fromPolylineXY(pts).length()
            length = measure.points_length(pts)                     # on the ground when the project has an ellipsoid
            wanted = value if self.mode == "to" else length + value
            # the flat length that is `wanted` on the ground (the line's own scale carries on past its end)
            target = measure.flat_at(pts, wanted) if measure.ellipsoidal and flat > 0 else wanted
            if proportional:
                new, _old = G.scale_line(pts, target, first)
            else:
                new, _old = G.change_length(pts, pts[-1] if first else pts[0], target)     # the far end moves
            return None if new is None else G.line_like(g, new)
        return fn

    def act(self):
        if not editable_selection(self.canvas()):
            self.ctl.iface.messageBar().pushInfo(self.LABEL, "Select lines first, then press Change Length.")
            return None
        if self.canvas().mapSettings().destinationCrs().isGeographic():
            self._set_result("The map is in degrees - switch the project to a projected CRS so lengths are in metres.")
            return None
        changed, _layers, skipped = apply_to_selection(self.canvas(), self.transform(), "Change line length")
        text = (f"Changed the length of {changed} line(s)." if changed
                else "Nothing changed: the selection has no single-part line whose length would change.")
        if skipped:
            text += f" Not editable, left alone: {', '.join(skipped)}."
        self._set_result(text)
        self.clear_preview()
        return changed

    def _set_result(self, text):
        if self.result_label is not None and not sip.isdeleted(self.result_label):
            self.result_label.setText(text)

    # ------------------------------------------------------------------ preview
    def clear_preview(self):
        if self._band is not None:
            self._band.reset(LINE)

    def refresh_preview(self):
        self.clear_preview()
        if not self.show_preview or self.result_label is None or not self.spins:
            return
        if self._band is None:
            self._band = QgsRubberBand(self.canvas(), LINE)
            self._band.setColor(QColor(0, 0, 0))
            self._band.setWidth(2)
            self._band.setLineStyle(Qt.PenStyle.DashLine)
        fn = self.transform()
        for g in selected_geometries(self.canvas(), PREVIEW_MAX):
            out = fn(g)
            if out is not None:
                self._band.addGeometry(out, QgsCoordinateReferenceSystem(), False)
        self._band.updatePosition()
        self._band.update()

    def update_page(self):
        super().update_page()
        if self.proportion_check is not None and not sip.isdeleted(self.proportion_check):
            self.proportion_check.setEnabled(self.has_cogo())
        if self.spins:
            first = self.spins["to"]
            selected = selected_geometries(self.canvas(), 1)
            if selected and not first.hasFocus() and first.value() == 0.0:
                pts = G.single_line(selected[0])
                if pts:                                      # start from the current length, as Pro does
                    measure = self.measure()
                    factor = QgsUnitTypes.fromUnitToUnitFactor(self.unit_combos["to"].currentData(), measure.unit)
                    first.blockSignals(True)
                    first.setValue(measure.points_length(pts) / factor)
                    first.blockSignals(False)
        self.refresh_preview()

    def deactivate(self):
        super().deactivate()
        self.clear_preview()

    def dispose(self):
        super().dispose()
        if self._band is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(self._band)
            self._band = None

"""
Generalize, laid out like the Generalize pane of ArcGIS Pro: "Change the selection." and the selected features, a
Method (Simplify, Smooth or Densify), the Value it needs in a unit of your choice, and at the bottom Show Preview and
the Generalize button.

    Simplify   remove vertices that move the shape by less than the Maximum Allowable Offset
    Smooth     a smooth curve through the vertices; the offset is how far a chord of it may lie from the curve
    Densify    more vertices: one at most a Distance apart, or - for curved segments - within an Offset / Angle

The preview draws the result over the selected features (blue line, vertices as dots) before anything is written.
Every run is one undoable edit command per layer.
"""

import os
from contextlib import suppress

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject
from qgis.core import QgsSettings, QgsUnitTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QWidget,
)

from ..selection import _transform
from . import geometry_ops as G
from .page import ActionPage, button, check, section_label, spin
from .transform import apply_to_selection, editable_selection

SETTINGS = "kga_editor/modify_"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
LINE = Qgis.GeometryType.Line
BLUE = QColor(51, 153, 255)
UNITS = (Qgis.DistanceUnit.Meters, Qgis.DistanceUnit.Kilometers, Qgis.DistanceUnit.Feet, Qgis.DistanceUnit.Yards,
         Qgis.DistanceUnit.Miles, Qgis.DistanceUnit.NauticalMiles)
METHODS = (("simplify", "Simplify", "gen_simplify.svg", "Remove vertices that move the shape by less than the offset."),
           ("smooth", "Smooth", "gen_smooth.svg", "Replace the lines by a smooth curve through the vertices."),
           ("densify", "Densify", "gen_densify.svg", "Add vertices along the lines."))
PREVIEW_MAX = 100                       # features drawn in the preview


def simplified(geom, tolerance):
    if geom.type() == Qgis.GeometryType.Point or tolerance <= 0:
        return None
    out = geom.simplify(tolerance)
    return None if out is None or out.isNull() or out.isEmpty() else out


def selected_geometries(canvas, limit):
    """Up to `limit` selected features' geometries (editable layers shown on the map), in the map's CRS."""
    canvas_crs = canvas.mapSettings().destinationCrs()
    out = []
    for layer, ids in editable_selection(canvas):
        to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
        for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(ids)[:limit]).setNoAttributes()):
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            if to_map is not None:
                g.transform(to_map)
            out.append(g)
            if len(out) >= limit:
                return out
    return out


class GeneralizePage(ActionPage):
    LABEL = "Generalize"
    HINT = "remove, smooth or add vertices in the selected lines and polygons."
    ACTION = "Generalize"
    SHOW_HINT = False
    SHOW_STATUS = False

    def __init__(self, ctl):
        super().__init__(ctl)
        settings = QgsSettings()
        self.method = settings.value(SETTINGS + "generalize_method", "simplify")
        if self.method not in ("simplify", "smooth", "densify"):
            self.method = "simplify"
        self.densify_how = settings.value(SETTINGS + "generalize_densify", "distance")
        if self.densify_how not in ("distance", "offset", "angle"):
            self.densify_how = "distance"
        self.show_preview = settings.value(SETTINGS + "generalize_preview", False, type=bool)
        self.radios = {}
        self.how_radios = {}
        self.unit_combos = {}
        self.spins = {}
        self.result_label = None
        self._band = None
        self._dots = None

    # ------------------------------------------------------------------ page
    def _unit_combo(self, key):
        box = QComboBox()
        current = 0
        for i, unit in enumerate(UNITS):
            box.addItem(QgsUnitTypes.toAbbreviatedString(unit), unit)
            if unit == QgsProject.instance().distanceUnits():
                current = i
        box.setCurrentIndex(current)
        box.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        self.unit_combos[key] = box
        return box

    def _value_row(self, key, label, default, with_unit=True, radio_group=None):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        if radio_group is not None:
            head = QRadioButton(label)
            head.setChecked(key == self.densify_how)
            radio_group.addButton(head)
            self.how_radios[key] = head
            head.toggled.connect(lambda on, k=key: on and self.set_densify_how(k))
        else:
            head = QLabel(label)
        lay.addWidget(head, 1)
        box = spin(default, 0.0, 1e9, 3, 1.0)
        box.valueChanged.connect(lambda _v: self.refresh_preview())
        self.spins[key] = box
        lay.addWidget(box)
        if with_unit:
            lay.addWidget(self._unit_combo(key))
        return row

    def build_options(self, box):
        box.addWidget(section_label("Method"))
        grid = QGridLayout()
        group = QButtonGroup(self.page())
        for col, (key, text, icon, tip) in enumerate(METHODS):
            picture = QLabel()
            picture.setPixmap(QIcon(os.path.join(ICON_DIR, icon)).pixmap(30, 30))
            picture.setToolTip(tip)
            radio = QRadioButton(text)
            radio.setToolTip(tip)
            radio.setChecked(key == self.method)
            radio.toggled.connect(lambda on, k=key: on and self.set_method(k))
            group.addButton(radio)
            self.radios[key] = radio
            grid.addWidget(picture, 0, col, Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(radio, 1, col, Qt.AlignmentFlag.AlignHCenter)
        box.addLayout(grid)
        box.addWidget(section_label("Value"))
        settings = QgsSettings()
        self.offset_row = self._value_row(
            "offset_max", "Maximum Allowable Offset", float(settings.value(SETTINGS + "generalize_offset", 0.0)))
        self.tolerance_spin = self.spins["offset_max"]                 # what Simplify and Smooth read
        box.addWidget(self.offset_row)
        self.densify_box = QWidget()
        lay = QGridLayout(self.densify_box)
        lay.setContentsMargins(0, 0, 0, 0)
        how_group = QButtonGroup(self.densify_box)
        lay.addWidget(self._value_row("distance", "Distance", float(settings.value(SETTINGS + "generalize_distance", 100.0)),
                                      radio_group=how_group))
        lay.addWidget(self._value_row("offset", "Offset", 0.0, radio_group=how_group))
        lay.addWidget(self._value_row("angle", "Angle", 0.0, with_unit=False, radio_group=how_group))
        box.addWidget(self.densify_box)
        self.result_label = QLabel()
        self.result_label.setObjectName("KgaModifyHint")
        self.result_label.setWordWrap(True)
        box.addWidget(self.result_label)
        self._show_method()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        row.addWidget(button(self.ACTION, self.run, "Apply to the selected features (Ctrl+Z undoes it)"))
        box.addLayout(row)

    def build_page(self, box):
        self.build_options(box)

    def _show_method(self):
        self.offset_row.setVisible(self.method != "densify")
        self.densify_box.setVisible(self.method == "densify")

    def set_method(self, key):
        self.method = key
        QgsSettings().setValue(SETTINGS + "generalize_method", key)
        self._show_method()
        self.refresh_preview()

    def set_densify_how(self, key):
        self.densify_how = key
        QgsSettings().setValue(SETTINGS + "generalize_densify", key)
        self.refresh_preview()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "generalize_preview", self.show_preview)
        if getattr(self, "preview_check", None) is not None and self.preview_check.isChecked() != self.show_preview:
            self.preview_check.setChecked(self.show_preview)
        self.refresh_preview()

    # ------------------------------------------------------------------ what a run does
    def _map_units(self, key):
        """The value of a field in map units (the angle is in degrees: left as it is)."""
        value = self.spins[key].value()
        combo = self.unit_combos.get(key)
        if combo is None:
            return value
        map_units = self.canvas().mapSettings().destinationCrs().mapUnits()
        return value * QgsUnitTypes.fromUnitToUnitFactor(combo.currentData(), map_units)

    def transform(self):
        """fn(geometry in the map's CRS) -> new geometry or None, for the chosen method and values."""
        if self.method == "simplify":
            tolerance = self._map_units("offset_max")
            return lambda g: simplified(g, tolerance)
        if self.method == "smooth":
            offset = self._map_units("offset_max")
            return lambda g: G.smooth_geometry(g, offset)
        how = self.densify_how
        value = self._map_units(how)
        return lambda g: G.densify_geometry(g, how, value)

    def act(self):
        if not editable_selection(self.canvas()):
            self.ctl.iface.messageBar().pushInfo(self.LABEL, "Select features first, then press Generalize.")
            return None
        fn = self.transform()
        counts = [0, 0]

        def counted(g):
            out = fn(g)
            if out is not None:
                counts[0] += sum(1 for _ in g.vertices())
                counts[1] += sum(1 for _ in out.vertices())
            return out

        changed, _layers, skipped = apply_to_selection(self.canvas(), counted, "Generalize features")
        text = (f"Generalized {changed} feature(s): {counts[0]} -> {counts[1]} vertices." if changed
                else "Nothing to change with these values.")
        if skipped:
            text += f" Not editable, left alone: {', '.join(skipped)}."
        self._set_result(text)
        self.clear_preview()
        return changed, counts

    def _set_result(self, text):
        if self.result_label is not None and not sip.isdeleted(self.result_label):
            self.result_label.setText(text)

    # ------------------------------------------------------------------ preview
    def _selected_geometries(self):
        return selected_geometries(self.canvas(), PREVIEW_MAX)

    def _bands(self):
        if self._band is None:
            self._band = QgsRubberBand(self.canvas(), LINE)
            self._band.setColor(BLUE)
            self._band.setWidth(3)
            self._dots = QgsRubberBand(self.canvas(), Qgis.GeometryType.Point)
            self._dots.setColor(QColor(0, 70, 150))
            self._dots.setFillColor(BLUE)
            self._dots.setIconSize(7)
            with suppress(AttributeError):
                self._dots.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        return self._band, self._dots

    def clear_preview(self):
        if self._band is not None:
            self._band.reset(LINE)
            self._dots.reset(Qgis.GeometryType.Point)

    def refresh_preview(self):
        self.clear_preview()
        if not self.show_preview or self.canvas() is None or self.result_label is None:
            return
        fn = self.transform()
        band, dots = self._bands()
        points = []
        for g in self._selected_geometries():
            out = fn(g)
            if out is None:
                if g.type() == Qgis.GeometryType.Point:
                    continue
                out = g                                    # nothing to change at these values: show the feature as it is
            outline = out if out.type() == LINE else out.convertToType(LINE, False)
            if outline is not None and not outline.isNull():
                band.addGeometry(outline, QgsCoordinateReferenceSystem(), False)
            points += [QgsPointXY(v.x(), v.y()) for v in out.vertices()]
        band.updatePosition()
        band.update()
        points = points[:3000]
        for i, p in enumerate(points):
            dots.addPoint(p, i == len(points) - 1)

    def update_page(self):
        super().update_page()
        self.refresh_preview()

    def deactivate(self):
        super().deactivate()
        self.clear_preview()

    def dispose(self):
        super().dispose()
        for item in (self._band, self._dots):
            if item is not None:
                with suppress(Exception):
                    self.canvas().scene().removeItem(item)
        self._band = self._dots = None

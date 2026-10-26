"""
Copy Parallel, laid out like the Copy Parallel pane of ArcGIS Pro: "Change the selection." and the selected lines, the
Template (the line layer that receives the copies, and whether they keep the source attribute values), Features
(separate features or one feature per source line), Values (Distance in a unit of your choice and Repetitions) and
the Method: Side (Both, Left or Right of the line's direction) and Corners (Mitered, Rounded or Beveled).

Repetition k is copied at k x Distance. The preview draws the copies (olive lines) and arrows on the selected lines
showing the direction that Left and Right refer to. The new lines are selected afterwards. One undoable edit command.
"""

import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QWidget,
)

from ..selection import _transform, all_vector_layers, map_layers
from . import geometry_ops as G
from .buffer import layer_icon
from .divide import DIST_UNITS
from .page import DoneStrip, button, check, project_layer, section_label, spin
from .pick import _to_layer
from .reshape import PickTool, _copyable

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
JOINS = (("mitered", "Mitered", Qgis.JoinStyle.Miter), ("rounded", "Rounded", Qgis.JoinStyle.Round),
         ("beveled", "Beveled", Qgis.JoinStyle.Bevel))
SIDES = (("both", "Both"), ("left", "Left"), ("right", "Right"))
SEGMENTS = 16
MITER_LIMIT = 5.0
PREVIEW_MAX = 60


def offset_lines(geom, distance, side, join, repetitions):
    """
    The copies of a line geometry: for repetition k = 1..n the line offset by k x distance to the left of its direction
    (side "left"), to the right ("right") or both. Returns [(side, k, geometry)]; offsets GEOS cannot make are left out.
    """
    out = []
    for k in range(1, repetitions + 1):
        for name, sign in (("left", 1), ("right", -1)):
            if side not in ("both", name):
                continue
            shifted = geom.offsetCurve(sign * distance * k, SEGMENTS, join, MITER_LIMIT)
            if shifted is not None and not shifted.isNull() and not shifted.isEmpty():
                out.append((name, k, shifted))
    return out


class ParallelTool(PickTool):
    KEY = "copy_parallel"
    LABEL = "Copy Parallel"
    HINT = "select lines, choose the template, distance and side, then press Copy."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False                # clicking selects until you press Copy

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.use_template = settings.value(SETTINGS + "parallel_use_template", True, type=bool)
        self.keep_attributes = settings.value(SETTINGS + "parallel_keep", True, type=bool)
        self.separate = settings.value(SETTINGS + "parallel_separate", True, type=bool)
        self.side = settings.value(SETTINGS + "parallel_side", "both")
        self.join = settings.value(SETTINGS + "parallel_join", "mitered")
        self.remove_loops = settings.value(SETTINGS + "parallel_loops", True, type=bool)
        self.show_preview = settings.value(SETTINGS + "parallel_preview", True, type=bool)
        if self.side not in ("both", "left", "right"):
            self.side = "both"
        if self.join not in ("mitered", "rounded", "beveled"):
            self.join = "mitered"
        self.info = self.template_check = self.template_combo = self.keep_check = None
        self.separate_radio = self.single_radio = self.distance_spin = self.unit_combo = None
        self.repetitions_spin = self.copy_button = self.loops_check = self.align_check = None
        self.side_radios = {}
        self.join_radios = {}
        self._open = False
        self._band = self._arrows = None
        self._listed = []
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def start_select_mode(self):
        self.set_selecting(True, auto=True)

    def activate(self):
        self._open = True
        super().activate()
        self.refresh_preview()

    def deactivate(self):
        self._open = False
        self._clear_preview()
        super().deactivate()
        self._clear_preview()

    def dispose(self):
        for item in (self._band, self._arrows):
            if item is not None:
                with suppress(Exception):
                    self.canvas().scene().removeItem(item)
        self._band = self._arrows = None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        super().build_selection_row(box)

    def _choices(self, items, current, radios, setter, icons=()):
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        group = QButtonGroup(host)
        for col, item in enumerate(items):
            key, text = item[0], item[1]
            radio = QRadioButton(text)
            radio.setChecked(key == current)
            radio.toggled.connect(lambda on, k=key: on and setter(k))
            group.addButton(radio)
            radios[key] = radio
            if icons:
                picture = QLabel()
                picture.setPixmap(QIcon(icons[col]).pixmap(30, 30))
                grid.addWidget(picture, 0, col, Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(radio, 1, col, Qt.AlignmentFlag.AlignHCenter)
        return host

    def build_page(self, box):
        icons = os.path.join(os.path.dirname(__file__), "icons")
        settings = QgsSettings()
        box.addWidget(section_label("Template"))
        self.template_check = check("Use Template", self.use_template, self.set_use_template)
        box.addWidget(self.template_check)
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        self.keep_check = check("Keep source attribute values", self.keep_attributes, self.set_keep)
        box.addWidget(self.keep_check)
        box.addWidget(section_label("Features"))
        group = QButtonGroup(self.page())
        self.separate_radio = QRadioButton("Copy to separate features")
        self.single_radio = QRadioButton("Copy to a single feature")
        for radio in (self.separate_radio, self.single_radio):
            group.addButton(radio)
            box.addWidget(radio)
        self.separate_radio.setChecked(self.separate)
        self.single_radio.setChecked(not self.separate)
        self.separate_radio.toggled.connect(lambda on: self.set_separate(on))
        box.addWidget(section_label("Values"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Distance"), 1)
        self.distance_spin = spin(float(settings.value(SETTINGS + "parallel_distance", 10.0)), 0.0, 1e12, 3, 1.0)
        self.distance_spin.valueChanged.connect(self._values_changed)
        row.addWidget(self.distance_spin)
        self.unit_combo = QComboBox()
        index = 0
        for i, unit in enumerate(DIST_UNITS):
            self.unit_combo.addItem(QgsUnitTypes.toAbbreviatedString(unit), unit)
            if unit == QgsProject.instance().distanceUnits():
                index = i
        self.unit_combo.setCurrentIndex(index)
        self.unit_combo.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        row.addWidget(self.unit_combo)
        box.addLayout(row)
        reps = QHBoxLayout()
        reps.addWidget(QLabel("Repetitions"), 1)
        self.repetitions_spin = QSpinBox()
        self.repetitions_spin.setRange(1, 100)
        self.repetitions_spin.setValue(int(settings.value(SETTINGS + "parallel_repetitions", 1)))
        self.repetitions_spin.valueChanged.connect(self._values_changed)
        reps.addWidget(self.repetitions_spin, 1)
        box.addLayout(reps)
        box.addWidget(section_label("Method"))
        box.addWidget(QLabel("Side"))
        box.addWidget(self._choices(SIDES, self.side, self.side_radios, self.set_side,
                                    [os.path.join(icons, f"par_{k}.svg") for k, _t in SIDES]))
        box.addWidget(QLabel("Corners"))
        box.addWidget(self._choices(JOINS, self.join, self.join_radios, self.set_join,
                                    [os.path.join(icons, f"par_{k}.svg") for k, _t, _j in JOINS]))
        self.loops_check = check("Remove self-intersecting loops", self.remove_loops, self.set_loops)
        self.loops_check.setToolTip("Drop the loops an offset makes on the inside of a tight bend")
        self.align_check = check("Align the direction of connected lines", True, lambda _on: None)
        self.align_check.setEnabled(False)                                    # Pro greys it out for a single line too
        box.addWidget(self.loops_check)
        box.addWidget(self.align_check)
        self._rebuild_templates()
        self._features_state()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        self.copy_button = button("Copy", self.copy, "Copy the selected lines (Ctrl+Z undoes it)")
        row.addWidget(self.copy_button)
        box.addLayout(row)

    # ------------------------------------------------------------------ the template
    def _template_layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.geometryType() == LINE
                and (shown is None or layer.id() in shown)]

    def _rebuild_templates(self):
        current = self.template_combo.currentData() or QgsSettings().value(SETTINGS + "parallel_template", "")
        layers = self._template_layers()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for layer in layers:
            self.template_combo.addItem(layer_icon(layer), layer.name(), layer.id())
        self._listed = [layer.id() for layer in layers]
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(current)))
        self.template_combo.blockSignals(False)
        self.template_combo.setEnabled(self.use_template)

    def _template_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "parallel_template", self.template_combo.currentData() or "")

    def set_use_template(self, on):
        self.use_template = bool(on)
        QgsSettings().setValue(SETTINGS + "parallel_use_template", self.use_template)
        self.template_combo.setEnabled(self.use_template)

    def set_keep(self, on):
        self.keep_attributes = bool(on)
        QgsSettings().setValue(SETTINGS + "parallel_keep", self.keep_attributes)

    def set_separate(self, on):
        self.separate = bool(on)
        QgsSettings().setValue(SETTINGS + "parallel_separate", self.separate)

    def _features_state(self):
        """"Copy to ... features" only matters when there is more than one copy of a line."""
        many = self.repetitions_spin.value() > 1
        self.separate_radio.setEnabled(many)
        self.single_radio.setEnabled(many)

    # ------------------------------------------------------------------ the values
    def _values_changed(self, _value):
        settings = QgsSettings()
        settings.setValue(SETTINGS + "parallel_distance", self.distance_spin.value())
        settings.setValue(SETTINGS + "parallel_repetitions", self.repetitions_spin.value())
        self._features_state()
        self.refresh_preview()

    def set_side(self, key):
        self.side = key
        QgsSettings().setValue(SETTINGS + "parallel_side", key)
        self._features_state()
        self.refresh_preview()

    def set_join(self, key):
        self.join = key
        QgsSettings().setValue(SETTINGS + "parallel_join", key)
        self.refresh_preview()

    def set_loops(self, on):
        self.remove_loops = bool(on)
        QgsSettings().setValue(SETTINGS + "parallel_loops", self.remove_loops)
        self.refresh_preview()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "parallel_preview", self.show_preview)
        if getattr(self, "preview_check", None) is not None and self.preview_check.isChecked() != self.show_preview:
            self.preview_check.setChecked(self.show_preview)
        self.refresh_preview()

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.copy_button is None or sip.isdeleted(self.copy_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        self.refresh_preview()

    # ------------------------------------------------------------------ what is copied
    def _distance(self):
        crs = self.canvas().mapSettings().destinationCrs()
        return self.distance_spin.value() * QgsUnitTypes.fromUnitToUnitFactor(self.unit_combo.currentData(), crs.mapUnits())

    def _join(self):
        return next(j for key, _t, j in JOINS if key == self.join)

    def _selected_lines(self, limit=None):
        """[(layer, fid, geometry in the map's CRS, attributes)] of the selected line features."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        shown = map_layers(self.canvas())
        out = []
        for layer in all_vector_layers():
            if not (layer.isSpatial() and layer.geometryType() == LINE and layer.selectedFeatureCount()
                    and (shown is None or layer.id() in shown)):
                continue
            to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
            ids = list(layer.selectedFeatureIds())[:limit] if limit else list(layer.selectedFeatureIds())
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
                g = QgsGeometry(f.geometry())
                if g.isNull() or g.isEmpty():
                    continue
                if to_map is not None:
                    g.transform(to_map)
                out.append((layer, f.id(), g, f.attributes()))
        return out

    def plan(self, limit=None):
        """[(source layer, attributes, [offset geometries in the map's CRS])] - one entry per source line."""
        distance = self._distance()
        if distance <= 0 or self.canvas().mapSettings().destinationCrs().isGeographic():
            return []
        plan = []
        for layer, _fid, geom, attrs in self._selected_lines(limit):
            made = offset_lines(geom, distance, self.side, self._join(), self.repetitions_spin.value())
            copies = [g for _s, _k, g in made]
            if copies:
                plan.append((layer, attrs, copies))
        return plan

    # ------------------------------------------------------------------ preview
    def _clear_preview(self):
        if self._band is not None:
            self._band.reset(LINE)
        if self._arrows is not None:
            self._arrows.reset(POLYGON)

    def refresh_preview(self):
        self._clear_preview()
        if self.copy_button is None or not self._open:
            return
        lines = self._selected_lines(PREVIEW_MAX)
        geographic = self.canvas().mapSettings().destinationCrs().isGeographic()
        ready = bool(lines) and self.distance_spin.value() > 0 and not geographic and self.template_for() is not None
        self.copy_button.setEnabled(ready)
        if not lines:
            return
        mupp = self.canvas().mapUnitsPerPixel()
        if self._arrows is None:
            self._arrows = QgsRubberBand(self.canvas(), POLYGON)
            self._arrows.setColor(QColor(0, 0, 0))
            self._arrows.setFillColor(QColor(0, 0, 0))
            self._arrows.setWidth(1)
        for _layer, _fid, geom, _attrs in lines:
            for tri in G.direction_arrows(geom, 90 * mupp, 16 * mupp):
                self._arrows.addGeometry(QgsGeometry.fromPolygonXY([tri + [tri[0]]]), QgsCoordinateReferenceSystem(), False)
        self._arrows.updatePosition()
        self._arrows.update()
        if not self.show_preview or not ready:
            return
        if self._band is None:
            self._band = QgsRubberBand(self.canvas(), LINE)
            self._band.setColor(QColor(140, 150, 100))
            self._band.setWidth(1)
        for _layer, _attrs, copies in self.plan(PREVIEW_MAX):
            for g in copies:
                self._band.addGeometry(g, QgsCoordinateReferenceSystem(), False)
        self._band.updatePosition()
        self._band.update()

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ the run
    def template_for(self):
        """The layer that receives the copies: the chosen template, else the source layer when it is editable."""
        if self.use_template:
            return project_layer(self.template_combo.currentData())
        for layer, _fid, _g, _a in self._selected_lines(1):
            return layer if layer.isEditable() else None
        return None

    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))

    def copy(self):
        if self.canvas().mapSettings().destinationCrs().isGeographic():
            self.tell("The map is in degrees - switch the project to a projected CRS so distances are in metres.", ok=False)
            return 0
        if self.distance_spin.value() <= 0:
            self.tell("Set a Distance above 0.", ok=False)
            return 0
        plan = self.plan()
        if not plan:
            self.tell("Select the lines to copy first.", ok=False)
            return 0
        target = self.template_for()
        if target is None:
            self.tell("Choose an editable line layer as the Template.", ok=False)
            return 0
        multi = QgsWkbTypes.isMultiType(target.wkbType())
        single = not self.separate and self.repetitions_spin.value() > 1 and multi
        target.beginEditCommand("Copy parallel")
        made = []
        ok = True
        for layer, source_attrs, copies in plan:
            attrs = _copyable(target, source_attrs) if self.keep_attributes and layer is target else (
                self._carried(target, layer, source_attrs) if self.keep_attributes else {})
            groups = [copies] if single else [[g] for g in copies]
            for group in groups:
                geom = group[0] if len(group) == 1 else QgsGeometry.collectGeometry(group)
                if multi and not geom.isMultipart():
                    geom.convertToMultiType()
                elif not multi and geom.isMultipart():
                    geom = QgsGeometry.fromPolylineXY(geom.asMultiPolyline()[0])
                feature = QgsVectorLayerUtils.createFeature(target, _to_layer(self.canvas(), target, geom), attrs)
                ok = target.addFeature(feature) and ok
                made.append(feature.id())
        if ok:
            target.endEditCommand()
        else:
            target.destroyEditCommand()
            self.tell(f"Could not edit {target.name()} - is it in edit mode?", ok=False)
            return 0
        target.triggerRepaint()
        self._clear_preview()
        for layer in all_vector_layers():
            if layer.selectedFeatureCount():
                layer.removeSelection()
        target.selectByIds(made)
        self.tell(f"Copy completed: {len(made)} new line(s) in {target.name()}.")
        self.refresh_page()
        return len(made)

    @staticmethod
    def _carried(target, source, source_attrs):
        """Source attribute values for fields the template layer shares by name (keys and unique fields stay fresh)."""
        wanted = _copyable(source, source_attrs)
        carried = {}
        for index, value in wanted.items():
            at = target.fields().lookupField(source.fields().at(index).name())
            if at >= 0:
                carried[at] = value
        owned = set(target.primaryKeyAttributes() or [])
        return {i: v for i, v in carried.items() if i not in owned}


TOOL_CLASSES = {ParallelTool.KEY: ParallelTool}

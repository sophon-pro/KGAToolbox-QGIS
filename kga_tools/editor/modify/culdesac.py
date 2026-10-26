"""
Cul-de-sac, laid out like the Cul-de-sac pane of ArcGIS Pro: "Change the selection." and the selected lines, the
Template (the editable layer the outline is made in), the Side of the line that gets the turnaround and the Values -
Street width, Radius and Return radius - with Show Preview and Create at the bottom. Black arrows on the selected
lines show which way they run, which is what Start of line and End of line refer to.

The outline of the turnaround is one line (a closed ring in a polygon layer): the circle of the Radius centred on the
end of the street, joined to each kerb - the street's two edges, half the Street width either side of the line - by a
return curve of the Return radius that is tangent to both. Show Preview draws it in blue. The values may each be given
in their own distance unit.
"""

import math
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCompoundCurve,
    QgsCircularString,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsSettings,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QButtonGroup, QComboBox, QHBoxLayout, QLabel, QRadioButton

from ...core.modify_features import DISTANCE_UNITS, Workspace, fit_to_layer
from ...gui.modify_base import make_feature
from ..selection import _transform, all_vector_layers, map_layers
from . import geometry_ops as G
from .buffer import layer_icon
from .cogo import CogoSettings
from .page import DoneStrip, SelectedTree, button, check, project_layer, section_label, selection_banner, spin
from .reshape import PickTool

SETTINGS = "kga_editor/modify_culdesac_"
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
BLUE = QColor(40, 50, 160)
UNIT_ABBREVIATIONS = {None: "map", "Meters": "m", "Kilometers": "km", "Feet": "ft", "FeetUSSurvey": "ftUS",
                      "Yards": "yd", "Miles": "mi", "NauticalMiles": "nmi"}
ARROW_SPACING_PX = 90
ARROW_SIZE_PX = 16
NEED_SELECTION = "Select one or more lines"


# ---------------------------------------------------------------------------------------- geometry
def cul_de_sac_outline(end, direction, width, radius, return_radius, closed=False):
    """
    The turnaround of a street that ends at `end` heading `direction` (a unit vector, all in work units): the
    circle of `radius` centred on `end`, joined to the kerbs (`width` / 2 either side) by return curves of
    `return_radius`. A QgsLineString from the left kerb round the far side to the right kerb (closed across the
    street with `closed`). ValueError when the numbers do not fit together.
    """
    half = width / 2.0
    if width <= 0 or radius <= 0 or return_radius < 0:
        raise ValueError("the street width and the radius must be more than zero")
    if radius < half - 1e-9:
        raise ValueError("the radius must be at least half the street width")
    ahead = (-direction[1], direction[0])                                   # the left of the way the street runs

    def at(u, v):
        return QgsPoint(end.x() + direction[0] * u + ahead[0] * v, end.y() + direction[1] * u + ahead[1] * v)

    reach = math.sqrt(max((radius + return_radius) ** 2 - (half + return_radius) ** 2, 0.0))
    sides = []
    for sign in (1.0, -1.0):                                                 # left, then right
        centre = (-reach, sign * (half + return_radius))
        scale = radius / (radius + return_radius)
        contact = (centre[0] * scale, centre[1] * scale)
        kerb = (centre[0], sign * half)
        sides.append((kerb, centre, contact))
    # the return curve on the left: kerb -> contact, about its centre
    curves = []
    for kerb, centre, contact in sides:
        if return_radius > 0:
            a0 = math.atan2(kerb[1] - centre[1], kerb[0] - centre[0])
            a1 = math.atan2(contact[1] - centre[1], contact[0] - centre[0])
            delta = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi            # the short way round
            mid = a0 + delta / 2.0
            middle = (centre[0] + return_radius * math.cos(mid), centre[1] + return_radius * math.sin(mid))
            curves.append(QgsCircularString(at(*kerb), at(*middle), at(*contact)))
        else:
            curves.append(None)
    left, right = sides[0], sides[1]
    outline = QgsCompoundCurve()
    if curves[0] is not None:
        outline.addCurve(curves[0])
    outline.addCurve(QgsCircularString(at(*left[2]), at(radius, 0.0), at(*right[2])))
    if curves[1] is not None:
        back = curves[1].reversed()
        outline.addCurve(back)
    line = outline.curveToLine()
    if closed:
        points = list(line.points())
        points.append(points[0])
        line = QgsLineString(points)
    return line


def line_points(geometry):
    """The vertices of a single-part line as QgsPointXY, or None."""
    if geometry is None or geometry.isNull() or geometry.type() != LINE:
        return None
    if geometry.isMultipart() and geometry.constGet().numGeometries() != 1:
        return None
    pts = [QgsPointXY(v.x(), v.y()) for v in geometry.vertices()]
    return pts if len(pts) >= 2 else None


def selected_lines(canvas):
    """[(layer, ids)] of the selected features of the line layers shown on the map."""
    shown = map_layers(canvas)
    return [(layer, list(layer.selectedFeatureIds())) for layer in all_vector_layers()
            if layer.isSpatial() and layer.geometryType() == LINE and layer.selectedFeatureCount()
            and (shown is None or layer.id() in shown)]


def build_outline(canvas, geometry, at_start, target, values, closed=False):
    """
    The outline for the end of `geometry` (a line in the map's CRS), as a geometry in `target`'s CRS. values:
    [(width, unit name), (radius, unit name), (return radius, unit name)].
    """
    pts = line_points(geometry)
    if pts is None:
        raise ValueError("only single-part lines can have a cul-de-sac")
    end, neighbour = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
    here, there = QgsGeometry.fromPointXY(end), QgsGeometry.fromPointXY(neighbour)
    canvas_crs = canvas.mapSettings().destinationCrs()
    if target.crs() != canvas_crs:
        for g in (here, there):
            g.transform(_transform(canvas_crs, target.crs()))
    workspaces = [Workspace(target, unit, extent_hint=here.asPoint()) for _v, unit in values]
    if any(w.fell_back for w in workspaces):
        raise ValueError("the layer is in degrees and no projected CRS fits it - pick a real distance unit or "
                         "reproject")
    start, other = workspaces[0].to_work(here).asPoint(), workspaces[0].to_work(there).asPoint()
    dx, dy = start.x() - other.x(), start.y() - other.y()
    length = math.hypot(dx, dy)
    if length < 1e-12:
        raise ValueError("the line ends with a zero-length segment")
    width, radius, return_radius = (w.distance(v) for w, (v, _u) in zip(workspaces, values))
    line = cul_de_sac_outline(start, (dx / length, dy / length), width, radius, return_radius, closed)
    shape = QgsGeometry(line)
    if closed:
        shape = QgsGeometry.fromPolygonXY([[QgsPointXY(p.x(), p.y()) for p in line.points()]])
    return workspaces[0].from_work(shape)


# ---------------------------------------------------------------------------------------- tool
class CulDeSacTool(PickTool):
    KEY = "cul_de_sac"
    LABEL = "Cul-de-sac"
    HINT = "select a line, choose the side and the values, then press Create."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False

    def __init__(self, canvas, iface, select_tool=None):
        s = QgsSettings()
        self.use_template = s.value(SETTINGS + "use_template", True, type=bool)
        self.at_start = s.value(SETTINGS + "start", False, type=bool)
        self.show_preview = s.value(SETTINGS + "preview", True, type=bool)
        self.info = self.tree = self.banner = self.template_combo = self.create_button = None
        self.spins = {}
        self.unit_boxes = {}
        self._listed = []
        self._open = False
        self._result = None
        self._result_sig = frozenset()
        self._band = None
        self._arrows = None
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        self._open = True
        super().activate()
        self.refresh_page()

    def deactivate(self):
        self._open = False
        self._draw()
        super().deactivate()

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

    def build_page(self, box):
        self.banner = selection_banner(self._banner_clicked)
        self.banner.setChecked(self._selecting)
        self._select_button = self.banner
        box.addWidget(self.banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)
        box.addWidget(section_label("Template"))
        self.use_check = check("Use Template", self.use_template, self.set_use_template)
        self.use_check.setToolTip("Create the outline in the template layer. Off: in the selected line's own layer")
        box.addWidget(self.use_check)
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        box.addWidget(section_label("Side"))
        group = QButtonGroup(self.banner)
        self.start_radio = QRadioButton("Start of line")
        self.end_radio = QRadioButton("End of line")
        for radio in (self.start_radio, self.end_radio):
            group.addButton(radio)
            box.addWidget(radio)
        self.start_radio.setChecked(self.at_start)
        self.end_radio.setChecked(not self.at_start)
        self.start_radio.toggled.connect(self.set_side)
        box.addWidget(section_label("Values"))
        default_unit = CogoSettings.load().distance_unit
        for key, text, value in (("width", "Street width", 15.0), ("radius", "Radius", 15.0),
                                 ("return", "Return radius", 10.0)):
            row = QHBoxLayout()
            row.addWidget(QLabel(text), 1)
            box_spin = spin(float(QgsSettings().value(SETTINGS + key, value)), 0.0, 1e9, 3, 1.0)
            box_spin.valueChanged.connect(lambda _v, k=key: self._value_changed(k))
            units = QComboBox()
            for label, name in DISTANCE_UNITS:
                units.addItem(UNIT_ABBREVIATIONS.get(name, name), name)
                units.setItemData(units.count() - 1, label, 3)
            saved = QgsSettings().value(SETTINGS + key + "_unit", default_unit) or None
            units.setCurrentIndex(max(0, units.findData(saved)))
            units.currentIndexChanged.connect(lambda _i, k=key: self._value_changed(k))
            self.spins[key], self.unit_boxes[key] = box_spin, units
            row.addWidget(box_spin)
            row.addWidget(units)
            box.addLayout(row)
        self._rebuild_templates()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        self.preview_check.setToolTip("Draw the outline on the map")
        row.addWidget(self.preview_check, 1)
        self.create_button = button("Create", self.create_selected, "Add the outline to the template layer "
                                    "(Ctrl+Z undoes it)")
        row.addWidget(self.create_button)
        box.addLayout(row)

    def _banner_clicked(self):
        self.set_selecting(not self._selecting)

    # ------------------------------------------------------------------ options
    def set_use_template(self, on):
        self.use_template = bool(on)
        QgsSettings().setValue(SETTINGS + "use_template", self.use_template)
        self._edited()

    def set_side(self, start_on):
        self.at_start = bool(start_on)
        QgsSettings().setValue(SETTINGS + "start", self.at_start)
        self._edited()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "preview", self.show_preview)
        self.update_page()

    def _value_changed(self, key):
        s = QgsSettings()
        s.setValue(SETTINGS + key, self.spins[key].value())
        s.setValue(SETTINGS + key + "_unit", self.unit_boxes[key].currentData() or "")
        self._edited()

    def values(self):
        return [(self.spins[k].value(), self.unit_boxes[k].currentData()) for k in ("width", "radius", "return")]

    # ------------------------------------------------------------------ the template
    def _template_layers(self):
        shown = map_layers(self.canvas())
        layers = [layer for layer in all_vector_layers()
                  if layer.isEditable() and layer.isSpatial() and layer.geometryType() in (LINE, POLYGON)
                  and (shown is None or layer.id() in shown)]
        return sorted(layers, key=lambda layer: layer.geometryType() != LINE)          # line layers first

    def _rebuild_templates(self):
        current = self.template_combo.currentData() or QgsSettings().value(SETTINGS + "template", "")
        layers = self._template_layers()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for layer in layers:
            self.template_combo.addItem(layer_icon(layer), layer.name(), layer.id())
        self._listed = [layer.id() for layer in layers]
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(current)))
        self.template_combo.blockSignals(False)

    def _template_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "template", self.template_combo.currentData() or "")
        self._edited()

    def target_for(self, layer):
        """The layer the outline of a line of `layer` goes in, or None."""
        if self.use_template:
            return project_layer(self.template_combo.currentData()) if self.template_combo is not None else None
        return layer if layer.isEditable() else None

    # ------------------------------------------------------------------ the selected lines
    def _lines(self):
        """[(layer, feature id, geometry in the map's CRS)] of the selected single-part lines."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        out = []
        for layer, ids in selected_lines(self.canvas()):
            transform = _transform(layer.crs(), canvas_crs) if layer.crs() != canvas_crs else None
            for feature in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
                geometry = QgsGeometry(feature.geometry())
                if transform is not None:
                    geometry.transform(transform)
                out.append((layer, feature.id(), geometry))
        return out

    def plan(self):
        """[(layer, feature id, outline in the map's CRS)] for the selected lines. ValueError for the first problem."""
        out = []
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        for layer, fid, geometry in self._lines():
            target = self.target_for(layer)
            if target is None:
                raise ValueError("there is no editable template layer" if self.use_template
                                 else f"{layer.name()} is not editable - turn Use Template on")
            outline = build_outline(self.canvas(), geometry, self.at_start, target, self.values(),
                                    closed=target.geometryType() == POLYGON)
            shown = QgsGeometry(outline)
            if target.crs() != canvas_crs:
                shown.transform(_transform(target.crs(), canvas_crs))
            out.append((layer, fid, shown, target, outline))
        return out

    # ------------------------------------------------------------------ preview
    def _edited(self):
        self._result = None
        self.update_page()

    def _draw(self, outlines=None, arrows=None):
        for key in ("_band", "_arrows"):
            band = getattr(self, key)
            if band is not None:
                band.reset(LINE if key == "_band" else POLYGON)
        if not self._open:
            return
        if outlines:
            if self._band is None:
                self._band = QgsRubberBand(self.canvas(), LINE)
                self._band.setColor(BLUE)
                self._band.setWidth(2)
            for shape in outlines:
                outline = shape if shape.type() == LINE else shape.convertToType(LINE, False)
                self._band.addGeometry(outline, QgsCoordinateReferenceSystem(), True)
        if arrows:
            if self._arrows is None:
                self._arrows = QgsRubberBand(self.canvas(), POLYGON)
                self._arrows.setColor(QColor(0, 0, 0))
                self._arrows.setFillColor(QColor(0, 0, 0))
                self._arrows.setWidth(1)
            for tri in arrows:
                self._arrows.addGeometry(QgsGeometry.fromPolygonXY([tri + [tri[0]]]), QgsCoordinateReferenceSystem(),
                                         False)
            self._arrows.updatePosition()
            self._arrows.update()

    def _signature(self):
        return frozenset((layer.id(), fid) for layer, ids in selected_lines(self.canvas()) for fid in ids)

    def update_page(self):
        if self.create_button is None or sip.isdeleted(self.create_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        self.template_combo.setEnabled(self.use_template)
        self.tree.refresh(self.canvas())
        lines = self._lines()
        self.banner.setText("Change the selection." if lines else NEED_SELECTION)
        mupp = self.canvas().mapUnitsPerPixel()
        arrows = []
        for _layer, _fid, geometry in lines:
            arrows += G.direction_arrows(geometry, ARROW_SPACING_PX * mupp, ARROW_SIZE_PX * mupp)
        problem = ""
        plan = []
        if lines:
            try:
                plan = self.plan()
            except ValueError as exc:
                problem = str(exc)
        self.create_button.setEnabled(bool(plan) and not problem)
        self._draw([p[2] for p in plan] if self.show_preview else None, arrows)
        if self._result and self._signature() != self._result_sig:
            self._result = None
        if self._result:
            self.info.show_text(*self._result)
        elif problem:
            self.info.show_text(problem[:1].upper() + problem[1:] + ".", ok="info")
        else:
            self.info.hide()

    # ------------------------------------------------------------------ the run
    def create_selected(self):
        try:
            plan = self.plan()
        except ValueError as exc:
            self._finish(f"{str(exc)[:1].upper()}{str(exc)[1:]}.", False)
            return 0
        if not plan:
            self._finish(NEED_SELECTION + ".", False)
            return 0
        added = 0
        by_target = {}
        for _layer, _fid, _shown, target, outline in plan:
            by_target.setdefault(target, []).append(outline)
        for target, outlines in by_target.items():
            target.beginEditCommand("Cul-de-sac")
            ok = False
            for outline in outlines:
                for part in fit_to_layer(outline, target):
                    if target.addFeature(make_feature(target, part, {})):
                        added += 1
                        ok = True
            if ok:
                target.endEditCommand()
            else:
                target.destroyEditCommand()
            target.triggerRepaint()
        self._finish("Cul-de-sac completed." if added else "Nothing was created.", bool(added))
        return added

    def _finish(self, text, ok):
        self._result = (text, ok)
        self._result_sig = self._signature()
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))
        self.update_page()


TOOL_CLASSES = {CulDeSacTool.KEY: CulDeSacTool}

"""
Construct Polygons, laid out like the Construct Polygons pane of ArcGIS Pro: "Change the selection." and the selected
lines, the Template (the editable polygon layer that receives the polygons), the Cluster Tolerance and "Use existing
features in target", and the Construct button.

The selected lines are joined where they cross or meet and every closed area they enclose becomes a polygon (lines
that do not close an area are ignored). Line ends that stop short of another line by no more than the Cluster
Tolerance are joined to it. "Use existing features in target" also lets the outlines of the polygons already in the
template layer close areas, and leaves out any polygon that is already there. The new polygons take the template's
default attribute values and are selected afterwards. One undoable edit.
"""

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel

from ..selection import _transform, all_vector_layers, map_layers
from .buffer import layer_icon
from .divide import DIST_UNITS
from .page import DoneStrip, button, check, project_layer, section_label, spin
from .pick import _to_layer
from .reshape import PickTool

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
DEFAULT_TOLERANCE_M = 0.001                          # Pro's default: 0.003281 ft


def _vertices(geom):
    return [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]


def snap_ends(lines, tolerance):
    """
    The lines with every end that stops within `tolerance` of another line (or of another line's end) extended to
    touch it, so that nearly-closed outlines close. `lines`: [QgsGeometry]; single-part lines are handled, others
    are returned as they were.
    """
    if tolerance <= 0:
        return list(lines)
    result = []
    for index, line in enumerate(lines):
        if line.isMultipart() or line.isEmpty():
            result.append(line)
            continue
        pts = _vertices(line)
        if len(pts) < 2:
            result.append(line)
            continue
        others = [g for j, g in enumerate(lines) if j != index]
        for end in (0, -1):
            here = QgsGeometry.fromPointXY(pts[end])
            best = None
            for other in others:
                d = other.distance(here)
                if 1e-12 < d <= tolerance and (best is None or d < best[0]):
                    best = (d, other)
            if best is not None:
                target = best[1].nearestPoint(here).asPoint()
                if end == 0:
                    pts.insert(0, QgsPointXY(target))
                else:
                    pts.append(QgsPointXY(target))
        result.append(QgsGeometry.fromPolylineXY(pts))
    return result


def construct_polygons(lines, tolerance=0.0, existing=()):
    """
    The polygons enclosed by `lines` (QgsGeometry lines in one CRS), after joining ends within `tolerance`. `existing`
    polygons add their outlines to the lines and are left out of the result. Returns [QgsGeometry polygons].
    """
    lines = [g for g in lines if g is not None and not g.isNull() and not g.isEmpty()]
    outlines = []
    for poly in existing:
        outline = poly.convertToType(LINE, False)
        if outline is not None and not outline.isNull():
            outlines.append(outline)
    parts = snap_ends(lines + outlines, tolerance)
    if not parts:
        return []
    noded = QgsGeometry.unaryUnion(parts)
    if noded is None or noded.isNull() or noded.isEmpty():
        return []
    found = QgsGeometry.polygonize([noded])
    if found is None or found.isNull() or found.isEmpty():
        return []
    polygons = [p for p in found.asGeometryCollection() if p.type() == POLYGON and p.area() > 1e-12]
    for old in existing:                               # already in the layer: not made twice
        polygons = [p for p in polygons if p.symDifference(old).area() > 1e-9 * max(1.0, old.area())]
    return polygons


class PolygonTool(PickTool):
    KEY = "construct_polygons"
    LABEL = "Construct Polygons"
    HINT = "select the lines that enclose an area, choose the template, then press Construct."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False                # clicking selects until you press Construct

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.use_existing = settings.value(SETTINGS + "construct_existing", False, type=bool)
        self.info = self.template_combo = self.tolerance_spin = self.unit_combo = None
        self.existing_check = self.construct_button = None
        self._listed = []
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ page
    def start_select_mode(self):
        self.set_selecting(True, auto=True)

    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        super().build_selection_row(box)

    def build_page(self, box):
        box.addWidget(section_label("Template"))
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        box.addWidget(section_label("Values"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Cluster Tolerance"), 1)
        stored = QgsSettings().value(SETTINGS + "construct_tolerance", None)
        unit = QgsProject.instance().distanceUnits()
        default = float(stored) if stored is not None else (
            QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters, unit) * DEFAULT_TOLERANCE_M)
        self.tolerance_spin = spin(default, 0.0, 1e9, 6, 0.001)
        self.tolerance_spin.valueChanged.connect(self._tolerance_changed)
        row.addWidget(self.tolerance_spin)
        self.unit_combo = QComboBox()
        index = 0
        for i, each in enumerate(DIST_UNITS):
            self.unit_combo.addItem(QgsUnitTypes.toAbbreviatedString(each), each)
            if each == unit:
                index = i
        self.unit_combo.setCurrentIndex(index)
        row.addWidget(self.unit_combo)
        box.addLayout(row)
        self.existing_check = check("Use existing features in target", self.use_existing, self.set_existing)
        box.addWidget(self.existing_check)
        self._rebuild_templates()

    def build_footer(self, box):
        row = QHBoxLayout()
        row.addStretch(1)
        self.construct_button = button("Construct", self.construct, "Build polygons from the selected lines")
        row.addWidget(self.construct_button)
        box.addLayout(row)

    def _template_layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.geometryType() == POLYGON
                and (shown is None or layer.id() in shown)]

    def _rebuild_templates(self):
        current = self.template_combo.currentData() or QgsSettings().value(SETTINGS + "construct_template", "")
        layers = self._template_layers()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for layer in layers:
            self.template_combo.addItem(layer_icon(layer), layer.name(), layer.id())
        self._listed = [layer.id() for layer in layers]
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(current)))
        self.template_combo.blockSignals(False)

    def _template_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "construct_template", self.template_combo.currentData() or "")
        self._refresh_state()

    def _tolerance_changed(self, value):
        QgsSettings().setValue(SETTINGS + "construct_tolerance", float(value))

    def set_existing(self, on):
        self.use_existing = bool(on)
        QgsSettings().setValue(SETTINGS + "construct_existing", self.use_existing)

    def template(self):
        return project_layer(self.template_combo.currentData()) if self.template_combo is not None else None

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.construct_button is None or sip.isdeleted(self.construct_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        self._refresh_state()

    def _refresh_state(self):
        if self.construct_button is not None and not sip.isdeleted(self.construct_button):
            self.construct_button.setEnabled(bool(self._selected_lines(1)) and self.template() is not None)

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ the run
    def _tolerance(self):
        crs = self.canvas().mapSettings().destinationCrs()
        return self.tolerance_spin.value() * QgsUnitTypes.fromUnitToUnitFactor(self.unit_combo.currentData(), crs.mapUnits())

    def _selected_lines(self, limit=None):
        """[geometry in the map's CRS] of the selected line features of the layers shown on the map."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        shown = map_layers(self.canvas())
        out = []
        for layer in all_vector_layers():
            if not (layer.isSpatial() and layer.geometryType() == LINE and layer.selectedFeatureCount()
                    and (shown is None or layer.id() in shown)):
                continue
            to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
            ids = list(layer.selectedFeatureIds())[:limit] if limit else list(layer.selectedFeatureIds())
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids).setNoAttributes()):
                g = QgsGeometry(f.geometry())
                if g.isNull() or g.isEmpty():
                    continue
                if to_map is not None:
                    g.transform(to_map)
                out.append(g)
        return out

    def _existing(self, template):
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        to_map = None if template.crs() == canvas_crs else _transform(template.crs(), canvas_crs)
        out = []
        for f in template.getFeatures(QgsFeatureRequest().setNoAttributes()):
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            if to_map is not None:
                g.transform(to_map)
            out += [p for p in g.asGeometryCollection() if p.type() == POLYGON]
        return out

    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))

    def construct(self):
        template = self.template()
        if template is None:
            self.tell("Choose an editable polygon layer as the Template.", ok=False)
            return 0
        lines = self._selected_lines()
        if not lines:
            self.tell("Select the lines first.", ok=False)
            return 0
        existing = self._existing(template) if self.use_existing else []
        polygons = construct_polygons(lines, self._tolerance(), existing)
        if not polygons:
            self.tell("No closed area was found: the selected lines do not enclose a polygon.", ok=False)
            return 0
        multi = QgsWkbTypes.isMultiType(template.wkbType())
        template.beginEditCommand("Construct polygons")
        made = []
        ok = True
        for polygon in polygons:
            geom = QgsGeometry(polygon)
            if multi and not geom.isMultipart():
                geom.convertToMultiType()
            feature = QgsVectorLayerUtils.createFeature(template, _to_layer(self.canvas(), template, geom))
            ok = template.addFeature(feature) and ok
            made.append(feature.id())
        if ok:
            template.endEditCommand()
        else:
            template.destroyEditCommand()
            self.tell(f"Could not edit {template.name()} - is it in edit mode?", ok=False)
            return 0
        template.triggerRepaint()
        for layer in all_vector_layers():
            if layer.selectedFeatureCount():
                layer.removeSelection()
        template.selectByIds(made)
        self.tell(f"Construct Polygons completed: {len(made)} polygon(s) added to {template.name()}.")
        self.refresh_page()
        return len(made)


TOOL_CLASSES = {PolygonTool.KEY: PolygonTool}

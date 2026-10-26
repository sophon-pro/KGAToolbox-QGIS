"""
Planarize, laid out like the Planarize pane of ArcGIS Pro: "Change the selection." and the selected lines, a Cluster
Tolerance (in the project's distance unit) and the Planarize button.

Every selected line is cut where it crosses another selected line, or where another selected line ends on it
(within the cluster tolerance), so that no two lines cross without a vertex there. The first piece of a cut line
keeps the original feature; the other pieces are new features with the same attributes (primary key and unique
fields get fresh values). One undoable edit command per layer.
"""

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel

from ..selection import _transform
from . import geometry_ops as G
from .page import DoneStrip, button, spin
from .pick import _to_layer
from .reshape import PickTool, _copyable
from .transform import editable_selection

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
DEFAULT_TOLERANCE_M = 0.001                          # Pro's default: 0.0032808399 ft


def _line_parts(geom):
    """The line parts of a geometry as QgsGeometry parts (one for a single-part line)."""
    if geom is None or geom.isNull() or geom.isEmpty() or geom.type() != LINE:
        return []
    return geom.asGeometryCollection()


def planarize_selection(canvas, tolerance):
    """
    Cut the selected lines of the editable layers shown on the map where they cross or touch each other.
    `tolerance` is in map units. Returns (lines cut, lines before, lines after, names of layers that failed).
    """
    canvas_crs = canvas.mapSettings().destinationCrs()
    items = []                                           # [(layer, fid, geometry in map CRS, attributes)]
    for layer, ids in editable_selection(canvas):
        if layer.geometryType() != LINE:
            continue
        to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
        for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(ids))):
            geom = QgsGeometry(f.geometry())
            if geom.isNull() or geom.isEmpty():
                continue
            if to_map is not None:
                geom.transform(to_map)
            items.append((layer, f.id(), geom, _copyable(layer, f.attributes())))
    if len(items) < 1:
        return 0, 0, 0, []
    per_layer = {}
    cut_features = 0
    total_after = 0
    for index, (layer, fid, geom, attrs) in enumerate(items):
        others = [g for j, (_l, _f, g, _a) in enumerate(items) if j != index]
        boundary = QgsGeometry.collectGeometry(others) if others else QgsGeometry()
        pieces = []
        for part in _line_parts(geom):
            length = part.length()
            cuts = G._distances_along(part, boundary, length, tolerance) if not boundary.isEmpty() else []
            edges = [0.0] + cuts + [length]
            curve = part.constGet()
            pieces += [QgsGeometry(curve.curveSubstring(a, b)) for a, b in zip(edges, edges[1:]) if b - a > 1e-12]
        total_after += max(1, len(pieces))
        if len(pieces) <= len(_line_parts(geom)):
            continue                                     # nothing cut here
        cut_features += 1
        multi = QgsWkbTypes.isMultiType(layer.wkbType())
        shaped = []
        for piece in pieces:
            out = QgsGeometry(piece)
            if multi and not out.isMultipart():
                out.convertToMultiType()
            shaped.append(out)
        entry = per_layer.setdefault(layer.id(), (layer, [], []))
        entry[1].append((fid, shaped[0]))
        entry[2].extend((piece, attrs) for piece in shaped[1:])
    failed = []
    for layer, updates, adds in per_layer.values():
        layer.beginEditCommand("Planarize")
        ok = all(layer.changeGeometry(fid, _to_layer(canvas, layer, g)) for fid, g in updates)
        made = []
        for g, attrs in adds:
            if ok:
                feature = QgsVectorLayerUtils.createFeature(layer, _to_layer(canvas, layer, g), attrs)
                ok = layer.addFeature(feature)
                made.append(feature.id())
        if ok:
            layer.endEditCommand()
            layer.selectByIds(list(layer.selectedFeatureIds()) + made)          # the new pieces are selected too
        else:
            layer.destroyEditCommand()
            failed.append(layer.name())
        layer.triggerRepaint()
    return cut_features, len(items), total_after, failed


class PlanarizeTool(PickTool):
    KEY = "planarize"
    LABEL = "Planarize"
    HINT = "select the lines, then press Planarize: they are cut where they cross each other."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False                # clicking selects until you press Planarize

    def __init__(self, canvas, iface, select_tool=None):
        self.info = None
        self.tolerance_spin = None
        self.planarize_button = None
        self._result = None                # (text, ok), shown until the selection changes
        self._result_sig = None
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ page
    def start_select_mode(self):
        self.set_selecting(True, auto=True)

    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        super().build_selection_row(box)

    def _unit(self):
        return QgsProject.instance().distanceUnits()

    def _factor(self):
        """Map units in one project distance unit."""
        crs = self.canvas().mapSettings().destinationCrs()
        return QgsUnitTypes.fromUnitToUnitFactor(self._unit(), crs.mapUnits())

    def build_page(self, box):
        row = QHBoxLayout()
        row.addWidget(QLabel("Cluster Tolerance :"))
        default = QgsSettings().value(SETTINGS + "planarize_tolerance", None)
        if default is None:
            default = QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters, self._unit()) * DEFAULT_TOLERANCE_M
        self.tolerance_spin = spin(float(default), 0.0, 1e9, 10, 0.001)
        self.tolerance_spin.valueChanged.connect(self.set_tolerance)
        row.addWidget(self.tolerance_spin, 1)
        self.unit_label = QLabel(QgsUnitTypes.toAbbreviatedString(self._unit()))
        row.addWidget(self.unit_label)
        box.addLayout(row)

    def build_footer(self, box):
        row = QHBoxLayout()
        row.addStretch(1)
        self.planarize_button = button("Planarize", self.planarize, "Cut the selected lines where they cross")
        row.addWidget(self.planarize_button)
        box.addLayout(row)

    def set_tolerance(self, value):
        QgsSettings().setValue(SETTINGS + "planarize_tolerance", float(value))

    def selected_lines(self):
        return sum(len(ids) for layer, ids in editable_selection(self.canvas()) if layer.geometryType() == LINE)

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.planarize_button is None or sip.isdeleted(self.planarize_button):
            return
        signature = frozenset((layer.id(), fid) for layer, ids in editable_selection(self.canvas()) for fid in ids)
        if self._result and signature != self._result_sig:
            self._result = None
        self.planarize_button.setEnabled(self.selected_lines() > 0)
        if self._result:
            self.info.show_text(*self._result)
        else:
            self.info.hide()
        self.unit_label.setText(QgsUnitTypes.toAbbreviatedString(self._unit()))

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ the run
    def planarize(self):
        if not self.selected_lines():
            self.say("select the lines first.")
            return 0
        cut, before, after, failed = planarize_selection(self.canvas(), self.tolerance_spin.value() * self._factor())
        if failed:
            text, ok = f"Could not edit {', '.join(failed)} - is it in edit mode?", False
        elif cut:
            text, ok = f"Planarize completed: {before} line(s) became {after}.", True
        else:
            text, ok = "Nothing to planarize: the selected lines do not cross each other.", False
        self._result = (text, ok)
        self._result_sig = frozenset((layer.id(), fid) for layer, ids in editable_selection(self.canvas()) for fid in ids)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))
        self.refresh_page()
        return cut


TOOL_CLASSES = {PlanarizeTool.KEY: PlanarizeTool}

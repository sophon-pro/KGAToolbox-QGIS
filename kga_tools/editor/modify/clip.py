"""
Clip, laid out like the Clip pane of ArcGIS Pro: a blue "Make a selection." bar, "Select one or more ..." (the select
button of the tab you are on), the Input Features (the clip features: points, lines or polygons) and the Target
Features (lines or polygons that get clipped), a Buffer Distance, and "When clipping features":

    Discard (Remainder)   remove the part of each target that lies inside the clip area ("Keep coincident" keeps a
                          line's part that runs along the clip boundary)
    Preserve              keep only the part inside the clip area
    Contain               delete the targets that lie outside the clip area; the others stay as they are
    Split                 keep both: the part inside and the part outside become separate features

Points and lines clip through the buffer around them (so they need a Buffer Distance above 0); polygons may be
grown by it too. "Clip all editable features" clips every editable line and polygon the clip area touches instead
of the Target Features. New pieces copy the attributes. One undoable edit command per layer.
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
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
)

from ..selection import _transform, all_vector_layers, map_layers
from .divide import DIST_UNITS, DivideTool
from .page import DoneStrip, button, check, selection_banner, spin
from .pick import _to_layer
from .reshape import PickTool, _copyable
from .split import TABS_QSS, FeatureList, _live

SETTINGS = "kga_editor/modify_"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
POINT = Qgis.GeometryType.Point
MODES = (("discard", "Discard (Remainder)", "Discard the area that intersects", "clip_discard.svg"),
         ("preserve", "Preserve", "Preserve the area that intersects", "clip_preserve.svg"),
         ("contain", "Contain", "Delete all features outside the clip area", "clip_contain.svg"),
         ("split", "Split", "Preserve both areas", "clip_split.svg"))
INPUT_BANNER = "Select one or more points, lines or polygons."
TARGET_BANNER = "Select one or more lines or polygons."
EPS = 1e-9


# ---------------------------------------------------------------------------------------- maths
def _same_kind(geom, like):
    """Only the parts of `geom` of the same kind (line / polygon) as `like`; None when nothing is left."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    parts = [p for p in geom.asGeometryCollection() if p.type() == like.type() and not p.isEmpty()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return QgsGeometry.collectGeometry(parts)


def clip_area(geometries, distance):
    """The area the input features clip with: each buffered by `distance` (polygons stay as they are at 0), united."""
    areas = []
    for g in geometries:
        if g is None or g.isNull() or g.isEmpty():
            continue
        if distance > EPS:
            g = g.buffer(distance, 12)
        elif g.type() != POLYGON:
            continue                                    # a point or a line has no area until it is buffered
        areas.append(g)
    if not areas:
        return None
    area = QgsGeometry.unaryUnion(areas)
    return None if area is None or area.isNull() or area.isEmpty() else area


def clip_geometry(g, area, mode, keep_coincident=False):
    """
    What clipping does to one target geometry: ("keep",) when it is untouched, ("delete",), ("update", geometry), or
    ("split", inside, outside).
    """
    if not g.intersects(area):
        return ("delete",) if mode == "contain" else ("keep",)
    if mode == "contain":
        return ("keep",)
    inside = _same_kind(g.intersection(area), g)
    outside = _same_kind(g.difference(area), g)
    if mode == "preserve":
        return ("update", inside) if inside is not None else ("delete",)
    if mode == "discard":
        if keep_coincident and g.type() == LINE:
            edge = _same_kind(g.intersection(area.convertToType(LINE, False)), g)
            if edge is not None:
                outside = edge if outside is None else _same_kind(outside.combine(edge), g)
        if outside is None:
            return ("delete",)
        return ("update", outside)
    if inside is None or outside is None:                  # split: all inside, or all outside - nothing to cut
        return ("keep",)
    return ("split", inside, outside)


# ---------------------------------------------------------------------------------------- the tool
class ClipTool(PickTool):
    KEY = "clip"
    LABEL = "Clip"
    HINT = "pick the clip (input) features and the target features, then press Clip."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = False
    SELECT_ONE_SHOT = True

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.mode = settings.value(SETTINGS + "clip_mode", "discard")
        if self.mode not in ("discard", "preserve", "contain", "split"):
            self.mode = "discard"
        self.keep_coincident = settings.value(SETTINGS + "clip_coincident", False, type=bool)
        self.clip_all = settings.value(SETTINGS + "clip_all", False, type=bool)
        self.inputs = []                       # [(layer, fid)] the clip features
        self.targets = []                      # [(layer, fid)] what is clipped
        self._capture = None                   # "input" | "target": the next selection fills that list
        self._arming = False                   # a Select button is being pressed
        self._result = None                    # (text, ok): what the last Clip did, shown while the lists are empty
        self.tab = 0
        self.mode_radios = {}
        self.info = None
        self.banner = None
        self.tabs = None
        self.input_list = self.target_list = None
        self.distance_spin = None
        self.unit_combo = None
        self.clip_button = None
        self._bands = {}
        self._open = False
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        self._open = True
        super().activate()
        self._show_marks()

    def deactivate(self):
        self._open = False
        self._capture = None
        self._clear_marks()
        super().deactivate()
        self._clear_marks()

    def dispose(self):
        for band in self._bands.values():
            with suppress(Exception):
                self.canvas().scene().removeItem(band)
        self._bands = {}
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        self.banner = selection_banner(self._banner_clicked)
        self._select_button = self.banner
        self.banner.setChecked(self._selecting)
        box.addWidget(self.banner)

    def build_page(self, box):
        self.tabs = QTabWidget()
        self.tabs.setObjectName("KgaSplitTabs")
        self.tabs.setStyleSheet(TABS_QSS)
        self.input_list = FeatureList("Select Input Features", lambda: self.capture_for("input"))
        self.target_list = FeatureList("Select Target Features", lambda: self.capture_for("target"))
        self.tabs.addTab(self.input_list, "Input Features")
        self.tabs.addTab(self.target_list, "Target Features")
        self.tabs.currentChanged.connect(self.set_tab)
        self.tabs.setMinimumHeight(190)
        box.addWidget(self.tabs, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Buffer Distance:"), 1)
        self.distance_spin = spin(float(settings_value("clip_distance", 0.0)), 0.0, 1e12, 3, 1.0)
        self.distance_spin.valueChanged.connect(self.set_distance)
        row.addWidget(self.distance_spin)
        self.unit_combo = QComboBox()
        index = 0
        for i, unit in enumerate(DIST_UNITS):
            self.unit_combo.addItem(QgsUnitTypes.toAbbreviatedString(unit), unit)
            if unit == QgsProject.instance().distanceUnits():
                index = i
        self.unit_combo.setCurrentIndex(index)
        self.unit_combo.currentIndexChanged.connect(lambda _i: self.refresh_page())
        row.addWidget(self.unit_combo)
        box.addLayout(row)
        box.addWidget(QLabel("When clipping features"))
        group = QButtonGroup(self.page())
        for key, title, text, icon in MODES:
            line = QHBoxLayout()
            radio = QRadioButton()
            radio.setChecked(key == self.mode)
            radio.toggled.connect(lambda on, k=key: on and self.set_mode(k))
            group.addButton(radio)
            self.mode_radios[key] = radio
            picture = QLabel()
            picture.setPixmap(QIcon(os.path.join(ICON_DIR, icon)).pixmap(34, 34))
            words = QVBoxLayout()
            words.setSpacing(0)
            head = QLabel(title)
            bold = QFont(head.font())
            bold.setBold(True)
            head.setFont(bold)
            words.addWidget(head)
            words.addWidget(QLabel(text))
            line.addWidget(radio)
            line.addWidget(picture)
            line.addLayout(words, 1)
            box.addLayout(line)
            if key == "discard":
                indent = QHBoxLayout()
                indent.setContentsMargins(64, 0, 0, 0)
                self.coincident_check = check("Keep coincident", self.keep_coincident, self.set_coincident)
                self.coincident_check.setToolTip("Keep the part of a line that runs along the clip boundary")
                indent.addWidget(self.coincident_check)
                box.addLayout(indent)
        self._banner_text()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.all_check = check("Clip all editable features", self.clip_all, self.set_clip_all)
        self.all_check.setToolTip("Clip every editable line and polygon the clip area touches, not just the Target Features")
        row.addWidget(self.all_check, 1)
        self.clip_button = button("Clip", self.clip, "Clip the target features (Ctrl+Z undoes it)")
        row.addWidget(self.clip_button)
        box.addLayout(row)

    def _banner_text(self):
        if self.banner is None or sip.isdeleted(self.banner):
            return
        have = self.inputs if self.tab == 0 else self.targets
        self.banner.setText("Change the selection." if have else (INPUT_BANNER if self.tab == 0 else TARGET_BANNER))

    def update_page(self):
        if self.input_list is None or sip.isdeleted(self.input_list):
            return
        self.inputs, self.targets = _live(self.inputs), _live(self.targets)
        if self._capture is not None and not self._arming:
            self._store_capture()
        self.input_list.show_features(self.inputs, self.canvas())
        self.target_list.show_features(self.targets, self.canvas())
        self.input_list.button.setChecked(self._capture == "input" and self._selecting)
        self.target_list.button.setChecked(self._capture == "target" and self._selecting)
        self.target_list.setEnabled(not self.clip_all)
        ready = bool(self.inputs) and (self.clip_all or bool(self.targets))
        if self.clip_button is not None and not sip.isdeleted(self.clip_button):
            self.clip_button.setEnabled(ready)
        if self.info is not None and not sip.isdeleted(self.info):
            if self.inputs and (self.targets or self.clip_all):
                self.info.hide()
            elif self._result is not None and not (self.inputs or self.targets):
                self.info.show_text(*self._result)
            else:
                self.info.show_text("Make a selection.", ok="info")
        self._banner_text()
        self._show_marks()

    def set_tab(self, index):
        self.tab = int(index)
        self._capture = None
        self.set_selecting(False)
        self.refresh_page()

    def set_mode(self, key):
        self.mode = key
        QgsSettings().setValue(SETTINGS + "clip_mode", key)

    def set_coincident(self, on):
        self.keep_coincident = bool(on)
        QgsSettings().setValue(SETTINGS + "clip_coincident", self.keep_coincident)

    def set_clip_all(self, on):
        self.clip_all = bool(on)
        QgsSettings().setValue(SETTINGS + "clip_all", self.clip_all)
        self.refresh_page()

    def set_distance(self, value):
        QgsSettings().setValue(SETTINGS + "clip_distance", float(value))
        self.refresh_page()

    def _banner_clicked(self):
        self.capture_for("input" if self.tab == 0 else "target")

    # ------------------------------------------------------------------ picking the features
    def capture_for(self, kind):
        """The next selection on the map fills the Input or the Target list."""
        if self._selecting and self._capture == kind:
            self._capture = None
            self.set_selecting(False)
            return
        self._arming = True                    # clearing the selection fires selectionChanged: not a pick
        self._result = None
        try:
            self._capture = kind
            if kind == "input":
                self.inputs = []
            else:
                self.targets = []
            for layer in all_vector_layers():
                if layer.selectedFeatureCount():
                    layer.removeSelection()
            self.set_selecting(True)
        finally:
            self._arming = False
        self.refresh_page()

    def _store_capture(self):
        shown = map_layers(self.canvas())
        items = []
        for layer in all_vector_layers():
            if not (layer.selectedFeatureCount() and layer.isSpatial() and (shown is None or layer.id() in shown)):
                continue
            if self._capture == "target" and not (layer.isEditable() and layer.geometryType() in (LINE, POLYGON)):
                continue
            items += [(layer, fid) for fid in layer.selectedFeatureIds()]
        if self._capture == "input":
            self.inputs = items
        else:
            self.targets = items

    # ------------------------------------------------------------------ marks on the map
    def _band(self, key, colour):
        band = self._bands.get(key)
        if band is None:
            band = QgsRubberBand(self.canvas(), LINE)
            band.setColor(colour)
            band.setWidth(3)
            band.setLineStyle(Qt.PenStyle.DashLine)
            self._bands[key] = band
        return band

    def _clear_marks(self):
        for band in self._bands.values():
            band.reset(LINE)

    def _geometry_of(self, layer, fid):
        geom = QgsGeometry(layer.getFeature(fid).geometry())
        if geom.isNull() or geom.isEmpty():
            return None
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        if layer.crs() != canvas_crs:
            geom.transform(_transform(layer.crs(), canvas_crs))
        return geom

    def _distance_in_map_units(self):
        crs = self.canvas().mapSettings().destinationCrs()
        factor = QgsUnitTypes.fromUnitToUnitFactor(self.unit_combo.currentData(), crs.mapUnits())
        return self.distance_spin.value() * factor

    def _show_marks(self):
        """The clip area in orange dashes and the targets in cyan dashes."""
        self._clear_marks()
        if not self._open or self.distance_spin is None:
            return
        area = clip_area([self._geometry_of(layer, fid) for layer, fid in _live(self.inputs)],
                         self._distance_in_map_units())
        orange = self._band("area", QColor(255, 165, 0))
        if area is not None:
            outline = area.convertToType(LINE, False)
            if outline is not None and not outline.isNull():
                orange.addGeometry(outline, QgsCoordinateReferenceSystem(), False)
        else:
            for layer, fid in _live(self.inputs):                     # no area yet: mark the features themselves
                geom = self._geometry_of(layer, fid)
                if geom is not None:
                    line = geom if geom.type() == LINE else geom.convertToType(LINE, False)
                    if line is not None and not line.isNull():
                        orange.addGeometry(line, QgsCoordinateReferenceSystem(), False)
        cyan = self._band("targets", QColor(0, 200, 220))
        for layer, fid in _live(self.targets) if not self.clip_all else []:
            geom = self._geometry_of(layer, fid)
            if geom is not None:
                line = geom if geom.type() == LINE else geom.convertToType(LINE, False)
                if line is not None and not line.isNull():
                    cyan.addGeometry(line, QgsCoordinateReferenceSystem(), False)
        for band in self._bands.values():
            band.updatePosition()
            band.update()

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ the run
    def _all_targets(self, area, exclude):
        """Every editable line and polygon feature (shown layers) the clip area's box touches."""
        shown = map_layers(self.canvas())
        out = []
        for layer in all_vector_layers():
            if (not layer.isEditable() or layer.geometryType() not in (LINE, POLYGON)
                    or (shown is not None and layer.id() not in shown)):
                continue
            box = area.boundingBox()
            if layer.crs() != self.canvas().mapSettings().destinationCrs():
                box = _transform(self.canvas().mapSettings().destinationCrs(), layer.crs()).transformBoundingBox(box)
            for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(box).setNoAttributes()):
                if (layer.id(), f.id()) not in exclude:
                    out.append((layer, f.id()))
        return out

    def clip(self):
        if not self.inputs:
            self.tell("Pick the input (clip) features first.", ok=False)
            return 0
        area = clip_area([self._geometry_of(layer, fid) for layer, fid in _live(self.inputs)],
                         self._distance_in_map_units())
        if area is None:
            self.tell("Points and lines clip through a buffer: set a Buffer Distance above 0.", ok=False)
            return 0
        exclude = {(layer.id(), fid) for layer, fid in self.inputs}
        targets = self._all_targets(area, exclude) if self.clip_all else _live(self.targets)
        if not targets:
            self.tell("Pick the target features first.", ok=False)
            return 0
        per_layer = {}
        touched = 0
        for layer, fid in targets:
            if not layer.isEditable():
                continue
            g = self._geometry_of(layer, fid)
            if g is None:
                continue
            action = clip_geometry(g, area, self.mode, self.keep_coincident)
            if action[0] == "keep":
                continue
            entry = per_layer.setdefault(layer.id(), (layer, [], [], []))       # (layer, updates, deletes, adds)
            attrs = _copyable(layer, layer.getFeature(fid).attributes())
            multi = QgsWkbTypes.isMultiType(layer.wkbType())
            if action[0] == "delete":
                entry[2].append(fid)
            elif action[0] == "update":
                shaped = DivideTool._shaped(action[1], multi)
                entry[1].append((fid, shaped[0]))
                entry[3].extend((extra, attrs) for extra in shaped[1:])
            else:
                inside, outside = (DivideTool._shaped(p, multi) for p in action[1:])
                entry[1].append((fid, inside[0]))
                entry[3].extend((extra, attrs) for extra in inside[1:] + outside)
            touched += 1
        if not per_layer:
            self.tell("Nothing to clip: no target feature meets the clip area in a way this option changes.", ok=False)
            return 0
        failed = []
        for layer, updates, deletes, adds in per_layer.values():
            layer.beginEditCommand("Clip features")
            ok = all(layer.changeGeometry(fid, _to_layer(self.canvas(), layer, g)) for fid, g in updates)
            ok = ok and all(layer.deleteFeature(fid) for fid in deletes)
            for g, attrs in adds:
                if ok:
                    feature = QgsVectorLayerUtils.createFeature(layer, _to_layer(self.canvas(), layer, g), attrs)
                    ok = layer.addFeature(feature)
            if ok:
                layer.endEditCommand()
            else:
                layer.destroyEditCommand()
                failed.append(layer.name())
            layer.triggerRepaint()
        if failed:
            self.tell(f"Could not edit {', '.join(failed)} - is it in edit mode?", ok=False)
            return 0
        self.inputs, self.targets = [], []
        self._capture = None
        self._result = (f"Clip completed: {touched} feature(s) changed.", True)
        self.say(self._result[0] + " (Ctrl+Z undoes it)")
        self.refresh_page()
        return touched

    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text)


def settings_value(key, default):
    return QgsSettings().value(SETTINGS + key, default)


TOOL_CLASSES = {ClipTool.KEY: ClipTool}

"""
Offset, laid out like the Offset pane of ArcGIS Pro: points are created along a reference line, each at a distance
along it and a distance to its side.

    Interactive   draw the reference line with the Two-Point Line tool (click its start and its end) or pick an existing
                  line with the Trace Line tool (click it)
    By Feature    the first selected line feature is the reference line

    Range         the part of the reference line used: Start from / End at (distances along it; the Distance column
                  counts from Start from; a row beyond End at is left out). The bar shows the line's length
    Values        one new point per row: Distance along the line, Offset to its side, and the Side (None puts the point
                  on the line, Left / Right of the line's direction; a negative Offset goes to the other side)
    Template      the editable point layer that receives the points (they take its default attribute values)

Show Preview marks where the points will go; the reference line is drawn in blue with an arrow showing its direction.
The new points are selected afterwards. One undoable edit command.
"""

import math
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..selection import _transform, all_vector_layers, map_layers
from .buffer import layer_icon
from .page import DoneStrip, SelectedTree, button, check, project_layer, section_label, selection_banner, spin
from .pick import _to_layer, pick_features
from .reshape import PickTool
from .split import TABS_QSS

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
POINT = Qgis.GeometryType.Point
POLYGON = Qgis.GeometryType.Polygon
SIDES = ("None", "Left", "Right")
METHODS = (("two_point", "Two-Point Line", "Two-Point Line Tool"), ("trace", "Trace Line", "Trace Line Tool"))
BLUE = QColor(51, 153, 255)


def point_along(line, distance, offset, side):
    """
    The point `distance` along `line` (a QgsGeometry line) and `offset` to its side: side "Left" / "Right" of the
    line's direction (a negative offset goes to the other side), "None" on the line. None when beyond either end.
    """
    length = line.length()
    if distance < -1e-9 or distance > length + 1e-9:
        return None
    s = max(0.0, min(length, distance))
    here = line.interpolate(s).asPoint()
    if side == "None" or abs(offset) < 1e-12:
        return QgsPointXY(here)
    angle = line.interpolateAngle(s)                          # clockwise from north
    direction = (math.sin(angle), math.cos(angle))
    left = (-direction[1], direction[0])
    signed = offset if side == "Left" else -offset
    return QgsPointXY(here.x() + left[0] * signed, here.y() + left[1] * signed)


def number_of(text, default=0.0):
    """The number at the start of a cell's text ("100 m" -> 100.0)."""
    text = text.strip().replace(",", "")
    end = 0
    while end < len(text) and (text[end].isdigit() or text[end] in "+-."):
        end += 1
    try:
        return float(text[:end])
    except ValueError:
        return default


class OffsetTool(PickTool):
    KEY = "offset"
    LABEL = "Offset"
    HINT = "draw or pick a reference line, give the distances and offsets, then press Create."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = False
    SELECT_ONE_SHOT = False                # By Feature: clicking selects until you press Create

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.show_preview = settings.value(SETTINGS + "offset_preview", True, type=bool)
        self.method = settings.value(SETTINGS + "offset_method", "two_point")
        if self.method not in ("two_point", "trace"):
            self.method = "two_point"
        self.tab = 0                       # 0 Interactive, 1 By Feature
        self.reference = None              # the reference line, in the map's CRS
        self._start = None                 # the first click of a two-point line
        self.info = self.tab_bar = self.method_row = self.banner = None
        self.method_checks = {}
        self.start_spin = self.end_spin = self.length_info = self.table = self.template_combo = None
        self.create_button = self.preview_check = None
        self.unit_label_start = self.unit_label_end = None
        self._open_row = None
        self._bands = {}
        self._listed = []
        self._open = False
        self._syncing = False
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        self._open = True
        super().activate()
        self._show_reference()
        self.refresh_preview()

    def deactivate(self):
        self._open = False
        self._start = None
        self._clear_bands()
        super().deactivate()
        self._clear_bands()

    def dispose(self):
        for band in self._bands.values():
            with suppress(Exception):
                self.canvas().scene().removeItem(band)
        self._bands = {}
        super().dispose()

    def reset(self):
        self._start = None
        super().reset()

    def start_select_mode(self):
        self.set_selecting(self.tab == 1)               # By Feature selects; Interactive draws or traces

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        self.tab_bar = QTabBar()
        self.tab_bar.setStyleSheet(TABS_QSS)
        self.tab_bar.addTab("Interactive")
        self.tab_bar.addTab("By Feature")
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setCurrentIndex(self.tab)
        self.tab_bar.currentChanged.connect(self.set_tab)
        box.addWidget(self.tab_bar)
        self.method_row = QWidget()
        mr = QHBoxLayout(self.method_row)
        mr.setContentsMargins(0, 0, 0, 0)
        self.method_checks = {}
        for key, short, long_text in METHODS:
            box_check = QCheckBox(short)
            box_check.setToolTip(long_text)
            box_check.setChecked(key == self.method)
            box_check.clicked.connect(lambda _c=False, k=key: self.set_method(k))
            self.method_checks[key] = box_check
            mr.addWidget(box_check, 1)
        box.addWidget(self.method_row)
        self.banner = selection_banner(lambda: self.set_selecting(not self._selecting))
        self._select_button = self.banner
        self.banner.setChecked(self._selecting)
        box.addWidget(self.banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)

    def build_page(self, box):
        box.addWidget(section_label("Range"))
        for key, text in (("start", "Start from"), ("end", "End at")):
            row = QHBoxLayout()
            row.addWidget(QLabel(text), 1)
            box_spin = spin(0.0, 0.0, 1e9, 5, 1.0)
            box_spin.valueChanged.connect(lambda _v: self._range_changed())
            setattr(self, f"{key}_spin", box_spin)
            row.addWidget(box_spin)
            label = QLabel(self._unit_text())
            setattr(self, f"unit_label_{key}", label)
            row.addWidget(label)
            box.addLayout(row)
        self.length_info = DoneStrip()
        box.addWidget(self.length_info)
        box.addWidget(section_label("Values"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Distance", "Offset", "Side"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(130)
        self.table.setMaximumHeight(220)
        self.table.cellClicked.connect(self._table_clicked)
        self.table.itemChanged.connect(self._cell_edited)
        box.addWidget(self.table)
        self._reset_table()
        box.addWidget(section_label("Template"))
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        self._rebuild_templates()
        self._apply_tab()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        self.create_button = button("Create", self.create, "Add the points to the template layer (Ctrl+Z undoes it)")
        row.addWidget(self.create_button)
        box.addLayout(row)

    # ------------------------------------------------------------------ units
    def _unit(self):
        return QgsProject.instance().distanceUnits()

    def _unit_text(self):
        return QgsUnitTypes.toAbbreviatedString(self._unit())

    def _factor(self):
        """Map units in one project distance unit."""
        crs = self.canvas().mapSettings().destinationCrs()
        return QgsUnitTypes.fromUnitToUnitFactor(self._unit(), crs.mapUnits())

    # ------------------------------------------------------------------ tabs and method
    def set_tab(self, index):
        self.tab = int(index)
        self._start = None
        self._apply_tab()
        self._load_reference()
        self.refresh_page()

    def _apply_tab(self):
        interactive = self.tab == 0
        self.method_row.setVisible(interactive)
        self.banner.setVisible(not interactive)
        self.tree.setVisible(not interactive)
        self.set_selecting(not interactive)

    def set_method(self, key):
        self.method = key
        QgsSettings().setValue(SETTINGS + "offset_method", key)
        for k, box_check in self.method_checks.items():
            box_check.setChecked(k == key)                  # one of the two is always on
        self._start = None
        self.say("click the start and the end of the reference line." if key == "two_point"
                 else "click the line to use as the reference.")

    # ------------------------------------------------------------------ the reference line
    def _load_reference(self):
        """By Feature: the first selected line feature; Interactive keeps what was drawn or picked."""
        if self.tab == 1:
            canvas_crs = self.canvas().mapSettings().destinationCrs()
            self.reference = None
            for layer in all_vector_layers():
                if layer.isSpatial() and layer.geometryType() == LINE and layer.selectedFeatureCount():
                    fid = sorted(layer.selectedFeatureIds())[0]
                    g = QgsGeometry(layer.getFeature(fid).geometry())
                    if g.isNull() or g.isEmpty():
                        continue
                    if layer.crs() != canvas_crs:
                        g.transform(_transform(layer.crs(), canvas_crs))
                    self.reference = self._single(g)
                    break
        self._range_from_reference()
        self._show_reference()

    @staticmethod
    def _single(geom):
        """A single-part line: the first part of a multi-part one."""
        parts = geom.asGeometryCollection()
        return QgsGeometry(parts[0]) if parts else None

    def _range_from_reference(self):
        self._syncing = True
        try:
            length = self.reference.length() if self.reference is not None else 0.0
            self.start_spin.setValue(0.0)
            self.end_spin.setValue(length / self._factor())
        finally:
            self._syncing = False
        self._range_changed()

    def _range_changed(self):
        if self._syncing or self.length_info is None:
            return
        if self.reference is None:
            self.length_info.hide()
        else:
            text = f"Line length is {self.reference.length() / self._factor():,.2f} {self._unit_text()}"
            self.length_info.show_text(text, ok="info")
        self.refresh_preview()

    def _set_reference(self, line):
        self.reference = line
        self._range_from_reference()
        self._show_reference()
        self.refresh_page()

    def _band(self, key, colour, width):
        band = self._bands.get(key)
        if band is None:
            band = QgsRubberBand(self.canvas(), POLYGON if key == "head" else LINE)
            band.setColor(colour)
            band.setWidth(width)
            if key == "head":
                band.setFillColor(colour)
            self._bands[key] = band
        return band

    def _clear_bands(self):
        for key, band in self._bands.items():
            band.reset(POLYGON if key == "head" else LINE)
        dots = self._bands.get("dots")
        if dots is not None:
            dots.reset(POINT)

    def _show_reference(self, tail=None):
        """The reference line in blue with an arrow head at its end (the live line while the 2nd point is chosen)."""
        for key in ("reference", "head"):
            if key in self._bands:
                self._bands[key].reset(POLYGON if key == "head" else LINE)
        if not self._open:
            return
        pts = None
        if self._start is not None and tail is not None:
            pts = [self._start, QgsPointXY(tail)]
        elif self.reference is not None:
            pts = [QgsPointXY(v.x(), v.y()) for v in self.reference.vertices()]
        if not pts or len(pts) < 2:
            return
        line = self._band("reference", BLUE, 3)
        for i, p in enumerate(pts):
            line.addPoint(p, i == len(pts) - 1)
        tip, behind = pts[-1], pts[-2]
        dx, dy = tip.x() - behind.x(), tip.y() - behind.y()
        n = math.hypot(dx, dy)
        if n < 1e-12:
            return
        d = (dx / n, dy / n)
        mupp = self.canvas().mapUnitsPerPixel()
        base = QgsPointXY(tip.x() - d[0] * 14 * mupp, tip.y() - d[1] * 14 * mupp)
        side = (-d[1] * 5 * mupp, d[0] * 5 * mupp)
        head = self._band("head", BLUE, 1)
        for i, p in enumerate((tip, QgsPointXY(base.x() + side[0], base.y() + side[1]),
                               QgsPointXY(base.x() - side[0], base.y() - side[1]))):
            head.addPoint(p, i == 2)

    # ------------------------------------------------------------------ the values table
    def _reset_table(self):
        table = self.table
        table.blockSignals(True)
        table.setRowCount(0)
        self._open_row = None
        self._add_row("0", "0", "None", refresh=False)
        self._open_row = table.rowCount()
        table.insertRow(self._open_row)
        item = QTableWidgetItem("Click to add new row.")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        table.setItem(self._open_row, 0, item)
        table.setSpan(self._open_row, 0, 1, 4)
        table.blockSignals(False)

    def _add_row(self, distance="0", offset="0", side="None", refresh=True):
        table = self.table
        table.blockSignals(True)
        row = table.rowCount() if self._open_row is None else self._open_row
        table.insertRow(row)
        number = QTableWidgetItem(str(row + 1))
        number.setFlags(Qt.ItemFlag.ItemIsEnabled)
        number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, 0, number)
        for col, text in ((1, distance), (2, offset)):
            table.setItem(row, col, QTableWidgetItem(f"{text} {self._unit_text()}"))
        combo = QComboBox()
        combo.addItems(list(SIDES))
        combo.setCurrentText(side)
        combo.currentTextChanged.connect(lambda _t: self.refresh_preview())
        table.setCellWidget(row, 3, combo)
        if self._open_row is not None:
            self._open_row += 1
        table.blockSignals(False)
        if refresh:
            self.refresh_preview()

    def _table_clicked(self, row, _col):
        if self._open_row is not None and row == self._open_row:
            self._add_row()

    def _cell_edited(self, _item):
        self.refresh_preview()

    def rows(self):
        """[(distance, offset, side)] in the project's distance unit, one per real row."""
        out = []
        for row in range(self._open_row if self._open_row is not None else self.table.rowCount()):
            combo = self.table.cellWidget(row, 3)
            out.append((number_of(self.table.item(row, 1).text()), number_of(self.table.item(row, 2).text()),
                        combo.currentText() if combo is not None else "None"))
        return out

    # ------------------------------------------------------------------ the template
    def _template_layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.geometryType() == POINT
                and (shown is None or layer.id() in shown)]

    def _rebuild_templates(self):
        current = self.template_combo.currentData() or QgsSettings().value(SETTINGS + "offset_template", "")
        layers = self._template_layers()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for layer in layers:
            self.template_combo.addItem(layer_icon(layer), layer.name(), layer.id())
        self._listed = [layer.id() for layer in layers]
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(current)))
        self.template_combo.blockSignals(False)

    def _template_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "offset_template", self.template_combo.currentData() or "")
        self.refresh_preview()

    def template(self):
        return project_layer(self.template_combo.currentData()) if self.template_combo is not None else None

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "offset_preview", self.show_preview)
        if getattr(self, "preview_check", None) is not None and self.preview_check.isChecked() != self.show_preview:
            self.preview_check.setChecked(self.show_preview)
        self.refresh_preview()

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.create_button is None or sip.isdeleted(self.create_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        if self.tab == 1:
            self._load_reference()
        self.refresh_preview()

    # ------------------------------------------------------------------ the points
    def plan(self):
        """[QgsPointXY in the map's CRS] for the rows that fall inside the range."""
        if self.reference is None:
            return []
        factor = self._factor()
        start, end = self.start_spin.value() * factor, self.end_spin.value() * factor
        points = []
        for distance, offset, side in self.rows():
            along = start + distance * factor
            if along > end + 1e-9 or along < start - 1e-9:
                continue
            p = point_along(self.reference, along, offset * factor, side)
            if p is not None:
                points.append(p)
        return points

    def refresh_preview(self):
        if "dots" in self._bands:
            self._bands["dots"].reset(POINT)
        if self.create_button is None or not self._open:
            return
        points = self.plan()
        self.create_button.setEnabled(bool(points) and self.template() is not None)
        if not self.show_preview or not points:
            return
        dots = self._bands.get("dots")
        if dots is None:
            dots = QgsRubberBand(self.canvas(), POINT)
            dots.setColor(QColor(30, 30, 160))
            dots.setFillColor(QColor(255, 255, 255))
            dots.setIconSize(9)
            with suppress(AttributeError):
                dots.setIcon(QgsRubberBand.IconType.ICON_CROSS)
            self._bands["dots"] = dots
        for i, p in enumerate(points):
            dots.addPoint(p, i == len(points) - 1)

    # ------------------------------------------------------------------ the map (Interactive tab)
    def on_move(self, point):
        if self.tab == 0 and self.method == "two_point" and self._start is not None:
            self._show_reference(point)

    def on_click(self, point):
        if self.tab != 0:
            return
        if self.method == "two_point":
            if self._start is None:
                self._start = QgsPointXY(point)
                self.say("now click the end of the line.")
                return
            if (point.x() - self._start.x()) ** 2 + (point.y() - self._start.y()) ** 2 < 1e-18:
                self.say("the two points are the same.")
                return
            line = QgsGeometry.fromPolylineXY([self._start, QgsPointXY(point)])
            self._start = None
            self._set_reference(line)
            return
        hits = [h for h in pick_features(self.canvas(), point, (LINE,))]
        if not hits:
            self.say("no line there.")
            return
        geom = hits[0][2]
        parts = geom.asGeometryCollection()
        here = QgsGeometry.fromPointXY(point)
        nearest = min(parts, key=lambda g: g.distance(here)) if parts else None
        if nearest is not None:
            self._set_reference(QgsGeometry(nearest))

    def canvasMoveEvent(self, e):
        if self.forward_select("canvasMoveEvent", e):
            return
        point = QgsPointXY(e.snapPoint())
        self.snap_indicator.setMatch(e.mapPointMatch())
        self.on_move(point)

    # ------------------------------------------------------------------ the run
    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))

    def create(self):
        template = self.template()
        if template is None:
            self.tell("Choose an editable point layer as the Template.", ok=False)
            return 0
        if self.reference is None:
            self.tell("Draw or pick the reference line first.", ok=False)
            return 0
        points = self.plan()
        if not points:
            self.tell("No row falls inside the range - check Distance, Start from and End at.", ok=False)
            return 0
        multi = QgsWkbTypes.isMultiType(template.wkbType())
        template.beginEditCommand("Offset")
        made = []
        ok = True
        for p in points:
            geom = QgsGeometry.fromPointXY(p)
            if multi:
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
        self.tell(f"Offset completed: {len(made)} point(s) added to {template.name()}.")
        self.refresh_page()
        return len(made)


TOOL_CLASSES = {OffsetTool.KEY: OffsetTool}

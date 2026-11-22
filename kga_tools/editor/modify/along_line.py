"""
Points On Line, laid out like the Create Points Along Line pane of ArcGIS Pro: the length of the line, the
points to make, and a Create button. The points go into a point layer you choose.

    Line       Select Line: the first selected line feature (the Select button of the pane picks it on the map).
               Draw Line: click the points of a path on the map instead - right-click, double-click or Enter finishes it,
               Backspace removes the last point, Esc starts again. The line is drawn in blue with arrows showing the
               direction the distances are counted in.
    Target     the editable point layer that receives the points (they take its default attribute values)

    Number of Points   N points spaced equally between the two ends of the line (the ends are not counted)
    Equal Distance     a point every Distance from the Orientation end, as many as fit on the line
    Distances          the table: each row is a Distance and a Number of Points - that many points, Distance apart,
                       one after the other from the Orientation end. Remaining Length is what is left of the line
    Orientation        From Start / From End: which end the distances are counted from (Equal Distance and Distances)
    Additional Points  also a point at the start and / or at the end of the line

Show Preview marks the points that will be made. Lengths are measured on the ground when the project has an ellipsoid
(what $length reports), else flat in the map's units. The new points are selected afterwards; one undoable edit.
"""

from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.compat import scoped
from ..selection import _transform, all_vector_layers, map_layers
from . import geometry_ops as G
from .buffer import layer_icon
from .ground import LengthMeasure
from .offset import number_of
from .page import DoneStrip, SelectedTree, button, check, project_layer, section_label, selection_banner, spin
from .pick import _to_layer
from .reshape import PickTool
from .split import TABS_QSS

SETTINGS = "kga_editor/modify_along_"
LINE = Qgis.GeometryType.Line
POINT = Qgis.GeometryType.Point
POLYGON = Qgis.GeometryType.Polygon
BLUE = QColor(51, 153, 255)
try:                                        # QGIS >= 3.40 / 4
    MARKER_BOX = Qgis.VertexMarkerType.Box
except AttributeError:
    MARKER_BOX = scoped(QgsVertexMarker, 'IconType', 'ICON_BOX')
MODES = (("number", "Number of Points"), ("distance", "Equal Distance"), ("table", "Distances"))
UNITS = ((Qgis.DistanceUnit.Meters, "m"), (Qgis.DistanceUnit.Kilometers, "km"), (Qgis.DistanceUnit.Feet, "ft"),
         (Qgis.DistanceUnit.Yards, "yd"), (Qgis.DistanceUnit.Miles, "mi"))
EPS = 1e-9
MAX_POINTS = 10000                          # more than this many points is a mistake (a distance far too small)
START_TEXT = "Select a line, or draw one, then choose the points to make."


# ---------------------------------------------------------------------------------------- the maths
def positions(length, mode, number=0, distance=0.0, rows=(), from_end=False, at_start=False, at_end=False):
    """
    The distances along a line of `length`, counted from its START, where points go; sorted, each once.
      "number":   `number` points equally spaced between the ends (the ends themselves are the Additional Points)
      "distance": a point every `distance` from the chosen end, as many as fit (one exactly at the far end counts)
      "table":    rows = [(distance, count)]: `count` points `distance` apart, one row after the other from the chosen end;
                  the points stop at the end of the line
    `at_start` / `at_end` add a point at either end. At most MAX_POINTS.
    """
    out = []
    if length > EPS:
        if mode == "number":
            n = max(0, int(number))
            out = [length * k / (n + 1) for k in range(1, min(n, MAX_POINTS + 1) + 1)]
        elif mode == "distance":
            if distance > EPS:
                out = _walk(length, [(distance, int(length / distance) + 1)], from_end)
        else:
            out = _walk(length, rows, from_end)
        if at_start:
            out.append(0.0)
        if at_end:
            out.append(length)
    out = sorted(max(0.0, min(length, d)) for d in out)
    unique = []
    for d in out:
        if not unique or d - unique[-1] > EPS * max(1.0, length):
            unique.append(d)
    return unique[:MAX_POINTS + 1]                                # one over the limit tells the caller it is too many


def _walk(length, rows, from_end):
    """Positions from the start of the line for rows of (distance, count) walked from the chosen end."""
    out, done = [], 0.0
    for distance, count in rows:
        if distance <= EPS:
            continue
        for _ in range(max(0, int(count))):
            done += distance
            if done > length + EPS * max(1.0, length) or len(out) > MAX_POINTS:
                return out
            out.append(length - done if from_end else done)
    return out


def remaining_length(length, rows):
    """What is left of the line after the Distances table: the length less every row's distance x number of points."""
    return length - sum(distance * max(0, int(count)) for distance, count in rows if distance > 0)


# ---------------------------------------------------------------------------------------- the tool
class PointsAlongLineTool(PickTool):
    KEY = "points_along_line"
    LABEL = "Points On Line"
    HINT = "select or draw a line, choose the points to make, then press Create."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = False
    SELECT_ONE_SHOT = False                # Select Line: clicking selects until you press Create

    def __init__(self, canvas, iface, select_tool=None):
        s = QgsSettings()
        self.show_preview = s.value(SETTINGS + "preview", True, type=bool)
        self.mode = s.value(SETTINGS + "mode", "number")
        if self.mode not in dict(MODES):
            self.mode = "number"
        self.from_end = s.value(SETTINGS + "from_end", False, type=bool)
        self.at_start = s.value(SETTINGS + "at_start", True, type=bool)
        self.at_end = s.value(SETTINGS + "at_end", True, type=bool)
        self.source = 0                    # 0 Select Line, 1 Draw Line
        self.reference = None              # the line, in the map's CRS
        self.path = []                     # Draw Line: the points clicked so far
        self._finished = False             # the drawn path is complete: the next click starts a new one
        self._ignore_next_release = False
        self.info = self.source_bar = self.mode_bar = self.banner = self.template_combo = None
        self.unit_combo = self.length_label = self.remaining_row = self.remaining_label = None
        self.orientation_button = self.at_start_check = self.at_end_check = None
        self.number_spin = self.distance_spin = self.table = None
        self.create_button = self.preview_check = None
        self.rows_widgets = []
        self._open_row = None
        self._bands = {}
        self._marker = None
        self._listed = []
        self._open = False
        self.too_many = False              # the points asked for are more than MAX_POINTS
        self._warned = False
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        self._open = True
        super().activate()
        self.refresh_page()

    def deactivate(self):
        self._open = False
        self._clear_bands()
        super().deactivate()

    def dispose(self):
        for item in list(self._bands.values()) + ([self._marker] if self._marker is not None else []):
            with suppress(Exception):
                self.canvas().scene().removeItem(item)
        self._bands, self._marker = {}, None
        super().dispose()

    def reset(self):
        self.path = []
        self._finished = False
        super().reset()

    def start_select_mode(self):
        self.set_selecting(self.source == 0)           # Select Line selects; Draw Line draws

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        self.source_bar = QTabBar()
        self.source_bar.setStyleSheet(TABS_QSS)
        self.source_bar.addTab("Select Line")
        self.source_bar.addTab("Draw Line")
        self.source_bar.setDrawBase(False)
        self.source_bar.setCurrentIndex(self.source)
        self.source_bar.currentChanged.connect(self.set_source)
        box.addWidget(self.source_bar)
        self.banner = selection_banner(lambda: self.set_selecting(not self._selecting))
        self._select_button = self.banner
        self.banner.setChecked(self._selecting)
        box.addWidget(self.banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)

    def build_page(self, box):
        box.addWidget(section_label("Target point layer"))
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.setToolTip("The editable point layer that receives the points")
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        row = QHBoxLayout()
        row.addWidget(QLabel("Line Length:"))
        self.length_label = QLabel("-")
        row.addWidget(self.length_label, 1)
        self.unit_combo = QComboBox()
        for unit, text in UNITS:
            self.unit_combo.addItem(text, unit)
        saved = QgsSettings().value(SETTINGS + "unit", None)
        default = self.unit_combo.findData(QgsProject.instance().distanceUnits())
        index = self.unit_combo.findText(saved) if saved else -1
        self.unit_combo.setCurrentIndex(index if index >= 0 else max(0, default))
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        row.addWidget(self.unit_combo)
        box.addLayout(row)
        self.remaining_row = QHBoxLayout()
        self.remaining_row.addWidget(QLabel("Remaining Length:"))
        self.remaining_label = QLabel("-")
        self.remaining_row.addWidget(self.remaining_label, 1)
        box.addLayout(self.remaining_row)
        main_box = box                      # what follows needs a target layer: one box to switch off with it
        self.options = QWidget()
        box = QVBoxLayout(self.options)
        box.setContentsMargins(0, 0, 0, 0)
        self.mode_bar = QTabBar()
        self.mode_bar.setStyleSheet(TABS_QSS)
        for key, text in MODES:
            self.mode_bar.addTab(text)
        self.mode_bar.setDrawBase(False)
        self.mode_bar.setCurrentIndex([k for k, _t in MODES].index(self.mode))
        self.mode_bar.currentChanged.connect(self.set_mode_index)
        box.addWidget(self.mode_bar)
        orient = QHBoxLayout()
        orient.addWidget(QLabel("Orientation:"))
        self.orientation_button = QPushButton("From End" if self.from_end else "From Start")
        self.orientation_button.setObjectName("KgaModifyButton")
        self.orientation_button.setToolTip("Which end of the line the distances are counted from")
        self.orientation_button.clicked.connect(lambda _c=False: self.set_from_end(not self.from_end))
        orient.addWidget(self.orientation_button)
        orient.addStretch(1)
        self.orientation_row = orient
        box.addLayout(orient)
        box.addWidget(section_label("Additional Points"))
        self.at_start_check = check("At start of line", self.at_start, self.set_at_start)
        self.at_end_check = check("At end of line", self.at_end, self.set_at_end)
        box.addWidget(self.at_start_check)
        box.addWidget(self.at_end_check)
        number_row = QHBoxLayout()
        self.number_label = QLabel("Number of Points:")
        number_row.addWidget(self.number_label)
        self.number_spin = QSpinBox()
        self.number_spin.setRange(0, 10000)
        self.number_spin.setValue(int(QgsSettings().value(SETTINGS + "number", 10)))
        self.number_spin.valueChanged.connect(self._number_changed)
        number_row.addWidget(self.number_spin)
        number_row.addStretch(1)
        box.addLayout(number_row)
        self.number_row = number_row
        distance_row = QHBoxLayout()
        self.distance_label = QLabel("Distance:")
        distance_row.addWidget(self.distance_label)
        self.distance_spin = spin(float(QgsSettings().value(SETTINGS + "distance", 100.0)), 0.0, 1e9, 3, 10.0)
        self.distance_spin.valueChanged.connect(self._distance_changed)
        distance_row.addWidget(self.distance_spin)
        distance_row.addStretch(1)
        box.addLayout(distance_row)
        self.distance_row = distance_row
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Distance", "Number Of Points"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(140)
        self.table.cellClicked.connect(self._table_clicked)
        self.table.itemChanged.connect(self._cell_edited)
        box.addWidget(self.table)
        main_box.addWidget(self.options)
        self._reset_table()
        self._rebuild_templates()
        self._apply_source()
        self._apply_mode()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        self.create_button = button("Create", self.create, "Add the points to the target layer (Ctrl+Z undoes it)")
        row.addWidget(self.create_button)
        box.addLayout(row)

    # ------------------------------------------------------------------ what is shown for the source and the mode
    def set_source(self, index):
        self.source = int(index)
        self.path, self._finished = [], False
        self._apply_source()
        self._load_reference()
        self.refresh_page()

    def _apply_source(self):
        selecting = self.source == 0
        self.banner.setVisible(selecting)
        self.tree.setVisible(selecting)
        self.set_selecting(selecting)

    def set_mode_index(self, index):
        self.mode = MODES[int(index)][0]
        QgsSettings().setValue(SETTINGS + "mode", self.mode)
        self._apply_mode()
        self.refresh_preview()

    def set_mode(self, key):
        self.mode_bar.setCurrentIndex([k for k, _t in MODES].index(key))

    def _apply_mode(self):
        number, distance, table = self.mode == "number", self.mode == "distance", self.mode == "table"
        for layout, shown in ((self.number_row, number), (self.distance_row, distance), (self.orientation_row, not number)):
            for i in range(layout.count()):
                widget = layout.itemAt(i).widget()
                if widget is not None:
                    widget.setVisible(shown)
        self.table.setVisible(table)
        for i in range(self.remaining_row.count()):
            widget = self.remaining_row.itemAt(i).widget()
            if widget is not None:
                widget.setVisible(table)
        self.update_lengths()

    # ------------------------------------------------------------------ options
    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "preview", self.show_preview)
        self.refresh_preview()

    def set_from_end(self, on):
        self.from_end = bool(on)
        QgsSettings().setValue(SETTINGS + "from_end", self.from_end)
        self.orientation_button.setText("From End" if self.from_end else "From Start")
        self.refresh_preview()

    def set_at_start(self, on):
        self.at_start = bool(on)
        QgsSettings().setValue(SETTINGS + "at_start", self.at_start)
        self.refresh_preview()

    def set_at_end(self, on):
        self.at_end = bool(on)
        QgsSettings().setValue(SETTINGS + "at_end", self.at_end)
        self.refresh_preview()

    def _number_changed(self, value):
        QgsSettings().setValue(SETTINGS + "number", int(value))
        self.refresh_preview()

    def _distance_changed(self, value):
        QgsSettings().setValue(SETTINGS + "distance", float(value))
        self.refresh_preview()

    def _unit_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "unit", self.unit_combo.currentText())
        self.update_lengths()
        self.refresh_preview()

    # ------------------------------------------------------------------ units and lengths
    def measure(self):
        return LengthMeasure(self.canvas())

    def unit_factor(self):
        """Measure units (metres on the ground, else the map's units) in one of the chosen unit."""
        return QgsUnitTypes.fromUnitToUnitFactor(self.unit_combo.currentData(), self.measure().unit)

    def line_length(self):
        """The reference line's length in the measure's unit (0 with no line)."""
        return self.measure().length(self.reference) if self.reference is not None else 0.0

    def _text(self, value):
        return f"{value / self.unit_factor():,.3f}"

    def update_lengths(self):
        if self.length_label is None or sip.isdeleted(self.length_label):
            return
        length = self.line_length()
        self.length_label.setText(self._text(length) if self.reference is not None else "-")
        rows = self.rows()
        if self.reference is None:
            self.remaining_label.setText("-")
        else:
            left = remaining_length(length, [(d * self.unit_factor(), n) for d, n in rows])
            self.remaining_label.setText(self._text(left))
            self.remaining_label.setStyleSheet("color: #c0392b;" if left < -EPS else "")

    # ------------------------------------------------------------------ the Distances table
    def _reset_table(self):
        table = self.table
        table.blockSignals(True)
        table.setRowCount(0)
        self._open_row = 0
        table.insertRow(0)
        item = QTableWidgetItem("Click to add new row.")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        table.setItem(0, 0, item)
        table.setSpan(0, 0, 1, 2)
        table.blockSignals(False)

    def add_row(self, distance="", count="1", refresh=True):
        table = self.table
        table.blockSignals(True)
        row = self._open_row
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(str(distance)))
        table.setItem(row, 1, QTableWidgetItem(str(count)))
        self._open_row += 1
        table.blockSignals(False)
        if refresh:
            self.update_lengths()
            self.refresh_preview()

    def remove_row(self, row):
        if 0 <= row < self._open_row:
            self.table.removeRow(row)
            self._open_row -= 1
            self.update_lengths()
            self.refresh_preview()

    def clear_rows(self):
        self._reset_table()
        self.update_lengths()
        self.refresh_preview()

    def _table_clicked(self, row, _col):
        if row == self._open_row:
            self.add_row()

    def _cell_edited(self, _item):
        self.update_lengths()
        self.refresh_preview()

    def rows(self):
        """[(distance in the chosen unit, number of points)] of the table, rows with a distance only."""
        out = []
        for row in range(self._open_row or 0):
            distance = number_of(self.table.item(row, 0).text(), 0.0)
            count = int(number_of(self.table.item(row, 1).text(), 1.0))
            if distance > 0 and count > 0:
                out.append((distance, count))
        return out

    # ------------------------------------------------------------------ the target layer
    def _template_layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.geometryType() == POINT
                and (shown is None or layer.id() in shown)]

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
        self.refresh_preview()

    def template(self):
        return project_layer(self.template_combo.currentData()) if self.template_combo is not None else None

    # ------------------------------------------------------------------ the line
    def _load_reference(self):
        """Select Line: the first selected line feature (its first part); Draw Line keeps what was drawn."""
        if self.source == 0:
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
                    parts = g.asGeometryCollection()
                    self.reference = QgsGeometry(parts[0]) if parts else None
                    break
        elif self._finished and len(self.path) > 1:
            self.reference = QgsGeometry.fromPolylineXY(self.path)
        else:
            self.reference = None
        self.update_lengths()
        self._draw_reference()

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.create_button is None or sip.isdeleted(self.create_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        if self.source == 0:
            self._load_reference()
        self.refresh_preview()

    # ------------------------------------------------------------------ drawing on the map
    def _band(self, key, colour, width, kind=LINE, dashed=False):
        band = self._bands.get(key)
        if band is None:
            band = QgsRubberBand(self.canvas(), kind)
            band.setColor(colour)
            band.setWidth(width)
            if kind == POLYGON:
                band.setFillColor(QColor(255, 255, 255, 0))
            if dashed:
                band.setLineStyle(Qt.PenStyle.DashLine)
            self._bands[key] = band
        return band

    def _clear_bands(self):
        for key, band in self._bands.items():
            band.reset(POLYGON if key == "arrows" else POINT if key == "dots" else LINE)
        if self._marker is not None:
            self._marker.hide()

    def _draw_reference(self):
        """The line in blue with hollow arrows along it, or the sketch of the path being drawn."""
        for key, kind in (("reference", LINE), ("arrows", POLYGON), ("sketch", LINE)):
            if key in self._bands:
                self._bands[key].reset(kind)
        if not self._open:
            return
        if self.source == 1 and not self._finished:
            return self._draw_sketch(None)
        if self.reference is None:
            return
        pts = [QgsPointXY(v.x(), v.y()) for v in self.reference.vertices()]
        line = self._band("reference", BLUE, 3)
        for i, p in enumerate(pts):
            line.addPoint(p, i == len(pts) - 1)
        arrows = self._band("arrows", BLUE, 2, POLYGON)
        mupp = self.canvas().mapUnitsPerPixel()
        for tri in G.direction_arrows(self.reference, 90 * mupp, 14 * mupp):
            arrows.addGeometry(QgsGeometry.fromPolygonXY([tri + [tri[0]]]), QgsCoordinateReferenceSystem(), False)
        arrows.updatePosition()
        arrows.update()

    def _draw_sketch(self, tail):
        band = self._band("sketch", QColor(0, 0, 0), 2, dashed=True)
        band.reset(LINE)
        shown = self.path + ([tail] if tail is not None else [])
        for i, p in enumerate(shown):
            band.addPoint(p, i == len(shown) - 1)
        if self._marker is None and shown:
            self._marker = QgsVertexMarker(self.canvas())
            self._marker.setIconType(MARKER_BOX)
            self._marker.setColor(QColor(0, 140, 60))
            self._marker.setIconSize(8)
            self._marker.setPenWidth(2)
        if self._marker is not None:
            if shown:
                self._marker.setCenter(shown[0])
                self._marker.show()
            else:
                self._marker.hide()

    # ------------------------------------------------------------------ the mouse and keys (Draw Line)
    def on_move(self, point):
        if self.source == 1 and self.path and not self._finished:
            self._draw_sketch(point)

    def on_click(self, point):
        if self.source != 1:
            return
        if self._ignore_next_release:                  # the second click of a double-click
            self._ignore_next_release = False
            return
        if self._finished:                             # a new path
            self.path, self._finished, self.reference = [], False, None
            self.update_lengths()
            self.refresh_preview()
        if not self.path or point != self.path[-1]:
            self.path.append(QgsPointXY(point))
        self._draw_sketch(None)
        self.say("click the next point; right-click or Enter finishes the line.")

    def canvasDoubleClickEvent(self, e):
        if self.source == 1 and e.button() == Qt.MouseButton.LeftButton and not self._selecting:
            self._ignore_next_release = True
            self.finish_path()

    def on_right_click(self, e):
        if self.source == 1:
            self.finish_path()
        else:
            super().on_right_click(e)

    def finish_path(self):
        """The drawn path becomes the line the points go on (two points at least)."""
        if self._finished or len(self.path) < 2:
            return False
        self._finished = True
        self.reference = QgsGeometry.fromPolylineXY(self.path)
        self._draw_reference()
        self.update_lengths()
        self.refresh_preview()
        self.refresh_page()
        return True

    def remove_last_point(self):
        if self.path and not self._finished:
            self.path.pop()
            self._draw_sketch(None)

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):
            return
        if self.source == 1 and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finish_path()
            e.ignore()
        elif self.source == 1 and e.key() == Qt.Key.Key_Backspace:
            self.remove_last_point()
            e.ignore()
        elif self.source == 1 and e.key() == Qt.Key.Key_Escape and self.path:
            self.path, self._finished, self.reference = [], False, None
            self._draw_reference()
            self.update_lengths()
            self.refresh_preview()
            e.ignore()
        else:
            super().keyPressEvent(e)

    # ------------------------------------------------------------------ the points
    def distances(self):
        """Where the points go: distances in the measure's unit along the line from its start."""
        if self.reference is None:
            return []
        factor = self.unit_factor()
        rows = [(d * factor, n) for d, n in self.rows()]
        return positions(self.line_length(), self.mode, self.number_spin.value(), self.distance_spin.value() * factor,
                         rows, self.from_end, self.at_start, self.at_end)

    def plan(self):
        """[QgsPointXY in the map's CRS] of the points that will be made."""
        if self.reference is None:
            return []
        measure = self.measure()
        pts = [QgsPointXY(v.x(), v.y()) for v in self.reference.vertices()]
        distances = self.distances()
        self.too_many = len(distances) > MAX_POINTS
        if self.too_many:
            return []
        flats = measure.flats_at(pts, distances) if measure.usable_for(self.reference) else distances
        return [QgsPointXY(self.reference.interpolate(f).asPoint()) for f in flats]

    def refresh_preview(self):
        if "dots" in self._bands:
            self._bands["dots"].reset(POINT)
        if self.create_button is None or sip.isdeleted(self.create_button):
            return
        self.options.setEnabled(self.template() is not None)             # nothing to set up without a target layer
        points = self.plan()
        self._warn_too_many()
        self.create_button.setEnabled(bool(points) and self.template() is not None)
        if not self._open or not self.show_preview or not points:
            return
        dots = self._band("dots", QColor(30, 30, 160), 1, POINT)
        dots.setFillColor(QColor(255, 255, 255))
        dots.setIconSize(9)
        with suppress(AttributeError):
            dots.setIcon(QgsRubberBand.IconType.ICON_CROSS)
        for i, p in enumerate(points):
            dots.addPoint(p, i == len(points) - 1)

    def _warn_too_many(self):
        """A blue bar when the settings would make more than MAX_POINTS points (no preview and no Create then)."""
        if self.info is None or sip.isdeleted(self.info):
            return
        if self.too_many:
            self.info.show_text(f"That is more than {MAX_POINTS:,} points: the distance is too small for this line.",
                                ok="info")
            self._warned = True
        elif self._warned:
            self._warned = False
            self.info.hide()

    # ------------------------------------------------------------------ the run
    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))

    def create(self):
        target = self.template()
        if target is None:
            self.tell("Choose an editable point layer as the target.", ok=False)
            return 0
        if self.reference is None:
            self.tell("Select a line or draw one first.", ok=False)
            return 0
        points = self.plan()
        if self.too_many:
            self.tell(f"That is more than {MAX_POINTS:,} points - use a larger distance.", ok=False)
            return 0
        if not points:
            self.tell("No points fall on the line - check the numbers and the distances.", ok=False)
            return 0
        multi = QgsWkbTypes.isMultiType(target.wkbType())
        target.beginEditCommand("Construct points along line")
        made, ok = [], True
        for p in points:
            geom = QgsGeometry.fromPointXY(p)
            if multi:
                geom.convertToMultiType()
            feature = QgsVectorLayerUtils.createFeature(target, _to_layer(self.canvas(), target, geom))
            ok = target.addFeature(feature) and ok
            made.append(feature.id())
        if ok:
            target.endEditCommand()
        else:
            target.destroyEditCommand()
            self.tell(f"Could not edit {target.name()} - is it in edit mode?", ok=False)
            return 0
        target.triggerRepaint()
        for layer in all_vector_layers():
            if layer.selectedFeatureCount():
                layer.removeSelection()
        target.selectByIds(made)
        self.tell(f"Points On Line completed: {len(made)} point(s) added to {target.name()}.")
        self.refresh_page()
        return len(made)


TOOL_CLASSES = {PointsAlongLineTool.KEY: PointsAlongLineTool}

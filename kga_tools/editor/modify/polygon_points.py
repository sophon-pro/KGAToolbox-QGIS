"""
Points On Polygon Boundary: points placed ON the boundary of a polygon (not its vertices), laid out like Points On Line.
Select a polygon, choose the target point layer, then one of three ways to place the points:

    Number of Points   N points spaced equally all the way round the boundary. No starting point is needed: the first
                       point is at the starting point if you pick one, else at the polygon's first vertex
    Equal Distance     a point every Distance along the boundary, starting from the starting point
    Distances          the table: each row is a Distance and a Number of Points - that many points, Distance apart, one
                       row after the other from the starting point. Remaining Length is what is left of the boundary

Starting point (Equal Distance and Distances need one): with the polygon selected, click one of its vertices or any
point of its edge - the green square marks it. Clear Start forgets it.
Direction          Clockwise / Counter-clockwise: which way round the boundary the distances are counted. Blue arrows on the
                   boundary show it
Add a point at the starting point   also a point on the starting point itself (Equal Distance and Distances; with Number of
                   Points the first point is always there)

Only the outer boundary of the polygon is used - holes are ignored (the first part of a multi-part polygon). A point that
would fall back on the starting point after the full circle is not made twice. Show Preview marks the points that will be
made. Lengths are measured on the ground when the project has an ellipsoid (what $length reports), else flat in the map's
units. The new points are selected afterwards; one undoable edit.
"""

from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointLocator,
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

SETTINGS = "kga_editor/modify_polypoints_"
LINE = Qgis.GeometryType.Line
POINT = Qgis.GeometryType.Point
POLYGON = Qgis.GeometryType.Polygon
BLUE = QColor(51, 153, 255)
try:                                        # QGIS >= 3.40 / 4
    MARKER_BOX = Qgis.VertexMarkerType.Box
    MARKER_X = Qgis.VertexMarkerType.X
    MARKER_TRIANGLE = Qgis.VertexMarkerType.DoubleTriangle
except AttributeError:
    MARKER_BOX = scoped(QgsVertexMarker, 'IconType', 'ICON_BOX')
    MARKER_X = scoped(QgsVertexMarker, 'IconType', 'ICON_X')
    MARKER_TRIANGLE = scoped(QgsVertexMarker, 'IconType', 'ICON_DOUBLE_TRIANGLE')
SNAP_ICONS = {"vertex": MARKER_BOX, "midpoint": MARKER_TRIANGLE, "edge": MARKER_X}     # like QGIS' own snap marks
SNAP_COLOUR = QColor(255, 0, 255)
MODES = (("number", "Number of Points"), ("distance", "Equal Distance"), ("table", "Distances"))
UNITS = ((Qgis.DistanceUnit.Meters, "m"), (Qgis.DistanceUnit.Kilometers, "km"), (Qgis.DistanceUnit.Feet, "ft"),
         (Qgis.DistanceUnit.Yards, "yd"), (Qgis.DistanceUnit.Miles, "mi"))
EPS = 1e-9
MAX_POINTS = 10000                          # more than this many points is a mistake (a distance far too small)
PICK_PX = 10                                # how close a click must be to the boundary to pick a starting point
VERTEX_PX = 8                               # ... and to a vertex to snap to it


# ---------------------------------------------------------------------------------------- the maths
def ring_is_counter_clockwise(points):
    """True when a closed ring (QgsPointXY, y up) runs counter-clockwise."""
    area = sum(a.x() * b.y() - b.x() * a.y() for a, b in zip(points, points[1:]))
    return area > 0


def walks(perimeter, mode, number=0, distance=0.0, rows=(), add_start=False):
    """
    How far from the starting point, going round the boundary, each point is: [distance]. A full circle is not made
    twice: a point that would land back on the starting point is left out (add_start puts one there on purpose).
      "number":   `number` points spaced equally all the way round - the first one is at the starting point
      "distance": a point every `distance`
      "table":    rows = [(distance, count)] one after the other
    """
    out = []
    if perimeter <= EPS:
        return out
    limit = perimeter - EPS * max(1.0, perimeter)
    if mode == "number":
        n = max(0, min(int(number), MAX_POINTS + 1))
        return [perimeter * k / n for k in range(n)] if n else []
    if mode == "distance":
        rows = [(distance, int(perimeter / distance) + 1)] if distance > EPS else []
    done = 0.0
    for step, count in rows:
        if step <= EPS:
            continue
        for _ in range(max(0, int(count))):
            done += step
            if done >= limit or len(out) > MAX_POINTS:                  # one over: the caller sees it is too many
                break
            out.append(done)
        if done >= limit:
            break
    if add_start:
        out.insert(0, 0.0)
    return out


def boundary_positions(perimeter, mode, number=0, distance=0.0, rows=(), start=0.0, forward=True, add_start=False):
    """
    Where the points go: distances along the ring from its first vertex, in the order the points are walked. `start` is
    where the starting point is (a distance along the ring from its first vertex); `forward` is True when the walk goes
    the way the ring's vertices run.
    """
    # the walks are strictly increasing and below one full circle, so no two points coincide
    return [(start + w) % perimeter if forward else (start - w) % perimeter
            for w in walks(perimeter, mode, number, distance, rows, add_start)]


def remaining_length(perimeter, rows):
    """What is left of the boundary after the Distances table."""
    return perimeter - sum(distance * max(0, int(count)) for distance, count in rows if distance > 0)


def snap_on_ring(ring, point, tolerance, snap_tolerance):
    """
    What a click or the mouse is snapping to on the ring: (flat distance along the ring from its first vertex, the point on
    the ring, "vertex" | "midpoint" | "edge"), or None when `point` is further than `tolerance` from the ring. A vertex or
    the middle of an edge within `snap_tolerance` wins over the plain nearest point of the edge.
    """
    index, near = G.nearest_segment(ring, point)
    if G._norm(G._sub(near, point)) > tolerance:
        return None
    a, b = ring[index], ring[index + 1]
    along = sum(G._norm(G._sub(q, p)) for p, q in zip(ring[:index], ring[1:index + 1]))
    best = None
    middle = QgsPointXY((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
    for kind, spot, offset in (("vertex", a, 0.0), ("vertex", b, G._norm(G._sub(b, a))),
                               ("midpoint", middle, G._norm(G._sub(b, a)) / 2.0)):
        d = G._norm(G._sub(spot, point))
        if d <= snap_tolerance and (best is None or d < best[0]):
            best = (d, kind, spot, offset)
    if best is not None:
        _d, kind, spot, offset = best
        flat = along + offset
        if kind == "vertex" and flat >= sum(G._norm(G._sub(q, p)) for p, q in zip(ring, ring[1:])) - 1e-9:
            flat = 0.0                                                    # the closing vertex is the first one
        return flat, QgsPointXY(spot), kind
    return along + G._norm(G._sub(near, a)), QgsPointXY(near), "edge"


def start_on_ring(ring, point, tolerance, vertex_tolerance):
    """(flat distance along the ring, the point on it) for a click; see snap_on_ring. None off the ring."""
    hit = snap_on_ring(ring, point, tolerance, vertex_tolerance)
    return None if hit is None else hit[:2]


# ---------------------------------------------------------------------------------------- the tool
class PointsOnPolygonBoundaryTool(PickTool):
    KEY = "points_on_polygon_boundary"
    LABEL = "Points On Polygon Boundary"
    HINT = "select a polygon, choose the points to make (and a starting point), then press Create."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True                     # select first; a selection ends Select mode and clicks pick the starting point

    def __init__(self, canvas, iface, select_tool=None):
        s = QgsSettings()
        self.show_preview = s.value(SETTINGS + "preview", True, type=bool)
        self.mode = s.value(SETTINGS + "mode", "number")
        if self.mode not in dict(MODES):
            self.mode = "number"
        self.counter_clockwise = s.value(SETTINGS + "ccw", False, type=bool)
        self.add_start = s.value(SETTINGS + "add_start", True, type=bool)
        self.polygon_id = None             # (layer id, feature id) of the polygon in use
        self.ring = None                   # its outer boundary: closed list of QgsPointXY in the map's CRS
        self.start_flat = None             # the starting point: flat distance along the ring from its first vertex
        self.start_point = None            # ... and where it is on the map
        self.info = self.mode_bar = self.banner = self.template_combo = self.unit_combo = None
        self.length_label = self.remaining_label = self.remaining_row = self.direction_button = None
        self.start_label = self.clear_button = self.add_start_check = None
        self.number_spin = self.distance_spin = self.table = self.create_button = self.preview_check = None
        self.number_row = self.distance_row = self.start_row = None
        self._open_row = None
        self._bands = {}
        self._marker = None
        self.snap_kind = None              # "vertex" | "midpoint" | "edge": what the magenta mark is on
        self._snap_marker = None           # the magenta mark of what a click would pick as the starting point
        self._listed = []
        self._open = False
        self._profile_cache = None         # (key, flat distances, ground distances) at the ring's vertices
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
        for item in list(self._bands.values()) + [m for m in (self._marker, self._snap_marker) if m is not None]:
            with suppress(Exception):
                self.canvas().scene().removeItem(item)
        self._bands, self._marker, self._snap_marker = {}, None, None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
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
        row.addWidget(QLabel("Boundary Length:"))
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
        for _key, text in MODES:
            self.mode_bar.addTab(text)
        self.mode_bar.setDrawBase(False)
        self.mode_bar.setCurrentIndex([k for k, _t in MODES].index(self.mode))
        self.mode_bar.currentChanged.connect(self.set_mode_index)
        box.addWidget(self.mode_bar)
        box.addWidget(section_label("Starting point"))
        self.start_row = QHBoxLayout()
        self.start_label = QLabel("")
        self.start_label.setWordWrap(True)
        self.start_row.addWidget(self.start_label, 1)
        self.clear_button = button("Clear Start", self.clear_start, "Forget the starting point")
        self.start_row.addWidget(self.clear_button)
        box.addLayout(self.start_row)
        direction = QHBoxLayout()
        direction.addWidget(QLabel("Direction:"))
        self.direction_button = QPushButton("")
        self.direction_button.setObjectName("KgaModifyButton")
        self.direction_button.setToolTip("Which way round the boundary the distances are counted from the starting point")
        self.direction_button.clicked.connect(lambda _c=False: self.set_counter_clockwise(not self.counter_clockwise))
        direction.addWidget(self.direction_button)
        direction.addStretch(1)
        box.addLayout(direction)
        self._set_direction_text()
        self.add_start_check = check("Add a point at the starting point", self.add_start, self.set_add_start)
        box.addWidget(self.add_start_check)
        self.number_row = QHBoxLayout()
        self.number_row.addWidget(QLabel("Number of Points:"))
        self.number_spin = QSpinBox()
        self.number_spin.setRange(0, 10000)
        self.number_spin.setValue(int(QgsSettings().value(SETTINGS + "number", 10)))
        self.number_spin.valueChanged.connect(self._number_changed)
        self.number_row.addWidget(self.number_spin)
        self.number_row.addStretch(1)
        box.addLayout(self.number_row)
        self.distance_row = QHBoxLayout()
        self.distance_row.addWidget(QLabel("Distance:"))
        self.distance_spin = spin(float(QgsSettings().value(SETTINGS + "distance", 100.0)), 0.0, 1e9, 3, 10.0)
        self.distance_spin.valueChanged.connect(self._distance_changed)
        self.distance_row.addWidget(self.distance_spin)
        self.distance_row.addStretch(1)
        box.addLayout(self.distance_row)
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
        self._apply_mode()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        self.create_button = button("Create", self.create, "Add the points to the target layer (Ctrl+Z undoes it)")
        row.addWidget(self.create_button)
        box.addLayout(row)

    # ------------------------------------------------------------------ the mode and the options
    def set_mode_index(self, index):
        self.mode = MODES[int(index)][0]
        QgsSettings().setValue(SETTINGS + "mode", self.mode)
        self._apply_mode()
        self.refresh_preview()

    def set_mode(self, key):
        self.mode_bar.setCurrentIndex([k for k, _t in MODES].index(key))

    def _apply_mode(self):
        number, distance, table = self.mode == "number", self.mode == "distance", self.mode == "table"
        for layout, shown in ((self.number_row, number), (self.distance_row, distance), (self.remaining_row, table)):
            for i in range(layout.count()):
                widget = layout.itemAt(i).widget()
                if widget is not None:
                    widget.setVisible(shown)
        self.table.setVisible(table)
        self.add_start_check.setEnabled(not number)          # with Number of Points the first point is the start
        self._update_start_label()
        self.update_lengths()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "preview", self.show_preview)
        self.refresh_preview()

    def _set_direction_text(self):
        self.direction_button.setText("Counter-clockwise" if self.counter_clockwise else "Clockwise")

    def set_counter_clockwise(self, on):
        self.counter_clockwise = bool(on)
        QgsSettings().setValue(SETTINGS + "ccw", self.counter_clockwise)
        self._set_direction_text()
        self._draw_boundary()
        self.refresh_preview()

    def set_add_start(self, on):
        self.add_start = bool(on)
        QgsSettings().setValue(SETTINGS + "add_start", self.add_start)
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

    def profile(self):
        """([flat], [ground]) distance at each vertex of the ring - worked out once per ring and ellipsoid."""
        measure = self.measure()
        key = (id(self.ring), measure.name)
        if self._profile_cache is None or self._profile_cache[0] != key:
            self._profile_cache = (key,) + tuple(measure.profile(self.ring))
        return self._profile_cache[1], self._profile_cache[2]

    def perimeter(self):
        """The boundary's length in the measure's unit (0 with no polygon)."""
        return self.profile()[1][-1] if self.ring is not None else 0.0

    def _text(self, value):
        return f"{value / self.unit_factor():,.3f}"

    def update_lengths(self):
        if self.length_label is None or sip.isdeleted(self.length_label):
            return
        length = self.perimeter()
        self.length_label.setText(self._text(length) if self.ring is not None else "-")
        if self.ring is None:
            self.remaining_label.setText("-")
        else:
            left = remaining_length(length, [(d * self.unit_factor(), n) for d, n in self.rows()])
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

    # ------------------------------------------------------------------ the polygon
    def _load_polygon(self):
        """The first selected polygon feature: its outer boundary (first part). A different polygon forgets the start."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        found, ring = None, None
        for layer in all_vector_layers():
            if layer.isSpatial() and layer.geometryType() == POLYGON and layer.selectedFeatureCount():
                fid = sorted(layer.selectedFeatureIds())[0]
                g = QgsGeometry(layer.getFeature(fid).geometry())
                if g.isNull() or g.isEmpty():
                    continue
                if layer.crs() != canvas_crs:
                    g.transform(_transform(layer.crs(), canvas_crs))
                parts = g.asGeometryCollection()
                rings = parts[0].asPolygon() if parts else []
                if rings and len(rings[0]) > 3:
                    found, ring = (layer.id(), fid), [QgsPointXY(p) for p in rings[0]]      # the holes are ignored
                    break
        changed = found != self.polygon_id or (ring is not None and self.ring is not None and ring != self.ring)
        self.polygon_id, self.ring = found, ring
        self._profile_cache = None
        if changed:
            self.start_flat = self.start_point = None
        self._update_start_label()
        self.update_lengths()
        self._draw_boundary()

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.create_button is None or sip.isdeleted(self.create_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        self._load_polygon()
        self.refresh_preview()

    # ------------------------------------------------------------------ the starting point
    def _update_start_label(self):
        if self.start_label is None or sip.isdeleted(self.start_label):
            return
        if self.ring is None:
            text = "Select a polygon first."
        elif self.start_flat is not None:
            text = "Starting point set."
        elif self.mode == "number":
            text = "Optional: click a vertex or the edge of the polygon. Otherwise the first vertex is used."
        else:
            text = "Click a vertex or the edge of the polygon to set the starting point."
        self.start_label.setText(text)
        self.clear_button.setEnabled(self.start_flat is not None)

    def set_start(self, flat, point):
        self.start_flat, self.start_point = flat, QgsPointXY(point)
        self._update_start_label()
        self._draw_start()
        self.refresh_preview()

    def clear_start(self):
        self.start_flat = self.start_point = None
        self._update_start_label()
        self._draw_start()
        self.refresh_preview()

    def snap_at(self, point):
        """What the mouse is over on the selected polygon: (flat distance, point, kind) or None. Vertices, the middle of
        an edge and the edge itself snap whether or not the project has snapping on (and whatever other layers hold)."""
        if self._selecting or self.ring is None:
            return None
        mupp = self.canvas().mapUnitsPerPixel()
        return snap_on_ring(self.ring, QgsPointXY(point), PICK_PX * mupp, VERTEX_PX * mupp)

    def canvasMoveEvent(self, e):
        if self.forward_select("canvasMoveEvent", e):
            self._show_snap(None)                                # selecting: nothing to snap to
            return
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self.on_move(QgsPointXY(e.originalMapPoint()))          # the mouse itself: our own snapping, not the project's

    def canvasReleaseEvent(self, e):
        if self.forward_select("canvasReleaseEvent", e):
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.on_click(QgsPointXY(e.originalMapPoint()))
        elif e.button() == Qt.MouseButton.RightButton:
            self.on_right_click(e)

    def on_move(self, point):
        self._show_snap(self.snap_at(point))

    def _show_snap(self, hit):
        """The magenta mark: a square on a vertex, a double triangle on the middle of an edge, an X on the edge."""
        if hit is None:
            if self._snap_marker is not None:
                self._snap_marker.hide()
            return
        if self._snap_marker is None:
            self._snap_marker = QgsVertexMarker(self.canvas())
            self._snap_marker.setColor(SNAP_COLOUR)
            self._snap_marker.setIconSize(12)
            self._snap_marker.setPenWidth(2)
        self._snap_marker.setIconType(SNAP_ICONS[hit[2]])
        self.snap_kind = hit[2]
        self._snap_marker.setCenter(hit[1])
        self._snap_marker.show()

    def on_click(self, point):
        if self._selecting or self.ring is None:
            return
        hit = self.snap_at(point)
        if hit is None:
            self.say("click a vertex or the edge of the selected polygon.")
            return
        self.set_start(hit[0], hit[1])

    # ------------------------------------------------------------------ drawing on the map
    def _band(self, key, colour, width, kind=LINE):
        band = self._bands.get(key)
        if band is None:
            band = QgsRubberBand(self.canvas(), kind)
            band.setColor(colour)
            band.setWidth(width)
            if kind == POLYGON:
                band.setFillColor(QColor(255, 255, 255, 0))
            self._bands[key] = band
        return band

    def _clear_bands(self):
        for key, band in self._bands.items():
            band.reset(POLYGON if key == "arrows" else POINT if key == "dots" else LINE)
        for marker in (self._marker, self._snap_marker):
            if marker is not None:
                marker.hide()

    def _walk_ring(self):
        """The boundary in the order the distances are counted: the ring's own order or the reverse."""
        pts = list(self.ring)
        return pts if self.forward() else list(reversed(pts))

    def forward(self):
        """True when counting the chosen way round follows the order of the ring's vertices."""
        return ring_is_counter_clockwise(self.ring) == self.counter_clockwise

    def _draw_boundary(self):
        """The boundary in blue with hollow arrows showing the direction, and the starting point."""
        for key, kind in (("reference", LINE), ("arrows", POLYGON)):
            if key in self._bands:
                self._bands[key].reset(kind)
        if not self._open or self.ring is None:
            self._draw_start()
            return
        pts = self._walk_ring()
        line = self._band("reference", BLUE, 3)
        for i, p in enumerate(pts):
            line.addPoint(p, i == len(pts) - 1)
        arrows = self._band("arrows", BLUE, 2, POLYGON)
        mupp = self.canvas().mapUnitsPerPixel()
        for tri in G.direction_arrows(QgsGeometry.fromPolylineXY(pts), 90 * mupp, 14 * mupp):
            arrows.addGeometry(QgsGeometry.fromPolygonXY([tri + [tri[0]]]), QgsCoordinateReferenceSystem(), False)
        arrows.updatePosition()
        arrows.update()
        self._draw_start()

    def _draw_start(self):
        if self._marker is None and self.start_point is not None:
            self._marker = QgsVertexMarker(self.canvas())
            self._marker.setIconType(MARKER_BOX)
            self._marker.setColor(QColor(0, 140, 60))
            self._marker.setIconSize(9)
            self._marker.setPenWidth(2)
        if self._marker is not None:
            if self._open and self.start_point is not None and self.ring is not None:
                self._marker.setCenter(self.start_point)
                self._marker.show()
            else:
                self._marker.hide()

    # ------------------------------------------------------------------ the points
    def positions(self):
        """Distances along the ring (the measure's unit) from its first vertex where the points go."""
        if self.ring is None:
            return []
        factor = self.unit_factor()
        total = self.perimeter()
        start = 0.0
        if self.start_flat is not None:
            flat, ground = self.profile()
            start = LengthMeasure._interpolate(flat, ground, self.start_flat)
        rows = [(d * factor, n) for d, n in self.rows()]
        return boundary_positions(total, self.mode, self.number_spin.value(), self.distance_spin.value() * factor, rows,
                                  start, self.forward(), self.add_start)

    def _ring_geometry(self):
        key = id(self.ring)
        if getattr(self, "_ring_line", (None,))[0] != key:
            self._ring_line = (key, QgsGeometry.fromPolylineXY(self.ring))
        return self._ring_line[1]

    def needs_start(self):
        return self.mode != "number" and self.start_flat is None

    def plan(self):
        """[QgsPointXY in the map's CRS] of the points that will be made (none while a starting point is missing)."""
        if self.ring is None or self.needs_start():
            return []
        positions = self.positions()
        self.too_many = len(positions) > MAX_POINTS
        if self.too_many:
            return []
        line = self._ring_geometry()
        flats = self.measure().flats_at(self.ring, positions, self.profile())
        return [QgsPointXY(line.interpolate(f).asPoint()) for f in flats]

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
            self.info.show_text(f"That is more than {MAX_POINTS:,} points: the distance is too small for this boundary.",
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
        if self.ring is None:
            self.tell("Select a polygon first.", ok=False)
            return 0
        if self.needs_start():
            self.tell("Click a vertex or the edge of the polygon to set the starting point.", ok=False)
            return 0
        points = self.plan()
        if self.too_many:
            self.tell(f"That is more than {MAX_POINTS:,} points - use a larger distance.", ok=False)
            return 0
        if not points:
            self.tell("No points to make - check the numbers and the distances.", ok=False)
            return 0
        multi = QgsWkbTypes.isMultiType(target.wkbType())
        target.beginEditCommand("Construct points on polygon boundary")
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
        self.tell(f"Points On Polygon Boundary completed: {len(made)} point(s) added to {target.name()}.")
        self.refresh_page()
        return len(made)


TOOL_CLASSES = {PointsOnPolygonBoundaryTool.KEY: PointsOnPolygonBoundaryTool}

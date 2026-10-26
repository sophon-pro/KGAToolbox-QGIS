"""
Divide, laid out like the Divide pane of ArcGIS Pro (the page changes with what is selected: polygons or lines).

POLYGONS: "Change the selection." and the selected polygons, a Method
(Proportional Area, Equal Area, Equal Width), the Direction (a polygon edge or a drawn line; Switch picks the side
to start from), the Values for the method, an information bar with the areas, Show Preview and the Divide button.

    Proportional Area   N parts that each hold 1/N of the polygon
    Equal Area          parts of a given area, laid out from the start side; the rest stays as the remaining polygon
                        (or is shared out equally between the parts)
    Equal Width         parts of a given width, from the start side; the rest stays (or is merged into the last part)

The dividing lines run parallel to the direction. The first part keeps the original feature, the others are new
features with the same attributes. One undoable edit command per layer. The maths is `divide_polygon`.

LINES: Method Distance (a table of distances, with repetitions and the rest shared out if wanted), Equal Parts or
Percentage; Direction From start / From end of the line; Units; the information bar with the lengths. The maths is
`divide_line`; the preview marks where the line will be cut.
"""

import math
import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
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
    QHeaderView,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..selection import _transform
from .generalize import selected_geometries
from .ground import AreaMeasure, LengthMeasure
from .page import DoneStrip, button, check, section_label, spin
from .pick import commit, pick_features
from .reshape import PickTool, _copyable
from .transform import editable_selection

SETTINGS = "kga_editor/modify_"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
POLYGON = Qgis.GeometryType.Polygon
LINE = Qgis.GeometryType.Line
EPS = 1e-9
AREA_UNITS = (Qgis.AreaUnit.SquareMeters, Qgis.AreaUnit.SquareKilometers, Qgis.AreaUnit.Hectares,
              Qgis.AreaUnit.SquareFeet, Qgis.AreaUnit.SquareYards, Qgis.AreaUnit.Acres, Qgis.AreaUnit.SquareMiles)
DIST_UNITS = (Qgis.DistanceUnit.Meters, Qgis.DistanceUnit.Kilometers, Qgis.DistanceUnit.Feet, Qgis.DistanceUnit.Yards,
              Qgis.DistanceUnit.Miles)
METHODS = (("proportional", "Proportional Area", "div_proportional.svg",
            "Divide the polygon into parts that each hold the same share of it."),
           ("area", "Equal Area", "div_area.svg", "Parts of a given area, one after the other from the start side."),
           ("width", "Equal Width", "div_width.svg", "Parts of a given width, one after the other from the start side."))
LINE_METHODS = (("distance", "Distance", "div_ldistance.svg", "Cut the line at the distances in the table."),
                ("parts", "Equal Parts", "div_lparts.svg", "Cut the line into parts of the same length."),
                ("percentage", "Percentage", "div_lpercent.svg", "Cut the line in two at a percentage of its length."))
DISTANCE_MODES = (("Distance: cut at each distance in turn", "div_lmode1.svg"),
                  ("Equal distance: one distance, repeated", "div_lmode2.svg"),
                  ("Distance with repetitions: several distances, each repeated", "div_lmode3.svg"))
PREVIEW_MAX = 30


# ---------------------------------------------------------------------------------------- maths
def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > EPS else None


class _Frame:
    """The polygon in a frame whose second axis `n` is the direction the parts are stacked in."""

    def __init__(self, geom, d, n):
        box = geom.boundingBox()
        self.origin = QgsPointXY(box.center())
        self.d, self.n = d, n
        self.reach = 2.0 * math.hypot(box.width(), box.height()) + 1.0
        us = [self.u(QgsPointXY(v.x(), v.y())) for v in geom.vertices()]
        self.lo, self.hi = min(us), max(us)

    def u(self, p):
        return (p.x() - self.origin.x()) * self.n[0] + (p.y() - self.origin.y()) * self.n[1]

    def point(self, t, u):
        return QgsPointXY(self.origin.x() + self.d[0] * t + self.n[0] * u, self.origin.y() + self.d[1] * t + self.n[1] * u)

    def band(self, a, b):
        """The strip between u = a and u = b (None = unbounded), as a polygon."""
        a = -self.reach if a is None else a
        b = self.reach if b is None else b
        r = self.reach
        return QgsGeometry.fromPolygonXY([[self.point(-r, a), self.point(r, a), self.point(r, b), self.point(-r, b),
                                           self.point(-r, a)]])

    def line(self, u):
        return QgsGeometry.fromPolylineXY([self.point(-self.reach, u), self.point(self.reach, u)])


def _flat(geom):
    return geom.area()


def _area_below(geom, frame, u, measure=_flat):
    return measure(geom.intersection(frame.band(None, u)))


def _cut_for_area(geom, frame, target, measure=_flat):
    """The u at which the part of the polygon below it holds `target` area."""
    lo, hi = frame.lo, frame.hi
    for _ in range(48):
        mid = (lo + hi) / 2
        if _area_below(geom, frame, mid, measure) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def divide_polygon(geom, d, n, method, count, value=0.0, option=False, measure=_flat):
    """
    Divide a polygon with lines parallel to `d`, stacking the parts along `n` (both unit vectors).
    method "proportional": `count` parts of equal area; "area": `count` parts of `value` area each;
    "width": `count` parts `value` wide each. `option`: for "area" share the rest out equally, for "width" merge the
    rest into the last part. Returns (pieces, cuts, info): the pieces in order (the remaining polygon last when
    there is one), the cutting lines inside the polygon, and {"total", "new", "remaining"} areas. None when it
    cannot be divided. `measure(geometry)` gives the area the parts are balanced by (default: the flat area; see
    AreaMeasure); `value` and the areas in `info` are in the same unit.
    """
    if geom is None or geom.isNull() or geom.type() != POLYGON or count < 1:
        return None
    total = measure(geom)
    frame = _Frame(geom, d, n)
    if frame.hi - frame.lo < EPS or total < EPS:
        return None
    if method == "area" and option:
        method, value = "proportional", 0.0                  # sharing the rest equally = equal parts of everything
    cuts_u = []
    if method == "proportional":
        part = total / count
        cuts_u = [_cut_for_area(geom, frame, part * k, measure) for k in range(1, count)]
    elif method == "area":
        if value <= EPS:
            return None
        for k in range(1, count + 1):
            if value * k >= total - EPS:
                break
            cuts_u.append(_cut_for_area(geom, frame, value * k, measure))
    else:
        if value <= EPS:
            return None
        cuts_u = [frame.lo + value * k for k in range(1, count + 1) if frame.lo + value * k < frame.hi - EPS]
        if option and cuts_u:
            cuts_u = cuts_u[:-1] if len(cuts_u) == count else cuts_u     # the last part takes the rest
    edges = [None] + cuts_u + [None]
    pieces = []
    for a, b in zip(edges, edges[1:]):
        piece = geom.intersection(frame.band(a, b))
        if piece is not None and not piece.isNull() and not piece.isEmpty() and piece.area() > EPS:
            pieces.append(piece)
    if len(pieces) < 2:
        return None
    cut_lines = []
    for u in cuts_u:
        inside = geom.intersection(frame.line(u))
        if inside is not None and not inside.isNull() and not inside.isEmpty():
            cut_lines.append(inside)
    has_remaining = (method in ("area", "width") and not (method == "width" and option)
                     and len(cuts_u) == count and len(pieces) == count + 1)
    new_pieces = pieces[:-1] if has_remaining else pieces
    info = {"total": total, "new": sum(measure(p) for p in new_pieces), "part": measure(new_pieces[0]),
            "remaining": measure(pieces[-1]) if has_remaining else 0.0, "new_count": len(new_pieces)}
    return pieces, cut_lines, info


def divide_line(geom, method, from_end, parts=2, percentage=0.0, rows=(), mode=1, option=False, measure=None):
    """
    Cut a single-part line. method "parts": `parts` equal pieces; "percentage": two pieces, the first `percentage` % of
    the length from the chosen end; "distance": pieces of the lengths in `rows` = [(distance, repetitions)] in turn
    from the chosen end (mode 1 uses each distance once, mode 2 repeats the only row, mode 3 repeats every row);
    `option` shares the rest of the line out - equally in mode 2, in proportion in mode 3. Returns (pieces in order
    from the start of the line, the cut points, {"total", "part", "first", "second", "sum", "remaining"}), or None.
    With a LengthMeasure on the project's ellipsoid the lengths are ground lengths (what $length reports) and `rows`
    are in that measure's unit; the cuts are then placed where the ground distance falls.
    """
    if geom is None or geom.isNull() or geom.type() != LINE or geom.isMultipart():
        return None
    ground = measure is not None and measure.usable_for(geom)
    points = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()] if ground else None
    length = measure.points_length(points) if ground else geom.length()
    if length < EPS:
        return None
    curve = geom.constGet()
    steps = []
    if method == "parts":
        if parts < 2:
            return None
        steps = [length / parts] * (parts - 1)
    elif method == "percentage":
        if not 0.0 < percentage < 100.0:
            return None
        steps = [length * percentage / 100.0]
    else:
        seq = []
        for distance, repetitions in rows:
            if distance > EPS:
                seq += [distance] * (max(1, int(repetitions)) if mode != 1 else 1)
        if mode == 2:
            seq = seq[:max(1, int(rows[0][1]))] if rows else []
        total_seq = sum(seq)
        if not seq:
            return None
        if option and 0 < total_seq < length - EPS:
            if mode == 2:
                seq = [d + (length - total_seq) / len(seq) for d in seq]
            elif mode == 3:
                seq = [d * length / total_seq for d in seq]
        steps = seq
    cuts, done = [], 0.0
    for step in steps:
        done += step
        if done >= length - EPS:
            break
        cuts.append(length - done if from_end else done)
    cuts.sort()
    if not cuts:
        return None
    flat_cuts = [measure.flat_at(points, c) for c in cuts] if ground else cuts           # where along the geometry
    flat_total = geom.length()
    edges = [0.0] + flat_cuts + [flat_total]
    pieces = [QgsGeometry(curve.curveSubstring(a, b)) for a, b in zip(edges, edges[1:])]
    points = [geom.interpolate(c).asPoint() for c in flat_cuts]
    first = length * percentage / 100.0 if method == "percentage" else 0.0
    used = sum(steps)
    info = {"total": length, "part": length / parts if method == "parts" else 0.0, "first": first,
            "second": length - first, "sum": used, "remaining": max(0.0, length - used)}
    return pieces, [QgsPointXY(p) for p in points], info


def edge_direction(geom, point):
    """(d, n, (a, b)): the direction of the polygon edge nearest `point`, the unit normal pointing into the polygon
    and the edge's end points."""
    outline = geom.convertToType(LINE, False)
    if outline is None or outline.isNull():
        return None
    _dist, nearest, after, _side = outline.closestSegmentWithContext(point)
    if after < 0:
        return None
    a, b = outline.vertexAt(after - 1), outline.vertexAt(after)
    d = _unit((b.x() - a.x(), b.y() - a.y()))
    if d is None:
        return None
    n = (-d[1], d[0])
    probe = QgsPointXY(nearest.x() + n[0] * 1e-6 * max(1.0, geom.boundingBox().width()),
                       nearest.y() + n[1] * 1e-6 * max(1.0, geom.boundingBox().width()))
    if not geom.contains(QgsGeometry.fromPointXY(probe)):
        n = (-n[0], -n[1])
    return d, n, (QgsPointXY(a.x(), a.y()), QgsPointXY(b.x(), b.y()))


# ---------------------------------------------------------------------------------------- the tool
class DivideTool(PickTool):
    KEY = "divide"
    LABEL = "Divide"
    HINT = "choose the polygons, a method and a direction, then press Divide."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    # clicking another polygon selects it: Select mode stays on until a direction is being picked
    SELECT_ONE_SHOT = False

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.method = settings.value(SETTINGS + "divide_method", "proportional")
        if self.method not in ("proportional", "area", "width"):
            self.method = "proportional"
        self.count = int(settings.value(SETTINGS + "divide_count", 2))
        self.share_rest = settings.value(SETTINGS + "divide_share", False, type=bool)
        self.merge_rest = settings.value(SETTINGS + "divide_merge", False, type=bool)
        self.show_preview = settings.value(SETTINGS + "divide_preview", True, type=bool)
        self.kind = "polygon"                  # what the page is showing: "polygon" or "line" (follows the selection)
        self.line_method = settings.value(SETTINGS + "divide_line_method", "parts")
        if self.line_method not in ("distance", "parts", "percentage"):
            self.line_method = "parts"
        self.from_end = settings.value(SETTINGS + "divide_from_end", False, type=bool)
        self.dist_mode = int(settings.value(SETTINGS + "divide_dist_mode", 1))
        if self.dist_mode not in (1, 2, 3):
            self.dist_mode = 1
        self.line_radios = {}
        self.from_radios = {}
        self.mode_buttons = {}
        self.table = None
        self._table_open_row = None            # the "Click to add new row" row of the distance table
        self.direction = None                  # (d, n): set by an edge or a drawn line, in map coordinates
        self.flip = False                      # Side > Switch
        self.pick_mode = None                  # "edge" | "line" while a direction is being picked
        self._line_start = None
        self.radios = {}
        self.spins = {}
        self.unit_combos = {}
        self.info = None
        self.divide_button = None
        self.switch_button = None
        self._bands = {}
        self._open = False                     # the tool is the active map tool: only then is anything drawn
        self._mark = None                      # (a, b, extend): the line the direction was set by
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def start_select_mode(self):
        self.set_selecting(True, auto=True)

    def activate(self):
        self._open = True
        super().activate()
        self.refresh_preview()

    def deactivate(self):
        self._open = False                     # first: resetting below refreshes the page
        self.pick_mode = None
        self._line_start = None
        self.direction = None
        self.flip = False
        self._clear_preview()
        self._clear_direction_mark()
        super().deactivate()
        self._clear_preview()

    def reset(self):
        self._line_start = None
        super().reset()

    def dispose(self):
        for band in self._bands.values():
            with suppress(Exception):
                self.canvas().scene().removeItem(band)
        self._bands = {}
        super().dispose()

    # ------------------------------------------------------------------ page
    def _combo(self, units, current, key, formatter):
        box = QComboBox()
        index = 0
        for i, unit in enumerate(units):
            box.addItem(formatter(unit), unit)
            if unit == current:
                index = i
        box.setCurrentIndex(index)
        box.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        self.unit_combos[key] = box
        return box

    def _value_row(self, key, label, default, units, current, formatter):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(label), 1)
        box = spin(default, 0.0, 1e12, 3, 1.0)
        box.valueChanged.connect(lambda _v: self.refresh_preview())
        self.spins[key] = box
        lay.addWidget(box)
        lay.addWidget(self._combo(units, current, key, formatter))
        return row

    def _tool_button(self, text, icon, tip, slot):
        b = QToolButton()
        b.setObjectName("KgaModifyToolTile")
        b.setCheckable(True)
        b.setIcon(QIcon(os.path.join(ICON_DIR, icon)))
        b.setIconSize(QSize(26, 26))
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b.setToolTip(tip)
        b.clicked.connect(lambda _c=False: slot())
        return b

    def _method_grid(self, methods, current, radios, setter):
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        group = QButtonGroup(host)
        for col, (key, text, icon, tip) in enumerate(methods):
            picture = QLabel()
            picture.setPixmap(QIcon(os.path.join(ICON_DIR, icon)).pixmap(30, 30))
            picture.setToolTip(tip)
            radio = QRadioButton(text)
            radio.setToolTip(tip)
            radio.setChecked(key == current)
            radio.toggled.connect(lambda on, k=key: on and setter(k))
            group.addButton(radio)
            radios[key] = radio
            grid.addWidget(picture, 0, col, Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(radio, 1, col, Qt.AlignmentFlag.AlignHCenter)
        return host

    def build_page(self, box):
        settings = QgsSettings()
        box.addWidget(section_label("Method"))
        self.poly_methods = self._method_grid(METHODS, self.method, self.radios, self.set_method)
        self.line_methods = self._method_grid(LINE_METHODS, self.line_method, self.line_radios, self.set_line_method)
        box.addWidget(self.poly_methods)
        box.addWidget(self.line_methods)
        box.addWidget(section_label("Direction"))
        self.poly_direction = QWidget()
        pd = QVBoxLayout(self.poly_direction)
        pd.setContentsMargins(0, 0, 0, 0)
        self.edge_button = self._tool_button("Choose a polygon edge to set the direction", "div_edge.svg",
                                             "Click an edge of the polygon: the parts are cut parallel to it",
                                             lambda: self.set_pick_mode("edge"))
        self.line_button = self._tool_button("Draw a line to set the direction", "div_line.svg",
                                             "Click two points: the parts are cut parallel to that line",
                                             lambda: self.set_pick_mode("line"))
        pd.addWidget(self.edge_button)
        pd.addWidget(self.line_button)
        side = QHBoxLayout()
        side.addWidget(QLabel("Side"), 1)
        self.switch_button = button("Switch", self.switch_side, "Start from the opposite side")
        side.addWidget(self.switch_button)
        pd.addLayout(side)
        box.addWidget(self.poly_direction)
        self.line_direction = QWidget()
        ld = QVBoxLayout(self.line_direction)
        ld.setContentsMargins(0, 0, 0, 0)
        direction_group = QButtonGroup(self.line_direction)
        for key, text, icon in (("start", "From start of line", "div_fromstart.svg"),
                                ("end", "From end of line", "div_fromend.svg")):
            row = QHBoxLayout()
            radio = QRadioButton()
            radio.setChecked((key == "end") == self.from_end)
            radio.toggled.connect(lambda on, k=key: on and self.set_from_end(k == "end"))
            direction_group.addButton(radio)
            self.from_radios[key] = radio
            picture = QLabel()
            picture.setPixmap(QIcon(os.path.join(ICON_DIR, icon)).pixmap(26, 26))
            row.addWidget(radio)
            row.addWidget(picture)
            row.addWidget(QLabel(text), 1)
            ld.addLayout(row)
        box.addWidget(self.line_direction)
        box.addWidget(section_label("Values"))
        self.area_row = self._value_row("area", "Area", float(settings.value(SETTINGS + "divide_area", 0.0)),
                                        AREA_UNITS, QgsProject.instance().areaUnits(), QgsUnitTypes.toAbbreviatedString)
        self.width_row = self._value_row("width", "Width", float(settings.value(SETTINGS + "divide_width", 0.0)),
                                         DIST_UNITS, QgsProject.instance().distanceUnits(), QgsUnitTypes.toAbbreviatedString)
        box.addWidget(self.area_row)
        box.addWidget(self.width_row)
        self.percent_row = QWidget()
        pr = QHBoxLayout(self.percent_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(QLabel("Percentage"), 1)
        self.percent_spin = spin(float(settings.value(SETTINGS + "divide_percent", 50.0)), 0.0, 100.0, 2, 5.0, "%")
        self.percent_spin.valueChanged.connect(lambda _v: self.refresh_preview())
        pr.addWidget(self.percent_spin, 2)
        box.addWidget(self.percent_row)
        self.distance_panel = self._build_distance_panel()
        box.addWidget(self.distance_panel)
        self.parts_row = QWidget()
        parts = QHBoxLayout(self.parts_row)
        parts.setContentsMargins(0, 0, 0, 0)
        parts.addWidget(QLabel("Number of parts"), 1)
        self.parts_spin = QSpinBox()
        self.parts_spin.setRange(1, 1000)
        self.parts_spin.setValue(max(1, self.count))
        self.parts_spin.valueChanged.connect(self.set_count)
        parts.addWidget(self.parts_spin, 2)
        box.addWidget(self.parts_row)
        self.info = DoneStrip()
        box.addWidget(self.info)
        self.share_check = check("Distribute remaining area equally", self.share_rest, self.set_share)
        self.merge_check = check("Merge remaining area", self.merge_rest, self.set_merge)
        self.equal_check = check("Distribute remaining length equally", False, lambda _on: self.refresh_preview())
        self.prop_check = check("Distribute remaining length proportionally", False, lambda _on: self.refresh_preview())
        for widget in (self.share_check, self.merge_check, self.equal_check, self.prop_check):
            box.addWidget(widget)
        self._show_kind()

    # ------------------------------------------------------------------ the distance table (lines)
    def _build_distance_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        group = QButtonGroup(panel)
        group.setExclusive(True)
        for number, (tip, icon) in enumerate(DISTANCE_MODES, start=1):
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIcon(QIcon(os.path.join(ICON_DIR, icon)))
            b.setIconSize(QSize(30, 30))
            b.setToolTip(tip)
            b.setChecked(number == self.dist_mode)
            b.clicked.connect(lambda _c=False, m=number: self.set_dist_mode(m))
            group.addButton(b)
            self.mode_buttons[number] = b
            top.addWidget(b)
        top.addStretch(1)
        top.addWidget(QLabel("Units"))
        combo = QComboBox()
        index = 0
        for i, unit in enumerate(DIST_UNITS):
            combo.addItem(QgsUnitTypes.toAbbreviatedString(unit), unit)
            if unit == QgsProject.instance().distanceUnits():
                index = i
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        self.unit_combos["line"] = combo
        top.addWidget(combo)
        lay.addLayout(top)
        self.table = QTableWidget(0, 2)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(90)
        self.table.setMaximumHeight(150)
        self.table.cellClicked.connect(self._table_clicked)
        self.table.itemChanged.connect(lambda _item: self.refresh_preview())
        lay.addWidget(self.table)
        self._setup_table()
        return panel

    def _setup_table(self):
        """Columns and rows for the chosen distance mode: Distance; Distance + Repetitions (one row); several rows."""
        table = self.table
        table.blockSignals(True)
        table.clear()
        table.setRowCount(0)
        columns = ["#", "Distance"] + (["Repetitions"] if self.dist_mode != 1 else [])
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, len(columns)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self._table_open_row = None
        self._add_row(count=1)
        if self.dist_mode != 2:                                    # a row of its own that adds the next one when clicked
            self._table_open_row = table.rowCount()
            table.insertRow(self._table_open_row)
            item = QTableWidgetItem("Click to add new row.")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            table.setItem(self._table_open_row, 0, item)
            table.setSpan(self._table_open_row, 0, 1, len(columns))
        table.blockSignals(False)

    def _add_row(self, count=1):
        table = self.table
        for _ in range(count):
            row = table.rowCount() if self._table_open_row is None else self._table_open_row
            table.insertRow(row)
            number = QTableWidgetItem(str(row + 1))
            number.setFlags(Qt.ItemFlag.ItemIsEnabled)
            number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 0, number)
            for col, text in ((1, "0"), (2, "1")):
                if col < table.columnCount():
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    table.setItem(row, col, item)
            if self._table_open_row is not None:
                self._table_open_row += 1

    def _table_clicked(self, row, _col):
        if self._table_open_row is not None and row == self._table_open_row:
            self.table.blockSignals(True)
            self._add_row()
            self.table.blockSignals(False)
            self.refresh_preview()

    def table_rows(self):
        """[(distance in the units chosen, repetitions)] of the real rows of the table."""
        rows = []
        stop = self._table_open_row if self._table_open_row is not None else self.table.rowCount()
        for row in range(stop):
            def number(col, default):
                item = self.table.item(row, col)
                try:
                    return float(item.text().replace(",", "")) if item is not None else default
                except ValueError:
                    return default
            rows.append((number(1, 0.0), number(2, 1.0) if self.table.columnCount() > 2 else 1.0))
        return rows

    def set_dist_mode(self, mode):
        self.dist_mode = mode
        QgsSettings().setValue(SETTINGS + "divide_dist_mode", mode)
        self._setup_table()
        self._show_kind()
        self.refresh_preview()

    def set_line_method(self, key):
        self.line_method = key
        QgsSettings().setValue(SETTINGS + "divide_line_method", key)
        self._show_kind()
        self.refresh_preview()

    def set_from_end(self, on):
        self.from_end = bool(on)
        QgsSettings().setValue(SETTINGS + "divide_from_end", self.from_end)
        self.refresh_preview()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        self.divide_button = button("Divide", self.divide, "Divide the selected features (Ctrl+Z undoes it)")
        row.addWidget(self.divide_button)
        box.addLayout(row)

    def _show_kind(self):
        """The page follows what is selected: polygon controls, or line controls."""
        poly, line = self.kind == "polygon", self.kind == "line"
        distance = line and self.line_method == "distance"
        self.poly_methods.setVisible(poly)
        self.line_methods.setVisible(line)
        self.poly_direction.setVisible(poly)
        self.line_direction.setVisible(line)
        self.area_row.setVisible(poly and self.method == "area")
        self.width_row.setVisible(poly and self.method == "width")
        self.percent_row.setVisible(line and self.line_method == "percentage")
        self.distance_panel.setVisible(distance)
        self.parts_row.setVisible(poly or (line and self.line_method == "parts"))
        self.share_check.setVisible(poly and self.method == "area")
        self.merge_check.setVisible(poly and self.method == "width")
        self.equal_check.setVisible(distance and self.dist_mode == 2)
        self.prop_check.setVisible(distance and self.dist_mode == 3)

    def _show_method(self):
        self._show_kind()

    def set_method(self, key):
        self.method = key
        QgsSettings().setValue(SETTINGS + "divide_method", key)
        self._show_kind()
        self.refresh_preview()

    def set_count(self, value):
        self.count = int(value)
        QgsSettings().setValue(SETTINGS + "divide_count", self.count)
        self.refresh_preview()

    def set_share(self, on):
        self.share_rest = bool(on)
        QgsSettings().setValue(SETTINGS + "divide_share", self.share_rest)
        self.refresh_preview()

    def set_merge(self, on):
        self.merge_rest = bool(on)
        QgsSettings().setValue(SETTINGS + "divide_merge", self.merge_rest)
        self.refresh_preview()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "divide_preview", self.show_preview)
        if getattr(self, "preview_check", None) is not None and self.preview_check.isChecked() != self.show_preview:
            self.preview_check.setChecked(self.show_preview)
        self.refresh_preview()

    def switch_side(self):
        self.flip = not self.flip
        self._draw_direction()                               # the arrow turns round
        self.refresh_preview()

    def set_pick_mode(self, mode):
        """Pick the direction on the map: a click on a polygon edge, or two clicks for a line."""
        self.pick_mode = mode if self.pick_mode != mode else None
        self._line_start = None
        self._clear_direction_mark()
        self.set_selecting(self.pick_mode is None)          # picking a direction and selecting polygons share the map
        self.refresh_page()
        self.say({"edge": "click an edge of the polygon.", "line": "click the start and the end of the line.",
                  None: ""}[self.pick_mode])

    def _selection_kind(self):
        """"line" when only lines are selected, else "polygon" (the page opens with the polygon controls)."""
        kinds = {layer.geometryType() for layer, _ids in editable_selection(self.canvas())}
        return "line" if LINE in kinds and POLYGON not in kinds else "polygon"

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.divide_button is None or sip.isdeleted(self.divide_button):
            return
        kind = self._selection_kind()
        if kind != self.kind:
            self.kind = kind
            self._show_kind()
        self.edge_button.setChecked(self.pick_mode == "edge")
        self.line_button.setChecked(self.pick_mode == "line")
        self.switch_button.setEnabled(self.direction is not None)
        self.refresh_preview()

    # ------------------------------------------------------------------ the numbers
    def _factor(self, key, kind):
        """Map units (or map units squared) in one of the unit combo's unit."""
        crs = self.canvas().mapSettings().destinationCrs()
        unit = self.unit_combos[key].currentData()
        if kind == "area":
            return QgsUnitTypes.fromUnitToUnitFactor(unit, self.measure().unit)         # in the unit the parts are measured in
        return QgsUnitTypes.fromUnitToUnitFactor(unit, crs.mapUnits())

    def measure(self):
        """How areas are measured now: on the project's ellipsoid when it has one, else flat in the map's units."""
        return AreaMeasure(self.canvas())

    def length_measure(self):
        """The same for lengths."""
        return LengthMeasure(self.canvas())

    def _value_in_map_units(self):
        if self.method == "area":
            return self.spins["area"].value() * self._factor("area", "area")
        if self.method == "width":
            return self.spins["width"].value() * self._factor("width", "distance")
        return 0.0

    def _vectors(self):
        if self.direction is None:
            return None
        d, n = self.direction
        return d, ((-n[0], -n[1]) if self.flip else n)

    def _line_factor(self):
        """Map units in one of the lines' unit (the Units box)."""
        return QgsUnitTypes.fromUnitToUnitFactor(self.unit_combos["line"].currentData(), self.length_measure().unit)

    def _plan_line(self, geom):
        factor = self._line_factor()
        rows = [(d * factor, r) for d, r in self.table_rows()]
        option = self.equal_check.isChecked() if self.dist_mode == 2 else self.prop_check.isChecked()
        return divide_line(geom, self.line_method, self.from_end, self.parts_spin.value(), self.percent_spin.value(),
                           rows, self.dist_mode, option, self.length_measure())

    def _plan(self, geom):
        if geom.type() == LINE:
            return self._plan_line(geom)
        vectors = self._vectors()
        if vectors is None:
            return None
        option = self.share_rest if self.method == "area" else self.merge_rest if self.method == "width" else False
        return divide_polygon(geom, vectors[0], vectors[1], self.method, self.count, self._value_in_map_units(), option,
                              self.measure())

    def _selected(self, limit=PREVIEW_MAX):
        """The selected geometries the page is showing controls for."""
        wanted = LINE if self.kind == "line" else POLYGON
        return [g for g in selected_geometries(self.canvas(), limit) if g.type() == wanted]

    def _polygons(self, limit=PREVIEW_MAX):
        return self._selected(limit)

    def _area_text(self, area):
        unit = QgsProject.instance().areaUnits()
        factor = QgsUnitTypes.fromUnitToUnitFactor(self.measure().unit, unit)
        return f"{area * factor:,.2f} {QgsUnitTypes.toAbbreviatedString(unit)}"

    def _length_text(self, length):
        unit = self.unit_combos["line"].currentData()
        return f"{length / self._line_factor():,.2f} {QgsUnitTypes.toAbbreviatedString(unit)}"

    def refresh_info(self, plans):
        if self.info is None or sip.isdeleted(self.info):
            return
        geoms = self._selected()
        if not geoms:
            self.info.hide()
            return
        done = [p for p in plans if p is not None]
        if self.kind == "line":
            measure = self.length_measure()
            total = sum(measure.length(g) for g in geoms)
            lines = [f"Total line length: {self._length_text(total)}"]
            first = done[0][2] if done else None
            if self.line_method == "parts":
                lines.append(f"Length of each part: {self._length_text(first['part'] if first else 0.0)}")
            elif self.line_method == "percentage":
                lines.append(f"Length of first line: {self._length_text(first['first'] if first else 0.0)}")
                lines.append(f"Length of second line: {self._length_text(first['second'] if first else total)}")
            else:
                used = sum(p[2]["sum"] for p in done)
                lines.append(f"Sum of distances: {self._length_text(used)}")
                lines.append(f"Remaining line length: {self._length_text(max(0.0, total - used))}")
            if measure.ellipsoidal:
                lines.append(f"Lengths are measured on the {measure.name} ellipsoid, like $length.")
            self.info.show_text("\n".join(lines), ok="info")
            return
        measure = self.measure()
        total = sum(measure(g) for g in geoms)
        lines = [f"Total polygon area: {self._area_text(total)}"]
        if self.method == "proportional":
            if done:
                lines.append(f"New polygon area: {self._area_text(done[0][2]['part'])}")
        else:
            new = sum(p[2]["new"] for p in done)
            lines.append(f"Sum of new polygon areas: {self._area_text(new)}")
            lines.append(f"Remaining polygon area: {self._area_text(total - new)}")
        if measure.ellipsoidal:
            lines.append(f"Areas are measured on the {measure.name} ellipsoid, like $area.")
        self.info.show_text("\n".join(lines), ok="info")

    # ------------------------------------------------------------------ preview
    def _band(self, key, colour, width, dashed=False):
        band = self._bands.get(key)
        if band is None:
            band = QgsRubberBand(self.canvas(), LINE)
            band.setColor(colour)
            band.setWidth(width)
            if dashed:
                band.setLineStyle(Qt.PenStyle.DashLine)
            self._bands[key] = band
        return band

    def _clear_preview(self):
        if "cuts" in self._bands:
            self._bands["cuts"].reset(LINE)
        if "dots" in self._bands:
            self._bands["dots"].reset(Qgis.GeometryType.Point)
        if "line_arrows" in self._bands:
            self._bands["line_arrows"].reset(POLYGON)

    def _clear_direction_mark(self):
        self._mark = None
        for key, kind in (("direction", LINE), ("arrow", LINE), ("head", POLYGON)):
            if key in self._bands:
                self._bands[key].reset(kind)

    def refresh_preview(self):
        self._clear_preview()
        if self.info is None or not self._open:
            return
        geoms = self._selected()
        self._draw_line_arrows(geoms)
        plans = [self._plan(g) for g in geoms]
        self.refresh_info(plans)
        ready = bool(geoms) and any(p is not None for p in plans)
        if self.divide_button is not None and not sip.isdeleted(self.divide_button):
            self.divide_button.setEnabled(ready)
        if not self.show_preview or not ready:
            return
        cuts_band = self._band("cuts", QColor(110, 110, 110), 1, dashed=True)      # Pro: thin grey dashes
        dots = self._dots_band()
        points = []
        for plan in plans:
            if plan is None:
                continue
            for cut in plan[1]:
                if isinstance(cut, QgsPointXY):
                    points.append(cut)
                else:
                    cuts_band.addGeometry(cut, QgsCoordinateReferenceSystem(), False)
        cuts_band.updatePosition()
        cuts_band.update()
        for i, p in enumerate(points[:2000]):
            dots.addPoint(p, i == len(points[:2000]) - 1)

    def _draw_line_arrows(self, geoms):
        """Pro's small black arrow at the end of each selected line the cutting runs towards."""
        if self.kind != "line":
            return
        band = self._bands.get("line_arrows")
        if band is None:
            band = QgsRubberBand(self.canvas(), POLYGON)
            band.setColor(QColor(0, 0, 0))
            band.setFillColor(QColor(0, 0, 0))
            band.setWidth(1)
            self._bands["line_arrows"] = band
        mupp = self.canvas().mapUnitsPerPixel()
        triangles = []
        for g in geoms:
            pts = [QgsPointXY(v.x(), v.y()) for v in g.vertices()] if not g.isMultipart() else []
            if len(pts) < 2:
                continue
            tip, behind = (pts[0], pts[1]) if self.from_end else (pts[-1], pts[-2])      # the end the measuring runs to
            d = _unit((tip.x() - behind.x(), tip.y() - behind.y()))
            if d is None:
                continue
            base = QgsPointXY(tip.x() - d[0] * 14 * mupp, tip.y() - d[1] * 14 * mupp)
            side = (-d[1] * 5 * mupp, d[0] * 5 * mupp)
            triangles.append([tip, QgsPointXY(base.x() + side[0], base.y() + side[1]),
                              QgsPointXY(base.x() - side[0], base.y() - side[1])])
        for triangle in triangles:
            band.addGeometry(QgsGeometry.fromPolygonXY([triangle + [triangle[0]]]), QgsCoordinateReferenceSystem(), False)
        band.updatePosition()
        band.update()

    def _dots_band(self):
        band = self._bands.get("dots")
        if band is None:
            band = QgsRubberBand(self.canvas(), Qgis.GeometryType.Point)
            band.setColor(QColor(200, 40, 40))
            band.setFillColor(QColor(200, 40, 40))
            band.setIconSize(8)
            with suppress(AttributeError):
                band.setIcon(QgsRubberBand.IconType.ICON_BOX)
            self._bands["dots"] = band
        return band

    def _mark_direction(self, a, b):
        """While the second point of a line is being chosen: a dashed line from a to the mouse."""
        band = self._band("direction", QColor(0, 0, 0), 2, dashed=True)
        band.setColor(QColor(0, 0, 0))
        band.setWidth(2)
        band.setLineStyle(Qt.PenStyle.DashLine)
        band.reset(LINE)
        band.addPoint(a, False)
        band.addPoint(b, True)

    def _show_direction(self, a, b, extend=0.0):
        """Pro's mark: the line (a to b, stretched by `extend` of its length at both ends) in blue and an arrow from
        its middle pointing the way the parts will be laid out."""
        self._mark = (a, b, extend)
        self._draw_direction()

    def _draw_direction(self):
        if getattr(self, "_mark", None) is None or self.direction is None:
            return
        a, b, extend = self._mark
        dx, dy = b.x() - a.x(), b.y() - a.y()
        a = QgsPointXY(a.x() - dx * extend, a.y() - dy * extend)
        b = QgsPointXY(b.x() + dx * extend, b.y() + dy * extend)
        blue = QColor(51, 153, 255)
        line = self._band("direction", blue, 3)
        line.setColor(blue)
        line.setWidth(3)
        line.setLineStyle(Qt.PenStyle.SolidLine)
        line.reset(LINE)
        line.addPoint(a, False)
        line.addPoint(b, True)
        d, n = self._vectors()
        mupp = self.canvas().mapUnitsPerPixel()
        mid = QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        tip = QgsPointXY(mid.x() + n[0] * 45 * mupp, mid.y() + n[1] * 45 * mupp)
        arrow = self._band("arrow", blue, 3)
        arrow.reset(LINE)
        arrow.addPoint(mid, False)
        arrow.addPoint(tip, True)
        head = self._bands.get("head")
        if head is None:
            head = QgsRubberBand(self.canvas(), POLYGON)
            head.setColor(blue)
            head.setFillColor(blue)
            head.setWidth(1)
            self._bands["head"] = head
        head.reset(POLYGON)
        base = QgsPointXY(tip.x() - n[0] * 11 * mupp, tip.y() - n[1] * 11 * mupp)
        side = (-n[1] * 6 * mupp, n[0] * 6 * mupp)
        corners = [QgsPointXY(tip.x() + n[0] * 3 * mupp, tip.y() + n[1] * 3 * mupp),
                   QgsPointXY(base.x() + side[0], base.y() + side[1]), QgsPointXY(base.x() - side[0], base.y() - side[1])]
        for i, p in enumerate(corners):
            head.addPoint(p, i == len(corners) - 1)            # the last one refreshes the band's extent

    # ------------------------------------------------------------------ the map
    def on_click(self, point):
        if self.pick_mode == "edge":
            polygons = [h for h in pick_features(self.canvas(), point, (POLYGON,))]
            if not polygons:
                self.say("no polygon edge there.")
                return
            found = edge_direction(polygons[0][2], point)
            if found is None:
                self.say("could not read that edge.")
                return
            d, n, (a, b) = found
            self._set_direction(d, n)
            self._show_direction(a, b, extend=0.2)
        elif self.pick_mode == "line":
            if self._line_start is None:
                self._line_start = point
                self._mark_direction(point, point)
                return
            start = self._line_start
            d = _unit((point.x() - start.x(), point.y() - start.y()))
            if d is None:
                self.say("the two points are the same.")
                return
            self._set_direction(d, (-d[1], d[0]))
            self._show_direction(start, point)

    def _set_direction(self, d, n):
        self.direction = (d, n)
        self.flip = False
        self.pick_mode = None
        self._line_start = None
        self.set_selecting(True)                             # back to selecting polygons
        self.refresh_page()
        self.say("direction set.")

    def on_move(self, point):
        if self.pick_mode == "line" and self._line_start is not None:
            self._mark_direction(self._line_start, point)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self.pick_mode is not None and not self._selecting:
            self.set_pick_mode(self.pick_mode)               # toggles it off
            e.ignore()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------ the run
    def divide(self):
        wanted = LINE if self.kind == "line" else POLYGON
        if wanted == POLYGON and self.direction is None:
            self.say("set the direction first.")
            return 0
        changes, adds, made = [], [], 0
        for layer, ids in editable_selection(self.canvas()):
            if layer.geometryType() != wanted:
                continue
            canvas_crs = self.canvas().mapSettings().destinationCrs()
            to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
            multi = QgsWkbTypes.isMultiType(layer.wkbType())
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(ids))):
                g = QgsGeometry(f.geometry())
                if to_map is not None:
                    g.transform(to_map)
                plan = self._plan(g)
                if plan is None:
                    continue
                attrs = _copyable(layer, f.attributes())
                pieces = [self._shaped(p, multi) for p in plan[0]]
                changes.append((layer, f.id(), pieces[0][0]))
                for extra in pieces[0][1:]:
                    adds.append((layer, extra, attrs))
                for group in pieces[1:]:
                    for extra in group:
                        adds.append((layer, extra, attrs))
                made += len(plan[0]) - 1
        if not changes:
            self.say("nothing to divide - check the direction and the values.")
            return 0
        failed = commit(self.canvas(), changes, adds, "Divide features")
        if failed:
            self.say(f"could not edit {', '.join(failed)} - is it in edit mode?")
            return 0
        self.say(f"Divide completed: {made} new feature(s). (Ctrl+Z undoes it)")
        self._clear_preview()
        self.refresh_page()
        return made

    @staticmethod
    def _shaped(piece, multi):
        """[piece geometries] in the layer's form: one multi-part piece, or one polygon per part in a single-part layer."""
        if multi:
            out = QgsGeometry(piece)
            if not out.isMultipart():
                out.convertToMultiType()
            return [out]
        if piece.isMultipart():
            if piece.type() == LINE:
                return [QgsGeometry.fromPolylineXY(part) for part in piece.asMultiPolyline()]
            return [QgsGeometry.fromPolygonXY(poly) for poly in piece.asMultiPolygon()]
        return [QgsGeometry(piece)]


TOOL_CLASSES = {DivideTool.KEY: DivideTool}

"""
Traverse, laid out like the Traverse pane of ArcGIS Pro: a blue instruction bar, the Template (the editable line layer
the lines are made in), the Start point, the Courses table and the COGO settings, with Show Preview and Create at the
bottom. Click the start point on the map (it snaps; a green square marks it) or type it, then fill in one row per
course - Direction, Distance and, for a curve, Radius ("Click to add new row." adds one). Each course starts where the
one before ends. Show Preview draws the traverse in blue while you type.

    Direction   in the direction type and units of the COGO settings (north azimuth 45-30-00, quadrant N45-30-00E ...)
    Distance    in the distance unit of the settings (the layer's map units when none is chosen); with a Radius it is
                the length of the chord. Radius: positive turns clockwise, negative counter-clockwise; empty = straight
    One feature per course   each course is a line carrying its Direction, Distance, Radius and ArcLength; off, the
                traverse is one connected line

The new lines are selected afterwards. One undoable edit command.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsSettings
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QTableWidget, QTableWidgetItem

from ..selection import _transform, all_vector_layers, map_layers
from .buffer import layer_icon
from .cogo import CogoSettings, SettingsForm, parse_direction, traverse, traverse_shapes
from .page import DoneStrip, button, check, project_layer, section_label, spin
from .reshape import PickTool
from ...core.compat import enum_value

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
BLUE = QColor(0, 121, 193, 220)
GREEN = QColor(40, 170, 70)
COLUMNS = ("#", "Direction", "Distance", "Radius")
MARKER_BOX = enum_value((Qgis, 'VertexMarkerType.Box'), (QgsVertexMarker, 'IconType.ICON_BOX'))  # QGIS >= 3.40 / 4

START_TEXT = "Click on the map to set the start point."
COURSES_TEXT = "Enter the courses: a direction and a distance (and a radius for a curve) in each row."


class TraverseTool(PickTool):
    KEY = "traverse"
    LABEL = "Traverse"
    HINT = ("click the start point on the map (or type it), fill in the courses - direction, distance and, for a "
            "curve, radius - then press Create.")
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = False

    def __init__(self, canvas, iface, select_tool=None):
        s = QgsSettings()
        self.show_preview = s.value(SETTINGS + "traverse_preview", True, type=bool)
        self.per_course = s.value(SETTINGS + "traverse_per_course", True, type=bool)
        self.info = self.template_combo = self.table = self.form = self.create_button = None
        self.x_spin = self.y_spin = self.preview_check = None
        self.start_set = False
        self._open = False
        self._open_row = None
        self._listed = []
        self._result = None                  # (text, ok) of the last Create, until something is edited
        self._band = None
        self._marker = None
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

    def reset(self):
        self.start_set = False
        self._draw()
        super().reset()

    def dispose(self):
        for item in (self._band, self._marker):
            if item is not None:
                with suppress(Exception):
                    self.canvas().scene().removeItem(item)
        self._band = self._marker = None
        super().dispose()

    def start_over(self):
        self._result = None
        super().start_over()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)

    def build_page(self, box):
        box.addWidget(section_label("Template"))
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.setToolTip("The editable line layer the lines are created in")
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        box.addWidget(section_label("Start point"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.x_spin, self.y_spin = spin(0.0, decimals=4), spin(0.0, decimals=4)
        for label, widget in (("X (map CRS)", self.x_spin), ("Y (map CRS)", self.y_spin)):
            widget.setToolTip("Click on the map to set the start point, or type it")
            widget.valueChanged.connect(lambda _v: self._start_typed())
            form.addRow(label, widget)
        box.addLayout(form)
        box.addWidget(section_label("Courses"))
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(140)
        self.table.setMaximumHeight(240)
        self.table.cellClicked.connect(self._table_clicked)
        self.table.itemChanged.connect(self._cell_edited)
        box.addWidget(self.table)
        self._reset_table()
        row = QHBoxLayout()
        self.remove_button = button("Remove row", self.remove_row, "Remove the selected row")
        row.addWidget(self.remove_button)
        row.addWidget(button("Clear", self.clear_rows, "Remove every row"))
        row.addStretch(1)
        box.addLayout(row)
        box.addWidget(section_label("COGO settings"))
        self.form = SettingsForm(CogoSettings.load())
        for signal in (self.form.type_box.currentIndexChanged, self.form.unit_box.currentIndexChanged,
                       self.form.distance_box.currentIndexChanged, self.form.create_box.toggled):
            signal.connect(lambda _v=None: self._settings_changed())
        box.addWidget(self.form)
        self._unit_header()
        self._rebuild_templates()

    def build_footer(self, box):
        self.per_check = check("One feature per course, with its COGO values", self.per_course, self.set_per_course)
        self.per_check.setToolTip("Off: the whole traverse is a single connected line")
        box.addWidget(self.per_check)
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        self.preview_check.setToolTip("Draw the traverse on the map while you type")
        row.addWidget(self.preview_check, 1)
        self.create_button = button("Create", self.create_from_form, "Add the lines to the template layer "
                                    "(Ctrl+Z undoes it)")
        row.addWidget(self.create_button)
        box.addLayout(row)

    # ------------------------------------------------------------------ options
    def set_per_course(self, on):
        self.per_course = bool(on)
        QgsSettings().setValue(SETTINGS + "traverse_per_course", self.per_course)

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "traverse_preview", self.show_preview)
        self.update_page()

    def _settings_changed(self):
        self.form.settings().save()
        self._unit_header()
        self._edited()

    def _unit_header(self):
        text = self.form.distance_box.currentText() if self.form is not None else ""
        item = self.table.horizontalHeaderItem(2)
        if item is not None:
            item.setText(f"Distance ({text})" if text and self.form.distance_box.currentData() else "Distance")

    # ------------------------------------------------------------------ the template
    def _template_layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.geometryType() == LINE
                and (shown is None or layer.id() in shown)]

    def _rebuild_templates(self):
        current = self.template_combo.currentData() or QgsSettings().value(SETTINGS + "traverse_template", "")
        layers = self._template_layers()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for layer in layers:
            self.template_combo.addItem(layer_icon(layer), layer.name(), layer.id())
        self._listed = [layer.id() for layer in layers]
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(current)))
        self.template_combo.blockSignals(False)

    def _template_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "traverse_template", self.template_combo.currentData() or "")
        self._edited()

    def template(self):
        return project_layer(self.template_combo.currentData()) if self.template_combo is not None else None

    # ------------------------------------------------------------------ the courses table
    def _reset_table(self):
        table = self.table
        table.blockSignals(True)
        table.setRowCount(0)
        self._open_row = None
        self._add_row(refresh=False)
        self._open_row = table.rowCount()
        table.insertRow(self._open_row)
        item = QTableWidgetItem("Click to add new row.")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        table.setItem(self._open_row, 0, item)
        table.setSpan(self._open_row, 0, 1, len(COLUMNS))
        table.blockSignals(False)

    def _add_row(self, direction="", distance="", radius="", refresh=True):
        table = self.table
        table.blockSignals(True)
        row = table.rowCount() if self._open_row is None else self._open_row
        table.insertRow(row)
        number = QTableWidgetItem(str(row + 1))
        number.setFlags(Qt.ItemFlag.ItemIsEnabled)
        number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, 0, number)
        for col, text in ((1, direction), (2, distance), (3, radius)):
            table.setItem(row, col, QTableWidgetItem(str(text)))
        if self._open_row is not None:
            self._open_row += 1
        table.blockSignals(False)
        if refresh:
            self._edited()

    def add_row(self, values=("", "", "")):
        self._add_row(*values)

    def remove_row(self):
        row = self.table.currentRow()
        if row < 0 or (self._open_row is not None and row >= self._open_row):
            return
        self.table.blockSignals(True)
        self.table.removeRow(row)
        self._open_row -= 1
        for r in range(self._open_row):
            self.table.item(r, 0).setText(str(r + 1))
        if self._open_row == 0:
            self.table.blockSignals(False)
            self._add_row(refresh=False)
        self.table.blockSignals(False)
        self._edited()

    def clear_rows(self):
        self._reset_table()
        self._edited()

    def _table_clicked(self, row, _col):
        if self._open_row is not None and row == self._open_row:
            self._add_row()

    def _cell_edited(self, _item):
        self._edited()

    def _cell(self, row, col):
        item = self.table.item(row, col)
        return item.text().strip() if item is not None else ""

    def courses(self):
        """[(north azimuth, distance, radius)]; ValueError('row N: ...') for the first bad row. Empty rows are skipped."""
        settings = self.form.settings()
        out = []
        for row in range(self._open_row if self._open_row is not None else self.table.rowCount()):
            direction, distance, radius = (self._cell(row, c) for c in (1, 2, 3))
            if not (direction or distance or radius):
                continue
            try:
                azimuth = parse_direction(direction, settings.direction_type, settings.direction_unit)
                if not distance:
                    raise ValueError("the distance is empty")
                length = float(distance)
                curve = float(radius) if radius else 0.0
                if length <= 0:
                    raise ValueError("the distance must be more than zero")
            except ValueError as exc:
                text = str(exc)
                if text.startswith("could not convert"):
                    text = "the distance and radius must be numbers"
                raise ValueError(f"row {row + 1}: {text}") from None
            out.append((azimuth, length, curve))
        if not out:
            raise ValueError("add at least one course")
        return out

    # ------------------------------------------------------------------ the start point
    def start_point(self):
        return QgsPointXY(self.x_spin.value(), self.y_spin.value())

    def set_start(self, point):
        for spin_box, value in ((self.x_spin, point.x()), (self.y_spin, point.y())):
            spin_box.blockSignals(True)
            spin_box.setValue(value)
            spin_box.blockSignals(False)
        self.start_set = True
        self._edited()

    def _start_typed(self):
        self.start_set = True
        self._edited()

    # ------------------------------------------------------------------ the map
    def on_click(self, point):
        self.set_start(QgsPointXY(point))

    def on_right_click(self, _e):
        self.start_set = False
        self._edited()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.start_set = False
            self._edited()
            e.ignore()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------ preview and state
    def _edited(self):
        """Something changed: forget the last result and bring the strip, the button and the preview up to date."""
        self._result = None
        self.update_page()

    def _draw(self, points=None):
        """The start marker and, with points, the preview line (both gone when there is nothing to show)."""
        if self._band is not None:
            self._band.reset(LINE)
        if self._marker is not None:
            self._marker.hide()
        if not self._open or not self.start_set:
            return
        if self._marker is None:
            self._marker = QgsVertexMarker(self.canvas())
            self._marker.setIconType(MARKER_BOX)
            self._marker.setColor(GREEN)
            self._marker.setPenWidth(3)
            self._marker.setIconSize(11)
        self._marker.setCenter(self.start_point())
        self._marker.show()
        if points and len(points) > 1:
            if self._band is None:
                self._band = QgsRubberBand(self.canvas(), LINE)
                self._band.setColor(BLUE)
                self._band.setWidth(3)
                self._band.setLineStyle(Qt.PenStyle.DashLine)
            for i, p in enumerate(points):
                self._band.addPoint(p, i == len(points) - 1)

    def _preview_points(self):
        """The traverse in the map's CRS, or [] (no start, no template, or a row that cannot be read yet)."""
        layer = self.template()
        if layer is None or not self.start_set:
            return []
        try:
            shapes = traverse_shapes(self.canvas(), layer, self.start_point(), self.courses(), self.form.settings(),
                                     False)
        except ValueError:
            return []
        geometry = QgsGeometry(shapes[0][0])
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        if layer.crs() != canvas_crs:
            geometry.transform(_transform(layer.crs(), canvas_crs))
        return [QgsPointXY(v.x(), v.y()) for v in geometry.vertices()]

    def update_page(self):
        if self.create_button is None or sip.isdeleted(self.create_button):
            return
        if [layer.id() for layer in self._template_layers()] != self._listed:
            self._rebuild_templates()
        self._draw(self._preview_points() if self.show_preview else None)
        ready = self.start_set and self.template() is not None
        try:
            self.courses()
        except ValueError:
            ready = False
        self.create_button.setEnabled(ready)
        if self._result:
            self.info.show_text(*self._result)
        elif not self.start_set:
            self.info.show_text(START_TEXT, ok="info")
        elif self.template() is None:
            self.info.show_text("There is no editable line layer to use as a template.", ok="info")
        else:
            try:
                self.courses()
                self.info.hide()
            except ValueError as exc:
                text = str(exc)
                self.info.show_text(COURSES_TEXT if text == "add at least one course" else text[:1].upper() + text[1:]
                                    + ".", ok="info")

    # ------------------------------------------------------------------ the run
    def tell(self, text, ok=True):
        self._result = (text, ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))
        self.update_page()

    def create_from_form(self):
        layer = self.template()
        if layer is None:
            self.tell("There is no editable line layer to use as a template.", ok=False)
            return 0
        if not self.start_set:
            self.tell(START_TEXT, ok=False)
            return 0
        try:
            courses = self.courses()
        except ValueError as exc:
            self.tell(f"{str(exc)[:1].upper()}{str(exc)[1:]}.", ok=False)
            return 0
        settings = self.form.settings()
        settings.save()
        return self.create(layer, self.start_point(), courses, settings, self.per_course)

    def create(self, layer, point, courses, settings, per_course=True):
        created = []
        try:
            count, added = traverse(self.canvas(), layer, point, courses, settings, per_course, created)
        except ValueError as exc:
            self.tell(f"{str(exc)[:1].upper()}{str(exc)[1:]}.", ok=False)
            return 0
        if count:
            layer.selectByIds([fid for _l, fid in created])
        text = "Traverse completed." if count else "Nothing was created."
        if added:
            text += f" (Fields added: {', '.join(added)})"
        self.tell(text, ok=bool(count))
        return count


TOOL_CLASSES = {TraverseTool.KEY: TraverseTool}

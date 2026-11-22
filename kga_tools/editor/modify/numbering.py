"""
Sequential Numbering, laid out like the Sequential Numbering pane of ArcGIS Pro: the Layer, the Field, the Format, the
Start value and the Increment, and the tool tile. Draw a line across the features on the map - click its points,
then right-click, double-click or press Enter - and the features it crosses get the numbers in the order the line
meets them; a single click numbers the one feature under it. The Start value moves on to the next number after each
run, so the next line carries on from there.

    Layer / Field   an editable layer and one of its number or text fields
    Format          for a text field: the text around the number, with # standing for it - "A-###" gives A-001, A-002 ...
                    (as many # as digits; a lone # means no padding). Greyed out "#" for a number field
    Start value     the first number; Increment: the step (negative counts down)

Values longer than a text field allows are cut to its length. One undoable edit command per run.
"""

import os
import re
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSettings,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor
from ...branding import themed_icon
from qgis.PyQt.QtWidgets import QComboBox, QLabel, QLineEdit, QSizePolicy, QSpinBox, QToolButton

from ...algorithms.sequential_numbering_dialog import SequentialNumberingMapTool, classify_field
from ..selection import all_vector_layers, map_layers
from .page import DoneStrip, add_form, project_layer
from .reshape import PickTool
from ...core.compat import enum_value

SETTINGS = "kga_editor/modify_"
KGA_ICON = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "icons", "editing/seqNumbering.png"))
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
MARKER_BOX = enum_value((Qgis, 'VertexMarkerType.Box'), (QgsVertexMarker, 'IconType.ICON_BOX'))  # QGIS >= 3.40 / 4
PICK_RADIUS_PX = 6


def format_number(fmt, number, kind):
    """The value to write: a number for number fields, else `fmt` with its first run of # replaced by the number
    (zero-padded to as many digits as there are #; the number is appended when there is no #)."""
    if kind == "int":
        return int(round(number))
    if kind == "double":
        return float(number)
    text = str(int(number)) if float(number).is_integer() else ("%g" % number)
    match = re.search(r"#+", fmt or "")
    if match is None:
        return f"{fmt or ''}{text}"
    digits = len(match.group(0))
    body = text.zfill(digits) if digits > 1 else text
    return fmt[:match.start()] + body + fmt[match.end():]


def features_along(layer, points, canvas):
    """
    The ids of the features of `layer` met by the path `points` (map CRS), in the order they are met (a single point:
    the features within a few pixels of it, nearest first).
    """
    single = len(points) == 1
    tolerance = canvas.mapUnitsPerPixel() * PICK_RADIUS_PX
    if single:
        path = QgsGeometry.fromPointXY(points[0])
        search = path.buffer(tolerance, 8)
    else:
        path = QgsGeometry.fromPolylineXY(points)
        search = path if layer.geometryType() == POLYGON else path.buffer(tolerance, 8)
    canvas_crs = canvas.mapSettings().destinationCrs()
    if canvas_crs != layer.crs():
        transform = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
        path.transform(transform)
        search.transform(transform)
    engine = QgsGeometry.createGeometryEngine(search.constGet())
    engine.prepareGeometry()
    hits = []
    for feature in layer.getFeatures(QgsFeatureRequest().setFilterRect(search.boundingBox()).setNoAttributes()):
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty() or not engine.intersects(geometry.constGet()):
            continue
        if single:
            measure = geometry.distance(path)
        else:
            measure = SequentialNumberingMapTool._measure(path, geometry, False)
        hits.append((measure, feature.id()))
    hits.sort()
    return [fid for _m, fid in hits]


class NumberingTool(PickTool):
    KEY = "sequential_numbering"
    LABEL = "Sequential Numbering"
    HINT = "choose the layer, field and numbers, then draw a line across the features (right-click or Enter to finish)."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = False

    def __init__(self, canvas, iface, select_tool=None):
        self.info = self.layer_combo = self.field_combo = self.format_edit = None
        self.start_spin = self.increment_spin = self.tool_tile = None
        self._listed = []
        self._open = False
        self._band = None
        self._marker = None
        self._ignore_next_release = False
        self.points = []
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        self._open = True
        super().activate()
        self.refresh_page()

    def deactivate(self):
        self._open = False
        self.points = []
        self._draw()
        super().deactivate()

    def reset(self):
        self.points = []
        self._draw()
        super().reset()

    def dispose(self):
        for item in (self._band, self._marker):
            if item is not None:
                with suppress(Exception):
                    self.canvas().scene().removeItem(item)
        self._band = self._marker = None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)

    def build_page(self, box):
        box.addWidget(QLabel("Layer"))
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self._layer_changed)
        box.addWidget(self.layer_combo)
        box.addWidget(QLabel("Field"))
        self.field_combo = QComboBox()
        self.field_combo.currentIndexChanged.connect(self._field_changed)
        box.addWidget(self.field_combo)
        form = add_form(box)
        self.format_edit = QLineEdit(str(QgsSettings().value(SETTINGS + "numbering_format", "#")))
        self.format_edit.textChanged.connect(self._format_changed)
        form.addRow("Format", self.format_edit)
        self.start_spin = QSpinBox()
        self.start_spin.setRange(-2147483647, 2147483647)
        self.start_spin.setValue(int(QgsSettings().value(SETTINGS + "numbering_start", 1)))
        self.start_spin.valueChanged.connect(lambda v: QgsSettings().setValue(SETTINGS + "numbering_start", int(v)))
        form.addRow("Start value", self.start_spin)
        self.increment_spin = QSpinBox()
        self.increment_spin.setRange(-1000000, 1000000)
        self.increment_spin.setValue(int(QgsSettings().value(SETTINGS + "numbering_increment", 1)))
        self.increment_spin.valueChanged.connect(lambda v: QgsSettings().setValue(SETTINGS + "numbering_increment",
                                                                                  int(v)))
        form.addRow("Increment", self.increment_spin)
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(themed_icon(KGA_ICON))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Sequential Numbering")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT)
        tile.clicked.connect(lambda _c=False: tile.setChecked(True))
        self.tool_tile = tile
        box.addWidget(tile)
        self._rebuild_layers()

    # ------------------------------------------------------------------ layers and fields
    def _layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and (shown is None or layer.id() in shown)]

    def _rebuild_layers(self):
        current = self.layer_combo.currentData() or QgsSettings().value(SETTINGS + "numbering_layer", "")
        layers = self._layers()
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in layers:
            self.layer_combo.addItem(layer.name(), layer.id())
        self._listed = [layer.id() for layer in layers]
        self.layer_combo.setCurrentIndex(max(0, self.layer_combo.findData(current)))
        self.layer_combo.blockSignals(False)
        self._rebuild_fields()

    def current_layer(self):
        return project_layer(self.layer_combo.currentData()) if self.layer_combo is not None else None

    def _layer_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "numbering_layer", self.layer_combo.currentData() or "")
        self._rebuild_fields()

    def _rebuild_fields(self):
        layer = self.current_layer()
        wanted = QgsSettings().value(SETTINGS + "numbering_field", "")
        keep = self.field_combo.currentData() or wanted
        self.field_combo.blockSignals(True)
        self.field_combo.clear()
        if layer is not None:
            for field in layer.fields():
                if classify_field(field) in ("int", "double", "string"):
                    self.field_combo.addItem(field.name(), field.name())
        self.field_combo.setCurrentIndex(max(0, self.field_combo.findData(keep)))
        self.field_combo.blockSignals(False)
        self._field_changed(0)

    def _field_changed(self, _index):
        if self.field_combo.currentData():
            QgsSettings().setValue(SETTINGS + "numbering_field", self.field_combo.currentData())
        self._show_format()

    def field_kind(self):
        layer = self.current_layer()
        name = self.field_combo.currentData() if self.field_combo is not None else None
        if layer is None or not name:
            return None
        index = layer.fields().lookupField(name)
        return classify_field(layer.fields().at(index)) if index >= 0 else None

    def _show_format(self):
        """Only a text field takes a format; a number field shows a greyed-out # like Pro."""
        text = self.field_kind() == "string"
        self.format_edit.setEnabled(text)
        self.format_edit.blockSignals(True)
        self.format_edit.setText(str(QgsSettings().value(SETTINGS + "numbering_format", "#")) if text else "#")
        self.format_edit.blockSignals(False)

    def _format_changed(self, value):
        if self.format_edit.isEnabled():
            QgsSettings().setValue(SETTINGS + "numbering_format", value)

    def update_page(self):
        if self.layer_combo is None or sip.isdeleted(self.layer_combo) or not self._open:
            return
        if [layer.id() for layer in self._layers()] != self._listed:
            self._rebuild_layers()

    # ------------------------------------------------------------------ drawing
    def _sketch(self):
        if self._band is None:
            self._band = QgsRubberBand(self.canvas(), LINE)
            self._band.setColor(QColor(0, 0, 0))
            self._band.setWidth(2)
            self._band.setLineStyle(Qt.PenStyle.DashLine)
        return self._band

    def _draw(self, tail=None):
        band = self._sketch()
        band.reset(LINE)
        shown = self.points + ([tail] if tail is not None else [])
        for i, p in enumerate(shown):
            band.addPoint(p, i == len(shown) - 1)
        if self._marker is None:
            if not shown:
                return
            self._marker = QgsVertexMarker(self.canvas())
            self._marker.setIconType(MARKER_BOX)
            self._marker.setColor(QColor(0, 140, 60))
            self._marker.setIconSize(8)
            self._marker.setPenWidth(2)
        if shown:
            self._marker.setCenter(shown[0])
            self._marker.show()
        else:
            self._marker.hide()

    def canvasMoveEvent(self, e):
        point = QgsPointXY(e.snapPoint())                  # must run BEFORE mapPointMatch()
        self.snap_indicator.setMatch(e.mapPointMatch())
        if self.points:
            self._draw(point)

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.finish()
        elif e.button() == Qt.MouseButton.LeftButton:
            if self._ignore_next_release:                  # the second click of a double-click
                self._ignore_next_release = False
                return
            point = QgsPointXY(e.snapPoint())
            if not self.points or point != self.points[-1]:
                self.points.append(point)
            self._draw()

    def canvasDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._ignore_next_release = True
            self.finish()

    def keyPressEvent(self, e):
        key = e.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.points:
            self.finish()
            e.ignore()
        elif key == Qt.Key.Key_Backspace and self.points:
            self.points.pop()
            self._draw()
            e.ignore()
        else:
            super().keyPressEvent(e)

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ the run
    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text)

    def finish(self):
        points, self.points = list(self.points), []
        self._draw()
        if not points:
            return 0
        layer = self.current_layer()
        if layer is None:
            self.tell("Choose an editable layer first.", ok=False)
            return 0
        name = self.field_combo.currentData()
        index = layer.fields().lookupField(name) if name else -1
        if index < 0:
            self.tell("Choose a field first.", ok=False)
            return 0
        ids = features_along(layer, points, self.canvas())
        if not ids:
            self.tell("No feature of that layer lies under the line.", ok=False)
            return 0
        return self.number(layer, index, ids)

    def number(self, layer, index, ids):
        """Write the next values, from the Start value, to the features in the given order."""
        kind = classify_field(layer.fields().at(index))
        length = layer.fields().at(index).length()
        value = self.start_spin.value()
        step = self.increment_spin.value()
        fmt = self.format_edit.text() if kind == "string" else "#"
        layer.beginEditCommand("Sequential Numbering")
        written, ok = 0, True
        for fid in ids:
            text = format_number(fmt, value, kind)
            if kind == "string" and length and length > 0 and len(text) > length:
                text = text[:length]
            ok = layer.changeAttributeValue(fid, index, text) and ok
            written += 1
            value += step
        if ok:
            layer.endEditCommand()
        else:
            layer.destroyEditCommand()
            self.tell(f"Could not write to {layer.name()} - is it in edit mode?", ok=False)
            return 0
        self.start_spin.setValue(value)                    # the next line carries on from here
        self.canvas().refresh()
        self.tell(f"Sequential numbering completed: {written} feature(s) numbered, next value {value}.")
        return written


TOOL_CLASSES = {NumberingTool.KEY: NumberingTool}

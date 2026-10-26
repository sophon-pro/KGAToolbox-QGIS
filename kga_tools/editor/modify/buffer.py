"""
Buffer, laid out like the Buffer pane of ArcGIS Pro: "Change the selection." and the selected features, the Template
(the layer that receives the buffers: a polygon layer gets polygons, a line layer gets the outlines), the Values
(Buffer Distance in a unit of your choice, Rings, Dissolve), Show Preview and the Buffer button.

    Buffer Distance   how far out from each selected feature (points, lines and polygons)
    Rings             several buffers, each one Buffer Distance further out; a polygon template gets them as separate
                      bands (ring 2 is the area between the first and the second buffer)
    Dissolve          the buffers of all the selected features become one shape (off: one shape per feature)

The new features take the template layer's default attribute values and are selected afterwards. One undoable edit.
"""

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
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QSpinBox

from ..selection import _transform, all_vector_layers, map_layers
from .divide import DIST_UNITS, DivideTool
from .page import DoneStrip, button, check, project_layer, section_label, spin
from .pick import _to_layer
from .reshape import PickTool

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
SEGMENTS = 24                                    # segments per quarter circle
PREVIEW_MAX = 60                                 # selected features previewed


def layer_icon(layer):
    """A small swatch in the layer's colour: a square for polygons, a dot for points, a line for lines."""
    colour = QColor(160, 160, 160)
    with suppress(Exception):
        symbol = layer.renderer().symbol()
        if symbol is not None:
            colour = symbol.color()
    pixmap = QPixmap(22, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    if layer.geometryType() == POLYGON:
        painter.fillRect(2, 1, 18, 12, colour)
    elif layer.geometryType() == Qgis.GeometryType.Point:
        painter.setPen(QPen(colour.darker(150), 1))
        painter.setBrush(colour)
        painter.drawEllipse(7, 2, 9, 9)
    else:
        painter.setPen(QPen(colour, 2))
        painter.drawLine(1, 7, 21, 7)
    painter.end()
    return QIcon(pixmap)


def buffer_rings(geometries, distance, rings, dissolve):
    """
    The buffers of `geometries` at `distance`, 2 x distance ... `rings` x distance. Returns [[ring k buffer for
    each shape] for k in 1..rings]: one shape per ring when dissolving, else one per geometry. Each is the whole
    buffer out to that distance (not the band between two rings).
    """
    out = []
    for k in range(1, rings + 1):
        shapes = [g.buffer(distance * k, SEGMENTS) for g in geometries]
        shapes = [s for s in shapes if s is not None and not s.isNull() and not s.isEmpty()]
        if dissolve and len(shapes) > 1:
            merged = QgsGeometry.unaryUnion(shapes)
            shapes = [merged] if merged is not None and not merged.isNull() and not merged.isEmpty() else shapes
        out.append(shapes)
    return out


def ring_polygons(buffers):
    """Polygons for a polygon template: ring 1 whole, every further ring the band outside the one before it."""
    result = []
    for k, shapes in enumerate(buffers):
        for index, shape in enumerate(shapes):
            if k == 0:
                result.append(shape)
                continue
            inner = buffers[k - 1][index] if index < len(buffers[k - 1]) else None
            band = shape.difference(inner) if inner is not None else shape
            if band is not None and not band.isNull() and not band.isEmpty():
                result.append(band)
    return result


def ring_outlines(buffers):
    """Lines for a line template: the outline of every ring's buffer."""
    result = []
    for shapes in buffers:
        for shape in shapes:
            outline = shape.convertToType(LINE, False)
            if outline is not None and not outline.isNull() and not outline.isEmpty():
                result.append(outline)
    return result


class BufferTool(PickTool):
    KEY = "buffer"
    LABEL = "Buffer"
    HINT = "select features, choose the template and the distance, then press Buffer."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False                # clicking selects until you press Buffer

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.dissolve = settings.value(SETTINGS + "buffer_dissolve", True, type=bool)
        self.show_preview = settings.value(SETTINGS + "buffer_preview", True, type=bool)
        self.info = self.template_combo = self.distance_spin = self.unit_combo = None
        self.rings_spin = self.dissolve_check = self.buffer_button = None
        self._open = False
        self._band = self._fill = None
        self._listed = []
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def start_select_mode(self):
        self.set_selecting(True, auto=True)

    def activate(self):
        self._open = True
        super().activate()
        self.refresh_preview()

    def deactivate(self):
        self._open = False
        self._clear_preview()
        super().deactivate()
        self._clear_preview()

    def dispose(self):
        for item in (self._band, self._fill):
            if item is not None:
                with suppress(Exception):
                    self.canvas().scene().removeItem(item)
        self._band = self._fill = None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        super().build_selection_row(box)

    def build_page(self, box):
        settings = QgsSettings()
        box.addWidget(section_label("Template"))
        self.template_combo = QComboBox()
        self.template_combo.setIconSize(QSize(22, 14))
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        box.addWidget(section_label("Values"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Buffer Distance"), 1)
        self.distance_spin = spin(float(settings.value(SETTINGS + "buffer_distance", 10.0)), 0.0, 1e12, 3, 1.0)
        self.distance_spin.valueChanged.connect(self._values_changed)
        row.addWidget(self.distance_spin)
        self.unit_combo = QComboBox()
        index = 0
        for i, unit in enumerate(DIST_UNITS):
            self.unit_combo.addItem(QgsUnitTypes.toAbbreviatedString(unit), unit)
            if unit == QgsProject.instance().distanceUnits():
                index = i
        self.unit_combo.setCurrentIndex(index)
        self.unit_combo.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        row.addWidget(self.unit_combo)
        box.addLayout(row)
        rings = QHBoxLayout()
        rings.addWidget(QLabel("Rings"), 1)
        self.rings_spin = QSpinBox()
        self.rings_spin.setRange(1, 100)
        self.rings_spin.setValue(int(settings.value(SETTINGS + "buffer_rings", 1)))
        self.rings_spin.valueChanged.connect(self._values_changed)
        rings.addWidget(self.rings_spin, 1)
        box.addLayout(rings)
        self.dissolve_check = check("Dissolve", self.dissolve, self.set_dissolve)
        box.addWidget(self.dissolve_check)
        self._rebuild_templates()

    def build_footer(self, box):
        row = QHBoxLayout()
        self.preview_check = check("Show Preview", self.show_preview, self.set_preview)
        row.addWidget(self.preview_check, 1)
        self.buffer_button = button("Buffer", self.run_buffer, "Add the buffers to the template layer (Ctrl+Z undoes it)")
        row.addWidget(self.buffer_button)
        box.addLayout(row)

    def _template_layers(self):
        shown = map_layers(self.canvas())
        return [layer for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.geometryType() in (LINE, POLYGON)
                and (shown is None or layer.id() in shown)]

    def _template_ids(self):
        return [layer.id() for layer in self._template_layers()]

    def _rebuild_templates(self):
        """Editable polygon and line layers, in the layer tree's order; the one used last stays chosen."""
        current = self.template_combo.currentData() or QgsSettings().value(SETTINGS + "buffer_template", "")
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for layer in self._template_layers():
            self.template_combo.addItem(layer_icon(layer), layer.name(), layer.id())
        self._listed = self._template_ids()
        index = self.template_combo.findData(current)
        self.template_combo.setCurrentIndex(max(0, index))
        self.template_combo.blockSignals(False)

    def _template_changed(self, _index):
        QgsSettings().setValue(SETTINGS + "buffer_template", self.template_combo.currentData() or "")
        self.refresh_preview()

    def _values_changed(self, _value):
        settings = QgsSettings()
        settings.setValue(SETTINGS + "buffer_distance", self.distance_spin.value())
        settings.setValue(SETTINGS + "buffer_rings", self.rings_spin.value())
        self.refresh_preview()

    def set_dissolve(self, on):
        self.dissolve = bool(on)
        QgsSettings().setValue(SETTINGS + "buffer_dissolve", self.dissolve)
        self.refresh_preview()

    def set_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "buffer_preview", self.show_preview)
        if getattr(self, "preview_check", None) is not None and self.preview_check.isChecked() != self.show_preview:
            self.preview_check.setChecked(self.show_preview)
        self.refresh_preview()

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.buffer_button is None or sip.isdeleted(self.buffer_button):
            return
        if self._template_ids() != self._listed:
            self._rebuild_templates()
        self.refresh_preview()

    # ------------------------------------------------------------------ the numbers
    def template(self):
        return project_layer(self.template_combo.currentData()) if self.template_combo is not None else None

    def _distance(self):
        crs = self.canvas().mapSettings().destinationCrs()
        return self.distance_spin.value() * QgsUnitTypes.fromUnitToUnitFactor(self.unit_combo.currentData(), crs.mapUnits())

    def _selected(self, limit=None):
        """[geometry in the map's CRS] of the selected features of the layers shown on the map."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        shown = map_layers(self.canvas())
        out = []
        for layer in all_vector_layers():
            if not (layer.isSpatial() and layer.selectedFeatureCount() and (shown is None or layer.id() in shown)):
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

    def plan(self, limit=None):
        """The shapes the template layer would receive, in the map's CRS; [] when something is missing."""
        template = self.template()
        geoms = self._selected(limit)
        distance = self._distance()
        if template is None or not geoms or distance <= 0:
            return []
        if self.canvas().mapSettings().destinationCrs().isGeographic():
            return []
        buffers = buffer_rings(geoms, distance, self.rings_spin.value(), self.dissolve)
        return ring_polygons(buffers) if template.geometryType() == POLYGON else ring_outlines(buffers)

    # ------------------------------------------------------------------ preview
    def _clear_preview(self):
        if self._band is not None:
            self._band.reset(LINE)
        if self._fill is not None:
            self._fill.reset(POLYGON)

    def refresh_preview(self):
        self._clear_preview()
        if self.buffer_button is None or not self._open:
            return
        template = self.template()
        ready = (template is not None and self.distance_spin.value() > 0 and bool(self._selected(1))
                 and not self.canvas().mapSettings().destinationCrs().isGeographic())
        self.buffer_button.setEnabled(ready)
        if not self.show_preview or not ready:
            return
        shapes = self.plan(PREVIEW_MAX)
        if template.geometryType() == POLYGON:
            if self._fill is None:
                self._fill = QgsRubberBand(self.canvas(), POLYGON)
                self._fill.setColor(QColor(150, 150, 130, 160))
                self._fill.setFillColor(QColor(255, 235, 190, 70))
                self._fill.setWidth(1)
            for shape in shapes:
                self._fill.addGeometry(shape, QgsCoordinateReferenceSystem(), False)
            self._fill.updatePosition()
            self._fill.update()
        else:
            if self._band is None:
                self._band = QgsRubberBand(self.canvas(), LINE)
                self._band.setColor(QColor(150, 150, 130))
                self._band.setWidth(1)
            for shape in shapes:
                self._band.addGeometry(shape, QgsCoordinateReferenceSystem(), False)
            self._band.updatePosition()
            self._band.update()

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ the run
    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))

    def run_buffer(self):
        template = self.template()
        if template is None:
            self.tell("Choose an editable polygon or line layer as the Template.", ok=False)
            return 0
        if self.canvas().mapSettings().destinationCrs().isGeographic():
            self.tell("The map is in degrees - switch the project to a projected CRS so distances are in metres.", ok=False)
            return 0
        if self.distance_spin.value() <= 0:
            self.tell("Set a Buffer Distance above 0.", ok=False)
            return 0
        shapes = self.plan()
        if not shapes:
            self.tell("Select the features to buffer first.", ok=False)
            return 0
        multi = QgsWkbTypes.isMultiType(template.wkbType())
        shaper = DivideTool._shaped if template.geometryType() == POLYGON else self._line_pieces
        pieces = []
        for shape in shapes:
            pieces += shaper(shape, multi)
        template.beginEditCommand("Buffer")
        made = []
        ok = True
        for geom in pieces:
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
        self._clear_preview()
        for layer in all_vector_layers():
            if layer.selectedFeatureCount():
                layer.removeSelection()
        template.selectByIds(made)
        self.tell(f"Buffer completed: {len(made)} new feature(s) in {template.name()}.")
        self.refresh_page()
        return len(made)

    @staticmethod
    def _line_pieces(geom, multi):
        """Outlines for a line layer: one multi-part line, or one line per part in a single-part layer."""
        if multi:
            out = QgsGeometry(geom)
            if not out.isMultipart():
                out.convertToMultiType()
            return [out]
        if geom.isMultipart():
            return [QgsGeometry.fromPolylineXY(part) for part in geom.asMultiPolyline()]
        return [QgsGeometry(geom)]


TOOL_CLASSES = {BufferTool.KEY: BufferTool}

"""
Edit Vertices, laid out like the Edit Vertices pane of ArcGIS Pro and selecting like Move, Rotate and Scale.

The page has "Change the selection." and the selected features, the Edit Vertices tool tile, and at the bottom a
table of the vertices of the feature picked in the list: # / X / Y in the map's units. Type a new X or Y in a cell to
move that vertex, select rows and press the red cross to delete them, copy the table to the clipboard.

On the map (the tool is ours, not QGIS's Vertex tool, so selecting and editing are one flow):
    click a feature          selects it (replacing the selection) and shows its vertices as handles
    click empty map          clears the selection (Shift / Ctrl click adds / removes features)
    drag on empty map        with features selected: selects the VERTICES inside the box (Alt held: a freehand lasso);
                             Shift adds to the vertices already chosen, Ctrl removes from them. Chosen vertices are
                             orange; Shift / Ctrl click on a handle toggles one. To pick features by dragging use the
                             "Change the selection." button
    drag a handle            moves that vertex (snapping to the features around), one undoable edit
    double-click a segment   adds a vertex there
    Delete                   removes every chosen vertex, else the vertex under the mouse, else the rows selected in the
                             table; Esc first drops the chosen vertices
The table follows the map: a dragged vertex, an undo or a redo updates it, and the vertex of the selected row is
circled on the map.

Every change is one undoable edit command, written in the layer's own CRS.
"""

import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointLocator,
    QgsPointXY,
    QgsRectangle,
    QgsUnitTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QItemSelectionModel, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor, QGuiApplication, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from ..selection import _transform
from .page import hint_label, project_layer, section_label
from .pick import commit
from .transform import TransformTool, editable_selection
from ...core.compat import enum_value

MARKER_CIRCLE = enum_value((Qgis, 'VertexMarkerType.Circle'), (QgsVertexMarker, 'IconType.ICON_CIRCLE'))  # QGIS >= 3.40 / 4

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")


def _parts(geom):
    abstract = geom.constGet()
    return [abstract.geometryN(i) for i in range(abstract.numGeometries())] if geom.isMultipart() else [abstract]


def _rings(geom):
    """Every ring of a polygon geometry, exterior ring first in each part."""
    rings = []
    for polygon in _parts(geom):
        rings.append(polygon.exteriorRing())
        rings.extend(polygon.interiorRing(k) for k in range(polygon.numInteriorRings()))
    return rings


def shape_ok(geom):
    """False when edits left a line with fewer than 2 vertices or a polygon ring with fewer than 3 (or nothing)."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return False
    if geom.type() == Qgis.GeometryType.Polygon:
        return all(ring.numPoints() >= 4 for ring in _rings(geom))           # 3 corners plus the closing vertex
    if geom.type() == Qgis.GeometryType.Line:
        return all(part.numPoints() >= 2 for part in _parts(geom))
    return True


def end_numbers(geom):
    """The vertex numbers drawn red, like Pro: the last vertex of every line part and of every polygon ring (a
    polygon ring's closing vertex repeats its first, so the one before it is the last)."""
    if geom.type() == Qgis.GeometryType.Point:
        return set()
    numbers, start = set(), 0
    parts = _rings(geom) if geom.type() == Qgis.GeometryType.Polygon else _parts(geom)
    for part in parts:
        count = part.numPoints()
        closed = geom.type() == Qgis.GeometryType.Polygon and count > 1 and part.isClosed()
        numbers.add(start + count - (2 if closed else 1))
        start += count
    return numbers


def vertex_rows(geom):
    """[(vertex number in the geometry, x, y)] for the table: a polygon ring's closing vertex repeats its first, so
    it is left out (moving the first one moves both)."""
    if geom.type() != Qgis.GeometryType.Polygon:
        return [(number, point.x(), point.y()) for number, point in enumerate(geom.vertices())]
    rows, number = [], 0                                    # number counts every stored vertex, closers included
    for ring in _rings(geom):
        count = ring.numPoints()
        closed = count > 1 and ring.isClosed()
        for k in range(count):
            if not (closed and k == count - 1):
                point = ring.pointN(k)
                rows.append((number + k, point.x(), point.y()))
        number += count
    return rows


class SizeGrip(QFrame):
    """A thin separator line above the table: drag it up or down to make the table taller or shorter."""

    def __init__(self, table, parent=None):
        super().__init__(parent)
        self._table = table
        self._start = None
        self.setFixedHeight(9)
        self.setCursor(Qt.CursorShape.SplitVCursor)
        self.setToolTip("Drag to resize the vertex table")
        self.setStyleSheet("QFrame { border-top: 1px solid #a9adb1; border-bottom: 1px solid #d8dadc; margin: 3px 0; }"
                           "QFrame:hover { border-top-color: #0079c1; border-bottom-color: #0079c1; }")

    def mousePressEvent(self, e):
        self._start = (e.globalPos().y() if hasattr(e, "globalPos") else int(e.globalPosition().y()),
                       self._table.height())

    def mouseMoveEvent(self, e):
        if self._start is None:
            return
        y = e.globalPos().y() if hasattr(e, "globalPos") else int(e.globalPosition().y())
        self._table.setMinimumHeight(max(60, min(900, self._start[1] + self._start[0] - y)))

    def mouseReleaseEvent(self, e):
        self._start = None


class VertexTable(QWidget):
    """The vertices of one feature, in the map's CRS: editable X and Y, delete, copy."""

    COL_NUMBER, COL_X, COL_Y = range(3)
    edited = pyqtSignal()
    refreshed = pyqtSignal()

    def __init__(self, canvas, say=None, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.say = say or (lambda _text: None)
        self.layer = None
        self.fid = None
        self.rows = []                      # [(vertex number, x, y)] as shown
        self._loading = False
        self.decimals = 3

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        grip_row = box.count()                # the grip goes first, once the table exists
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        self.delete_button = QToolButton(self)
        self.delete_button.setObjectName("KgaModifyVertexDelete")
        self.delete_button.setText("✕")
        self.delete_button.setToolTip("Delete the selected vertices")
        self.delete_button.setStyleSheet("QToolButton { color: #c0392b; font-size: 12pt; border: none; padding: 0 6px; }"
                                         "QToolButton:disabled { color: #c9a9a4; }")
        self.delete_button.clicked.connect(self.delete_selected)
        self.copy_button = QToolButton(self)
        self.copy_button.setText("Copy")
        self.copy_button.setToolTip("Copy the table to the clipboard")
        self.copy_button.clicked.connect(self.copy_table)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("KgaModifyHint")
        bar.addWidget(self.delete_button)
        bar.addWidget(self.count_label, 1)
        bar.addWidget(self.copy_button)
        box.addLayout(bar)

        self.table = QTableWidget(0, 3, self)
        self.table.setMinimumHeight(170)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_NUMBER, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_X, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_Y, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._edited)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        box.addWidget(self.table, 1)
        self.grip = SizeGrip(self.table, self)
        box.insertWidget(grip_row, self.grip)
        self.set_units()

        self.marker = QgsVertexMarker(canvas)
        self.marker.setIconType(MARKER_CIRCLE)
        self.marker.setColor(QColor(255, 140, 0))
        self.marker.setIconSize(16)
        self.marker.setPenWidth(3)
        self.marker.hide()

    # ------------------------------------------------------------------ what is shown
    def set_units(self):
        crs = self.canvas.mapSettings().destinationCrs()
        unit = QgsUnitTypes.toString(crs.mapUnits()).capitalize() if crs.isValid() else ""
        self.decimals = 8 if crs.isValid() and crs.isGeographic() else 3
        self.table.setHorizontalHeaderLabels(["#", f"X ({unit})", f"Y ({unit})"])

    def load(self, layer, fid):
        """Show the vertices of feature `fid` of `layer` (None clears the table)."""
        if layer is not self.layer:
            self._disconnect()
            self.layer = layer
            if layer is not None:
                layer.geometryChanged.connect(self._geometry_changed)
        self.fid = fid
        self.refresh()

    def clear(self):
        self.load(None, None)

    def _disconnect(self):
        if self.layer is not None and not sip.isdeleted(self.layer):
            with suppress(TypeError, RuntimeError):
                self.layer.geometryChanged.disconnect(self._geometry_changed)
        self.layer = None

    def _geometry_changed(self, fid, _geometry):
        if fid == self.fid:
            self.refresh()

    def geometry(self):
        """The feature's geometry in the map's CRS (a copy), or None."""
        if self.layer is None or sip.isdeleted(self.layer) or self.fid is None:
            return None
        feature = self.layer.getFeature(self.fid)
        if not feature.isValid() or feature.geometry().isNull() or feature.geometry().isEmpty():
            return None
        g = QgsGeometry(feature.geometry())
        crs = self.canvas.mapSettings().destinationCrs()
        if self.layer.crs() != crs:
            g.transform(_transform(self.layer.crs(), crs))
        return g

    def refresh(self):
        g = self.geometry()
        keep = {index.row() for index in self.table.selectionModel().selectedRows()}
        self.rows = vertex_rows(g) if g is not None else []
        self.set_units()
        self._loading = True
        self.table.setRowCount(len(self.rows))
        for row, (_number, x, y) in enumerate(self.rows):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            index_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, self.COL_NUMBER, index_item)
            for col, value in ((self.COL_X, x), (self.COL_Y, y)):
                item = QTableWidgetItem(f"{value:.{self.decimals}f}")
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
        self._loading = False
        self.table.clearSelection()
        for row in keep:
            if row < len(self.rows):
                self.table.selectRow(row)
        n = len(self.rows)
        self.count_label.setText(f"{n} vertex" if n == 1 else f"{n} vertices" if n else "")
        self.delete_button.setEnabled(bool(self.table.selectionModel().selectedRows()))
        self._update_marker()
        self.refreshed.emit()

    # ------------------------------------------------------------------ the map marker
    def _selected_rows(self):
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _selection_changed(self):
        self.delete_button.setEnabled(bool(self._selected_rows()))
        self._update_marker()

    def _update_marker(self):
        rows = self._selected_rows()
        if rows and rows[0] < len(self.rows):
            _number, x, y = self.rows[rows[0]]
            self.marker.setCenter(QgsPointXY(x, y))
            self.marker.show()
        else:
            self.marker.hide()

    # ------------------------------------------------------------------ editing
    def _write(self, g, text):
        failed = commit(self.canvas, [(self.layer, self.fid, g)], [], text)
        if failed:
            self.say(f"could not edit {', '.join(failed)} - is it in edit mode?")
        self.refresh()
        if not failed:
            self.edited.emit()
        return not failed

    def _edited(self, item):
        if self._loading or item.column() not in (self.COL_X, self.COL_Y) or item.row() >= len(self.rows):
            return
        number, x, y = self.rows[item.row()]
        try:
            value = float(item.text().replace(",", "").replace(" ", ""))
        except ValueError:
            self.say(f"'{item.text()}' is not a number.")
            self.refresh()
            return
        g = self.geometry()
        if g is None:
            self.refresh()
            return
        point = g.vertexAt(number).clone()                      # keeps Z and M
        point.setX(value if item.column() == self.COL_X else x)
        point.setY(value if item.column() == self.COL_Y else y)
        if not g.moveVertex(point, number):
            self.say("that vertex cannot be moved there.")
            self.refresh()
            return
        self._write(g, "Move vertex")

    def delete_selected(self):
        """The red cross: delete the selected rows' vertices (the shape must stay valid: 2 points for a line,
        3 for a polygon ring)."""
        rows = self._selected_rows()
        g = self.geometry()
        if not rows or g is None:
            return False
        ok = True
        for row in sorted(rows, reverse=True):                  # from the end, so earlier numbers stay right
            if not g.deleteVertex(self.rows[row][0]) or not shape_ok(g):          # stop before the shape is gone
                ok = False
                break
        if not ok or not shape_ok(g) or len(vertex_rows(g)) != len(self.rows) - len(rows):
            self.say("those vertices cannot be deleted - a line needs 2 vertices and a polygon ring 3.")
            self.refresh()
            return False
        return self._write(g, "Delete vertices")

    def copy_table(self):
        lines = ["#\tX\tY"] + [f"{i + 1}\t{x:.{self.decimals}f}\t{y:.{self.decimals}f}"
                               for i, (_n, x, y) in enumerate(self.rows)]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.say(f"{len(self.rows)} vertices copied.")

    def dispose(self):
        self._disconnect()
        with suppress(Exception):
            self.canvas.scene().removeItem(self.marker)


class EditVerticesTool(TransformTool):
    KEY = "edit_vertices"
    LABEL = "Edit Vertices"
    HINT = ("click a feature to select it and drag its vertex handles; drag a box (Alt: lasso) to choose several "
            "vertices, then Delete; click empty map to clear; double-click a segment to add a vertex.")
    MARQUEE_PX = 4                          # a drag shorter than this is a click
    SHOW_HINT = False
    SHOW_STATUS = False
    AUTO_SELECT = False                     # clicking a feature selects it: no Select mode to start in
    HANDLE_PX = 8                           # how close the mouse must be to a handle (pixels)
    MAX_HANDLES = 4000                      # handles drawn / hit-tested for the selection

    def __init__(self, canvas, iface, select_tool=None):
        super().__init__(canvas, iface, select_tool)
        self.vertices = None                # the VertexTable of the page
        self.tool_tile = None
        self._shown = None                  # (layer id, feature id) whose vertices the table lists
        self._features = []                 # [(layer, fid, geometry in the map's CRS, vertex rows)] of the selection
        self._drag = None                   # [layer, fid, vertex number, geometry, moved] while a handle is dragged
        self._hover = None                  # (layer, fid, vertex number, x, y) under the mouse
        self._watched = []                  # layers whose geometry changes redraw the handles
        self._chosen = set()                # {(layer id, feature id, vertex number)} picked with a box, lasso or Shift-click
        self._marquee = None                # {"points": [...], "lasso": bool, "moved": bool} while a box is being dragged
        self.chosen_label = None
        self.outline = QgsRubberBand(canvas, Qgis.GeometryType.Line)           # Pro: a cyan line with black dashes on it
        self.outline.setColor(QColor(0, 230, 240))
        self.outline.setWidth(3)
        self.dashes = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self.dashes.setColor(QColor(0, 0, 0))
        self.dashes.setWidth(1)
        self.dashes.setLineStyle(Qt.PenStyle.DashLine)
        self.handles = self._handle_band(canvas, QColor(0, 110, 40), QColor(70, 190, 90))          # green squares ...
        self.end_handles = self._handle_band(canvas, QColor(150, 0, 0), QColor(235, 40, 40))        # ... red for the ends
        self.chosen_band = self._handle_band(canvas, QColor(170, 80, 0), QColor(255, 170, 40))     # chosen vertices: orange
        self.chosen_band.setIconSize(11)
        self.marquee_band = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self.marquee_band.setColor(QColor(0, 0, 0))
        self.marquee_band.setWidth(1)
        self.marquee_band.setLineStyle(Qt.PenStyle.DashLine)
        self.hover_marker = QgsVertexMarker(canvas)
        self.hover_marker.setIconType(MARKER_CIRCLE)
        self.hover_marker.setColor(QColor(220, 0, 0))
        self.hover_marker.setIconSize(15)
        self.hover_marker.setPenWidth(3)
        self.hover_marker.hide()

    # ------------------------------------------------------------------ page
    def build_page(self, box):
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.currentItemChanged.connect(self._tree_current)
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "edit_vertices.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Edit Vertices")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT)
        tile.clicked.connect(lambda _c=False: self.set_selecting(False))
        self.tool_tile = tile
        box.addWidget(tile)
        box.addWidget(section_label("On the map"))
        for name, how in (("Move a vertex", "drag its handle."),
                          ("Add a vertex", "double-click a segment."),
                          ("Select vertices", "drag a box on the map (Alt: freehand lasso). Shift adds, Ctrl removes."),
                          ("Delete vertices",
                           "press Delete to remove the chosen ones, or the one under the mouse; or select rows in the "
                           "table and press the red cross."),
                          ("Cancel a drag", "press Esc before releasing.")):
            note = hint_label("")
            note.setTextFormat(Qt.TextFormat.RichText)
            note.setText(f"&bull;&nbsp;<b>{name}</b> &rarr; {how}")
            box.addWidget(note)
        self.chosen_label = hint_label("")
        self.chosen_label.setStyleSheet("font-weight: bold; color: #aa5000;")
        self.chosen_label.hide()
        box.addWidget(self.chosen_label)

    def build_footer(self, box):
        self.vertices = VertexTable(self.canvas(), say=self.say)
        self.vertices.refreshed.connect(self._draw_handles)
        box.addWidget(self.vertices)

    def update_page(self):
        super().update_page()
        if self.tool_tile is not None and not sip.isdeleted(self.tool_tile):
            self.tool_tile.setChecked(not self._selecting)
        self._sync()

    # ------------------------------------------------------------------ what is selected, shown and watched
    def _read_features(self):
        features, count = [], 0
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        for layer, ids in editable_selection(self.canvas()):
            to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
                g = QgsGeometry(f.geometry())
                if g.isNull() or g.isEmpty():
                    continue
                if to_map is not None:
                    g.transform(to_map)
                rows = vertex_rows(g)
                count += len(rows)
                features.append((layer, f.id(), g, rows))
                if count >= self.MAX_HANDLES:
                    return features
        return features

    def _sync(self):
        """The selection or a geometry changed: re-read the features, redraw the handles, update the table."""
        self._features = self._read_features()
        valid = {(layer.id(), fid, number) for layer, fid, _g, rows in self._features for number, _x, _y in rows}
        self._chosen &= valid                                   # vertices of features that are no longer selected go
        self._watch([layer for layer, *_rest in self._features])
        self._draw_handles()
        self._set_hover(None)
        self._sync_table()

    def _watch(self, layers):
        unique = []
        for layer in layers:
            if layer not in unique:
                unique.append(layer)
        for layer in list(self._watched):
            if layer not in unique:
                with suppress(TypeError, RuntimeError):
                    layer.geometryChanged.disconnect(self._geometry_changed)
                self._watched.remove(layer)
        for layer in unique:
            if layer not in self._watched:
                layer.geometryChanged.connect(self._geometry_changed)
                self._watched.append(layer)

    def _geometry_changed(self, _fid, _geometry):
        if self._drag is None:
            self._features = self._read_features()
            self._draw_handles()

    def _sync_table(self):
        table = self.vertices
        if table is None or getattr(self, "tree", None) is None:
            return
        leaves = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                data = top.child(j).data(0, Qt.ItemDataRole.UserRole)
                if data:
                    leaves.append((tuple(data), top.child(j)))
        if not leaves:
            self._shown = None
            table.clear()
            return
        keys = [data for data, _item in leaves]
        if self._shown not in keys:
            self._shown = keys[0]                               # the first selected feature, until another is picked
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(leaves[keys.index(self._shown)][1])
        self.tree.blockSignals(False)
        table.load(project_layer(self._shown[0]), self._shown[1])

    def _tree_current(self, current, _previous):
        data = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        if data and self.vertices is not None:
            self._shown = tuple(data)
            self.vertices.load(project_layer(self._shown[0]), self._shown[1])

    def _show_feature(self, layer, fid, number):
        """A handle was grabbed: list that feature in the table and select the vertex's row."""
        self._shown = (layer.id(), fid)
        if self.vertices is None:
            return
        self.vertices.load(layer, fid)
        for row, (n, _x, _y) in enumerate(self.vertices.rows):
            if n == number:
                self.vertices.table.selectRow(row)
                break

    # ------------------------------------------------------------------ handles and hover
    @staticmethod
    def _handle_band(canvas, border, fill):
        band = QgsRubberBand(canvas, Qgis.GeometryType.Point)
        band.setColor(border)
        band.setFillColor(fill)
        band.setIconSize(9)
        band.setWidth(2)
        with suppress(AttributeError):
            band.setIcon(QgsRubberBand.IconType.ICON_BOX)
        return band

    def _draw_handles(self):
        for band, kind in ((self.outline, Qgis.GeometryType.Line), (self.dashes, Qgis.GeometryType.Line),
                           (self.handles, Qgis.GeometryType.Point), (self.end_handles, Qgis.GeometryType.Point),
                           (self.chosen_band, Qgis.GeometryType.Point)):
            band.reset(kind)
        outlines = []
        for _layer, _fid, g, _rows in self._features:
            if g.type() == Qgis.GeometryType.Polygon:
                g = g.convertToType(Qgis.GeometryType.Line, False)
            if g is not None and not g.isNull() and g.type() == Qgis.GeometryType.Line:
                outlines.append(g)
        for i, line in enumerate(outlines):
            last = i == len(outlines) - 1
            for band in (self.outline, self.dashes):
                band.addGeometry(line, QgsCoordinateReferenceSystem(), last)
        points, ends = [], []
        for _layer, _fid, g, rows in self._features:
            red = end_numbers(g)
            for number, x, y in rows:
                (ends if number in red else points).append(QgsPointXY(x, y))
        every = points + ends
        for i, point in enumerate(every):
            self.handles.addPoint(point, i == len(every) - 1)       # the last one refreshes the band's extent
        for i, point in enumerate(ends):
            self.end_handles.addPoint(point, i == len(ends) - 1)
        picked = [QgsPointXY(x, y) for layer, fid, _g, rows in self._features for number, x, y in rows
                  if (layer.id(), fid, number) in self._chosen]
        for i, point in enumerate(picked):
            self.chosen_band.addPoint(point, i == len(picked) - 1)
        self._announce()

    def _tolerance(self):
        return self.HANDLE_PX * self.canvas().mapUnitsPerPixel()

    def _vertex_at(self, point):
        """(layer, fid, vertex number, x, y) of the handle nearest `point` within a few pixels, else None."""
        best, tol2 = None, self._tolerance() ** 2
        for layer, fid, _g, rows in self._features:
            for number, x, y in rows:
                d2 = (x - point.x()) ** 2 + (y - point.y()) ** 2
                if d2 <= tol2 and (best is None or d2 < best[0]):
                    best = (d2, (layer, fid, number, x, y))
        return best[1] if best else None

    def _set_hover(self, hit):
        self._hover = hit
        if hit is None:
            self.hover_marker.hide()
            self.setCursor(QCursor(self.CURSOR))
        else:
            self.hover_marker.setCenter(QgsPointXY(hit[3], hit[4]))
            self.hover_marker.show()
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))

    # ------------------------------------------------------------------ writing
    def _write(self, layer, fid, g, text):
        failed = commit(self.canvas(), [(layer, fid, g)], [], text)
        if failed:
            self.say(f"could not edit {', '.join(failed)} - is it in edit mode?")
        self._sync()
        return not failed

    def _geometry_of(self, layer, fid):
        for lyr, f, g, _rows in self._features:
            if lyr is layer and f == fid:
                return QgsGeometry(g)
        return None

    # ------------------------------------------------------------------ mouse
    def canvasPressEvent(self, e):
        if self.forward_select("canvasPressEvent", e):
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        point = QgsPointXY(e.mapPoint())
        hit = self._vertex_at(point)
        if hit is not None:
            layer, fid, number, _x, _y = hit
            if self._adding(e):                                 # Shift / Ctrl on a handle: choose / drop that vertex
                self._toggle((layer.id(), fid, number))
                return
            g = self._geometry_of(layer, fid)
            if g is not None:
                self._drag = [layer, fid, number, g, False]
                self._show_feature(layer, fid, number)
                return
        if self._features:                                      # empty map with features selected: a vertex box / lasso
            modifiers = e.modifiers() if hasattr(e, "modifiers") else Qt.KeyboardModifier.NoModifier
            self._marquee = {"points": [point], "lasso": bool(modifiers & Qt.KeyboardModifier.AltModifier),
                             "moved": False}
            return
        self._start_select_gesture(e)                           # nothing selected yet: the select tool picks features

    def canvasMoveEvent(self, e):
        if self._gesture == "select":
            self.select_tool.canvasMoveEvent(e)
            return
        if self.forward_select("canvasMoveEvent", e):
            return
        if self._marquee is not None:
            self._grow_marquee(QgsPointXY(e.mapPoint()))
            return
        point, match = self._snap(e)
        self._show_snap(match)
        if self._drag is not None:
            self._drag_to(point)
        else:
            self._set_hover(self._vertex_at(QgsPointXY(e.mapPoint())))

    def _moved(self, g, number, point):
        moved = QgsGeometry(g)
        vertex = g.vertexAt(number).clone()                     # keeps Z and M
        vertex.setX(point.x())
        vertex.setY(point.y())
        moved.moveVertex(vertex, number)
        return moved

    def _drag_to(self, point):
        layer, _fid, number, g, _moved = self._drag
        self._drag[4] = True
        self._geoms = [(layer.geometryType(), self._moved(g, number, point))]
        self._draw()

    def canvasReleaseEvent(self, e):
        if self._gesture == "select":
            self._gesture = None
            self.select_tool.canvasReleaseEvent(e)
            self._items = None
            self.refresh_page()
            return
        if self.forward_select("canvasReleaseEvent", e):
            return
        if self._marquee is not None:
            self._finish_marquee(e)
            return
        drag, self._drag = self._drag, None
        if drag is None or e.button() != Qt.MouseButton.LeftButton:
            return
        layer, fid, number, g, moved = drag
        self._clear_preview()
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        point = self._snap(e)[0]
        old = g.vertexAt(number)
        if moved and (abs(point.x() - old.x()) > 1e-9 or abs(point.y() - old.y()) > 1e-9):
            self._write(layer, fid, self._moved(g, number, point), "Move vertex")
        else:
            self._sync()

    # ------------------------------------------------------------------ choosing several vertices
    def _pixel_distance(self, a, b):
        to_pixel = self.canvas().getCoordinateTransform()
        pa, pb = to_pixel.transform(a), to_pixel.transform(b)
        return ((pa.x() - pb.x()) ** 2 + (pa.y() - pb.y()) ** 2) ** 0.5

    @staticmethod
    def _marquee_shape(m, tail=None):
        """The box (a rectangle's outline) or the lasso drawn so far, as points of the map."""
        points = m["points"] + ([tail] if tail is not None else [])
        if m["lasso"] or len(points) < 2:
            return points
        a, b = points[0], points[-1]
        return [a, QgsPointXY(b.x(), a.y()), b, QgsPointXY(a.x(), b.y()), a]

    def _grow_marquee(self, point):
        m = self._marquee
        if not m["moved"] and self._pixel_distance(m["points"][0], point) < self.MARQUEE_PX:
            return
        m["moved"] = True
        if m["lasso"]:
            m["points"].append(point)
            shape = self._marquee_shape(m)
        else:
            shape = self._marquee_shape(m, point)
        self.marquee_band.reset(Qgis.GeometryType.Line)
        for i, p in enumerate(shape):
            self.marquee_band.addPoint(p, i == len(shape) - 1)

    def _finish_marquee(self, e):
        m, self._marquee = self._marquee, None
        self.marquee_band.reset(Qgis.GeometryType.Line)
        if not m["moved"]:                                      # a click: the select tool takes it (feature / clear)
            self.select_tool.canvasPressEvent(e)
            self._gesture = "select"
            self.canvasReleaseEvent(e)
            return
        shape = self._marquee_shape(m, QgsPointXY(e.mapPoint()))
        found = self._vertices_in(shape, m["lasso"])
        modifiers = e.modifiers() if hasattr(e, "modifiers") else Qt.KeyboardModifier.NoModifier
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            self._chosen -= found
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._chosen |= found
        else:
            self._chosen = set(found)
        self._chosen_changed()

    def _vertices_in(self, shape, lasso):
        """{(layer id, feature id, vertex number)} of the handles inside the box or the lasso."""
        if len(shape) < 3:
            return set()
        if lasso:
            region = QgsGeometry.fromPolygonXY([shape + [shape[0]]])

            def inside(x, y):
                return region.contains(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        else:
            xs, ys = [p.x() for p in shape], [p.y() for p in shape]
            box = QgsRectangle(min(xs), min(ys), max(xs), max(ys))

            def inside(x, y):
                return box.contains(QgsPointXY(x, y))
        return {(layer.id(), fid, number) for layer, fid, _g, rows in self._features for number, x, y in rows
                if inside(x, y)}

    def _toggle(self, key):
        self._chosen ^= {key}
        self._chosen_changed()

    def _chosen_changed(self):
        self._draw_handles()
        table = self.vertices
        if table is not None and self._shown is not None and table.layer is not None:       # the table follows the map
            numbers = {n for lid, fid, n in self._chosen if (lid, fid) == self._shown}
            table.table.clearSelection()
            model, selection = table.table.model(), table.table.selectionModel()
            for row, (number, _x, _y) in enumerate(table.rows):
                if number in numbers:                           # selectRow would drop the rows chosen before it
                    selection.select(model.index(row, 0), QItemSelectionModel.SelectionFlag.Select
                                     | QItemSelectionModel.SelectionFlag.Rows)

    def _announce(self):
        label = self.chosen_label
        if label is None or sip.isdeleted(label):
            return
        n = len(self._chosen)
        label.setVisible(n > 0)
        if n:
            label.setText(f"{n} vertex chosen - press Delete to remove it." if n == 1
                          else f"{n} vertices chosen - press Delete to remove them.")

    def clear_chosen(self):
        if self._chosen:
            self._chosen = set()
            self._chosen_changed()

    def delete_chosen(self):
        """Delete every chosen vertex: one edit command per layer, and a feature that would be left too small (a line
        under 2 vertices, a polygon ring under 3) is skipped whole. Returns how many vertices went."""
        by_feature = {}
        for layer_id, fid, number in self._chosen:
            by_feature.setdefault((layer_id, fid), []).append(number)
        changes, deleted, skipped = [], 0, 0
        for (layer_id, fid), numbers in by_feature.items():
            layer = project_layer(layer_id)
            g = self._geometry_of(layer, fid) if layer is not None else None
            if g is None:
                continue
            before = len(vertex_rows(g))
            ok = True
            for n in sorted(numbers, reverse=True):                                       # from the end: numbers stay right
                if not g.deleteVertex(n) or not shape_ok(g):                              # stop before the shape is gone
                    ok = False
                    break
            if not ok or len(vertex_rows(g)) != before - len(numbers):
                skipped += len(numbers)
                continue
            changes.append((layer, fid, g))
            deleted += len(numbers)
        failed = commit(self.canvas(), changes, [], "Delete vertices") if changes else []
        self._chosen = set()
        self._sync()
        self._announce()
        if failed:
            self.say(f"could not edit {', '.join(failed)} - is it in edit mode?")
            return 0
        text = f"{deleted} vertices deleted. (Ctrl+Z undoes it)" if deleted else "Nothing was deleted."
        if skipped:
            text += f" {skipped} vertices were left because a line needs 2 vertices and a polygon ring 3."
        self.say(text)
        return deleted

    def canvasDoubleClickEvent(self, e):
        """Double-click on a segment of a selected feature: add a vertex there."""
        if self._selecting or e.button() != Qt.MouseButton.LeftButton:
            return
        point = QgsPointXY(e.mapPoint())
        if self._vertex_at(point) is not None:
            return                                              # on a handle: that is not a segment
        tol2 = self._tolerance() ** 2
        best = None
        for layer, fid, g, _rows in self._features:
            sqr, near, after, _side = g.closestSegmentWithContext(point)
            if after >= 0 and sqr <= tol2 and (best is None or sqr < best[0]):
                best = (sqr, layer, fid, g, near, after)
        if best is None:
            return
        _sqr, layer, fid, g, near, after = best
        added = QgsGeometry(g)
        if not added.insertVertex(near.x(), near.y(), after):
            self.say("a vertex cannot be added there.")
            return
        self._write(layer, fid, added, "Add vertex")

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):
            return
        if e.key() == Qt.Key.Key_Escape and self._marquee is not None:
            self._marquee = None
            self.marquee_band.reset(Qgis.GeometryType.Line)
            e.ignore()
        elif e.key() == Qt.Key.Key_Escape and self._drag is not None:
            self._drag = None
            self._clear_preview()
            self._sync()
            e.ignore()
        elif e.key() == Qt.Key.Key_Escape and self._chosen:
            self.clear_chosen()
            e.ignore()
        elif e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._chosen:
                self.delete_chosen()
            elif self._hover is not None:
                self.delete_vertex(self._hover)
            elif self.vertices is not None:
                self.vertices.delete_selected()
            e.ignore()                                          # never QGIS's own Delete: that removes whole features

    def delete_vertex(self, hit):
        layer, fid, number, _x, _y = hit
        g = self._geometry_of(layer, fid)
        if g is None:
            return False
        before = len(vertex_rows(g))
        if not g.deleteVertex(number) or not shape_ok(g) or len(vertex_rows(g)) != before - 1:
            self.say("that vertex cannot be deleted - a line needs 2 vertices and a polygon ring 3.")
            return False
        return self._write(layer, fid, g, "Delete vertex")

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        super().activate()
        self._sync()

    def deactivate(self):
        self._drag = None
        self._marquee = None
        self._chosen = set()
        self.marquee_band.reset(Qgis.GeometryType.Line)
        for layer in list(self._watched):
            with suppress(TypeError, RuntimeError):
                layer.geometryChanged.disconnect(self._geometry_changed)
        self._watched = []
        self._features = []
        for band, kind in ((self.outline, Qgis.GeometryType.Line), (self.dashes, Qgis.GeometryType.Line),
                           (self.handles, Qgis.GeometryType.Point), (self.end_handles, Qgis.GeometryType.Point),
                           (self.chosen_band, Qgis.GeometryType.Point)):
            band.reset(kind)
        self._announce()
        self.hover_marker.hide()
        if self.vertices is not None:
            self.vertices.clear()                  # also lets go of the layer, so a later edit cannot bring the marker back
        super().deactivate()

    def dispose(self):
        self.deactivate()
        for item in (self.outline, self.dashes, self.handles, self.end_handles, self.chosen_band, self.marquee_band,
                     self.hover_marker):
            with suppress(Exception):
                self.canvas().scene().removeItem(item)
        if self.vertices is not None:
            self.vertices.dispose()
            self.vertices = None
        super().dispose()


TOOL_CLASSES = {EditVerticesTool.KEY: EditVerticesTool}

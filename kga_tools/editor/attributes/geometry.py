"""
Geometry tab of the Attributes pane: the vertices of the selected feature, editable.

Parts and rings are folders; each vertex row shows X, Y (and Z, M when the geometry has them) in the
layer's CRS. Edit a coordinate in place and only that vertex moves. Clicking a row marks the vertex
on the map. Like attribute values, edits follow Auto Apply and go into the layer's edit buffer.
"""

from contextlib import suppress

from qgis.core import (
    QgsApplication,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPoint,
    QgsProject,
    QgsUnitTypes,
    QgsWkbTypes,
)
from qgis.gui import QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..selection import GEOM_LINE, GEOM_POLYGON
from .fields import ensure_editing

MAX_VERTICES = 5000
ROLE_VERTEX = Qt.ItemDataRole.UserRole          # global vertex number; None on part / ring folders
COL_X, COL_Y, COL_Z, COL_M = 1, 2, 3, 4


class GeometryWidget(QWidget):

    pendingChanged = pyqtSignal(bool)
    editingStarted = pyqtSignal(object)
    problem = pyqtSignal(str)
    status = pyqtSignal(str)
    before_edit = None               # callable(layer), run just before an edit is written

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.layer = None
        self.fid = None
        self._geometry = None              # working copy, includes pending moves
        self._pending = {}                 # vertex number -> QgsPoint
        self._marker = None
        self._building = False
        self._decimals = 3

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.summary = QLabel(self)
        self.summary.setObjectName("KgaAttrHint")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.tree = QTreeWidget(self)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(14)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.SelectedClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(self._current_changed)
        lay.addWidget(self.tree, 1)

        # Copy button at the right end of the header row
        self.copy_button = QToolButton(self.tree.header())
        self.copy_button.setObjectName("KgaAttrCopy")
        self.copy_button.setIcon(QgsApplication.getThemeIcon("/mActionEditCopy.svg"))
        self.copy_button.setAutoRaise(True)
        self.copy_button.setToolTip("Copy the coordinates of the selected vertex (all vertices if none is selected)")
        self.copy_button.setFixedSize(24, 22)
        self.copy_button.clicked.connect(self.copy_coordinates)
        self.tree.header().installEventFilter(self)
        self._place_copy_button()
        self.copy_button.setEnabled(False)

    def eventFilter(self, obj, event):
        if obj is self.tree.header() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._place_copy_button()
        return super().eventFilter(obj, event)

    def _place_copy_button(self):
        header = self.tree.header()
        self.copy_button.move(max(0, header.width() - self.copy_button.width() - 3),
                              max(0, (header.height() - self.copy_button.height()) // 2))
        self.copy_button.raise_()

    def _vertex_items(self):
        out = []
        stack = [self.tree.invisibleRootItem()]
        while stack:
            node = stack.pop(0)
            for k in range(node.childCount()):
                child = node.child(k)
                (out if child.data(0, ROLE_VERTEX) is not None else stack).append(child)
        return out

    def _row_text(self, item):
        """One tab-separated row: Vertex, X, Y (and Z, M when the geometry has them)."""
        return chr(9).join(item.text(c) for c in range(self.tree.columnCount()))

    def copy_coordinates(self):
        """Copy as tab-separated columns (Vertex, X, Y ...) with a header row, so it pastes into a
        spreadsheet as columns. The selected vertex, or every vertex when none is selected."""
        header = self.tree.headerItem()
        head = chr(9).join(header.text(c) for c in range(self.tree.columnCount()))
        current = self.tree.currentItem()
        if current is not None and current.data(0, ROLE_VERTEX) is not None:
            rows = [self._row_text(current)]
            message = "Copied vertex {}".format(current.text(0))
        else:
            items = self._vertex_items()
            if not items:
                return ""
            rows = [self._row_text(i) for i in items]
            message = "Copied {} vertices (Vertex, X, Y)".format(len(items))
        text = chr(10).join([head] + rows)
        QApplication.clipboard().setText(text)
        self.status.emit(message)
        return text

    # ------------------------------------------------------------------ target
    def set_target(self, layer, fids, build=True):
        """Show the geometry of one feature. With build=False the page only remembers the feature
        and fills its table when `ensure_built()` is called (the tab is opened)."""
        self._pending.clear()
        self.clear_marker()
        self.layer = layer
        self.copy_button.setEnabled(False)
        self.fid = fids[0] if len(fids) == 1 else None
        self._geometry = None
        self._unbuilt = False
        if not build and layer is not None and self.fid is not None and layer.isSpatial():
            self._building = True
            self.tree.clear()
            self._building = False
            self.summary.setText("")
            self._unbuilt = True
            return
        self._building = True
        self.tree.clear()
        self._building = False
        if layer is None:
            self.summary.setText("")
            return
        if not layer.isSpatial():
            self.summary.setText("This layer has no geometry.")
            return
        if self.fid is None:
            self.summary.setText("Select one feature (click it in the list above) to see its geometry.")
            return
        feature = next(layer.getFeatures(QgsFeatureRequest(self.fid)), None)
        if feature is None or feature.geometry().isNull():
            self.summary.setText("This feature has no geometry.")
            return
        self._geometry = QgsGeometry(feature.geometry())
        self._decimals = 8 if layer.crs().isGeographic() else 3
        self._rebuild()

    def ensure_built(self):
        """Fill the table now if `set_target(..., build=False)` left it for later."""
        if getattr(self, '_unbuilt', False) and self.layer is not None and not sip.isdeleted(self.layer):
            self.set_target(self.layer, [self.fid])

    def refresh_from_layer(self):
        """The feature changed underneath us (undo, other tool): read it again."""
        if self.layer is not None and self.fid is not None and not sip.isdeleted(self.layer):
            self.set_target(self.layer, [self.fid], build=not getattr(self, '_unbuilt', False))

    # ------------------------------------------------------------------ tree
    def _rebuild(self):
        geom = self._geometry
        self._building = True
        self.tree.clear()
        abstract = geom.constGet()
        has_z, has_m = abstract.is3D(), abstract.isMeasure()
        headers = ["Vertex", "X", "Y"] + (["Z"] if has_z else []) + (["M"] if has_m else [])
        self.tree.setColumnCount(len(headers))
        self.tree.setHeaderLabels(headers)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 90)
        self._has_z, self._has_m = has_z, has_m
        groups = {}
        total = 0
        multi_part = geom.isMultipart() and geom.constGet().partCount() > 1
        has_rings = geom.type() == GEOM_POLYGON
        for n, vertex in enumerate(geom.vertices()):
            if n >= MAX_VERTICES:
                break
            total += 1
            ok, vid = geom.vertexIdFromVertexNr(n)
            parent = self.tree.invisibleRootItem()
            if multi_part:
                key = ("part", vid.part)
                if key not in groups:
                    groups[key] = self._folder(parent, "Part {}".format(vid.part + 1))
                parent = groups[key]
            if has_rings:
                key = ("ring", vid.part, vid.ring)
                if key not in groups:
                    groups[key] = self._folder(parent, "Exterior ring" if vid.ring == 0
                                               else "Interior ring {}".format(vid.ring))
                parent = groups[key]
            item = QTreeWidgetItem(parent, ["{}".format(vid.vertex + 1)])
            item.setData(0, ROLE_VERTEX, n)
            self._fill_vertex(item, vertex)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.tree.expandAll()
        self._building = False
        self._summarize(geom, total)
        self.copy_button.setEnabled(total > 0)

    @staticmethod
    def _folder(parent, text):
        item = QTreeWidgetItem(parent, [text])
        item.setData(0, ROLE_VERTEX, None)
        item.setFirstColumnSpanned(True)
        return item

    def _fill_vertex(self, item, point):
        d = self._decimals
        item.setText(COL_X, "{:.{d}f}".format(point.x(), d=d))
        item.setText(COL_Y, "{:.{d}f}".format(point.y(), d=d))
        col = COL_Z
        if self._has_z:
            item.setText(col, "" if point.z() != point.z() else "{:.{d}f}".format(point.z(), d=d))
            col += 1
        if self._has_m:
            item.setText(col, "" if point.m() != point.m() else "{:.{d}f}".format(point.m(), d=d))

    def _summarize(self, geom, shown):
        kind = QgsWkbTypes.displayString(geom.wkbType())
        count = geom.constGet().nCoordinates()
        text = "{}, {} vertices".format(kind, count)
        with suppress(Exception):
            da = QgsDistanceArea()
            da.setSourceCrs(self.layer.crs(), QgsProject.instance().transformContext())
            da.setEllipsoid(QgsProject.instance().ellipsoid())
            if geom.type() == GEOM_LINE:
                text += ", length {:,.2f} {}".format(
                    da.measureLength(geom), QgsUnitTypes.toAbbreviatedString(da.lengthUnits()))
            elif geom.type() == GEOM_POLYGON:
                text += ", length {:,.2f} {}, area {:,.2f} {}".format(
                    da.measurePerimeter(geom), QgsUnitTypes.toAbbreviatedString(da.lengthUnits()),
                    da.measureArea(geom), QgsUnitTypes.toAbbreviatedString(da.areaUnits()))
        if count > shown:
            text += ". Showing the first {}.".format(shown)
        self.summary.setText(text)

    # ------------------------------------------------------------------ editing
    def _item_changed(self, item, column):
        if self._building or column == 0:
            return
        n = item.data(0, ROLE_VERTEX)
        if n is None or self._geometry is None:
            return
        old = self._geometry.vertexAt(n)
        try:
            x = float(item.text(COL_X))
            y = float(item.text(COL_Y))
            z, m = old.z(), old.m()
            col = COL_Z
            if self._has_z:
                text = item.text(col).strip()
                z = float(text) if text else float("nan")
                col += 1
            if self._has_m:
                text = item.text(col).strip()
                m = float(text) if text else float("nan")
        except ValueError:
            self.problem.emit("Enter a number for the coordinate.")
            self._restore(item, old)
            return
        point = QgsPoint(x, y)
        if self._has_z:
            point.addZValue(z if z == z else 0.0)
        if self._has_m:
            point.addMValue(m if m == m else 0.0)
        if (x, y) == (old.x(), old.y()) and (not self._has_z or z == old.z()) and (not self._has_m or m == old.m()):
            return
        if not self._geometry.moveVertex(point, n):
            self.problem.emit("That vertex could not be moved.")
            self._restore(item, old)
            return
        if self.panel.model.auto_apply:
            if not self._write(self._geometry):
                self._geometry = QgsGeometry(self._geometry)
                self.refresh_from_layer()
                return
            self._rebuild_keep(n)
        else:
            self._pending[n] = point
            self.pendingChanged.emit(True)
            self._rebuild_keep(n)
        self.show_marker(n)

    def _restore(self, item, point):
        self._building = True
        self._fill_vertex(item, point)
        self._building = False

    def _rebuild_keep(self, vertex_nr):
        self._rebuild()
        self._select_vertex(vertex_nr)

    def _write(self, geometry):
        layer = self.layer
        if self.before_edit is not None:
            self.before_edit(layer)
        was = layer.isEditable()
        if not ensure_editing(layer):
            self.problem.emit("This layer cannot be edited.")
            return False
        if not was:
            self.editingStarted.emit(layer)
        layer.beginEditCommand("Edit vertices")
        ok = layer.changeGeometry(self.fid, geometry)
        if ok:
            layer.endEditCommand()
            layer.triggerRepaint()
        else:
            layer.destroyEditCommand()
            self.problem.emit("The layer did not accept that geometry.")
        return ok

    def has_pending(self):
        return bool(self._pending)

    def apply_pending(self):
        if not self._pending or self._geometry is None:
            return True
        ok = self._write(self._geometry)
        if ok:
            self._pending.clear()
            self.pendingChanged.emit(False)
        return ok

    def cancel_pending(self):
        if self._pending:
            self._pending.clear()
            self.pendingChanged.emit(False)
            self.refresh_from_layer()

    # ------------------------------------------------------------------ map marker
    def _select_vertex(self, n):
        it = self.tree.invisibleRootItem()
        stack = [it]
        while stack:
            node = stack.pop()
            for k in range(node.childCount()):
                child = node.child(k)
                if child.data(0, ROLE_VERTEX) == n:
                    self.tree.setCurrentItem(child)
                    return
                stack.append(child)

    def _current_changed(self, current, _previous):
        if self._building or current is None:
            return
        n = current.data(0, ROLE_VERTEX)
        if n is None:
            self.clear_marker()
        else:
            self.show_marker(n)

    def show_marker(self, n):
        self.clear_marker()
        if self._geometry is None or self.layer is None or not self.panel.isVisible():
            return
        point = self._geometry.vertexAt(n)
        canvas = self.panel.canvas
        with suppress(Exception):
            ct = QgsCoordinateTransform(self.layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
            mapped = ct.transform(point.x(), point.y())
            marker = QgsVertexMarker(canvas)
            marker.setCenter(mapped)
            marker.setIconType(QgsVertexMarker.IconType.ICON_BOX)
            marker.setColor(QColor(255, 140, 0))
            marker.setIconSize(12)
            marker.setPenWidth(3)
            self._marker = marker

    def clear_marker(self):
        if self._marker is not None:
            with suppress(Exception):
                self.panel.canvas.scene().removeItem(self._marker)
            self._marker = None

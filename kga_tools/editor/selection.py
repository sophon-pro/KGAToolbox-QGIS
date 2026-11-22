"""
Multi-layer selection and editing of the selection.

* SelectTool  - click or drag on the map; selects from EVERY visible, selectable layer
                (no need to choose a layer in the Layers panel).
                Shift = add, Ctrl = remove, Shift+Ctrl = select from the current selection,
                N = next overlapping feature, Delete/Backspace = delete selected features,
                Esc = cancel the rectangle.
* delete_selected() - delete the selected features of every editable layer.

All edits go into the normal QGIS edit buffer: Save Edits / Discard Edits, and Ctrl+Z per layer.
"""

from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor

from .overlap_switcher import OverlapSwitcher

CLICK_TOLERANCE_PX = 6        # how close a click must be to a point or line (pixels)
DRAG_THRESHOLD_PX = 4         # mouse movement that turns a click into a rectangle

GEOM_POINT = Qgis.GeometryType.Point
GEOM_LINE = Qgis.GeometryType.Line
GEOM_POLYGON = Qgis.GeometryType.Polygon
SELECT_SET = Qgis.SelectBehavior.SetSelection
SELECT_ADD = Qgis.SelectBehavior.AddToSelection
SELECT_REMOVE = Qgis.SelectBehavior.RemoveFromSelection
SELECT_INTERSECT = Qgis.SelectBehavior.IntersectSelection


def _transform(src, dst):
    return QgsCoordinateTransform(src, dst, QgsProject.instance())


def _status(iface, text, ms=6000):
    iface.mainWindow().statusBar().showMessage(text, ms)


def behavior_from_modifiers(modifiers):
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
    if shift and ctrl:
        return SELECT_INTERSECT
    if shift:
        return SELECT_ADD
    if ctrl:
        return SELECT_REMOVE
    return SELECT_SET


def all_vector_layers():
    return [lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and not sip.isdeleted(lyr)]


def map_layers(canvas):
    """Layers shown on the map right now (visible in the Layers panel)."""
    try:
        return set(lyr.id() for lyr in canvas.layers(True) if lyr is not None)    # True: layers inside group layers too
    except TypeError:
        return set(lyr.id() for lyr in canvas.layers() if lyr is not None)
    except Exception:
        return None


def selection_summary():
    layers = [lyr for lyr in all_vector_layers() if lyr.selectedFeatureCount()]
    n = sum(lyr.selectedFeatureCount() for lyr in layers)
    return n, layers


def delete_selected(iface):
    """
    Delete the selected features of every layer that is shown on the map and in edit mode.
    Selections in hidden layers (made in an attribute table, for example) are left alone, so
    nothing you cannot see is ever deleted. Returns (deleted, layers, skipped).
    """
    deleted, layers, skipped = 0, 0, []
    shown = map_layers(iface.mapCanvas())
    hidden = []
    for layer in all_vector_layers():
        ids = layer.selectedFeatureIds()
        if not ids:
            continue
        if shown is not None and layer.id() not in shown:
            if layer.isSpatial():
                hidden.append(layer.name())                # a table without geometry is simply skipped
            continue
        if not layer.isEditable():
            skipped.append(layer.name())
            continue
        layer.beginEditCommand("Delete selected features")
        if layer.deleteFeatures(ids):
            layer.endEditCommand()
            deleted += len(ids)
            layers += 1
        else:
            layer.destroyEditCommand()
            skipped.append(layer.name())
        layer.triggerRepaint()
    if deleted or skipped or hidden:
        text = f"Deleted {deleted} feature(s) from {layers} layer(s)." if deleted else "Nothing deleted."
        if skipped:
            text += f" Not editable, kept: {', '.join(skipped)}."
        if hidden:
            text += f" Hidden layers, kept: {', '.join(hidden)}."
        _status(iface, "Edit Session: " + text + " (Discard Edits or Ctrl+Z to undo)", 8000)
    return deleted, layers, skipped


# ---------------------------------------------------------------------------------------- select
class SelectTool(QgsMapTool):

    def __init__(self, canvas, manager, iface):
        super().__init__(canvas)
        self.manager = manager
        self.iface = iface
        self._press_px = None
        self._press_map = None
        self._dragging = False
        self._candidates = []          # [(layer, fid)] under the last click, best first
        self._cand_index = 0
        self.band = QgsRubberBand(canvas, GEOM_POLYGON)
        self.band.setColor(QColor(255, 140, 0, 220))
        self.band.setFillColor(QColor(255, 140, 0, 40))
        self.band.setWidth(1)
        self.switcher = OverlapSwitcher(canvas, self)      # Pro-style "switch selection" button
        try:
            self.setCursor(QgsApplication.getThemeCursor(QgsApplication.Cursor.Select))
        except Exception:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def right_click_menu_allowed(self):
        return True

    kga_right_drag_zoom = True      # Smooth Navigation: hold right button + drag up/down zooms, as in Pan

    def flags(self):
        """Right-click gives QGIS's map menu (Copy Coordinate, Select Feature ...) plus the KGA entries
        (Attribute, Filter Selected Features), like the Pan tool does."""
        flags_type = getattr(QgsMapTool, "Flags", None)         # Qt5 QFlags; Qt6 uses the enum itself
        flag = QgsMapTool.Flag.ShowContextMenu if self.right_click_menu_allowed() else QgsMapTool.Flag(0)
        return flags_type(flag) if flags_type is not None else flag

    def dispose(self):
        self.switcher.dispose()
        self.band.reset(GEOM_POLYGON)
        self.canvas().scene().removeItem(self.band)

    def deactivate(self):
        self._cancel_drag()
        self.switcher.hide()
        super().deactivate()

    # ---------------------------------------------------------------- mouse
    def canvasPressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_px = e.pixelPoint()
            self._press_map = QgsPointXY(e.mapPoint())
            self._dragging = False
            self.switcher.hide()

    def canvasMoveEvent(self, e):
        if self._press_px is None:
            return
        d = e.pixelPoint() - self._press_px
        if not self._dragging and abs(d.x()) + abs(d.y()) > DRAG_THRESHOLD_PX:
            self._dragging = True
        if self._dragging:
            self.band.reset(GEOM_POLYGON)
            self.band.addGeometry(QgsGeometry.fromRect(QgsRectangle(self._press_map, e.mapPoint())),
                                  QgsCoordinateReferenceSystem())

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self._press_px is None:
            return
        behavior = behavior_from_modifiers(e.modifiers())
        try:
            if self._dragging:
                rect = QgsRectangle(self._press_map, e.mapPoint())
                self._candidates = []
                self.select_rectangle(rect, behavior)
            else:
                self.select_click(e.mapPoint(), behavior, e.pixelPoint())
        finally:
            self._cancel_drag()

    def keyPressEvent(self, e):
        # QGIS convention: e.ignore() = "this tool handled the key"
        key = e.key()
        if key == Qt.Key.Key_N and self._candidates:
            self.next_candidate()
            e.ignore()
        elif key == Qt.Key.Key_Escape and self._dragging:
            self._cancel_drag()
            e.ignore()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.manager.is_running():
            # Edit Session On only: with it Off, QGIS's own Delete Selected (active layer) applies
            delete_selected(self.iface)        # all editable layers, not only the active one
            self._candidates = []
            self.switcher.hide()
            e.ignore()                         # stops QGIS's own Delete Selected (active layer only)

    def _cancel_drag(self):
        self._press_px = None
        self._press_map = None
        self._dragging = False
        self.band.reset(GEOM_POLYGON)

    # ---------------------------------------------------------------- selecting
    def _layers(self):
        return self.manager.selectable_layers(self.canvas())

    def hits_at(self, map_point):
        """[(layer, fid)] under a click: top layer first, nearest feature first within a layer."""
        canvas = self.canvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        tol = CLICK_TOLERANCE_PX * canvas.mapUnitsPerPixel()
        p = QgsPointXY(map_point)
        rect = QgsRectangle(p.x() - tol, p.y() - tol, p.x() + tol, p.y() + tol)
        hits = []
        for layer in self._layers():
            with suppress(Exception):
                ct = _transform(canvas_crs, layer.crs())
                lrect = ct.transformBoundingBox(rect)
                lpt = QgsGeometry.fromPointXY(ct.transform(p))
                ltol = max(lrect.width(), lrect.height()) / 2.0
                request = QgsFeatureRequest().setFilterRect(lrect).setNoAttributes()
                found = []
                for f in layer.getFeatures(request):
                    g = f.geometry()
                    if g.isEmpty():
                        continue
                    d = g.distance(lpt)          # 0 inside a polygon
                    if 0 <= d <= ltol:
                        found.append((d, f.id()))
                hits.extend((layer, fid) for _d, fid in sorted(found))
        return hits

    def features_in_rectangle(self, rect):
        """{layer: [fid]} of features that intersect a rectangle (canvas CRS)."""
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        result = {}
        for layer in self._layers():
            with suppress(Exception):
                poly = QgsGeometry.fromRect(rect)
                poly.transform(_transform(canvas_crs, layer.crs()))
                request = QgsFeatureRequest().setFilterRect(poly.boundingBox()).setNoAttributes()
                engine = QgsGeometry.createGeometryEngine(poly.constGet())
                engine.prepareGeometry()
                ids = [f.id() for f in layer.getFeatures(request)
                       if not f.geometry().isEmpty() and engine.intersects(f.geometry().constGet())]
                result[layer] = ids
        return result

    def _hold_map(self, layers):
        """Freeze the canvas for a selection change that cannot alter the map image, and say whether it
        was frozen. With the Pro-style selection the map ignores what is selected (the outline is an
        overlay), so the full redraw QGIS starts after every selection change - 200-300 ms of the GUI
        thread per click in a big project - shows nothing new. A layer whose style asks what is selected
        is redrawn as usual."""
        from ..gui import pro_selection
        canvas = self.canvas()
        with suppress(Exception):
            if (pro_selection.map_ignores_selection(canvas) and not canvas.isFrozen()
                    and not any(pro_selection.style_uses_selection(layer) for layer in layers)):
                canvas.freeze(True)
                return True
        return False

    def _release_map(self):
        from ..gui import pro_selection
        canvas = self.canvas()
        canvas.freeze(False)
        if not pro_selection.map_ignores_selection(canvas):
            canvas.refresh()            # the change itself switched QGIS's selection paint back on (a huge selection)

    def apply(self, per_layer, behavior):
        """Apply {layer: [fid]} to the selection of all selectable layers."""
        layers = self._layers()
        if behavior == SELECT_SET:
            changing = [lyr for lyr in all_vector_layers() if lyr.selectedFeatureCount()]
            changing += [lyr for lyr, ids in per_layer.items() if ids and lyr not in changing]
        elif behavior == SELECT_INTERSECT:
            changing = layers
        else:
            changing = [lyr for lyr, ids in per_layer.items() if ids]
        held = self._hold_map(changing)
        try:
            if behavior == SELECT_SET:
                for layer in all_vector_layers():          # a new selection replaces the old one everywhere
                    if layer.selectedFeatureCount():
                        layer.removeSelection()
                for layer, ids in per_layer.items():
                    if ids:
                        layer.selectByIds(ids, SELECT_ADD)
            elif behavior == SELECT_INTERSECT:
                for layer in layers:
                    layer.selectByIds(per_layer.get(layer, []), SELECT_INTERSECT)
            else:
                for layer, ids in per_layer.items():
                    if ids:
                        layer.selectByIds(ids, behavior)
        finally:
            if held:
                self._release_map()

    def select_click(self, map_point, behavior, pixel=None):
        hits = self.hits_at(map_point)
        self._candidates, self._cand_index = hits, 0
        self.switcher.hide()
        if not hits:
            if behavior == SELECT_SET:
                self.apply({}, SELECT_SET)            # clicking empty space clears the selection
            self._report()
            return
        layer, fid = hits[0]
        self.apply({layer: [fid]}, behavior)
        self._follow(layer)
        self._report(len(hits))
        if behavior == SELECT_SET and len(hits) > 1:        # something lies underneath: offer the switch
            if pixel is None:
                pixel = self.toCanvasCoordinates(QgsPointXY(map_point))
            self.switcher.show_for(hits, 0, pixel)

    def select_rectangle(self, rect, behavior):
        self.switcher.hide()
        per_layer = self.features_in_rectangle(rect)
        self.apply(per_layer, behavior)
        top = next((lyr for lyr in self._layers() if lyr.selectedFeatureCount()), None)
        if top is not None:
            self._follow(top)
        self._report()

    def next_candidate(self):
        """N key: replace the clicked feature with the next one under the same spot."""
        if len(self._candidates) < 2:
            return
        self.goto_candidate((self._cand_index + 1) % len(self._candidates))

    def goto_candidate(self, index):
        """Switch the selection to candidate `index` (N key and the overlap button's list)."""
        if not 0 <= index < len(self._candidates):
            return
        self.switcher._busy = True              # our own selection changes must not dismiss the button
        held = self._hold_map([lyr for lyr, _fid in (self._candidates[self._cand_index], self._candidates[index])
                               if not sip.isdeleted(lyr)])
        try:
            layer, fid = self._candidates[self._cand_index]
            if not sip.isdeleted(layer):
                layer.deselect(fid)
            self._cand_index = index
            layer, fid = self._candidates[index]
            layer.select(fid)
        finally:
            self.switcher._busy = False
            if held:
                self._release_map()
        self.switcher.set_current(index)
        self._follow(layer)
        self._report(len(self._candidates))

    def _follow(self, layer):
        """Make the layer of the selected feature the active layer, so QGIS tools work on it."""
        from .manager import option
        if option("active_follows_selection") and self.iface.activeLayer() is not layer:
            self.iface.setActiveLayer(layer)

    def _report(self, overlapping=0):
        n, layers = selection_summary()
        text = f"Selected: {n} feature(s) in {len(layers)} layer(s)."
        if overlapping > 1:
            pos = self._cand_index + 1
            text += f"  {overlapping} features here ({pos}/{overlapping}) - press N for the next one."
        text += "  Shift = add, Ctrl = remove, Delete = delete."
        _status(self.iface, text, 10000)

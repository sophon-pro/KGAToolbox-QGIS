"""
"Editing tools use the layer under the mouse".

QGIS's own editing tools (Reshape, Rotate, Scale, Move Feature, Add/Delete Ring or Part,
Offset Curve, Simplify, Reverse Line, ...) always work
on the ACTIVE layer: they ask the map canvas for its current layer at the moment of the click.

This helper watches mouse presses on the map. When a QGIS editing tool is active and you press on
a feature of another editable layer, that layer is made active just BEFORE the tool sees the
click - so every QGIS editing tool works on whatever layer you click, without choosing it in the
Layers panel first.

Which layer: only layers the tool can use (Reverse Line -> lines, Add Ring -> polygons ...) and
that QGIS accepts for it. A layer whose SELECTED feature is under the mouse wins; otherwise the
top-most one. Trim/Extend chooses its feature while the mouse moves, so for that tool the layer
under the mouse is followed while hovering.

Safety: it only switches at the START of an operation. Tools that need two clicks (pick, then
drop - e.g. Move Feature, Rotate, Offset Curve) would otherwise edit the wrong layer, so after an
operation has started no switching happens until the edit is finished (QGIS reports the edit) or
cancelled (Esc / right-click / another tool). Tools that create new features (Add Feature,
shape digitizing, Create Features) are never affected: there you choose the target layer.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer
from qgis.gui import QgsMapTool, QgsMapToolCapture, QgsMapToolDigitizeFeature
from qgis.PyQt.QtCore import QEvent, QObject, Qt

from .advanced import ANY, PG, SPECS, spec_for_action

# Only QGIS's OWN editing tools are followed (never tools of other plugins).
# (Vertex Tool "Current Layer" is left alone: its purpose IS the current layer - use Vertex (all layers).)
QGIS_TOOLS = set(SPECS)
# Tools you use by clicking inside a polygon's hole
RING_TOOLS = {"mActionDeleteRing", "mActionFillRing"}
# Tools whose operation takes two clicks (pick, then drop/confirm): no switching in between.
TWO_CLICK_TOOLS = {"mActionMoveFeature", "mActionMoveFeatureCopy", "mActionRotateFeature", "mActionScaleFeature",
                   "mActionOffsetCurve"}
# These act on the active layer's SELECTION when it has one (a click anywhere moves/rotates/scales it):
SELECTION_TOOLS = {"mActionMoveFeature", "mActionMoveFeatureCopy", "mActionRotateFeature", "mActionScaleFeature"}
from .manager import option

CLICK_TOLERANCE_PX = 6


class LayerUnderMouse(QObject):

    def __init__(self, iface, manager, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.manager = manager
        self.canvas = iface.mapCanvas()
        self.busy = False                 # an operation has started; don't switch until it ends
        self._watched = set()
        self.installed = False
        self._last_hover = None
        self._line = []                   # points drawn with Reshape / Split (map coordinates)
        self.allowed = set(QGIS_TOOLS)

    # ------------------------------------------------------------ lifecycle
    def install(self):
        if self.installed:
            return
        self.canvas.viewport().installEventFilter(self)
        self.canvas.installEventFilter(self)                   # key presses (Esc)
        self.canvas.mapToolSet.connect(self._reset)
        QgsProject.instance().layersAdded.connect(self._watch_layers)
        QgsProject.instance().layersWillBeRemoved.connect(self._forget_layers)
        QgsProject.instance().cleared.connect(self._forget_all)
        self._watch_layers(list(QgsProject.instance().mapLayers().values()))
        self.installed = True

    def uninstall(self):
        if not self.installed:
            return
        try:
            self.canvas.viewport().removeEventFilter(self)
            self.canvas.removeEventFilter(self)
            self.canvas.mapToolSet.disconnect(self._reset)
            QgsProject.instance().layersAdded.disconnect(self._watch_layers)
            QgsProject.instance().layersWillBeRemoved.disconnect(self._forget_layers)
            QgsProject.instance().cleared.disconnect(self._forget_all)
        except (TypeError, RuntimeError):
            pass
        for layer in list(QgsProject.instance().mapLayers().values()):
            if layer.id() in self._watched and isinstance(layer, QgsVectorLayer):
                for sig in (layer.editCommandEnded, layer.editCommandDestroyed):
                    try:
                        sig.disconnect(self._reset)
                    except (TypeError, RuntimeError):
                        pass
        self._watched.clear()
        self.installed = False

    def _watch_layers(self, layers):
        for layer in layers:
            if isinstance(layer, QgsVectorLayer) and layer.id() not in self._watched:
                self._watched.add(layer.id())
                # an edit was completed or abandoned -> the operation is over
                layer.editCommandEnded.connect(self._reset)
                layer.editCommandDestroyed.connect(self._reset)

    def _forget_layers(self, layer_ids):
        for lid in layer_ids:
            self._watched.discard(lid)          # a reopened project reuses ids: watch the new layers again

    def _forget_all(self):
        self._watched.clear()
        self.busy = False

    def _reset(self, *args):
        self.busy = False

    # ------------------------------------------------------------ decisions
    def applies_to(self, tool):
        if tool is None or not option("follow_mouse_edit") or not self.manager.is_running():
            return False
        if not (tool.flags() & QgsMapTool.Flag.EditTool):
            return False                                      # not a QGIS editing tool
        if isinstance(tool, QgsMapToolDigitizeFeature):
            return False                                      # Add Feature / shapes: user picks the layer
        action = tool.action()
        if action is None or action.objectName() not in self.allowed:
            return False                                      # not one of QGIS's own editing tools
        spec = spec_for_action(action)
        if spec is not None and spec[2] in ("one_selected", "selection"):
            return False      # Add Part: the selected feature's layer; Merge: the selection's layer
        return True

    @staticmethod
    def geometry_types(tool):
        spec = spec_for_action(tool.action()) if tool is not None else None
        return spec[1] if spec is not None else ANY

    def layers_at(self, map_point, geometry_types=ANY, holes_count=False):
        """
        Visible, selectable, editable layers with a feature under the point, best first:
        1. a SELECTED feature under the mouse, 2. a point, line, vertex or polygon edge near the
        mouse, 3. the inside of a polygon - each group in Layers-panel order (top first).
        """
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        tol = CLICK_TOLERANCE_PX * self.canvas.mapUnitsPerPixel()
        p = QgsPointXY(map_point)
        rect = QgsRectangle(p.x() - tol, p.y() - tol, p.x() + tol, p.y() + tol)
        ranked = []
        for order, layer in enumerate(self.manager.selectable_layers(self.canvas)):
            if not layer.isEditable() or layer.geometryType() not in geometry_types:
                continue
            try:
                ct = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
                lrect = ct.transformBoundingBox(rect)
                lpt = QgsGeometry.fromPointXY(ct.transform(p))
                ltol = max(lrect.width(), lrect.height()) / 2.0
                selected = set(layer.selectedFeatureIds())
                if holes_count:
                    # A click inside a hole touches no geometry, and QGIS's rectangle search of
                    # edited features is exact - so, like QGIS's ring tools, search what is on screen.
                    view = ct.transformBoundingBox(self.canvas.extent())
                    request = QgsFeatureRequest().setFilterRect(view).setNoAttributes().setLimit(5000)
                else:
                    request = QgsFeatureRequest().setFilterRect(lrect).setNoAttributes().setLimit(50)
                best = None                                   # (not selected, inside-only)
                for f in layer.getFeatures(request):
                    g = f.geometry()
                    if holes_count and layer.geometryType() == PG and not g.isEmpty():
                        g = g.removeInteriorRings()           # Delete/Fill Ring: you click INSIDE the hole
                    if g.isEmpty() or g.distance(lpt) > ltol:
                        continue
                    inside_only = False
                    if layer.geometryType() == PG:
                        edge = QgsGeometry(g.constGet().boundary()) if g.constGet() is not None else None
                        inside_only = edge is None or edge.isEmpty() or edge.distance(lpt) > ltol
                    key = (f.id() not in selected, inside_only)
                    if best is None or key < best:
                        best = key
                    if best == (False, False):
                        break
                if best is not None:
                    ranked.append((best, order, layer))
            except Exception as err:                          # report, never break the click
                QgsMessageLog.logMessage(f"Layer under mouse: '{layer.name()}' skipped: {err!r}",
                                         "Edit Session", Qgis.MessageLevel.Info)
                continue
        ranked.sort(key=lambda t: (t[0], t[1]))
        return [layer for _, _, layer in ranked]

    def layer_at(self, map_point, geometry_types=ANY):
        layers = self.layers_at(map_point, geometry_types)
        return layers[0] if layers else None

    def switch_to(self, tool, layers):
        """Make the first layer QGIS accepts for this tool active. Returns the layer or None."""
        previous = self.iface.activeLayer()
        action = tool.action() if tool is not None else None
        for layer in layers:
            if self.iface.activeLayer() is not layer:
                self.iface.setActiveLayer(layer)
            if action is None or action.isEnabled():
                return layer
        if previous is not None and self.iface.activeLayer() is not previous:
            self.iface.setActiveLayer(previous)               # nothing fits: leave things as they were
        return None

    # ------------------------------------------------------------ events
    def eventFilter(self, obj, event):
        with suppress(Exception):
            etype = event.type()
            if etype == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.busy = False
                self._line = []
            elif etype == QEvent.Type.KeyPress and obj is self.canvas:
                self._on_key(event)
            elif etype == QEvent.Type.MouseButtonPress and obj is self.canvas.viewport():
                self._on_press(event)
            elif (etype == QEvent.Type.MouseMove and obj is self.canvas.viewport()
                  and event.buttons() == Qt.MouseButton.NoButton):
                self._on_hover(event)
        return False                                          # never swallow the event

    def _finish_tool(self, tool):
        spec = spec_for_action(tool.action()) if tool is not None and tool.action() is not None else None
        return spec is not None and spec[2] == "fixed" and isinstance(tool, QgsMapToolCapture) and self.applies_to(tool)

    def _on_key(self, event):
        tool = self.canvas.mapTool()
        if not self._finish_tool(tool):
            return
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self._line:
            self._line.pop()                                  # QGIS removes the last vertex
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._switch_for_line(tool)

    def _switch_for_line(self, tool):
        """Reshape / Split finishing: make the layer whose feature the drawn line crosses active."""
        pts, self._line = self._line, []
        if len(pts) < 2 or not tool.isCapturing():
            return
        active = self.iface.activeLayer()
        layers = self.layers_crossed(pts, self.geometry_types(tool))
        if not layers or active in layers[:1]:
            return
        if active is not None and hasattr(active, "crs") and layers[0].crs() != active.crs():
            # QGIS keeps the drawn points in the active layer's coordinates: switching now would
            # move the line. Leave it, and say how to do it.
            self.iface.mainWindow().statusBar().showMessage(
                f"Edit Session: the line crosses '{layers[0].name()}', which uses another CRS than the active "
                f"layer '{active.name()}'. Start the line on a feature of '{layers[0].name()}', or make it active.", 10000)
            return
        same_crs = [lyr for lyr in layers if active is None or lyr.crs() == active.crs()]
        self.switch_to(tool, same_crs)

    def layers_crossed(self, canvas_points, geometry_types):
        """Editable layers with a feature crossed by the line: selected features first, then top-most."""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        line = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in canvas_points])
        ranked = []
        for order, layer in enumerate(self.manager.selectable_layers(self.canvas)):
            if not layer.isEditable() or layer.geometryType() not in geometry_types:
                continue
            try:
                ct = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
                lline = QgsGeometry(line)
                lline.transform(ct)
                selected = set(layer.selectedFeatureIds())
                best = None
                for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(lline.boundingBox()).setNoAttributes()):
                    if f.geometry().intersects(lline):
                        key = f.id() not in selected
                        best = key if best is None else min(best, key)
                        if best is False:
                            break
                if best is not None:
                    ranked.append((best, order, layer))
            except Exception as err:
                QgsMessageLog.logMessage(f"Line crossing: '{layer.name()}' skipped: {err!r}", "Edit Session",
                                         Qgis.MessageLevel.Info)
        ranked.sort(key=lambda t: (t[0], t[1]))
        return [layer for _, _, layer in ranked]

    def _on_press(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.busy = False                                 # right-click finishes / cancels in QGIS tools
            tool = self.canvas.mapTool()
            if self._finish_tool(tool):
                self._switch_for_line(tool)                   # Reshape / Split: before QGIS finishes the line
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        tool = self.canvas.mapTool()
        if not self.applies_to(tool):
            return
        if self._finish_tool(tool):                           # Reshape / Split: remember the line,
            if not tool.isCapturing():                        # the layer is chosen when it is finished
                self._line = []
            self._line.append(QgsPointXY(self._map_point(event)))
            return
        if isinstance(tool, QgsMapToolCapture) and tool.isCapturing():
            return                                            # in the middle of drawing (ring ...)
        mods = event.modifiers()
        name = tool.action().objectName()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            return              # Ctrl/Alt+click has its own meaning (rotation anchor, offset from another layer ...)
        if name in SELECTION_TOOLS and self._active_has_fitting_selection(tool):
            return              # QGIS moves/rotates/scales the active layer's selection wherever you click
        spec = spec_for_action(tool.action())
        if spec is not None and spec[2] in ("hover", "fixed"):
            return                    # Trim/Extend: chosen while hovering; Reshape/Split: when finished
        layers = self.layers_at(self._map_point(event), self.geometry_types(tool),
                                holes_count=name in RING_TOOLS)
        if not layers:
            self.busy = False                                 # click on empty map: nothing to switch, start fresh
            return
        if self.busy and not isinstance(tool, QgsMapToolCapture):
            return                                            # second click of a pick-then-drop tool
        layer = self.switch_to(tool, layers)                  # selected / top-most editable feature wins
        if layer is not None and name in TWO_CLICK_TOOLS:
            self.busy = True                                  # pick done: the next click must stay on this layer

    def _active_has_fitting_selection(self, tool):
        layer = self.iface.activeLayer()
        return (isinstance(layer, QgsVectorLayer) and layer.isEditable() and layer.selectedFeatureCount() > 0
                and layer.geometryType() in self.geometry_types(tool))

    def _map_point(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        return self.canvas.getCoordinateTransform().toMapCoordinates(pos)

    def _on_hover(self, event):
        """Trim/Extend picks the feature to change while the mouse moves, on the active layer."""
        tool = self.canvas.mapTool()
        spec = spec_for_action(tool.action()) if tool is not None else None
        if spec is None or spec[2] != "hover" or not self.applies_to(tool):
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        key = (pos.x(), pos.y())
        if key == self._last_hover:                           # the mouse has not really moved
            return
        self._last_hover = key
        layers = self.layers_at(self._map_point(event), spec[1])
        if layers and self.iface.activeLayer() not in layers[:1]:
            self.switch_to(tool, layers)

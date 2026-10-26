"""
Split, laid out like the Split pane of ArcGIS Pro, in two tabs:

    Interactive   draw a line across the features (click its points; right-click, double-click or Enter finishes,
                  Backspace removes the last point, Esc cancels). With a selection only the selected features are
                  split; "Allow splitting without a selection" splits every editable feature the line crosses.
    By Feature    pick Input Features (lines, or polygons whose outline cuts) and Target Features (what gets split),
                  each with its "Select ... Features" button, then press Split.

Pieces keep the attributes of the feature they came from (QGIS' own split). One undoable edit per layer. Ctrl+Z / Ctrl+Y
(Ctrl+Shift+Z) undo and redo the splits made here in every layer they touched; the layer that was split also becomes
the active layer, so QGIS' own Undo and Redo reach it.
"""

import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsSettings,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..edit_tools import split_with_line
from ..selection import _transform, all_vector_layers, map_layers
from .page import DoneStrip, SelectedTree, button, check, selection_banner
from .reshape import PickTool
from ...core.compat import enum_value

SETTINGS = "kga_editor/modify_"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
SUCCESS = Qgis.GeometryOperationResult.Success
MARKER_BOX = enum_value((Qgis, 'VertexMarkerType.Box'), (QgsVertexMarker, 'IconType.ICON_BOX'))  # QGIS >= 3.40 / 4
INFO_TEXT = ("Click the points of a line across the features, then right-click or press Enter. Pieces keep the "
             "attributes of the feature they come from.")
BY_FEATURE_TEXT = ("Pick the input features that cut (lines, or polygon outlines) and the target features to split, "
                   "then press Split.")
TABS_QSS = """
QTabWidget#KgaSplitTabs::pane { border: none; }
QTabBar::tab { background: transparent; padding: 4px 8px; color: #333333; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #0079c1; border-bottom: 2px solid #0079c1; }
"""


def outline_lines(geom):
    """The lines a feature cuts with: a line's parts, or every ring of a polygon, as point lists."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    if geom.type() == LINE:
        parts = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
        return [list(p) for p in parts if len(p) > 1]
    if geom.type() == POLYGON:
        polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
        return [list(ring) for poly in polys for ring in poly if len(ring) > 1]
    return []


def _live(items):
    """The (layer, fid) pairs whose layer still exists."""
    return [(layer, fid) for layer, fid in items if not sip.isdeleted(layer)]


def split_by_features(canvas, inputs, targets):
    """
    Split the target features with the input features. inputs / targets: [(layer, fid)]; only targets in editable
    layers are split. Returns (new features, layers changed, layers skipped because they are not editable).
    """
    canvas_crs = canvas.mapSettings().destinationCrs()
    lines = []                                           # the cutting lines in map coordinates
    for layer, fid in inputs:
        feature = layer.getFeature(fid)
        geom = QgsGeometry(feature.geometry())
        if geom.isNull():
            continue
        if layer.crs() != canvas_crs:
            geom.transform(_transform(layer.crs(), canvas_crs))
        lines += outline_lines(geom)
    by_layer = {}
    for layer, fid in targets:
        by_layer.setdefault(layer.id(), (layer, []))[1].append(fid)
    new_features, changed, skipped = 0, [], []
    for layer, ids in by_layer.values():
        if not layer.isEditable():
            skipped.append(layer.name())
            continue
        if layer.geometryType() not in (LINE, POLYGON):
            continue
        to_layer = None
        if layer.crs() != canvas_crs:
            to_layer = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
        previous = list(layer.selectedFeatureIds())
        before = len(layer.editBuffer().addedFeatures()) if layer.editBuffer() else 0
        layer.beginEditCommand("Split features")
        layer.selectByIds(ids)                           # QGIS splits the selected features of a layer
        done = False
        for pts in lines:
            moved = pts if to_layer is None else [to_layer.transform(QgsPointXY(p)) for p in pts]
            result, _topo = layer.splitFeatures(QgsLineString([QgsPoint(p) for p in moved]), True, False)
            done = done or result == SUCCESS
            new_ids = [f for f in layer.allFeatureIds() if f < 0 and f not in ids]
            layer.selectByIds(list(ids) + new_ids)       # pieces are split by the next line too
        layer.selectByIds(previous)
        if done:
            layer.endEditCommand()
            after = len(layer.editBuffer().addedFeatures()) if layer.editBuffer() else before
            new_features += max(after - before, 0)
            changed.append(layer.name())
        else:
            layer.destroyEditCommand()
        layer.triggerRepaint()
    return new_features, changed, skipped


class FeatureList(QWidget):
    """One of the By Feature lists: the features picked so far, grouped by layer, and its Select button."""

    def __init__(self, button_text, on_select, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.tree = QTreeWidget()
        self.tree.setObjectName("KgaModifySelectedTree")
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        lay.addWidget(self.tree, 1)
        self.button = QToolButton()
        self.button.setObjectName("KgaModifyBanner")
        self.button.setCheckable(True)
        self.button.setIcon(QIcon(os.path.join(ICON_DIR, "select_change.svg")))
        self.button.setIconSize(QSize(22, 22))
        self.button.setText(button_text)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.button.clicked.connect(lambda _c=False: on_select())
        lay.addWidget(self.button)

    def show_features(self, items, canvas):
        """items: [(layer, fid)]."""
        from ..attributes.layer_browser import feature_label
        self.tree.clear()
        grouped = {}
        for layer, fid in items:
            grouped.setdefault(layer.id(), (layer, []))[1].append(fid)
        for layer, ids in grouped.values():
            top = QTreeWidgetItem(self.tree, [f"{layer.name()} ({len(ids)})"])
            for fid in ids[:SelectedTree.MAX_PER_LAYER]:
                child = QTreeWidgetItem(top, [feature_label(layer, layer.getFeature(fid))])
                child.setData(0, Qt.ItemDataRole.UserRole, (layer.id(), fid))
            top.setExpanded(True)


class SplitTool(PickTool):
    KEY = "split"
    LABEL = "Split"
    HINT = ("draw a line across the features (right-click or Enter to split), or pick input and target features on "
            "the By Feature tab.")
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = False
    SELECT_ONE_SHOT = True

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.allow_no_selection = settings.value(SETTINGS + "split_no_selection", False, type=bool)
        self.tab = 0
        self.inputs = []                      # By Feature: [(layer, fid)] the lines that cut
        self.targets = []                     # By Feature: [(layer, fid)] what is cut
        self._capture = None                  # "input" | "target": the next selection fills that list
        self._arming = False                  # a Select button is being pressed
        self._ignore_next_release = False
        self._markers = None
        self._bands = {}
        self.done_strip = None
        self.tabs = None
        self.banner = None
        self.input_list = self.target_list = None
        self.split_button = None
        self.points = []
        self._undone = []                     # splits made here: [(layer id, undo index before, index after)] each
        self._redoable = []
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        super().activate()
        self._show_marks()

    def deactivate(self):
        self._capture = None
        self.points = []
        self._draw()
        self._clear_marks()
        super().deactivate()

    def reset(self):
        self.points = []
        self._draw()
        super().reset()

    def dispose(self):
        self._clear_marks()
        for item in list(self._bands.values()) + list(self._markers or ()):
            with suppress(Exception):
                self.canvas().scene().removeItem(item)
        self._bands, self._markers = {}, None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.done_strip = DoneStrip()
        box.addWidget(self.done_strip)
        self.done_strip.show_text(INFO_TEXT, ok="info")

    def build_page(self, box):
        self.tabs = QTabWidget()
        self.tabs.setObjectName("KgaSplitTabs")
        self.tabs.setStyleSheet(TABS_QSS)
        self.tabs.addTab(self._interactive_tab(), "Interactive")
        self.tabs.addTab(self._by_feature_tab(), "By Feature")
        self.tabs.setCurrentIndex(self.tab)
        self.tabs.currentChanged.connect(self.set_tab)
        box.addWidget(self.tabs, 100)

    def _interactive_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        self.banner = selection_banner(self._banner_clicked)
        self._select_button = self.banner
        self.banner.setChecked(self._selecting)
        lay.addWidget(self.banner)
        self.tree = SelectedTree()
        lay.addWidget(self.tree)
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "split.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Split")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT + " Backspace removes the last point, Esc cancels.")
        tile.clicked.connect(lambda _c=False: self.draw_mode())
        self.tool_tile = tile
        lay.addWidget(tile)
        lay.addStretch(1)
        self.nosel_check = check("Allow splitting without a selection", self.allow_no_selection,
                                 self.set_allow_no_selection)
        self.nosel_check.setToolTip("With nothing selected, split every editable feature the line crosses")
        lay.addWidget(self.nosel_check)
        return page

    def _by_feature_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        self.feature_tabs = QTabWidget()
        self.feature_tabs.setObjectName("KgaSplitTabs")
        self.feature_tabs.setStyleSheet(TABS_QSS)
        self.input_list = FeatureList("Select Input Features", lambda: self.capture_for("input"))
        self.target_list = FeatureList("Select Target Features", lambda: self.capture_for("target"))
        self.feature_tabs.addTab(self.input_list, "Input Features")
        self.feature_tabs.addTab(self.target_list, "Target Features")
        lay.addWidget(self.feature_tabs, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.split_button = button("Split", self.split_features, "Split the target features with the input features")
        row.addWidget(self.split_button)
        lay.addLayout(row)
        return page

    def update_page(self):
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        if self.input_list is None or sip.isdeleted(self.input_list):
            return
        self.inputs, self.targets = _live(self.inputs), _live(self.targets)
        if self._capture is not None and not self._arming:
            self._store_capture()
        self.input_list.show_features(self.inputs, self.canvas())
        self.target_list.show_features(self.targets, self.canvas())
        self.input_list.button.setChecked(self._capture == "input" and self._selecting)
        self.target_list.button.setChecked(self._capture == "target" and self._selecting)
        for widget in (self.banner, self.tree):
            if widget is not None and not sip.isdeleted(widget):
                widget.setVisible(not self.allow_no_selection)
        self._show_marks()

    def set_tab(self, index):
        self.tab = int(index)
        self._capture = None
        self.set_selecting(False)
        self.points = []
        self._draw()
        self._select_button = self.banner if self.tab == 0 else None
        if self.done_strip is not None:
            self.done_strip.show_text(INFO_TEXT if self.tab == 0 else BY_FEATURE_TEXT, ok="info")
        self._show_marks()

    def set_allow_no_selection(self, on):
        self.allow_no_selection = bool(on)
        QgsSettings().setValue(SETTINGS + "split_no_selection", self.allow_no_selection)
        if on and self._selecting and self._capture is None:
            self.set_selecting(False)
        self.refresh_page()

    def draw_mode(self):
        if self._selecting:
            self.set_selecting(False)
        if getattr(self, "tool_tile", None) is not None:
            self.tool_tile.setChecked(True)

    def _banner_clicked(self):
        self._capture = None
        self.set_selecting(not self._selecting)

    # ------------------------------------------------------------------ By Feature
    def capture_for(self, kind):
        """The next selection on the map fills the Input or the Target list."""
        if self._selecting and self._capture == kind:
            self._capture = None
            self.set_selecting(False)
            return
        self._arming = True                    # clearing the selection fires selectionChanged: not a pick
        try:
            self._capture = kind
            if kind == "input":
                self.inputs = []
            else:
                self.targets = []
            for layer in all_vector_layers():
                if layer.selectedFeatureCount():
                    layer.removeSelection()
            self.set_selecting(True)
        finally:
            self._arming = False
        self.refresh_page()

    def _store_capture(self):
        shown = map_layers(self.canvas())
        items = []
        for layer in all_vector_layers():
            if (layer.selectedFeatureCount() and layer.isSpatial() and layer.geometryType() in (LINE, POLYGON)
                    and (shown is None or layer.id() in shown)):
                items += [(layer, fid) for fid in layer.selectedFeatureIds()]
        if self._capture == "input":
            self.inputs = items
        else:
            self.targets = [(layer, fid) for layer, fid in items if layer.isEditable()]

    def split_features(self):
        if not self.inputs or not self.targets:
            self.tell("Pick input features (lines or polygons) and target features first.", ok=False)
            return 0
        before = self._positions()
        new, changed, skipped = split_by_features(self.canvas(), self.inputs, self.targets)
        if changed:
            self._record(before)
            text = f"Split completed: {new} new feature(s)."
            self.inputs, self.targets = [], []
            self._capture = None
        else:
            text = "Nothing was split - the input lines must cross the target features."
        if skipped:
            text += f" Not editable, left alone: {', '.join(skipped)}."
        self.tell(text, ok=bool(changed))
        self.refresh_page()
        return new

    # ------------------------------------------------------------------ undo and redo
    def _positions(self):
        """{layer id: undo stack index} of the editable layers, taken before a split."""
        return {layer.id(): layer.undoStack().index() for layer in all_vector_layers()
                if layer.isEditable() and not sip.isdeleted(layer)}

    def _record(self, before):
        """Remember which layers the split just changed (by their undo stacks) and make the first one active."""
        changed = []
        for layer in all_vector_layers():
            if layer.id() in before and layer.undoStack().index() != before[layer.id()]:
                changed.append((layer.id(), before[layer.id()], layer.undoStack().index()))
        if not changed:
            return
        self._undone.append(changed)
        self._redoable = []
        setter = getattr(self.iface, "setActiveLayer", None)
        layer = QgsProject.instance().mapLayer(changed[0][0])
        if setter is not None and layer is not None:
            setter(layer)                              # QGIS' own Ctrl+Z works on the active layer

    def undo_split(self):
        """Undo the last split made here in every layer it changed. Returns True when something was undone."""
        while self._undone:
            entry = self._undone.pop()
            done = False
            for layer_id, _before, after in entry:
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer is not None and layer.undoStack().index() == after:       # not already undone elsewhere
                    layer.undoStack().undo()
                    layer.triggerRepaint()
                    done = True
            if done:
                self._redoable.append(entry)
                self._after_history("Split undone.")
                return True
        return False

    def redo_split(self):
        """Redo the split that was undone last. Returns True when something was redone."""
        while self._redoable:
            entry = self._redoable.pop()
            done = False
            for layer_id, before, _after in entry:
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer is not None and layer.undoStack().index() == before and layer.undoStack().canRedo():
                    layer.undoStack().redo()
                    layer.triggerRepaint()
                    done = True
            if done:
                self._undone.append(entry)
                self._after_history("Split redone.")
                return True
        return False

    def _after_history(self, text):
        self.tell(text, ok=True)
        self._show_marks()
        self.refresh_page()

    def tell(self, text, ok=True):
        if self.done_strip is not None:
            self.done_strip.show_text(text, ok=ok)
        self.say(text)

    # ------------------------------------------------------------------ marks on the map
    def _band(self, key, colour, width, dashed=True):
        band = self._bands.get(key)
        if band is None:
            band = QgsRubberBand(self.canvas(), LINE)
            band.setColor(colour)
            band.setWidth(width)
            if dashed:
                band.setLineStyle(Qt.PenStyle.DashLine)
            self._bands[key] = band
        return band

    def _clear_marks(self):
        for key in ("inputs", "targets"):
            band = self._bands.get(key)
            if band is not None:
                band.reset(LINE)

    def _show_marks(self):
        """By Feature: the picked inputs (orange dashes) and targets (cyan dashes) on the map."""
        self._clear_marks()
        if self.tab != 1 or not self.isActive():
            return
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        for key, colour, items in (("inputs", QColor(255, 165, 0), self.inputs),
                                   ("targets", QColor(0, 200, 220), self.targets)):
            band = self._band(key, colour, 3)
            for layer, fid in _live(items):
                geom = QgsGeometry(layer.getFeature(fid).geometry())
                if geom.isNull():
                    continue
                if layer.crs() != canvas_crs:
                    geom.transform(_transform(layer.crs(), canvas_crs))
                outline = geom if geom.type() == LINE else geom.convertToType(LINE, False)
                if outline is not None and not outline.isNull():
                    band.addGeometry(outline, QgsCoordinateReferenceSystem(), False)
            band.updatePosition()
            band.update()

    # ------------------------------------------------------------------ drawing (Interactive)
    def _sketch(self):
        band = self._band("sketch", QColor(0, 0, 0), 1)
        band.setWidth(2)
        return band

    def _draw(self, tail=None):
        band = self._sketch()
        band.reset(LINE)
        shown = self.points + ([tail] if tail is not None else [])
        for i, p in enumerate(shown):
            band.addPoint(p, i == len(shown) - 1)
        self._place_markers(shown)

    def _place_markers(self, shown):
        """Pro's sketch: a green square at the first point, a red one at the last."""
        if self._markers is None:
            if not shown:
                return
            self._markers = []
            for colour in (QColor(0, 140, 60), QColor(220, 30, 30)):
                marker = QgsVertexMarker(self.canvas())
                marker.setIconType(MARKER_BOX)
                marker.setColor(colour)
                marker.setIconSize(8)
                marker.setPenWidth(2)
                self._markers.append(marker)
        start, end = self._markers
        if not shown:
            start.hide()
            end.hide()
            return
        start.setCenter(shown[0])
        start.show()
        end.setCenter(shown[-1])
        end.setVisible(len(shown) > 1)

    def on_move(self, point):
        if self.tab == 0 and self.points:
            self._draw(point)

    def on_click(self, point):
        if self.tab != 0 or self._selecting:
            return
        if self._ignore_next_release:                  # the second click of a double-click
            self._ignore_next_release = False
            return
        if not self.allow_no_selection and not self._has_selection():
            self.set_selecting(True, auto=True)
            self.say("select the features to split first.")
            return
        if not self.points or point != self.points[-1]:
            self.points.append(point)
        self._draw()

    def canvasDoubleClickEvent(self, e):
        if self.tab == 0 and e.button() == Qt.MouseButton.LeftButton and not self._selecting:
            self._ignore_next_release = True
            self.finish()

    def on_right_click(self, e):
        if self.tab == 0:
            self.finish()

    def finish(self):
        points, self.points = list(self.points), []
        self._draw()
        if len(points) < 2:
            return 0
        before = self._positions()
        new, changed = split_with_line(points, self.canvas().mapSettings().destinationCrs(), None)
        if changed:
            self._record(before)
            text = f"Split completed: {new} new feature(s)."
        else:
            text = "Nothing was split - draw the line right across the features."
        self.tell(text, ok=bool(changed))
        self.refresh_page()
        return new

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):
            return
        key = e.key()
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if ctrl and key == Qt.Key.Key_Z and not shift and self.undo_split():
            e.accept()
            return
        if ctrl and (key == Qt.Key.Key_Y or (key == Qt.Key.Key_Z and shift)) and self.redo_split():
            e.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.tab == 0 and self.points:
            self.finish()
            e.ignore()
        elif key == Qt.Key.Key_Backspace and self.tab == 0 and self.points:
            self.points.pop()
            self._draw()
            e.ignore()
        else:
            super().keyPressEvent(e)

    def canvasMoveEvent(self, e):
        if self.forward_select("canvasMoveEvent", e):
            return
        point = QgsPointXY(e.snapPoint())                  # must run BEFORE mapPointMatch()
        self.snap_indicator.setMatch(e.mapPointMatch() if self.tab == 0 else QgsPointLocator.Match())
        self.on_move(point)


TOOL_CLASSES = {SplitTool.KEY: SplitTool}

"""
The Attributes pane: selected features in a tree, their values below, editable in place.

Layout, top to bottom: Selection / Layers tabs, the "Change the selection." banner (a map tool),
the tree, a draggable splitter, Attributes / Geometry tabs with the field list, and the Auto Apply
switch with Apply / Cancel.
"""

from contextlib import suppress

from qgis.core import QgsFeatureRequest, QgsProject, QgsSettings, QgsVectorLayer
from ...gui.panel_bubble import ToolbarReveal
from qgis.gui import QgsDockWidget, QgsHighlight
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction, QActionGroup
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction, QActionGroup

import os

from ...branding import open_docs
from .fields import AttributeModel, FieldView
from .geometry import GeometryWidget
from .layer_browser import LayerBrowser, feature_label, feature_labeler
from .style import PANEL_QSS
from .widgets import ToggleSwitch, make_tab_bar
from ..manager import edit_blocked

TITLE = "Attributes"
DOCS_NAME = "open_feature_attribute"        # khmergrs.com/docs/qgis/<name>, like every KGA tool
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
SETTING_AUTO_APPLY = "kga_editor/attributes_auto_apply"

ROLE_KIND = Qt.ItemDataRole.UserRole           # "layer" | "feature"
ROLE_LAYER = Qt.ItemDataRole.UserRole + 1      # layer id
ROLE_FID = Qt.ItemDataRole.UserRole + 2
MAX_TREE_FEATURES = 1000                       # features listed per layer; the count shows the real total
MAX_HIGHLIGHT = 500
HIGHLIGHT_COLOR = QColor(0, 190, 255)

SHAPES = (("rectangle", "Rectangle", "Click a feature or drag a rectangle"),
          ("polygon", "Polygon", "Click the corners; double-click or right-click to finish"),
          ("freehand", "Freehand", "Drag to draw around the features"),
          ("circle", "Circle", "Click the centre, then drag out the radius"),
          ("line", "Line", "Click along a line; double-click or right-click to finish"))


def layer_tree_layers():
    """Vector layers in Layers-panel order."""
    out = []
    for node in QgsProject.instance().layerTreeRoot().findLayers():
        layer = node.layer()
        if isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer):
            out.append(layer)
    return out


class AttributesPanel(ToolbarReveal, QgsDockWidget):

    shapeChanged = pyqtSignal(str)

    def __init__(self, iface, parent=None):
        super().__init__(TITLE, parent)
        self.setObjectName("KGAAttributesPanel")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self._watched = {}                 # layer id -> layer, for selection / attribute signals
        self._highlights = []
        self._geometry_target = None
        self._refreshing = False
        # A closed pane does not follow the selection: listing the selected features and their
        # geometry on every click or rectangle select cost from 50 ms to over a second, with nobody
        # looking. It notes that it is out of date and catches up when it is shown.
        self._stale = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._scheduled_refresh)

        self.model = AttributeModel(self)
        self.model.editingStarted.connect(self._editing_started)
        self.model.pendingChanged.connect(self._pending_changed)
        self.model.problem.connect(self._problem)
        self.model.valuesWritten.connect(self._values_written)

        self._build_ui()
        auto = QgsSettings().value(SETTING_AUTO_APPLY, True, type=bool)
        self.switch.setChecked(auto)
        self.model.auto_apply = auto
        self._pending_changed(False)

        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        project.cleared.connect(self._layers_changed)
        self._layers_changed()

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        root = QWidget(self)
        root.setObjectName("KgaAttrRoot")
        root.setStyleSheet(PANEL_QSS)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 4, 8, 6)
        outer.setSpacing(6)

        head = QHBoxLayout()
        self.tabs = make_tab_bar(["Selection", "Layers"], root)
        self.tabs.currentChanged.connect(self._tab_changed)
        head.addWidget(self.tabs)
        head.addStretch(1)
        self.help_button = QToolButton(root)
        self.help_button.setObjectName("KgaAttrHelp")
        self.help_button.setText("?")
        self.help_button.setToolTip("Open the documentation for this tool")
        self.help_button.clicked.connect(self._show_help)
        head.addWidget(self.help_button)
        outer.addLayout(head)

        # the banner IS the select tool: checked while the tool is active
        self.select_action = QAction(QIcon(os.path.join(ICON_DIR, "select_change.svg")),
                                     "Change the selection.", self)
        self.select_action.setObjectName("mActionKgaAttributesSelect")
        self.select_action.setCheckable(True)
        self.select_action.setToolTip("Change the selection. Click a feature or drag on the map. "
                                      "Shift adds, Ctrl removes. Use the arrow for other shapes.")
        self.banner = QToolButton(root)
        self.banner.setObjectName("KgaAttrBanner")
        self.banner.setDefaultAction(self.select_action)
        self.banner.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.banner.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.banner.setMinimumHeight(34)
        self.banner.setIconSize(self.banner.iconSize() * 1.2)
        self.shape_menu = QMenu(self.banner)
        self.shape_group = QActionGroup(self)
        self.shape_actions = {}
        for key, text, tip in SHAPES:
            action = self.shape_menu.addAction(text)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.setData(key)
            self.shape_group.addAction(action)
            self.shape_actions[key] = action
            action.triggered.connect(lambda _c=False, k=key: self.shapeChanged.emit(k))
        self.shape_actions["rectangle"].setChecked(True)
        self.banner.setMenu(self.shape_menu)
        self.banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer.addWidget(self.banner)

        # ---- tree area
        self.splitter = QSplitter(Qt.Orientation.Vertical, root)
        self.splitter.setChildrenCollapsible(False)
        self.top_stack = QStackedWidget(self.splitter)

        self.tree = self._make_tree()
        self.tree.currentItemChanged.connect(self._tree_current_changed)
        self.tree.customContextMenuRequested.connect(lambda pos: self.contextMenuFor(self.tree, pos))
        self.browser = LayerBrowser(self)
        self.browser.currentChanged.connect(self._layers_current_changed)
        self.browser.featureStepped.connect(self._stepped)
        self.browser.list.customContextMenuRequested.connect(self._browser_menu)
        self.hint = QLabel("No features are selected.\nUse Change the selection to pick features on the map.")
        self.hint.setObjectName("KgaAttrHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.hint.setWordWrap(True)
        for w in (self.tree, self.browser, self.hint):
            self.top_stack.addWidget(w)

        # ---- details
        detail = QWidget(self.splitter)
        dv = QVBoxLayout(detail)
        dv.setContentsMargins(0, 2, 0, 0)
        dv.setSpacing(4)
        self.detail_tabs = make_tab_bar(["Attributes", "Geometry"], detail)
        self.detail_tabs.currentChanged.connect(self._detail_tab_changed)
        dv.addWidget(self.detail_tabs)
        self.notice = QLabel(detail)
        self.notice.setObjectName("KgaAttrNotice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        dv.addWidget(self.notice)
        self.detail_stack = QStackedWidget(detail)
        self.field_view = FieldView(detail)
        self.field_view.setModel(self.model)
        self.geometry = GeometryWidget(self)
        self.geometry.pendingChanged.connect(lambda _on: self._pending_changed(False))
        self.geometry.editingStarted.connect(self._editing_started)
        self.geometry.problem.connect(self._problem)
        self.geometry.status.connect(self._problem)
        self.empty = QLabel("", detail)
        self.empty.setObjectName("KgaAttrHint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.empty.setWordWrap(True)
        for w in (self.field_view, self.geometry, self.empty):
            self.detail_stack.addWidget(w)
        dv.addWidget(self.detail_stack, 1)
        self.splitter.addWidget(self.top_stack)
        self.splitter.addWidget(detail)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([180, 360])
        outer.addWidget(self.splitter, 1)

        # ---- footer
        foot = QHBoxLayout()
        label = QLabel("Auto Apply", root)
        label.setObjectName("KgaAttrAutoApply")
        self.switch = ToggleSwitch(root)
        self.switch.setToolTip("On: each value is saved to the layer as soon as you finish typing it.\n"
                               "Off: values wait for Apply (or Cancel).")
        self.switch.toggled.connect(self._auto_apply_toggled)
        self.apply_button = QPushButton("Apply", root)
        self.cancel_button = QPushButton("Cancel", root)
        for b in (self.apply_button, self.cancel_button):
            b.setObjectName("KgaAttrButton")
        self.apply_button.clicked.connect(self.apply_pending)
        self.cancel_button.clicked.connect(self.cancel_pending)
        foot.addWidget(label)
        foot.addWidget(self.switch)
        foot.addStretch(1)
        foot.addWidget(self.apply_button)
        foot.addWidget(self.cancel_button)
        outer.addLayout(foot)
        self.setWidget(root)

    @staticmethod
    def _make_tree():
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(True)
        tree.setIndentation(18)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        return tree

    # ------------------------------------------------------------- watching layers
    def _layers_changed(self, *_args):
        current = {lyr.id(): lyr for lyr in QgsProject.instance().mapLayers().values()
                   if isinstance(lyr, QgsVectorLayer) and not sip.isdeleted(lyr)}
        for lid in list(self._watched):
            if lid not in current:
                self._watched.pop(lid)
        for lid, layer in current.items():
            if lid in self._watched:
                continue
            self._watched[lid] = layer
            layer.selectionChanged.connect(self.schedule_refresh)
            layer.featureDeleted.connect(self.schedule_refresh)    # an edit-buffer delete drops the selection silently
            layer.attributeValueChanged.connect(self._attribute_changed)
            layer.geometryChanged.connect(self._geometry_changed)
            for sig in self._reload_signals(layer):
                sig.connect(self._features_changed)
            layer.editingStopped.connect(self._layer_state_changed)
            layer.editingStarted.connect(self._layer_state_changed)
            layer.afterRollBack.connect(self._layer_state_changed)
        self.schedule_refresh()

    def disconnect_all(self):
        project = QgsProject.instance()
        for sig, slot in ((project.layersAdded, self._layers_changed), (project.layersRemoved, self._layers_changed),
                          (project.cleared, self._layers_changed)):
            with suppress(TypeError, RuntimeError):
                sig.disconnect(slot)
        for layer in list(self._watched.values()):
            if sip.isdeleted(layer):
                continue
            for sig, slot in ((layer.selectionChanged, self.schedule_refresh),
                              (layer.featureDeleted, self.schedule_refresh),
                              (layer.attributeValueChanged, self._attribute_changed),
                              (layer.geometryChanged, self._geometry_changed),
                              *((sig, self._features_changed) for sig in self._reload_signals(layer)),
                              (layer.editingStopped, self._layer_state_changed),
                              (layer.editingStarted, self._layer_state_changed),
                              (layer.afterRollBack, self._layer_state_changed)):
                with suppress(TypeError, RuntimeError):
                    sig.disconnect(slot)
        self._watched.clear()
        self.clear_highlights()
        self._timer.stop()
        self.browser.shutdown()

    def schedule_refresh(self, *_args):
        if not self._refreshing:
            self._timer.start()

    def _scheduled_refresh(self):
        if self.isHidden():                     # closed (a pane whose window is not up yet counts as open)
            self._stale = True
            return
        self.refresh()

    def _attribute_changed(self, _fid, _idx, _value):
        self.model.refresh()
        self._relabel()

    @staticmethod
    def _reload_signals(layer):
        return (layer.featureAdded, layer.featureDeleted, layer.afterRollBack, layer.afterCommitChanges)

    def _features_changed(self, *_args):
        if self.browser.layer() is self.sender():
            self.browser.schedule_reload()

    def _geometry_changed(self, fid, _geometry):
        if fid == self.geometry.fid and not self.geometry.has_pending():
            self.geometry.refresh_from_layer()

    def _layer_state_changed(self, *_args):
        if self.model.layer is not None and not sip.isdeleted(self.model.layer):
            self.model.layoutChanged.emit()
            self.model.refresh()
            self._update_notice()

    # ------------------------------------------------------------- building the trees
    def selected_layers(self):
        return [lyr for lyr in layer_tree_layers() if lyr.selectedFeatureCount()]

    def refresh(self):
        """Rebuild both trees from the current selection, keeping the current row when it survives."""
        if self._refreshing:
            return
        self._stale = False
        self._refreshing = True
        try:
            wanted = self._current_key()
            self.apply_pending()
            self._fill_selection_tree(wanted)
            self.browser.set_layers(layer_tree_layers())
            self._sync_visible_pages()
            self._show_current_target()
        finally:
            self._refreshing = False

    def _current_key(self):
        item = self.tree.currentItem() if self.tabs.currentIndex() == 0 else None
        if item is None:
            return None
        return (item.data(0, ROLE_KIND), item.data(0, ROLE_LAYER), item.data(0, ROLE_FID))

    def _fill_selection_tree(self, wanted):
        tree = self.tree
        tree.blockSignals(True)
        tree.clear()
        pick = None
        first_feature = None
        for layer in self.selected_layers():
            count = layer.selectedFeatureCount()
            node = QTreeWidgetItem(tree, ["{} ({})".format(layer.name(), count)])
            node.setData(0, ROLE_KIND, "layer")
            node.setData(0, ROLE_LAYER, layer.id())
            node.setData(0, ROLE_FID, None)
            node.setToolTip(0, layer.name())
            if wanted and wanted[0] == "layer" and wanted[1] == layer.id():
                pick = node
            ids = sorted(layer.selectedFeatureIds())[:MAX_TREE_FEATURES]
            request = QgsFeatureRequest().setFilterFids(ids)
            features = {f.id(): f for f in layer.getFeatures(request)}
            name_of = feature_labeler(layer)    # the display expression, prepared once for the layer
            for fid in ids:
                feature = features.get(fid)
                if feature is None:
                    continue
                child = QTreeWidgetItem(node, [name_of(feature)])
                child.setData(0, ROLE_KIND, "feature")
                child.setData(0, ROLE_LAYER, layer.id())
                child.setData(0, ROLE_FID, fid)
                if first_feature is None:
                    first_feature = child
                if wanted and wanted[0] == "feature" and wanted[1] == layer.id() and wanted[2] == fid:
                    pick = child
            if count > MAX_TREE_FEATURES:
                more = QTreeWidgetItem(node, ["... {} more (edit the layer to change them all)".format(
                    count - MAX_TREE_FEATURES)])
                more.setFlags(Qt.ItemFlag.NoItemFlags)
            node.setExpanded(True)
        tree.blockSignals(False)
        target = pick or first_feature
        if target is not None:
            tree.setCurrentItem(target)

    def _sync_visible_pages(self):
        if self.tabs.currentIndex() == 0:
            self.top_stack.setCurrentWidget(self.tree if self.tree.topLevelItemCount() else self.hint)
        else:
            self.top_stack.setCurrentWidget(self.browser)

    def _relabel(self):
        """Refresh the feature names in the lists after an attribute changed."""
        self.browser.clear_cache()
        for n in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(n)
            layer = self._watched.get(node.data(0, ROLE_LAYER))
            if layer is None or sip.isdeleted(layer):
                continue
            for k in range(node.childCount()):
                child = node.child(k)
                fid = child.data(0, ROLE_FID)
                if fid is None:
                    continue
                feature = next(layer.getFeatures(QgsFeatureRequest(fid)), None)
                if feature is not None:
                    child.setText(0, feature_label(layer, feature))

    # ------------------------------------------------------------- current target
    def _tab_changed(self, _index):
        self.apply_pending()
        self._sync_visible_pages()
        self._show_current_target()

    def _tree_current_changed(self, _cur, _prev):
        if self._refreshing:
            return
        self.apply_pending()
        self._show_current_target()

    def _layers_current_changed(self):
        if self._refreshing or self.tabs.currentIndex() != 1:
            return
        self.apply_pending()
        self._show_current_target()

    def _stepped(self):
        """The arrows / play moved to another feature: do what the drop-down says on the map."""
        layer, fid = self.browser.current()
        if layer is None or fid is None or not layer.isSpatial():
            return
        action = self.browser.map_action
        if action == "flash":
            self.canvas.flashFeatureIds(layer, [fid])
        elif action == "pan":
            self.canvas.panToFeatureIds(layer, [fid])
        elif action == "zoom":
            self.canvas.zoomToFeatureIds(layer, [fid])

    def _detail_tab_changed(self, index):
        self._show_detail_page()

    def current_target(self):
        """(layer, [fids], kind) for what the details show; layer is None when nothing is chosen."""
        if self.tabs.currentIndex() == 1:
            layer, fid = self.browser.current()
            return (layer, [fid], "feature") if layer is not None and fid is not None else (None, [], None)
        item = self.tree.currentItem()
        if item is None:
            return None, [], None
        layer = self._watched.get(item.data(0, ROLE_LAYER))
        if layer is None or sip.isdeleted(layer):
            return None, [], None
        if item.data(0, ROLE_KIND) == "feature":
            return layer, [item.data(0, ROLE_FID)], "feature"
        return layer, sorted(layer.selectedFeatureIds()), "layer"

    def _show_current_target(self):
        layer, fids, kind = self.current_target()
        self.clear_highlights()
        if layer is None or not fids:
            self.model.clear()
            self._geometry_target = None
            self.geometry.set_target(None, [])
            self.empty.setText("" if layer is None else "This layer has no selected features.")
            self._show_detail_page()
            self._update_notice()
            return
        self.model.set_target(layer, fids)
        # the vertex table (one row per vertex, thousands for a district) is only filled when the
        # Geometry tab is the one on screen; otherwise it waits until the tab is opened
        self.geometry.set_target(layer, fids if kind == "feature" else [],
                                 build=self.detail_tabs.currentIndex() == 1)
        self._show_detail_page()
        self._update_notice()
        self.highlight(layer, fids)

    def _show_detail_page(self):
        layer = self.model.layer
        if layer is None:
            self.detail_stack.setCurrentWidget(self.empty)
        elif self.detail_tabs.currentIndex() == 1:
            self.geometry.ensure_built()
            self.detail_stack.setCurrentWidget(self.geometry)
        else:
            self.detail_stack.setCurrentWidget(self.field_view)

    def _update_notice(self):
        layer = self.model.layer
        text = ""
        if layer is not None and not sip.isdeleted(layer):
            if not layer.isEditable() and not layer.supportsEditing():
                text = "This layer is read-only; its values cannot be changed."
            elif edit_blocked(layer):
                text = "Editing is switched off for this layer (List By Editing); tick it there to change its values."
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    # ------------------------------------------------------------- Auto Apply / Apply / Cancel
    def _auto_apply_toggled(self, on):
        QgsSettings().setValue(SETTING_AUTO_APPLY, bool(on))
        self.model.set_auto_apply(on)
        if on:
            self.apply_pending()
        self._pending_changed()

    def has_pending(self):
        return self.model.has_pending() or self.geometry.has_pending()

    def _pending_changed(self, _pending=False):
        manual = not self.switch.isChecked()
        pending = self.has_pending()
        self.apply_button.setVisible(manual)
        self.cancel_button.setVisible(manual)
        self.apply_button.setEnabled(pending)
        self.cancel_button.setEnabled(pending)

    def apply_pending(self):
        if self.model.has_pending():
            self.model.apply_pending()
        if self.geometry.has_pending():
            self.geometry.apply_pending()
        self._pending_changed()

    def cancel_pending(self):
        self.model.cancel_pending()
        self.geometry.cancel_pending()
        self._pending_changed()

    def set_before_edit(self, func):
        """func(layer) runs just before the pane writes an edit (the KGA Editor turns Edit Session on)."""
        self.model.before_edit = func
        self.geometry.before_edit = func

    def _editing_started(self, layer):
        self.iface.messageBar().pushInfo(
            TITLE, "Editing started on '{}'. Save Edits (KGA Editor) or Save Layer Edits writes your changes."
            .format(layer.name()))
        self._update_notice()

    def _problem(self, message):
        self.iface.mainWindow().statusBar().showMessage("Attributes: " + message, 6000)

    def _values_written(self):
        self._relabel()
        layer = self.model.layer
        if layer is not None:
            layer.triggerRepaint()

    def _show_help(self):
        open_docs(DOCS_NAME)

    # ------------------------------------------------------------- map feedback
    def clear_highlights(self):
        for h in self._highlights:
            with suppress(Exception):
                self.canvas.scene().removeItem(h)
        self._highlights = []

    def highlight(self, layer, fids):
        """Outline the shown feature(s) on the map. Skipped for tables and while the pane is hidden."""
        if not fids or not layer.isSpatial() or not self.isVisible():
            return
        request = QgsFeatureRequest().setFilterFids(list(fids)[:MAX_HIGHLIGHT])
        for feature in layer.getFeatures(request):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            h = QgsHighlight(self.canvas, geometry, layer)
            h.setColor(HIGHLIGHT_COLOR)
            h.setFillColor(QColor(0, 190, 255, 60))
            h.setWidth(3)
            h.show()
            self._highlights.append(h)

    def showEvent(self, event):
        if self._stale:
            self.refresh()                      # the selection changed while the pane was closed
        super().showEvent(event)
        layer, fids, _kind = self.current_target()
        if layer is not None:
            self.clear_highlights()
            self.highlight(layer, fids)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.clear_highlights()
        self.geometry.clear_marker()
        self.apply_pending()

    # ------------------------------------------------------------- context menu
    def contextMenuFor(self, tree, pos):
        item = tree.itemAt(pos)
        if item is None:
            return
        layer = self._watched.get(item.data(0, ROLE_LAYER))
        if layer is None or sip.isdeleted(layer):
            return
        self._feature_menu(tree, pos, layer, item.data(0, ROLE_FID))

    def _browser_menu(self, pos):
        index = self.browser.list.indexAt(pos)
        layer = self.browser.layer()
        if index.isValid() and layer is not None:
            self._feature_menu(self.browser.list, pos, layer, index.data(ROLE_FID))

    def _feature_menu(self, widget, pos, layer, fid):
        """Right-click menu for a feature row (fid) or a layer row (fid None = its selected features)."""
        ids = [fid] if fid is not None else sorted(layer.selectedFeatureIds())
        if not ids or not layer.isSpatial():
            return
        menu = QMenu(widget)
        menu.addAction("Flash").triggered.connect(lambda: self.canvas.flashFeatureIds(layer, ids))
        menu.addAction("Zoom To").triggered.connect(lambda: self.canvas.zoomToFeatureIds(layer, ids))
        menu.addAction("Pan To").triggered.connect(lambda: self.canvas.panToFeatureIds(layer, ids))
        menu.addSeparator()
        if fid is not None:
            if fid in layer.selectedFeatureIds():
                menu.addAction("Clear").triggered.connect(lambda: layer.deselect(fid))
            else:
                menu.addAction("Add to selection").triggered.connect(lambda: layer.select(fid))
            menu.addAction("Select only this").triggered.connect(lambda: self.select_only(layer, fid))
        else:
            menu.addAction("Clear").triggered.connect(layer.removeSelection)
        viewport = widget.viewport() if hasattr(widget, "viewport") else widget
        menu.exec(viewport.mapToGlobal(pos))

    def show_feature(self, layer, fid):
        """Open the pane on one feature (Layers tab, list scrolled to it) without touching the selection."""
        self.show()
        self.raise_()
        self.tabs.setCurrentIndex(1)
        self.browser.set_layers(layer_tree_layers())
        self.browser.choose_layer(layer)
        self.browser.choose_feature(fid)

    def select_only(self, layer, fid):
        for other in layer_tree_layers():
            if other.selectedFeatureCount():
                other.removeSelection()
        layer.selectByIds([fid])

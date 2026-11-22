"""Layer Auto-Sort: ArcGIS Pro style ordering of the Layers panel.

Owns the Sort Layers by Type action, the Auto-sort New Layers switch, the
Settings entry and the Layers panel context menu item.

New layers are caught on the layer tree itself, not on QgsProject's
legendLayersAdded: Processing results, Add Open Data and most plugins add a
layer with addMapLayer(layer, False) and then insert the node themselves, which
never fires legendLayersAdded. A layer is "new" the first time its id appears in
the tree; moves (drag and drop, our own sorting) re-insert ids already known.
Opening a project is skipped, so a saved order is never changed.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsApplication, QgsLayerTree, QgsProject
from qgis.PyQt.QtCore import QTimer

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

from ..branding import icon as _icon
from ..core.layer_sort import (
    Ranker, is_paused, load_settings, place_layer, save_setting,
    sort_custom_order, sort_group,
)
from .layer_sort_dialog import SettingsDialog


class LayerSortController:

    def __init__(self, iface):
        self.iface = iface
        self.sort_action = None
        self.auto_action = None
        self.settings_action = None
        self._busy = False
        self._connected = False
        self._known = set()
        self._epoch = 0

    def initGui(self):
        win = self.iface.mainWindow()

        self.sort_action = QAction(_icon('data_management/sortLayer.png'),
                                   'Sort Layers by Type', win)
        self.sort_action.setToolTip(
            'Sort Layers by Type - points > lines > polygons > rasters')
        self.sort_action.triggered.connect(lambda *_: self.sort_all())

        self.auto_action = QAction('Auto-sort New Layers', win)
        self.auto_action.setCheckable(True)
        self.auto_action.setChecked(load_settings()['auto_sort'])
        self.auto_action.toggled.connect(
            lambda on: save_setting('autoSort', on))

        self.settings_action = QAction(
            QgsApplication.getThemeIcon('/mActionOptions.svg'),
            'Layer Sort Settings…', win)
        self.settings_action.triggered.connect(lambda *_: self.open_settings())

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        self._known = set(root.findLayerIds())
        root.addedChildren.connect(self._on_tree_added)
        root.removedChildren.connect(self._resync_known)
        project.cleared.connect(self._on_project_reset)
        project.readProject.connect(self._on_project_reset)
        self.iface.layerTreeView().contextMenuAboutToShow.connect(
            self._extend_context_menu)
        self._connected = True

    def unload(self):
        if self._connected:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            for signal, slot in (
                (root.addedChildren, self._on_tree_added),
                (root.removedChildren, self._resync_known),
                (project.cleared, self._on_project_reset),
                (project.readProject, self._on_project_reset),
                (self.iface.layerTreeView().contextMenuAboutToShow,
                 self._extend_context_menu),
            ):
                with suppress(TypeError, RuntimeError):
                    signal.disconnect(slot)
            self._connected = False
        for action in (self.sort_action, self.auto_action,
                       self.settings_action):
            if action is not None:
                action.deleteLater()
        self.sort_action = self.auto_action = self.settings_action = None

    # --------------------------------------------------------------- actions
    def _ranker(self):
        return Ranker(load_settings()['order'])

    def _run_keeping_selection(self, func):
        """Run a tree change without re-triggering ourselves, then restore
        the selected layer (moved nodes are replaced by clones)."""
        current = self.iface.activeLayer()
        self._busy = True
        try:
            return func()
        finally:
            self._busy = False
            if current is not None:
                self.iface.layerTreeView().setCurrentLayer(current)

    def sort_all(self, group=None):
        root = QgsProject.instance().layerTreeRoot()
        ranker = self._ranker()
        sync = load_settings()['sync_layer_order']

        def work():
            moved = sort_group(group or root, ranker, recursive=True)
            if sync and sort_custom_order(root, ranker):
                moved += 1
            return moved

        moved = self._run_keeping_selection(work)
        text = ('Layers sorted by type.' if moved
                else 'Layers were already in order.')
        self.iface.messageBar().pushMessage(
            'KGA Toolbox', text, Qgis.MessageLevel.Success, 3)

    def open_settings(self):
        dlg = SettingsDialog(self.iface.mainWindow())
        if dlg.exec():
            self.auto_action.setChecked(load_settings()['auto_sort'])

    # ------------------------------------------------- automatic placement
    def _resync_known(self, *_):
        self._known = set(QgsProject.instance().layerTreeRoot().findLayerIds())

    def _on_project_reset(self, *_):
        # Opening or clearing a project: whatever it put in the tree is "saved",
        # and anything scheduled before it is stale.
        self._epoch += 1
        self._resync_known()

    @staticmethod
    def _ids_under(node):
        if QgsLayerTree.isLayer(node):
            return [node.layerId()]
        if QgsLayerTree.isGroup(node):
            return list(node.findLayerIds())
        return []

    def _on_tree_added(self, node, index_from, index_to):
        added = []
        for child in node.children()[index_from:index_to + 1]:
            added.extend(self._ids_under(child))
        new = [i for i in added if i not in self._known]
        self._known.update(added)
        if (not new or self._busy or is_paused()
                or not load_settings()['auto_sort']):
            return
        epoch = self._epoch
        # wait one event-loop turn: the caller may still be filling the tree
        QTimer.singleShot(0, lambda: self._place_new(new, epoch))

    def _place_new(self, ids, epoch):
        if epoch != self._epoch:
            return                  # a project was opened in between
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        layers = [project.mapLayer(i) for i in ids]
        layers = [lyr for lyr in layers if lyr is not None]
        if not layers:
            return
        ranker = self._ranker()
        sync = load_settings()['sync_layer_order']

        def work():
            # lowest band first, so a multi-layer drop ends up correctly stacked
            for layer in sorted(layers, key=ranker, reverse=True):
                place_layer(root, layer, ranker)
            if sync:
                sort_custom_order(root, ranker, new_ids=ids)

        self._run_keeping_selection(work)

    # ---------------------------------------------------------- context menu
    def _extend_context_menu(self, menu):
        if self.sort_action is None:
            return
        node = self.iface.layerTreeView().currentNode()
        menu.addSeparator()
        if node is not None and QgsLayerTree.isGroup(node):
            act = menu.addAction(self.sort_action.icon(), 'Sort Group by Type')
            act.triggered.connect(lambda *_, n=node: self.sort_all(group=n))
        menu.addAction(self.sort_action)

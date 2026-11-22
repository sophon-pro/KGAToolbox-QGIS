# -*- coding: utf-8 -*-
"""Tracks the current layer of the Layers panel and the signals of that one layer.

Only the active layer is ever connected. When the user picks another layer every
connection made to the previous one is removed, so repeated switching never
stacks duplicate handlers and no reference to an old layer is kept.

Rapid switching (arrow keys in the Layers panel) is coalesced: the panel is
rebuilt once, a few milliseconds after the selection settles.
"""

from contextlib import suppress

from qgis.core import QgsMapLayer, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal


class LayerWatcher(QObject):
    # emitted with the new current layer (QgsMapLayer) or None
    layerChanged = pyqtSignal(object)
    # emitted for the *current* layer only
    nameChanged = pyqtSignal()
    selectionChanged = pyqtSignal()
    styleChanged = pyqtSignal()      # renderer or labeling changed outside this plugin
    fieldsChanged = pyqtSignal()
    dataChanged = pyqtSignal()       # features added/removed, edits committed

    SWITCH_DELAY_MS = 25
    SELECTION_DELAY_MS = 60
    STYLE_DELAY_MS = 80

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._layer = None
        self._layer_id = None
        self._connections = []
        self._own_changes = 0          # >0 while this plugin is modifying the layer
        self._pending = None
        self._has_pending = False
        self._started = False

        self._switch_timer = self._timer(self.SWITCH_DELAY_MS, self._apply_pending)
        self._sel_timer = self._timer(self.SELECTION_DELAY_MS, self.selectionChanged.emit)
        self._style_timer = self._timer(self.STYLE_DELAY_MS, self.styleChanged.emit)
        self._data_timer = self._timer(150, self.dataChanged.emit)

        # metrics for the test-suite / benchmarks
        self.switch_count = 0

    def _timer(self, ms, slot):
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(slot)
        return t

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._started:
            return
        self._started = True
        view = self.iface.layerTreeView()
        if view is not None and hasattr(view, "currentLayerChanged"):
            view.currentLayerChanged.connect(self._on_current_changed)
            self._view = view
        else:  # very old / unusual hosts
            self.iface.currentLayerChanged.connect(self._on_current_changed)
            self._view = None
        prj = QgsProject.instance()
        prj.cleared.connect(self._on_project_cleared)
        prj.readProject.connect(self._on_project_read)
        self._on_current_changed(self.iface.activeLayer())
        self._apply_pending()

    def stop(self):
        if not self._started:
            return
        self._started = False
        for t in (self._switch_timer, self._sel_timer, self._style_timer, self._data_timer):
            t.stop()
        with suppress(TypeError, RuntimeError):
            if self._view is not None:
                self._view.currentLayerChanged.disconnect(self._on_current_changed)
            else:
                self.iface.currentLayerChanged.disconnect(self._on_current_changed)
        prj = QgsProject.instance()
        for sig, slot in ((prj.cleared, self._on_project_cleared), (prj.readProject, self._on_project_read)):
            with suppress(TypeError, RuntimeError):
                sig.disconnect(slot)
        self._disconnect_layer()
        self._layer = None
        self._layer_id = None

    # ------------------------------------------------------------------ public
    def layer(self):
        """The current layer, or None. Never returns a deleted layer."""
        if self._layer is None:
            return None
        if self._layer_id and QgsProject.instance().mapLayer(self._layer_id) is None:
            # layer removed from the project but deletion signal not delivered yet
            return None
        return self._layer

    def vector_layer(self):
        lyr = self.layer()
        return lyr if isinstance(lyr, QgsVectorLayer) else None

    def connection_count(self):
        return len(self._connections)

    def begin_own_change(self):
        """Call around changes the plugin makes, so they are not echoed back."""
        self._own_changes += 1

    def end_own_change(self):
        self._own_changes = max(0, self._own_changes - 1)
        # drop the style notification our own change produced
        self._style_timer.stop()

    def own_change(self):
        watcher = self

        class _Guard(object):
            def __enter__(self):
                watcher.begin_own_change()

            def __exit__(self, *exc):
                watcher.end_own_change()
                return False
        return _Guard()

    def refresh(self):
        """Re-announce the current layer (e.g. after the dock is shown again)."""
        self.layerChanged.emit(self.layer())

    # ------------------------------------------------------------------ slots
    def _on_current_changed(self, layer):
        self._pending = layer
        self._has_pending = True
        self._switch_timer.start()

    def _apply_pending(self):
        self._switch_timer.stop()
        if not self._has_pending:
            return
        layer = self._pending
        self._pending = None
        self._has_pending = False
        if layer is not None and not isinstance(layer, QgsMapLayer):
            layer = None
        if layer is self._layer and layer is not None:
            return
        self._disconnect_layer()
        self._layer = layer
        self._layer_id = layer.id() if layer is not None else None
        if layer is not None:
            self._connect_layer(layer)
        self.switch_count += 1
        self.layerChanged.emit(layer)

    def _on_project_cleared(self):
        self._pending = None
        self._has_pending = False
        self._switch_timer.stop()
        if self._layer is not None:
            self._disconnect_layer()
            self._layer = None
            self._layer_id = None
            self.layerChanged.emit(None)

    def _on_project_read(self, *_args):
        self._on_current_changed(self.iface.activeLayer())

    def _on_layer_deleted(self):
        self._disconnect_layer(deleted=True)
        self._layer = None
        self._layer_id = None
        self.layerChanged.emit(None)

    def _on_style(self, *_args):
        if self._own_changes:
            return
        self._style_timer.start()

    # ------------------------------------------------------------------ per-layer connections
    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _connect_layer(self, layer):
        self._connect(layer.willBeDeleted, self._on_layer_deleted)
        self._connect(layer.nameChanged, self.nameChanged.emit)
        self._connect(layer.styleChanged, self._on_style)
        if hasattr(layer, "rendererChanged"):
            self._connect(layer.rendererChanged, self._on_style)
        if isinstance(layer, QgsVectorLayer):
            self._connect(layer.selectionChanged, lambda *a: self._sel_timer.start())
            self._connect(layer.updatedFields, self.fieldsChanged.emit)
            self._connect(layer.featureAdded, lambda *a: self._data_timer.start())
            self._connect(layer.featuresDeleted, lambda *a: self._data_timer.start())
            self._connect(layer.afterCommitChanges, lambda *a: self._data_timer.start())
            self._connect(layer.afterRollBack, lambda *a: self._data_timer.start())
            self._connect(layer.editingStarted, self.nameChanged.emit)
            self._connect(layer.editingStopped, self.nameChanged.emit)
            self._connect(layer.dataSourceChanged, lambda *a: self._data_timer.start())

    def _disconnect_layer(self, deleted=False):
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass  # object already gone
        self._connections = []
        for t in (self._sel_timer, self._style_timer, self._data_timer):
            t.stop()

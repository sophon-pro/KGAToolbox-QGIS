# -*- coding: utf-8 -*-
"""The vertices and nodes view: switch, scheduling and the background worker.

On/off comes from three places that all end in `set_enabled`: the status-bar button, the map's
right-click menu ("Enable / Disable Vertices and Node View") and Settings > Editor. The state is the
``KGA/settings/node_view`` setting, so it survives a restart.

While it is on, the map is described once for a rectangle a quarter bigger than the view on every side
(see `engine.compute`), in a Python thread (feature sources can be read from any thread), and the overlay
shows the finished arrays. Panning inside that rectangle, or zooming a little, costs nothing but a repaint;
leaving it (or zooming a lot, or editing, or changing layers) schedules a new run after a short quiet
period, and a run that is overtaken by a newer view is cancelled. The old markers stay on screen until the
new ones arrive, so nothing flickers.
"""

import threading
import traceback
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
    QgsWkbTypes,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ...core.compat import scoped
from ...branding import LOG_TAG, icon
from ...core import kga_settings as ks
from .. import status_buttons
from . import engine
from .overlay import NodeOverlay, Style

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

TEXT_ON = 'Enable Vertices and Node View'
TEXT_OFF = 'Disable Vertices and Node View'
TOOLTIP = ('Show the vertices, nodes and midpoints of the lines and polygons on the map, like ArcGIS Pro.\n'
           'Squares are vertices; magenta circles are nodes where features meet or cross.\n'
           'Change the options in KGA Toolbox > Settings > Editor.')
ICON = 'interface/nodeView.png'
#: The rectangle that is described is the view grown by this factor (1.5 = a quarter on every side).
GROW = 1.5
#: Quiet time after the view changed before a new run starts.
VIEW_DELAY_MS = 140
DATA_DELAY_MS = 70
#: The picture of the last run is kept while the scale stays within these ratios of the one it was made for.
SCALE_LOW = 0.7
SCALE_HIGH = 1.5


class _Bridge(QObject):
    """Carries a finished run from the worker thread to the GUI thread (queued signal)."""

    done = pyqtSignal(object)


def _line_or_polygon(layer):
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return False
    kind = layer.geometryType()
    return kind in (QgsWkbTypes.GeometryType.LineGeometry, QgsWkbTypes.GeometryType.PolygonGeometry)


def _reverse():
    try:
        return Qgis.TransformDirection.Reverse
    except AttributeError:      # QGIS < 3.22
        return scoped(QgsCoordinateTransform, 'TransformDirection', 'ReverseTransform')


class NodeViewController(QObject):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.action = None
        self.status_button = None
        self.overlay = None
        self.result = None
        self._installed = False
        self._busy = False
        self._again = False
        self._dirty = True
        self._cancel = threading.Event()
        self._bridge = _Bridge()
        self._bridge.done.connect(self._on_done)
        self._watched = {}                  # layer id -> layer whose edits are followed
        self._failed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._kick)

    # ------------------------------------------------------------------ lifecycle
    def install(self):
        """Create the action (its button is in the status bar) and follow the canvas; the view starts if the setting is on."""
        if self._installed:
            return
        self._installed = True
        self.action = QAction(icon(ICON), 'Vertices and Nodes', self)
        self.action.setCheckable(True)
        self.action.setObjectName('mActionKgaNodeView')
        self.action.setToolTip(TOOLTIP)
        self.action.setStatusTip('Show or hide vertices and nodes on the map')
        self.action.triggered.connect(self._action_triggered)
        self.canvas.contextMenuAboutToShow.connect(self._canvas_menu)
        self.canvas.extentsChanged.connect(self._view_changed)
        self.canvas.layersChanged.connect(self._layers_changed)
        with suppress(Exception):
            self.iface.currentLayerChanged.connect(self._current_layer_changed)
        self._add_status_button()
        with suppress(Exception):               # the status bar may not be finished yet at start-up
            self.iface.initializationCompleted.connect(self._add_status_button)
        self.refresh()

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        self._timer.stop()
        self._cancel.set()
        for signal, slot in ((self.canvas.contextMenuAboutToShow, self._canvas_menu),
                             (self.canvas.extentsChanged, self._view_changed),
                             (self.canvas.layersChanged, self._layers_changed),
                             (self.iface.currentLayerChanged, self._current_layer_changed)):
            with suppress(Exception):
                signal.disconnect(slot)
        self._unwatch_all()
        self._remove_overlay()
        self._remove_status_button()
        with suppress(Exception):
            self.iface.initializationCompleted.disconnect(self._add_status_button)
        if self.action is not None:
            with suppress(RuntimeError):
                self.action.deleteLater()
        self.action = None

    # ------------------------------------------------------------------ the switch
    @property
    def enabled(self):
        return ks.node_view_enabled()

    def set_enabled(self, on):
        ks.set_value(ks.KEY_NODE_VIEW, bool(on))
        self.refresh()

    def toggle(self):
        self.set_enabled(not self.enabled)

    def _action_triggered(self, checked=False):
        self.set_enabled(checked)

    def refresh(self, *_args):
        """Follow the settings: switch on / off, new colours, sizes, layers."""
        if not self._installed:
            return
        try:
            on = self.enabled
            if self.action is not None:
                self.action.blockSignals(True)
                self.action.setChecked(on)
                self.action.blockSignals(False)
            if on:
                if self.overlay is None:
                    self.overlay = NodeOverlay(self.canvas)
                self.overlay.set_style(self._style())
                self._watch_layers()
                self._dirty = True
                self._timer.start(0)
            else:
                self._timer.stop()
                self._cancel.set()
                self._again = False
                self._unwatch_all()
                self._remove_overlay()
                self.result = None
        except RuntimeError:                # the plugin was unloaded meanwhile
            pass

    @staticmethod
    def _style():
        return Style(ks.node_view_vertex_color(), ks.node_view_node_color(), ks.node_view_midpoint_color(),
                     ks.node_view_vertex_size(), ks.node_view_node_size())

    def _remove_overlay(self):
        if self.overlay is not None:
            with suppress(Exception):
                self.canvas.scene().removeItem(self.overlay)
            self.overlay = None

    # ------------------------------------------------------------------ status bar button
    def _add_status_button(self, *_args):
        """The same switch as a button in the status bar, right beside the coordinate box."""
        if self.status_button is None and self.action is not None:
            self.status_button = status_buttons.add_button(self.iface, self.action, 'kgaNodeViewStatusButton')

    def _remove_status_button(self):
        button, self.status_button = self.status_button, None
        status_buttons.remove_button(self.iface, button)

    # ------------------------------------------------------------------ right-click menu
    def _canvas_menu(self, menu, _event):
        with suppress(Exception):               # never break the map's menu over this
            on = self.enabled
            action = QAction(icon(ICON), TEXT_OFF if on else TEXT_ON, menu)
            action.setObjectName('kgaNodeViewMenuAction')
            action.setToolTip(TOOLTIP)
            action.triggered.connect(lambda _c=False: self.set_enabled(not on))
            menu.addSeparator()
            menu.addAction(action)

    # ------------------------------------------------------------------ what changes the picture
    def _active(self):
        return self._installed and self.overlay is not None and not sip.isdeleted(self.overlay)

    def _view_changed(self, *_args):
        if not self._active():
            return
        self.overlay.update()
        if self._dirty or not self._covers():
            self._timer.start(VIEW_DELAY_MS)

    def _layers_changed(self, *_args):
        if not self._active():
            return
        self._watch_layers()
        self._mark_dirty()

    def _current_layer_changed(self, *_args):
        if self._active() and ks.node_view_scope() == ks.NODE_SCOPE_ACTIVE:
            self._mark_dirty()

    def _layer_edited(self, *_args):
        if self._active():
            self._mark_dirty()

    def _mark_dirty(self):
        self._dirty = True
        self._timer.start(DATA_DELAY_MS)

    def _covers(self):
        """True while the last result still describes the view."""
        result = self.result
        if result is None:
            return False
        extent = self.canvas.mapSettings().visibleExtent()
        x0, y0, x1, y1 = result.rect
        if (extent.xMinimum() < x0 or extent.yMinimum() < y0 or extent.xMaximum() > x1
                or extent.yMaximum() > y1):
            return False
        ratio = self.canvas.mapSettings().mapUnitsPerPixel() / result.mupp
        return SCALE_LOW <= ratio <= SCALE_HIGH

    def _watch_layers(self):
        """Follow edits of the layers on the map (a moved vertex moves its marker)."""
        current = {}
        for layer in self.canvas.layers():
            if _line_or_polygon(layer):
                current[layer.id()] = layer
        for layer_id in [k for k in self._watched if k not in current]:
            layer = self._watched.pop(layer_id)
            self._disconnect(layer)
        for layer_id, layer in current.items():
            if layer_id not in self._watched:
                for signal in self._signals(layer):
                    signal.connect(self._layer_edited)
                self._watched[layer_id] = layer

    @staticmethod
    def _signals(layer):
        return (layer.layerModified, layer.editingStarted, layer.editingStopped)

    def _disconnect(self, layer):
        if sip.isdeleted(layer):
            return
        for signal in self._signals(layer):
            with suppress(Exception):
                signal.disconnect(self._layer_edited)

    def _unwatch_all(self):
        for layer in self._watched.values():
            self._disconnect(layer)
        self._watched = {}

    # ------------------------------------------------------------------ the run
    def _scope_layers(self):
        scope = ks.node_view_scope()
        layers = [layer for layer in self.canvas.layers() if _line_or_polygon(layer)]
        if scope == ks.NODE_SCOPE_ACTIVE:
            active = self.iface.activeLayer()
            return [layer for layer in layers if active is not None and layer.id() == active.id()]
        if scope == ks.NODE_SCOPE_EDITABLE:
            return [layer for layer in layers if layer.isEditable()]
        return layers

    @staticmethod
    def _layer_rect(transform, rect):
        """The map rectangle in the layer's CRS, or None when it cannot be converted."""
        try:
            return transform.transformBoundingBox(rect, _reverse())
        except Exception:
            return None

    def _build_request(self):
        settings = self.canvas.mapSettings()
        scale = self.canvas.scale()
        rect = QgsRectangle(settings.visibleExtent())
        rect.scale(GROW)
        destination = settings.destinationCrs()
        project = QgsProject.instance()
        layers = []
        for layer in self._scope_layers():
            if layer.hasScaleBasedVisibility() and not layer.isInScaleRange(scale):
                continue
            transform = None
            layer_rect = rect
            if layer.crs() != destination:
                transform = QgsCoordinateTransform(layer.crs(), destination, project)
                layer_rect = self._layer_rect(transform, rect)
                if layer_rect is None:
                    continue
            with suppress(Exception):
                if not layer.extent().intersects(layer_rect):
                    continue
            layers.append((QgsVectorLayerFeatureSource(layer),
                           (layer_rect.xMinimum(), layer_rect.yMinimum(), layer_rect.xMaximum(),
                            layer_rect.yMaximum()), transform))
        return engine.Request(
            layers, (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()),
            settings.mapUnitsPerPixel(), vertices=ks.node_view_vertices(), nodes=ks.node_view_nodes(),
            midpoints=ks.node_view_midpoints(), max_vertices=ks.node_view_max_vertices(),
            vertex_px=ks.node_view_vertex_size(), node_px=ks.node_view_node_size())

    def _kick(self):
        """Start a run for the view as it is now (or make the running one start over)."""
        try:
            if not self._active():
                return
            if self._busy:
                self._again = True
                self._cancel.set()
                return
            self._dirty = False
            request = self._build_request()
            if not request.layers or not (request.vertices or request.nodes or request.midpoints):
                self.result = engine.Result(request)
                self.overlay.set_result(self.result)
                return
            self._busy = True
            self._again = False
            cancel = self._cancel = threading.Event()
            thread = threading.Thread(target=self._work, args=(request, cancel), daemon=True,
                                      name='KGA-node-view')
            thread.start()
        except Exception:
            self._busy = False
            self._log()

    def _work(self, request, cancel):
        """The worker thread: no Qt widget, no map canvas, only the feature sources of the request."""
        payload = None
        try:
            payload = engine.compute(request, cancel.is_set)
        except engine.Stop:
            payload = None
        except Exception:
            payload = engine.Result(request)
            payload.error = traceback.format_exc()
        with suppress(RuntimeError):            # the controller may be gone by now
            self._bridge.done.emit(payload)

    def _on_done(self, result):
        self._busy = False
        try:
            if not self._active():
                return
            if result is not None and not self._again:
                if result.error:
                    self._log(result.error)
                else:
                    self.result = result
                    self.overlay.set_result(result)
            if self._again or self._dirty:
                self._again = False
                self._timer.start(0)
        except Exception:
            self._log()

    def _log(self, text=None):
        if self._failed and text is None:
            return
        self._failed = True
        QgsMessageLog.logMessage('Vertices and nodes view failed:\n{}'.format(text or traceback.format_exc()),
                                 LOG_TAG, Qgis.MessageLevel.Warning)

# -*- coding: utf-8 -*-
"""Geometry info in the status bar: length / perimeter / area of the selected feature(s).

Shown to the left of the Vertices and Nodes button while features of exactly one layer are selected;
hidden when nothing is selected or when the selection spans several layers.
"""

from contextlib import suppress

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtWidgets import QLabel

from .. import status_buttons
from . import engine

NAME = 'kgaGeometryInfoStatusLabel'


class GeometryInfoController(QObject):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.label = None
        self._installed = False
        self._watched = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self.update)

    def install(self):
        if self._installed:
            return
        self._installed = True
        project = QgsProject.instance()
        project.layersAdded.connect(self._layers_added)
        project.layerWillBeRemoved.connect(self._layer_removed)
        with suppress(Exception):
            project.ellipsoidChanged.connect(self._schedule)
        self._layers_added(list(project.mapLayers().values()))
        self._add_label()
        with suppress(Exception):               # the status bar may not be finished yet at start-up
            self.iface.initializationCompleted.connect(self._add_label)
        self.update()

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        self._timer.stop()
        project = QgsProject.instance()
        for signal, slot in ((project.layersAdded, self._layers_added),
                             (project.layerWillBeRemoved, self._layer_removed),
                             (project.ellipsoidChanged, self._schedule),
                             (self.iface.initializationCompleted, self._add_label)):
            with suppress(Exception):
                signal.disconnect(slot)
        for layer in list(self._watched.values()):
            self._unwatch(layer)
        self._watched.clear()
        label, self.label = self.label, None
        if label is not None and not sip.isdeleted(label):
            label.setParent(None)
            label.deleteLater()

    # ------------------------------------------------------------------ label
    def _add_label(self, *_args):
        if self.label is not None:
            return
        label = QLabel()
        label.setObjectName(NAME)
        label.setContentsMargins(6, 0, 6, 0)
        label.hide()
        if status_buttons.add_label(self.iface, label) is None:
            label.deleteLater()
            return
        self.label = label
        self.update()

    # ------------------------------------------------------------------ following the selection
    def _layers_added(self, layers):
        for layer in layers:
            if isinstance(layer, QgsVectorLayer) and layer.id() not in self._watched:
                self._watched[layer.id()] = layer
                layer.selectionChanged.connect(self._schedule)
                layer.geometryChanged.connect(self._schedule)
                layer.editingStopped.connect(self._schedule)

    def _layer_removed(self, layer_id):
        layer = self._watched.pop(layer_id, None)
        if layer is not None:
            self._unwatch(layer)
        self._schedule()

    def _unwatch(self, layer):
        if sip.isdeleted(layer):
            return
        for signal in (layer.selectionChanged, layer.geometryChanged, layer.editingStopped):
            with suppress(Exception):
                signal.disconnect(self._schedule)

    def _schedule(self, *_args):
        if self._installed:
            self._timer.start()

    # ------------------------------------------------------------------ what is shown
    def update(self):
        if not self._installed or self.label is None or sip.isdeleted(self.label):
            return
        text = tip = ''
        with suppress(Exception):
            layer = engine.selected_layer()
            result = engine.measure(layer) if layer is not None else None
            if result is not None:
                text, tip = engine.describe(result)
        self.label.setText(text)
        self.label.setToolTip(tip)
        self.label.setVisible(bool(text))

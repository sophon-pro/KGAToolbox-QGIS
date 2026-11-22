# -*- coding: utf-8 -*-
"""A tick-box list of layers: the project's, plus any browsed to on disk.

Shared by the layer package windows. The list follows the Layers panel, keeps
its ticks when layers come and go, and lets files that are not in the project
be added with Browse.
"""

import os

from qgis.core import (Qgis, QgsProject, QgsProviderRegistry,
                       QgsProviderSublayerDetails, QgsRasterLayer,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget)

#: Item data: a project layer's id, or "file:<n>" for one browsed to.
KEY_ROLE = Qt.ItemDataRole.UserRole
FILE_PREFIX = 'file:'


def tr(text):
    return text


def packable(layer):
    return isinstance(layer, (QgsVectorLayer, QgsRasterLayer)) and layer.isValid()


class ProjectLayerChecklist(QWidget):
    """Emits `changed` when the ticks or the list change, `message` for text."""

    changed = pyqtSignal()
    message = pyqtSignal(str, bool)

    def __init__(self, label, parent=None):
        super().__init__(parent)
        # Layers browsed to, by item key. Kept here, not in the item: a Qt item
        # hands a stored QGIS object back downcast, which loses the subclass.
        self._files = {}
        self._counter = 0
        self._tick_next = set()         # browsed layers, ticked on arrival

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(QLabel(label, self))
        self.list = QListWidget(self)
        self.list.setToolTip(tr(
            'Tick the layers to use. Layers from the project are listed; use '
            'Browse to add files that are not in the project.'))
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        outer.addWidget(self.list, 1)

        tools = QHBoxLayout()
        for text, state in ((tr('Select all'), True), (tr('Clear'), False)):
            button = QPushButton(text, self)
            button.clicked.connect(
                lambda _checked=False, s=state: self._tick_all(s))
            tools.addWidget(button)
        tools.addStretch(1)
        browse = QPushButton(tr('Browse...'), self)
        browse.setToolTip(tr(
            'Add layers from files on disk that are not in the project.'))
        browse.clicked.connect(self._browse_layers)
        tools.addWidget(browse)
        outer.addLayout(tools)

        project = QgsProject.instance()
        project.layersAdded.connect(self.repopulate)
        project.layersRemoved.connect(self.repopulate)
        self.repopulate()

    def disconnect_project(self):
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(self.repopulate)
            except (TypeError, RuntimeError):
                pass

    # ------------------------------------------------------------------ list

    def _checked_keys(self):
        return {self.list.item(i).data(KEY_ROLE)
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked}

    def repopulate(self, *_args):
        """Rebuild the list: project layers in panel order, then browsed ones."""
        ticked = self._checked_keys() | self._tick_next
        self._tick_next = set()
        self.list.blockSignals(True)
        self.list.clear()
        project = QgsProject.instance()
        for layer in project.layerTreeRoot().layerOrder():
            if packable(layer):
                self._add_item(layer, layer.id(), ticked)
        for key, layer in self._files.items():
            self._add_item(layer, key, ticked)
        self.list.blockSignals(False)
        self.changed.emit()

    def _add_item(self, layer, key, ticked):
        kind = tr('raster') if isinstance(layer, QgsRasterLayer) else tr('vector')
        item = QListWidgetItem('{}  ({})'.format(layer.name(), kind))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(KEY_ROLE, key)
        item.setToolTip(layer.source())
        item.setCheckState(Qt.CheckState.Checked if key in ticked
                           else Qt.CheckState.Unchecked)
        self.list.addItem(item)

    def _tick_all(self, state):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(
                Qt.CheckState.Checked if state else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self.changed.emit()

    def layers(self):
        """The ticked layers, in list order."""
        found = []
        project = QgsProject.instance()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            key = item.data(KEY_ROLE)
            layer = (self._files.get(key) if key.startswith(FILE_PREFIX)
                     else project.mapLayer(key))
            if packable(layer):
                found.append(layer)
        return found

    # ---------------------------------------------------------------- browse

    def _browse_layers(self):
        registry = QgsProviderRegistry.instance()
        filters = ';;'.join([
            tr('All supported (*.*)'),
            registry.fileVectorFilters(),
            registry.fileRasterFilters(),
        ])
        files, _filter = QFileDialog.getOpenFileNames(
            self, tr('Add layers from files'), '', filters)
        if not files:
            return

        context = QgsProject.instance().transformContext()
        options = QgsProviderSublayerDetails.LayerOptions(context)
        added = 0
        for path in files:
            details = [d for d in registry.querySublayers(path)
                       if d.type() in (Qgis.LayerType.Vector,
                                       Qgis.LayerType.Raster)]
            if not details:
                self.message.emit(tr('"{}" holds no layer QGIS can read.').format(
                    os.path.basename(path)), True)
                continue
            for detail in details:
                layer = detail.toLayer(options)
                if not packable(layer):
                    continue
                self._counter += 1
                key = '{}{}'.format(FILE_PREFIX, self._counter)
                self._files[key] = layer
                self._tick_next.add(key)
                added += 1
        self.repopulate()
        if added:
            self.message.emit(tr('{} layer(s) added to the list.').format(added),
                              False)

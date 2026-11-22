# -*- coding: utf-8 -*-
"""List by Data Source: the Layers panel grouped by where each layer's data lives.

Like ArcGIS Pro's "List by Data Source": one heading per database, folder or
service, with the layers that come from it underneath. A heading shows only the
database (with its extension, e.g. Training.gdb) or the folder name; a second
button reveals the full path. The panel's own tree is hidden, not replaced, so
switching back restores it exactly as it was.

Rows: a tick box for the layer's visibility, then the layer's legend swatches.
While Map Tabs is on, only the layers of the active map are listed.
"""

import os
import re
from contextlib import suppress
from urllib.parse import urlparse

from qgis.core import QgsApplication, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QFont, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDockWidget, QMenu, QToolBar, QTreeWidget, QTreeWidgetItem,
)

from ...branding import icon as branding_icon
from ...data_ingest.compat import QAction
from .layer_inspector import active_map_layer_ids
from .utils import (
    IS_WINDOWS, KIND_DATABASE, KIND_DIRECTORY, KIND_FILE, KIND_MEMORY, KIND_REMOTE,
    classify_provider, clean_path, decode_source,
)

_ICONS = os.path.join(os.path.dirname(__file__), 'icons')
SETTING_BY_SOURCE = 'KGA/layerList/bySource'
SETTING_FULL_PATH = 'KGA/layerList/fullPath'

#: Files that hold many layers: the heading is the file itself, not its folder.
CONTAINERS = {'.gpkg', '.gdb', '.sqlite', '.db', '.mdb', '.accdb', '.kml', '.kmz', '.zip', '.mbtiles',
              '.sde', '.gpx', '.gml', '.dxf', '.dwg', '.osm', '.pbf', '.xlsx', '.xls', '.ods', '.nc',
              '.hdf', '.h5'}

ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_ID = Qt.ItemDataRole.UserRole + 1
ROLE_KEY = Qt.ItemDataRole.UserRole + 2
KIND_HEAD = 'head'
KIND_LAYER = 'layer'
KIND_SWATCH = 'swatch'


def _icon(name):
    """An icon by name: 'theme:mGeoPackage' is QGIS's own, '*.png' a KGA toolbox icon, else ours."""
    if name.startswith('theme:'):
        return QgsApplication.getThemeIcon('/{}.svg'.format(name[6:]))
    if name.endswith('.png'):
        return branding_icon(name)
    return QIcon(os.path.join(_ICONS, name))


#: The icon of each file type, as QGIS's Browser or Pro show it.
FILE_ICONS = {
    '.gpkg': 'theme:mGeoPackage', '.gdb': 'gdb.svg', '.kgalp': 'data_management/layerPackage.png',
    '.sqlite': 'theme:mIconSpatialite', '.db': 'theme:mIconSpatialite',
    '.kml': 'kml.svg', '.kmz': 'kml.svg', '.gpx': 'theme:mIconGpx', '.zip': 'theme:mIconZip',
    '.csv': 'theme:mIconDelimitedText', '.txt': 'theme:mIconDelimitedText',
    '.xlsx': 'theme:mIconDelimitedText', '.xls': 'theme:mIconDelimitedText',
    '.ods': 'theme:mIconDelimitedText', '.nc': 'theme:mIconRaster', '.hdf': 'theme:mIconRaster',
    '.h5': 'theme:mIconRaster',
}
PROVIDER_ICONS = {
    'postgres': 'theme:mIconPostgis', 'mssql': 'theme:mIconMssql', 'oracle': 'theme:mIconOracle',
    'hana': 'theme:mIconHana', 'spatialite': 'theme:mIconSpatialite',
    'wms': 'theme:mIconWms', 'wfs': 'theme:mIconWfs', 'wcs': 'theme:mIconWcs',
    'xyz': 'theme:mIconXyz', 'arcgismapserver': 'theme:mActionAddAmsLayer',
    'arcgisfeatureserver': 'theme:mActionAddAfsLayer', 'vectortile': 'theme:mActionAddVectorTileLayer',
    'mdal': 'theme:mActionAddMeshLayer', 'pdal': 'theme:mActionAddPointCloudLayer',
    'copc': 'theme:mActionAddPointCloudLayer', 'ept': 'theme:mActionAddPointCloudLayer',
}


_BRACED = re.compile(r'^\{(?P<archive>[^}]+)\}')
_ARCHIVE = re.compile(r'^(?P<archive>.*?\.(?:zip|kgalp|kmz|gz|tgz|tar|7z))(?:/|$)', re.IGNORECASE)


def archive_of(parts, path):
    """The archive file (.zip, .kgalp ...) a layer is read from in place, or ''."""
    if not parts.get('vsiPrefix') and not path.startswith('{'):
        return ''
    match = _BRACED.match(path) or _ARCHIVE.match(path)
    return clean_path(match.group('archive')) if match else ''


def source_group(layer):
    """(key, short name, full path, icon file) for the heading a layer belongs under."""
    provider = layer.providerType() or ''
    source = layer.source() or ''
    parts = decode_source(provider, source)
    path = clean_path(parts.get('path') or '')
    kind = classify_provider(provider, source, path)

    if kind == KIND_MEMORY:
        return 'memory', 'Temporary layers', 'Temporary (memory) layers', 'theme:mIconMemory'
    archive = archive_of(parts, path)
    if archive:     # read in place from a package: the package is the heading
        key = archive.lower() if IS_WINDOWS else archive
        ext = os.path.splitext(archive)[1].lower()
        return key, os.path.basename(archive), archive, FILE_ICONS.get(ext, 'theme:mIconZip')
    if kind in (KIND_FILE, KIND_DIRECTORY) and path:
        ext = os.path.splitext(path.rstrip('/'))[1].lower()
        # a file that names a sub-layer ("|layername=..") or a GDAL sub-dataset holds
        # several layers whatever its extension is: it is the heading itself
        multi = bool(parts.get('layerName') or parts.get('layerId') or parts.get('_subdataset')
                     or parts.get('vsiSuffix'))
        if ext in CONTAINERS or multi:
            full = path.rstrip('/')
            name, icon = os.path.basename(full) or full, FILE_ICONS.get(ext, 'database.svg')
        else:
            full = os.path.dirname(path) or path
            name, icon = os.path.basename(full) or full, 'theme:mIconFolder'
        key = full.lower() if IS_WINDOWS else full
        return key, name, full, icon
    if kind == KIND_DATABASE:
        db = str(parts.get('dbname') or parts.get('service') or provider)
        host = str(parts.get('host') or '')
        port = str(parts.get('port') or '')
        full = '{}{}{}'.format(db, ' @ ' + host if host else '', ':' + port if host and port else '')
        return 'db:' + full, db, full, PROVIDER_ICONS.get(provider.lower(), 'database.svg')
    if kind == KIND_REMOTE:
        url = str(parts.get('url') or path or source)
        host = urlparse(url).netloc or url[:60]
        return 'web:' + url, host, url, PROVIDER_ICONS.get(provider.lower(), 'cloud.svg')
    return ('other:' + provider, provider or 'Other sources', source[:200],
            PROVIDER_ICONS.get(provider.lower(), 'database.svg'))


def _as_icon(value):
    if isinstance(value, QIcon):
        return value
    if isinstance(value, QPixmap):
        return QIcon(value)
    return QIcon()


class SourceTree(QTreeWidget):
    """The grouped list. Owned and shown/hidden by `SourceListController`."""

    def __init__(self, iface, parent=None, catalog_getter=None):
        super().__init__(parent)
        self.iface = iface
        self.catalog_getter = catalog_getter
        self.full_path = False
        self._busy = False
        self._collapsed = set()        # heading keys / layer ids the user folded
        self._connected = set()        # layer ids whose style signals are hooked
        self._items = {}               # layer id -> item
        self.setHeaderHidden(True)
        self.setIndentation(20)
        self.setIconSize(QSize(18, 18))
        self.setUniformRowHeights(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAnimated(False)
        self._apply_style()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.rebuild)

        self.itemChanged.connect(self._on_item_changed)
        self.itemCollapsed.connect(lambda it: self._remember(it, True))
        self.itemExpanded.connect(lambda it: self._remember(it, False))
        self.itemSelectionChanged.connect(self._on_selection)
        self.itemDoubleClicked.connect(self._on_double_click)
        self.customContextMenuRequested.connect(self._context_menu)

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        self._signals = [
            (project.layersAdded, self.schedule), (project.layersRemoved, self.schedule),
            (project.cleared, self.schedule), (project.readProject, self.schedule),
            (root.addedChildren, self.schedule), (root.removedChildren, self.schedule),
            (root.nameChanged, self.schedule), (root.visibilityChanged, self._sync_checks),
            (iface.currentLayerChanged, self._sync_selection),
        ]
        for signal, slot in self._signals:
            signal.connect(slot)
        with suppress(Exception):
            from ..map_tabs import add_listener
            add_listener(self.schedule)
        self.rebuild()

    # ------------------------------------------------------------------ look
    def _apply_style(self):
        from ..pro_layers import build_style, is_dark
        self.setStyleSheet(build_style(is_dark(self)).replace('QgsLayerTreeView', 'QTreeWidget'))

    def cleanup(self):
        self._timer.stop()
        for signal, slot in self._signals:
            with suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
        self._signals = []
        with suppress(Exception):
            from ..map_tabs import remove_listener
            remove_listener(self.schedule)

    def set_full_path(self, on):
        self.full_path = bool(on)
        for i in range(self.topLevelItemCount()):
            head = self.topLevelItem(i)
            head.setText(0, head.data(0, ROLE_ID + 10) if self.full_path else head.data(0, ROLE_ID + 11))

    # --------------------------------------------------------------- content
    def schedule(self, *args):
        self._timer.start()

    def _layers(self):
        project = QgsProject.instance()
        only = active_map_layer_ids()
        out = []
        for node in project.layerTreeRoot().findLayers():
            layer = node.layer()
            if layer is not None and (only is None or layer.id() in only):
                out.append(layer)
        return out

    def _legend_items(self, layer):
        """[(icon, text)] for the layer's legend (its symbol swatches)."""
        try:
            model = self.iface.layerTreeView().layerTreeModel()
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            rows = []
            for legend in model.layerLegendNodes(node, False):
                icon = _as_icon(legend.data(Qt.ItemDataRole.DecorationRole))
                text = legend.data(Qt.ItemDataRole.DisplayRole)
                rows.append((icon, str(text) if text else ''))
            return rows
        except Exception:
            return []

    def rebuild(self):
        if self._busy:
            return
        self._busy = True
        try:
            current = self.iface.activeLayer()
            current_id = current.id() if current is not None else None
            self.clear()
            self._items = {}
            groups = {}
            for layer in self._layers():
                key, short, full, icon = source_group(layer)
                if key not in groups:
                    head = QTreeWidgetItem([full if self.full_path else short])
                    head.setData(0, ROLE_KIND, KIND_HEAD)
                    head.setData(0, ROLE_KEY, key)
                    head.setData(0, ROLE_ID + 10, full)
                    head.setData(0, ROLE_ID + 11, short)
                    head.setIcon(0, _icon(icon))
                    head.setToolTip(0, full)
                    font = QFont(head.font(0))
                    font.setBold(True)
                    head.setFont(0, font)
                    groups[key] = head
                    self.addTopLevelItem(head)
                self._add_layer(groups[key], layer)
            self.expandAll()
            for key, head in groups.items():
                if key in self._collapsed:
                    head.setExpanded(False)
            for lid, item in self._items.items():
                if lid in self._collapsed:
                    item.setExpanded(False)
            if current_id in self._items:
                self._items[current_id].setSelected(True)
        finally:
            self._busy = False

    def _add_layer(self, head, layer):
        item = QTreeWidgetItem([layer.name()])
        item.setData(0, ROLE_KIND, KIND_LAYER)
        item.setData(0, ROLE_ID, layer.id())
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        on = node is not None and node.isVisible()
        item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        item.setToolTip(0, layer.source())
        if not layer.isValid():
            item.setIcon(0, QgsApplication.getThemeIcon('/mIconWarning.svg'))
        head.addChild(item)
        self._items[layer.id()] = item

        rows = self._legend_items(layer)
        single = len(rows) == 1
        for icon, text in rows:
            child = QTreeWidgetItem(['' if single else text])
            child.setData(0, ROLE_KIND, KIND_SWATCH)
            child.setData(0, ROLE_ID, layer.id())
            child.setIcon(0, icon)
            child.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.addChild(child)
        self._hook(layer)

    def _hook(self, layer):
        if layer.id() in self._connected:
            return
        self._connected.add(layer.id())
        for name in ('rendererChanged', 'styleChanged', 'nameChanged'):
            signal = getattr(layer, name, None)
            if signal is not None:
                signal.connect(self.schedule)

    # ----------------------------------------------------------- interaction
    def _layer_of(self, item):
        if item is None or item.data(0, ROLE_KIND) not in (KIND_LAYER, KIND_SWATCH):
            return None
        return QgsProject.instance().mapLayer(item.data(0, ROLE_ID))

    def _remember(self, item, collapsed):
        if self._busy:
            return
        key = item.data(0, ROLE_KEY) if item.data(0, ROLE_KIND) == KIND_HEAD else item.data(0, ROLE_ID)
        if key is None:
            return
        (self._collapsed.add if collapsed else self._collapsed.discard)(key)

    def _on_item_changed(self, item, column):
        if self._busy or item.data(0, ROLE_KIND) != KIND_LAYER:
            return
        node = QgsProject.instance().layerTreeRoot().findLayer(item.data(0, ROLE_ID))
        if node is not None:
            node.setItemVisibilityChecked(item.checkState(0) == Qt.CheckState.Checked)

    def _sync_checks(self, *args):
        root = QgsProject.instance().layerTreeRoot()
        self._busy = True
        try:
            for lid, item in self._items.items():
                node = root.findLayer(lid)
                on = node is not None and node.isVisible()
                state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
                if item.checkState(0) != state:
                    item.setCheckState(0, state)
        finally:
            self._busy = False

    def _on_selection(self):
        if self._busy:
            return
        layer = self._layer_of(self.currentItem())
        if layer is not None and layer is not self.iface.activeLayer():
            self._busy = True
            try:
                self.iface.setActiveLayer(layer)
            finally:
                self._busy = False

    def _sync_selection(self, layer=None):
        if self._busy or layer is None:
            return
        item = self._items.get(layer.id())
        if item is not None and not item.isSelected():
            self._busy = True
            try:
                self.setCurrentItem(item)
            finally:
                self._busy = False

    def _on_double_click(self, item, column):
        layer = self._layer_of(item)
        if layer is not None:
            self.iface.showLayerProperties(layer)

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if item is not None and item.data(0, ROLE_KIND) == KIND_HEAD:
            return self._heading_menu(item, pos)
        layer = self._layer_of(item)
        if layer is None:
            return
        self.setCurrentItem(self._items.get(layer.id(), item))
        menu = QMenu(self)
        menu.addAction(QgsApplication.getThemeIcon('/mActionZoomToLayer.svg'), 'Zoom to Layer',
                       lambda: self._zoom(layer))
        table = menu.addAction(QgsApplication.getThemeIcon('/mActionOpenTable.svg'), 'Open Attribute Table',
                               lambda: self.iface.showAttributeTable(layer))
        table.setEnabled(hasattr(layer, 'getFeatures'))
        menu.addSeparator()
        menu.addAction('Properties…', lambda: self.iface.showLayerProperties(layer))
        menu.addSeparator()
        menu.addAction(QgsApplication.getThemeIcon('/mActionRemoveLayer.svg'), 'Remove Layer',
                       lambda: QgsProject.instance().removeMapLayer(layer.id()))
        menu.exec(self.viewport().mapToGlobal(pos))

    def _heading_menu(self, item, pos):
        path = item.data(0, ROLE_ID + 10) or ''
        on_disk = bool(path) and os.path.exists(path)
        catalog = self.catalog_getter() if self.catalog_getter is not None else None
        menu = QMenu(self)
        reveal = menu.addAction(_icon('database.svg'), 'Reveal in Catalog',
                                lambda: catalog.reveal_path(path))
        reveal.setEnabled(on_disk and catalog is not None and catalog.panel is not None)
        folder = menu.addAction(_icon('folder.svg'), 'Open Directory',
                                lambda: self._open_directory(path))
        folder.setEnabled(on_disk)
        menu.exec(self.viewport().mapToGlobal(pos))

    @staticmethod
    def _open_directory(path):
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _zoom(self, layer):
        self.iface.setActiveLayer(layer)
        self.iface.zoomToActiveLayer()

    def expand_all(self):
        self._collapsed.clear()
        self.expandAll()

    def collapse_all(self):
        for i in range(self.topLevelItemCount()):
            head = self.topLevelItem(i)
            self._collapsed.add(head.data(0, ROLE_KEY))
            head.setExpanded(False)


class SourceListController:
    """The two Layers-panel buttons and the grouped list they switch on."""

    def __init__(self, iface, catalog_getter=None):
        self.iface = iface
        self.catalog_getter = catalog_getter
        self.list_action = None
        self.path_action = None
        self.tree = None
        self.toolbar = None
        self._view = None
        self._extra = []

    # --------------------------------------------------------------- set up
    def initGui(self):  # noqa: N802 - QGIS naming
        win = self.iface.mainWindow()
        self.list_action = QAction(_icon('list_by_source.svg'), 'List by Data Source', win)
        self.list_action.setObjectName('kgaListBySourceAction')
        self.list_action.setToolTip('List by Data Source: group the layers under their database or folder')
        self.list_action.setCheckable(True)
        self.list_action.toggled.connect(self._toggle)

        self.path_action = QAction(_icon('show_path.svg'), 'Show Full Path', win)
        self.path_action.setObjectName('kgaShowFullPathAction')
        self.path_action.setToolTip('Show the full path of each database or folder')
        self.path_action.setCheckable(True)
        self.path_action.setEnabled(False)
        self.path_action.toggled.connect(self._toggle_path)

        self._place_buttons()
        settings = QgsSettings()
        if settings.value(SETTING_BY_SOURCE, False, type=bool):
            QTimer.singleShot(0, lambda: self._restore(settings.value(SETTING_FULL_PATH, False, type=bool)))

    def _layers_dock(self):
        for dock in self.iface.mainWindow().findChildren(QDockWidget):
            if dock.objectName() == 'Layers' or dock.windowTitle() == 'Layers':
                return dock
        return None

    def _place_buttons(self):
        """Just before "Open Layer Styling Panel" in the Layers panel toolbar."""
        dock = self._layers_dock()
        bars = dock.findChildren(QToolBar) if dock is not None else []
        for bar in bars:
            for action in bar.actions():
                text = '{} {}'.format(action.text() or '', action.toolTip() or '')
                if action.objectName() == 'mActionStyleDock' or 'Layer Styling' in text:
                    bar.insertAction(action, self.list_action)
                    bar.insertAction(action, self.path_action)
                    self.toolbar = bar
                    break
            if self.toolbar is not None:
                break
        if self.toolbar is None and bars:
            first = bars[0].actions()[0] if bars[0].actions() else None
            bars[0].insertAction(first, self.list_action)
            bars[0].insertAction(first, self.path_action)
            self.toolbar = bars[0]
        if self.toolbar is None:
            self.list_action.setEnabled(False)
            return
        # the panel's own Expand All / Collapse All keep working on the grouped list
        for action in self.toolbar.actions():
            name = action.objectName()
            if name == 'mActionExpandTree':
                action.triggered.connect(self._expand)
                self._extra.append((action, self._expand))
            elif name == 'mActionCollapseTree':
                action.triggered.connect(self._collapse)
                self._extra.append((action, self._collapse))

    def _expand(self, *args):
        if self.tree is not None and self.tree.isVisible():
            self.tree.expand_all()

    def _collapse(self, *args):
        if self.tree is not None and self.tree.isVisible():
            self.tree.collapse_all()

    # --------------------------------------------------------------- switch
    def _restore(self, full):
        self.list_action.setChecked(True)
        if full:
            self.path_action.setChecked(True)

    def _toggle(self, on):
        QgsSettings().setValue(SETTING_BY_SOURCE, bool(on))
        self.path_action.setEnabled(bool(on))
        if on:
            if not self._show():
                self.list_action.blockSignals(True)
                self.list_action.setChecked(False)
                self.list_action.blockSignals(False)
                self.path_action.setEnabled(False)
        else:
            self._hide()

    def _toggle_path(self, on):
        QgsSettings().setValue(SETTING_FULL_PATH, bool(on))
        if self.tree is not None:
            self.tree.set_full_path(on)

    def _show(self):
        view = self.iface.layerTreeView()
        parent = view.parentWidget()
        layout = parent.layout() if parent is not None else None
        index = layout.indexOf(view) if layout is not None else -1
        if index < 0:
            self.iface.messageBar().pushWarning(
                'List by Data Source', 'The Layers panel layout was not recognised.')
            return False
        if self.tree is None:
            self.tree = SourceTree(self.iface, parent, self.catalog_getter)
            self.tree.full_path = self.path_action.isChecked()
            layout.insertWidget(index + 1, self.tree)
        self._view = view
        view.hide()
        self.tree.rebuild()
        self.tree.show()
        return True

    def _hide(self):
        if self.tree is not None:
            self.tree.hide()
        if self._view is not None:
            with suppress(RuntimeError):
                self._view.show()

    def unload(self):
        with suppress(RuntimeError):
            self._hide()
        for action, slot in self._extra:
            with suppress(TypeError, RuntimeError):
                action.triggered.disconnect(slot)
        self._extra = []
        if self.tree is not None:
            with suppress(RuntimeError):
                self.tree.cleanup()
                self.tree.deleteLater()
            self.tree = None
        for action in (self.list_action, self.path_action):
            if action is None:
                continue
            with suppress(RuntimeError):
                if self.toolbar is not None:
                    self.toolbar.removeAction(action)
                action.deleteLater()
        self.list_action = self.path_action = None

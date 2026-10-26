# -*- coding: utf-8 -*-
"""The KGA Catalog dock panel: ArcGIS Pro's Catalog pane for QGIS.

Four tabs, as in Pro:

* **Project**   - Toolboxes, Databases and Folders of the open workspace.
* **Connections** - the QGIS data-source connections (GeoPackage, PostgreSQL,
  WMS, XYZ Tiles, ArcGIS REST ...). Pro's *Portal* tab; there is no portal here.
* **Computer**  - Home, This PC, the profile folder and Network.
* **Favorites** - folders the user keeps close, shared by every project.

All four are views onto one private `QgsBrowserGuiModel` (see `catalog_items`),
so every item behaves exactly as it does in the QGIS Browser: drag it onto the
map, double-click a layer, right-click for QGIS's own menu plus the KGA entries
(Copy / Paste, layer packages, ...).
"""

import os
import traceback
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDirectoryItem,
    QgsLayerItem,
    QgsMessageLog,
    QgsSettings,
)
from qgis.gui import (
    QgsBrowserGuiModel,
    QgsBrowserTreeView,
    QgsDataItemGuiContext,
    QgsDockWidget,
    QgsFilterLineEdit,
    QgsGui,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    QModelIndex,
    QPointF,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    pyqtSignal,
)
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAction,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..branding import LOG_TAG
from ..core import gpkg_layers as gl
from ..branding import icon as _icon
from . import browser_paste as bp
from . import catalog_items as ci
from .project_open_guard import asking_enabled, set_asking

SETTINGS_TAB = 'KGA/catalog/tab'

TAB_PROJECT = 'project'
TAB_CONNECTIONS = 'connections'
TAB_COMPUTER = 'computer'
TAB_FAVORITES = 'favorites'

#: (key, title, search placeholder)
TABS = (
    (TAB_PROJECT, 'Project', 'Search Project'),
    (TAB_CONNECTIONS, 'Connections', 'Search Connections'),
    (TAB_COMPUTER, 'Computer', 'Search Computer'),
    (TAB_FAVORITES, 'Favorites', 'Search Favorites'),
)

#: Tabs that show one root's children at their top level.
ROOT_OF_TAB = {
    TAB_PROJECT: ci.ROOT_PROJECT,
    TAB_COMPUTER: ci.ROOT_COMPUTER,
    TAB_FAVORITES: ci.FAVORITES_PATH,
}


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def tab_of_root(item):
    """Which tab a root item of the private model belongs to, or None."""
    path = item.path()
    if path == ci.ROOT_PROJECT:
        return TAB_PROJECT
    if path == ci.ROOT_COMPUTER:
        return TAB_COMPUTER
    if path == ci.FAVORITES_PATH:
        return TAB_FAVORITES
    # Home, the drives and Project Home are the Computer tab's job, built by
    # hand there; leave the stock ones out everywhere.
    if isinstance(item, QgsDirectoryItem):
        return None
    # Spatial Bookmarks live in the Project tab (the Project root makes its own);
    # the others here are not data connections.
    key = item.providerKey()
    if key.startswith('special:') or key == ci.BOOKMARKS_PROVIDER:
        return None
    return TAB_CONNECTIONS


# ------------------------------------------------------------- appearance

#: Pro's flat tabs: plain text, the open one blue with a blue underline.
TAB_STYLE = """
QTabWidget::pane { border: 0; }
QTabBar::tab {
    background: transparent; border: 0; border-bottom: 2px solid transparent;
    padding: 4px 8px; margin-right: 2px;
}
QTabBar::tab:hover { color: #0079c1; }
QTabBar::tab:selected { color: #0079c1; border-bottom: 2px solid #0079c1; }
"""


_BADGED = {}


def home_badged(icon):
    """`icon` with a small house at its bottom right: Pro's "default" mark."""
    key = icon.cacheKey()
    cached = _BADGED.get(key)
    if cached is not None:
        return cached
    result = QIcon()
    for size in (16, 24, 32):
        base = icon.pixmap(size, size)
        if base.isNull():
            continue
        painter = QPainter(base)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        u = size / 16.0
        # white halo so the house reads on any folder colour
        painter.setPen(QPen(QColor('#ffffff'), 1.6 * u))
        painter.setBrush(QColor('#ffffff'))
        roof = QPolygonF([QPointF(9.0 * u, 9.6 * u), QPointF(12.5 * u, 6.4 * u),
                          QPointF(16.0 * u, 9.6 * u)])
        painter.drawPolygon(roof)
        painter.drawRect(QRectF(9.8 * u, 9.4 * u, 5.4 * u, 5.4 * u))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#d9531e'))
        painter.drawPolygon(roof)
        painter.setBrush(QColor('#e8743b'))
        painter.drawRect(QRectF(10.4 * u, 9.8 * u, 4.2 * u, 4.4 * u))
        painter.setBrush(QColor('#ffffff'))
        painter.drawRect(QRectF(11.9 * u, 11.6 * u, 1.2 * u, 2.6 * u))
        painter.end()
        result.addPixmap(base)
    _BADGED[key] = result
    return result


def hamburger_icon(color):
    """Three horizontal bars in `color`, drawn so it follows the theme."""
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 3.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    for y in (9, 16, 23):
        painter.drawLine(7, y, size - 7, y)
    painter.end()
    return QIcon(pixmap)


# --------------------------------------------------------------- the proxy

def _root_less(a, b, fallback):
    """Top-level connection nodes in the Browser's own order (its sort keys)."""
    key_a, key_b = a.sortKey(), b.sortKey()
    if type(key_a) is type(key_b) and key_a != key_b:
        try:
            return key_a < key_b
        except TypeError:
            return fallback
    if a.name().lower() != b.name().lower():
        return a.name().lower() < b.name().lower()
    return fallback


def _sort_key(item):
    """The Catalog sort key of `item`, or None when it has none."""
    key = item.property(ci.SORT_PROPERTY) or item.sortKey()
    if isinstance(key, str) and key.startswith(ci.SORT_PREFIX):
        return key
    return None


class CatalogProxy(QSortFilterProxyModel):
    """Keeps the root rows one tab wants, and filters by name.

    The name filter looks at what has been loaded, so it is cheap and never
    reads a drive: a row stays if it matches or something under it does.
    """

    def __init__(self, tab, parent=None):
        super().__init__(parent)
        self.tab = tab
        self.text = ''
        self.setDynamicSortFilter(True)
        self.sort(0)

    def lessThan(self, left, right):                # noqa: N802 - Qt naming
        """Our keyed nodes in key order; everything else as QGIS ordered it."""
        source = self.sourceModel()
        a, b = source.dataItem(left), source.dataItem(right)
        if a is not None and b is not None:
            key_a, key_b = _sort_key(a), _sort_key(b)
            if key_a is not None and key_b is not None:
                return key_a < key_b
            if not left.parent().isValid():
                return _root_less(a, b, left.row() < right.row())
        return left.row() < right.row()

    # --------------------------------------------------------------- items

    def dataItem(self, index):      # noqa: N802 - the Browser's own name
        """The `QgsDataItem` behind a proxy index (what Ctrl+C / Ctrl+V use)."""
        if not index.isValid() or self.sourceModel() is None:
            return None
        return self.sourceModel().dataItem(self.mapToSource(index))

    def set_text(self, text):
        self.text = (text or '').strip().lower()
        self.invalidateFilter()

    # -------------------------------------------------------------- filter

    def _matches(self, source, index):
        item = source.dataItem(index)
        if item is not None and self.text in item.name().lower():
            return True
        for row in range(source.rowCount(index)):
            if self._matches(source, source.index(row, 0, index)):
                return True
        return False

    def filterAcceptsRow(self, row, parent):        # noqa: N802 - Qt naming
        source = self.sourceModel()
        index = source.index(row, 0, parent)
        if not parent.isValid():
            item = source.dataItem(index)
            if item is None or tab_of_root(item) != self.tab:
                return False
            if self.tab in ROOT_OF_TAB:
                return True         # the root itself never filters out
        if not self.text:
            return True
        return self._matches(source, index)

    # --------------------------------------------------------------- icons

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DecorationRole:
            item = self.dataItem(index)
            if item is not None and (item.property(ci.DEFAULT_PROPERTY)
                                     or ci.is_default_database(item)):
                base = super().data(index, role)
                if isinstance(base, QIcon) and not base.isNull():
                    return home_badged(base)
            if item is not None and item.providerKey() in (
                    ci.KEY_COMPUTER, ci.KEY_PROJECT) \
                    and isinstance(item, QgsDirectoryItem):
                custom = ci.ICONS.get(ci._norm_key(item.dirPath()))
                if custom is not None:
                    return custom
        return super().data(index, role)


# ---------------------------------------------------------------- one page

class CatalogPage(QWidget):
    """One tab: a search box, a tree, and the buttons that tab needs."""

    def __init__(self, panel, tab, placeholder):
        super().__init__(panel)
        self.panel = panel
        self.tab = tab
        self._layers = []
        self._has_add = False
        self.proxy = CatalogProxy(tab, self)
        self.proxy.setSourceModel(panel.model)

        self.view = QgsBrowserTreeView(self)
        self.view.setModel(self.proxy)
        self.view.setHeaderHidden(True)
        self.view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setDragEnabled(True)
        self.view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.view.setExpandsOnDoubleClick(False)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.show_menu)
        self.view.doubleClicked.connect(self.double_clicked)

        self.search = QgsFilterLineEdit(self)
        self.search.setPlaceholderText(placeholder)
        self.search.textChanged.connect(self.proxy.set_text)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.search, 1)

        self.header = QHBoxLayout()
        self.header.setContentsMargins(0, 0, 0, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addLayout(self.header)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

        self._root_timer = QTimer(self)
        self._root_timer.setSingleShot(True)
        self._root_timer.timeout.connect(self.apply_root)
        for signal in (self.proxy.modelReset, self.proxy.layoutChanged,
                       self.proxy.rowsInserted, self.proxy.rowsRemoved):
            signal.connect(self._schedule_root)
        self.apply_root()

    # --------------------------------------------------------------- root

    def _schedule_root(self, *_args):
        self._root_timer.start(0)

    def apply_root(self):
        """Show the tab's root item's children as the top level."""
        path = ROOT_OF_TAB.get(self.tab)
        if path is None:
            return
        row = ci.root_row(self.panel.model, path)
        if row < 0:
            self.view.setRootIndex(QModelIndex())
            return
        index = self.proxy.mapFromSource(self.panel.model.index(row, 0))
        if index.isValid() and index != self.view.rootIndex():
            self.view.setRootIndex(index)

    # ---------------------------------------------------------- selection

    def selected_items(self):
        items = []
        selection = self.view.selectionModel()
        if selection is None:
            return items
        for index in selection.selectedRows():
            item = self.proxy.dataItem(index)
            if item is not None:
                items.append(item)
        return items

    def item_at(self, index):
        return self.proxy.dataItem(index)

    # ----------------------------------------------------------- behaviour

    def context(self):
        context = QgsDataItemGuiContext()
        context.setView(self.view)
        context.setMapCanvas(self.panel.iface.mapCanvas())
        context.setMessageBar(self.panel.iface.messageBar())
        return context

    def show_menu(self, pos):
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        item = self.item_at(index)
        if item is None:
            return
        selected = self.selected_items()
        if not any(sel is item for sel in selected):
            self.view.setCurrentIndex(index)
            selected = [item]
        menu = QMenu(self.view)
        try:
            populate_menu(menu, item, selected, self.context())
            self.adjust_menu(menu, item)
        except Exception:
            _log('Could not build the Catalog menu:\n{}'.format(
                traceback.format_exc()))
        if menu.actions():
            menu.exec(self.view.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def adjust_menu(self, menu, item):
        """Swap QGIS's model-editing entries for ones that are safe here.

        QGIS's Remove / Rename Favorite and Hide from Browser reach into the
        browser model through the tree view and delete the item under the
        cursor from inside the menu click. With the Catalog's proxy model in
        between that crashed QGIS, so they are replaced: favourites are edited
        in the saved list and the node refreshed afterwards, and Hide from
        Browser is dropped (it would hide a path in a model nobody else sees).
        """
        favorite = is_favorite(item)
        layer = geopackage_layer(item)
        layers = [geopackage_layer(sel) for sel in self.selected_items()]
        self._layers = [(g[0], g[1], sel) for g, sel in
                        zip(layers, self.selected_items()) if g is not None] \
            if layers and all(g is not None for g in layers) else []
        path = item.dirPath() if isinstance(item, QgsDirectoryItem) else item.path()
        self._has_add = has_add_to_map(menu)
        self._adjust_actions(menu, item, favorite, layer, path)
        tidy_separators(menu)

    def _adjust_actions(self, menu, item, favorite, layer, path):
        """The work of `adjust_menu`, applied to `menu` and every submenu of it."""
        for action in list(menu.actions()):
            if action.menu() is not None:
                # Manage > Delete Layer lives in a submenu.
                self._adjust_actions(action.menu(), item, favorite, layer, path)
                continue
            text = action.text().replace('&', '').rstrip('.\u2026 ')
            if text == 'Hide from Browser':
                menu.removeAction(action)
            elif text in STOCK_ADD_ENTRIES and self._has_add:
                menu.removeAction(action)       # ours, above, does the same
            elif text.startswith('Delete Selected Layers') and self._layers:
                menu.insertAction(action, self._later_action(
                    menu, action.text(), self.panel.delete_geopackage_layers,
                    list(self._layers)))
                menu.removeAction(action)
            elif text.startswith('Delete Layer') and layer is not None:
                # QGIS's own entry does nothing here (no dialog, no delete).
                menu.insertAction(action, self._later_action(
                    menu, action.text(), self.panel.delete_geopackage_layer,
                    (layer[0], layer[1], item)))
                menu.removeAction(action)
            elif favorite and text == 'Remove Favorite':
                menu.insertAction(action, self._later_action(
                    menu, 'Remove Favorite', self.panel.remove_favorite, path))
                menu.removeAction(action)
            elif favorite and text == 'Rename Favorite':
                menu.insertAction(action, self._later_action(
                    menu, 'Rename Favorite...', self.panel.rename_favorite, path))
                menu.removeAction(action)

    @staticmethod
    def _later_action(menu, text, callback, path):
        """An action that runs `callback(path)` after the menu has closed."""
        action = QAction(text, menu)
        action.triggered.connect(
            lambda _checked=False: QTimer.singleShot(0, lambda: callback(path)))
        return action

    def add_layer_to_map(self, item):
        """Double-click on a layer: the KGA Add to Map, ahead of QGIS's own."""
        if not isinstance(item, QgsLayerItem):
            return False
        sources = bp.sources_from_items([item])
        extras = bp.other_layer_uris([item])
        if not (sources or extras):
            return False
        bp.BrowserCopyPaste(self.panel.iface).add_to_map(sources, extras)
        return True

    def double_clicked(self, index):
        item = self.item_at(index)
        if item is None:
            return
        try:
            if self.add_layer_to_map(item):
                return
            if handle_double_click(item, self.context()):
                return
        except Exception:
            _log('Catalog double-click failed:\n{}'.format(
                traceback.format_exc()))
            return
        self.view.setExpanded(index, not self.view.isExpanded(index))


def geopackage_layer(item):
    """(gpkg path, table name) for a vector layer inside a GeoPackage, else None."""
    if not isinstance(item, QgsLayerItem) or item.providerKey() != 'ogr':
        return None
    parts = gl.split_uri(item.uri())
    return parts if parts is not None and os.path.isfile(parts[0]) else None


def is_favorite(item):
    """True for a folder listed under the Favorites root."""
    try:
        parent = item.parent()
        return parent is not None and parent.path() == ci.FAVORITES_PATH
    except Exception:
        return False


#: QGIS's own "add to the project" entries; the KGA "Add to Map" replaces them.
STOCK_ADD_ENTRIES = ('Add Layer to Project', 'Add Layers to Project',
                     'Add Selected Layers to Project')


def has_add_to_map(menu):
    return any(a.text().replace('&', '') == 'Add to Map' for a in menu.actions())


def tidy_separators(menu):
    """Drop leading, trailing and doubled separators, here and in submenus."""
    previous_separator = True               # so a leading one goes too
    for action in list(menu.actions()):
        if action.menu() is not None:
            tidy_separators(action.menu())
        if action.isSeparator():
            if previous_separator:
                menu.removeAction(action)
            previous_separator = True
        else:
            previous_separator = False
    for action in reversed(menu.actions()):
        if not action.isSeparator():
            break
        menu.removeAction(action)


def populate_menu(menu, item, selected, context):
    """Fill `menu` for `item` the way QGIS's Browser panel does."""
    for provider in QgsGui.dataItemGuiProviderRegistry().providers():
        try:
            provider.populateContextMenu(item, menu, selected, context)
        except Exception:
            _log('Menu provider {} failed:\n{}'.format(
                provider.name(), traceback.format_exc()))
    for action in item.actions(menu):
        menu.addAction(action)


def handle_double_click(item, context):
    """True when something took the double-click (a layer was added, ...)."""
    for provider in QgsGui.dataItemGuiProviderRegistry().providers():
        if provider.handleDoubleClick(item, context):
            return True
    return bool(item.handleDoubleClick())


# --------------------------------------------------------------- the panel

class CatalogPanel(QgsDockWidget):
    """The dock: tabs over one shared model."""

    setWorkspaceRequested = pyqtSignal()
    openWorkspaceRequested = pyqtSignal()
    convertProjectRequested = pyqtSignal()
    shareWorkspaceRequested = pyqtSignal()
    importPackageRequested = pyqtSignal()
    refreshRequested = pyqtSignal(str)

    def __init__(self, iface, model, parent=None):
        super().__init__('Catalog', parent)
        self.iface = iface
        self.model = model
        self.setObjectName('KgaCatalogPanel')
        self.setWindowTitle('Catalog')

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.setStyleSheet(TAB_STYLE)
        self.pages = {}
        for key, title, placeholder in TABS:
            page = CatalogPage(self, key, placeholder)
            self.pages[key] = page
            self.tabs.addTab(page, title)
        self._build_project_header()
        self._build_favorites_header()
        self._build_menu_button()
        self.setWidget(self.tabs)

        with suppress(Exception):
            self.tabs.setCurrentIndex(
                int(QgsSettings().value(SETTINGS_TAB, 0)))
        self.tabs.currentChanged.connect(self._remember_tab)

    # ------------------------------------------------------------- headers

    def _build_project_header(self):
        page = self.pages[TAB_PROJECT]
        self.workspace_label = QLabel('')
        self.workspace_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction)
        page.header.addWidget(self.workspace_label, 1)

    def _build_menu_button(self):
        """The hamburger at the right of the tab row, as in Pro."""
        button = QToolButton()
        button.setAutoRaise(True)
        button.setIcon(hamburger_icon(self.palette().color(
            self.palette().ColorRole.WindowText)))
        button.setToolTip('Catalog options')
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet('QToolButton::menu-indicator { image: none; }')
        menu = QMenu(button)
        menu.addAction(_icon('setWorkspace.png'), 'Set Workspace...',
                       self.setWorkspaceRequested.emit)
        menu.addAction(_icon('openWorkspace.png'), 'Open Workspace...',
                       self.openWorkspaceRequested.emit)
        menu.addAction(_icon('convertProject.png'),
                       'Convert Project to Workspace...',
                       self.convertProjectRequested.emit)
        menu.addAction(_icon('shareWorkspace.png'), 'Share Workspace...',
                       self.shareWorkspaceRequested.emit)
        menu.addAction(_icon('importProjectPackage.png'),
                       'Import Project Package...',
                       self.importPackageRequested.emit)
        menu.addSeparator()
        menu.addAction(QgsApplication.getThemeIcon('/mActionRefresh.svg'),
                       'Refresh', self.refresh_tab)
        menu.addAction(QgsApplication.getThemeIcon('/mActionCollapseTree.svg'),
                       'Collapse All', self.collapse_tab)
        menu.addSeparator()
        menu.addAction(QgsApplication.getThemeIcon('/mIconFavorites.svg'),
                       'Add Favorite Folder...', self.add_favorite)
        menu.addSeparator()
        self.ask_action = menu.addAction(
            'Ask to convert projects when opening')
        self.ask_action.setCheckable(True)
        self.ask_action.setToolTip(
            'When you open a traditional QGIS project, offer to convert it to a '
            'KGA workspace first')
        self.ask_action.toggled.connect(set_asking)
        # "Don't ask me again" can switch it off elsewhere; show the truth.
        menu.aboutToShow.connect(self._sync_ask_action)
        button.setMenu(menu)
        self.menu_button = button
        self.tabs.setCornerWidget(button, Qt.Corner.TopRightCorner)

    # Favourites live in QGIS's own setting, one 'folder|||name' per entry.
    FAVORITES_SETTING = 'browser/favourites'
    FAVORITE_SEPARATOR = '|||'

    def _favorite_entries(self):
        value = QgsSettings().value(self.FAVORITES_SETTING, []) or []
        return [value] if isinstance(value, str) else list(value)

    def _favorite_path(self, entry):
        return os.path.normcase(os.path.normpath(
            entry.split(self.FAVORITE_SEPARATOR)[0]))

    def remove_favorite(self, path):
        """Take a folder out of the favourites and refresh the tab."""
        key = os.path.normcase(os.path.normpath(path))
        entries = self._favorite_entries()
        kept = [e for e in entries if self._favorite_path(e) != key]
        if len(kept) != len(entries):
            QgsSettings().setValue(self.FAVORITES_SETTING, kept)
        self.refresh_favorites()

    def rename_favorite(self, path, new_name=None):
        key = os.path.normcase(os.path.normpath(path))
        entries = self._favorite_entries()
        if new_name is None:
            current = next((e.split(self.FAVORITE_SEPARATOR)[-1] for e in entries
                            if self._favorite_path(e) == key), '')
            new_name, accepted = QInputDialog.getText(
                self, 'Rename Favorite', 'Name:', text=current)
            if not accepted:
                return
        new_name = new_name.strip()
        if not new_name:
            return
        QgsSettings().setValue(self.FAVORITES_SETTING, [
            '{}{}{}'.format(e.split(self.FAVORITE_SEPARATOR)[0],
                            self.FAVORITE_SEPARATOR, new_name)
            if self._favorite_path(e) == key else e for e in entries])
        self.refresh_favorites()

    def delete_geopackage_layer(self, target):
        """Delete a layer from its GeoPackage, after asking, and refresh the tree."""
        self.delete_geopackage_layers([target])

    def delete_geopackage_layers(self, targets, ask=True):
        """Delete several GeoPackage layers after one question; refresh the nodes."""
        if not targets:
            return
        window = self.iface.mainWindow()
        if ask:
            if len(targets) == 1:
                what = 'the layer "{}" from {}'.format(
                    targets[0][1], os.path.basename(targets[0][0]))
            else:
                names = ['{} ({})'.format(n, os.path.basename(p))
                         for p, n, _i in targets]
                what = 'these {} layers:'.format(len(targets))
                what += chr(10) + chr(10).join('  ' + n for n in names)
            answer = QMessageBox.question(
                window, 'Delete Layer' + ('s' if len(targets) > 1 else ''),
                'Delete {}?'.format(what) + chr(10) * 2
                + 'This removes the tables and their data from the GeoPackage '
                'and cannot be undone.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        done, failed, parents = [], [], []
        for path, name, item in targets:
            try:
                gl.delete_layer(path, name)
                done.append(name)
            except gl.LayerError as error:
                failed.append('{}: {}'.format(name, error))
            with suppress(Exception):
                parent = item.parent()
                if parent is not None and not sip.isdeleted(parent) \
                        and all(parent is not q for q in parents):
                    parents.append(parent)
        if failed:
            QMessageBox.warning(window, 'Delete Layer', chr(10).join(failed))
        if done:
            self.iface.messageBar().pushSuccess(
                'KGA Toolbox', 'Deleted {} from the GeoPackage.'.format(
                    '"{}"'.format(done[0]) if len(done) == 1
                    else '{} layers'.format(len(done))))
        for parent in parents:
            with suppress(Exception):
                parent.refresh()

    def refresh_favorites(self):
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            item = self.model.dataItem(index)
            if item is not None and item.path() == ci.FAVORITES_PATH:
                # A plain refresh keeps a child whose path is unchanged, which
                # would keep a renamed favourite's old name.
                item.depopulate()
                self.model.refresh(index)
                return

    def _sync_ask_action(self):
        self.ask_action.blockSignals(True)
        self.ask_action.setChecked(asking_enabled())
        self.ask_action.blockSignals(False)

    def collapse_tab(self):
        page = self.current_page()
        if page is not None:
            page.view.collapseAll()

    def _build_favorites_header(self):
        page = self.pages[TAB_FAVORITES]
        title = QLabel('<b>My Favorites</b>')
        add = QToolButton()
        add.setAutoRaise(True)
        add.setText('Add Item')
        add.setToolTip('Add a folder to your favorites')
        add.setIcon(QgsApplication.getThemeIcon('/mIconFavorites.svg'))
        add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add.clicked.connect(lambda _c=False: self.add_favorite())
        page.header.addWidget(title, 1)
        page.header.addWidget(add)

    def set_workspace_text(self, workspace):
        if workspace is None:
            self.workspace_label.setText('No workspace')
            self.workspace_label.setToolTip('')
            return
        self.workspace_label.setText('<b>{}</b>'.format(workspace.display_name))
        self.workspace_label.setToolTip(workspace.root)

    # ------------------------------------------------------------- actions

    def add_favorite(self, path=None):
        if not path:
            path = QFileDialog.getExistingDirectory(
                self, 'Add a folder to Favorites')
        if path:
            # Named after the folder, as the Browser's own "Add as a Favorite"
            # does; the default is the full path.
            self.model.addFavoriteDirectory(
                path, os.path.basename(os.path.normpath(path)) or path)
            self.tabs.setCurrentWidget(self.pages[TAB_FAVORITES])

    def current_page(self):
        return self.tabs.currentWidget()

    def refresh_tab(self):
        page = self.current_page()
        if page is not None:
            self.refreshRequested.emit(page.tab)

    def show_tab(self, key):
        page = self.pages.get(key)
        if page is not None:
            self.tabs.setCurrentWidget(page)

    def _remember_tab(self, index):
        QgsSettings().setValue(SETTINGS_TAB, int(index))


# ------------------------------------------------------------ model factory

def build_model(iface, workspace):
    """The private model the panel views. Roots are created right here."""
    ci.STATE.workspace = workspace
    ci.STATE.building = True
    try:
        model = QgsBrowserGuiModel()
        model.setMapCanvas(iface.mapCanvas())
        model.setMessageBar(iface.messageBar())
        model.initialize()
    finally:
        ci.STATE.building = False
    return model

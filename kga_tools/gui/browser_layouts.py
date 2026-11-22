# -*- coding: utf-8 -*-
"""`.kgalayout` and `.kgamap` shortcuts in the Browser and the Catalog.

A shortcut in a workspace's Layouts folder stands for a print layout stored in
the project. Double-click it (or right-click > Open Layout) to open the layout
in the layout designer. Plain QGIS items, with behaviour on a
`QgsDataItemGuiProvider`, for the same sip reason as the `.kgapp` items.
"""

import os
import traceback

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataCollectionItem,
    QgsDataItem,
    QgsDataItemProvider,
    QgsMessageLog,
    QgsProject,
)
from qgis.gui import QgsDataItemGuiProvider, QgsGui

from ..branding import LOG_TAG
from ..branding import icon as _kga_icon
from ..core import layout_shortcuts as ls
from ..core import map_shortcuts as ms

PROVIDER_KEY = 'kga_layout_shortcut'
MAP_PROVIDER_KEY = 'kga_map_shortcut'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def shortcut_item(path, parent):
    layout, _project = ls.read(path)
    item = QgsDataItem(Qgis.BrowserItemType.Custom, parent,
                       layout or os.path.splitext(os.path.basename(path))[0],
                       path, PROVIDER_KEY)
    item.setIcon(QgsApplication.getThemeIcon('/mActionNewLayout.svg'))
    item.setToolTip('Print layout "{}"\nDouble-click to open'.format(layout))
    return item


class LayoutShortcutProvider(QgsDataItemProvider):

    def name(self):
        return PROVIDER_KEY

    def capabilities(self):
        return Qgis.DataItemProviderCapability.Files

    def createDataItem(self, path, parentItem):     # noqa: N802 - Qt naming
        if path and path.lower().endswith(ls.EXTENSION) and os.path.isfile(path):
            return shortcut_item(path, parentItem)
        return None


def is_shortcut(item):
    return item.providerKey() == PROVIDER_KEY \
        and item.path().lower().endswith(ls.EXTENSION)


def open_layout(iface, path):
    """Open the layout a shortcut names in the designer; message on failure."""
    bar = iface.messageBar()
    name, project_file = ls.read(path)
    current = QgsProject.instance()
    if project_file and os.path.normcase(os.path.normpath(project_file)) != \
            os.path.normcase(os.path.normpath(current.fileName())):
        bar.pushWarning('KGA Toolbox', 'This layout belongs to the project {}. '
                        'Open that project first.'.format(
                            os.path.basename(project_file)))
        return False
    layout = current.layoutManager().layoutByName(name)
    if layout is None:
        bar.pushWarning('KGA Toolbox', 'The layout "{}" is no longer in the '
                        'project.'.format(name))
        return False
    iface.openLayoutDesigner(layout)
    return True


class LayoutShortcutMenu(QgsDataItemGuiProvider):

    def __init__(self, iface):
        super().__init__()
        self.iface = iface

    def name(self):
        return 'kga_layout_shortcut_menu'

    def populateContextMenu(self, item, menu, selected_items, context):
        try:
            if not is_shortcut(item):
                return
            path = item.path()
            existing = menu.actions()
            first = existing[0] if existing else None
            action = menu.addAction(
                QgsApplication.getThemeIcon('/mActionNewLayout.svg'),
                'Open Layout', lambda: open_layout(self.iface, path))
            if first is not None:
                menu.removeAction(action)
                menu.insertAction(first, action)
                menu.insertSeparator(first)
        except Exception:
            _log('Could not add the layout menu entries:\n{}'.format(
                traceback.format_exc()))

    def handleDoubleClick(self, item, context):     # noqa: N802 - Qt naming
        try:
            if is_shortcut(item):
                open_layout(self.iface, item.path())
                return True
        except Exception:
            _log('Layout double-click failed:\n{}'.format(
                traceback.format_exc()))
        return False


# ------------------------------------------------------------------- maps

def map_item(path, parent):
    name, _project = ms.read(path)
    item = QgsDataCollectionItem(
        parent, name or os.path.splitext(os.path.basename(path))[0], path,
        MAP_PROVIDER_KEY)
    item.setIcon(_kga_icon('mapping/mapTab.png'))
    item.setToolTip('Map "{}"\nDouble-click to open'.format(name))
    # what the map uses, as written into the shortcut
    for number, layer in enumerate(ms.read_layers(path)):
        leaf = QgsDataItem(Qgis.BrowserItemType.Custom, item, layer,
                           '{}|layer={}'.format(path, number), MAP_PROVIDER_KEY)
        leaf.setIcon(QgsApplication.getThemeIcon('/mIconLayer.svg'))
        item.addChildItem(leaf, False)
    item.setState(Qgis.BrowserItemState.Populated)
    return item


class MapShortcutProvider(QgsDataItemProvider):

    def name(self):
        return MAP_PROVIDER_KEY

    def capabilities(self):
        return Qgis.DataItemProviderCapability.Files

    def createDataItem(self, path, parentItem):     # noqa: N802 - Qt naming
        if path and path.lower().endswith(ms.EXTENSION) and os.path.isfile(path):
            return map_item(path, parentItem)
        return None


def is_map_shortcut(item):
    return item.providerKey() == MAP_PROVIDER_KEY \
        and item.path().lower().endswith(ms.EXTENSION)


def open_map(iface, path):
    """Open the map a shortcut names as a tab; message on failure."""
    from . import map_tabs
    bar = iface.messageBar()
    name, project_file = ms.read(path)
    current = QgsProject.instance()
    if project_file and os.path.normcase(os.path.normpath(project_file)) != \
            os.path.normcase(os.path.normpath(current.fileName())):
        bar.pushWarning('KGA Toolbox', 'This map belongs to the project {}. '
                        'Open that project first.'.format(
                            os.path.basename(project_file)))
        return False
    tabs = map_tabs.active()
    if tabs is None:
        bar.pushWarning('KGA Toolbox', 'Turn on Map tabs in KGA Toolbox > '
                        'Settings to open maps.')
        return False
    group = tabs.group_by_id(ms.read_id(path))
    if group is None:
        group = next((tabs.group_by_id(mid) for mid, title, _open
                      in map_tabs.list_maps() if title == name), None)
    if group is None:
        bar.pushWarning('KGA Toolbox', 'The map "{}" is no longer in the '
                        'project.'.format(name))
        return False
    tabs.open_map(group)
    return True


class MapShortcutMenu(QgsDataItemGuiProvider):

    def __init__(self, iface):
        super().__init__()
        self.iface = iface

    def name(self):
        return 'kga_map_shortcut_menu'

    def populateContextMenu(self, item, menu, selected_items, context):
        try:
            if not is_map_shortcut(item):
                return
            path = item.path()
            existing = menu.actions()
            first = existing[0] if existing else None
            action = menu.addAction(_kga_icon('mapping/mapTab.png'), 'Open Map',
                                    lambda: open_map(self.iface, path))
            if first is not None:
                menu.removeAction(action)
                menu.insertAction(first, action)
                menu.insertSeparator(first)
        except Exception:
            _log('Could not add the map menu entries:\n{}'.format(
                traceback.format_exc()))

    def handleDoubleClick(self, item, context):     # noqa: N802 - Qt naming
        try:
            if is_map_shortcut(item):
                open_map(self.iface, item.path())
                return True
        except Exception:
            _log('Map double-click failed:\n{}'.format(
                traceback.format_exc()))
        return False


class LayoutShortcutInstaller(object):
    """Registers the layout and the map shortcut items."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.menu = None
        self.map_provider = None
        self.map_menu = None

    def install(self):
        if self.provider is not None:
            return
        self.provider = LayoutShortcutProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self.provider)
        self.menu = LayoutShortcutMenu(self.iface)
        QgsGui.dataItemGuiProviderRegistry().addProvider(self.menu)
        self.map_provider = MapShortcutProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self.map_provider)
        self.map_menu = MapShortcutMenu(self.iface)
        QgsGui.dataItemGuiProviderRegistry().addProvider(self.map_menu)

    def uninstall(self):
        if self.provider is None:
            return
        try:
            for menu in (self.menu, self.map_menu):
                QgsGui.dataItemGuiProviderRegistry().removeProvider(menu)
            for provider in (self.provider, self.map_provider):
                QgsApplication.dataItemProviderRegistry().removeProvider(provider)
        finally:
            self.provider = None
            self.menu = None
            self.map_provider = None
            self.map_menu = None

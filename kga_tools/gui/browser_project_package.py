# -*- coding: utf-8 -*-
"""`.kgapp` project packages in the Browser and the Catalog.

A `.kgapp` file in a folder shows up as an item with the package icon and its
summary as the tooltip; right-click it (or double-click) for **Import Project
Package...**. Registered globally, so the QGIS Browser, the Data Source Manager
and the Catalog panel all show it.

The item is a plain QGIS item on purpose: a Python subclass handed to the Browser
from a provider is detached by sip and loses its overrides (see the `.kgalp`
item), so behaviour lives on the right-click menu and the double-click handler of
a `QgsDataItemGuiProvider`, which look the package up by the item's file path.
"""

import os
import traceback

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataItem,
    QgsDataItemProvider,
    QgsMessageLog,
)
from qgis.gui import QgsDataItemGuiProvider, QgsGui
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices

from ..branding import LOG_TAG
from ..branding import icon as _icon
from ..core import projectpkg as pp

PROVIDER_KEY = 'kga_project_package'
ICON_FILE = 'workspace/projectPackage.png'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def package_item(path, parent):
    """A plain Browser item for the package at `path`."""
    item = QgsDataItem(Qgis.BrowserItemType.Custom, parent,
                       os.path.basename(path), path, PROVIDER_KEY)
    item.setIcon(_icon(ICON_FILE))
    item.setToolTip(tooltip(path))
    return item


def tooltip(path):
    """The package's name and summary, or just its file name when unreadable."""
    try:
        info = pp.read_manifest(path).info
    except pp.ProjectPackageError:
        return os.path.basename(path)
    parts = [info.name or os.path.basename(path)]
    if info.summary:
        parts.append(info.summary)
    if info.author:
        parts.append('by ' + info.author)
    parts.append('Double-click to import')
    return '\n'.join(parts)


class ProjectPackageProvider(QgsDataItemProvider):

    def name(self):
        return PROVIDER_KEY

    def capabilities(self):
        return Qgis.DataItemProviderCapability.Files

    def createDataItem(self, path, parentItem):     # noqa: N802 - Qt naming
        if path and path.lower().endswith(pp.EXTENSION) and os.path.isfile(path):
            return package_item(path, parentItem)
        return None


def is_package_item(item):
    return item.providerKey() == PROVIDER_KEY \
        and item.path().lower().endswith(pp.EXTENSION)


class ProjectPackageMenu(QgsDataItemGuiProvider):
    """Right-click and double-click on a package."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def name(self):
        return 'kga_project_package_menu'

    def populateContextMenu(self, item, menu, selected_items, context):
        try:
            if not is_package_item(item):
                return
            path = item.path()
            existing = menu.actions()
            first = existing[0] if existing else None
            actions = [
                menu.addAction(_icon('workspace/importProjectPackage.png'),
                               'Import Project Package...',
                               lambda: self.controller.import_project_package(path)),
                menu.addAction('Show in Folder',
                               lambda: QDesktopServices.openUrl(
                                   QUrl.fromLocalFile(os.path.dirname(path)))),
            ]
            if first is not None:
                for action in actions:
                    menu.removeAction(action)
                    menu.insertAction(first, action)
                menu.insertSeparator(first)
        except Exception:
            _log('Could not add the project package menu entries:\n{}'.format(
                traceback.format_exc()))

    def handleDoubleClick(self, item, context):     # noqa: N802 - Qt naming
        try:
            if is_package_item(item):
                self.controller.import_project_package(item.path())
                return True
        except Exception:
            _log('Project package double-click failed:\n{}'.format(
                traceback.format_exc()))
        return False


class ProjectPackageInstaller(object):
    """Registers the provider and menu on load and removes them on unload."""

    def __init__(self, controller):
        self.controller = controller
        self.provider = None
        self.menu = None

    def install(self):
        if self.provider is not None:
            return
        self.provider = ProjectPackageProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self.provider)
        self.menu = ProjectPackageMenu(self.controller)
        QgsGui.dataItemGuiProviderRegistry().addProvider(self.menu)

    def uninstall(self):
        if self.provider is None:
            return
        try:
            QgsGui.dataItemGuiProviderRegistry().removeProvider(self.menu)
            QgsApplication.dataItemProviderRegistry().removeProvider(self.provider)
        finally:
            self.provider = None
            self.menu = None

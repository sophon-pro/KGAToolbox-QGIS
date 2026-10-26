# -*- coding: utf-8 -*-
"""`.kgalp` layer packages in the QGIS Browser panel.

A package shows up like a database: expand it to see the layers inside, drag
one onto the map, or double-click it. The items read straight out of the zip
(GDAL's `/vsizip/`), so listing a package costs only its manifest, and
drag-and-drop, layer properties and the Data Source Manager work as they do for
a GeoPackage - read-only, since the data sits inside an archive.

A layer dragged out of the Browser arrives with QGIS's default style. *Add to
Project (with style)* on the right-click menu unpacks the package into a cache
folder in the QGIS profile (see `packaging.extract_cached`) and loads the layer
with its packaged style and SVG symbols, as Open Layer Package does.
"""

import os
import traceback

from qgis.core import (Qgis, QgsDataCollectionItem, QgsDataItemProvider,
                       QgsLayerItem, QgsMessageLog, QgsProject, QgsApplication)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication
from qgis.gui import QgsDataItemGuiProvider, QgsGui

from ..branding import LOG_TAG, icon
from ..core import packaging as P
from ..core.compat import enum_value

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

PROVIDER_KEY = 'kga_layer_package'
ICON_FILE = 'layerPackage.png'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


#: Layer items made here, by item path: where each came from. The right-click
#: menu uses it to tell a packaged layer from any other, and to find the
#: manifest entry to load with its style.
_ORIGIN = {}


def _vsizip(package, inner):
    """A GDAL path to `inner` inside the package, read in place.

    The braces matter: without them GDAL only treats a file as an archive when
    it knows the extension, and `.kgalp` is not one it knows.
    """
    return '/vsizip/{%s}/%s' % (package.replace(os.sep, '/'), inner)


def _layer_type(entry):
    kinds = Qgis.BrowserLayerType
    if entry.get('kind') == 'raster':
        return kinds.Raster
    return {'point': kinds.Point, 'line': kinds.Line,
            'polygon': kinds.Polygon}.get(entry.get('geometry'),
                                          kinds.TableLayer)


def package_item(path, parent):
    """The Browser item for one package, its layers already listed.

    Built from the manifest alone - a few kilobytes - so a folder holding many
    packages lists instantly. The items read straight out of the zip with
    `/vsizip/`, so nothing is unpacked until a layer is added with its style.

    These are plain QGIS items on purpose. A Python subclass handed to the
    Browser from a provider is detached from its C++ object by sip, and its
    overrides silently stop being called, so the behaviour that needs Python
    lives on the right-click menu instead.
    """
    item = QgsDataCollectionItem(parent, os.path.basename(path), path,
                                 PROVIDER_KEY)
    item.setIcon(icon(ICON_FILE))
    try:
        manifest = P.read_manifest(path)
    except P.PackageError as exc:
        _log(str(exc))
        return item

    children = []
    for entry in manifest.get('layers') or []:
        if entry.get('kind') == 'raster':
            if not entry.get('file'):
                continue
            uri, key = _vsizip(path, entry['file']), 'gdal'
        else:
            if not entry.get('table'):
                continue
            uri = '{}|layername={}'.format(
                _vsizip(path, P.DATA_GPKG), entry['table'])
            key = 'ogr'
        child_path = '{}/{}'.format(path, entry.get('id'))
        _ORIGIN[child_path] = (path, entry)
        children.append(QgsLayerItem(
            item, entry.get('name') or 'layer', child_path, uri,
            _layer_type(entry), key))
    for child in children:
        item.addChildItem(child)
    item.setState(Qgis.BrowserItemState.Populated)
    return item


class PackageProvider(QgsDataItemProvider):

    def name(self):
        return PROVIDER_KEY

    def capabilities(self):
        from qgis.core import QgsDataProvider
        return enum_value((Qgis, 'DataItemProviderCapability.Files'), (QgsDataProvider, 'File'))

    def createDataItem(self, path, parentItem):     # noqa: N802 - Qt naming
        if path and path.lower().endswith(P.EXTENSION) and os.path.isfile(path):
            return package_item(path, parentItem)
        return None


def add_entry_to_project(package, entry):
    """Load one packaged layer with its style and add it to the project.

    The package is unpacked into the browse cache first (once per version),
    because a style and its SVG symbols need real files to point at.
    """
    root = P.extract_cached(package)
    layer, warnings, _notes = P.load_entry_layer(entry, root)
    for message in warnings:
        _log(message)
        if iface is not None:
            iface.messageBar().pushWarning('Layer Package', message)
    if layer is None:
        return None
    QgsProject.instance().addMapLayer(layer)
    return layer


# ------------------------------------------------------------------ menu

class PackageMenu(QgsDataItemGuiProvider):
    """Right-click entries for packages and the layers inside them."""

    def name(self):
        return 'kga_layer_package_menu'

    def populateContextMenu(self, item, menu, selected_items, context):
        try:
            self._populate(item, menu)
        except Exception:
            _log('Could not add the layer package menu entries:\n{}'.format(
                traceback.format_exc()))

    def _populate(self, item, menu):
        existing = menu.actions()
        first = existing[0] if existing else None
        actions = []

        origin = _ORIGIN.get(item.path())
        if isinstance(item, QgsDataCollectionItem) and \
                item.providerKey() == PROVIDER_KEY:
            path = item.path()
            actions.append(menu.addAction(
                'Open Layer Package...', lambda: self.open_package(path)))
            actions.append(menu.addAction(
                'Add Layers to Layer Package...',
                lambda: self.add_to_package(path)))
        elif origin is not None and isinstance(item, QgsLayerItem):
            package, entry = origin
            actions.append(menu.addAction(
                'Add to Project (with style)',
                lambda: self.add_styled(package, entry)))
        if not actions:
            return

        # Ours go above QGIS's own entries, which are already in the menu.
        for action in actions:
            menu.removeAction(action)
            if first is not None:
                menu.insertAction(first, action)
            else:
                menu.addAction(action)
        if first is not None:
            menu.insertSeparator(first)

    @staticmethod
    def add_styled(package, entry):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            add_entry_to_project(package, entry)
        except Exception:
            _log('Could not add "{}" from the package:\n{}'.format(
                entry.get('name'), traceback.format_exc()))
            if iface is not None:
                iface.messageBar().pushWarning(
                    'Layer Package', 'Could not add "{}".'.format(
                        entry.get('name')))
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def open_package(path):
        from .open_package_dialog import show_open_package
        show_open_package(path)

    @staticmethod
    def add_to_package(path):
        from .add_to_package_dialog import show_add_to_package
        show_add_to_package(path)


class BrowserPackageInstaller(object):
    """Registers the provider on load and takes it out again on unload."""

    def __init__(self):
        self.provider = None
        self.menu = None

    def install(self):
        if self.provider is not None:
            return
        self.provider = PackageProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self.provider)
        self.menu = PackageMenu()
        QgsGui.dataItemGuiProviderRegistry().addProvider(self.menu)

    def uninstall(self):
        if self.provider is None:
            return
        try:
            QgsGui.dataItemGuiProviderRegistry().removeProvider(self.menu)
            QgsApplication.dataItemProviderRegistry().removeProvider(
                self.provider)
        finally:
            self.provider = None
            self.menu = None
            _ORIGIN.clear()

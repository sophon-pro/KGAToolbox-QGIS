# -*- coding: utf-8 -*-
"""The items behind the KGA Catalog: Project tree and Computer tree.

The Catalog does not draw its own tree. It owns a private `QgsBrowserGuiModel`,
so a folder, a GeoPackage or a File Geodatabase in it is the very same item the
QGIS Browser shows, with the same layers, drag-and-drop and right-click menus.
What the Browser does not have are the ArcGIS-Pro-style grouping nodes
(Toolboxes / Databases / Folders, Home / This PC / Network). Two small data-item
providers add those as the model's root items.

Rules these items follow, all probed on QGIS 3.44:

* **Plain C++ classes only.** A Python subclass of a data item that is handed to
  the model is detached by sip and loses its overrides. Behaviour lives on the
  `QgsDataItemGuiProvider` in `catalog_menu.py`, keyed by item path / provider.
* **Never call `QgsDataItem.children()`.** It re-wraps the children and the old
  wrappers take the C++ objects with them; an access violation follows. Rebuild
  a node with `depopulate()` and fill it again.
* **The providers answer only while a private model is being built.** They have
  the `Net` capability, which is what makes `QgsBrowserModel` ask them for a
  root item, so without the flag the stock Browser panel and the Data Source
  Manager would grow a second "Computer" node.
* `QgsDirectoryItem.setIcon` is ignored, so special folders get their icon from
  the Catalog's proxy model (`ICONS`).
"""

import os
import sys

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataCollectionItem,
    QgsDataItem,
    QgsDataItemProvider,
    QgsDirectoryItem,
    QgsLayerItem,
    QgsProject,
)
from qgis.PyQt.QtCore import QDir, QFileInfo, QStandardPaths, QStorageInfo
from qgis.PyQt.QtWidgets import QFileIconProvider

from ..branding import icon as _kga_icon
from ..core import toolbox_scripts as ts

#: Dynamic property set on the item that is the default folder / database.
DEFAULT_PROPERTY = 'kga_default'
#: Dynamic property carrying the sort key of a stock QGIS item (Spatial
#: Bookmarks), whose own `sortKey()` is not ours to set.
SORT_PROPERTY = 'kga_sort_key'

KEY_PROJECT = 'kga_catalog_project'
KEY_COMPUTER = 'kga_catalog_computer'

ROOT_PROJECT = 'kga-catalog:project'
ROOT_COMPUTER = 'kga-catalog:computer'
PATH_MAPS = 'kga-catalog:maps'
PATH_MAP_PREFIX = 'kga-catalog:map:'
PATH_MAP_LAYER_PREFIX = 'kga-catalog:maplayer:'
PATH_LAYOUTS = 'kga-catalog:layouts'
PATH_LAYOUT_PREFIX = 'kga-catalog:layout:'
PATH_TOOLBOXES = 'kga-catalog:toolboxes'
PATH_TOOLBOX = 'kga-catalog:toolbox'
PATH_DATABASES = 'kga-catalog:databases'
PATH_FOLDERS = 'kga-catalog:folders'
PATH_HOME = 'kga-catalog:home'
PATH_THIS_PC = 'kga-catalog:thispc'
PATH_NETWORK = 'kga-catalog:network'
PATH_EMPTY = 'kga-catalog:empty'

#: Name of QGIS's own provider of the Spatial Bookmarks node.
BOOKMARKS_PROVIDER = 'bookmarks_item'
PATH_BOOKMARKS = 'bookmarks:'

FAVORITES_PATH = 'favorites:'

# The browser model lists children by name and ignores sort keys; the Catalog's
# proxy honours the ones that start with SORT_PREFIX, which keeps Pro's order
# (Home before This PC, the project folder before attached ones) and leaves
# everything else in the order QGIS gave it.
SORT_PREFIX = 'kga:'

#: path -> QIcon for items whose own icon is fixed by QGIS (directories).
ICONS = {}


class _State(object):
    building = False
    workspace = None
    layouts_shown = False      # the Layouts section exists only with layouts


STATE = _State()


# ----------------------------------------------------------------- helpers

def _theme_icon(name):
    return QgsApplication.getThemeIcon(name)


def _norm_key(path):
    return os.path.normcase(os.path.normpath(path))


def _collection(parent, name, path, key, icon=None, order=None):
    item = QgsDataCollectionItem(parent, name, path, key)
    if icon is not None and not icon.isNull():
        item.setIcon(icon)
    if order is not None:
        item.setSortKey(SORT_PREFIX + order)
    return item


def _directory(parent, name, path, key, icon=None, order=None):
    item = QgsDirectoryItem(parent, name, path, path, key)
    if icon is not None and not icon.isNull():
        ICONS[_norm_key(path)] = icon
    if order is not None:
        item.setSortKey(SORT_PREFIX + order)
    return item


def _add(parent, child, live=False):
    """Add `child` under `parent`.

    `addChildItem` only emits the model's insert signals when asked to
    (`refresh=True`). A node that is still being assembled is not in any model
    yet, so adding to it must stay quiet; `live=True` is for the node a rebuild
    has just emptied *on screen*, where the views need to hear about each row.
    Only the direct children of that node are live: whatever hangs below them
    was assembled before they were added.
    """
    parent.addChildItem(child, live)


def _finish(item):
    """Mark a node whose children were all added up front as complete."""
    item.setState(Qgis.BrowserItemState.Populated)
    return item


def bookmarks_item(parent):
    """A new Spatial Bookmarks node from QGIS's own provider, or None.

    It is the stock item (zoom, edit, delete, import / export all work); the
    Catalog just lists it in the Project tab instead of under Connections.
    """
    for provider in QgsApplication.dataItemProviderRegistry().providers():
        if provider.name() == BOOKMARKS_PROVIDER:
            return provider.createDataItem('', parent)
    return None


def _file_item(parent, path):
    """The Browser item QGIS itself makes for the file at `path`, or None."""
    files = Qgis.DataItemProviderCapability.Files
    for provider in QgsApplication.dataItemProviderRegistry().providers():
        if not (provider.capabilities() & files):
            continue
        item = provider.createDataItem(path, parent)
        if item is not None:
            return item
    return None


# ------------------------------------------------------------ Project tree

def fill_project(root, workspace, live=False):
    """Fill `root` with Toolboxes / Databases / Folders for `workspace`."""
    if workspace is None:
        _add(root, _collection(
            root, 'No workspace - use Project > Set Workspace', PATH_EMPTY,
            KEY_PROJECT), live)
        return _finish(root)

    maps = _collection(root, 'Maps', PATH_MAPS, KEY_PROJECT,
                       _kga_icon('mapTab.png'), '0')
    fill_maps(maps)
    _add(root, _finish(maps), live)

    toolboxes = _collection(root, 'Toolboxes', PATH_TOOLBOXES, KEY_PROJECT,
                            _theme_icon('/processingAlgorithm.svg'), '1')
    box = _collection(toolboxes, 'Toolbox', PATH_TOOLBOX, KEY_PROJECT,
                      _theme_icon('/processingScript.svg'))
    fill_toolbox(box, workspace)
    _add(toolboxes, _finish(box))
    _add(root, _finish(toolboxes), live)

    databases = _collection(root, 'Databases', PATH_DATABASES, KEY_PROJECT,
                            _theme_icon('/mIconDbSchema.svg'), '2')
    fill_databases(databases, workspace)
    _add(root, _finish(databases), live)

    STATE.layouts_shown = bool(project_layouts())
    if STATE.layouts_shown:
        layouts = _collection(root, 'Layouts', PATH_LAYOUTS, KEY_PROJECT,
                              _theme_icon('/mIconLayout.svg'), '2b')
        fill_layouts(layouts)
        _add(root, _finish(layouts), live)

    folders = _collection(root, 'Folders', PATH_FOLDERS, KEY_PROJECT,
                          _theme_icon('/mIconFolder.svg'), '3')
    fill_folders(folders, workspace)
    _add(root, _finish(folders), live)

    bookmarks = bookmarks_item(root)
    if bookmarks is not None:
        bookmarks.setSortKey(SORT_PREFIX + '4')
        bookmarks.setProperty(SORT_PROPERTY, SORT_PREFIX + '4')
        _add(root, bookmarks, live)
    return _finish(root)


def layer_icon(layer):
    """The icon QGIS shows for `layer` (by its type and geometry)."""
    try:
        from qgis.core import QgsIconUtils
        icon = QgsIconUtils.iconForLayer(layer)
        if icon is not None and not icon.isNull():
            return icon
    except (ImportError, AttributeError):
        pass
    return _theme_icon('/mIconLayer.svg')


def fill_maps(node, workspace=None, live=False):
    """One node per map of the project, open or not (Pro's Maps folder).

    A map expands to the layers it uses.
    """
    from . import map_tabs
    root = QgsProject.instance().layerTreeRoot()
    groups = {map_tabs.map_id(g): g for g in root.children()
              if map_tabs.is_map(g)}
    for mid, name, is_open in map_tabs.list_maps():
        item = _collection(node, name, PATH_MAP_PREFIX + mid, KEY_PROJECT,
                           _kga_icon('mapTab.png'))
        item.setToolTip('{}{}'.format(
            name, '' if is_open else ' (closed) - double-click to open'))
        group = groups.get(mid)
        for layer_node in (group.findLayers() if group is not None else []):
            leaf = QgsDataItem(
                Qgis.BrowserItemType.Custom, item, layer_node.name(),
                '{}{}:{}'.format(PATH_MAP_LAYER_PREFIX, mid,
                                 layer_node.layerId()), KEY_PROJECT)
            layer = layer_node.layer()
            if layer is not None:
                leaf.setIcon(layer_icon(layer))
                leaf.setToolTip(layer.source())
            item.addChildItem(leaf, False)
        _add(node, _finish(item), live)
    return _finish(node)


def project_layouts():
    """The project's print layouts and reports, by name."""
    layouts = QgsProject.instance().layoutManager().layouts()
    return sorted(layouts, key=lambda layout: layout.name().lower())


def fill_layouts(node, workspace=None, live=False):
    """One leaf per layout (Pro's Layouts folder)."""
    for layout in project_layouts():
        leaf = QgsDataItem(Qgis.BrowserItemType.Custom, node, layout.name(),
                           PATH_LAYOUT_PREFIX + layout.name(), KEY_PROJECT)
        leaf.setIcon(_theme_icon('/mIconLayout.svg'))
        leaf.setToolTip(layout.name())
        _add(node, leaf, live)
    return _finish(node)


def fill_toolbox(box, workspace, live=False):
    """One leaf per script in the workspace's Toolbox folder."""
    box.setToolTip(workspace.toolbox)
    for script in ts.list_scripts(workspace.toolbox):
        name = os.path.splitext(os.path.basename(script))[0]
        leaf = QgsDataItem(Qgis.BrowserItemType.Custom, box, name, script,
                           KEY_PROJECT)
        leaf.setIcon(_theme_icon('/mIconPythonFile.svg'))
        leaf.setToolTip(script)
        _add(box, leaf, live)
    return _finish(box)


def is_default_database(item):
    """True for any item standing for the workspace's default GeoPackage.

    The Databases node marks its own item; the same file listed under the
    project folder is a stock Browser item, so it is recognised by its path.
    """
    workspace = STATE.workspace
    if workspace is None or not workspace.default_database:
        return False
    if isinstance(item, QgsLayerItem) or not isinstance(item, QgsDataCollectionItem):
        return False
    path = item.path()
    if path.startswith('gpkg:'):
        path = path[len('gpkg:'):].lstrip('/')
    if not path.lower().endswith('.gpkg'):
        return False
    return _norm_key(path) == _norm_key(workspace.default_database)


def mark_default(item):
    """Flag `item` as the default; the Catalog's proxy draws the home badge."""
    item.setProperty(DEFAULT_PROPERTY, True)


def fill_databases(node, workspace, live=False):
    """Every database in the project folder, then the attached ones.

    The default one (home badge) leads.
    """
    default = _norm_key(workspace.default_database) \
        if workspace.default_database else ''
    for path in workspace.all_databases():
        item = _file_item(node, path)
        if item is None:
            continue
        is_default = _norm_key(path) == default
        item.setSortKey(SORT_PREFIX + ('0' if is_default else
                                       '1-' + os.path.basename(path).lower()))
        if is_default:
            mark_default(item)
        _add(node, item, live)
    return _finish(node)


def fill_folders(node, workspace, live=False):
    """The project folder under the project's name, then attached folders.

    The default folder (home badge) leads; until the user picks another it is
    the project folder.
    """
    default = _norm_key(workspace.default_folder)
    for path in workspace.all_folders():
        is_root = _norm_key(path) == _norm_key(workspace.root)
        name = workspace.display_name if is_root else (os.path.basename(path) or path)
        item = _directory(node, name, path, KEY_PROJECT, None,
                          '0' if _norm_key(path) == default
                          else '1-' + name.lower())
        if _norm_key(path) == default:
            mark_default(item)
        _add(node, item, live)
    return _finish(node)


SECTIONS = {PATH_MAPS: fill_maps, PATH_LAYOUTS: fill_layouts, PATH_TOOLBOX: fill_toolbox, PATH_DATABASES: fill_databases,
            PATH_FOLDERS: fill_folders}


def rebuild_section(model, path, workspace):
    """Refill one section of the Project tree; the rest keeps its expansion."""
    root_index = model.index(root_row(model, ROOT_PROJECT), 0)
    section = _find_virtual(model, root_index, path)
    if section is None or workspace is None:
        return False
    _refill(section, lambda: SECTIONS[path](section, workspace, True))
    return True


def _find_virtual(model, index, path):
    """A node of ours below `index`, found by path (our own nodes only)."""
    for row in range(model.rowCount(index)):
        child = model.index(row, 0, index)
        item = model.dataItem(child)
        if item is None or not item.path().startswith('kga-catalog:'):
            continue
        if item.path() == path:
            return item
        found = _find_virtual(model, child, path)
        if found is not None:
            return found
    return None


def _refill(node, fill):
    """Empty `node` and fill it again; `fill` adds with `live=True`."""
    node.depopulate()
    fill()


def rebuild_project(model, workspace):
    """Refill the Project root in `model` after the workspace changed."""
    STATE.workspace = workspace
    root = find_root(model, ROOT_PROJECT)
    if root is None:
        return False
    _refill(root, lambda: fill_project(root, workspace, True))
    return True


# ----------------------------------------------------------- Computer tree

def special_folders():
    """(label, path) for the standard folders that exist on this machine."""
    where = QStandardPaths.StandardLocation
    wanted = (('Desktop', where.DesktopLocation),
              ('Downloads', where.DownloadLocation),
              ('Documents', where.DocumentsLocation),
              ('Pictures', where.PicturesLocation),
              ('Videos', where.MoviesLocation))
    found = []
    for label, location in wanted:
        path = QStandardPaths.writableLocation(location)
        if path and os.path.isdir(path):
            found.append((label, path))
    return found


def _is_remote(volume):
    if sys.platform == 'win32':
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(
                volume.rootPath().replace('/', '\\')) == 4   # DRIVE_REMOTE
        except Exception:
            return False
    return bytes(volume.fileSystemType()).decode(
        'utf-8', 'ignore').lower() in ('nfs', 'cifs', 'smbfs', 'smb3', 'afpfs')


def drives():
    """[(label, path, remote)] for every mounted, ready volume."""
    found = []
    for volume in QStorageInfo.mountedVolumes():
        if not volume.isValid() or not volume.isReady():
            continue
        path = volume.rootPath()
        if sys.platform != 'win32' and (path.startswith('/snap')
                                        or path.startswith('/boot')
                                        or path.startswith('/sys')):
            continue
        shown = path.replace('/', '\\') if sys.platform == 'win32' else path
        name = volume.name() or volume.displayName()
        label = '{} ({})'.format(shown, name) if name and name != path else shown
        found.append((label, path, _is_remote(volume)))
    return found


def fill_computer(root, live=False):
    """Fill `root` with Home / This PC / <user> / Network."""
    icons = QFileIconProvider()
    IconType = QFileIconProvider.IconType

    home_path = QDir.homePath()
    home = _collection(root, 'Home', PATH_HOME, KEY_COMPUTER,
                       icons.icon(IconType.Folder), '1')
    seen = set()
    for number, (label, path) in enumerate(special_folders()):
        seen.add(_norm_key(path))
        _add(home, _directory(
            home, label, path, KEY_COMPUTER, icons.icon(QFileInfo(path)),
            '0-{}'.format(number)))
    # Like Pro, Home also lists the project's own folders next to the
    # standard ones. The rest of the profile is under its own node below.
    workspace = STATE.workspace
    if workspace is not None:
        for path in [workspace.root] + list(workspace.folders):
            if not os.path.isdir(path) or _norm_key(path) in seen:
                continue
            seen.add(_norm_key(path))
            name = os.path.basename(path) or path
            _add(home, _directory(home, name, path, KEY_COMPUTER, None,
                                  '1-' + name.lower()))
    _add(root, _finish(home), live)

    this_pc = _collection(root, 'This PC' if sys.platform == 'win32'
                          else 'Computer', PATH_THIS_PC, KEY_COMPUTER,
                          icons.icon(IconType.Computer), '2')
    network = _collection(root, 'Network', PATH_NETWORK, KEY_COMPUTER,
                          icons.icon(IconType.Network), '4')
    for label, path, remote in drives():
        kind = IconType.Network if remote else IconType.Drive
        _add(this_pc, _directory(this_pc, label, path, KEY_COMPUTER,
                                 icons.icon(kind)))
        if remote:
            _add(network, _directory(network, label, path, KEY_COMPUTER,
                                     icons.icon(kind)))
    _add(root, _finish(this_pc), live)

    # The profile folder sits beside This PC in Pro's Computer tab.
    user = _directory(root, os.path.basename(home_path) or home_path,
                      home_path, KEY_COMPUTER, icons.icon(IconType.Folder),
                      '3')
    _add(root, user, live)
    _add(root, _finish(network), live)
    return _finish(root)


def rebuild_computer(model, workspace=None):
    if workspace is not None:
        STATE.workspace = workspace
    root = find_root(model, ROOT_COMPUTER)
    if root is None:
        return False
    _refill(root, lambda: fill_computer(root, True))
    return True


# --------------------------------------------------------------- providers

class _RootProvider(QgsDataItemProvider):
    """Hands the model one root item, and only while a Catalog model is built."""

    key = ''
    root_path = ''
    title = ''

    def name(self):
        return self.key

    def capabilities(self):
        # Anything but "none" makes QgsBrowserModel ask for a root item.
        return Qgis.DataItemProviderCapability.Net

    def createDataItem(self, path, parentItem):     # noqa: N802 - Qt naming
        if path or not STATE.building:
            return None
        root = _collection(parentItem, self.title, self.root_path, self.key)
        return self.fill(root)

    def fill(self, root):
        raise NotImplementedError


class ProjectProvider(_RootProvider):
    key = KEY_PROJECT
    root_path = ROOT_PROJECT
    title = 'Project'

    def fill(self, root):
        return fill_project(root, STATE.workspace)


class ComputerProvider(_RootProvider):
    key = KEY_COMPUTER
    root_path = ROOT_COMPUTER
    title = 'Computer'

    def fill(self, root):
        return fill_computer(root)


# ------------------------------------------------------------ model access

def find_root(model, path):
    """The root item of `model` with this path, or None.

    Walks the model rows rather than `rootItems()`, which hands back the
    C++ objects with ownership and ends in a crash.
    """
    for row in range(model.rowCount()):
        item = model.dataItem(model.index(row, 0))
        if item is not None and item.path() == path:
            return item
    return None


def root_row(model, path):
    for row in range(model.rowCount()):
        item = model.dataItem(model.index(row, 0))
        if item is not None and item.path() == path:
            return row
    return -1

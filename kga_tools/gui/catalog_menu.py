# -*- coding: utf-8 -*-
"""The Catalog's own right-click entries and double-click behaviour.

QGIS's menus (Add Layer to Project, Export, Properties, Remove Favorite ...) and
KGA's Copy / Paste come from the providers already in the registry; the Catalog
panel asks them all. This provider adds only what is specific to the Catalog's
grouping nodes:

* Folders            > Attach Folder...
* a project folder   > Make Default; an attached one > Remove from Project
* Databases          > Attach Database...
* a project database > Make Default; an attached one > Remove from Project
* Toolbox            > New Script..., Open Toolbox Folder, Refresh
* a script           > Run, Edit Script, Show in Folder
* any other folder or GeoPackage > Add to Project (it then appears under
  Folders or Databases, at the same level as the rest, as in ArcGIS Pro)

It has no state of its own: every action calls back into the controller.
"""

import os
import traceback

from qgis.core import (
    Qgis,
    QgsDataCollectionItem,
    QgsDirectoryItem,
    QgsLayerItem,
    QgsMessageLog,
)
from qgis.gui import QgsDataItemGuiProvider

from ..branding import LOG_TAG
from . import catalog_items as ci
from . import map_tabs


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def _parent_path(item):
    try:
        parent = item.parent()
    except Exception:
        return ''
    return parent.path() if parent is not None else ''


def database_file(item):
    """The path of the GeoPackage file `item` stands for, or ''.

    Layers inside a GeoPackage report the file as their path too, so they are
    left out; so are connection nodes that are not a single file.
    """
    if isinstance(item, QgsLayerItem) \
            or not isinstance(item, QgsDataCollectionItem):
        return ''
    path = item.path()
    if path.startswith('gpkg:'):
        path = path[len('gpkg:'):].lstrip('/')
    return path if path.lower().endswith('.gpkg') and os.path.isfile(path) else ''


def is_script(item):
    return (item.providerKey() == ci.KEY_PROJECT
            and not isinstance(item, QgsDirectoryItem)
            and item.path().lower().endswith('.py')
            and _parent_path(item) == ci.PATH_TOOLBOX)


def is_map_item(item):
    return (item.providerKey() == ci.KEY_PROJECT
            and item.path().startswith(ci.PATH_MAP_PREFIX))


def is_layout_item(item):
    return (item.providerKey() == ci.KEY_PROJECT
            and item.path().startswith(ci.PATH_LAYOUT_PREFIX))


def _selected_paths(item, selected, kind):
    """Paths of every selected item of the same `kind` as `item`, `item` first."""
    paths = [item.path()]
    for other in selected or ():
        if other is not None and kind(other) and other.path() not in paths:
            paths.append(other.path())
    return paths


class CatalogMenu(QgsDataItemGuiProvider):

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def name(self):
        return 'kga_catalog_menu'

    # ------------------------------------------------------------ menu

    def populateContextMenu(self, item, menu, selected_items, context):
        try:
            entries = self.entries(item, selected_items)
        except Exception:
            _log('Could not add the Catalog menu entries:\n{}'.format(
                traceback.format_exc()))
            return
        if not entries:
            return
        existing = menu.actions()
        first = existing[0] if existing else None
        added = []
        for text, callback in entries:
            action = menu.addAction(text, callback)
            added.append(action)
        if first is not None:
            for action in added:
                menu.removeAction(action)
                menu.insertAction(first, action)
            menu.insertSeparator(first)

    def entries(self, item, selected=()):
        """[(text, callable)] for `item`, in menu order.

        `selected` is every item of the selection: Open, Duplicate and Delete
        act on all the maps (or layouts) in it, Rename on one.
        """
        c = self.controller
        path = item.path()
        key = item.providerKey()
        parent = _parent_path(item)
        out = []

        if path == ci.PATH_MAPS or c.is_maps_folder(path):
            out.append(('Create a New Map...', c.new_map))
            out.append(('Import Existing Map...', c.import_map))
        elif is_map_item(item):
            if map_tabs.active() is not None:
                paths = _selected_paths(item, selected, is_map_item)
                out.append(('Open', lambda ps=paths: [c.open_map(p) for p in ps]))
                if len(paths) == 1:
                    out.append(('Rename...', lambda p=path: c.rename_map(p)))
                out.append(('Duplicate',
                            lambda ps=paths: [c.duplicate_map(p) for p in ps]))
                out.append(('Delete...' if len(paths) == 1
                            else 'Delete {} Maps...'.format(len(paths)),
                            lambda ps=paths: c.delete_maps(ps)))
        elif path == ci.PATH_LAYOUTS or c.is_layouts_folder(path):
            out.append(('Create a New Layout...', c.new_layout))
            out.append(('Import Existing Layout...', c.import_layout))
        elif is_layout_item(item):
            paths = _selected_paths(item, selected, is_layout_item)
            out.append(('Open', lambda ps=paths: [c.open_layout(p) for p in ps]))
            if len(paths) == 1:
                out.append(('Rename...', lambda p=path: c.rename_layout(p)))
            out.append(('Duplicate',
                        lambda ps=paths: [c.duplicate_layout(p) for p in ps]))
            out.append(('Delete...' if len(paths) == 1
                        else 'Delete {} Layouts...'.format(len(paths)),
                        lambda ps=paths: c.delete_layouts(ps)))
        elif path == ci.PATH_FOLDERS:
            out.append(('Attach Folder...', c.attach_folder))
        elif path == ci.PATH_DATABASES:
            out.append(('Attach Database...', c.attach_database))
        elif path == ci.PATH_TOOLBOX:
            out.append(('New Script...', c.new_script))
            out.append(('Open Toolbox Folder', c.open_toolbox_folder))
            out.append(('Refresh', c.refresh_project))
        elif path in (ci.PATH_TOOLBOXES, ci.ROOT_PROJECT):
            out.append(('Refresh', c.refresh_project))
        elif is_script(item):
            out.append(('Run', lambda p=path: c.run_script(p)))
            out.append(('Edit Script', lambda p=path: c.edit_script(p)))
            out.append(('Show in Folder', lambda p=path: c.show_in_folder(p)))
        elif parent == ci.PATH_FOLDERS and isinstance(item, QgsDirectoryItem):
            workspace = c.workspace
            if workspace is not None:
                if ci._norm_key(path) != ci._norm_key(workspace.default_folder):
                    out.append(('Make Default',
                                lambda p=path: c.make_default_folder(p)))
                if ci._norm_key(path) != ci._norm_key(workspace.root):
                    out.append(('Remove from Project',
                                lambda p=path: c.detach_folder(p)))
        elif parent == ci.PATH_DATABASES:
            workspace = c.workspace
            if workspace is not None:
                if ci._norm_key(path) != ci._norm_key(workspace.default_database):
                    out.append(('Make Default',
                                lambda p=path: c.make_default_database(p)))
                # One that lives in the project folder is listed because it is
                # there; only an attached one can be taken out again.
                if not workspace.is_project_database(path):
                    out.append(('Remove from Project',
                                lambda p=path: c.detach_database(p)))
        elif c.workspace is not None and isinstance(item, QgsDirectoryItem) \
                and key != ci.KEY_PROJECT:
            out.append(('Add to Project',
                        lambda p=item.dirPath(): c.add_to_project(p)))
        elif c.workspace is not None and database_file(item) and not any(
                ci._norm_key(database_file(item)) == ci._norm_key(known)
                for known in c.workspace.all_databases()):
            out.append(('Add to Project',
                        lambda p=database_file(item): c.add_to_project(p)))
        return out

    # ---------------------------------------------------- double-click

    def handleDoubleClick(self, item, context):     # noqa: N802 - Qt naming
        try:
            if is_script(item):
                self.controller.run_script(item.path())
                return True
            if is_layout_item(item):
                self.controller.open_layout(item.path())
                return True
            if is_map_item(item):
                self.controller.open_map(item.path())
                return True
        except Exception:
            _log('Catalog double-click failed:\n{}'.format(
                traceback.format_exc()))
        return False

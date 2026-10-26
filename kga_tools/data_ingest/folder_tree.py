# -*- coding: utf-8 -*-
"""The folder tree of the Data Explorer dock: a Home section (Desktop, Downloads,
Documents, Pictures, Videos and the workspace folders) above the drives.

Folders are filled in when they are first expanded, so a slow network drive
never delays the dock opening.
"""

import os
import sys

from qgis.PyQt.QtCore import QDir, QFileInfo, QStandardPaths, QStorageInfo, Qt
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import QFileIconProvider

ROLE_PATH = Qt.ItemDataRole.UserRole + 1
ROLE_LOADED = Qt.ItemDataRole.UserRole + 2


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _inside(target, base):
    return target == base or target.startswith(base.rstrip("\\/") + os.sep)


def special_folders():
    """(label, path) for the standard user folders that exist."""
    where = QStandardPaths.StandardLocation
    wanted = (("Desktop", where.DesktopLocation), ("Downloads", where.DownloadLocation),
              ("Documents", where.DocumentsLocation), ("Pictures", where.PicturesLocation),
              ("Videos", where.MoviesLocation))
    found = []
    for label, location in wanted:
        path = QStandardPaths.writableLocation(location)
        if path and os.path.isdir(path):
            found.append((label, path))
    return found


def workspace_folders():
    """The KGA workspace folders (the Catalog's Untitled workspace and friends)."""
    try:
        from ..gui import catalog_items as ci
        ws = ci.STATE.workspace
        if ws is None:
            return []
        return [p for p in [ws.root] + list(ws.folders) if p and os.path.isdir(p)]
    except Exception:
        return []


def drives():
    """[(label, path)] for every mounted, ready volume, labelled like 'OS (C:)'."""
    found = []
    for vol in QStorageInfo.mountedVolumes():
        if not vol.isValid() or not vol.isReady():
            continue
        path = vol.rootPath()
        if sys.platform == "win32":
            shown = path.replace("/", "\\").rstrip("\\")
            name = vol.name()
            label = "%s (%s)" % (name, shown) if name else shown
        else:
            if path.startswith(("/snap", "/boot", "/sys", "/proc", "/dev", "/run")):
                continue
            label = vol.name() or path
        found.append((label, path))
    return found


class FolderTreeModel(QStandardItemModel):
    """Home + drives; every folder item carries its path and loads children lazily."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icons = QFileIconProvider()
        self.rebuild()

    def _folder_item(self, label, path, icon=None):
        item = QStandardItem(icon or self._icons.icon(QFileInfo(path)), label)
        item.setEditable(False)
        item.setData(path, ROLE_PATH)
        item.setData(False, ROLE_LOADED)
        item.appendRow(QStandardItem())  # placeholder so the expander arrow shows
        return item

    def rebuild(self):
        self.clear()
        root = self.invisibleRootItem()
        home = QStandardItem(self._icons.icon(QFileIconProvider.IconType.Folder), "Home")
        home.setEditable(False)
        home.setData(QDir.homePath(), ROLE_PATH)
        seen = set()
        for label, path in special_folders():
            seen.add(_key(path))
            home.appendRow(self._folder_item(label, path))
        for path in workspace_folders():
            if _key(path) not in seen:
                seen.add(_key(path))
                home.appendRow(self._folder_item(os.path.basename(path.rstrip("/\\")) or path, path))
        root.appendRow(home)
        drive_icon = self._icons.icon(QFileIconProvider.IconType.Drive)
        for label, path in drives():
            root.appendRow(self._folder_item(label, path, drive_icon))

    def path_of(self, index):
        return index.data(ROLE_PATH) if index.isValid() else None

    def load_children(self, item):
        """Replace the placeholder under `item` with its sub-folders (once)."""
        if item is None or item.data(ROLE_LOADED) is not False:
            return
        item.setData(True, ROLE_LOADED)
        item.removeRows(0, item.rowCount())
        entries = QDir(item.data(ROLE_PATH)).entryInfoList(
            QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot,
            QDir.SortFlag.Name | QDir.SortFlag.IgnoreCase)
        for info in entries:
            item.appendRow(self._folder_item(info.fileName(), info.absoluteFilePath()))

    def index_for_path(self, path):
        """Index of the deepest node holding `path`, expanding as needed (invalid if none)."""
        target = _key(path)
        best, best_len = None, -1
        candidates = []
        for r in range(self.rowCount()):
            top = self.item(r)
            if top.data(ROLE_LOADED) is None:  # the Home node: its children are the roots
                candidates += [top.child(c) for c in range(top.rowCount())]
            else:
                candidates.append(top)
        for it in candidates:
            k = _key(it.data(ROLE_PATH))
            if _inside(target, k) and len(k) > best_len:
                best, best_len = it, len(k)
        if best is None:
            return self.index(-1, -1)
        while _key(best.data(ROLE_PATH)) != target:
            self.load_children(best)
            nxt = None
            for r in range(best.rowCount()):
                child = best.child(r)
                if _inside(target, _key(child.data(ROLE_PATH))):
                    nxt = child
                    break
            if nxt is None:
                break
            best = nxt
        return best.index()

    def refresh_path(self, path):
        """Re-read the sub-folders of `path` (if that node has been opened); returns the index."""
        idx = self.index_for_path(path)
        item = self.itemFromIndex(idx) if idx.isValid() else None
        if item is None or item.data(ROLE_LOADED) is None:
            return idx
        if _key(item.data(ROLE_PATH)) == _key(path) and item.data(ROLE_LOADED):
            item.setData(False, ROLE_LOADED)
            item.removeRows(0, item.rowCount())
            self.load_children(item)
        return idx

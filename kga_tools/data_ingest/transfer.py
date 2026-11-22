# -*- coding: utf-8 -*-
"""Copy / Cut / Paste and drag-and-drop move of data in the Data Explorer.

The writing is done by the Browser paste engine (`core/data_paste.py`), which already
translates between shapefiles, GeoPackages and File Geodatabases. This module turns
file-list entries into paste sources, remembers what was copied or cut, and works out
which sources a Move may delete afterwards (only those that were written in full).
"""

import os
import shutil

from ..core import data_paste as dp
from ..core import gdb_convert as gc
from . import delete_data as DD
from . import file_renamer as FR
from . import inspector
from .compat import ignore

COPY, MOVE = "copy", "move"


def _key(path):
    return os.path.normcase(os.path.abspath(path))


class Clipboard:
    """What was copied or cut: the file-list entries and the paste sources made from them."""

    def __init__(self):
        self.mode = COPY
        self.items = []  # [(entry, [Source, ...])]

    def set(self, entries, mode):
        self.mode = mode
        self.items = [(e, s) for e, s in ((e, sources_for_entry(e)) for e in entries) if s]

    def sources(self):
        return [s for _e, group in self.items for s in group]

    def entries(self):
        return [e for e, _group in self.items]

    def __len__(self):
        return len(self.items)

    def clear(self):
        self.items = []


def container_sources(path):
    """One Source per table of a GeoPackage / File Geodatabase."""
    layers, _domains = gc.describe(path, path.lower().endswith(gc.GDB_SUFFIX))
    return [dp.Source(info.name, "%s|layername=%s" % (path, info.name), "ogr") for info in layers]


def sources_for_entry(entry):
    """Paste sources for a file-list entry ({path, kind, name, sublayer, is_folder}); [] if not copyable."""
    path = entry.get("path")
    if not path or entry.get("is_folder") or os.path.isdir(path) and not inspector.is_container(path):
        return []
    name = entry.get("name") or os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
    if entry.get("sublayer"):
        return [dp.Source(name, "%s|layername=%s" % (path, name), "ogr")]
    if entry.get("kind") == inspector.KIND_RASTER:
        return [dp.Source(name, path, "gdal", dp.KIND_RASTER)]
    if dp.container_path(path):
        try:
            return container_sources(path)
        except Exception as exc:
            ignore(exc)
            return []
    return [dp.Source(name, path, "ogr")]


def _source_container(source):
    """(path of the file holding the source, layer name or '')."""
    path = source.uri.split("|", 1)[0]
    layer = ""
    if "|layername=" in source.uri:
        layer = source.uri.split("|layername=", 1)[1].split("|")[0]
    return path, layer


def already_there(source, target):
    """True when `source` already lives in `target` (a move would do nothing)."""
    path, _layer = _source_container(source)
    if target.kind == dp.TARGET_FOLDER:
        return _key(os.path.dirname(path)) == _key(target.path)
    return _key(path) == _key(target.path)


def movable(items, target):
    """(items to move, entries skipped because they are already in the target)."""
    keep, skipped = [], []
    for entry, group in items:
        if all(already_there(s, target) for s in group):
            skipped.append(entry)
        else:
            keep.append((entry, group))
    return keep, skipped


def into_itself(entry, target):
    """A folder / database cannot be moved into itself."""
    path = entry.get("path") or ""
    return bool(path) and target.kind != dp.TARGET_FOLDER and _key(path) == _key(target.path)


def deletable_entries(items, result):
    """Entries of a finished move whose every source was written (so the original may go)."""
    if result.canceled:
        return []
    bad = {n for n, _r in result.failed} | {n for n, _r in result.skipped}
    done = {n for n, _w in result.written}
    return [e for e, group in items if group and all(s.name in done and s.name not in bad for s in group)]


def delete_moved(entries):
    """Send the originals of a finished move to the Recycle Bin. Returns (deleted targets, failures)."""
    targets = DD.targets_from(entries)
    ids = DD.project_layers_for(targets)
    DD.remove_project_layers(ids)
    deleted, failed = DD.delete_targets(targets)
    for t in deleted:
        inspector.forget_sublayers(t.path)
    return deleted, failed, len(ids)


def entry_from_path(path):
    """A file-list style entry for a plain path (used by drag and drop)."""
    kind = inspector.KIND_RASTER if path.lower().endswith((".tif", ".tiff", ".img", ".jp2", ".asc")) \
        else inspector.KIND_VECTOR
    return {"path": path, "kind": kind, "name": os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]}


def is_whole_file(entry):
    """A dataset on disk (not a table inside a database, not a folder): it can be moved as files."""
    path = entry.get("path") or ""
    return bool(path) and not entry.get("sublayer") and not entry.get("is_folder") and os.path.exists(path) \
        and not (os.path.isdir(path) and not inspector.is_container(path))


def _free_name(folder, name):
    stem, ext = os.path.splitext(name)
    candidate, i = name, 1
    while os.path.exists(os.path.join(folder, candidate)):
        i += 1
        candidate = "%s_%d%s" % (stem, i, ext)
    return candidate


def _remove_with_retry(path):
    """Delete a file or folder; a file another program still holds is retried briefly. True when gone."""
    import gc as _gc
    import time
    for attempt in range(6):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
            return True
        except OSError as exc:
            ignore(exc)
            _gc.collect()
            time.sleep(0.25 * (attempt + 1))
    return not os.path.exists(path)


def transfer_file(path, folder, copy):
    """Copy or move a dataset (with its sidecar files, or a whole .gdb folder) into `folder`.

    A move copies first and deletes the originals afterwards, so a file that is still open elsewhere
    never leaves a half-moved dataset: the copy is complete and the originals that could not be
    removed are returned. Never overwrites: a taken name gets _2, _3...
    Returns (new path, [originals left behind])."""
    path = path.rstrip("/\\")
    if _key(os.path.dirname(path)) == _key(folder) and not copy:
        return path, []
    name = _free_name(folder, os.path.basename(path))
    dest = os.path.join(folder, name)
    if os.path.isdir(path):
        if _key(dest).startswith(_key(path) + os.sep):
            raise OSError("cannot put a folder inside itself")
        shutil.copytree(path, dest)
        olds = [path]
    else:
        pairs = FR.dataset_files(path, dest)
        try:
            for old, new in pairs:
                shutil.copy2(old, new)
        except OSError:
            for _old, new in pairs:
                if os.path.exists(new):
                    _remove_with_retry(new)
            raise
        olds = [old for old, _new in pairs]
    if copy:
        return dest, []
    return dest, [o for o in olds if not _remove_with_retry(o)]


# ---- project layers that hold a database open while it is written

def container_for_paste(target, options, sources):
    """The .gpkg / .gdb a paste will write to (even if it does not exist yet), or ''."""
    if target.kind != dp.TARGET_FOLDER:
        return target.path
    if options.format in (dp.FMT_GPKG, dp.FMT_GDB):
        return dp._container_in_folder(target, options, sources)
    return ""


def detach_layers(container):
    """Take the project layers that read `container` out of the project, keeping their style and place,
    so Windows lets the database be changed (a layer left open makes 'Cannot delete ... .gdbtable').
    Returns the records for `reattach_layers`."""
    from qgis.core import QgsProject
    from qgis.PyQt.QtXml import QDomDocument
    if not container or not os.path.exists(container):
        return []
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    records, ids = [], []
    for layer in list(project.mapLayers().values()):
        path, _name = DD._layer_source(layer)
        if not path or _key(path) != _key(container):
            continue
        node = root.findLayer(layer.id())
        parent = node.parent() if node is not None else None
        doc = QDomDocument()
        try:
            layer.exportNamedStyle(doc)
        except (RuntimeError, TypeError) as exc:
            ignore(exc)
            doc = None
        records.append({"name": layer.name(), "source": layer.source(), "provider": layer.providerType(),
                        "style": doc, "parent": parent,
                        "index": parent.children().index(node) if parent is not None and node is not None else -1,
                        "visible": node.isVisible() if node is not None else True})
        ids.append(layer.id())
    DD.remove_project_layers(ids)
    return records


def reattach_layers(records):
    """Put the layers `detach_layers` took out back, same name, style, group and position."""
    from qgis.core import QgsProject, QgsVectorLayer
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    back = 0
    for rec in records:
        layer = QgsVectorLayer(rec["source"], rec["name"], rec["provider"])
        if not layer.isValid():
            continue
        if rec["style"] is not None:
            layer.importNamedStyle(rec["style"])
        project.addMapLayer(layer, False)
        parent = rec["parent"]
        try:
            if parent is None:
                parent = root
            node = parent.insertLayer(max(rec["index"], 0), layer)
        except RuntimeError:
            node = root.insertLayer(0, layer)
        if node is not None:
            node.setItemVisibilityChecked(rec["visible"])
        back += 1
    return back

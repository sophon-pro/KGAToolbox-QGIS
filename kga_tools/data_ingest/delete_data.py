# -*- coding: utf-8 -*-
"""Delete data from the Data Explorer: shapefiles / files with their sidecars, folders, and
single layers or tables inside a database (GeoPackage, SQLite, File Geodatabase).

Files and folders go to the Recycle Bin when the system has one; a layer inside a database
cannot be recovered. Project layers that point at deleted data are removed from the map.
"""

import os
import shutil

from qgis.core import QgsProject, QgsProviderRegistry, QgsVectorLayer
from qgis.PyQt.QtCore import QFile

from . import file_renamer as FR
from .compat import ignore

try:
    from osgeo import gdal, ogr
except ImportError:  # pragma: no cover - QGIS always ships GDAL
    gdal = ogr = None

K_FILE, K_FOLDER, K_LAYER = "file", "folder", "layer"


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _inside(path, base):
    path, base = _key(path), _key(base)
    return path != base and path.startswith(base.rstrip("\\/") + os.sep)


class Target:
    """One thing to delete: a file/folder (`path`) or a table of a database (`path` + `layer`)."""

    def __init__(self, kind, path, layer=None):
        self.kind, self.path, self.layer = kind, path, layer

    @property
    def label(self):
        base = os.path.basename(self.path.rstrip("/\\")) or self.path
        return "%s ▸ %s" % (base, self.layer) if self.kind == K_LAYER else base

    @property
    def recoverable(self):
        return self.kind != K_LAYER


def targets_from(entries):
    """Build the delete list from file-list entries: dicts with path, is_folder, and `sublayer`
    (True for a table shown inside a database). Anything inside another target is dropped."""
    found = []
    for e in entries:
        path = e.get("path")
        if not path:
            continue
        if e.get("sublayer"):
            found.append(Target(K_LAYER, path, e.get("name")))
        elif e.get("is_folder") or os.path.isdir(path):
            found.append(Target(K_FOLDER, path))
        else:
            found.append(Target(K_FILE, path))
    whole = [t for t in found if t.kind != K_LAYER]
    out, seen = [], set()
    for t in found:
        if any(w is not t and (_key(w.path) == _key(t.path) and t.kind == K_LAYER or _inside(t.path, w.path))
               for w in whole):
            continue
        ident = (t.kind, _key(t.path), t.layer)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(t)
    return out


def refusal(target):
    """Why this target must not be deleted, or ''."""
    path = os.path.abspath(target.path)
    bare = path.rstrip("/" + chr(92))
    if not bare or os.path.dirname(bare) in ("", bare):
        return "%s is a drive or root folder." % path
    if _key(path) == _key(os.path.expanduser("~")):
        return "%s is your home folder." % path
    if not os.path.exists(path):
        return "%s no longer exists." % path
    return ""


def files_of(target):
    """Every physical file removed by a file target (a shapefile brings its sidecars)."""
    if target.kind != K_FILE:
        return [target.path]
    return [old for old, _new in FR.dataset_files(target.path, target.path)]


def summary(targets):
    """Text for the confirmation box."""
    n_files = sum(1 for t in targets if t.kind == K_FILE)
    n_dirs = sum(1 for t in targets if t.kind == K_FOLDER)
    n_layers = sum(1 for t in targets if t.kind == K_LAYER)
    parts = []
    if n_files:
        parts.append("%d dataset%s" % (n_files, "" if n_files == 1 else "s"))
    if n_dirs:
        parts.append("%d folder%s (with everything inside)" % (n_dirs, "" if n_dirs == 1 else "s"))
    if n_layers:
        parts.append("%d layer%s / table%s inside a database" % (n_layers, "" if n_layers == 1 else "s",
                                                                 "" if n_layers == 1 else "s"))
    names = [t.label for t in targets[:8]]
    if len(targets) > 8:
        names.append("... and %d more" % (len(targets) - 8))
    text = "Delete %s?" % " and ".join(parts) + "\n\n" + "\n".join(names) + "\n\n"
    if n_layers:
        text += "A layer or table deleted from a database cannot be recovered.\n"
    if n_files or n_dirs:
        text += "Files and folders go to the Recycle Bin where the system has one; otherwise they are removed for good.\n"
    text += "Layers in the current project that use this data are removed from the map."
    return text


# ----------------------------------------------------------------- project layers

def _layer_source(layer):
    """(path, layer name) of a layer's source, or ('', '')."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception as exc:  # unknown provider: not ours
        ignore(exc)
        return "", ""
    return parts.get("path") or "", parts.get("layerName") or ""


def project_layers_for(targets):
    """Ids of project layers whose data one of the targets removes."""
    ids = []
    for layer in QgsProject.instance().mapLayers().values():
        path, name = _layer_source(layer)
        if not path:
            continue
        for t in targets:
            if t.kind == K_LAYER:
                hit = _key(path) == _key(t.path) and (name or "").lower() == (t.layer or "").lower()
            elif t.kind == K_FOLDER:
                hit = _key(path) == _key(t.path) or _inside(path, t.path)
            else:
                hit = any(_key(path) == _key(f) for f in files_of(t))
            if hit:
                ids.append(layer.id())
                break
    return ids


def remove_project_layers(ids):
    for lid in ids:
        layer = QgsProject.instance().mapLayer(lid)
        if isinstance(layer, QgsVectorLayer):
            try:
                layer.dataProvider().reloadData()  # let go of a pooled file connection
            except (RuntimeError, AttributeError) as exc:
                ignore(exc)
    QgsProject.instance().removeMapLayers(list(ids))


# ----------------------------------------------------------------- deleting

def _to_trash(path):
    """Move to the Recycle Bin; True on success."""
    try:
        res = QFile.moveToTrash(path)
    except (AttributeError, TypeError) as exc:
        ignore(exc)
        return False
    return bool(res[0] if isinstance(res, tuple) else res)


def _remove_path(path, permanent):
    if not os.path.exists(path):
        return
    if not permanent and _to_trash(path):
        return
    if not permanent:
        raise OSError("could not move %s to the Recycle Bin" % os.path.basename(path))
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def delete_layer(path, name):
    """Remove table `name` from the database at `path` (GDAL; no SQL)."""
    ds = ogr.Open(path, 1)
    if ds is None:
        raise OSError("cannot open %s for editing: %s" % (os.path.basename(path.rstrip("/\\")),
                                                          gdal.GetLastErrorMsg() or "unknown error"))
    index = next((i for i in range(ds.GetLayerCount()) if ds.GetLayerByIndex(i).GetName() == name), None)
    if index is None:
        ds = None
        raise OSError("'%s' is not in %s" % (name, os.path.basename(path.rstrip("/\\"))))
    gdal.ErrorReset()
    err = ds.DeleteLayer(index)
    ds = None
    if err != 0:
        raise OSError(gdal.GetLastErrorMsg() or "this database format cannot delete layers")


def delete_targets(targets, permanent=False):
    """Delete every target. Returns (deleted, [(target, message)])."""
    deleted, failed = [], []
    for t in targets:
        why = refusal(t)
        if why:
            failed.append((t, why))
            continue
        try:
            if t.kind == K_LAYER:
                delete_layer(t.path, t.layer)
            elif t.kind == K_FOLDER:
                _remove_path(t.path, permanent)
            else:
                for f in files_of(t):
                    _remove_path(f, permanent)
            deleted.append(t)
        except (OSError, RuntimeError) as exc:
            failed.append((t, str(exc)))
    return deleted, failed

# -*- coding: utf-8 -*-
"""
Layer Path Manager - copy datasets to a new folder (then re-point layers).

Safety rules
------------
* Originals are only ever *read*: never moved, changed or deleted.
* An existing file in the destination is never overwritten.  If an identical
  copy (same size) is already there it is reused; if a *different* file with
  the same name exists the dataset is skipped and reported.
* Every file is written under a temporary name (``*.lpm-part``) and renamed
  only once it is complete and its size verified, so an interrupted copy
  never leaves a half-written file under the real name.
* On failure or cancel, files created by the current dataset are removed.
* Free disk space is checked before copying.

``build_plan`` runs on the main thread (fast, metadata only);
``execute_plan`` is thread safe (plain files) and runs in a QgsTask.
"""

import filecmp
import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from qgis.core import QgsFeedback

from .utils import IS_WINDOWS, LayerRecord, clean_path, human_size

CHUNK = 8 * 1024 * 1024
PART_SUFFIX = ".lpm-part"

# Extensions (after the dataset's stem) that belong to the dataset.
SIDE_REMAINDERS = {
    "shx", "dbf", "prj", "cpg", "qix", "sbn", "sbx", "shp.xml", "xml", "qmd", "qml", "sld",
    "fix", "atx", "ain", "aih", "ixs", "mxs", "tfw", "tifw", "tiffw", "wld", "jgw", "jpgw",
    "pgw", "pngw", "rrd", "ige", "aux", "hdr", "csvt", "ovr", "msk", "aux.xml", "dat", "map",
    "id", "ind", "idx",
}
MAIN_SUFFIXES = ("-wal", "-shm", "-journal")   # GeoPackage / SQLite companions

ST_READY = "Ready"
ST_REUSE = "Already there"
ST_CONFLICT = "Different file exists"
ST_MISSING = "Source missing"
ST_SAME = "Same folder"
ST_COPIED = "Copied"
ST_FAILED = "Failed"
ST_CANCELLED = "Cancelled"


@dataclass
class CopyItem:
    """One physical dataset (may serve several layers, e.g. a GeoPackage)."""
    src_path: str
    dest_path: str
    is_dir: bool
    files: List[Tuple[str, str]] = field(default_factory=list)   # (src, dst)
    layer_ids: List[str] = field(default_factory=list)
    layer_names: List[str] = field(default_factory=list)
    size: int = 0
    status: str = ST_READY
    message: str = ""
    keep_original: bool = False      # MOVE: dataset also used by other layers -> keep it
    notice: str = ""                 # what the content comparison found (shown to the user)
    level: str = ""                  # "", "info" or "warn"
    equivalent: bool = False         # REUSE of a file with the same features (not byte-identical)
    duplicate_of: str = ""           # same data already in the folder under another name
    renamed: bool = False

    @property
    def needs_copy(self) -> bool:
        return self.status == ST_READY

    @property
    def usable(self) -> bool:
        """Can layers be switched to dest_path?"""
        return self.status in (ST_COPIED, ST_REUSE)


@dataclass
class CopyPlan:
    target: str
    source_root: str
    items: List[CopyItem]
    skipped_layers: List[str]            # non-file / not-in-scope notes
    free_bytes: int = -1
    message: str = ""

    @property
    def to_copy(self) -> List[CopyItem]:
        return [i for i in self.items if i.needs_copy]

    @property
    def copy_bytes(self) -> int:
        return sum(i.size for i in self.to_copy)

    @property
    def enough_space(self) -> bool:
        return self.free_bytes < 0 or self.free_bytes > self.copy_bytes * 1.05 + 1024 * 1024

    @property
    def layer_count(self) -> int:
        return sum(len(i.layer_ids) for i in self.items
                   if i.status in (ST_READY, ST_REUSE))

    def path_map(self) -> Dict[str, str]:
        """old dataset path -> new dataset path for successfully copied/reused items."""
        return {i.src_path: i.dest_path for i in self.items if i.usable}


# ----------------------------------------------------------------------
# Which files make up a dataset?
# ----------------------------------------------------------------------

def dataset_files(path: str) -> List[str]:
    """All files belonging to a single-file dataset (main file + sidecars)."""
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    files = [path]
    try:
        entries = os.listdir(folder)
    except OSError:
        return files
    nl, sl = name.lower(), stem.lower()
    for e in entries:
        el = e.lower()
        full = os.path.join(folder, e)
        if el == nl or not os.path.isfile(full):
            continue
        if el.startswith(nl + ".") or any(el == nl + s for s in MAIN_SUFFIXES):
            files.append(full)                       # dem.tif.aux.xml, x.gpkg-wal
        elif el.startswith(sl + ".") and el[len(sl) + 1:] in SIDE_REMAINDERS:
            files.append(full)                       # roads.dbf, roads.shp.xml
    return files


def _norm(p: str) -> str:
    p = clean_path(p)
    return p.lower() if IS_WINDOWS else p


def _drive_label(path: str) -> str:
    drive = os.path.splitdrive(path)[0]
    return drive.replace(":", "").replace("\\", "").replace("/", "_").strip("_") or "root"


def _common_root(paths: List[str]) -> str:
    dirs = [os.path.dirname(p.rstrip("/")) for p in paths]
    if not dirs:
        return ""
    try:
        return clean_path(os.path.commonpath(dirs))
    except ValueError:      # different drives
        return ""


# ----------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------

NAMING_NUMBER = "number"       # roads_2.shp
NAMING_FOLDER = "folder"       # 20260521_roads.shp (source folder in front)
NAMING_PREFIX = "prefix"       # <prefix>roads.shp


def build_plan(records: List[LayerRecord], target: str, keep_structure: bool = False,
               naming: str = NAMING_NUMBER, prefix: str = "", overrides: Optional[Dict] = None,
               analyse: bool = True) -> CopyPlan:
    """Plan copying the datasets of ``records`` into ``target``.

    keep_structure=False (default): every dataset goes directly into ``target``.
    keep_structure=True: sub-folders below the datasets' common folder are kept
    (datasets from different drives get one sub-folder per drive).

    Name clashes never overwrite anything: an identical copy already in the
    destination is reused, otherwise the dataset gets a free name (``name_2``).
    """
    target = clean_path(target)
    skipped = []
    datasets: "OrderedDict[str, CopyItem]" = OrderedDict()
    for r in records:
        if not r.is_file_based:
            skipped.append(f"{r.name}: {r.source_kind.lower()} - nothing to copy")
            continue
        key = _norm(r.path)
        item = datasets.get(key)
        if item is None:
            item = CopyItem(src_path=clean_path(r.path), dest_path="",
                            is_dir=os.path.isdir(r.path))
            datasets[key] = item
        item.layer_ids.append(r.layer_id)
        item.layer_names.append(r.name)

    items = list(datasets.values())
    root = _common_root([i.src_path for i in items]) if keep_structure else ""
    claimed = set()
    for item in items:
        if not keep_structure:
            item.dest_path = clean_path(os.path.join(target, os.path.basename(item.src_path.rstrip("/"))))
        elif root:
            item.dest_path = clean_path(os.path.join(target, os.path.relpath(item.src_path, root)))
        else:  # several drives: <target>/<drive>/<path without drive>
            _drive, rest = os.path.splitdrive(item.src_path)
            item.dest_path = clean_path(os.path.join(target, _drive_label(item.src_path),
                                                     rest.lstrip("/\\")))
        _plan_item(item, target, claimed, naming, prefix, (overrides or {}).get(item.src_path),
                   analyse)

    free = -1
    try:
        probe = target
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if probe:
            free = shutil.disk_usage(probe).free
    except OSError:
        pass
    plan = CopyPlan(target, root, items, skipped, free)
    if not plan.enough_space:
        plan.message = (f"Not enough free space: {human_size(plan.copy_bytes)} needed, "
                        f"{human_size(free)} free")
    return plan


def _split_name(name: str):
    low = name.lower()
    for dbl in (".copc.laz",):
        if low.endswith(dbl):
            return name[:-len(dbl)], name[-len(dbl):]
    return os.path.splitext(name)


def _dest_files(src: str, dest: str, is_dir: bool) -> List[Tuple[str, str]]:
    """(src, dst) pairs; sidecars follow the dataset's (possibly new) name."""
    if is_dir:
        out = []
        for root, _dirs, files in os.walk(src):
            for f in files:
                s = os.path.join(root, f)
                out.append((clean_path(s), clean_path(os.path.join(dest, os.path.relpath(s, src)))))
        return out
    old_name, new_name = os.path.basename(src), os.path.basename(dest)
    old_stem, new_stem = _split_name(old_name)[0], _split_name(new_name)[0]
    dst_dir = os.path.dirname(dest)
    out = []
    for s in dataset_files(src):
        b = os.path.basename(s)
        if b.lower().startswith(old_name.lower()):
            nb = new_name + b[len(old_name):]
        elif b.lower().startswith(old_stem.lower() + "."):
            nb = new_stem + b[len(old_stem):]
        else:
            nb = b
        out.append((clean_path(s), clean_path(os.path.join(dst_dir, nb))))
    return out


def _same_content(a: str, b: str) -> bool:
    """True only if two files are byte-for-byte identical (size check first, then content).

    Size alone is not enough: two different Shapefiles with equal-length
    attribute edits have the same sizes but different data.
    """
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _name_candidates(stem: str, ext: str, naming: str, prefix: str, src_folder: str):
    """Names to try, in order, when the wanted name is taken."""
    if naming == NAMING_FOLDER and src_folder:
        base = f"{src_folder}_{stem}"
    elif naming == NAMING_PREFIX and prefix:
        base = f"{prefix}{stem}"
    else:
        base = None
    if base:
        yield f"{base}{ext}"
        for n in range(2, 1000):
            yield f"{base}_{n}{ext}"
    else:
        for n in range(2, 1000):
            yield f"{stem}_{n}{ext}"


def _plan_item(item: CopyItem, target: str, claimed: set, naming: str = NAMING_NUMBER,
               prefix: str = "", override: Optional[str] = None, analyse: bool = True):
    from . import data_compare as dcmp
    src = item.src_path
    if not os.path.exists(src):
        item.status, item.message = ST_MISSING, "The original data is missing, so it can't be copied"
        return
    if _norm(src) == _norm(item.dest_path):
        item.status, item.message = ST_SAME, "Already in this folder"
        return
    if item.is_dir and (_norm(target) + "/").startswith(_norm(src) + "/"):
        item.status, item.message = ST_FAILED, "The destination is inside this dataset"
        return

    # user chose "use the existing file instead" (same data under another name)
    if override and override.startswith("use:"):
        existing = clean_path(override[4:])
        item.dest_path, item.files = existing, _dest_files(src, existing, item.is_dir)
        item.status, item.equivalent = ST_REUSE, True
        item.notice = f"Using the existing {os.path.basename(existing)} (same data) - nothing copied"
        item.level = "info"
        claimed.add(_norm(existing))
        return

    wanted = item.dest_path
    stem, ext = _split_name(os.path.basename(wanted))
    folder = os.path.dirname(wanted)
    files = _dest_files(src, wanted, item.is_dir)
    taken = _norm(wanted) in claimed or any(_norm(d) in claimed for _s, d in files)
    exists = os.path.exists(wanted) or any(os.path.exists(d) for _s, d in files)
    vector = analyse and dcmp.is_vector(src)

    if not taken and exists:
        all_there = all(os.path.exists(d) for _s, d in files)
        if all_there and all(_same_content(s, d) for s, d in files):
            item.dest_path, item.files = wanted, files
            item.status = ST_REUSE
            item.message = "A byte-for-byte identical copy is already there - it will be used"
            claimed.add(_norm(wanted))
            claimed.update(_norm(d) for _s, d in files)
            return
        rel, s_new, s_old = dcmp.compare(src, wanted) if (vector and os.path.exists(wanted)) \
            else (None, None, None)
        if rel == dcmp.SAME_DATA and override != "copy":
            # scenario 4: same name, same features (files differ only in bytes)
            item.dest_path, item.files = wanted, files
            item.status, item.equivalent = ST_REUSE, True
            item.notice = (f"Same data already there as {os.path.basename(wanted)} "
                           f"({dcmp.geometries(s_new)}, {dcmp.feature_count(s_new)} features) - "
                           "the layer will use it")
            item.level = "info"
            claimed.add(_norm(wanted))
            return
        if rel == dcmp.SAME_GEOM:
            # scenario 3: another version - same name and geometry, different features
            item.notice = (f"Another version is already there: same name and geometry "
                           f"({dcmp.geometries(s_new)}), {dcmp.feature_count(s_new)} vs "
                           f"{dcmp.feature_count(s_old)} features")
            item.level = "warn"
        elif rel == dcmp.DIFF_GEOM:
            # scenario 1: same name, different geometry type - a different dataset
            item.notice = (f"A different dataset with this name is already there "
                           f"({dcmp.geometries(s_old)} there, yours is {dcmp.geometries(s_new)}) - "
                           "both are kept")
            item.level = "info"
        elif rel == dcmp.SAME_DATA:      # override == "copy"
            item.notice = "Same data is already there - copied anyway as you chose"
            item.level = "info"
        else:
            item.notice = "A different file with this name is already there - both are kept"
            item.level = "info"
    elif taken:
        item.notice = "Another selected layer uses a different file with the same name - both are kept"
        item.level = "info"

    if taken or exists:
        src_folder = os.path.basename(os.path.dirname(src.rstrip("/")))
        for name in _name_candidates(stem, ext, naming, prefix, src_folder):
            dest = clean_path(os.path.join(folder, name))
            files = _dest_files(src, dest, item.is_dir)
            if _norm(dest) in claimed or any(_norm(d) in claimed for _s, d in files):
                continue
            if os.path.exists(dest) or any(os.path.exists(d) for _s, d in files):
                # an earlier copy under this name that is byte-identical -> reuse it
                if all(os.path.exists(d) and _same_content(s, d) for s, d in files):
                    item.dest_path, item.files = dest, files
                    item.status, item.renamed = ST_REUSE, True
                    item.notice = f"An identical copy is already there as {name} - it will be used"
                    item.level = "info"
                    item.message = item.notice
                    claimed.add(_norm(dest))
                    claimed.update(_norm(d) for _s, d in files)
                    return
                continue
            item.dest_path, item.files, item.renamed = dest, files, True
            item.notice += f". Yours is saved as {name}"
            item.message = item.notice
            break
        else:
            item.status, item.message = ST_FAILED, "No free file name found"
            return
    else:
        item.dest_path, item.files = wanted, files

    # scenario 2: the same data is already in the folder under a different name
    if vector and item.status == ST_READY and override != "copy":
        dup = dcmp.find_same_data(src, os.path.dirname(item.dest_path), exclude=[item.dest_path])
        if dup:
            item.duplicate_of = dup
            msg = f"The same data is already in this folder as {os.path.basename(dup)}"
            item.notice = msg + (f". {item.notice}" if item.notice else "")
            item.level = "warn"
    if item.notice and not item.message:
        item.message = item.notice

    claimed.add(_norm(item.dest_path))
    claimed.update(_norm(d) for _s, d in item.files)
    item.size = 0
    for s, _d in item.files:
        try:
            item.size += os.path.getsize(s)
        except OSError:
            pass


def delete_originals(items: List[CopyItem]) -> Tuple[List[str], List[str]]:
    """MOVE step: delete the original files of items that were copied and switched.

    Only files this tool copied (``item.files`` sources) are deleted, and only
    when every one of them has a verified copy of the same size.  Returns
    (deleted paths, error messages).
    """
    deleted, errors = [], []
    for item in items:
        if item.status not in (ST_COPIED, ST_REUSE):
            continue
        # verify again right before deleting: byte-identical copy, or (for a reused
        # file with the same features) identical feature fingerprints
        if item.equivalent:
            from . import data_compare as dcmp
            ok = os.path.exists(item.dest_path) and dcmp.same_data(item.src_path, item.dest_path)
        else:
            ok = all(os.path.exists(d) and _same_content(s, d)
                     for s, d in item.files if os.path.exists(s))
        if not ok:
            errors.append(f"{os.path.basename(item.src_path)}: copy could not be verified - "
                          "original kept")
            continue
        try:
            if item.is_dir:
                shutil.rmtree(item.src_path)
                deleted.append(item.src_path)
            else:
                for s, _d in item.files:
                    if os.path.exists(s):
                        os.remove(s)
                        deleted.append(s)
        except OSError as exc:
            errors.append(f"{os.path.basename(item.src_path)}: could not delete the original "
                          f"({exc.strerror or exc}). The copy is in place - delete it manually.")
    return deleted, errors


# ----------------------------------------------------------------------
# Execute (thread safe)
# ----------------------------------------------------------------------

def _copy_file(src: str, dst: str, feedback, done_bytes: int, total: int) -> int:
    part = dst + PART_SUFFIX
    with open(src, "rb") as fi, open(part, "wb") as fo:
        while True:
            if feedback is not None and feedback.isCanceled():
                raise InterruptedError("Cancelled")
            buf = fi.read(CHUNK)
            if not buf:
                break
            fo.write(buf)
            done_bytes += len(buf)
            if feedback is not None and total:
                feedback.setProgress(min(99.0, 100.0 * done_bytes / total))
    shutil.copystat(src, part)
    if os.path.getsize(part) != os.path.getsize(src):
        raise IOError(f"Size check failed for {os.path.basename(src)}")
    if os.path.exists(dst):   # appeared meanwhile - never overwrite
        raise FileExistsError(f"{dst} appeared during the copy; not overwritten")
    os.replace(part, dst)
    return done_bytes


def execute_plan(feedback: Optional[QgsFeedback], plan: CopyPlan) -> CopyPlan:
    total = max(1, plan.copy_bytes)
    done = 0
    for item in plan.items:
        if not item.needs_copy:
            continue
        if feedback is not None and feedback.isCanceled():
            item.status, item.message = ST_CANCELLED, "Not copied (stopped)"
            continue
        created_files, created_dirs = [], []
        try:
            for s, d in item.files:
                ddir = os.path.dirname(d)
                if not os.path.isdir(ddir):
                    # remember which folders we create (deepest last)
                    probe, new = ddir, []
                    while probe and not os.path.isdir(probe):
                        new.append(probe)
                        probe = os.path.dirname(probe)
                    os.makedirs(ddir, exist_ok=True)
                    created_dirs.extend(reversed(new))
                if os.path.exists(d):
                    raise FileExistsError(f"{os.path.basename(d)} already exists; not overwritten")
                done = _copy_file(s, d, feedback, done, total)
                created_files.append(d)
            item.status, item.message = ST_COPIED, f"{len(item.files)} file(s) copied"
        except InterruptedError:
            _cleanup(created_files, created_dirs, item)
            item.status, item.message = ST_CANCELLED, "Stopped - partial copy removed"
        except Exception as exc:
            _cleanup(created_files, created_dirs, item)
            item.status, item.message = ST_FAILED, f"Copy failed: {exc} (partial copy removed)"
    if feedback is not None and not feedback.isCanceled():
        feedback.setProgress(100.0)
    return plan


def _cleanup(created_files, created_dirs, item):
    """Remove only what this copy created (files, *.lpm-part leftovers, new empty folders)."""
    for f in created_files:
        try:
            os.remove(f)
        except OSError:
            pass
    for _s, d in item.files:
        try:
            if os.path.exists(d + PART_SUFFIX):
                os.remove(d + PART_SUFFIX)
        except OSError:
            pass
    for d in sorted(created_dirs, key=len, reverse=True):
        try:
            os.rmdir(d)          # only succeeds if empty
        except OSError:
            pass

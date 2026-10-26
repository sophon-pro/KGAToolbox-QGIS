"""Persistent background index of geodata for fast, rich search.

Like Premiere Pro's media-intelligence analysis or ArcGIS Pro's item
index, the expensive part (opening every dataset to read its layers,
fields, CRS and extent) runs once in the background; afterwards searches
answer instantly from a small SQLite database, and re-indexing only
re-reads files whose size or modification time changed.

Indexing happens in two passes inside a QgsTask:
  1. "names" pass - a fast directory walk (os.scandir + stat) so file
     names are searchable within seconds;
  2. "deep" pass - GDAL/OGR open each new/changed dataset and record
     layer names, geometry types, field names, CRS, feature counts and
     the extent in WGS 84 (used by the "in map extent" filter).
"""

import json
import os
import sqlite3
import time

from qgis.core import QgsApplication, QgsMessageLog, QgsTask

from . import inspector
from .compat import ignore

try:
    from osgeo import gdal, ogr, osr
except ImportError:  # pragma: no cover
    gdal = ogr = osr = None

LOG_TAG = "Data Explorer"
SCHEMA_VERSION = 3
MAX_FILES_PER_ROOT = 300000
CSV_BBOX_MAX_BYTES = 50 * 1024 * 1024
SKIP_DIRS = {"$recycle.bin", "system volume information", "__pycache__", "node_modules",
             ".git", ".svn", "appdata", "library", "proc", "sys", "dev"}


def db_path():
    base = QgsApplication.qgisSettingsDirPath()
    return os.path.join(base, "kga_data_ingest_index.sqlite")


def connect(path=None):
    con = sqlite3.connect(path or db_path(), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(con)
    return con


def _ensure_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS datasets (
            path TEXT PRIMARY KEY, folder TEXT, name TEXT, ext TEXT, kind TEXT,
            size INTEGER, mtime REAL, deep INTEGER DEFAULT 0, info TEXT,
            xmin REAL, ymin REAL, xmax REAL, ymax REAL, error TEXT);
        CREATE INDEX IF NOT EXISTS idx_datasets_folder ON datasets(folder);
        CREATE TABLE IF NOT EXISTS roots (path TEXT PRIMARY KEY, last_scan REAL, files INTEGER);
    """)
    row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION:
        con.executescript("DELETE FROM datasets; DELETE FROM roots;")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        con.commit()


def norm_path(path):
    return os.path.normcase(os.path.abspath(path.rstrip("/\\") or path))


# ---------------------------------------------------------------------------
# Reading the index (main thread)
# ---------------------------------------------------------------------------

_SELECT_ALL = "SELECT path, folder, name, ext, kind, size, mtime, deep, info, xmin, ymin, xmax, ymax FROM datasets"
_SELECT_TREE = _SELECT_ALL + " WHERE folder = ? OR folder LIKE ?"
_SELECT_FOLDER = _SELECT_ALL + " WHERE folder = ?"


def load_records(scope_folders=None, recursive=True, con=None):
    """Return dataset records under the given folders (or everything)."""
    own = con is None
    con = con or connect()
    try:
        rows = []
        if not scope_folders:
            rows = con.execute(_SELECT_ALL).fetchall()
        else:
            for f in scope_folders:
                nf = norm_path(f)
                if recursive:
                    like = nf.rstrip("/\\") + os.sep + "%"
                    rows += con.execute(_SELECT_TREE, (nf, like)).fetchall()
                else:
                    rows += con.execute(_SELECT_FOLDER, (nf,)).fetchall()
        out, seen = [], set()
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            info = {}
            if r[8]:
                try:
                    info = json.loads(r[8])
                except ValueError:
                    info = {}
            out.append({"path": info.get("display_path", r[0]), "key": r[0], "folder": r[1],
                        "name": r[2], "ext": r[3], "kind": r[4], "size": r[5], "mtime": r[6],
                        "deep": bool(r[7]), "layers": info.get("layers", []),
                        "bbox": None if r[9] is None else (r[9], r[10], r[11], r[12])})
        return out
    finally:
        if own:
            con.close()


def root_status(con=None):
    own = con is None
    con = con or connect()
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, last_scan, files FROM roots")}
    finally:
        if own:
            con.close()


def forget_root(path):
    con = connect()
    try:
        nf = norm_path(path)
        con.execute("DELETE FROM roots WHERE path = ?", (nf,))
        con.execute("DELETE FROM datasets WHERE folder = ? OR folder LIKE ?",
                    (nf, nf.rstrip("/\\") + os.sep + "%"))
        con.commit()
    finally:
        con.close()


def clear_index():
    con = connect()
    try:
        con.executescript("DELETE FROM datasets; DELETE FROM roots;")
        con.commit()
    finally:
        con.close()


def index_counts():
    con = connect()
    try:
        total = con.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        deep = con.execute("SELECT COUNT(*) FROM datasets WHERE deep = 1").fetchone()[0]
        return total, deep
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Metadata extraction (worker thread, GDAL/OGR only - no QGIS GUI objects)
# ---------------------------------------------------------------------------

def _authid(srs):
    if srs is None:
        return ""
    try:
        s = srs.Clone()
        if s.GetAuthorityCode(None) is None:
            s.AutoIdentifyEPSG()
        name, code = s.GetAuthorityName(None), s.GetAuthorityCode(None)
        if name and code:
            return "%s:%s" % (name, code)
        return s.GetName() or ""
    except Exception:
        return ""


def _srs_name(srs):
    if srs is None:
        return ""
    try:
        return srs.GetName() or ""
    except Exception:
        return ""


def _to_wgs84(srs, xmin, ymin, xmax, ymax):
    if None in (xmin, ymin, xmax, ymax):
        return None
    try:
        if srs is None:
            # assume lon/lat if it looks like it
            if -181 <= xmin <= 181 and -91 <= ymin <= 91 and -181 <= xmax <= 181 and -91 <= ymax <= 91:
                return (xmin, ymin, xmax, ymax)
            return None
        src = srs.Clone()
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(4326)
        for s in (src, dst):
            try:
                s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception as exc:
                ignore(exc)
        ct = osr.CoordinateTransformation(src, dst)
        if hasattr(ct, "TransformBounds"):
            b = ct.TransformBounds(xmin, ymin, xmax, ymax, 21)
            return (b[0], b[1], b[2], b[3])
        xs, ys = [], []
        for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
            p = ct.TransformPoint(x, y)
            xs.append(p[0])
            ys.append(p[1])
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return None


def _merge_bbox(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _open(src, flags):
    """gdal.OpenEx that never raises (GDAL may be in exception mode)."""
    try:
        return gdal.OpenEx(src, flags | gdal.OF_READONLY)
    except Exception:
        return None


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        QgsMessageLog.logMessage("Index: %s on %s: %s" % (fn.__name__, args[0], exc), LOG_TAG)
        return []


def _vector_layers(src):
    out = []
    ds = _open(src, gdal.OF_VECTOR)
    if ds is None:
        return out
    for i in range(ds.GetLayerCount()):
        try:
            out.append(_vector_layer_info(ds.GetLayerByIndex(i)))
        except Exception as exc:
            ignore(exc)
            continue
    ds = None
    return [o for o in out if o]


def _vector_layer_info(lyr):
    if lyr is None:
        return None
    defn = lyr.GetLayerDefn()
    gtype = lyr.GetGeomType()
    srs = lyr.GetSpatialRef()
    try:
        count = lyr.GetFeatureCount(0)
    except Exception:
        count = -1
    extent = None
    if gtype != ogr.wkbNone:
        try:
            e = lyr.GetExtent(0, 1)
            if e:
                extent = (e[0], e[2], e[1], e[3])
        except Exception:
            extent = None
    return {
        "name": lyr.GetName(),
        "kind": "table" if gtype == ogr.wkbNone else "vector",
        "geom": "None" if gtype == ogr.wkbNone else ogr.GeometryTypeToName(gtype),
        "crs": _authid(srs), "crs_name": _srs_name(srs),
        "count": count if count is not None else -1,
        "fields": [defn.GetFieldDefn(j).GetName() for j in range(defn.GetFieldCount())],
        "bbox": _to_wgs84(srs, *extent) if extent else None,
    }


def _raster_layers(src, name):
    out = []
    ds = _open(src, gdal.OF_RASTER)
    if ds is None:
        return out
    subs = ds.GetSubDatasets() or []
    if ds.RasterCount == 0 and subs:
        for sub_name, desc in subs[:200]:
            out.append({"name": desc.split(" ")[0] if desc else sub_name, "kind": "raster",
                        "geom": "", "crs": "", "crs_name": "", "count": -1, "fields": [],
                        "bbox": None, "subdataset": sub_name})
        ds = None
        return out
    if ds.RasterCount > 0:
        gt = ds.GetGeoTransform(can_return_null=True)
        srs = ds.GetSpatialRef()
        bbox = None
        if gt:
            w, h = ds.RasterXSize, ds.RasterYSize
            xs = [gt[0], gt[0] + w * gt[1] + h * gt[2], gt[0] + w * gt[1], gt[0] + h * gt[2]]
            ys = [gt[3], gt[3] + w * gt[4] + h * gt[5], gt[3] + w * gt[4], gt[3] + h * gt[5]]
            bbox = _to_wgs84(srs, min(xs), min(ys), max(xs), max(ys))
        band = ds.GetRasterBand(1)
        out.append({
            "name": name, "kind": "raster",
            "geom": "%dx%d, %d band(s), %s" % (ds.RasterXSize, ds.RasterYSize, ds.RasterCount,
                                               gdal.GetDataTypeName(band.DataType)),
            "crs": _authid(srs), "crs_name": _srs_name(srs),
            "count": -1, "fields": [ds.GetRasterBand(b).GetDescription() or "band%d" % b
                                    for b in range(1, min(ds.RasterCount, 50) + 1)],
            "bbox": bbox,
        })
    ds = None
    return out


def _csv_layer(path, name):
    sniff = inspector.sniff_csv(path)
    bbox = None
    if sniff["x_field"] and sniff["y_field"] and os.path.getsize(path) <= CSV_BBOX_MAX_BYTES:
        import csv
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                reader = csv.DictReader(fh, delimiter=sniff["delimiter"])
                xs, ys = [], []
                for row in reader:
                    try:
                        xs.append(float(row[sniff["x_field"]]))
                        ys.append(float(row[sniff["y_field"]]))
                    except (TypeError, ValueError, KeyError):
                        continue
                if xs:
                    bbox = _to_wgs84(None, min(xs), min(ys), max(xs), max(ys))
        except OSError:
            pass
    kind = "vector" if (sniff["x_field"] or sniff["wkt_field"]) else "table"
    return [{"name": name, "kind": kind,
             "geom": "Point (from %s/%s)" % (sniff["x_field"], sniff["y_field"]) if sniff["x_field"]
             else ("WKT" if sniff["wkt_field"] else "None"),
             "crs": "EPSG:4326" if bbox else "", "crs_name": "", "count": -1,
             "fields": sniff["fields"], "bbox": bbox}]


def extract_metadata(path):
    """Return (layers, bbox_wgs84, error) for one dataset."""
    if gdal is None:
        return [], None, "GDAL not available"
    name = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
    kind = inspector.guess_kind_from_extension(path)
    if kind in (inspector.KIND_POINTCLOUD, inspector.KIND_MESH):
        return [], None, ""
    src = inspector.source_uri_for(path)
    layers = []
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        if inspector.is_delimited_text(path):
            layers = _csv_layer(path, name)
        elif inspector.is_container(path):
            layers = _safe(_vector_layers, src) + _safe(_raster_layers, src, name)
        elif kind == inspector.KIND_RASTER:
            layers = _safe(_raster_layers, src, name) or _safe(_vector_layers, src)
        else:
            layers = _safe(_vector_layers, src) or _safe(_raster_layers, src, name)
    except Exception as exc:
        return layers, None, str(exc)
    finally:
        gdal.PopErrorHandler()
    bbox = None
    for lyr in layers:
        bbox = _merge_bbox(bbox, lyr.get("bbox"))
    return layers, bbox, "" if layers else "not readable by GDAL/OGR"


# ---------------------------------------------------------------------------
# Background indexing task
# ---------------------------------------------------------------------------

def _walk(root, is_canceled):
    count = 0
    stack = [root]
    while stack:
        if is_canceled():
            return
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            name = e.name
            if name.startswith("."):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if os.path.splitext(name)[1].lower() in inspector.DIR_DATASETS:
                    yield e.path
                    count += 1
                elif name.lower() not in SKIP_DIRS:
                    stack.append(e.path)
                continue
            if not inspector.is_geodata_candidate(e.path):
                continue
            stem, ext = os.path.splitext(e.path)
            if ext.lower() in (".dbf", ".prj", ".shx", ".cpg") and (
                    os.path.exists(stem + ".shp") or os.path.exists(stem + ".SHP")):
                continue
            yield e.path
            count += 1
            if count >= MAX_FILES_PER_ROOT:
                return


def _stat(path):
    try:
        if os.path.isdir(path):
            return inspector.dataset_size_bytes(path), os.path.getmtime(path)
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return None, None


class IndexTask(QgsTask):
    """Incrementally (re)indexes one or more folder trees."""

    def __init__(self, roots, deep=True, on_done=None, db=None):
        label = ", ".join(os.path.basename(r.rstrip("/\\")) or r for r in roots[:3])
        super().__init__("Data Explorer: indexing %s" % label, QgsTask.Flag.CanCancel)
        self.roots = [r for r in roots if r and os.path.isdir(r)]
        self.deep = deep
        self.on_done = on_done
        self.db = db
        self.stats = {"files": 0, "new": 0, "changed": 0, "removed": 0, "deep": 0, "errors": 0}
        self.phase = "names"

    def run(self):
        try:
            con = connect(self.db)
        except Exception as exc:
            QgsMessageLog.logMessage("Index unavailable: %s" % exc, LOG_TAG)
            return False
        try:
            for root in self.roots:
                if self.isCanceled():
                    return False
                self._names_pass(con, root)
            if self.deep:
                self.phase = "deep"
                self._deep_pass(con)
            return not self.isCanceled()
        finally:
            con.close()

    def _names_pass(self, con, root):
        nroot = norm_path(root)
        like = nroot.rstrip("/\\") + os.sep + "%"
        existing = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT path, size, mtime FROM datasets WHERE folder = ? OR folder LIKE ?", (nroot, like))}
        seen = set()
        batch = 0
        for path in _walk(root, self.isCanceled):
            key = norm_path(path)
            seen.add(key)
            size, mtime = _stat(path)
            if size is None:
                continue
            self.stats["files"] += 1
            old = existing.get(key)
            if old is not None and old[0] == size and abs((old[1] or 0) - mtime) < 1e-3:
                continue
            self.stats["new" if old is None else "changed"] += 1
            name = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
            info = json.dumps({"display_path": path, "layers": []})
            con.execute(
                "INSERT OR REPLACE INTO datasets (path, folder, name, ext, kind, size, mtime, deep, info,"
                " xmin, ymin, xmax, ymax, error) VALUES (?,?,?,?,?,?,?,0,?,NULL,NULL,NULL,NULL,NULL)",
                (key, norm_path(os.path.dirname(path.rstrip("/\\"))), name,
                 os.path.splitext(path.rstrip("/\\"))[1].lower(),
                 inspector.guess_kind_from_extension(path), size, mtime, info))
            batch += 1
            if batch >= 500:
                con.commit()
                batch = 0
                self.setProgress(min(30.0, self.progress() + 1))
        if self.isCanceled():
            con.commit()
            return
        removed = [k for k in existing if k not in seen]
        for k in removed:
            con.execute("DELETE FROM datasets WHERE path = ?", (k,))
        self.stats["removed"] += len(removed)
        con.execute("INSERT OR REPLACE INTO roots VALUES (?,?,?)", (nroot, time.time(), len(seen)))
        con.commit()

    def _deep_pass(self, con):
        todo = []
        for root in self.roots:
            nroot = norm_path(root)
            todo += con.execute(
                "SELECT path, info, kind FROM datasets WHERE deep = 0 AND (folder = ? OR folder LIKE ?)",
                (nroot, nroot.rstrip("/\\") + os.sep + "%")).fetchall()
        if not todo:
            return
        total = max(len(todo), 1)
        for i, (key, info_json, kind) in enumerate(todo):
            if self.isCanceled():
                break
            try:
                display = json.loads(info_json).get("display_path", key)
            except (ValueError, TypeError):
                display = key
            layers, bbox, err = extract_metadata(display)
            kinds = {lyr["kind"] for lyr in layers}
            if kinds and kind in (inspector.KIND_UNKNOWN, inspector.KIND_TABLE, inspector.KIND_VECTOR):
                kind = "vector" if "vector" in kinds else ("raster" if "raster" in kinds else "table")
            con.execute(
                "UPDATE datasets SET deep = 1, info = ?, kind = ?, xmin = ?, ymin = ?, xmax = ?, ymax = ?,"
                " error = ? WHERE path = ?",
                (json.dumps({"display_path": display, "layers": layers}), kind,
                 *(bbox if bbox else (None, None, None, None)), err or None, key))
            self.stats["deep"] += 1
            if err:
                self.stats["errors"] += 1
            if i % 50 == 0:
                con.commit()
            self.setProgress(30 + 70.0 * (i + 1) / total)
        con.commit()

    def finished(self, result):
        if self.on_done is not None:
            self.on_done(self, bool(result))

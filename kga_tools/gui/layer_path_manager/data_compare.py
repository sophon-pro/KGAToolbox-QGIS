# -*- coding: utf-8 -*-
"""
Layer Path Manager - compare the *contents* of two vector datasets.

Used when data is copied/moved into a folder that already contains files:

* geometry type   (Point / Line / Polygon / Table, multi-types count as the same)
* feature count, attribute field names and CRS   (cheap "summary")
* a fingerprint of every feature (geometry + attribute values) - computed only
  when the summaries match.  Feature order does not matter.

Datasets are opened READ-ONLY through GDAL/OGR (bundled with QGIS); nothing is
written.  Results are cached by path + modification time + size.
"""

import hashlib
import os
from typing import Dict, Optional, Tuple

try:
    from osgeo import gdal, ogr
except ImportError:  # pragma: no cover - GDAL always ships with QGIS
    gdal = ogr = None

VECTOR_EXTS = {".shp", ".gpkg", ".geojson", ".json", ".fgb", ".kml", ".gml", ".tab", ".mif",
               ".sqlite", ".gpx", ".parquet"}
MAX_SCAN_FILES = 500                     # files looked at per folder for duplicates
MAX_HASH_FEATURES = 2_000_000           # safety limit for fingerprints

SAME_DATA = "same_data"     # same geometry type and identical features
SAME_GEOM = "same_geom"     # same geometry type, different features (another version)
DIFF_GEOM = "diff_geom"     # different geometry type (a different dataset)

_summary_cache: Dict[Tuple, Optional[dict]] = {}
_hash_cache: Dict[Tuple, Optional[str]] = {}


def is_vector(path: str) -> bool:
    p = path.lower().rstrip("/")
    return os.path.splitext(p)[1] in VECTOR_EXTS or p.endswith(".gdb")


def _key(path: str) -> Tuple:
    try:
        st = os.stat(path)
        return (path, st.st_mtime, st.st_size)
    except OSError:
        return (path, None, None)


def _open(path: str):
    if ogr is None:
        return None
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        return ogr.Open(path, 0)          # 0 = read-only
    except Exception:
        return None
    finally:
        gdal.PopErrorHandler()


def geometry_family(geom_type: int) -> str:
    name = (ogr.GeometryTypeToName(geom_type) or "").lower()
    if "point" in name:
        return "Point"
    if "line" in name or ("curve" in name and "polygon" not in name):
        return "Line"
    if "polygon" in name or "surface" in name:
        return "Polygon"
    if geom_type == ogr.wkbNone or "none" in name:
        return "Table"
    return "Other"


def summary(path: str) -> Optional[dict]:
    """{'layers': [{name, geom, count, fields, crs}], 'multi': bool} or None."""
    k = _key(path)
    if k in _summary_cache:
        return _summary_cache[k]
    out = None
    ds = _open(path) if is_vector(path) else None
    if ds is not None:
        layers = []
        try:
            for i in range(ds.GetLayerCount()):
                lyr = ds.GetLayer(i)
                defn = lyr.GetLayerDefn()
                srs = lyr.GetSpatialRef()
                crs = ""
                if srs is not None:
                    try:
                        srs.AutoIdentifyEPSG()
                    except Exception:  # CRS not identifiable: keep the WKT-based value
                        crs = crs or ""
                    code = srs.GetAuthorityCode(None)
                    crs = f"EPSG:{code}" if code else srs.ExportToProj4().strip()
                layers.append({
                    "name": lyr.GetName(),
                    "geom": geometry_family(lyr.GetGeomType()),
                    "count": lyr.GetFeatureCount(),
                    "fields": tuple(sorted(defn.GetFieldDefn(j).GetName().lower()
                                           for j in range(defn.GetFieldCount()))),
                    "crs": crs,
                })
            out = {"layers": layers, "multi": len(layers) > 1}
        finally:
            ds = None
    _summary_cache[k] = out
    return out


def _cheap(s: dict) -> Tuple:
    """Comparable summary; layer names only matter for multi-layer containers."""
    return tuple((x["name"] if s["multi"] else "", x["geom"], x["count"], x["fields"], x["crs"])
                 for x in sorted(s["layers"], key=lambda x: x["name"]))


def geometries(s: Optional[dict]) -> str:
    if not s or not s["layers"]:
        return "?"
    return "/".join(sorted({x["geom"] for x in s["layers"]}))


def feature_count(s: Optional[dict]) -> int:
    return sum(max(0, x["count"]) for x in s["layers"]) if s else 0


def fingerprint(path: str) -> Optional[str]:
    """Order-independent hash of every feature's geometry and attribute values."""
    k = _key(path)
    if k in _hash_cache:
        return _hash_cache[k]
    result = None
    ds = _open(path)
    if ds is not None:
        try:
            layer_hashes = []
            multi = ds.GetLayerCount() > 1
            total = 0
            for i in range(ds.GetLayerCount()):
                lyr = ds.GetLayer(i)
                defn = lyr.GetLayerDefn()
                names = sorted(range(defn.GetFieldCount()),
                               key=lambda j: defn.GetFieldDefn(j).GetName().lower())
                feats = []
                lyr.ResetReading()
                for f in lyr:
                    total += 1
                    if total > MAX_HASH_FEATURES:
                        raise OverflowError
                    g = f.GetGeometryRef()
                    wkb = bytes(g.ExportToIsoWkb()) if g is not None else b""
                    attrs = repr([f.GetField(j) for j in names]).encode("utf-8", "replace")
                    feats.append(hashlib.sha256(wkb + b"|" + attrs).hexdigest())
                feats.sort()
                h = hashlib.sha256("".join(feats).encode()).hexdigest()
                layer_hashes.append(((lyr.GetName() if multi else ""), h))
            layer_hashes.sort()
            result = hashlib.sha256(repr(layer_hashes).encode()).hexdigest()
        except OverflowError:
            result = None
        except Exception:
            result = None
        finally:
            ds = None
    _hash_cache[k] = result
    return result


def compare(a: str, b: str) -> Tuple[Optional[str], Optional[dict], Optional[dict]]:
    """Relation between datasets ``a`` and ``b``: SAME_DATA / SAME_GEOM / DIFF_GEOM / None."""
    sa, sb = summary(a), summary(b)
    if not sa or not sb or not sa["layers"] or not sb["layers"]:
        return None, sa, sb
    if geometries(sa) != geometries(sb):
        return DIFF_GEOM, sa, sb
    if _cheap(sa) == _cheap(sb):
        fa, fb = fingerprint(a), fingerprint(b)
        if fa is not None and fa == fb:
            return SAME_DATA, sa, sb
    return SAME_GEOM, sa, sb


def find_same_data(path: str, folder: str, exclude=()) -> Optional[str]:
    """Another dataset in ``folder`` (any name) holding exactly the same features as ``path``."""
    sa = summary(path)
    if not sa or not sa["layers"] or not os.path.isdir(folder):
        return None
    want = _cheap(sa)
    excl = {os.path.normcase(os.path.abspath(e)) for e in exclude}
    try:
        names = sorted(os.listdir(folder))[:MAX_SCAN_FILES * 4]
    except OSError:
        return None
    seen = 0
    for n in names:
        cand = os.path.join(folder, n)
        if not is_vector(cand) or os.path.normcase(os.path.abspath(cand)) in excl:
            continue
        seen += 1
        if seen > MAX_SCAN_FILES:
            break
        sb = summary(cand)
        if sb and _cheap(sb) == want:
            fa, fb = fingerprint(path), fingerprint(cand)
            if fa is not None and fa == fb:
                return cand.replace(os.sep, "/")
    return None


def same_data(a: str, b: str) -> bool:
    rel, _sa, _sb = compare(a, b)
    return rel == SAME_DATA

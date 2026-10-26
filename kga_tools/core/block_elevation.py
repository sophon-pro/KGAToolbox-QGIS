# -*- coding: utf-8 -*-
"""Highest / lowest DEM point per block - the engine behind Paddy Irrigation Tools.

For every feature of a polygon or line layer, find the highest (and/or lowest)
DEM cell and report it as a point. Polygons use the cell-centre rule of QGIS
zonal statistics; lines use every cell the line passes through. Optional steps:
resample the DEM first, clean the block geometry on a working copy, write the
results back into the block layers, and save to GeoPackage / Shapefile / Excel.

Nothing here builds a widget. `run_analysis` is called by
`gui/block_elevation_dialog.py` on the GUI thread and reports through `log`.
"""

import contextlib
import math
import os
import tempfile
import time

from qgis.core import (
    NULL, Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsMapLayer, QgsPointXY, QgsRectangle, QgsSpatialIndex, QgsVectorFileWriter,
    QgsProcessingFeatureSourceDefinition, QgsProcessingFeedback, QgsProject,
    QgsRasterLayer, QgsVectorDataProvider, QgsVectorLayer,
)

from .compat import T_DOUBLE, T_INT, T_STRING, make_field

try:
    POLYGON_GEOM, LINE_GEOM = Qgis.GeometryType.Polygon, Qgis.GeometryType.Line
except AttributeError:                              # QGIS < 3.30
    from qgis.core import QgsWkbTypes
    POLYGON_GEOM = QgsWkbTypes.GeometryType.PolygonGeometry
    LINE_GEOM = QgsWkbTypes.GeometryType.LineGeometry
RESERVED = {"src_layer", "block_fid", "elev", "x", "y", "lon", "lat", "top_cells",
            "dist_along", "fid"}

try:
    WKB_NOGEOM = Qgis.WkbType.NoGeometry
except AttributeError:                              # QGIS < 3.30
    from qgis.core import QgsWkbTypes
    WKB_NOGEOM = QgsWkbTypes.Type.NoGeometry

KEY = "_bzid"
TEMP = "TEMPORARY_OUTPUT"
MAX_CELLS = 60_000_000  # cap for the temporary subdivided DEM (~240 MB float32)

try:
    CAP_ADD = Qgis.VectorProviderCapability.AddAttributes
    CAP_CHANGE = Qgis.VectorProviderCapability.ChangeAttributeValues
except AttributeError:                              # QGIS < 3.34
    CAP_ADD = QgsVectorDataProvider.Capability.AddAttributes
    CAP_CHANGE = QgsVectorDataProvider.Capability.ChangeAttributeValues


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
class LogFeedback(QgsProcessingFeedback):
    def __init__(self, logfn):
        super().__init__()
        self._log = logfn

    def reportError(self, error, fatalError=False):
        self._log("  [processing] " + str(error))

    def pushWarning(self, warning):
        self._log("  [processing warning] " + str(warning))


def as_layer(obj, kind="vector"):
    if isinstance(obj, QgsMapLayer):
        return obj
    return QgsVectorLayer(obj, "tmp", "ogr") if kind == "vector" else QgsRasterLayer(obj, "tmp")


def num(v):
    """QVariant NULL / None -> None, else float."""
    if v is None or v == NULL:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


RES_CHOICES = [("Native (DEM's own resolution)", None), ("0.5 m", 0.5), ("1 m", 1.0),
               ("30 m", 30.0), ("Custom...", "custom")]
RESAMPLE_NAMES = {0: "nearest neighbour", 5: "average", 7: "maximum", 8: "minimum"}


def snapped_window(grid_ext, zext, cx, cy, clamp=True, pad=1):
    """Window around zext, snapped to the grid origin of grid_ext, with a one-cell margin.
    Returns (xmin, xmax, ymin, ymax)."""
    gx, gy = grid_ext.xMinimum(), grid_ext.yMaximum()
    lo = 0 if clamp else -10 ** 9  # unclamped windows may start before the grid origin
    xmin = gx + max(lo, math.floor((zext.xMinimum() - gx) / cx) - pad) * cx
    xmax = gx + (math.ceil((zext.xMaximum() - gx) / cx) + pad) * cx
    ymax = gy - max(lo, math.floor((gy - zext.yMaximum()) / cy) - pad) * cy
    ymin = gy - (math.ceil((gy - zext.yMinimum()) / cy) + pad) * cy
    if clamp:
        xmax = min(xmax, grid_ext.xMaximum())
        ymin = max(ymin, grid_ext.yMinimum())
    return xmin, xmax, ymin, ymax


def coarsen_exact(src_path, band, window, kx, ky, method, nodata, out_path, log):
    """Exact block aggregation for integer ratios (e.g. 0.5 m -> 30 m = 60 x 60 source cells).
    window = (xmin, xmax, ymin, ymax) aligned to the source grid. Reads the DEM in strips,
    so memory use stays small even for large 0.5 m DEMs."""
    import numpy as np
    from osgeo import gdal
    ds = gdal.Open(src_path)
    if ds is None:
        return False
    gt = ds.GetGeoTransform()
    if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
        return False  # rotated rasters -> let GDAL warp handle it
    b = ds.GetRasterBand(band)
    src_nd = b.GetNoDataValue()
    sx, sy = gt[1], -gt[5]
    col0 = int(round((window[0] - gt[0]) / sx))
    row0 = int(round((gt[3] - window[3]) / sy))
    ncols = int(round((window[1] - window[0]) / (sx * kx)))
    nrows = int(round((window[3] - window[2]) / (sy * ky)))
    out = np.full((nrows, ncols), nodata, dtype="float32")
    W, H = ds.RasterXSize, ds.RasterYSize
    for r in range(nrows):
        r_a, r_b = row0 + r * ky, row0 + (r + 1) * ky
        ra, rb = max(r_a, 0), min(r_b, H)
        if ra >= rb:
            continue
        c_a, c_b = col0, col0 + ncols * kx
        ca, cb = max(c_a, 0), min(c_b, W)
        if ca >= cb:
            continue
        strip = np.full((ky, ncols * kx), np.nan)
        data = b.ReadAsArray(ca, ra, cb - ca, rb - ra).astype("float64")
        if src_nd is not None:
            data[data == src_nd] = np.nan
        strip[ra - r_a:rb - r_a, ca - c_a:cb - c_a] = data
        blocks = strip.reshape(ky, ncols, kx).transpose(1, 0, 2).reshape(ncols, -1)
        valid = ~np.all(np.isnan(blocks), axis=1)
        if not valid.any():
            continue
        if method == 7:
            vals = np.nanmax(blocks[valid], axis=1)
        elif method == 8:
            vals = np.nanmin(blocks[valid], axis=1)
        elif method == 5:
            vals = np.nanmean(blocks[valid], axis=1)
        else:  # nearest: the source cell at the block centre
            vals = strip[ky // 2].reshape(ncols, kx)[:, kx // 2][valid]
        vals = np.where(np.isnan(vals), nodata, vals)
        out[r, valid] = vals
    drv = gdal.GetDriverByName("GTiff")
    o = drv.Create(out_path, ncols, nrows, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE", "TILED=YES"])
    o.SetGeoTransform((window[0], sx * kx, 0, window[3], 0, -sy * ky))
    o.SetProjection(ds.GetProjection())
    ob = o.GetRasterBand(1)
    ob.SetNoDataValue(nodata)
    ob.WriteArray(out)
    o = None
    ds = None
    return True


def locate_max(ds, band, geom, chunk_rows=2048, touched_only=False, stats=False, lowest=False):
    """Find the highest valid cell inside geom (same CRS as the raster).
    Polygons: cell-centre rule (like QGIS zonal statistics); if no centre falls inside,
    retries with 'all touched'. Lines (touched_only=True): every cell the line touches.
    Returns (value, x, y, n_cells_with_that_value) or None;
    with stats=True: (value, x, y, ties, min, mean, count)."""
    import numpy as np
    from osgeo import gdal
    gt = ds.GetGeoTransform()
    if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
        return None
    b = ds.GetRasterBand(band)
    nd = b.GetNoDataValue()
    W, H = ds.RasterXSize, ds.RasterYSize
    bb = geom.boundingBox()
    c0 = max(0, int(math.floor((bb.xMinimum() - gt[0]) / gt[1])))
    c1 = min(W, int(math.ceil((bb.xMaximum() - gt[0]) / gt[1])))
    r0 = max(0, int(math.floor((gt[3] - bb.yMaximum()) / -gt[5])))
    r1 = min(H, int(math.ceil((gt[3] - bb.yMinimum()) / -gt[5])))
    if c0 >= c1 or r0 >= r1:
        return None
    vds, lyr = ogr_geom_layer(ds, geom)
    w = c1 - c0
    sgn = -1.0 if lowest else 1.0  # lowest point = highest of the negated values
    for all_touched in ((True,) if touched_only else (False, True)):
        best = None  # [value, row, col, ties]
        s_min, s_sum, s_n = None, 0.0, 0
        for rs in range(r0, r1, chunk_rows):
            h = min(r1, rs + chunk_rows) - rs
            mds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
            mds.SetGeoTransform((gt[0] + c0 * gt[1], gt[1], 0, gt[3] + rs * gt[5], 0, gt[5]))
            gdal.RasterizeLayer(mds, [1], lyr, burn_values=[1],
                                options=["ALL_TOUCHED=TRUE"] if all_touched else [])
            mask = mds.ReadAsArray().astype(bool)
            if not mask.any():
                continue
            data = b.ReadAsArray(c0, rs, w, h).astype("float64")
            valid = mask & np.isfinite(data)
            if nd is not None:
                valid &= data != nd
            if not valid.any():
                continue
            if stats:
                dv = data[valid]
                s_min = float(dv.min()) if s_min is None else min(s_min, float(dv.min()))
                s_sum += float(dv.sum())
                s_n += int(dv.size)
            vals = np.where(valid, sgn * data, -np.inf)
            i = int(np.argmax(vals))
            v = float(vals.flat[i])  # score (value * sgn)
            ties = int((vals == v).sum())
            if best is None or v > best[0]:
                best = [v, rs + i // w, c0 + i % w, ties]
            elif v == best[0]:
                best[3] += ties
        if best is not None:
            v, row, col, ties = best
            v = sgn * v
            out = (v, gt[0] + (col + 0.5) * gt[1], gt[3] + (row + 0.5) * gt[5], ties)
            if stats:
                out += (s_min, s_sum / s_n if s_n else None, s_n)
            return out
    return None


def ogr_geom_layer(ds, geom):
    """One-feature in-memory OGR layer holding `geom` (already in the raster's CRS), with the
    raster's SRS so GDAL does not warn 'Failed to fetch spatial reference' for every block.
    Returns (datasource, layer) - keep the datasource alive while the layer is used."""
    from osgeo import ogr, osr
    vds = ogr_memory_datasource()
    srs = None
    wkt = ds.GetProjection()
    if wkt:
        srs = osr.SpatialReference()
        if srs.ImportFromWkt(wkt) != 0:
            srs = None
        else:
            try:
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except AttributeError:  # GDAL < 3
                pass
    lyr = vds.CreateLayer("g", srs=srs, geom_type=ogr.wkbUnknown)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geom.asWkb())))
    lyr.CreateFeature(feat)
    return vds, lyr


def ogr_memory_datasource():
    """In-memory OGR datasource. GDAL >= 3.11 merged the vector 'Memory' driver into 'MEM'
    ('Memory' is a deprecated alias); try the new name first so any GDAL version works."""
    from osgeo import ogr
    for name in ("MEM", "Memory"):
        drv = ogr.GetDriverByName(name)
        if drv is None:
            continue
        try:
            ds = drv.CreateDataSource("m")
        except Exception:
            ds = None
        if ds is not None:
            return ds
    raise RuntimeError("GDAL has no in-memory vector driver (Memory / MEM).")


# --------------------------------------------------------------------------- #
#  Terrain-based high / low point (ignores trees, dikes, huts ...)
# --------------------------------------------------------------------------- #
METHODS = [("Top of general slope - ground filter + smoothing (recommended)", "slope"),
           ("Highest ground - ground filter only (removes trees / dikes)", "ground"),
           ("Highest DEM cell (raw, includes trees / dikes)", "raw")]
FLOW_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _minmax_filter(a, size, op):
    """Square moving min / max (scipy if available, otherwise pure numpy)."""
    import numpy as np
    if size < 2:
        return a
    try:
        from scipy import ndimage
        f = ndimage.minimum_filter if op == "min" else ndimage.maximum_filter
        return f(a, size=size, mode="nearest")
    except ImportError:
        from numpy.lib.stride_tricks import sliding_window_view
        f = np.min if op == "min" else np.max
        r = size // 2
        p_ = np.pad(a, ((r, size - 1 - r), (0, 0)), mode="edge")
        a = f(sliding_window_view(p_, size, axis=0), axis=-1)
        p_ = np.pad(a, ((0, 0), (r, size - 1 - r)), mode="edge")
        return f(sliding_window_view(p_, size, axis=1), axis=-1)


def _box_sum(x, size):
    """Moving-window sum (square window, integral image)."""
    import numpy as np
    r = size // 2
    p_ = np.pad(x, ((r + 1, size - r - 1), (r + 1, size - r - 1)), mode="constant")
    c = p_.cumsum(0).cumsum(1)
    return c[size:, size:] - c[:-size, size:] - c[size:, :-size] + c[:-size, :-size]


def ground_surface(data, use, k_ground, k_smooth):
    """1) Morphological opening (min then max filter, size k_ground cells) inside the block:
          removes raised objects NARROWER than the window (tree crowns, dikes, huts) and keeps
          real high ground that is wider than it.
       2) Optional block-limited moving average (k_smooth cells): the general slope.
    Cells outside the block never influence the result. Returns (ground, smooth)."""
    import numpy as np
    g = data.copy()
    if k_ground >= 2:
        e = _minmax_filter(np.where(use, data, np.inf), k_ground, "min")
        g = _minmax_filter(np.where(use, e, -np.inf), k_ground, "max")
    g = np.where(use, g, np.nan)
    sm = g
    if k_smooth >= 2:
        w = use.astype("float64")
        num_ = _box_sum(np.where(use, g, 0.0), k_smooth)
        den = _box_sum(w, k_smooth)
        sm = np.where(use & (den > 0), num_ / np.maximum(den, 1e-12), np.nan)
    return g, sm


def plane_fit(xs, ys, zs):
    """Robust least-squares plane z = a*x + b*y + c (outliers > 3 MAD dropped twice).
    Returns (a, b, c, r2) or None."""
    import numpy as np
    if zs.size < 6:
        return None
    if zs.size > 60000:  # subsample big blocks
        sel = np.random.default_rng(0).choice(zs.size, 60000, replace=False)
        xs, ys, zs = xs[sel], ys[sel], zs[sel]
    x0, y0 = xs.mean(), ys.mean()
    X, Y, Z = xs - x0, ys - y0, zs
    keep = np.ones(Z.size, bool)
    coef = None
    for _ in range(3):
        A = np.c_[X[keep], Y[keep], np.ones(keep.sum())]
        coef, *_ = np.linalg.lstsq(A, Z[keep], rcond=None)
        res = Z - (coef[0] * X + coef[1] * Y + coef[2])
        mad = np.median(np.abs(res[keep] - np.median(res[keep]))) * 1.4826
        if mad <= 1e-9:
            break
        new = np.abs(res) <= 3 * mad
        if new.sum() < 6 or (new == keep).all():
            break
        keep = new
    a, b, c = coef
    zk = Z[keep]
    pred = a * X[keep] + b * Y[keep] + c
    ss_tot = float(((zk - zk.mean()) ** 2).sum())
    r2 = 1 - float(((zk - pred) ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    return a, b, c - a * x0 - b * y0, r2


def locate_terrain(ds, band, geom, p, lowest=False):
    """High (or low) point of the GROUND / general slope of one polygon block.
    Returns (value, x, y, n_cells_near_top, extra) or None.
    extra = ground_z, raw_cell, slope_pct, fall_m, aspect, flow_dir, fit_r2."""
    import numpy as np
    from osgeo import gdal
    gt = ds.GetGeoTransform()
    if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
        return None
    cs = abs(gt[1])

    def odd(k):  # odd windows stay centred on the cell
        return k if k % 2 else k + 1
    k_g = odd(max(1, int(round(p["ground_win"] / cs)))) if p["method"] in ("slope", "ground") else 1
    k_s = odd(max(1, int(round(p["smooth_win"] / cs)))) if p["method"] == "slope" else 1
    pad = k_g + k_s + 1
    b = ds.GetRasterBand(band)
    nd = b.GetNoDataValue()
    W, H = ds.RasterXSize, ds.RasterYSize
    bb = geom.boundingBox()
    c0 = max(0, int(math.floor((bb.xMinimum() - gt[0]) / gt[1])) - pad)
    c1 = min(W, int(math.ceil((bb.xMaximum() - gt[0]) / gt[1])) + pad)
    r0 = max(0, int(math.floor((gt[3] - bb.yMaximum()) / -gt[5])) - pad)
    r1 = min(H, int(math.ceil((gt[3] - bb.yMinimum()) / -gt[5])) + pad)
    if c0 >= c1 or r0 >= r1:
        return None
    w, h = c1 - c0, r1 - r0
    if w * h > 40_000_000:
        raise RuntimeError(f"Block too large for the terrain method at this resolution "
                           f"({w * h:,} cells). Choose a coarser analysis resolution (e.g. 1 m, Average).")
    vds, lyr = ogr_geom_layer(ds, geom)
    data = b.ReadAsArray(c0, r0, w, h).astype("float64")
    valid = np.isfinite(data)
    if nd is not None:
        valid &= data != nd
    use = None
    for opts in ([], ["ALL_TOUCHED=TRUE"]):  # cell-centre rule, then all-touched for tiny blocks
        mds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
        mds.SetGeoTransform((gt[0] + c0 * gt[1], gt[1], 0, gt[3] + r0 * gt[5], 0, gt[5]))
        gdal.RasterizeLayer(mds, [1], lyr, burn_values=[1], options=opts)
        use = mds.ReadAsArray().astype(bool) & valid
        if use.any():
            break
    if not use.any():
        return None
    ground, smooth = ground_surface(data, use, k_g, k_s)
    score = smooth if p["method"] == "slope" else ground
    sgn = -1.0 if lowest else 1.0
    s = np.where(use, sgn * score, -np.inf)
    i = int(np.argmax(s))
    best = float(s.flat[i])
    row, col = i // w, i % w
    near = int((s >= best - 0.01).sum())  # cells within 1 cm of the top (flat top size)
    rows, cols = np.nonzero(use)
    xs = gt[0] + (c0 + cols + 0.5) * gt[1]
    ys = gt[3] + (r0 + rows + 0.5) * gt[5]
    gz = float(ground[row, col])
    # The smoothed surface finds the top of the general slope, but averaging pulls values at
    # the block edge down (or up for the lowest point). Report the real ground (trees / dikes
    # removed) at that spot, so the water level needed is not under-estimated.
    value = gz if p["method"] == "slope" else sgn * best
    extra = {"ground_z": gz, "slope_z": float(score[row, col]),
             "raw_cell": float((data[use].min() if lowest else data[use].max())),
             "slope_pct": None, "fall_m": None, "aspect": None, "flow_dir": None, "fit_r2": None}
    fit = plane_fit(xs, ys, ground[rows, cols])
    if fit is not None:
        a, bb_, c, r2 = fit
        pl = a * xs + bb_ * ys + c
        extra["slope_pct"] = 100.0 * math.hypot(a, bb_)
        extra["fall_m"] = float(pl.max() - pl.min())
        asp = (math.degrees(math.atan2(-a, -bb_)) + 360.0) % 360.0  # downslope bearing
        extra["aspect"] = asp
        extra["flow_dir"] = FLOW_DIRS[int((asp + 22.5) // 45) % 8]
        extra["fit_r2"] = r2
    return (value, gt[0] + (c0 + col + 0.5) * gt[1], gt[3] + (r0 + row + 0.5) * gt[5],
            near, extra)


EXTRA_FIELDS = [("ground_z", "Ground_z"), ("slope_z", "Smooth_z"), ("raw_cell", "Raw_cell"), ("slope_pct", "Slope_pct"),
                ("fall_m", "Fall_m"), ("aspect", "Aspect"), ("flow_dir", "Flow_dir"),
                ("fit_r2", "Fit_R2")]


def point_records(blocks, loc, work, carry, grouped, dem_crs, nd, along, extra=None):
    """Highest-point records for one block layer (one per feature, or one per group).
    carry = [(source_field_name, output_field_name)] copied from the block holding the peak."""
    to_wgs = QgsCoordinateTransform(dem_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    QgsProject.instance())
    xy_dec = 7 if dem_crs.isGeographic() else 3
    present = [(src_n, out_n) for src_n, out_n in carry if work.fields().indexOf(src_n) != -1]
    attrs = {}
    for f in work.getFeatures():
        attrs[int(f[KEY])] = {out_n: f[src_n] for src_n, out_n in present}
    recs, seen = [], set()
    for zid, (v, x, y, ties, src) in sorted(loc.items()):
        if grouped:
            if src in seen:  # one point per group
                continue
            seen.add(src)
        ll = to_wgs.transform(QgsPointXY(x, y))
        d = along.get(src)
        recs.append({"src_layer": blocks.name(), "block_fid": src, "attrs": attrs.get(src, {}),
                     "elev": round(v, nd), "X": round(x, xy_dec), "Y": round(y, xy_dec), "gx": x, "gy": y,
                     "Lon": round(ll.x(), 7), "Lat": round(ll.y(), 7), "Top_cells": ties,
                     "dist_along": None if d is None else round(d, 3),
                     "extra": (extra or {}).get(src)})
    return recs


def add_point_layer(records, dem_crs, name, log, carry_defs=(), nd=3, lowest=False):
    """One point layer for all processed block layers ('src_layer' tells them apart).
    carry_defs = [QgsField] for the copied block attributes (already renamed if needed)."""
    lyr = QgsVectorLayer("Point", name, "memory")
    lyr.setCrs(dem_crs)
    pr = lyr.dataProvider()
    has_lines = any(r["dist_along"] is not None for r in records)
    fields = ([make_field("src_layer", T_STRING), make_field("block_fid", T_INT)] +
              [QgsField(f) for f in carry_defs] +
              [make_field("elev", T_DOUBLE, 12, nd), make_field("X", T_DOUBLE, 16, 3),
               make_field("Y", T_DOUBLE, 16, 3), make_field("Lon", T_DOUBLE, 14, 7),
               make_field("Lat", T_DOUBLE, 14, 7), make_field("Top_cells", T_INT)])
    if has_lines:
        fields.append(make_field("dist_along", T_DOUBLE, 14, 2))
    has_extra = any(r.get("extra") for r in records)
    if has_extra:
        for key, fname_ in EXTRA_FIELDS:
            fields.append(make_field(fname_, T_STRING) if key == "flow_dir"
                          else make_field(fname_, T_DOUBLE, 12, 3))
    pr.addAttributes(fields)
    lyr.updateFields()
    feats = []
    for r in records:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["gx"], r["gy"])))  # exact position
        vals = [r["src_layer"], r["block_fid"]]
        for cf in carry_defs:
            v = r["attrs"].get(cf.name(), NULL)
            if v is not None and v != NULL and cf.type() == T_STRING and not isinstance(v, str):
                v = str(v)
            vals.append(NULL if v is None else v)
        vals += [r["elev"], r["X"], r["Y"], r["Lon"], r["Lat"], r["Top_cells"]]
        if has_lines:
            vals.append(NULL if r["dist_along"] is None else r["dist_along"])
        if has_extra:
            ex = r.get("extra") or {}
            for key, _ in EXTRA_FIELDS:
                v = ex.get(key)
                vals.append(NULL if v is None else (v if key == "flow_dir" else round(v, 3)))
        f.setAttributes(vals)
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    with contextlib.suppress(Exception):            # styling is cosmetic
        from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer
        sym = QgsMarkerSymbol.createSimple({"name": "triangle", "color": "0,90,255" if lowest else "255,0,0",
                                            "outline_color": "0,0,0", "size": "3.2",
                                            "angle": "180" if lowest else "0"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    QgsProject.instance().addMapLayer(lyr)
    log(f"Point layer added: {lyr.name()} ({len(feats)} points, memory layer - save via Export)")
    return lyr


def safe_name(name, maxlen=60):
    """File/sheet/table-safe name."""
    out = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(name)).strip("_")
    return (out or "layer")[:maxlen]


def unique_path(path):
    """Never overwrite: add _1, _2 ... if the file (or folder) already exists."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


def shp_field_names(fields, long_name):
    """10-character, unique names for shapefiles; 'Higher_elevation…' becomes 'Hi_Elev…'."""
    out, used = [], set()
    for f in fields:
        n = f.name()
        if len(n) > 10 and long_name and n.startswith(long_name):
            n = "Hi_Elev" + n[len(long_name):]
        n = n[:10]
        k = 1
        while n.lower() in used:
            n = f"{n[:8]}_{k}"
            k += 1
        used.add(n.lower())
        out.append(n)
    return out


def write_layer(layer, path, driver, table, first, no_geometry=False, long_name=None):
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = driver
    o.fileEncoding = "UTF-8"
    if driver == "ESRI Shapefile":
        o.attributes = list(range(layer.fields().count()))
        o.attributesExportNames = shp_field_names(layer.fields(), long_name)
    if table:
        o.layerName = table
    if no_geometry:
        o.overrideGeometryType = WKB_NOGEOM
    if driver != "ESRI Shapefile":
        o.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if first
                                  else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
    res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsProject.instance().transformContext(), o)
    if res[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"Could not write {table or path}: {res[1]}")


def summary_table(rows, res_label_txt):
    t = QgsVectorLayer("None", "Summary", "memory")
    pr = t.dataProvider()
    pr.addAttributes([make_field("layer", T_STRING), make_field("blocks_with_value", T_INT),
                      make_field("blocks_no_data", T_INT), make_field("resolution", T_STRING),
                      make_field("status", T_STRING), make_field("run_time", T_STRING)])
    t.updateFields()
    now = time.strftime("%Y-%m-%d %H:%M")
    feats = []
    for name, ok, nmiss, err in rows:
        f = QgsFeature(t.fields())
        f.setAttributes([name, ok, nmiss, res_label_txt, ("ERROR: " + err) if err else "ok", now])
        feats.append(f)
    pr.addFeatures(feats)
    return t


def table_name(name, lyr):
    """Table / sheet / file name for an output layer."""
    return safe_name(lyr.name()) if name == "__raw__" else safe_name(f"{name}_MaxElev")


def export_results(results, points, summary, folder, base, formats, res_txt, log, long_name=None):
    """results = [(source_layer_name, result_layer)]; returns {format: path}."""
    os.makedirs(folder, exist_ok=True)
    base = safe_name(base or "Block_MaxElev")
    out = {}
    tables = [(table_name(name, lyr), lyr) for name, lyr in results]
    for pl in (points if isinstance(points, (list, tuple)) else [points]):
        if pl is not None:
            tables.append((safe_name(pl.name()), pl))

    if "gpkg" in formats:
        path = unique_path(os.path.join(folder, base + ".gpkg"))
        for i, (tname, lyr) in enumerate(tables):
            write_layer(lyr, path, "GPKG", tname, i == 0)
        write_layer(summary, path, "GPKG", "Summary", False)
        out["gpkg"] = path
        log(f"Saved GeoPackage: {path}  ({len(tables)} layers + Summary table)")

    if "shp" in formats:
        sub = unique_path(os.path.join(folder, base + "_shp"))
        os.makedirs(sub)
        for tname, lyr in tables:
            write_layer(lyr, os.path.join(sub, tname + ".shp"), "ESRI Shapefile", None, True,
                        long_name=long_name)
        write_layer(summary, os.path.join(sub, "Summary.shp"), "ESRI Shapefile", None, True)
        out["shp"] = sub
        log(f"Saved Shapefiles in: {sub}  ({len(tables)} .shp + Summary.dbf; "
            "field names shortened to 10 characters, e.g. Hi_Elev)")

    if "xlsx" in formats:
        path = unique_path(os.path.join(folder, base + ".xlsx"))
        write_layer(summary, path, "XLSX", "Summary", True)
        used = {"Summary"}
        for tname, lyr in tables:
            sheet = tname[:31]
            k = 1
            while sheet in used:  # Excel sheet names: max 31 chars, unique
                sheet = f"{tname[:28]}_{k}"
                k += 1
            used.add(sheet)
            write_layer(lyr, path, "XLSX", sheet, False, no_geometry=True)
        out["xlsx"] = path
        log(f"Saved Excel: {path}  (sheets: Summary + {len(tables)} result sheets)")
    return out


def polygons_only(g):
    """Keep only the polygon parts of a geometry (intersections can add lines/points)."""
    if g is None or g.isEmpty():
        return QgsGeometry()
    if g.type() == POLYGON_GEOM:
        return g
    parts = [p_ for p_ in g.asGeometryCollection() if p_.type() == POLYGON_GEOM and p_.area() > 0]
    return QgsGeometry.collectGeometry(parts) if parts else QgsGeometry()


def clean_polygons(layer, p, run, log, label_of, src_name):
    """Snap / remove overlaps / fill small gaps on a working copy (DEM CRS, metres).
    Returns (clean_layer, issues) where issues = [(kind, geometry, area, blocks)]."""
    issues = []
    lyr = layer
    tol = p.get("snap_tol", 0) or 0
    if tol > 0:
        before = {int(f[KEY]): f.geometry() for f in lyr.getFeatures()}
        lyr = as_layer(run("native:snapgeometries", {"INPUT": lyr, "REFERENCE_LAYER": lyr,
                                                        "TOLERANCE": tol, "BEHAVIOR": 0, "OUTPUT": TEMP}))
        if any(not f.geometry().isGeosValid() for f in lyr.getFeatures() if not f.geometry().isEmpty()):
            lyr = as_layer(run("native:fixgeometries", {"INPUT": lyr, "OUTPUT": TEMP}))
        n_snap = 0

        def lbl(k):
            return str(label_of.get(k)) if label_of.get(k, NULL) not in (None, NULL) else f"fid {k}"
        for f in lyr.getFeatures():
            k = int(f[KEY])
            old = before.get(k)
            if old is None or old.isEmpty() or f.geometry().isEmpty():
                continue
            diff = polygons_only(old.symDifference(f.geometry()))
            if not diff.isEmpty() and diff.area() > 0.001:
                issues.append(("boundary snapped", diff, diff.area(), lbl(k)))
                n_snap += 1
        log(f"  snapped boundaries within {tol:g} m ({n_snap} block(s) adjusted)")
    feats = {int(f[KEY]): QgsFeature(f) for f in lyr.getFeatures()}
    geom = {k: polygons_only(f.geometry()) for k, f in feats.items()}

    def name(k):
        return str(label_of.get(k, k)) if label_of.get(k, NULL) not in (None, NULL) else f"fid {k}"

    # overlaps: small ones (digitising slivers) are removed from the smaller block;
    # large ones (e.g. one block drawn inside another) are only reported
    ratio = (p.get("overlap_pct", 10) or 0) / 100.0
    idx, kept, n_ov, n_big = QgsSpatialIndex(), {}, 0, 0
    for k in sorted(geom, key=lambda k: -geom[k].area()):  # larger blocks first
        g = geom[k]
        if g.isEmpty():
            continue
        own_area = g.area()
        for c in idx.intersects(g.boundingBox()):
            inter = polygons_only(g.intersection(kept[c]))
            if inter.isEmpty() or inter.area() <= 0.01:
                continue
            if p.get("fix_overlaps") and inter.area() <= ratio * own_area:
                issues.append(("overlap removed", inter, inter.area(),
                               f"{name(k)} overlaps {name(c)} (kept in {name(c)})"))
                g = polygons_only(g.difference(kept[c]).makeValid())
                n_ov += 1
                if g.isEmpty():
                    break
            else:
                pct = 100.0 * inter.area() / own_area if own_area else 100.0
                issues.append(("large overlap (not changed)", inter, inter.area(),
                               f"{name(k)} and {name(c)} share {pct:.0f}% of {name(k)}"))
                n_big += 1
        geom[k] = g
        if not g.isEmpty():
            kept[k] = g
            idx.addFeature(k, g.boundingBox())
    if n_ov:
        log(f"  removed {n_ov} small overlap(s) (the shared area stays with the larger block)")
    if n_big:
        log(f"  NOTE: {n_big} large overlap(s) left unchanged - see Topology_issues")

    # gaps: holes inside the union of all blocks
    gap_max = p.get("gap_max", 0) or 0
    if gap_max > 0 and geom:
        union = QgsGeometry.unaryUnion([g for g in geom.values() if not g.isEmpty()])
        polys = union.asMultiPolygon() if union.isMultipart() else [union.asPolygon()]
        idx = QgsSpatialIndex()
        for k, g in geom.items():
            if not g.isEmpty():
                idx.addFeature(k, g.boundingBox())
        # rings the user drew INSIDE a block (trees, ponds, houses ...) are excluded areas,
        # not gaps between blocks -> never fill them back
        own_rings = []
        if p.get("keep_rings", True):
            for g in geom.values():
                if g.isEmpty():
                    continue
                for pg in (g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]):
                    for r_ in pg[1:]:
                        h_ = QgsGeometry.fromPolygonXY([r_])
                        if h_.area() > 0:
                            own_rings.append(h_)
        ring_union = QgsGeometry.unaryUnion(own_rings) if own_rings else QgsGeometry()
        n_fill = n_big = n_ring = 0
        for poly in polys:
            for ring in poly[1:]:
                hole = QgsGeometry.fromPolygonXY([ring])
                a = hole.area()
                if a <= 0:
                    continue
                if not ring_union.isEmpty() and hole.intersects(ring_union):
                    shared = hole.intersection(ring_union).area()
                    if shared >= 0.5 * a:  # this hole is a block's own ring -> keep it
                        n_ring += 1
                        continue
                edge = QgsGeometry.fromPolylineXY(ring)
                best, best_len = None, 0.0
                for c in idx.intersects(hole.boundingBox().buffered(0.01)):
                    ln = edge.intersection(geom[c].buffer(0.01, 2)).length()
                    if ln > best_len:
                        best, best_len = c, ln
                if a <= gap_max and best is not None:
                    geom[best] = polygons_only(geom[best].combine(hole).makeValid())
                    issues.append(("gap filled", hole, a, f"added to {name(best)}"))
                    n_fill += 1
                else:
                    issues.append(("gap (not filled, larger than limit)", hole, a,
                                   f"next to {name(best)}" if best is not None else ""))
                    n_big += 1
        if n_fill or n_big:
            log(f"  gaps: {n_fill} filled (<= {gap_max:g} m²), {n_big} larger gap(s) left as they are")
        if n_ring:
            log(f"  {n_ring} ring(s) inside blocks kept as excluded areas (not filled)")

    out = QgsVectorLayer("Polygon", "clean", "memory")
    out.setCrs(lyr.crs())
    out.dataProvider().addAttributes(lyr.fields().toList())
    out.updateFields()
    new, lost = [], []
    for k, f in feats.items():
        if geom[k].isEmpty():
            lost.append(k)
            continue
        nf = QgsFeature(out.fields())
        nf.setAttributes(f.attributes())
        nf.setGeometry(geom[k])
        new.append(nf)
    out.dataProvider().addFeatures(new)
    if lost:
        log(f"  WARNING: {len(lost)} block(s) lie completely inside a larger block and get no "
            f"point: {', '.join(name(k) for k in lost[:10])}")
        for k in lost:
            issues.append(("block completely covered by another", feats[k].geometry(),
                           feats[k].geometry().area(), name(k)))
    return out, issues


def add_issue_layer(issues, crs, log):
    lyr = QgsVectorLayer("MultiPolygon", "Topology_issues", "memory")
    lyr.setCrs(crs)
    lyr.dataProvider().addAttributes([make_field("src_layer", T_STRING), make_field("issue", T_STRING),
                                      make_field("area_m2", T_DOUBLE, 14, 2),
                                      make_field("blocks", T_STRING)])
    lyr.updateFields()
    # Same stamp the topology checks leave, so the Error Inspector lists it.
    lyr.setCustomProperty("kga_tools/topology_error_layer", "block_issue")
    feats = []
    for src, kind, g, a, blocks in issues:
        f = QgsFeature(lyr.fields())
        g2 = QgsGeometry(g)
        g2.convertToMultiType()
        f.setGeometry(g2)
        f.setAttributes([src, kind, round(a, 2), blocks])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    with contextlib.suppress(Exception):            # styling is cosmetic
        from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer
        sym = QgsFillSymbol.createSimple({"color": "255,0,255,90", "outline_color": "255,0,255",
                                          "outline_width": "0.6"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    QgsProject.instance().addMapLayer(lyr)
    log(f"Issue layer added: Topology_issues ({len(feats)} item(s) to review)")
    return lyr


def res_label(res):
    """0.5 -> '0p5m', 30 -> '30m' (safe inside field names)."""
    return f"{res:g}".replace(".", "p") + "m"


# --------------------------------------------------------------------------- #
#  Core analysis
# --------------------------------------------------------------------------- #
def run_analysis(p, log):
    import processing                                # loaded by QGIS, not by us
    t0 = time.time()
    dem, blocks, band = p["dem"], p["blocks"], p["band"]
    if dem is None or not dem.isValid():
        raise ValueError("Choose a valid DEM raster layer.")
    if blocks is None or not blocks.isValid():
        raise ValueError("Choose a valid block layer (polygon or line).")
    gtype = blocks.geometryType()
    if gtype not in (POLYGON_GEOM, LINE_GEOM):
        raise ValueError(f"Layer '{blocks.name()}' is not a polygon or line layer.")
    is_line = gtype == LINE_GEOM
    write_blocks = p.get("write_blocks", True)
    fb = LogFeedback(log)

    def run(alg, params):
        return processing.run(alg, params, feedback=fb)["OUTPUT"]

    dem_crs, blk_crs = dem.crs(), blocks.crs()
    cell = dem.rasterUnitsPerPixelX()
    unit = "deg" if dem_crs.isGeographic() else "map units"
    log(f"DEM    : {dem.name()}  | band {band} | cell {cell:g} {unit} | {dem_crs.authid()}")
    log(f"Blocks : {blocks.name()}  | {'line' if is_line else 'polygon'} | "
        f"{blocks.featureCount()} features | {blk_crs.authid()}")
    if dem_crs.isGeographic():
        log("WARNING: DEM is in degrees. Consider reprojecting it to UTM (e.g. EPSG:32648).")

    # 1) working copy carrying the original feature id
    use_sel = p["selected_only"] and blocks.selectedFeatureCount() > 0
    src = QgsProcessingFeatureSourceDefinition(blocks.id(), True) if use_sel else blocks
    if use_sel:
        log(f"Using {blocks.selectedFeatureCount()} selected feature(s) only")
    work = as_layer(run("native:fieldcalculator", {
        "INPUT": src, "FIELD_NAME": KEY, "FIELD_TYPE": 1, "FIELD_LENGTH": 0,
        "FIELD_PRECISION": 0, "FORMULA": "$id", "OUTPUT": TEMP}))
    processed = {int(f[KEY]) for f in work.getFeatures()}

    # 1b) repair invalid geometries on the working copy (the block layer is not changed)
    issues = []
    label_field = p.get("label_field")
    label_of = ({int(f[KEY]): f[label_field] for f in work.getFeatures()}
                if label_field and work.fields().indexOf(label_field) != -1 else {})
    bad = []
    for f in work.getFeatures():
        g = f.geometry()
        if g is not None and not g.isEmpty() and not g.isGeosValid():
            bad.append(int(f[KEY]))
    if bad:
        log(f"Repairing {len(bad)} invalid geometr{'y' if len(bad) == 1 else 'ies'} "
            f"(block fid: {', '.join(map(str, bad[:15]))}{' ...' if len(bad) > 15 else ''}); "
            "your layer itself is not changed.")
        bad_geoms = {int(f[KEY]): f.geometry() for f in work.getFeatures() if int(f[KEY]) in set(bad)}
        work = as_layer(run("native:fixgeometries", {"INPUT": work, "OUTPUT": TEMP}))
        for f in work.getFeatures():
            k = int(f[KEY])
            if k in bad_geoms and f.geometry() is not None and not f.geometry().isEmpty():
                lbl = label_of.get(k)
                issues.append(("invalid geometry repaired", f.geometry(), f.geometry().area(),
                               str(lbl) if lbl not in (None, NULL) else f"fid {k}"))
        still = [int(f[KEY]) for f in work.getFeatures()
                 if f.geometry() is None or f.geometry().isEmpty() or not f.geometry().isGeosValid()]
        lost = sorted(set(processed) - {int(f[KEY]) for f in work.getFeatures()})
        if lost:
            log(f"  WARNING: block fid {', '.join(map(str, lost[:15]))} could not be repaired "
                "(no area left) and gets no point.")
        if still:
            log(f"  WARNING: block fid {', '.join(map(str, still[:15]))} is still invalid; skipped.")
            work.dataProvider().deleteFeatures([f.id() for f in work.getFeatures()
                                                if int(f[KEY]) in set(still)])

    # 2) blocks -> DEM CRS (never warp the DEM)
    zones = work
    if blk_crs != dem_crs:
        log("Reprojecting blocks to the DEM CRS ...")
        zones = as_layer(run("native:reprojectlayer",
                             {"INPUT": work, "TARGET_CRS": dem_crs, "OUTPUT": TEMP}))

    # 2b) clean polygon blocks: snap, overlaps, gaps (working copy only)
    if not is_line:
        if dem_crs.isGeographic():
            log("Block cleaning skipped: the DEM is in degrees (needs metres).")
        else:
            log("Cleaning blocks (working copy - your layer is not changed) ...")
            zones, more = clean_polygons(zones, p, run, log, label_of, blocks.name())
            issues += more

    # 3) optional inner buffer (polygons only)
    if p["inner_buffer"] > 0 and is_line:
        log("Inner buffer ignored for line layers.")
    if p["inner_buffer"] > 0 and not is_line:
        dist = p["inner_buffer"] / 111320.0 if dem_crs.isGeographic() else p["inner_buffer"]
        log(f"Inner buffer: -{p['inner_buffer']:g} m (skips boundary dikes/roads)")
        zones = as_layer(run("native:buffer", {
            "INPUT": zones, "DISTANCE": -dist, "SEGMENTS": 5, "END_CAP_STYLE": 0,
            "JOIN_STYLE": 1, "MITER_LIMIT": 2, "DISSOLVE": False, "OUTPUT": TEMP}))

    # 4) analysis resolution
    min_area = None
    for f in zones.getFeatures():
        g = f.geometry()
        if g is not None and not g.isEmpty() and g.area() > 0:
            a = g.area()
            min_area = a if min_area is None or a < min_area else min_area
    if min_area:
        log(f"Smallest zone ~ {min_area:,.1f} sq units (~{math.sqrt(min_area):,.1f} across)")

    prov_r = dem.dataProvider()
    nodata = prov_r.sourceNoDataValue(band) if prov_r.sourceHasNoDataValue(band) else -9999.0
    ze = zones.extent().intersect(dem.extent())  # only where blocks overlap the DEM
    raster_in, rcell = dem, cell
    tmp_files = []

    def warp(src_layer, res, method, window, tag):
        path = os.path.join(tempfile.gettempdir(), f"blkmax_{tag}_{int(time.time() * 1000)}.tif")
        processing.run("gdal:warpreproject", {
            "INPUT": src_layer.source(), "SOURCE_CRS": dem_crs, "TARGET_CRS": dem_crs,
            "RESAMPLING": method, "NODATA": nodata, "TARGET_RESOLUTION": res,
            "TARGET_EXTENT": "{},{},{},{}".format(*window), "TARGET_EXTENT_CRS": dem_crs,
            "OPTIONS": "COMPRESS=DEFLATE|TILED=YES", "DATA_TYPE": 0,
            "MULTITHREADING": True, "OUTPUT": path}, feedback=fb)
        tmp_files.append(path)
        lyr = QgsRasterLayer(path, tag)
        if not lyr.isValid():
            raise RuntimeError(f"Resampled DEM ({tag}) could not be opened.")
        return lyr

    def build_raster(coarse_method, tag, why=""):
        """Analysis grid: resample to the chosen resolution, then small-block safety.
        Returns (raster, band, cell size, coarsened?)."""
        raster_in, band_r, rcell, coarsened = dem, band, cell, False
        # 4a) resample the DEM to the resolution chosen by the user
        target_res = p["target_res"]
        if target_res and not ze.isEmpty():
            target_u = target_res / 111320.0 if dem_crs.isGeographic() else target_res
            if abs(target_u - cell) / cell > 0.01:
                coarser = target_u > cell
                method = coarse_method if coarser else 0  # refining is always nearest neighbour
                window = snapped_window(dem.extent(), ze, target_u, target_u, clamp=not coarser)
                ncells = ((window[1] - window[0]) / target_u) * ((window[3] - window[2]) / target_u)
                if ncells > MAX_CELLS:
                    raise ValueError(
                        f"A {target_res:g} m grid over the blocks would be {ncells:,.0f} cells "
                        f"(limit {MAX_CELLS:,.0f}). Choose a coarser resolution or select fewer blocks.")
                log(f"Resampling DEM {cell:g} -> {target_res:g} m using "
                    f"{RESAMPLE_NAMES.get(method, method)}{why}"
                    + ("" if coarser else " (finer grid: values copied, nothing interpolated)"))
                kx, ky = target_u / cell, target_u / dem.rasterUnitsPerPixelY()
                integer_ratio = (coarser and abs(kx - round(kx)) < 1e-6 and abs(ky - round(ky)) < 1e-6
                                 and dem.providerType() == "gdal")
                done = False
                if integer_ratio:
                    path = os.path.join(tempfile.gettempdir(), f"blkmax_{tag}_{int(time.time() * 1000)}.tif")
                    # window aligned to the DEM grid in whole target cells
                    done = coarsen_exact(dem.source().split("|")[0], band, window, int(round(kx)),
                                         int(round(ky)), method, nodata, path, log)
                    if done:
                        tmp_files.append(path)
                        raster_in = QgsRasterLayer(path, tag)
                        band_r = 1
                        if not raster_in.isValid():
                            raise RuntimeError("Resampled DEM could not be opened.")
                        log(f"  exact {int(round(kx))} x {int(round(ky))} cell aggregation")
                if not done:
                    if coarser:  # GDAL max/average is unreliable at the edges of small grids
                        window = snapped_window(dem.extent(), ze, target_u, target_u, clamp=False, pad=4)
                    raster_in = warp(dem, target_u, method, window, tag)
                    band_r = band if raster_in.bandCount() >= band else 1
                rcell = target_u
                coarsened = coarser
            elif not why:
                log(f"Chosen resolution {target_res:g} m equals the DEM resolution; no resampling needed")
        elif target_res and ze.isEmpty() and not why:
            log("WARNING: no block overlaps the DEM.")

        # 4b) small-block safety: subdivide cells when blocks are only a few cells wide
        factor = 1
        if p["subdivide"] and p.get("method", "raw") == "raw" and min_area and not ze.isEmpty():
            side = math.sqrt(min_area)
            if side / rcell < 10:  # fewer than ~100 cells in the smallest block
                factor = min(20, math.ceil(10 * rcell / side))
        if factor > 1:
            window = snapped_window(raster_in.extent(), ze, rcell, rcell, clamp=True)
            base_cells = round((window[1] - window[0]) / rcell) * round((window[3] - window[2]) / rcell)
            max_factor = max(1, int(math.sqrt(MAX_CELLS / max(base_cells, 1))))
            if factor > max_factor:
                log(f"  Subdivision reduced from x{factor} to x{max_factor} to stay within memory "
                    f"({MAX_CELLS:,.0f} cells). Very small blocks may still be under-sampled; "
                    "check Elev_Cells.")
                factor = max_factor
        if factor > 1:
            if not why:
                log(f"Small-block safety: splitting each {rcell:g} cell x{factor} "
                    "(nearest neighbour, values unchanged)")
            raster_in = warp(raster_in, rcell / factor, 0, window, tag + "sub")
        return raster_in, band_r, rcell, coarsened

    # terrain method: lines keep the raw method; a coarse grid must be an Average, not a Maximum
    p.setdefault("method", "raw")
    if p["method"] != "raw" and is_line:
        log("Terrain method is for polygon blocks; lines use the highest DEM cell.")
        p["method"] = "raw"
    if p["method"] != "raw" and dem_crs.isGeographic():
        raise ValueError("The terrain method needs a DEM in metres. Reproject the DEM to UTM "
                         "(e.g. EPSG:32648) or choose 'Highest DEM cell'.")
    if p["method"] != "raw":
        log(f"Method: {dict((v, k) for k, v in METHODS)[p['method']]}")
        log(f"  ground filter {p['ground_win']:g} m (removes trees / dikes narrower than this)"
            + (f", smoothing {p['smooth_win']:g} m" if p["method"] == "slope" else ""))
        if p["resample"] != 5:
            p["resample"] = 5
            log("  coarsening (if any) uses Average for the terrain method")
    res_used = p["target_res"] if p["target_res"] else cell
    raster_in, band_r, rcell, coarsened = build_raster(p["resample"], "res")
    log(f"Analysis resolution: {res_used:g} m")
    find_max, find_min = p.get("find_max", True), p.get("find_min", False)
    lo_method = p["resample"]
    raster_lo, band_lo, rcell_lo, coarsened_lo = raster_in, band_r, rcell, coarsened
    if find_min and coarsened and p["resample"] == 7:
        # a "Maximum" grid cannot show the lowest point -> matching "Minimum" grid
        lo_method = 8
        raster_lo, band_lo, rcell_lo, coarsened_lo = build_raster(8, "lo", " (for the lowest point)")

    per = {}  # zid -> [max, min, mean, count]
    from osgeo import gdal
    if is_line:
        # 5) lines: every DEM cell the line touches
        ds = gdal.Open(raster_in.source().split("|")[0]) if raster_in.providerType() == "gdal" else None
        if ds is None:
            raise RuntimeError("This raster type cannot be read directly for line layers.")
        log("Sampling the DEM cells touched by each line ...")
        for f in zones.getFeatures():
            zid, g = int(f[KEY]), f.geometry()
            hit = None if g is None or g.isEmpty() else locate_max(ds, band_r, g, touched_only=True,
                                                                   stats=True)
            if hit is None:
                per[zid] = [None, None, None, 0]
            else:
                per[zid] = [hit[0], hit[4], hit[5], hit[6]]
        ds = None
    else:
        # 5) zonal statistics per polygon: 0=count 2=mean 5=min 6=max
        log("Running zonal statistics ...")
        zs = as_layer(run("native:zonalstatisticsfb", {
            "INPUT": zones, "INPUT_RASTER": raster_in, "RASTER_BAND": band_r,
            "COLUMN_PREFIX": "_z", "STATISTICS": [0, 2, 5, 6], "OUTPUT": TEMP}))
        for f in zs.getFeatures():
            per[int(f[KEY])] = [num(f["_zmax"]), num(f["_zmin"]), num(f["_zmean"]),
                                int(num(f["_zcount"]) or 0)]

    # 6) optional grouping by attribute
    final = dict(per)
    if p["mode"] == "field":
        zf = p["zone_field"]
        if not zf:
            raise ValueError("Choose a field to combine by (e.g. 'layer').")
        if work.fields().indexOf(zf) == -1:
            raise ValueError(f"Field '{zf}' does not exist in layer '{blocks.name()}'.")
        key_of = {int(f[KEY]): f[zf] for f in work.getFeatures()}
        groups = {}
        for zid, (mx, mn, me, c) in per.items():
            g = groups.setdefault(key_of.get(zid), [None, None, 0.0, 0])
            if mx is not None:
                g[0] = mx if g[0] is None else max(g[0], mx)
                g[1] = mn if g[1] is None else min(g[1], mn)
                g[2] += (me or 0) * c
                g[3] += c
        for zid in per:
            g = groups[key_of.get(zid)]
            final[zid] = [g[0], g[1], (g[2] / g[3]) if g[3] else None, g[3]]
        log(f"Combined {len(per)} polygons into {len(groups)} groups by '{zf}'")

    # 6b) location (XY) of the highest / lowest cell
    geoms = {int(f[KEY]): f.geometry() for f in zones.getFeatures()}

    terrain = p["method"] != "raw"
    ext_hi, ext_lo = {}, {}  # zid -> slope / ground details (terrain method)

    def find_points(r_lyr, r_band, r_cell, r_coarsened, method, lowest):
        word = "lowest" if lowest else "highest"
        ext = ext_lo if lowest else ext_hi
        better = (lambda a, b: a < b) if lowest else (lambda a, b: a > b)
        loc_, along_ = {}, {}
        ds = gdal.Open(r_lyr.source().split("|")[0]) if r_lyr.providerType() == "gdal" else None
        if ds is None:
            log(f"WARNING: this raster type cannot be read directly; XY of the {word} point skipped.")
            return loc_, along_
        log(f"Locating the {word} {'ground / general slope' if terrain else 'cell'} in each block ...")
        for n, (zid, g) in enumerate(geoms.items()):
            if g is None or g.isEmpty() or per.get(zid, [None])[0] is None:
                continue
            if terrain:
                hit = locate_terrain(ds, r_band, g, p, lowest=lowest)
                if hit is not None:
                    ext[zid] = hit[4]
            else:
                hit = locate_max(ds, r_band, g, touched_only=is_line, lowest=lowest)
            if hit is not None:
                loc_[zid] = hit[:4] + (zid,)  # (value, x, y, ties, feature holding the point)
            if n % 200 == 199:
                log(f"  {n + 1} / {len(geoms)} blocks")
        ds = None
        # coarsened with Maximum (highest) / Minimum (lowest): trace back to the ORIGINAL DEM cell
        refined = set()
        if loc_ and not terrain and r_coarsened and method == (8 if lowest else 7) and dem.providerType() == "gdal":
            ods = gdal.Open(dem.source().split("|")[0])
            if ods is not None:
                gx0, gy0 = dem.extent().xMinimum(), dem.extent().yMaximum()  # coarse grid origin
                e = cell * 0.01
                for zid, (v, x, y, ties, src) in list(loc_.items()):
                    cx0 = gx0 + math.floor((x - gx0) / r_cell) * r_cell
                    cy1 = gy0 - math.floor((gy0 - y) / r_cell) * r_cell
                    cellg = QgsGeometry.fromRect(QgsRectangle(cx0 + e, cy1 - r_cell + e,
                                                              cx0 + r_cell - e, cy1 - e))
                    if is_line:  # only fine cells the line itself touches
                        cellg = cellg.intersection(geoms[src])
                        if cellg.isEmpty():
                            continue
                    hit = locate_max(ods, p["band"], cellg, touched_only=is_line, lowest=lowest)
                    if hit is not None and abs(hit[0] - v) < 1e-4:
                        loc_[zid] = (v, hit[1], hit[2], hit[3], src)
                        refined.add(zid)
                ods = None
                log(f"  {word} point traced to the original {cell:g} m cells for {len(refined)} block(s)")
        if is_line:  # put the point ON the line, inside its cell
            for zid, (v, x, y, ties, src) in list(loc_.items()):
                pc = (cell if zid in refined else r_lyr.rasterUnitsPerPixelX()) / 2.0
                g = geoms[src]
                part = g.intersection(QgsGeometry.fromRect(QgsRectangle(x - pc, y - pc, x + pc, y + pc)))
                base = part if not part.isEmpty() else g
                npt = base.nearestPoint(QgsGeometry.fromPointXY(QgsPointXY(x, y))).asPoint()
                loc_[zid] = (v, npt.x(), npt.y(), ties, src)
                along_[src] = g.lineLocatePoint(QgsGeometry.fromPointXY(npt))
        if p["mode"] == "field":  # the group's point is the best among its members
            best_of = {}
            for zid, v in loc_.items():
                kk = key_of.get(zid)
                if kk not in best_of or better(v[0], best_of[kk][0]):
                    best_of[kk] = v
            loc_ = {zid: best_of[key_of.get(zid)] for zid in per if key_of.get(zid) in best_of}
        n_ties = sum(1 for v in loc_.values() if v[3] > 1)
        if terrain:
            flat = sum(1 for e_ in ext.values() if e_.get("fall_m") is not None and e_["fall_m"] < 0.05)
            log(f"  {word} point found for {len(loc_)} block(s)"
                + (f"; {flat} block(s) are almost level (fall < 5 cm): their {word} point is "
                   "weakly defined, check Fall_m / Slope_pct" if flat else ""))
            return loc_, along_
        log(f"  {word} point found for {len(loc_)} block(s)"
            + (f"; {n_ties} have several cells with the same {word} value "
               "(the first one is reported, see Top_cells)" if n_ties else ""))
        return loc_, along_

    loc, along = (find_points(raster_in, band_r, rcell, coarsened, p["resample"], False)
                  if find_max else ({}, {}))
    loc_min, along_min = (find_points(raster_lo, band_lo, rcell_lo, coarsened_lo, lo_method, True)
                          if find_min else ({}, {}))
    p["find_xy"] = find_max
    if terrain:  # the reported elevation is the ground / general-slope value, not the raw cell
        for zid in final:
            if zid in loc:
                final[zid] = [loc[zid][0]] + list(final[zid][1:])

    # 7) write results into the block layers (optional)
    sfx = "_" + res_label(res_used) if p["suffix_res"] else ""
    nd = p["decimals"]
    missing = [k for k in sorted(processed) if final.get(k, [None])[0] is None]
    n_done = len(processed)
    specs, target = [], None
    if write_blocks:
        if p["new_layer"]:
            target = work

            def key_for(f):
                return int(f[KEY])
        else:
            target = blocks

            def key_for(f):
                return f.id()

        prov = target.dataProvider()
        caps = prov.capabilities()
        if not (caps & CAP_ADD and caps & CAP_CHANGE):
            raise RuntimeError("This layer cannot be edited. Tick 'Create a new output layer'.")
        shp = prov.storageType() == "ESRI Shapefile"

        def fname(base, short):
            """Field name with optional resolution suffix; shapefiles are limited to 10 chars."""
            name = (short if sfx else base) + sfx
            if shp and len(name) > 10:
                name = short + sfx if len(short + sfx) <= 10 else short[:10 - len(sfx)] + sfx
            return name

        specs = [(fname(p["out_field"], p["out_field"]), T_DOUBLE)]
        if p["write_extra"]:
            specs += [(fname("Elev_Min", "EMin"), T_DOUBLE), (fname("Elev_Mean", "EMean"), T_DOUBLE),
                      (fname("Elev_Cells", "ECells"), T_INT)]
        if find_max:
            specs += [(fname("MaxElev_X", "MaxX"), T_DOUBLE), (fname("MaxElev_Y", "MaxY"), T_DOUBLE)]
        if find_min:
            specs += [(fname("Lowest_elevation", "Lo_Elev"), T_DOUBLE),
                      (fname("MinElev_X", "MinX"), T_DOUBLE), (fname("MinElev_Y", "MinY"), T_DOUBLE)]
        if terrain:  # slope of each block's ground surface (plane fit)
            specs += [(fname("Slope_pct", "Slope"), T_DOUBLE), (fname("Fall_m", "Fall"), T_DOUBLE),
                      (fname("Flow_dir", "FlowDir"), T_STRING), (fname("Aspect", "Aspect"), T_DOUBLE)]
        new = [make_field(n, t) for n, t in specs if target.fields().indexOf(n) == -1]
        if new:
            prov.addAttributes(new)
            target.updateFields()
        idx = [target.fields().indexOf(n) for n, _ in specs]

        xy_dec = 7 if dem_crs.isGeographic() else 3

        def r(v):
            return NULL if v is None else round(v, nd)
        changes, missing = {}, []
        for f in target.getFeatures():
            k = key_for(f)
            if k not in processed:
                continue
            s_ = final.get(k)
            if s_ is None or s_[0] is None:
                missing.append(k)
                vals = [NULL] if not p["write_extra"] else [NULL, NULL, NULL, 0]
            else:
                vals = [r(s_[0])] if not p["write_extra"] else [r(s_[0]), r(s_[1]), r(s_[2]), int(s_[3])]
            def xy(loc):
                if loc is None:
                    return [NULL, NULL]
                pt = QgsPointXY(loc[1], loc[2])
                if blk_crs != dem_crs:  # report X/Y in the block layer's own CRS
                    pt = QgsCoordinateTransform(dem_crs, blk_crs, QgsProject.instance()).transform(pt)
                dec = 7 if blk_crs.isGeographic() else xy_dec
                return [round(pt.x(), dec), round(pt.y(), dec)]
            if find_max:
                vals += xy(loc.get(k))
            if find_min:
                lm = loc_min.get(k)
                vals += [NULL if lm is None else round(lm[0], nd)] + xy(lm)
            if terrain:
                ex = ext_hi.get(k) or ext_lo.get(k) or {}

                def rr(v, d=3):
                    return NULL if v is None else round(v, d)
                vals += [rr(ex.get("slope_pct")), rr(ex.get("fall_m")),
                         ex.get("flow_dir") or NULL, rr(ex.get("aspect"), 1)]
            changes[f.id()] = {i: v for i, v in zip(idx, vals)}
        prov.changeAttributeValues(changes)
        n_done = len(changes)

    # highest-point records (the dialog puts all layers into one point layer)
    records, records_min = [], []
    carry = list(p.get("carry", []))
    if p["mode"] == "field" and p["zone_field"] and \
            p["zone_field"] not in [c[0] for c in carry]:
        carry.append((p["zone_field"], p["zone_field"]))
    if loc:
        records = point_records(blocks, loc, work, carry, p["mode"] == "field", dem_crs, nd, along,
                                extra=ext_hi if terrain else None)
    if loc_min:
        records_min = point_records(blocks, loc_min, work, carry, p["mode"] == "field", dem_crs, nd,
                                    along_min, extra=ext_lo if terrain else None)
    p["_records_min"] = records_min

    if write_blocks:
        if p["new_layer"]:
            prov.deleteAttributes([target.fields().indexOf(KEY)])
            target.updateFields()
            target.setName(f"{blocks.name()}_MaxElev")
            QgsProject.instance().addMapLayer(target)
            log(f"New layer added: {target.name()} (memory layer - save it via Export)")
        else:
            with contextlib.suppress(Exception):
                target.reload()  # refresh open attribute tables
        target.triggerRepaint()

    # summary
    maxes = [s[0] for s in final.values() if s[0] is not None]
    log("-" * 60)
    log(f"Blocks with a value : {n_done - len(missing)} / {n_done}")
    log(f"Resolution used     : {res_used:g} m")
    if specs:
        log(f"Fields written      : {', '.join(n for n, _ in specs)}")
    else:
        log("Block layer unchanged (highest points only)")
    if maxes:
        log(f"Max elevation range : {min(maxes):.{nd}f} to {max(maxes):.{nd}f}")
    if missing:
        log(f"WARNING: {len(missing)} block(s) got no DEM value (outside the DEM / all NoData "
            f"/ removed by the inner buffer). Feature ids: {missing[:20]}"
            + (" ..." if len(missing) > 20 else ""))

    raster_in = None
    for path in tmp_files:
        with contextlib.suppress(OSError):
            os.remove(path)
    log(f"Done in {time.time() - t0:.1f} s")
    p["_issues"] = [(blocks.name(),) + tuple(i) for i in issues]
    p["_zones"] = zones
    return n_done - len(missing), missing, records, sfx, target

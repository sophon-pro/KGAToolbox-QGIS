# -*- coding: utf-8 -*-
"""CSV of surveyed points -> DEM GeoTIFF -> contour lines.

The numerics behind the CSV to DEM and Contour tool. Both front ends call
``run`` - the Processing algorithm (scripts, the modeller, batch) and the
window - so they cannot drift apart. ``feedback`` is anything with
``pushInfo``, ``setProgress`` and ``isCanceled``.

Each interpolation method has its own options (``METHOD_OPTIONS``); the window
builds its per-method panel from the same table.
"""

import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
from osgeo import gdal, ogr, osr
from scipy.interpolate import RBFInterpolator, griddata
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

gdal.UseExceptions()

NODATA = -9999.0

IDW, LINEAR, CUBIC, NEAREST, RBF = "idw", "linear", "cubic", "nearest", "rbf"
METHODS = (IDW, LINEAR, CUBIC, NEAREST, RBF)

#: Default value of every method option. The window and the algorithm both
#: start from these, so a script that sets nothing gets what the window shows.
DEFAULT_OPTIONS = {
    "power": 2.0,            # IDW: weight = 1 / distance ** power
    "neighbours": 16,        # IDW: nearest points used per cell
    "radius": 0.0,           # IDW: ignore points further than this (0 = off)
    "fill_edges": True,      # Linear/Cubic: nearest height outside the outline
    "clamp": False,          # Cubic: keep heights inside the surveyed range
    "rbf_kernel": "thin_plate_spline",
    "rbf_neighbours": 50,    # RBF: nearest points per local fit
    "rbf_smoothing": 0.0,    # RBF: 0 = pass through every point
}

#: Options each method reads, so the window shows only those.
METHOD_OPTIONS = {
    IDW: ("power", "neighbours", "radius"),
    LINEAR: ("fill_edges",),
    CUBIC: ("fill_edges", "clamp"),
    NEAREST: (),
    RBF: ("rbf_kernel", "rbf_neighbours", "rbf_smoothing"),
}

RBF_KERNELS = ("thin_plate_spline", "cubic", "quintic", "linear")

#: Above this many cells the window asks before it starts.
BIG_GRID = 25_000_000


class Canceled(Exception):
    """The user stopped the job."""


def check(feedback):
    if feedback.isCanceled():
        raise Canceled()


# ----------------------------------------------------------------- reading

def _pick_column(df, spec, label):
    """Resolve a user-typed field name (case-insensitive) or 1-based column number."""
    spec = spec.strip()
    for c in df.columns:
        if c.lower() == spec.lower():
            return c
    if spec.isdigit() and 1 <= int(spec) <= df.shape[1]:
        return df.columns[int(spec) - 1]
    raise ValueError(
        f"{label} field '{spec}' not found in the CSV. "
        f"Available columns: {', '.join(df.columns)}")


def load_points(csv_path, feedback, x_field="", y_field="", z_field=""):
    """Load CSV. Chosen X/Y/Z fields win; otherwise the layout is auto-detected
    (single-layout and dual side-by-side layout)."""
    df = pd.read_csv(csv_path, header=0, sep=None, engine="python",
                     encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]

    chosen = [f.strip() for f in (x_field, y_field, z_field)]
    if any(chosen):
        if not all(chosen):
            raise ValueError("Set all three of X, Y and Z fields, or leave all blank.")
        cols = [_pick_column(df, f, l) for f, l in zip(chosen, ("X", "Y", "Z"))]
        if len(set(cols)) < 3:
            raise ValueError("X, Y and Z fields must be three different columns.")
        df = df[cols].copy()
        df.columns = ["Easting", "Northing", "Elevation"]
        return _finish_points(df, feedback)

    aliases = {
        "Easting":   ("easting", "east", "x", "lon", "long", "longitude"),
        "Northing":  ("northing", "north", "y", "lat", "latitude"),
        "Elevation": ("elevation", "elev", "z", "height", "alt", "altitude", "rl"),
    }
    lower = {c.lower(): c for c in df.columns}
    named = {}
    for target, names in aliases.items():
        hit = next((lower[n] for n in names if n in lower), None)
        if hit is not None:
            named[target] = hit

    if len(named) == 3:
        # Header names identify the columns, whatever the layout
        df = df.rename(columns={v: k for k, v in named.items()})
    elif df.shape[1] >= 10:
        # Dual-layout: two tables side by side (11 columns)
        left = df.iloc[:, 0:5].copy()
        right = df.iloc[:, 6:11].copy() if df.shape[1] >= 11 else pd.DataFrame()
        left.columns = ["No", "Easting", "Northing", "Elevation", "Code"]
        if not right.empty:
            right.columns = left.columns
        df = pd.concat([left, right], ignore_index=True)
    elif df.shape[1] >= 3:
        # Positional: X,Y,Z  |  No,X,Y,Z  |  No,X,Y,Z,Code
        names = {3: ["Easting", "Northing", "Elevation"],
                 4: ["No", "Easting", "Northing", "Elevation"]}.get(
                     df.shape[1], ["No", "Easting", "Northing", "Elevation", "Code"])
        df = df.iloc[:, :len(names)].copy()
        df.columns = names
    else:
        raise ValueError(
            f"CSV has {df.shape[1]} column(s); need at least 3 "
            "(Easting, Northing, Elevation).")

    return _finish_points(df, feedback)


def _finish_points(df, feedback):
    for c in ["Easting", "Northing", "Elevation"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Easting", "Northing", "Elevation"])
    if df.empty:
        raise ValueError("No rows with a numeric X, Y and Z were found. "
                         "Check which columns are chosen.")

    feedback.pushInfo(f"  Points loaded : {len(df):,}")
    feedback.pushInfo(f"  X range       : {df['Easting'].min():.3f}  to  {df['Easting'].max():.3f}")
    feedback.pushInfo(f"  Y range       : {df['Northing'].min():.3f}  to  {df['Northing'].max():.3f}")
    feedback.pushInfo(f"  Elev range    : {df['Elevation'].min():.3f}  to  {df['Elevation'].max():.3f}")
    return df


def auto_resolution(df):
    """Estimate pixel size from point density (~1/500 of extent, min 0.5 m)."""
    xr = df["Easting"].max() - df["Easting"].min()
    yr = df["Northing"].max() - df["Northing"].min()
    spacing = math.sqrt((xr * yr) / max(len(df), 1))
    res = max(min(max(xr, yr) / 500, spacing * 0.8), 0.5)
    return round(res, 2)


def grid_size(df, resolution):
    """(cols, rows) of the DEM the points would give at `resolution`."""
    w = df["Easting"].max() - df["Easting"].min() + 2 * resolution
    h = df["Northing"].max() - df["Northing"].min() + 2 * resolution
    return (max(int(math.ceil(w / resolution)), 2),
            max(int(math.ceil(h / resolution)), 2))


# ----------------------------------------------------------- interpolation

def _merged(options):
    merged = dict(DEFAULT_OPTIONS)
    merged.update(options or {})
    return merged


def _chunks(n, size):
    for s in range(0, n, size):
        yield s, min(s + size, n)


def idw_interpolate(kxy, kz, qxy, power=2.0, k=16, radius=0.0,
                    feedback=None, report=None):
    """Inverse distance weighting over the `k` nearest points (KD-tree).

    Memory stays flat however many survey points there are. With a `radius`,
    points further away are ignored; a cell with none inside it takes the
    nearest point's height rather than falling to zero.
    """
    tree = cKDTree(kxy)
    k = max(1, min(int(k), len(kxy)))
    out = np.empty(len(qxy), dtype=np.float64)
    for s, e in _chunks(len(qxy), 50_000):
        if feedback is not None:
            check(feedback)
        dist, idx = tree.query(qxy[s:e], k=k)
        if k == 1:
            dist, idx = dist[:, None], idx[:, None]
        zl = kz[idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            w = 1.0 / np.power(dist, power)
        if radius > 0:
            w[dist > radius] = 0.0
        exact = dist == 0
        has_exact = exact.any(axis=1)
        w[has_exact] = exact[has_exact]          # a point on the cell wins
        ws = w.sum(axis=1)
        zi = np.where(ws > 0, (w * zl).sum(axis=1) / np.where(ws > 0, ws, 1), zl[:, 0])
        out[s:e] = zi
        if report is not None:
            report(e / len(qxy))
    return out


def _rbf_interpolate(kxy, kz, qxy, opts, feedback, report):
    # Local fits on a shifted copy: a UTM easting of 5e5 ruins the conditioning.
    origin = kxy.mean(axis=0)
    try:
        fit = RBFInterpolator(
            kxy - origin, kz, kernel=opts["rbf_kernel"],
            neighbors=min(int(opts["rbf_neighbours"]), len(kxy)),
            smoothing=float(opts["rbf_smoothing"]))
    except np.linalg.LinAlgError as exc:
        raise ValueError(
            "The RBF fit is singular - the points are probably duplicated or "
            "all on one line. Remove duplicates or raise the smoothing.") from exc
    out = np.empty(len(qxy), dtype=np.float64)
    for s, e in _chunks(len(qxy), 20_000):
        check(feedback)
        out[s:e] = fit(qxy[s:e] - origin)
        report(e / len(qxy))
    return out


def interpolate(kxy, kz, qxy, method, options=None, feedback=None, report=None):
    """Heights at `qxy` (flat array) from the points, by `method`."""
    opts = _merged(options)
    if report is None:
        def report(_f):
            return None

    if method == IDW:
        return idw_interpolate(kxy, kz, qxy, opts["power"], opts["neighbours"],
                               opts["radius"], feedback, report)
    if method == NEAREST:
        _d, idx = cKDTree(kxy).query(qxy)
        report(1.0)
        return kz[idx]
    if method == RBF:
        return _rbf_interpolate(kxy, kz, qxy, opts, feedback, report)

    # Linear / Cubic: only defined inside the outline of the points.
    zg = griddata(kxy, kz, qxy, method=method)
    report(0.8)
    gaps = np.isnan(zg)
    if opts["fill_edges"] and gaps.any():
        _d, idx = cKDTree(kxy).query(qxy[gaps])
        zg[gaps] = kz[idx]
    if method == CUBIC and opts["clamp"]:
        zg = np.clip(zg, kz.min(), kz.max())
    report(1.0)
    return zg


# ------------------------------------------------------------------ writing

def generate_dem(df, resolution, method, epsg, out_path, feedback,
                 options=None, progress=None):
    """Interpolate points -> GeoTIFF DEM via GDAL. `progress` gets 0..1."""
    x, y, z = df["Easting"].values, df["Northing"].values, df["Elevation"].values

    # Extent with 1-pixel buffer
    x0, x1 = x.min() - resolution, x.max() + resolution
    y0, y1 = y.min() - resolution, y.max() + resolution

    cols = max(int(math.ceil((x1 - x0) / resolution)), 2)
    rows = max(int(math.ceil((y1 - y0) / resolution)), 2)
    feedback.pushInfo(f"  Grid       : {cols} cols x {rows} rows ({cols * rows:,} cells)")
    feedback.pushInfo(f"  Resolution : {resolution} m")
    feedback.pushInfo(f"  Method     : {method.upper()}")

    # Build query grid (top -> bottom raster order)
    xi = np.linspace(x0, x1, cols)
    yi = np.linspace(y1, y0, rows)
    gx, gy = np.meshgrid(xi, yi)
    qxy = np.column_stack([gx.ravel(), gy.ravel()])
    kxy = np.column_stack([x, y])

    zg = interpolate(kxy, z, qxy, method, options, feedback,
                     progress).reshape(rows, cols)
    check(feedback)
    if np.isnan(zg).any():
        feedback.pushInfo("  Cells outside the points' outline are left as NoData.")

    # Write GeoTIFF
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, cols, rows, 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES",
                     "BLOCKXSIZE=256", "BLOCKYSIZE=256"])
    pw = (x1 - x0) / cols
    ph = -(y1 - y0) / rows
    ds.SetGeoTransform([x0, pw, 0, y1, 0, ph])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(np.where(np.isnan(zg), NODATA, zg).astype(np.float32))
    band.FlushCache()
    ds = None

    geo_transform = (x0, pw, 0, y1, 0, ph)
    feedback.pushInfo(f"  Elev range : {np.nanmin(zg):.3f}  to  {np.nanmax(zg):.3f} m")
    feedback.pushInfo(f"  DEM saved  : {out_path}")
    return zg, geo_transform


def _smooth(zg, sigma):
    """Gaussian blur that copes with NoData (NaN) cells instead of spreading them."""
    if sigma <= 0:
        return zg.copy()
    bad = np.isnan(zg)
    if not bad.any():
        return gaussian_filter(zg, sigma=sigma)
    weight = gaussian_filter((~bad).astype(float), sigma=sigma)
    blurred = gaussian_filter(np.where(bad, 0.0, zg), sigma=sigma)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = blurred / weight
    out[bad | (weight < 1e-6)] = np.nan
    return out


def _segments(cs):
    """(level, vertices) for every traced line; matplotlib 3.8 dropped .collections."""
    if hasattr(cs, "allsegs"):
        for lvl, segs in zip(cs.levels, cs.allsegs):
            for seg in segs:
                yield lvl, seg
    else:                                       # matplotlib < 3.8
        for lvl, col in zip(cs.levels, cs.collections):
            for path in col.get_paths():
                yield lvl, path.vertices


def generate_contours(zg, geo_transform, epsg, interval, smooth_sigma,
                      out_path, fmt, feedback):
    """Extract contour lines -> Shapefile or GeoPackage via OGR."""
    from matplotlib.figure import Figure        # no pyplot: safe off the GUI thread

    rows, cols = zg.shape
    work = _smooth(zg, smooth_sigma)

    z_min, z_max = float(np.nanmin(zg)), float(np.nanmax(zg))
    levels = np.arange(
        math.floor(z_min / interval) * interval,
        math.ceil(z_max / interval) * interval + interval,
        interval,
    )
    feedback.pushInfo(f"  Levels     : {len(levels)}  (interval = {interval} m)")

    # Pixel-centre world coordinates
    x0, pw, _, y0, _, ph = geo_transform
    xs = x0 + (np.arange(cols) + 0.5) * pw
    ys = y0 + (np.arange(rows) + 0.5) * ph

    ax = Figure().subplots()
    cs = ax.contour(xs, ys, np.ma.masked_invalid(work), levels=levels)
    check(feedback)

    drv = ogr.GetDriverByName("GPKG" if fmt == "gpkg" else "ESRI Shapefile")
    if os.path.exists(out_path):
        drv.DeleteDataSource(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    ds_out = drv.CreateDataSource(out_path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    lyr = ds_out.CreateLayer(Path(out_path).stem, srs=srs,
                             geom_type=ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("elevation", ogr.OFTReal))
    lyr.CreateField(ogr.FieldDefn("elev_m", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("interval", ogr.OFTReal))

    fc = 0
    for lvl, seg in _segments(cs):
        if len(seg) < 2:
            continue
        line = ogr.Geometry(ogr.wkbLineString)
        for vx, vy in seg:
            line.AddPoint_2D(float(vx), float(vy))
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(line)
        feat.SetField("elevation", float(lvl))
        feat.SetField("elev_m", int(round(lvl)))
        feat.SetField("interval", float(interval))
        lyr.CreateFeature(feat)
        fc += 1

    ds_out = None
    feedback.pushInfo(f"  Features   : {fc:,}")
    feedback.pushInfo(f"  Contour    : {out_path}")
    return fc


# ---------------------------------------------------------------------- job

def run(csv_path, out_dir, feedback, *, x_field="", y_field="", z_field="",
        resolution=0.0, method=IDW, options=None, epsg=32648,
        contour=True, interval=1.0, smoothing=1.0, fmt="shp"):
    """The whole job. Returns ``{'dem': path, 'contour': path or None, 'base': name}``.

    Raises ``Canceled`` when `feedback` is canceled, ``ValueError`` for bad input.
    """
    base = Path(csv_path).stem
    os.makedirs(out_dir, exist_ok=True)

    feedback.pushInfo("[1/3] Loading CSV")
    feedback.setProgress(2)
    check(feedback)
    df = load_points(csv_path, feedback, x_field, y_field, z_field)
    if len(df) < 3:
        raise ValueError(f"Only {len(df)} usable point(s); at least 3 are needed.")

    feedback.pushInfo("\n[2/3] Building the DEM")
    check(feedback)
    cell = resolution if resolution > 0 else auto_resolution(df)
    if resolution <= 0:
        feedback.pushInfo(f"  Auto cell size : {cell} m")
    dem_path = os.path.join(out_dir, f"{base}_DEM.tif")
    top = 70 if contour else 95
    zg, geo = generate_dem(
        df, cell, method, epsg, dem_path, feedback, options,
        progress=lambda f: feedback.setProgress(10 + f * (top - 10)))

    cnt_path = None
    if contour:
        feedback.pushInfo("\n[3/3] Tracing contours")
        feedback.setProgress(75)
        ext = "gpkg" if fmt == "gpkg" else "shp"
        cnt_path = os.path.join(out_dir, f"{base}_Contour_{interval}m.{ext}")
        generate_contours(zg, geo, epsg, interval, smoothing,
                          cnt_path, fmt, feedback)
    else:
        feedback.pushInfo("\n[3/3] Contours skipped")

    feedback.setProgress(100)
    return {"dem": dem_path, "contour": cnt_path, "base": base}

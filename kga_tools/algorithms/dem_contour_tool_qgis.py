"""
╔══════════════════════════════════════════════════════════════════╗
║   DEM & Contour Tool  —  QGIS Processing Toolbox Script          ║
║                                                                    ║
║   Mode A : CSV  → DEM → Contour                                   ║
║   Mode B : DEM  →       Contour only                              ║
║                                                                    ║
║   WHERE TO FIND IT                                                ║
║   Ships with the KGA Toolbox plugin. Processing Toolbox →         ║
║   KGA Toolbox → KGA Irrigation Tools → DEM & Contour Tool,        ║
║   or the Irrigation Tools button on the KGA Toolbox toolbar.      ║
╚══════════════════════════════════════════════════════════════════╝

CHECKPOINT / RESUME  (same pattern as the DEM Elevation Correction tool)
--------------------------------------------------------------------------
Contours are generated in row TILES. After each tile is written and
flushed to disk, progress is recorded in a single checkpoint.json in the
output folder — same idea as the correction tool's stage checkpoints:

    checkpoint[stage_key] = {
        "fingerprint":      md5 of everything that determines this output,
        "output_path":      path to the contour file being built,
        "status":           "in_progress" | "complete",
        "completed_tiles":  [0, 1, 2, ...],
        "num_tiles":        total tile count,
    }

Rerunning the tool with the SAME DEM, output folder, and parameters:
  - fingerprint matches + status "complete"   -> instantly skipped
  - fingerprint matches + status "in_progress" -> resumes from the next
    unfinished tile
  - fingerprint differs (DEM changed, interval changed, script updated,
    etc.) -> treated as stale, starts fresh automatically
  - "Force re-run" checkbox -> ignores checkpoint.json entirely

Just like the correction tool, bump SCRIPT_VERSION whenever the contour
logic changes, so old checkpoints from a previous version of this script
are never silently (and wrongly) reused.
"""

# ── QGIS imports ─────────────────────────────────────────────────────────────
from qgis.PyQt.QtCore import QCoreApplication, QSettings
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsProject,
)

# ── scientific / GDAL ────────────────────────────────────────────────────────
import os, math, json, hashlib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

from osgeo import gdal, ogr, osr
gdal.UseExceptions()

# Bump this whenever the contour logic changes, so old checkpoint.json
# files from a previous script version are automatically treated as stale
# instead of being wrongly reused.
SCRIPT_VERSION = "2"


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED UTILITY FUNCTIONS  (unchanged from the original tool)
# ═════════════════════════════════════════════════════════════════════════════

def auto_detect_method(df, feedback):
    """Choose interpolation method from point count & density."""
    n       = len(df)
    xr      = df["Easting"].max()  - df["Easting"].min()
    yr      = df["Northing"].max() - df["Northing"].min()
    density = n / max(xr * yr, 1)
    spacing = math.sqrt(1.0 / max(density, 1e-9))

    if n < 50:
        m, r = "nearest", f"Very few points ({n}) — nearest safest"
    elif n < 300:
        m, r = "linear",  f"Sparse points ({n}) — linear triangulation"
    elif density > 0.5:
        m, r = "idw",     f"Dense survey ({n} pts, {density:.5f} pts/m²) — IDW"
    elif spacing > 50:
        m, r = "cubic",   f"Wide spacing ({spacing:.1f} m avg) — cubic smoother"
    else:
        m, r = "idw",     f"General survey ({n} pts) — IDW"

    feedback.pushInfo(f"  Auto-detect → {m.upper()}  ({r})")
    return m


def auto_resolution(df):
    xr      = df["Easting"].max()  - df["Easting"].min()
    yr      = df["Northing"].max() - df["Northing"].min()
    spacing = math.sqrt((xr * yr) / max(len(df), 1))
    return round(max(min(max(xr, yr) / 500, spacing * 0.8), 0.5), 2)


def load_points(csv_path, feedback):
    """Load CSV — supports single and dual side-by-side layouts."""
    feedback.pushInfo(f"  Reading : {csv_path}")
    df = pd.read_csv(csv_path, header=0)

    if df.shape[1] >= 10:
        left  = df.iloc[:, 0:5].copy()
        right = df.iloc[:, 6:11].copy() if df.shape[1] >= 11 else pd.DataFrame()
        left.columns = ["No", "Easting", "Northing", "Elevation", "Code"]
        if not right.empty:
            right.columns = left.columns
        df = pd.concat([left, right], ignore_index=True)
    else:
        df.columns = ["No", "Easting", "Northing", "Elevation", "Code"][: df.shape[1]]

    for c in ["Easting", "Northing", "Elevation"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Easting", "Northing", "Elevation"])

    feedback.pushInfo(f"  Points    : {len(df):,}")
    feedback.pushInfo(f"  X range   : {df['Easting'].min():.3f}  –  {df['Easting'].max():.3f}")
    feedback.pushInfo(f"  Y range   : {df['Northing'].min():.3f}  –  {df['Northing'].max():.3f}")
    feedback.pushInfo(f"  Elev range: {df['Elevation'].min():.3f}  –  {df['Elevation'].max():.3f} m")
    return df


def idw_interpolate(kxy, kz, qxy, power=2.0, k=16):
    n = qxy.shape[0]
    z = np.empty(n, dtype=np.float64)
    for s in range(0, n, 30_000):
        e   = min(s + 30_000, n)
        d2  = np.sum((kxy[np.newaxis] - qxy[s:e, np.newaxis]) ** 2, axis=2)
        if kxy.shape[0] > k:
            idx = np.argpartition(d2, k, axis=1)[:, :k]
            d2  = np.take_along_axis(d2, idx, axis=1)
            zl  = kz[idx]
        else:
            zl  = np.tile(kz, (e - s, 1))
        exact = d2 == 0
        w     = np.where(d2 == 0, 0.0, 1.0 / (d2 ** (power / 2)))
        ws    = w.sum(1, keepdims=True); ws[ws == 0] = 1
        zi    = (w * zl).sum(1) / ws.squeeze()
        for i in np.where(exact.any(1))[0]:
            zi[i] = zl[i, np.argmax(exact[i])]
        z[s:e] = zi
    return z


# ═════════════════════════════════════════════════════════════════════════════
#  "REMEMBER LAST SETTINGS"  (QSettings — persists across QGIS restarts,
#  no file to manage. Same idea as the correction tool's save_last_settings,
#  just using Qt's native per-user settings store instead of a JSON file
#  since this is a Processing script with an auto-generated dialog rather
#  than a hand-built one.)
# ═════════════════════════════════════════════════════════════════════════════

_SETTINGS_GROUP = "DEMContourTool/last_run"


def _last(key, default, type_=None):
    s = QSettings()
    full_key = f"{_SETTINGS_GROUP}/{key}"
    return s.value(full_key, default, type=type_) if type_ is not None else s.value(full_key, default)


def _remember(key, value):
    QSettings().setValue(f"{_SETTINGS_GROUP}/{key}", value)


# ═════════════════════════════════════════════════════════════════════════════
#  CHECKPOINT HELPERS  (identical pattern to the DEM correction tool)
# ═════════════════════════════════════════════════════════════════════════════

def contour_fingerprint(dem_path, dem_mtime, interval, smooth, fmt, tile_rows, overlap_rows):
    """Hash of everything that determines this contour output. If any of
    these change between runs, the checkpoint is considered stale."""
    key = {
        "dem_path": os.path.abspath(dem_path), "dem_mtime": dem_mtime,
        "interval": interval, "smooth": smooth, "fmt": fmt,
        "tile_rows": tile_rows, "overlap_rows": overlap_rows,
        "script_version": SCRIPT_VERSION,
    }
    return hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()


def load_checkpoint(out_dir):
    path = os.path.join(out_dir, "checkpoint.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_checkpoint(out_dir, checkpoint):
    path = os.path.join(out_dir, "checkpoint.json")
    with open(path, "w") as f:
        json.dump(checkpoint, f, indent=2)


# ═════════════════════════════════════════════════════════════════════════════
#  TILED / CHECKPOINTED CONTOUR ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def _iter_lines(geom):
    """Yield every LineString piece out of a (Multi)LineString / GeometryCollection."""
    if geom is None or geom.IsEmpty():
        return
    flat_type = ogr.GT_Flatten(geom.GetGeometryType())
    if flat_type == ogr.wkbLineString:
        yield geom
    elif flat_type in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection):
        for i in range(geom.GetGeometryCount()):
            for g in _iter_lines(geom.GetGeometryRef(i)):
                yield g
    # points/polygons from degenerate intersections are skipped


def generate_contours(dem_path, interval, smooth, fmt, output_dir, feedback,
                      tile_rows=3000, overlap_rows=30, force=False,
                      stage_key="contour", cancel_check=None):
    """
    Row-tiled, checkpointed contour generator.

    Reads the DEM tile-by-tile from disk (never loads the whole raster into
    RAM). Every tile uses the SAME LEVEL_BASE (from the DEM's global min)
    so levels line up across tiles, and is read with a small overlap then
    clipped back to its own exclusive row range, so tiles fit together with
    no duplicate or missing lines at the seams.

    Progress is checkpointed to checkpoint.json in output_dir after every
    tile — same fingerprint/status pattern as the DEM correction tool.
    """
    base = Path(dem_path).stem
    ext      = "gpkg" if fmt == "gpkg" else "shp"
    drv_name = "GPKG"  if fmt == "gpkg" else "ESRI Shapefile"
    cnt_path = os.path.join(output_dir, f"{base}_Contour_{interval}m.{ext}")

    dem_mtime = os.path.getmtime(dem_path)
    fp = contour_fingerprint(dem_path, dem_mtime, interval, smooth, fmt, tile_rows, overlap_rows)

    checkpoint = load_checkpoint(output_dir)
    cached = checkpoint.get(stage_key)
    completed = set()
    resuming = False

    if not force and cached and cached.get("fingerprint") == fp:
        if cached.get("status") == "complete" and os.path.exists(cached.get("output_path", "")):
            feedback.pushInfo(f"  ✔ SKIPPED (already complete) — {cached['output_path']}")
            return cached["output_path"]
        elif cached.get("status") == "in_progress" and os.path.exists(cached.get("output_path", "")):
            completed = set(cached.get("completed_tiles", []))
            resuming = True
            feedback.pushInfo(f"  ↻ Resuming: {len(completed)}/{cached.get('num_tiles', '?')} "
                              f"tiles already done (checkpoint found).")
    elif cached and cached.get("fingerprint") != fp:
        feedback.pushInfo("  Checkpoint found but parameters/DEM changed since last run "
                          "— starting fresh.")

    ds_in = gdal.Open(dem_path)
    if ds_in is None:
        raise RuntimeError(f"Cannot open DEM: {dem_path}")
    band   = ds_in.GetRasterBand(1)
    gt     = ds_in.GetGeoTransform()
    prj    = ds_in.GetProjection()
    nodata = band.GetNoDataValue()
    cols, rows = ds_in.RasterXSize, ds_in.RasterYSize
    x0, pw, _, y0, _, ph = gt
    x_min, x_max = x0, x0 + cols * pw

    srs = osr.SpatialReference()
    srs.ImportFromWkt(prj)

    feedback.pushInfo(f"  DEM size  : {cols} × {rows} px  (streamed tile-by-tile, not fully loaded)")

    z_min, z_max = band.ComputeRasterMinMax(False)
    level_base = math.floor(z_min / interval) * interval
    n_levels_est = int(math.floor(z_max / interval) - math.floor(z_min / interval)) + 1
    feedback.pushInfo(f"  Elev range: {z_min:.3f} – {z_max:.3f} m   (~{n_levels_est} levels)")

    num_tiles = math.ceil(rows / tile_rows)
    tiles = [(i * tile_rows, min(i * tile_rows + tile_rows, rows)) for i in range(num_tiles)]

    drv_v = ogr.GetDriverByName(drv_name)
    layer_stem = Path(cnt_path).stem

    if resuming:
        ds_out = ogr.Open(cnt_path, 1)  # update mode
        if ds_out is None:
            feedback.pushWarning(f"  Checkpoint says tiles are done but '{cnt_path}' won't open "
                                 f"for update — starting fresh instead.")
            resuming = False
            completed = set()

    if not resuming:
        if os.path.exists(cnt_path):
            drv_v.DeleteDataSource(cnt_path)
        ds_out = drv_v.CreateDataSource(cnt_path)
        lyr = ds_out.CreateLayer(layer_stem, srs=srs, geom_type=ogr.wkbLineString)
        lyr.CreateField(ogr.FieldDefn("elevation", ogr.OFTReal))
        lyr.CreateField(ogr.FieldDefn("elev_m",    ogr.OFTInteger))
        lyr.CreateField(ogr.FieldDefn("interval",  ogr.OFTReal))
        lyr.CreateField(ogr.FieldDefn("tile_id",   ogr.OFTInteger))
        completed = set()
        checkpoint[stage_key] = {"fingerprint": fp, "output_path": cnt_path,
                                  "status": "in_progress", "completed_tiles": [],
                                  "num_tiles": num_tiles}
        save_checkpoint(output_dir, checkpoint)
    else:
        lyr = ds_out.GetLayer(0)

    layer_name = lyr.GetName()

    def progress_cb(tile_idx):
        completed.add(tile_idx)
        checkpoint[stage_key] = {"fingerprint": fp, "output_path": cnt_path,
                                  "status": "in_progress", "completed_tiles": sorted(completed),
                                  "num_tiles": num_tiles}
        save_checkpoint(output_dir, checkpoint)

    for tidx, (core_start, core_end) in enumerate(tiles):
        if cancel_check and cancel_check():
            feedback.pushInfo("  Cancelled — checkpoint saved, safe to resume later.")
            break
        if tidx in completed:
            continue

        feedback.setProgress(15 + int(70 * tidx / num_tiles))
        feedback.pushInfo(f"  Tile {tidx+1}/{num_tiles}  (rows {core_start}:{core_end})")

        # Idempotency: wipe any partial output from a previous crash mid-tile
        ds_out.ExecuteSQL(f'DELETE FROM "{layer_name}" WHERE tile_id = {tidx}')

        read_start = max(core_start - overlap_rows, 0)
        read_end   = min(core_end + overlap_rows, rows)
        arr = band.ReadAsArray(0, read_start, cols, read_end - read_start).astype(np.float64)
        if nodata is not None:
            arr[arr == nodata] = np.nan

        if smooth > 0:
            valid_mean = np.nanmean(arr) if np.isfinite(arr).any() else 0.0
            arr = gaussian_filter(np.nan_to_num(arr, nan=valid_mean), sigma=smooth)
            if nodata is not None:
                orig = band.ReadAsArray(0, read_start, cols, read_end - read_start)
                arr[orig == nodata] = np.nan

        nodata_val = -999999.0
        arr_filled = np.where(np.isnan(arr), nodata_val, arr).astype(np.float32)

        sub_gt = (x0, pw, 0, y0 + read_start * ph, 0, ph)
        mem_ds = gdal.GetDriverByName("MEM").Create("", cols, arr.shape[0], 1, gdal.GDT_Float32)
        mem_ds.SetGeoTransform(sub_gt)
        mem_ds.SetProjection(srs.ExportToWkt())
        mem_band = mem_ds.GetRasterBand(1)
        mem_band.SetNoDataValue(nodata_val)
        mem_band.WriteArray(arr_filled)
        mem_band.FlushCache()

        mem_vec_ds = ogr.GetDriverByName("Memory").CreateDataSource("tile_tmp")
        mem_lyr = mem_vec_ds.CreateLayer("tile", srs=srs, geom_type=ogr.wkbLineString)
        mem_lyr.CreateField(ogr.FieldDefn("elevation", ogr.OFTReal))

        options = [
            f"LEVEL_INTERVAL={interval}",
            f"LEVEL_BASE={level_base}",
            f"NODATA={nodata_val}",
            "ELEV_FIELD=elevation",
        ]
        gdal.ContourGenerateEx(mem_band, mem_lyr, options=options)

        # Clip to this tile's exclusive (non-overlap) row range so tiles
        # butt together without gaps or duplicate lines.
        core_ymax = y0 + core_start * ph
        core_ymin = y0 + core_end * ph
        core_box = ogr.CreateGeometryFromWkt(
            f"POLYGON(({x_min} {core_ymin}, {x_max} {core_ymin}, "
            f"{x_max} {core_ymax}, {x_min} {core_ymax}, {x_min} {core_ymin}))")

        n_written = 0
        mem_lyr.ResetReading()
        for feat in mem_lyr:
            elev = feat.GetField("elevation")
            clipped = feat.GetGeometryRef().Intersection(core_box)
            for line in _iter_lines(clipped):
                out_feat = ogr.Feature(lyr.GetLayerDefn())
                out_feat.SetGeometry(line)
                out_feat.SetField("elevation", elev)
                out_feat.SetField("elev_m", int(round(elev)))
                out_feat.SetField("interval", float(interval))
                out_feat.SetField("tile_id", tidx)
                lyr.CreateFeature(out_feat)
                out_feat = None
                n_written += 1

        mem_vec_ds = None
        mem_ds = None
        ds_out.FlushCache()   # commit this tile to disk BEFORE marking it done in checkpoint.json
        progress_cb(tidx)
        feedback.pushInfo(f"    ...{n_written:,} line segments written "
                          f"({len(completed)}/{num_tiles} tiles complete)")

    ds_in = None
    total_feat = lyr.GetFeatureCount()
    ds_out = None

    if len(completed) == num_tiles:
        checkpoint[stage_key] = {"fingerprint": fp, "output_path": cnt_path,
                                  "status": "complete", "completed_tiles": sorted(completed),
                                  "num_tiles": num_tiles}
        save_checkpoint(output_dir, checkpoint)
        feedback.pushInfo(f"  ✔ Contour → {cnt_path}  ({total_feat:,} features total)")
    else:
        feedback.pushWarning(
            f"  Stopped after {len(completed)}/{num_tiles} tiles. Run this tool again "
            f"with the same DEM/output folder/parameters to resume — checkpoint.json "
            f"has the progress.")

    return cnt_path


# ═════════════════════════════════════════════════════════════════════════════
#  PROCESSING ALGORITHM
# ═════════════════════════════════════════════════════════════════════════════

class DEMContourTool(QgsProcessingAlgorithm):

    # ── parameter keys ───────────────────────────────────────────────────────
    MODE           = "MODE"
    INPUT_CSV      = "INPUT_CSV"
    INPUT_DEM      = "INPUT_DEM"
    OUTPUT_FOLDER  = "OUTPUT_FOLDER"

    # CSV-mode only
    RESOLUTION     = "RESOLUTION"
    METHOD         = "METHOD"
    EPSG           = "EPSG"

    # Contour
    DO_CONTOUR     = "DO_CONTOUR"
    CONTOUR_INT    = "CONTOUR_INT"
    CONTOUR_SMOOTH = "CONTOUR_SMOOTH"
    CONTOUR_FMT    = "CONTOUR_FMT"

    # Tiling / checkpoint
    TILE_ROWS      = "TILE_ROWS"
    OVERLAP_ROWS   = "OVERLAP_ROWS"
    FORCE_RERUN    = "FORCE_RERUN"

    # Options
    LOAD_LAYERS    = "LOAD_LAYERS"

    # ── metadata ─────────────────────────────────────────────────────────────
    def name(self):        return "demcontourtool"
    def displayName(self): return "DEM and Contour Tool"
    def group(self):       return "KGA Irrigation Tools"
    def groupId(self):     return "kgairrigationtools"
    def tr(self, s):       return QCoreApplication.translate("Processing", s)
    def createInstance(self): return DEMContourTool()

    def shortHelpString(self):
        return (
            "<b>DEM &amp; Contour Tool</b><br><br>"
            "Two modes — select at the top:<br><br>"
            "<b>Mode A — CSV → DEM → Contour</b><br>"
            "Interpolates surveyed points (CSV) into a DEM GeoTIFF,<br>"
            "then generates contour lines.<br>"
            "Supports single-layout and dual side-by-side CSV formats.<br><br>"
            "<b>Mode B — DEM → Contour only</b><br>"
            "Skips interpolation. Loads an existing DEM directly<br>"
            "and generates contour lines from it.<br><br>"
            "<b>Interpolation methods (Mode A):</b><br>"
            "• Auto-detect — analyses point count &amp; density<br>"
            "• IDW — best for dense surveys<br>"
            "• Linear — fast, for moderate point counts<br>"
            "• Cubic — smooth surfaces, wide spacing<br>"
            "• Nearest — very sparse or classified data<br><br>"
            "<b>Checkpoint / Resume:</b> contours are generated in row tiles "
            "and progress is saved to <code>checkpoint.json</code> in the "
            "output folder after every tile. If interrupted, rerun with the "
            "same DEM/output folder/parameters to continue automatically — "
            "same mechanism as the DEM Elevation Correction tool. Tick "
            "'Force re-run' to ignore an existing checkpoint.<br><br>"
            "<b>Remembers your settings:</b> every field above (except "
            "'Force re-run', which always resets to unchecked) is saved when "
            "you click Run and pre-filled automatically the next time you "
            "open this dialog — no need to re-enter the DEM, output folder, "
            "interval, etc. to resume a previous run.<br><br>"
            "<b>Default EPSG:</b> 32648 (WGS 84 / UTM zone 48N — Cambodia)"
        )

    # ── parameters ───────────────────────────────────────────────────────────
    def initAlgorithm(self, config=None):

        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, self.tr("Input Mode"),
            options=["Mode A — CSV → DEM → Contour", "Mode B — DEM → Contour only"],
            defaultValue=_last("mode", 0, int),
        ))

        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_CSV, self.tr("Input Point CSV  [Mode A only]"),
            behavior=QgsProcessingParameterFile.File,
            fileFilter="CSV Files (*.csv);;All Files (*.*)", optional=True,
            defaultValue=_last("input_csv", None, str) or None,
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.RESOLUTION, self.tr("DEM Resolution (m)  [Mode A — 0 = auto-detect]"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=_last("resolution", 0.0, float), minValue=0.0, optional=True,
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, self.tr("Interpolation Method  [Mode A only]"),
            options=["Auto-detect (analyses point density)", "IDW — Inverse Distance Weighting",
                    "Linear — Delaunay triangulation", "Cubic — smooth surface", "Nearest Neighbour"],
            defaultValue=_last("method", 0, int),
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.EPSG, self.tr("CRS — EPSG Code  [Mode A only]"),
            type=QgsProcessingParameterNumber.Integer, defaultValue=_last("epsg", 32648, int),
        ))

        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_DEM, self.tr("Input DEM Raster  [Mode B only]"), optional=True,
            defaultValue=_last("input_dem", None, str) or None,
        ))

        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Output Folder"),
            defaultValue=_last("output_folder", None, str) or None,
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.DO_CONTOUR, self.tr("Generate Contour Lines"),
            defaultValue=_last("do_contour", True, bool),
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.CONTOUR_INT, self.tr("Contour Interval (m)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=_last("contour_interval", 1.0, float), minValue=0.01,
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.CONTOUR_SMOOTH, self.tr("Contour Smoothing — Gaussian σ  (0 = off)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=_last("contour_smooth", 1.0, float), minValue=0.0,
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.CONTOUR_FMT, self.tr("Contour Output Format"),
            options=["Shapefile (.shp)", "GeoPackage (.gpkg)"],
            defaultValue=_last("contour_fmt", 1, int),
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.TILE_ROWS, self.tr("Tile height (rows)"),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=_last("tile_rows", 3000, int), minValue=100,
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.OVERLAP_ROWS, self.tr("Tile overlap (rows)"),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=_last("overlap_rows", 30, int), minValue=1,
        ))

        # Force-rerun is deliberately NEVER restored from last time -- it must
        # always be re-checked consciously, so a resumable run never gets
        # silently thrown away just because it was checked on a previous run.
        self.addParameter(QgsProcessingParameterBoolean(
            self.FORCE_RERUN,
            self.tr("Force re-run everything (ignore checkpoint.json in output folder)"),
            defaultValue=False,
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD_LAYERS, self.tr("Add output layers to QGIS map canvas"),
            defaultValue=_last("load_layers", True, bool),
        ))

    # ── main ─────────────────────────────────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):

        mode_idx    = self.parameterAsEnum   (parameters, self.MODE,          context)
        csv_path    = self.parameterAsFile   (parameters, self.INPUT_CSV,     context)
        dem_layer   = self.parameterAsRasterLayer(parameters, self.INPUT_DEM, context)
        output_dir  = self.parameterAsString (parameters, self.OUTPUT_FOLDER, context)
        res_param   = self.parameterAsDouble (parameters, self.RESOLUTION,    context)
        method_idx  = self.parameterAsEnum   (parameters, self.METHOD,        context)
        epsg        = self.parameterAsInt    (parameters, self.EPSG,          context)
        do_contour  = self.parameterAsBoolean(parameters, self.DO_CONTOUR,    context)
        cnt_int     = self.parameterAsDouble (parameters, self.CONTOUR_INT,   context)
        cnt_smooth  = self.parameterAsDouble (parameters, self.CONTOUR_SMOOTH,context)
        cnt_fmt_idx = self.parameterAsEnum   (parameters, self.CONTOUR_FMT,   context)
        tile_rows   = self.parameterAsInt    (parameters, self.TILE_ROWS,     context)
        overlap_rows= self.parameterAsInt    (parameters, self.OVERLAP_ROWS,  context)
        force_rerun = self.parameterAsBoolean(parameters, self.FORCE_RERUN,   context)
        load_layers = self.parameterAsBoolean(parameters, self.LOAD_LAYERS,   context)

        mode    = "csv" if mode_idx == 0 else "dem"
        methods = ["auto", "idw", "linear", "cubic", "nearest"]
        method  = methods[method_idx]
        cnt_fmt = ["shp", "gpkg"][cnt_fmt_idx]

        # Remember everything (except Force re-run, see initAlgorithm) so the
        # dialog opens pre-filled with these same values next time.
        _remember("mode", mode_idx)
        _remember("input_csv", csv_path or "")
        _remember("input_dem", dem_layer.source() if dem_layer else "")
        _remember("output_folder", output_dir)
        _remember("resolution", res_param)
        _remember("method", method_idx)
        _remember("epsg", epsg)
        _remember("do_contour", do_contour)
        _remember("contour_interval", cnt_int)
        _remember("contour_smooth", cnt_smooth)
        _remember("contour_fmt", cnt_fmt_idx)
        _remember("tile_rows", tile_rows)
        _remember("overlap_rows", overlap_rows)
        _remember("load_layers", load_layers)

        os.makedirs(output_dir, exist_ok=True)

        feedback.pushInfo("=" * 55)
        feedback.pushInfo("  DEM & Contour Tool")
        feedback.pushInfo(f"  Mode      : {'A — CSV → DEM → Contour' if mode=='csv' else 'B — DEM → Contour only'}")
        feedback.pushInfo("=" * 55)

        dem_path = None
        base     = None

        # ═════════════════════════════════════════════════════════════════════
        #  MODE A — CSV → DEM
        # ═════════════════════════════════════════════════════════════════════
        if mode == "csv":

            if not csv_path or not os.path.isfile(csv_path):
                feedback.reportError("Mode A selected but no valid CSV provided.")
                return {}

            feedback.pushInfo("\n[1/3]  Loading CSV …")
            feedback.setProgress(5)
            if feedback.isCanceled(): return {}

            df = load_points(csv_path, feedback)

            if method == "auto":
                method = auto_detect_method(df, feedback)
            else:
                feedback.pushInfo(f"  Method    : {method.upper()}  (manual)")

            res = auto_resolution(df) if res_param <= 0 else float(res_param)
            feedback.pushInfo(f"  Resolution: {res} m  {'(auto)' if res_param <= 0 else '(manual)'}")

            feedback.pushInfo("\n[2/3]  Generating DEM …")
            feedback.setProgress(15)
            if feedback.isCanceled(): return {}

            x, y, z = (df["Easting"].values, df["Northing"].values, df["Elevation"].values)
            x0, x1 = x.min() - res, x.max() + res
            y0, y1 = y.min() - res, y.max() + res
            cols   = max(int(math.ceil((x1-x0)/res)), 2)
            rows   = max(int(math.ceil((y1-y0)/res)), 2)
            feedback.pushInfo(f"  Grid      : {cols} cols × {rows} rows")

            xi = np.linspace(x0, x1, cols)
            yi = np.linspace(y1, y0, rows)
            gx, gy = np.meshgrid(xi, yi)
            qxy = np.column_stack([gx.ravel(), gy.ravel()])
            kxy = np.column_stack([x, y])

            if method == "idw":
                zg = idw_interpolate(kxy, z, qxy).reshape(rows, cols)
            else:
                zg = griddata(kxy, z, qxy, method=method).reshape(rows, cols)
                mask = np.isnan(zg)
                if mask.any():
                    feedback.pushInfo("  Filling NoData edges …")
                    zg[mask] = griddata(kxy, z, qxy[mask.ravel()], method="nearest")

            base     = Path(csv_path).stem
            dem_path = os.path.join(output_dir, f"{base}_DEM.tif")
            pw =  (x1-x0)/cols
            ph = -(y1-y0)/rows
            geo_transform = (x0, pw, 0, y1, 0, ph)

            drv = gdal.GetDriverByName("GTiff")
            ds  = drv.Create(dem_path, cols, rows, 1, gdal.GDT_Float32,
                             ["COMPRESS=LZW","TILED=YES","BLOCKXSIZE=256","BLOCKYSIZE=256"])
            ds.SetGeoTransform(geo_transform)
            srs_obj = osr.SpatialReference()
            srs_obj.ImportFromEPSG(epsg)
            ds.SetProjection(srs_obj.ExportToWkt())
            band = ds.GetRasterBand(1)
            band.SetNoDataValue(-9999.0)
            band.WriteArray(zg.astype(np.float32))
            band.FlushCache(); ds = None

            feedback.pushInfo(f"  Elev range: {np.nanmin(zg):.3f}  –  {np.nanmax(zg):.3f} m")
            feedback.pushInfo(f"  ✔ DEM → {dem_path}")
            feedback.setProgress(60)

        # ═════════════════════════════════════════════════════════════════════
        #  MODE B — DEM only  (metadata only — no full-raster read)
        # ═════════════════════════════════════════════════════════════════════
        else:
            if dem_layer is None:
                feedback.reportError("Mode B selected but no DEM layer provided.")
                return {}

            feedback.pushInfo("\n[1/2]  Reading DEM metadata …")
            feedback.setProgress(10)
            if feedback.isCanceled(): return {}

            dem_path = dem_layer.source()
            base     = Path(dem_path).stem

            ds_in = gdal.Open(dem_path)
            if ds_in is None:
                feedback.reportError(f"Cannot open DEM: {dem_path}")
                return {}
            gt  = ds_in.GetGeoTransform()
            band = ds_in.GetRasterBand(1)
            z_min, z_max = band.ComputeRasterMinMax(False)
            feedback.pushInfo(f"  File      : {Path(dem_path).name}")
            feedback.pushInfo(f"  Size      : {ds_in.RasterXSize} × {ds_in.RasterYSize} px")
            feedback.pushInfo(f"  Pixel     : {abs(gt[1]):.3f} × {abs(gt[5]):.3f} m")
            feedback.pushInfo(f"  Elev range: {z_min:.3f}  –  {z_max:.3f} m")
            ds_in = None
            feedback.setProgress(15)

        # ═════════════════════════════════════════════════════════════════════
        #  CONTOUR  (both modes — tiled & checkpointed)
        # ═════════════════════════════════════════════════════════════════════
        cnt_path   = None
        step_label = "[3/3]" if mode == "csv" else "[2/2]"

        if do_contour:
            feedback.pushInfo(f"\n{step_label}  Generating Contours …")
            if feedback.isCanceled(): return {}

            cnt_path = generate_contours(
                dem_path, cnt_int, cnt_smooth, cnt_fmt, output_dir, feedback,
                tile_rows=tile_rows, overlap_rows=overlap_rows, force=force_rerun,
                cancel_check=feedback.isCanceled,
            )
        else:
            feedback.pushInfo(f"\n{step_label}  Contour skipped.")

        feedback.setProgress(90)

        # ═════════════════════════════════════════════════════════════════════
        #  LOAD INTO CANVAS
        # ═════════════════════════════════════════════════════════════════════
        if load_layers:
            feedback.pushInfo("\n  Loading layers into QGIS canvas …")

            if mode == "csv" and dem_path and os.path.isfile(dem_path):
                rl = QgsRasterLayer(dem_path, f"{base} DEM")
                if rl.isValid():
                    QgsProject.instance().addMapLayer(rl)
                    feedback.pushInfo(f"  ✔ Raster added : {base} DEM")
                else:
                    feedback.pushWarning(f"  Could not load DEM: {dem_path}")

            if cnt_path and os.path.exists(cnt_path):
                vl = QgsVectorLayer(cnt_path, f"{base} Contour {cnt_int}m", "ogr")
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
                    feedback.pushInfo(f"  ✔ Vector added : {base} Contour {cnt_int}m")
                else:
                    feedback.pushWarning(f"  Could not load contour: {cnt_path}")

        # ═════════════════════════════════════════════════════════════════════
        #  SUMMARY
        # ═════════════════════════════════════════════════════════════════════
        feedback.setProgress(100)
        feedback.pushInfo("\n" + "=" * 55)
        feedback.pushInfo("  ✔  Done (or safely paused — rerun to resume)")
        if mode == "csv":
            feedback.pushInfo(f"  DEM     → {Path(dem_path).name}")
        if cnt_path:
            feedback.pushInfo(f"  Contour → {Path(cnt_path).name}")
        feedback.pushInfo(f"  Folder  → {output_dir}")
        feedback.pushInfo("=" * 55)

        result = {"OUTPUT_FOLDER": output_dir}
        if dem_path: result["DEM"]     = dem_path
        if cnt_path: result["CONTOUR"] = cnt_path
        return result

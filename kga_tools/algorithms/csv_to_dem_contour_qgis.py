"""
╔══════════════════════════════════════════════════════╗
║   CSV → DEM → Contour  —  QGIS Processing Script   ║
╠══════════════════════════════════════════════════════╣
║  INSTALL                                            ║
║  1. Processing Toolbox → ⚙ → Add Script to Toolbox ║
║  2. Browse to this file → Open                      ║
║  3. Scripts → Reservoir → CSV to DEM and Contour   ║
╚══════════════════════════════════════════════════════╝
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsProject,
)

import os, math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

from osgeo import gdal, ogr, osr
gdal.UseExceptions()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ═══════════════════════════════════════════════════════
#  CORE FUNCTIONS
# ═══════════════════════════════════════════════════════

def load_points(csv_path, feedback):
    """Load CSV — supports single-layout and dual side-by-side layout."""
    df = pd.read_csv(csv_path, header=0)

    # Dual-layout: two tables side by side (11 columns)
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

    feedback.pushInfo(f"  Points loaded : {len(df):,}")
    feedback.pushInfo(f"  X range       : {df['Easting'].min():.3f}  –  {df['Easting'].max():.3f}")
    feedback.pushInfo(f"  Y range       : {df['Northing'].min():.3f}  –  {df['Northing'].max():.3f}")
    feedback.pushInfo(f"  Elev range    : {df['Elevation'].min():.3f}  –  {df['Elevation'].max():.3f}")
    return df


def auto_resolution(df):
    """Estimate pixel size from point density (~1/500 of extent, min 0.5 m)."""
    xr = df["Easting"].max()  - df["Easting"].min()
    yr = df["Northing"].max() - df["Northing"].min()
    spacing = math.sqrt((xr * yr) / max(len(df), 1))
    res = max(min(max(xr, yr) / 500, spacing * 0.8), 0.5)
    return round(res, 2)


def idw_interpolate(kxy, kz, qxy, power=2.0, k=16):
    """Fast chunk-based IDW."""
    n, z = qxy.shape[0], np.empty(qxy.shape[0], dtype=np.float64)
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


def generate_dem(df, resolution, method, epsg, out_path, feedback):
    """Interpolate points → GeoTIFF DEM via GDAL."""
    x, y, z = df["Easting"].values, df["Northing"].values, df["Elevation"].values

    # Extent with 1-pixel buffer
    x0, x1 = x.min() - resolution, x.max() + resolution
    y0, y1 = y.min() - resolution, y.max() + resolution

    cols = max(int(math.ceil((x1 - x0) / resolution)), 2)
    rows = max(int(math.ceil((y1 - y0) / resolution)), 2)
    feedback.pushInfo(f"  Grid      : {cols} cols × {rows} rows")
    feedback.pushInfo(f"  Resolution: {resolution} m")

    # Build query grid (top → bottom raster order)
    xi = np.linspace(x0, x1, cols)
    yi = np.linspace(y1, y0, rows)
    gx, gy = np.meshgrid(xi, yi)
    qxy = np.column_stack([gx.ravel(), gy.ravel()])
    kxy = np.column_stack([x, y])

    feedback.pushInfo(f"  Method    : {method.upper()}")
    if method == "idw":
        zg = idw_interpolate(kxy, z, qxy).reshape(rows, cols)
    else:
        zg = griddata(kxy, z, qxy, method=method).reshape(rows, cols)
        nan_mask = np.isnan(zg)
        if nan_mask.any():
            feedback.pushInfo("  Filling edge NoData gaps with nearest …")
            zg[nan_mask] = griddata(kxy, z, qxy[nan_mask.ravel()], method="nearest")

    # Write GeoTIFF
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")
    ds  = drv.Create(out_path, cols, rows, 1, gdal.GDT_Float32,
                     ["COMPRESS=LZW", "TILED=YES",
                      "BLOCKXSIZE=256", "BLOCKYSIZE=256"])
    pw =  (x1 - x0) / cols
    ph = -(y1 - y0) / rows
    ds.SetGeoTransform([x0, pw, 0, y1, 0, ph])
    srs = osr.SpatialReference(); srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(zg.astype(np.float32))
    band.FlushCache(); ds = None

    geo_transform = (x0, pw, 0, y1, 0, ph)
    feedback.pushInfo(f"  Elev range: {np.nanmin(zg):.3f}  –  {np.nanmax(zg):.3f} m")
    feedback.pushInfo(f"  DEM saved : {out_path}")
    return zg, geo_transform


def generate_contours(zg, geo_transform, epsg,
                      interval, smooth_sigma,
                      out_path, fmt, feedback):
    """Extract contour lines → Shapefile or GeoPackage via OGR."""
    rows, cols = zg.shape
    work = gaussian_filter(zg, sigma=smooth_sigma) if smooth_sigma > 0 else zg.copy()

    z_min, z_max = float(np.nanmin(zg)), float(np.nanmax(zg))
    levels = np.arange(
        math.floor(z_min / interval) * interval,
        math.ceil (z_max / interval) * interval + interval,
        interval,
    )
    feedback.pushInfo(f"  Levels    : {len(levels)}  (interval = {interval} m)")

    # Pixel-centre world coordinates
    x0, pw, _, y0, _, ph = geo_transform
    xs = np.array([x0 + (j + 0.5) * pw for j in range(cols)])
    ys = np.array([y0 + (i + 0.5) * ph for i in range(rows)])

    # Extract contour paths with matplotlib (Agg, no display)
    fig, ax = plt.subplots()
    cs = ax.contour(xs, ys, work, levels=levels)
    plt.close(fig)

    # Write with OGR
    drv_name = "GPKG" if fmt == "gpkg" else "ESRI Shapefile"
    drv = ogr.GetDriverByName(drv_name)
    if os.path.exists(out_path):
        drv.DeleteDataSource(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    ds_out = drv.CreateDataSource(out_path)
    srs    = osr.SpatialReference(); srs.ImportFromEPSG(epsg)
    lyr    = ds_out.CreateLayer(Path(out_path).stem, srs=srs,
                                geom_type=ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("elevation", ogr.OFTReal))
    lyr.CreateField(ogr.FieldDefn("elev_m",    ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("interval",  ogr.OFTReal))

    # matplotlib >= 3.8 removed .collections; use .allsegs instead
    import matplotlib as _mpl
    _mpl_ver = tuple(int(x) for x in _mpl.__version__.split(".")[:2])

    fc = 0
    if _mpl_ver >= (3, 8):
        # New API: allsegs is a list-of-lists of numpy arrays
        for lvl, segs in zip(cs.levels, cs.allsegs):
            for seg in segs:
                if len(seg) < 2:
                    continue
                line = ogr.Geometry(ogr.wkbLineString)
                for vx, vy in seg:
                    line.AddPoint(float(vx), float(vy))
                feat = ogr.Feature(lyr.GetLayerDefn())
                feat.SetGeometry(line)
                feat.SetField("elevation", float(lvl))
                feat.SetField("elev_m",    int(round(lvl)))
                feat.SetField("interval",  float(interval))
                lyr.CreateFeature(feat)
                fc += 1
    else:
        # Old API (matplotlib < 3.8)
        for lvl, col_obj in zip(cs.levels, cs.collections):
            for path in col_obj.get_paths():
                if len(path.vertices) < 2:
                    continue
                line = ogr.Geometry(ogr.wkbLineString)
                for vx, vy in path.vertices:
                    line.AddPoint(float(vx), float(vy))
                feat = ogr.Feature(lyr.GetLayerDefn())
                feat.SetGeometry(line)
                feat.SetField("elevation", float(lvl))
                feat.SetField("elev_m",    int(round(lvl)))
                feat.SetField("interval",  float(interval))
                lyr.CreateFeature(feat)
                fc += 1

    ds_out = None
    feedback.pushInfo(f"  Features  : {fc:,}")
    feedback.pushInfo(f"  Contour   : {out_path}")


# ═══════════════════════════════════════════════════════
#  QGIS PROCESSING ALGORITHM
# ═══════════════════════════════════════════════════════

class CSVtoDEMContour(QgsProcessingAlgorithm):

    INPUT_CSV      = "INPUT_CSV"
    OUTPUT_FOLDER  = "OUTPUT_FOLDER"
    RESOLUTION     = "RESOLUTION"
    METHOD         = "METHOD"
    EPSG           = "EPSG"
    DO_CONTOUR     = "DO_CONTOUR"
    CONTOUR_INT    = "CONTOUR_INT"
    CONTOUR_SMOOTH = "CONTOUR_SMOOTH"
    CONTOUR_FMT    = "CONTOUR_FMT"
    LOAD_LAYERS    = "LOAD_LAYERS"

    def name(self):        return "csvtodemcontour"
    def displayName(self): return "CSV to DEM and Contour"
    def group(self):       return "KGA Irrigation Tools"
    def groupId(self):     return "kgairrigationtools"

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/csvtodemcontour'
    def tr(self, s):       return QCoreApplication.translate("Processing", s)
    def createInstance(self): return CSVtoDEMContour()

    def shortHelpString(self):
        return (
            "<b>CSV → DEM → Contour</b><br><br>"
            "Converts a surveyed point CSV into:<br>"
            "① A DEM GeoTIFF<br>"
            "② Contour lines (Shapefile or GeoPackage)<br><br>"
            "<b>CSV columns:</b> No, Easting, Northing, Elevation, Code<br>"
            "Dual side-by-side layout is detected automatically.<br><br>"
            "<b>Methods:</b> IDW · Linear · Cubic · Nearest<br>"
            "<b>Default EPSG:</b> 32648 (WGS 84 / UTM zone 48N)"
        )

    def initAlgorithm(self, config=None):

        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_CSV, self.tr("Input Point CSV"),
            behavior=QgsProcessingParameterFile.File,
            fileFilter="CSV Files (*.csv);;All Files (*.*)",
        ))

        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Output Folder"),
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.RESOLUTION, self.tr("DEM Resolution (m)  [0 = auto-detect]"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0, optional=True,
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, self.tr("Interpolation Method"),
            options=["IDW – Inverse Distance Weighting",
                     "Linear – Delaunay triangulation",
                     "Cubic – smooth surface",
                     "Nearest Neighbour"],
            defaultValue=0,
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.EPSG, self.tr("CRS – EPSG Code"),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=32648,
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.DO_CONTOUR, self.tr("Generate Contour Lines"),
            defaultValue=True,
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.CONTOUR_INT, self.tr("Contour Interval (m)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=0.01,
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.CONTOUR_SMOOTH,
            self.tr("Contour Smoothing – Gaussian σ  (0 = off)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=0.0,
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.CONTOUR_FMT, self.tr("Contour Output Format"),
            options=["Shapefile (.shp)", "GeoPackage (.gpkg)"],
            defaultValue=0,
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD_LAYERS,
            self.tr("Add output layers to QGIS map canvas"),
            defaultValue=True,
        ))

    # ── run ──────────────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):

        feedback.pushInfo("=" * 55)
        feedback.pushInfo("  CSV → DEM → Contour")
        feedback.pushInfo("=" * 55)

        # ── parameters ───────────────────────────────────────────────────────
        csv_path    = self.parameterAsFile   (parameters, self.INPUT_CSV,      context)
        out_dir     = self.parameterAsString (parameters, self.OUTPUT_FOLDER,  context)
        res_param   = self.parameterAsDouble (parameters, self.RESOLUTION,     context)
        method_idx  = self.parameterAsEnum   (parameters, self.METHOD,         context)
        epsg        = self.parameterAsInt    (parameters, self.EPSG,           context)
        do_contour  = self.parameterAsBoolean(parameters, self.DO_CONTOUR,     context)
        cnt_int     = self.parameterAsDouble (parameters, self.CONTOUR_INT,    context)
        cnt_smooth  = self.parameterAsDouble (parameters, self.CONTOUR_SMOOTH, context)
        cnt_fmt_idx = self.parameterAsEnum   (parameters, self.CONTOUR_FMT,    context)
        load_layers = self.parameterAsBoolean(parameters, self.LOAD_LAYERS,    context)

        method  = ["idw", "linear", "cubic", "nearest"][method_idx]
        cnt_fmt = ["shp", "gpkg"][cnt_fmt_idx]
        base    = Path(csv_path).stem
        os.makedirs(out_dir, exist_ok=True)

        # ── Step 1 : Load CSV ─────────────────────────────────────────────────
        feedback.pushInfo("\n[1/3]  Loading CSV …")
        feedback.setProgress(5)
        if feedback.isCanceled(): return {}
        df = load_points(csv_path, feedback)

        # ── Step 2 : Generate DEM ─────────────────────────────────────────────
        feedback.pushInfo("\n[2/3]  Generating DEM …")
        feedback.setProgress(20)
        if feedback.isCanceled(): return {}

        resolution = res_param if res_param > 0 else auto_resolution(df)
        feedback.pushInfo(f"  Auto-res  : {resolution} m" if res_param <= 0
                          else f"  Resolution: {resolution} m")

        dem_path = os.path.join(out_dir, f"{base}_DEM.tif")
        zg, geo_transform = generate_dem(
            df, resolution, method, epsg, dem_path, feedback)
        feedback.setProgress(65)

        # ── Step 3 : Generate Contours ────────────────────────────────────────
        cnt_path = None
        if do_contour:
            feedback.pushInfo("\n[3/3]  Generating Contours …")
            if feedback.isCanceled(): return {}
            ext      = "gpkg" if cnt_fmt == "gpkg" else "shp"
            cnt_path = os.path.join(out_dir,
                                    f"{base}_Contour_{cnt_int}m.{ext}")
            generate_contours(zg, geo_transform, epsg,
                              cnt_int, cnt_smooth,
                              cnt_path, cnt_fmt, feedback)
        else:
            feedback.pushInfo("\n[3/3]  Contour skipped.")

        feedback.setProgress(90)

        # ── Load layers into QGIS ─────────────────────────────────────────────
        if load_layers:
            feedback.pushInfo("\n  Loading layers into QGIS canvas …")

            rl = QgsRasterLayer(dem_path, f"{base} DEM")
            if rl.isValid():
                QgsProject.instance().addMapLayer(rl)
                feedback.pushInfo(f"  ✔ {base} DEM")
            else:
                feedback.pushWarning(f"  Could not load DEM: {dem_path}")

            if cnt_path and os.path.exists(cnt_path):
                vl = QgsVectorLayer(cnt_path,
                                    f"{base} Contour {cnt_int}m", "ogr")
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
                    feedback.pushInfo(f"  ✔ {base} Contour {cnt_int}m")
                else:
                    feedback.pushWarning(f"  Could not load contour: {cnt_path}")

        # ── Summary ───────────────────────────────────────────────────────────
        feedback.setProgress(100)
        feedback.pushInfo("\n" + "=" * 55)
        feedback.pushInfo("  ✔  Done!")
        feedback.pushInfo(f"  DEM     → {Path(dem_path).name}")
        if cnt_path:
            feedback.pushInfo(f"  Contour → {Path(cnt_path).name}")
        feedback.pushInfo(f"  Folder  → {out_dir}")
        feedback.pushInfo("=" * 55)

        result = {"DEM": dem_path}
        if cnt_path:
            result["Contour"] = cnt_path
        return result

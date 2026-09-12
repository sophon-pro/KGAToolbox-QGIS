# -*- coding: utf-8 -*-

import os
import re
import csv
import json
import base64
import hashlib
import io
from contextlib import suppress
import numpy as np
from osgeo import gdal

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination,
    QgsProcessingOutputFile,
    QgsProcessingOutputString,
    QgsProcessingOutputHtml,
    QgsProcessing,
    QgsProcessingException,
    QgsRasterLayer,
    QgsProject,
)
from ..branding import docs_url

gdal.UseExceptions()

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

SCRIPT_VERSION = "combined-2"
CATEGORY_NAME = "KGA Irrigation Tools"
CATEGORY_ID = "kgairrigationtools"

METHOD_OPTIONS = [
    "Auto - Compare All (Best CV)",
    "Global Offset",
    "Polynomial Trend (degree 1)",
    "Polynomial Trend (degree 2)",
    "IDW (distance-decay)",
]

MERGE_EXTENT_OPTIONS = [
    "Reference DEM extent",
    "Coarsest DEM extent (full)",
]

EXCEL_ROW_LIMIT = 1_048_576
SAFETY_MARGIN_ROWS = 1_000_000

HTML_STYLE = """
<style>
  body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 16px; color: #222; }
  h2 { color: #2F5496; border-bottom: 2px solid #2F5496; padding-bottom: 4px; margin-top: 28px; }
  h3 { color: #2F5496; margin-top: 20px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 18px; }
  th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: right; font-size: 13px; }
  th { background: #2F5496; color: white; text-align: center; }
  td:first-child, th:first-child { text-align: left; }
  tr:nth-child(even) { background: #f5f7fa; }
  .note { color: #666; font-size: 12px; font-style: italic; }
  img { display: block; margin: 10px 0 24px 0; max-width: 100%; }
</style>
"""


def html_table(headers, rows):
    out = ["<table>", "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"]
    for r in rows:
        cells = []
        for v in r:
            if isinstance(v, float):
                cells.append(f"<td>{v:.4f}</td>")
            else:
                cells.append(f"<td>{v}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def fig_to_html_img(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    plt.close(fig)
    buf.seek(0)
    data = base64.b64encode(buf.read()).decode("ascii")
    return f'<img src="data:image/png;base64,{data}"/>'


# ===========================================================================
# SHARED GDAL / NUMPY RASTER HELPERS  (no rasterio, no scipy)
# ===========================================================================

def get_geotransform(path):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    xsize, ysize = ds.RasterXSize, ds.RasterYSize
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    ds = None
    return gt, proj, xsize, ysize, nodata


def raster_extent(path):
    gt, proj, xsize, ysize, _ = get_geotransform(path)
    minx = gt[0]
    maxy = gt[3]
    maxx = minx + xsize * gt[1]
    miny = maxy + ysize * gt[5]
    return (minx, min(miny, maxy), maxx, max(miny, maxy))


def dem_resolution(path):
    ds = gdal.Open(path)
    res = abs(ds.GetGeoTransform()[1])
    ds = None
    return res


def sanitize_label(s):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")
    return s or "layer"


def pixel_xy(gt, rows, cols):
    xs = gt[0] + cols * gt[1] + rows * gt[2] + 0.5 * gt[1]
    ys = gt[3] + cols * gt[4] + rows * gt[5] + 0.5 * gt[5]
    return xs, ys


def resolve_dems_cfg(layers, overrides_str, feedback):
    overrides = None
    overrides_str = (overrides_str or "").strip()
    if overrides_str:
        try:
            overrides = [float(v.strip()) for v in overrides_str.split(",")]
        except ValueError:
            raise QgsProcessingException("NoData overrides must be numbers separated by commas.")
        if len(overrides) != len(layers):
            raise QgsProcessingException(
                f"Got {len(overrides)} NoData overrides but {len(layers)} DEM layers -- counts must match.")

    dems_cfg = []
    for i, lyr in enumerate(layers):
        path = lyr.dataProvider().dataSourceUri().split("|")[0]
        if overrides is not None:
            nodata = overrides[i]
        else:
            nodata = lyr.dataProvider().sourceNoDataValue(1)
            if nodata is None:
                feedback.pushWarning(
                    f"'{lyr.name()}' has no NoData set and no override was given -- "
                    f"no pixels will be masked as invalid for this layer.")
        dems_cfg.append({"path": path, "nodata": nodata})
    return dems_cfg


# ===========================================================================
# ALGORITHM 1 HELPERS -- correction + merge
# ===========================================================================

def read_window(path, nodata_override, bounds):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    minx, miny, maxx, maxy = bounds
    col0 = int(round((minx - gt[0]) / gt[1]))
    row0 = int(round((maxy - gt[3]) / gt[5]))
    col1 = int(round((maxx - gt[0]) / gt[1]))
    row1 = int(round((miny - gt[3]) / gt[5]))
    col0c, row0c = max(col0, 0), max(row0, 0)
    col1c = min(col1, ds.RasterXSize)
    row1c = min(row1, ds.RasterYSize)
    xsize, ysize = max(1, col1c - col0c), max(1, row1c - row0c)

    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray(col0c, row0c, xsize, ysize).astype(np.float32)
    win_gt = (gt[0] + col0c * gt[1], gt[1], gt[2], gt[3] + row0c * gt[5], gt[4], gt[5])
    nodata = nodata_override if nodata_override is not None else band.GetNoDataValue()
    proj = ds.GetProjection()
    ds = None

    mask = np.isclose(arr, nodata, atol=1e-3) if nodata is not None else np.zeros_like(arr, bool)
    return np.ma.masked_array(arr, mask=mask), win_gt, proj


def aggregate_reference_to_grid(ref_path, ref_nodata, dst_gt, dst_shape, dst_proj):
    height, width = dst_shape
    minx = dst_gt[0]
    maxy = dst_gt[3]
    maxx = minx + width * dst_gt[1]
    miny = maxy + height * dst_gt[5]
    opts = gdal.WarpOptions(
        format="MEM", outputBounds=(minx, min(miny, maxy), maxx, max(miny, maxy)),
        width=width, height=height, dstSRS=dst_proj,
        srcNodata=ref_nodata, dstNodata=np.nan, resampleAlg="average",
    )
    out_ds = gdal.Warp("", ref_path, options=opts)
    arr = out_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    out_ds = None
    return np.ma.masked_invalid(arr)


def build_calibration_points(diff, gt, stride):
    rows, cols = np.indices(diff.shape)
    rows, cols = rows[::stride, ::stride], cols[::stride, ::stride]
    vals = np.asarray(diff)[::stride, ::stride]
    mask = np.ma.getmaskarray(diff)[::stride, ::stride]
    valid = ~mask
    rr, cc, vv = rows[valid], cols[valid], vals[valid]
    if len(vv) == 0:
        return np.empty((0, 2)), np.empty((0,))
    xs, ys = pixel_xy(gt, rr, cc)
    return np.column_stack([xs, ys]), vv


class GlobalOffsetModel:
    name = "Global Offset"

    def fit(self, xy, values):
        self.offset = float(np.mean(values))

    def predict(self, xy):
        return np.full(len(xy), self.offset)


class PolyTrendModel:
    def __init__(self, degree=1):
        self.degree = degree
        self.name = f"Polynomial Trend (degree {degree})"

    def _features(self, xy):
        x, y = xy[:, 0] - self.x0, xy[:, 1] - self.y0
        if self.degree == 1:
            return np.column_stack([np.ones_like(x), x, y])
        return np.column_stack([np.ones_like(x), x, y, x**2, y**2, x*y])

    def fit(self, xy, values):
        self.x0, self.y0 = xy[:, 0].mean(), xy[:, 1].mean()
        A = self._features(xy)
        self.coeffs, *_ = np.linalg.lstsq(A, values, rcond=None)

    def predict(self, xy):
        return self._features(xy) @ self.coeffs


class IDWModel:
    def __init__(self, power=2.0, k=12, full_strength_dist=3000.0, chunk=500,
                 max_calibration_points=15000):
        self.power, self.k, self.fade = power, k, full_strength_dist
        self.chunk = chunk
        self.max_calibration_points = max_calibration_points
        self.name = f"IDW (k={k}, fade@{int(full_strength_dist)}m)"

    def fit(self, xy, values):
        if len(values) > self.max_calibration_points:
            rng = np.random.default_rng(42)
            idx = rng.choice(len(values), size=self.max_calibration_points, replace=False)
            xy, values = xy[idx], values[idx]
        self.cal_xy = xy.astype(np.float32)
        self.cal_vals = values.astype(np.float32)
        self.k_eff = min(self.k, len(values))

    def predict(self, query_xy):
        n = len(query_xy)
        query_xy = np.asarray(query_xy, dtype=np.float32)
        out = np.empty(n, dtype=np.float32)
        for i in range(0, n, self.chunk):
            q = query_xy[i:i + self.chunk]
            d = np.sqrt(((q[:, None, :] - self.cal_xy[None, :, :]) ** 2).sum(axis=2))
            idx = np.argpartition(d, self.k_eff - 1, axis=1)[:, :self.k_eff]
            dsel = np.take_along_axis(d, idx, axis=1)
            vsel = self.cal_vals[idx]
            dsafe = np.where(dsel == 0, 1e-6, dsel)
            w = 1.0 / (dsafe ** self.power)
            w /= w.sum(axis=1, keepdims=True)
            idw_val = (w * vsel).sum(axis=1)
            nearest = dsel.min(axis=1)
            fade = np.clip(1.0 - (nearest - self.fade) / self.fade, 0.0, 1.0)
            out[i:i + self.chunk] = idw_val * fade
        return out


def cross_validate(factory, xy, values, folds=5, seed=42, max_test_points_per_fold=4000):
    n = len(values)
    idx = np.arange(n)
    rng = np.random.default_rng(seed)
    rng.shuffle(idx)
    fold_size = max(1, n // folds)
    errs = []
    for f in range(folds):
        test_idx = idx[f * fold_size:(f + 1) * fold_size]
        train_idx = np.setdiff1d(idx, test_idx)
        if len(test_idx) == 0 or len(train_idx) < 5:
            continue
        if len(test_idx) > max_test_points_per_fold:
            test_idx = rng.choice(test_idx, size=max_test_points_per_fold, replace=False)
        m = factory()
        m.fit(xy[train_idx], values[train_idx])
        pred = m.predict(xy[test_idx])
        errs.append(pred - values[test_idx])
    if not errs:
        return float("inf")
    e = np.concatenate(errs)
    return float(np.sqrt(np.mean(e ** 2)))


def make_factory(method, p):
    return {
        "Global Offset": lambda: GlobalOffsetModel(),
        "Polynomial Trend (degree 1)": lambda: PolyTrendModel(1),
        "Polynomial Trend (degree 2)": lambda: PolyTrendModel(2),
        "IDW (distance-decay)": lambda: IDWModel(p["idw_power"], p["idw_k"], p["idw_fade"]),
    }[method]


def select_or_fit_model(method, xy, values, p, log):
    """Returns (model, cv_rmse_of_selected_method)."""
    if method == "Auto - Compare All (Best CV)":
        log("  Comparing methods via cross-validation:")
        candidates = ["Global Offset", "Polynomial Trend (degree 1)",
                      "Polynomial Trend (degree 2)", "IDW (distance-decay)"]
        scored = []
        for c in candidates:
            f = make_factory(c, p)
            rmse = cross_validate(f, xy, values, folds=p["cv_folds"])
            scored.append((c, rmse, f))
            log(f"    {c:32s} CV-RMSE = {rmse:.4f} m")
        scored.sort(key=lambda r: r[1])
        best_name, best_rmse, best_factory = scored[0]
        log(f"  -> Selected: {best_name} (CV-RMSE = {best_rmse:.4f} m)")
        model = best_factory()
        model.fit(xy, values)
        return model, best_rmse
    else:
        f = make_factory(method, p)
        rmse = cross_validate(f, xy, values, folds=p["cv_folds"])
        log(f"  {method} CV-RMSE = {rmse:.4f} m")
        model = f()
        model.fit(xy, values)
        return model, rmse


# ===========================================================================
# ONE CORRECTION STAGE + BLOCKWISE APPLY
# ===========================================================================

def apply_correction_blockwise(target_path, target_nodata, model, out_path,
                                block_rows=800, log=print, clip_range=None,
                                resume_row=0, progress_cb=None, cancel_check=None):
    ds = gdal.Open(target_path)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    width, height = ds.RasterXSize, ds.RasterYSize
    band = ds.GetRasterBand(1)

    if resume_row > 0 and os.path.exists(out_path):
        out_ds = gdal.Open(out_path, gdal.GA_Update)
        out_band = out_ds.GetRasterBand(1)
        log(f"  Resuming from row {resume_row} of {height} (partial output found)")
    else:
        driver = gdal.GetDriverByName("GTiff")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out_ds = driver.Create(out_path, width, height, 1, gdal.GDT_Float32, options=["COMPRESS=LZW"])
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_band = out_ds.GetRasterBand(1)
        out_band.SetNoDataValue(target_nodata)
        resume_row = 0

    for row0 in range(resume_row, height, block_rows):
        if cancel_check and cancel_check():
            out_band.FlushCache()
            ds = None
            out_ds = None
            raise QgsProcessingException("Canceled by user")

        row1 = min(row0 + block_rows, height)
        block = band.ReadAsArray(0, row0, width, row1 - row0).astype(np.float32)
        mask = np.isclose(block, target_nodata, atol=1e-3) | np.isnan(block)

        rows, cols = np.indices(block.shape)
        block_gt = (gt[0], gt[1], gt[2], gt[3] + row0 * gt[5], gt[4], gt[5])
        xs, ys = pixel_xy(block_gt, rows.ravel(), cols.ravel())
        query_xy = np.column_stack([xs, ys])

        correction = model.predict(query_xy).reshape(block.shape)
        if clip_range is not None:
            correction = np.clip(correction, clip_range[0], clip_range[1])
        corrected = np.where(mask, target_nodata, block + correction).astype(np.float32)
        out_band.WriteArray(corrected, 0, row0)
        out_band.FlushCache()
        log(f"    ...rows {row0}-{row1} of {height}")
        if progress_cb:
            progress_cb(row1)

    out_band.FlushCache()
    ds = None
    out_ds = None
    return out_path


def stage_fingerprint(ref_path, ref_nodata, target_path, target_nodata, method, p):
    key = {
        "ref_path": ref_path, "ref_nodata": ref_nodata,
        "target_path": target_path, "target_nodata": target_nodata,
        "method": method, "idw_power": p["idw_power"], "idw_k": p["idw_k"],
        "idw_fade": p["idw_fade"], "cv_folds": p["cv_folds"], "stride": p["stride"],
        "script_version": SCRIPT_VERSION,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


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


def run_stage(label, ref_path, ref_nodata, ref_extent, target_path, target_nodata,
              method, p, out_dir, log, checkpoint, stage_key, force=False, cancel_check=None):
    fp = stage_fingerprint(ref_path, ref_nodata, target_path, target_nodata, method, p)
    out_path = os.path.join(out_dir, f"corrected_{sanitize_label(label)}.tif")

    cached = checkpoint.get(stage_key)
    resume_row = 0
    if not force and cached and cached.get("fingerprint") == fp:
        if cached.get("status") == "complete" and os.path.exists(cached.get("output_path", "")):
            log(f"{label} -- SKIPPED (already complete): {cached['output_path']}")
            return {"output_path": cached["output_path"], "extent": raster_extent(cached["output_path"]),
                    "nodata": target_nodata, "method_used": cached.get("method_used"),
                    "fingerprint": fp, "cv_rmse": cached.get("cv_rmse"),
                    "raw_diff_mean": cached.get("raw_diff_mean"), "raw_diff_std": cached.get("raw_diff_std"),
                    "overlap_pixels": cached.get("overlap_pixels"),
                    "calibration_points": cached.get("calibration_points"),
                    "calib_diff_sample": np.array(cached.get("calib_diff_sample", []))}
        elif cached.get("status") == "in_progress" and os.path.exists(cached.get("output_path", "")):
            resume_row = cached.get("completed_rows", 0)

    log(f"--- {label} ---")
    if resume_row > 0:
        log(f"Partial output found -- resuming from row {resume_row}.")

    target_win, t_gt, t_proj = read_window(target_path, target_nodata, ref_extent)
    ref_agg = aggregate_reference_to_grid(ref_path, ref_nodata, t_gt, target_win.shape, t_proj)

    combined_mask = np.ma.getmaskarray(target_win) | np.ma.getmaskarray(ref_agg)
    target_m = np.ma.masked_array(np.ma.filled(target_win, np.nan), mask=combined_mask)
    ref_m = np.ma.masked_array(np.ma.filled(ref_agg, np.nan), mask=combined_mask)
    diff = ref_m - target_m
    dv = diff.compressed()
    if dv.size == 0:
        raise QgsProcessingException(f"{label}: no overlapping valid pixels.")
    log(f"Overlap: {dv.size} valid pixels | mean diff={dv.mean():.3f} m, std={dv.std():.3f} m")

    xy, values = build_calibration_points(diff, t_gt, p["stride"])
    log(f"Calibration points: {len(values)}")

    model, cv_rmse = select_or_fit_model(method, xy, values, p, log)

    diff_min, diff_max = float(dv.min()), float(dv.max())
    margin = 0.25 * (diff_max - diff_min + 1e-6)
    clip_range = (diff_min - margin, diff_max + margin)
    log(f"Correction capped to [{clip_range[0]:.3f}, {clip_range[1]:.3f}] m")

    # Small subsample kept only for the Visualization tab's histograms --
    # capped so a huge raster doesn't bloat memory/checkpoint size.
    calib_sample = values
    if len(calib_sample) > 20000:
        rng = np.random.default_rng(0)
        calib_sample = rng.choice(calib_sample, size=20000, replace=False)

    def progress_cb(completed_rows):
        checkpoint[stage_key] = {"fingerprint": fp, "output_path": out_path,
                                  "method_used": model.name, "status": "in_progress",
                                  "completed_rows": completed_rows}
        save_checkpoint(out_dir, checkpoint)

    log(f"Applying correction -> {out_path}")
    apply_correction_blockwise(target_path, target_nodata, model, out_path, log=log,
                                clip_range=clip_range, resume_row=resume_row,
                                progress_cb=progress_cb, cancel_check=cancel_check)

    checkpoint[stage_key] = {
        "fingerprint": fp, "output_path": out_path, "method_used": model.name, "status": "complete",
        "cv_rmse": cv_rmse, "raw_diff_mean": float(dv.mean()), "raw_diff_std": float(dv.std()),
        "overlap_pixels": int(dv.size), "calibration_points": int(len(values)),
        "calib_diff_sample": calib_sample.tolist(),
    }
    save_checkpoint(out_dir, checkpoint)

    return {"output_path": out_path, "extent": raster_extent(target_path),
            "nodata": target_nodata, "method_used": model.name, "fingerprint": fp,
            "cv_rmse": cv_rmse, "raw_diff_mean": float(dv.mean()), "raw_diff_std": float(dv.std()),
            "overlap_pixels": int(dv.size), "calibration_points": int(len(values)),
            "calib_diff_sample": calib_sample}


def run_correction_chain(sorted_dems, method, p, out_dir, log, checkpoint, force=False, cancel_check=None):
    stages = []
    prev_path = sorted_dems[0]["path"]
    prev_nodata = sorted_dems[0]["nodata"]
    prev_extent = raster_extent(prev_path)

    for i in range(1, len(sorted_dems)):
        target = sorted_dems[i]
        stage_key = f"stage{i}"
        label = f"Stage {i}/{len(sorted_dems) - 1}: {target['label']}"
        log(f"Correcting DEM {i + 1} of {len(sorted_dems)} "
            f"({target['label']}) against {os.path.basename(prev_path)}")
        result = run_stage(label, prev_path, prev_nodata, prev_extent,
                            target["path"], target["nodata"], method, p, out_dir,
                            log, checkpoint, stage_key, force=force, cancel_check=cancel_check)
        result["target_label"] = target["label"]
        stages.append(result)
        prev_path = result["output_path"]
        prev_nodata = target["nodata"]
        prev_extent = result["extent"]

    return stages


def merge_fingerprint(sources, bounds, resolution):
    key = {"sources": sources, "bounds": bounds, "resolution": resolution,
           "script_version": SCRIPT_VERSION}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def merge_layers(sources, bounds, resolution, out_path, block_rows=800, log=print,
                  resume_row=0, progress_cb=None, cancel_check=None):
    minx, miny, maxx, maxy = bounds
    width = max(1, int(round((maxx - minx) / resolution)))
    height = max(1, int(round((maxy - miny) / resolution)))
    out_gt = (minx, resolution, 0.0, maxy, 0.0, -resolution)

    with_ds = gdal.Open(sources[0][0])
    proj = with_ds.GetProjection()
    with_ds = None

    log(f"Merge grid: {width} x {height} at {resolution} m ({len(sources)} sources, finest wins)")

    if resume_row > 0 and os.path.exists(out_path):
        out_ds = gdal.Open(out_path, gdal.GA_Update)
        out_band = out_ds.GetRasterBand(1)
        log(f"  Resuming merge from row {resume_row} of {height}")
    else:
        driver = gdal.GetDriverByName("GTiff")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out_ds = driver.Create(out_path, width, height, 1, gdal.GDT_Float32, options=["COMPRESS=LZW"])
        out_ds.SetGeoTransform(out_gt)
        out_ds.SetProjection(proj)
        out_band = out_ds.GetRasterBand(1)
        out_band.SetNoDataValue(-9999.0)
        resume_row = 0

    for row0 in range(resume_row, height, block_rows):
        if cancel_check and cancel_check():
            out_band.FlushCache()
            out_ds = None
            raise QgsProcessingException("Canceled by user")

        row1 = min(row0 + block_rows, height)
        b_h = row1 - row0
        block_maxy = maxy + row0 * out_gt[5]
        block_miny = maxy + row1 * out_gt[5]
        merged = np.full((b_h, width), -9999.0, dtype=np.float32)
        filled = np.zeros((b_h, width), dtype=bool)

        for path, nodata in sources:
            src_res = abs(gdal.Open(path).GetGeoTransform()[1])
            resample_alg = "average" if src_res < resolution else "bilinear"
            opts = gdal.WarpOptions(
                format="MEM", outputBounds=(minx, block_miny, maxx, block_maxy),
                width=width, height=b_h, dstSRS=proj,
                srcNodata=nodata, dstNodata=np.nan, resampleAlg=resample_alg,
            )
            src_ds = gdal.Warp("", path, options=opts)
            buf = src_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
            src_ds = None
            take = (~filled) & np.isfinite(buf)
            merged[take] = buf[take]
            filled |= np.isfinite(buf)

        out_band.WriteArray(merged, 0, row0)
        out_band.FlushCache()
        log(f"    ...merge rows {row0}-{row1} of {height}")
        if progress_cb:
            progress_cb(row1)

    out_band.FlushCache()
    out_ds = None
    return out_path


def run_merge(sources, bounds, resolution, out_path, out_dir, checkpoint, log, force=False, cancel_check=None):
    fp = merge_fingerprint(sources, bounds, resolution)
    cached = checkpoint.get("merge")
    resume_row = 0
    if not force and cached and cached.get("fingerprint") == fp:
        if cached.get("status") == "complete" and os.path.exists(cached.get("output_path", "")):
            log(f"MERGE -- SKIPPED (already complete): {out_path}")
            return out_path, fp
        elif cached.get("status") == "in_progress" and os.path.exists(cached.get("output_path", "")):
            resume_row = cached.get("completed_rows", 0)

    log("--- MERGE ---")

    def progress_cb(completed_rows):
        checkpoint["merge"] = {"fingerprint": fp, "output_path": out_path,
                                "status": "in_progress", "completed_rows": completed_rows}
        save_checkpoint(out_dir, checkpoint)

    merge_layers(sources, bounds, resolution, out_path, log=log,
                 resume_row=resume_row, progress_cb=progress_cb, cancel_check=cancel_check)

    checkpoint["merge"] = {"fingerprint": fp, "output_path": out_path, "status": "complete"}
    save_checkpoint(out_dir, checkpoint)
    return out_path, fp


# ===========================================================================
# POINT-SAMPLING HELPERS (shared by the report + the Excel export tool)
# ===========================================================================

def read_full(path, nodata_override):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    nodata = nodata_override if nodata_override is not None else band.GetNoDataValue()
    ds = None
    mask = np.isclose(arr, nodata, atol=1e-3) if nodata is not None else np.zeros_like(arr, bool)
    return np.ma.masked_array(arr, mask=mask), gt, proj


def aggregate_to_grid(src_path, src_nodata, dst_gt, dst_shape, dst_proj):
    height, width = dst_shape
    minx = dst_gt[0]
    maxy = dst_gt[3]
    maxx = minx + width * dst_gt[1]
    miny = maxy + height * dst_gt[5]
    opts = gdal.WarpOptions(
        format="MEM", outputBounds=(minx, min(miny, maxy), maxx, max(miny, maxy)),
        width=width, height=height, dstSRS=dst_proj,
        srcNodata=src_nodata, dstNodata=np.nan, resampleAlg="average",
    )
    out_ds = gdal.Warp("", src_path, options=opts)
    arr = out_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    out_ds = None
    return np.ma.masked_invalid(arr)


def col_name(label):
    base = os.path.splitext(label)[0]
    safe = "".join(c if c.isalnum() else "_" for c in base).strip("_")
    return (safe or "DEM") + "_Elev"


def build_sample_table(sorted_dems, stride, max_rows, log):
    ref = sorted_dems[0]
    log(f"Reading finest DEM ({ref['label']}) as the sampling grid, "
        f"aggregating {len(sorted_dems) - 1} other DEM(s) onto it...")
    ref_arr, gt, proj = read_full(ref["path"], ref["nodata"])

    other_arrs = []
    for d in sorted_dems[1:]:
        other_arrs.append(aggregate_to_grid(d["path"], d["nodata"], gt, ref_arr.shape, proj))

    height, width = ref_arr.shape
    total_at_stride = (height // stride) * (width // stride)
    log(f"Grid size: {width} x {height}. At stride={stride}, estimated sample points: {total_at_stride:,}")

    while total_at_stride > max_rows and stride < max(height, width):
        stride += 1
        total_at_stride = (height // stride) * (width // stride)
    if stride > 1:
        log(f"Using stride={stride} to stay under the {max_rows:,}-row limit ({total_at_stride:,} points).")

    rows_idx, cols_idx = np.mgrid[0:height:stride, 0:width:stride]
    rows_idx, cols_idx = rows_idx.ravel(), cols_idx.ravel()

    ref_valid_full = ~np.ma.getmaskarray(ref_arr)
    ref_vals_full = np.ma.filled(ref_arr.astype(np.float32), np.nan)
    ref_valid = ref_valid_full[rows_idx, cols_idx]
    ref_vals = ref_vals_full[rows_idx, cols_idx]

    other_valid, other_vals = [], []
    for arr in other_arrs:
        valid_full = ~np.ma.getmaskarray(arr)
        vals_full = np.ma.filled(arr, np.nan)
        other_valid.append(valid_full[rows_idx, cols_idx])
        other_vals.append(vals_full[rows_idx, cols_idx])

    keep = ref_valid.copy()
    for v in other_valid:
        keep |= v
    rows_idx, cols_idx = rows_idx[keep], cols_idx[keep]
    ref_valid, ref_vals = ref_valid[keep], ref_vals[keep]
    other_valid = [v[keep] for v in other_valid]
    other_vals = [v[keep] for v in other_vals]

    xs, ys = pixel_xy(gt, rows_idx, cols_idx)
    log(f"Final sample count: {len(xs):,} rows")

    labels = [d["label"] for d in sorted_dems]
    cols = {label: v for label, v in zip(labels[1:], other_vals)}
    valids = {label: v for label, v in zip(labels[1:], other_valid)}

    table = {"X": xs, "Y": ys}
    table[col_name(ref["label"])] = ref_vals
    for label in labels[1:]:
        table[col_name(label)] = cols[label]

    valid_matrix = [ref_valid] + [valids[label] for label in labels[1:]]
    zone = np.full(len(xs), "", dtype=object)
    for i in range(len(xs)):
        present = [labels[j] for j in range(len(labels)) if valid_matrix[j][i]]
        zone[i] = "+".join(present) if present else "(none)"
    table["Zone"] = zone

    diff_cols = []
    ref_name = col_name(ref["label"])
    prev_name, prev_valid, prev_vals = ref_name, ref_valid, ref_vals
    for j, label in enumerate(labels[1:]):
        name = col_name(label)
        v_valid, v_vals = other_valid[j], other_vals[j]
        diff_ref_name = f"Diff_{name.replace('_Elev', '')}_minus_{ref_name.replace('_Elev', '')}"
        table[diff_ref_name] = np.where(ref_valid & v_valid, v_vals - ref_vals, np.nan)
        diff_cols.append(diff_ref_name)
        if name != prev_name:
            diff_prev_name = f"Diff_{name.replace('_Elev', '')}_minus_{prev_name.replace('_Elev', '')}"
            if diff_prev_name not in table:
                table[diff_prev_name] = np.where(prev_valid & v_valid, v_vals - prev_vals, np.nan)
                diff_cols.append(diff_prev_name)
        prev_name, prev_valid, prev_vals = name, v_valid, v_vals

    columns = (["X", "Y", ref_name]
               + [col_name(label) for label in labels[1:]]
               + ["Zone"] + diff_cols)
    return table, columns, stride


def stats_from_values(d):
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"n": 0}
    return {
        "n": int(d.size), "mean": float(np.mean(d)), "median": float(np.median(d)),
        "std": float(np.std(d)), "rmse": float(np.sqrt(np.mean(d ** 2))),
        "min": float(np.min(d)), "max": float(np.max(d)),
    }


def build_summary(table, diff_cols):
    rows = []
    for c in diff_cols:
        rows.append({"column": c, **stats_from_values(table[c])})
    zone_vals, zone_counts = np.unique(table["Zone"], return_counts=True)
    for z, n in zip(zone_vals, zone_counts):
        rows.append({"column": f"(count) Zone = {z}", "n": int(n)})
    rows.append({"column": "(count) Total rows", "n": len(table["Zone"])})
    return rows


def write_excel(table, columns, diff_cols, out_path, log):
    n = len(table["X"])
    summary_rows = build_summary(table, diff_cols)

    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
        from openpyxl.styles import Font, PatternFill
        from openpyxl.formatting.rule import ColorScaleRule

        wb = openpyxl.Workbook()

        ws_sum = wb.active
        ws_sum.title = "Summary"
        header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")
        sum_cols = ["column", "n", "mean", "median", "std", "rmse", "min", "max"]
        ws_sum.append([c.upper() for c in sum_cols])
        for cell in ws_sum[1]:
            cell.fill = header_fill
            cell.font = header_font
        for r in summary_rows:
            ws_sum.append([r.get(c, "") for c in sum_cols])
        for idx, c in enumerate(sum_cols, start=1):
            ws_sum.column_dimensions[get_column_letter(idx)].width = 26 if c == "column" else 12
        ws_sum.freeze_panes = "A2"

        ws = wb.create_sheet("DEM Samples")
        ws.append(columns)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
        for i in range(n):
            row = []
            for c in columns:
                v = table[c][i]
                if isinstance(v, (np.floating, float)) and np.isnan(v):
                    row.append(None)
                elif isinstance(v, np.generic):
                    row.append(v.item())
                else:
                    row.append(v)
            ws.append(row)
        for idx, c in enumerate(columns, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = 16
        ws.freeze_panes = "A2"

        for c in diff_cols:
            col_letter = get_column_letter(columns.index(c) + 1)
            rule = ColorScaleRule(
                start_type="min", start_color="F8696B",
                mid_type="num", mid_value=0, mid_color="FFFFFF",
                end_type="max", end_color="5A8AC6",
            )
            ws.conditional_formatting.add(f"{col_letter}2:{col_letter}{n + 1}", rule)

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        wb.save(out_path)
        log(f"Excel file written: {out_path}")
        return out_path
    except ImportError:
        log("openpyxl not available in this QGIS Python -- writing CSV files instead.")
        base = os.path.splitext(out_path)[0]
        csv_path = base + ".csv"
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for i in range(n):
                writer.writerow([table[c][i] for c in columns])
        log(f"CSV file written: {csv_path}")

        summary_path = base + "_summary.csv"
        with open(summary_path, "w", newline="") as f:
            writer = csv.writer(f)
            sum_cols = ["column", "n", "mean", "median", "std", "rmse", "min", "max"]
            writer.writerow(sum_cols)
            for r in summary_rows:
                writer.writerow([r.get(c, "") for c in sum_cols])
        log(f"Summary CSV written: {summary_path}")
        return csv_path


# ===========================================================================
# REPORT BUILDERS (Statistics tab / Visualization tab)
# ===========================================================================

def build_statistics_html(sorted_dems, stages, summary_rows):
    parts = [HTML_STYLE, "<h2>DEM Elevation Correction & Point Sample -- Statistics</h2>"]

    parts.append("<h3>Reference DEM &amp; input DEMs (finest &rarr; coarsest)</h3>")
    rows = [["Reference", sorted_dems[0]["label"], f'{dem_resolution(sorted_dems[0]["path"]):.4g}']]
    rows += [[i, d["label"], f'{dem_resolution(d["path"]):.4g}']
             for i, d in enumerate(sorted_dems[1:], start=1)]
    parts.append(html_table(["#", "DEM", "Resolution"], rows))

    parts.append("<h3>Correction stages</h3>")
    rows = []
    for i, s in enumerate(stages, start=1):
        rows.append([
            f"Stage {i}: {s.get('target_label', '')}", s.get("method_used", ""),
            s.get("cv_rmse"), s.get("overlap_pixels"), s.get("calibration_points"),
            s.get("raw_diff_mean"), s.get("raw_diff_std"),
        ])
    parts.append(html_table(
        ["Stage", "Method used", "CV-RMSE (m)", "Overlap px", "Calib. points",
         "Raw diff mean (m)", "Raw diff std (m)"], rows))
    parts.append('<p class="note">"Raw diff" is reference-minus-target before correction, '
                 'over the overlap area used to fit the model.</p>')

    if summary_rows:
        parts.append("<h3>Post-correction point-sample differences</h3>")
        rows = []
        for r in summary_rows:
            rows.append([r.get("column", ""), r.get("n", ""), r.get("mean", ""), r.get("median", ""),
                         r.get("std", ""), r.get("rmse", ""), r.get("min", ""), r.get("max", "")])
        parts.append(html_table(["Column", "N", "Mean", "Median", "Std", "RMSE", "Min", "Max"], rows))
        parts.append('<p class="note">Sampled after correction across the finest DEM + every corrected '
                     'output, at the stride set in "Report sample stride".</p>')

    return "\n".join(parts)


def build_visualization_html(sorted_dems, stages, table, diff_cols):
    parts = [HTML_STYLE, "<h2>DEM Elevation Correction & Point Sample -- Visualization</h2>"]

    if not _HAS_MPL:
        parts.append('<p class="note">matplotlib is not available in this QGIS Python environment, '
                     "so charts couldn't be generated. The Statistics tab still has the full numbers.</p>")
        return "\n".join(parts)

    # --- Chart 1: CV-RMSE by stage ---
    if stages:
        labels = [f"Stage {i}\n{s.get('target_label', '')}" for i, s in enumerate(stages, start=1)]
        rmses = [s.get("cv_rmse") or 0 for s in stages]
        fig, ax = plt.subplots(figsize=(min(9, 2 + 1.4 * len(stages)), 4))
        ax.bar(labels, rmses, color="#2F5496")
        ax.set_ylabel("CV-RMSE (m)")
        ax.set_title("Cross-validation RMSE by correction stage")
        for i, v in enumerate(rmses):
            ax.text(i, v, f"{v:.3f}", ha="center", va="bottom", fontsize=9)
        parts.append("<h3>Correction quality by stage</h3>")
        parts.append(fig_to_html_img(fig))

    # --- Chart 2: histograms of calibration differences per stage ---
    stages_with_data = [s for s in stages if s.get("calib_diff_sample") is not None
                         and len(s["calib_diff_sample"]) > 0]
    if stages_with_data:
        ncols = min(3, len(stages_with_data))
        nrows = int(np.ceil(len(stages_with_data) / ncols))
        fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.4 * nrows), squeeze=False)
        for idx, s in enumerate(stages_with_data):
            ax = axes[idx // ncols][idx % ncols]
            vals = np.asarray(s["calib_diff_sample"])
            ax.hist(vals, bins=40, color="#5A8AC6", edgecolor="white")
            ax.axvline(0, color="#F8696B", linewidth=1)
            ax.set_title(f"Stage {idx + 1}: {s.get('target_label', '')}", fontsize=10)
            ax.set_xlabel("Reference - target (m), before correction")
        for idx in range(len(stages_with_data), nrows * ncols):
            axes[idx // ncols][idx % ncols].axis("off")
        fig.tight_layout()
        parts.append("<h3>Calibration difference distributions (before correction)</h3>")
        parts.append(fig_to_html_img(fig))

    # --- Chart 3: boxplot of post-correction point-sample differences ---
    if diff_cols:
        data = [np.asarray(table[c])[np.isfinite(table[c])] for c in diff_cols]
        data = [d for d in data if d.size > 0]
        labels = [c.replace("Diff_", "").replace("_minus_", "\nvs ") for c, d in zip(diff_cols, data) if d.size > 0]
        if data:
            fig, ax = plt.subplots(figsize=(min(10, 2 + 1.4 * len(data)), 4.5))
            ax.boxplot(data, labels=labels, showfliers=False)
            ax.axhline(0, color="#F8696B", linewidth=1)
            ax.set_ylabel("Difference (m)")
            ax.set_title("Post-correction point-sample differences")
            fig.tight_layout()
            parts.append("<h3>Post-correction differences (point samples)</h3>")
            parts.append(fig_to_html_img(fig))

    return "\n".join(parts)


# ===========================================================================
# ALGORITHM 1: DEM Elevation Correction and Point Sample Report
# ===========================================================================

class DemCorrectionReportAlgorithm(QgsProcessingAlgorithm):
    REFERENCE_DEM = "REFERENCE_DEM"
    REFERENCE_NODATA = "REFERENCE_NODATA"
    INPUT_DEMS = "INPUT_DEMS"
    NODATA_OVERRIDES = "NODATA_OVERRIDES"
    METHOD = "METHOD"
    IDW_POWER = "IDW_POWER"
    IDW_K = "IDW_K"
    IDW_FADE = "IDW_FADE"
    CV_FOLDS = "CV_FOLDS"
    STRIDE = "STRIDE"
    DO_MERGE = "DO_MERGE"
    MERGE_EXTENT = "MERGE_EXTENT"
    MERGE_RESOLUTION = "MERGE_RESOLUTION"
    REPORT_STRIDE = "REPORT_STRIDE"
    REPORT_MAX_POINTS = "REPORT_MAX_POINTS"
    FORCE_RERUN = "FORCE_RERUN"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    OUTPUT_MERGED = "OUTPUT_MERGED"
    OUTPUT_SUMMARY = "OUTPUT_SUMMARY"
    OUTPUT_STATS = "OUTPUT_STATS"
    OUTPUT_VIZ = "OUTPUT_VIZ"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return DemCorrectionReportAlgorithm()

    def name(self):
        return "demelevationcorrectionandpointsamplereport"

    def displayName(self):
        return self.tr("DEM Elevation Correction and Point Sample Report")

    def group(self):
        return self.tr(CATEGORY_NAME)

    def groupId(self):
        return CATEGORY_ID

    def helpUrl(self):
        return docs_url('demelevationcorrectionandpointsamplereport')

    def shortHelpString(self):
        return self.tr(
            "Corrects one or more DEMs against a Reference DEM you choose (e.g. your "
            "LiDAR or other trusted survey), optionally merges them all into one "
            "seamless raster, and builds a Statistics tab and a Visualization tab "
            "(see the Processing Results panel after running) summarizing correction "
            "quality and post-correction agreement between the DEMs.\n\n"
            "The Reference DEM is never modified -- it's the elevation truth every "
            "other DEM is corrected toward. The other DEMs are auto-sorted from "
            "finest to coarsest resolution and chained: the finest of them is "
            "corrected directly against the Reference DEM, the next-finest is "
            "corrected against that (already-corrected) result, and so on.\n\n"
            "NoData overrides (optional): comma-separated values, in the same order "
            "as the DEMs you select. Leave blank to use each raster's own NoData.\n\n"
            "Resumable via a checkpoint.json written to the output folder."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.REFERENCE_DEM,
            self.tr("Reference DEM (the elevation truth -- never modified)")))
        self.addParameter(QgsProcessingParameterString(
            self.REFERENCE_NODATA,
            self.tr("Reference DEM NoData override (blank = use its own NoData)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_DEMS, self.tr("DEMs to correct against the reference (1 or more)"),
            layerType=QgsProcessing.SourceType.TypeRaster))
        self.addParameter(QgsProcessingParameterString(
            self.NODATA_OVERRIDES,
            self.tr("NoData overrides (comma-separated, matching DEM order; blank = use each layer's own NoData)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, self.tr("Correction method (all stages)"),
            options=METHOD_OPTIONS, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.IDW_POWER, self.tr("IDW power"),
            type=QgsProcessingParameterNumber.Type.Double, defaultValue=2.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.IDW_K, self.tr("IDW neighbors (k)"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=12))
        self.addParameter(QgsProcessingParameterNumber(
            self.IDW_FADE, self.tr("IDW full-strength distance (m)"),
            type=QgsProcessingParameterNumber.Type.Double, defaultValue=3000.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.CV_FOLDS, self.tr("Cross-validation folds"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=5))
        self.addParameter(QgsProcessingParameterNumber(
            self.STRIDE, self.tr("Calibration subsample stride"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=4))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DO_MERGE, self.tr("Merge finest DEM + all corrected DEMs"), defaultValue=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.MERGE_EXTENT, self.tr("Merge extent"),
            options=MERGE_EXTENT_OPTIONS, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MERGE_RESOLUTION, self.tr("Merge resolution (m)"),
            type=QgsProcessingParameterNumber.Type.Double, defaultValue=2.5))
        self.addParameter(QgsProcessingParameterNumber(
            self.REPORT_STRIDE, self.tr("Report sample stride (for Statistics/Visualization tabs)"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=20, minValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            self.REPORT_MAX_POINTS, self.tr("Report max sample points"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=200000, minValue=100))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FORCE_RERUN, self.tr("Force re-run everything (ignore checkpoint.json)"),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Output folder")))
        self.addOutput(QgsProcessingOutputFile(
            self.OUTPUT_MERGED, self.tr("Merged DEM (if merge was enabled)")))
        self.addOutput(QgsProcessingOutputString(
            self.OUTPUT_SUMMARY, self.tr("Run summary")))
        self.addOutput(QgsProcessingOutputHtml(
            self.OUTPUT_STATS, self.tr("Statistics")))
        self.addOutput(QgsProcessingOutputHtml(
            self.OUTPUT_VIZ, self.tr("Visualization")))

    def processAlgorithm(self, parameters, context, feedback):
        ref_layer = self.parameterAsRasterLayer(parameters, self.REFERENCE_DEM, context)
        if ref_layer is None:
            raise QgsProcessingException(self.tr("Select a Reference DEM."))
        ref_path = ref_layer.dataProvider().dataSourceUri().split("|")[0]
        ref_nodata_override = self.parameterAsString(parameters, self.REFERENCE_NODATA, context).strip()
        if ref_nodata_override:
            try:
                ref_nodata = float(ref_nodata_override)
            except ValueError:
                raise QgsProcessingException(self.tr("Reference DEM NoData override must be a number."))
        else:
            ref_nodata = ref_layer.dataProvider().sourceNoDataValue(1)
            if ref_nodata is None:
                feedback.pushWarning(
                    f"Reference DEM '{ref_layer.name()}' has no NoData set and no override was given -- "
                    f"no pixels will be masked as invalid for it.")

        layers = self.parameterAsLayerList(parameters, self.INPUT_DEMS, context)
        if len(layers) < 1:
            raise QgsProcessingException(self.tr("Select at least 1 DEM to correct against the reference."))

        overrides_str = self.parameterAsString(parameters, self.NODATA_OVERRIDES, context)
        dems_cfg = resolve_dems_cfg(layers, overrides_str, feedback)

        method = METHOD_OPTIONS[self.parameterAsEnum(parameters, self.METHOD, context)]
        p = {
            "idw_power": self.parameterAsDouble(parameters, self.IDW_POWER, context),
            "idw_k": self.parameterAsInt(parameters, self.IDW_K, context),
            "idw_fade": self.parameterAsDouble(parameters, self.IDW_FADE, context),
            "cv_folds": self.parameterAsInt(parameters, self.CV_FOLDS, context),
            "stride": self.parameterAsInt(parameters, self.STRIDE, context),
        }
        do_merge = self.parameterAsBoolean(parameters, self.DO_MERGE, context)
        merge_extent_choice = MERGE_EXTENT_OPTIONS[self.parameterAsEnum(parameters, self.MERGE_EXTENT, context)]
        merge_resolution = self.parameterAsDouble(parameters, self.MERGE_RESOLUTION, context)
        report_stride = self.parameterAsInt(parameters, self.REPORT_STRIDE, context)
        report_max_points = self.parameterAsInt(parameters, self.REPORT_MAX_POINTS, context)
        force = self.parameterAsBoolean(parameters, self.FORCE_RERUN, context)
        out_dir = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(out_dir, exist_ok=True)

        log = feedback.pushInfo
        cancel_check = feedback.isCanceled

        # The Reference DEM is always the chain's root, regardless of its own
        # resolution -- it's the elevation truth, not just "the finest one".
        # Everything else is auto-sorted finest -> coarsest and chained onto it.
        others_sorted = sorted(dems_cfg, key=lambda d: dem_resolution(d["path"]))
        sorted_dems = [{"path": ref_path, "nodata": ref_nodata, "label": os.path.basename(ref_path)}]
        sorted_dems += others_sorted
        for d in sorted_dems[1:]:
            d["label"] = os.path.basename(d["path"])
        log(f"Reference DEM: {sorted_dems[0]['label']}  (res={dem_resolution(ref_path):.3g})")
        log(f"{len(sorted_dems) - 1} DEM(s) to correct, sorted finest -> coarsest:")
        for i, d in enumerate(sorted_dems[1:], start=1):
            log(f"  {i}. {d['label']}  (res={dem_resolution(d['path']):.3g})")

        checkpoint = load_checkpoint(out_dir)
        if checkpoint and not force:
            log("Found existing checkpoint.json -- will skip stages already complete and unchanged.")
        elif force:
            log("Force re-run enabled -- ignoring any existing checkpoint.")

        stages = run_correction_chain(sorted_dems, method, p, out_dir, log, checkpoint,
                                       force=force, cancel_check=cancel_check)
        feedback.setProgress(55)

        report = {f"stage{i}": {k: v for k, v in s.items() if k != "calib_diff_sample"}
                  for i, s in enumerate(stages, start=1)}
        with open(os.path.join(out_dir, "correction_report.json"), "w") as f:
            json.dump(report, f, indent=2, default=str)

        results = {self.OUTPUT_FOLDER: out_dir}
        summary_lines = [f"Reference: {sorted_dems[0]['label']}. "
                          f"{len(stages)} DEM(s) corrected against it in a chain."]
        for i, s in enumerate(stages, start=1):
            summary_lines.append(f"  Stage {i}: {s['output_path']}  (method: {s['method_used']})")

        with suppress(Exception):
            for s in stages:
                name = os.path.splitext(os.path.basename(s["output_path"]))[0]
                lyr = QgsRasterLayer(s["output_path"], name)
                if lyr.isValid():
                    QgsProject.instance().addMapLayer(lyr)

        out_merge_path = ""
        if do_merge:
            if merge_extent_choice == "Reference DEM extent":
                bounds = raster_extent(sorted_dems[0]["path"])
            else:
                bounds = raster_extent(sorted_dems[-1]["path"])

            out_merge_path = os.path.join(out_dir, "MERGED_FINAL_DEM.tif")
            sources = [(sorted_dems[0]["path"], sorted_dems[0]["nodata"])]
            sources += [(s["output_path"], s["nodata"]) for s in stages]
            out_merge_path, _ = run_merge(sources, bounds, merge_resolution, out_merge_path,
                                           out_dir, checkpoint, log, force=force, cancel_check=cancel_check)
            summary_lines.append(f"Merged output: {out_merge_path}")
            with suppress(Exception):
                name = os.path.splitext(os.path.basename(out_merge_path))[0]
                lyr = QgsRasterLayer(out_merge_path, name)
                if lyr.isValid():
                    QgsProject.instance().addMapLayer(lyr)

        feedback.setProgress(70)

        # --- Statistics / Visualization tabs: sample points across the
        # finest DEM + every corrected output (i.e. the post-correction
        # picture, same source set used for the merge). ---
        log("Building Statistics and Visualization report tabs...")
        sample_dems = [{"path": sorted_dems[0]["path"], "nodata": sorted_dems[0]["nodata"],
                         "label": sorted_dems[0]["label"]}]
        for s, target in zip(stages, sorted_dems[1:]):
            sample_dems.append({"path": s["output_path"], "nodata": s["nodata"],
                                 "label": f"Corrected_{target['label']}"})

        summary_rows = []
        table, diff_cols = {}, []
        try:
            table, columns, _ = build_sample_table(sample_dems, report_stride, report_max_points, log)
            diff_cols = [c for c in columns if c.startswith("Diff_")]
            summary_rows = build_summary(table, diff_cols)
        except Exception as e:
            log(f"Point-sample report step failed (correction/merge outputs are still valid): {e}")

        stats_html = build_statistics_html(sorted_dems, stages, summary_rows)
        stats_path = os.path.join(out_dir, "statistics_report.html")
        with open(stats_path, "w", encoding="utf-8") as f:
            f.write(stats_html)

        viz_html = build_visualization_html(sorted_dems, stages, table, diff_cols)
        viz_path = os.path.join(out_dir, "visualization_report.html")
        with open(viz_path, "w", encoding="utf-8") as f:
            f.write(viz_html)

        feedback.setProgress(100)
        log("DONE")

        results[self.OUTPUT_MERGED] = out_merge_path
        results[self.OUTPUT_SUMMARY] = "\n".join(summary_lines)
        results[self.OUTPUT_STATS] = stats_path
        results[self.OUTPUT_VIZ] = viz_path
        return results


# ===========================================================================
# ALGORITHM 2: DEM Point Sample Export to Excel
# ===========================================================================

class DemPointSampleExportAlgorithm(QgsProcessingAlgorithm):
    INPUT_DEMS = "INPUT_DEMS"
    NODATA_OVERRIDES = "NODATA_OVERRIDES"
    STRIDE = "STRIDE"
    MAX_ROWS = "MAX_ROWS"
    FILE_NAME = "FILE_NAME"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    OUTPUT_FILE = "OUTPUT_FILE"
    OUTPUT_SUMMARY = "OUTPUT_SUMMARY"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return DemPointSampleExportAlgorithm()

    def name(self):
        return "dempointsampleexport"

    def displayName(self):
        return self.tr("DEM Point Sample Export to Excel")

    def group(self):
        return self.tr(CATEGORY_NAME)

    def groupId(self):
        return CATEGORY_ID

    def helpUrl(self):
        return docs_url('dempointsampleexport')

    def shortHelpString(self):
        return self.tr(
            "Samples 2 or more DEMs (e.g. a reference DEM plus one or more corrected "
            "DEMs) at a regular grid of points and exports one row per point to Excel: "
            "X, Y, one elevation column per DEM, a Zone column (which DEMs have valid "
            "data there), and difference columns vs. the finest reference DEM and vs. "
            "each DEM's immediately-finer neighbour.\n\n"
            "NoData overrides (optional): comma-separated values in the same order as "
            "the DEMs you select. Leave blank to use each raster's own NoData metadata.\n\n"
            "Excel caps out at ~1,048,576 rows per sheet, so pixels are sampled every "
            "Nth pixel (stride); the stride is automatically increased if needed to "
            "stay under the row limit you set."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_DEMS, self.tr("DEM layers to sample (2 or more)"),
            layerType=QgsProcessing.SourceType.TypeRaster))
        self.addParameter(QgsProcessingParameterString(
            self.NODATA_OVERRIDES,
            self.tr("NoData overrides (comma-separated, matching DEM order; blank = use each layer's own NoData)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.STRIDE, self.tr("Pixel stride (sample every Nth pixel)"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=20, minValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_ROWS, self.tr("Max rows (Excel limit is ~1,048,576)"),
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=SAFETY_MARGIN_ROWS, minValue=1))
        self.addParameter(QgsProcessingParameterString(
            self.FILE_NAME, self.tr("Output file name"), defaultValue="dem_point_samples.xlsx"))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Output folder")))
        self.addOutput(QgsProcessingOutputFile(
            self.OUTPUT_FILE, self.tr("Exported Excel (or CSV) file")))
        self.addOutput(QgsProcessingOutputString(
            self.OUTPUT_SUMMARY, self.tr("Run summary")))

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_DEMS, context)
        if len(layers) < 2:
            raise QgsProcessingException(self.tr("Select at least 2 DEM layers."))

        overrides_str = self.parameterAsString(parameters, self.NODATA_OVERRIDES, context)
        dems_cfg = resolve_dems_cfg(layers, overrides_str, feedback)
        for d in dems_cfg:
            d["label"] = os.path.basename(d["path"])

        stride = self.parameterAsInt(parameters, self.STRIDE, context)
        max_rows = min(self.parameterAsInt(parameters, self.MAX_ROWS, context), EXCEL_ROW_LIMIT - 10)
        file_name = self.parameterAsString(parameters, self.FILE_NAME, context).strip() or "dem_point_samples.xlsx"
        out_dir = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(out_dir, exist_ok=True)

        log = feedback.pushInfo

        sorted_dems = sorted(dems_cfg, key=lambda d: dem_resolution(d["path"]))
        log(f"{len(sorted_dems)} DEM layers, sorted finest -> coarsest:")
        for i, d in enumerate(sorted_dems):
            log(f"  {i + 1}. {d['label']}  (res={dem_resolution(d['path']):.3g})")

        table, columns, stride_used = build_sample_table(sorted_dems, stride, max_rows, log)
        feedback.setProgress(70)

        diff_cols = [c for c in columns if c.startswith("Diff_")]
        out_path = os.path.join(out_dir, file_name)
        final_path = write_excel(table, columns, diff_cols, out_path, log)

        feedback.setProgress(100)
        log(f"DONE -- {len(table['X']):,} rows exported (stride={stride_used}).")

        summary = f"{len(table['X']):,} rows exported (stride={stride_used}) to {final_path}"
        return {
            self.OUTPUT_FOLDER: out_dir,
            self.OUTPUT_FILE: final_path,
            self.OUTPUT_SUMMARY: summary,
        }

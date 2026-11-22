# -*- coding: utf-8 -*-
"""
Quality control before and after download.

* ``estimate`` / ``preflight`` – cheap checks run before downloading (no network):
  output size, AOI coverage, cloud cover, size limit.
* ``assess_output`` – runs on every finished file: readability (full checksum
  read), georeferencing, band count, NoData share, per-band statistics,
  saturation, constant bands, plausible reflectance, tiling/COG layout.
* ``fidelity_check`` – for native-resolution outputs, samples valid pixels and
  compares them with the original source pixels at the same map location
  (must be identical: proves no shift, flip, band swap or value change).
"""

import math
import random
from concurrent.futures import ThreadPoolExecutor

try:
    from osgeo import gdal, osr
except ImportError:  # pragma: no cover
    gdal = None
    osr = None

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}


# ---------------------------------------------------------------------------
# Before download
# ---------------------------------------------------------------------------
def estimate(scene, aoi, res_kind, res_value, n_bands, default_res, bytes_per_sample=2):
    """Rough output size for one scene (no network)."""
    native = scene.gsd or default_res or 30.0
    res = native / max(res_value, 0.01) if res_kind == "pct" else float(res_value)
    x0, y0, x1, y1 = aoi.bbox
    lat = math.radians((y0 + y1) / 2.0)
    w_m = abs(x1 - x0) * 111320.0 * max(math.cos(lat), 0.01)
    h_m = abs(y1 - y0) * 110570.0
    width, height = max(1, int(w_m / res)), max(1, int(h_m / res))
    mp = width * height / 1e6
    mb = mp * n_bands * bytes_per_sample * 0.55  # DEFLATE typically ~45 % saving
    return {"width": width, "height": height, "megapixels": mp, "est_mb": mb, "pixel_size": res}


def preflight(scenes, aoi, res_kind, res_value, n_bands, default_res, max_mp, bytes_per_sample=2):
    """Return (summary_text, [(level, message)]) for the selected scenes."""
    if not scenes:
        return "No scenes selected.", []
    msgs = []
    total_mb = 0.0
    too_big = []
    for s in scenes:
        est = estimate(s, aoi, res_kind, res_value, n_bands, default_res, bytes_per_sample)
        total_mb += est["est_mb"]
        if est["megapixels"] > max_mp:
            too_big.append(s.date)
    first = estimate(scenes[0], aoi, res_kind, res_value, n_bands, default_res, bytes_per_sample)
    summary = ("{n} scene{pl} · ≈ {w:,} × {h:,} px · {px:g} m pixels · {b} band{bpl} · ≈ {mb} total"
               .format(n=len(scenes), pl="" if len(scenes) == 1 else "s", w=first["width"],
                       h=first["height"], px=round(first["pixel_size"], 2), b=n_bands,
                       bpl="" if n_bands == 1 else "s", mb=_fmt_mb(total_mb)))
    partial = [s for s in scenes if s.aoi_coverage is not None and s.aoi_coverage < 99.5]
    if partial:
        msgs.append((WARN, "{} scene(s) cover only part of the AOI ({}). Areas outside the "
                           "scene will be NoData.".format(
                               len(partial), ", ".join("{:.0f}%".format(s.aoi_coverage)
                                                       for s in partial[:5]))))
    cloudy = [s for s in scenes if s.cloud_cover is not None and s.cloud_cover > 30]
    if cloudy:
        msgs.append((WARN, "{} scene(s) have more than 30 % cloud cover.".format(len(cloudy))))
    if too_big:
        msgs.append((FAIL, "Output exceeds the {:,} MP limit – choose a coarser resolution or "
                           "a smaller AOI.".format(max_mp)))
    if total_mb > 2000:
        msgs.append((WARN, "Large download (≈ {}). Make sure you have disk space.".format(
            _fmt_mb(total_mb))))
    if not msgs:
        msgs.append((PASS, "Selection looks good: full AOI coverage and acceptable size."))
    return summary, msgs


def _fmt_mb(mb):
    return "{:.1f} GB".format(mb / 1024.0) if mb >= 1024 else "{:.0f} MB".format(max(mb, 0.1)) \
        if mb >= 1 else "< 1 MB"


# ---------------------------------------------------------------------------
# After download
# ---------------------------------------------------------------------------
def _numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def assess_output(path, expected_bands=None, nodata=None, clipped=False, max_sample_px=2048):
    """Inspect a finished raster. Returns dict(status, checks=[{level,msg}], bands=[...], ...)."""
    report = {"status": PASS, "checks": [], "bands": [], "valid_percent": None}

    def add(level, msg):
        report["checks"].append({"level": level, "message": msg})
        if _RANK[level] > _RANK[report["status"]]:
            report["status"] = level

    if gdal is None:
        add(WARN, "GDAL not available – quality check skipped.")
        return report
    gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", "NO")
    np = _numpy()
    try:
        ds = gdal.OpenEx(path, gdal.OF_RASTER)
    except RuntimeError as exc:
        ds = None
        add(FAIL, "File cannot be opened: {}".format(exc))
    if ds is None:
        if not report["checks"]:
            add(FAIL, "File cannot be opened.")
        return report

    # --- georeferencing
    gt = ds.GetGeoTransform(can_return_null=True)
    wkt = ds.GetProjection()
    if not gt or not wkt:
        add(FAIL, "No georeferencing (CRS or geotransform missing).")
    elif gt[2] != 0 or gt[4] != 0:
        add(WARN, "Rotated geotransform – some tools may not support it.")
    else:
        srs = osr.SpatialReference(wkt=wkt)
        srs.AutoIdentifyEPSG()
        code = srs.GetAuthorityCode(None)
        add(PASS, "Georeferenced: {} · {:g} m pixels · {} × {} px".format(
            "EPSG:" + code if code else srs.GetName(), round(abs(gt[1]), 3),
            ds.RasterXSize, ds.RasterYSize))

    # --- band count
    if expected_bands is not None and ds.RasterCount != expected_bands:
        add(FAIL, "Expected {} bands, file has {}.".format(expected_bands, ds.RasterCount))

    # --- integrity: full read of every band
    try:
        for i in range(1, ds.RasterCount + 1):
            ds.GetRasterBand(i).Checksum()
        if gdal.GetLastErrorType() >= gdal.CE_Failure:
            raise RuntimeError(gdal.GetLastErrorMsg())
        add(PASS, "All {} band(s) read without errors (no corrupt tiles).".format(ds.RasterCount))
    except RuntimeError as exc:
        add(FAIL, "Read error – file may be truncated or corrupt: {}".format(exc))
        return report

    # --- statistics on a (decimated) full-extent read
    if np is not None:
        scale = min(1.0, float(max_sample_px) / max(ds.RasterXSize, ds.RasterYSize))
        bx, by = max(1, int(ds.RasterXSize * scale)), max(1, int(ds.RasterYSize * scale))
        valid_union = None
        for i in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(i)
            arr = band.ReadAsArray(buf_xsize=bx, buf_ysize=by).astype("float64")
            nd = band.GetNoDataValue()
            valid = np.isfinite(arr)
            if nd is not None:
                valid &= arr != nd
            valid_union = valid if valid_union is None else (valid_union | valid)
            vals = arr[valid]
            info = {"band": i, "name": band.GetDescription() or "band_{}".format(i),
                    "valid_percent": round(100.0 * valid.mean(), 2)}
            if vals.size:
                dtype_max = {gdal.GDT_Byte: 255, gdal.GDT_UInt16: 65535}.get(band.DataType)
                info.update(min=float(vals.min()), max=float(vals.max()), mean=round(float(vals.mean()), 3),
                            std=round(float(vals.std()), 3),
                            p2=float(np.percentile(vals, 2)), p98=float(np.percentile(vals, 98)))
                info["dtype_max"] = dtype_max
                info["saturated_percent"] = (round(100.0 * float((vals >= dtype_max).mean()), 3)
                                             if dtype_max else 0.0)
                sc, off = band.GetScale(), band.GetOffset()
                if sc not in (None, 1.0) or off not in (None, 0.0):
                    info["scaled_mean"] = round(info["mean"] * (sc or 1.0) + (off or 0.0), 4)
            report["bands"].append(info)

        valid_pct = 100.0 * float(valid_union.mean()) if valid_union is not None else 0.0
        report["valid_percent"] = round(valid_pct, 2)
        if valid_pct == 0:
            add(FAIL, "Image is empty – every pixel is NoData (scene does not cover the AOI).")
        elif valid_pct < 50 and not clipped:
            add(WARN, "Only {:.1f} % of the area has data (scene edge or partial coverage).".format(valid_pct))
        elif clipped:
            add(PASS, "{:.1f} % of the bounding box has data (outside the polygon = NoData).".format(valid_pct))
        else:
            add(PASS, "{:.1f} % of the area has valid data.".format(valid_pct))

        for b in report["bands"]:
            if "std" not in b:
                if valid_pct > 0:
                    add(WARN, "Band {} ({}) has no valid pixels.".format(b["band"], b["name"]))
                continue
            if b["std"] == 0:
                add(WARN, "Band {} ({}) is constant (value {:g}) – likely invalid.".format(
                    b["band"], b["name"], b["min"]))
            # 8-bit display products (TCI, aerial mosaics) clip highlights by design,
            # so they get a higher tolerance than 16-bit sensor data.
            limit = 15 if b.get("dtype_max") == 255 else 5
            if b.get("saturated_percent", 0) > limit:
                add(WARN, "Band {} ({}) is saturated on {:.1f} % of pixels (bright clouds, snow or "
                          "sand clipped at {}).".format(b["band"], b["name"], b["saturated_percent"],
                                                       b.get("dtype_max")))
            sm = b.get("scaled_mean")
            if sm is not None and not (-0.05 <= sm <= 1.2):
                add(WARN, "Band {} ({}) mean reflectance {:.3f} is outside the plausible 0–1 range."
                    .format(b["band"], b["name"], sm))
        if valid_pct > 0 and not any(c["level"] == WARN and "Band" in c["message"]
                                     for c in report["checks"]):
            add(PASS, "Band statistics plausible (no empty, constant or saturated bands).")

    # --- layout
    meta = ds.GetMetadata("IMAGE_STRUCTURE") or {}
    ovr = ds.GetRasterBand(1).GetOverviewCount()
    big = max(ds.RasterXSize, ds.RasterYSize) > 1024
    bw, bh = ds.GetRasterBand(1).GetBlockSize()
    tiled = bw < ds.RasterXSize or bw == bh
    layout = meta.get("LAYOUT", "")
    comp = meta.get("COMPRESSION", "none")
    if big and ovr == 0:
        add(WARN, "No overviews – large image may display slowly.")
    if not tiled and big:
        add(WARN, "File is striped, not tiled.")
    add(PASS, "Layout: {}{}, {} compression, {} overview level(s).".format(
        "Cloud-Optimized GeoTIFF" if layout == "COG" else "GeoTIFF",
        " (tiled)" if tiled else "", comp, ovr))
    ds = None
    return report


def _sample_positions(out, n_samples, seed, clusters=8):
    """Valid output pixels, grouped in `clusters` small neighbourhoods.

    Reading one pixel of a remote COG fetches its whole 1024 x 1024 tile, so scattering 40
    samples over the image costs 40 tile downloads per band; 8 neighbourhoods cost 8 and still
    expose a shift, band swap or value change just as well.
    """
    rnd = random.Random(seed)  # nosec B311 - reproducible sample positions, not security
    b1 = out.GetRasterBand(1)
    nd = b1.GetNoDataValue()
    per = max(1, n_samples // clusters)
    spread = 96
    candidates = []
    tries = 0
    while len(candidates) < per * clusters and tries < clusters * 60:
        tries += 1
        cx, cy = rnd.randrange(out.RasterXSize), rnd.randrange(out.RasterYSize)
        v = b1.ReadAsArray(cx, cy, 1, 1)[0][0]
        if nd is not None and v == nd:
            continue
        x0, y0 = max(0, cx - spread), max(0, cy - spread)
        w, h = min(out.RasterXSize, cx + spread) - x0, min(out.RasterYSize, cy + spread) - y0
        patch = b1.ReadAsArray(x0, y0, w, h)
        found = 0
        for _ in range(per * 6):
            px, py = rnd.randrange(w), rnd.randrange(h)
            if nd is None or patch[py][px] != nd:
                candidates.append((x0 + px, y0 + py))
                found += 1
                if found == per:
                    break
    return candidates


def _compare_band(out_path, bi, spath, sband, candidates):
    """Compare one output band with its source band; returns (compared, mismatches)."""
    gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", "NO")
    gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetThreadLocalConfigOption("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    try:
        out = gdal.Open(out_path)
        gt = out.GetGeoTransform()
        out_srs = osr.SpatialReference(wkt=out.GetProjection())
        out_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        sds = gdal.Open(spath)
        ssrs = osr.SpatialReference(wkt=sds.GetProjection())
        ssrs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(out_srs, ssrs)
        sgt = sds.GetGeoTransform()
        obnd = out.GetRasterBand(bi)
        sbnd = sds.GetRasterBand(sband)
        onv = obnd.GetNoDataValue()
        compared = mismatches = 0
        for px, py in candidates:
            x = gt[0] + (px + 0.5) * gt[1]
            y = gt[3] + (py + 0.5) * gt[5]
            sx, sy, _ = ct.TransformPoint(x, y)
            col = int(math.floor((sx - sgt[0]) / sgt[1]))
            row = int(math.floor((sy - sgt[3]) / sgt[5]))
            if not (0 <= col < sds.RasterXSize and 0 <= row < sds.RasterYSize):
                continue
            ov = obnd.ReadAsArray(px, py, 1, 1)[0][0]
            if onv is not None and ov == onv:
                continue  # masked in output (e.g. alpha/mask edge) - nothing to compare
            sv = sbnd.ReadAsArray(col, row, 1, 1)[0][0]
            compared += 1
            if float(sv) != float(ov):
                mismatches += 1
        return compared, mismatches
    finally:
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)


def fidelity_check(out_path, sources, n_samples=40, seed=7):
    """
    Compare output pixels with the source pixels at the same map location.

    ``sources``: list of (gdal_path, band_index) in output band order.
    Only meaningful for nearest-neighbour (native resolution) outputs.
    Returns dict(status, compared, mismatches, message).
    """
    if gdal is None:
        return {"status": WARN, "compared": 0, "mismatches": 0, "message": "GDAL not available."}
    gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", "NO")
    out = gdal.Open(out_path)
    candidates = _sample_positions(out, n_samples, seed)
    out = None
    if not candidates:
        return {"status": WARN, "compared": 0, "mismatches": 0,
                "message": "No valid pixels to compare."}
    # The bands are independent remote reads: do them side by side.
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
        results = list(pool.map(
            lambda item: _compare_band(out_path, item[0], item[1][0], item[1][1], candidates),
            enumerate(sources, start=1)))
    compared = sum(r[0] for r in results)
    mismatches = sum(r[1] for r in results)
    if compared == 0:
        return {"status": WARN, "compared": 0, "mismatches": 0, "message": "No overlapping pixels."}
    if mismatches == 0:
        return {"status": PASS, "compared": compared, "mismatches": 0,
                "message": "{} sampled pixels identical to the source (no shift, band swap or "
                           "value change).".format(compared)}
    return {"status": FAIL, "compared": compared, "mismatches": mismatches,
            "message": "{} of {} sampled pixels differ from the source.".format(mismatches, compared)}

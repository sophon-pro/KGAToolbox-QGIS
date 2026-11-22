"""
pipeline.py - Glue between raster I/O and the analysis engine.

Used by the QGIS background task and by the command-line interface, so the
same code path produces identical results in both.
"""

import os
import tempfile
import time

import numpy as np

from . import blocks, focal, landcover, raster_io
from .engine import analyse

FEET_TO_M = 0.3048


def _scope_text(window, used, tile, info):
    if window:
        return "current map extent ({} x {} px, full resolution)".format(window[2], window[3])
    if len(used) == 1 and used[0][2] == info["width"] and used[0][3] == info["height"]:
        return "whole raster ({} x {} px, full resolution)".format(info["width"], info["height"])
    return "{} sampled tile{} of {} px (full resolution)".format(
        len(used), "" if len(used) == 1 else "s", tile)


def _peak_cover(bres, windows, k, vrt_img, has_alpha, n_bands, nir, lc_tiles, info,
                gpkg, nominal):
    """Classify the orthophoto right under each block's highest cell, using the
    vegetation / texture thresholds found on the sampled tiles."""
    thr = [t["masks"]["threshold"] if "masks" in t else None for t in (lc_tiles or []) if t]
    tthr = [t["masks"].get("texture_threshold") for t in (lc_tiles or []) if t and "masks" in t]
    if not any(v is not None for v in thr):        # tiles carry raw bands: derive thresholds
        thr, tthr = [], []
        for t in lc_tiles or []:
            if t and "bands" in t:
                c = landcover.classify(t["bands"], t.get("nir", False), None,
                                       nominal if k == 1 else nominal * k)
                thr.append(c["threshold"])
                tthr.append(c.get("texture_threshold"))
    thr = [v for v in thr if v is not None]
    tthr = [v for v in tthr if v is not None]
    if not thr:
        return
    vthr = float(np.median(thr))
    tex_thr = float(np.median(tthr)) if tthr else None
    names = ["red", "green", "blue"] + (["nir"] if nir else [])
    rad = max(2, int(round(2.0 / nominal)))         # ~2 m around the cell (native px)
    for b, (wx, wy, ww, wh) in zip(bres, windows):
        pk = b.get("peak")
        if not pk:
            continue
        cx, cy = wx + pk["col"] * k + k // 2, wy + pk["row"] * k + k // 2
        x0, y0 = max(0, cx - rad), max(0, cy - rad)
        x1, y1 = min(info["width"], cx + rad + 1), min(info["height"], cy + rad + 1)
        win = (x0, y0, x1 - x0, y1 - y0)
        raw = [raster_io.read_windows(vrt_img, bi + 1, [win], 1)[0]
               for bi in range(n_bands + int(has_alpha))]
        if has_alpha:
            al = raw.pop()
            raw = [np.where(al >= 128, v, np.nan) for v in raw]
        arr = {nm: raw[i] for i, nm in enumerate(names)}
        idx, valid, _ = landcover.vegetation_index(arr["red"], arr["green"], arr.get("blue"),
                                                   arr.get("nir"))
        gt = info["geotransform"]
        pk["x"] = gt[0] + (cx + 0.5) * gt[1]
        pk["y"] = gt[3] + (cy + 0.5) * gt[5]
        if valid.sum() < 3:
            pk["cover"] = None
            continue
        if gpkg and raster_io.rasterize_buildings(gpkg, info, [win], 1)[0].mean() >= 0.3:
            pk["cover"] = "building"
            continue
        if float(np.nanmedian(idx[valid])) > vthr:
            if tex_thr is not None:
                cv = landcover.local_texture(arr["green"].astype(np.float64), 3)
                rough = float(np.nanmedian(cv[valid])) > tex_thr
                pk["cover"] = "tree" if rough else "crop/grass"
            else:
                pk["cover"] = "tree"
        else:
            pk["cover"] = "roof/bare/other"


def run(path, band=1, layer_name="", ref_path=None, ref_band=1,
        n_tiles=16, tile=512, z_units="m", extent=None, params=None,
        write_diagnostics=False, out_dir=None, progress=None, is_cancelled=None,
        ortho_path=None, ortho_bands=(1, 2, 3), ortho_nir=None,
        buildings_path=None, buildings_layer=None, block_grid=True):
    """Run the full diagnostic on a raster file.

    extent : optional (xmin, ymin, xmax, ymax) in the raster CRS - analyse
             exactly this area at full resolution ("current map extent").
    ortho_path / ortho_bands (R, G, B) / ortho_nir : optional orthophoto for
             the land-cover height test (NIR band index enables NDVI).
    buildings_path / buildings_layer : optional building-footprint polygons.
    block_grid : run the adaptive N x N block inspection (whole-layer scope).
    Returns engine.Result; result.info contains raster details and, when
    requested, the paths of diagnostic GeoTIFFs.
    """
    progress = progress or (lambda *_: None)
    t0 = time.time()
    z = FEET_TO_M if z_units == "ft" else 1.0

    progress(1, "Reading raster description")
    info = raster_io.describe(path, band)
    texts = raster_io.metadata_texts(info, layer_name)

    # ---- real resolution: was the raster resampled to finer pixels? -------
    progress(2, "Checking the real resolution of the data")
    probe, _ = raster_io.sample_tiles(path, band, 4, 1024, z_factor=z)
    k = focal.effective_factor(probe) if probe else 1
    k = k if k >= 3 else 1
    nominal = (info["pixel_dx_m"] * info["pixel_dy_m"]) ** 0.5
    if k > 1:
        info["pixel_dx_m"] *= k
        info["pixel_dy_m"] *= k
        params = dict(params or {}, resampled=True)

    progress(3, "Sampling tiles")
    window = raster_io.extent_window(info, *extent, max_px=3000 * k) if extent else None
    if window is not None:
        # Split a large window into ~1024 px tiles to keep memory modest
        x0, y0, w, h = window
        step = 1024 * k
        wins = [(x, y, min(step, x0 + w - x), min(step, y0 + h - y))
                for y in range(y0, y0 + h, step) for x in range(x0, x0 + w, step)]
        wins = [wv for wv in wins if wv[2] >= 32 * k and wv[3] >= 32 * k]
        tiles, used = raster_io.sample_tiles(path, band, windows=wins, z_factor=z,
                                             is_cancelled=is_cancelled, k=k)
    else:
        tiles, used = raster_io.sample_tiles(path, band, n_tiles, tile * k, z_factor=z,
                                             is_cancelled=is_cancelled, k=k)
    if not tiles:
        raise ValueError("No tile with at least 70% valid pixels was found.")

    ref_tiles = None
    if ref_path:
        progress(4, "Aligning reference layer")
        vrt = raster_io.aligned_reference(ref_path, ref_band, info)
        ref_tiles, _ = raster_io.sample_tiles(vrt, 1, windows=used, z_factor=z, k=k)

    lc_tiles = None
    if ortho_path:
        progress(4, "Resampling orthophoto to the elevation grid")
        bands = list(ortho_bands) + ([ortho_nir] if ortho_nir else [])
        vrt_img, has_alpha = raster_io.aligned_imagery(ortho_path, bands, info)
        names = ["red", "green", "blue"] + (["nir"] if ortho_nir else [])
        gpkg = None
        if buildings_path:
            progress(4, "Rasterising building footprints")
            gpkg = raster_io.prepare_buildings(buildings_path, buildings_layer, info)
        # When the elevation grid is fine (<= 1.5 m) but the analysis runs on
        # coarser effective pixels, classify the orthophoto at its FULL detail
        # (crown texture works there) and aggregate the masks to the analysis
        # grid.  Otherwise read the bands directly on the analysis grid.
        native = k > 1 and nominal <= 1.5
        lc_tiles = []
        for t, win in enumerate(used):
            if is_cancelled and is_cancelled():
                break
            kk = 1 if native else k
            raw = [raster_io.read_windows(vrt_img, bi + 1, [win], kk)[0]
                   for bi in range(len(bands) + int(has_alpha))]
            if has_alpha:                      # transparent collar -> NoData
                al = raw.pop()
                raw = [np.where(al >= 128, b, np.nan) for b in raw]
            arrs = {nm: raw[bi] for bi, nm in enumerate(names)}
            if all(np.isnan(a).all() for a in arrs.values()):
                lc_tiles.append(None)          # orthophoto does not cover this tile
                continue
            bm = raster_io.rasterize_buildings(gpkg, info, [win], kk)[0] if gpkg else None
            if not native:
                lc_tiles.append({"bands": arrs, "nir": bool(ortho_nir), "bldg": bm})
                continue
            lc = landcover.classify(arrs, bool(ortho_nir), bm, nominal)
            tshape = tiles[t].shape
            agg = {key: landcover.aggregate(lc[key], k, tshape)
                   for key in ("veg", "crop", "bldg", "valid")}
            masks = {"veg": agg["veg"] >= 0.5, "bldg": agg["bldg"] >= 0.5,
                     "crop": agg["crop"] >= 0.5}
            masks["valid"] = agg["valid"] >= 0.5
            masks["open"] = masks["valid"] & ~masks["veg"] & ~masks["bldg"]
            masks.update(index_name=lc["index_name"], threshold=lc["threshold"],
                         texture_used=lc["texture_used"],
                         texture_threshold=lc.get("texture_threshold"))
            lc_tiles.append({"masks": masks})
        if not any(lc_tiles):
            lc_tiles = []                      # supplied but no overlap -> note in report

    # ---- adaptive block grid (block-level inspection) -----------------------
    grid, block_tiles = None, None
    if block_grid:
        from .engine import DEFAULTS
        prm = dict(DEFAULTS, **(params or {}))
        res_eff = (info["pixel_dx_m"] * info["pixel_dy_m"]) ** 0.5
        # widest window the engine may use (coarse flat mode) -> safe block size
        obj_w = max(prm["object_width_m"], 7 * res_eff) if res_eff >= 5 else prm["object_width_m"]
        win_px = min(focal.odd(obj_w / res_eff), prm["max_window_px"])
        if window is not None:                 # map-extent scope: grid over that window
            x0, y0, w, h = window
        else:
            x0, y0, w, h = 0, 0, info["width"], info["height"]
        grid = blocks.design_grid(w, h, k, res_eff, win_px)
        grid["windows"] = [(x0 + a, y0 + b, c, d) for a, b, c, d in grid["windows"]]
        grid["cells"] = [(r, c, x0 + a, y0 + b, x0 + e, y0 + f)
                         for r, c, a, b, e, f in grid["cells"]]
        progress(4, "Reading {} x {} block grid".format(grid["nx"], grid["ny"]))
        block_tiles, _ = raster_io.sample_tiles(path, band, windows=grid["windows"],
                                                z_factor=z, k=k, is_cancelled=is_cancelled)
        block_tiles = [bt.astype(np.float32) for bt in block_tiles]

    def scaled(pct, msg):
        progress(5 + int(pct * 0.9), msg)

    result = analyse(
        tiles, info["pixel_dx_m"], info["pixel_dy_m"], meta_texts=texts, params=params,
        reference_tiles=ref_tiles, landcover_tiles=lc_tiles, block_tiles=block_tiles,
        progress=scaled, is_cancelled=is_cancelled,
        keep_rasters=write_diagnostics, info={
            "layer": layer_name or os.path.basename(path),
            "path": path,
            "band": band,
            "size": "{} x {} px".format(info["width"], info["height"]),
            "crs": "{}{}".format(info["crs"], " (EPSG:{})".format(info["epsg"]) if info["epsg"] else ""),
            "data_type": info["data_type"],
            "vertical_units": "feet (converted)" if z_units == "ft" else "metres",
            "vertical_datum": info["vertical_datum"],
            "scope": _scope_text(window, used, tile, info),
            "reference_layer": os.path.basename(ref_path) if ref_path else "",
            "orthophoto": os.path.basename(ortho_path) if ortho_path else "",
            "building_footprints": os.path.basename(buildings_path) if buildings_path else "",
        })

    # Optional diagnostic rasters.  Every analysed tile is written to a
    # "tiles" sub-folder and the tiles of each product are joined in ONE
    # virtual mosaic (VRT), so the map gets one layer per product.
    rasters = result.info.pop("_rasters", None)
    if write_diagnostics and rasters:
        from osgeo import gdal
        out_dir = out_dir or tempfile.mkdtemp(prefix="dem_dsm_")
        tile_dir = os.path.join(out_dir, "tiles")
        os.makedirs(tile_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        products = {}          # key -> (kind, label, [tile paths])
        export_objects = (params or {}).get("export_objects", True)
        for ti, r in enumerate(rasters):
            win = used[r["tile_index"]]
            items = []
            if export_objects:
                items += [("object_height", "height", "Object height (m)", r["object_height"]),
                          ("object_mask", "mask", "Object mask", r["object_mask"])]
            if "filtered" in r:
                tag = "{:g}".format(r["filter_m"])
                items += [("filtered_" + tag, "surface", "Filtered surface ({} m window)".format(tag),
                           r["filtered"]),
                          ("filter_dz_" + tag, "dz", "Removed by filter, dZ ({} m window)".format(tag),
                           r["filter_dz"])]
            for key, kind, label, arr in items:
                tp = os.path.join(tile_dir, "{}_{}_{:03d}.tif".format(stem, key.replace(".", "_"), ti + 1))
                raster_io.write_geotiff(tp, arr, info, win, k=k)
                products.setdefault(key, (kind, label, []))[2].append(tp)
        layers = []
        for key, (kind, label, tiles_) in products.items():
            vrt = os.path.join(out_dir, "{}_{}.vrt".format(stem, key.replace(".", "_")))
            opts = gdal.BuildVRTOptions(srcNodata=None if kind == "mask" else -9999.0,
                                        VRTNodata=None if kind == "mask" else -9999.0)
            gdal.BuildVRT(vrt, tiles_, options=opts)
            layers.append({"path": vrt, "kind": kind, "label": label, "tiles": len(tiles_)})
        result.info["diagnostic_layers"] = layers
        result.info["diagnostic_rasters"] = [lk["path"] for lk in layers]

    # Attach block geometry (map coordinates) to the block results
    bres = result.info.pop("_blocks", None)
    if grid is not None and bres is not None:
        gt = info["geotransform"]
        for b, (r, c, x0, y0, x1, y1), win in zip(bres, grid["cells"], grid["windows"]):
            b.update(row=r, col=c, window=win,
                     bbox=(gt[0] + x0 * gt[1], gt[3] + y1 * gt[5],
                           gt[0] + x1 * gt[1], gt[3] + y0 * gt[5]))
            if b.get("peak"):
                px = win[0] + b["peak"]["col"] * k + k // 2
                py = win[1] + b["peak"]["row"] * k + k // 2
                b["peak"]["x"] = gt[0] + (px + 0.5) * gt[1]
                b["peak"]["y"] = gt[3] + (py + 0.5) * gt[5]
        if ortho_path:
            progress(97, "Checking land cover under the highest cells")
            _peak_cover(bres, grid["windows"], k, vrt_img, has_alpha, len(bands),
                        bool(ortho_nir), lc_tiles, info, gpkg, nominal)
            covers = [b["peak"].get("cover") for b in bres if b.get("peak", {}).get("cover")]
            if covers:
                summ = {c: round(100 * covers.count(c) / len(covers), 1)
                        for c in ("tree", "building", "crop/grass", "roof/bare/other")}
                result.info["block_summary"]["peaks"]["cover_pct"] = summ
                for ind in result.indicators:
                    if ind.key == "peaks":
                        ind.finding += (" Under the highest cells the orthophoto shows: {:.0f}% "
                                        "trees, {:.0f}% building footprints, {:.0f}% crops/grass, "
                                        "{:.0f}% roofs / bare soil / roads.".format(summ["tree"], summ["building"],
                                                             summ["crop/grass"], summ["roof/bare/other"]))
        result.info["blocks"] = bres
        result.info["block_grid"] = {"nx": grid["nx"], "ny": grid["ny"],
                                     "block_m": [round(v, 1) for v in grid["block_m"]]}
        result.info["block_context"].update(path=path, band=band, k=k, z_factor=z,
                                            crs_wkt=info["wkt"])
    if k > 1:
        result.info["nominal_pixel_m"] = round(nominal, 3)
        result.info["resampling_factor"] = k
        if nominal * k >= 10:
            result.notes.insert(0, (
                "This raster has {:.2f} m pixels, but its real detail is only ~{:.0f} m: it was "
                "resampled from coarser data (e.g. Copernicus GLO-30 or SRTM warped to a fine "
                "grid). It was analysed at its real resolution (~{:.0f} m), so small objects "
                "cannot be resolved.").format(nominal, nominal * k, nominal * k))
        else:
            result.info["interpolated_surface"] = True
            result.notes.insert(0, (
                "This {:.2f} m raster is an INTERPOLATED surface: elevations vary linearly "
                "between points ~{:.0f} m apart. That is typical of a DTM built by TIN / "
                "kriging from filtered ground points (or of photogrammetric matching), and "
                "less typical of a first-return DSM, which is rough at the pixel scale. It was "
                "analysed at ~{:.0f} m; buildings and tree crowns are still resolvable.")
                .format(nominal, nominal * k, nominal * k))
    result.info["runtime_s"] = round(time.time() - t0, 1)
    return result

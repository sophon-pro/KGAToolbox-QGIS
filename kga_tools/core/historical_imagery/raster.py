# -*- coding: utf-8 -*-
"""
GDAL processing pipeline: remote COG bands -> clipped, resampled, stacked GeoTIFF/COG.

The pipeline is lazy until the very last step: every band is wrapped in an
in-memory warped VRT (clip window, target grid, optional polygon cutline), the
VRTs are stacked with BuildVRT, and only the final gdal.Translate actually
reads pixels – via HTTP range requests, so only the AOI window is downloaded.
"""

import contextlib
import json
import math
import os
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import AOI

try:
    from osgeo import gdal, osr
except ImportError:  # pragma: no cover - always available inside QGIS
    gdal = None
    osr = None


RGB_INTERP = {}
if gdal is not None:
    RGB_INTERP = {gdal.GCI_RedBand: "red", gdal.GCI_GreenBand: "green", gdal.GCI_BlueBand: "blue"}


class ProcessingCanceled(Exception):
    pass


class OutputTooLarge(Exception):
    pass


GDAL_HTTP_CONFIG = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_TIMEOUT": "120",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_USERAGENT": "KGAToolbox-HistoricalImagery/1.1",
    "COMPRESS_OVERVIEW": "DEFLATE",
    "GDAL_PAM_ENABLED": "NO",  # never leave .aux.xml clutter next to outputs
}


def configure_gdal_for_thread():
    """Apply HTTP-friendly GDAL options to the current (worker) thread only."""
    for key, value in GDAL_HTTP_CONFIG.items():
        gdal.SetThreadLocalConfigOption(key, value)


@contextlib.contextmanager
def gdal_exceptions():
    """Enable GDAL exceptions locally without changing QGIS' global setting."""
    if hasattr(gdal, "ExceptionMgr"):
        with gdal.ExceptionMgr(useExceptions=True):
            yield
        return
    previous = gdal.GetUseExceptions()
    gdal.UseExceptions()
    try:
        yield
    finally:
        if not previous:
            gdal.DontUseExceptions()


def _srs(wkt):
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return srs


def _epsg_srs(epsg):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(epsg))
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return srs


def transform_bbox(bbox, src_srs, dst_srs, densify=21):
    """Transform a bbox with edge densification (robust for UTM/LCC targets)."""
    if src_srs.IsSame(dst_srs):
        return tuple(bbox)
    ct = osr.CoordinateTransformation(src_srs, dst_srs)
    x0, y0, x1, y1 = bbox
    pts = []
    for i in range(densify):
        t = i / float(densify - 1)
        pts += [(x0 + (x1 - x0) * t, y0), (x0 + (x1 - x0) * t, y1),
                (x0, y0 + (y1 - y0) * t), (x1, y0 + (y1 - y0) * t)]
    xs, ys = [], []
    for x, y in pts:
        tx, ty, _ = ct.TransformPoint(x, y)
        if math.isfinite(tx) and math.isfinite(ty):
            xs.append(tx)
            ys.append(ty)
    if not xs:
        raise RuntimeError("Could not transform the AOI into the image coordinate system.")
    return (min(xs), min(ys), max(xs), max(ys))


def _intersect(a, b):
    out = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    if out[0] >= out[2] or out[1] >= out[3]:
        return None
    return out


def _snap(bounds, res, origin_x, origin_y):
    """Snap bounds outward to the pixel grid defined by an origin and resolution."""
    x0 = origin_x + math.floor((bounds[0] - origin_x) / res) * res
    x1 = origin_x + math.ceil((bounds[2] - origin_x) / res) * res
    y0 = origin_y + math.floor((bounds[1] - origin_y) / res) * res
    y1 = origin_y + math.ceil((bounds[3] - origin_y) / res) * res
    return (x0, y0, x1, y1)


def creation_options(fmt):
    if fmt == "COG":
        return ["COMPRESS=DEFLATE", "PREDICTOR=YES", "BIGTIFF=IF_SAFER", "OVERVIEWS=AUTO",
                "NUM_THREADS=ALL_CPUS"]
    return ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"]


def _log(message):
    with contextlib.suppress(Exception):
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(str(message), "Satellite Imagery Archive", Qgis.MessageLevel.Warning)


def _same_native_grid(bands, first):
    """True when every band sits on the first band's pixel grid and needs no mask handling."""
    gt0 = first["gt"]
    for bd in bands:
        if bd["mask"] is not None or bd["size"] != (first["w"], first["h"]):
            return False
        if any(abs(a - b) > 1e-9 for a, b in zip(bd["gt"], gt0)):
            return False
    return True


FETCH_WORKERS = 16


def _chunks(x_off, y_off, width, height, block):
    """Split a window into pieces that follow the source's internal tile grid."""
    bw = block[0] if 256 <= block[0] <= 4096 else 1024
    bh = block[1] if 256 <= block[1] <= 4096 else 1024
    xs, x = [], x_off
    while x < x_off + width:
        nxt = min(x_off + width, (x // bw + 1) * bw)
        xs.append((x, nxt - x))
        x = nxt
    ys, y = [], y_off
    while y < y_off + height:
        nxt = min(y_off + height, (y // bh + 1) * bh)
        ys.append((y, nxt - y))
        y = nxt
    return xs, ys


def _fast_stack(bands, gt, bounds, res, nodata, tile_dir, tag, tmp, progress, is_canceled):
    """Read the AOI window of every band in parallel pieces and return a stacked VRT path."""
    x_off = int(round((bounds[0] - gt[0]) / res))
    y_off = int(round((gt[3] - bounds[3]) / res))
    width = int(round((bounds[2] - bounds[0]) / res))
    height = int(round((bounds[3] - bounds[1]) / res))
    xs, ys = _chunks(x_off, y_off, width, height, bands[0]["block"])

    jobs = []
    for bi, bd in enumerate(bands):
        nd = bd["nodata"] if bd["nodata"] is not None else nodata
        bd["out_nodata"] = nd
        for yi, (y, h) in enumerate(ys):
            for xi, (x, w) in enumerate(xs):
                jobs.append((bi, bd["path"], bd["band"], nd, [x, y, w, h],
                             os.path.join(tile_dir, "b{}_{}_{}.tif".format(bi, yi, xi))))
    total = len(jobs)
    done = [0]
    lock = threading.Lock()
    abort = threading.Event()

    def fetch(job):
        if abort.is_set() or is_canceled():
            return None
        _bi, path, band, nd, win, out = job
        configure_gdal_for_thread()
        kwargs = dict(bandList=[band], srcWin=win, creationOptions=["TILED=YES", "BLOCKXSIZE=256",
                                                                     "BLOCKYSIZE=256"])
        if nd is not None:
            kwargs["noData"] = nd
        with gdal_exceptions():
            ds = gdal.Translate(out, path, **kwargs)
        if ds is None:
            raise RuntimeError("could not read {}".format(gdal.GetLastErrorMsg() or "a piece of the image"))
        ds = None
        with lock:
            done[0] += 1
            progress(100.0 * done[0] / total)
        return True

    pool = ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, total))
    try:
        futures = [pool.submit(fetch, j) for j in jobs]
        for fut in as_completed(futures):
            fut.result()                       # re-raises the first failure
    except BaseException:
        abort.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if is_canceled():
        raise ProcessingCanceled()

    # one mosaic per band, then the bands side by side
    band_vrts = []
    for bi in range(len(bands)):
        pieces = [j[5] for j in jobs if j[0] == bi]
        vrt = "/vsimem/hid_{}_f{}.vrt".format(tag, bi)
        gdal.BuildVRT(vrt, pieces)
        tmp.append(vrt)
        band_vrts.append(vrt)
    if len(band_vrts) == 1:
        return band_vrts[0]
    stack = "/vsimem/hid_{}_fstack.vrt".format(tag)
    gdal.BuildVRT(stack, band_vrts, options=gdal.BuildVRTOptions(separate=True))
    tmp.append(stack)
    return stack


def _warp_stack(bands, dst_wkt, bounds, res, resample, nodata, cutline, tag, tmp, is_canceled):
    """Lazy per-band warped VRTs stacked in one VRT (general path: any CRS, resolution, clip)."""
    warped = []
    for i, bd in enumerate(bands):
        if is_canceled():
            raise ProcessingCanceled()
        sel = "/vsimem/hid_{}_{}_sel.vrt".format(tag, i)
        topts = dict(format="VRT", bandList=[bd["band"]])
        if bd["mask"] is not None:
            topts["maskBand"] = bd["mask"]
        gdal.Translate(sel, bd["path"], options=gdal.TranslateOptions(**topts))
        tmp.append(sel)
        nd = bd["nodata"] if bd["nodata"] is not None else nodata
        if nd is None and (cutline or bd["mask"] is not None):
            nd = 0
        bd["out_nodata"] = nd
        kwargs = dict(format="VRT", dstSRS=dst_wkt, outputBounds=bounds, xRes=res, yRes=res,
                      resampleAlg=resample)
        if nd is not None:
            kwargs["dstNodata"] = nd
            if bd["nodata"] is not None or bd["mask"] is None:
                kwargs["srcNodata"] = nd
        if cutline:
            kwargs.update(cutlineDSName=cutline, cropToCutline=False)
        out_vrt = "/vsimem/hid_{}_{}.vrt".format(tag, i)
        gdal.Warp(out_vrt, sel, options=gdal.WarpOptions(**kwargs))
        tmp.append(out_vrt)
        warped.append(out_vrt)

    if len(warped) == 1:
        stack = warped[0]
    else:
        stack = "/vsimem/hid_{}_stack.vrt".format(tag)
        gdal.BuildVRT(stack, warped, options=gdal.BuildVRTOptions(separate=True))
        tmp.append(stack)
    return stack


def build_raster(sources, aoi: AOI, out_path, res_kind="pct", res_value=1.0, fmt="GTiff",
                 nodata=0, clip_to_polygon=False, already_processed=False,
                 max_megapixels=400, metadata=None, progress=None, is_canceled=None):
    """
    Create ``out_path`` from ``sources`` (list of BandSource).

    Returns a dict describing the output (crs, pixel size, size, band names...).
    """
    if gdal is None:
        raise RuntimeError("GDAL Python bindings are not available.")
    progress = progress or (lambda pct: None)
    is_canceled = is_canceled or (lambda: False)
    configure_gdal_for_thread()
    tmp = []  # /vsimem paths to clean up
    tmp_dirs = []  # temporary folders to remove
    tag = uuid.uuid4().hex[:10]

    with gdal_exceptions():
        try:
            # ---- 1. inspect sources -------------------------------------
            bands = []  # dicts: path, band, name, nodata, mask, scale, offset
            first = None
            native_res = None
            for src in sources:
                if is_canceled():
                    raise ProcessingCanceled()
                ds = gdal.Open(src.path)
                gt = ds.GetGeoTransform()
                if first is None:
                    first = dict(wkt=ds.GetProjection(), gt=gt, w=ds.RasterXSize, h=ds.RasterYSize)
                    if not first["wkt"]:
                        raise RuntimeError("The image has no coordinate reference system.")
                res = min(abs(gt[1]), abs(gt[5]))
                native_res = res if native_res is None else min(native_res, res)
                # An alpha band is turned into a mask (transparent edges stay NoData)
                # instead of being exported as a 4th "colour" band.
                alpha = [i for i in range(1, ds.RasterCount + 1)
                         if ds.GetRasterBand(i).GetColorInterpretation() == gdal.GCI_AlphaBand]
                idxs = [b for b in (src.bands or range(1, ds.RasterCount + 1)) if b not in alpha] \
                    or list(src.bands or range(1, ds.RasterCount + 1))
                for j, b in enumerate(idxs):
                    band = ds.GetRasterBand(b)
                    desc = band.GetDescription()
                    if src.names and len(src.names) == len(idxs):
                        name = src.names[j]
                    elif src.names:
                        name = "{}_{}".format(src.names[0], b)
                    elif desc:
                        name = desc
                    elif band.GetColorInterpretation() in RGB_INTERP:
                        name = RGB_INTERP[band.GetColorInterpretation()]
                    else:
                        name = "band_{}".format(b)
                    mask = None
                    if band.GetNoDataValue() is None:
                        if alpha:
                            mask = alpha[0]
                        elif band.GetMaskFlags() & gdal.GMF_PER_DATASET:
                            mask = "mask"
                    scale = src.scales[j] if src.scales and len(src.scales) == len(idxs) else None
                    offset = src.offsets[j] if src.offsets and len(src.offsets) == len(idxs) else None
                    if scale is None and offset is None:
                        sc, off = band.GetScale(), band.GetOffset()
                        if (sc not in (None, 1.0)) or (off not in (None, 0.0)):
                            scale, offset = sc, off
                    bands.append(dict(path=src.path, band=b, name=name, nodata=band.GetNoDataValue(),
                                      mask=mask, scale=scale, offset=offset, gt=gt,
                                      size=(ds.RasterXSize, ds.RasterYSize),
                                      block=tuple(band.GetBlockSize())))
                ds = None
            if not bands:
                raise RuntimeError("No bands to process.")

            # ---- 2. target grid ----------------------------------------------
            src_srs = _srs(first["wkt"])
            dst_srs = src_srs
            if res_kind == "m" and src_srs.IsGeographic() and not already_processed:
                dst_srs = _epsg_srs(aoi.utm_epsg())  # metres requested on a lat/lon image

            if already_processed:
                res = native_res
            elif res_kind == "pct":
                res = native_res / max(res_value, 0.01)
            else:
                res = float(res_value)

            gt = first["gt"]
            src_extent = (gt[0], gt[3] + gt[5] * first["h"], gt[0] + gt[1] * first["w"], gt[3])
            src_extent_dst = transform_bbox(src_extent, src_srs, dst_srs)
            aoi_dst = transform_bbox(aoi.bbox, _epsg_srs(4326), dst_srs)
            bounds = _intersect(aoi_dst, src_extent_dst)
            if bounds is None:
                raise RuntimeError("The area of interest does not overlap this image.")
            if dst_srs is src_srs:
                bounds = _snap(bounds, res, gt[0], gt[3])  # stay on the native pixel grid
            else:
                bounds = _snap(bounds, res, 0.0, 0.0)
            width = int(round((bounds[2] - bounds[0]) / res))
            height = int(round((bounds[3] - bounds[1]) / res))
            megapixels = width * height / 1e6
            if megapixels > max_megapixels:
                raise OutputTooLarge(
                    "Output would be {:,} × {:,} px ({:,.0f} MP per band), above the {:,} MP limit. "
                    "Choose a coarser resolution, a smaller AOI, or raise the limit in Settings."
                    .format(width, height, megapixels, max_megapixels))

            # Compare resolutions in the SAME units: the native pixel size expressed in the
            # output CRS (degrees vs metres would otherwise be compared when reprojecting).
            if dst_srs is src_srs:
                native_dst = native_res
            else:
                native_dst = min((src_extent_dst[2] - src_extent_dst[0]) / first["w"],
                                 (src_extent_dst[3] - src_extent_dst[1]) / first["h"])
            ratio = res / native_dst if native_dst else 1.0
            if already_processed or (res_kind == "pct" and res_value >= 0.999 and dst_srs is src_srs):
                resample = "near"
            elif ratio > 1.2:
                resample = "average"     # coarsening: area-weighted mean, no aliasing
            elif ratio < 0.8:
                resample = "bilinear"    # refining: smooth interpolation
            else:
                resample = "near"        # ≈ native size (e.g. reprojection): keep original values

            cutline = None
            if clip_to_polygon and aoi.geometry:
                cutline = "/vsimem/hid_{}_cut.geojson".format(tag)
                fc = {"type": "FeatureCollection", "features": [
                    {"type": "Feature", "properties": {}, "geometry": aoi.geometry}]}
                gdal.FileFromMemBuffer(cutline, json.dumps(fc).encode("utf-8"))
                tmp.append(cutline)

            dst_wkt = dst_srs.ExportToWkt()

            # ---- 3. the stack of bands that step 4 materialises -------------
            # Fast path: native grid, no reprojection or clipping. The window is read in
            # parallel blocks into local files (the network, not the CPU, is the limit, and
            # many requests in flight beat one by a wide margin). Anything else, or any
            # failure of the fast path, goes through GDAL's warper as before.
            p_base, p_scale = 0.0, 1.0
            stack = None
            if (dst_srs is src_srs and resample == "near" and not cutline
                    and _same_native_grid(bands, first)):
                tile_dir = tempfile.mkdtemp(prefix="hid_tiles_")
                tmp_dirs.append(tile_dir)
                try:
                    stack = _fast_stack(bands, first["gt"], bounds, res, nodata, tile_dir, tag,
                                        tmp, lambda pct: progress(pct * 0.8), is_canceled)
                    p_base, p_scale = 80.0, 0.2
                except ProcessingCanceled:
                    raise
                except Exception as exc:  # noqa: BLE001 - fall back to the warper
                    _log("Parallel read failed, using the standard path: {}".format(exc))
                    stack = None
            if stack is None:
                stack = _warp_stack(bands, dst_wkt, bounds, res, resample, nodata, cutline, tag,
                                    tmp, is_canceled)

            # Band descriptions + dataset metadata travel into the final file.
            ds = gdal.Open(stack, gdal.GA_Update)
            for i, bd in enumerate(bands):
                rb = ds.GetRasterBand(i + 1)
                rb.SetDescription(str(bd["name"]))
                if bd["scale"] is not None or bd["offset"] is not None:
                    rb.SetScale(float(bd["scale"] if bd["scale"] is not None else 1.0))
                    rb.SetOffset(float(bd["offset"] if bd["offset"] is not None else 0.0))
            if metadata:
                ds.SetMetadata({str(k).upper(): str(v) for k, v in metadata.items() if v is not None})
            ds = None

            # ---- 4. materialise (this is where pixels are downloaded) ----
            driver = fmt
            if fmt == "COG" and gdal.GetDriverByName("COG") is None:
                driver = "GTiff"  # GDAL < 3.1

            def callback(complete, _msg, _data):
                progress(p_base + complete * 100.0 * p_scale)
                return 0 if is_canceled() else 1

            if os.path.exists(out_path):
                gdal.GetDriverByName("GTiff").Delete(out_path)
            try:
                gdal.Translate(out_path, stack, options=gdal.TranslateOptions(
                    format=driver, creationOptions=creation_options(driver), callback=callback))
            except RuntimeError:
                if is_canceled():
                    raise ProcessingCanceled()
                raise
            if is_canceled():
                raise ProcessingCanceled()

            # Internal overviews make big GeoTIFFs display fast in QGIS.
            if driver == "GTiff" and max(width, height) > 1024:
                ds = gdal.Open(out_path, gdal.GA_Update)
                levels = [f for f in (2, 4, 8, 16, 32) if max(width, height) / f >= 256]
                if levels:
                    ds.BuildOverviews("AVERAGE", levels)
                ds = None

            epsg = None
            with contextlib.suppress(Exception):
                dst_srs.AutoIdentifyEPSG()
                epsg = dst_srs.GetAuthorityCode(None)
            return {
                "path": out_path,
                "driver": driver,
                "crs": "EPSG:{}".format(epsg) if epsg else dst_srs.ExportToProj4(),
                "pixel_size": res,
                "width": width,
                "height": height,
                "bounds": bounds,
                "band_names": [b["name"] for b in bands],
                "nodata": bands[0].get("out_nodata"),
                "band_count": len(bands),
                "sources": [(b["path"], b["band"]) for b in bands],  # in-memory only (may hold tokens)
                "scale_offset": [[b["scale"], b["offset"]] for b in bands],
                "resampling": resample,
                "clipped_to_polygon": bool(cutline),
            }
        except ProcessingCanceled:
            _remove(out_path)
            raise
        except OutputTooLarge:
            raise
        except Exception:
            _remove(out_path)
            raise
        finally:
            for p in tmp:
                with contextlib.suppress(Exception):
                    gdal.Unlink(p)
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

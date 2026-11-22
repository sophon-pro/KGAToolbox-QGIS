"""
raster_io.py - GDAL helpers: raster description, tile sampling, reference
alignment and diagnostic GeoTIFF output.

Works on any GDAL-readable file (GeoTIFF, COG, VRT, .hgt, .img, .asc ...).
No QGIS imports, so it can also be used from the command line.
"""

import math
import os

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

# How a resampled raster is brought back to its real resolution.  Point
# sampling (nearest) keeps values close to the original measurements; block
# averaging would blur the already-interpolated surface a second time.
_RESAMPLE = gdal.GRIORA_NearestNeighbour


# --------------------------------------------------------------------------
# Description
# --------------------------------------------------------------------------
def describe(path, band=1):
    """Collect geometry, units and textual metadata for a raster."""
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    b = ds.GetRasterBand(band)
    gt = ds.GetGeoTransform()
    srs = osr.SpatialReference()
    wkt = ds.GetProjection()
    if wkt:
        srs.ImportFromWkt(wkt)

    is_geo = bool(wkt) and srs.IsGeographic()
    cy = gt[3] + gt[5] * ds.RasterYSize / 2.0
    if is_geo:
        lat = cy
        dx_m = abs(gt[1]) * 111320.0 * math.cos(math.radians(lat))
        dy_m = abs(gt[5]) * 110574.0
    else:
        unit = srs.GetLinearUnits() if wkt else 1.0
        dx_m, dy_m = abs(gt[1]) * unit, abs(gt[5]) * unit

    meta = {}
    for domain in (None, "IMAGE_STRUCTURE"):
        md = ds.GetMetadata(domain) if domain else ds.GetMetadata()
        meta.update(md or {})
    meta.update(b.GetMetadata() or {})

    vert = ""
    if wkt and srs.IsCompound():
        vert = srs.GetAttrValue("VERT_CS") or srs.GetAttrValue("VERTCRS") or "compound CRS"

    info = {
        "path": path,
        "driver": ds.GetDriver().ShortName,
        "width": ds.RasterXSize,
        "height": ds.RasterYSize,
        "bands": ds.RasterCount,
        "band": band,
        "data_type": gdal.GetDataTypeName(b.DataType),
        "nodata": b.GetNoDataValue(),
        "crs": (srs.GetAttrValue("PROJCS") or srs.GetAttrValue("GEOGCS") or "unknown") if wkt else "none",
        "epsg": srs.GetAuthorityCode(None) if wkt else None,
        "geographic": is_geo,
        "pixel_dx_m": dx_m,
        "pixel_dy_m": dy_m,
        "vertical_datum": vert or "not declared",
        "band_description": b.GetDescription(),
        "metadata": meta,
        "geotransform": gt,
        "wkt": wkt,
    }
    ds = None
    return info


def metadata_texts(info, layer_name=""):
    """Text sources searched by the naming/metadata indicator."""
    path = info["path"]
    texts = {
        "file name": os.path.basename(path),
        "folder": os.path.basename(os.path.dirname(path)),
        "layer name": layer_name,
        "band description": info.get("band_description", ""),
    }
    md = info.get("metadata", {})
    useful = {k: v for k, v in md.items()
              if k.upper() not in ("AREA_OR_POINT", "INTERLEAVE", "COMPRESSION", "LAYOUT")}
    if useful:
        texts["GDAL metadata"] = " ".join("{} {}".format(k, v) for k, v in useful.items())[:4000]
    # Common side-car metadata files (product readme / xml)
    for ext in (".xml", ".aux.xml", ".txt"):
        side = os.path.splitext(path)[0] + ext
        if os.path.isfile(side) and os.path.getsize(side) < 2_000_000:
            try:
                with open(side, "r", errors="ignore") as fh:
                    texts["side-car " + ext] = fh.read(20000)
            except OSError:
                pass
    return texts


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def _read(band, xoff, yoff, w, h, nodata, z_factor, k=1):
    """Read a window; with k > 1 it is point-sampled every k pixels (used
    when the raster was resampled to pixels much finer than its real detail)."""
    if k > 1:
        a = band.ReadAsArray(xoff, yoff, w, h, buf_xsize=max(1, w // k),
                             buf_ysize=max(1, h // k),
                             resample_alg=_RESAMPLE).astype(np.float64)
    else:
        a = band.ReadAsArray(xoff, yoff, w, h).astype(np.float64)
    if nodata is not None:
        a[a == nodata] = np.nan
    a *= z_factor                    # to metres
    a[(a < -1000) | (a > 9000)] = np.nan   # undeclared fill values (-9999, -32768 ...)
    return a


def tile_windows(width, height, n_tiles=16, tile=512):
    """Evenly spread candidate windows (xoff, yoff, w, h) over the raster."""
    w, h = min(tile, width), min(tile, height)
    if width <= tile and height <= tile:
        return [(0, 0, width, height)]
    side = max(1, int(math.ceil(math.sqrt(n_tiles * 2))))   # oversample candidates
    xs = np.linspace(0, width - w, side).astype(int)
    ys = np.linspace(0, height - h, side).astype(int)
    seen, out = set(), []
    for y in ys:
        for x in xs:
            if (x, y) not in seen:
                seen.add((x, y))
                out.append((int(x), int(y), w, h))
    return out


def sample_tiles(path, band=1, n_tiles=16, tile=512, z_factor=1.0, windows=None,
                 is_cancelled=None, min_valid=0.7, k=1):
    """Read up to `n_tiles` full-resolution tiles with enough valid data.

    Full-resolution sampling (instead of reading an overview) preserves the
    fine detail that separates DSMs from DEMs.
    """
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    b = ds.GetRasterBand(band)
    nodata = b.GetNoDataValue()
    cands = windows or tile_windows(ds.RasterXSize, ds.RasterYSize, n_tiles, tile)
    if windows is not None:
        n_tiles = len(windows)             # explicit windows: read every one of them

    # Visit candidates in a spread-out order so early stops still cover the scene
    order = np.random.default_rng(7).permutation(len(cands)) if windows is None else range(len(cands))
    tiles, used = [], []
    for ci in order:
        if is_cancelled and is_cancelled():
            break
        x, y, w, h = cands[ci]
        a = _read(b, x, y, w, h, nodata, z_factor, k)
        if np.isfinite(a).mean() >= min_valid or windows is not None:
            tiles.append(a)
            used.append((x, y, w, h))
            if len(tiles) >= n_tiles:
                break
    ds = None
    return tiles, used


def extent_window(info, xmin, ymin, xmax, ymax, max_px=3000):
    """Pixel window for a map extent (in the raster CRS), clipped to the raster."""
    gt = info["geotransform"]
    x0 = int(math.floor((xmin - gt[0]) / gt[1]))
    x1 = int(math.ceil((xmax - gt[0]) / gt[1]))
    y0 = int(math.floor((ymax - gt[3]) / gt[5]))
    y1 = int(math.ceil((ymin - gt[3]) / gt[5]))
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0, x1 = max(0, x0), min(info["width"], x1)        # intersect with raster
    y0, y1 = max(0, y0), min(info["height"], y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("The current map extent does not overlap this layer.")
    w, h = x1 - x0, y1 - y0
    if w < 32 or h < 32:
        raise ValueError("The map extent covers too few pixels of this layer (zoom out).")
    if w > max_px or h > max_px:
        raise ValueError("The map extent is {} x {} px; zoom in to at most {} px per side "
                         "or use whole-layer sampling.".format(w, h, max_px))
    return (x0, y0, w, h)


def aligned_reference(ref_path, ref_band, info, z_factor=1.0):
    """Open the reference layer warped (bilinear) onto the primary layer's grid.

    Returns the path of an in-memory VRT; read it with the same windows.
    """
    gt = info["geotransform"]
    xmin, ymax = gt[0], gt[3]
    xmax = xmin + gt[1] * info["width"]
    ymin = ymax + gt[5] * info["height"]
    src = gdal.Open(ref_path)
    vrt = "/vsimem/dem_dsm_ref_{}.vrt".format(abs(hash((ref_path, info["path"]))))
    opts = gdal.WarpOptions(
        format="VRT", outputBounds=(xmin, ymin, xmax, ymax),
        width=info["width"], height=info["height"],
        dstSRS=info["wkt"] or None, resampleAlg="bilinear",
        srcBands=[ref_band], dstNodata=-99999.0,
    )
    gdal.Warp(vrt, src, options=opts)
    src = None
    return vrt


def write_geotiff(path, array, info, window, nodata=-9999.0, k=1):
    """Write a diagnostic raster that covers `window` of the primary layer
    (pixel size multiplied by k when the analysis ran on averaged pixels)."""
    x, y = window[0], window[1]
    h, w = array.shape
    gt = info["geotransform"]
    new_gt = (gt[0] + x * gt[1], gt[1] * k, gt[2], gt[3] + y * gt[5], gt[4], gt[5] * k)
    is_int = array.dtype.kind in "ui"
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 1, gdal.GDT_Byte if is_int else gdal.GDT_Float32,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(new_gt)
    if info["wkt"]:
        ds.SetProjection(info["wkt"])
    band = ds.GetRasterBand(1)
    out = array if is_int else np.where(np.isfinite(array), array, nodata).astype(np.float32)
    band.WriteArray(out)
    if not is_int:
        band.SetNoDataValue(nodata)
    band.FlushCache()
    ds = None
    return path


# --------------------------------------------------------------------------
# Orthophoto and building footprints (land-cover test)
# --------------------------------------------------------------------------
def aligned_imagery(img_path, bands, info):
    """Warp the chosen orthophoto bands onto the elevation grid.

    A finer orthophoto is AVERAGED into each elevation pixel (so a 10 cm
    image becomes the mean colour of each 50 cm cell); a coarser one is
    resampled bilinearly.  Returns (vrt_path, has_alpha): band k of the VRT
    is `bands[k]`; when the orthophoto has an alpha band it is appended last.
    """
    src = gdal.Open(img_path)
    if src.RasterCount < 3:
        raise ValueError("The orthophoto '{}' has {} band(s). It looks like an elevation "
                         "raster; choose the RGB image instead.".format(
                             os.path.basename(img_path), src.RasterCount))
    # Drone orthophotos are usually RGBA: honour the alpha band so the
    # transparent collar is treated as NoData, never as black pixels.
    alpha = [k for k in range(1, src.RasterCount + 1)
             if src.GetRasterBand(k).GetColorInterpretation() == gdal.GCI_AlphaBand]
    src_bands = list(bands)
    if alpha:
        if alpha[0] in src_bands:
            raise ValueError("Band {} of the orthophoto is its transparency (alpha) band, "
                             "not a colour or NIR band.".format(alpha[0]))
        src_bands.append(alpha[0])
    gt_src = src.GetGeoTransform()
    srs_src = osr.SpatialReference()
    srs_src.ImportFromWkt(src.GetProjection())
    srs_dst = osr.SpatialReference()
    if info["wkt"]:
        srs_dst.ImportFromWkt(info["wkt"])
    # Rough pixel-size comparison in metres
    unit = 111320.0 if srs_src.IsGeographic() else (srs_src.GetLinearUnits() or 1.0)
    finer = abs(gt_src[1]) * unit < info["pixel_dx_m"] * 0.9

    gt = info["geotransform"]
    xmin, ymax = gt[0], gt[3]
    xmax = xmin + gt[1] * info["width"]
    ymin = ymax + gt[5] * info["height"]
    vrt = "/vsimem/dem_dsm_img_{}.vrt".format(abs(hash((img_path, tuple(bands), info["path"]))))
    gdal.Warp(vrt, src, options=gdal.WarpOptions(
        format="VRT", outputBounds=(xmin, ymin, xmax, ymax),
        width=info["width"], height=info["height"], dstSRS=info["wkt"] or None,
        resampleAlg="average" if finer else "bilinear", srcBands=src_bands,
        outputType=gdal.GDT_Float32, dstNodata=-1.0))
    src = None
    return vrt, bool(alpha)


def read_windows(path, band, windows, k=1):
    """Read raw values for each window (NoData -> NaN, no elevation filters);
    k > 1 block-averages like `sample_tiles`."""
    ds = gdal.Open(path)
    b = ds.GetRasterBand(band)
    nodata = b.GetNoDataValue()
    out = []
    for x, y, w, h in windows:
        if k > 1:
            a = b.ReadAsArray(x, y, w, h, buf_xsize=max(1, w // k), buf_ysize=max(1, h // k),
                              resample_alg=_RESAMPLE).astype(np.float64)
        else:
            a = b.ReadAsArray(x, y, w, h).astype(np.float64)
        if nodata is not None:
            a[a == nodata] = np.nan
        out.append(a)
    ds = None
    return out


def prepare_buildings(vec_path, layer_name, info):
    """Reproject building polygons to the elevation CRS, clipped to its extent.
    Returns an in-memory GeoPackage path (or None if no features overlap)."""
    gt = info["geotransform"]
    xmin, ymax = gt[0], gt[3]
    xmax = xmin + gt[1] * info["width"]
    ymin = ymax + gt[5] * info["height"]
    out = "/vsimem/dem_dsm_bldg_{}.gpkg".format(abs(hash((vec_path, layer_name, info["path"]))))
    opts = gdal.VectorTranslateOptions(
        format="GPKG", dstSRS=info["wkt"] or None, spatFilter=(xmin, ymin, xmax, ymax),
        spatSRS=info["wkt"] or None, layers=[layer_name] if layer_name else None,
        layerName="buildings", geometryType="PROMOTE_TO_MULTI")
    ds = gdal.VectorTranslate(out, vec_path, options=opts)
    n = ds.GetLayer(0).GetFeatureCount() if ds is not None and ds.GetLayerCount() else 0
    ds = None
    return out if n else None


def rasterize_buildings(gpkg, info, windows, k=1):
    """Building mask (uint8) for each window of the elevation grid."""
    gt = info["geotransform"]
    src = gdal.OpenEx(gpkg, gdal.OF_VECTOR)
    masks = []
    for x, y, w, h in windows:
        mem = gdal.GetDriverByName("MEM").Create("", max(1, w // k), max(1, h // k), 1,
                                                 gdal.GDT_Byte)
        mem.SetGeoTransform((gt[0] + x * gt[1], gt[1] * k, 0, gt[3] + y * gt[5], 0, gt[5] * k))
        if info["wkt"]:
            mem.SetProjection(info["wkt"])
        gdal.Rasterize(mem, src, layers=["buildings"], burnValues=[1])
        masks.append(mem.GetRasterBand(1).ReadAsArray())
        mem = None
    src = None
    return masks

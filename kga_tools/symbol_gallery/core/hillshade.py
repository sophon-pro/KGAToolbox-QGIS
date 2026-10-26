# -*- coding: utf-8 -*-
"""
Hillshade for DEMs – the "RAS Mapper look" (colour ramp + shaded relief).

Two methods:

* **live**  – a second, multiply-blended hillshade layer of the same file right
  above the DEM.  Instant, adjustable at any time, nothing written to disk.
* **baked** – colours and hillshade combined into one RGBA GeoTIFF (shaded
  relief image).  One layer, fast to draw, identical in every program
  (QGIS, ArcGIS, Google Earth, reports).  Runs as a background task.

Strength adapts to the terrain: "Auto" exaggerates flat floodplains enough to
show their texture and leaves mountains at their natural relief.
"""
import math
import os

from qgis.PyQt.QtGui import QPainter
from qgis.core import QgsSettings

SETTINGS = "symbol_gallery/hillshade/"

BLENDS = [  # key, label, QPainter mode
    ("multiply", "Multiply (classic, darkens)", QPainter.CompositionMode.CompositionMode_Multiply),
    ("softlight", "Soft light (keeps colours bright)", QPainter.CompositionMode.CompositionMode_SoftLight),
    ("overlay", "Overlay (strong contrast)", QPainter.CompositionMode.CompositionMode_Overlay),
]
BLEND_MODES = {k: m for k, _l, m in BLENDS}

PRESETS = {  # strength -> opacity
    "subtle": 0.30, "normal": 0.50, "strong": 0.70, "very strong": 0.90,
}

DEFAULTS = {
    "strength": "normal",
    "opacity": 0.50,
    "auto_z": True,          # adapt exaggeration to the terrain
    "z": 2.0,                # manual vertical exaggeration
    "multi": True,           # multidirectional light (soft, RAS Mapper-like)
    "azimuth": 315.0,
    "altitude": 45.0,
    "blend": "multiply",
    "smooth": True,          # (derived) any smoothing at all
    "smooth_level": 2,       # 0 off · 1 bilinear · 2 cubic · 3–10 cubic + relief blur
}

SMOOTH_MAX = 10


def smooth_label(level):
    level = int(level)
    if level <= 0:
        return "Off – sharp cells"
    if level == 1:
        return "Light"
    if level == 2:
        return "Smooth"
    n = 2 * (level - 2) + 1
    word = "Soft relief" if level <= 5 else "Very soft relief"
    return "%s (blur %d×%d cells)" % (word, n, n)


def load():
    s = QgsSettings()
    o = dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        t = type(v)
        try:
            o[k] = s.value(SETTINGS + k, v, type=t)
        except Exception:
            o[k] = v
    if o["blend"] not in BLEND_MODES:
        o["blend"] = "multiply"
    if s.value(SETTINGS + "smooth_level") is None:      # settings from 2.7/2.8
        o["smooth_level"] = 2 if o["smooth"] else 0
    o["smooth_level"] = int(min(SMOOTH_MAX, max(0, int(o["smooth_level"]))))
    o["smooth"] = o["smooth_level"] > 0
    o["opacity"] = min(1.0, max(0.05, float(o["opacity"])))
    o["z"] = min(100.0, max(0.1, float(o["z"])))
    o["altitude"] = min(90.0, max(5.0, float(o["altitude"])))
    o["azimuth"] = float(o["azimuth"]) % 360.0
    return o


def save(opts):
    opts["smooth"] = int(opts.get("smooth_level", 0)) > 0
    s = QgsSettings()
    for k in DEFAULTS:
        s.setValue(SETTINGS + k, opts[k])


# --------------------------------------------------------------------------
# terrain analysis
# --------------------------------------------------------------------------
def _metres_per_unit(layer):
    """(metres per horizontal unit in x, in y)."""
    if not layer.crs().isGeographic():
        return 1.0, 1.0
    lat = layer.extent().center().y()
    return 111320.0 * max(math.cos(math.radians(lat)), 0.2), 110570.0


def _sample(layer, size=400):
    """Down-sampled DEM as a numpy array (NaN for nodata), plus cell size in metres."""
    import numpy as np
    prov = layer.dataProvider()
    ext = layer.extent()
    if ext.isEmpty():
        return None, 0, 0
    aspect = ext.height() / ext.width()
    cols = max(16, min(size, layer.width()))
    rows = max(16, min(int(cols * aspect) or 16, layer.height()))
    block = prov.block(1, ext, cols, rows)
    if not block.isValid():
        return None, 0, 0
    dtypes = {1: "uint8", 2: "uint16", 3: "int16", 4: "uint32", 5: "int32",
              6: "float32", 7: "float64"}
    dt = dtypes.get(int(block.dataType()))
    data = None
    if dt:
        try:
            raw = bytes(block.data())
            data = np.frombuffer(raw, dtype=dt, count=rows * cols).reshape(rows, cols)
            data = data.astype("float64")
            if block.hasNoDataValue():
                data[data == block.noDataValue()] = np.nan
        except Exception:
            data = None
    if data is None:            # slow but safe fallback
        data = np.empty((rows, cols), dtype="float64")
        for r in range(rows):
            for c in range(cols):
                data[r, c] = np.nan if block.isNoData(r, c) else block.value(r, c)
    mx, my = _metres_per_unit(layer)
    return data, ext.width() / cols * mx, ext.height() / rows * my


def terrain_stats(layer):
    """dict(min, max, relief, mean_slope_deg) from a quick sample – or None."""
    try:
        import numpy as np
        data, dx, dy = _sample(layer, 300)
        if data is None or not np.isfinite(data).any():
            return None
        gy, gx = np.gradient(data, dy, dx)
        slope = np.hypot(gx, gy)
        slope = slope[np.isfinite(slope)]
        mean_tan = float(np.mean(slope)) if slope.size else 0.0
        lo, hi = float(np.nanmin(data)), float(np.nanmax(data))
        return {"min": lo, "max": hi, "relief": hi - lo,
                "mean_slope_deg": math.degrees(math.atan(mean_tan)), "mean_tan": mean_tan}
    except Exception:
        return None


def auto_exaggeration(layer, stats=None):
    """Exaggeration that gives the whole-DEM view a mean slope of ~8°.

    Flat floodplains (mean slope < 1°) get up to 25×, hilly terrain stays ~1–2×."""
    stats = stats or terrain_stats(layer)
    if not stats or stats["mean_tan"] <= 0:
        return 2.0
    z = math.tan(math.radians(8.0)) / stats["mean_tan"]
    return round(min(25.0, max(1.0, z)), 1)


def exaggeration(layer, opts, stats=None):
    return auto_exaggeration(layer, stats) if opts["auto_z"] else float(opts["z"])


def renderer_z(layer, exag):
    """Exaggeration -> QgsHillshadeRenderer z factor (handles DEMs in degrees)."""
    mx, _my = _metres_per_unit(layer)
    return exag / mx if layer.crs().isGeographic() else exag


# --------------------------------------------------------------------------
# live layer
# --------------------------------------------------------------------------
def configure_live(hs_layer, dem_layer, opts, exag=None):
    """Apply *opts* to an existing live hillshade layer (renderer + blending)."""
    from qgis.core import QgsHillshadeRenderer
    if exag is None:
        exag = exaggeration(dem_layer, opts)
    # style parts first, renderer last: QGIS snapshots the layer style on renderer
    # change (Layer Styling undo) and would otherwise restore stale settings
    level = int(opts.get("smooth_level", 2))
    # levels 3+ shade a blurred copy of the DEM (virtual file, nothing computed)
    want = smoothed_source(dem_layer, level) or dem_layer.source()
    if hs_layer.source() != want:
        hs_layer.setDataSource(want, hs_layer.name(), dem_layer.providerType()
                               if want == dem_layer.source() else "gdal")
    smooth(hs_layer, level)
    smooth(dem_layer, level)
    hs_layer.setBlendMode(BLEND_MODES.get(opts["blend"], QPainter.CompositionMode.CompositionMode_Multiply))
    hs_layer.setCustomProperty("symbol_gallery/hillshade_exaggeration", exag)
    r = QgsHillshadeRenderer(hs_layer.dataProvider(), 1, float(opts["azimuth"]),
                             float(opts["altitude"]))
    r.setZFactor(renderer_z(dem_layer, exag))
    try:
        r.setMultiDirectional(bool(opts["multi"]))
    except AttributeError:  # pragma: no cover
        pass
    r.setOpacity(float(opts["opacity"]))
    hs_layer.setRenderer(r)
    hs_layer.triggerRepaint()
    return exag


# --------------------------------------------------------------------------
# baked shaded-relief GeoTIFF
# --------------------------------------------------------------------------
def _blend(c, h, mode, o):
    """c, h: float arrays 0..1 (colour, hillshade) -> blended 0..1."""
    import numpy as np
    if mode == "softlight":
        b = (1 - 2 * h) * c * c + 2 * h * c
    elif mode == "overlay":
        b = np.where(c < 0.5, 2 * c * h, 1 - 2 * (1 - c) * (1 - h))
    else:
        b = c * h
    return c * (1 - o) + b * o


def colour_lut(renderer, n=1024):
    """(lo, hi, lut[n,4] uint8) from a single-band pseudocolor renderer."""
    import numpy as np
    shader = renderer.shader().rasterShaderFunction()
    lo, hi = renderer.classificationMin(), renderer.classificationMax()
    lut = np.zeros((n, 4), dtype="uint8")
    for i in range(n):
        v = lo + (hi - lo) * i / (n - 1.0)
        res = shader.shade(v)
        if res and res[0]:
            lut[i] = res[1:5]
    return lo, hi, lut


def bake(dem_path, out_path, lo, hi, lut, opts, exag, metres_x, feedback=None):
    """Write an RGBA GeoTIFF of colour × hillshade.  Processes in row blocks so a
    country-size 30 m DEM never has to fit in memory.  Returns out_path."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    src = gdal.Open(dem_path)
    band = src.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    tmp = out_path + ".hs_tmp.tif"
    dem_opts = dict(format="GTiff", computeEdges=True, zFactor=float(exag),
                    creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    if src.GetSpatialRef() is not None and src.GetSpatialRef().IsGeographic():
        dem_opts["scale"] = float(metres_x)
    if opts["multi"]:
        dem_opts["multiDirectional"] = True
    else:
        dem_opts["azimuth"] = float(opts["azimuth"])
        dem_opts["altitude"] = float(opts["altitude"])
    if feedback:
        feedback(2, "Computing hillshade…")
    shade_src = src
    if opts.get("smooth_source"):
        shade_src = gdal.Open(opts["smooth_source"])
    gdal.DEMProcessing(tmp, shade_src, "hillshade", **dem_opts)
    hs = gdal.Open(tmp)
    hsb = hs.GetRasterBand(1)

    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(out_path, src.RasterXSize, src.RasterYSize, 4, gdal.GDT_Byte,
                     ["COMPRESS=DEFLATE", "TILED=YES", "PHOTOMETRIC=RGB", "ALPHA=YES",
                      "BIGTIFF=IF_SAFER"])
    out.SetGeoTransform(src.GetGeoTransform())
    out.SetProjection(src.GetProjection())
    n = lut.shape[0]
    span = (hi - lo) or 1.0
    o = float(opts["opacity"])
    mode = opts["blend"]
    rows = 512
    h_total = src.RasterYSize
    for y in range(0, h_total, rows):
        ny = min(rows, h_total - y)
        z = band.ReadAsArray(0, y, src.RasterXSize, ny).astype("float64")
        shade = hsb.ReadAsArray(0, y, src.RasterXSize, ny).astype("float64") / 255.0
        valid = np.isfinite(z)
        if nodata is not None:
            valid &= z != nodata
        idx = np.clip(((z - lo) / span * (n - 1)), 0, n - 1)
        idx = np.where(valid, idx, 0).astype("int32")
        rgba = lut[idx]
        # gdaldem marks edge/nodata hillshade as 0: treat as neutral light there
        shade = np.where(valid & (shade > 0), shade, 1.0)
        for b in range(3):
            c = rgba[..., b] / 255.0
            v = _blend(c, shade, mode, o)
            out.GetRasterBand(b + 1).WriteArray(
                np.clip(v * 255.0 + 0.5, 0, 255).astype("uint8"), 0, y)
        alpha = np.where(valid, rgba[..., 3], 0).astype("uint8")
        out.GetRasterBand(4).WriteArray(alpha, 0, y)
        if feedback:
            feedback(5 + 93.0 * (y + ny) / h_total, None)
    out.FlushCache()
    out = None
    hs = None
    try:
        os.remove(tmp)
    except OSError:
        pass
    return out_path


# --------------------------------------------------------------------------
# smooth display (no blocky DEM cells when zoomed in)
# --------------------------------------------------------------------------
def smooth(layer, level=2):
    """Display smoothing: 0 off (nearest), 1 bilinear, 2+ cubic when zoomed in.

    Uses QGIS's resample *filter* (works on the rendered image), which is safe at
    nodata edges – provider-level resampling would blend -9999 cells into the
    real elevations and draw dark rims along the DEM border.  *level* may also be
    a bool (True = cubic)."""
    if level is True:
        level = 2
    level = int(level or 0)
    try:
        from qgis.core import Qgis, QgsCubicRasterResampler, QgsBilinearRasterResampler
        try:
            if layer.dataProvider().isProviderResamplingEnabled():
                layer.dataProvider().enableProviderResampling(False)
            layer.setResamplingStage(Qgis.RasterResamplingStage.ResampleFilter)
        except AttributeError:  # pragma: no cover - QGIS < 3.16
            pass
        f = layer.resampleFilter()
        if level <= 0:
            f.setZoomedInResampler(None)
            f.setZoomedOutResampler(None)
        elif level == 1:
            f.setZoomedInResampler(QgsBilinearRasterResampler())
            f.setZoomedOutResampler(QgsBilinearRasterResampler())
        else:
            f.setZoomedInResampler(QgsCubicRasterResampler())
            f.setZoomedOutResampler(QgsBilinearRasterResampler())
        f.setMaxOversampling(2.0)
        layer.triggerRepaint()
        return True
    except Exception:
        return False


def _local_file(layer):
    try:
        src = layer.source().split("|")[0]
    except Exception:
        return None
    if layer.providerType() != "gdal" or not os.path.isfile(src):
        return None
    return src


def smoothed_source(dem_layer, level):
    """Path of a Gaussian-blurred virtual copy of the DEM for levels 3–10, else None.

    A tiny GDAL VRT with a KernelFilteredSource: the blur happens on the fly
    while drawing, nodata cells are respected, and nothing big is written."""
    level = int(level or 0)
    if level < 3:
        return None
    src = _local_file(dem_layer)
    if src is None:
        return None
    n = 2 * (level - 2) + 1                       # 3×3 … 17×17 cells
    import hashlib
    from qgis.core import QgsApplication
    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "symbol_gallery", "smooth")
    key = hashlib.md5(("%s|%s" % (src, os.path.getmtime(src))).encode("utf-8"), usedforsecurity=False).hexdigest()
    path = os.path.join(folder, "%s_k%d.vrt" % (key[:16], n))
    if os.path.exists(path):
        return path
    try:
        import re
        from osgeo import gdal
        os.makedirs(folder, exist_ok=True)
        gdal.UseExceptions()
        tmp = path + ".tmp.vrt"
        gdal.Translate(tmp, src, format="VRT", bandList=[1])
        xml = open(tmp, encoding="utf-8").read()
        os.remove(tmp)
        sigma = max(0.6, n / 4.0)
        c = n // 2
        coefs = []
        for y in range(n):
            for x in range(n):
                coefs.append(math.exp(-((x - c) ** 2 + (y - c) ** 2) / (2 * sigma * sigma)))
        total = sum(coefs)
        coef_txt = " ".join("%.7f" % (v / total) for v in coefs)
        m = re.search(r"<(SimpleSource|ComplexSource)[^>]*>(.*?)</\1>", xml, re.S)
        if m is None:
            return None
        inner = m.group(2)
        # relative paths inside the VRT would point into the cache folder
        inner = re.sub(r'<SourceFilename relativeToVRT="1">(.*?)</SourceFilename>',
                       lambda mm: '<SourceFilename relativeToVRT="0">%s</SourceFilename>'
                       % src, inner)
        new = ('<KernelFilteredSource>%s<Kernel normalized="1"><Size>%d</Size>'
               '<Coefs>%s</Coefs></Kernel></KernelFilteredSource>' % (inner, n, coef_txt))
        xml = xml[:m.start()] + new + xml[m.end():]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
        return path
    except Exception:
        return None

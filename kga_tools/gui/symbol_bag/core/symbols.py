# -*- coding: utf-8 -*-
"""
Symbol Bag – symbol serialization, previews and resource handling.

Vector symbols are stored using QGIS's own symbol XML
(QgsSymbolLayerUtils.saveSymbol / loadSymbol) so every property,
including multilayer symbols, data-defined properties, sub-symbols,
SVG / font markers and pattern fills, round-trips exactly.

Raster styles are stored as a QGIS named style document (the same
format as a .qml file) limited to the Symbology category, so every
raster renderer QGIS supports (pseudocolor, hillshade, paletted,
multiband, gray, contour ...) is preserved.
"""

from contextlib import suppress
import base64
import math
import os
import tempfile

from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QPointF, QRectF
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPixmap, QLinearGradient, QPainterPath, QPen
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsMapLayer,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsPalettedRasterRenderer,
    QgsRasterLayer,
    QgsRasterShader,
    QgsReadWriteContext,
    QgsRectangle,
    QgsSymbol,
    QgsSymbolLayerUtils,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ....core.compat import scoped
from .compat import limits_cumulative_cut, stats_min_max
from .log import log_warning
from .storage import KIND_FILL, KIND_LINE, KIND_MARKER, KIND_RASTER

STRETCH_ATTR = "symbolbag-stretch"
BLEND_ATTR = "symbolbag-blend"

# ----------------------------------------------------------------------
# enum compatibility (QGIS 3.28 … 3.x and QGIS 4 / Qt6)
# ----------------------------------------------------------------------
try:
    _ST = Qgis.SymbolType
    SYMBOL_TYPE_TO_KIND = {_ST.Marker: KIND_MARKER, _ST.Line: KIND_LINE, _ST.Fill: KIND_FILL}
except AttributeError:  # pragma: no cover - very old QGIS
    SYMBOL_TYPE_TO_KIND = {scoped(QgsSymbol, 'SymbolType', 'Marker'): KIND_MARKER, scoped(QgsSymbol, 'SymbolType', 'Line'): KIND_LINE,
                           scoped(QgsSymbol, 'SymbolType', 'Fill'): KIND_FILL}

try:
    _GT = Qgis.GeometryType
    _GEOM_TO_KIND = {_GT.Point: KIND_MARKER, _GT.Line: KIND_LINE, _GT.Polygon: KIND_FILL}
except AttributeError:  # QGIS < 3.30
    _GEOM_TO_KIND = {scoped(QgsWkbTypes, 'GeometryType', 'PointGeometry'): KIND_MARKER,
                     scoped(QgsWkbTypes, 'GeometryType', 'LineGeometry'): KIND_LINE,
                     scoped(QgsWkbTypes, 'GeometryType', 'PolygonGeometry'): KIND_FILL}

SYMBOLOGY = QgsMapLayer.StyleCategory.Symbology


def symbol_kind(symbol):
    return SYMBOL_TYPE_TO_KIND.get(symbol.type()) if symbol is not None else None


def layer_kind(layer):
    """Return the Symbol Bag kind a layer accepts, or None."""
    if isinstance(layer, QgsRasterLayer):
        return KIND_RASTER
    if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
        return _GEOM_TO_KIND.get(layer.geometryType())
    return None


# ----------------------------------------------------------------------
# vector symbol XML
# ----------------------------------------------------------------------
def symbol_to_xml(symbol, embed=True):
    """Serialize a QgsSymbol to an XML string using QGIS's native format."""
    sym = symbol.clone()
    if embed:
        embed_resources(sym)
    doc = QDomDocument("symbolbag")
    elem = QgsSymbolLayerUtils.saveSymbol("symbol", sym, doc, QgsReadWriteContext())
    doc.appendChild(elem)
    return doc.toString()


def symbol_from_xml(xml):
    """Rebuild a QgsSymbol from XML. Returns None when the XML is invalid."""
    doc = QDomDocument()
    ok = doc.setContent(xml)
    if isinstance(ok, tuple):
        ok = ok[0]
    if not ok:
        return None
    elem = doc.documentElement()
    if elem.tagName() != "symbol":
        elem = elem.firstChildElement("symbol")
    if elem.isNull():
        return None
    return QgsSymbolLayerUtils.loadSymbol(elem, QgsReadWriteContext())


# ----------------------------------------------------------------------
# external resources (SVG / raster images)
# ----------------------------------------------------------------------
def _iter_symbol_layers(symbol):
    if symbol is None:
        return
    for i in range(symbol.symbolLayerCount()):
        sl = symbol.symbolLayer(i)
        yield sl
        sub = sl.subSymbol()
        if sub is not None:
            for inner in _iter_symbol_layers(sub):
                yield inner


_PATH_ACCESSORS = (
    # (getter, setter) pairs for symbol layer types that reference files
    ("path", "setPath"),                  # SVG marker, raster marker
    ("svgFilePath", "setSvgFilePath"),    # SVG fill
    ("imageFilePath", "setImageFilePath"),  # raster fill
)


def _resource_paths(sl):
    for getter, setter in _PATH_ACCESSORS:
        if hasattr(sl, getter) and hasattr(sl, setter):
            try:
                value = getattr(sl, getter)()
            except TypeError:
                continue
            if isinstance(value, str):
                yield getter, setter, value


def _is_builtin_svg(path):
    norm = os.path.normcase(os.path.abspath(path))
    for folder in QgsApplication.svgPaths():
        folder = os.path.normcase(os.path.abspath(folder))
        if norm.startswith(folder + os.sep):
            return True
    return False


def resolve_resource(path):
    """Return an absolute filesystem path for an SVG name, or None."""
    if not path or path.startswith("base64:") or "://" in path:
        return None
    if os.path.isabs(path) and os.path.exists(path):
        return path
    try:
        from qgis.core import QgsPathResolver
        resolved = QgsSymbolLayerUtils.svgSymbolNameToPath(path, QgsPathResolver())
    except Exception:
        resolved = path
    return resolved if resolved and os.path.exists(resolved) else None


def embed_resources(symbol, max_bytes=2 * 1024 * 1024):
    """Embed user SVG / image files as base64 so the symbol is self-contained.

    Built-in QGIS SVGs are left as references (they exist on every install).
    Returns the number of embedded files.
    """
    count = 0
    for sl in _iter_symbol_layers(symbol):
        for _getter, setter, value in _resource_paths(sl):
            if not value or value.startswith("base64:") or "://" in value:
                continue
            real = resolve_resource(value)
            if real is None or _is_builtin_svg(real):
                continue
            try:
                if os.path.getsize(real) > max_bytes:
                    continue
                with open(real, "rb") as fh:
                    data = base64.b64encode(fh.read()).decode("ascii")
                getattr(sl, setter)("base64:" + data)
                count += 1
            except (OSError, TypeError) as e:
                log_warning("Could not embed resource {}: {}".format(real, e))
    return count


def missing_resources(symbol):
    """List of referenced SVG / image files that cannot be found."""
    missing = []
    for sl in _iter_symbol_layers(symbol):
        for _g, _s, value in _resource_paths(sl):
            if not value or value.startswith("base64:") or "://" in value:
                continue
            if resolve_resource(value) is None:
                missing.append(value)
    return missing


def svg_marker_symbol(svg_path, size=6.0, embed=True):
    from qgis.core import QgsMarkerSymbol, QgsSvgMarkerSymbolLayer
    sl = QgsSvgMarkerSymbolLayer(svg_path, size)
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, sl)
    if embed:
        embed_resources(sym)
    return sym


def svg_fill_symbol(svg_path, width=8.0, embed=True):
    from qgis.core import QgsFillSymbol, QgsSVGFillSymbolLayer
    sl = QgsSVGFillSymbolLayer(svg_path, width)
    sym = QgsFillSymbol()
    sym.changeSymbolLayer(0, sl)
    if embed:
        embed_resources(sym)
    return sym


# ----------------------------------------------------------------------
# raster styles
# ----------------------------------------------------------------------
def _parse(xml):
    doc = QDomDocument()
    ok = doc.setContent(xml)
    if isinstance(ok, tuple):
        ok = ok[0]
    return doc if ok else None


def raster_style_from_layer(layer, stretch=False):
    """Capture the symbology of a raster layer as an XML style document."""
    doc = QDomDocument("qgis")
    err = layer.exportNamedStyle(doc, QgsReadWriteContext(), SYMBOLOGY)
    if err:
        raise ValueError(err)
    root = doc.documentElement()
    root.setAttribute(STRETCH_ATTR, "1" if stretch else "0")
    return doc.toString()


def raster_style_info(xml):
    """Return dict(renderer=type, stretch=bool, bands=int) for a raster style."""
    info = {"renderer": "", "stretch": False}
    doc = _parse(xml)
    if doc is None:
        return info
    root = doc.documentElement()
    info["stretch"] = root.attribute(STRETCH_ATTR, "0") == "1"
    rr = root.firstChildElement("pipe").firstChildElement("rasterrenderer")
    if rr.isNull():
        nodes = doc.elementsByTagName("rasterrenderer")
        if nodes.count():
            rr = nodes.at(0).toElement()
    info["renderer"] = rr.attribute("type", "") if not rr.isNull() else ""
    return info


def is_raster_style_xml(xml):
    return bool(raster_style_info(xml)["renderer"])


def set_raster_stretch_flag(xml, stretch):
    doc = _parse(xml)
    if doc is None:
        return xml
    doc.documentElement().setAttribute(STRETCH_ATTR, "1" if stretch else "0")
    return doc.toString()


RENDERER_LABELS = {
    "singlebandpseudocolor": "Pseudocolor",
    "singlebandgray": "Grayscale",
    "multibandcolor": "Multiband color",
    "paletted": "Paletted / categorized",
    "hillshade": "Hillshade",
    "contour": "Contours",
    "singlecolor": "Single color",
}


def _band_count(layer):
    try:
        return layer.bandCount()
    except Exception:
        return 1


def _fix_bands(layer, renderer):
    """Remap band numbers that don't exist on the target layer.

    Returns a warning string or '' – raises ValueError if impossible.
    """
    count = _band_count(layer)
    try:
        used = list(renderer.usesBands())
    except Exception:
        used = []
    if all(1 <= b <= count for b in used if b > 0):
        return ""
    t = renderer.type()
    if t == "multibandcolor":
        if count >= 3:
            renderer.setRedBand(min(renderer.redBand(), count) or 1)
            renderer.setGreenBand(min(renderer.greenBand(), count) or 1)
            renderer.setBlueBand(min(renderer.blueBand(), count) or 1)
            return "Some bands did not exist and were remapped."
        raise ValueError("This multiband style needs at least 3 bands; the layer has {}.".format(count))
    for setter in ("setBand", "setGrayBand", "setInputBand"):
        if hasattr(renderer, setter):
            getattr(renderer, setter)(1)
            return "The style's band did not exist on this layer, band 1 is used instead."
    raise ValueError("The style uses band(s) {} but the layer only has {}.".format(used, count))


def _band_minmax(layer, band, extent=None):
    provider = layer.dataProvider()
    stats = provider.bandStatistics(band, stats_min_max(), extent or QgsRectangle(), 250000)
    lo, hi = stats.minimumValue, stats.maximumValue
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return None
    return lo, hi


def rescale_pseudocolor(renderer, new_min, new_max):
    """Linearly stretch a pseudocolor ramp to a new value range."""
    shader = renderer.shader()
    fn = shader.rasterShaderFunction() if shader else None
    if fn is None:
        return False
    items = fn.colorRampItemList()
    values = [it.value for it in items if math.isfinite(it.value)]
    if len(values) < 2:
        return False
    old_min, old_max = min(values), max(values)
    if old_max == old_min:
        return False
    new_items = []
    scale = (new_max - new_min) / (old_max - old_min)
    for it in items:
        v = it.value
        if math.isfinite(v):
            nv = new_min + (v - old_min) * scale
            label = it.label
            with suppress(TypeError, ValueError):
                float(label)
                label = "{:.6g}".format(nv)
            new_items.append(QgsColorRampShader.ColorRampItem(nv, it.color, label))
        else:
            new_items.append(QgsColorRampShader.ColorRampItem(v, it.color, it.label))
    new_fn = QgsColorRampShader(new_min, new_max)
    new_fn.setColorRampType(fn.colorRampType())
    with suppress(Exception):
        new_fn.setClassificationMode(fn.classificationMode())
    new_fn.setClip(fn.clip())
    if fn.sourceColorRamp() is not None:
        new_fn.setSourceColorRamp(fn.sourceColorRamp().clone())
    new_fn.setColorRampItemList(new_items)
    new_shader = QgsRasterShader(new_min, new_max)
    new_shader.setRasterShaderFunction(new_fn)
    renderer.setShader(new_shader)
    renderer.setClassificationMin(new_min)
    renderer.setClassificationMax(new_max)
    return True


def apply_raster_style(layer, xml, stretch=None, extent=None):
    """Apply a raster style to a layer. Returns a list of warnings.

    Only symbology is touched – data source, CRS, name etc. are preserved.
    """
    doc = _parse(xml)
    if doc is None:
        raise ValueError("The stored raster style is not valid XML.")
    info = raster_style_info(xml)
    if stretch is None:
        stretch = info["stretch"]
    ok, err = layer.importNamedStyle(doc, SYMBOLOGY)
    if not ok:
        raise ValueError(err or "QGIS could not read this raster style.")
    warnings = []
    renderer = layer.renderer()
    if renderer is None:
        raise ValueError("The style did not produce a raster renderer.")
    msg = _fix_bands(layer, renderer)
    if msg:
        warnings.append(msg)
    if stretch:
        t = renderer.type()
        if t == "singlebandpseudocolor":
            mm = _band_minmax(layer, renderer.band(), extent)
            if mm:
                rescale_pseudocolor(renderer, mm[0], mm[1])
        elif t in ("singlebandgray", "multibandcolor"):
            layer.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                                         limits_cumulative_cut(),
                                         extent or QgsRectangle())
    return warnings


# ----------------------------------------------------------------------
# synthetic preview raster
# ----------------------------------------------------------------------
_SAMPLE_PATHS = {}


def _terrain(x, y):
    hills = [(0.30, 0.35, 0.20, 900), (0.72, 0.30, 0.16, 650), (0.55, 0.78, 0.22, 1100),
             (0.12, 0.82, 0.12, 400), (0.88, 0.75, 0.13, 520)]
    z = 60.0 + 40.0 * x
    for cx, cy, r, h in hills:
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        z += h * math.exp(-d2 / (2 * r * r))
    # meandering river valley
    z -= 260.0 * math.exp(-((y - 0.55 - 0.12 * math.sin(x * 6.0)) ** 2) / 0.004)
    return max(z, 0.0)


def sample_raster_path(kind="dem"):
    """Small synthetic GeoTIFFs used for raster previews and for editing
    raster styles when no raster layer is selected.

    kind 'dem'   : band 1 elevation (single band styles)
    kind 'image' : 4 bands Blue, Green, Red, NIR (multiband styles)
    """
    path = _SAMPLE_PATHS.get(kind)
    if path and os.path.exists(path):
        return path
    import struct
    from osgeo import gdal, osr
    gdal.UseExceptions()
    size = 128
    folder = tempfile.mkdtemp(prefix="symbolbag_")
    path = os.path.join(folder, "sample_{}.tif".format(kind))
    nb = 1 if kind == "dem" else 4
    ds = gdal.GetDriverByName("GTiff").Create(path, size, size, nb, gdal.GDT_Float32)
    ds.SetGeoTransform((500000, 30, 0, 1300000, 0, -30))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32648)
    ds.SetProjection(srs.ExportToWkt())
    bands = [[] for _ in range(nb)]
    for row in range(size):
        y = row / (size - 1.0)
        for col in range(size):
            x = col / (size - 1.0)
            z = _terrain(x, y)
            if kind == "dem":
                bands[0].append(z)
                continue
            water = z < 25
            veg = max(0.0, min(1.0, 1.0 - abs(z - 420.0) / 650.0))
            bare = 1.0 - veg
            if water:
                b, g, r, n = 0.08, 0.07, 0.04, 0.02
            else:
                b = 0.04 + 0.08 * bare
                g = 0.08 + 0.08 * veg + 0.12 * bare
                r = 0.03 + 0.20 * bare
                n = 0.18 + 0.30 * veg
            for i, v in enumerate((b, g, r, n)):
                bands[i].append(v)
    for i, values in enumerate(bands, start=1):
        band = ds.GetRasterBand(i)
        band.WriteRaster(0, 0, size, size, struct.pack("{}f".format(len(values)), *values))
        band.SetNoDataValue(-9999)
    ds.FlushCache()
    ds = None
    _SAMPLE_PATHS[kind] = path
    return path


def sample_raster_layer(name="Symbol Bag sample terrain", kind="dem"):
    layer = QgsRasterLayer(sample_raster_path(kind), name, "gdal")
    return layer if layer.isValid() else None


# ----------------------------------------------------------------------
# previews
# ----------------------------------------------------------------------
def image_to_png(image):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def png_to_pixmap(data):
    pm = QPixmap()
    pm.loadFromData(data, "PNG")
    return pm


def vector_preview(symbol, size):
    """QPixmap preview of a vector symbol (native QGIS rendering)."""
    if symbol is None:
        return broken_preview(size)
    pad = max(2, size // 10)
    kind = symbol_kind(symbol)
    if kind == KIND_MARKER:
        # Enlarge small markers so they read well on a card (preview only)
        sym = symbol.clone()
        with suppress(Exception):
            current = sym.size()
            target_mm = (size * 0.5) / 3.78
            if 0 < current < target_mm:
                sym.setSize(target_mm)
        return QgsSymbolLayerUtils.symbolPreviewPixmap(sym, QSize(size, size), pad)
    if kind == KIND_LINE:
        sym = symbol.clone()
        with suppress(Exception):
            if 0 < sym.width() < 0.5:
                sym.setWidth(0.5)
        return QgsSymbolLayerUtils.symbolPreviewPixmap(sym, QSize(size, size), pad)
    return QgsSymbolLayerUtils.symbolPreviewPixmap(symbol, QSize(size, size), pad)


def broken_preview(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(200, 60, 60), max(2, size // 16))
    p.setPen(pen)
    m = size * 0.28
    p.drawLine(QPointF(m, m), QPointF(size - m, size - m))
    p.drawLine(QPointF(size - m, m), QPointF(m, size - m))
    p.end()
    return pm


def _paletted_preview(renderer, size):
    classes = renderer.classes()
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    if not classes:
        return img
    p = QPainter(img)
    n = len(classes)
    cols = int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / float(cols)))
    cw, ch = size / float(cols), size / float(rows)
    for i, c in enumerate(classes):
        r, col = divmod(i, cols)
        p.fillRect(QRectF(col * cw, r * ch, cw + 0.5, ch + 0.5), c.color)
    p.end()
    return img


def raster_preview_image(xml, size):
    """Render a raster style on the synthetic terrain sample."""
    kind = "image" if raster_style_info(xml)["renderer"] == "multibandcolor" else "dem"
    layer = sample_raster_layer(kind=kind)
    if layer is None:
        raise ValueError("Preview raster unavailable")
    apply_raster_style(layer, xml, stretch=False)
    renderer = layer.renderer()
    if renderer.type() == "paletted" and isinstance(renderer, QgsPalettedRasterRenderer):
        return _paletted_preview(renderer, size)
    # Preview only: fit absolute ramps (slope, NDVI ...) to the sample data so
    # the full colour range is visible.
    if renderer.type() == "singlebandpseudocolor":
        if renderer.band() != 1:
            renderer.setBand(1)
        mm = _band_minmax(layer, 1)
        if mm:
            rescale_pseudocolor(renderer, mm[0], mm[1])
    elif renderer.type() == "singlebandgray":
        layer.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                                     limits_cumulative_cut())
    elif renderer.type() == "multibandcolor":
        # common reflectance stretch keeps the colour balance of the sample image
        dp = layer.dataProvider()
        for band, setter in ((renderer.redBand(), renderer.setRedContrastEnhancement),
                             (renderer.greenBand(), renderer.setGreenContrastEnhancement),
                             (renderer.blueBand(), renderer.setBlueContrastEnhancement)):
            ce = QgsContrastEnhancement(dp.dataType(max(1, band)))
            ce.setContrastEnhancementAlgorithm(
                QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
            ce.setMinimumValue(0.0)
            ce.setMaximumValue(0.32)
            setter(ce)
    # render at the raster's own resolution (avoids nearest-neighbour stair
    # steps that hillshading turns into a grid), then scale the image smoothly
    native = layer.width()
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(layer.crs())
    settings.setExtent(layer.extent())
    settings.setOutputSize(QSize(native, native))
    settings.setBackgroundColor(QColor(0, 0, 0, 0))
    img = QImage(native, native, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.renderSynchronously()
    painter.end()
    if native != size:
        img = img.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img


def ramp_bar_preview(colors, size):
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, size, 0)
    n = max(1, len(colors) - 1)
    for i, c in enumerate(colors):
        grad.setColorAt(i / float(n), c)
    p.fillRect(0, 0, size, size, grad)
    p.end()
    return img


def rounded(pixmap, radius):
    out = QPixmap(pixmap.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, pixmap.width(), pixmap.height()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pixmap)
    p.end()
    return out

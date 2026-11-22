# -*- coding: utf-8 -*-
"""
Symbol Bag – the default starter library.

All symbols are built with native QGIS symbol layer classes, so they can
be edited with the regular QGIS symbol selector afterwards.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    Qgis,
    QgsArrowSymbolLayer,
    QgsColorRampShader,
    QgsFillSymbol,
    QgsFontMarkerSymbolLayer,
    QgsGradientColorRamp,
    QgsGradientFillSymbolLayer,
    QgsGradientStop,
    QgsHillshadeRenderer,
    QgsLinePatternFillSymbolLayer,
    QgsLineSymbol,
    QgsMarkerLineSymbolLayer,
    QgsMarkerSymbol,
    QgsMultiBandColorRenderer,
    QgsPointPatternFillSymbolLayer,
    QgsRasterShader,
    QgsShapeburstFillSymbolLayer,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsSvgMarkerSymbolLayer,
    QgsContrastEnhancement,
)
from qgis.PyQt.QtGui import QPainter

from .compat import shader_type
from .log import log_warning
from .storage import KIND_RASTER
from .symbols import raster_style_from_layer, sample_raster_layer, symbol_kind, symbol_to_xml

_SHAPE = Qgis.MarkerShape


def C(hex_or_rgb, alpha=255):
    c = QColor(hex_or_rgb)
    c.setAlpha(alpha)
    return c


# ----------------------------------------------------------------------
# point symbols
# ----------------------------------------------------------------------
def _marker(shape, fill, stroke="#232323", size=4.0, stroke_width=0.4):
    sl = QgsSimpleMarkerSymbolLayer(shape, size)
    sl.setColor(C(fill))
    sl.setStrokeColor(C(stroke))
    sl.setStrokeWidth(stroke_width)
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, sl)
    return sym


def _svg_marker(path, size, fill=None):
    sl = QgsSvgMarkerSymbolLayer(path, size)
    if fill:
        sl.setFillColor(C(fill))
        sl.setStrokeColor(C("#ffffff"))
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, sl)
    return sym


def _font_marker(char, color, size=6.0):
    sl = QgsFontMarkerSymbolLayer(QFont().defaultFamily(), char, size, C(color))
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, sl)
    return sym


def _halo_marker(shape, fill, size=4.0):
    """Two-layer marker: soft halo + solid core."""
    halo = QgsSimpleMarkerSymbolLayer(_SHAPE.Circle, size * 2.0)
    halo.setColor(C(fill, 70))
    halo.setStrokeStyle(Qt.PenStyle.NoPen)
    core = QgsSimpleMarkerSymbolLayer(shape, size)
    core.setColor(C(fill))
    core.setStrokeColor(C("#ffffff"))
    core.setStrokeWidth(0.5)
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, halo)
    sym.appendSymbolLayer(core)
    return sym


def point_symbols():
    out = [
        ("Circle", _marker(_SHAPE.Circle, "#2e86de"), "General", "circle, simple"),
        ("Square", _marker(_SHAPE.Square, "#10ac84"), "General", "square, simple"),
        ("Triangle", _marker(_SHAPE.Triangle, "#f39c12", size=4.6), "General", "triangle, simple"),
        ("Diamond", _marker(_SHAPE.Diamond, "#8e44ad", size=4.6), "General", "diamond, simple"),
        ("Star", _marker(_SHAPE.Star, "#f1c40f", "#7f6a00", size=5.6), "General", "star, simple"),
        ("Cross", _marker(_SHAPE.Cross2, "#c0392b", "#c0392b", size=4.0, stroke_width=0.8), "General", "cross, x"),
        ("Glow point", _halo_marker(_SHAPE.Circle, "#e74c3c"), "General", "halo, highlight, multilayer"),
        ("Sample site", _halo_marker(_SHAPE.Square, "#16a085", size=3.2), "General", "survey, field, multilayer"),
        ("Font star", _font_marker("★", "#e67e22"), "General", "font marker"),
    ]
    svgs = [
        ("Village", "gpsicons/house.svg", 6.0, "Administrative Boundaries", "settlement, svg"),
        ("City", "gpsicons/city_medium.svg", 7.0, "Administrative Boundaries", "settlement, svg"),
        ("Tree", "gpsicons/tree.svg", 6.0, "Agriculture", "vegetation, svg"),
        ("Anchor / port", "gpsicons/anchor.svg", 6.0, "Hydrology", "port, svg"),
        ("Airport", "gpsicons/plane.svg", 7.0, "Transportation", "airport, svg"),
        ("Fuel station", "gpsicons/gas.svg", 6.0, "Transportation", "poi, svg"),
        ("Survey point", "amenity/amenity_survey_point.svg", 6.0, "General", "survey, svg"),
    ]
    for name, path, size, coll, tags in svgs:
        out.append((name, _svg_marker(path, size, "#34495e"), coll, tags))
    return out


# ----------------------------------------------------------------------
# line symbols
# ----------------------------------------------------------------------
def _line(color, width=0.6, style=Qt.PenStyle.SolidLine, cap=Qt.PenCapStyle.RoundCap):
    sl = QgsSimpleLineSymbolLayer(C(color), width, style)
    sl.setPenCapStyle(cap)
    sl.setPenJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return sl


def _line_symbol(*layers):
    sym = QgsLineSymbol()
    sym.changeSymbolLayer(0, layers[0])
    for sl in layers[1:]:
        sym.appendSymbolLayer(sl)
    return sym


def _custom_dash(color, width, pattern):
    sl = _line(color, width, cap=Qt.PenCapStyle.FlatCap)
    sl.setUseCustomDashPattern(True)
    sl.setCustomDashVector(pattern)
    return sl


def line_symbols():
    rail_ticks = QgsMarkerLineSymbolLayer(True, 3.0)
    tick = QgsSimpleMarkerSymbolLayer(_SHAPE.Line, 2.2)
    tick.setStrokeColor(C("#2d2d2d"))
    tick.setStrokeWidth(0.4)
    tick_sym = QgsMarkerSymbol()
    tick_sym.changeSymbolLayer(0, tick)
    rail_ticks.setSubSymbol(tick_sym)

    arrow = QgsArrowSymbolLayer()
    arrow.setArrowWidth(0.8)
    arrow.setArrowStartWidth(0.8)
    arrow.setHeadLength(2.5)
    arrow.setHeadThickness(1.6)
    arrow.setIsCurved(True)
    arrow_fill = QgsFillSymbol.createSimple({"color": "#e74c3c", "outline_style": "no"})
    arrow.setSubSymbol(arrow_fill)

    flow = QgsMarkerLineSymbolLayer(True, 6.0)
    chevron = QgsSimpleMarkerSymbolLayer(_SHAPE.ArrowHeadFilled, 2.0)
    chevron.setColor(C("#1e6fb8"))
    chevron.setStrokeStyle(Qt.PenStyle.NoPen)
    chev_sym = QgsMarkerSymbol()
    chev_sym.changeSymbolLayer(0, chevron)
    flow.setSubSymbol(chev_sym)

    return [
        ("Solid line", _line_symbol(_line("#34495e", 0.6)), "General", "solid, simple"),
        ("Dashed line", _line_symbol(_line("#34495e", 0.6, Qt.PenStyle.DashLine, Qt.PenCapStyle.FlatCap)),
         "General", "dashed"),
        ("Dotted line", _line_symbol(_line("#34495e", 0.7, Qt.PenStyle.DotLine)), "General", "dotted"),
        ("Arrow", _line_symbol(arrow), "General", "arrow, direction, flow"),
        ("Highway", _line_symbol(_line("#b03a2e", 2.4), _line("#f8c471", 1.6)),
         "Transportation", "road, motorway, multilayer"),
        ("Primary road", _line_symbol(_line("#7f8c8d", 1.8), _line("#ffffff", 1.2)),
         "Transportation", "road, multilayer"),
        ("Track / trail", _line_symbol(_custom_dash("#8d6e63", 0.6, [3.0, 1.5])),
         "Transportation", "path, dashed"),
        ("Railway", _line_symbol(_line("#2d2d2d", 0.5), rail_ticks), "Transportation", "rail, marker line"),
        ("River", _line_symbol(_line("#4fa3e0", 0.9)), "Hydrology", "stream, water"),
        ("River with flow", _line_symbol(_line("#4fa3e0", 0.9), flow), "Hydrology", "stream, flow, marker line"),
        ("Canal", _line_symbol(_line("#1e6fb8", 1.2), _line("#aed6f1", 0.5)), "Hydrology", "canal, multilayer"),
        ("International boundary",
         _line_symbol(_line("#d7bde2", 2.6), _custom_dash("#6c3483", 0.6, [4.0, 1.2, 0.6, 1.2])),
         "Administrative Boundaries", "border, dash-dot, multilayer"),
        ("Province boundary", _line_symbol(_custom_dash("#7d3c98", 0.45, [3.0, 1.0, 0.6, 1.0, 0.6, 1.0])),
         "Administrative Boundaries", "border, dash-dot"),
        ("Contour", _line_symbol(_line("#a0522d", 0.25)), "Terrain & Elevation", "contour, elevation"),
    ]


# ----------------------------------------------------------------------
# polygon symbols
# ----------------------------------------------------------------------
def _simple_fill(fill, stroke, width=0.3, style=Qt.BrushStyle.SolidPattern, stroke_style=Qt.PenStyle.SolidLine):
    sl = QgsSimpleFillSymbolLayer(C(fill) if isinstance(fill, str) else fill, style,
                                  C(stroke) if isinstance(stroke, str) else stroke, stroke_style, width)
    return sl


def _fill_symbol(*layers):
    sym = QgsFillSymbol()
    sym.changeSymbolLayer(0, layers[0])
    for sl in layers[1:]:
        sym.appendSymbolLayer(sl)
    return sym


def _hatch(color, angle, distance=2.0, width=0.25):
    sl = QgsLinePatternFillSymbolLayer()
    sl.setLineAngle(angle)
    sl.setDistance(distance)
    line = QgsLineSymbol()
    line.changeSymbolLayer(0, _line(color, width))
    sl.setSubSymbol(line)
    return sl


def _dots(color, size=0.8, distance=3.0):
    sl = QgsPointPatternFillSymbolLayer()
    sl.setDistanceX(distance)
    sl.setDistanceY(distance)
    dot = QgsSimpleMarkerSymbolLayer(_SHAPE.Circle, size)
    dot.setColor(C(color))
    dot.setStrokeStyle(Qt.PenStyle.NoPen)
    m = QgsMarkerSymbol()
    m.changeSymbolLayer(0, dot)
    sl.setSubSymbol(m)
    return sl


def _outline(color, width, style=Qt.PenStyle.SolidLine):
    return _simple_fill(C("#000000", 0), color, width, Qt.BrushStyle.NoBrush, style)


def polygon_symbols():
    gradient = QgsGradientFillSymbolLayer(C("#f9e79f"), C("#d35400"))
    shapeburst = QgsShapeburstFillSymbolLayer(C("#2e86de"), C("#2e86de", 0))
    shapeburst.setMaxDistance(3.0)
    shapeburst.setUseWholeShape(False)
    forest_tree = QgsPointPatternFillSymbolLayer()
    forest_tree.setDistanceX(5.0)
    forest_tree.setDistanceY(5.0)
    forest_tree.setSubSymbol(_svg_marker("gpsicons/tree.svg", 3.0, "#1e8449"))
    return [
        ("Solid fill", _fill_symbol(_simple_fill("#7fb3d5", "#2e4053")), "General", "solid, simple"),
        ("Outline only", _fill_symbol(_outline("#c0392b", 0.6)), "General", "outline, no fill"),
        ("Hatch", _fill_symbol(_simple_fill("#fdfefe", "#566573"), _hatch("#566573", 45)),
         "General", "pattern, hatch, line pattern"),
        ("Cross-hatch", _fill_symbol(_hatch("#566573", 45), _hatch("#566573", 135), _outline("#566573", 0.3)),
         "General", "pattern, cross hatch"),
        ("Dot pattern", _fill_symbol(_simple_fill("#fef9e7", "#7d6608"), _dots("#7d6608")),
         "General", "pattern, point pattern"),
        ("Gradient", _fill_symbol(gradient, _outline("#a04000", 0.3)), "General", "gradient"),
        ("Inner glow", _fill_symbol(shapeburst, _outline("#1b4f72", 0.4)), "General", "shapeburst, glow"),
        ("Water body", _fill_symbol(_simple_fill("#aed6f1", "#2e86c1", 0.4)), "Hydrology", "lake, water"),
        ("Wetland", _fill_symbol(_simple_fill("#d6eaf8", "#5499c7", 0.3), _hatch("#5499c7", 0, 2.5, 0.2)),
         "Hydrology", "marsh, swamp, pattern"),
        ("Forest", _fill_symbol(_simple_fill("#a9dfbf", "#1e8449", 0.3), forest_tree),
         "Agriculture", "vegetation, forest, svg pattern"),
        ("Rice paddy", _fill_symbol(_simple_fill("#f9e79f", "#b7950b", 0.3), _hatch("#7dcea0", 90, 1.6, 0.3)),
         "Agriculture", "crop, paddy, field"),
        ("Cropland", _fill_symbol(_simple_fill("#fad7a0", "#ca6f1e", 0.3), _dots("#ca6f1e", 0.5, 2.0)),
         "Agriculture", "crop, field"),
        ("Urban area", _fill_symbol(_simple_fill("#d5d8dc", "#7b7d7d", 0.3)), "General", "built-up, urban"),
        ("Province", _fill_symbol(_simple_fill(C("#f5eef8", 160), C("#6c3483"), 0.6,
                                               stroke_style=Qt.PenStyle.DashDotLine)),
         "Administrative Boundaries", "admin, province"),
        ("Study area", _fill_symbol(_outline("#e74c3c", 1.0, Qt.PenStyle.DashLine)),
         "Administrative Boundaries", "aoi, extent, outline"),
    ]


# ----------------------------------------------------------------------
# raster / terrain styles
# ----------------------------------------------------------------------
def _pseudo(layer, stops, interp="Interpolated", band=1, labels=None):
    """stops: list of (value, '#hex')."""
    fn = QgsColorRampShader(stops[0][0], stops[-1][0])
    fn.setColorRampType(shader_type(interp))
    items = []
    for i, (v, col) in enumerate(stops):
        label = labels[i] if labels else "{:g}".format(v)
        items.append(QgsColorRampShader.ColorRampItem(v, C(col), label))
    fn.setColorRampItemList(items)
    ramp = QgsGradientColorRamp(C(stops[0][1]), C(stops[-1][1]), False,
                                [QgsGradientStop(i / float(len(stops) - 1), C(c)) for i, (_, c) in
                                 enumerate(stops[1:-1], start=1)])
    fn.setSourceColorRamp(ramp)
    shader = QgsRasterShader(stops[0][0], stops[-1][0])
    shader.setRasterShaderFunction(fn)
    r = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, shader)
    r.setClassificationMin(stops[0][0])
    r.setClassificationMax(stops[-1][0])
    return r


def raster_styles(layer):
    """Return [(name, renderer, blend_mode|None, stretch, collection, tags, description)]."""
    out = []
    out.append(("DEM hypsometric", _pseudo(layer, [
        (0, "#1a9850"), (0.2, "#91cf60"), (0.4, "#d9ef8b"), (0.6, "#fee08b"),
        (0.8, "#d8b365"), (0.9, "#a6611a"), (1.0, "#ffffff")]),
        None, True, "Terrain & Elevation", "dem, elevation, hypsometric",
        "Classic green-to-white elevation tint. Stretched to each layer's min/max."))
    out.append(("DEM earth tones", _pseudo(layer, [
        (0, "#4d7a3a"), (0.3, "#a3b56b"), (0.55, "#d9c58b"), (0.8, "#a67c52"), (1.0, "#5b3a29")]),
        None, True, "Terrain & Elevation", "dem, elevation", "Muted earth-tone elevation ramp."))
    out.append(("Bathymetry", _pseudo(layer, [
        (0, "#08306b"), (0.35, "#2171b5"), (0.7, "#6baed6"), (1.0, "#deebf7")]),
        None, True, "Hydrology", "depth, water, bathymetry", "Deep-to-shallow blue ramp."))
    gray = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    ce = QgsContrastEnhancement(layer.dataProvider().dataType(1))
    ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
    gray.setContrastEnhancement(ce)
    out.append(("DEM grayscale", gray, None, True, "Terrain & Elevation", "dem, gray",
                "Single-band gray, stretched with a cumulative cut."))
    hs = QgsHillshadeRenderer(layer.dataProvider(), 1, 315.0, 45.0)
    hs.setZFactor(1.0)
    out.append(("Hillshade (multiply)", hs, QPainter.CompositionMode.CompositionMode_Multiply, False,
                "Terrain & Elevation", "hillshade, relief",
                "Sun azimuth 315°, altitude 45°, multiply blending – place above a DEM tint."))
    mhs = QgsHillshadeRenderer(layer.dataProvider(), 1, 315.0, 45.0)
    mhs.setMultiDirectional(True)
    out.append(("Hillshade multidirectional", mhs, None, False, "Terrain & Elevation", "hillshade, relief",
                "Multidirectional hillshade."))
    out.append(("Slope classes (°)", _pseudo(layer, [
        (5, "#1a9641"), (15, "#a6d96a"), (30, "#ffffbf"), (45, "#fdae61"), (90, "#d7191c")],
        interp="Discrete", labels=["0–5° flat", "5–15° gentle", "15–30° moderate", "30–45° steep", "> 45° very steep"]),
        None, False, "Terrain & Elevation", "slope, classification",
        "Discrete slope classes in degrees – apply to a slope raster."))
    out.append(("Aspect (8 directions)", _pseudo(layer, [
        (22.5, "#ff0000"), (67.5, "#ffa500"), (112.5, "#ffff00"), (157.5, "#00ff00"), (202.5, "#00ffff"),
        (247.5, "#00a5ff"), (292.5, "#0000ff"), (337.5, "#ff00ff"), (360, "#ff0000")],
        interp="Discrete", labels=["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"]),
        None, False, "Terrain & Elevation", "aspect, classification",
        "Eight compass classes (0–360°) – apply to an aspect raster."))
    out.append(("NDVI", _pseudo(layer, [
        (-1, "#0c0c8a"), (0, "#d7c9a9"), (0.2, "#f6e8a1"), (0.4, "#a6d96a"), (0.6, "#1a9641"),
        (1, "#004d1a")]),
        None, False, "Remote Sensing", "ndvi, vegetation, index", "Vegetation index ramp for values −1 … 1."))
    out.append(("Temperature (diverging)", _pseudo(layer, [
        (0, "#2c7bb6"), (0.25, "#abd9e9"), (0.5, "#ffffbf"), (0.75, "#fdae61"), (1, "#d7191c")]),
        None, True, "Remote Sensing", "lst, temperature, diverging", "Blue-to-red diverging ramp."))
    if layer.bandCount() >= 3:
        tc = QgsMultiBandColorRenderer(layer.dataProvider(), 3, 2, 1)
        out.append(("True color (R3 G2 B1)", tc, None, True, "Remote Sensing", "rgb, multiband",
                    "Natural color for imagery stored as Blue, Green, Red, NIR (e.g. Sentinel-2 B2-B3-B4-B8 stack)."))
    if layer.bandCount() >= 4:
        fc = QgsMultiBandColorRenderer(layer.dataProvider(), 4, 3, 2)
        out.append(("False color NIR (R4 G3 B2)", fc, None, True, "Remote Sensing", "nir, cir, multiband",
                    "Color-infrared composite: vegetation shows red."))
    return out


def raster_style_xml(layer, renderer, blend_mode=None, stretch=False):
    layer.setRenderer(renderer)
    if blend_mode is not None:
        layer.setBlendMode(blend_mode)
    else:
        layer.setBlendMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    return raster_style_from_layer(layer, stretch=stretch)


# ----------------------------------------------------------------------
# seeding
# ----------------------------------------------------------------------
def seed_library(store, only_missing=False):
    """Populate the library with the starter symbols. Returns number added."""
    added = 0
    coll_ids = {}

    def cid(name):
        if name not in coll_ids:
            coll_ids[name] = store.ensure_collection_path([name])
        return coll_ids[name]

    for group in (point_symbols, line_symbols, polygon_symbols):
        try:
            entries = group()
        except Exception as e:
            log_warning("Default symbols ({}) failed: {}".format(group.__name__, e))
            continue
        for name, sym, coll, tags in entries:
            try:
                _uid, created = store.add_symbol(name, symbol_kind(sym), symbol_to_xml(sym, embed=False),
                                                 tags=tags, collection_id=cid(coll), builtin=True, notify=False)
                added += int(created)
            except Exception as e:
                log_warning("Default symbol '{}' failed: {}".format(name, e))

    layer = sample_raster_layer(kind="image")  # 4 bands so multiband presets can be built
    if layer is not None:
        try:
            for name, renderer, blend, stretch, coll, tags, desc in raster_styles(layer):
                xml = raster_style_xml(layer, renderer, blend, stretch)
                _uid, created = store.add_symbol(name, KIND_RASTER, xml, subtype=renderer.type(),
                                                 description=desc, tags=tags, collection_id=cid(coll),
                                                 builtin=True, notify=False)
                added += int(created)
        except Exception as e:
            log_warning("Default raster styles failed: {}".format(e))
    else:
        log_warning("GDAL sample raster unavailable – raster presets not created.")
    store.mark_seeded()
    store.notify()
    return added

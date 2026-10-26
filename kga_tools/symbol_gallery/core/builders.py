# -*- coding: utf-8 -*-
"""
Small, version-tolerant helpers for building QGIS symbols in code.

Every symbol layer is created through the class ``create(props)`` factory,
which is the same path QGIS uses when it reads a style XML.  Property keys
used here have been stable since QGIS 3.16, so the catalog keeps working
across LTR releases without touching enum names that moved in 3.30+.
"""
import base64

from qgis.PyQt.QtGui import QColor

from .compat import VANCHOR_BOTTOM
from qgis.core import (
    QgsSymbolLayerUtils,
    QgsMarkerSymbol,
    QgsLineSymbol,
    QgsFillSymbol,
    QgsSimpleLineSymbolLayer,
    QgsMarkerLineSymbolLayer,
    QgsHashedLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSvgMarkerSymbolLayer,
    QgsSimpleFillSymbolLayer,
    QgsLinePatternFillSymbolLayer,
    QgsPointPatternFillSymbolLayer,
    QgsShapeburstFillSymbolLayer,
    QgsGradientFillSymbolLayer,
    QgsRandomMarkerFillSymbolLayer,
    QgsArrowSymbolLayer,
    QgsEffectStack,
    QgsDrawSourceEffect,
    QgsOuterGlowEffect,
    QgsInnerGlowEffect,
    QgsDropShadowEffect,
    QgsGradientColorRamp,
    QgsGradientStop,
)


# --------------------------------------------------------------------------
# colours
# --------------------------------------------------------------------------
def qc(value, alpha=None):
    """'#rrggbb' / '#rrggbbaa'-less / QColor -> QColor (optional alpha 0-255)."""
    c = QColor(value) if not isinstance(value, QColor) else QColor(value)
    if alpha is not None:
        c.setAlpha(int(alpha))
    return c


def enc(value, alpha=None):
    return QgsSymbolLayerUtils.encodeColor(qc(value, alpha))


def _s(v):
    return str(v)


# --------------------------------------------------------------------------
# symbol assembly
# --------------------------------------------------------------------------
def _assemble(symbol, layers, opacity=1.0):
    # symbols are created with a default layer; replace it
    while symbol.symbolLayerCount():
        symbol.deleteSymbolLayer(0)
    for lyr in layers:
        symbol.appendSymbolLayer(lyr)
    symbol.setOpacity(opacity)
    return symbol


def marker_symbol(*layers, opacity=1.0):
    return _assemble(QgsMarkerSymbol(), layers, opacity)


def line_symbol(*layers, opacity=1.0):
    return _assemble(QgsLineSymbol(), layers, opacity)


def fill_symbol(*layers, opacity=1.0):
    return _assemble(QgsFillSymbol(), layers, opacity)


# --------------------------------------------------------------------------
# line layers
# --------------------------------------------------------------------------
def line(color, width=0.4, style="solid", dash=None, cap="round", join="round",
         offset=0.0, alpha=None, inside=False):
    """Simple line.  Inside a *fill* symbol a positive ``offset`` moves the
    line toward the polygon interior and ``inside=True`` clips the stroke to
    the polygon (half the width is drawn, always on the inside)."""
    props = {
        "line_color": enc(color, alpha),
        "draw_inside_polygon": "1" if inside else "0",
        "line_width": _s(width),
        "line_width_unit": "MM",
        "line_style": style,
        "capstyle": cap,
        "joinstyle": join,
        "offset": _s(offset),
        "offset_unit": "MM",
    }
    if dash:
        props.update({
            "use_custom_dash": "1",
            "customdash": ";".join(_s(d) for d in dash),
            "customdash_unit": "MM",
            "line_style": "solid",
        })
    return QgsSimpleLineSymbolLayer.create(props)


def marker_line(marker, interval=5.0, placement="interval", offset=0.0,
                offset_along=0.0, rotate=True):
    """placement: interval | vertex | firstvertex | lastvertex | centralpoint
    | curvepoint | segmentcenter"""
    props = {
        "placement": placement,
        "interval": _s(interval),
        "interval_unit": "MM",
        "offset": _s(offset),
        "offset_unit": "MM",
        "offset_along_line": _s(offset_along),
        "offset_along_line_unit": "MM",
        "rotate": "1" if rotate else "0",
    }
    lyr = QgsMarkerLineSymbolLayer.create(props)
    lyr.setSubSymbol(marker.clone())
    return lyr


def hash_line(color, width=0.3, length=1.5, interval=2.0, angle=0.0,
              offset=0.0, placement="interval", offset_along=0.0):
    """Perpendicular ticks along a line.  *angle* is relative to the
    perpendicular (QGIS convention): 0 = square ticks, 45 = slanted."""
    props = {
        "placement": placement,
        "interval": _s(interval),
        "interval_unit": "MM",
        "hash_angle": _s(angle),
        "hash_length": _s(length),
        "hash_length_unit": "MM",
        "offset": _s(offset),
        "offset_unit": "MM",
        "offset_along_line": _s(offset_along),
        "offset_along_line_unit": "MM",
        "rotate": "1",
    }
    lyr = QgsHashedLineSymbolLayer.create(props)
    lyr.setSubSymbol(line_symbol(line(color, width, cap="flat")))
    return lyr


def arrow_line(color, width=1.2, head=3.0, outline="#ffffff", curved=True,
               single=True):
    props = {
        "arrow_width": _s(width),
        "arrow_width_unit": "MM",
        "arrow_start_width": _s(width),
        "arrow_start_width_unit": "MM",
        "head_length": _s(head),
        "head_length_unit": "MM",
        "head_thickness": _s(head * 0.6),
        "head_thickness_unit": "MM",
        "head_type": "0" if single else "2",
        "arrow_type": "0",
        "is_curved": "1" if curved else "0",
        "is_repeated": "0",
    }
    lyr = QgsArrowSymbolLayer.create(props)
    lyr.setSubSymbol(fill_symbol(simple_fill(color, outline, 0.15)))
    return lyr


# --------------------------------------------------------------------------
# marker layers
# --------------------------------------------------------------------------
def simple_marker(shape="circle", size=3.0, color="#ff0000", outline="#000000",
                  outline_width=0.2, angle=0.0, offset=(0, 0), no_outline=False,
                  alpha=None, outline_alpha=None):
    props = {
        "name": shape,
        "size": _s(size),
        "size_unit": "MM",
        "color": enc(color, alpha),
        "outline_color": enc(outline, outline_alpha),
        "outline_width": _s(outline_width),
        "outline_width_unit": "MM",
        "outline_style": "no" if no_outline else "solid",
        "angle": _s(angle),
        "offset": "%s,%s" % offset,
        "offset_unit": "MM",
        "joinstyle": "round",
    }
    return QgsSimpleMarkerSymbolLayer.create(props)


def svg_marker(svg_text, size=6.0, fill="#333333", outline="#ffffff",
               outline_width=0.0, anchor_bottom=False, angle=0.0):
    """SVG is embedded as base64 so saved styles stay portable."""
    data = "base64:" + base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
    props = {
        "name": data,
        "size": _s(size),
        "size_unit": "MM",
        "color": enc(fill),
        "outline_color": enc(outline),
        "outline_width": _s(outline_width),
        "outline_width_unit": "MM",
        "angle": _s(angle),
    }
    lyr = QgsSvgMarkerSymbolLayer.create(props)
    if anchor_bottom:
        # QgsMarkerSymbolLayer.VerticalAnchorPoint.Bottom == 3
        try:
            lyr.setVerticalAnchorPoint(VANCHOR_BOTTOM)
        except AttributeError:  # pragma: no cover - very old API
            pass
    return lyr


# --------------------------------------------------------------------------
# fill layers
# --------------------------------------------------------------------------
def simple_fill(color, outline="#000000", outline_width=0.26, style="solid",
                outline_style="solid", alpha=None, outline_alpha=None):
    props = {
        "color": enc(color, alpha),
        "outline_color": enc(outline, outline_alpha),
        "outline_width": _s(outline_width),
        "outline_width_unit": "MM",
        "style": style,
        "outline_style": outline_style,
        "joinstyle": "round",
    }
    return QgsSimpleFillSymbolLayer.create(props)


def no_fill_outline(color, width=0.4, alpha=None):
    return simple_fill("#000000", color, width, style="no", alpha=0,
                       outline_alpha=alpha)


def hatch(color, angle=45.0, spacing=2.0, width=0.25, alpha=None, offset=0.0):
    props = {
        "angle": _s(angle),
        "distance": _s(spacing),
        "distance_unit": "MM",
        "offset": _s(offset),
        "offset_unit": "MM",
        "color": enc(color, alpha),
    }
    lyr = QgsLinePatternFillSymbolLayer.create(props)
    lyr.setSubSymbol(line_symbol(line(color, width, cap="flat", alpha=alpha)))
    return lyr


def point_pattern(marker, dx=4.0, dy=4.0, offset_x=None, offset_y=0.0):
    props = {
        "distance_x": _s(dx),
        "distance_y": _s(dy),
        "distance_x_unit": "MM",
        "distance_y_unit": "MM",
        # staggered rows look far more natural than a plain grid
        "displacement_x": _s(dx / 2.0 if offset_x is None else offset_x),
        "displacement_y": _s(offset_y),
        "displacement_x_unit": "MM",
        "displacement_y_unit": "MM",
    }
    lyr = QgsPointPatternFillSymbolLayer.create(props)
    lyr.setSubSymbol(marker.clone())
    return lyr


def random_markers(marker, density=20.0, seed=7):
    """density: one marker per *density* mm² of polygon (at map scale)."""
    props = {
        "count_method": "1",          # DensityBasedCount
        "point_count": "1",
        "density_area": _s(density),
        "density_area_unit": "MM",
        "seed": _s(seed),
        "clip_points": "1",
    }
    lyr = QgsRandomMarkerFillSymbolLayer.create(props)
    lyr.setSubSymbol(marker.clone())
    return lyr


def shapeburst(color, color2=None, distance=3.0, blur=1, whole=False,
               alpha=None, alpha2=0):
    props = {
        "color": enc(color, alpha),
        "gradient_color2": enc(color2 or color, alpha2),
        "color_type": "0",
        "blur_radius": _s(blur),
        "use_whole_shape": "1" if whole else "0",
        "max_distance": _s(distance),
        "distance_unit": "MM",
        "ignore_rings": "0",
    }
    return QgsShapeburstFillSymbolLayer.create(props)


def gradient(color1, color2, angle=90.0, radial=False):
    props = {
        "color": enc(color1),
        "gradient_color2": enc(color2),
        "color_type": "0",
        "type": "1" if radial else "0",
        "coordinate_mode": "0",
        "spread": "0",
        "angle": _s(angle),
        "reference_point1": "0.5,0" if not radial else "0.5,0.5",
        "reference_point2": "0.5,1" if not radial else "1,1",
        "reference_point1_iscentroid": "1" if radial else "0",
    }
    return QgsGradientFillSymbolLayer.create(props)


# --------------------------------------------------------------------------
# paint effects  (glow / shadow) – the "cool" factor
# --------------------------------------------------------------------------
def _stack(*effects):
    st = QgsEffectStack()
    for e in effects:
        st.appendEffect(e)
    return st


def with_glow(layer, color, spread=2.0, blur=3, opacity=0.8, inner=False):
    g = QgsInnerGlowEffect() if inner else QgsOuterGlowEffect()
    g.setColor(qc(color))
    g.setSpread(spread)
    g.setBlurLevel(blur)
    g.setOpacity(opacity)
    if inner:
        layer.setPaintEffect(_stack(QgsDrawSourceEffect(), g))
    else:
        layer.setPaintEffect(_stack(g, QgsDrawSourceEffect()))
    return layer


def with_shadow(layer, color="#000000", distance=1.0, angle=135, blur=4,
                opacity=0.45):
    d = QgsDropShadowEffect()
    d.setColor(qc(color))
    d.setOffsetDistance(distance)
    d.setOffsetAngle(angle)
    d.setBlurLevel(blur)
    d.setOpacity(opacity)
    layer.setPaintEffect(_stack(d, QgsDrawSourceEffect()))
    return layer


# --------------------------------------------------------------------------
# colour ramps
# --------------------------------------------------------------------------
def ramp(colors, positions=None, discrete=False):
    """colors: list of hex strings (>= 2).  positions default to even spacing."""
    n = len(colors)
    if positions is None:
        positions = [i / (n - 1.0) for i in range(n)]
    stops = [QgsGradientStop(p, qc(c)) for p, c in zip(positions[1:-1], colors[1:-1])]
    return QgsGradientColorRamp(qc(colors[0]), qc(colors[-1]), discrete, stops)

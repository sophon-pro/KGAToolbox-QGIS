# -*- coding: utf-8 -*-
"""
Symbol Bag – recoloring symbols and raster styles.

Only colours change. Size, shape, stroke width, layer structure, rotation,
offsets, data-defined properties, effects … are left exactly as they were,
because every edit goes through the symbol layer's own colour setters (vector)
or touches only colour attributes in the style XML (raster).

A *target* says what to recolour:
    ("tint",)                 whole symbol, keeping its shades (smart)
    ("fills",)                every visible fill / line colour
    ("outlines",)             every visible outline colour
    ("slot", path, part)      one colour of one symbol layer
    ("stop", index)           one raster colour stop / class
"""

from contextlib import suppress
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import QgsApplication

from .storage import KIND_RASTER
from .symbols import raster_style_info, symbol_from_xml, symbol_to_xml

LINE_TYPES = {"SimpleLine", "MarkerLine", "HashLine", "ArrowLine", "InterpolatedLine", "Lineburst",
              "LineburstLine", "FilledLine", "RasterLine"}
NO_COLOR_TYPES = {"RasterFill", "RasterMarker", "RasterLine", "GeometryGenerator"}
PART_LABELS = {"fill": "fill", "line": "line", "outline": "outline", "color2": "end color", "glyph": "glyph"}


class Slot(object):
    """One recolourable colour inside a symbol."""

    __slots__ = ("path", "part", "label", "color", "layer_type", "note")

    def __init__(self, path, part, label, color, layer_type, note=""):
        self.path = path
        self.part = part
        self.label = label
        self.color = QColor(color)
        self.layer_type = layer_type
        self.note = note

    @property
    def key(self):
        return ("slot", tuple(self.path), self.part)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _visible_name(layer_type):
    with suppress(Exception):
        md = QgsApplication.symbolLayerRegistry().symbolLayerMetadata(layer_type)
        if md is not None:
            return md.visibleName()
    return layer_type


def _layer_at(symbol, path):
    sym = symbol
    layer = None
    for i, idx in enumerate(path):
        if sym is None or idx >= sym.symbolLayerCount():
            return None
        layer = sym.symbolLayer(idx)
        if i < len(path) - 1:
            sym = layer.subSymbol()
    return layer


def _svg_params(layer):
    """(fill_recolourable, stroke_recolourable) for SVG based layers."""
    path = ""
    for getter in ("path", "svgFilePath"):
        if hasattr(layer, getter):
            try:
                path = getattr(layer, getter)()
                break
            except TypeError:
                continue
    if not path:
        return True, True
    try:
        res = QgsApplication.svgCache().containsParams(path, QColor(), QColor(), True)
        return bool(res[0]), bool(res[1])
    except Exception:
        return True, True


def _no_stroke(layer):
    try:
        return layer.strokeStyle() == Qt.PenStyle.NoPen
    except (AttributeError, TypeError):
        return False


def _no_brush(layer):
    try:
        return layer.brushStyle() == Qt.BrushStyle.NoBrush
    except (AttributeError, TypeError):
        return False


def _get(layer, part):
    if part == "outline":
        return QColor(layer.strokeColor())
    if part == "color2":
        return QColor(layer.color2())
    return QColor(layer.color())


def _set(layer, part, color):
    if part == "outline":
        layer.setStrokeColor(color)
    elif part == "color2":
        layer.setColor2(color)
    else:
        if part == "fill" and color.alpha() > 0 and _no_brush(layer):
            # an outline-only polygon ("No brush"): picking a fill colour means "fill it"
            try:
                layer.setBrushStyle(Qt.BrushStyle.SolidPattern)
            except (AttributeError, TypeError):
                pass
        layer.setColor(color)


# ----------------------------------------------------------------------
# vector symbols
# ----------------------------------------------------------------------
def color_slots(symbol, _path=(), _prefix=""):
    """Every recolourable colour of a symbol, walking into sub-symbols
    (marker lines, pattern fills, arrows …)."""
    slots = []
    if symbol is None:
        return slots
    n = symbol.symbolLayerCount()
    for i in range(n):
        layer = symbol.symbolLayer(i)
        path = _path + (i,)
        lt = layer.layerType()
        name = _visible_name(lt)
        prefix = "{}Layer {} · {}".format(_prefix, i + 1, name) if n > 1 or _prefix else name
        sub = layer.subSymbol()
        if sub is not None:
            # the layer's own colour *is* its sub-symbol's colour
            slots.extend(color_slots(sub, path, prefix + " › "))
            continue
        if lt in NO_COLOR_TYPES:
            continue
        note = ""
        fill_ok, stroke_ok = True, True
        if lt in ("SvgMarker", "SVGFill"):
            fill_ok, stroke_ok = _svg_params(layer)
            if not (fill_ok or stroke_ok):
                note = "this SVG has fixed colours"
        main_part = "line" if lt in LINE_TYPES else ("glyph" if lt == "FontMarker" else "fill")
        c = layer.color()
        if c.isValid() and fill_ok and _no_brush(layer) and main_part == "fill":
            # empty fill (outline-only polygon): offered so it can be given a colour,
            # but invisible, so "Whole" leaves it empty
            c = QColor(c)
            c.setAlpha(0)
            slots.append(Slot(path, main_part, "{} · {}".format(prefix, PART_LABELS[main_part]), c, lt,
                              "no fill"))
        elif c.isValid() and fill_ok:
            slots.append(Slot(path, main_part, "{} · {}".format(prefix, PART_LABELS[main_part]), c, lt, note))
        if hasattr(layer, "color2"):
            with suppress(TypeError):
                c2 = layer.color2()
                if c2.isValid():
                    slots.append(Slot(path, "color2", "{} · end color".format(prefix), c2, lt))
        if lt not in LINE_TYPES and stroke_ok and not _no_stroke(layer):
            sc = layer.strokeColor()
            if sc.isValid():
                slots.append(Slot(path, "outline", "{} · outline".format(prefix), sc, lt))
    return slots


def main_color(symbol):
    """The colour that represents the symbol (most visible, saturated slot)."""
    best, score = None, -1.0
    for s in color_slots(symbol):
        c = s.color
        if c.alpha() < 20:
            continue
        sc = c.alpha() / 255.0 * (0.35 + c.hsvSaturationF()) * (1.0 if s.part != "outline" else 0.6)
        if sc > score:
            best, score = QColor(c), sc
    return best or QColor(symbol.color())


def _is_neutral(c):
    return c.hsvSaturationF() < 0.12 or c.valueF() < 0.12


def tint(color, ref, target):
    """Move `color` the way `ref` moves to `target`: same hue, the colour keeps
    its own lightness / saturation relative to the reference. Alpha is kept."""
    h = target.hsvHueF()
    if h < 0:
        h = max(0.0, ref.hsvHueF())
    rs, rv = max(0.05, ref.hsvSaturationF()), max(0.05, ref.valueF())
    s = min(1.0, color.hsvSaturationF() * target.hsvSaturationF() / rs)
    v = min(1.0, color.valueF() * target.valueF() / rv)
    out = QColor.fromHsvF(h, s, v)
    out.setAlpha(color.alpha())
    return out


def recolor_symbol(symbol, target, color):
    """Return a recoloured clone of symbol. The original is never modified."""
    sym = symbol.clone()
    color = QColor(color)
    kind = target[0]
    if kind == "slot":
        layer = _layer_at(sym, target[1])
        if layer is not None:
            _set(layer, target[2], color)
        return sym
    slots = color_slots(sym)
    if kind == "tint":
        ref = main_color(symbol)
        ref_neutral = _is_neutral(ref)
        alpha_k = color.alphaF() / max(0.01, ref.alphaF())
        for s in slots:
            if s.color.alpha() == 0:
                continue                                    # invisible stays invisible
            if _is_neutral(s.color) and not ref_neutral:
                continue                                    # keep black/white/grey details
            new = tint(s.color, ref, color) if not ref_neutral else QColor(color)
            new.setAlphaF(min(1.0, s.color.alphaF() * alpha_k) if not ref_neutral else
                          min(1.0, s.color.alphaF() * alpha_k))
            _set(_layer_at(sym, s.path), s.part, new)
        return sym
    if kind == "outlines":
        want = ("outline",)
    elif kind == "both":
        # fill AND outline: on dense layers (features a few pixels wide) the
        # outline is most of what is visible, so recolouring only the fill
        # seems to do nothing at that scale
        want = ("fill", "line", "glyph", "color2", "outline")
    else:
        want = ("fill", "line", "glyph", "color2")
    alpha_k = color.alphaF()
    def group(part):
        return "outline" if part == "outline" else "fill"
    visible = {group(s.part) for s in slots if s.part in want and s.color.alpha() > 0}
    for s in slots:
        if s.part not in want:
            continue
        if s.color.alpha() == 0:
            if group(s.part) in visible:
                continue        # e.g. a hidden helper layer next to a visible fill: keep it hidden
            base = 1.0          # every chosen part is empty (outline-only polygon): fill it
        else:
            base = s.color.alphaF()
        new = QColor(color)
        new.setAlphaF(min(1.0, base * alpha_k))
        _set(_layer_at(sym, s.path), s.part, new)
    return sym


def color_of(symbol, target):
    """Colour to show in the picker for a target."""
    if target[0] == "slot":
        layer = _layer_at(symbol, target[1])
        return _get(layer, target[2]) if layer is not None else QColor()
    if target[0] in ("fills", "outlines", "both"):
        want = ("outline",) if target[0] == "outlines" else ("fill", "line", "glyph", "color2")
        slots = [s for s in color_slots(symbol) if s.part in want]
        for s in slots:
            if s.color.alpha() > 0:
                return QColor(s.color)
        if slots:
            # only empty fills: show their colour at full opacity – picking fills them
            c = QColor(slots[0].color)
            c.setAlpha(255)
            return c
    return main_color(symbol)


def is_empty_target(symbol, target):
    """True when every part a fill / outline target would change is invisible
    (e.g. "Fill color" on an outline-only polygon)."""
    if symbol is None or target[0] not in ("fills", "outlines"):
        return False
    want = ("outline",) if target[0] == "outlines" else ("fill", "line", "glyph", "color2")
    slots = [s for s in color_slots(symbol) if s.part in want]
    return bool(slots) and all(s.color.alpha() == 0 for s in slots)


# ----------------------------------------------------------------------
# raster styles (XML-level: only colour attributes change)
# ----------------------------------------------------------------------
def _parse(xml):
    doc = QDomDocument()
    ok = doc.setContent(xml)
    if isinstance(ok, tuple):
        ok = ok[0]
    return doc if ok else None


def _stop_elements(doc):
    out = []
    for tag in ("item", "paletteEntry"):
        nodes = doc.elementsByTagName(tag)
        for i in range(nodes.count()):
            e = nodes.at(i).toElement()
            if e.hasAttribute("color"):
                out.append(e)
    return out


def _elem_color(e):
    c = QColor(e.attribute("color"))
    c.setAlpha(int(float(e.attribute("alpha", "255") or 255)))
    return c


def _set_elem_color(e, c):
    e.setAttribute("color", c.name())
    e.setAttribute("alpha", str(c.alpha()))


def raster_stops(xml):
    """[(index, value, label, QColor)] of a pseudocolor / paletted style."""
    doc = _parse(xml)
    if doc is None:
        return []
    out = []
    for i, e in enumerate(_stop_elements(doc)):
        out.append((i, e.attribute("value"), e.attribute("label"), _elem_color(e)))
    return out


def raster_recolorable(xml):
    return raster_style_info(xml)["renderer"] in ("singlebandpseudocolor", "paletted") and bool(raster_stops(xml))


def recolor_raster(xml, target, color):
    doc = _parse(xml)
    if doc is None:
        return xml
    elems = _stop_elements(doc)
    if not elems:
        return xml
    color = QColor(color)
    if target[0] == "stop":
        i = target[1]
        if 0 <= i < len(elems):
            _set_elem_color(elems[i], color)
    else:   # tint the whole ramp, keeping its light / dark structure
        cols = [_elem_color(e) for e in elems]
        ref = max(cols, key=lambda c: c.hsvSaturationF() * c.alphaF())
        for e, c in zip(elems, cols):
            if c.alpha() == 0:
                continue
            new = tint(c, ref, color) if not _is_neutral(c) else QColor(c)
            new.setAlpha(c.alpha())
            _set_elem_color(e, new)
    return doc.toString()


def raster_color_of(xml, target):
    stops = raster_stops(xml)
    if not stops:
        return QColor()
    if target[0] == "stop" and 0 <= target[1] < len(stops):
        return QColor(stops[target[1]][3])
    return max((s[3] for s in stops), key=lambda c: c.hsvSaturationF() * c.alphaF())


def apply_ramp(xml, ramp, reverse=False):
    """Re-colour every stop from a QGIS colour ramp, evenly along the stops'
    position (values and labels are kept)."""
    doc = _parse(xml)
    if doc is None or ramp is None:
        return xml
    elems = _stop_elements(doc)
    n = len(elems)
    if n == 0:
        return xml
    for i, e in enumerate(elems):
        t = i / float(n - 1) if n > 1 else 0.0
        c = QColor(ramp.color(1.0 - t if reverse else t))
        c.setAlpha(_elem_color(e).alpha())
        _set_elem_color(e, c)
    return doc.toString()


def reverse_ramp(xml):
    doc = _parse(xml)
    if doc is None:
        return xml
    elems = _stop_elements(doc)
    cols = [_elem_color(e) for e in elems]
    for e, c in zip(elems, reversed(cols)):
        _set_elem_color(e, c)
    return doc.toString()


# ----------------------------------------------------------------------
# one entry point for the UI
# ----------------------------------------------------------------------
def targets_for(kind, xml):
    """[(label, target)] offered in the picker for a stored definition."""
    if kind == KIND_RASTER:
        stops = raster_stops(xml)
        if not stops:
            return []
        out = [("Whole ramp (keep shades)", ("tint",))]
        for i, value, label, _c in stops:
            out.append(("Stop {} · {}".format(i + 1, label or value), ("stop", i)))
        return out
    sym = symbol_from_xml(xml)
    if sym is None:
        return []
    # Only choices that do something different.
    #  simple symbol (one fill and/or one outline): Fill color / Outline / Fill + outline –
    #    "Whole" would just repeat "Fill + outline", so it is not offered;
    #  multi-layer symbol (patterns, hatches…): Whole (keeps each layer's shade) /
    #    All fills / All outlines / single layers.
    slots = color_slots(sym)
    fills = [s for s in slots if s.part != "outline"]
    outs = [s for s in slots if s.part == "outline"]
    main = "Line color" if kind == "line" else "Fill color"
    if len(slots) == 1:
        return [(main if fills else "Outline color", ("fills",) if fills else ("outlines",))]
    if len(fills) <= 1 and len(outs) <= 1 and fills and outs:
        fill_t = (main, ("fills",))
        out_t = ("Outline color", ("outlines",))
        both_t = ("Fill + outline (same color)", ("both",))
        if all(s.color.alpha() == 0 for s in fills):
            return [out_t, fill_t, both_t]      # empty fill: start on what is visible
        return [fill_t, out_t, both_t]
    out = [("Whole symbol (keep shades)", ("tint",))]
    if len(fills) == 1:
        out.append((main, ("fills",)))
    elif fills:
        out.append(("All fill / line colors", ("fills",)))
    if len(outs) == 1:
        out.append(("Outline color", ("outlines",)))
    elif outs:
        out.append(("All outline colors", ("outlines",)))
    for s in slots:
        if (s in fills and len(fills) > 1) or (s in outs and len(outs) > 1):
            out.append((s.label + (" (" + s.note + ")" if s.note else ""), s.key))
    return out


def recolor(kind, xml, target, color):
    if kind == KIND_RASTER:
        return recolor_raster(xml, target, color)
    sym = symbol_from_xml(xml)
    if sym is None:
        return xml
    return symbol_to_xml(recolor_symbol(sym, target, color))


def current_color(kind, xml, target):
    if kind == KIND_RASTER:
        return raster_color_of(xml, target)
    sym = symbol_from_xml(xml)
    return color_of(sym, target) if sym is not None else QColor()


# ----------------------------------------------------------------------
# live layers (recolour what is on the map)
# ----------------------------------------------------------------------
RASTER_ITEM = "_raster"


def layer_items(layer, keys=None):
    """Colour items of a map layer as [(key, label, xml)].

    Vector: one item per legend symbol (single symbol, categories, ranges,
    rules – also inside cluster / displacement wrappers). `keys` limits the
    items to selected legend classes. Raster: one item with the style XML.
    """
    from qgis.core import QgsRasterLayer, QgsVectorLayer
    from .symbols import raster_style_from_layer
    if isinstance(layer, QgsRasterLayer):
        return [(RASTER_ITEM, layer.name(), raster_style_from_layer(layer, stretch=False))]
    if not isinstance(layer, QgsVectorLayer) or layer.renderer() is None:
        return []
    out = []
    for item in layer.renderer().legendSymbolItems():
        sym = item.symbol()
        if sym is None:
            continue
        key = item.ruleKey()
        if keys is not None and key not in keys:
            continue
        label = item.label() or layer.name()
        out.append((key, label, symbol_to_xml(sym, embed=False)))
    return out


def _raster_colors_in_place(layer, xml):
    """Live raster preview: set the new stop colours on the layer's current
    pseudocolor shader (same stops) instead of loading a whole new style.
    Returns False when that is not possible."""
    try:
        r = layer.renderer()
        if r is None or r.type() != "singlebandpseudocolor":
            return False
        fn = r.shader().rasterShaderFunction()
        items = fn.colorRampItemList()
        cols = [s[3] for s in raster_stops(xml)]
        if len(items) != len(cols):
            return False
        for it, c in zip(items, cols):
            it.color = QColor(c)
        fn.setColorRampItemList(items)
        return True
    except (AttributeError, RuntimeError, TypeError):
        return False


def apply_layer_items(layer, items, live=False):
    """Write recoloured items back to the layer. Only the colours differ from
    what the layer had, so classes, values, widths … stay as they are.

    live=True (while dragging): change the symbols of the layer's current
    renderer in place and just repaint – no new renderer, so QGIS does not
    rebuild the legend (and lose the Layers-panel selection) every frame."""
    from qgis.core import QgsRasterLayer
    from .symbols import apply_raster_style
    if isinstance(layer, QgsRasterLayer):
        xml = items.get(RASTER_ITEM)
        if xml:
            if not (live and _raster_colors_in_place(layer, xml)):
                apply_raster_style(layer, xml, stretch=False)
            layer.triggerRepaint()
        return
    if live and layer.renderer() is not None:
        renderer = layer.renderer()
        for key, xml in items.items():
            sym = symbol_from_xml(xml)
            if sym is not None:
                renderer.setLegendSymbolItem(key, sym)
        layer.triggerRepaint()
        return
    renderer = layer.renderer().clone()
    for key, xml in items.items():
        sym = symbol_from_xml(xml)
        if sym is not None:
            renderer.setLegendSymbolItem(key, sym)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


# ----------------------------------------------------------------------
# line / outline thickness
# ----------------------------------------------------------------------
WIDTH_LABELS = {"line": "Line width", "fill": "Outline width", "marker": "Marker outline",
                "polygon": "Outline width", "point": "Marker outline"}


def _width_slots(symbol):
    """[(get, set, unit)] for the strokes that make a symbol's thickness:
    simple-line widths and the outline (stroke) width of fills and markers.
    Hidden strokes (no pen) are skipped."""
    from qgis.core import QgsSimpleLineSymbolLayer
    out = []
    for i in range(symbol.symbolLayerCount()):
        sl = symbol.symbolLayer(i)
        if isinstance(sl, QgsSimpleLineSymbolLayer):
            if sl.penStyle() != Qt.PenStyle.NoPen:
                out.append((sl.width, sl.setWidth, sl.widthUnit()))
        elif sl.layerType() == "ArrowLine" and hasattr(sl, "arrowWidth"):
            def put(v, sl=sl):
                old = sl.arrowWidth()
                sl.setArrowWidth(v)
                if hasattr(sl, "setArrowStartWidth") and old > 0:
                    sl.setArrowStartWidth(sl.arrowStartWidth() * v / old)
            out.append((sl.arrowWidth, put, sl.arrowWidthUnit() if hasattr(sl, "arrowWidthUnit") else None))
        elif hasattr(sl, "strokeWidth") and hasattr(sl, "setStrokeWidth"):
            style = sl.strokeStyle() if hasattr(sl, "strokeStyle") else None
            if style == Qt.PenStyle.NoPen:
                continue
            unit = sl.strokeWidthUnit() if hasattr(sl, "strokeWidthUnit") else None
            out.append((sl.strokeWidth, sl.setStrokeWidth, unit))
    return out


def _unit_label(unit):
    if unit is None:
        return "mm"
    try:
        from qgis.core import QgsUnitTypes
        return QgsUnitTypes.toAbbreviatedString(unit) or "mm"
    except Exception:
        return "mm"


def width_info(kind, xml):
    """(value, unit label) of a symbol's thickness, or None when it has none
    (rasters, fills without outline …). Value = its widest stroke."""
    if kind not in WIDTH_LABELS:
        return None
    sym = symbol_from_xml(xml)
    if sym is None:
        return None
    slots = _width_slots(sym)
    if not slots:
        return None
    if kind == "line" and hasattr(sym, "width") and sym.width() > 0:
        value = sym.width()
    else:
        value = max(get() for get, _s, _u in slots)
    return float(value), _unit_label(slots[0][2])


def set_width(kind, xml, value):
    """Return xml with the thickness set to `value`. Multi-stroke symbols keep
    their proportions (a casing stays wider than its centre line); colours,
    sizes, dashes and offsets are untouched."""
    sym = symbol_from_xml(xml)
    if sym is None or kind not in WIDTH_LABELS:
        return xml
    value = max(0.0, float(value))
    slots = _width_slots(sym)
    if not slots:
        return xml
    has_plain = any(sym.symbolLayer(i).layerType() != "ArrowLine" for i in range(sym.symbolLayerCount()))
    if kind == "line" and hasattr(sym, "setWidth") and has_plain and sym.width() > 0:
        old = sym.width()
        sym.setWidth(value)                    # all layers in proportion (incl. marker lines)
        for i in range(sym.symbolLayerCount()):
            sl = sym.symbolLayer(i)
            if sl.layerType() == "ArrowLine" and old > 0:
                sl.setArrowWidth(sl.arrowWidth() * value / old)
    else:
        ref = max(get() for get, _s, _u in slots)
        for get, put, _u in slots:
            put(get() * value / ref if ref > 0 else value)
    return symbol_to_xml(sym, embed=False)


# ----------------------------------------------------------------------
# point (marker) size
# ----------------------------------------------------------------------
SIZE_KINDS = ("marker", "point")


def size_info(kind, xml):
    """(size, unit label) of a point symbol – its largest marker – or None."""
    if kind not in SIZE_KINDS:
        return None
    sym = symbol_from_xml(xml)
    if sym is None or not hasattr(sym, "size"):
        return None
    try:
        value = float(sym.size())
        unit = sym.sizeUnit() if hasattr(sym, "sizeUnit") else None
    except Exception:
        return None
    return value, _unit_label(unit)


def set_size(kind, xml, value):
    """Return xml with the point size set to `value`. Multi-layer markers keep
    their proportions (a halo stays larger than its dot) and their offsets;
    colours and outlines are untouched."""
    if kind not in SIZE_KINDS:
        return xml
    sym = symbol_from_xml(xml)
    if sym is None or not hasattr(sym, "setSize"):
        return xml
    sym.setSize(max(0.0, float(value)))
    return symbol_to_xml(sym, embed=False)

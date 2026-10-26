# -*- coding: utf-8 -*-
"""
Applying gallery entries to layers, saving them to the QGIS Style Manager
and exporting them.  No GUI code lives here, so it can be unit-tested
headlessly.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsVectorLayer,
    QgsRasterLayer,
    QgsFillSymbol,
    QgsSimpleFillSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer,
    QgsRuleBasedRenderer,
    QgsColorRampShader,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleBandGrayRenderer,
    QgsStyle,
    QgsProject,
)

from .compat import GEOM_TO_SYMBOL, SYMBOL_MARKER, SYMBOL_LINE, SYMBOL_FILL, STATS_MIN_MAX, SHADER_INTERPOLATED, SHADER_CONTINUOUS, STYLE_RAMP, STYLE_SYMBOL
from .catalog import KIND_LABELS, MARKER, LINE, FILL

STYLE_PREFIX = "Gallery – "
STYLE_TAG = "Symbol Gallery"



class ApplyError(Exception):
    """Raised with a user-facing message when an entry cannot be applied."""


# --------------------------------------------------------------------------
# compatibility
# --------------------------------------------------------------------------
def _layer_symbol_type(layer):
    if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
        return None
    return GEOM_TO_SYMBOL.get(layer.geometryType())


def geometry_word(layer):
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    kind = _sym_kind(_layer_symbol_type(layer))
    if kind is not None:
        return {MARKER: "point", LINE: "line", FILL: "polygon"}[kind]
    return "table" if isinstance(layer, QgsVectorLayer) else "unsupported"


def _sym_kind(symbol_type):
    if symbol_type == SYMBOL_MARKER:
        return MARKER
    if symbol_type == SYMBOL_LINE:
        return LINE
    if symbol_type == SYMBOL_FILL:
        return FILL
    return None


def check(entry, layer):
    """Return (ok: bool, reason: str).  *reason* is user-facing."""
    if layer is None:
        return False, "Select a layer in the Layers panel first."
    if not layer.isValid():
        return False, "The active layer is not valid."

    if entry.is_ramp:
        if isinstance(layer, QgsRasterLayer):
            return True, "Applies as a single-band pseudocolor ramp."
        if isinstance(layer, QgsVectorLayer):
            r = layer.renderer()
            if isinstance(r, (QgsGraduatedSymbolRenderer, QgsCategorizedSymbolRenderer)):
                return True, "Recolours the existing classes with this ramp."
            return False, ("Colour ramps apply to raster layers, or to vector layers "
                           "that already use a Graduated or Categorized renderer.")
        return False, "Colour ramps apply to raster or classified vector layers."

    if not isinstance(layer, QgsVectorLayer):
        return False, "%s symbols need a vector layer." % KIND_LABELS[entry.kind]
    target = _layer_symbol_type(layer)
    if target is None:
        return False, "The active layer has no geometry."
    tkind = _sym_kind(target)
    if tkind == entry.kind:
        return True, ""
    if entry.kind == LINE and tkind == FILL:
        return True, "Line symbol will be used as the polygon outline."
    return False, "This is a %s symbol, but '%s' is a %s layer." % (
        KIND_LABELS[entry.kind].lower(), layer.name(), geometry_word(layer))


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------
def symbol_for_layer(entry, layer):
    """Build the entry's symbol, converted to the layer's geometry if needed."""
    sym = entry.build()
    target = _layer_symbol_type(layer)
    if entry.kind == LINE and _sym_kind(target) == FILL:
        # line symbol layers are valid outline layers of a fill symbol
        fill = QgsFillSymbol()
        fill.deleteSymbolLayer(0)
        for i in range(sym.symbolLayerCount()):
            fill.appendSymbolLayer(sym.symbolLayer(i).clone())
        fill.setOpacity(sym.opacity())
        return fill
    return sym


def apply(entry, layer, keep_classes=True, symbol=None, class_colours=True, base=None):
    """Apply *entry* to *layer*.

    Returns ``(message, previous_renderer_clone)`` so callers can offer undo.
    Raises :class:`ApplyError` with a friendly message if impossible.
    *base*: renderer to start from instead of the layer's current one (live
    pop-up edits start again from the style the layer had before).
    """
    ok, reason = check(entry, layer)
    if not ok:
        raise ApplyError(reason)

    previous = layer.renderer().clone() if layer.renderer() else None

    if entry.is_ramp:
        if isinstance(layer, QgsRasterLayer):
            msg = _apply_ramp_raster(entry, layer)
        else:
            msg = _apply_ramp_vector(entry, layer)
    else:
        msg = _apply_symbol(entry, layer, keep_classes, symbol, class_colours, base)

    _done(layer)
    return msg, previous


def _color_layer_index(entry, sym):
    if entry.color_layer is not None and entry.color_layer < sym.symbolLayerCount():
        return entry.color_layer
    if entry.kind == FILL:
        for i in range(sym.symbolLayerCount()):
            sl = sym.symbolLayer(i)
            if isinstance(sl, QgsSimpleFillSymbolLayer) and sl.brushStyle() != Qt.BrushStyle.NoBrush:
                return i
    return 0


def main_colour(sym):
    """Colour of a symbol, robust to colour-locked layers.

    QgsSymbol.color() returns black when every layer is locked (common in styles
    converted from ArcMap), which would turn every class black."""
    if sym is None:
        return None
    for i in range(sym.symbolLayerCount()):
        sl = sym.symbolLayer(i)
        if not sl.isLocked():
            return sl.color()
    return sym.symbolLayer(0).color() if sym.symbolLayerCount() else sym.color()


def _lock_layout(sym, keep):
    """Only the main colour layer follows colour changes (same layout in all modes)."""
    for i in range(sym.symbolLayerCount()):
        sym.symbolLayer(i).setLocked(i != keep)
    return sym


def _unlock_if_all_locked(sym, keep):
    n = sym.symbolLayerCount()
    if n and all(sym.symbolLayer(i).isLocked() for i in range(n)):
        sym.symbolLayer(min(keep, n - 1)).setLocked(False)
    return sym


def _class_symbol(template, keep, class_color):
    """Clone *template*, giving its colour layer the class hue while keeping
    the template's transparency; every other layer is locked."""
    sym = template.clone()
    for i in range(sym.symbolLayerCount()):
        sym.symbolLayer(i).setLocked(i != keep)
    target = sym.symbolLayer(keep)
    c = QColor(class_color)
    c.setAlpha(target.color().alpha())
    target.setColor(c)
    return sym


def _cls(sym, keep, class_color, class_colours):
    """Class symbol: recoloured with the class colour, or the symbol as it is."""
    if class_colours and class_color is not None:
        return _class_symbol(sym, keep, class_color)
    return _lock_layout(sym.clone(), keep)


def _apply_symbol(entry, layer, keep_classes, symbol=None, class_colours=True, base=None):
    if symbol is not None:          # edited in the gallery
        sym = symbol.clone()
        if _sym_kind(sym.type()) == LINE and _sym_kind(_layer_symbol_type(layer)) == FILL:
            sym = _line_to_fill(sym)
    else:
        sym = symbol_for_layer(entry, layer)
    r = base if base is not None else layer.renderer()
    keep = _color_layer_index(entry, sym)

    if keep_classes and isinstance(r, QgsCategorizedSymbolRenderer):
        r = r.clone()
        for i, cat in enumerate(r.categories()):
            if cat.symbol() is not None:
                r.updateCategorySymbol(i, _cls(sym, keep, main_colour(cat.symbol()), class_colours))
        r.setSourceSymbol(_class_symbol(sym, keep, sym.symbolLayer(keep).color()))
        layer.setRenderer(r)
        return "'%s' applied to %d classes of '%s'%s." % (
            entry.name, len(r.categories()), layer.name(),
            " (class colours kept)" if class_colours else "")

    if keep_classes and isinstance(r, QgsGraduatedSymbolRenderer):
        r = r.clone()
        for i, rng in enumerate(r.ranges()):
            if rng.symbol() is not None:
                r.updateRangeSymbol(i, _cls(sym, keep, main_colour(rng.symbol()), class_colours))
        r.setSourceSymbol(_class_symbol(sym, keep, sym.symbolLayer(keep).color()))
        layer.setRenderer(r)
        return "'%s' applied to %d classes of '%s'%s." % (
            entry.name, len(r.ranges()), layer.name(),
            " (class colours kept)" if class_colours else "")

    if keep_classes and isinstance(r, QgsRuleBasedRenderer):
        r = r.clone()
        n = 0
        for rule in r.rootRule().descendants():
            old = rule.symbol()
            if old is None:
                continue
            rule.setSymbol(_cls(sym, keep, main_colour(old), class_colours))
            n += 1
        layer.setRenderer(r)
        return "'%s' applied to %d rules of '%s' (rule colours kept)." % (
            entry.name, n, layer.name())

    layer.setRenderer(QgsSingleSymbolRenderer(_unlock_if_all_locked(sym, keep)))
    return "'%s' applied to '%s'." % (entry.name, layer.name())


def _apply_ramp_vector(entry, layer):
    r = layer.renderer().clone()
    r.updateColorRamp(entry.build())
    layer.setRenderer(r)
    return "Ramp '%s' applied to the classes of '%s'." % (entry.name, layer.name())


def _apply_ramp_raster(entry, layer):
    provider = layer.dataProvider()
    old = layer.renderer()
    band, mn, mx = 1, None, None
    if isinstance(old, QgsSingleBandPseudoColorRenderer):
        band = old.band()
        mn, mx = old.classificationMin(), old.classificationMax()
    elif isinstance(old, QgsSingleBandGrayRenderer):
        band = old.grayBand()
        ce = old.contrastEnhancement()
        if ce is not None:
            mn, mx = ce.minimumValue(), ce.maximumValue()
    if mn is None or mx is None or mn != mn or mx != mx or mn >= mx:
        stats = provider.bandStatistics(
            band, STATS_MIN_MAX,
            layer.extent(), 250000)
        mn, mx = stats.minimumValue, stats.maximumValue
    if mn == mx:
        mx = mn + 1.0

    fcn = QgsColorRampShader(mn, mx, entry.build(),
                             SHADER_INTERPOLATED,
                             SHADER_CONTINUOUS)
    fcn.classifyColorRamp(0, band, layer.extent(), provider)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fcn)
    renderer = QgsSingleBandPseudoColorRenderer(provider, band, shader)
    renderer.setClassificationMin(mn)
    renderer.setClassificationMax(mx)
    if old is not None:
        renderer.setOpacity(old.opacity())
    if layer.bandCount() == 1:       # smooth DEM-like rasters (no blocky cells)
        # set BEFORE setRenderer: QGIS snapshots the whole layer style when the
        # renderer changes (Layer Styling undo) and re-applies that snapshot
        from . import hillshade
        hillshade.smooth(layer, hillshade.load().get("smooth_level", 2))
    layer.setRenderer(renderer)
    return "Ramp '%s' applied to band %d of '%s' (%.4g – %.4g)." % (
        entry.name, band, layer.name(), mn, mx)


# --------------------------------------------------------------------------
# Hillshade overlay (the "RAS Mapper look": colour ramp + shaded relief)
# --------------------------------------------------------------------------
HILLSHADE_KEY = "symbol_gallery/hillshade_of"


def is_dem(layer):
    return isinstance(layer, QgsRasterLayer) and layer.isValid() and layer.bandCount() == 1


def wants_hillshade(entry):
    return bool(getattr(entry, "is_ramp", False)) and "relief" in (entry.tags or [])


def hillshade_for(layer):
    """The gallery hillshade layer belonging to *layer*, or None."""
    if layer is None:
        return None
    for lyr in QgsProject.instance().mapLayers().values():
        if lyr.customProperty(HILLSHADE_KEY) == layer.id():
            return lyr
    return None


def add_hillshade(layer, opts=None):
    """Add a live hillshade of *layer* right above it (method "live").

    Returns the new layer (or the existing one, re-configured with *opts*).
    The DEM itself is not changed."""
    from . import hillshade
    if not is_dem(layer):
        raise ApplyError("Select a single-band elevation raster (DEM) first.")
    opts = opts or hillshade.load()
    old = hillshade_for(layer)
    if old is not None:
        hillshade.configure_live(old, layer, opts)
        return old
    hs = QgsRasterLayer(layer.source(), "%s – hillshade" % layer.name(), layer.providerType())
    if not hs.isValid():
        raise ApplyError("Could not open '%s' a second time for the hillshade." % layer.name())
    hillshade.configure_live(hs, layer, opts)
    hs.setCustomProperty(HILLSHADE_KEY, layer.id())
    project = QgsProject.instance()
    project.addMapLayer(hs, False)
    root = project.layerTreeRoot()
    node = root.findLayer(layer.id())
    parent = node.parent() if node is not None else root
    index = parent.children().index(node) if node is not None else 0
    parent.insertLayer(index, hs)            # directly above the DEM
    return hs


def remove_hillshade(layer):
    hs = hillshade_for(layer)
    if hs is not None:
        QgsProject.instance().removeMapLayer(hs.id())
        return True
    return False


def restore(layer, renderer):
    """Undo helper: put a previously cloned renderer back."""
    layer.setRenderer(renderer.clone())
    _done(layer)


# --------------------------------------------------------------------------
# Style Manager / export
# --------------------------------------------------------------------------
def style_name(entry):
    """Unique, readable Style Manager name (pack items carry their pack title)."""
    if entry.id.startswith("pack:") and entry.category.endswith(" (pack)"):
        pack = entry.category.replace(" (pack)", "")
        return "%s%s – %s" % (STYLE_PREFIX, pack, entry.name)
    return STYLE_PREFIX + entry.name


def save_to_style(entries, style=None, favorite=False):
    """Add entries to a QgsStyle (default: the user's Style Manager).

    Returns (added, updated) counts.  Entries are tagged 'Symbol Gallery'
    and with their category, so they are easy to find in any symbol
    selector via the tag filter.
    """
    style = style or QgsStyle.defaultStyle()
    added = updated = 0
    for e in entries:
        name = style_name(e)
        tags = [STYLE_TAG, e.category] + e.tags
        if e.is_ramp:
            existed = name in style.colorRampNames()
            style.addColorRamp(name, e.build(), True)
            etype = STYLE_RAMP
        else:
            existed = name in style.symbolNames()
            style.addSymbol(name, e.build(), True)
            etype = STYLE_SYMBOL
        # tagSymbol only adds tags that are not yet present
        style.tagSymbol(etype, name, tags)
        if favorite:
            style.addFavorite(etype, name)
        if existed:
            updated += 1
        else:
            added += 1
    return added, updated


def export_xml(entries, path):
    """Write entries to a QGIS style XML (importable via Style Manager)."""
    tmp = QgsStyle()
    if not tmp.createMemoryDatabase():
        raise ApplyError("Could not create a temporary style database.")
    save_to_style(entries, tmp)
    if not tmp.exportXml(path):
        raise ApplyError(tmp.errorString() or "Could not write %s" % path)
    return path


# --------------------------------------------------------------------------
# individual classes (categorized / graduated / rule-based)
# --------------------------------------------------------------------------
def _done(layer):
    """Ask QGIS to redraw.  setRenderer() already emitted rendererChanged and
    styleChanged (legend + Layer Styling update), so we must NOT emit them again –
    each extra emission rebuilds every legend icon of the layer."""
    layer.triggerRepaint()


def class_keys(layer, renderer=None):
    """Fast [(key, label, colour)] of a classified layer – no symbol cloning.

    Keys are the legend rule keys (category/range UUIDs, rule keys)."""
    if not isinstance(layer, QgsVectorLayer):
        return []
    r = renderer if renderer is not None else layer.renderer()
    if r is None:
        return []
    out = []
    if isinstance(r, QgsCategorizedSymbolRenderer):
        for c in r.categories():
            sym = c.symbol()
            out.append((c.uuid(), c.label() or str(c.value()) or "(no label)",
                        main_colour(sym)))
        return out
    if isinstance(r, QgsGraduatedSymbolRenderer):
        for g in r.ranges():
            sym = g.symbol()
            out.append((g.uuid(), g.label() or "(no label)", main_colour(sym)))
        return out
    if isinstance(r, QgsRuleBasedRenderer):
        items = r.legendSymbolItems()          # keep the list alive while reading
        return [(it.ruleKey(), it.label() or "(no label)", main_colour(it.symbol()))
                for it in items if it.symbol() is not None]
    return []


def legend_classes(layer):
    """[(key, label, symbol)] for a vector layer with a classified renderer, else []."""
    if not isinstance(layer, QgsVectorLayer) or layer.renderer() is None:
        return []
    r = layer.renderer()
    if not isinstance(r, (QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer,
                          QgsRuleBasedRenderer)):
        return []
    out = []
    items = r.legendSymbolItems()       # keep the list alive while reading it
    for item in items:
        sym = item.symbol()
        if sym is None or not item.ruleKey():
            continue
        # clone: the item (and its symbol) is freed when the list goes away
        out.append((str(item.ruleKey()), item.label() or "(no label)", sym.clone()))
    return out


def apply_to_classes(entry, layer, keys, symbol=None, keep_class_colour=False, base=None):
    """Give each class in *keys* its own copy of the symbol.

    *symbol* (optional) is an edited symbol to use instead of entry.build().
    *base* (optional) is the renderer to start from (live pop-up edits).
    Returns (message, previous_renderer_clone).
    """
    base_renderer = base
    if entry.is_ramp:
        raise ApplyError("Colour ramps apply to the whole layer, not to single classes.")
    ok, reason = check(entry, layer)
    if not ok:
        raise ApplyError(reason)
    classes = dict((k, (lbl, col)) for k, lbl, col in class_keys(layer, base_renderer))
    keys = [k for k in keys if k in classes]
    if not keys:
        raise ApplyError("Select one or more classes first (in the gallery's class list "
                         "or in the Layers panel).")
    base_sym = symbol.clone() if symbol is not None else symbol_for_layer(entry, layer)
    if symbol is not None and _sym_kind(base_sym.type()) == LINE and \
            _sym_kind(_layer_symbol_type(layer)) == FILL:
        base_sym = _line_to_fill(base_sym)
    previous = layer.renderer().clone()
    r = (base_renderer if base_renderer is not None else layer.renderer()).clone()
    keep = _color_layer_index(entry, base_sym)
    for k in keys:
        sym = _cls(base_sym, keep, classes[k][1], keep_class_colour)
        r.setLegendSymbolItem(k, sym)
    layer.setRenderer(r)
    _done(layer)
    if len(keys) == 1:
        msg = "'%s' applied to class '%s'." % (entry.name, classes[keys[0]][0])
    else:
        msg = "'%s' applied to %d classes of '%s'." % (entry.name, len(keys), layer.name())
    return msg, previous


def _line_to_fill(sym):
    fill = QgsFillSymbol()
    fill.deleteSymbolLayer(0)
    for i in range(sym.symbolLayerCount()):
        fill.appendSymbolLayer(sym.symbolLayer(i).clone())
    fill.setOpacity(sym.opacity())
    return fill

# -*- coding: utf-8 -*-
"""
Symbol Bag – applying stored symbols to QGIS layers.

Only the layer *renderer* (symbology) is changed. Data source, geometry,
attributes, labels, fields, CRS and every other layer property is left
untouched. Each apply is recorded so it can be undone.
"""

from contextlib import suppress
from qgis.core import (
    QgsMapLayerStyle,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)

from .storage import KIND_RASTER, KIND_LABELS
from .symbols import apply_raster_style, layer_kind, symbol_from_xml

MODE_REPLACE = "replace"     # switch to a single-symbol renderer
MODE_KEEP_COLORS = "keep"    # keep classes, restyle each class but keep its colour
MODE_SAME = "same"           # keep classes, every class gets exactly this symbol

CLASSIFIED_TYPES = ("categorizedSymbol", "graduatedSymbol", "RuleRenderer")
CLASSIFIED_LABELS = {
    "categorizedSymbol": "Categorized",
    "graduatedSymbol": "Graduated",
    "RuleRenderer": "Rule-based",
}


class ApplyError(Exception):
    pass


def _inner_renderer(renderer):
    """Follow wrappers (point cluster/displacement, inverted polygons…)."""
    seen = 0
    while renderer is not None and hasattr(renderer, "embeddedRenderer") and seen < 5:
        inner = renderer.embeddedRenderer()
        if inner is None:
            break
        renderer = inner
        seen += 1
    return renderer


def classified_type(layer):
    """Return the classified renderer type of a vector layer, or None."""
    if not isinstance(layer, QgsVectorLayer):
        return None
    r = _inner_renderer(layer.renderer())
    if r is not None and r.type() in CLASSIFIED_TYPES:
        return r.type()
    return None


def is_compatible(layer, kind):
    return layer is not None and layer_kind(layer) == kind


def incompatibility_reason(layer, kind):
    if layer is None:
        return "No layer selected."
    lk = layer_kind(layer)
    if lk is None:
        return "'{}' has no geometry/symbology that Symbol Bag can style.".format(layer.name())
    if lk != kind:
        return "'{}' is a {} layer – this is a {} symbol.".format(
            layer.name(), KIND_LABELS.get(lk, lk).lower(), KIND_LABELS.get(kind, kind).lower())
    return ""


def _restyle(renderer, symbol, mode):
    """Modify renderer in place; return a replacement renderer or None."""
    t = renderer.type() if renderer is not None else ""
    if mode == MODE_REPLACE or t not in CLASSIFIED_TYPES:
        if t == "singleSymbol":
            renderer.setSymbol(symbol.clone())
            return None
        return QgsSingleSymbolRenderer(symbol.clone())
    if t == "categorizedSymbol":
        if mode == MODE_KEEP_COLORS:
            renderer.updateSymbols(symbol)
        else:
            for i in range(len(renderer.categories())):
                renderer.updateCategorySymbol(i, symbol.clone())
        renderer.setSourceSymbol(symbol.clone())
        return None
    if t == "graduatedSymbol":
        if mode == MODE_KEEP_COLORS:
            renderer.updateSymbols(symbol)
        else:
            for i in range(len(renderer.ranges())):
                renderer.updateRangeSymbol(i, symbol.clone())
        renderer.setSourceSymbol(symbol.clone())
        return None
    if t == "RuleRenderer":
        for rule in renderer.rootRule().descendants():
            old = rule.symbol()
            if old is None:
                continue
            new = symbol.clone()
            if mode == MODE_KEEP_COLORS:
                new.setColor(old.color())
            rule.setSymbol(new)
        return None
    return QgsSingleSymbolRenderer(symbol.clone())


def apply_vector_symbol(layer, symbol, mode=MODE_KEEP_COLORS):
    renderer = layer.renderer()
    if renderer is None:
        layer.setRenderer(QgsSingleSymbolRenderer(symbol.clone()))
        return
    renderer = renderer.clone()
    # Walk into wrapper renderers so e.g. a point cluster keeps clustering
    chain = [renderer]
    while hasattr(chain[-1], "embeddedRenderer") and chain[-1].embeddedRenderer() is not None and len(chain) < 5:
        chain.append(chain[-1].embeddedRenderer().clone())
    target = chain[-1]
    replacement = _restyle(target, symbol, mode)
    if replacement is not None:
        if len(chain) == 1:
            layer.setRenderer(replacement)
            return
        chain[-1] = replacement
    # re-assemble wrappers from the inside out
    for i in range(len(chain) - 2, -1, -1):
        chain[i].setEmbeddedRenderer(chain[i + 1])
    layer.setRenderer(chain[0])


class LayerStyleUndo(object):
    """Simple undo stack: one entry per apply operation (may cover many layers)."""

    def __init__(self, limit=20):
        self.limit = limit
        self.stack = []

    def snapshot(self, layers):
        entry = []
        for layer in layers:
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            entry.append((layer.id(), style))
        self.stack.append(entry)
        del self.stack[:-self.limit]

    def can_undo(self):
        return bool(self.stack)

    def undo(self):
        if not self.stack:
            return 0
        entry = self.stack.pop()
        restored = 0
        for layer_id, style in entry:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is None:
                continue
            style.writeToLayer(layer)
            layer.triggerRepaint()
            layer.emitStyleChanged()
            restored += 1
        return restored


def apply_to_layers(layers, kind, xml, mode=MODE_KEEP_COLORS, undo=None, raster_stretch=None):
    """Apply a stored definition to several layers.

    Returns (applied_layers, skipped [(layer_name, reason)], warnings).
    """
    applied, skipped, warnings = [], [], []
    targets = []
    for layer in layers:
        reason = incompatibility_reason(layer, kind)
        if reason:
            skipped.append((layer.name() if layer else "?", reason))
        else:
            targets.append(layer)
    if not targets:
        return applied, skipped, warnings
    symbol = None
    if kind != KIND_RASTER:
        symbol = symbol_from_xml(xml)
        if symbol is None:
            raise ApplyError("The stored symbol definition could not be read.")
    if undo is not None:
        undo.snapshot(targets)
    for layer in targets:
        try:
            if kind == KIND_RASTER:
                for w in apply_raster_style(layer, xml, stretch=raster_stretch):
                    warnings.append("{}: {}".format(layer.name(), w))
            else:
                apply_vector_symbol(layer, symbol, mode)
            layer.triggerRepaint()
            layer.emitStyleChanged()
            applied.append(layer)
        except Exception as e:  # keep going for the other layers
            skipped.append((layer.name(), str(e)))
    return applied, skipped, warnings


# ----------------------------------------------------------------------
# applying to individual legend items (categories / ranges / rules)
# ----------------------------------------------------------------------
def renderer_keys(layer):
    """Rule keys of every legend item of a vector layer's renderer."""
    try:
        return {item.ruleKey() for item in layer.renderer().legendSymbolItems()}
    except Exception:
        return set()


def legend_rule_key(legend_node):
    """Rule key of a Layers-panel legend item (category, range or rule), or None."""
    roles = []
    for getter in (lambda: legend_node.RuleKeyRole,
                   lambda: legend_node.LegendNodeRoles.RuleKeyRole):
        try:
            roles.append(getter())
        except AttributeError:
            pass
    try:
        from qgis.core import Qgis as _Q
        roles.insert(0, _Q.LegendNodeRole.RuleKey)
    except AttributeError:
        pass
    for role in roles:
        key = None
        with suppress(Exception):
            key = legend_node.data(role)
        if key:
            return key
    return None


def apply_to_legend_items(targets, kind, xml, undo=None):
    """targets: list of (layer, [rule keys]). Only those classes change –
    every other class, the classification field and the values stay as they are.

    Returns (applied_layers, changed_item_count, skipped [(name, reason)]).
    """
    symbol = symbol_from_xml(xml) if kind != KIND_RASTER else None
    if kind != KIND_RASTER and symbol is None:
        raise ApplyError("The stored symbol definition could not be read.")
    applied, skipped, changed = [], [], 0
    ok_targets = []
    for layer, keys in targets:
        reason = incompatibility_reason(layer, kind)
        if reason:
            skipped.append((layer.name(), reason))
        elif kind == KIND_RASTER or not isinstance(layer, QgsVectorLayer):
            skipped.append((layer.name(), "Raster classes cannot be restyled one by one."))
        else:
            ok_targets.append((layer, keys))
    if undo is not None and ok_targets:
        undo.snapshot([lyr for lyr, _ in ok_targets])
    for layer, keys in ok_targets:
        renderer = layer.renderer().clone()
        valid = {item.ruleKey() for item in renderer.legendSymbolItems()}
        n = 0
        for key in keys:
            if key in valid:
                renderer.setLegendSymbolItem(key, symbol.clone())
                n += 1
        if n:
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            layer.emitStyleChanged()
            applied.append(layer)
            changed += n
        else:
            skipped.append((layer.name(), "The selected class was not found in the layer's renderer."))
    return applied, changed, skipped

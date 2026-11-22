"""
Quick Symbology engine (no UI): read and change a layer's symbols in a few calls.

Two things are supported, the two most used renderers:

* Single symbol - fill colour, outline colour and outline width of the layer's symbol.
* Unique values (a categorized renderer) - one class per value of a field or expression, with a
  colour scheme, ordered classes, editable labels, and an "All other values" class that is always last.

Any symbol edit is made on a copy and handed back to the renderer, so QGIS's own undo/redraw paths
see an ordinary renderer change. Layers drawn with another renderer (graduated, rule-based ...) are
only replaced when the user asks for it.
"""

from qgis.core import (
    NULL,
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsDropShadowEffect,
    QgsPaintEffectRegistry,
    QgsRendererCategory,
    QgsSettings,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsSymbolLayerUtils,
    QgsVectorLayer,
    QgsVectorLayerUtils,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor

from ...core.compat import scoped
from ...core.compat import T_STRING

SETTINGS = "kga_editor/quick_symbol_"
OTHER_LABEL = "All other values"
OTHER_COLOR = "#a0a0a0"
MAX_AUTO_CLASSES = 100            # classes made automatically; the rest are added with the + button
DEFAULT_RAMP = "Spectral"
POINTS_PER_MM = 72.0 / 25.4

SINGLE = "single"
CATEGORIZED = "categorized"
OTHER = "other"


# ------------------------------------------------------------------------------------ layers
def can_symbolize(layer):
    """A valid vector layer with geometry."""
    return (isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer) and layer.isValid()
            and layer.isSpatial() and layer.renderer() is not None)


def renderer_kind(layer):
    renderer = layer.renderer() if layer is not None else None
    if renderer is None:
        return OTHER
    kind = renderer.type()
    if kind == "singleSymbol":
        return SINGLE
    if kind == "categorizedSymbol":
        return CATEGORIZED
    return OTHER


def renderer_name(layer):
    """A plain-words name of the layer's renderer, for messages."""
    names = {"graduatedSymbol": "graduated", "RuleRenderer": "rule-based", "invertedPolygonRenderer": "inverted polygon",
             "pointCluster": "point cluster", "pointDisplacement": "point displacement", "heatmapRenderer": "heatmap",
             "25dRenderer": "2.5D", "nullSymbol": "no symbols", "mergedFeatureRenderer": "merged features"}
    renderer = layer.renderer()
    return names.get(renderer.type(), renderer.type()) if renderer is not None else "unknown"


def default_symbol(layer):
    return QgsSymbol.defaultSymbol(layer.geometryType())


def base_symbol(layer):
    """A copy of the symbol the layer is drawn with now (the first one, for renderers with many)."""
    renderer = layer.renderer()
    kind = renderer_kind(layer)
    symbol = None
    if kind == SINGLE:
        symbol = renderer.symbol()
    elif kind == CATEGORIZED:
        symbol = renderer.sourceSymbol()
        if symbol is None:
            symbol = _symbol_copy(renderer, 0)
    elif renderer is not None:
        try:
            from qgis.core import QgsRenderContext
            symbols = renderer.symbols(QgsRenderContext())
            symbol = symbols[0] if symbols else None
        except Exception:
            symbol = None
    return symbol.clone() if symbol is not None else default_symbol(layer)


# ------------------------------------------------------------------------------------ symbol style
_FILL_LIKE = (QgsSimpleFillSymbolLayer, QgsSimpleMarkerSymbolLayer)


def _simple_layers(symbol):
    return [sl for sl in (symbol.symbolLayer(i) for i in range(symbol.symbolLayerCount()))
            if isinstance(sl, _FILL_LIKE + (QgsSimpleLineSymbolLayer,))]


def symbol_kind(symbol):
    """'fill', 'line' or 'marker'."""
    return {Qgis.SymbolType.Fill: "fill", Qgis.SymbolType.Line: "line"}.get(symbol.type(), "marker")


def _to_points(width, unit):
    if unit == Qgis.RenderUnit.Millimeters:
        return width * POINTS_PER_MM
    if unit == Qgis.RenderUnit.Inches:
        return width * 72.0
    return width                      # points, or a unit with no fixed size (shown as typed)


def read_style(symbol):
    """
    What the quick editor shows for a symbol:
    {'kind', 'fill': QColor|None, 'outline': QColor|None, 'width': points|None, 'editable': bool}.
    'editable' is False when the symbol has no simple layer, so only its main colour can be changed.
    """
    kind = symbol_kind(symbol)
    layers = _simple_layers(symbol)
    if not layers:
        return {"kind": kind, "fill": QColor(symbol.color()), "outline": None, "width": None, "editable": False}
    first = layers[0]
    if isinstance(first, QgsSimpleLineSymbolLayer):
        return {"kind": kind, "fill": None, "outline": QColor(first.color()),
                "width": _to_points(first.width(), first.widthUnit()), "editable": True}
    return {"kind": kind, "fill": QColor(first.fillColor()), "outline": QColor(first.strokeColor()),
            "width": _to_points(first.strokeWidth(), first.strokeWidthUnit()), "editable": True}


def apply_style(symbol, fill=None, outline=None, width=None):
    """Change a symbol in place. None means leave that part alone. Width is in points."""
    layers = _simple_layers(symbol)
    if not layers:
        if fill is not None:
            symbol.setColor(fill)
        return symbol
    for sl in layers:
        if isinstance(sl, QgsSimpleLineSymbolLayer):
            if outline is not None:
                sl.setColor(outline)
            if width is not None:
                sl.setWidthUnit(Qgis.RenderUnit.Points)
                sl.setWidth(float(width))
            continue
        if fill is not None:
            sl.setFillColor(fill)
        if outline is not None:
            sl.setStrokeColor(outline)
        if width is not None:
            sl.setStrokeWidthUnit(Qgis.RenderUnit.Points)
            sl.setStrokeWidth(float(width))
    return symbol


def preview_pixmap(symbol, width, height):
    return QgsSymbolLayerUtils.symbolPreviewPixmap(symbol, QSize(width, height))


# ------------------------------------------------------------------------------------ drop shadow
# The shadow is the layer's "draw effect" (Layer Properties > Symbology > Draw effects): one effect for the
# whole layer, so it covers every class of a unique-value map alike.
def _effect_stack(renderer):
    stack = renderer.paintEffect()
    return stack if stack is not None and stack.type() == "effectStack" else None


def _shadow_of(stack):
    if stack is None:
        return None
    for i in range(stack.count()):
        effect = stack.effect(i)
        if effect.type() == "dropShadow":
            return effect
    return None


def _is_default(shadow):
    """True when a drop shadow still has QGIS's own settings (the on/off flag aside)."""
    fresh = QgsDropShadowEffect()
    mine = {k: v for k, v in shadow.properties().items() if k != "enabled"}
    return mine == {k: v for k, v in fresh.properties().items() if k != "enabled"}


def has_shadow(layer):
    """True when the layer is drawn with a drop shadow."""
    stack = _effect_stack(layer.renderer())
    shadow = _shadow_of(stack)
    return bool(stack is not None and stack.enabled() and shadow is not None and shadow.enabled())


def set_shadow(layer, on):
    """
    Switch the layer's drop shadow on or off. Switching on uses a soft black shadow (1.5 mm down and to the
    right) unless the layer's drop shadow was already set up in Layer Properties, which is left as it is.
    """
    renderer = layer.renderer()
    stack = _effect_stack(renderer)
    if stack is None:
        renderer.setPaintEffect(QgsPaintEffectRegistry.defaultStack())
        stack = _effect_stack(renderer)
    shadow = _shadow_of(stack)
    if on:
        stack.setEnabled(True)
        if shadow is None:
            stack.insertEffect(0, QgsDropShadowEffect())          # under the symbol itself
            shadow = _shadow_of(stack)
        if _is_default(shadow):                                          # never set up: use a softer look
            shadow.setColor(QColor(0, 0, 0))
            shadow.setOpacity(0.5)
            shadow.setOffsetUnit(Qgis.RenderUnit.Millimeters)
            shadow.setOffsetDistance(1.5)
            shadow.setBlurLevel(3)
        shadow.setEnabled(True)
    elif shadow is not None:
        shadow.setEnabled(False)
    _refresh(layer)


def _carry_effect(layer, new_renderer):
    """A renderer that replaces the layer's current one keeps its draw effects (call before setRenderer)."""
    old = layer.renderer()
    effect = old.paintEffect() if old is not None else None
    if effect is not None:
        new_renderer.setPaintEffect(effect.clone())


# ------------------------------------------------------------------------------------ colour schemes
def ramp_names():
    return list(QgsStyle.defaultStyle().colorRampNames())


def default_ramp_name():
    saved = QgsSettings().value(SETTINGS + "ramp", "", type=str)
    names = ramp_names()
    if saved in names:
        return saved
    return DEFAULT_RAMP if DEFAULT_RAMP in names else (names[0] if names else "")


def remember_ramp(name):
    QgsSettings().setValue(SETTINGS + "ramp", name)


def make_ramp(name):
    ramp = QgsStyle.defaultStyle().colorRamp(name)
    if ramp is None:
        from qgis.core import QgsGradientColorRamp
        ramp = QgsGradientColorRamp(QColor("#2b83ba"), QColor("#d7191c"))
    return ramp


def ramp_color(ramp, index, count):
    """The colour for class `index` of `count`, spread evenly along the ramp."""
    return ramp.color(0.5 if count <= 1 else index / (count - 1))


# ------------------------------------------------------------------------------------ values
def is_plain_field(layer, text):
    return layer.fields().lookupField(text) >= 0


def text_fields(layer):
    return [f.name() for f in layer.fields() if f.type() == T_STRING]


def default_field(layer):
    """The field a new unique-value map starts with: the first text field, else the first field."""
    keys = set(layer.primaryKeyAttributes()) if hasattr(layer, "primaryKeyAttributes") else set()
    for i, f in enumerate(layer.fields()):
        if f.type() == T_STRING and i not in keys:
            return f.name()
    for i, f in enumerate(layer.fields()):
        if i not in keys:
            return f.name()
    return layer.fields()[0].name() if layer.fields().count() else None


def _sort_key(value):
    if isinstance(value, bool):
        return (0, int(value), "")
    if isinstance(value, (int, float)):
        return (0, value, "")
    return (1, 0, str(value).casefold())


def unique_values(layer, text):
    """The distinct, non-empty values of a field or expression, sorted."""
    if is_plain_field(layer, text):
        raw = layer.uniqueValues(layer.fields().lookupField(text))
    else:
        raw, _ok = QgsVectorLayerUtils.getValues(layer, text)
    seen, values = set(), []
    for v in raw:
        if v is None or v == NULL or v == "":
            continue
        key = (type(v).__name__, str(v))
        if key in seen:
            continue
        seen.add(key)
        values.append(v)
    return sorted(values, key=_sort_key)


# ------------------------------------------------------------------------------------ categories
def _symbol_copy(renderer, index):
    """
    A copy of the symbol of class `index`, or None. categories() hands back COPIES, and a category owns
    its symbol: the category must stay alive while its symbol is used, so it is held in a local here.
    """
    categories = renderer.categories()
    if not 0 <= index < len(categories):
        return None
    category = categories[index]
    return category.symbol().clone()


def is_other(category):
    value = category.value()
    return value is None or value == NULL or value == ""


def other_index(renderer):
    for i, cat in enumerate(renderer.categories()):
        if is_other(cat):
            return i
    return -1


def class_index(renderer, key):
    """Index of the category whose legend key (uuid) is `key`, or -1."""
    for i, cat in enumerate(renderer.categories()):
        if cat.uuid() == key:
            return i
    return -1


def _other_category(base):
    symbol = base.clone()
    symbol.setColor(QColor(OTHER_COLOR))
    return QgsRendererCategory("", symbol, OTHER_LABEL)


def _refresh(layer):
    layer.triggerRepaint()
    layer.emitStyleChanged()


def _classes(layer):
    return layer.renderer().categories()


class QuickSymbols:
    """Switches a layer between single symbol and unique values, and edits the classes."""

    def __init__(self):
        self._single = {}                    # layer id -> the single symbol the layer had before unique values

    # ---------------------------------------------------------------- single <-> unique values
    def to_unique(self, layer, text=None, ramp_name=None, max_classes=MAX_AUTO_CLASSES):
        """
        Draw the layer with unique values of `text` (default: the first text field). Returns
        (field, number of classes, truncated) or None when the layer has no field.
        """
        text = text or default_field(layer)
        if not text:
            return None
        kind = renderer_kind(layer)
        if kind == SINGLE:
            self._single[layer.id()] = layer.renderer().symbol().clone()
        base = base_symbol(layer)
        ramp = make_ramp(ramp_name or default_ramp_name())
        values = unique_values(layer, text)
        truncated = len(values) > max_classes
        values = values[:max_classes]
        renderer = QgsCategorizedSymbolRenderer(text)
        renderer.setSourceSymbol(base.clone())
        renderer.setSourceColorRamp(ramp.clone())
        for i, value in enumerate(values):
            symbol = base.clone()
            symbol.setColor(ramp_color(ramp, i, len(values)))
            renderer.addCategory(QgsRendererCategory(value, symbol, str(value)))
        renderer.addCategory(_other_category(base))
        _carry_effect(layer, renderer)
        layer.setRenderer(renderer)
        _refresh(layer)
        return text, len(values), truncated

    def to_single(self, layer):
        """Back to a single symbol: the one the layer had before, or the layer's base symbol."""
        symbol = self._single.pop(layer.id(), None)
        if symbol is None:
            symbol = base_symbol(layer)
        renderer = QgsSingleSymbolRenderer(symbol.clone())
        _carry_effect(layer, renderer)
        layer.setRenderer(renderer)
        _refresh(layer)

    def replace_with_single(self, layer):
        """Replace any other renderer with a single symbol made from its first symbol."""
        renderer = QgsSingleSymbolRenderer(base_symbol(layer))
        _carry_effect(layer, renderer)
        layer.setRenderer(renderer)
        _refresh(layer)

    def forget(self, removed):
        for item in removed:
            self._single.pop(item if isinstance(item, str) else item.id(), None)

    # ---------------------------------------------------------------- single symbol / one class
    @staticmethod
    def symbol_of(layer, key=None):
        """A copy of the layer's single symbol, or of class `key`'s symbol."""
        renderer = layer.renderer()
        if key is None:
            return renderer.symbol().clone() if renderer.type() == "singleSymbol" else None
        return _symbol_copy(renderer, class_index(renderer, key))

    @staticmethod
    def set_symbol(layer, symbol, key=None):
        renderer = layer.renderer()
        if key is None:
            if renderer.type() != "singleSymbol":
                return False
            renderer.setSymbol(symbol.clone())
        else:
            i = class_index(renderer, key)
            if i < 0:
                return False
            renderer.updateCategorySymbol(i, symbol.clone())
        _refresh(layer)
        return True

    # ---------------------------------------------------------------- classes
    @staticmethod
    def set_field(layer, text, ramp_name=None, max_classes=MAX_AUTO_CLASSES):
        """Another field or expression: the classes are made again (symbol edits on classes are lost)."""
        renderer = layer.renderer()
        base = renderer.sourceSymbol().clone() if renderer.sourceSymbol() is not None else base_symbol(layer)
        ramp = make_ramp(ramp_name or default_ramp_name())
        values = unique_values(layer, text)
        truncated = len(values) > max_classes
        values = values[:max_classes]
        new = QgsCategorizedSymbolRenderer(text)
        new.setSourceSymbol(base.clone())
        new.setSourceColorRamp(ramp.clone())
        for i, value in enumerate(values):
            symbol = base.clone()
            symbol.setColor(ramp_color(ramp, i, len(values)))
            new.addCategory(QgsRendererCategory(value, symbol, str(value)))
        new.addCategory(_other_category(base))
        _carry_effect(layer, new)
        layer.setRenderer(new)
        _refresh(layer)
        return len(values), truncated

    @staticmethod
    def set_scheme(layer, ramp_name):
        """Recolour the classes along another colour ramp ('All other values' keeps its grey)."""
        renderer = layer.renderer()
        ramp = make_ramp(ramp_name)
        real = [i for i, c in enumerate(renderer.categories()) if not is_other(c)]
        for n, i in enumerate(real):
            symbol = _symbol_copy(renderer, i)
            symbol.setColor(ramp_color(ramp, n, len(real)))
            renderer.updateCategorySymbol(i, symbol)
        renderer.setSourceColorRamp(ramp.clone())
        remember_ramp(ramp_name)
        _refresh(layer)

    @staticmethod
    def addable(layer, ramp_name=None):
        """
        Values of the layer's field that are not classes yet, with the colour each would get:
        [(value, QColor)], colours spread along the colour scheme across all of them.
        """
        renderer = layer.renderer()
        have = {(type(c.value()).__name__, str(c.value())) for c in renderer.categories() if not is_other(c)}
        values = [v for v in unique_values(layer, renderer.classAttribute())
                  if (type(v).__name__, str(v)) not in have]
        ramp = make_ramp(ramp_name or default_ramp_name())
        return [(v, ramp_color(ramp, i, len(values))) for i, v in enumerate(values)]

    @staticmethod
    def add_values(layer, items):
        """Add classes for [(value, QColor)] just before 'All other values'. Returns how many were added."""
        renderer = layer.renderer()
        base = renderer.sourceSymbol().clone() if renderer.sourceSymbol() is not None else base_symbol(layer)
        had_other = other_index(renderer) >= 0
        added = 0
        for value, color in items:
            symbol = base.clone()
            symbol.setColor(QColor(color))
            renderer.addCategory(QgsRendererCategory(value, symbol, str(value)))
            added += 1
        if had_other:                              # addCategory appends: put 'All other values' back last
            i = other_index(renderer)
            last = len(renderer.categories()) - 1
            if i != last and i >= 0:
                renderer.moveCategory(i, last)
        _refresh(layer)
        return added

    @staticmethod
    def remove(layer, key):
        renderer = layer.renderer()
        i = class_index(renderer, key)
        if i < 0 or is_other(renderer.categories()[i]):
            return False                           # 'All other values' stays
        renderer.deleteCategory(i)
        _refresh(layer)
        return True

    @staticmethod
    def move(layer, key, delta):
        """Move a class up (-1) or down (+1); never past 'All other values'. Returns True if it moved."""
        renderer = layer.renderer()
        i = class_index(renderer, key)
        if i < 0 or is_other(renderer.categories()[i]):
            return False
        j = i + delta
        limit = other_index(renderer)
        limit = len(renderer.categories()) if limit < 0 else limit
        if j < 0 or j >= limit:
            return False
        renderer.moveCategory(i, j)
        _refresh(layer)
        return True

    @staticmethod
    def set_label(layer, key, text):
        renderer = layer.renderer()
        i = class_index(renderer, key)
        if i < 0:
            return False
        renderer.updateCategoryLabel(i, text)
        _refresh(layer)
        return True


# ------------------------------------------------------------------------------------ selected class
def selected_class_key(view, layer):
    """
    The legend key of the class selected in the Layers panel, when it belongs to `layer`'s unique-value
    symbols; otherwise None. `view` is QGIS's QgsLayerTreeView.
    """
    if view is None or layer is None or renderer_kind(layer) != CATEGORIZED:
        return None
    try:
        node = view.currentLegendNode()
    except Exception:
        return None
    if node is None:
        return None
    try:
        from qgis.core import QgsLayerTreeModelLegendNode
        key = node.data(scoped(QgsLayerTreeModelLegendNode, 'CustomRole', 'RuleKeyRole'))
        owner = node.layerNode().layer() if node.layerNode() is not None else None
    except Exception:
        return None
    if owner is not None and owner.id() != layer.id():
        return None
    return key if key and class_index(layer.renderer(), key) >= 0 else None

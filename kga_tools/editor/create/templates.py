"""
Feature templates for the Create Features pane.

A template is "a layer + the look and values new features get": one per category of a Categorized
layer, one per rule of a Rule-based layer, one for any other layer - the same idea as the
templates in ArcGIS Pro's Create Features pane. A template can carry a name, a description, tags,
a default tool and default attribute values; those edits live in the layer's custom properties, so
they are saved with the project.
"""

import json
from contextlib import suppress
from dataclasses import dataclass, field

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsProject,
    QgsRenderContext,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
)
from qgis.PyQt import sip

from ..sketch.templates import _base_renderer, _equalities, _field_name, _is_null

GEOM_POINT = Qgis.GeometryType.Point
GEOM_LINE = Qgis.GeometryType.Line
GEOM_POLYGON = Qgis.GeometryType.Polygon
GEOMETRIES = (GEOM_POINT, GEOM_LINE, GEOM_POLYGON)
CAP_ADD_FEATURES = Qgis.VectorProviderCapability.AddFeatures

OVERRIDES_PROPERTY = "kga_create/templates"     # layer custom property: JSON {template id: overrides}
FAVORITES_ENTRY = ("kga_create", "favorites")   # project entry: JSON list of template keys

GEOMETRY_NAMES = {GEOM_POINT: "Point", GEOM_LINE: "Line", GEOM_POLYGON: "Polygon"}


@dataclass
class Template:
    layer_id: str
    ident: str                      # "layer" | "cat:<value>" | "rule:<key>" - stable inside the layer
    label: str                      # name from the symbology
    symbol: object = None           # QgsSymbol (a clone; owned here) or None
    attrs: dict = field(default_factory=dict)    # {field index: value} from the category / rule
    note: str = ""                  # why the category cannot set a value, if so
    name: str = ""                  # user's own name (override), else ""
    description: str = ""
    tags: list = field(default_factory=list)
    tool: str = ""                  # default tool id, "" = first tool of the geometry type
    defaults: dict = field(default_factory=dict)  # {field name: text typed in the properties page}

    @property
    def key(self):
        return f"{self.layer_id}::{self.ident}"

    @property
    def title(self):
        return self.name or self.label


def digitizable(layer):
    """A point / line / polygon vector layer that can take new features."""
    if not isinstance(layer, QgsVectorLayer) or sip.isdeleted(layer) or not layer.isValid():
        return False
    if layer.geometryType() not in GEOMETRIES or layer.readOnly():
        return False
    try:
        return bool(layer.dataProvider().capabilities() & CAP_ADD_FEATURES)
    except Exception:
        return False


def project_layers():
    """Digitizable layers of the project, in Layers-panel order."""
    out = []
    for node in QgsProject.instance().layerTreeRoot().findLayers():
        layer = node.layer()
        if digitizable(layer):
            out.append(layer)
    return out


def _convert(layer, name, value):
    """(field index, value converted to the field's type) or None when it does not fit."""
    idx = layer.fields().lookupField(name)
    if idx < 0:
        return None
    fld = layer.fields().at(idx)
    try:
        converted = fld.convertCompatible(value)
        if isinstance(converted, tuple):
            converted = converted[-1]
    except Exception:
        return None
    return idx, converted


def _clone(symbol):
    return symbol.clone() if symbol is not None else None


def _category_template(layer, cat, cats):
    value = cat.value()
    if isinstance(value, (list, tuple)):            # a merged category: its first value
        value = value[0] if value else None
    empty = _is_null(value)
    renderer = _base_renderer(layer.renderer())
    name = _field_name(layer, renderer.classAttribute())
    ident = f"cat:{value}" if not empty else ("cat:<null>" if value is None else "cat:<empty>")
    label = cat.label() or ("<Null>" if empty else str(value))
    tmpl = Template(layer.id(), ident, label, _clone(cat.symbol()))
    if name is None:
        tmpl.note = "the categories use an expression, not a field"
        return tmpl
    if empty:                                        # "<Null>": new features leave the field empty
        tmpl.attrs = {layer.fields().lookupField(name): None if value is None else ""}
        return tmpl
    got = _convert(layer, name, value)
    if got is None:
        tmpl.note = f"'{value}' does not fit field '{name}'"
        return tmpl
    tmpl.attrs = {got[0]: got[1]}
    return tmpl


def _rule_template(layer, rule):
    pairs, note, node = [], "", rule
    while node is not None and node.parent() is not None:        # this rule and its parents
        if node.isElse():
            return None
        text = node.filterExpression()
        if text:
            found = _equalities(text)
            if found is None:
                pairs, note = [], "the rule is not a simple  field = value  rule"
                break
            pairs = found + pairs
        node = node.parent()
    tmpl = Template(layer.id(), f"rule:{rule.ruleKey()}", rule.label() or rule.description() or "Rule",
                    _clone(rule.symbol()), note=note)
    for name, value in pairs:
        got = _convert(layer, name, value)
        if got is not None:
            tmpl.attrs[got[0]] = got[1]
    return tmpl


def _rules(rule):
    """Rules with a symbol, parents before children (a parent that only groups has no symbol)."""
    for child in rule.children():
        if child.symbol() is not None and not child.isElse():
            yield child
        yield from _rules(child)


def layer_templates(layer):
    """The templates of one layer (at least one), with the user's saved edits applied."""
    renderer = _base_renderer(layer.renderer()) if layer.renderer() is not None else None
    out = []
    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        cats = renderer.categories()
        out = [t for t in (_category_template(layer, c, cats) for c in cats) if t is not None]
    elif isinstance(renderer, QgsRuleBasedRenderer):
        out = [t for t in (_rule_template(layer, r) for r in _rules(renderer.rootRule())) if t is not None]
    if not out:
        symbol = None
        with suppress(Exception):
            symbol = _clone(renderer.symbol()) if renderer is not None and renderer.symbol() else None
        if symbol is None:
            with suppress(Exception):
                symbols = renderer.symbols(QgsRenderContext())
                symbol = _clone(symbols[0]) if symbols else None
        out = [Template(layer.id(), "layer", layer.name(), symbol)]
    saved = load_overrides(layer)
    unique, seen = [], set()
    for tmpl in out:
        if tmpl.ident in seen:                       # two categories with one value: keep the first
            continue
        seen.add(tmpl.ident)
        apply_overrides(tmpl, saved.get(tmpl.ident))
        unique.append(tmpl)
    return unique


# ---------------------------------------------------------------- user edits (saved in the project)
def load_overrides(layer):
    try:
        data = json.loads(layer.customProperty(OVERRIDES_PROPERTY, "") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def apply_overrides(tmpl, data):
    if not isinstance(data, dict):
        return
    tmpl.name = str(data.get("name", "") or "")
    tmpl.description = str(data.get("description", "") or "")
    tags = data.get("tags", [])
    tmpl.tags = [str(t) for t in tags] if isinstance(tags, list) else []
    tmpl.tool = str(data.get("tool", "") or "")
    defaults = data.get("defaults", {})
    tmpl.defaults = {str(k): v for k, v in defaults.items()} if isinstance(defaults, dict) else {}


def save_overrides(layer, tmpl):
    """Write the template's edits into the layer; nothing is stored when it has none."""
    saved = load_overrides(layer)
    data = {"name": tmpl.name, "description": tmpl.description, "tags": tmpl.tags,
            "tool": tmpl.tool, "defaults": tmpl.defaults}
    if any(data.values()):
        saved[tmpl.ident] = data
    else:
        saved.pop(tmpl.ident, None)
    if saved:
        layer.setCustomProperty(OVERRIDES_PROPERTY, json.dumps(saved))
    else:
        layer.removeCustomProperty(OVERRIDES_PROPERTY)


def effective_attributes(layer, tmpl):
    """{field index: value} a new feature gets: the category's value, then the user's defaults."""
    attrs = dict(tmpl.attrs)
    for name, text in tmpl.defaults.items():
        if text is None or str(text) == "":
            continue
        got = _convert(layer, name, text)
        if got is not None:
            attrs[got[0]] = got[1]
    return attrs


def default_problems(layer, tmpl):
    """Names of fields whose typed default does not fit the field's type."""
    bad = []
    for name, text in tmpl.defaults.items():
        if text is None or str(text) == "":
            continue
        idx = layer.fields().lookupField(name)
        if idx < 0:
            continue
        got = _convert(layer, name, text)
        if got is None or (got[1] is None) or (hasattr(got[1], "isNull") and got[1].isNull()):
            bad.append(name)
    return bad


# ---------------------------------------------------------------- favorites
def load_favorites():
    text, ok = QgsProject.instance().readEntry(*FAVORITES_ENTRY, "[]")
    try:
        data = json.loads(text) if ok else []
        return [str(k) for k in data] if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def save_favorites(keys):
    QgsProject.instance().writeEntry(*FAVORITES_ENTRY, json.dumps(sorted(set(keys))))

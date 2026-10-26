"""
Category templates: new features take the attribute values of the category (symbol) you select
in the Layers panel - like choosing a feature template in other GIS software.

Example: a layer styled "Categorized" on field z with categories AC and AD. Click "AD" under the
layer in the Layers panel, draw - the new feature gets z = 'AD' and is drawn with the AD symbol.

Supported renderers:
* Categorized on a field                  -> field = category value
* Rule-based with simple rules            -> every  "field" = value  in the rule (joined by AND,
                                             parent rules included)
Anything else (graduated, expressions, "all other values", ELSE rules) sets no attribute.
"""

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsExpression,
    QgsExpressionNodeBinaryOperator,
    QgsExpressionNodeColumnRef,
    QgsExpressionNodeLiteral,
    QgsExpressionNodeUnaryOperator,
    QgsLayerTreeModelLegendNode,
    QgsRuleBasedRenderer,
)


def _is_null(value):
    if value is None or value == "":
        return True
    try:
        from qgis.PyQt.QtCore import QVariant
        return isinstance(value, QVariant) and value.isNull()
    except ImportError:
        return False


def _rule_key_role():
    for path in (("CustomRole", "RuleKey"),                  # QGIS 3.36+ and QGIS 4
                 ("LegendNodeRoles", "RuleKeyRole")):        # older name
        try:
            return getattr(getattr(QgsLayerTreeModelLegendNode, path[0]), path[1])
        except AttributeError:
            continue
    return None


def current_legend_node(iface, layer):
    """The legend node (category) selected in the Layers panel, if it belongs to this layer."""
    try:
        view = iface.layerTreeView()
        node = view.currentLegendNode() if view is not None else None
    except Exception:
        return None
    if node is None:
        return None
    try:
        layer_node = node.layerNode()
        if layer_node is None or layer_node.layer() is None or layer_node.layer().id() != layer.id():
            return None
    except Exception:
        return None
    return node


def _rule_key(node):
    role = _rule_key_role()
    if role is None:
        return None
    try:
        key = node.data(int(role))
    except Exception:
        return None
    return str(key) if key else None


def _field_name(layer, attribute):
    """A renderer attribute that is a plain field (name or "quoted name") -> field name, else None."""
    if not attribute:
        return None
    if layer.fields().lookupField(attribute) >= 0:
        return layer.fields().at(layer.fields().lookupField(attribute)).name()
    exp = QgsExpression(attribute)
    root = exp.rootNode()
    if not exp.hasParserError() and isinstance(root, QgsExpressionNodeColumnRef):
        idx = layer.fields().lookupField(root.name())
        if idx >= 0:
            return layer.fields().at(idx).name()
    return None


def _equalities(expression_text):
    """'"z" = 'AD' AND "n" = 5' -> [('z', 'AD'), ('n', 5)].  None if the rule is not that simple."""
    exp = QgsExpression(expression_text)
    if exp.hasParserError() or exp.rootNode() is None:
        return None
    pairs = []
    ops = QgsExpressionNodeBinaryOperator.BinaryOperator

    def walk(n):
        if not isinstance(n, QgsExpressionNodeBinaryOperator):
            return False
        if n.op() == ops.boAnd:
            return walk(n.opLeft()) and walk(n.opRight())
        if n.op() != ops.boEQ:
            return False
        a, b = n.opLeft(), n.opRight()
        if isinstance(b, QgsExpressionNodeColumnRef):
            a, b = b, a
        if isinstance(a, QgsExpressionNodeColumnRef):
            if isinstance(b, QgsExpressionNodeLiteral):
                pairs.append((a.name(), b.value()))
                return True
            if (isinstance(b, QgsExpressionNodeUnaryOperator)                       # "n" = -5
                    and b.op() == QgsExpressionNodeUnaryOperator.UnaryOperator.uoMinus
                    and isinstance(b.operand(), QgsExpressionNodeLiteral)
                    and isinstance(b.operand().value(), (int, float))):
                pairs.append((a.name(), -b.operand().value()))
                return True
        return False

    return pairs if walk(exp.rootNode()) else None


def _base_renderer(renderer):
    """Inverted polygons, point clusters and point displacement wrap another renderer: use it."""
    for _ in range(5):
        inner = getattr(renderer, "embeddedRenderer", None)
        inner = inner() if callable(inner) else None
        if inner is None:
            break
        renderer = inner
    return renderer


def _category_values(layer, key, node):
    renderer = _base_renderer(layer.renderer())
    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        field = _field_name(layer, renderer.classAttribute())
        if field is None:
            return None, "the categories use an expression, not a field"
        cats = renderer.categories()
        cat = next((c for c in cats if key and c.uuid() == key), None)
        if cat is None:                                      # fall back on the label shown in the panel
            label = node.data(0)
            cat = next((c for c in cats if c.label() == label), None)
        if cat is None:
            return None, None
        value = cat.value()
        if isinstance(value, (list, tuple)):                 # merged category: use its first value
            value = value[0] if value else None
        if _is_null(value):
            # An empty category (NULL / '') and "all other values" share one slot in QGIS: features
            # with an empty value are always drawn with the LAST empty category in the list.
            empties = [c for c in cats if _is_null(c.value())]
            if len(empties) > 1:
                return None, ("this category has no value, so new features are left empty - QGIS draws empty "
                              "values with the last empty category ('all other values'). Give the category a "
                              "value or delete it in Layer Properties > Symbology")
            return None, None                                # "all other values": nothing to set
        return [(field, value)], cat.label() or str(value)
    if isinstance(renderer, QgsRuleBasedRenderer):
        rule = renderer.rootRule().findRuleByKey(key) if key else None
        if rule is None:
            return None, None
        label = rule.label()
        pairs = []
        while rule is not None and rule.parent() is not None:          # this rule and its parents
            if rule.isElse():
                return None, None
            text = rule.filterExpression()
            if text:
                found = _equalities(text)
                if found is None:
                    return None, "the rule is not a simple  field = value  rule"
                pairs = found + pairs
            rule = rule.parent()
        return (pairs or None), label
    return None, None


def category_attributes(iface, layer):
    """
    Attributes for a new feature from the category selected in the Layers panel.
    Returns (attributes {field index: value}, description) - ({}, None) when nothing applies.
    description is e.g. "AD (z = 'AD')", or "Category ... not used: <reason>".
    """
    if layer is None:
        return {}, None
    node = current_legend_node(iface, layer)
    if node is None:
        return {}, None
    try:
        pairs, label = _category_values(layer, _rule_key(node), node)
    except Exception:
        return {}, None
    if not pairs:
        if label:                                            # a reason why it cannot be used
            return {}, f"Selected category not used: {label}."
        return {}, None
    attrs, shown = {}, []
    fields = layer.fields()
    for name, value in pairs:
        idx = fields.lookupField(name)
        if idx < 0:
            continue
        field = fields.at(idx)
        try:
            value = field.convertCompatible(value)
            if isinstance(value, tuple):                     # some builds return (ok, value)
                value = value[-1]
        except Exception:
            return {}, f"Selected category not used: '{value}' does not fit field '{field.name()}'."
        attrs[idx] = value
        shown.append(f"{field.name()} = {value!r}" if isinstance(value, str) else f"{field.name()} = {value}")
    if not attrs:
        return {}, None
    return attrs, f"{label} ({', '.join(shown)})"

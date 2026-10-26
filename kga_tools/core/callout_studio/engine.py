# -*- coding: utf-8 -*-
"""
Callout Label Studio - styling engine.

Builds QgsPalLayerSettings with callouts (simple line, Manhattan, curved,
balloon) from a plain-dict configuration, so the same config can drive the
dialog, user presets (stored as JSON) and the "Copy PyQGIS code" feature.

Written to work on QGIS 3.22 LTR through QGIS 3.4x and QGIS 4 (Qt6):
every enum that moved between versions goes through a small compat helper.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    Qgis,
    QgsUnitTypes,
    QgsMapLayer,
    QgsPalLayerSettings,
    QgsTextFormat,
    QgsTextBufferSettings,
    QgsCallout,
    QgsSimpleLineCallout,
    QgsManhattanLineCallout,
    QgsCurvedLineCallout,
    QgsBalloonCallout,
    QgsLineSymbol,
    QgsSimpleLineSymbolLayer,
    QgsMarkerLineSymbolLayer,
    QgsTemplatedLineSymbolLayerBase,
    QgsMarkerSymbol,
    QgsFillSymbol,
    QgsMargins,
    QgsEffectStack,
    QgsDropShadowEffect,
    QgsDrawSourceEffect,
    QgsVectorLayerSimpleLabeling,
    QgsRuleBasedLabeling,
    QgsWkbTypes,
)
from ..compat import enum_value

# --------------------------------------------------------------------------
# Version-compat helpers
# --------------------------------------------------------------------------


def _ev(owner, enum_name, *members):
    """Return an enum value, trying scoped access (Qt6 / new API) first,
    then unscoped (PyQt5 / old API). Several member spellings may be given."""
    for member in members:
        try:
            return getattr(getattr(owner, enum_name), member)
        except AttributeError:
            pass
        try:
            return getattr(owner, member)
        except AttributeError:
            pass
    raise AttributeError("{}.{}: none of {}".format(owner.__name__, enum_name, members))


def unit_mm():
    return enum_value((Qgis, 'RenderUnit.Millimeters'), (QgsUnitTypes, 'RenderMillimeters'))


def unit_pt():
    return enum_value((Qgis, 'RenderUnit.Points'), (QgsUnitTypes, 'RenderPoints'))


def label_placement(name):
    try:
        return getattr(Qgis.LabelPlacement, name)
    except AttributeError:
        return getattr(QgsPalLayerSettings, name)


def quadrant(name):
    """name like 'AboveRight'."""
    try:
        return getattr(Qgis.LabelQuadrantPosition, name)
    except AttributeError:
        return getattr(QgsPalLayerSettings, "Quadrant" + name)


def geometry_kind(layer):
    """'point', 'line', 'polygon' or None."""
    if layer is None:
        return None
    gt = layer.geometryType()
    pt, ln, pg = (enum_value((Qgis, 'GeometryType.' + name), (QgsWkbTypes, 'GeometryType.%sGeometry' % name),
                             (QgsWkbTypes, '%sGeometry' % name)) for name in ('Point', 'Line', 'Polygon'))
    if gt == pt:
        return "point"
    if gt == ln:
        return "line"
    if gt == pg:
        return "polygon"
    return None


def labeling_style_category():
    return _ev(QgsMapLayer, "StyleCategory", "Labeling")


# --------------------------------------------------------------------------
# Choices shown in the UI  (key, label)
# --------------------------------------------------------------------------

CALLOUT_TYPES = [
    ("simple", "Simple line"),
    ("manhattan", "Manhattan (elbow)"),
    ("curved", "Curved line"),
    ("balloon", "Balloon / speech bubble"),
]

LINE_STYLES = [("solid", "Solid"), ("dash", "Dashed"), ("dot", "Dotted")]

END_MARKERS = [("none", "None"), ("dot", "Dot at feature"), ("arrow", "Arrow at feature")]

ORIENTATIONS = [("auto", "Automatic"), ("cw", "Clockwise"), ("ccw", "Counter-clockwise")]

FEATURE_ANCHORS = [
    ("pole", "Pole of inaccessibility"),
    ("exterior", "Point on exterior"),
    ("surface", "Point on surface"),
    ("centroid", "Centroid"),
]

LABEL_ANCHORS = [
    ("exterior", "Closest point on label"),
    ("centroid", "Label centroid"),
    ("top_left", "Top left"),
    ("top_middle", "Top middle"),
    ("top_right", "Top right"),
    ("middle_left", "Middle left"),
    ("middle_right", "Middle right"),
    ("bottom_left", "Bottom left"),
    ("bottom_middle", "Bottom middle"),
    ("bottom_right", "Bottom right"),
]

PLACEMENTS = [
    ("ring", "Around feature at a distance (fast, auto-avoids overlaps)"),
    ("offset", "Fixed X / Y offset from feature"),
    ("outside", "Outside polygon edge (slow on detailed boundaries)"),
]

QUADRANTS = [
    ("AboveLeft", "Above left"), ("Above", "Above"), ("AboveRight", "Above right"),
    ("Left", "Left"), ("Over", "Centre"), ("Right", "Right"),
    ("BelowLeft", "Below left"), ("Below", "Below"), ("BelowRight", "Below right"),
]

_FEATURE_ANCHOR_ENUM = {
    "pole": "PoleOfInaccessibility",
    "exterior": "PointOnExterior",
    "surface": "PointOnSurface",
    "centroid": "Centroid",
}

_LABEL_ANCHOR_ENUM = {
    "exterior": "LabelPointOnExterior",
    "centroid": "LabelCentroid",
    "top_left": "LabelTopLeft",
    "top_middle": "LabelTopMiddle",
    "top_right": "LabelTopRight",
    "middle_left": "LabelMiddleLeft",
    "middle_right": "LabelMiddleRight",
    "bottom_left": "LabelBottomLeft",
    "bottom_middle": "LabelBottomMiddle",
    "bottom_right": "LabelBottomRight",
}

# --------------------------------------------------------------------------
# Defaults and presets
# --------------------------------------------------------------------------

DEFAULTS = {
    # callout
    "callout_type": "simple",
    "line_color": "#ff37474f",
    "line_width": 0.3,
    "line_style": "solid",
    "casing": True,
    "casing_color": "#ffffffff",
    "end_marker": "dot",
    "marker_size": 1.6,
    "min_length": 0.0,
    "offset_anchor": 0.0,
    "curvature": 15,
    "orientation": "auto",
    "fill_color": "#ffffffff",
    "stroke_color": "#ff546e7a",
    "stroke_width": 0.25,
    "corner_radius": 1.2,
    "wedge_width": 2.5,
    "margin": 1.0,
    "shadow": False,
    "feature_anchor": "pole",
    "label_anchor": "exterior",
    "all_parts": False,
    # text
    "font_family": "",
    "font_size": 9.0,
    "bold": False,
    "italic": False,
    "text_color": "#ff212121",
    "buffer": True,
    "buffer_color": "#ffffffff",
    "buffer_size": 0.8,
    # placement
    "placement": "ring",
    "distance": 5.0,
    "x_offset": 8.0,
    "y_offset": -6.0,
    "quadrant": "AboveRight",
    "show_all": False,
    "scale_limit": False,
    "min_scale": 25000.0,
    # organized layout
    "arrange": "auto",
    "arrange_margin": 6.0,
    "arrange_gap": 1.0,
    "arrange_even": False,
    "arrange_rotate": True,
    # numbered key

    "key_enable": False,
    "key_style": "123",
    "key_order": "reading",
    "key_start": 1,
    "key_look": "badge",
    "key_badge_color": "#ff263238",
    "key_number_color": "#ffffffff",
    "key_number_size": 7.0,
    "key_number_pos": "inside",
    "key_position": "right",
    "key_columns": 2,
    "key_margin": 6.0,
    "key_title": "",
    "key_box": True,
    "key_box_fill": "#ffffffff",
    "key_box_stroke": "#ff37474f",
    "key_on_map": True,
}

# Keys a preset never overrides (user's typography/placement choices survive).
PRESERVED_ON_PRESET = ("font_family", "font_size", "placement", "distance",
                       "x_offset", "y_offset", "quadrant", "show_all",
                       "scale_limit", "min_scale", "arrange", "arrange_margin",
                       "arrange_gap", "arrange_even", "arrange_rotate") + tuple(
                           k for k in DEFAULTS if k.startswith("key_"))

BUILTIN_PRESETS = [
    ("Classic Leader", {
        "callout_type": "simple", "line_color": "#ff37474f", "line_width": 0.3,
        "casing": True, "end_marker": "dot", "text_color": "#ff212121", "buffer": True,
    }),
    ("Manhattan Elbow", {
        "callout_type": "manhattan", "line_color": "#ff1565c0", "line_width": 0.35,
        "casing": True, "end_marker": "dot", "text_color": "#ff0d47a1", "buffer": True,
        "bold": True,
    }),
    ("Curved Arrow", {
        "callout_type": "curved", "line_color": "#ffc62828", "line_width": 0.35,
        "curvature": 20, "casing": True, "end_marker": "arrow", "marker_size": 2.2,
        "text_color": "#ffb71c1c", "buffer": True,
    }),
    ("Speech Balloon", {
        "callout_type": "balloon", "fill_color": "#fffffde7", "stroke_color": "#ff8d6e63",
        "stroke_width": 0.25, "corner_radius": 1.5, "wedge_width": 2.5, "margin": 1.0,
        "shadow": False, "text_color": "#ff3e2723", "buffer": False,
    }),
    ("Dark Badge", {
        "callout_type": "balloon", "fill_color": "#f0263238", "stroke_color": "#ff263238",
        "stroke_width": 0.1, "corner_radius": 2.5, "wedge_width": 2.0, "margin": 1.2,
        "shadow": False, "text_color": "#ffffffff", "buffer": False, "bold": True,
    }),
    ("Imagery Overlay", {
        "callout_type": "simple", "line_color": "#ffffd600", "line_width": 0.45,
        "casing": True, "casing_color": "#cc000000", "end_marker": "arrow", "marker_size": 2.4,
        "text_color": "#ffffffff", "buffer": True, "buffer_color": "#e6000000",
        "buffer_size": 1.0, "bold": True, "icon_bg": "#ff3d4a3a",
    }),
    ("Survey Dashed", {
        "callout_type": "simple", "line_color": "#ff616161", "line_width": 0.2,
        "line_style": "dash", "casing": False, "end_marker": "none",
        "text_color": "#ff424242", "buffer": True, "italic": True,
    }),
    ("Map Inset Box", {
        "callout_type": "balloon", "fill_color": "#ffffffff", "stroke_color": "#ff212121",
        "stroke_width": 0.3, "corner_radius": 0.0, "wedge_width": 1.5, "margin": 1.2,
        "shadow": False, "text_color": "#ff212121", "buffer": False,
    }),
]


def merged(base, overrides):
    out = dict(DEFAULTS)
    out.update(base or {})
    out.update(overrides or {})
    return out


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

_PEN_STYLES = {
    "solid": Qt.PenStyle.SolidLine,
    "dash": Qt.PenStyle.DashLine,
    "dot": Qt.PenStyle.DotLine,
}


def build_line_symbol(cfg):
    width = float(cfg["line_width"])
    layers = []

    if cfg["casing"]:
        casing = QgsSimpleLineSymbolLayer(QColor(cfg["casing_color"]), width + 0.5, Qt.PenStyle.SolidLine)
        casing.setWidthUnit(unit_mm())
        casing.setPenCapStyle(Qt.PenCapStyle.RoundCap)
        casing.setPenJoinStyle(Qt.PenJoinStyle.RoundJoin)
        layers.append(casing)

    main = QgsSimpleLineSymbolLayer(QColor(cfg["line_color"]), width,
                                    _PEN_STYLES.get(cfg["line_style"], Qt.PenStyle.SolidLine))
    main.setWidthUnit(unit_mm())
    main.setPenCapStyle(Qt.PenCapStyle.RoundCap)
    main.setPenJoinStyle(Qt.PenJoinStyle.RoundJoin)
    layers.append(main)

    if cfg["end_marker"] in ("dot", "arrow"):
        # The callout line runs from the label to the feature, so the
        # LAST vertex is the feature end.
        ml = QgsMarkerLineSymbolLayer()
        try:
            ml.setPlacements(Qgis.MarkerLinePlacement.LastVertex)
        except (AttributeError, TypeError):
            ml.setPlacement(enum_value((QgsTemplatedLineSymbolLayerBase, 'Placement.LastVertex'),
                                       (QgsTemplatedLineSymbolLayerBase, 'LastVertex')))
        shape = "circle" if cfg["end_marker"] == "dot" else "filled_arrowhead"
        marker = QgsMarkerSymbol.createSimple({
            "name": shape,
            "size": str(cfg["marker_size"]),
            "size_unit": "MM",
            "outline_style": "solid" if cfg["casing"] else "no",
            "outline_width": "0.2",
            "outline_width_unit": "MM",
        })
        marker.setColor(QColor(cfg["line_color"]))
        try:
            marker.symbolLayer(0).setStrokeColor(QColor(cfg["casing_color"]))
        except AttributeError:
            pass
        ml.setSubSymbol(marker)
        layers.append(ml)

    return QgsLineSymbol(layers)


def build_fill_symbol(cfg):
    fill = QgsFillSymbol.createSimple({
        "style": "solid",
        "outline_style": "solid" if float(cfg["stroke_width"]) > 0 else "no",
        "outline_width": str(cfg["stroke_width"]),
        "outline_width_unit": "MM",
        "joinstyle": "round",
    })
    layer = fill.symbolLayer(0)
    layer.setColor(QColor(cfg["fill_color"]))
    try:
        layer.setStrokeColor(QColor(cfg["stroke_color"]))
    except AttributeError:
        pass
    if cfg["shadow"]:
        stack = QgsEffectStack()
        shadow = QgsDropShadowEffect()
        try:
            shadow.setOffsetDistance(0.6)
            shadow.setOffsetUnit(unit_mm())
            shadow.setOpacity(0.35)
        except AttributeError:
            pass
        stack.appendEffect(shadow)
        stack.appendEffect(QgsDrawSourceEffect())
        layer.setPaintEffect(stack)
    return fill


def build_callout(cfg):
    ctype = cfg["callout_type"]

    if ctype == "balloon":
        c = QgsBalloonCallout()
        c.setFillSymbol(build_fill_symbol(cfg))
        c.setCornerRadius(float(cfg["corner_radius"]))
        c.setCornerRadiusUnit(unit_mm())
        c.setWedgeWidth(float(cfg["wedge_width"]))
        c.setWedgeWidthUnit(unit_mm())
        m = float(cfg["margin"])
        c.setMargins(QgsMargins(m, m, m, m))
        c.setMarginsUnit(unit_mm())
    else:
        cls = {"simple": QgsSimpleLineCallout,
               "manhattan": QgsManhattanLineCallout,
               "curved": QgsCurvedLineCallout}[ctype]
        c = cls()
        c.setLineSymbol(build_line_symbol(cfg))
        c.setMinimumLength(float(cfg["min_length"]))
        c.setMinimumLengthUnit(unit_mm())
        c.setDrawCalloutToAllParts(bool(cfg["all_parts"]))
        try:
            c.setLabelAnchorPoint(_ev(QgsCallout, "LabelAnchorPoint",
                                      _LABEL_ANCHOR_ENUM[cfg["label_anchor"]]))
        except (AttributeError, KeyError):
            pass
        if ctype == "curved":
            c.setCurvature(float(cfg["curvature"]) / 100.0)
            orient = {"auto": ("Automatic", "OrientationAutomatic"),
                      "cw": ("Clockwise", "OrientationClockwise"),
                      "ccw": ("CounterClockwise", "OrientationCounterClockwise")}[cfg["orientation"]]
            try:
                c.setOrientation(_ev(QgsCurvedLineCallout, "Orientation", *orient))
            except AttributeError:
                pass

    try:
        c.setOffsetFromAnchor(float(cfg["offset_anchor"]))
        c.setOffsetFromAnchorUnit(unit_mm())
    except AttributeError:
        pass
    try:
        c.setAnchorPoint(_ev(QgsCallout, "AnchorPoint", _FEATURE_ANCHOR_ENUM[cfg["feature_anchor"]]))
    except (AttributeError, KeyError):
        pass
    c.setEnabled(True)
    return c


def build_text_format(cfg):
    fmt = QgsTextFormat()
    font = QFont(cfg["font_family"]) if cfg["font_family"] else QFont()
    font.setBold(bool(cfg["bold"]))
    font.setItalic(bool(cfg["italic"]))
    fmt.setFont(font)
    fmt.setSize(float(cfg["font_size"]))
    fmt.setSizeUnit(unit_pt())
    fmt.setColor(QColor(cfg["text_color"]))

    buf = QgsTextBufferSettings()
    buf.setEnabled(bool(cfg["buffer"]))
    buf.setSize(float(cfg["buffer_size"]))
    buf.setSizeUnit(unit_mm())
    buf.setColor(QColor(cfg["buffer_color"]))
    try:
        buf.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    except AttributeError:
        pass
    fmt.setBuffer(buf)
    return fmt


def _set_above_below_line(s):
    """Allow line labels above and below the line (API differs per version)."""
    candidates = []
    try:
        F = Qgis.LabelLinePlacementFlag
        candidates.append(lambda: Qgis.LabelLinePlacementFlags(F.AboveLine | F.BelowLine))
        candidates.append(lambda: F.AboveLine | F.BelowLine)
    except AttributeError:
        pass
    try:
        from qgis.core import QgsLabeling
        G = QgsLabeling.LinePlacementFlag
        candidates.append(lambda: QgsLabeling.LinePlacementFlags(G.AboveLine | G.BelowLine))
    except (ImportError, AttributeError):
        pass
    try:
        ls = s.lineSettings()
    except AttributeError:
        ls = None
    if ls is not None:
        for make in candidates:
            try:
                ls.setPlacementFlags(make())
                s.setLineSettings(ls)
                return
            except (TypeError, AttributeError):
                continue
    try:
        s.placementFlags = QgsPalLayerSettings.AboveLine | QgsPalLayerSettings.BelowLine
    except (AttributeError, TypeError):
        pass


def _apply_placement(s, cfg, kind):
    mode = cfg["placement"]
    if kind == "line":
        # Lines: horizontal label floating above/below the line.
        s.placement = label_placement("Horizontal")
        s.dist = float(cfg["distance"])
        s.distUnits = unit_mm()
        _set_above_below_line(s)
        return

    if mode == "offset":
        s.placement = label_placement("OverPoint")
        s.quadOffset = quadrant(cfg["quadrant"])
        s.xOffset = float(cfg["x_offset"])
        s.yOffset = float(cfg["y_offset"])
        s.offsetUnits = unit_mm()
        return

    # ring: "Around centroid" for polygons. QGIS's OutsidePolygons mode tests
    # candidates along every vertex and can take minutes on detailed boundaries,
    # so it is only used when explicitly chosen.
    if kind == "polygon" and mode == "outside":
        s.placement = label_placement("OutsidePolygons")
    else:
        s.placement = label_placement("AroundPoint")
    s.dist = float(cfg["distance"])
    s.distUnits = unit_mm()


def build_label_settings(cfg, expression, is_expression, kind, layout=None):
    s = QgsPalLayerSettings()
    s.fieldName = expression
    s.isExpression = bool(is_expression)
    s.setFormat(build_text_format(cfg))
    s.setCallout(build_callout(cfg))
    _apply_placement(s, cfg, kind)

    if layout:
        from .arrange import apply_layout_properties
        apply_layout_properties(s, layout)

    if cfg["show_all"] or layout:
        try:
            ps = s.placementSettings()
            ps.setOverlapHandling(Qgis.LabelOverlapHandling.AllowOverlapIfRequired)
            ps.setAllowDegradedPlacement(True)
            s.setPlacementSettings(ps)
        except AttributeError:
            s.displayAll = True

    if cfg["scale_limit"]:
        s.scaleVisibility = True
        s.minimumScale = float(cfg["min_scale"])
        s.maximumScale = 0.0
    return s


def build_labeling(cfg, expression, is_expression, kind, filter_expression="", layout=None):
    settings = build_label_settings(cfg, expression, is_expression, kind, layout)
    if filter_expression and filter_expression.strip():
        root = QgsRuleBasedLabeling.Rule(None)
        rule = QgsRuleBasedLabeling.Rule(settings)
        rule.setFilterExpression(filter_expression)
        rule.setDescription("Callout Label Studio")
        root.appendChild(rule)
        return QgsRuleBasedLabeling(root)
    return QgsVectorLayerSimpleLabeling(settings)


def apply_to_layer(layer, cfg, expression, is_expression, filter_expression="", mm_units=None):
    """mm_units: layer-CRS units per millimetre at the current map scale;
    required for organized (column/row) layouts."""
    kind = geometry_kind(layer)
    if cfg.get("key_enable"):
        from .numberkey import build_number_key_labeling
        lab = build_number_key_labeling(layer, cfg, expression, is_expression, filter_expression, mm_units, kind)
        if lab is not None:
            layer.setLabeling(lab)
            layer.setLabelsEnabled(True)
            layer.triggerRepaint()
            return
    layout = None
    if cfg.get("arrange", "auto") != "auto" and mm_units:
        from .arrange import compute_layout
        layout = compute_layout(layer, cfg, expression, is_expression, filter_expression, mm_units, kind)
    layer.setLabeling(build_labeling(cfg, expression, is_expression, kind, filter_expression, layout))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def read_existing_label_field(layer):
    """Return (fieldName, isExpression) of the layer's current simple labeling, if any."""
    lab = layer.labeling() if layer is not None else None
    if isinstance(lab, QgsVectorLayerSimpleLabeling):
        s = lab.settings()
        if s.fieldName:
            return s.fieldName, s.isExpression
    return None


# --------------------------------------------------------------------------
# PyQGIS code generator (teaching aid)
# --------------------------------------------------------------------------

def _rgba(hexargb):
    c = QColor(hexargb)
    return "QColor({}, {}, {}, {})".format(c.red(), c.green(), c.blue(), c.alpha())


def generate_code(cfg, layer_name, expression, is_expression, kind, filter_expression=""):
    L = []
    a = L.append
    a("# Generated by Callout Label Studio — QGIS 3.30+ / QGIS 4 syntax")
    a("from qgis.core import *")
    a("from qgis.PyQt.QtCore import Qt")
    a("from qgis.PyQt.QtGui import QColor, QFont")
    a("")
    a("layer = QgsProject.instance().mapLayersByName({!r})[0]".format(layer_name))
    a("MM = Qgis.RenderUnit.Millimeters")
    a("")
    a("# --- Text -------------------------------------------------------")
    a("fmt = QgsTextFormat()")
    a("font = QFont({!r})".format(cfg["font_family"]) if cfg["font_family"] else "font = QFont()")
    a("font.setBold({}); font.setItalic({})".format(bool(cfg["bold"]), bool(cfg["italic"])))
    a("fmt.setFont(font)")
    a("fmt.setSize({}); fmt.setSizeUnit(Qgis.RenderUnit.Points)".format(cfg["font_size"]))
    a("fmt.setColor({})".format(_rgba(cfg["text_color"])))
    if cfg["buffer"]:
        a("buf = QgsTextBufferSettings()")
        a("buf.setEnabled(True); buf.setSize({}); buf.setSizeUnit(MM)".format(cfg["buffer_size"]))
        a("buf.setColor({})".format(_rgba(cfg["buffer_color"])))
        a("fmt.setBuffer(buf)")
    a("")
    a("# --- Callout ----------------------------------------------------")
    ctype = cfg["callout_type"]
    if ctype == "balloon":
        a("callout = QgsBalloonCallout()")
        a("fill = QgsFillSymbol.createSimple({{'outline_width': '{}', 'outline_width_unit': 'MM'}})".format(cfg["stroke_width"]))
        a("fill.symbolLayer(0).setColor({})".format(_rgba(cfg["fill_color"])))
        a("fill.symbolLayer(0).setStrokeColor({})".format(_rgba(cfg["stroke_color"])))
        if cfg["shadow"]:
            a("fx = QgsEffectStack(); fx.appendEffect(QgsDropShadowEffect()); fx.appendEffect(QgsDrawSourceEffect())")
            a("fill.symbolLayer(0).setPaintEffect(fx)")
        a("callout.setFillSymbol(fill)")
        a("callout.setCornerRadius({}); callout.setCornerRadiusUnit(MM)".format(cfg["corner_radius"]))
        a("callout.setWedgeWidth({}); callout.setWedgeWidthUnit(MM)".format(cfg["wedge_width"]))
        m = cfg["margin"]
        a("callout.setMargins(QgsMargins({0}, {0}, {0}, {0})); callout.setMarginsUnit(MM)".format(m))
    else:
        cls = {"simple": "QgsSimpleLineCallout", "manhattan": "QgsManhattanLineCallout",
               "curved": "QgsCurvedLineCallout"}[ctype]
        a("callout = {}()".format(cls))
        a("layers = []")
        if cfg["casing"]:
            a("casing = QgsSimpleLineSymbolLayer({}, {})".format(_rgba(cfg["casing_color"]), float(cfg["line_width"]) + 0.5))
            a("casing.setWidthUnit(MM); casing.setPenCapStyle(Qt.PenCapStyle.RoundCap); layers.append(casing)")
        pen = {"solid": "SolidLine", "dash": "DashLine", "dot": "DotLine"}[cfg["line_style"]]
        a("line = QgsSimpleLineSymbolLayer({}, {}, Qt.PenStyle.{})".format(_rgba(cfg["line_color"]), cfg["line_width"], pen))
        a("line.setWidthUnit(MM); line.setPenCapStyle(Qt.PenCapStyle.RoundCap); layers.append(line)")
        if cfg["end_marker"] != "none":
            shape = "circle" if cfg["end_marker"] == "dot" else "filled_arrowhead"
            a("tip = QgsMarkerLineSymbolLayer(); tip.setPlacements(Qgis.MarkerLinePlacement.LastVertex)")
            a("m = QgsMarkerSymbol.createSimple({{'name': '{}', 'size': '{}', 'size_unit': 'MM'}})".format(shape, cfg["marker_size"]))
            a("m.setColor({}); tip.setSubSymbol(m); layers.append(tip)".format(_rgba(cfg["line_color"])))
        a("callout.setLineSymbol(QgsLineSymbol(layers))")
        a("callout.setMinimumLength({}); callout.setMinimumLengthUnit(MM)".format(cfg["min_length"]))
        if ctype == "curved":
            a("callout.setCurvature({})".format(float(cfg["curvature"]) / 100.0))
    a("callout.setOffsetFromAnchor({}); callout.setOffsetFromAnchorUnit(MM)".format(cfg["offset_anchor"]))
    a("callout.setAnchorPoint(QgsCallout.AnchorPoint.{})".format(_FEATURE_ANCHOR_ENUM[cfg["feature_anchor"]]))
    a("callout.setEnabled(True)")
    a("")
    a("# --- Label settings ---------------------------------------------")
    a("s = QgsPalLayerSettings()")
    a("s.fieldName = {!r}".format(expression))
    a("s.isExpression = {}".format(bool(is_expression)))
    a("s.setFormat(fmt)")
    a("s.setCallout(callout)")
    if kind == "line":
        a("s.placement = Qgis.LabelPlacement.Horizontal")
        a("s.dist = {}; s.distUnits = MM".format(cfg["distance"]))
    elif cfg["placement"] == "offset":
        a("s.placement = Qgis.LabelPlacement.OverPoint")
        a("s.quadOffset = Qgis.LabelQuadrantPosition.{}".format(cfg["quadrant"]))
        a("s.xOffset = {}; s.yOffset = {}; s.offsetUnits = MM".format(cfg["x_offset"], cfg["y_offset"]))
    else:
        a("s.placement = Qgis.LabelPlacement.{}".format(
            "OutsidePolygons" if kind == "polygon" and cfg["placement"] == "outside" else "AroundPoint"))
        a("s.dist = {}; s.distUnits = MM".format(cfg["distance"]))
    if cfg["show_all"]:
        a("ps = s.placementSettings(); ps.setOverlapHandling(Qgis.LabelOverlapHandling.AllowOverlapIfRequired)")
        a("s.setPlacementSettings(ps)")
    if cfg.get("key_enable"):
        a("# NOTE: the Number key (numbers + key boxes) is computed by Callout Label Studio")
        a("# and is not reproduced by this script; it shows the base label style only.")
    if cfg.get("arrange", "auto") != "auto":
        a("# Organized column/row layout: positions are computed by Callout Label Studio")
        a("# and stored as data-defined PositionX / PositionY / Hali / Vali expressions.")
    if cfg["scale_limit"]:
        a("s.scaleVisibility = True; s.minimumScale = {}; s.maximumScale = 0".format(cfg["min_scale"]))
    a("")
    if filter_expression and filter_expression.strip():
        a("root = QgsRuleBasedLabeling.Rule(None)")
        a("rule = QgsRuleBasedLabeling.Rule(s)")
        a("rule.setFilterExpression({!r})".format(filter_expression))
        a("root.appendChild(rule)")
        a("layer.setLabeling(QgsRuleBasedLabeling(root))")
    else:
        a("layer.setLabeling(QgsVectorLayerSimpleLabeling(s))")
    a("layer.setLabelsEnabled(True)")
    a("layer.triggerRepaint()")
    return "\n".join(L) + "\n"

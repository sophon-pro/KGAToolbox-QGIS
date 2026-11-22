"""
Quick Labeling engine (no UI): label a layer with one field, in one call.

A "quick label" is an ordinary single-label setting (QgsVectorLayerSimpleLabeling) plus a marker
property on the layer, so the toolbar button knows which layers it switched on. Whatever labeling
the layer had before is kept in memory and put back when Quick Labeling is switched off.
"""

import json
import random

from qgis.core import (
    Qgis,
    QgsLabelLineSettings,
    QgsPalLayerSettings,
    QgsSettings,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsTextShadowSettings,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt import sip
from qgis.PyQt.QtGui import QColor, QFont

from ...core.compat import T_STRING

MARK = "kga_editor/quick_label"          # layer custom property: this layer's labels come from Quick Labeling
FORMAT_PROP = "kga_editor/quick_label_format"   # layer custom property: the numeric field + options behind the expression
SETTINGS = "kga_editor/quick_label_"
DEFAULT_SIZE = 10.0
DEFAULT_COLOR = "#000000"
DEFAULT_STYLE = "Regular"
DEFAULT_HALO_SIZE = 1.0                  # points
DEFAULT_HALO_COLOR = "#ffffff"
SHADOW_DISTANCE = 1.5                    # points: a soft black shadow down and to the right
SHADOW_BLUR = 1.5
SHADOW_OPACITY = 0.7
DECIMALS = 2                             # "round to 2 decimals"
# What is written after the number, as expression text. The superscript 2 is char(178) (U+00B2).
# Note: a legacy-encoded Khmer font (e.g. "Khmer MEF1") draws its own symbol for U+00B2 - pick a Unicode font.
# A unit also converts the number: a length field is taken to be in metres and an area field in square metres
# (what Shape_Length / Shape_Area and the measures of a projected layer are), and is multiplied by the factor below.
LENGTH_UNITS = (("m", "m"), ("km", "km"), ("ft", "ft"), ("mi", "mi"))     # (id, text); the first is the default
DEFAULT_LENGTH_UNIT = LENGTH_UNITS[0][0]
LENGTH_FACTOR = {"m": "1", "km": "0.001", "ft": "3.280839895", "mi": "0.000621371192"}   # metres -> the unit
AREA_UNITS = (                                    # (id, text shown in the window); the first is the default
    ("m2", "m\u00b2"), ("km2", "km\u00b2"), ("ha", "ha"), ("ft2", "ft\u00b2"),
    ("yd2", "yd\u00b2"), ("ac", "ac"), ("mi2", "mi\u00b2"),
)
DEFAULT_AREA_UNIT = AREA_UNITS[0][0]
AREA_FACTOR = {                                   # square metres -> the unit, as exact expression text
    "m2": "1", "km2": "0.000001", "ha": "0.0001", "ft2": "10.7639104167",
    "yd2": "1.1959900463", "ac": "0.000247105381", "mi2": "0.000000386102158542",
}
UNIT_EXPRESSION = {_id: "'{}'".format(_text) for _id, _text in LENGTH_UNITS}
for _id, _text in AREA_UNITS:
    UNIT_EXPRESSION[_id] = "'{}'||char(178)".format(_text[:-1]) if _text.endswith("\u00b2") else "'{}'".format(_text)
UNITS = tuple(UNIT_EXPRESSION)                    # every id the window and the layer property may carry
UNIT_FACTOR = dict(LENGTH_FACTOR, **AREA_FACTOR)
LENGTH_IDS = tuple(_id for _id, _text in LENGTH_UNITS)

_rng = random.SystemRandom()


# Where the label sits, per kind of layer: (id, text shown in the window). The first of each is the default.
POSITIONS = {
    "point": (("around", "Around point"), ("above", "Above"), ("below", "Below"), ("left", "Left"),
              ("right", "Right"), ("over", "Over point"), ("cartographic", "Cartographic")),
    "line": (("parallel", "Parallel"), ("above", "Above line"), ("below", "Below line"), ("on", "On line"),
             ("curved", "Curved"), ("horizontal", "Horizontal")),
    "polygon": (("centroid", "Around centroid"), ("over", "Over centroid"), ("horizontal", "Horizontal"),
                ("free", "Free (angled)"), ("perimeter", "Along perimeter"), ("outside", "Outside polygon")),
}


def geometry_kind(layer):
    """'point', 'line' or 'polygon' for a vector layer, else None."""
    if not isinstance(layer, QgsVectorLayer) or sip.isdeleted(layer):
        return None
    return {Qgis.GeometryType.Point: "point", Qgis.GeometryType.Line: "line",
            Qgis.GeometryType.Polygon: "polygon"}.get(layer.geometryType())


def units_allowed(layer):
    """(length unit, area unit) offered for this layer: a line has a length and no area, a polygon an area
    and no length (its perimeter is rarely what is labelled), a point neither."""
    return {"line": (True, False), "polygon": (False, True), "point": (False, False)}.get(geometry_kind(layer),
                                                                                           (True, True))


def positions(layer):
    """The (id, text) choices for the layer's geometry (empty when it has none)."""
    return POSITIONS.get(geometry_kind(layer), ())


def _position_for(layer, wanted):
    """`wanted` if it is a choice for this layer, else the position used last for this kind, else the default."""
    ids = [pid for pid, _text in positions(layer)]
    if not ids:
        return ""
    if wanted in ids:
        return wanted
    saved = QgsSettings().value(SETTINGS + "position_" + geometry_kind(layer), "", type=str)
    return saved if saved in ids else ids[0]


# Which positions take an offset (a distance in points from the feature): the label is pushed away by
# `dist` (around a point or centroid, beside a line, off a perimeter) or, for a point Above/Below/Left/Right,
# by x / y offsets. The others (on a line, horizontal, free, over the centroid) have nothing to offset.
_DISTANCE = {"point": ("around", "cartographic"), "line": ("parallel", "above", "below", "curved"),
             "polygon": ("centroid", "perimeter", "outside")}
_SIDE = {"above": (0, -1), "below": (0, 1), "left": (-1, 0), "right": (1, 0)}      # QGIS: +y is down
DEFAULT_OFFSET = 0.0


def offset_applies(layer_or_kind, position):
    """True when the position can be offset from the feature."""
    kind = layer_or_kind if isinstance(layer_or_kind, str) else geometry_kind(layer_or_kind)
    return position in _DISTANCE.get(kind, ()) or (kind == "point" and position in _SIDE)


def _line_flags(*flags):
    out = Qgis.LabelLinePlacementFlags()
    for flag in flags:
        out |= flag
    return out


def xy_applies(layer_or_kind, position):
    """True when the position takes an X / Y offset (a point label placed by offset from the point)."""
    kind = layer_or_kind if isinstance(layer_or_kind, str) else geometry_kind(layer_or_kind)
    return kind == "point" and (position in _SIDE or position == "over")


def _set_offset(settings, kind, position, offset, x=0.0, y=0.0):
    """Write the offset (points) the way the placement of `position` uses it. `x` / `y` shift a point label
    placed by offset from the point (x to the right, y UP; QGIS itself counts y downwards)."""
    offset = max(0.0, float(offset or 0.0))
    settings.dist = 0.0
    settings.xOffset = settings.yOffset = 0.0
    settings.distUnits = settings.offsetUnits = Qgis.RenderUnit.Points
    if offset_applies(kind, position):
        settings.dist = offset                      # the offset-from-point placements ignore it, so it also stores the value
    if xy_applies(kind, position):
        dx, dy = _SIDE.get(position, (0, 0))
        settings.xOffset = dx * offset + float(x or 0.0)
        settings.yOffset = dy * offset - float(y or 0.0)


def _offset_of(settings, position):
    """(offset, x, y) the label settings carry; x / y only for a point label placed by offset from the point."""
    offset = float(settings.dist or 0.0)
    if position not in _SIDE and position != "over":
        return offset, 0.0, 0.0
    dx, dy = _SIDE.get(position, (0, 0))
    return offset, settings.xOffset - dx * offset, -(settings.yOffset - dy * offset)


def _set_position(settings, kind, position):
    """Write the placement of a position id into the label settings."""
    Placement, Quadrant, Flag = Qgis.LabelPlacement, Qgis.LabelQuadrantPosition, Qgis.LabelLinePlacementFlag
    if kind == "point":
        settings.placement = {"cartographic": Placement.OrderedPositionsAroundPoint,
                              "around": Placement.AroundPoint}.get(position, Placement.OverPoint)
        settings.quadOffset = {"above": Quadrant.Above, "below": Quadrant.Below, "left": Quadrant.Left,
                               "right": Quadrant.Right}.get(position, Quadrant.Over)
    elif kind == "line":
        settings.placement = {"curved": Placement.Curved, "horizontal": Placement.Horizontal}.get(position, Placement.Line)
        line = QgsLabelLineSettings()
        line.setPlacementFlags({"above": _line_flags(Flag.AboveLine, Flag.MapOrientation),
                                "below": _line_flags(Flag.BelowLine, Flag.MapOrientation),
                                "on": _line_flags(Flag.OnLine)}.get(
            position, _line_flags(Flag.AboveLine, Flag.BelowLine, Flag.MapOrientation)))
        settings.setLineSettings(line)
    elif kind == "polygon":
        settings.placement = {"over": Placement.OverPoint, "horizontal": Placement.Horizontal, "free": Placement.Free,
                              "perimeter": Placement.Line, "outside": Placement.OutsidePolygons}.get(position,
                                                                                                    Placement.AroundPoint)
        settings.quadOffset = Quadrant.Over


def _position_of(layer, settings):
    """The position id a layer's label settings stand for (the default when they match none)."""
    kind = geometry_kind(layer)
    if kind is None:
        return ""
    Placement, Quadrant, Flag = Qgis.LabelPlacement, Qgis.LabelQuadrantPosition, Qgis.LabelLinePlacementFlag
    placement = settings.placement
    if kind == "point":
        if placement == Placement.OrderedPositionsAroundPoint:
            return "cartographic"
        if placement == Placement.OverPoint:
            names = {Quadrant.Above: "above", Quadrant.Below: "below", Quadrant.Left: "left", Quadrant.Right: "right"}
            return names.get(settings.quadOffset, "over")
    elif kind == "line":
        if placement == Placement.Curved:
            return "curved"
        if placement == Placement.Horizontal:
            return "horizontal"
        if placement == Placement.Line:
            flags = settings.lineSettings().placementFlags()
            if flags & Flag.OnLine:
                return "on"
            if flags & Flag.AboveLine and flags & Flag.BelowLine:
                return "parallel"
            return "below" if flags & Flag.BelowLine else "above"
    elif kind == "polygon":
        names = {Placement.OverPoint: "over", Placement.Horizontal: "horizontal", Placement.Free: "free",
                 Placement.Line: "perimeter", Placement.OutsidePolygons: "outside"}
        if placement in names:
            return names[placement]
    return POSITIONS[kind][0][0]


# ------------------------------------------------------------------------------------ style
def default_style():
    """The font, style, size and colour used last (or the defaults)."""
    s = QgsSettings()
    family = s.value(SETTINGS + "family", "", type=str) or QFont().family()
    return {
        "family": family,
        "style": s.value(SETTINGS + "style", DEFAULT_STYLE, type=str) or DEFAULT_STYLE,
        "size": s.value(SETTINGS + "size", DEFAULT_SIZE, type=float) or DEFAULT_SIZE,
        "color": s.value(SETTINGS + "color", DEFAULT_COLOR, type=str) or DEFAULT_COLOR,
        "shadow": s.value(SETTINGS + "shadow", False, type=bool),
        "halo": s.value(SETTINGS + "halo", False, type=bool),
        "halo_size": s.value(SETTINGS + "halo_size", DEFAULT_HALO_SIZE, type=float) or DEFAULT_HALO_SIZE,
        "halo_color": s.value(SETTINGS + "halo_color", DEFAULT_HALO_COLOR, type=str) or DEFAULT_HALO_COLOR,
        "offset": s.value(SETTINGS + "offset", 0.0, type=float),
    }


def remember_style(style):
    s = QgsSettings()
    s.setValue(SETTINGS + "family", style["family"])
    s.setValue(SETTINGS + "style", style["style"])
    s.setValue(SETTINGS + "size", float(style["size"]))
    s.setValue(SETTINGS + "color", QColor(style["color"]).name(QColor.NameFormat.HexArgb))
    s.setValue(SETTINGS + "shadow", bool(style.get("shadow", False)))
    s.setValue(SETTINGS + "halo", bool(style.get("halo", False)))
    s.setValue(SETTINGS + "halo_size", float(style.get("halo_size", DEFAULT_HALO_SIZE)))
    s.setValue(SETTINGS + "halo_color", QColor(style.get("halo_color", DEFAULT_HALO_COLOR)).name(QColor.NameFormat.HexArgb))


def shadow_settings(enabled, base=None):
    """Quick Labeling's drop shadow (soft black, down and right); `base` keeps the parameters it already has."""
    if base is not None and base.enabled() == bool(enabled):
        return QgsTextShadowSettings(base)
    shadow = QgsTextShadowSettings()
    shadow.setEnabled(bool(enabled))
    shadow.setOffsetAngle(135)
    shadow.setOffsetDistance(SHADOW_DISTANCE)
    shadow.setOffsetUnit(Qgis.RenderUnit.Points)
    shadow.setBlurRadius(SHADOW_BLUR)
    shadow.setBlurRadiusUnit(Qgis.RenderUnit.Points)
    shadow.setOpacity(SHADOW_OPACITY)
    shadow.setColor(QColor("#000000"))
    return shadow


def _format(style, base=None):
    """The text format for `style`. With `base` (the format already on the layer) everything the window does
    not manage - background, mask, wrapping, the Label Bag's tweaks - is kept."""
    font = QFont(style["family"])
    if style.get("style"):
        font.setStyleName(style["style"])
    fmt = QgsTextFormat(base) if base is not None else QgsTextFormat()
    fmt.setFont(font)
    fmt.setSize(float(style["size"]))
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(style["color"]))
    halo = QgsTextBufferSettings(fmt.buffer()) if base is not None else QgsTextBufferSettings()   # the halo is QGIS's text "buffer"
    halo.setEnabled(bool(style.get("halo", False)))
    halo.setSize(float(style.get("halo_size", DEFAULT_HALO_SIZE)))
    halo.setSizeUnit(Qgis.RenderUnit.Points)
    halo.setColor(QColor(style.get("halo_color", DEFAULT_HALO_COLOR)))
    fmt.setBuffer(halo)
    shadow = QgsTextShadowSettings()                     # "Drop shadow": QGIS's text shadow, a soft black one
    if base is not None and fmt.shadow().enabled() == bool(style.get("shadow", False)):
        shadow = QgsTextShadowSettings(fmt.shadow())     # unchanged: keep the shadow as it is
    shadow.setEnabled(bool(style.get("shadow", False)))
    shadow.setOffsetAngle(135)
    shadow.setOffsetDistance(SHADOW_DISTANCE)
    shadow.setOffsetUnit(Qgis.RenderUnit.Points)
    shadow.setBlurRadius(SHADOW_BLUR)
    shadow.setBlurRadiusUnit(Qgis.RenderUnit.Points)
    shadow.setOpacity(SHADOW_OPACITY)
    shadow.setColor(QColor("#000000"))
    fmt.setShadow(shadow)
    return fmt


# ------------------------------------------------------------------------------------ fields
def can_label(layer):
    """A vector layer with something to label (a spatial layer with at least one field)."""
    return (isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer) and layer.isValid()
            and layer.isSpatial() and layer.fields().count() > 0)


def candidate_fields(layer):
    """Field names worth labelling with: text fields first, never the key columns if anything else exists."""
    fields = layer.fields()
    keys = set(layer.primaryKeyAttributes()) if hasattr(layer, "primaryKeyAttributes") else set()
    names = [f.name() for i, f in enumerate(fields) if i not in keys]
    strings = [f.name() for i, f in enumerate(fields) if i not in keys and f.type() == T_STRING]
    return strings or names or [f.name() for f in fields]


def pick_field(layer):
    """A random field of the layer (a text field when there is one)."""
    names = candidate_fields(layer)
    return _rng.choice(names) if names else None


def is_numeric(layer, name):
    """True when `name` is a numeric field of the layer (integer, long, decimal, double)."""
    if layer is None or sip.isdeleted(layer) or not name:
        return False
    index = layer.fields().lookupField(name)
    return index >= 0 and layer.fields().at(index).isNumeric()


def label_expression(field, round_it=False, unit="", thousands=False):
    """
    The label expression for a numeric field shown rounded, with thousands separators and/or with a unit
    after it: round("Shape_Area",2)||'m'||char(178). Without any option it is just the quoted field.
    A unit converts the number first (metres for a length, square metres for an area):
    round("Shape_Area"*0.0001,2)||'ha'.
    The separator is always a comma ('en'), so 9999 shows as 9,999: with rounding the decimals are fixed
    (9,999.00), without it the number keeps its own decimals.
    """
    expr = '"' + field.replace('"', '""') + '"'
    factor = UNIT_FACTOR.get(unit, "1")
    if factor != "1":
        expr = "({}*{})".format(expr, factor)
    if thousands:
        expr = ("format_number({},{},'en')".format(expr, DECIMALS) if round_it
                else "format_number({},10,'en',false,true)".format(expr))
    elif round_it:
        expr = "round({},{})".format(expr, DECIMALS)
    if unit in UNIT_EXPRESSION:
        expr += "||" + UNIT_EXPRESSION[unit]
    return expr


def _saved_format(layer, expression, is_expression):
    """(field, round, unit, thousands) when the layer's expression is still exactly what Quick Labeling built."""
    try:
        saved = json.loads(layer.customProperty(FORMAT_PROP) or "")
        field, round_it, unit = str(saved["field"]), bool(saved["round"]), str(saved["unit"])
        thousands = bool(saved.get("thousands", False))
    except (ValueError, TypeError, KeyError):
        return None
    if (is_expression and (round_it or unit or thousands)
            and label_expression(field, round_it, unit, thousands) == expression):
        return field, round_it, unit, thousands
    return None                                            # the label was edited since: show it as it is


# ------------------------------------------------------------------------------------ state
def has_labels(layer):
    """True when the layer shows labels, whoever set them (Quick Labeling, Layer Properties, Callout Studio)."""
    return (isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer)
            and layer.labelsEnabled() and layer.labeling() is not None)


def is_quick(layer):
    """True when this layer is currently labelled by Quick Labeling."""
    return (isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer)
            and bool(layer.customProperty(MARK, False)) and layer.labelsEnabled()
            and layer.labeling() is not None)


def current(layer):
    """(field or expression, is_expression, style dict) of the layer's quick label, or None."""
    if not is_quick(layer):
        return None
    labeling = layer.labeling()
    if not hasattr(labeling, "settings"):
        return None
    settings = labeling.settings()
    fmt = settings.format()
    font = fmt.font()
    halo = fmt.buffer()
    style = {"family": font.family(), "style": font.styleName() or DEFAULT_STYLE,
             "size": fmt.size(), "color": fmt.color().name(QColor.NameFormat.HexArgb),
             "shadow": fmt.shadow().enabled(),
             "halo": halo.enabled(), "halo_size": halo.size(),
             "halo_color": halo.color().name(QColor.NameFormat.HexArgb),
             "position": _position_of(layer, settings)}
    style["offset"], style["offset_x"], style["offset_y"] = _offset_of(settings, style["position"])
    style["round"], style["unit"], style["thousands"] = False, "", False
    saved = _saved_format(layer, settings.fieldName, bool(settings.isExpression))
    if saved is not None:                                  # a numeric field with round / unit options
        field, style["round"], style["unit"], style["thousands"] = saved
        return field, False, style
    return settings.fieldName, bool(settings.isExpression), style


# ------------------------------------------------------------------------------------ apply
def apply(layer, field, is_expression=False, style=None, remember=True):
    """Label `layer` with `field` (or an expression) in `style`. Does not touch the saved 'previous' labeling."""
    style = dict(style or default_style())
    round_it = bool(style.get("round"))
    unit = style.get("unit") if style.get("unit") in UNITS else ""
    thousands = bool(style.get("thousands"))
    existing = layer.labeling().settings() if is_quick(layer) and layer.labeling().type() == "simple" else None
    settings = QgsPalLayerSettings(existing) if existing is not None else QgsPalLayerSettings()
    formatted = bool(not is_expression and (round_it or unit or thousands) and is_numeric(layer, field))
    if formatted:
        settings.fieldName = label_expression(field, round_it, unit, thousands)       # handled automatically
        settings.isExpression = True
        layer.setCustomProperty(FORMAT_PROP, json.dumps({"field": field, "round": round_it, "unit": unit, "thousands": thousands}))
    else:
        settings.fieldName = field
        settings.isExpression = bool(is_expression)
        layer.removeCustomProperty(FORMAT_PROP)
    settings.setFormat(_format(style, existing.format() if existing is not None else None))
    position = _position_for(layer, style.get("position"))
    style["position"] = position
    _set_position(settings, geometry_kind(layer), position)
    _set_offset(settings, geometry_kind(layer), position, style.get("offset", DEFAULT_OFFSET),
                style.get("offset_x", 0.0), style.get("offset_y", 0.0))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.setCustomProperty(MARK, True)
    layer.triggerRepaint()
    layer.emitStyleChanged()                     # the Label Bag and the other label panels follow
    if remember:
        remember_style(style)
        QgsSettings().setValue(SETTINGS + "offset", max(0.0, float(style.get("offset") or 0.0)))
        if position:
            QgsSettings().setValue(SETTINGS + "position_" + geometry_kind(layer), position)


class QuickLabels:
    """Keeps what each layer was labelled with before Quick Labeling, so switching off can put it back."""

    def __init__(self):
        self._previous = {}                      # layer id -> (labeling clone or None, labels were enabled)

    def enable(self, layer, field=None, style=None):
        """Switch Quick Labeling on for `layer` with `field` (random if None). Returns the field, or None."""
        if not can_label(layer):
            return None
        if not is_quick(layer) and layer.id() not in self._previous:
            labeling = layer.labeling()
            self._previous[layer.id()] = (labeling.clone() if labeling is not None else None,
                                          layer.labelsEnabled())
        field = field or pick_field(layer)
        if field is None:
            return None
        apply(layer, field, False, style)
        return field

    def disable(self, layer):
        """Switch it off: put back the labeling the layer had before, or turn labels off."""
        if layer is None or sip.isdeleted(layer):
            return
        was_quick = bool(layer.customProperty(MARK, False))
        previous = self._previous.pop(layer.id(), None)
        layer.removeCustomProperty(MARK)
        layer.removeCustomProperty(FORMAT_PROP)
        if not was_quick:
            return
        if previous is not None:
            labeling, enabled = previous
            layer.setLabeling(labeling)
            layer.setLabelsEnabled(bool(enabled and labeling is not None))
        else:                                            # project reopened: nothing to go back to
            layer.setLabeling(None)
            layer.setLabelsEnabled(False)
        layer.triggerRepaint()
        layer.emitStyleChanged()

    def turn_off(self, layer):
        """
        Switch off whatever labels the layer shows. Quick Labels give back the labeling the layer had
        before; labels from Layer Properties or Callout Label Studio are only switched off (their
        settings stay on the layer, so the Labels tab can bring them back). Returns 'quick' or 'other'.
        """
        if layer is not None and not sip.isdeleted(layer) and layer.customProperty(MARK, False):
            self.disable(layer)
            return "quick"
        layer.setLabelsEnabled(False)
        layer.triggerRepaint()
        layer.emitStyleChanged()
        return "other"

    def forget(self, removed):
        """Layers are going: drop what was kept for them (ids or layer objects, as the signal sends)."""
        for item in removed:
            self._previous.pop(item if isinstance(item, str) else item.id(), None)


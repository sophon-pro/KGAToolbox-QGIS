# -*- coding: utf-8 -*-
"""
Numbered key labeling: a small number/letter on every feature, plus key
boxes ("1. Trapeang Pou", "2. Srae Kruo" …) placed in tidy columns outside
the data. Ideal when polygons are too small to hold their names.

Everything is built as one QgsRuleBasedLabeling on the layer itself
(rule 1 = numbers on features, rule 2 = key boxes), so it shows in print
layouts and is saved/exported with the layer style. No data is modified.
"""

from qgis.PyQt.QtCore import QSizeF
from qgis.PyQt.QtGui import QColor, QFont, QFontMetricsF
from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsPalLayerSettings,
    QgsProperty,
    QgsRectangle,
    QgsRuleBasedLabeling,
    QgsTextBackgroundSettings,
)

from . import engine as E
from .arrange import _anchor, _label_texts, _map_expr, _prop, PT_TO_MM
from ..compat import enum_value

NUMBER_STYLES = [("123", "1, 2, 3 …"), ("ABC", "A, B, C …"), ("abc", "a, b, c …")]
NUMBER_ORDERS = [
    ("reading", "Reading order (top-left → bottom-right)"),
    ("west_east", "West → East"),
    ("north_south", "North → South"),
    ("alpha", "Alphabetical by name"),
]
NUMBER_LOOKS = [("badge", "Circle badge"), ("plain", "Plain bold with halo")]
NUMBER_POSITIONS = [("inside", "Inside the feature"),
                    ("outside", "Outside with leader line (Callout tab style)")]
KEY_POSITIONS = [("right", "Right of the data"), ("left", "Left of the data"),
                 ("below", "Below the data"), ("above", "Above the data")]

KEY_DEFAULTS = {k: v for k, v in E.DEFAULTS.items() if k.startswith("key_")}


def _ev(owner, enum, *names):
    return E._ev(owner, enum, *names)


def to_code(n, style):
    if style == "123":
        return str(n)
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s if style == "ABC" else s.lower()


def _order(fids, anchors, names, order, ext):
    if order == "alpha":
        return sorted(fids, key=lambda i: names.get(i, "").lower())
    if order == "west_east":
        return sorted(fids, key=lambda i: (anchors[i].x(), -anchors[i].y()))
    if order == "north_south":
        return sorted(fids, key=lambda i: (-anchors[i].y(), anchors[i].x()))
    # reading order: horizontal bands, left to right inside each band
    n = max(len(fids), 1)
    bands = max(1, int(round(n ** 0.5)))
    h = max(ext.height(), 1e-9) / bands
    top = ext.yMaximum()
    return sorted(fids, key=lambda i: (int((top - anchors[i].y()) / h), anchors[i].x()))


# --------------------------------------------------------------------------
# label settings for each rule
# --------------------------------------------------------------------------

def _number_settings(cfg, kind, codes):
    s = QgsPalLayerSettings()
    s.fieldName = _map_expr(codes, "''")
    s.isExpression = True

    fmt = QgsTextFormat_from(cfg, size=cfg["key_number_size"], bold=True,
                             color=cfg["key_number_color"] if cfg["key_look"] == "badge" else cfg["key_badge_color"])
    if cfg["key_look"] == "badge":
        bg = QgsTextBackgroundSettings()
        bg.setEnabled(True)
        bg.setType(_ev(QgsTextBackgroundSettings, "ShapeType", "ShapeCircle"))
        bg.setSizeType(_ev(QgsTextBackgroundSettings, "SizeType", "SizeBuffer"))
        bg.setSize(QSizeF(0.45, 0.45))
        bg.setSizeUnit(E.unit_mm())
        bg.setFillColor(QColor(cfg["key_badge_color"]))
        bg.setStrokeColor(QColor("#ffffffff"))
        bg.setStrokeWidth(0.25)
        bg.setStrokeWidthUnit(E.unit_mm())
        fmt.setBackground(bg)
        buf = fmt.buffer()
        buf.setEnabled(False)
        fmt.setBuffer(buf)
    s.setFormat(fmt)

    if cfg["key_number_pos"] == "outside" and kind != "line":
        s.setCallout(E.build_callout(cfg))
        E._apply_placement(s, dict(cfg, placement="ring"), kind)
    else:
        if kind == "line":
            s.placement = E.label_placement("Horizontal")
        else:
            s.placement = E.label_placement("OverPoint")
            try:
                s.centroidInside = True
            except AttributeError:
                pass
    _allow_overlap(s)
    return s


def QgsTextFormat_from(cfg, size, bold, color):
    c = dict(cfg)
    c.update({"font_size": size, "bold": bold, "text_color": color})
    return E.build_text_format(c)


def _allow_overlap(s):
    try:
        ps = s.placementSettings()
        ps.setOverlapHandling(Qgis.LabelOverlapHandling.AllowOverlapIfRequired)
        ps.setAllowDegradedPlacement(True)
        s.setPlacementSettings(ps)
    except AttributeError:
        s.displayAll = True


def _key_settings(cfg, texts, positions):
    s = QgsPalLayerSettings()
    s.fieldName = _map_expr(texts, "''")
    s.isExpression = True
    fmt = E.build_text_format(dict(cfg, buffer=False if cfg["key_box"] else cfg["buffer"]))
    if cfg["key_box"]:
        bg = QgsTextBackgroundSettings()
        bg.setEnabled(True)
        bg.setType(_ev(QgsTextBackgroundSettings, "ShapeType", "ShapeRectangle"))
        bg.setSizeType(_ev(QgsTextBackgroundSettings, "SizeType", "SizeBuffer"))
        bg.setSize(QSizeF(1.8, 1.4))
        bg.setSizeUnit(E.unit_mm())
        bg.setRadii(QSizeF(0.8, 0.8))
        bg.setRadiiUnit(E.unit_mm())
        bg.setFillColor(QColor(cfg["key_box_fill"]))
        bg.setStrokeColor(QColor(cfg["key_box_stroke"]))
        bg.setStrokeWidth(0.3)
        bg.setStrokeWidthUnit(E.unit_mm())
        fmt.setBackground(bg)
    s.setFormat(fmt)
    s.multilineAlign = enum_value((Qgis, 'LabelMultiLineAlignment.Left'),
                                  (QgsPalLayerSettings, 'MultiLineAlignment.MultiLeft'),
                                  (QgsPalLayerSettings, 'MultiLeft'))
    s.placement = E.label_placement("OverPoint")

    dd = s.dataDefinedProperties()
    dd.setProperty(_prop("PositionX"), QgsProperty.fromExpression(_map_expr({k: v[0] for k, v in positions.items()})))
    dd.setProperty(_prop("PositionY"), QgsProperty.fromExpression(_map_expr({k: v[1] for k, v in positions.items()})))
    dd.setProperty(_prop("Hali"), QgsProperty.fromExpression("'Left'"))
    dd.setProperty(_prop("Vali"), QgsProperty.fromExpression("'Top'"))
    s.setDataDefinedProperties(dd)
    _allow_overlap(s)
    try:
        obs = s.obstacleSettings()
        obs.setIsObstacle(False)
        s.setObstacleSettings(obs)
    except AttributeError:
        pass
    return s


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def number_key_entries(layer, cfg, expression, is_expression, filter_expression, kind):
    """Shared by map labels and print-layout key.
    Returns (ordered fids, codes{fid:code}, names{fid:name}, data extent) or None."""
    cfg = dict(KEY_DEFAULTS, **cfg)
    req = QgsFeatureRequest()
    if filter_expression and filter_expression.strip():
        req.setFilterExpression(filter_expression)
    feats = [f for f in layer.getFeatures(req) if f.hasGeometry()]
    anchors = {}
    for f in feats:
        p = _anchor(f.geometry(), kind, cfg.get("feature_anchor", "pole"))
        if p is not None:
            anchors[f.id()] = p
    feats = [f for f in feats if f.id() in anchors]
    if not feats:
        return None
    names = _label_texts(layer, expression, is_expression, feats)

    ext = QgsRectangle()
    ext.setMinimal()
    for f in feats:
        ext.combineExtentWith(f.geometry().boundingBox())
    ordered = _order(list(anchors), anchors, names, cfg["key_order"], ext)
    start = int(cfg["key_start"])
    codes = {fid: to_code(k + start, cfg["key_style"]) for k, fid in enumerate(ordered)}
    names = {fid: " ".join(names.get(fid, "").split()) for fid in ordered}
    return ordered, codes, names, ext


def key_columns(cfg, ordered, codes, names):
    """Split the key into column chunks -> list of (first fid, [lines])."""
    ncols = max(1, min(int(cfg["key_columns"]), len(ordered)))
    per = -(-len(ordered) // ncols)
    chunks = [ordered[i:i + per] for i in range(0, len(ordered), per)]
    title = (cfg.get("key_title") or "").strip()
    out = []
    for ci, chunk in enumerate(chunks):
        lines = ["{}. {}".format(codes[f], names.get(f, "")) for f in chunk]
        if title:
            lines.insert(0, title if ci == 0 else " ")
        out.append((chunk, lines))
    return out


def build_number_key_labeling(layer, cfg, expression, is_expression, filter_expression, mm_units, kind):
    cfg = dict(KEY_DEFAULTS, **cfg)
    res = number_key_entries(layer, cfg, expression, is_expression, filter_expression, kind)
    if res is None:
        return None
    ordered, codes, names, ext = res
    if not mm_units:
        mm_units = max(ext.width(), ext.height(), 1e-9) / 250.0

    # ---- key boxes: split the ordered list into columns ------------------
    columns = key_columns(cfg, ordered, codes, names)
    chunks = [c for c, _ in columns]

    font = QFont(cfg["font_family"]) if cfg["font_family"] else QFont()
    font.setBold(bool(cfg["bold"]))
    font.setItalic(bool(cfg["italic"]))
    font.setPixelSize(100)
    fm = QFontMetricsF(font)
    adv = fm.horizontalAdvance if hasattr(fm, "horizontalAdvance") else fm.width
    px_to_u = float(cfg["font_size"]) * PT_TO_MM / 100.0 * mm_units
    line_h = fm.lineSpacing() * px_to_u
    pad_x = (1.8 * 2 + 0.6) * mm_units if cfg["key_box"] else 0.0
    pad_y = (1.4 * 2) * mm_units if cfg["key_box"] else 0.0
    gap = 4.0 * mm_units
    margin = float(cfg["key_margin"]) * mm_units

    texts, sizes = {}, []
    for chunk, lines in columns:
        texts[chunk[0]] = "\n".join(lines)
        w = max(adv(t) for t in lines) * px_to_u + pad_x
        h = len(lines) * line_h + pad_y
        sizes.append((w, h))

    positions = {}
    total_w = sum(w for w, _ in sizes) + gap * (len(sizes) - 1)
    max_h = max(h for _, h in sizes)
    pos = cfg["key_position"]
    if pos == "right":
        x, y = ext.xMaximum() + margin, ext.yMaximum()
    elif pos == "left":
        x, y = ext.xMinimum() - margin - total_w, ext.yMaximum()
    elif pos == "below":
        x, y = ext.xMinimum(), ext.yMinimum() - margin
    else:  # above
        x, y = ext.xMinimum(), ext.yMaximum() + margin + max_h
    for chunk, (w, h) in zip(chunks, sizes):
        # background is drawn around the text, so shift the text inside by the padding
        positions[chunk[0]] = (x + pad_x / 2.0, y - pad_y / 2.0)
        x += w + gap

    # ---- assemble rule-based labeling -----------------------------------
    root = QgsRuleBasedLabeling.Rule(None)
    num_rule = QgsRuleBasedLabeling.Rule(_number_settings(cfg, kind, codes))
    num_rule.setDescription("Numbers on features")
    if filter_expression and filter_expression.strip():
        num_rule.setFilterExpression(filter_expression)
    root.appendChild(num_rule)

    if not cfg.get("key_on_map", True):
        return QgsRuleBasedLabeling(root)
    key_ids = ",".join(str(c[0]) for c in chunks)
    key_rule = QgsRuleBasedLabeling.Rule(_key_settings(cfg, texts, positions))
    key_rule.setDescription("Key (number → name)")
    key_rule.setFilterExpression("$id IN ({})".format(key_ids))
    root.appendChild(key_rule)
    return QgsRuleBasedLabeling(root)

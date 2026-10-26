# -*- coding: utf-8 -*-
"""
Organized label layouts: stack labels in tidy columns (left / right / both
sides) or rows (top / bottom / both) around the data, sorted so leader lines
do not cross, and nudged apart only as much as needed ("dodge") so leaders
stay as straight as possible.

Positions are written as data-defined label properties (PositionX/Y, Hali,
Vali, rotation) using an expression map keyed by feature id, so nothing is
written to the user's data and it works on any layer/provider.
"""

from qgis.PyQt.QtGui import QFont, QFontMetricsF
from qgis.core import (
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    NULL,
    QgsFeatureRequest,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRectangle,
)

ARRANGE_MODES = [
    ("auto", "Off — automatic placement"),
    ("left", "Column on the left"),
    ("right", "Column on the right"),
    ("both", "Columns on both sides"),
    ("top", "Row along the top"),
    ("bottom", "Row along the bottom"),
    ("top_bottom", "Rows top and bottom"),
]

PT_TO_MM = 25.4 / 72.0


def _prop(name):
    try:
        return getattr(QgsPalLayerSettings.Property, name)
    except AttributeError:
        return getattr(QgsPalLayerSettings, name)


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def _anchor(geom, kind, feature_anchor):
    try:
        return _anchor_unsafe(geom, kind, feature_anchor)
    except Exception:
        return None


def _anchor_unsafe(geom, kind, feature_anchor):
    if geom is None or geom.isEmpty():
        return None
    if kind == "polygon":
        g = geom.centroid() if feature_anchor == "centroid" else geom.pointOnSurface()
    elif kind == "line":
        g = geom.interpolate(geom.length() / 2.0)
        if g is None or g.isEmpty():
            g = geom.centroid()
    else:
        g = geom if not geom.isMultipart() else geom.centroid()
    if g is None or g.isEmpty():
        g = geom.centroid()
    if g is None or g.isEmpty():
        return QgsPointXY(geom.boundingBox().center())
    return QgsPointXY(g.asPoint())


def mm_to_layer_units(canvas, layer):
    """How many layer-CRS units one millimetre on the canvas covers."""
    ms = canvas.mapSettings()
    per_mm_map = canvas.mapUnitsPerPixel() * ms.outputDpi() / 25.4
    c = ms.extent().center()
    try:
        ct = QgsCoordinateTransform(ms.destinationCrs(), layer.crs(), QgsProject.instance())
        p0 = ct.transform(c)
        p1 = ct.transform(QgsPointXY(c.x() + per_mm_map * 10, c.y()))
        d = ((p1.x() - p0.x()) ** 2 + (p1.y() - p0.y()) ** 2) ** 0.5 / 10.0
        return d if d > 0 else per_mm_map
    except Exception:
        return per_mm_map


# --------------------------------------------------------------------------
# 1-D de-overlap ("dodge") with variable label sizes
# --------------------------------------------------------------------------

def dodge(desired, sizes, pad):
    """desired: centre coordinates (ascending); sizes: extent of each label
    along the axis. Returns centre positions with no overlaps, moving labels
    as little as possible (least-squares within each merged cluster).
    Cluster merging is near-linear, so thousands of labels are fine."""
    clusters = []  # each: [indices, offsets, sum(desired - offset)]

    def start(c):
        return c[2] / len(c[0])

    for i in range(len(desired)):
        clusters.append([[i], [0.0], desired[i]])
        while len(clusters) > 1:
            a, b = clusters[-2], clusters[-1]
            need = (sizes[a[0][-1]] + sizes[b[0][0]]) / 2.0 + pad
            if start(b) - (start(a) + a[1][-1]) < need:
                shift = a[1][-1] + need
                a[0].extend(b[0])
                a[1].extend(o + shift for o in b[1])
                a[2] += b[2] - shift * len(b[0])
                clusters.pop()
            else:
                break
    out = [0.0] * len(desired)
    for idx, offs, ssum in clusters:
        s0 = ssum / len(idx)
        for i, o in zip(idx, offs):
            out[i] = s0 + o
    return out


def _spread(desired, sizes, pad, lo, hi):
    """Evenly distribute along [lo, hi] (expanding if too tight)."""
    n = len(desired)
    if n == 0:
        return []
    if n == 1:
        return [desired[0]]
    need = sum(sizes) - (sizes[0] + sizes[-1]) / 2.0 + pad * (n - 1)
    span = max(hi - lo, need)
    mid = (lo + hi) / 2.0
    step = span / (n - 1)
    start = mid - span / 2.0
    return [start + k * step for k in range(n)]


def _cross(p1, p2, q1, q2):
    def o(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 0) - (v < 0)
    return (o(p1, p2, q1) * o(p1, p2, q2) < 0) and (o(q1, q2, p1) * o(q1, q2, p2) < 0)


def uncross(fids, slots, anchor_xy, slot_xy, max_passes=60, budget=1.0):
    """Swap labels between fixed slots until no two leader lines intersect.
    fids[k] sits in slots[k]; returns the re-ordered fid list.
    Stops after `budget` seconds so live preview stays responsive on huge layers."""
    import time
    fids = list(fids)
    n = len(fids)
    if n < 2:
        return fids
    S = [slot_xy(v) for v in slots]
    P = [anchor_xy(i) for i in fids]
    deadline = time.monotonic() + budget
    for _ in range(max_passes):
        swapped = False
        for a in range(n):
            if time.monotonic() > deadline:
                return fids
            sax, say = S[a]
            for b in range(a + 1, n):
                pax, pay = P[a]
                pbx, pby = P[b]
                sbx, sby = S[b]
                # quick bounding-box rejection
                if max(say, pay) < min(sby, pby) or max(sby, pby) < min(say, pay) or \
                   max(sax, pax) < min(sbx, pbx) or max(sbx, pbx) < min(sax, pax):
                    continue
                d1 = (pax - sax) * (sby - say) - (pay - say) * (sbx - sax)
                d2 = (pax - sax) * (pby - say) - (pay - say) * (pbx - sax)
                if d1 * d2 >= 0:
                    continue
                d3 = (pbx - sbx) * (say - sby) - (pby - sby) * (sax - sbx)
                d4 = (pbx - sbx) * (pay - sby) - (pby - sby) * (pax - sbx)
                if d3 * d4 >= 0:
                    continue
                fids[a], fids[b] = fids[b], fids[a]
                P[a], P[b] = P[b], P[a]
                swapped = True
        if not swapped:
            break
    return fids


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _label_texts(layer, expression, is_expression, feats):
    ctx = QgsExpressionContext()
    ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    exp = QgsExpression(expression if is_expression else QgsExpression.quotedColumnRef(expression))
    exp.prepare(ctx)
    out = {}
    for f in feats:
        ctx.setFeature(f)
        v = exp.evaluate(ctx)
        try:
            is_null = v is None or v == NULL or (hasattr(v, "isNull") and v.isNull())
        except Exception:
            is_null = v is None
        out[f.id()] = "" if is_null else str(v)
    return out


def compute_layout(layer, cfg, expression, is_expression, filter_expression, mm_units, kind):
    """Return dict fid -> (x, y, hali, vali, rotation)."""
    mode = cfg["arrange"]
    req = QgsFeatureRequest()
    if filter_expression and filter_expression.strip():
        req.setFilterExpression(filter_expression)
    feats = [f for f in layer.getFeatures(req) if f.hasGeometry()]
    anchors = {}
    for f in feats:
        p = _anchor(f.geometry(), kind, cfg.get("feature_anchor", "pole"))
        if p is not None:
            anchors[f.id()] = p
    if not anchors:
        return {}
    feats = [f for f in feats if f.id() in anchors]
    texts = _label_texts(layer, expression, is_expression, feats)

    ext = QgsRectangle()
    ext.setMinimal()
    for p in anchors.values():
        ext.combineExtentWith(p.x(), p.y())
    # include the full geometry extent so labels clear the data, not just anchors
    for f in feats:
        ext.combineExtentWith(f.geometry().boundingBox())

    # text metrics in mm, converted to layer units
    font = QFont(cfg["font_family"]) if cfg["font_family"] else QFont()
    font.setBold(bool(cfg["bold"]))
    font.setItalic(bool(cfg["italic"]))
    font.setPixelSize(100)
    fm = QFontMetricsF(font)
    size_mm = float(cfg["font_size"]) * PT_TO_MM
    adv = fm.horizontalAdvance if hasattr(fm, "horizontalAdvance") else fm.width

    def width_u(fid):
        return adv(texts.get(fid, "")) / 100.0 * size_mm * mm_units

    height_u = fm.height() / 100.0 * size_mm * mm_units
    pad = float(cfg["arrange_gap"]) * mm_units
    margin = float(cfg["arrange_margin"]) * mm_units
    even = bool(cfg["arrange_even"])
    vertical_rows = bool(cfg["arrange_rotate"])
    if cfg.get("callout_type") == "balloon":
        extra = 2 * float(cfg.get("margin", 1.0)) * mm_units
        height_u += extra
        pad += extra / 2.0

    result = {}
    cx = ext.center().x()
    cy = ext.center().y()

    def do_column(fids, side):
        if not fids:
            return
        fids = sorted(fids, key=lambda i: anchors[i].y())
        desired = [anchors[i].y() for i in fids]
        sizes = [height_u] * len(fids)
        ys = _spread(desired, sizes, pad, ext.yMinimum(), ext.yMaximum()) if even \
            else dodge(desired, sizes, pad)
        x = ext.xMinimum() - margin if side == "left" else ext.xMaximum() + margin
        hali = "Right" if side == "left" else "Left"
        fids = uncross(fids, ys, lambda i: (anchors[i].x(), anchors[i].y()), lambda y: (x, y))
        for i, y in zip(fids, ys):
            result[i] = (x, y, hali, "Half", None)

    def do_row(fids, side):
        if not fids:
            return
        fids = sorted(fids, key=lambda i: anchors[i].x())
        desired = [anchors[i].x() for i in fids]
        sizes = [height_u if vertical_rows else width_u(i) for i in fids]
        xs = _spread(desired, sizes, pad, ext.xMinimum(), ext.xMaximum()) if even \
            else dodge(desired, sizes, pad)
        y = ext.yMaximum() + margin if side == "top" else ext.yMinimum() - margin
        if vertical_rows:  # equal slot sizes, so slots can be swapped freely
            fids = uncross(fids, xs, lambda i: (anchors[i].x(), anchors[i].y()), lambda x: (x, y))
        for i, x in zip(fids, xs):
            if vertical_rows:
                # text reads bottom-to-top; its start (top row) or end (bottom row) touches the data side
                result[i] = (x, y, "Left" if side == "top" else "Right", "Half", 270.0)
            else:
                result[i] = (x, y, "Center", "Bottom" if side == "top" else "Top", None)

    ids = list(anchors.keys())
    if mode == "left":
        do_column(ids, "left")
    elif mode == "right":
        do_column(ids, "right")
    elif mode == "both":
        do_column([i for i in ids if anchors[i].x() < cx], "left")
        do_column([i for i in ids if anchors[i].x() >= cx], "right")
    elif mode == "top":
        do_row(ids, "top")
    elif mode == "bottom":
        do_row(ids, "bottom")
    elif mode == "top_bottom":
        do_row([i for i in ids if anchors[i].y() >= cy], "top")
        do_row([i for i in ids if anchors[i].y() < cy], "bottom")
    return result


def _map_expr(values, default="NULL"):
    """map_get(map('fid', value, ...), to_string($id))"""
    parts = []
    for fid, v in values.items():
        if isinstance(v, str):
            parts.append("'{}',{}".format(fid, QgsExpression.quotedString(v)))
        else:
            parts.append("'{}',{!r}".format(fid, float(v)))
    if not parts:
        return default
    return "coalesce(map_get(map({}), to_string($id)), {})".format(",".join(parts), default)


def apply_layout_properties(settings, layout):
    """Write data-defined properties for a computed layout onto settings."""
    if not layout:
        return
    dd = settings.dataDefinedProperties()
    xs = {fid: v[0] for fid, v in layout.items()}
    ys = {fid: v[1] for fid, v in layout.items()}
    hal = {fid: v[2] for fid, v in layout.items()}
    val = {fid: v[3] for fid, v in layout.items()}
    rot = {fid: v[4] for fid, v in layout.items() if v[4] is not None}
    dd.setProperty(_prop("PositionX"), QgsProperty.fromExpression(_map_expr(xs)))
    dd.setProperty(_prop("PositionY"), QgsProperty.fromExpression(_map_expr(ys)))
    dd.setProperty(_prop("Hali"), QgsProperty.fromExpression(_map_expr(hal, "'Left'")))
    dd.setProperty(_prop("Vali"), QgsProperty.fromExpression(_map_expr(val, "'Half'")))
    if rot:
        try:
            dd.setProperty(_prop("LabelRotation"), QgsProperty.fromExpression(_map_expr(rot)))
        except AttributeError:
            dd.setProperty(_prop("Rotation"), QgsProperty.fromExpression(_map_expr(rot)))
    settings.setDataDefinedProperties(dd)

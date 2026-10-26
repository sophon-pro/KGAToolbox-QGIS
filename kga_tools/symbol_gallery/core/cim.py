# -*- coding: utf-8 -*-
"""
Minimal ArcGIS Pro CIM -> QGIS converter used by "Import style pack".

* CIMPointSymbol vector markers (paths, rings, Bezier curves, nested markers,
  text and embedded pictures) are rendered to an embedded SVG marker.
* CIMLineSymbol / CIMPolygonSymbol solid strokes, dashes, solid fills and
  hatch fills are mapped onto native QGIS symbol layers.

Anything else is skipped and reported, never guessed.
"""
import base64
import json
import math
import sqlite3


def color(c, default=None):
    if not c:
        return default, 1.0
    t = c.get("type", "")
    v = c.get("values", [])
    if t == "CIMRGBColor":
        r, g, b = v[:3]
        a = v[3] if len(v) > 3 else 100
    elif t == "CIMCMYKColor":
        cc, m, y, k = [x / 100.0 for x in v[:4]]
        r, g, b = [255 * (1 - x) * (1 - k) for x in (cc, m, y)]
        a = v[4] if len(v) > 4 else 100
    elif t == "CIMGrayColor":
        r = g = b = v[0]
        a = v[1] if len(v) > 1 else 100
    elif t == "CIMHSVColor":
        import colorsys
        rr, gg, bb = colorsys.hsv_to_rgb(v[0] / 360.0, v[1] / 100.0, v[2] / 100.0)
        r, g, b = rr * 255, gg * 255, bb * 255  # noqa
        a = v[3] if len(v) > 3 else 100
    else:
        r, g, b, a = 0, 0, 0, 100
    return "#%02x%02x%02x" % (int(round(r)), int(round(g)), int(round(b))), a / 100.0


def _pts_path(parts, close):
    d = []
    for part in parts:
        for i, p in enumerate(part):
            d.append(("M" if i == 0 else "L") + "%.3f %.3f" % (p[0], p[1]))
        if close:
            d.append("Z")
    return " ".join(d)


def _curve_path(parts, close):
    d = []
    for part in parts:
        cur = None
        for i, seg in enumerate(part):
            if isinstance(seg, list):
                d.append(("M" if i == 0 else "L") + "%.3f %.3f" % (seg[0], seg[1]))
                cur = seg
            elif "b" in seg:
                end, c1, c2 = seg["b"]
                d.append("C%.3f %.3f %.3f %.3f %.3f %.3f" % (c1[0], c1[1], c2[0], c2[1],
                                                           end[0], end[1]))
                cur = end
            elif "c" in seg:  # circular arc: end, interior point -> approximate
                end, mid = seg["c"]
                d.append(_arc3(cur, mid, end))
                cur = end
            elif "a" in seg:
                end = seg["a"][0]
                d.append("L%.3f %.3f" % (end[0], end[1]))
                cur = end
        if close:
            d.append("Z")
    return " ".join(d)


def _arc3(p1, p2, p3):
    """SVG arc through three points."""
    ax, ay = p1[:2]
    bx, by = p2[:2]
    cx, cy = p3[:2]
    dd = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(dd) < 1e-9:
        return "L%.3f %.3f" % (cx, cy)
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / dd
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / dd
    r = math.hypot(ax - ux, ay - uy)
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    sweep = 1 if cross > 0 else 0
    a1 = math.atan2(ay - uy, ax - ux)
    a3 = math.atan2(cy - uy, cx - ux)
    a2 = math.atan2(by - uy, bx - ux)

    def norm(a):
        return a % (2 * math.pi)
    if sweep:
        large = 1 if norm(a3 - a1) > math.pi else 0
    else:
        large = 1 if norm(a1 - a3) > math.pi else 0
    del a2
    return "A%.3f %.3f 0 %d %d %.3f %.3f" % (r, r, large, sweep, cx, cy)


def geom_path(g):
    if "rings" in g:
        return _pts_path(g["rings"], True), True
    if "paths" in g:
        return _pts_path(g["paths"], False), False
    if "curveRings" in g:
        return _curve_path(g["curveRings"], True), True
    if "curvePaths" in g:
        return _curve_path(g["curvePaths"], False), False
    return None, False


def style_layers(sym):
    """Return list of svg attribute strings, bottom-most first."""
    out = []
    for sl in reversed(sym.get("symbolLayers", [])):
        if sl.get("enable") is False:
            continue
        t = sl["type"]
        if t == "CIMSolidFill":
            c, a = color(sl.get("color"), "#000000")
            out.append(('fill', 'fill="%s" fill-opacity="%.3f" stroke="none"' % (c, a)))
        elif t == "CIMSolidStroke":
            c, a = color(sl.get("color"), "#000000")
            w = sl.get("width", 1)
            cap = {"Butt": "butt", "Round": "round", "Square": "square"}.get(
                sl.get("capStyle"), "butt")
            join = {"Miter": "miter", "Round": "round", "Bevel": "bevel"}.get(
                sl.get("joinStyle"), "round")
            dash = ""
            for eff in sl.get("effects", []) or []:
                if eff.get("type") == "CIMGeometricEffectDashes" and eff.get("dashTemplate"):
                    dash = ' stroke-dasharray="%s"' % " ".join(
                        "%.3f" % x for x in eff["dashTemplate"])
            out.append(('stroke', 'fill="none" stroke="%s" stroke-opacity="%.3f" '
                        'stroke-width="%.3f" stroke-linecap="%s" stroke-linejoin="%s"%s'
                        % (c, a, w, cap, join, dash)))
    return out


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# set False to drop text / number labels drawn inside vector markers
INCLUDE_TEXT = True


def graphic_svg(g):
    geom = g["geometry"]
    sym = g.get("symbol", {})
    parts = []
    st = sym.get("type")
    if "x" in geom and "y" in geom:
        x, y = geom["x"], geom["y"]
        if st == "CIMTextSymbol":
            if not INCLUDE_TEXT:
                return ""
            text = g.get("textString", "")
            if not text:
                return ""
            h = sym.get("height", 10)
            fam = sym.get("fontFamilyName", "Arial")
            weight = "bold" if "Bold" in sym.get("fontStyleName", "") else "normal"
            fill = "#000000"
            inner = sym.get("symbol", {})
            for sl in inner.get("symbolLayers", []):
                if sl["type"] == "CIMSolidFill":
                    fill = color(sl.get("color"), "#000000")[0]
            anchor = {"Left": "start", "Center": "middle", "Right": "end"}.get(
                sym.get("horizontalAlignment"), "middle")
            # text is drawn un-flipped
            return ('<text transform="translate(%.3f %.3f) scale(1 -1)" font-family="%s" '
                    'font-size="%.3f" font-weight="%s" fill="%s" text-anchor="%s" '
                    'dominant-baseline="central">%s</text>'
                    % (x, y, esc(fam), h, weight, fill, anchor, esc(text)))
        if st == "CIMPointSymbol":
            for sl in reversed(sym.get("symbolLayers", [])):
                parts.append(nested_marker(sl, x, y))
            return "".join(parts)
        return ""
    d, closed = geom_path(geom)
    if not d:
        return ""
    for kind, attrs in style_layers(sym):
        if kind == "fill" and not closed:
            continue
        parts.append('<path d="%s" %s/>' % (d, attrs))
    return "".join(parts)


def nested_marker(sl, x, y):
    t = sl.get("type")
    size = sl.get("size", 6)
    rot = sl.get("rotation", 0) or 0
    ox, oy = sl.get("offsetX", 0) or 0, sl.get("offsetY", 0) or 0
    if t == "CIMVectorMarker":
        inner, fw, fh = vector_marker_body(sl)
        if fh <= 0:
            return ""
        s = size / fh
        return ('<g transform="translate(%.3f %.3f) rotate(%.3f) scale(%.4f) '
                'translate(%.3f %.3f)">%s</g>'
                % (x + ox, y + oy, rot, s, -fw / 2 - sl["frame"]["xmin"] * 0,
                   -fh / 2, inner))
    if t == "CIMPictureMarker":
        url = sl.get("url", "")
        if not url.startswith("data:"):
            return ""
        w = size * (sl.get("scaleX", 1) or 1)
        return ('<g transform="translate(%.3f %.3f) rotate(%.3f) scale(1 -1)">'
                '<image x="%.3f" y="%.3f" width="%.3f" height="%.3f" '
                'preserveAspectRatio="xMidYMid meet" xlink:href="%s"/></g>'
                % (x + ox, y + oy, rot, -w / 2, -size / 2, w, size, url))
    return ""


def vector_marker_body(layer):
    fr = layer.get("frame", {"xmin": 0, "ymin": 0, "xmax": 17, "ymax": 17})
    fw = fr["xmax"] - fr["xmin"]
    fh = fr["ymax"] - fr["ymin"]
    body = "".join(graphic_svg(g) for g in layer.get("markerGraphics", []))
    # move frame origin to 0,0
    return ('<g transform="translate(%.3f %.3f)">%s</g>' % (-fr["xmin"], -fr["ymin"], body),
            fw, fh)


def point_symbol_to_svg(sym):
    """Returns (svg_text, width_pt, height_pt) or None."""
    layers = [sl for sl in sym.get("symbolLayers", []) if sl.get("enable", True)]
    vms = [sl for sl in layers if sl["type"] == "CIMVectorMarker"]
    if not vms:
        return None
    # use the first (top) vector marker frame as reference; draw all bottom-up
    ref = vms[0]
    fr = ref["frame"]
    fw = fr["xmax"] - fr["xmin"]
    fh = fr["ymax"] - fr["ymin"]
    size = ref.get("size", 10)
    scale_pt = size / fh if fh else 1  # points per frame unit
    w_pt, h_pt = fw * scale_pt, fh * scale_pt
    pad = 0.06 * max(w_pt, h_pt)
    bodies = []
    for sl in reversed(vms):
        lfr = sl["frame"]
        lfw = lfr["xmax"] - lfr["xmin"]
        lfh = lfr["ymax"] - lfr["ymin"]
        ls = (sl.get("size", 10) / lfh) if lfh else 1
        inner, _, _ = vector_marker_body(sl)
        ox = (sl.get("offsetX", 0) or 0)
        oy = (sl.get("offsetY", 0) or 0)
        # centre each layer's frame on the reference centre
        bodies.append('<g transform="translate(%.3f %.3f) scale(%.5f) translate(%.3f %.3f)">%s</g>'
                      % (w_pt / 2 + ox, h_pt / 2 + oy, ls, -lfw / 2, -lfh / 2, inner))
    W, H = w_pt + 2 * pad, h_pt + 2 * pad
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'width="%.2f" height="%.2f" viewBox="0 0 %.3f %.3f">'
           '<g transform="translate(%.3f %.3f) scale(1 -1)">%s</g></svg>'
           % (W, H, W, H, pad, H - pad, "".join(bodies)))
    return svg, W, H


def load(content):
    return json.JSONDecoder().raw_decode(content.rstrip("\x00"))[0]


PT_TO_MM = 25.4 / 72.0


def _qcolor_props(c):
    hexc, a = color(c, "#000000")
    from qgis.PyQt.QtGui import QColor
    from qgis.core import QgsSymbolLayerUtils
    q = QColor(hexc)
    q.setAlphaF(max(0.0, min(1.0, a)))
    return QgsSymbolLayerUtils.encodeColor(q)


def _dash(sl):
    for eff in sl.get("effects", []) or []:
        if eff.get("type") == "CIMGeometricEffectDashes" and eff.get("dashTemplate"):
            return ";".join("%.3f" % (x * PT_TO_MM) for x in eff["dashTemplate"])
    return None


def _line_layer(sl):
    from qgis.core import QgsSimpleLineSymbolLayer
    props = {
        "line_color": _qcolor_props(sl.get("color")),
        "line_width": "%.3f" % (sl.get("width", 1) * PT_TO_MM),
        "line_width_unit": "MM",
        "capstyle": {"Butt": "flat", "Round": "round", "Square": "square"}.get(
            sl.get("capStyle"), "round"),
        "joinstyle": {"Miter": "miter", "Round": "round", "Bevel": "bevel"}.get(
            sl.get("joinStyle"), "round"),
    }
    dash = _dash(sl)
    if dash:
        props.update({"use_custom_dash": "1", "customdash": dash, "customdash_unit": "MM"})
    return QgsSimpleLineSymbolLayer.create(props)


def _convert_line(sym):
    from qgis.core import QgsLineSymbol
    layers = [_line_layer(sl) for sl in reversed(sym.get("symbolLayers", []))
              if sl.get("enable", True) and sl["type"] == "CIMSolidStroke"]
    if not layers:
        return None
    out = QgsLineSymbol(layers)
    return out


def _convert_fill(sym):
    from qgis.core import (QgsFillSymbol, QgsSimpleFillSymbolLayer,
                           QgsLinePatternFillSymbolLayer, QgsLineSymbol)
    layers = []
    for sl in reversed(sym.get("symbolLayers", [])):
        if not sl.get("enable", True):
            continue
        t = sl["type"]
        if t == "CIMSolidFill":
            layers.append(QgsSimpleFillSymbolLayer.create({
                "color": _qcolor_props(sl.get("color")), "outline_style": "no",
                "style": "solid"}))
        elif t == "CIMSolidStroke":
            layers.append(_line_layer(sl))
        elif t == "CIMHatchFill":
            ls = sl.get("lineSymbol", {})
            strokes = [x for x in ls.get("symbolLayers", []) if x["type"] == "CIMSolidStroke"]
            lyr = QgsLinePatternFillSymbolLayer.create({
                "angle": "%.2f" % (sl.get("rotation", 0) or 0),
                "distance": "%.3f" % ((sl.get("separation", 5) or 5) * PT_TO_MM),
                "distance_unit": "MM"})
            if strokes:
                lyr.setSubSymbol(QgsLineSymbol([_line_layer(strokes[0])]))
            layers.append(lyr)
    if not layers:
        return None
    return QgsFillSymbol(layers)


def _convert_point(sym):
    from qgis.core import QgsMarkerSymbol, QgsSvgMarkerSymbolLayer
    r = point_symbol_to_svg(sym)
    if not r:
        return None
    svg, w, _h = r
    lyr = QgsSvgMarkerSymbolLayer.create({
        "name": "base64:" + base64.b64encode(svg.encode("utf-8")).decode("ascii"),
        "size": "%.3f" % (w * PT_TO_MM), "size_unit": "MM"})
    return QgsMarkerSymbol([lyr])


def stylx_to_style(path, style, tag=None):
    """Read an ArcGIS Pro .stylx and add what can be converted to *style*.

    Returns (added, skipped) counts.
    """
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = con.execute("SELECT NAME, CATEGORY, TAGS, CONTENT FROM ITEMS").fetchall()
    finally:
        con.close()
    added = skipped = 0
    used = set(n.lower() for n in style.symbolNames())
    for name, category, tags, content in rows:
        try:
            sym = load(content or "")
        except ValueError:
            skipped += 1
            continue
        t = sym.get("type")
        try:
            if t == "CIMPointSymbol":
                q = _convert_point(sym)
            elif t == "CIMLineSymbol":
                q = _convert_line(sym)
            elif t == "CIMPolygonSymbol":
                q = _convert_fill(sym)
            else:
                q = None
        except Exception:  # one bad item must not stop the import
            q = None
        if q is None:
            skipped += 1
            continue
        base = (name or "").strip()
        generic = base.lower() in ("", "point symbol", "line symbol", "polygon symbol")
        if generic:
            base = {"CIMPointSymbol": "Icon", "CIMLineSymbol": "Line",
                    "CIMPolygonSymbol": "Fill"}[t]
        k = 1
        nm = "%s %03d" % (base, k) if generic else base
        while nm.lower() in used:
            k += 1
            nm = "%s %03d" % (base, k)
        used.add(nm.lower())
        style.addSymbol(nm, q, True)
        tag_list = [x for x in (tags or "").split(";") if x and x not in ("rgb", "cmyk")]
        if category:
            tag_list.append(category)
        if tag:
            tag_list.append(tag)
        if tag_list:
            from .compat import STYLE_SYMBOL
            style.tagSymbol(STYLE_SYMBOL, nm, tag_list)
        added += 1
    return added, skipped

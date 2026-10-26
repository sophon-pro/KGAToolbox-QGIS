# -*- coding: utf-8 -*-
"""Gravity command check per block - the engine behind step 2 of Paddy Irrigation Tools.

Chain:  field (block) -> Tertiary Canal (TC) -> Secondary Canal (SC) -> Main Canal (MC) -> Reservoir

Inputs: block polygons, one high point per block (the points step 1 makes, or your own),
optional canal lines with a level field (MC / SC / TC, or 1 / 2 / 3) and a reservoir level.

1) Field water level each block needs:  FSL_req = top of block + field water depth.
2) Plot-to-plot links: neighbouring blocks (dikes / gaps up to the tolerance are bridged).
   Block A can feed block B when top_A - top_B >= spill loss. "Head" blocks have no
   neighbour high enough to feed them and need a canal turnout.
3) Canal turnouts: the nearest point on a canal within the search distance.
   Req_WL = FSL_req + turnout loss + field-channel gradient x field-channel length.
4) Service: blocks are processed from high to low, served from a canal or plot-to-plot.
5) Canal levels. DESIGN: the water level each canal needs at its upstream end, computed
   TC -> SC -> MC, then the reservoir level needed. CHECK: your canal water levels are
   compared with the need at every turnout, junction and the reservoir.
6) Optional DEM: ground along each canal -> fill / cut under the design water level.

Output layers (memory layers; optional GeoPackage): Command_Blocks, Command_Links,
Turnouts, Field_channels, Canal_Command, and an HTML report.

Nothing here builds a widget; `gui/gravity_command_page.py` calls `run_command`.
"""

import math
import os
import time

from qgis.core import (
    NULL, Qgis, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsSpatialIndex, QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from .block_elevation import POLYGON_GEOM
from .compat import T_DOUBLE, T_INT, T_STRING, enum_value, make_field

ORDER_NAMES = {1: "MC", 2: "SC", 3: "TC", 4: "FC"}
SERVICE_ORDER = ("Direct (critical)", "Direct", "Cascade", "NOT commanded", "Direct (WL unknown)",
                 "Not served", "No elevation")
SERVICE_INFO = {
    "Direct (critical)": ("#7b2d8e", "Own turnout. This block sets how high its canal must be."),
    "Direct": ("#2e9e44", "Own turnout on a canal."),
    "Cascade": ("#6fb7e8", "Fed plot-to-plot from a higher neighbour - no turnout needed."),
    "NOT commanded": ("#e0301e", "The canal water level is too low for this block."),
    "Direct (WL unknown)": ("#b38add", "Near a canal that has no water level - cannot be checked."),
    "Not served": ("#f39c12", "No canal within reach and no higher neighbour can feed it."),
    "No elevation": ("#9e9e9e", "No high point - re-run Step 1 for this block."),
}


# =========================================================================== #
#  Core logic (plain Python, no QGIS objects) - easy to test
# =========================================================================== #
def num(v):
    """QVariant NULL / None / text -> float or None."""
    if v is None or v == NULL:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def parse_order(v):
    """Canal level -> 1 (MC), 2 (SC), 3 (TC), 4 (field / quaternary) or None."""
    if v is None or v == NULL:
        return None
    s = str(v).strip().upper()
    if not s or s == "NULL":
        return None
    try:
        n = int(float(s))
        return n if 1 <= n <= 9 else None
    except ValueError:
        pass
    for keys, n in ((("MC", "MAIN"), 1), (("SC", "SECOND"), 2), (("TC", "TERT"), 3),
                    (("FC", "QC", "FIELD", "QUAT", "WATERCOURSE"), 4)):
        if any(k in s for k in keys):
            return n
    return None


def build_suppliers(tops, nbrs, spill_loss):
    """Plot-to-plot links. tops {b: top or None}, nbrs {b: {n: shared_len}}.
    Returns {b: [(a, drop)]}: neighbours a that can feed b by gravity (drop = top_a - top_b)."""
    sup = {b: [] for b in tops}
    for b, nb in nbrs.items():
        if tops.get(b) is None:
            continue
        for a in nb:
            if tops.get(a) is None:
                continue
            drop = tops[a] - tops[b]
            if drop > 0 and drop >= spill_loss:
                sup[b].append((a, drop))
    return sup


def profile_value(segs, s):
    """Water level at chainage s from [(s_a, s_b, wl_a, wl_b)] segments (one per canal
    feature, s_a <= s_b). Linear inside a segment, so 'one level per feature' gives steps at
    feature ends; beyond the data the nearest segment end is used. None if no data."""
    if not segs:
        return None
    best = None
    for a, b, wa, wb in segs:
        if a - 1e-6 <= s <= b + 1e-6:
            if b - a < 1e-9:
                return wa
            t = (min(max(s, a), b) - a) / (b - a)
            return wa + (wb - wa) * t
        d, w = (a - s, wa) if s < a else (s - b, wb)
        if best is None or d < best[0]:
            best = (d, w)
    return best[1]


def assign_service(tops, suppliers, cands, p, wl_at=None):
    """Decide how every block gets water. Blocks are processed from high to low so a feeding
    neighbour is always decided first.
    cands {b: [(canal_id, canal_order, chainage_up, field_channel_len, turnout_xy)]}
    p: depth, turnout_loss, fgrad (m/m), spill_loss, mode ('design'|'check'),
       prefer ('lowest'|'nearest'), cascade_first (bool)
    wl_at(canal_id, chainage_up) -> canal water level or None (check mode)
    Returns {b: dict(kind, ok, ...)}; kind in direct / cascade / none / noelev."""
    svc = {}
    for b, t in tops.items():
        if t is None:
            svc[b] = dict(kind="noelev", ok=False)
    order = sorted((b for b in tops if tops[b] is not None), key=lambda b: (-tops[b], str(b)))
    check = p["mode"] == "check"
    for b in order:
        fsl = tops[b] + p["depth"]
        # ---- direct options
        opts = []
        for cid, cord, s_up, dist, txy in cands.get(b, []):
            req = fsl + p["turnout_loss"] + p["fgrad"] * dist
            wl = wl_at(cid, s_up) if (check and wl_at) else None
            m = None if wl is None else wl - req
            opts.append(dict(canal=cid, corder=cord, s=s_up, dist=dist, turnout=txy,
                             req=req, wl=wl, margin=m))

        def pref_key(o):
            if p.get("prefer", "lowest") == "nearest":
                return (o["dist"],)
            return (-(o["corder"] or 3), o["dist"])

        direct = None
        if opts:
            if not check:
                direct = min(opts, key=pref_key)
            else:
                good = [o for o in opts if o["margin"] is not None and o["margin"] >= 0]
                known = [o for o in opts if o["margin"] is not None]
                if good:
                    direct = min(good, key=pref_key)
                elif known:
                    direct = max(known, key=lambda o: o["margin"])
                else:
                    direct = min(opts, key=pref_key)
        if direct is not None:
            if not check:
                direct_ok = True
            elif direct["margin"] is None:
                direct_ok = None  # canal has no water level there
            else:
                direct_ok = direct["margin"] >= 0
        else:
            direct_ok = False
        # ---- plot-to-plot option: best already-served higher neighbour
        casc = None
        for a, drop in suppliers.get(b, []):
            sa = svc.get(a)
            if sa is None or sa.get("ok") is not True:
                continue
            if casc is None or drop > casc[1]:
                casc = (a, drop)

        def as_cascade():
            a, drop = casc
            return dict(kind="cascade", ok=True, supplier=a, drop=drop,
                        margin=drop - p["spill_loss"], steps=svc[a]["steps"] + 1,
                        root=svc[a]["root"])

        def as_direct():
            d = dict(direct)
            d.update(kind="direct", ok=direct_ok, steps=0, root=b)
            return d

        if casc is not None and p.get("cascade_first"):
            svc[b] = as_cascade()
        elif direct is not None and direct_ok is not False:
            svc[b] = as_direct()
        elif casc is not None:
            svc[b] = as_cascade()
        elif direct is not None:
            svc[b] = as_direct()  # failing (check mode)
        else:
            svc[b] = dict(kind="none", ok=False, steps=None, root=None)
    return svc


def canal_requirements(canals, svc, cgrad, s_loss):
    """Water level each canal needs at its upstream end (design rule), worked from the
    lowest level (TC) up to the main canal.
    canals {c: dict(parent, junc_s)} ; junc_s = chainage on the parent where c starts.
    Returns req {c: level or None}, crit {c: text}, depth {c: hierarchy depth}, warnings."""
    warn = []
    depth = {}
    for c in canals:
        seen, x, d = set(), c, 0
        while canals[x].get("parent") is not None:
            if x in seen or canals[x]["parent"] not in canals:
                warn.append(f"canal {x}: parent link is broken or circular - treated as a root")
                canals[x]["parent"] = None  # cut the bad link itself; depth so far stays valid
                if x in seen:
                    d = 0  # it was a loop: the canal becomes a root
                break
            seen.add(x)
            x = canals[x]["parent"]
            d += 1
        depth[c] = d
    children = {c: [] for c in canals}
    for c, v in canals.items():
        if v.get("parent") is not None:
            children[v["parent"]].append(c)
    req, crit = {}, {}
    for c in sorted(canals, key=lambda c: -depth[c]):
        best, who = None, ""
        for b, v in svc.items():
            if v.get("kind") == "direct" and v.get("canal") == c:
                r = v["req"] + cgrad * v["s"]
                if best is None or r > best:
                    best, who = r, ("block", b)
        for k in children[c]:
            if req.get(k) is None:
                continue
            r = req[k] + s_loss + cgrad * (canals[k].get("junc_s") or 0.0)
            if best is None or r > best:
                best, who = r, ("canal", k)
        req[c], crit[c] = best, who
    return req, crit, depth, warn


def canal_tree_order(cs):
    """Canals as a tree: each top canal followed by its own children (highest need first),
    with 'depth' set to the indent level in this tree."""
    ids = {r.get("id") for r in cs}
    if None in ids:
        return sorted(cs, key=lambda r: (r["depth"], -(r["req"] or -1e9)))
    lvl = {"MC": 1, "SC": 2, "TC": 3, "FC": 4}
    kids = {}
    for r in cs:
        kids.setdefault(r.get("parent") if r.get("parent") in ids else None, []).append(r)

    def key(r):
        return (lvl.get(r.get("level"), 9), -(r["req"] if r["req"] is not None else -1e9),
                str(r["name"]))
    out, seen = [], set()

    def walk(r, d):
        if r["id"] in seen:
            return
        seen.add(r["id"])
        out.append(dict(r, depth=d))
        for k in sorted(kids.get(r["id"], []), key=key):
            walk(k, d + 1)
    for r in sorted(kids.get(None, []), key=key):
        walk(r, 0)
    for r in cs:  # anything left (loops) at the end
        if r["id"] not in seen:
            walk(r, 0)
    return out


def build_report(sm):
    """Plain-language HTML results report (no QGIS objects - easy to test)."""
    from html import escape as E

    def f2(v):
        return "-" if v is None else f"{v:.2f}"
    design = sm.get("mode") != "check"
    counts = sm.get("counts", {})
    h = ["<html><head><meta charset='utf-8'><style>"
         "body{font-family:'Segoe UI',Arial,sans-serif;font-size:10pt;color:#1d2b1d}"
         "h2{color:#1e5a2b;margin:14px 0 4px}h1{color:#1e5a2b;font-size:15pt;margin:0}"
         "table{border-collapse:collapse}td,th{border:1px solid #c8d8c8;padding:3px 7px}"
         "th{background:#eef6ee;text-align:left}.ok{color:#1e7b34;font-weight:bold}"
         ".bad{color:#c0261b;font-weight:bold}.ans{background:#eef6ee;border:1px solid #9cc79c;"
         "padding:8px;font-size:11pt}.sw{width:14px}</style></head><body>",
         f"<h1>Gravity command - results ({'DESIGN' if design else 'CHECK'} mode)</h1>"]

    # ---- the answer in one or two sentences
    ans = []
    if sm.get("res_req") is not None:
        ans.append(f"The reservoir must be at least <b>{sm['res_req']:.2f} m</b> "
                   f"(via {E(str(sm.get('res_via')))}, controlled by {E(str(sm.get('res_ctrl')))}).")
        if sm.get("res_fsl") is not None:
            d = sm["res_fsl"] - sm["res_req"]
            ans.append(f"Your reservoir FSL is {sm['res_fsl']:.2f} m: "
                       + (f"<span class='ok'>OK, {d:+.2f} m to spare.</span>" if d >= 0 else
                          f"<span class='bad'>TOO LOW by {-d:.2f} m</span> - the critical blocks "
                          "below cannot be irrigated by gravity as designed."))
        elif design:
            ans.append("Tick <i>Check against reservoir FSL</i> to compare it with your reservoir.")
    if not design:
        bad = counts.get("NOT commanded", 0)
        ans.append(f"<span class='bad'>{bad} block(s) are NOT commanded</span> by your canal water levels."
                   if bad else "<span class='ok'>All blocks next to a canal are commanded.</span>")
    if not sm.get("has_canals"):
        ans.append(f"No canal layer: <b>{sm.get('heads', 0)} head block(s)</b> cannot be fed by any "
                   "neighbour and need their own turnout - your TCs must reach these blocks.")
    if design and sm.get("has_canals"):
        ans.append("In DESIGN mode the canals are not tested: the tool calculates the water level each "
                   "canal <i>needs</i>. Green means 'turnout planned', not 'proven OK'.")
    h.append("<p class='ans'>" + "<br>".join(ans) + "</p>")

    # ---- blocks
    h.append(f"<h2>Blocks ({sm.get('n_blocks', 0)})</h2><table><tr><th></th><th>Service</th>"
             "<th>Blocks</th><th>Meaning</th></tr>")
    for k in SERVICE_ORDER:
        if counts.get(k):
            col, txt = SERVICE_INFO[k]
            h.append(f"<tr><td class='sw' style='background:{col}'></td><td>{E(k)}</td>"
                     f"<td align='right'>{counts[k]}</td><td>{E(txt)}</td></tr>")
    h.append("</table>")

    # ---- critical blocks
    crit = sm.get("critical") or []
    if crit and design:
        h.append("<h2>Critical blocks - they decide how high each canal must be</h2>"
                 "<p>To lower a canal, improve these blocks first: move the turnout closer to the "
                 "high point, feed the block plot-to-plot, level the field, or pump.</p>"
                 "<table><tr><th>Block</th><th>Canal</th><th>Top (m)</th><th>Canal WL needed (m)</th>"
                 "<th>Field channel (m)</th></tr>")
        for r in crit[:30]:
            dist = "-" if r["dist"] is None else "%.0f" % r["dist"]
            h.append(f"<tr><td>{E(str(r['block']))}</td><td>{E(str(r['canal']))} ({r['level']})</td>"
                     f"<td>{f2(r['top'])}</td><td><b>{f2(r['req'])}</b></td><td>{dist}</td></tr>")
        h.append("</table>")

    # ---- canals
    cs = sm.get("canals") or []
    if cs:
        cs = canal_tree_order(cs)
        h.append("<h2>Canals (main canal first)</h2><table><tr><th>Canal</th><th>Level</th>"
                 "<th>Length (m)</th><th>Blocks</th><th>WL needed at start (m)</th><th>Controlled by</th>"
                 + ("<th>Worst margin (m)</th><th>Junction margin (m)</th>" if not design else "")
                 + "<th>WL above ground, max (m)</th></tr>")
        for r in cs[:80]:
            fill = r.get("fill")
            fcell = f2(fill) if fill is None or fill <= 1.0 else f"<span class='bad'>{fill:.2f}</span>"
            def marg(v):
                return f2(v) if v is None or v >= 0 else "<span class='bad'>%s</span>" % f2(v)
            extra = "" if design else "<td>%s</td><td>%s</td>" % (marg(r.get("worst")), marg(r.get("junc")))
            length = "-" if r["L"] is None else "%.0f" % r["L"]
            indent = "&nbsp;&nbsp;&nbsp;" * r["depth"]
            h.append(f"<tr><td>{indent}{E(str(r['name']))}</td><td>{r['level']}</td>"
                     f"<td align='right'>{length}</td>"
                     f"<td align='right'>{r['blocks']}</td><td><b>{f2(r['req'])}</b></td>"
                     f"<td>{E(str(r['crit'] or '-'))}</td>{extra}<td>{fcell}</td></tr>")
        h.append("</table><p>WL above ground &gt; 1 m (red) means a high embankment / fill is needed "
                 "on that canal.</p>")

    # ---- suggestions
    tips = []
    if sm.get("n_could"):
        tips.append(f"<b>{sm['n_could']} block(s)</b> with their own turnout could be fed plot-to-plot "
                    "from a higher neighbour. Tick <i>Prefer plot-to-plot supply</i> and run again to "
                    "see a layout with fewer turnouts.")
    if any(r.get("level") == "?" for r in cs):
        tips.append("Some canals have <b>no recognised level</b> (MC / SC / TC): they are not linked to "
                    "a parent canal, so their need does not reach the reservoir. Fix the level field.")
    if sum(1 for r in cs if r.get("level") == "MC") > 1:
        tips.append("There is more than one <b>main canal</b>. If some of them are secondary canals, "
                    "correct their level - each MC is treated as starting at the reservoir.")
    if tips:
        h.append("<h2>Suggestions</h2><ul>" + "".join(f"<li>{t}</li>" for t in tips) + "</ul>")
    notes = sm.get("notes") or []
    if notes:
        h.append("<h2>Warnings and notes from the run</h2><ul>"
                 + "".join(f"<li>{E(n)}</li>" for n in notes[:40]) + "</ul>")

    h.append("<h2>How to read the map</h2><ul>"
             "<li><b>Command_Blocks</b> colour = how the block gets water (table above). Click a block "
             "(Identify) and read <i>Status</i>.</li>"
             "<li><b>Command_Links</b> dark blue arrows = water passing from a higher block to a lower "
             "neighbour. The grey 'possible' links are switched off; tick them in the legend to see them.</li>"
             "<li><b>Turnouts</b> (yellow squares) are labelled with the canal water level they need; "
             "<b>Field_channels</b> (dashed) lead to the block's high point.</li>"
             "<li><b>Canal_Command</b> arrows point downstream and are labelled with the water level "
             "needed at the start of each canal.</li></ul></body></html>")
    return "".join(h)


# =========================================================================== #
#  QGIS helpers
# =========================================================================== #
def polygons_only(g):
    if g is None or g.isEmpty():
        return QgsGeometry()
    if g.type() == POLYGON_GEOM:
        return g
    parts = [q for q in g.asGeometryCollection() if q.type() == POLYGON_GEOM and q.area() > 0]
    return QgsGeometry.collectGeometry(parts) if parts else QgsGeometry()


def line_parts(g):
    """Single polylines of a (multi)line geometry, merged where they connect."""
    if g is None or g.isEmpty():
        return []
    if g.isMultipart():
        m = g.mergeLines()
        if m is not None and not m.isEmpty():
            g = m
    if g.isMultipart():
        return [QgsGeometry.fromPolylineXY(pl) for pl in g.asMultiPolyline() if len(pl) > 1]
    pl = g.asPolyline()
    return [QgsGeometry.fromPolylineXY(pl)] if len(pl) > 1 else []


def end_vertices(g):
    """First and last vertex of a (multi)line."""
    if g.isMultipart():
        mp = g.asMultiPolyline()
        return mp[0][0], mp[-1][-1]
    pl = g.asPolyline()
    return pl[0], pl[-1]


def unique_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


class DemSampler:
    """Median DEM value within a radius around a point (working CRS -> DEM CRS)."""

    def __init__(self, layer, band, wcrs):
        from osgeo import gdal
        self.ds = gdal.Open(layer.source().split("|")[0]) if layer.providerType() == "gdal" else None
        if self.ds is None:
            raise RuntimeError(f"DEM '{layer.name()}' cannot be read directly (needs a GDAL raster).")
        self.gt = self.ds.GetGeoTransform()
        if abs(self.gt[2]) > 1e-12 or abs(self.gt[4]) > 1e-12:
            raise RuntimeError("Rotated DEMs are not supported.")
        self.band = self.ds.GetRasterBand(band)
        self.nd = self.band.GetNoDataValue()
        self.geo = layer.crs().isGeographic()
        self.tr = None if layer.crs() == wcrs else \
            QgsCoordinateTransform(wcrs, layer.crs(), QgsProject.instance())

    def median(self, x, y, radius):
        import numpy as np
        pt = QgsPointXY(x, y) if self.tr is None else self.tr.transform(QgsPointXY(x, y))
        gt = self.gt
        r = radius / 111320.0 if self.geo else radius
        r = max(r, abs(gt[1]) * 0.75)  # at least the cell itself
        W, H = self.ds.RasterXSize, self.ds.RasterYSize
        c0 = max(0, int(math.floor((pt.x() - r - gt[0]) / gt[1])))
        c1 = min(W, int(math.ceil((pt.x() + r - gt[0]) / gt[1])))
        r0 = max(0, int(math.floor((gt[3] - (pt.y() + r)) / -gt[5])))
        r1 = min(H, int(math.ceil((gt[3] - (pt.y() - r)) / -gt[5])))
        if c0 >= c1 or r0 >= r1:
            return None
        a = self.band.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype("float64")
        cols = gt[0] + (np.arange(c0, c1) + 0.5) * gt[1]
        rows = gt[3] + (np.arange(r0, r1) + 0.5) * gt[5]
        dx, dy = np.meshgrid(cols - pt.x(), rows - pt.y())
        ok = (dx * dx + dy * dy <= r * r) & np.isfinite(a)
        if self.nd is not None:
            ok &= a != self.nd
        return float(np.median(a[ok])) if ok.any() else None


def new_layer(geom_type, name, crs, fields):
    lyr = QgsVectorLayer(geom_type, name, "memory")
    lyr.setCrs(crs)
    lyr.dataProvider().addAttributes(fields)
    lyr.updateFields()
    return lyr


def fill_layer(lyr, rows):
    """rows = [(QgsGeometry, [values])]"""
    feats = []
    for g, vals in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(g)
        f.setAttributes([NULL if v is None else v for v in vals])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()


def style_categories(lyr, field, cats, opacity=1.0, width=None, present=None):
    """Categorised style; categories whose value is not in `present` are left out."""
    try:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsSymbol
        rc = []
        if present is not None and any(c[0] in present for c in cats):
            cats = [c for c in cats if c[0] in present]
        for val, col, lab in cats:
            sym = QgsSymbol.defaultSymbol(lyr.geometryType())
            sym.setColor(QColor(col))
            sym.setOpacity(opacity)
            if width is not None and hasattr(sym, "setWidth"):
                sym.setWidth(width)
            rc.append(QgsRendererCategory(val, sym, lab))
        lyr.setRenderer(QgsCategorizedSymbolRenderer(field, rc))
    except Exception:
        return None  # styling is cosmetic


def arrow_symbol(color, width):
    from qgis.core import QgsLineSymbol
    try:
        from qgis.core import QgsArrowSymbolLayer
        sl = QgsArrowSymbolLayer.create({"arrow_width": str(width), "arrow_start_width": str(width),
                                         "head_length": "2.2", "head_thickness": "1.4",
                                         "head_type": "0", "arrow_type": "0", "is_curved": "0",
                                         "is_repeated": "0"})
        sl.subSymbol().setColor(QColor(color))
        sym = QgsLineSymbol()
        sym.changeSymbolLayer(0, sl)
        return sym
    except Exception:
        return QgsLineSymbol.createSimple({"color": color, "width": str(width)})


def style_arrows(lyr, field, cats, present=None, hidden=()):
    """Arrow style per category; `hidden` categories start switched off in the legend."""
    try:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
        if present is not None and any(c[0] in present for c in cats):
            cats = [c for c in cats if c[0] in present]
        rc = [QgsRendererCategory(v, arrow_symbol(c, w), lab, v not in hidden) for v, c, w, lab in cats]
        lyr.setRenderer(QgsCategorizedSymbolRenderer(field, rc))
    except Exception:
        return None  # styling is cosmetic


def add_labels(lyr, expression, size=8.0, color="#08306b", on_line=False):
    """Simple expression labels with a white halo."""
    try:
        from qgis.core import (QgsPalLayerSettings, QgsTextBufferSettings, QgsTextFormat,
                               QgsVectorLayerSimpleLabeling)
        st = QgsPalLayerSettings()
        st.fieldName = expression
        st.isExpression = True
        fmt = QgsTextFormat()
        fmt.setSize(size)
        fmt.setColor(QColor(color))
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.8)
        buf.setColor(QColor("white"))
        fmt.setBuffer(buf)
        st.setFormat(fmt)
        if on_line:
            st.placement = enum_value((Qgis, 'LabelPlacement.Line'), (QgsPalLayerSettings, 'Placement.Line'),
                                      (QgsPalLayerSettings, 'Line'))
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(st))
        lyr.setLabelsEnabled(True)
    except Exception:
        return None  # styling is cosmetic


def mid_point(g):
    """Point half-way along a (multi)line geometry, or None."""
    try:
        m = g.interpolate(g.length() / 2.0)
        if m is not None and not m.isEmpty():
            return m.asPoint()
    except Exception:
        m = None  # fall back to the centroid below
    try:
        return g.nearestPoint(g.centroid()).asPoint()
    except Exception:
        return None


# =========================================================================== #
#  Analysis
# =========================================================================== #
def run_command(p, log):
    t0 = time.time()
    notes = []
    _log = log

    def log(msg):  # noqa: F811 - keep WARNING / NOTE lines for the results report
        m = str(msg).strip()
        if m.startswith(("WARNING", "NOTE")):
            notes.append(m)
        _log(msg)
    prj = QgsProject.instance()
    blocks, pts, canal_lyr = p["blocks"], p["points"], p.get("canals")
    dem, res_lyr = p.get("dem"), p.get("res_point")
    if blocks is None or not blocks.isValid():
        raise ValueError("Choose the block (polygon) layer.")
    if pts is None or not pts.isValid():
        raise ValueError("Choose the high-point layer (e.g. HighestPoints).")
    if not p["resample"] and not p["elev_field"]:
        raise ValueError("Choose the elevation field of the high points, or tick 'Re-sample "
                         "elevation from the DEM'.")
    if p["resample"] and dem is None:
        raise ValueError("Re-sampling needs a DEM.")
    check = p["mode"] == "check"
    if check and canal_lyr is None:
        raise ValueError("CHECK mode needs a canal layer with water level fields.")
    if check and not p["wl_field"]:
        raise ValueError("Choose the canal water level field(s) for CHECK mode.")

    # ---- working CRS (metres)
    wcrs = None
    for lyr_ in (blocks, canal_lyr, dem, pts):
        if lyr_ is not None and lyr_.crs().isValid() and not lyr_.crs().isGeographic():
            wcrs = lyr_.crs()
            break
    if wcrs is None:
        raise ValueError("All layers are in degrees. Reproject the blocks to UTM (e.g. EPSG:32648).")
    log(f"Working CRS: {wcrs.authid()} (metres)")

    def to_w(lyr):
        return None if lyr.crs() == wcrs else QgsCoordinateTransform(lyr.crs(), wcrs, prj)

    sampler = DemSampler(dem, p["band"], wcrs) if dem is not None else None

    # ---- 1) blocks
    tb = to_w(blocks)
    lf = p.get("label_field")
    B = {}
    feats = blocks.getSelectedFeatures() if (p["selected_only"] and blocks.selectedFeatureCount()) \
        else blocks.getFeatures()
    n_fixed = 0
    for f in feats:
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        g = QgsGeometry(g)
        if tb:
            g.transform(tb)
        if not g.isGeosValid():
            g = polygons_only(g.makeValid())
            n_fixed += 1
            if g.isEmpty():
                continue
        lab = f[lf] if lf and f.fields().indexOf(lf) != -1 else None
        lab = str(lab) if lab not in (None, NULL) and str(lab).strip() else f"fid {f.id()}"
        B[f.id()] = dict(geom=g, label=lab, attrs=f.attributes())
    if not B:
        raise ValueError("No block polygons to process.")
    log(f"Blocks: {len(B)}" + (f" ({n_fixed} invalid geometries repaired in memory)" if n_fixed else ""))
    bidx = QgsSpatialIndex()
    for k, b in B.items():
        bidx.addFeature(k, b["geom"].boundingBox())

    def lab(k):
        return B[k]["label"] if k in B else str(k)

    # ---- 2) high point per block
    tp = to_w(pts)
    hp, n_dup, n_out, n_noz = {}, 0, 0, 0
    for f in pts.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        g = QgsGeometry(g)
        if tp:
            g.transform(tp)
        pt = g.asMultiPoint()[0] if g.isMultipart() else g.asPoint()
        pg = QgsGeometry.fromPointXY(pt)
        bid, bd = None, None
        for c in bidx.intersects(QgsRectangle(pt.x() - 0.01, pt.y() - 0.01, pt.x() + 0.01, pt.y() + 0.01)):
            if B[c]["geom"].intersects(pg):
                bid = c
                break
        if bid is None:  # point just outside (on the edge / dike)
            for c in bidx.nearestNeighbor(pt, 3):
                d = B[c]["geom"].distance(pg)
                if d <= p["match_tol"] and (bd is None or d < bd):
                    bid, bd = c, d
        if bid is None:
            n_out += 1
            continue
        if p["resample"]:
            z = sampler.median(pt.x(), pt.y(), p["sample_r"])
        else:
            z = num(f[p["elev_field"]]) if f.fields().indexOf(p["elev_field"]) != -1 else None
        if z is None:
            n_noz += 1
            continue
        if bid in hp:
            n_dup += 1
            if z <= hp[bid][0]:
                continue
        hp[bid] = (z, pt.x(), pt.y())
    tops = {k: (hp[k][0] if k in hp else None) for k in B}
    no_top = [k for k in B if tops[k] is None]
    log(f"High points matched to {len(hp)} / {len(B)} blocks"
        + (" (elevation re-sampled from the DEM)" if p["resample"] else f" (field '{p['elev_field']}')"))
    if n_dup:
        log(f"  NOTE: {n_dup} extra point(s) in blocks that already had one - the highest is used")
    if n_out:
        log(f"  NOTE: {n_out} point(s) are not inside / within {p['match_tol']:g} m of a block - ignored")
    if n_noz:
        log(f"  NOTE: {n_noz} point(s) without elevation - ignored")
    if no_top:
        log(f"  WARNING: {len(no_top)} block(s) have no high point (e.g. 'Combine features' was used "
            f"in the first tool): {', '.join(lab(k) for k in no_top[:12])}"
            + (" ..." if len(no_top) > 12 else ""))

    # ---- 3) neighbours and plot-to-plot links
    tol, minlen = p["adj_tol"], p["min_shared"]
    bnd = {k: QgsGeometry(b["geom"].constGet().boundary()) for k, b in B.items()}
    bufs = {}
    border_pt = {}  # (a, b) -> point in the middle of their shared border
    nbrs = {k: {} for k in B}
    for a in B:
        ra = QgsRectangle(B[a]["geom"].boundingBox())
        ra.grow(tol)
        for c in bidx.intersects(ra):
            if c <= a:
                continue
            if B[a]["geom"].distance(B[c]["geom"]) > tol:
                continue
            if c not in bufs:
                bufs[c] = B[c]["geom"].buffer(tol, 4)
            shared_g = bnd[a].intersection(bufs[c])
            shared = shared_g.length()
            if shared >= minlen:
                nbrs[a][c] = nbrs[c][a] = shared
                mp_ = mid_point(shared_g)
                if mp_ is not None:
                    border_pt[(a, c)] = border_pt[(c, a)] = mp_
    n_pairs = sum(len(v) for v in nbrs.values()) // 2
    suppliers = build_suppliers(tops, nbrs, p["spill_loss"])
    heads = [k for k in B if tops[k] is not None and not suppliers[k]]
    isolated = [k for k in B if not nbrs[k]]
    log(f"Neighbours: {n_pairs} pairs (gap tolerance {tol:g} m, shared border >= {minlen:g} m); "
        f"{len(heads)} head block(s) need their own turnout")
    if isolated:
        log(f"  NOTE: {len(isolated)} block(s) have no neighbour - increase the gap tolerance if "
            f"blocks are separated by wide dikes/roads")

    # ---- 4) canals
    canals = {}
    cands = {k: [] for k in B}
    if canal_lyr is not None:
        tc = to_w(canal_lyr)
        nf, of = p.get("canal_name"), p.get("canal_level")
        wl1, wl2 = p.get("wl_field"), p.get("wl_field2")
        groups = {}
        for f in canal_lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            g = QgsGeometry(g)
            if tc:
                g.transform(tc)
            name = f[nf] if nf and f.fields().indexOf(nf) != -1 else None
            name = str(name).strip() if name not in (None, NULL) and str(name).strip() else None
            key = name if (name and p["dissolve"]) else f"#{f.id()}"
            gr = groups.setdefault(key, dict(name=name or f"canal {f.id()}", geoms=[], orders=[],
                                             samples=[]))
            gr["geoms"].append(g)
            o = parse_order(f[of]) if of and f.fields().indexOf(of) != -1 else None
            if o is not None:
                gr["orders"].append(o)
            if check:
                e0, e1 = end_vertices(g)
                if p["wl_mode"] == "const":
                    wa = wb = num(f[wl1])
                else:
                    wa, wb = num(f[wl1]), (num(f[wl2]) if wl2 else None)
                    wa = wb if wa is None else wa  # only one level known -> constant
                    wb = wa if wb is None else wb
                if wa is not None:
                    gr["samples"].append((e0, e1, wa, wb))
        unknown_lvl = 0
        for key, gr in groups.items():
            merged = QgsGeometry.unaryUnion(gr["geoms"]) if len(gr["geoms"]) > 1 else gr["geoms"][0]
            parts = line_parts(merged)
            if len(parts) > 1 and len(gr["geoms"]) > 1:
                log(f"  NOTE: '{gr['name']}' does not form one continuous line - "
                    f"split into {len(parts)} reaches")
            order = min(gr["orders"]) if gr["orders"] else None
            if order is None:
                unknown_lvl += 1
            for i, part in enumerate(parts):
                cid = len(canals)
                L = part.length()
                samp = []
                for e0, e1, wa, wb in gr["samples"]:
                    g0, g1 = QgsGeometry.fromPointXY(e0), QgsGeometry.fromPointXY(e1)
                    if part.distance(g0) > 0.5 or part.distance(g1) > 0.5:
                        continue  # this feature belongs to another reach
                    a, b = part.lineLocatePoint(g0), part.lineLocatePoint(g1)
                    if a > b:  # feature digitised against the merged line
                        a, b, wa, wb = b, a, wb, wa
                    samp.append((a, b, wa, wb))
                samp.sort()
                canals[cid] = dict(name=gr["name"] + (f" ({i + 1})" if len(parts) > 1 else ""),
                                   order=order, geom=part, L=L, rev=False, parent=None,
                                   junc_s=None, samples=samp)
        log(f"Canals: {len(canals)} reach(es)" +
            (f"; {unknown_lvl} without a recognised level (MC/SC/TC) - not linked into the "
             "hierarchy" if unknown_lvl and of else "") +
            ("" if of else "; no level field - each canal is treated on its own"))
        if check:
            nos = [c["name"] for c in canals.values() if not c["samples"]]
            if nos:
                log(f"  WARNING: no water level for {len(nos)} canal(s): {', '.join(nos[:8])}"
                    + (" ..." if len(nos) > 8 else ""))

        # orientation and parent canal
        cidx = QgsSpatialIndex()
        for c, v in canals.items():
            cidx.addFeature(c, v["geom"].boundingBox())
        jt = p["junc_tol"]
        rpt = None
        if res_lyr is not None:
            for f in res_lyr.getFeatures():
                g = QgsGeometry(f.geometry())
                if not g.isEmpty():
                    tr_ = to_w(res_lyr)
                    if tr_:
                        g.transform(tr_)
                    rpt = g
                    break
        for c, v in canals.items():
            pl = v["geom"].asPolyline()
            ends = [QgsGeometry.fromPointXY(pl[0]), QgsGeometry.fromPointXY(pl[-1])]
            best = [None, None]  # per end: (order, dist, parent)
            if v["order"] is not None:
                for e_i, eg in enumerate(ends):
                    r_ = eg.boundingBox()
                    r_.grow(jt)
                    for q in cidx.intersects(r_):
                        o = canals[q]["order"]
                        if q == c or o is None or o >= v["order"]:
                            continue
                        d = canals[q]["geom"].distance(eg)
                        if d > jt:
                            continue
                        cand = (-o, d, q)  # nearest higher-level canal: SC before MC
                        if best[e_i] is None or cand < best[e_i]:
                            best[e_i] = cand
            if p["direction"] == "digitised":
                up = 0
                if best[0] is None and best[1] is not None:
                    log(f"  WARNING: '{v['name']}' meets its parent canal at its LAST vertex - "
                        "check the digitising direction (or use direction 'Auto')")
            elif best[0] is not None or best[1] is not None:
                if best[0] is None:
                    up = 1
                elif best[1] is None:
                    up = 0
                else:
                    up = 0 if best[0] <= best[1] else 1
            elif rpt is not None:
                up = 0 if ends[0].distance(rpt) <= ends[1].distance(rpt) else 1
            elif sampler is not None:
                z0 = sampler.median(pl[0].x(), pl[0].y(), 2.0)
                z1 = sampler.median(pl[-1].x(), pl[-1].y(), 2.0)
                up = 1 if (z0 is not None and z1 is not None and z1 > z0) else 0
            else:
                up = 0
            v["rev"] = up == 1
            if best[up] is not None:
                v["parent"] = best[up][2]
                v["_jpt"] = ends[up]
        for c, v in canals.items():  # junction chainage on the parent (parent oriented above)
            if v["parent"] is not None:
                par = canals[v["parent"]]
                sd = par["geom"].lineLocatePoint(par["geom"].nearestPoint(v["_jpt"]))
                v["junc_s"] = par["L"] - sd if par["rev"] else sd
        roots = [c for c, v in canals.items() if v["parent"] is None]
        log(f"  hierarchy: {len(canals) - len(roots)} canal(s) linked to a parent, {len(roots)} start "
            "at the reservoir / have no parent")

        # turnout candidates
        sd_max = p["search"]
        for b in B:
            if tops[b] is None:
                continue
            hpg = QgsGeometry.fromPointXY(QgsPointXY(hp[b][1], hp[b][2]))
            r_ = QgsRectangle(B[b]["geom"].boundingBox())
            r_.grow(sd_max)
            for c in cidx.intersects(r_):
                v = canals[c]
                if v["geom"].distance(B[b]["geom"]) > sd_max:
                    continue
                tpt = v["geom"].nearestPoint(hpg)
                sdig = v["geom"].lineLocatePoint(tpt)
                s_up = v["L"] - sdig if v["rev"] else sdig
                cands[b].append((c, v["order"], s_up, tpt.distance(hpg), tpt.asPoint()))

    def wl_at(c, s_up):
        v = canals[c]
        return profile_value(v["samples"], v["L"] - s_up if v["rev"] else s_up)

    # ---- 5) service
    svc = assign_service(tops, suppliers, cands, dict(
        depth=p["depth"], turnout_loss=p["turnout_loss"], fgrad=p["fgrad"],
        spill_loss=p["spill_loss"], mode=p["mode"], prefer=p["prefer"],
        cascade_first=p["cascade_first"]), wl_at if check else None)

    # ---- 6) canal levels
    req, crit, cdepth = {}, {}, {}
    if canals:
        req, crit, cdepth, warns = canal_requirements(canals, svc, p["cgrad"], p["struct_loss"])
        for w_ in warns:
            log("  WARNING: " + w_)
        if not check:  # design level at each turnout and the surplus over the block's need
            for b, v in svc.items():
                if v.get("kind") == "direct" and req.get(v["canal"]) is not None:
                    v["wl"] = req[v["canal"]] - p["cgrad"] * v["s"]
                    v["margin"] = v["wl"] - v["req"]

    def crit_text(c):
        w = crit.get(c)
        if not w:
            return None
        return f"block {lab(w[1])}" if w[0] == "block" else f"canal {canals[w[1]]['name']}"

    # ---- 7) output layers
    out = {}
    root = prj.layerTreeRoot()
    grp = root.insertGroup(0, "Gravity command " + time.strftime("%H:%M:%S"))

    def add(lyr):
        prj.addMapLayer(lyr, False)
        grp.addLayer(lyr)
        out[lyr.name()] = lyr

    # 7a) blocks
    src_fields = blocks.fields()
    new_specs = [("Top_z", T_DOUBLE), ("FSL_req", T_DOUBLE), ("Head", T_STRING), ("Service", T_STRING),
                 ("Status", T_STRING), ("Canal", T_STRING), ("Canal_lvl", T_STRING),
                 ("Chain_m", T_DOUBLE), ("Turnout_m", T_DOUBLE), ("Req_WL", T_DOUBLE),
                 ("Canal_WL", T_DOUBLE), ("Margin_m", T_DOUBLE), ("Supplier", T_STRING),
                 ("Steps", T_INT), ("Root_block", T_STRING), ("Feeds_n", T_INT), ("Higher_nb", T_INT)]
    new_names = {n.lower() for n, _ in new_specs}
    flds = []
    for f_ in src_fields:
        q = QgsField(f_)
        if q.name().lower() in new_names or q.name().lower() == "fid":
            q.setName("o_" + q.name())
        flds.append(q)
    flds += [make_field(n, t) for n, t in new_specs]
    bl = new_layer("MultiPolygon", "Command_Blocks", wcrs, flds)
    feeds = {}
    for b, v in svc.items():
        if v.get("kind") == "cascade":
            feeds[v["supplier"]] = feeds.get(v["supplier"], 0) + 1

    def r3(x):
        return None if x is None else round(x, 3)
    crit_blocks = {w[1] for w in crit.values() if w and w[0] == "block"}
    rows, counts = [], {}
    rep_canals = []
    for b, info in B.items():
        v, t = svc[b], tops[b]
        k = v["kind"]
        cname = clvl = None
        if k == "noelev":
            serv, stat = "No elevation", "No high point in this block - run the first tool or add a point"
        elif k == "cascade":
            serv = "Cascade"
            stat = (f"Plot-to-plot from {lab(v['supplier'])} (drop {v['drop']:.2f} m, "
                    f"margin {v['margin']:+.2f} m)")
        elif k == "direct":
            cv = canals[v["canal"]]
            cname, clvl = cv["name"], ORDER_NAMES.get(cv["order"], "?")
            if not check:
                serv = "Direct (critical)" if b in crit_blocks else "Direct"
                stat = (f"Turnout on {cname} at ch {v['s']:.0f} m: canal WL needed "
                        f"{v['req']:.2f} m" + (" (controls this canal)" if crit.get(v["canal"]) ==
                                                ("block", b) else ""))
            elif v["ok"] is None:
                serv = "Direct (WL unknown)"
                stat = f"{cname} has no water level at this turnout - cannot check"
            elif v["ok"]:
                serv = "Direct"
                stat = f"Commanded by {cname}: WL {v['wl']:.2f} >= needed {v['req']:.2f} m"
            else:
                serv = "NOT commanded"
                stat = (f"{cname} WL {v['wl']:.2f} < needed {v['req']:.2f} m ({v['margin']:+.2f}) - "
                        "raise the canal, move the turnout, feed from a neighbour or pump")
        else:
            serv = "Not served"
            if not cands.get(b) and not suppliers.get(b):
                stat = (f"Head block, no canal within {p['search']:g} m - needs a TC / turnout"
                        if canals else "Head block - needs a canal turnout")
            elif not cands.get(b):
                stat = (f"No canal within {p['search']:g} m and its higher neighbours are not served")
            else:
                stat = "Not served"
        counts[serv] = counts.get(serv, 0) + 1
        g = QgsGeometry(info["geom"])
        g.convertToMultiType()
        vals = list(info["attrs"]) + [
            r3(t), r3(None if t is None else t + p["depth"]),
            None if t is None else ("Y" if b in heads else "N"), serv, stat, cname, clvl,
            r3(v.get("s")) if k == "direct" else None, r3(v.get("dist")) if k == "direct" else None,
            r3(v.get("req")) if k == "direct" else None, r3(v.get("wl")) if k == "direct" else None,
            r3(v.get("margin")), lab(v["supplier"]) if k == "cascade" else None, v.get("steps"),
            lab(v["root"]) if v.get("root") is not None else None, feeds.get(b, 0),
            len(suppliers.get(b, []))]
        rows.append((g, vals))
    fill_layer(bl, rows)
    style_categories(bl, "Service", [("Direct (critical)", "#7b2d8e", "Direct - CONTROLS the canal level"),
                                     ("Direct", "#2e9e44", "Direct from canal"),
                                     ("Cascade", "#6fb7e8", "Plot-to-plot"),
                                     ("NOT commanded", "#e0301e", "NOT commanded"),
                                     ("Direct (WL unknown)", "#b38add", "Direct - WL unknown"),
                                     ("Not served", "#f39c12", "Not served"),
                                     ("No elevation", "#9e9e9e", "No elevation")], opacity=0.55,
                     present=set(counts))

    # 7b) plot-to-plot links
    ll = new_layer("LineString", "Command_Links", wcrs,
                   [make_field("from_blk", T_STRING), make_field("to_blk", T_STRING),
                    make_field("drop_m", T_DOUBLE), make_field("used", T_INT)])
    rows = []
    for b, lst in suppliers.items():
        for a, drop in lst:
            used = 1 if svc[b].get("kind") == "cascade" and svc[b].get("supplier") == a else 0
            via = [QgsPointXY(border_pt[(a, b)])] if (a, b) in border_pt else []
            g = QgsGeometry.fromPolylineXY([QgsPointXY(hp[a][1], hp[a][2])] + via +
                                           [QgsPointXY(hp[b][1], hp[b][2])])
            rows.append((g, [lab(a), lab(b), round(drop, 3), used]))
    fill_layer(ll, rows)
    style_arrows(ll, "used", [(1, "#1f4e9e", 0.7, "Used plot-to-plot supply"),
                              (0, "#9aa5b1", 0.25, "Possible (not used) - tick to show")],
                 hidden=(0,))

    # 7c) turnouts + field channels, 7d) canals
    if canals:
        tl = new_layer("Point", "Turnouts", wcrs,
                       [make_field("block", T_STRING), make_field("canal", T_STRING),
                        make_field("level", T_STRING), make_field("chain_m", T_DOUBLE),
                        make_field("fc_len_m", T_DOUBLE), make_field("req_wl", T_DOUBLE),
                        make_field("canal_wl", T_DOUBLE), make_field("margin_m", T_DOUBLE),
                        make_field("critical", T_STRING)])
        fl = new_layer("LineString", "Field_channels", wcrs,
                       [make_field("block", T_STRING), make_field("canal", T_STRING),
                        make_field("length_m", T_DOUBLE)])
        trow, frow = [], []
        for b, v in svc.items():
            if v.get("kind") != "direct":
                continue
            cv = canals[v["canal"]]
            tpt = v["turnout"]
            trow.append((QgsGeometry.fromPointXY(tpt),
                         [lab(b), cv["name"], ORDER_NAMES.get(cv["order"], "?"), r3(v["s"]),
                          r3(v["dist"]), r3(v["req"]), r3(v.get("wl")), r3(v.get("margin")),
                          "Y" if crit.get(v["canal"]) == ("block", b) else "N"]))
            frow.append((QgsGeometry.fromPolylineXY([tpt, QgsPointXY(hp[b][1], hp[b][2])]),
                         [lab(b), cv["name"], r3(v["dist"])]))
        fill_layer(tl, trow)
        fill_layer(fl, frow)
        try:
            from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer, QgsLineSymbol
            tl.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
                {"name": "square", "color": "255,215,0", "outline_color": "0,0,0", "size": "2.4"})))
            fl.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple(
                {"color": "0,150,200", "width": "0.35", "line_style": "dash"})))
        except Exception:
            log('NOTE: layer styling skipped')

        cl = new_layer("LineString", "Canal_Command", wcrs, [
            make_field("canal", T_STRING), make_field("level", T_STRING), make_field("length_m", T_DOUBLE),
            make_field("upstream", T_STRING), make_field("parent", T_STRING), make_field("junc_ch_m", T_DOUBLE),
            make_field("blocks_n", T_INT), make_field("child_n", T_INT), make_field("Req_WL_up", T_DOUBLE),
            make_field("Req_WL_dn", T_DOUBLE), make_field("Critical", T_STRING), make_field("WL_up", T_DOUBLE),
            make_field("WL_dn", T_DOUBLE), make_field("Worst_marg", T_DOUBLE), make_field("Junc_marg", T_DOUBLE),
            make_field("Gnd_min", T_DOUBLE), make_field("Gnd_max", T_DOUBLE),
            make_field("WL_gnd_max", T_DOUBLE), make_field("WL_gnd_min", T_DOUBLE)])
        nblk, worst = {}, {}
        for b, v in svc.items():
            if v.get("kind") == "direct":
                nblk[v["canal"]] = nblk.get(v["canal"], 0) + 1
                m_ = v.get("margin")
                if m_ is not None and (v["canal"] not in worst or m_ < worst[v["canal"]]):
                    worst[v["canal"]] = m_
        nchild = {}
        for c, v in canals.items():
            if v["parent"] is not None:
                nchild[v["parent"]] = nchild.get(v["parent"], 0) + 1
        rows, bad_j, bad_c = [], 0, 0
        for c, v in canals.items():
            L = v["L"]
            pl = v["geom"].asPolyline()
            g = QgsGeometry.fromPolylineXY(list(reversed(pl)) if v["rev"] else pl)
            rq = req.get(c)
            rq_dn = None if rq is None else rq - p["cgrad"] * L
            wl_up = wl_at(c, 0.0) if check else None
            wl_dn = wl_at(c, L) if check else None
            m_up = worst.get(c) if check else None  # worst turnout margin on this canal
            jm = None
            if check and v["parent"] is not None:
                wp = wl_at(v["parent"], v["junc_s"])
                if wp is not None and wl_up is not None:
                    jm = wp - p["struct_loss"] - wl_up
            if jm is not None and jm < 0:
                bad_j += 1
            if m_up is not None and m_up < 0:
                bad_c += 1
            gmin = gmax = fmax = fmin = None
            if sampler is not None:
                step = max(p["gnd_step"], L / 2000.0)
                n_ = max(2, int(L / step) + 1)
                for i in range(n_):
                    s_up = min(L, i * step) if i < n_ - 1 else L
                    sdig = L - s_up if v["rev"] else s_up
                    q = v["geom"].interpolate(sdig)
                    if q is None or q.isEmpty():
                        continue
                    qp = q.asPoint()
                    z = sampler.median(qp.x(), qp.y(), 1.0)
                    if z is None:
                        continue
                    gmin = z if gmin is None else min(gmin, z)
                    gmax = z if gmax is None else max(gmax, z)
                    w = wl_at(c, s_up) if check else (None if rq is None else rq - p["cgrad"] * s_up)
                    if w is not None:
                        fmax = w - z if fmax is None else max(fmax, w - z)
                        fmin = w - z if fmin is None else min(fmin, w - z)
            rows.append((g, [v["name"], ORDER_NAMES.get(v["order"], "?"), r3(L),
                             "end" if v["rev"] else "start",
                             canals[v["parent"]]["name"] if v["parent"] is not None else None,
                             r3(v["junc_s"]), nblk.get(c, 0), nchild.get(c, 0), r3(rq), r3(rq_dn),
                             crit_text(c), r3(wl_up), r3(wl_dn), r3(m_up), r3(jm),
                             r3(gmin), r3(gmax), r3(fmax), r3(fmin)]))
        fill_layer(cl, rows)
        rep_canals = [dict(name=r_[1][0], level=r_[1][1], L=r_[1][2], blocks=r_[1][6], req=r_[1][8],
                           crit=r_[1][10], worst=r_[1][13], junc=r_[1][14], fill=r_[1][17],
                           depth=cdepth.get(c_, 0), id=c_, parent=canals[c_]["parent"])
                      for c_, r_ in zip(canals, rows)]
        add_labels(cl, "\"canal\" || coalesce('  need ' || format_number(\"Req_WL_up\", 2) || ' m', '')"
                   + (" || coalesce('  has ' || format_number(\"WL_up\", 2), '')" if check else ""),
                   size=8.5, color="#08306b", on_line=True)
        add_labels(tl, "format_number(\"req_wl\", 2)", size=7.0, color="#5a3d00")
        style_arrows(cl, "level", [("MC", "#08306b", 1.4, "Main canal"),
                                   ("SC", "#2171b5", 1.0, "Secondary canal"),
                                   ("TC", "#41b6c4", 0.7, "Tertiary canal"),
                                   ("FC", "#7fcdbb", 0.5, "Field / quaternary"),
                                   ("?", "#636363", 0.6, "Level unknown")],
                     present={r_[1][1] for r_ in rows})
        for l_ in (tl, cl, fl):
            add(l_)
    add(ll)
    add(bl)

    # ---- 8) reservoir and summary
    log("-" * 60)
    for k_ in SERVICE_ORDER:
        if counts.get(k_):
            log(f"{k_:<22}: {counts[k_]} block(s)")
    summary = {"counts": counts, "heads": len(heads)}
    if canals:
        # only top-level canals (normally the MC) start at the reservoir; a lower canal without a
        # parent is an unconnected (orphan) canal and must not drive the reservoir level
        known = [v["order"] for v in canals.values() if v["order"] is not None]
        top = min(known) if known else None
        roots = [c for c, v in canals.items() if v["parent"] is None
                 and (top is None or v["order"] is None or v["order"] == top)]
        orphans = [c for c, v in canals.items() if v["parent"] is None and c not in roots]
        if orphans:
            log(f"WARNING: {len(orphans)} canal(s) are not joined to a higher-level canal within "
                f"{p['junc_tol']:g} m and are left out of the reservoir level: "
                + ", ".join(canals[c]["name"] for c in orphans[:10]) + (" ..." if len(orphans) > 10 else "")
                + "  -> check their ends touch the parent canal, or raise the junction tolerance")
        need = [(req[c] + p["struct_loss"], c) for c in roots if req.get(c) is not None]
        if need:
            rmax, rc = max(need)
            summary["res_req"] = rmax
            log(f"Reservoir level needed (design rule): {rmax:.2f} m  (via {canals[rc]['name']}, "
                f"controlled by {crit_text(rc)})")
            if p.get("res_fsl") is not None:
                d_ = p["res_fsl"] - rmax
                summary["res_margin"] = d_
                log(f"Reservoir FSL {p['res_fsl']:.2f} m -> margin {d_:+.2f} m "
                    + ("OK" if d_ >= 0 else "TOO LOW: lower the commanded area or pump the high blocks"))
        if check:
            if p.get("res_fsl") is not None:
                for c in roots:
                    wu = wl_at(c, 0.0)
                    if wu is not None:
                        d_ = p["res_fsl"] - p["struct_loss"] - wu
                        log(f"  {canals[c]['name']}: reservoir FSL - loss - canal start WL = {d_:+.2f} m"
                            + ("" if d_ >= 0 else "  <-- reservoir cannot feed this level"))
            if bad_j:
                log(f"WARNING: {bad_j} junction(s) where the child canal starts higher than its parent "
                    "can deliver (see Junc_marg < 0)")
            if bad_c:
                log(f"WARNING: {bad_c} canal(s) too low for some of their blocks (Worst_marg < 0)")
    # blocks with a turnout that a served higher neighbour could feed instead
    n_could = 0
    if not p["cascade_first"]:
        n_could = sum(1 for b, v in svc.items() if v.get("kind") == "direct" and
                      any(svc.get(a, {}).get("ok") is True for a, _ in suppliers.get(b, [])))
    crit_rows = []
    for c, w in crit.items():
        if w and w[0] == "block":
            v = svc[w[1]]
            crit_rows.append(dict(block=lab(w[1]), canal=canals[c]["name"],
                                  level=ORDER_NAMES.get(canals[c]["order"], "?"), top=tops[w[1]],
                                  req=v.get("req"), dist=v.get("dist"), s=v.get("s")))
    crit_rows.sort(key=lambda r_: -(r_["req"] or 0))
    summary.update(mode=p["mode"], n_blocks=len(B), canals=rep_canals, critical=crit_rows,
                   n_could=n_could, notes=notes, res_fsl=p.get("res_fsl"),
                   res_via=(canals[rc]["name"] if canals and summary.get("res_req") is not None else None),
                   res_ctrl=(crit_text(rc) if canals and summary.get("res_req") is not None else None),
                   has_canals=bool(canals), cascade_first=p["cascade_first"])
    log(f"Done in {time.time() - t0:.1f} s")
    summary["layers"] = out
    summary["report"] = build_report(summary)
    return summary


def save_gpkg(layers, path, log):
    path = unique_path(path)
    first = True
    for name, lyr in layers.items():
        o = QgsVectorFileWriter.SaveVectorOptions()
        o.driverName = "GPKG"
        o.layerName = name
        o.fileEncoding = "UTF-8"
        o.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if first
                                  else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
        res = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path, QgsProject.instance().transformContext(), o)
        if res[0] != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(f"Could not write {name}: {res[1]}")
        first = False
    log(f"Saved GeoPackage: {path}")
    return path

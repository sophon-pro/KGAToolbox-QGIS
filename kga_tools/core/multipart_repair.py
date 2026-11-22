# -*- coding: utf-8 -*-
"""Multipart Line Repair - the engine (no GUI).

Built for lines edited many times with Split / Trim-Extend / Reshape, where
"Merge lines" alone fails because the parts do not touch EXACTLY:
  - small gaps between part ends (undershoot)
  - overlapping / duplicated segments (overshoot, double digitising)
  - tiny leftover parts (slivers after cutting)
  - duplicate vertices

Repair applied to every true multipart feature:
  1. remove duplicate vertices
  2. drop tiny parts shorter than "Min part length"
  3. snap part END-POINTS that are closer than "Snap tolerance"
  4. dissolve overlaps (union) and merge lines
  -> one part left: the feature is fixed (same feature, same attributes)
  -> several parts left: they are really separate (branches, distant pieces)
     and are exploded into single features (attributes copied)
"""

from qgis.core import (
    Qgis, QgsFeatureRequest, QgsGeometry, QgsLineString, QgsPointXY, QgsWkbTypes,
)

LINE_GEOM = Qgis.GeometryType.Line if hasattr(Qgis, "GeometryType") else QgsWkbTypes.LineGeometry


def _straight(part):
    """A single-part geometry as a plain line string (arcs and compound curves segmentized)."""
    if isinstance(part, QgsLineString):
        return QgsGeometry(part.clone())
    return QgsGeometry(part.curveToLine())


def parts_of(geom):
    """Split any line geometry into a list of single-part QgsGeometry."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    ag = geom.constGet()
    if geom.isMultipart():
        return [_straight(ag.geometryN(i)) for i in range(ag.numGeometries())]
    return [_straight(ag)]


def part_count(geom):
    if geom.isNull() or geom.isEmpty():
        return 0
    return geom.constGet().numGeometries() if geom.isMultipart() else 1


def snap_endpoints(parts, tol):
    """Move part end-points that lie within `tol` of another part's end-point
    to their common average position (cluster), so they touch exactly."""
    pts = []  # (part index, vertex index, point)
    for i, p in enumerate(parts):
        ls = p.constGet()
        if not isinstance(ls, QgsLineString) or ls.numPoints() < 2:
            continue
        n = ls.numPoints()
        pts.append((i, 0, QgsPointXY(ls.xAt(0), ls.yAt(0))))
        pts.append((i, n - 1, QgsPointXY(ls.xAt(n - 1), ls.yAt(n - 1))))

    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a in range(len(pts)):
        for b in range(a + 1, len(pts)):
            if pts[a][0] == pts[b][0]:
                continue  # never join a part to itself
            if pts[a][2].distance(pts[b][2]) <= tol:
                parent[find(a)] = find(b)

    clusters = {}
    for k in range(len(pts)):
        clusters.setdefault(find(k), []).append(k)

    for members in clusters.values():
        if len(members) < 2:
            continue
        cx = sum(pts[k][2].x() for k in members) / len(members)
        cy = sum(pts[k][2].y() for k in members) / len(members)
        for k in members:
            i, vi, _ = pts[k]
            ls = parts[i].get()
            ls.setXAt(vi, cx)
            ls.setYAt(vi, cy)
    return parts


def repair(geom, s):
    """Return the list of single-part geometries after cleaning."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    parts = parts_of(geom)

    if s["dedupe"]:
        for p in parts:
            p.removeDuplicateNodes(1e-6)
    parts = [p for p in parts if not p.isEmpty() and p.constGet().nCoordinates() >= 2]
    if not parts:
        return []

    if s["min_len"] > 0 and len(parts) > 1:
        keep = [p for p in parts if p.length() >= s["min_len"]]
        parts = keep or [max(parts, key=lambda p: p.length())]

    if s["tol"] > 0 and len(parts) > 1:
        snap_endpoints(parts, s["tol"])

    if len(parts) == 1:
        return parts

    g = QgsGeometry.unaryUnion(parts) if s["union"] else QgsGeometry.collectGeometry(parts)
    if g is None or g.isEmpty():
        return parts
    g = g.mergeLines()
    out = [p for p in parts_of(g) if p.length() > 0]
    return out or parts


def diagnose(geom, s):
    """Explain why a multipart feature is multipart and what repair will do."""
    parts = parts_of(geom)
    lengths = [p.length() for p in parts]
    max_gap, overlap = 0.0, False
    for i, p in enumerate(parts):
        others = [q for j, q in enumerate(parts) if j != i]
        if others:
            max_gap = max(max_gap, min(p.distance(q) for q in others))
        for q in parts[i + 1:]:
            if p.intersects(q) and p.intersection(q).length() > 1e-6:
                overlap = True

    causes = []
    if overlap:
        causes.append("overlap")
    if s["min_len"] > 0 and min(lengths) < s["min_len"]:
        causes.append("tiny part")
    if max_gap == 0 and not overlap:
        causes.append("touching")
    elif 0 < max_gap <= s["tol"]:
        causes.append("small gap")
    elif max_gap > s["tol"]:
        causes.append("gap > tolerance")

    after = len(repair(geom, s))
    status = "Fixable -> 1 part" if after <= 1 else f"Separate -> {after} features"
    return {"parts": len(parts), "shortest": min(lengths), "max_gap": max_gap,
            "cause": ", ".join(causes), "after": after, "status": status}


def bad_ids(lyr):
    req = QgsFeatureRequest().setNoAttributes()
    return [f.id() for f in lyr.getFeatures(req) if part_count(f.geometry()) > 1]


def scan_layer(lyr, s):
    total = multi_type = empty = 0
    details = []
    req = QgsFeatureRequest().setNoAttributes()
    for f in lyr.getFeatures(req):
        total += 1
        g = f.geometry()
        n = part_count(g)
        if n == 0:
            empty += 1
            continue
        if g.isMultipart():
            multi_type += 1
        if n > 1:
            d = diagnose(g, s)
            d["fid"] = f.id()
            details.append(d)
    return total, multi_type, empty, details


def match_type(g, wkb):
    """Make geometry fit the layer type (single/multi, Z, M)."""
    g = QgsGeometry(g)
    gw = g.wkbType()
    if QgsWkbTypes.hasZ(wkb) and not QgsWkbTypes.hasZ(gw):
        g.get().addZValue(0)
    elif not QgsWkbTypes.hasZ(wkb) and QgsWkbTypes.hasZ(gw):
        g.get().dropZValue()
    if QgsWkbTypes.hasM(wkb) and not QgsWkbTypes.hasM(gw):
        g.get().addMValue(0)
    elif not QgsWkbTypes.hasM(wkb) and QgsWkbTypes.hasM(gw):
        g.get().dropMValue()
    if QgsWkbTypes.isMultiType(wkb):
        g.convertToMultiType()
    else:
        g.convertToSingleType()
    return g


def build_repaired_layer(lyr, s, log=print):
    """Memory copy of the layer (exact clone of fields, type and CRS made by
    QGIS itself), then the multipart geometries are repaired inside the copy."""
    mem = lyr.materialize(QgsFeatureRequest())
    log(f"  copied {mem.featureCount()} of {lyr.featureCount()} features to memory")
    wkb = mem.wkbType()
    changes = {}
    for fid in bad_ids(mem):
        f = mem.getFeature(fid)
        parts = repair(f.geometry(), s)
        if parts:
            changes[fid] = match_type(QgsGeometry.collectGeometry(parts), wkb)
    if changes:
        pr = mem.dataProvider()
        if not pr.changeGeometryValues(changes):
            log("  ! some repaired geometries were rejected: " + "; ".join(pr.errors()[-3:]))
        log(f"  repaired {len(changes)} multipart geometries")
    mem.updateExtents()
    return mem

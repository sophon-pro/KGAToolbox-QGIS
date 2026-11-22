# -*- coding: utf-8 -*-
"""Sliver and Spike Cleaner - the geometry engine (no GUI).

Cleans the overhangs, wedges and thin strips left after splitting polygons
with lines, one feature at a time:

  0. Validate     - repair invalid geometry, keep polygons only
  1. Spikes       - sharp tips (angle) are square-trimmed along the corner
                    edge, or the tip vertex is removed
  2. Narrow parts - any part narrower than WIDTH is removed, however long it
                    is (morphological opening with mitre joins, so normal
                    corners stay square). A whole feature narrower than WIDTH
                    can be deleted or kept and flagged
  3. Small parts  - parts and holes smaller than MIN AREA are removed
  4. Finishing    - vertices snapped back to the original coordinates,
                    duplicate and extra collinear vertices removed

`clean_feature(geometry, cfg, stats)` is the entry point.
"""

import math

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsWkbTypes

CAP_FLAT = Qgis.EndCapStyle.Flat
JOIN_MITER = Qgis.JoinStyle.Miter
MITER_LIMIT = 5.0


def angle_at(a, b, c):
    """Angle (degrees) at vertex b between b->a and b->c."""
    v1 = (a.x() - b.x(), a.y() - b.y())
    v2 = (c.x() - b.x(), c.y() - b.y())
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 0.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos))


def tri_area(a, b, c):
    return abs((b.x() - a.x()) * (c.y() - a.y()) -
               (c.x() - a.x()) * (b.y() - a.y())) / 2.0


def ring_area(ring):
    return QgsGeometry.fromPolygonXY([ring]).area()


def polygonal_only(g):
    """Return only the polygon content of a geometry (or empty geometry)."""
    if g is None or g.isEmpty():
        return QgsGeometry()
    if g.type() == QgsWkbTypes.PolygonGeometry:
        return g
    parts = [p for p in g.asGeometryCollection()
             if p.type() == QgsWkbTypes.PolygonGeometry and not p.isEmpty()]
    return QgsGeometry.collectGeometry(parts) if parts else QgsGeometry()


def to_polys(g):
    return g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]


def from_polys(polys):
    if len(polys) == 1:
        return QgsGeometry.fromPolygonXY(polys[0])
    return QgsGeometry.fromMultiPolygonXY(polys)


def make_valid(g):
    if g.isGeosValid():
        return QgsGeometry(g)
    return polygonal_only(g.makeValid())


class RefIndex:
    """Grid index of original vertices, used to snap results back to the
    exact original coordinates (keeps shared edges identical)."""

    def __init__(self, tol):
        self.tol = tol if tol > 0 else 1e-6
        self.grid = {}
        self.keys = set()

    def _cell(self, x, y):
        return (int(math.floor(x / self.tol)), int(math.floor(y / self.tol)))

    def add_geom(self, g):
        for poly in to_polys(g):
            for ring in poly:
                for p in ring:
                    self.grid.setdefault(self._cell(p.x(), p.y()), []).append(p)
                    self.keys.add((round(p.x(), 6), round(p.y(), 6)))

    def snap(self, p):
        cx, cy = self._cell(p.x(), p.y())
        best, best_d = None, self.tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in self.grid.get((cx + dx, cy + dy), ()):
                    d = math.hypot(q.x() - p.x(), q.y() - p.y())
                    if d <= best_d:
                        best, best_d = q, d
        return QgsPointXY(best) if best is not None else p

    def has(self, p):
        return (round(p.x(), 6), round(p.y(), 6)) in self.keys


# ============================================================================
# Step 1 - spikes
# ============================================================================

def line_intersect(p1, p2, p3, p4):
    d1x, d1y = p2.x() - p1.x(), p2.y() - p1.y()
    d2x, d2y = p4.x() - p3.x(), p4.y() - p3.y()
    den = d1x * d2y - d1y * d2x
    if abs(den) < 1e-12:
        return None
    ex, ey = p3.x() - p1.x(), p3.y() - p1.y()
    t = (ex * d2y - ey * d2x) / den
    s = (ex * d1y - ey * d1x) / den
    return QgsPointXY(p1.x() + t * d1x, p1.y() + t * d1y), t, s


def try_square_trim(pts, i, max_trim):
    n = len(pts)
    if n < 5:
        return None
    PP, P, T = pts[i - 2], pts[i - 1], pts[i]
    N, NN = pts[(i + 1) % n], pts[(i + 2) % n]
    dev_n = abs(180 - angle_at(T, N, NN))
    dev_p = abs(180 - angle_at(PP, P, T))
    order = ['N', 'P'] if dev_n >= dev_p else ['P', 'N']
    eps = 1e-9
    for side in order:
        if side == 'N':
            r = line_intersect(P, T, NN, N)
            if not r:
                continue
            X, t, s = r
            if not (eps < t <= 1 + eps and s >= 1 - eps) or tri_area(X, T, N) > max_trim:
                continue
            new = list(pts)
            new[i] = X
            del new[(i + 1) % n]
            return new
        else:
            r = line_intersect(N, T, PP, P)
            if not r:
                continue
            X, t, s = r
            if not (eps < t <= 1 + eps and s >= 1 - eps) or tri_area(P, T, X) > max_trim:
                continue
            new = list(pts)
            new[i] = X
            del new[i - 1]
            return new
    return None


def clean_spikes_ring(ring, angle_tol, mode, max_trim, stats):
    pts = list(ring[:-1])
    skip = set()
    changed = True
    while changed and len(pts) > 3:
        changed = False
        dedup = [p for i, p in enumerate(pts) if p != pts[i - 1]]
        if len(dedup) != len(pts):
            pts, changed = dedup, True
            continue
        n = len(pts)
        for i in range(n):
            key = (round(pts[i].x(), 6), round(pts[i].y(), 6))
            if key in skip:
                continue
            if angle_at(pts[i - 1], pts[i], pts[(i + 1) % n]) >= angle_tol:
                continue
            if mode == 'trim':
                new = try_square_trim(pts, i, max_trim)
                if new:
                    pts = new
                    stats['trimmed'] += 1
                    changed = True
                    break
            if tri_area(pts[i - 1], pts[i], pts[(i + 1) % n]) <= max_trim:
                del pts[i]
                stats['tips'] += 1
                changed = True
                break
            skip.add(key)
            stats['skipped'] += 1
    return pts + [pts[0]] if len(pts) >= 3 else None


# ============================================================================
# Step 2 - narrow parts (morphological opening)
# ============================================================================

def opening(g, width):
    """Remove every part narrower than `width` (shrink then grow back)."""
    d = width / 2.0
    e = g.buffer(-d, 8, CAP_FLAT, JOIN_MITER, MITER_LIMIT)
    if e is None or e.isEmpty():
        return QgsGeometry()
    return e.buffer(d, 8, CAP_FLAT, JOIN_MITER, MITER_LIMIT)


# ============================================================================
# Step 3 + 4 - small parts, snap, tidy
# ============================================================================

def tidy_ring(ring, ref, do_snap):
    pts = [ref.snap(p) for p in ring[:-1]] if do_snap else list(ring[:-1])
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        # duplicate / near-duplicate vertices
        for i in range(len(pts)):
            a, b = pts[i - 1], pts[i]
            if math.hypot(a.x() - b.x(), a.y() - b.y()) < 1e-6:
                del pts[i]
                changed = True
                break
        if changed:
            continue
        # extra collinear vertices created by the cleaning (originals kept)
        n = len(pts)
        for i in range(n):
            if ref.has(pts[i]):
                continue
            if angle_at(pts[i - 1], pts[i], pts[(i + 1) % n]) > 179.5:
                del pts[i]
                changed = True
                break
    return pts + [pts[0]] if len(pts) >= 3 else None


def finish_polys(polys, min_area, ref, do_snap, stats):
    out = []
    for poly in polys:
        rings = []
        for k, ring in enumerate(poly):
            r = tidy_ring(ring, ref, do_snap)
            if not r or ring_area(r) < min_area:
                stats['small'] += 1
                if k == 0:          # outer ring gone -> whole part gone
                    rings = None
                    break
                continue            # hole removed (filled)
            rings.append(r)
        if rings:
            out.append(rings)
    return out


# ============================================================================
# Full pipeline for one feature
# ============================================================================

def clean_feature(g_orig, cfg, stats):
    """Returns (new_geometry or None, status)."""
    g_orig = QgsGeometry(g_orig)
    if QgsWkbTypes.isCurvedType(g_orig.wkbType()):
        g_orig.convertToStraightSegment()
    g0 = make_valid(g_orig)
    if g0.isEmpty():
        return None, 'invalid / empty'
    ref = RefIndex(cfg['snap_tol'])
    ref.add_geom(g0)
    g = g0

    # ---- Step 1: spikes ----
    if cfg['spike_on']:
        polys = []
        for poly in to_polys(g):
            rings = []
            for k, ring in enumerate(poly):
                r = clean_spikes_ring(ring, cfg['angle'], cfg['mode'],
                                      cfg['max_trim'], stats)
                if r:
                    rings.append(r)
                elif k == 0:
                    rings = None
                    break
            if rings:
                polys.append(rings)
        if not polys:
            return None, 'small feature'
        g = make_valid(from_polys(polys))
        if g.isEmpty():
            return None, 'small feature'
        ref.add_geom(g)

    status = 'ok'

    # ---- Step 2: narrow parts ----
    if cfg['width_on'] and cfg['width'] > 0:
        op = polygonal_only(opening(g, cfg['width']))
        if op.isEmpty():
            stats['narrow_feat'] += 1
            if cfg['narrow_action'] == 'delete':
                return None, 'narrow feature'
            status = 'narrow_kept'          # keep as is, flag for review
        else:
            g = op

    # ---- Step 3 + 4: small parts / holes, snap back, tidy ----
    polys = finish_polys(to_polys(g), cfg['min_area'], ref,
                         cfg['snap_tol'] > 0, stats)
    if not polys:
        return None, 'small feature'
    g = make_valid(from_polys(polys))
    if g.isEmpty():
        return None, 'small feature'
    return g, status

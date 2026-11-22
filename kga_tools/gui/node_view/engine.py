# -*- coding: utf-8 -*-
"""The numbers behind the vertices and nodes view: no widgets, no map canvas, nothing that has to run
in the GUI thread.

``compute`` takes feature sources (``QgsVectorLayerFeatureSource``, which may be read from any thread),
reads the line and polygon geometries inside a rectangle, and returns three arrays of map coordinates:

* ``squares``  - vertices that belong to one feature only;
* ``circles``  - nodes: vertices where two features meet (a shared vertex, a line ending on another
  feature, a polygon corner on a neighbour's edge) plus the points where two features cross without
  a vertex;
* ``diamonds`` - the midpoints of the segments (only when asked for).

Everything is numpy: geometries are read from their WKB in one ``frombuffer`` per ring (no per-vertex
Python objects), nodes are found with a uniform grid and vectorised point-to-segment / segment-to-segment
tests, and the result is thinned to one marker per few screen pixels so a zoomed-out view stays cheap to
draw whatever the data holds.
"""

import time
from contextlib import suppress

import numpy as np

#: Candidate pairs examined per numpy block (bounds memory: ~100 bytes per pair).
BLOCK = 1_500_000
#: A grid whose segment-to-cell entries would pass this is coarsened (long segments cover many cells).
MAX_ENTRIES = 6_000_000
#: Give up looking for crossings above this many candidate pairs, and stop looking after this long.
MAX_CROSS_PAIRS = 40_000_000
ANALYSIS_BUDGET_S = 2.5


class Stop(Exception):
    """The request was cancelled or a limit was reached; see ``reason``."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Request(object):
    """What to compute.

    ``layers``: list of (source, filter_rect, transform) - the feature source, the rectangle in the
    layer's CRS and a QgsCoordinateTransform to the map CRS (or None when the CRS is the same).
    ``rect``: (xmin, ymin, xmax, ymax) in map CRS; ``mupp`` map units per pixel.
    """

    def __init__(self, layers, rect, mupp, vertices=True, nodes=True, midpoints=False, max_vertices=80000,
                 vertex_px=7.0, node_px=10.0):
        self.layers = layers
        self.rect = tuple(rect)
        self.mupp = float(mupp)
        self.vertices = vertices
        self.nodes = nodes
        self.midpoints = midpoints
        self.max_vertices = int(max_vertices)
        self.vertex_px = vertex_px
        self.node_px = node_px


class Result(object):
    """What ``compute`` found, in map coordinates."""

    def __init__(self, request=None):
        empty = np.empty((0, 2))
        self.squares = empty
        self.circles = empty
        self.diamonds = empty
        self.rect = request.rect if request is not None else (0, 0, 0, 0)
        self.mupp = request.mupp if request is not None else 1.0
        self.too_many = False          # more vertices in view than the limit: nothing is drawn
        self.partial = False           # the crossings search ran out of time / pairs
        self.error = ''
        self.seconds = 0.0
        self.vertex_count = 0
        self.key = None                # set by the controller: what the request depended on


# ---------------------------------------------------------------------------------------- geometry reading

class _Unsupported(Exception):
    pass


def _parse_wkb(buf, pos, out):
    """Append (coords Nx2, is_polygon_ring) for every line / ring in the WKB at *pos*; return the next pos."""
    little = buf[pos] == 1
    pos += 1
    endian = '<' if little else '>'
    code = int.from_bytes(buf[pos:pos + 4], 'little' if little else 'big')
    pos += 4
    dim = 2
    if code & 0x80000000:
        dim += 1
    if code & 0x40000000:
        dim += 1
    has_srid = code & 0x20000000
    code &= 0x0FFFFFFF
    if code >= 1000:                    # ISO WKB: 1000 = Z, 2000 = M, 3000 = ZM
        dim = (2, 3, 3, 4)[code // 1000]
        code %= 1000
    if has_srid:
        pos += 4
    u32 = np.dtype(endian + 'u4')
    f64 = np.dtype(endian + 'f8')
    if code == 2:                       # LineString
        n = int(np.frombuffer(buf, u32, 1, pos)[0])
        pos += 4
        out.append((np.frombuffer(buf, f64, n * dim, pos).reshape(n, dim)[:, :2], False))
        return pos + n * dim * 8
    if code == 3:                       # Polygon
        rings = int(np.frombuffer(buf, u32, 1, pos)[0])
        pos += 4
        for _ in range(rings):
            n = int(np.frombuffer(buf, u32, 1, pos)[0])
            pos += 4
            out.append((np.frombuffer(buf, f64, n * dim, pos).reshape(n, dim)[:, :2], True))
            pos += n * dim * 8
        return pos
    if code in (4, 5, 6, 7):            # Multi* / collection
        count = int(np.frombuffer(buf, u32, 1, pos)[0])
        pos += 4
        for _ in range(count):
            pos = _parse_wkb(buf, pos, out)
        return pos
    if code == 1:                       # Point: not a line, nothing to show
        return pos + dim * 8
    raise _Unsupported(code)


def _fallback_rings(geometry):
    """Rings of a geometry the WKB reader does not know (curves): straight lines through its vertices."""
    out = []
    for part in geometry.parts():
        name = part.geometryType() if hasattr(part, 'geometryType') else ''
        if name == 'Polygon' or hasattr(part, 'exteriorRing'):
            rings = [part.exteriorRing()] + [part.interiorRing(i) for i in range(part.numInteriorRings())]
            for ring in rings:
                pts = [(v.x(), v.y()) for v in ring.vertices()]
                if len(pts) > 2:
                    out.append((np.array(pts, dtype='f8'), True))
        elif name == 'LineString' or hasattr(part, 'numPoints') or hasattr(part, 'vertices'):
            pts = []
            with suppress(Exception):
                pts = [(v.x(), v.y()) for v in part.vertices()]
            if len(pts) > 1:
                out.append((np.array(pts, dtype='f8'), False))
    return out


def _reproject(geometry, transform):
    """Transform *geometry* in place to the map CRS; False if that fails (a point outside the CRS's area)."""
    try:
        return geometry.transform(transform) == 0
    except Exception:
        return False


def rings_of(geometry):
    """[(coords Nx2, is_polygon_ring)] of a QgsGeometry (lines and polygons; points give nothing)."""
    out = []
    try:
        _parse_wkb(bytes(geometry.asWkb()), 0, out)
        return out
    except _Unsupported:
        return _fallback_rings(geometry)
    except (ValueError, IndexError):
        return []


class _Collector(object):
    """Gathers rings from several layers into flat arrays."""

    def __init__(self, limit):
        self.limit = limit
        self.count = 0                    # vertices so far
        self.feature = 0                  # running feature number: the "who owns this vertex" id
        self._coords = []
        self._lens = []
        self._closed = []
        self._fids = []

    def add(self, rings):
        if not rings:
            return
        self.feature += 1
        for coords, polygon in rings:
            n = len(coords)
            closed = polygon or (n > 2 and coords[0, 0] == coords[-1, 0] and coords[0, 1] == coords[-1, 1])
            if closed:
                n -= 1                    # the closing vertex repeats the first one
            if n < 2:
                continue
            self._coords.append(coords[:n])
            self._lens.append(n)
            self._closed.append(closed)
            self._fids.append(self.feature)
            self.count += n

    def arrays(self):
        """(V Nx2, nxt N, fid N): vertices, the index of each one's next vertex (-1 for the end of an open
        line) and the feature number."""
        if not self._coords:
            return np.empty((0, 2)), np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
        v = np.ascontiguousarray(np.concatenate(self._coords), dtype='f8')
        lens = np.array(self._lens, dtype=np.int64)
        closed = np.array(self._closed, dtype=bool)
        starts = np.cumsum(lens) - lens
        nxt = np.arange(1, len(v) + 1, dtype=np.int64)
        nxt[starts + lens - 1] = np.where(closed, starts, -1)
        fid = np.repeat(np.array(self._fids, dtype=np.int64), lens)
        return v, nxt, fid


# ---------------------------------------------------------------------------------------- analysis

def _cells(points, origin, cell):
    return np.floor((points - origin) / cell).astype(np.int64)


def _segment_grid(a, b, origin, cell, tol):
    """Sorted (key, segment index) pairs of every grid cell each (tol-grown) segment touches, or None
    if that would be too many entries. Returns (keys, segs, stride)."""
    lo = np.minimum(a, b) - tol
    hi = np.maximum(a, b) + tol
    c0 = _cells(lo, origin, cell)
    c1 = _cells(hi, origin, cell)
    nx = c1[:, 0] - c0[:, 0] + 1
    ny = c1[:, 1] - c0[:, 1] + 1
    counts = nx * ny
    total = int(counts.sum())
    if total > MAX_ENTRIES:
        return None
    seg = np.repeat(np.arange(len(a), dtype=np.int64), counts)
    off = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
    nxr = nx[seg]
    ix = c0[seg, 0] + off % nxr
    iy = c0[seg, 1] + off // nxr
    stride = int(max(c1[:, 1].max(), 0)) + 3
    keys = ix * stride + iy
    order = np.argsort(keys, kind='stable')
    return keys[order], seg[order], stride


def _find_nodes(v, nxt, fid, tol, cell, deadline, cancel, want_crossings):
    """(is_node per vertex, crossing points Mx2, partial)."""
    n = len(v)
    is_node = np.zeros(n, dtype=bool)
    crossings = np.empty((0, 2))
    partial = False
    idx = np.nonzero(nxt >= 0)[0]
    if len(idx) == 0:
        return is_node, crossings, partial
    a = v[idx]
    b = v[nxt[idx]]
    sfid = fid[idx]
    origin = v.min(axis=0) - cell
    grid = None
    for _ in range(5):
        grid = _segment_grid(a, b, origin, cell, tol)
        if grid is not None:
            break
        cell *= 2.0
    if grid is None:
        return is_node, crossings, True
    keys, segs, stride = grid

    # vertex on / at another feature's segment
    vc = _cells(v, origin, cell)
    vkey = vc[:, 0] * stride + vc[:, 1]
    lo = np.searchsorted(keys, vkey, 'left')
    hi = np.searchsorted(keys, vkey, 'right')
    cnt = hi - lo
    csum = np.cumsum(cnt)
    start = 0
    tol2 = tol * tol
    while start < n:
        base = csum[start - 1] if start else 0
        end = int(np.searchsorted(csum, base + BLOCK, 'right'))
        end = min(max(end, start + 1), n)
        c = cnt[start:end]
        total = int(c.sum())
        if total:
            vi = np.repeat(np.arange(start, end, dtype=np.int64), c)
            pos = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(c) - c, c) + np.repeat(lo[start:end], c)
            sj = segs[pos]
            ok = fid[vi] != sfid[sj]
            vi, sj = vi[ok], sj[ok]
            if len(vi):
                pa = a[sj]
                ab = b[sj] - pa
                ap = v[vi] - pa
                den = np.einsum('ij,ij->i', ab, ab)
                t = np.clip(np.einsum('ij,ij->i', ap, ab) / np.maximum(den, 1e-300), 0.0, 1.0)
                d = ap - t[:, None] * ab
                hit = np.einsum('ij,ij->i', d, d) <= tol2
                is_node[vi[hit]] = True
        start = end
        if cancel():
            raise Stop('cancelled')
        if time.perf_counter() > deadline:
            partial = True
            return is_node, crossings, partial

    if want_crossings:
        crossings, partial = _find_crossings(a, b, sfid, keys, segs, tol, deadline, cancel)
    return is_node, crossings, partial


def _find_crossings(a, b, sfid, keys, segs, tol, deadline, cancel):
    """Points where two segments of different features cross each other away from both ends."""
    m = len(keys)
    if m < 2:
        return np.empty((0, 2)), False
    group_end = np.searchsorted(keys, keys, 'right')
    pairs = group_end - np.arange(m, dtype=np.int64) - 1
    if int(pairs.sum()) > MAX_CROSS_PAIRS:
        return np.empty((0, 2)), True
    csum = np.cumsum(pairs)
    found = []
    start = 0
    partial = False
    while start < m:
        base = csum[start - 1] if start else 0
        end = int(np.searchsorted(csum, base + BLOCK, 'right'))
        end = min(max(end, start + 1), m)
        c = pairs[start:end]
        total = int(c.sum())
        if total:
            i = np.repeat(np.arange(start, end, dtype=np.int64), c)
            j = i + 1 + (np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(c) - c, c))
            si, sj = segs[i], segs[j]
            ok = sfid[si] != sfid[sj]
            si, sj = si[ok], sj[ok]
            if len(si):
                p, r = a[si], b[si] - a[si]
                q, s = a[sj], b[sj] - a[sj]
                den = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
                qp = q - p
                good = np.abs(den) > 1e-12 * np.maximum(np.hypot(r[:, 0], r[:, 1]) * np.hypot(s[:, 0], s[:, 1]),
                                                        1e-300)
                den_safe = np.where(good, den, 1.0)
                t = (qp[:, 0] * s[:, 1] - qp[:, 1] * s[:, 0]) / den_safe
                u = (qp[:, 0] * r[:, 1] - qp[:, 1] * r[:, 0]) / den_safe
                lr = np.hypot(r[:, 0], r[:, 1])
                ls = np.hypot(s[:, 0], s[:, 1])
                # away from both ends by more than the tolerance: the end cases are vertex-on-segment nodes
                inside = (good & (t * lr > tol) & ((1.0 - t) * lr > tol) & (u * ls > tol) & ((1.0 - u) * ls > tol))
                if inside.any():
                    found.append(p[inside] + t[inside, None] * r[inside])
        start = end
        if cancel():
            raise Stop('cancelled')
        if time.perf_counter() > deadline:
            partial = True
            break
    if not found:
        return np.empty((0, 2)), partial
    pts = np.concatenate(found)
    # the same pair can be met in several grid cells
    key = np.round(pts / max(tol, 1e-12)).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    return pts[np.sort(first)], partial


def thin(points, cell, origin):
    """One point per square of side *cell* (the first one met)."""
    if len(points) < 2 or cell <= 0:
        return points
    c = np.floor((points - origin) / cell).astype(np.int64)
    key = c[:, 0] * (int(c[:, 1].max()) + 3) + c[:, 1]
    _, first = np.unique(key, return_index=True)
    return points[np.sort(first)]


# ---------------------------------------------------------------------------------------- the entry point

def compute(request, cancel=lambda: False):
    """Read the layers of *request* and return a Result. Raises Stop('cancelled') when cancel() says so."""
    started = time.perf_counter()
    result = Result(request)
    from qgis.core import QgsFeatureRequest, QgsRectangle
    collector = _Collector(request.max_vertices)
    for source, rect, transform in request.layers:
        if cancel():
            raise Stop('cancelled')
        feature_request = QgsFeatureRequest().setFilterRect(QgsRectangle(*rect))
        feature_request.setSubsetOfAttributes([])
        for count, feature in enumerate(source.getFeatures(feature_request)):
            if count & 63 == 0:
                if cancel():
                    raise Stop('cancelled')
                time.sleep(0)                   # let the GUI thread have the interpreter now and then
            geometry = feature.geometry()
            if geometry.isNull():
                continue
            if transform is not None and not _reproject(geometry, transform):
                continue
            collector.add(rings_of(geometry))
            if collector.count > request.max_vertices:
                result.too_many = True
                result.vertex_count = collector.count
                result.seconds = time.perf_counter() - started
                return result
    v, nxt, fid = collector.arrays()
    result.vertex_count = len(v)
    if not len(v):
        result.seconds = time.perf_counter() - started
        return result
    mupp = request.mupp
    origin = np.array([request.rect[0], request.rect[1]])
    is_node = np.zeros(len(v), dtype=bool)
    crossings = np.empty((0, 2))
    if request.nodes:
        tol = max(mupp * 0.5, 1e-9)
        cell = max(mupp * 24.0, tol * 8.0)
        is_node, crossings, result.partial = _find_nodes(
            v, nxt, fid, tol, cell, started + ANALYSIS_BUDGET_S, cancel, want_crossings=True)
    if request.vertices:
        result.squares = thin(v[~is_node] if request.nodes else v, mupp * max(request.vertex_px * 0.6, 3.0),
                              origin)
    if request.nodes:
        circles = np.concatenate([v[is_node], crossings]) if len(crossings) else v[is_node]
        result.circles = thin(circles, mupp * max(request.node_px * 0.5, 3.0), origin)
    if request.midpoints:
        idx = np.nonzero(nxt >= 0)[0]
        a, b = v[idx], v[nxt[idx]]
        long_enough = np.hypot(*(b - a).T) > mupp * 16.0       # a midpoint on a shorter segment would only crowd it
        mids = (a[long_enough] + b[long_enough]) * 0.5
        result.diamonds = thin(mids, mupp * 4.0, origin)
    result.seconds = time.perf_counter() - started
    return result

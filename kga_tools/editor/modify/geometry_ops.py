"""
Plain geometry maths behind the Reshape tools (no map canvas, no layers): extend or trim a line to a
boundary, round or square the corner between two lines, change a line's length, reverse a line.

Lines are lists of QgsPointXY; results are new lists / QgsGeometry, or None when the case does not work.
Z and M values are not kept by the line edits (the result is a plain 2D line).
"""

import math

from qgis.core import Qgis, QgsAbstractGeometry, QgsCircularString, QgsGeometry, QgsPoint, QgsPointXY

EPS = 1e-9


# ---------------------------------------------------------------------------------------- vectors
def _sub(a, b):
    return (a.x() - b.x(), a.y() - b.y())


def _dot(u, v):
    return u[0] * v[0] + u[1] * v[1]


def _norm(u):
    return math.hypot(u[0], u[1])


def _unit(u):
    n = _norm(u)
    return (u[0] / n, u[1] / n) if n > EPS else None


def _plus(p, u, k=1.0):
    return QgsPointXY(p.x() + u[0] * k, p.y() + u[1] * k)


def line_intersection(a1, b1, a2, b2):
    """Where the infinite lines a1-b1 and a2-b2 cross, or None if parallel."""
    d1, d2 = _sub(b1, a1), _sub(b2, a2)
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) < EPS * max(1.0, _norm(d1) * _norm(d2)):
        return None
    w = _sub(a2, a1)
    t = (w[0] * d2[1] - w[1] * d2[0]) / cross
    return _plus(a1, d1, t)


def closest_on_segment(a, b, p):
    d = _sub(b, a)
    length2 = _dot(d, d)
    t = 0.0 if length2 < EPS else max(0.0, min(1.0, _dot(_sub(p, a), d) / length2))
    return _plus(a, d, t)


def nearest_segment(pts, p):
    """(index of the segment nearest p, the nearest point on it)."""
    best = None
    for i in range(len(pts) - 1):
        q = closest_on_segment(pts[i], pts[i + 1], p)
        d = _norm(_sub(q, p))
        if best is None or d < best[0]:
            best = (d, i, q)
    return best[1], best[2]


def clean(pts):
    """Drop consecutive duplicate vertices."""
    out = []
    for p in pts:
        if not out or _norm(_sub(p, out[-1])) > EPS:
            out.append(QgsPointXY(p))
    return out


# ---------------------------------------------------------------------------------------- line <-> geometry
def single_line(geom):
    """Vertices of a one-part line, else None (polygons and multi-part lines are not handled)."""
    if geom is None or geom.isNull() or geom.type() != Qgis.GeometryType.Line:
        return None
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
        return list(parts[0]) if len(parts) == 1 else None
    return list(geom.asPolyline())


def line_like(original, pts):
    """A geometry of the same kind as `original` (single or multi-part line) made from pts."""
    g = QgsGeometry.fromPolylineXY(pts)
    if original.isMultipart():
        g.convertToMultiType()
    return g


# ---------------------------------------------------------------------------------------- corner
def corner(pts1, click1, pts2, click2, radius=0.0, flip=(False, False)):
    """
    Meet two lines at their corner: each line is cut (or extended) on the segment nearest its click so that it
    ends where the lines meet, or - with a radius - where a circular arc of that radius touches it.
    `flip` keeps the other side of the corner on the first / second line instead of the clicked one.
    Returns (new pts1, new pts2, arc pts or None), or (None, message, None).
    """
    i1, q1 = nearest_segment(pts1, click1)
    i2, q2 = nearest_segment(pts2, click2)
    a1, b1, a2, b2 = pts1[i1], pts1[i1 + 1], pts2[i2], pts2[i2 + 1]
    p = line_intersection(a1, b1, a2, b2)
    if p is None:
        return None, "the two segments are parallel, so they never meet", None
    d1, d2 = _unit(_sub(b1, a1)), _unit(_sub(b2, a2))
    if d1 is None or d2 is None:
        return None, "a line has a zero-length segment there", None
    s1 = _dot(_sub(q1, p), d1)
    s2 = _dot(_sub(q2, p), d2)
    if abs(s1) < EPS or abs(s2) < EPS:
        s1 = s1 or 1.0                  # the click is right on the corner: keep the segment's far end
        s2 = s2 or 1.0
    if flip[0]:
        s1 = -s1
    if flip[1]:
        s2 = -s2
    u1 = (d1[0], d1[1]) if s1 > 0 else (-d1[0], -d1[1])        # from the corner towards the part that stays
    u2 = (d2[0], d2[1]) if s2 > 0 else (-d2[0], -d2[1])
    cos_t = max(-1.0, min(1.0, _dot(u1, u2)))
    theta = math.acos(cos_t)
    if theta < 1e-4:
        return None, "the two lines leave the corner in the same direction", None
    t1 = t2 = p
    arc = None
    if radius > 0:
        if abs(math.pi - theta) < 1e-6:
            return None, "the two lines are straight on, there is no corner to round", None
        reach = radius / math.tan(theta / 2)
        bis = _unit((u1[0] + u2[0], u1[1] + u2[1]))
        centre = _plus(p, bis, radius / math.sin(theta / 2))
        t1, t2 = _plus(p, u1, reach), _plus(p, u2, reach)
        mid = _plus(centre, bis, -radius)
        arc = _arc(t1, mid, t2)
    new1 = _cut(pts1, i1, t1, u1, d1)
    new2 = _cut(pts2, i2, t2, u2, d2)
    if new1 is None or new2 is None:
        which = "the first" if new1 is None else "the second"
        return None, (f"{which} line is too short on that segment for this corner "
                      "(try a smaller radius or click a longer segment)"), None
    return new1, new2, arc


def corner_radius_at(pts1, click1, pts2, click2, point):
    """
    The radius of the arc that touches the second line at the point of it nearest `point` (and the first line the
    same distance from the corner): what Fillet uses when no fixed radius is set, so the mouse sets the size.
    None when the lines have no corner to round.
    """
    i1, _q1 = nearest_segment(pts1, click1)
    i2, q2 = nearest_segment(pts2, click2)
    p = line_intersection(pts1[i1], pts1[i1 + 1], pts2[i2], pts2[i2 + 1])
    d1, d2 = _unit(_sub(pts1[i1 + 1], pts1[i1])), _unit(_sub(pts2[i2 + 1], pts2[i2]))
    if p is None or d1 is None or d2 is None:
        return None
    _j, near = nearest_segment(pts2, point)
    side1 = _dot(_sub(_q1, p), d1) or 1.0
    side2 = _dot(_sub(q2, p), d2) or 1.0
    u1 = d1 if side1 > 0 else (-d1[0], -d1[1])
    u2 = d2 if side2 > 0 else (-d2[0], -d2[1])
    theta = math.acos(max(-1.0, min(1.0, _dot(u1, u2))))
    reach = _norm(_sub(near, p))
    if theta < 1e-4 or abs(math.pi - theta) < 1e-6 or reach < EPS:
        return None
    return reach * math.tan(theta / 2)


def corner_point(pts1, click1, pts2, click2):
    """Where the segments nearest the two clicks meet when extended, or None when they are parallel."""
    i1, _q1 = nearest_segment(pts1, click1)
    i2, _q2 = nearest_segment(pts2, click2)
    return line_intersection(pts1[i1], pts1[i1 + 1], pts2[i2], pts2[i2 + 1])


def _vertex_corner(pts, click1, click2):
    """(vertex index, u1, u2, lengths) of the corner between the two segments of one line nearest the clicks, or
    (None, message, None, None)."""
    i1, _q1 = nearest_segment(pts, click1)
    i2, _q2 = nearest_segment(pts, click2)
    if i1 == i2:
        return None, "pick two different segments of the line", None, None
    lo, hi = sorted((i1, i2))
    if hi != lo + 1:
        return None, "pick the two segments that meet at the corner", None, None
    v = pts[hi]
    u1, u2 = _unit(_sub(pts[lo], v)), _unit(_sub(pts[hi + 1], v))
    if u1 is None or u2 is None:
        return None, "the line has a zero-length segment there", None, None
    return hi, u1, u2, (_norm(_sub(pts[lo], v)), _norm(_sub(pts[hi + 1], v)))


def vertex_fillet(pts, click1, click2, radius):
    """
    Round the corner of ONE line: the two segments nearest the clicks must meet at a vertex, which is replaced by an
    arc of the radius tangent to both. Returns (new pts of the whole line, arc pts), or (None, message).
    """
    hi, u1, u2, lengths = _vertex_corner(pts, click1, click2)
    if hi is None:
        return None, u1
    theta = math.acos(max(-1.0, min(1.0, _dot(u1, u2))))
    if theta < 1e-4 or abs(math.pi - theta) < 1e-6:
        return None, "the two segments are straight on or fold back, there is no corner to round"
    reach = radius / math.tan(theta / 2)
    if reach > min(lengths) + EPS:
        return None, "the segments are too short for this radius (try a smaller radius)"
    v = pts[hi]
    bis = _unit((u1[0] + u2[0], u1[1] + u2[1]))
    centre = _plus(v, bis, radius / math.sin(theta / 2))
    t1, t2 = _plus(v, u1, reach), _plus(v, u2, reach)
    arc = _arc(t1, _plus(centre, bis, -radius), t2)
    return clean(pts[:hi] + arc + pts[hi + 1:]), arc


def vertex_radius_at(pts, click1, click2, point):
    """The radius of the arc that touches the second segment where the mouse is (None when there is no corner)."""
    hi, u1, u2, _lengths = _vertex_corner(pts, click1, click2)
    if hi is None:
        return None
    i2, _q = nearest_segment(pts, click2)
    seg = i2 if i2 in (hi - 1, hi) else hi
    near = closest_on_segment(pts[seg], pts[seg + 1], point)
    theta = math.acos(max(-1.0, min(1.0, _dot(u1, u2))))
    reach = _norm(_sub(near, pts[hi]))
    if theta < 1e-4 or abs(math.pi - theta) < 1e-6 or reach < EPS:
        return None
    return reach * math.tan(theta / 2)


# ---------------------------------------------------------------------------------------- polygon corners
def polygon_rings(geom):
    """[(part, ring, closed list of points)] of a polygon geometry: every ring, exterior first in each part."""
    if geom is None or geom.isNull() or geom.type() != Qgis.GeometryType.Polygon:
        return []
    parts = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    return [(i, j, [QgsPointXY(p) for p in ring]) for i, rings in enumerate(parts) for j, ring in enumerate(rings)
            if len(ring) > 3]


def nearest_ring_edge(geom, point):
    """(part, ring, segment index, closed ring points, distance) of the polygon edge nearest `point`, or None."""
    best = None
    for part, ring, pts in polygon_rings(geom):
        index, near = nearest_segment(pts, point)
        distance = _norm(_sub(near, point))
        if best is None or distance < best[4]:
            best = (part, ring, index, pts, distance)
    return best


def ring_corner(ring, click1, click2):
    """(vertex index, u1, u2, lengths) of the corner between the two edges of a closed ring nearest the clicks, or
    (None, message, None, None). u1 / u2 point from the corner along the previous / next edge."""
    m = len(ring) - 1
    i1, _q1 = nearest_segment(ring, click1)
    i2, _q2 = nearest_segment(ring, click2)
    if i1 == i2:
        return None, "pick two different edges of the polygon", None, None
    if (i2 - i1) % m == 1:
        v = (i1 + 1) % m
    elif (i1 - i2) % m == 1:
        v = (i2 + 1) % m
    else:
        return None, "pick the two edges that meet at the corner", None, None
    here, before, after = ring[v], ring[(v - 1) % m], ring[(v + 1) % m]
    u1, u2 = _unit(_sub(before, here)), _unit(_sub(after, here))
    if u1 is None or u2 is None:
        return None, "the polygon has a zero-length edge there", None, None
    return v, u1, u2, (_norm(_sub(before, here)), _norm(_sub(after, here)))


def ring_fillet(ring, click1, click2, radius):
    """
    Round one corner of a closed polygon ring: the corner vertex between the edges nearest the clicks is replaced by an
    arc of the radius tangent to both. Returns (new closed ring, arc points) or (None, message).
    """
    v, u1, u2, lengths = ring_corner(ring, click1, click2)
    if v is None:
        return None, u1
    theta = math.acos(max(-1.0, min(1.0, _dot(u1, u2))))
    if theta < 1e-4 or abs(math.pi - theta) < 1e-6:
        return None, "the two edges are straight on or fold back, there is no corner to round"
    reach = radius / math.tan(theta / 2)
    if reach > min(lengths) + EPS:
        return None, "the edges are too short for this radius (try a smaller radius)"
    here = ring[v]
    bis = _unit((u1[0] + u2[0], u1[1] + u2[1]))
    centre = _plus(here, bis, radius / math.sin(theta / 2))
    arc = _arc(_plus(here, u1, reach), _plus(centre, bis, -radius), _plus(here, u2, reach))
    open_ring = ring[:-1]
    out = clean(open_ring[:v] + arc + open_ring[v + 1:])
    if len(out) < 3:
        return None, "the polygon would be left with fewer than 3 corners"
    return out + [QgsPointXY(out[0])], arc


def ring_radius_at(ring, click1, click2, point):
    """The radius of the arc that touches the second edge where the mouse is (None when there is no corner)."""
    v, u1, u2, _lengths = ring_corner(ring, click1, click2)
    if v is None:
        return None
    edge, _q = nearest_segment(ring, click2)                                 # the edge the second click is on
    near = closest_on_segment(ring[edge], ring[edge + 1], point)
    theta = math.acos(max(-1.0, min(1.0, _dot(u1, u2))))
    reach = _norm(_sub(near, ring[v]))
    if theta < 1e-4 or abs(math.pi - theta) < 1e-6 or reach < EPS:
        return None
    return reach * math.tan(theta / 2)


def polygon_with_ring(geom, part, ring, pts):
    """The polygon geometry with one ring replaced by `pts` (single or multi-part like the original)."""
    parts = [[list(r) for r in rings] for rings in (geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()])]
    parts[part][ring] = list(pts)
    return QgsGeometry.fromMultiPolygonXY(parts) if geom.isMultipart() else QgsGeometry.fromPolygonXY(parts[0])


def _points_in(geom):
    """The point locations in an intersection result (line overlaps are ignored)."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    return [QgsPointXY(part.asPoint()) for part in geom.asGeometryCollection() if part.type() == Qgis.GeometryType.Point]


def split_line_at(pts, p, tol=1e-7):
    """The line cut in two at the point p of it, or None when p is at (or off) the line's ends."""
    line = QgsGeometry.fromPolylineXY(pts)
    length = line.length()
    here = QgsGeometry.fromPointXY(p)
    d = line.lineLocatePoint(here)
    if d <= tol or d >= length - tol or line.distance(here) > 1e-6 * max(1.0, length):
        return None
    first, second = single_line(_substring(line, 0, d)), single_line(_substring(line, d, length))
    if not first or not second:
        return None
    first, second = clean(first), clean(second)
    return (first, second) if len(first) >= 2 and len(second) >= 2 else None


def line_intersection_plan(pts1, click1, pts2, click2, variant=0):
    """
    What Line Intersection does to two lines. Where they cross, each line is cut in two there (four pieces);
    a line that only ends on the other is left alone while the other is cut; lines that do not meet are both
    extended to where they would (the clicks pick the segments). Returns (ops, point, number of intersections) with
    ops = [op for line 1, op for line 2], op = ("split", first, second) | ("extend", new points, the extension or
    None) | ("none",); or (None, message, 0). `variant` chooses among several crossings.
    """
    found = _points_in(QgsGeometry.fromPolylineXY(pts1).intersection(QgsGeometry.fromPolylineXY(pts2)))
    if found:
        found.sort(key=lambda q: (round(q.x(), 9), round(q.y(), 9)))
        point = found[variant % len(found)]
        ops = []
        for pts in (pts1, pts2):
            cut = split_line_at(pts, point)
            ops.append(("split",) + cut if cut else ("none",))
        if all(op[0] == "none" for op in ops):
            return None, "the lines already meet at their ends", 0
        return ops, point, len(found)
    new1, new2, _arc = corner(pts1, click1, pts2, click2, 0.0)
    if new1 is None:
        return None, new2, 0
    ops = []
    for old, new in ((pts1, new1), (pts2, new2)):
        longer = QgsGeometry.fromPolylineXY(new).length() > QgsGeometry.fromPolylineXY(old).length() + EPS
        extension = None
        if longer:
            moved_start = _norm(_sub(new[0], old[0])) > EPS
            extension = [new[0], old[0]] if moved_start else [old[-1], new[-1]]
        ops.append(("extend", new, extension))
    return ops, corner_point(pts1, click1, pts2, click2), 1


def _cut(pts, i, t, u, d):
    """The line with its segment i ended at t; the part on the side u (from t) stays. None if t falls outside."""
    if _dot(u, d) > 0:                                  # the end side stays
        if _dot(_sub(pts[i + 1], t), d) < -EPS:
            return None
        out = [t] + pts[i + 1:]
    else:                                               # the start side stays
        if _dot(_sub(t, pts[i]), d) < -EPS:
            return None
        out = pts[:i + 1] + [t]
    out = clean(out)
    return out if len(out) >= 2 else None


def _arc(start, mid, end):
    circular = QgsCircularString(QgsPoint(start), QgsPoint(mid), QgsPoint(end))
    return single_line(QgsGeometry(circular.curveToLine())) or [start, mid, end]


# ---------------------------------------------------------------------------------------- extend / trim
def _substring(line, start, end):
    """The part of a line geometry between two distances along it."""
    return QgsGeometry(line.constGet().curveSubstring(start, end))


def _points_of(geom):
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    return [QgsPointXY(v) for v in geom.vertices()]


def _distances_along(line, boundary, length, tol=0.0):
    """
    Sorted distances along `line` at which it meets `boundary` (the ends themselves do not count). Within `tol`
    a feature that ends next to the line counts as touching it: what looks like a junction on screen is one.
    """
    found = set()
    for p in _points_of(line.intersection(boundary)):
        d = line.lineLocatePoint(QgsGeometry.fromPointXY(p))
        if EPS < d < length - EPS:
            found.add(round(d, 9))
    if tol > 0:
        for part in boundary.asGeometryCollection():
            vertices = _points_of(part)
            for p in (vertices[0], vertices[-1]) if vertices else ():
                here = QgsGeometry.fromPointXY(p)
                if line.distance(here) <= tol:
                    d = line.lineLocatePoint(here)
                    if EPS < d < length - EPS and not any(abs(d - known) <= tol for known in found):
                        found.add(round(d, 9))
    return sorted(found)


def _extend_end(pts, line, boundary, at_start):
    end, neighbour = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
    if QgsGeometry.fromPointXY(end).distance(boundary) < 1e-7:
        return None, "the end of the line is already on a feature", None
    direction = _unit(_sub(end, neighbour))
    if direction is None:
        return None, "the line ends with a zero-length segment", None
    box = boundary.boundingBox()
    box.combineExtentWith(line.boundingBox())
    reach = 2 * math.hypot(box.width(), box.height()) + line.length() + 1.0
    ray = QgsGeometry.fromPolylineXY([end, _plus(end, direction, reach)])
    hits = [p for p in _points_of(ray.intersection(boundary)) if _norm(_sub(p, end)) > EPS]
    if not hits:
        return None, "nothing ahead of the line end to extend to", None
    target = min(hits, key=lambda p: _norm(_sub(p, end)))
    out = [target] + pts[1:] if at_start else pts[:-1] + [target]          # the end vertex itself moves
    return clean(out), "extended", [end, target]


def _trim_at(pts, line, boundary, c, length, tol=0.0):
    cuts = _distances_along(line, boundary, length, tol)
    before = [d for d in cuts if d < c]
    after = [d for d in cuts if d > c]
    if not cuts:
        return None, "nothing to trim the line back to", None
    if before and after:
        return None, "the clicked part lies between two features - click a part that ends at the line's end", None
    if after:                                              # remove the start, up to the next feature
        keep, gone = _substring(line, after[0], length), _substring(line, 0, after[0])
    else:                                                  # remove the end, from the last feature
        keep, gone = _substring(line, 0, before[-1]), _substring(line, before[-1], length)
    out, removed = single_line(keep), single_line(gone)
    if not out or len(clean(out)) < 2:
        return None, "nothing would be left of the line", None
    return clean(out), "trimmed", removed


def extend_trim_plan(pts, boundary, click, mode="both", tol=0.0):
    """
    What Extend or Trim does to the line `pts` for a click: mode "trim" removes the clicked part when it ends the
    line (cut back to the nearest feature of `boundary`), "extend" extends the end nearer the click to the nearest
    feature ahead of it, "both" trims when the clicked part is a stub beyond a feature and extends otherwise.
    Returns (new pts, "trimmed" | "extended", the part removed / the extension) or (None, message, None).
    """
    line = QgsGeometry.fromPolylineXY(pts)
    length = line.length()
    if length < EPS:
        return None, "the line has no length", None
    c = line.lineLocatePoint(QgsGeometry.fromPointXY(click))
    if mode == "extend":
        return _extend_end(pts, line, boundary, c < length / 2)
    cuts = _distances_along(line, boundary, length, tol)
    stub = bool([d for d in cuts if d < c]) != bool([d for d in cuts if d > c])
    if mode == "trim" or stub:
        return _trim_at(pts, line, boundary, c, length, tol)
    return _extend_end(pts, line, boundary, c < length / 2)


# ---------------------------------------------------------------------------------------- length
def change_length(pts, near, new_length):
    """
    The line with the end nearer `near` moved so the whole line is new_length long: cut back when shorter,
    continued straight on when longer. Returns (new pts, old length), or (None, message).
    """
    if new_length <= EPS:
        return None, "the length must be more than zero"
    line = QgsGeometry.fromPolylineXY(pts)
    length = line.length()
    at_start = _norm(_sub(near, pts[0])) < _norm(_sub(near, pts[-1]))
    if abs(new_length - length) < EPS:
        return None, "that is already the length"
    if new_length < length:
        sub = _substring(line, length - new_length, length) if at_start else _substring(line, 0, new_length)
        return clean(single_line(sub) or []), length
    end, neighbour = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
    direction = _unit(_sub(end, neighbour))
    if direction is None:
        return None, "the line ends with a zero-length segment"
    target = _plus(end, direction, new_length - length)
    return (([target] + pts[1:]) if at_start else (pts[:-1] + [target])), length          # the end vertex moves


def scale_line(pts, new_length, preserve_first):
    """
    The whole line scaled about its first (or last) vertex until it is new_length long, so every segment changes by
    the same factor. Returns (new pts, old length), or (None, message).
    """
    if new_length <= EPS:
        return None, "the length must be more than zero"
    length = QgsGeometry.fromPolylineXY(pts).length()
    if length < EPS:
        return None, "the line has no length"
    if abs(new_length - length) < EPS:
        return None, "that is already the length"
    factor = new_length / length
    anchor = pts[0] if preserve_first else pts[-1]
    return [QgsPointXY(anchor.x() + (p.x() - anchor.x()) * factor, anchor.y() + (p.y() - anchor.y()) * factor)
            for p in pts], length


# ---------------------------------------------------------------------------------------- reverse
def reversed_geometry(geom, near=None):
    """
    The geometry with every line part running the other way (None if it is not a line). With `near` (a point), a
    multi-part line has only the part nearest that point reversed.
    """
    if geom is None or geom.isNull() or geom.type() != Qgis.GeometryType.Line:
        return None
    if geom.isMultipart():
        parts = [list(p) for p in geom.asMultiPolyline()]
        if near is not None and len(parts) > 1:
            here = QgsGeometry.fromPointXY(near)
            nearest = min(range(len(parts)), key=lambda i: QgsGeometry.fromPolylineXY(parts[i]).distance(here))
            parts[nearest] = parts[nearest][::-1]
        else:
            parts = [p[::-1] for p in parts]
        return QgsGeometry.fromMultiPolylineXY(parts)
    return QgsGeometry.fromPolylineXY(list(reversed(geom.asPolyline())))


def direction_arrows(geom, spacing, size, near=None):
    """
    Small triangles along the line pointing the way it runs: [[3 points]], one about every `spacing` along each
    part (at least one per part), `size` long. With `near`, only the multi-part line's part nearest that point.
    """
    if geom is None or geom.isNull() or geom.type() != Qgis.GeometryType.Line:
        return []
    parts = [list(p) for p in (geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()])]
    if near is not None and len(parts) > 1:
        here = QgsGeometry.fromPointXY(near)
        parts = [min(parts, key=lambda p: QgsGeometry.fromPolylineXY(p).distance(here))]
    arrows = []
    for pts in parts:
        line = QgsGeometry.fromPolylineXY(pts)
        length = line.length()
        if length < EPS:
            continue
        count = max(1, int(length // spacing))
        for i in range(count):
            d = (i + 0.5) * length / count
            ahead = _unit(_sub(line.interpolate(min(length, d + size * 0.5)).asPoint(),
                               line.interpolate(max(0.0, d - size * 0.5)).asPoint()))
            if ahead is None:
                continue
            centre = line.interpolate(d).asPoint()
            tip = _plus(centre, ahead, size * 0.6)
            base = _plus(centre, ahead, -size * 0.4)
            side = (-ahead[1] * size * 0.3, ahead[0] * size * 0.3)
            arrows.append([tip, QgsPointXY(base.x() + side[0], base.y() + side[1]),
                           QgsPointXY(base.x() - side[0], base.y() - side[1])])
    return arrows


# ---------------------------------------------------------------------------------------- generalize
def _catmull_rom(p0, p1, p2, p3, t):
    t2, t3 = t * t, t * t * t
    return QgsPointXY(*(0.5 * ((2 * b) + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3)
                        for a, b, c, d in ((p0.x(), p1.x(), p2.x(), p3.x()), (p0.y(), p1.y(), p2.y(), p3.y()))))


def _span(p0, p1, p2, p3, offset, out):
    """Points of the curve from p1 to p2 (p2 not included), split until no chord is further than `offset` off."""
    tol = offset if offset > 0 else 0.005 * max(_norm(_sub(p2, p1)), EPS)

    def walk(t0, a, t1, b, depth):
        tm = (t0 + t1) / 2
        m = _catmull_rom(p0, p1, p2, p3, tm)
        if depth < 8 and _norm(_sub(m, closest_on_segment(a, b, m))) > tol:
            walk(t0, a, tm, m, depth + 1)
            out.append(m)
            walk(tm, m, t1, b, depth + 1)
        elif depth < 2:                                 # at least a few points, so a straight run still bows nicely
            out.append(m)
    out.append(p1)
    walk(0.0, p1, 1.0, p2, 0)


def smooth_points(pts, closed, offset=0.0):
    """A smooth curve through the vertices (Catmull-Rom), as a denser point list; `offset` is how far a chord of it may
    lie from the curve (0 picks a fine default)."""
    pts = clean(pts)
    if closed and len(pts) > 1 and _norm(_sub(pts[0], pts[-1])) < EPS:
        pts = pts[:-1]
    if len(pts) < 3:
        return list(pts) + ([pts[0]] if closed and pts else [])
    n = len(pts)
    out = []
    for i in range(n if closed else n - 1):
        p1, p2 = pts[i], pts[(i + 1) % n]
        p0 = pts[(i - 1) % n] if (closed or i > 0) else p1
        p3 = pts[(i + 2) % n] if (closed or i + 2 < n) else p2
        _span(p0, p1, p2, p3, offset, out)
    out.append(out[0] if closed else pts[-1])
    return out


def smooth_geometry(geom, offset=0.0):
    """Lines and polygons as smooth curves through their vertices, or None for anything else."""
    kind = geom.type()
    if kind == Qgis.GeometryType.Line:
        parts = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
        lines = [smooth_points(list(p), False, offset) for p in parts if len(p) > 1]
        if not lines:
            return None
        return QgsGeometry.fromMultiPolylineXY(lines) if geom.isMultipart() else QgsGeometry.fromPolylineXY(lines[0])
    if kind == Qgis.GeometryType.Polygon:
        polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
        shaped = [[smooth_points(list(ring), True, offset) for ring in poly] for poly in polys if poly]
        if not shaped:
            return None
        return QgsGeometry.fromMultiPolygonXY(shaped) if geom.isMultipart() else QgsGeometry.fromPolygonXY(shaped[0])
    return None


def densify_geometry(geom, how, value):
    """
    More vertices, same shape: `how` "distance" puts one at most `value` apart along every segment; "offset" and
    "angle" (value in degrees) only change curved segments, which become straight pieces that stay within that
    offset / turn by that angle. Returns None when there is nothing to do.
    """
    if geom.type() == Qgis.GeometryType.Point or value <= 0:
        return None
    if how == "distance":
        out = geom.densifyByDistance(value)
    else:
        out = QgsGeometry(geom)
        tolerance_type = (QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference if how == "offset"
                          else QgsAbstractGeometry.SegmentationToleranceType.MaximumAngle)
        out.convertToStraightSegment(value if how == "offset" else math.radians(value), tolerance_type)
    if out is None or out.isNull() or out.isEmpty():
        return None
    return out

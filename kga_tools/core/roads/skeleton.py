# -*- coding: utf-8 -*-
"""From a road mask to a road graph: thinning, tracing, pruning, bridging.

Everything works in pixel space of one grid; the caller turns pixels into map
coordinates. numpy does the bulk work; scipy.ndimage labels and scipy.spatial
finds near neighbours (both ship with QGIS on Windows, macOS and OSGeo4W).

The steps:

1. **Thin** the mask to a one-pixel skeleton (Zhang-Suen, run as a lookup table
   over the pixels still on), then take out the leftover "staircase" pixels so
   every line pixel has exactly two neighbours.
2. **Trace** it: pixels with one neighbour are ends, pixels with three or more
   are junctions (a clump of junction pixels is one junction), and the runs in
   between are edges.
3. **Prune** short spurs - the whiskers a thinned blob always grows.
4. **Bridge** gaps: an end that points at another road a short way ahead is
   joined to it. Trees over a road, a shadow or a bridge deck break the mask;
   bridging mends most of those breaks, and every bridge is marked.
5. **Drop** pieces whose whole network is shorter than a minimum length.
"""

import math

import numpy as np

# Neighbour offsets in Zhang-Suen order: P2 (north) clockwise to P9 (north-west).
_OFFSETS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1),
            (-1, -1))


def _tables():
    """Deletion tables for the two Zhang-Suen sub-iterations, and the
    'simple point' table used to remove staircase pixels."""
    first = np.zeros(256, dtype=bool)
    second = np.zeros(256, dtype=bool)
    simple = np.zeros(256, dtype=bool)
    for code in range(256):
        p = [(code >> i) & 1 for i in range(8)]     # p[0] = P2 ... p[7] = P9
        count = sum(p)
        crossings = sum(1 for i in range(8) if p[i] == 0 and p[(i + 1) % 8])
        p2, p3, p4, p5, p6, p7, p8, p9 = p
        base = 2 <= count <= 6 and crossings == 1
        first[code] = base and not (p2 and p4 and p6) and not (p4 and p6 and p8)
        second[code] = base and not (p2 and p4 and p8) and not (p2 and p6 and p8)
        simple[code] = count >= 2 and _components(p) == 1
    return first, second, simple


def _components(p):
    """8-connected groups among the on neighbours of a pixel."""
    on = [i for i in range(8) if p[i]]
    if not on:
        return 0

    # Ring neighbours touch; so do two orthogonal neighbours (even indices)
    # two steps apart, across the corner between them.
    def touching(a, b):
        gap = (b - a) % 8
        if gap in (1, 7):
            return True
        return gap in (2, 6) and a % 2 == 0 and b % 2 == 0
    seen = set()
    groups = 0
    for start in on:
        if start in seen:
            continue
        groups += 1
        stack = [start]
        seen.add(start)
        while stack:
            a = stack.pop()
            for b in on:
                if b not in seen and touching(a, b):
                    seen.add(b)
                    stack.append(b)
    return groups


FIRST, SECOND, SIMPLE = _tables()


def _codes(flat, index, width):
    """Neighbour code of each pixel in `index` of a padded flat array."""
    code = np.zeros(len(index), dtype=np.int32)
    for bit, (dr, dc) in enumerate(_OFFSETS):
        code |= flat[index + dr * width + dc].astype(np.int32) << bit
    return code


def thin(mask, stop=None):
    """One-pixel-wide skeleton of a boolean mask (8-connected)."""
    padded = np.pad(mask.astype(np.uint8), 1)
    width = padded.shape[1]
    flat = padded.ravel()
    on = np.flatnonzero(flat)
    while True:
        if stop is not None and stop():
            return None
        changed = False
        for table in (FIRST, SECOND):
            codes = _codes(flat, on, width)
            delete = table[codes]
            if delete.any():
                flat[on[delete]] = 0
                on = on[~delete]
                changed = True
        if not changed:
            break
    _remove_staircases(flat, on, width)
    return padded[1:-1, 1:-1].astype(bool)


def _remove_staircases(flat, on, width):
    """Drop pixels that join two neighbours already touching each other.

    Done one pixel at a time, re-reading the neighbours, because removing two
    such pixels together can cut a line.
    """
    codes = _codes(flat, on, width)
    count = np.zeros(len(on), dtype=np.int32)
    for bit in range(8):
        count += (codes >> bit) & 1
    # Only pixels with an L of two orthogonal neighbours can be redundant.
    north, east, south, west = 1, 4, 16, 64
    corner = (((codes & north) > 0) & ((codes & east) > 0)) | \
        (((codes & east) > 0) & ((codes & south) > 0)) | \
        (((codes & south) > 0) & ((codes & west) > 0)) | \
        (((codes & west) > 0) & ((codes & north) > 0))
    for pixel in on[corner & (count >= 2)].tolist():
        code = 0
        for bit, (dr, dc) in enumerate(_OFFSETS):
            code |= int(flat[pixel + dr * width + dc]) << bit
        if SIMPLE[code] and _is_l_corner(code):
            flat[pixel] = 0


def _is_l_corner(code):
    """True when the pixel's neighbours are exactly what an L step leaves."""
    count = bin(code).count('1')
    return count == 2 and code in (1 | 4, 4 | 16, 16 | 64, 64 | 1)


def neighbour_count(skeleton):
    padded = np.pad(skeleton.astype(np.uint8), 1)
    total = np.zeros(skeleton.shape, dtype=np.uint8)
    rows, cols = skeleton.shape
    for dr, dc in _OFFSETS:
        total += padded[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]
    return np.where(skeleton, total, 0)


# ------------------------------------------------------------------ the graph

class Edge(object):
    __slots__ = ('ident', 'a', 'b', 'pixels', 'bridge', 'alive')

    def __init__(self, ident, a, b, pixels, bridge=False):
        self.ident = ident
        self.a = a
        self.b = b
        #: (n, 2) float array of (row, col), from node a to node b.
        self.pixels = pixels
        self.bridge = bridge
        self.alive = True

    def length(self):
        if len(self.pixels) < 2:
            return 0.0
        step = np.diff(self.pixels, axis=0)
        return float(np.sum(np.hypot(step[:, 0], step[:, 1])))


class Graph(object):
    """Nodes (row, col) and edges between them, in pixels."""

    def __init__(self):
        self.nodes = {}                 # id -> (row, col)
        self.edges = []
        self._next_node = 0

    def add_node(self, row, col):
        ident = self._next_node
        self._next_node += 1
        self.nodes[ident] = (float(row), float(col))
        return ident

    def add_edge(self, a, b, pixels, bridge=False):
        edge = Edge(len(self.edges), a, b, np.asarray(pixels, dtype=np.float64),
                    bridge)
        self.edges.append(edge)
        return edge

    def alive(self):
        return [e for e in self.edges if e.alive]

    def degrees(self):
        degree = {}
        for edge in self.alive():
            degree[edge.a] = degree.get(edge.a, 0) + 1
            degree[edge.b] = degree.get(edge.b, 0) + 1
        return degree

    def incident(self):
        found = {}
        for edge in self.alive():
            found.setdefault(edge.a, []).append(edge)
            found.setdefault(edge.b, []).append(edge)
        return found


def trace(skeleton, stop=None):
    """The skeleton as a Graph. Junction clumps become one node each."""
    from scipy import ndimage

    rows, cols = skeleton.shape
    degree = neighbour_count(skeleton)
    junction = skeleton & (degree >= 3)
    labels, count = ndimage.label(junction, structure=np.ones((3, 3)))
    graph = Graph()
    node_of = -np.ones(skeleton.shape, dtype=np.int64)
    if count:
        centres = ndimage.center_of_mass(junction, labels,
                                         range(1, count + 1))
        ids = np.array([graph.add_node(r, c) for r, c in centres])
        node_of[junction] = ids[labels[junction] - 1]
    for r, c in zip(*np.nonzero(skeleton & (degree == 1))):
        node_of[r, c] = graph.add_node(r, c)

    visited = np.zeros(skeleton.shape, dtype=bool)
    seen_pairs = set()

    def neighbours(r, c):
        for dr, dc in _OFFSETS:
            rr, cc = r + dr, c + dc
            if 0 <= rr < rows and 0 <= cc < cols and skeleton[rr, cc]:
                yield rr, cc

    def walk(start_node, r0, c0, r, c):
        path = [(r0, c0)]
        prev = (r0, c0)
        while True:
            node = node_of[r, c]
            if node >= 0:
                path.append((r, c))
                return node, path
            if visited[r, c]:
                return None, path
            visited[r, c] = True
            path.append((r, c))
            nxt = None
            for rr, cc in neighbours(r, c):
                if (rr, cc) == prev:
                    continue
                if node_of[rr, cc] == start_node and len(path) == 2:
                    continue                 # back into the clump we left
                nxt = (rr, cc)
                if node_of[rr, cc] >= 0:
                    break                     # prefer stepping onto a node
            if nxt is None:
                return None, path
            prev = (r, c)
            r, c = nxt

    node_pixels = np.argwhere(node_of >= 0)
    for index, (r, c) in enumerate(node_pixels.tolist()):
        if stop is not None and index % 2000 == 0 and stop():
            return None
        start = int(node_of[r, c])
        for rr, cc in neighbours(r, c):
            other = node_of[rr, cc]
            if other == start:
                continue
            if other >= 0:
                key = (min(start, other), max(start, other), min((r, c), (rr, cc)))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                graph.add_edge(start, int(other), [(r, c), (rr, cc)])
                continue
            if visited[rr, cc]:
                continue
            end, path = walk(start, r, c, rr, cc)
            if end is None:
                continue
            graph.add_edge(start, int(end), path)

    # Closed loops with no end and no junction (a ring road on its own).
    left = skeleton & ~visited & (node_of < 0)
    for r, c in zip(*np.nonzero(left)):
        if visited[r, c]:
            continue
        node = graph.add_node(r, c)
        node_of[r, c] = node
        visited[r, c] = True
        for rr, cc in neighbours(r, c):
            if not visited[rr, cc] and node_of[rr, cc] < 0:
                end, path = walk(node, r, c, rr, cc)
                if end is not None:
                    graph.add_edge(node, int(end), path)
                break
    _snap_ends_to_nodes(graph)
    return graph


def _snap_ends_to_nodes(graph):
    """Start and end every edge exactly on its nodes (junction centres)."""
    for edge in graph.edges:
        pixels = edge.pixels
        pixels[0] = graph.nodes[edge.a]
        pixels[-1] = graph.nodes[edge.b]


def prune(graph, spur_px, rounds=3):
    """Remove dead-end edges shorter than `spur_px`, then merge through nodes
    left with two edges."""
    for _round in range(rounds):
        degree = graph.degrees()
        removed = False
        for edge in graph.alive():
            if edge.bridge:
                continue
            da, db = degree.get(edge.a, 0), degree.get(edge.b, 0)
            dead_end = (da == 1 and db >= 3) or (db == 1 and da >= 3)
            if dead_end and edge.length() < spur_px:
                edge.alive = False
                removed = True
        merge_through(graph)
        if not removed:
            break


def merge_through(graph):
    """Join the two edges of every node that has exactly two."""
    while True:
        incident = graph.incident()
        merged_any = False
        for node, edges in incident.items():
            # A joined gap keeps its own line, so GAP_JOIN marks just the gap.
            if len(edges) != 2 or edges[0] is edges[1] \
                    or edges[0].bridge != edges[1].bridge:
                continue
            first, second = edges
            if not (first.alive and second.alive):
                continue                    # already used this pass
            a_path = first.pixels if first.b == node else first.pixels[::-1]
            b_path = second.pixels if second.a == node else second.pixels[::-1]
            start = first.a if first.b == node else first.b
            end = second.b if second.a == node else second.a
            first.alive = False
            second.alive = False
            merged = graph.add_edge(start, end,
                                    np.vstack((a_path, b_path[1:])),
                                    first.bridge)
            if start == end and merged.length() == 0:
                merged.alive = False
            merged_any = True
        if not merged_any:
            return


def _end_direction(edge, node, reach_px):
    """Unit vector pointing out of the road at `node`, from `reach_px` back."""
    pixels = edge.pixels if edge.b == node else edge.pixels[::-1]
    tip = pixels[-1]
    along = np.concatenate(([0.0], np.cumsum(np.hypot(
        *np.diff(pixels[::-1], axis=0).T))))
    index = int(np.searchsorted(along, reach_px))
    back = pixels[::-1][min(index, len(pixels) - 1)]
    vector = tip - back
    norm = math.hypot(vector[0], vector[1])
    if norm == 0:
        return None
    return vector / norm


def bridge(graph, max_gap_px, max_angle_deg=35.0, reach_px=None, stop=None):
    """Join dead ends to the road they point at. Returns the bridges made."""
    from scipy.spatial import cKDTree

    edges = graph.alive()
    if not edges:
        return 0
    reach_px = reach_px or max(3.0, max_gap_px / 3.0)
    points = []
    owner = []
    for edge in edges:
        for point in edge.pixels:
            points.append(point)
            owner.append(edge.ident)
    points = np.asarray(points)
    owner = np.asarray(owner)
    tree = cKDTree(points)
    degree = graph.degrees()
    incident = graph.incident()
    cos_limit = math.cos(math.radians(max_angle_deg))
    made = 0
    joined = set()
    for node, deg in list(degree.items()):
        if deg != 1 or node in joined:
            continue
        if stop is not None and stop():
            break
        edge = incident[node][0]
        direction = _end_direction(edge, node, reach_px)
        if direction is None:
            continue
        tip = np.asarray(graph.nodes[node])
        best = None
        for index in tree.query_ball_point(tip, max_gap_px):
            if owner[index] == edge.ident:
                continue
            vector = points[index] - tip
            distance = math.hypot(vector[0], vector[1])
            if distance < 1.0:
                continue
            cosine = (vector[0] * direction[0] + vector[1] * direction[1]) \
                / distance
            if cosine < cos_limit:
                continue
            # Favour short, straight-ahead joins.
            score = distance * (2.0 - cosine)
            if best is None or score < best[0]:
                best = (score, index)
        if best is None:
            continue
        target = points[best[1]]
        other = graph.edges[int(owner[best[1]])]
        end_node = None
        for candidate in (other.a, other.b):
            if np.allclose(graph.nodes[candidate], target):
                end_node = candidate
        if end_node is None:
            end_node = _split(graph, other, target)
        if end_node is None:
            continue
        graph.add_edge(node, end_node, [tuple(tip), tuple(target)], True)
        joined.add(node)
        if degree.get(end_node) == 1:
            joined.add(end_node)
        made += 1
    return made


def _split(graph, edge, point):
    """Cut `edge` at its vertex `point`; return the new node, or None."""
    pixels = edge.pixels
    matches = np.nonzero(np.all(np.isclose(pixels, point), axis=1))[0]
    if not len(matches):
        return None
    index = int(matches[0])
    if index == 0:
        return edge.a
    if index == len(pixels) - 1:
        return edge.b
    if not edge.alive:
        return None
    node = graph.add_node(*point)
    edge.alive = False
    graph.add_edge(edge.a, node, pixels[:index + 1], edge.bridge)
    graph.add_edge(node, edge.b, pixels[index:], edge.bridge)
    return node


def drop_small_networks(graph, min_length_px):
    """Remove connected pieces whose total length is under `min_length_px`."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for edge in graph.alive():
        ra, rb = find(edge.a), find(edge.b)
        if ra != rb:
            parent[ra] = rb
    total = {}
    for edge in graph.alive():
        root = find(edge.a)
        total[root] = total.get(root, 0.0) + edge.length()
    dropped = 0
    for edge in graph.alive():
        if total[find(edge.a)] < min_length_px:
            edge.alive = False
            dropped += 1
    return dropped


def network_ids(graph):
    """Edge ident -> network number (connected piece), 1-based."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for edge in graph.alive():
        ra, rb = find(edge.a), find(edge.b)
        if ra != rb:
            parent[ra] = rb
    numbers = {}
    found = {}
    for edge in graph.alive():
        root = find(edge.a)
        if root not in numbers:
            numbers[root] = len(numbers) + 1
        found[edge.ident] = numbers[root]
    return found

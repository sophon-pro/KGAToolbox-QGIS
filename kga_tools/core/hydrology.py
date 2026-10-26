# -*- coding: utf-8 -*-
"""Stream extraction from a DEM: fill, route, accumulate, trace.

The numerical core of *Generate River or Stream for ROI*. It works on plain
numpy arrays and knows nothing about QGIS, so it can be tested on a synthetic
terrain.

The method is the standard one for a DEM-derived drainage network:

1. **Fill depressions** with Priority-Flood (Barnes, Lehman and Mulla 2014).
   Every cell is reached from the edge of the grid (or from NoData) in order of
   rising elevation; a cell lower than the cell that reached it is a pit and is
   raised to that level. The order cells are reached in is kept, because it is
   a ready-made topological order: a cell is always reached after the cell it
   drains into.
2. **Route** each cell by D8 steepest descent on the filled surface. Cells on a
   flat (a filled pit, a lake, a plain) have no steepest neighbour, so they
   follow the flood tree instead, which leads them to the spill point.
3. **Accumulate** the contributing area down those links, in reverse flood
   order, so each cell is visited once.
4. **Threshold** the accumulation: a cell is stream when the ground upstream of
   it is at least as large as the chosen area.
5. **Trace** the stream cells into segments between junctions and number them
   (Strahler).

Everything is held in a grid padded by one cell on every side, so a neighbour
is always an index offset and never needs a bounds check. ``pad`` and ``unpad``
move between that and the plain 2-D array.
"""

import heapq
import math
from array import array
from collections import deque

import numpy as np

#: Cell-by-cell progress is reported this often, in cells.
REPORT_EVERY = 200000

#: (row step, column step) of the eight neighbours, east first, clockwise.
STEPS = ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))


class Stopped(Exception):
    """Raised inside a long loop when the caller asked it to stop."""


class Grid(object):
    """The padded grid every function here shares.

    `dx` and `dy` are the cell width and height in metres *per row* (they
    differ from row to row in a geographic or Mercator grid), so that slope and
    area are right whatever the CRS.
    """

    def __init__(self, rows, cols, dx, dy):
        self.rows = rows
        self.cols = cols
        self.width = cols + 2
        self.size = (rows + 2) * (cols + 2)
        self.dx = np.asarray(dx, dtype=np.float64)
        self.dy = np.asarray(dy, dtype=np.float64)
        w = self.width
        #: Index offset of each of the eight neighbours, in STEPS order.
        self.offsets = tuple(r * w + c for r, c in STEPS)

    def pad(self, array2d, fill):
        out = np.full((self.rows + 2, self.cols + 2), fill,
                      dtype=array2d.dtype)
        out[1:-1, 1:-1] = array2d
        return out.ravel()

    def unpad(self, flat):
        return flat.reshape(self.rows + 2, self.cols + 2)[1:-1, 1:-1]

    def row_col(self, index):
        """Row and column (unpadded) of padded flat indices."""
        index = np.asarray(index)
        return index // self.width - 1, index % self.width - 1

    def row_values(self, per_row, fill=0.0):
        """A per-row vector broadcast onto the padded grid, flattened."""
        padded = np.empty(self.rows + 2, dtype=np.float64)
        padded[1:-1] = per_row
        padded[0] = per_row[0]
        padded[-1] = per_row[-1]
        return np.repeat(padded[:, None], self.width, axis=1).ravel()

    def cell_area_m2(self):
        return self.dx * self.dy

    def step_length(self, row, step):
        """Metres from a cell in `row` to the neighbour `step` away."""
        dr, dc = step
        return math.hypot(dc * self.dx[row], dr * self.dy[row])


class Routing(object):
    """What :func:`route` finds: where every cell sends its water."""

    def __init__(self, grid, raw, filled, down, order, valid):
        self.grid = grid
        #: Padded, flattened elevation as read, and after filling depressions.
        self.raw = raw
        self.filled = filled
        #: Padded index of the cell each cell drains into; -1 at an outlet.
        self.down = down
        #: Padded indices, every valid cell, in the order the flood reached
        #: them: downstream cells before the cells that drain into them.
        self.order = order
        #: Padded boolean mask of cells that hold data.
        self.valid = valid


# ------------------------------------------------------------------ filling

def _outlet_seeds(valid_padded, width):
    """Valid cells on the grid edge or touching NoData: where water can leave."""
    valid = valid_padded.reshape(-1, width)
    near_void = np.zeros_like(valid)
    void = ~valid
    # A pad cell is void too, so the grid edge is covered by the same test.
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr or dc:
                near_void |= np.roll(np.roll(void, dr, axis=0), dc, axis=1)
    return np.flatnonzero((valid & near_void).ravel())


def route(grid, elevation, valid, progress=None, stop=None):
    """Fill the depressions of `elevation` and find every cell's way downhill.

    `elevation` is the plain 2-D array, `valid` its data mask. `progress` takes
    a fraction 0..1; `stop()` returns True to give up (raises :class:`Stopped`).
    """
    w = grid.width
    z = grid.pad(np.where(valid, elevation, 0).astype(np.float32), 0.0)
    ok = grid.pad(valid, False)
    n = grid.size

    # One unique rank per cell, in elevation order. The heap then holds plain
    # integers - far cheaper to compare than (elevation, index) pairs - and
    # equal elevations are broken the same way every time.
    members = np.flatnonzero(ok)
    by_rank = members[np.argsort(z[members], kind='stable')].astype(np.int32)
    rank = np.zeros(n, dtype=np.int32)
    rank[by_rank] = np.arange(len(by_rank), dtype=np.int32)

    closed = bytearray(n)
    # Anything that is not data counts as already visited.
    closed_view = np.frombuffer(closed, dtype=np.uint8)
    closed_view[~ok] = 1

    flat = z.copy()                         # becomes the filled surface
    parent = np.full(n, -1, dtype=np.int32)
    zv = memoryview(flat)
    pv = memoryview(parent)
    rv = memoryview(rank)
    bv = memoryview(by_rank)
    order = array('i')
    append = order.append
    offsets = grid.offsets

    heap = []
    for seed in _outlet_seeds(ok, w).tolist():
        closed[seed] = 1
        heap.append(rv[seed])
    heapq.heapify(heap)

    pit = deque()
    total = max(1, len(members))
    next_report = REPORT_EVERY
    pop = heapq.heappop
    push = heapq.heappush

    while heap or pit:
        if pit:
            cell = pit.popleft()
        else:
            cell = bv[pop(heap)]
        append(cell)
        level = zv[cell]
        for off in offsets:
            nb = cell + off
            if closed[nb]:
                continue
            closed[nb] = 1
            pv[nb] = cell
            if zv[nb] <= level:
                # A pit or a flat: it holds water up to the spill level, and
                # is visited at that level, before anything higher.
                zv[nb] = level
                pit.append(nb)
            else:
                push(heap, rv[nb])
        if len(order) >= next_report:
            next_report += REPORT_EVERY
            if stop is not None and stop():
                raise Stopped()
            if progress is not None:
                progress(min(1.0, len(order) / total))

    order = np.frombuffer(order, dtype=np.int32).copy()
    filled = flat
    if progress is not None:
        progress(1.0)
    down = _steepest(grid, filled, ok, parent)
    return Routing(grid, z, filled, down, order, ok)


def _steepest(grid, filled, ok, tree_parent):
    """D8 steepest descent on the filled surface; the flood tree on flats.

    Done in bands of cells, because eight neighbour comparisons over a large
    grid at once would hold a dozen full-size temporaries in memory.
    """
    n = grid.size
    w = grid.width
    target = np.full(n, -1, dtype=np.int32)
    low = np.where(ok, filled, np.float32(np.inf)).astype(np.float32)
    dx_rows = np.concatenate(([grid.dx[0]], grid.dx, [grid.dx[-1]]))
    dy_rows = np.concatenate(([grid.dy[0]], grid.dy, [grid.dy[-1]]))
    band = 1 << 21
    for start in range(w + 1, n - w - 1, band):
        end = min(n - w - 1, start + band)
        cells = np.arange(start, end, dtype=np.int64)
        rows = cells // w
        dx = dx_rows[rows]
        dy = dy_rows[rows]
        here = filled[start:end]
        best = np.zeros(end - start, dtype=np.float32)
        pick = np.zeros(end - start, dtype=np.int32)   # 0: no lower neighbour
        for (dr, dc), off in zip(STEPS, grid.offsets):
            distance = np.sqrt((dc * dx) ** 2 + (dr * dy) ** 2)
            slope = ((here - low[start + off:end + off]) / distance
                     ).astype(np.float32)
            better = slope > best
            best[better] = slope[better]
            pick[better] = off
        chosen = pick != 0
        local = np.where(chosen, cells + pick, -1).astype(np.int32)
        valid = ok[start:end]
        local[~valid] = -1
        flat = valid & ~chosen
        local[flat] = tree_parent[start:end][flat]
        target[start:end] = local
    return target


# ------------------------------------------------------------ accumulation

def accumulate(routing, progress=None, stop=None):
    """Contributing area, in m2, of every cell (itself included).

    Returned padded and flat. Cells are visited from the last one the flood
    reached back to the first, so each passes its total on exactly once.
    """
    grid = routing.grid
    area = grid.row_values(grid.dx * grid.dy).astype(np.float32)
    area[~routing.valid] = 0.0
    av = memoryview(area)
    dv = memoryview(routing.down)
    order = routing.order
    total = max(1, len(order))
    chunk = 500000
    done = 0
    for end in range(len(order), 0, -chunk):
        start = max(0, end - chunk)
        for cell in order[start:end][::-1].tolist():
            target = dv[cell]
            if target >= 0:
                av[target] += av[cell]
        done += end - start
        if stop is not None and stop():
            raise Stopped()
        if progress is not None:
            progress(min(1.0, done / total))
    return area


# -------------------------------------------------------------- thresholds

def stream_cell_count(area_m2, threshold_m2):
    return int(np.count_nonzero(area_m2 >= threshold_m2))


def threshold_for_density(grid, area_m2, routing, density_km_per_km2,
                          basin_km2):
    """Smallest contributing area whose network is about as dense as asked.

    Density is stream length over the ROI's area. Length is estimated from the
    cell count (a stream cell is about one cell step long, a little more on the
    diagonal), which is close enough to choose a threshold with and costs one
    pass per candidate. Returns the threshold in m2.
    """
    rows = grid.unpad(routing.valid).any(axis=1)
    cell = float(np.mean(np.sqrt(grid.dx[rows] * grid.dy[rows]))) \
        if rows.any() else 30.0
    wanted = density_km_per_km2 * basin_km2 * 1000.0 / (cell * 1.15)
    values = area_m2[routing.valid]
    if values.size == 0:
        return cell * cell * 100
    values = np.sort(values)[::-1]
    index = int(min(max(wanted, 1), len(values) - 1))
    return float(max(values[index], cell * cell * 2))


# ---------------------------------------------------------------- the network

class Segment(object):
    """A run of stream cells between two junctions (or a head and a junction)."""

    __slots__ = ('cells', 'order', 'ident', 'to_ident', 'head', 'junction')

    def __init__(self, cells, order, head, junction):
        self.cells = cells
        self.order = order
        #: True when nothing flows into the first cell: a stream's source.
        self.head = head
        #: True when the last cell is a junction the next segment starts from.
        self.junction = junction
        self.ident = 0
        #: Identifier of the segment this one flows into; 0 at an outlet.
        self.to_ident = 0


def trace(routing, area_m2, threshold_m2, progress=None, stop=None):
    """Stream cells -> numbered segments.

    Returns ``(segments, stream_mask, strahler)`` where `stream_mask` and
    `strahler` are padded and flat. Orders are Strahler's: a head is 1, two
    equal orders meeting make the next one up, anything else keeps the larger.
    """
    grid = routing.grid
    down = routing.down
    stream = (area_m2 >= threshold_m2) & routing.valid
    stream_cells = np.flatnonzero(stream)
    if stream_cells.size == 0:
        return [], stream, np.zeros(grid.size, dtype=np.int16)

    # Inflow counts, within the stream only.
    targets = down[stream_cells]
    inside = (targets >= 0)
    inside[inside] = stream[targets[inside]]
    inflow = np.bincount(targets[inside], minlength=grid.size).astype(np.int32)

    # Strahler, upstream first: the reverse of the flood order.
    ordered = routing.order[stream[routing.order]][::-1]
    strahler = np.zeros(grid.size, dtype=np.int16)
    best = np.zeros(grid.size, dtype=np.int16)
    ties = np.zeros(grid.size, dtype=np.int16)
    bv, tv = memoryview(best), memoryview(ties)
    sv = memoryview(strahler)
    dv = memoryview(down)
    stream_view = memoryview(stream.view(np.uint8))
    for done, cell in enumerate(ordered.tolist()):
        top = bv[cell]
        value = 1 if top == 0 else (top + 1 if tv[cell] > 1 else top)
        sv[cell] = value
        nxt = dv[cell]
        if nxt >= 0 and stream_view[nxt]:
            if value > bv[nxt]:
                bv[nxt] = value
                tv[nxt] = 1
            elif value == bv[nxt]:
                tv[nxt] += 1
        if done % REPORT_EVERY == 0:
            if stop is not None and stop():
                raise Stopped()

    # Segments start at a head (no stream inflow) or at a junction (two or
    # more), and run down until the next junction or the outlet. The junction
    # cell is the last vertex of every segment flowing into it and the first
    # of the one that carries on, so the lines join up.
    iv = memoryview(inflow)
    starts = stream_cells[inflow[stream_cells] != 1]
    segments = []
    owner = {}                              # first cell -> segment index
    total = max(1, len(starts))
    for count, start in enumerate(starts.tolist()):
        cells = [start]
        cell = start
        junction = False
        while True:
            nxt = dv[cell]
            if nxt < 0 or not stream_view[nxt]:
                break
            cells.append(nxt)
            if iv[nxt] >= 2:
                junction = True
                break
            cell = nxt
        segment = Segment(cells, int(sv[start]), iv[start] == 0, junction)
        segment.ident = len(segments) + 1
        owner[start] = segment
        segments.append(segment)
        if count % 2000 == 0:
            if stop is not None and stop():
                raise Stopped()
            if progress is not None:
                progress(min(1.0, count / total))
    for segment in segments:
        last = segment.cells[-1]
        follower = owner.get(last)
        if follower is not None and follower is not segment:
            segment.to_ident = follower.ident
    return segments, stream, strahler


# -------------------------------------------------------- per-segment metrics

def coordinates(grid, transform, cells):
    """Map (x, y) of padded cell indices, at cell centres."""
    rows, cols = grid.row_col(np.asarray(cells))
    x = transform[0] + (cols + 0.5) * transform[1] + (rows + 0.5) * transform[2]
    y = transform[3] + (cols + 0.5) * transform[4] + (rows + 0.5) * transform[5]
    return x, y


def segment_length_m(grid, segment):
    """Length of a segment along its cells, in metres."""
    cells = np.asarray(segment.cells)
    if len(cells) < 2:
        return 0.0
    rows, cols = grid.row_col(cells)
    drow = np.diff(rows)
    dcol = np.diff(cols)
    mid = np.clip(((rows[:-1] + rows[1:]) // 2), 0, grid.rows - 1)
    return float(np.sum(np.hypot(dcol * grid.dx[mid], drow * grid.dy[mid])))


class Windows(object):
    """Box sums over the grid, by integral image: a few lookups per cell.

    Built once for the elevation and the data mask, so the relief around any
    stream cell - and whether it sits near the edge of the data - costs
    nothing like a pass over the grid.
    """

    def __init__(self, grid, elevation_padded, valid_padded):
        self.grid = grid
        self.shape = grid.rows + 2, grid.cols + 2
        mask = np.asarray(valid_padded, dtype=np.float64).reshape(self.shape)
        height = np.where(mask > 0, np.asarray(
            elevation_padded, dtype=np.float64).reshape(self.shape), 0.0)
        self.height = height
        self.values = self._integral(height)
        self.count = self._integral(mask)

    def _integral(self, array2d):
        out = np.zeros((self.shape[0] + 1, self.shape[1] + 1))
        out[1:, 1:] = array2d.cumsum(axis=0).cumsum(axis=1)
        return out

    def _box(self, summed, cells, radius):
        rows = cells // self.grid.width
        cols = cells % self.grid.width
        r0 = np.clip(rows - radius, 0, self.shape[0])
        r1 = np.clip(rows + radius + 1, 0, self.shape[0])
        c0 = np.clip(cols - radius, 0, self.shape[1])
        c1 = np.clip(cols + radius + 1, 0, self.shape[1])
        return (summed[r1, c1] - summed[r0, c1] - summed[r1, c0]
                + summed[r0, c0])

    def relief(self, cells, radius=3):
        """Mean ground height around each cell, minus the cell's own.

        A real channel lies in a valley, so this is positive; a "stream" on a
        ridge or an open plain scores about zero or below. Only cells with data
        count towards the mean.
        """
        cells = np.asarray(cells)
        total = self._box(self.values, cells, radius)
        count = np.maximum(self._box(self.count, cells, radius), 1.0)
        rows = cells // self.grid.width
        cols = cells % self.grid.width
        return total / count - self.height[rows, cols]

    def near_void(self, cells, radius=2):
        """True where the box around a cell holds NoData or the grid's edge."""
        cells = np.asarray(cells)
        full = (2 * radius + 1) ** 2
        return self._box(self.count, cells, radius) < full

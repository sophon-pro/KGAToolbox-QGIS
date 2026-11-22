"""
focal.py - Fast, dependency-light focal (moving-window) operators.

Everything here is pure NumPy so the analysis engine runs inside any
QGIS install (OSGeo4W, macOS, Linux) without requiring SciPy.  When SciPy
*is* available it is used for connected-component labelling only.

All functions expect 2-D float arrays WITHOUT NaNs (the engine fills
NoData before calling them and masks the affected border afterwards).
"""

import warnings

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

try:  # optional accelerator for blob labelling
    from scipy import ndimage as _ndi
except Exception:  # pragma: no cover - SciPy missing is a supported case
    _ndi = None


# --------------------------------------------------------------------------
# Window helpers
# --------------------------------------------------------------------------
def odd(n, minimum=3):
    """Return the nearest odd integer >= minimum."""
    n = max(int(round(n)), minimum)
    return n if n % 2 == 1 else n + 1


def box_mean(a, size):
    """Mean filter (size x size) using an integral image - O(1) per pixel."""
    r = size // 2
    p = np.pad(a.astype(np.float64), r, mode="reflect")
    c = np.pad(p.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    s = c[size:, size:] - c[:-size, size:] - c[size:, :-size] + c[:-size, :-size]
    return s / float(size * size)


def _filter_1d(a, size, axis, func):
    r = size // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r, r)
    p = np.pad(a, pad, mode="edge")
    return func(sliding_window_view(p, size, axis=axis), axis=-1)


def min_filter(a, size):
    """Square minimum filter (grey-scale erosion), separable."""
    return _filter_1d(_filter_1d(a, size, 0, np.min), size, 1, np.min)


def max_filter(a, size):
    """Square maximum filter (grey-scale dilation), separable."""
    return _filter_1d(_filter_1d(a, size, 0, np.max), size, 1, np.max)




def _interp_axis(coarse, centres, n, axis):
    """Linear interpolation (and extrapolation) of block values located at
    `centres` onto pixel positions 0..n-1 along `axis`."""
    if len(centres) == 1:
        return np.repeat(coarse, n, axis=axis)
    x = np.arange(n, dtype=np.float64)
    idx = np.clip(np.searchsorted(centres, x) - 1, 0, len(centres) - 2)
    x0, x1 = centres[idx], centres[idx + 1]
    t = (x - x0) / (x1 - x0)          # not clipped: planes extrapolate exactly
    shape = [1, 1]
    shape[axis] = n
    t = t.reshape(shape)
    a = np.take(coarse, idx, axis=axis)
    b = np.take(coarse, idx + 1, axis=axis)
    return a * (1 - t) + b * t


def _block_starts(n, block):
    """Block start offsets covering 0..n; the last block is aligned to the end."""
    if n <= block:
        return np.array([0])
    starts = list(range(0, n - block + 1, block))
    if starts[-1] + block < n:
        starts.append(n - block)
    return np.array(starts)


def median_ground(a, block):
    """Robust local ground surface.

    Medians of `block` x `block` cells, linearly interpolated back to full
    resolution.  Unlike a morphological opening, a median ignores BOTH narrow
    bumps (objects) and narrow pits (ditches, channels), so densely incised
    bare terrain is not mistaken for objects.  Planar slopes are reproduced
    exactly (no padding is used, and edges are linearly extrapolated).
    """
    h, w = a.shape
    by, bx = min(max(2, int(block)), h), min(max(2, int(block)), w)
    sy, sx = _block_starts(h, by), _block_starts(w, bx)
    rows = sy[:, None] + np.arange(by)            # (ny, by)
    cols = sx[:, None] + np.arange(bx)            # (nx, bx)
    cells = a[rows][:, :, cols]                   # (ny, by, nx, bx)
    med = np.median(cells.transpose(0, 2, 1, 3).reshape(len(sy), len(sx), by * bx), axis=-1)
    cy = sy + (by - 1) / 2.0
    cx = sx + (bx - 1) / 2.0
    return _interp_axis(_interp_axis(med, cy, h, 0), cx, w, 1)


def running_median_1d(profile, size):
    """Running median along a profile (window = `size` samples)."""
    r = size // 2
    p = np.pad(profile, r, mode="edge")
    return np.median(sliding_window_view(p, size), axis=-1)



# --------------------------------------------------------------------------
# Terrain derivatives
# --------------------------------------------------------------------------
def slope_degrees(z, dx, dy):
    """Slope in degrees using central differences (Horn-like accuracy is
    not needed for a statistical diagnostic)."""
    gy, gx = np.gradient(z, dy, dx)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def block_std(a, valid, block=8):
    """Std-dev of `a` inside non-overlapping blocks that are fully valid."""
    h = (a.shape[0] // block) * block
    w = (a.shape[1] // block) * block
    if h == 0 or w == 0:
        return np.array([])
    av = a[:h, :w].reshape(h // block, block, w // block, block)
    vv = valid[:h, :w].reshape(h // block, block, w // block, block)
    full = vv.all(axis=(1, 3))
    stds = av.std(axis=(1, 3))
    return stds[full]


# --------------------------------------------------------------------------
# Connected components (8-connectivity)
# --------------------------------------------------------------------------
def label_blobs(mask):
    """Label connected True regions.  Returns (labels, n)."""
    if _ndi is not None:
        lab, n = _ndi.label(mask, structure=np.ones((3, 3), bool))
        return lab, int(n)

    # Pure-NumPy fallback (QGIS builds without SciPy): label propagation with
    # union-find "hooking" and pointer jumping - converges in a few dozen
    # rounds even for long, winding regions.
    h, w = mask.shape
    n_px = h * w
    flat_mask = mask.ravel()
    lab = np.arange(n_px, dtype=np.int64)
    pos = np.flatnonzero(flat_mask)
    if pos.size == 0:
        return np.zeros((h, w), np.int64), 0
    big = n_px
    shifts = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for _ in range(200):
        cur = np.where(flat_mask, lab, big).reshape(h, w)
        p = np.pad(cur, 1, mode="constant", constant_values=big)
        nb = cur.copy()
        for dy, dx in shifts:
            np.minimum(nb, p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w], out=nb)
        nbf = nb.ravel()[pos]
        old = lab[pos]
        changed = nbf < old
        if not changed.any():
            break
        # hook: the root of each pixel adopts the smaller neighbour label
        np.minimum.at(lab, old[changed], nbf[changed])
        lab[pos] = np.minimum(lab[pos], nbf)
        # pointer jumping (path compression)
        while True:
            nxt = lab[lab[pos]]
            if np.array_equal(nxt, lab[pos]):
                break
            lab[pos] = nxt
    out = np.zeros(n_px, np.int64)
    roots = lab[pos]
    uniq, inv = np.unique(roots, return_inverse=True)
    out[pos] = inv + 1
    return out.reshape(h, w), int(len(uniq))


def blob_elongation(labels, n, counts):
    """(longest bounding-box side)^2 / area for each blob: ~1 for squares and
    discs, large for long thin features such as embankments."""
    if n == 0:
        return np.array([])
    rows, cols = np.nonzero(labels)
    ids = labels[rows, cols]
    rmin = np.full(n + 1, np.iinfo(np.int64).max)
    rmax = np.full(n + 1, -1)
    cmin, cmax = rmin.copy(), rmax.copy()
    np.minimum.at(rmin, ids, rows)
    np.maximum.at(rmax, ids, rows)
    np.minimum.at(cmin, ids, cols)
    np.maximum.at(cmax, ids, cols)
    ext = np.maximum(rmax - rmin + 1, cmax - cmin + 1)[1:].astype(np.float64)
    return ext ** 2 / np.maximum(counts, 1)


def blob_stats(labels, n, values):
    """Per-blob pixel count and maximum of `values`."""
    if n == 0:
        return np.array([]), np.array([])
    flat = labels.ravel()
    counts = np.bincount(flat, minlength=n + 1)[1:]
    vmax = np.full(n + 1, -np.inf)
    np.maximum.at(vmax, flat, values.ravel())
    return counts, vmax[1:]


# --------------------------------------------------------------------------
# Effective (information) resolution
# --------------------------------------------------------------------------
def effective_factor(tiles, max_factor=180):
    """Estimate how many pixels one REAL measurement spans.

    A raster resampled to finer pixels (e.g. Copernicus 30 m warped to a
    0.5 m grid) is smooth between the original samples, so the mean absolute
    elevation difference d(L) grows linearly with the lag L (log-log slope
    ~1) up to about the original spacing, then flattens.  Genuine fine data
    is rough at the pixel scale (slope well below 1 from the first lag).

    1. breakpoint: the lag where the log-log slope of d(L) drops below 0.85
       (calibrated: 0.8-1.25 x the true factor on flat or noisy terrain);
    2. refinement: the repetition period of the interpolation kinks, searched
       only up to 1.9 x the breakpoint so harmonics are excluded.
    Returns 1 for native-resolution data.
    """
    width = min(min(t.shape) for t in tiles) if tiles else 0
    if width < 64:
        return 1
    lags = np.unique(np.round(2 ** np.arange(0, 8.01, 0.25)).astype(int))
    lags = lags[(lags <= width // 2) & (lags <= max_factor)]
    d = []
    for L in lags:
        vals = []
        for t in tiles:
            for a in (t, t.T):
                with np.errstate(all="ignore"), warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    v = np.nanmean(np.abs(a[:, L:] - a[:, :-L]))
                if np.isfinite(v):
                    vals.append(v)
        d.append(np.median(vals) if vals else np.nan)
    d = np.array(d)
    ok = np.isfinite(d) & (d > 0)
    lags, d = lags[ok], d[ok]
    if len(lags) < 4:
        return 1
    slope = np.diff(np.log(d)) / np.diff(np.log(lags))
    if slope[0] < 0.85:
        return 1                                   # rough at pixel scale: native data
    below = np.flatnonzero(slope < 0.85)
    breakpoint = float(np.sqrt(lags[below[0]] * lags[below[0] + 1])) if below.size \
        else float(lags[-1])
    if breakpoint < 3:
        return 1
    period = _kink_period(tiles, int(1.9 * breakpoint) + 2)
    factor = period if (period is not None and period >= 2) else int(round(breakpoint))
    return int(min(factor, max_factor))


def _kink_period(tiles, max_lag):
    """Try 2nd differences (bilinear / nearest) then 3rd (cubic, spline)."""
    for order in (2, 3):
        p = _kink_period_order(tiles, max_lag, order)
        if p is not None:
            return p
    return None


def _kink_period_order(tiles, max_lag, order):
    periods = []
    for t in tiles:
        for a in (t, t.T):
            if a.shape[1] < 3 * max_lag:
                continue
            for r0 in range(0, a.shape[0] - 16, 64):
                blk = a[r0:r0 + 64]
                with np.errstate(all="ignore"), warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    g = np.nanmean(np.abs(np.diff(blk, n=order, axis=1)), axis=0)
                if not np.isfinite(g).all() or g.std() == 0:
                    continue
                g = g - g.mean()
                ac = np.correlate(g, g, mode="full")[len(g) - 1:]
                ac = ac / ac[0]
                # Skip the smooth central lobe (first zero crossing), then take
                # the FIRST peak that is nearly as strong as the strongest one,
                # i.e. the fundamental spacing, not one of its harmonics.
                neg = np.flatnonzero(ac[1:] < 0)
                if neg.size == 0:
                    continue
                lo = int(neg[0]) + 1
                lags = np.arange(max(2, lo), min(max_lag, len(ac) - 1))
                if lags.size == 0:
                    continue
                pk = lags[(ac[lags] >= ac[lags - 1]) & (ac[lags] >= ac[lags + 1]) & (ac[lags] > 0.2)]
                if pk.size == 0:
                    continue
                strong = pk[ac[pk] >= 0.6 * ac[pk].max()]
                periods.append(int(strong[0]))
    if len(periods) < 3:
        return None
    vals, counts = np.unique(periods, return_counts=True)
    # Merge +/-1 px neighbours (non-integer spacing such as 61.7 px)
    score = np.array([counts[np.abs(vals - v) <= 1].sum() for v in vals])
    best = vals[np.argmax(score)]
    return int(best) if score.max() >= 0.3 * len(periods) else None

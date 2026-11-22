"""
multiscale.py - Multi-scale filter stability test (progressive morphological
filtering, as used by LiDAR ground filters).

The surface is "opened" (grey-scale erosion followed by dilation) with square
windows of increasing size, e.g. 1, 2, 3, 5, 8, 12, 16 m.  An opening removes
every raised feature narrower than the window and leaves broader terrain
untouched.  For each scale we record how much the surface drops (dZ = z -
opening) and follow the highest local points ("peaks"):

  * DSM  - tree crowns and houses vanish abruptly once the window exceeds
           their width (5-16 m): peaks drop by 5-15 m.
  * DEM  - terrain survives; peaks are mound tops / plot surfaces that stay in
           place (drop < 0.5 m) or broad embankments (long, thin - excluded).
  * Partially filtered DTM - compact peaks drop by only 0.7-4.5 m.

Only COMPACT removed regions count, so linear embankments, levees and canal
banks (also removed by large openings) are not mistaken for objects.

Neighbourhood statistics at the same scales (TPI = z - focal mean, focal
standard deviation, focal range) are reported for reference.  They are
highly correlated with the opening signal and are not scored separately.
"""

import numpy as np

from . import focal

SCALES_M = [1, 2, 3, 5, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256]


def scale_ladder(res, tile_px, coarse=False, scales_m=None):
    """Window sizes (odd px) for the analysis resolution; returns [(m, px)].

    scales_m : user-defined windows in metres (None = automatic ladder).
    A window must span >= 1.5 pixels; it is rounded to an odd pixel count, so
    the reported size (e.g. 1.5 m for "1 m" on 0.5 m data) is the real one.
    """
    out, seen = [], set()
    user = scales_m is not None and len(scales_m) > 0
    max_px = max(5, min(81 if user else 41, tile_px // 4 if user else tile_px // 6))
    for s in (sorted(scales_m) if user else SCALES_M):
        px = focal.odd(s / res, minimum=3)
        if s / res < 1.5 or px > max_px or px in seen:
            continue
        if not user and not coarse and s > 20:   # houses <= 12 m, crowns <= 15 m
            continue
        seen.add(px)
        out.append((round(px * res, 2), px))
    return out


def new_accumulator(ladder):
    return {"ladder": ladder,
            "removed": {px: 0 for _, px in ladder},       # flat px removed > 1 m (compact)
            "removed_full": {px: 0 for _, px in ladder},  # ... > 4.5 m
            "p995": {px: [] for _, px in ladder},
            "tpi": {px: [] for _, px in ladder},
            "fsd": {px: [] for _, px in ladder},
            "frange": {px: [] for _, px in ladder},
            "n_flat": 0, "peaks": []}


def tile_stats(z, safe, flat, th, res, acc, full_h=4.5, low_h=0.7, max_peaks=60):
    """Accumulate opening / neighbourhood statistics for one tile."""
    ladder = acc["ladder"]
    if not ladder or flat.sum() < 100:
        return
    acc["n_flat"] += int(flat.sum())
    # Candidate peaks: local maxima (~10 m) on flat terrain, ranked by height
    # above the robust local ground.
    w_peak = focal.odd(10.0 / res, minimum=3)
    is_max = (z >= focal.max_filter(z, w_peak) - 1e-6) & flat
    ys, xs = np.nonzero(is_max)
    if ys.size:
        order = np.argsort(-th[ys, xs])[:max_peaks]
        ys, xs = ys[order], xs[order]
    drops = np.zeros((ys.size, len(ladder)))
    compact_at = np.zeros((ys.size, len(ladder)), bool)

    for j, (_m, px) in enumerate(ladder):
        opened = focal.max_filter(focal.min_filter(z, px), px)
        dz = z - opened
        rem = (dz > 0.5) & safe
        lab, n = focal.label_blobs(rem)
        compact_lab = np.zeros(n + 1, bool)
        if n:
            counts, _ = focal.blob_stats(lab, n, dz)
            compact_lab[1:] = focal.blob_elongation(lab, n, counts) < 8.0
        comp = compact_lab[lab] & rem
        acc["removed"][px] += int((comp & flat & (dz > 1.0)).sum())
        acc["removed_full"][px] += int((comp & flat & (dz > full_h)).sum())
        f = dz[flat]
        acc["p995"][px].append(float(np.percentile(f, 99.5)))
        # neighbourhood statistics at the same window
        m1 = focal.box_mean(z, px)
        sd = np.sqrt(np.clip(focal.box_mean(z * z, px) - m1 * m1, 0, None))
        rng = focal.max_filter(z, px) - focal.min_filter(z, px)
        acc["tpi"][px].append(float(np.percentile((z - m1)[flat], 99.5)))
        acc["fsd"][px].append(float(np.percentile(sd[flat], 99)))
        acc["frange"][px].append(float(np.percentile(rng[flat], 99)))
        if ys.size:
            drops[:, j] = dz[ys, xs]
            compact_at[:, j] = comp[ys, xs]

    for i in range(ys.size):
        acc["peaks"].append((drops[i], compact_at[i], float(th[ys[i], xs[i]])))


def summarise(acc, res, full_h=4.5, low_h=0.7, coarse=False):
    """Peak stability + scale curve -> dict (or None when nothing to judge)."""
    ladder = acc["ladder"]
    if not ladder or not acc["peaks"] or acc["n_flat"] == 0:
        return None
    # objects: <= 16 m on fine data, <= ~7 pixels (village scale) on coarse data
    obj_max_m = max(16.0, 7 * res) if coarse else 16.0
    low_max_m = max(12.0, 5 * res) if coarse else 12.0
    # Windows are rounded to odd pixel counts (e.g. "12 m" -> 12.5 m), so the
    # limits get one pixel of tolerance.  User-defined ladders may still fall
    # outside a range: then the nearest windows are used instead of none.
    tol = res + 1e-6
    all_j = list(range(len(ladder)))
    jj_obj = [j for j, (m, _) in enumerate(ladder) if m <= obj_max_m + tol] or all_j[:1]
    jj_low = [j for j, (m, _) in enumerate(ladder) if m <= low_max_m + tol] or jj_obj
    jj_small = [j for j, (m, _) in enumerate(ladder) if m <= 3.5 + tol]
    n = len(acc["peaks"])
    full = low = stable = 0
    collapse_scales = []
    for drops, comp, _th in acc["peaks"]:
        d_obj = [(drops[j], j) for j in jj_obj if comp[j]]
        best = max(d_obj) if d_obj else (0.0, None)
        if best[0] >= full_h:
            full += 1
            first = min(j for j in jj_obj if comp[j] and drops[j] >= full_h)
            collapse_scales.append(ladder[first][0])
            continue
        d_low = [drops[j] for j in jj_low if comp[j]]
        if d_low and low_h <= max(d_low) < full_h:
            low += 1
        elif max((drops[j] for j in jj_low), default=0.0) < 0.5:
            stable += 1
    small_drop = [max(d[j] for j in jj_small) for d, _, _ in acc["peaks"]] if jj_small else []
    curve = []
    for m, px in ladder:
        curve.append({
            "scale_m": m,
            "removed_pct": round(100.0 * acc["removed"][px] / acc["n_flat"], 3),
            "removed_full_pct": round(100.0 * acc["removed_full"][px] / acc["n_flat"], 3),
            "p995_dz": round(float(np.median(acc["p995"][px])), 2),
            "tpi_p995": round(float(np.median(acc["tpi"][px])), 2),
            "focal_sd_p99": round(float(np.median(acc["fsd"][px])), 2),
            "focal_range_p99": round(float(np.median(acc["frange"][px])), 2),
        })
    warn = []
    if len(ladder) < 3:
        warn.append("Only {} filter window(s): the test needs several windows (e.g. 1, 2, 3, "
                    "5, 8, 12, 16 m) to see at WHICH scale objects disappear.".format(len(ladder)))
    if min(m for m, _ in ladder) > obj_max_m + tol:
        warn.append("All windows are wider than {:g} m: houses and trees are removed together "
                    "with broader features, so the result is less specific.".format(obj_max_m))
    if max(m for m, _ in ladder) < 5.0:
        warn.append("All windows are narrower than 5 m: houses (6-12 m) and crowns (5-15 m) "
                    "cannot be removed - only small bumps are tested.")
    return {
        "warnings": warn,
        "n_peaks": n,
        "full_pct": round(100.0 * full / n, 1),
        "low_pct": round(100.0 * low / n, 1),
        "stable_pct": round(100.0 * stable / n, 1),
        "median_collapse_scale_m": float(np.median(collapse_scales)) if collapse_scales else None,
        "small_scale_drop_p90": round(float(np.percentile(small_drop, 90)), 2) if small_drop else None,
        "curve": curve,
        "obj_max_m": obj_max_m,
    }

"""
blocks.py - Adaptive block-grid inspection (pure NumPy).

The raster extent is divided into an N x N grid whose block size adapts to
the pixel size and the extent.  Each block is analysed from a window at its
centre with five criteria:

  1. dZ max         - tallest COMPACT raised feature above a robust local
                      ground (detrended Zmax - Zmedian; levees, embankments
                      and village mounds are not counted).  Raw Zmax - Zmedian
                      is reported for reference.
  2. Slope break    - share of raised pixels whose edge drops steeply (walls,
                      canopy margins) + micro-slope vs terrain-slope ratio.
  3. Roughness      - focal (3x3) standard deviation of the detrended surface,
                      relative to sensor noise.
  4. Profiles       - automatic row / column / diagonal transects; abrupt
                      up-and-down spikes per km.
  5. Continuity     - Moran's I of the detrended surface and the share of
                      "raised islands": high-high clusters surrounded by a
                      sharp drop to ground (Local-Moran style).

Each block gets a score in [-1, +1] and a label:
  OBJECTS  (off-ground objects present, DSM-like)
  BARE     (bare earth)       MIXED     (weak evidence)
  N/A      (steep terrain or too much NoData)

The block consensus is NOT a majority vote: in a DSM of farmland most blocks
are bare fields and only village / tree blocks show objects.  It asks how many
blocks show clear objects, compared with what a bare-earth DEM produces
(almost none).
"""

import math

import numpy as np

from . import focal

CRITERIA = [
    ("dz", "Highest off-ground peak (dZ max)"),
    ("slope", "Slope discontinuity"),
    ("rough", "Local roughness (focal SD)"),
    ("profile", "Cross-section profile spikes"),
    ("moran", "Surface continuity (Moran / raised islands)"),
]
# Calibrated on a flat irrigation landscape (0.5 m): dZ, slope breaks and raised
# islands separate perfectly; profiles moderately; focal-SD roughness only
# weakly (bunds and embankment edges are rough too), hence its low weight.
WEIGHTS = {"dz": 1.0, "slope": 0.8, "rough": 0.3, "profile": 0.6, "moran": 0.8}


def _squash(v, c, s):
    return float(np.tanh((v - c) / s))


# --------------------------------------------------------------------------
# Grid design
# --------------------------------------------------------------------------
def design_grid(width, height, k, res_eff, win_px, max_side=20, max_window=256):
    """Adaptive grid on a raster of width x height FILE pixels.

    k        : file pixels per effective pixel (1 unless the raster was resampled)
    res_eff  : effective pixel size (m)
    win_px   : object window (effective px) used by the detectors

    Block side (effective px) >= max(64, 6 x object window) so each block is
    statistically meaningful, and at most `max_side` blocks per side.
    Returns dict(nx, ny, block_w, block_h (file px), windows=[(x, y, w, h)],
    cells=[(row, col, x0, y0, x1, y1) file px]).
    """
    wf, hf = width / k, height / k                         # effective px
    min_side = max(64, 6 * win_px)
    nx = int(np.clip(math.floor(wf / min_side), 1, max_side))
    ny = int(np.clip(math.floor(hf / min_side), 1, max_side))
    bw, bh = width / nx, height / ny                       # file px
    win_w = int(min(bw, max_window * k))
    win_h = int(min(bh, max_window * k))
    windows, cells = [], []
    for r in range(ny):
        for c in range(nx):
            x0, y0 = int(round(c * bw)), int(round(r * bh))
            x1, y1 = int(round((c + 1) * bw)), int(round((r + 1) * bh))
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            wx = int(np.clip(cx - win_w // 2, 0, width - win_w))
            wy = int(np.clip(cy - win_h // 2, 0, height - win_h))
            windows.append((wx, wy, win_w, win_h))
            cells.append((r, c, x0, y0, x1, y1))
    return {"nx": nx, "ny": ny, "block_w": bw, "block_h": bh,
            "block_m": (bw * res_eff / k, bh * res_eff / k), "windows": windows, "cells": cells}


# --------------------------------------------------------------------------
# One block
# --------------------------------------------------------------------------
def _peak_shape(z, safe, iy, ix, res, drop=0.25):
    """What kind of feature carries the highest cell?

    The crest = cells within `drop` m of the peak that connect to it.
      linear  - long thin crest: paddy dike / bund, road or canal embankment
      broad   - wide flat top: raised house plot, mound, levee shoulder
      compact - small peak: tree crown, roof (or a small heap)
    """
    r = max(5, int(round(60.0 / res)))                # look within ~60 m
    y0, y1 = max(0, iy - r), min(z.shape[0], iy + r + 1)
    x0, x1 = max(0, ix - r), min(z.shape[1], ix + r + 1)
    sub = z[y0:y1, x0:x1]
    crest = (sub >= z[iy, ix] - drop) & safe[y0:y1, x0:x1]
    lab, n = focal.label_blobs(crest)
    k = lab[iy - y0, ix - x0]
    if n == 0 or k == 0:
        return "compact"
    comp = lab == k
    count = int(comp.sum())
    elong = float(focal.blob_elongation(np.where(comp, 1, 0), 1, np.array([count]))[0])
    rows, cols = np.nonzero(comp)
    length_m = max(np.ptp(rows), np.ptp(cols)) * res + res
    area_m2 = count * res * res
    if elong >= 6.0 and length_m >= 10.0:
        return "linear"
    if area_m2 >= 150.0:
        return "broad"
    return "compact"


def _profiles(z, th, flat, win, h_thr, jump, res):
    """Rows, columns and both diagonals through the block.  Returns
    (spike_count, km_sampled, best_profile dict)."""
    n_r, n_c = z.shape
    lines = []
    # every ~4 m (at least 16 lines per direction) so small crowns are crossed
    n_lines = int(np.clip(n_r * res / 4.0, 16, 64))
    for i in np.unique(np.linspace(2, n_r - 3, n_lines).astype(int)):
        lines.append(("row", i, np.arange(n_c), np.full(n_c, i), res))
    for j in np.unique(np.linspace(2, n_c - 3, n_lines).astype(int)):
        lines.append(("col", j, np.full(n_r, j), np.arange(n_r), res))
    m = min(n_r, n_c)
    lines.append(("diag", 0, np.arange(m), np.arange(m), res * math.sqrt(2)))
    lines.append(("anti", 0, np.arange(m)[::-1], np.arange(m), res * math.sqrt(2)))

    spikes, km, best = 0, 0.0, None
    for kind, idx, xs, ys, step in lines:
        prof, fl = z[ys, xs], flat[ys, xs]
        if fl.sum() < 10:
            continue
        ground = focal.running_median_1d(prof, win)
        rel = prof - ground
        mask = (rel > h_thr) & fl
        m8 = np.concatenate(([0], mask.astype(np.int8), [0]))
        st = np.flatnonzero(np.diff(m8) == 1)
        en = np.flatnonzero(np.diff(m8) == -1) - 1
        nn = len(rel)
        left = rel[st] - rel[np.maximum(st - 1, 0)]
        right = rel[en] - rel[np.minimum(en + 1, nn - 1)]
        ok = (st > 0) & (en < nn - 1) & (left > jump) & (right > jump)
        spikes += int(ok.sum())
        km += fl.sum() * step / 1000.0
        peak = float(rel[mask].max()) if mask.any() else float(np.nanmax(rel))
        if best is None or peak > best["peak"]:
            best = {"kind": kind, "index": int(idx), "xs": xs, "ys": ys, "step_m": step,
                    "z": prof, "ground": ground,
                    "spikes": [(int(a), int(b)) for a, b in zip(st[ok], en[ok])],
                    "peak": peak}
    return spikes, km, best


def analyse_block(z_raw, sigma, h_thr, theta, res, win, ctx, p, detail=False):
    """Analyse one block window (effective resolution).  Returns a dict with
    per-criterion values / scores, label and, if detail=True, arrays for the
    inspector (hillshade input, object mask, best profile)."""
    valid = np.isfinite(z_raw)
    out = {"valid_pct": round(100 * float(valid.mean()), 1)}
    if valid.mean() < 0.5 or z_raw.shape[0] < 16 or z_raw.shape[1] < 16:
        out.update(label="N/A", reason="mostly NoData", score=0.0)
        return out
    z = z_raw.astype(np.float64).copy()
    z[~valid] = np.nanmedian(z_raw)
    border = max(win, ctx) // 2 + 1
    safe = focal.min_filter(valid.astype(np.uint8), focal.odd(2 * min(border, 6) + 1)) > 0

    zs = focal.box_mean(z, ctx)
    rng_ctx = focal.max_filter(zs, ctx) - focal.min_filter(zs, ctx)
    flat = (rng_ctx < math.tan(math.radians(p["flat_slope_deg"])) * ctx * res) & safe
    flat_frac = float(flat.sum() / max(safe.sum(), 1))
    out["flat_pct"] = round(100 * flat_frac, 1)
    out["raw_dz"] = round(float(np.nanmax(z_raw) - np.nanmedian(z_raw)), 2)
    if flat_frac < 0.5:      # hill crests mimic objects - do not guess
        out.update(label="N/A", reason="not flat enough", score=0.0)
        return out

    th = z - focal.median_ground(z, win)
    drop = np.degrees(np.arctan((th - focal.min_filter(th, 3)) / res))
    steep = (drop > theta) & (th > 0.5 * h_thr) & flat

    # ---- compact raised features (exclude levees / embankments) -----------
    cand = (th > h_thr) & flat
    lab, n = focal.label_blobs(cand)
    obj = np.zeros_like(cand)
    if n:
        counts, _ = focal.blob_stats(lab, n, th)
        compact = focal.blob_elongation(lab, n, counts) < 8.0
        if not p.get("smooth_edges"):
            perim = cand & ~(focal.min_filter(cand.astype(np.uint8), 3) > 0)
            n_perim = np.bincount(lab[perim], minlength=n + 1)[1:]
            n_sharp = np.bincount(lab[steep & cand], minlength=n + 1)[1:]
            compact &= n_sharp >= np.maximum(1, 0.25 * n_perim)
        obj = np.isin(lab, np.flatnonzero(compact) + 1)

    # Low residual bumps: compact, sharp raised features between ~0.5 m and the
    # object threshold - the signature of trees/houses only partly removed.
    lo_thr = max(0.5, 4 * sigma)
    low_pm = 0.0
    if lo_thr < h_thr and not p.get("smooth_edges"):   # needs real fine-scale edges
        lc = (th > lo_thr) & (th <= h_thr) & flat & ~obj
        llab, ln = focal.label_blobs(lc)
        if ln:
            lcount, lmax = focal.blob_stats(llab, ln, th)
            lcomp = (focal.blob_elongation(llab, ln, lcount) < 8.0) & (lcount >= 2) & (lmax < h_thr)
            ldrop = np.degrees(np.arctan((th - focal.min_filter(th, 3)) / res))
            lsharp = np.bincount(llab[(ldrop > 0.6 * theta) & lc], minlength=ln + 1)[1:] > 0
            keep = np.isin(llab, np.flatnonzero(lcomp & (lsharp | bool(p.get("smooth_edges")))) + 1)
            low_pm = 1000.0 * keep.sum() / max(flat.sum(), 1)

    # Highest cell of the block (the user's manual check, automated): its
    # height above the TYPICAL nearby ground (median of cells within ~15 m).
    # Tree tops / roofs stand 5-15 m above it; the top of a raised house plot,
    # mound or embankment stands only ~0-3 m above it.
    zz_safe = np.where(safe, z, -np.inf)
    iy, ix = np.unravel_index(int(np.argmax(zz_safe)), z.shape)
    rr = max(3, int(round(15.0 / res)))
    y0, y1 = max(0, iy - rr), min(z.shape[0], iy + rr + 1)
    x0, x1 = max(0, ix - rr), min(z.shape[1], ix + rr + 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    disk = (yy - iy) ** 2 + (xx - ix) ** 2 <= rr * rr
    nb = z[y0:y1, x0:x1][disk & safe[y0:y1, x0:x1]]
    # median, not a low percentile: a canal bed next to an embankment crest
    # must not make the crest look like a 4 m object
    peak_dz = float(z[iy, ix] - np.median(nb)) if nb.size >= 5 else 0.0
    out["peak"] = {"row": int(iy), "col": int(ix), "z": round(float(z[iy, ix]), 2),
                   "dz_local": round(peak_dz, 2), "shape": _peak_shape(z, safe, iy, ix, res)}

    # 1. dZ max (detrended, compact objects only)
    dz = float(np.percentile(th[obj], 95)) if obj.sum() >= 3 else 0.0
    s_dz = _squash(dz, 1.5 * h_thr, 0.5 * h_thr) if dz > 0 else -0.9

    # 2. slope discontinuity
    steep_pm = 1000.0 * steep.sum() / max(flat.sum(), 1)            # per mille
    sl = focal.slope_degrees(z, res, res)
    sl_t = focal.slope_degrees(zs, res, res)
    micro = float(np.std(sl[flat]))
    terr = float(np.std(sl_t[flat])) + 0.5
    s_slope = _squash(steep_pm, 3.0, 3.0)

    # 3. roughness: focal SD of the detrended surface relative to noise
    m1 = focal.box_mean(th, 3)
    fsd = np.sqrt(np.clip(focal.box_mean(th * th, 3) - m1 * m1, 0, None))
    # Objects occupy a small share of a farmland block, so the TAIL matters:
    # 99.5th percentile of the focal SD, in units of the object threshold.
    rough = float(np.percentile(fsd[flat], 99.5)) / h_thr
    s_rough = _squash(rough, 0.5, 0.25)

    # 4. profiles
    jump = math.tan(math.radians(theta)) * res * (0.5 if p.get("smooth_edges") else 1.0)
    spikes, km, best = _profiles(z, th, flat, win, h_thr, jump, res)
    dens = spikes / km if km > 0 else 0.0
    s_prof = _squash(dens, 0.6, 0.8)

    # 5. continuity: Moran's I of detrended surface + raised islands
    zz = np.where(flat, th, np.nan)
    mu, sd = np.nanmean(zz), np.nanstd(zz)
    moran = 0.0
    if sd > 0:
        zn = (zz - mu) / sd
        num, cnt = 0.0, 0
        for a, b in ((zn[:, 1:], zn[:, :-1]), (zn[1:, :], zn[:-1, :])):
            prod = a * b
            ok = np.isfinite(prod)
            num += prod[ok].sum()
            cnt += ok.sum()
        moran = float((num / max(cnt, 1)) / max(np.nanmean(zn ** 2), 1e-9))
    ring = (focal.max_filter(obj.astype(np.uint8), 5) > 0) & ~obj & flat
    island_ok = obj.any() and ring.any() and float(np.median(th[ring])) < 0.4 * h_thr
    islands_pm = 1000.0 * obj.sum() / max(flat.sum(), 1) if island_ok else 0.0
    s_moran = _squash(islands_pm, 4.0, 4.0)

    scores = {"dz": s_dz, "slope": s_slope, "rough": s_rough, "profile": s_prof, "moran": s_moran}
    values = {"dz": dz, "slope": steep_pm, "rough": rough, "profile": dens, "moran": islands_pm}
    wsum = sum(WEIGHTS.values())
    score = sum(WEIGHTS[k] * scores[k] for k in scores) / wsum
    strong = sum(1 for v in scores.values() if v >= 0.5)
    full_height = p.get("smooth_edges") or res > 5 or dz >= 4.5
    if dz > 0 and strong >= 2 and score >= 0.0 and full_height:
        label = "OBJECTS"
    elif strong == 0 and score <= -0.35:
        label = "BARE"
    else:
        label = "MIXED"
    out.update(label=label, score=round(score, 3), strong=strong,
               values={k: round(float(v), 3) for k, v in values.items()},
               scores={k: round(float(v), 3) for k, v in scores.items()},
               moran_i=round(moran, 3), micro_slope_sd=round(micro, 2),
               terrain_slope_sd=round(terr - 0.5, 2), spikes=spikes, km=round(km, 3),
               objects_pm=round(1000.0 * obj.sum() / max(flat.sum(), 1), 2),
               low_residual_pm=round(low_pm, 2))
    if detail:
        out["detail"] = {"z": z_raw, "object_height": np.where(safe, th, np.nan),
                         "objects": obj, "flat": flat, "profile": best}
    return out


# --------------------------------------------------------------------------
# Consensus across blocks
# --------------------------------------------------------------------------
def consensus(blocks):
    """Summarise block results -> dict for the report and the indicator."""
    ev = [b for b in blocks if b["label"] != "N/A"]
    n_ev = len(ev)
    count = {lab: sum(1 for b in blocks if b["label"] == lab)
             for lab in ("OBJECTS", "MIXED", "BARE", "N/A")}
    frac_obj = count["OBJECTS"] / n_ev if n_ev else 0.0
    per = {}
    for key, name in CRITERIA:
        vals = [b["values"][key] for b in ev]
        sc = [b["scores"][key] for b in ev]
        per[key] = {
            "name": name,
            "median": round(float(np.median(vals)), 3) if vals else None,
            "p90": round(float(np.percentile(vals, 90)), 3) if vals else None,
            "dsm_like_pct": round(100 * float(np.mean(np.array(sc) >= 0.5)), 1) if sc else 0.0,
            "dem_like_pct": round(100 * float(np.mean(np.array(sc) <= -0.5)), 1) if sc else 0.0,
        }
    peaks = [b["peak"]["dz_local"] for b in blocks if b.get("peak")]
    shapes = [b["peak"].get("shape") for b in blocks if b.get("peak")]
    pk = {"n": len(peaks),
          "shape_pct": {s: round(100.0 * shapes.count(s) / len(shapes), 1)
                        for s in ("linear", "broad", "compact")} if shapes else {},
          "median_dz": round(float(np.median(peaks)), 2) if peaks else None,
          "p90_dz": round(float(np.percentile(peaks, 90)), 2) if peaks else None,
          "tall_pct": round(100 * float(np.mean(np.array(peaks) >= 4.5)), 1) if peaks else 0.0}
    resid_blocks = sum(1 for b in ev if b.get("low_residual_pm", 0) >= 2.0 and b["label"] != "OBJECTS")
    return {"evaluable": n_ev, "counts": count, "frac_objects": frac_obj, "criteria": per,
            "peaks": pk,
            "residual_blocks": resid_blocks,
            "frac_residual": resid_blocks / n_ev if n_ev else 0.0}

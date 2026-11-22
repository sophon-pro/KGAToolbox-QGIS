"""
landcover.py - Land-cover masks from an orthophoto (and optional building
footprints) for the land-cover-conditioned height test.

Why this matters
----------------
The single-layer indicators must GUESS where objects are from the
elevations alone.  An orthophoto tells us directly where trees and roofs
are, so we can ask the decisive question:

    "Do vegetation / building pixels stand higher than the open ground
     right next to them?"

In a DSM they do (by the object height).  In a bare-earth DEM they do not
- even when villages sit on raised levees, because the comparison is made
against nearby open ground on the same levee.
"""

import warnings

import numpy as np

from . import focal


def _otsu(values, lo, hi):
    """Otsu threshold of `values`, clamped to [lo, hi]."""
    v = values[np.isfinite(values)]
    if v.size < 100:
        return (lo + hi) / 2.0
    hist, edges = np.histogram(v, bins=256, range=(np.percentile(v, 0.5), np.percentile(v, 99.5)))
    centres = (edges[:-1] + edges[1:]) / 2.0
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centres) / np.maximum(w0, 1)
    mt = (hist * centres).sum()
    m1 = (mt - np.cumsum(hist * centres)) / np.maximum(w1, 1)
    between = w0 * w1 * (m0 - m1) ** 2
    return float(np.clip(centres[np.argmax(between)], lo, hi))


def vegetation_index(red, green, blue=None, nir=None):
    """NDVI when NIR is available, otherwise Excess Green (ExG) on
    chromatic coordinates.  Returns (index, valid, name)."""
    red = red.astype(np.float64)
    green = green.astype(np.float64)
    if nir is not None:
        nir = nir.astype(np.float64)
        s = nir + red
        valid = np.isfinite(s) & (s > 0)
        idx = np.where(valid, (nir - red) / np.where(s > 0, s, 1), np.nan)
        return idx, valid, "NDVI"
    blue = blue.astype(np.float64)
    s = red + green + blue
    valid = np.isfinite(s) & (s > 0)
    if valid.any():
        valid &= s > 0.05 * np.percentile(s[valid], 99)   # drop deep shadow / black fill
    ss = np.where(valid, s, 1)
    exg = 2 * green / ss - red / ss - blue / ss
    return np.where(valid, exg, np.nan), valid, "ExG"


def local_texture(img, size=3):
    """Coefficient of variation of brightness in a size x size window.
    Tree crowns (leaves + gaps + shadow) are rough; crops, grass, bare soil
    and roads are smooth."""
    a = np.where(np.isfinite(img), img, 0.0)
    m = focal.box_mean(a, size)
    m2 = focal.box_mean(a * a, size)
    sd = np.sqrt(np.clip(m2 - m * m, 0, None))
    return sd / np.maximum(m, 1e-6)


def classify(bands, nir_available, building_mask=None, res=None):
    """Build land-cover masks for one tile.

    bands : dict with 'red', 'green', 'blue' and optionally 'nir' arrays
    res   : elevation pixel size (m); texture is used when <= 1.5 m

    Classes
      tree : vegetation with a rough crown texture  (can stand above ground)
      crop : smooth vegetation - rice, grass, crops  (ground level -> OPEN)
      bldg : building footprints (optional layer)
      open : everything else valid, including crops
    """
    idx, valid, name = vegetation_index(bands["red"], bands["green"], bands.get("blue"),
                                        bands.get("nir") if nir_available else None)
    if name == "NDVI":
        thr = _otsu(idx[valid], 0.20, 0.50)
    else:
        thr = _otsu(idx[valid], 0.02, 0.15)
    veg = valid & (idx > thr)
    bldg = np.zeros_like(veg) if building_mask is None else (building_mask > 0) & valid
    veg &= ~bldg

    texture_used = res is not None and res <= 1.5
    tex_thr = None
    if texture_used and veg.sum() > 50:
        bright = bands["green"].astype(np.float64)
        cv = local_texture(bright, 3)
        smooth_ref = cv[valid & ~veg]
        base = float(np.median(smooth_ref)) if smooth_ref.size > 50 else float(np.median(cv[veg]))
        # Rough enough to be a crown: well above the texture of smooth surfaces
        tex_thr = max(0.06, 2.5 * base)
        tree = veg & (cv > tex_thr)
        # Grow by 1 px so crown pixels that happen to be locally smooth join
        tree = (focal.max_filter(tree.astype(np.uint8), 3) > 0) & veg
        crop = veg & ~tree
    else:
        tree, crop = veg, np.zeros_like(veg)

    return {"veg": tree, "crop": crop, "bldg": bldg, "open": valid & ~tree & ~bldg,
            "valid": valid, "index_name": name, "threshold": thr,
            "texture_used": texture_used, "texture_threshold": tex_thr}


def masked_ground(z, open_mask, block):
    """Local ground from OPEN pixels only: block medians of open pixels,
    gaps (blocks that are mostly trees/roofs) filled from neighbours, then
    linearly interpolated to full resolution."""
    h, w = z.shape
    by, bx = min(block, h), min(block, w)
    sy, sx = focal._block_starts(h, by), focal._block_starts(w, bx)
    rows = sy[:, None] + np.arange(by)
    cols = sx[:, None] + np.arange(bx)
    cells = np.where(open_mask, z, np.nan)[rows][:, :, cols]
    cells = cells.transpose(0, 2, 1, 3).reshape(len(sy), len(sx), by * bx)
    enough = np.sum(np.isfinite(cells), axis=-1) >= max(5, 0.1 * by * bx)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.where(enough, np.nanmedian(np.where(enough[..., None], cells, 0.0), axis=-1), np.nan)
    if not np.isfinite(med).any():
        return None
    # Fill empty blocks by repeated neighbour averaging
    for _ in range(max(med.shape)):
        if np.isfinite(med).all():
            break
        p = np.pad(med, 1, constant_values=np.nan)
        nb = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            fill = np.nanmean(nb, axis=0)
        med = np.where(np.isfinite(med), med, fill)
    med = np.where(np.isfinite(med), med, np.nanmedian(med))
    cy = sy + (by - 1) / 2.0
    cx = sx + (bx - 1) / 2.0
    return focal._interp_axis(focal._interp_axis(med, cy, h, 0), cx, w, 1)


def aggregate(mask, k, shape):
    """Fraction of True pixels in each k x k block, cropped/padded to `shape`
    (the shape of the elevation tile read with the same factor k)."""
    h, w = shape
    m = np.asarray(mask, dtype=np.float32)[:h * k, :w * k]
    if m.shape != (h * k, w * k):
        m = np.pad(m, ((0, h * k - m.shape[0]), (0, w * k - m.shape[1])))
    return m.reshape(h, k, w, k).mean(axis=(1, 3))

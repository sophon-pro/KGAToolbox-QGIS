"""
engine.py - DEM vs DSM diagnostic engine (pure NumPy, no QGIS imports).

Workflow
--------
1.  Each sampled tile is cleaned (NoData -> filled, border masked).
2.  A robust sensor-noise estimate (sigma) is derived from the quietest
    blocks of the raster.  Every threshold below adapts to it, so noisy
    30 m SAR data and 0.5 m LiDAR are judged on fair terms.
3.  A "flat-context" mask keeps only pixels whose *regional* slope is low.
    Natural terrain is smooth at object scale there, so anything that sticks
    up sharply is very likely a building, tree or other above-ground object.
4.  Six single-layer indicators are computed, each mapped to a score in
    [-1, +1]  (+1 = strong DSM evidence, -1 = strong bare-earth evidence)
    and given a reliability weight.
5.  An optional 7th, pairwise indicator compares two co-registered layers.
6.  Scores are fused into a classification and a calibrated confidence that
    is capped by what the data can physically prove (resolution, terrain,
    agreement between indicators).

No single-layer method can be 100 % certain: a DSM of a bare desert is
indistinguishable from a DEM.  The engine therefore reports
"INCONCLUSIVE" instead of guessing when evidence is weak.
"""

import math
from dataclasses import dataclass, field, asdict

import numpy as np

from . import blocks as blockmod
from . import focal, landcover, multiscale
from .metadata_check import evaluate_metadata

ENGINE_VERSION = "2.1.2"

# --------------------------------------------------------------------------
# Tunable parameters (calibrated on simulated surfaces, see README)
# --------------------------------------------------------------------------
DEFAULTS = {
    "object_width_m": 60.0,       # widest object the detectors should isolate
    "min_object_height_m": 2.5,   # smallest height counted as an object
    "flat_slope_deg": 5.0,        # regional slope limit for "flat context"
    "context_m": 150.0,           # window for regional (context) slope
    "noise_k": 4.0,               # threshold = max(min height, k * sigma)
    "max_window_px": 81,
    "resampled": False,            # data interpolated from coarser pixels (no sharp edges)
}


@dataclass
class Indicator:
    key: str
    name: str
    question: str
    value: float
    value_text: str
    score: float          # -1 (DEM) .. +1 (DSM)
    weight: float         # reliability weight used in fusion
    finding: str
    method: str

    @property
    def verdict(self):
        if self.weight <= 0:
            return "N/A"
        if self.key == "landcover" and 0.5 <= self.value < 2.0:
            return "Partial"
        if self.score >= 0.25:
            return "DSM"
        if self.score <= -0.25:
            return "DEM"
        return "Neutral"

    def to_dict(self):
        d = asdict(self)
        d["verdict"] = self.verdict
        return d


@dataclass
class Result:
    classification: str              # "DSM", "DEM", "PARTIAL" or "INCONCLUSIVE"
    lean: str                        # direction of the evidence
    confidence: float                # 0..100 (never 100)
    combined_score: float            # -1..+1
    indicators: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    caps: list = field(default_factory=list)
    info: dict = field(default_factory=dict)
    reference: dict = field(default_factory=dict)

    @property
    def headline(self):
        if self.classification == "INCONCLUSIVE":
            return "INCONCLUSIVE (leans {})".format(self.lean)
        if self.classification == "PARTIAL":
            return "PARTIALLY FILTERED DEM"
        return self.classification

    def to_dict(self):
        return {
            "classification": self.classification,
            "headline": self.headline,
            "lean": self.lean,
            "confidence": round(self.confidence, 1),
            "combined_score": round(self.combined_score, 3),
            "indicators": [i.to_dict() for i in self.indicators],
            "notes": self.notes,
            "confidence_caps": self.caps,
            "info": self.info,
            "reference": self.reference,
            "engine_version": ENGINE_VERSION,
        }


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _squash(value, centre, scale):
    """Map a metric to a score in (-1, 1) with a smooth tanh curve."""
    return float(np.tanh((value - centre) / scale))


def _prepare(tile, border):
    """Return (filled array, valid mask eroded by `border` px) or None."""
    valid = np.isfinite(tile)
    if valid.mean() < 0.5 or valid.sum() < 400:
        return None
    z = tile.astype(np.float64, copy=True)
    z[~valid] = np.nanmedian(tile)
    # Pixels near NoData would see artificial edges - exclude them.
    safe = focal.min_filter(valid.astype(np.uint8), focal.odd(2 * border + 1)) > 0
    return z, safe


def _count_spikes(mask_1d, rel, jump):
    """Count raised runs that rise AND fall abruptly (both ends sharp).

    A building or crown has a steep edge on both sides; an embankment next
    to a canal has a vertical wall on one side only and is not counted."""
    if not mask_1d.any():
        return 0
    m = np.concatenate(([0], mask_1d.astype(np.int8), [0]))
    starts = np.flatnonzero(np.diff(m) == 1)          # first raised sample
    ends = np.flatnonzero(np.diff(m) == -1) - 1        # last raised sample
    n = len(rel)
    left = rel[starts] - rel[np.maximum(starts - 1, 0)]
    right = rel[ends] - rel[np.minimum(ends + 1, n - 1)]
    inside = (starts > 0) & (ends < n - 1)
    return int(np.count_nonzero(inside & (left > jump) & (right > jump)))


def _resolution_cap(res_m):
    for lim, cap in ((2, 97), (5, 94), (10, 90), (20, 85), (35, 78), (100, 65)):
        if res_m <= lim:
            return cap
    return 58


def _robust_skew(x):
    """Quantile (Bowley-type) skewness using the 5/50/95 percentiles."""
    p5, p50, p95 = np.percentile(x, [5, 50, 95])
    spread = p95 - p5
    return 0.0 if spread <= 1e-9 else float((p95 + p5 - 2 * p50) / spread)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def analyse(tiles, dx_m, dy_m, meta_texts=None, params=None,
            reference_tiles=None, landcover_tiles=None, progress=None,
            is_cancelled=None, keep_rasters=False, info=None, block_tiles=None):
    """Run the full diagnostic.

    tiles            : list of 2-D float arrays (NaN = NoData), elevations in m
    dx_m, dy_m       : pixel size in metres
    meta_texts       : {source: text} for the naming/metadata indicator
    reference_tiles  : optional list of co-registered tiles of a 2nd layer
    block_tiles      : optional list of block windows (adaptive grid) for the
                       block-level inspection (one array per grid cell)
    landcover_tiles  : optional list (aligned with `tiles`) of dicts
                       {"bands": {"red","green","blue"[,"nir"]}, "nir": bool,
                        "bldg": building mask or None} from an orthophoto
    progress(pct,msg): optional callback
    is_cancelled()   : optional callback -> bool
    keep_rasters     : return per-tile object-height arrays in result.info
    """
    p = dict(DEFAULTS)
    p.update(params or {})
    info = dict(info or {})
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)

    res = math.sqrt(dx_m * dy_m)
    # Interpolation erases sharp edges only when the real detail is coarse
    # (e.g. 30 m Copernicus).  A LiDAR DTM interpolated from ground points
    # ~3 m apart still shows sharp roofs/crowns if objects were present.
    p["smooth_edges"] = bool(p.get("resampled")) and res >= 10.0
    win = min(focal.odd(p["object_width_m"] / res), p["max_window_px"])
    ctx = focal.odd(p["context_m"] / res, minimum=5)
    border = max(win, ctx) // 2 + 1

    # ---------------- pass 1: prepare tiles + noise --------------------
    progress(5, "Preparing tiles")
    prepared, block_stds, flat_px, safe_px = [], [], 0, 0
    for idx, t in enumerate(tiles):
        prep = _prepare(t, border)
        if prep is None:
            continue
        z, safe = prep
        r3 = z - focal.box_mean(z, 3)
        block_stds.append(focal.block_std(r3, safe, 8))
        prepared.append((idx, z, safe))
        zs0 = focal.box_mean(z, ctx)
        rr0 = focal.max_filter(zs0, ctx) - focal.min_filter(zs0, ctx)
        flat_px += int(((rr0 < math.tan(math.radians(p["flat_slope_deg"])) * ctx * res) & safe).sum())
        safe_px += int(safe.sum())
    if not prepared:
        raise ValueError("No usable data: every sampled tile was mostly NoData.")

    # Coarse pixels (e.g. 30 m Copernicus) on FLAT land: the resolvable objects are
    # whole villages / tree clusters (100-300 m) that stand out as abrupt humps
    # on a floodplain.  On rolling terrain such humps are ordinary hills, so this
    # mode is used only when the land is predominantly flat.
    coarse_flat = res >= 5 and flat_px / max(safe_px, 1) >= 0.6 and p.get("coarse_window", True)
    if coarse_flat:
        win = min(focal.odd(max(p["object_width_m"], 7 * res) / res), p["max_window_px"])
        border = max(win, ctx) // 2 + 1
    p["coarse_flat"] = bool(coarse_flat)

    stds = np.concatenate(block_stds) if block_stds else np.array([])
    sigma = float(np.percentile(stds, 10) / 0.943) if stds.size else 0.0
    sigma = max(sigma, 0.02)
    h_thr = max(p["min_object_height_m"], p["noise_k"] * sigma)

    theta_obj = math.degrees(math.atan(p["min_object_height_m"] / (2 * res)))
    # The steepest drop to 8 neighbours of pure Gaussian noise averages ~1.4
    # sigma, so the noise-safe threshold is ~5.5 sigma per pixel.
    theta_noise = math.degrees(math.atan(5.5 * sigma / res))
    # 10 deg floor suits fine data; a 5 m canopy step across a 30 m pixel is ~9 deg
    # 10 deg floor suits fine data; a 5 m canopy step across a 30 m pixel is ~9 deg
    theta = min(max(theta_obj, 4.0 if coarse_flat else 10.0, theta_noise), 60.0)

    # ---------------- pass 2: per-tile measurements --------------------
    coarse_ms = bool(p.get("coarse_flat") or p.get("smooth_edges") or res >= 10)
    tile_px = min(min(z.shape) for _, z, _ in prepared)
    # 0.7 m bumps are meaningful on LiDAR; on noisy / coarse data they are noise
    ms_low_h = max(0.7, 4.0 * sigma)
    ms_full_h = max(4.5, 5.0 * sigma)        # "collapsed like a tree / house"
    ms_acc = multiscale.new_accumulator(
        multiscale.scale_ladder(res, tile_px, coarse_ms, p.get("ms_scales_m"))) \
        if p.get("multiscale", True) else None
    acc = dict(n_valid=0, n_flat=0, n_obj=0, n_steep=0, prof_km=0.0, spikes=0,
               th_sample=[], lc_dh={"veg": [], "bldg": [], "open": []},
               lc_n={"veg": 0, "bldg": 0, "open": 0}, lc_top=[], lc_index=set(),
               sl_sum=0.0, sl_sq=0.0, blobs=0, cand_blobs=0, blob_area=[], blob_h=[], resid=[], obj_heights=[])
    kept = []
    rng = np.random.default_rng(42)
    blob_min_px = max(1, int(math.ceil(4.0 / (dx_m * dy_m))))
    blob_max_m2 = 20000.0

    for i, (tile_idx, z, safe) in enumerate(prepared):
        if is_cancelled():
            raise Cancelled()
        progress(10 + int(70 * i / len(prepared)),
                 "Analysing tile {}/{}".format(i + 1, len(prepared)))

        # Flat context: the SMOOTHED surface must stay within a gentle-slope
        # envelope across the whole context window.  This excludes hilltops,
        # ridges and valley floors whose curvature mimics objects.
        zs = focal.box_mean(z, ctx)
        rng_ctx = focal.max_filter(zs, ctx) - focal.min_filter(zs, ctx)
        flat_relief = math.tan(math.radians(p["flat_slope_deg"])) * ctx * res
        flat = (rng_ctx < flat_relief) & safe

        # Height above a robust local ground (pseudo nDSM)
        th = z - focal.median_ground(z, win)

        # Multi-scale filter stability (progressive morphological opening)
        if ms_acc is not None:
            multiscale.tile_stats(z, safe, flat, th, res, ms_acc, full_h=ms_full_h, low_h=ms_low_h)

        # Edge sharpness: steepest drop to any of the 8 neighbours.  Unlike a
        # central-difference slope this also "sees" 1-pixel spikes.  Only drops
        # from RAISED pixels count, so ditch/channel banks are ignored.
        # Computed on the detrended surface so regional slope is not counted.
        drop = np.degrees(np.arctan((th - focal.min_filter(th, 3)) / res))
        steep = (drop > theta) & (th > 0.5 * h_thr) & flat
        sl_flat = focal.slope_degrees(z, dx_m, dy_m)[flat]      # for slope std-dev
        acc["sl_sum"] += float(sl_flat.sum())
        acc["sl_sq"] += float((sl_flat ** 2).sum())

        # Candidate objects = raised pixels; keep only blobs bounded by sharp
        # edges (buildings, crowns), not smooth terrain swells.
        cand = (th > h_thr) & flat
        lab, n = focal.label_blobs(cand)
        obj = np.zeros_like(cand)
        if n:
            perim = cand & ~(focal.min_filter(cand.astype(np.uint8), 3) > 0)
            n_perim = np.bincount(lab[perim], minlength=n + 1)[1:]
            n_sharp = np.bincount(lab[steep & cand], minlength=n + 1)[1:]
            sharp = n_sharp >= np.maximum(1, 0.25 * n_perim)
            counts, hmax = focal.blob_stats(lab, n, th)
            # Buildings and crowns are compact; embankments, levees and canal
            # banks are long and thin -> terrain, not objects.
            compact = focal.blob_elongation(lab, n, counts) < 8.0
            sharp &= compact
            # Interpolated (resampled) data has no sharp edges left, even in a
            # DSM: there, any compact raised feature counts as an object.
            keep_ids = np.flatnonzero(compact if p["smooth_edges"] else sharp) + 1
            obj = np.isin(lab, keep_ids)
            area = counts * dx_m * dy_m
            sized = (counts >= blob_min_px) & (area <= blob_max_m2) & compact
            ok = sharp & sized
            acc["cand_blobs"] += int(sized.sum())
            acc["blobs"] += int(ok.sum())
            acc["blob_area"].append(area[ok])
            acc["blob_h"].append(hmax[ok])

        # Residual asymmetry (elevation minus focal mean)
        resid = (z - focal.box_mean(z, win))[flat]
        if resid.size > 40000:
            resid = rng.choice(resid, 40000, replace=False)

        # Profile spikes along sampled rows and columns
        nr, nc = z.shape
        for axis, step_m, n_lines in ((0, dx_m, nr), (1, dy_m, nc)):
            stride = max(1, n_lines // 12)
            jump = math.tan(math.radians(theta)) * step_m
            for k in range(stride // 2, n_lines, stride):
                prof = z[k, :] if axis == 0 else z[:, k]
                fl = flat[k, :] if axis == 0 else flat[:, k]
                if fl.sum() < 10:
                    continue
                rel = prof - focal.running_median_1d(prof, win)
                acc["spikes"] += _count_spikes((rel > h_thr) & fl, rel,
                                               jump * (0.5 if p["smooth_edges"] else 1.0))
                acc["prof_km"] += fl.sum() * step_m / 1000.0

        th_f = th[flat]
        if th_f.size > 20000:
            th_f = rng.choice(th_f, 20000, replace=False)
        acc["th_sample"].append(th_f)

        # Indicator 8: land-cover-conditioned height (orthophoto)
        lct = landcover_tiles[tile_idx] if landcover_tiles else None
        if lct is not None:
            _landcover_tile(z, safe, lct, win, acc, rng, res)

        acc["n_valid"] += int(safe.sum())
        acc["n_flat"] += int(flat.sum())
        acc["n_obj"] += int(obj.sum())
        acc["n_steep"] += int(steep.sum())
        acc["resid"].append(resid)
        if obj.any():
            acc["obj_heights"].append(th[obj])

        if keep_rasters:
            out = np.where(safe, th, np.nan).astype(np.float32)
            item = {"tile_index": tile_idx, "object_height": out,
                    "object_mask": obj.astype(np.uint8)}
            if p.get("ms_export_m"):
                px = focal.odd(p["ms_export_m"] / res, minimum=3)
                opened = focal.max_filter(focal.min_filter(z, px), px)
                item["filtered"] = np.where(safe, opened, np.nan).astype(np.float32)
                item["filter_dz"] = np.where(safe, z - opened, np.nan).astype(np.float32)
                item["filter_m"] = round(px * res, 2)
            kept.append(item)

    progress(85, "Scoring indicators")

    # ---------------- derived statistics -------------------------------
    n_flat = acc["n_flat"]
    flat_frac = n_flat / max(acc["n_valid"], 1)
    flat_km2 = n_flat * dx_m * dy_m / 1e6
    obj_frac = acc["n_obj"] / max(n_flat, 1)
    steep_frac = acc["n_steep"] / max(n_flat, 1)
    sl_mean = acc["sl_sum"] / max(n_flat, 1)
    sl_std = math.sqrt(max(acc["sl_sq"] / max(n_flat, 1) - sl_mean ** 2, 0.0))
    resid_all = np.concatenate(acc["resid"]) if acc["resid"] else np.array([])
    skew = _robust_skew(resid_all) if resid_all.size > 500 else 0.0
    spike_density = acc["spikes"] / acc["prof_km"] if acc["prof_km"] > 0 else 0.0
    blob_density = acc["blobs"] / flat_km2 if flat_km2 > 0 else 0.0
    blob_area = np.concatenate(acc["blob_area"]) if acc["blob_area"] else np.array([])
    blob_h = np.concatenate(acc["blob_h"]) if acc["blob_h"] else np.array([])
    obj_h = np.concatenate(acc["obj_heights"]) if acc["obj_heights"] else np.array([])

    # Reliability of the data-driven indicators
    detect = float(np.clip((60.0 - res) / 45.0, 0.25, 1.0))  # objects resolvable?
    support = float(np.clip(n_flat / 20000.0, 0.2, 1.0)      # enough flat pixels?
                    * np.clip(flat_frac / 0.15, 0.2, 1.0))
    w_data = detect * support

    indicators = []

    # ---- 1. Metadata / naming ----------------------------------------
    meta = evaluate_metadata(meta_texts or {})
    if meta["weight"] > 0:
        txt = meta["product"] or (meta["hits"][0].split(": ", 1)[-1] if meta["hits"] else "-")
        finding = "; ".join(meta["hits"][:3])
    else:
        txt, finding = "No product or keyword found", "Name/metadata gives no evidence either way."
    indicators.append(Indicator(
        "metadata", "Metadata & naming", "Does the product name or metadata identify the model type?",
        meta["score"], txt, meta["score"], meta["weight"], finding,
        "Product registry (Copernicus, AW3D30, SRTM, FABDEM, 3DEP...) and DSM/DTM keywords "
        "searched in file path, layer name, band description and GDAL metadata."))

    # ---- 2. Height above robust local ground ----------------------------
    th_all = np.concatenate(acc["th_sample"]) if acc["th_sample"] else np.array([0.0])
    th999 = float(np.percentile(th_all, 99.9))
    s2 = _squash(obj_frac, 0.015, 0.02)
    p95h = float(np.percentile(obj_h, 95)) if obj_h.size else 0.0
    indicators.append(Indicator(
        "height", "Local height above ground",
        "Do narrow features rise above the local ground envelope?",
        obj_frac, "{:.2f} % of flat area > {:.1f} m".format(100 * obj_frac, h_thr),
        s2, 1.0 * w_data,
        ("Above-ground objects cover {:.1f}% of flat terrain (95th pct height {:.1f} m)."
         .format(100 * obj_frac, p95h) if acc["n_obj"] else
         "No sharp features rise more than {:.1f} m above the local ground. Highest "
         "residual bumps: {:.2f} m (99.9th pct).".format(h_thr, th999)),
        "Elevation minus a robust local ground (block medians of {} px = {:.0f} m, "
        "interpolated) in flat context; threshold = max(min height, {:.0f} x noise "
        "sigma).".format(win, win * res, p["noise_k"])))

    # ---- 3. Steep-slope fraction in flat terrain ----------------------
    s3 = _squash(steep_frac, 0.01, 0.015)
    indicators.append(Indicator(
        "slope", "Slope variance (steep edges)",
        "Are there wall-like slopes where the regional terrain is flat?",
        steep_frac, "{:.2f} % of flat area > {:.0f} deg".format(100 * steep_frac, theta),
        s3, 0.8 * w_data,
        "{:.2f}% of flat-context pixels drop more steeply than {:.0f} deg from a raised "
        "edge (walls, canopy edges). Slope std-dev in flat terrain: {:.1f} deg."
        .format(100 * steep_frac, theta, sl_std),
        "Steepest drop to the 8 neighbours on the detrended surface, counted only on raised "
        "pixels so ditch and channel banks are ignored; threshold adapts to pixel size and "
        "noise ({} px context window).".format(ctx)))

    # ---- 4. Residual asymmetry -----------------------------------------
    s4 = _squash(skew, 0.04, 0.10)
    indicators.append(Indicator(
        "skew", "Surface roughness asymmetry",
        "Is micro-relief biased upward (objects) rather than symmetric (terrain/noise)?",
        skew, "skew = {:+.3f}".format(skew), s4, 0.3 * w_data if resid_all.size > 500 else 0.0,
        ("Residuals are skewed upward - protruding objects dominate." if skew > 0.1 else
         "Residuals are skewed downward - incised channels/ditches dominate (bare-earth pattern)."
         if skew < -0.05 else "Residuals are roughly symmetric (terrain or sensor noise)."),
        "Quantile skewness of elevation minus {:.0f} m focal mean, flat context only. "
        "Low weight: bunds, levees and embankments also skew it upward."
        .format(win * res)))

    # ---- 5. Profile spikes ---------------------------------------------
    s5 = _squash(spike_density, 0.4, 0.6)
    indicators.append(Indicator(
        "profile", "Profile spikes",
        "Do cross-section profiles show abrupt up-and-down spikes?",
        spike_density, "{:.2f} spikes / km ({:.1f} km sampled)".format(spike_density, acc["prof_km"]),
        s5, 0.8 * w_data if acc["prof_km"] > 0.5 else 0.0,
        "{} spikes higher than {:.1f} m and narrower than {:.0f} m along {:.1f} km of "
        "flat profiles.".format(acc["spikes"], h_thr, win * res, acc["prof_km"]),
        "Automatic transects every ~1/12 of each tile (rows and columns); profile minus "
        "its running median."))

    # ---- 6. Object footprints ------------------------------------------
    # Share of raised, object-sized features that have SHARP edges.  Terrain
    # swells and levees rise gradually; roofs and crowns rise abruptly.
    n_cand = acc["cand_blobs"]
    sharp_ratio = acc["blobs"] / n_cand if n_cand else 0.0
    s6 = _squash(sharp_ratio, 0.5, 0.12) if n_cand else 0.0
    cand_density = n_cand / flat_km2 if flat_km2 > 0 else 0.0
    if blob_area.size:
        bf = ("{} of {} raised features are sharp-edged ({:.1f} objects / km2); median "
              "footprint {:.0f} m2, median height {:.1f} m.".format(
                  acc["blobs"], n_cand, blob_density, float(np.median(blob_area)),
                  float(np.median(blob_h))))
    else:
        bf = "No building- or tree-sized footprints with sharp edges were found."
    if p["smooth_edges"]:
        # Edges were smoothed away by interpolation: score how MANY compact,
        # object-sized raised features exist (bare floodplain: almost none).
        s6 = _squash(cand_density, 1.0, 1.5)
        indicators.append(Indicator(
            "footprint", "Structure / vegetation footprints",
            "Are there many compact, building- or crown-sized raised features?",
            cand_density, "{:.2f} raised features / km2 ({})".format(cand_density, n_cand), s6,
            0.8 * w_data if flat_km2 > 0.5 else 0.0,
            "{} compact raised features of object size ({:.2f} per km2 of flat terrain). "
            "Edge sharpness is not used because this raster was interpolated from coarser "
            "data.".format(n_cand, cand_density),
            "Connected-component labelling of raised pixels; long thin features (levees, "
            "embankments) excluded."))
    else:
        indicators.append(Indicator(
            "footprint", "Structure / vegetation footprints",
            "Do raised, building- or crown-sized blobs have sharp (vertical) edges?",
            sharp_ratio, "{:.0f} % sharp ({} of {})".format(100 * sharp_ratio, acc["blobs"], n_cand),
            s6, 0.8 * w_data * min(1.0, n_cand / 30.0), bf,
            "Connected-component labelling (8-neighbour) of raised pixels; a footprint counts "
            "as sharp when >= 25% of its outline drops steeper than the edge threshold. "
            "Sizes {:.0f}-{:.0f} m2.".format(blob_min_px * dx_m * dy_m, blob_max_m2)))

    # ---- 7. Multi-scale filter stability --------------------------------
    ms, ms_partial = None, False
    if ms_acc is not None:
        ms = multiscale.summarise(ms_acc, res, full_h=ms_full_h, low_h=ms_low_h, coarse=coarse_ms)
    if ms is not None and ms["n_peaks"] >= 10:
        scales_txt = ", ".join("{:g}".format(c["scale_m"]) for c in ms["curve"])
        if ms["full_pct"] >= 10:
            f_txt = ("{:.0f}% of the highest points vanish by 4.5 m or more once the filter "
                     "window reaches ~{:g} m - tree crowns / houses (DSM)."
                     .format(ms["full_pct"], ms["median_collapse_scale_m"] or 0))
        elif ms["low_pct"] >= 25 and not coarse_ms:
            f_txt = ("{:.0f}% of the highest points drop by only 0.7-4.5 m when filtered: small "
                     "compact bumps - residual objects (partially filtered DTM) or raised house "
                     "plots.".format(ms["low_pct"]))
        else:
            f_txt = ("{:.0f}% of the highest points stay in place (drop < 0.5 m) and none "
                     "collapse like trees or houses - terrain (DEM).".format(ms["stable_pct"]))
        indicators.append(Indicator(
            "multiscale", "Multi-scale filter stability",
            "Do the highest points collapse when the surface is filtered at growing scales?",
            ms["full_pct"] / 100.0,
            "{:.0f}% collapse >{:.1f} m, {:.0f}% drop {:.1f}-{:.1f} m, {:.0f}% stable"
            .format(ms["full_pct"], ms_full_h, ms["low_pct"], ms_low_h, ms_full_h,
                    ms["stable_pct"]),
            _squash(ms["full_pct"], 8.0, 6.0), 0.9 * max(w_data, 0.4),
            f_txt + " ({} peaks, filter windows {} m.)".format(ms["n_peaks"], scales_txt),
            "Progressive morphological opening (erosion + dilation) with square windows of "
            "increasing size; for each of the highest local points the elevation drop is "
            "tracked across scales. Only compact removed regions count (embankments and "
            "levees excluded)."))
        # partial-filtering hint only where small bumps are real (fine, quiet data)
        ms_partial = (not coarse_ms) and ms["low_pct"] >= 25 and ms["full_pct"] < 5

        info["multiscale"] = ms

    # Full-height test (fine data).  Real houses stand 4-8 m and village trees
    # 8-18 m.  Raised features of only 1-4.5 m are AMBIGUOUS: raised house
    # plots, mounds and embankment ends (ground) or residual objects in a
    # partially filtered DTM.  They must not count as full DSM evidence.
    low_objects = False
    if res <= 5 and acc["n_obj"] and p95h < 4.5:
        hf = float(np.clip((p95h - 2.0) / 2.5, 0.15, 1.0))
        for ind in indicators:
            if ind.key in ("height", "slope", "profile", "footprint") and ind.score > 0:
                ind.score *= hf
        low_objects = True

    # Absence of objects is weaker evidence when noise forces a high
    # detection threshold: shrink negative (bare-earth) scores accordingly.
    absence = float(np.clip(2.0 * p["min_object_height_m"] / h_thr, 0.3, 1.0))
    if absence < 1.0:
        for ind in indicators:
            if ind.key in ("height", "slope", "profile") and ind.score < 0:
                ind.score *= absence

    # ---------------- 7. optional pairwise check ----------------------
    notes, caps = list(meta["warnings"]), []
    if ms is not None:
        notes.extend("Filter test: " + w for w in ms.get("warnings", []))
    elif ms_acc is not None and not ms_acc["ladder"] and p.get("ms_scales_m"):
        notes.append("Filter test skipped: none of the windows ({}) fits this data - each "
                     "window must span at least 1.5 pixels ({:.1f} m) and at most ~{:.0f} m here."
                     .format(", ".join("{:g}".format(s) for s in p["ms_scales_m"]), 1.5 * res,
                             min(81, tile_px // 4) * res))
    pair = None
    if reference_tiles is not None:
        progress(90, "Pairwise comparison")
        pair = _pairwise(tiles, reference_tiles, h_thr)
        if pair is not None:
            indicators.append(pair["indicator"])
            notes.extend(pair["notes"])

    # ---------------- 8. optional orthophoto land-cover test ------------
    lc_ind = None
    if landcover_tiles is not None:
        progress(92, "Orthophoto land-cover test")
        lc_ind, lc_notes = _landcover_indicator(acc, sigma, res)
        if lc_ind is not None:
            indicators.append(lc_ind)
            notes.extend(lc_notes)
        else:
            notes.append("Orthophoto supplied, but too few vegetation/open-ground pixels "
                         "overlapped the sampled elevation tiles to run the land-cover test.")

    # ---------------- 9. optional block-grid consensus -------------------
    block_results, block_summary, residual_flag = [], None, False
    if block_tiles:
        for bi, bt in enumerate(block_tiles):
            if is_cancelled():
                raise Cancelled()
            if bi % 10 == 0:
                progress(93, "Inspecting block {}/{}".format(bi + 1, len(block_tiles)))
            block_results.append(blockmod.analyse_block(bt, sigma, h_thr, theta, res, win, ctx, p))
        block_summary = blockmod.consensus(block_results)
        n_ev = block_summary["evaluable"]
        if n_ev:
            fo = block_summary["frac_objects"]
            c = block_summary["counts"]
            indicators.append(Indicator(
                "blocks", "Block-grid consensus",
                "How many grid blocks contain clear off-ground objects?",
                fo, "{} of {} blocks with objects ({:.0f}%)".format(c["OBJECTS"], n_ev, 100 * fo),
                _squash(fo, 0.06, 0.05), 1.2 * min(1.0, n_ev / 16.0) * max(w_data, 0.4),
                "{} blocks OBJECTS, {} MIXED, {} BARE, {} not evaluable. A bare-earth DEM "
                "gives ~0% object blocks; a DSM of farmland may show objects only in "
                "village / tree blocks, so even 10-20% is strong evidence."
                .format(c["OBJECTS"], c["MIXED"], c["BARE"], c["N/A"]),
                "Adaptive N x N grid; each block scored on dZ max, slope discontinuity, focal "
                "SD, profile spikes and raised-island continuity (Moran)."))
            pk = block_summary["peaks"]
            if pk["n"] >= 4:
                indicators.append(Indicator(
                    "peaks", "Highest cells (max-cell check)",
                    "Does the highest cell of each block stand far above the ground beside it?",
                    pk["tall_pct"] / 100.0,
                    "{:.0f}% of block maxima > 4.5 m above nearby ground (median {:.1f} m)"
                    .format(pk["tall_pct"], pk["median_dz"]),
                    _squash(pk["tall_pct"], 10.0, 8.0), 1.0 * max(w_data, 0.4),
                    "Highest cell of each of {} blocks, compared with the ground within "
                    "~15 m: median {:.1f} m, 90th pct {:.1f} m. Tree tops and roofs stand "
                    "5-15 m above it; tops of raised house plots, mounds and embankments only "
                    "~0-3 m. The maxima sit on: {:.0f}% linear ridges (dikes / bunds / road "
                    "or canal embankments), {:.0f}% broad tops (house plots / mounds), {:.0f}% "
                    "compact peaks (trees / roofs / heaps).{}".format(
                        pk["n"], pk["median_dz"], pk["p90_dz"],
                        pk["shape_pct"].get("linear", 0), pk["shape_pct"].get("broad", 0),
                        pk["shape_pct"].get("compact", 0),
                        " When the highest point of most blocks is a dike or plot rather than "
                        "a tree or roof, the surface carries no full-height objects - a "
                        "bare-earth pattern." if pk["shape_pct"].get("linear", 0)
                        + pk["shape_pct"].get("broad", 0) >= 60 and pk["tall_pct"] < 10 else ""),
                    "Per block: argmax of elevation; ground = median of cells within "
                    "15 m. Points are added to the map ('Highest cells') for visual checking "
                    "against the orthophoto."))
            if n_ev >= 6 and block_summary["frac_residual"] >= 0.15 and fo < 0.03:
                ortho_bare = lc_ind is not None and lc_ind.weight >= 1.0 and lc_ind.score <= -0.8
                bldg_checked = acc["lc_n"]["bldg"] >= 50
                if ortho_bare and bldg_checked:
                    notes.append(
                        "{} of {} blocks contain small (0.5-{:.1f} m) bumps, but the orthophoto "
                        "shows trees AND buildings at the level of the ground beside them - the "
                        "bumps are small ground features (dike ends, heaps, termite mounds), not "
                        "remains of objects.".format(block_summary["residual_blocks"], n_ev, h_thr))
                elif ortho_bare:
                    notes.append(
                        "{} of {} blocks contain small (0.5-{:.1f} m) bumps. The orthophoto shows "
                        "the TREES are at ground level; roofs were not checked because no "
                        "building footprints were given (Orthophoto > Buildings). Add footprints "
                        "(e.g. Google Open Buildings / OSM) to rule out leftover house bumps."
                        .format(block_summary["residual_blocks"], n_ev, h_thr))
                else:
                    residual_flag = True
                    notes.append(
                        "{} of {} blocks contain many low (0.5-{:.1f} m), sharp-edged bumps but "
                        "no full-height objects - the signature of a PARTIALLY filtered DTM "
                        "(trees / houses only partly removed). Confirm with the orthophoto check."
                        .format(block_summary["residual_blocks"], n_ev, h_thr))

    # ---------------- quality notes -------------------------------------
    all_vals = np.concatenate([t[np.isfinite(t)].ravel()[::7] for t in tiles if np.isfinite(t).any()])
    if all_vals.size:
        med = float(np.median(all_vals))
        near0 = float(np.mean(np.abs(all_vals) < 0.5))
        if near0 > 0.3 and np.percentile(all_vals, 95) < 80 and med < 2:
            notes.append("Values look like heights above ground (nDSM/CHM): {:.0f}% of pixels "
                         "are ~0 m. Such a layer is neither a DEM nor a DSM.".format(100 * near0))
    if sigma > 1.5:
        notes.append("High sensor noise (sigma ~{:.1f} m) masks small objects; typical of "
                     "SRTM/ASTER-class data.".format(sigma))
    if flat_frac < 0.05:
        notes.append("Only {:.1f}% of the sample is flat; mountainous terrain hides objects "
                     "from the detectors.".format(100 * flat_frac))

    # ---------------- fusion ---------------------------------------------
    wsum = sum(i.weight for i in indicators)
    S = sum(i.weight * i.score for i in indicators) / wsum if wsum > 0 else 0.0
    conf = 50.0 + 50.0 * abs(S)

    data_inds = [i for i in indicators if i.key not in ("metadata",) and i.weight > 0]
    agree = sum(1 for i in data_inds if abs(i.score) >= 0.3 and np.sign(i.score) == np.sign(S))
    disagree = sum(1 for i in data_inds if abs(i.score) >= 0.3 and np.sign(i.score) != np.sign(S))

    def cap(limit, reason):
        nonlocal conf
        if conf > limit:
            conf = float(limit)
            caps.append(reason)

    # ---- decisive evidence: a second layer, or the orthophoto test --------
    # Both observe the objects directly, so they settle the verdict on their
    # own; elevation-only indicators then only modulate the confidence.
    decisive = None
    if pair is not None and abs(pair["indicator"].score) >= 0.8 \
            and pair["reference"].get("differ_pct", 0) >= 2.0:
        decisive = pair["indicator"]
    elif lc_ind is not None and lc_ind.weight >= 1.0 and abs(lc_ind.score) >= 0.8:
        decisive = lc_ind

    if decisive is not None:
        lean = "DSM" if decisive.score > 0 else "DEM"
        classification = lean
        if decisive.key == "pairwise":
            conf = min(98.0, 90.0 + pair["reference"]["differ_pct"] / 4.0)
            if lc_ind is not None and abs(lc_ind.score) >= 0.5 \
                    and np.sign(lc_ind.score) != np.sign(decisive.score):
                conf = min(conf, 80.0)
                caps.append("The orthophoto test disagrees with the two-layer comparison "
                            "(check dates and alignment).")
        else:
            conf = min(_resolution_cap(res), 95.0)
        elev = [i for i in indicators if i.key in ("height", "slope", "profile", "footprint")
                and i.weight > 0 and abs(i.score) >= 0.5]
        contra = sum(1 for i in elev if np.sign(i.score) != np.sign(decisive.score))
        if contra >= 2:
            conf -= 7.0
            caps.append("{} elevation-only indicators disagree with the {} - worth a visual "
                        "check with the object-height layers.".format(
                            contra, "two-layer comparison" if decisive.key == "pairwise"
                            else "orthophoto test"))
    else:
        cap(_resolution_cap(res),
            "Ceiling for single-layer analysis of {:.1f} m pixels: {}% (add an orthophoto "
            "or a reference layer for more).".format(res, _resolution_cap(res)))
        if flat_frac < 0.05:
            cap(60, "Only {:.1f}% flat terrain - too little for reliable object detection "
                    "(try a flatter extent or the 2-layer check).".format(100 * flat_frac))
        elif flat_frac < 0.15:
            cap(75, "Limited flat terrain ({:.0f}%) for object detection.".format(100 * flat_frac))
        if S < 0 and residual_flag:
            cap(60, "Many blocks show low residual bumps (see note below).")
        if S < 0:
            cap(92 if meta["score"] <= -0.75 else 85,
                "A DEM verdict rests on ABSENCE of objects; a DSM of bare land looks the "
                "same (add an orthophoto to prove where trees/roofs are).")
        if ms_partial and S <= 0:
            cap(60, "Multi-scale filtering: {:.0f}% of the highest points are small compact "
                    "bumps of 0.7-4.5 m - residual objects of a partially filtered DTM or raised "
                    "house plots. Run the orthophoto check to tell which."
                .format(ms["low_pct"]))
        if low_objects and obj_frac >= 0.003 and S <= 0:
            cap(60, "{:.1f}% of the flat area carries compact raised features only ~{:.1f} m "
                    "tall: raised house plots / mounds (ground) or residual objects of a "
                    "partially filtered DTM. Neither DEM nor DSM can be confirmed without the "
                    "orthophoto check.".format(100 * obj_frac, p95h))
        if S > 0 and acc["n_obj"] and (p95h < 2.5 or low_objects):
            cap(60, "The raised features found are only ~{:.1f} m tall (95th pct). Full-height "
                    "houses (4-8 m) and trees (8-18 m) would be taller. Such low features are "
                    "either GROUND (raised house plots, mounds) or residual objects of a "
                    "partially filtered DTM - only the orthophoto check can tell which."
                .format(p95h))
        if disagree >= 2:
            cap(70, "{} indicators contradict the majority.".format(disagree))
        if lc_ind is not None and abs(lc_ind.score) >= 0.5 and np.sign(lc_ind.score) != np.sign(S):
            cap(70, "The orthophoto land-cover test contradicts the elevation-only indicators "
                    "(check orthophoto date and alignment).")
        if agree < 3:
            cap(75, "Fewer than 3 data indicators give clear evidence.")
        if pair is not None and abs(pair["indicator"].score) >= 0.5 \
                and np.sign(pair["indicator"].score) != np.sign(S):
            cap(80, "Pairwise comparison disagrees with the single-layer evidence.")
        lean = "DSM" if S > 0 else "DEM"
        classification = lean if (conf >= 62 and abs(S) >= 0.15) else "INCONCLUSIVE"
    conf = float(min(conf, 99.0))

    # Third outcome, only provable with an orthophoto: objects are present but
    # stand well below their real height -> a partially filtered DTM.
    if lc_ind is not None and lc_ind.weight >= 1.0 and 0.5 <= lc_ind.value < 2.0:
        classification = "PARTIAL"
        lean = "DSM" if lc_ind.value >= 1.25 else "DEM"
        conf = float(min(_resolution_cap(res), 80.0 if 0.7 <= lc_ind.value <= 1.8 else 65.0))
        caps.append("Trees/roofs are raised {:.1f} m above nearby open ground - too little for "
                    "a DSM, too much for a clean bare-earth DEM.".format(lc_ind.value))

    info.update({
        "pixel_size_m": round(res, 3),
        "tiles_used": len(prepared),
        "pixels_analysed": acc["n_valid"],
        "flat_context_pct": round(100 * flat_frac, 1),
        "noise_sigma_m": round(sigma, 3),
        "object_height_threshold_m": round(h_thr, 2),
        "steep_slope_threshold_deg": round(theta, 1),
        "object_window_px": win,
        "context_window_px": ctx,
        "coarse_flat_mode": bool(coarse_flat),
        "agreeing_indicators": agree,
        "raised_features": acc["cand_blobs"],
        "sharp_features": acc["blobs"],
        "contradicting_indicators": disagree,
    })
    if keep_rasters:
        info["_rasters"] = kept
    if block_summary is not None:
        info["_blocks"] = block_results
        info["block_summary"] = block_summary
        # exact thresholds so the inspector can re-analyse a block identically
        info["block_context"] = {"sigma": sigma, "h_thr": h_thr, "theta": theta, "res": res,
                                 "win": win, "ctx": ctx, "params": p}

    progress(100, "Done")
    return Result(classification, lean, conf, S, indicators, notes, caps, info,
                  pair["reference"] if pair else {})


# --------------------------------------------------------------------------
# Land-cover-conditioned height test (orthophoto)
# --------------------------------------------------------------------------
def _landcover_tile(z, safe, lct, win, acc, rng, res):
    """Height of tree / roof pixels above the OPEN ground right next to them."""
    lc = lct["masks"] if "masks" in lct else \
        landcover.classify(lct["bands"], lct.get("nir", False), lct.get("bldg"), res)
    acc["lc_texture"] = acc.get("lc_texture", True) and lc["texture_used"]
    acc["lc_crop"] = acc.get("lc_crop", 0) + int(lc["crop"].sum())
    acc["lc_index"].add("{} > {:.2f}".format(lc["index_name"], lc["threshold"]))
    # Erode every class by 1 px so slight ortho/DEM misregistration and mixed
    # edge pixels do not contaminate the comparison.
    er = {k: (focal.min_filter(lc[k].astype(np.uint8), 3) > 0) & safe
          for k in ("veg", "bldg", "open")}
    if er["open"].sum() < 200:
        return
    # Ground = open pixels within ~15 m: the yard on the same raised house plot
    # or mound, not the paddy field below it.  A house on a 2 m plot is then
    # ~0 m above its ground in a DEM and its full height in a DSM.
    near = max(3, int(round(15.0 / res)))
    ground = landcover.masked_ground(z, er["open"], near)
    if ground is None:
        return
    dh = z - ground
    # Only judge trees/roofs that have open ground close by (forest edges,
    # scattered trees, village yards); deep inside a large forest the
    # ground estimate would be extrapolated from far away.
    near_open = focal.box_mean(er["open"].astype(np.float64), focal.odd(3 * near)) >= 0.15
    er["veg"] &= near_open
    er["bldg"] &= near_open
    for k in ("veg", "bldg", "open"):
        v = dh[er[k]]
        acc["lc_n"][k] += int(v.size)
        if v.size > 50000:
            v = rng.choice(v, 50000, replace=False)
        acc["lc_dh"][k].append(v)
    # Where are the locally highest cells?  (class codes: 0 open, 1 veg, 2 bldg)
    code = np.full(z.shape, -1, np.int8)
    code[er["open"]], code[er["veg"]], code[er["bldg"]] = 0, 1, 2
    m = code >= 0
    pairs = np.stack([dh[m], code[m].astype(np.float64), z[m]], axis=1)
    if len(pairs) > 60000:
        pairs = pairs[rng.choice(len(pairs), 60000, replace=False)]
    acc["lc_top"].append(pairs)


def _landcover_indicator(acc, sigma, res):
    """Build indicator 8 from accumulated land-cover statistics (or None)."""
    n = acc["lc_n"]
    if n["veg"] + n["bldg"] < 100 or n["open"] < 500:
        return None, []
    med = {k: float(np.median(np.concatenate(acc["lc_dh"][k]))) if n[k] >= 50 else None
           for k in ("veg", "bldg", "open")}
    base = med["open"] or 0.0
    contrast = {k: med[k] - base for k in ("veg", "bldg") if med[k] is not None}
    cls, c = max(contrast.items(), key=lambda kv: kv[1])
    score = _squash(c, 1.0, 0.7)
    n_obj = n["veg"] + n["bldg"]
    weight = 2.0 * min(1.0, n_obj / 2000.0)
    # Without crown texture (or NIR) green crops can mix into the "tree" class
    # and pull its median towards 0 m: then the test may support, not decide.
    rgb_only = not any(s.startswith("NDVI") for s in acc["lc_index"])
    if rgb_only and not acc.get("lc_texture", True) and n["bldg"] < 50:
        weight = min(weight, 0.8)

    # Share of the top-1% locally-highest cells that fall on trees/roofs,
    # compared with the share of the area those classes occupy.
    pairs = np.concatenate(acc["lc_top"])
    top = pairs[pairs[:, 0] >= np.percentile(pairs[:, 0], 99)]
    share_top = float(np.mean(top[:, 1] > 0)) if len(top) else 0.0
    share_area = float(np.mean(pairs[:, 1] > 0))
    top_raw = pairs[pairs[:, 2] >= np.percentile(pairs[:, 2], 99)]
    share_raw = float(np.mean(top_raw[:, 1] > 0)) if len(top_raw) else 0.0

    parts = []
    if "veg" in contrast:
        parts.append("trees {:+.2f} m".format(contrast["veg"]))
    if "bldg" in contrast:
        parts.append("buildings {:+.2f} m".format(contrast["bldg"]))
    value_text = ", ".join(parts) + " vs open ground"
    label = "tree" if cls == "veg" else "building"
    if c >= 2.0:
        finding = ("{} pixels stand {:.1f} m above the adjacent open ground - the surface "
                   "includes canopy/roofs (DSM).".format(label.capitalize(), c))
    elif c >= 0.5:
        finding = ("{} pixels are only {:.1f} m above adjacent open ground: objects were "
                   "partly removed or smoothed (partially filtered DTM, or low vegetation)."
                   .format(label.capitalize(), c))
    else:
        finding = ("Trees and roofs seen in the orthophoto are NOT raised above the adjacent "
                   "open ground ({:+.2f} m) - bare-earth surface (DEM).".format(c))
    finding += (" Trees/roofs cover {:.0f}% of the area but hold {:.0f}% of the top-1% "
                "highest cells ({:.0f}% of the top-1% cells above local ground)."
                .format(100 * share_area, 100 * share_raw, 100 * share_top))

    notes = []
    if not acc.get("lc_texture", True):
        notes.append("At this pixel size the orthophoto cannot separate tree crowns from green "
                     "crops; in rice areas prefer dry-season imagery or add building footprints.")
    elif acc.get("lc_crop", 0) > 0:
        finding += (" Smooth green areas (rice, grass, crops: {:.0f}% of vegetation) were "
                    "treated as ground level.".format(
                        100 * acc["lc_crop"] / max(acc["lc_crop"] + n["veg"], 1)))
    if 0.5 <= c < 2.0:
        notes.append("Orthophoto test: trees/roofs stand {:.1f} m above nearby open ground. "
                     "This is typical of a PARTIALLY filtered DTM (residual vegetation or "
                     "houses) rather than a clean DEM or a full DSM.".format(c))
    if max(share_raw, share_top) > 2 * share_area and c < 0.5:
        notes.append("The highest cells fall mostly on trees/houses, yet those pixels are not "
                     "raised above the open ground right next to them. That pattern means the "
                     "settlements and trees sit on naturally higher ground (levees, mounds, "
                     "raised house plots) - it is not evidence of a DSM.")
    ind = Indicator(
        "landcover", "Orthophoto land-cover height test",
        "Do trees / roofs from the orthophoto stand above the open ground next to them?",
        c, value_text, score, weight, finding,
        "Vegetation from {} (Otsu threshold) on the orthophoto resampled to the elevation "
        "grid; trees separated from smooth crops by crown texture{}; classes eroded by "
        "1 px; local ground = OPEN pixels within ~15 m (yard on the same plot / mound). "
        "Median height of each class above that ground, minus the open-ground median."
        .format("/".join(sorted(acc["lc_index"])) or "the orthophoto",
                " and building footprints" if n["bldg"] else ""))
    return ind, notes


# --------------------------------------------------------------------------
# Pairwise (two-layer) check
# --------------------------------------------------------------------------
def _pairwise(tiles_a, tiles_b, h_thr):
    """Compare layer A with a co-registered layer B (B resampled to A's grid)."""
    diffs = []
    for a, b in zip(tiles_a, tiles_b):
        if b is None or a.shape != b.shape:
            continue
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() > 100:
            diffs.append((a[m] - b[m]).astype(np.float64))
    if not diffs:
        return None
    d = np.concatenate(diffs)

    # Mode of the difference = vertical offset over open ground (datum/bias).
    # Coarse histogram peak, then refined with the median of nearby values.
    clipped = d[np.abs(d) < 100]
    if clipped.size < 100:
        offset = float(np.median(d))
    else:
        hist, edges = np.histogram(clipped, bins=np.arange(-100, 100.05, 0.05))
        hist = np.convolve(hist, np.ones(5) / 5, mode="same")
        peak = float(edges[np.argmax(hist)] + 0.025)
        near = clipped[np.abs(clipped - peak) < 0.5]
        offset = float(np.median(near)) if near.size else peak
    dc = d - offset
    pos = float(np.mean(dc > h_thr))
    neg = float(np.mean(dc < -h_thr))
    differ = pos + neg
    asym = (pos - neg) / (differ + 1e-3)
    score = float(np.clip(asym * min(1.0, differ / 0.02), -1, 1))

    notes = []
    if abs(offset) > 1.0:
        notes.append("Layers differ by a constant {:+.1f} m over open ground - check that "
                     "both use the same vertical datum (EGM96 / EGM2008 / ellipsoid).".format(offset))
    if differ < 0.005:
        notes.append("The two layers are nearly identical; the pairwise check cannot separate them.")

    if score > 0.25:
        finding = "This layer is higher than the reference on {:.1f}% of pixels and lower on {:.1f}% " \
                  "-> this layer carries the objects (DSM).".format(100 * pos, 100 * neg)
    elif score < -0.25:
        finding = "The reference is higher on {:.1f}% of pixels -> the reference carries the objects; " \
                  "this layer is the bare-earth one (DEM).".format(100 * neg)
    else:
        finding = "No consistent height difference between the layers."

    ind = Indicator(
        "pairwise", "Pairwise nDSM check (2 layers)",
        "Is this layer systematically higher than the reference where objects stand?",
        score, "+{:.1f}% / -{:.1f}% (offset {:+.2f} m)".format(100 * pos, 100 * neg, offset),
        score, 2.0 if differ >= 0.005 else 0.3, finding,
        "nDSM = this - reference on a common grid; open-ground offset removed via the "
        "histogram mode; |difference| > {:.1f} m counted.".format(h_thr))
    ref = {
        "classification": "DEM" if score > 0.25 else ("DSM" if score < -0.25 else "INCONCLUSIVE"),
        "higher_pct": round(100 * neg, 2),
        "lower_pct": round(100 * pos, 2),
        "offset_m": round(offset, 2),
        "median_diff_m": round(float(np.median(d)), 2),
        "differ_pct": round(100 * differ, 2),
    }
    return {"indicator": ind, "notes": notes, "reference": ref}

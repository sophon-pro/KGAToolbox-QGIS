# -*- coding: utf-8 -*-
"""Reservoir Storage Analyzer: the calculations.

Elevation-area-volume (H-A-V) tables from a DEM, the curve figure, the
inundation map sheet, volume-to-level lookup, depth rasters and a
contours-to-DEM interpolator. Pure numpy / scipy / matplotlib / GDAL: nothing
here touches Qt or the QGIS API, so it runs (and is tested) headless. The
window in ``gui/reservoir_storage_dialog.py`` is the only caller.

Everything is calculated in m, m2 and m3. The saved CSV holds the volume in
m3, 1,000 m3 and million m3; "volume unit" only changes how curves and maps
are labelled.

  - Flooded area = cells below the water level that connect to the lowest
    cell of the DEM. Isolated pits are not counted.
  - Excavated surface: ground inside the flooded area is lowered to the base
    level; the soil removed is reported separately.
  - Smoothing changes volumes slightly; keep it off for reported figures.

Long loops take ``stop``, a callable returning True once the user asked to
cancel, and raise ``Cancelled`` so the window can tell a stop from a failure.
"""

import os
import math
import time
from contextlib import contextmanager, suppress
from functools import wraps
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter, label as nd_label

from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap, LightSource, Normalize
from matplotlib.cm import ScalarMappable
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.patheffects import withStroke
import matplotlib.ticker as mticker
from osgeo import gdal, ogr, osr


MAX_LEVELS = 2000          # a level is a full-grid pass; more is a typo


class Cancelled(Exception):
    """Raised inside a worker when the user pressed Stop."""


def _check(stop):
    if stop is not None and stop():
        raise Cancelled()


VOLUME_UNITS = {
    "Mm3":    {"factor": 1e6, "label": "million m³", "combo": "Mm³  (million m³)"},
    "1000m3": {"factor": 1e3, "label": "1,000 m³",   "combo": "1,000 m³"},
    "m3":     {"factor": 1.0, "label": "m³",         "combo": "m³"},
}
UNIT_KEYS = list(VOLUME_UNITS)


def fmt_volume(v_m3, unit_key):
    """Format a volume (given in m³) in the chosen unit, with the unit."""
    u = VOLUME_UNITS[unit_key]
    v = v_m3 / u["factor"]
    if unit_key == "Mm3":
        return f"{v:,.3f} {u['label']}"
    return f"{v:,.1f} {u['label']}" if unit_key == "1000m3" else f"{v:,.0f} m³"


def smooth_dem(arr, sigma):
    """Gaussian smoothing that ignores NaN (no edge drop at the boundary)."""
    if sigma <= 0:
        return arr.copy()
    nan = np.isnan(arr)
    filled = np.where(nan, 0.0, arr)
    num = gaussian_filter(filled, sigma=sigma)
    den = gaussian_filter((~nan).astype(float), sigma=sigma)
    out = num / np.where(den < 1e-6, 1.0, den)
    out[nan] = np.nan
    return out


def find_seed(arr):
    """Deepest valid pixel = reservoir bottom. Returns (row, col) or None."""
    if not np.any(~np.isnan(arr)):
        return None
    return np.unravel_index(int(np.nanargmin(arr)), arr.shape)


def flooded_zone(arr, wl, seed):
    """Pixels below water level that are connected to the seed pixel."""
    with np.errstate(invalid="ignore"):
        wet = arr < wl                      # NaN compares False
    if seed is None or not wet[seed]:
        return np.zeros(arr.shape, dtype=bool)
    lab, _ = nd_label(wet)
    return lab == lab[seed]


def level_stats(arr, wl, seed, px_area, excavation=False, base=None):
    """
    Storage at one water level.

    Natural    : water = Σ (WL − ground) over the flooded zone
    Excavation : ground inside the flooded zone is cut down to `base`
                 bed    = min(ground, base)
                 water  = Σ max(WL − bed, 0)
                 soil   = Σ max(ground − base, 0)   (material removed)
    """
    zone = flooded_zone(arr, wl, seed)
    ground = arr[zone]
    natural = float(np.sum(wl - ground) * px_area)
    if excavation and base is not None:
        bed = np.minimum(ground, base)
        water = float(np.sum(np.maximum(wl - bed, 0.0)) * px_area)
        soil = float(np.sum(np.maximum(ground - base, 0.0)) * px_area)
    else:
        water, soil = natural, 0.0
    return {"zone": zone,
            "area_m2": float(zone.sum() * px_area),
            "volume_m3": water,
            "natural_m3": natural,
            "soil_m3": soil}


def build_table(arr, levels, px_area, excavation=False, base=None,
                progress=None, stop=None):
    """Elevation–Area–Volume table for a list of water levels."""
    seed = find_seed(arr)
    rows = []
    for i, wl in enumerate(levels):
        _check(stop)
        s = level_stats(arr, float(wl), seed, px_area, excavation, base)
        row = {
            "Water_Level_m": round(float(wl), 4),
            "Area_m2":       round(s["area_m2"], 2),
            "Area_ha":       round(s["area_m2"] / 1e4, 4),
            "Area_km2":      round(s["area_m2"] / 1e6, 6),
            "Volume_m3":     round(s["volume_m3"], 2),
            "Volume_1000m3": round(s["volume_m3"] / 1e3, 4),
            "Volume_Mm3":    round(s["volume_m3"] / 1e6, 6),
        }
        if excavation:
            row["Natural_Vol_m3"] = round(s["natural_m3"], 2)
            row["Excavated_Soil_m3"] = round(s["soil_m3"], 2)
        rows.append(row)
        if progress:
            progress(int((i + 1) / len(levels) * 100))
    return pd.DataFrame(rows)


def volume_m3_from_df(df):
    """Volume in m³ from a table/CSV of this tool or the older storage tool."""
    for col, f in [("Volume_m3", 1), ("Total_Vol_m3", 1),
                   ("Volume_Mm3", 1e6), ("Total_Vol_Mm3", 1e6),
                   ("Natural_Vol_m3", 1)]:
        if col in df.columns:
            return df[col].to_numpy(dtype=float) * f
    raise ValueError("No volume column found in the table.")


def level_for_volume(df, target_m3):
    """Interpolate water level (and area) for a target volume in m³."""
    df = df.sort_values("Water_Level_m")
    vols = np.maximum.accumulate(volume_m3_from_df(df))
    lev = df["Water_Level_m"].to_numpy(dtype=float)
    if target_m3 < vols[0] or target_m3 > vols[-1]:
        return {"error": f"Outside table range "
                         f"({vols[0]:,.0f} – {vols[-1]:,.0f} m³)"}
    # use the last level with each volume so flat zero rows don't bias
    keep = np.r_[np.diff(vols) > 0, True]
    v, lv = vols[keep], lev[keep]
    if len(v) == 1:
        elev = float(lv[0])
    else:
        elev = float(np.interp(target_m3, v, lv))
    out = {"elevation": elev, "volume_m3": float(target_m3)}
    if "Area_ha" in df.columns:
        out["area_ha"] = float(np.interp(elev, lev,
                                         df["Area_ha"].to_numpy(dtype=float)))
    return out


def make_levels(vmin, vmax, step):
    if step <= 0 or vmin >= vmax:
        raise ValueError("Need Min < Max and Step > 0.")
    if (vmax - vmin) / step > MAX_LEVELS:
        raise ValueError(f"That is more than {MAX_LEVELS:,} levels. "
                         "Raise the step or narrow From/To.")
    vals = np.arange(vmin, vmax + step * 0.5, step)
    return np.round(vals[vals <= vmax + 1e-9], 4)


def parse_levels(text):
    vals = [float(v) for v in text.replace(";", ",").split(",") if v.strip()]
    if len(vals) > MAX_LEVELS:
        raise ValueError(f"That is more than {MAX_LEVELS:,} levels.")
    return sorted(set(round(v, 4) for v in vals))


# ── H-V / H-A curve figure ───────────────────────────────────────────────

def draw_curves(fig, df, unit_key="Mm3", title="Storage Capacity Curve",
                subtitle=""):
    """Elevation–volume and elevation–area curves, report style."""
    fig.clear()
    u = VOLUME_UNITS[unit_key]
    elev = df["Water_Level_m"].to_numpy(dtype=float)
    vol = df["Volume_m3"].to_numpy(dtype=float) / u["factor"]
    area = df["Area_ha"].to_numpy(dtype=float)

    ax1, ax2 = fig.subplots(1, 2, sharey=True)
    fig.subplots_adjust(left=0.08, right=0.97, top=0.85, bottom=0.16,
                        wspace=0.08)
    marks = list(range(math.ceil(elev.min()), math.floor(elev.max()) + 1))
    vtop = max(float(np.nanmax(vol)), 1e-9)
    dec = int(min(3, max(0, 1 - math.floor(math.log10(vtop)))))

    for ax, data, xl, ttl, fmt in [
        (ax1, vol, f"Volume ({u['label']})", "Elevation – Volume",
         lambda x, _: f"{x:,.{dec}f}"),
        (ax2, area, "Area (ha)", "Elevation – Area", lambda x, _: f"{x:,.0f}"),
    ]:
        ax.plot(data, elev, color="black", lw=1.4, zorder=3)
        if marks:
            ax.plot(np.interp(marks, elev, data), marks, "o", ms=3.5,
                    mfc="white", mec="black", mew=1.0, zorder=4)
        ax.set_xlim(left=0)
        ax.set_ylim(elev.min(), elev.max())
        ax.set_title(ttl, fontsize=10)
        ax.set_xlabel(xl, fontsize=9)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt))
        ax.grid(True, color="#d0d0d0", lw=0.5)
        ax.set_axisbelow(True)
        ax.tick_params(labelsize=8, direction="in", top=True, right=True)

    ax1.set_ylabel("Water level (m)", fontsize=9)
    if "Natural_Vol_m3" in df.columns:
        ax1.plot(df["Natural_Vol_m3"] / u["factor"], elev, "--",
                 color="#555555", lw=1.0, label="Natural ground")
        ax1.plot(df["Excavated_Soil_m3"] / u["factor"], elev, ":",
                 color="#555555", lw=1.2, label="Soil removed")
        ax1.plot([], [], color="black", lw=1.4, label="Storage (excavated)")
        ax1.legend(fontsize=7.5, loc="lower right", frameon=False)

    vmax_m3 = float(df["Volume_m3"].iloc[-1])
    ax1.text(0.03, 0.97, f"{fmt_volume(vmax_m3, unit_key)} at {elev[-1]:.2f} m",
             transform=ax1.transAxes, fontsize=8, va="top")
    ax2.text(0.03, 0.97, f"{area[-1]:,.2f} ha at {elev[-1]:.2f} m",
             transform=ax2.transAxes, fontsize=8, va="top")

    fig.suptitle(title, fontsize=11, fontweight="bold", y=0.97)
    if subtitle:
        fig.text(0.5, 0.905, subtitle, ha="center", fontsize=8, color="#444444")
    fig.text(0.08, 0.025,
             f"Volume axis: 1 unit = {u['factor']:,.0f} m³.  Circles mark "
             f"whole-metre levels.  Max storage {vmax_m3:,.0f} m³.",
             fontsize=7.5, color="#444444")
    return fig


# ── Inundation maps ──────────────────────────────────────────────────────

def _contour_segments(xs, ys, grid, levels, min_len):
    """Contour lines as vertex arrays, dropping pieces shorter than min_len."""
    scratch = Figure()
    ax = scratch.add_subplot()
    cs = ax.contour(xs, ys, np.ma.masked_invalid(grid), levels=levels)
    out = []
    for segs in cs.allsegs:
        for s in segs:
            if len(s) < 2:
                continue
            if np.sum(np.hypot(*np.diff(s, axis=0).T)) >= min_len:
                out.append(s)
    return out


PAGE_SIZES_MM = {            # portrait width × height
    "A4": (210, 297), "A3": (297, 420), "A2": (420, 594),
    "A1": (594, 841), "A0": (841, 1189),
    "Letter": (215.9, 279.4), "Tabloid": (279.4, 431.8),
}


def page_inches(name, landscape=True):
    """Page size in inches, or None for 'Fit to content'."""
    if name not in PAGE_SIZES_MM:
        return None
    w, h = (v / 25.4 for v in PAGE_SIZES_MM[name])
    return (max(w, h), min(w, h)) if landscape else (min(w, h), max(w, h))


def _nice_length(span):
    """Round scale-bar length close to a fifth of the map width."""
    target = span / 5.0
    p = 10 ** math.floor(math.log10(target))
    for m in (1, 2, 2.5, 5, 10):
        if m * p >= target:
            return m * p
    return 10 * p


def _inner_ticks(lo, hi, n=4):
    """Round tick values at least 12 % of the span away from each edge."""
    span = hi - lo
    a, b = lo + 0.12 * span, hi - 0.12 * span
    return [t for t in mticker.MaxNLocator(n).tick_values(a, b) if a <= t <= b]


def _scale_bar(ax, extent, k):
    x0, x1, y0, y1 = extent
    length = _nice_length(x1 - x0)
    h = (y1 - y0) * 0.012
    bx = x0 + (x1 - x0) * 0.04
    by = y0 + (y1 - y0) * 0.05
    half = length / 2
    halo = [withStroke(linewidth=2 * k, foreground="white")]
    for i, fc in enumerate(("black", "white")):
        ax.add_patch(Rectangle((bx + i * half, by), half, h, fc=fc,
                               ec="black", lw=0.6 * k, zorder=8))
    unit, div = ("km", 1000.0) if length >= 1000 else ("m", 1.0)
    for i, v in enumerate((0, half, length)):
        ax.text(bx + v, by + h * 1.8,
                f"{v / div:g}" + (f" {unit}" if i == 2 else ""),
                ha="center", va="bottom", fontsize=6.5 * k, zorder=8,
                path_effects=halo)


def _north_arrow(ax, k):
    x, y = 0.95, 0.88
    ax.annotate("", xy=(x, y + 0.03), xytext=(x, y - 0.06),
                xycoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", lw=1.0 * k, color="black",
                                mutation_scale=9 * k), zorder=8)
    ax.text(x, y + 0.04, "N", transform=ax.transAxes, ha="center",
            va="bottom", fontsize=8 * k, fontweight="bold", zorder=8,
            path_effects=[withStroke(linewidth=2 * k, foreground="white")])


def render_inundation_maps(arr, arr_draw, gt, levels, px_area,
                           excavation=False, base=None, unit_key="Mm3",
                           contour_interval=0.5, min_len=50.0, n_cols=0,
                           title="Inundation Extent", meta=None,
                           page=None, dpi=300, out_path=None,
                           preview_path=None, log=print, stop=None):
    """
    Map sheet: one panel per water level on a hillshade, shared depth
    legend, scale bar, north arrow, summary table and footer.

    arr       DEM used for the numbers (same as the calculator)
    arr_draw  DEM used for hillshade/contours only (may be smoothed)
    page      (width_in, height_in) for a fixed paper size, or None to fit
    dpi       resolution of raster output (PNG/TIFF/JPG)
    """
    meta = meta or {}
    cell = abs(gt[1])
    seed = find_seed(arr)
    stats = []
    for wl in levels:
        _check(stop)
        stats.append(level_stats(arr, wl, seed, px_area, excavation, base))

    # ── map window: largest flooded extent + margin, same for all panels ─
    union = np.zeros(arr.shape, dtype=bool)
    for s in stats:
        union |= s["zone"]
    if not union.any():
        union = ~np.isnan(arr)
    rr, cc = np.where(union)
    # view = flooded box + equal margin on every side, even past the DEM edge
    ux0, ux1 = gt[0] + cc.min() * gt[1], gt[0] + (cc.max() + 1) * gt[1]
    uy1, uy0 = gt[3] + rr.min() * gt[5], gt[3] + (rr.max() + 1) * gt[5]
    margin = 0.06 * max(ux1 - ux0, uy1 - uy0)
    view = [ux0 - margin, ux1 + margin, uy0 - margin, uy1 + margin]
    pad = int(math.ceil(margin / abs(gt[1]))) + 1
    r0, r1 = max(rr.min() - pad, 0), min(rr.max() + pad + 1, arr.shape[0])
    c0, c1 = max(cc.min() - pad, 0), min(cc.max() + pad + 1, arr.shape[1])
    a = arr[r0:r1, c0:c1]
    ad = arr_draw[r0:r1, c0:c1]
    x0, y0 = gt[0] + c0 * gt[1], gt[3] + r0 * gt[5]
    extent = [x0, x0 + (c1 - c0) * gt[1], y0 + (r1 - r0) * gt[5], y0]
    xs = x0 + (np.arange(c1 - c0) + 0.5) * gt[1]
    ys = y0 + (np.arange(r1 - r0) + 0.5) * gt[5]

    # ── page geometry ────────────────────────────────────────────────────
    # Everything is designed for A4 and multiplied by k, so a bigger page
    # gets proportionally bigger text, lines and margins.
    n = len(levels)
    W, H = view[1] - view[0], view[3] - view[2]
    aspect = H / W
    k = 1.0 if page is None else min(page) / 8.27
    M, ylab, xlab, G = 0.45 * k, 0.32 * k, 0.26 * k, 0.12 * k
    header, after_grid = 0.80 * k, 0.30 * k
    legend_h, table_gap = 0.62 * k, 0.16 * k
    row_in = 0.22 * k
    table_h = row_in * (n + 1.5)
    footer_h = 0.30 * k
    fixed_h = header + xlab + after_grid + legend_h + table_gap + table_h \
        + footer_h

    def geometry(nc):
        nr = math.ceil(n / nc)
        if page is None:                     # fit: grid 9 in wide
            pw = (9.0 - (nc - 1) * G) / nc
            ph = pw * aspect
            max_ph = 6.0 if nr == 1 else 9.0 / nr
            if ph > max_ph:
                ph = max_ph
                pw = ph / aspect
        else:                                # fixed page: largest that fits
            avail_w = page[0] - 2 * M - ylab
            avail_h = page[1] - 2 * M - fixed_h
            pw = min((avail_w - (nc - 1) * G) / nc,
                     (avail_h - (nr - 1) * G) / nr / aspect)
            ph = pw * aspect
        gw = nc * pw + (nc - 1) * G
        gh = nr * ph + (nr - 1) * G
        if page is None:
            fw = max(ylab + gw, 6.8) + 2 * M
            fh = 2 * M + fixed_h + gh
        else:
            fw, fh = page
        return nr, pw, ph, gw, gh, fw, fh

    cands = range(1, min(n, 4) + 1)
    if n_cols and n_cols > 0:
        nc = min(n_cols, n)
    elif page is None:                       # closest to a landscape sheet
        nc = min(cands, key=lambda c: abs(math.log(
            geometry(c)[5] / geometry(c)[6] / 1.414))
            + 0.25 * (geometry(c)[0] * c - n))
    else:                                    # biggest panels on this page
        nc = max(cands, key=lambda c: geometry(c)[1]
                 - 0.02 * (geometry(c)[0] * c - n))
    nr, pw, ph, gw, gh, FW, FH = geometry(nc)
    if pw <= 0.5:
        raise ValueError("The page is too small for this many panels. "
                         "Choose a bigger page or fewer water levels.")

    # ── display layers at print resolution (numbers stay full-res) ──────
    step = max(1, int(min((c1 - c0) / (pw * dpi), (r1 - r0) / (ph * dpi))))
    if step > 1:
        log(f"  Drawing at 1/{step} of DEM resolution (table uses full DEM)")
    ad_s = ad[::step, ::step]
    xs_s, ys_s = xs[::step], ys[::step]
    log("  Hillshade and contours …")
    if np.all(np.isnan(ad_s)):
        raise ValueError("Nothing to draw: the area has no valid DEM cells.")
    filled = np.where(np.isnan(ad_s), np.nanmean(ad_s), ad_s)
    hs = LightSource(azdeg=315, altdeg=40).hillshade(
        filled, vert_exag=1, dx=cell * step, dy=cell * step)
    hs = np.where(np.isnan(ad_s), np.nan, hs)
    zmin, zmax = np.nanmin(ad_s), np.nanmax(ad_s)
    ci = contour_interval
    if (zmax - zmin) / ci > 300:            # unreadable and slow past this
        ci = math.ceil((zmax - zmin) / 300 / 0.5) * 0.5
        log(f"  Contour interval raised to {ci:g} m (relief is too large "
            "for the one asked for)")
    contours = _contour_segments(
        xs_s, ys_s, ad_s,
        np.arange(math.floor(zmin / ci) * ci, zmax + ci, ci), min_len)

    # outline of the DEM data (padded so edges at the array border close)
    dx, dy = gt[1] * step, gt[5] * step
    valid = np.pad((~np.isnan(ad_s)).astype(float), 1)
    xs_p = np.r_[xs_s[0] - dx, xs_s, xs_s[-1] + dx]
    ys_p = np.r_[ys_s[0] - dy, ys_s, ys_s[-1] + dy]
    dem_edge = _contour_segments(xs_p, ys_p, valid, [0.5], 0)

    depths, zones = [], []
    for wl, s in zip(levels, stats):
        z = s["zone"][r0:r1, c0:c1]
        bed = np.minimum(a, base) if (excavation and base is not None) else a
        d = np.where(z, np.maximum(wl - bed, 0.0), np.nan)
        depths.append(d)                    # full-res, for max depth
        zones.append(z[::step, ::step])
    dmax = max([float(np.nanmax(d)) for d in depths if np.any(~np.isnan(d))]
               or [1.0])
    dmax = max(dmax, 0.01)                  # a zero-range colour bar is invalid
    depth_cmap = LinearSegmentedColormap.from_list(
        "depth", ["#c6dbef", "#6baed6", "#2171b5", "#08306b"])

    # table/legend/footer need ~6.8 in (scaled); a narrow grid is centred
    block_w = max(ylab + gw, min(FW - 2 * M, 6.8 * k))
    block_h = fixed_h + gh
    tx = (FW - block_w) / 2                  # left edge of content block
    ty = (FH - block_h) / 2                  # top edge of content block
    gx0 = tx + ylab + (block_w - ylab - gw) / 2
    fig = Figure(figsize=(FW, FH), facecolor="white")
    FigureCanvasAgg(fig)

    def box(x, y_top, w, h):
        return [x / FW, 1 - (y_top + h) / FH, w / FW, h / FH]

    def at(x, y_top):
        return x / FW, 1 - y_top / FH

    font = {"fontfamily": "DejaVu Sans"}
    fig.text(*at(tx, ty + 0.30 * k), title, fontsize=13 * k,
             fontweight="bold", va="baseline", **font)
    surface = (f"Excavated surface, base level {base:.2f} m"
               if excavation and base is not None else "Natural ground surface")
    fig.text(*at(tx, ty + 0.56 * k), surface, fontsize=9 * k,
             color="#444444", va="baseline", **font)

    letters = "abcdefghijklmnopqrstuvwxyz"
    gy0 = ty + header
    nx_t = 4 if pw > 3.2 * k else 3
    ny_t = 4 if ph > 3.2 * k else 3
    for i, (wl, s, dep) in enumerate(zip(levels, stats, depths)):
        r, c = divmod(i, nc)
        x = gx0 + c * (pw + G)
        y = gy0 + r * (ph + G)
        ax = fig.add_axes(box(x, y, pw, ph))
        ax.set_facecolor("white")            # outside the DEM stays blank
        ax.imshow(hs, extent=extent, cmap="gray", vmin=-0.9, vmax=1.25,
                  interpolation="bilinear", zorder=1)
        for seg in contours:
            ax.plot(seg[:, 0], seg[:, 1], color="#8c8c8c", lw=0.3 * k,
                    alpha=0.6, zorder=2)
        ax.imshow(dep[::step, ::step], extent=extent, cmap=depth_cmap,
                  vmin=0, vmax=dmax,
                  interpolation="nearest", alpha=0.9, zorder=3)
        z = zones[i]
        if z.any():
            for seg in _contour_segments(xs_s, ys_s, z.astype(float), [0.5], 0):
                ax.plot(seg[:, 0], seg[:, 1], color="#08306b", lw=0.8 * k,
                        zorder=4)
        for seg in dem_edge:                 # DEM coverage outline
            ax.plot(seg[:, 0], seg[:, 1], color="#7a7a7a", lw=0.5 * k,
                    ls=(0, (4, 2)), zorder=5)
        ax.set_xlim(view[0], view[1])
        ax.set_ylim(view[2], view[3])
        ax.set_aspect("auto")                # the box already has map ratio
        ax.set_xticks(_inner_ticks(view[0], view[1], nx_t))
        ax.set_yticks(_inner_ticks(view[2], view[3], ny_t))
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        has_below = (r + 1) * nc + c < n
        ax.tick_params(labelsize=6.5 * k, direction="in", length=3 * k,
                       width=0.6 * k, pad=2 * k, top=True, right=True,
                       labelbottom=not has_below, labelleft=(c == 0))
        for lbl in ax.get_yticklabels():
            lbl.set_rotation(90)
            lbl.set_va("center")
        for sp in ax.spines.values():
            sp.set_linewidth(0.8 * k)
        ax.text(0.012, 0.985, f"({letters[i % 26]})  {wl:.2f} m",
                transform=ax.transAxes, ha="left", va="top",
                fontsize=8.5 * k, fontweight="bold", zorder=9, **font,
                bbox=dict(boxstyle="square,pad=0.35", fc="white", ec="black",
                          lw=0.6 * k))
        if i == 0:                           # one scale bar and arrow is enough
            _scale_bar(ax, view, k)
            _north_arrow(ax, k)

    # ── legend row (aligned with the content block) ──────────────────────
    y_leg = gy0 + gh + xlab + after_grid
    cax = fig.add_axes(box(tx, y_leg + 0.08 * k, min(2.6 * k, block_w * 0.45),
                           0.13 * k))
    cb = fig.colorbar(ScalarMappable(Normalize(0, dmax), depth_cmap),
                      cax=cax, orientation="horizontal")
    cb.set_label("Water depth (m)", fontsize=8 * k, labelpad=2 * k)
    cb.ax.tick_params(labelsize=7 * k, width=0.6 * k, length=3 * k,
                      pad=2 * k)
    cb.outline.set_linewidth(0.6 * k)
    handles = [Line2D([], [], color="#08306b", lw=1.0 * k,
                      label="Flooded extent"),
               Line2D([], [], color="#8c8c8c", lw=0.6 * k,
                      label=f"Contours ({ci:g} m)")]
    if dem_edge:
        handles.append(Line2D([], [], color="#7a7a7a", lw=0.6 * k,
                              ls=(0, (4, 2)), label="DEM coverage"))
    fig.legend(handles=handles, loc="upper right", frameon=False,
               fontsize=8 * k, handlelength=2.0, labelspacing=0.3,
               borderpad=0, borderaxespad=0,
               bbox_to_anchor=at(tx + block_w, y_leg))

    # ── summary table (full content width) ───────────────────────────────
    y_tab = y_leg + legend_h + table_gap
    tax = fig.add_axes(box(tx, y_tab, block_w, table_h))
    tax.axis("off")
    unit_lbl = VOLUME_UNITS[unit_key]["label"]
    cols = ["Panel", "Water level (m)", "Area (ha)", f"Storage ({unit_lbl})",
            "Max. depth (m)"]
    align = ["left", "right", "right", "right", "right"]
    xpos = [0.0, 0.34, 0.54, 0.80, 1.0]
    rows_total = n + 1.5
    row_h = 1.0 / rows_total
    for j, t in enumerate(cols):
        tax.text(xpos[j], 1 - 0.75 * row_h, t, ha=align[j], va="center",
                 fontsize=8 * k, fontweight="bold",
                 transform=tax.transAxes, **font)
    for yl, w in ((1.0, 1.0), (1 - 1.5 * row_h, 0.6), (0.0, 1.0)):
        tax.plot([0, 1], [yl, yl], color="black", lw=w * k,
                 transform=tax.transAxes, clip_on=False)
    dec = 3 if unit_key == "Mm3" else (1 if unit_key == "1000m3" else 0)
    for i, (wl, s, dep) in enumerate(zip(levels, stats, depths)):
        md = float(np.nanmax(dep)) if np.any(~np.isnan(dep)) else 0.0
        vals = [f"({letters[i % 26]})", f"{wl:.2f}",
                f"{s['area_m2'] / 1e4:,.2f}",
                f"{s['volume_m3'] / VOLUME_UNITS[unit_key]['factor']:,.{dec}f}",
                f"{md:.2f}"]
        yv = 1 - (i + 2) * row_h
        for j, t in enumerate(vals):
            tax.text(xpos[j], yv, t, ha=align[j], va="center",
                     fontsize=8 * k, transform=tax.transAxes, **font)

    # ── footer ───────────────────────────────────────────────────────────
    src = f"DEM: {meta.get('name', '')}"
    if meta.get("cell"):
        src += f" ({meta['cell']:g} m cells"
        src += f", {meta['crs']})" if meta.get("crs") else ")"
    src += (".  Flooded area = cells below the water level connected to "
            "the reservoir.")
    y_foot = ty + block_h - 0.05 * k
    fig.text(*at(tx, y_foot), src, fontsize=6.5 * k, color="#555555",
             va="baseline", **font)
    fig.text(*at(tx + block_w, y_foot), time.strftime("%d %B %Y"),
             fontsize=6.5 * k, color="#555555", ha="right", va="baseline",
             **font)

    if out_path:
        os.makedirs(Path(out_path).parent, exist_ok=True)
        ext = Path(out_path).suffix.lower()
        kw = {"dpi": dpi, "facecolor": "white"}
        if ext in (".tif", ".tiff"):
            kw["pil_kwargs"] = {"compression": "tiff_lzw"}
        fig.savefig(out_path, **kw)
        px = f", {int(FW * dpi)} × {int(FH * dpi)} px" \
            if ext not in (".pdf", ".svg") else ""
        log(f"  Map saved: {out_path}  ({FW * 25.4:.0f} × {FH * 25.4:.0f} mm"
            f"{px})")
    if preview_path:                          # small copy for the dialog
        fig.savefig(preview_path, dpi=min(150, 2200 / max(FW, FH)),
                    facecolor="white")
    return fig, stats


# ─────────────────────────────────────────────────────────────────────────
#  GDAL helpers
#
#  GDAL exceptions are switched on per call site through `_gdal_raises`, not
#  globally: gdal.UseExceptions() at import would change error handling for
#  every other KGA tool and for QGIS itself.
# ─────────────────────────────────────────────────────────────────────────


@contextmanager
def _exceptions():
    """GDAL/OGR/OSR raise instead of returning None, for one block only.

    Switching exceptions on for the interpreter would change behaviour for
    every other plugin sharing the ``osgeo`` modules.
    """
    mods = (gdal, ogr, osr)
    before = [m.GetUseExceptions() for m in mods]
    for m in mods:
        m.UseExceptions()
    try:
        yield
    finally:
        for m, was in zip(mods, before):
            if not was:
                m.DontUseExceptions()


def _raises(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _exceptions():
            return fn(*args, **kwargs)
    return wrapper


def _srs(wkt_or_srs):
    s = osr.SpatialReference()
    if isinstance(wkt_or_srs, osr.SpatialReference):
        s = wkt_or_srs.Clone()
    else:
        s.ImportFromWkt(wkt_or_srs)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def open_vector(source):
    """Open 'path|layername=x|subset=y' (QGIS style): (datasource, layer).

    The caller must keep the datasource referenced while it uses the layer.
    """
    parts = source.split("|")
    ds = ogr.Open(parts[0])
    if ds is None:
        raise ValueError(f"Cannot open vector: {parts[0]}")
    lyr, subset = None, None
    for p in parts[1:]:
        if p.startswith("layername="):
            lyr = ds.GetLayerByName(p.split("=", 1)[1])
        elif p.startswith("layerid="):
            lyr = ds.GetLayer(int(p.split("=", 1)[1]))
        elif p.startswith("subset="):
            subset = p.split("=", 1)[1]
    lyr = lyr or ds.GetLayer(0)
    if lyr is None:
        raise ValueError(f"No layer found in {parts[0]}")
    if subset:                       # honour the layer's filter in QGIS
        lyr.SetAttributeFilter(subset)
    return ds, lyr


def _mem_vector_driver():
    # GDAL 3.11 renamed the vector driver "Memory" to "MEM"
    for name in ("MEM", "Memory"):
        drv = ogr.GetDriverByName(name)
        if drv is None:
            continue
        with suppress(Exception):
            ds = drv.CreateDataSource("")
            if ds is not None:
                return ds
    raise RuntimeError("No in-memory OGR driver available")


@_raises
def rasterize_mask(source, ref_ds, all_touched=False, log=print):
    """Burn a vector onto the grid of ref_ds. Returns a bool array."""
    vds, lyr = open_vector(source)
    dem_srs = _srs(ref_ds.GetProjection()) if ref_ds.GetProjection() else None
    v_srs = lyr.GetSpatialRef()
    if dem_srs is not None and v_srs is not None and not dem_srs.IsSame(v_srs):
        log("  Vector CRS differs from DEM — reprojecting on the fly")
        v_srs = _srs(v_srs)
        xf = osr.CoordinateTransformation(v_srs, dem_srs)
        mem = _mem_vector_driver()
        out = mem.CreateLayer("r", dem_srs, lyr.GetGeomType())
        for feat in lyr:
            g = feat.GetGeometryRef()
            if g is None:
                continue
            g = g.Clone()
            g.Transform(xf)
            nf = ogr.Feature(out.GetLayerDefn())
            nf.SetGeometry(g)
            out.CreateFeature(nf)
        lyr, vds = out, mem
    mds = gdal.GetDriverByName("MEM").Create(
        "", ref_ds.RasterXSize, ref_ds.RasterYSize, 1, gdal.GDT_Byte)
    mds.SetGeoTransform(ref_ds.GetGeoTransform())
    mds.SetProjection(ref_ds.GetProjection())
    gdal.RasterizeLayer(mds, [1], lyr, burn_values=[1],
                        options=["ALL_TOUCHED=TRUE"] if all_touched else [])
    arr = mds.GetRasterBand(1).ReadAsArray() == 1
    del lyr, vds                    # release the datasource
    return arr


SIDES = ("west", "east", "north", "south")


def side_mask(source, ref_ds, side="west", log=print):
    """Split the grid with a line and keep one part.

    The line is burnt on the grid; the two largest pieces left on either side
    of it are compared by where their centres lie, and the one furthest
    west / east / north / south is kept (plus the line cells themselves).
    """
    line = rasterize_mask(source, ref_ds, all_touched=True, log=log)
    lab, n = nd_label(~line)
    if n < 2:
        log("  Warning: the line does not split the DEM into two parts - "
            "ignored. Extend the line beyond the DEM edges.")
        return np.ones(line.shape, dtype=bool)
    sizes = np.bincount(lab.ravel())[1:]
    order = np.argsort(sizes)[::-1] + 1              # label ids, biggest first
    first, second = int(order[0]), int(order[1])
    if sizes[second - 1] < 0.005 * sizes[first - 1]:
        log("  Warning: the line cuts off almost nothing - ignored. "
            "Extend the line across the whole DEM.")
        return np.ones(line.shape, dtype=bool)

    def centre(label_id):
        rr, cc = np.nonzero(lab == label_id)
        return cc.mean(), rr.mean()                  # x grows east, y grows south

    (x1, y1), (x2, y2) = centre(first), centre(second)
    pick_first = {"west": x1 < x2, "east": x1 > x2,
                  "north": y1 < y2, "south": y1 > y2}[side]
    keep_id = first if pick_first else second
    keep = (lab == keep_id) | line
    log(f"  {side.capitalize()} side kept: {int(keep.sum()):,} px")
    return keep


@_raises
def dem_info(path, exact=False):
    """Size, cell size, projection and (approximate) elevation range."""
    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    lo, hi = band.ComputeRasterMinMax(not exact)
    gt = ds.GetGeoTransform()
    out = {"nx": ds.RasterXSize, "ny": ds.RasterYSize, "cell": abs(gt[1]),
           "min": lo, "max": hi, "projection": ds.GetProjection()}
    ds = None
    return out


@_raises
def load_dem(s, log=print):
    """
    Read, mask and (optionally) smooth the DEM described by settings dict s.
    Returns dict(arr, gt, prj, px_area, cell, crs, name).
    """
    ds = gdal.Open(s["dem"])
    prj = ds.GetProjection()
    if prj:
        srs = _srs(prj)
        if srs.IsGeographic():
            raise ValueError(
                "The DEM is in a geographic CRS (degrees). Volumes would be "
                "wrong.\nReproject it first to a projected CRS in metres, "
                "e.g. WGS 84 / UTM 48N (EPSG:32648) for Cambodia.")
        lin = srs.GetLinearUnits()
        if abs(lin - 1.0) > 1e-6:
            log(f"  Warning: CRS linear unit is {lin:g} m per unit - the "
                "results assume metres. Reproject the DEM to a metric CRS.")
    else:
        log("  Warning: DEM has no CRS — assuming pixel size is in metres.")
    gt = ds.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0:
        raise ValueError("Rotated rasters are not supported. Warp the DEM "
                         "to a north-up grid first.")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float64)
    nd = band.GetNoDataValue()
    if nd is not None:
        arr[arr == nd] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    px_w, px_h = abs(gt[1]), abs(gt[5])
    log(f"  DEM    : {Path(s['dem']).name}  ({arr.shape[1]} × {arr.shape[0]} px,"
        f" {px_w:.3f} × {px_h:.3f} m)")

    if s.get("boundary"):
        m = rasterize_mask(s["boundary"], ds, log=log)
        arr[~m] = np.nan
        log(f"  Boundary applied ({int(m.sum()):,} px inside)")
    if s.get("line"):
        m = side_mask(s["line"], ds, s.get("side", "west"), log=log)
        arr[~m] = np.nan
    ds = None

    if not np.any(~np.isnan(arr)):
        raise ValueError("No valid DEM cells left after masking. "
                         "Check that the boundary overlaps the DEM.")
    # Trim empty NoData margins: everything after this runs on fewer cells
    rows = np.where(np.any(~np.isnan(arr), axis=1))[0]
    cols = np.where(np.any(~np.isnan(arr), axis=0))[0]
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    if (r1 - r0) * (c1 - c0) < arr.size:
        arr = arr[r0:r1, c0:c1].copy()
        gt = (gt[0] + c0 * gt[1], gt[1], 0.0, gt[3] + r0 * gt[5], 0.0, gt[5])
        log(f"  Trimmed to data extent: {arr.shape[1]} × {arr.shape[0]} px")
    if s.get("sigma", 0) > 0:
        log(f"  Smoothing σ = {s['sigma']} px")
        arr = smooth_dem(arr, s["sigma"])
    log(f"  Elev   : {np.nanmin(arr):.3f} – {np.nanmax(arr):.3f} m")
    if np.nanmin(arr) < -500:
        log("  Warning: the lowest cell is below -500 m. A NoData value "
            "(such as -9999 or -32768) may not be declared on this raster; "
            "volumes would be wrong. Set its NoData value first.")
    crs = ""
    if prj:
        sr = _srs(prj)
        code = sr.GetAuthorityCode(None)
        crs = f"{sr.GetAuthorityName(None)}:{code}" if code else sr.GetName()
    return {"arr": arr, "gt": gt, "prj": prj, "px_area": px_w * px_h,
            "cell": px_w, "crs": crs,
            "name": Path(s["dem"].split("|")[0]).stem}


@_raises
def write_geotiff(path, arr, gt, prj, nodata=-9999.0):
    os.makedirs(Path(path).parent, exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES"])
    ds.SetGeoTransform(gt)
    if prj:
        ds.SetProjection(prj)
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isnan(arr), nodata, arr).astype(np.float32))
    b.FlushCache()
    ds = None


def _line_points(geom):
    """Every (x, y) run of a line / polygon-ring geometry, as float arrays."""
    runs = []
    n = geom.GetGeometryCount()
    if n:                                   # multi-part, or a polygon's rings
        for j in range(n):
            runs += _line_points(geom.GetGeometryRef(j))
    elif geom.GetPointCount() >= 2:
        runs.append(np.array(geom.GetPoints(), dtype=float)[:, :2])
    return runs


def _extent_in(boundary, srs):
    """Extent (xmin, xmax, ymin, ymax) of a vector, expressed in `srs`."""
    bds, bl = open_vector(boundary)
    ext = bl.GetExtent()
    b_srs = bl.GetSpatialRef()
    if b_srs is not None and not _srs(b_srs).IsSame(srs):
        xf = osr.CoordinateTransformation(_srs(b_srs), srs)
        corners = [xf.TransformPoint(x, y)[:2]
                   for x in ext[:2] for y in ext[2:]]
        xs, ys = zip(*corners)
        ext = (min(xs), max(xs), min(ys), max(ys))
    del bds
    return ext


@_raises
def contours_to_dem(src, field, res, method, out_path, boundary=None,
                    crs_wkt=None, log=print, progress=lambda p: None,
                    stop=None):
    """Interpolate a DEM from elevation contour lines and save a GeoTIFF."""
    from scipy.interpolate import griddata
    from scipy.spatial import cKDTree

    ds, lyr = open_vector(src)
    srs = crs_wkt if crs_wkt else lyr.GetSpatialRef()
    if srs is None:
        raise ValueError("The contour layer has no CRS.")
    srs = _srs(srs)
    if srs.IsGeographic():
        raise ValueError("Contours are in degrees. Reproject them to a "
                         "metric CRS first.")
    prj = srs.ExportToWkt()

    step = max(res * 0.5, 0.25)
    xs, ys, zs = [], [], []
    n_feat = max(lyr.GetFeatureCount(), 1)
    for i, feat in enumerate(lyr):
        _check(stop)
        z = feat.GetField(field)
        g = feat.GetGeometryRef()
        if z is None or g is None:
            continue
        try:
            z = float(z)
        except (TypeError, ValueError):
            continue
        for pts in _line_points(g):
            seg = np.hypot(*np.diff(pts, axis=0).T)
            dist = np.r_[0, np.cumsum(seg)]
            if dist[-1] <= 0:
                continue
            t = np.arange(0, dist[-1] + step * 0.5, step)
            xs.append(np.interp(t, dist, pts[:, 0]))
            ys.append(np.interp(t, dist, pts[:, 1]))
            zs.append(np.full(t.size, z))
        if i % 50 == 0:
            progress(int(i / n_feat * 30))
    del ds
    if not xs:
        raise ValueError("No contour points found - check the elevation "
                         "field and that the layer holds lines.")
    px, py, pz = np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)
    log(f"  Sampled {px.size:,} points, Z {pz.min():.2f} – {pz.max():.2f} m")
    if np.unique(pz).size < 2:
        raise ValueError("All contours have the same elevation - nothing to "
                         "interpolate. Check the elevation field.")

    xmin, xmax, ymin, ymax = px.min(), px.max(), py.min(), py.max()
    if boundary:
        xmin, xmax, ymin, ymax = _extent_in(boundary, srs)
    # snap the origin to the cell size; pad one cell each side
    xmin = math.floor(xmin / res) * res - res
    ymin = math.floor(ymin / res) * res - res
    xmax = math.ceil(xmax / res) * res + res
    ymax = math.ceil(ymax / res) * res + res
    cols = int(round((xmax - xmin) / res))
    rows = int(round((ymax - ymin) / res))
    log(f"  Grid {cols} × {rows} px at {res:g} m")
    if cols * rows > 60_000_000:
        raise ValueError("Grid too large - increase the cell size.")
    gt = (xmin, res, 0.0, ymax, 0.0, -res)
    gx, gy = np.meshgrid(xmin + (np.arange(cols) + 0.5) * res,
                         ymax - (np.arange(rows) + 0.5) * res)
    q = np.column_stack([gx.ravel(), gy.ravel()])
    kp = np.column_stack([px, py])
    progress(40)

    log(f"  Interpolating ({method}) …")
    if method == "idw":
        tree = cKDTree(kp)
        out = np.empty(len(q))
        chunk = 500_000
        k = min(12, len(kp))
        for s0 in range(0, len(q), chunk):
            _check(stop)
            d, idx = tree.query(q[s0:s0 + chunk], k=k)
            if k == 1:
                d, idx = d[:, None], idx[:, None]
            d = np.maximum(d, 1e-9)
            w = 1.0 / d ** 2
            out[s0:s0 + chunk] = (w * pz[idx]).sum(1) / w.sum(1)
            progress(40 + int(s0 / len(q) * 45))
        z = out.reshape(rows, cols)
    else:
        _check(stop)
        z = griddata(kp, pz, q, method=method).reshape(rows, cols)
        gaps = np.isnan(z)
        if gaps.any():
            # Outside the outermost contour there is nothing to interpolate
            # from; carry the nearest contour's height so the grid is whole.
            z[gaps] = griddata(kp, pz, q[gaps.ravel()], method="nearest")
    progress(88)

    if boundary:
        mds = gdal.GetDriverByName("MEM").Create("", cols, rows, 1,
                                                 gdal.GDT_Byte)
        mds.SetGeoTransform(gt)
        mds.SetProjection(prj)
        z[~rasterize_mask(boundary, mds, log=log)] = np.nan
        mds = None
    write_geotiff(out_path, z, gt, prj)
    log(f"  DEM saved: {out_path}")
    progress(100)
    return out_path

# -*- coding: utf-8 -*-
"""Tile count, image size, download/disk estimates and warnings. Pure Python."""

import math
from dataclasses import dataclass, field

from . import tile_math as tm

#: Above this many tiles the details panel warns.
WARN_TILES = 100000
#: Above this many tiles Download is disabled. Configurable by the caller.
MAX_TILES = 5000000

#: Approximate output bytes per pixel, by format key.
BYTES_PER_PIXEL = {
    'tif': 0.45,          # JPEG-in-TIFF
    'tif_alpha': 2.6,     # DEFLATE RGBA
    'jpg': 0.4,
    'png': 2.2,
    'jp2': 0.5,
    'ecw': 0.15,
}
#: The intermediate GDAL work file (DEFLATE RGBA), bytes per mosaic pixel.
WORK_BYTES_PER_PIXEL = 3.0

#: Per-format limit on either image side, in pixels.
DIMENSION_LIMITS = {'jpg': 65500}


@dataclass
class Estimate:
    valid: bool = False
    bbox: tuple = None
    zoom: int = 0
    tile_range: tuple = None
    cols: int = 0
    rows: int = 0
    tiles: int = 0
    window: tuple = None
    native_w: int = 0
    native_h: int = 0
    out_w: int = 0
    out_h: int = 0
    res_m: float = 0.0
    area_km2: float = 0.0
    download_bytes: int = 0
    output_bytes: int = 0
    work_bytes: int = 0
    needs_bigtiff: bool = False
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)


def spherical_area_km2(bbox):
    """Area of a lon/lat rectangle on a sphere. Stands in for a geodesic one."""
    w, s, e, n = bbox
    r = 6371.0088
    return abs(math.radians(e - w) * (math.sin(math.radians(n)) -
                                      math.sin(math.radians(s)))) * r * r


def validate_bbox(bbox):
    """Return (bbox, error). The bbox comes back clamped to the Mercator square."""
    if bbox is None:
        return None, 'No extent chosen.'
    try:
        w, s, e, n = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return None, 'The extent is not valid.'
    if any(math.isnan(v) or math.isinf(v) for v in (w, s, e, n)):
        return None, 'The extent is not valid.'
    if w > e or s > n:
        return None, 'The extent is inverted (west > east or south > north).'
    if w < -180.0 or e > 180.0:
        if e - w >= 360.0:
            w, e = -180.0, 180.0
        else:
            return None, ('The extent crosses the antimeridian or lies outside '
                          'the world, which is not supported.')
    s, n = tm.clamp_lat(s), tm.clamp_lat(n)
    if (n - s) <= 1e-9 or (e - w) <= 1e-9:
        return None, 'The extent has no area.'
    return (w, s, e, n), None


def output_grid(bbox, tile_range, zoom, tile_size, transform_bounds):
    """Estimate the final pixel size in the output CRS.

    `transform_bounds(bbox4326)` returns (minx, miny, maxx, maxy) in the output
    CRS, or is None when the output is EPSG:3857 (no reprojection).
    """
    native_w, native_h = tm.crop_window(bbox, tile_range, zoom, tile_size)[2:]
    if transform_bounds is None:
        return native_w, native_h
    bounds = transform_bounds(bbox)
    if not bounds:
        return native_w, native_h
    out_w_units = bounds[2] - bounds[0]
    out_h_units = bounds[3] - bounds[1]
    diag_px = math.hypot(native_w, native_h)
    res = math.hypot(out_w_units, out_h_units) / diag_px if diag_px else 0
    if res <= 0:
        return native_w, native_h
    return (max(1, int(round(out_w_units / res))),
            max(1, int(round(out_h_units / res))))


def estimate(bbox, zoom, source, fmt='tif', transform_bounds=None,
             output_is_geographic=False, has_mask=False, avg_tile_bytes=None,
             max_tiles=MAX_TILES, free_bytes=None, area_km2=None,
             sample_blank=False, will_have_alpha=None, format_available=True):
    est = Estimate(zoom=zoom)
    bbox, error = validate_bbox(bbox)
    if error:
        est.blockers.append(error)
        return est
    est.bbox = bbox
    est.tile_range = tm.tile_range_for_bbox(bbox, zoom)
    xmin, xmax, ymin, ymax = est.tile_range
    est.cols, est.rows = xmax - xmin + 1, ymax - ymin + 1
    est.tiles = est.cols * est.rows
    ts = source.tile_size
    est.window = tm.crop_window(bbox, est.tile_range, zoom, ts)
    est.native_w, est.native_h = est.window[2], est.window[3]
    est.out_w, est.out_h = output_grid(bbox, est.tile_range, zoom, ts,
                                       transform_bounds)
    est.res_m = tm.ground_resolution(zoom, (bbox[1] + bbox[3]) / 2.0, ts)
    est.area_km2 = area_km2 if area_km2 is not None else spherical_area_km2(bbox)

    per_tile = avg_tile_bytes or source.avg_tile_bytes
    est.download_bytes = int(est.tiles * per_tile)

    reprojecting = transform_bounds is not None
    if will_have_alpha is None:
        will_have_alpha = has_mask or reprojecting
    key = fmt
    if fmt == 'tif' and will_have_alpha:
        key = 'tif_alpha'
    px = est.out_w * est.out_h
    est.output_bytes = int(px * BYTES_PER_PIXEL.get(key, 1.0))
    mw, mh = tm.mosaic_size(est.tile_range, ts)
    est.work_bytes = int(mw * mh * WORK_BYTES_PER_PIXEL)
    est.needs_bigtiff = est.output_bytes > 3.5 * 1024 ** 3 and fmt == 'tif'

    if est.tiles > max_tiles:
        est.blockers.append('{:,} tiles is above the limit of {:,}. Lower the '
                            'zoom level or choose a smaller area.'.format(
                                est.tiles, max_tiles))
    elif est.tiles > WARN_TILES:
        est.warnings.append('{:,} tiles is a very large download and may take '
                            'hours.'.format(est.tiles))
    limit = DIMENSION_LIMITS.get(fmt)
    if limit and max(est.out_w, est.out_h) > limit:
        est.blockers.append('The image would be {:,} x {:,} px; this format is '
                            'limited to {:,} px per side.'.format(
                                est.out_w, est.out_h, limit))
    if not format_available:
        est.blockers.append('This GDAL build cannot write the selected format.')
    if est.needs_bigtiff:
        est.warnings.append('The output is over 4 GB; BigTIFF will be used, '
                            'which some older software cannot open.')
    if reprojecting:
        est.warnings.append('The imagery will be reprojected and resampled to '
                            'the output CRS.')
    if output_is_geographic:
        est.warnings.append('The output CRS is geographic: the pixel size is in '
                            'degrees.')
    if sample_blank:
        est.warnings.append('Sample tiles look blank at this zoom level. The '
                            'source may have no imagery here; try a lower zoom.')
    if free_bytes is not None:
        need = est.work_bytes + est.output_bytes
        if free_bytes < need:
            est.warnings.append('Free disk space ({}) may be less than the '
                                '~{} needed for the work file and the '
                                'result.'.format(human_size(free_bytes),
                                                 human_size(need)))
    est.valid = not est.blockers
    return est


def human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return '{:.0f} {}'.format(n, unit) if unit == 'B' else \
                '{:.1f} {}'.format(n, unit)
        n /= 1024.0

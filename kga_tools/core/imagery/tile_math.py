# -*- coding: utf-8 -*-
"""XYZ / Web-Mercator tile arithmetic. Pure Python, no QGIS or GDAL imports.

Tile rows count down from the top (y = 0 is the northern edge), the scheme
Google, Esri and OpenStreetMap all use.
"""

import math

TILE_SIZE = 256
R_EARTH = 6378137.0
#: Half the width of the Web-Mercator world, in metres.
ORIGIN_SHIFT = math.pi * R_EARTH
#: Latitude at which the Mercator square is cut off.
MAX_LAT = 85.0511287798066

_EPS = 1e-9


def clamp_lat(lat):
    return max(-MAX_LAT, min(MAX_LAT, lat))


def lonlat_to_mercator(lon, lat):
    lat = clamp_lat(lat)
    x = lon * ORIGIN_SHIFT / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * R_EARTH
    return x, y


def mercator_to_lonlat(x, y):
    lon = x / ORIGIN_SHIFT * 180.0
    lat = math.degrees(2.0 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2.0)
    return lon, lat


def resolution(zoom, tile_size=TILE_SIZE):
    """Metres per pixel at the equator."""
    return 2.0 * ORIGIN_SHIFT / (tile_size * (2 ** zoom))


def ground_resolution(zoom, lat, tile_size=TILE_SIZE):
    """Metres per pixel on the ground at latitude `lat`."""
    return resolution(zoom, tile_size) * math.cos(math.radians(lat))


def _fractional_tile(lon, lat, zoom):
    n = 2 ** zoom
    mx, my = lonlat_to_mercator(lon, lat)
    fx = (mx + ORIGIN_SHIFT) / (2.0 * ORIGIN_SHIFT) * n
    fy = (ORIGIN_SHIFT - my) / (2.0 * ORIGIN_SHIFT) * n
    return fx, fy


def lonlat_to_tile(lon, lat, zoom):
    n = 2 ** zoom
    fx, fy = _fractional_tile(lon, lat, zoom)
    return (min(n - 1, max(0, int(math.floor(fx)))),
            min(n - 1, max(0, int(math.floor(fy)))))


def tile_bounds_3857(x, y, zoom, tile_size=TILE_SIZE):
    """(minx, miny, maxx, maxy) of one tile in EPSG:3857."""
    span = 2.0 * ORIGIN_SHIFT / (2 ** zoom)
    minx = -ORIGIN_SHIFT + x * span
    maxy = ORIGIN_SHIFT - y * span
    return minx, maxy - span, minx + span, maxy


def tile_range_for_bbox(bbox, zoom):
    """(x_min, x_max, y_min, y_max), inclusive, covering `bbox` (w, s, e, n).

    An edge lying exactly on a tile boundary does not pull in the neighbour.
    """
    w, s, e, n = bbox
    n_tiles = 2 ** zoom
    fx0, fy0 = _fractional_tile(w, n, zoom)
    fx1, fy1 = _fractional_tile(e, s, zoom)

    def lo(v):
        return min(n_tiles - 1, max(0, int(math.floor(v + _EPS))))

    def hi(v):
        return min(n_tiles - 1, max(0, int(math.ceil(v - _EPS)) - 1))

    x_min, x_max = lo(fx0), hi(fx1)
    y_min, y_max = lo(fy0), hi(fy1)
    return x_min, max(x_min, x_max), y_min, max(y_min, y_max)


def mosaic_size(tile_range, tile_size=TILE_SIZE):
    x_min, x_max, y_min, y_max = tile_range
    return (x_max - x_min + 1) * tile_size, (y_max - y_min + 1) * tile_size


def mosaic_geotransform(tile_range, zoom, tile_size=TILE_SIZE):
    """GDAL geotransform of the whole-tile mosaic, in EPSG:3857."""
    x_min, _x_max, y_min, _y_max = tile_range
    res = resolution(zoom, tile_size)
    return (-ORIGIN_SHIFT + x_min * tile_size * res, res, 0.0,
            ORIGIN_SHIFT - y_min * tile_size * res, 0.0, -res)


def crop_window(bbox, tile_range, zoom, tile_size=TILE_SIZE):
    """Pixel window (xoff, yoff, xsize, ysize) of `bbox` inside the mosaic.

    Rounded outward so the crop never loses part of the requested extent.
    """
    w, s, e, n = bbox
    gt = mosaic_geotransform(tile_range, zoom, tile_size)
    width, height = mosaic_size(tile_range, tile_size)
    res = gt[1]
    x0, y0 = lonlat_to_mercator(w, n)
    x1, y1 = lonlat_to_mercator(e, s)
    px0 = int(math.floor((x0 - gt[0]) / res + _EPS))
    px1 = int(math.ceil((x1 - gt[0]) / res - _EPS))
    py0 = int(math.floor((gt[3] - y0) / res + _EPS))
    py1 = int(math.ceil((gt[3] - y1) / res - _EPS))
    px0, py0 = max(0, px0), max(0, py0)
    px1, py1 = min(width, px1), min(height, py1)
    return px0, py0, max(1, px1 - px0), max(1, py1 - py0)


def iter_tiles(tile_range):
    """Row-major (x, y) over the range, the order they are downloaded in."""
    x_min, x_max, y_min, y_max = tile_range
    for y in range(y_min, y_max + 1):
        for x in range(x_min, x_max + 1):
            yield x, y


def tile_count(tile_range):
    x_min, x_max, y_min, y_max = tile_range
    return (x_max - x_min + 1) * (y_max - y_min + 1)

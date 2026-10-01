# -*- coding: utf-8 -*-
"""Fetch a handful of tiles to refine the download estimate and spot blank areas."""

from . import tile_math as tm
from .downloader import qgis_fetch
from .job import decode_tile

SAMPLE_COUNT = 8


def pick(tile_range, count=SAMPLE_COUNT):
    tiles = list(tm.iter_tiles(tile_range))
    if len(tiles) <= count:
        return tiles
    step = (len(tiles) - 1) / float(count - 1)
    return [tiles[int(round(i * step))] for i in range(count)]


def sample_tiles(source, tile_range, zoom, should_stop=lambda: False,
                 fetch=qgis_fetch, decode=decode_tile):
    """Return {'sizes': [...], 'blank': n, 'failed': n} for a spread of tiles."""
    sizes, blank, failed = [], 0, 0
    for x, y in pick(tile_range):
        if should_stop():
            break
        status, body, _err = fetch(source.tile_url(x, y, zoom))
        if status != 200 or not body:
            failed += 1
            continue
        sizes.append(len(body))
        try:
            arr = decode(body, source.tile_size)
        except ValueError:
            failed += 1
            continue
        rgb = arr[:3]
        if int(rgb.min()) == int(rgb.max()):
            blank += 1
    return {'sizes': sizes, 'blank': blank, 'failed': failed}

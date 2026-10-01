# -*- coding: utf-8 -*-
"""The sparse GDAL work file the tiles are stitched into.

One 256 px internal block per tile, so a tile write touches exactly one block
and a tile that never arrives stays as a hole (zero, therefore transparent).
GDAL datasets are not thread-safe: only one thread may hold a `Mosaic`.
"""

import os

import numpy as np
from osgeo import gdal, osr

from . import tile_math as tm

gdal.UseExceptions()


class Mosaic:
    def __init__(self, path, ds, tile_size):
        self.path = path
        self._ds = ds
        self.tile_size = tile_size
        self._bands = [ds.GetRasterBand(i + 1) for i in range(4)]

    @classmethod
    def create(cls, path, tile_range, zoom, tile_size=tm.TILE_SIZE):
        width, height = tm.mosaic_size(tile_range, tile_size)
        driver = gdal.GetDriverByName('GTiff')
        opts = ['TILED=YES', 'BLOCKXSIZE=%d' % tile_size,
                'BLOCKYSIZE=%d' % tile_size, 'COMPRESS=DEFLATE', 'ZLEVEL=1',
                'BIGTIFF=YES', 'SPARSE_OK=TRUE']
        if os.path.exists(path):
            driver.Delete(path)
        ds = driver.Create(path, width, height, 4, gdal.GDT_Byte, opts)
        ds.SetGeoTransform(tm.mosaic_geotransform(tile_range, zoom, tile_size))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3857)
        ds.SetProjection(srs.ExportToWkt())
        for i, interp in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand,
                                    gdal.GCI_BlueBand, gdal.GCI_AlphaBand)):
            ds.GetRasterBand(i + 1).SetColorInterpretation(interp)
        ds.FlushCache()
        return cls(path, ds, tile_size)

    @classmethod
    def open(cls, path, tile_range, tile_size=tm.TILE_SIZE):
        """Reopen an existing work file for update; raises if it does not fit."""
        ds = gdal.Open(path, gdal.GA_Update)
        width, height = tm.mosaic_size(tile_range, tile_size)
        if ds is None or ds.RasterXSize != width or ds.RasterYSize != height \
                or ds.RasterCount != 4:
            ds = None
            raise ValueError('The work file does not match this download.')
        return cls(path, ds, tile_size)

    def write_tile(self, col, row, array):
        """`array` is (4, tile, tile) uint8; col/row are tile offsets in the mosaic."""
        xoff, yoff = col * self.tile_size, row * self.tile_size
        for i in range(4):
            self._bands[i].WriteArray(np.ascontiguousarray(array[i]), xoff, yoff)

    def commit(self):
        """Flush to disk and reopen, so the file is complete and self-consistent."""
        self._bands = None
        self._ds.FlushCache()
        self._ds = None
        self._ds = gdal.Open(self.path, gdal.GA_Update)
        self._bands = [self._ds.GetRasterBand(i + 1) for i in range(4)]

    def close(self):
        self._bands = None
        if self._ds is not None:
            self._ds.FlushCache()
        self._ds = None

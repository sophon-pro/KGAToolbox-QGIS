# -*- coding: utf-8 -*-
"""Any raster layer as RGB blocks on one metric grid.

The model wants a fixed ground resolution (0.3 m), whatever the source: a tile
service at zoom 17 or 20, a satellite GeoTIFF, or a drone mosaic at 3 cm. Every
source is therefore *rendered* through QGIS into blocks of a grid in the area's
UTM zone at that resolution. Rendering, rather than reading bands, means:

* XYZ, WMS, WMTS and ArcGIS tile layers work exactly as they show on the map,
  with QGIS choosing the zoom level and reprojecting the tiles;
* a GeoTIFF's own renderer (band choice, contrast stretch) turns 16-bit or
  multispectral data into the RGB the user sees;
* nodata comes back transparent, so it is known per pixel.

A layer is cloned on the GUI thread and the clone rendered on the task's
thread, as QGIS's own "Convert map to raster" does. A local raster gets average
resampling when zoomed out, so a 3 cm drone mosaic is averaged down to 0.3 m
instead of picked at every tenth pixel.
"""

import math

import numpy as np
from qgis.core import (Qgis, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsMapRendererCustomPainterJob,
                       QgsMapSettings, QgsProject, QgsRasterLayer, QgsRectangle)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter

#: Model pixels per block side, before the context margin. A multiple of every
#: downsampling factor used.
BLOCK = 1024
#: Context added around every block, in model pixels, so chips at the block's
#: edge see past it and the stitched probabilities have no seams.
PAD = 64


class SamplerError(Exception):
    """The layer cannot be sampled. Shown to the user as-is."""


class Grid(object):
    """A north-up grid in a metric CRS: origin top-left, square pixels."""

    def __init__(self, crs, west, north, cols, rows, gsd):
        self.crs = crs
        self.west = west
        self.north = north
        self.cols = int(cols)
        self.rows = int(rows)
        self.gsd = float(gsd)

    @classmethod
    def around(cls, geometry, geometry_crs, crs, gsd, margin_m=0.0):
        """The grid covering `geometry`, snapped to whole pixels."""
        transform = QgsCoordinateTransform(geometry_crs, crs,
                                           QgsProject.instance())
        box = transform.transformBoundingBox(geometry.boundingBox())
        west = math.floor((box.xMinimum() - margin_m) / gsd) * gsd
        south = math.floor((box.yMinimum() - margin_m) / gsd) * gsd
        east = math.ceil((box.xMaximum() + margin_m) / gsd) * gsd
        north = math.ceil((box.yMaximum() + margin_m) / gsd) * gsd
        cols = max(1, int(round((east - west) / gsd)))
        rows = max(1, int(round((north - south) / gsd)))
        return cls(crs, west, north, cols, rows, gsd)

    @property
    def pixels(self):
        return self.cols * self.rows

    def geotransform(self):
        return (self.west, self.gsd, 0.0, self.north, 0.0, -self.gsd)

    def coarser(self, factor):
        """The same area at `factor` times the pixel size (whole blocks)."""
        return Grid(self.crs, self.west, self.north,
                    int(math.ceil(self.cols / float(factor))),
                    int(math.ceil(self.rows / float(factor))),
                    self.gsd * factor)

    def blocks(self, size=BLOCK):
        """(row, col, rows, cols) of each block, row by row."""
        for row in range(0, self.rows, size):
            for col in range(0, self.cols, size):
                yield row, col, min(size, self.rows - row), \
                    min(size, self.cols - col)

    def block_count(self, size=BLOCK):
        return int(math.ceil(self.rows / float(size))) * \
            int(math.ceil(self.cols / float(size)))


def utm_for(geometry, crs):
    """The UTM zone at the middle of `geometry`."""
    wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
    centre = geometry.boundingBox().center()
    with_transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
    point = with_transform.transform(centre)
    zone = int(min(60, max(1, math.floor((point.x() + 180.0) / 6.0) + 1)))
    return QgsCoordinateReferenceSystem(
        'EPSG:{0}'.format((32600 if point.y() >= 0 else 32700) + zone))


def prepare_layer(layer):
    """A copy of `layer` for the task to render. Call on the GUI thread."""
    if layer is None or not layer.isValid():
        raise SamplerError('Choose an imagery layer.')
    if not isinstance(layer, QgsRasterLayer):
        raise SamplerError('"{0}" is not a raster layer.'.format(layer.name()))
    clone = layer.clone()
    if clone is None or not clone.isValid():
        raise SamplerError('"{0}" could not be prepared for reading.'.format(
            layer.name()))
    if clone.providerType() == 'gdal':
        _average_when_zoomed_out(clone)
    return clone


def _average_when_zoomed_out(layer):
    provider = layer.dataProvider()
    with_method = getattr(provider, 'setZoomedOutResamplingMethod', None)
    if with_method is not None:
        try:
            method = provider.ResamplingMethod.Average
        except AttributeError:                  # an older QGIS
            method = None
        if method is not None:
            with_method(method)
            provider.setMaxOversampling(4.0)
            return
    from qgis.core import QgsBilinearRasterResampler
    resample = layer.resampleFilter()
    if resample is not None:
        resample.setZoomedOutResampler(QgsBilinearRasterResampler())


def is_local(layer):
    return layer is not None and layer.providerType() == 'gdal'


def looks_like_overlay(layer):
    """True for a layer whose name says it carries labels or roads drawn on."""
    name = (layer.name() or '').lower()
    return any(word in name for word in ('hybrid', 'label', 'traffic',
                                         'reference', 'road', 'street', 'osm',
                                         'openstreetmap', 'terrain', 'map'))


def native_resolution_m(layer, grid):
    """Metres per pixel of a local raster, or None for a tile service."""
    if not is_local(layer):
        return None
    extent = layer.extent()
    if layer.width() <= 0 or extent.isEmpty():
        return None
    size = extent.width() / float(layer.width())
    crs = layer.crs()
    if crs.isGeographic():
        return size * 111320.0 * math.cos(math.radians(extent.center().y()))
    from qgis.core import QgsUnitTypes
    return size * QgsUnitTypes.fromUnitToUnitFactor(
        crs.mapUnits(), Qgis.DistanceUnit.Meters)


def render(layer, grid, row, col, rows, cols, pad=PAD, scale=1):
    """One block, `pad` pixels wider on every side, as ``(rgb, valid)``.

    `rgb` is uint8 (rows + 2 pad, cols + 2 pad, 3) and `valid` a bool array of
    the same height and width, False where the layer drew nothing.

    `scale` above 1 draws the block that many times coarser and enlarges it
    smoothly: a tile service then asks for a lower zoom level, for where it has
    no imagery at the full one.
    """
    height = rows + 2 * pad
    width = cols + 2 * pad
    west = grid.west + (col - pad) * grid.gsd
    north = grid.north - (row - pad) * grid.gsd
    extent = QgsRectangle(west, north - height * grid.gsd,
                          west + width * grid.gsd, north)
    out_w = max(1, int(round(width / float(scale))))
    out_h = max(1, int(round(height / float(scale))))

    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(grid.crs)
    settings.setTransformContext(QgsProject.instance().transformContext())
    settings.setOutputSize(QSize(out_w, out_h))
    settings.setOutputDpi(96)
    settings.setExtent(extent)
    settings.setBackgroundColor(QColor(0, 0, 0, 0))
    settings.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
    settings.setFlag(Qgis.MapSettingsFlag.RenderBlocking, True)

    image = QImage(out_w, out_h, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.renderSynchronously()
    painter.end()
    if (out_w, out_h) != (width, height):
        image = image.scaled(width, height,
                             Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image_to_arrays(image)


def looks_like_placeholder(rgb, valid):
    """True for a block that is mostly one exact colour.

    Tile services answer a zoom level they have no imagery for with a stock
    tile - Esri's is flat grey with "Map data not yet available" written on it.
    Real imagery, even open water, never has most of its pixels at one exact
    RGB value.
    """
    if valid.mean() < 0.5:
        return False
    pixels = rgb[valid]
    if len(pixels) > 200000:
        pixels = pixels[::len(pixels) // 200000]
    packed = (pixels[:, 0].astype(np.int32) << 16) | \
        (pixels[:, 1].astype(np.int32) << 8) | pixels[:, 2].astype(np.int32)
    counts = np.bincount(np.unique(packed, return_inverse=True)[1])
    return counts.max() > 0.6 * len(packed)


def image_to_arrays(image):
    """ARGB32 QImage -> (uint8 RGB array, bool valid array)."""
    if image.format() != QImage.Format.Format_ARGB32:
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    line = image.bytesPerLine()
    bits = image.constBits()
    if hasattr(bits, 'setsize'):
        bits.setsize(line * height)
    raw = np.frombuffer(bits, dtype=np.uint8).reshape(height, line)
    pixels = raw[:, :width * 4].reshape(height, width, 4)
    # ARGB32 is stored B, G, R, A on every machine QGIS runs on.
    rgb = np.ascontiguousarray(pixels[:, :, 2::-1])
    valid = pixels[:, :, 3] > 0
    return rgb, valid


def downsample(array, factor):
    """Mean of `factor` x `factor` cells; the edge is padded by repetition."""
    if factor == 1:
        return array
    rows, cols = array.shape[:2]
    pad_r = (-rows) % factor
    pad_c = (-cols) % factor
    if pad_r or pad_c:
        widths = [(0, pad_r), (0, pad_c)] + [(0, 0)] * (array.ndim - 2)
        array = np.pad(array, widths, mode='edge')
        rows, cols = array.shape[:2]
    shape = (rows // factor, factor, cols // factor, factor) + array.shape[2:]
    return array.reshape(shape).mean(axis=(1, 3))

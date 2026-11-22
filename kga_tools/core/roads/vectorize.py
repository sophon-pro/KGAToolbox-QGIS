# -*- coding: utf-8 -*-
"""Probability -> clean mask -> surface polygons, edge lines and centrelines.

All arrays here are on the post-processing grid (twice the model's pixel size
by default: 0.6 m). Geometries come out as QgsGeometry in that grid's CRS.
"""

import numpy as np
from qgis.core import QgsGeometry, QgsPointXY


def memory_vector():
    """An in-memory OGR data source. GDAL 3.11 folded the old 'Memory'
    driver into 'MEM' and warns about the old name; older GDAL only has it."""
    from osgeo import gdal, ogr

    driver = gdal.GetDriverByName('MEM')
    if driver is not None and driver.GetMetadataItem('DCAP_VECTOR') == 'YES':
        return driver.Create('', 0, 0, 0, gdal.GDT_Unknown)
    return ogr.GetDriverByName('Memory').CreateDataSource('')


def roi_mask(grid, geometry):
    """Pixels whose centre lies inside `geometry` (in the grid's CRS)."""
    from osgeo import gdal, ogr, osr

    driver = gdal.GetDriverByName('MEM')
    target = driver.Create('', grid.cols, grid.rows, 1, gdal.GDT_Byte)
    target.SetGeoTransform(grid.geotransform())
    srs = osr.SpatialReference()
    srs.ImportFromWkt(grid.crs.toWkt())
    target.SetProjection(srs.ExportToWkt())
    source = memory_vector()
    layer = source.CreateLayer('roi', srs, ogr.wkbMultiPolygon)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geometry.asWkb())))
    layer.CreateFeature(feature)
    gdal.RasterizeLayer(target, [1], layer, burn_values=[1])
    return target.GetRasterBand(1).ReadAsArray().astype(bool)


def clean_mask(prob, threshold, inside, min_area_px, hole_px):
    """Threshold, then drop specks and fill pin-holes."""
    from scipy import ndimage

    mask = (prob >= threshold) & inside
    # One opening knocks off single-pixel fringes before the area test.
    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)))
    labels, count = ndimage.label(mask, structure=np.ones((3, 3)))
    if count:
        sizes = np.bincount(labels.ravel())
        small = sizes < min_area_px
        small[0] = False
        mask[small[labels]] = False
    holes, count = ndimage.label(~mask)
    if count:
        sizes = np.bincount(holes.ravel())
        fill = sizes < hole_px
        fill[0] = False
        mask |= fill[holes] & inside
    return mask


def mask_to_thin(mask, sigma_px):
    """The mask with its outline softened, to thin into fewer whiskers.

    Every bump on a mask's edge grows a spur when it is thinned. Blurring the
    mask and cutting it again at one half rounds the bumps off without moving
    the middle of the road.
    """
    from scipy import ndimage

    if sigma_px <= 0:
        return mask
    soft = ndimage.gaussian_filter(mask.astype(np.float32), sigma_px)
    return soft >= 0.5


def polygonize(mask, grid):
    """Mask -> list of QgsGeometry polygons (one per connected piece)."""
    from osgeo import gdal, ogr, osr

    driver = gdal.GetDriverByName('MEM')
    source = driver.Create('', grid.cols, grid.rows, 1, gdal.GDT_Byte)
    source.SetGeoTransform(grid.geotransform())
    srs = osr.SpatialReference()
    srs.ImportFromWkt(grid.crs.toWkt())
    source.SetProjection(srs.ExportToWkt())
    band = source.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    target = memory_vector()
    layer = target.CreateLayer('surface', srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('v', ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0, ['8CONNECTED=8'])
    polygons = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None:
            continue
        polygons.append(QgsGeometry.fromWkt(geometry.ExportToWkt()))
    return polygons


def generalize(geometry, tolerance, smooth):
    """Smooth, then simplify. Shared by lines and polygons.

    The order matters. Smoothing first rounds off the pixel stairs while the
    vertices are still dense, so it moves nothing by more than a pixel.
    Simplifying (Douglas-Peucker) then keeps only the vertices that matter
    within `tolerance` metres, so straight streets come out as straight lines
    and junction corners stay sharp. Smoothing *after* a strong simplify would
    cut every corner by a quarter of the segment either side - tens of metres
    on a long street.
    """
    if smooth:
        smoothed = geometry.smooth(2, 0.25)
        if smoothed is not None and not smoothed.isEmpty():
            geometry = smoothed
    if tolerance and tolerance > 0:
        simple = geometry.simplify(tolerance)
        if simple is not None and not simple.isEmpty():
            geometry = simple
    return geometry


def tidy_polygon(geometry, tolerance, smooth=True):
    """A surface polygon, generalized and valid."""
    geometry = generalize(geometry, tolerance, smooth)
    fixed = geometry.makeValid()
    return fixed if fixed is not None and not fixed.isEmpty() else geometry


def pixel_to_map(grid, pixels):
    """(n, 2) array of (row, col) pixel centres -> list of QgsPointXY."""
    xs = grid.west + (pixels[:, 1] + 0.5) * grid.gsd
    ys = grid.north - (pixels[:, 0] + 0.5) * grid.gsd
    return [QgsPointXY(x, y) for x, y in zip(xs.tolist(), ys.tolist())]


def centreline(grid, pixels, tolerance, smooth=True):
    """A traced pixel path as a line in map units, generalized."""
    geometry = QgsGeometry.fromPolylineXY(pixel_to_map(grid, pixels))
    if len(pixels) > 2:
        geometry = generalize(geometry, tolerance, smooth)
    return geometry


def sample(array, pixels):
    """Values of `array` at the (rounded) pixel positions."""
    rows = np.clip(np.round(pixels[:, 0]).astype(int), 0, array.shape[0] - 1)
    cols = np.clip(np.round(pixels[:, 1]).astype(int), 0, array.shape[1] - 1)
    return array[rows, cols]


def surface_kind(rgb_values):
    """'paved', 'unpaved' or 'unknown' from the colour along a road.

    Asphalt and concrete are grey: little difference between the channels.
    Laterite and earth roads in the region are red-brown to tan: red well above
    blue. A guess from colour only, and said to be one.
    """
    if not len(rgb_values):
        return 'unknown'
    rgb = np.median(np.asarray(rgb_values, dtype=np.float32), axis=0)
    red, green, blue = rgb
    spread = float(max(rgb) - min(rgb))
    if spread < 22:
        return 'paved'
    if red - blue > 28 and red >= green:
        return 'unpaved'
    return 'unknown'


def edge_lines(polygon, cut):
    """The outline of a surface polygon, less where it runs along `cut`
    (the area's boundary, where the edge is the tool's, not the road's)."""
    rings = []
    parts = polygon.asMultiPolygon() if polygon.isMultipart() \
        else [polygon.asPolygon()]
    for part in parts:
        for ring in part:
            if len(ring) >= 3:
                rings.append(QgsGeometry.fromPolylineXY(ring))
    if not rings:
        return None
    lines = QgsGeometry.unaryUnion(rings) if len(rings) > 1 else rings[0]
    if cut is not None:
        trimmed = lines.difference(cut)
        if trimmed is None or trimmed.isEmpty():
            return None
        lines = trimmed
    merged = lines.mergeLines()
    return merged if merged is not None and not merged.isEmpty() else lines

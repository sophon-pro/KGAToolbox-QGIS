# -*- coding: utf-8 -*-
"""Open land cover for the Add Open Data tool.

Land cover is asked for the way a DEM is - by area, through the same four
region-of-interest modes in :mod:`.dem_sources` - plus one more choice: the
*year* the map represents. What comes back is a classified GeoTIFF that already
knows what its numbers mean:

* a **colour table** with the publisher's own palette, so it opens coloured in
  QGIS, ArcGIS or anything else that reads GeoTIFF;
* **category names**, so Identify reports "Trees" rather than 2;
* a **raster attribute table** with ``lc_code``, ``lc_class``, the pixel count
  and the colour, written by GDAL to the ``.aux.xml`` beside the file, which is
  where ArcGIS and QGIS 3.30+ both look for one;
* optionally a **polygon GeoPackage** with ``lc_code`` and ``lc_class`` fields,
  for anyone who wants the classes as features.

Two sources ship:

**Esri Land Cover** (Impact Observatory / Microsoft / Esri, Sentinel-2, 10 m,
annual since 2017) is read from Esri's Living Atlas image service - the same
data behind livingatlas.arcgis.com/landcover. The service hands out at most
4000 x 4000 pixels per request, so an area is cut into chunks, each asked for in
the UTM zone of the area's centre at exactly 10 m, which is how the data is
published. Chunks are cached, so asking again for an overlapping area is cheap.

**ESA WorldCover** (10 m, 2020 and 2021) is Cloud Optimised GeoTIFF in a public
AWS bucket, read through ``/vsicurl/`` like the Copernicus DEM: only the blocks
inside the area cross the network.

No Qt widgets here. The dialog imports this; this must never import the dialog.
"""

import hashlib
import json
import math
import os
import time
from urllib.parse import urlencode

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsProject, QgsRectangle)

from .data_sources import KIND_LANDCOVER, DataSource, Option, SourceItem
from .dem_sources import (WGS84, DemError, _gdal_exceptions, _human,
                          _opens_as_raster, warp_to_file)
from .net import (METADATA_TTL, NetError, cached_path, download_to, get_json,
                  is_fresh)

#: Written where there is no class: outside a clipped shape, outside the data.
#: Both products use 0 for "no data" themselves, so it is kept.
NODATA = 0

#: Pixels one request may produce. Land cover is one byte a pixel, so this is
#: about 500 MB before compression - some 50,000 km2 at 10 m, a large province.
MAX_PIXELS = 500 * 1000 * 1000

#: Polygonising is far slower than writing the raster, and the result grows
#: with every field boundary. Past this the polygon option is refused.
MAX_POLYGON_PIXELS = 60 * 1000 * 1000

#: The layer name inside the polygon GeoPackage.
POLYGON_LAYER = 'landcover'

#: A throwaway table of code -> name, joined in once and then dropped. The class
#: names are written with a constant statement over this table instead of SQL
#: assembled from text, so no value is ever spliced into a statement.
LOOKUP_LAYER = 'kga_lc_lookup'
FILL_CLASS_NAMES = (
    'UPDATE landcover SET lc_class = (SELECT lc_name FROM kga_lc_lookup '
    'WHERE kga_lc_lookup.lc_code = landcover.lc_code)')


class LcClass(object):
    """One land-cover class: its code in the raster, its name, its colour."""

    def __init__(self, code, name, color, description=''):
        self.code = code
        self.name = name
        #: '#rrggbb', the publisher's own legend colour.
        self.color = color
        self.description = description

    def rgb(self):
        value = self.color.lstrip('#')
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


#: Esri / Impact Observatory 10 m annual land use / land cover (v2), with the
#: palette Esri's own viewer uses.
ESRI_CLASSES = (
    LcClass(1, 'Water', '#419BDF',
            'Water present for most of the year; excludes seasonal water.'),
    LcClass(2, 'Trees', '#397D49',
            'Significant tall (about 15 m or higher) dense vegetation.'),
    LcClass(4, 'Flooded vegetation', '#7A87C6',
            'Vegetation with obvious water mixed in for much of the year: '
            'wetlands, flooded rice paddies, mangroves.'),
    LcClass(5, 'Crops', '#E49635',
            'Human planted cereals, grasses and crops not at tree height.'),
    LcClass(7, 'Built area', '#C4281B',
            'Human-made structures, roads and paved surfaces.'),
    LcClass(8, 'Bare ground', '#A59B8F',
            'Rock, soil, sand, exposed riverbeds and mines.'),
    LcClass(9, 'Snow/ice', '#A8EBFF',
            'Large homogeneous areas of permanent snow or ice.'),
    LcClass(10, 'Clouds', '#616161',
            'No land cover information because of persistent cloud cover.'),
    LcClass(11, 'Rangeland', '#E3E2C3',
            'Open areas covered in homogeneous grasses and scrub.'),
)

#: ESA WorldCover classes and the official legend colours.
WORLDCOVER_CLASSES = (
    LcClass(10, 'Tree cover', '#006400'),
    LcClass(20, 'Shrubland', '#FFBB22'),
    LcClass(30, 'Grassland', '#FFFF4C'),
    LcClass(40, 'Cropland', '#F096FF'),
    LcClass(50, 'Built-up', '#FA0000'),
    LcClass(60, 'Bare / sparse vegetation', '#B4B4B4'),
    LcClass(70, 'Snow and ice', '#F0F0F0'),
    LcClass(80, 'Permanent water bodies', '#0064C8'),
    LcClass(90, 'Herbaceous wetland', '#0096A0'),
    LcClass(95, 'Mangroves', '#00CF75'),
    LcClass(100, 'Moss and lichen', '#FAE6A0'),
)


# ------------------------------------------------------------------- sources

class LandcoverSource(DataSource):
    """Base for the land-cover sources: an ROI and a year in, rasters out."""

    kind = KIND_LANDCOVER
    needs_country = False
    needs_api_key = False
    #: Metres per pixel, for the size estimate.
    resolution_m = 10
    CLASSES = ()
    licence = ''
    note = ''

    def years(self):
        """The years published, oldest first."""
        raise NotImplementedError

    def options(self):
        years = self.years()
        return [Option('year', 'Year', [(y, str(y)) for y in reversed(years)],
                       default=years[-1],
                       tooltip='The year the land-cover map represents. Each '
                               'map is a summary of that whole year.')]

    def year(self, options):
        years = self.years()
        wanted = (options or {}).get('year')
        try:
            wanted = int(wanted)
        except (TypeError, ValueError):
            return years[-1]
        return wanted if wanted in years else years[-1]

    def levels(self, iso3, options=None):
        return []

    def cached_file(self, item):
        # What is cached is a set of source tiles, never the finished file.
        return None

    def describe(self, iso3, level, options=None):
        options = options or {}
        roi = options.get('roi')
        if roi is None:
            raise DemError(options.get('roi_error') or
                           'Choose the area to download.')
        year = self.year(options)
        pixels = self.pixel_estimate(roi)
        if pixels > MAX_PIXELS:
            raise DemError(
                'That area is too large for one land-cover request: about '
                '{0:,} million pixels at {1} m, where one request is limited '
                'to {2:,} million. Choose a smaller area.'.format(
                    pixels // 1000000, self.resolution_m,
                    MAX_PIXELS // 1000000))

        rows = [
            ('Area', '{0} - {1:,.0f} km2'.format(roi.label, roi.area_km2())),
            ('Bounds', roi.describe_bounds()),
            ('Resolution', '{0} m'.format(self.resolution_m)),
            ('Classes', ', '.join('{0} {1}'.format(c.code, c.name)
                                  for c in self.CLASSES)),
            ('Output', '{0:,} pixels, about {1} before compression'.format(
                pixels, _human(pixels))),
        ]
        rows.extend(self.extra_rows(roi, year))
        notes = [self.note] if self.note else []
        notes.append('Clipped to the shape; pixels outside it are NoData.'
                     if roi.clip else 'Covers the bounding box of the area.')
        notes.append('Each pixel carries its class code; the file has the '
                     'class names, colours and an attribute table with '
                     'lc_code and lc_class.')

        item = SourceItem(
            title=self.title(year),
            cache_key='{0}_{1}'.format(year, _box_hash(roi)),
            extension='.tif',
            licence=self.licence,
            attribution=self.attribution_for(year),
            publisher=self.publisher,
            year=str(year),
            source_url=self.homepage,
            notes=' '.join(notes),
        )
        item.extra_rows = rows
        item.roi = roi
        item.year = year
        item.classes = self.CLASSES
        item.pixels = pixels
        return item

    def title(self, year):
        return '{0} {1}'.format(self.short_label, year)

    def attribution_for(self, year):
        return self.attribution

    def pixel_estimate(self, roi):
        """Pixels in the ROI's bounding box at the source's resolution."""
        return int(roi.bbox_area_km2() * 1e6 / (self.resolution_m ** 2))

    def extra_rows(self, roi, year):
        return []

    def rasters(self, item, on_progress=None):
        """The rasters to mosaic, or None when the user cancelled.

        `on_progress(fraction, text)` may return False to cancel.
        """
        raise NotImplementedError


# ---------------------------------------------------------------- Esri / IO

class EsriLandcoverSource(LandcoverSource):
    """Esri's 10 m annual land cover, from the Living Atlas image service."""

    id = 'esri_landcover'
    label = 'Esri Land Cover 10 m (Sentinel-2, Impact Observatory)'
    short_label = 'Esri Land Cover'
    publisher = 'Impact Observatory, Microsoft and Esri'
    homepage = 'https://livingatlas.arcgis.com/landcover/'
    licence = 'CC BY 4.0'
    attribution = ('Esri Land Cover 10 m, Sentinel-2 annual land use / land '
                   'cover. Impact Observatory, Microsoft and Esri (Karra et '
                   'al. 2021). CC BY 4.0')
    note = ('One map per year, from a year of Sentinel-2 imagery. Rice paddies '
            'and mangroves usually read as Flooded vegetation.')
    CLASSES = ESRI_CLASSES

    SERVICE = ('https://ic.imagery1.arcgis.com/arcgis/rest/services/'
               'Sentinel2_10m_LandCover/ImageServer')
    #: The service's own limit on one exportImage reply.
    CHUNK = 4000
    #: Used when the service cannot be asked which years it has.
    FALLBACK_YEARS = tuple(range(2017, 2026))
    #: Seconds before asking again after the service failed to answer. Every
    #: describe reads the year list - once per map pan on a map-extent area -
    #: and an unreachable service costs the full request timeout each time,
    #: on the GUI thread.
    RETRY_AFTER = 600
    _offline_until = 0

    def years(self):
        path = cached_path(self.id, 'years', '.json')
        cached = _read_years(path)
        if cached and is_fresh(path, METADATA_TTL):
            return cached
        if time.time() < self._offline_until:
            return cached or list(self.FALLBACK_YEARS)
        url = '{0}/query?{1}'.format(self.SERVICE, urlencode({
            'where': '1=1', 'outFields': 'Year', 'returnGeometry': 'false',
            'returnDistinctValues': 'true', 'f': 'json'}))
        try:
            payload = get_json(url)
            years = sorted({int(f['attributes']['Year'])
                            for f in payload.get('features', [])
                            if (f.get('attributes') or {}).get('Year')})
        except (NetError, KeyError, TypeError, ValueError, AttributeError):
            years = []
        if years:
            _write_years(path, years)
            return years
        self._offline_until = time.time() + self.RETRY_AFTER
        # Offline: a stale list beats a guessed one.
        return cached or list(self.FALLBACK_YEARS)

    def _grid(self, roi):
        """(epsg, xmin, ymin, xmax, ymax) - the ROI in its UTM zone, on the
        10 m grid the data is published on."""
        west, south, east, north = roi.bounds()
        lon, lat = (west + east) / 2.0, (south + north) / 2.0
        if abs(lat) > 84:
            raise DemError('Esri Land Cover is published in UTM, which stops '
                           'at 84 degrees north and 80 south.')
        zone = min(60, max(1, int((lon + 180.0) // 6) + 1))
        epsg = (32600 if lat >= 0 else 32700) + zone
        crs = QgsCoordinateReferenceSystem('EPSG:{0}'.format(epsg))
        box = QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), crs,
                                     QgsProject.instance()).transformBoundingBox(
            QgsRectangle(west, south, east, north))
        res = self.resolution_m
        return (epsg,
                math.floor(box.xMinimum() / res) * res,
                math.floor(box.yMinimum() / res) * res,
                math.ceil(box.xMaximum() / res) * res,
                math.ceil(box.yMaximum() / res) * res)

    def _chunks(self, roi):
        """[(xmin, ymin, xmax, ymax, width, height)] in the UTM grid, only the
        ones the ROI actually touches."""
        epsg, xmin, ymin, xmax, ymax = self._grid(roi)
        step = self.CHUNK * self.resolution_m
        to_wgs84 = None
        if roi.clip:
            to_wgs84 = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem('EPSG:{0}'.format(epsg)),
                QgsCoordinateReferenceSystem(WGS84), QgsProject.instance())
        chunks = []
        y = ymin
        while y < ymax:
            top = min(ymax, y + step)
            x = xmin
            while x < xmax:
                right = min(xmax, x + step)
                keep = True
                if to_wgs84 is not None:
                    box = to_wgs84.transformBoundingBox(
                        QgsRectangle(x, y, right, top))
                    keep = roi.geometry.intersects(QgsGeometry.fromRect(box))
                if keep:
                    chunks.append((x, y, right, top,
                                   int(round((right - x) / self.resolution_m)),
                                   int(round((top - y) / self.resolution_m))))
                x = right
            y = top
        return epsg, chunks

    def pixel_estimate(self, roi):
        _epsg, xmin, ymin, xmax, ymax = self._grid(roi)
        res = self.resolution_m
        return int((xmax - xmin) / res) * int((ymax - ymin) / res)

    def extra_rows(self, roi, year):
        epsg, chunks = self._chunks(roi)
        return [
            ('Grid', 'EPSG:{0} (UTM), 10 m - as published'.format(epsg)),
            ('Download', '{0} request{1} to the Esri image service'.format(
                len(chunks), '' if len(chunks) == 1 else 's')),
        ]

    def rasters(self, item, on_progress=None):
        epsg, chunks = self._chunks(item.roi)
        if not chunks:
            raise DemError('The area does not cover any land-cover pixels.')
        rule = json.dumps({'mosaicMethod': 'esriMosaicNone',
                           'where': 'Year={0}'.format(int(item.year))},
                          separators=(',', ':'))
        paths = []
        any_class = False
        for index, (xmin, ymin, xmax, ymax, width, height) in enumerate(chunks):
            text = 'Downloading {0} - part {1} of {2}...'.format(
                item.title, index + 1, len(chunks))
            if on_progress is not None \
                    and on_progress(index / float(len(chunks)), text) is False:
                return None
            key = 'y{0}_e{1}_{2:.0f}_{3:.0f}_{4}x{5}'.format(
                item.year, epsg, xmin, ymin, width, height)
            path = cached_path(self.id, key, '.tif')
            if not is_fresh(path):
                url = '{0}/exportImage?{1}'.format(self.SERVICE, urlencode({
                    'bbox': '{0:.0f},{1:.0f},{2:.0f},{3:.0f}'.format(
                        xmin, ymin, xmax, ymax),
                    'bboxSR': epsg, 'imageSR': epsg,
                    'size': '{0},{1}'.format(width, height),
                    'format': 'tiff', 'pixelType': 'U8', 'noData': NODATA,
                    'compression': 'LZ77',
                    'interpolation': 'RSP_NearestNeighbor',
                    'mosaicRule': rule, 'f': 'image'}))

                def chunk_progress(received, total, _i=index):
                    if on_progress is None:
                        return True
                    part = (received / float(total)) if total > 0 else 0.0
                    return on_progress((_i + part) / float(len(chunks)), text)

                if not download_to(url, path, chunk_progress):
                    return None
                if not _opens_as_raster(path):
                    reason = _esri_reason(path)
                    _remove(path)
                    raise DemError(reason or 'The Esri image service returned '
                                             'something that is not a raster.')
            any_class = any_class or _has_data(path)
            paths.append(path)
        if not any_class:
            raise DemError(
                'Esri Land Cover has no data for {0} over that area. The year '
                'may not be published yet, or the area is open sea.'.format(
                    item.year))
        return paths


# ------------------------------------------------------------ ESA WorldCover

class WorldCoverSource(LandcoverSource):
    """ESA WorldCover 10 m, Cloud Optimised GeoTIFFs in the AWS open bucket."""

    id = 'esa_worldcover'
    label = 'ESA WorldCover 10 m (2020, 2021)'
    short_label = 'ESA WorldCover'
    publisher = 'European Space Agency, WorldCover consortium'
    homepage = 'https://esa-worldcover.org'
    licence = 'CC BY 4.0'
    note = ('2020 (v100) and 2021 (v200) were made with different algorithm '
            'versions, so a difference between them is not necessarily real '
            'change on the ground.')
    CLASSES = WORLDCOVER_CLASSES

    BUCKET = 'https://esa-worldcover.s3.eu-central-1.amazonaws.com'
    VERSIONS = {2020: 'v100', 2021: 'v200'}

    def years(self):
        return sorted(self.VERSIONS)

    def attribution_for(self, year):
        return ('(c) ESA WorldCover project {0} / Contains modified Copernicus '
                'Sentinel data ({0}) processed by ESA WorldCover consortium. '
                'CC BY 4.0'.format(year))

    def pixel_estimate(self, roi):
        west, south, east, north = roi.bounds()
        # 1/12000 degree pixels, which is 10 m at the equator.
        return int((east - west) * 12000) * int((north - south) * 12000)

    def tile_names(self, roi):
        west, south, east, north = roi.bounds()
        names = []
        lat = int(math.floor(south / 3.0)) * 3
        while lat < north:
            lon = int(math.floor(west / 3.0)) * 3
            while lon < east:
                if not roi.clip or roi.geometry.intersects(
                        QgsGeometry.fromRect(QgsRectangle(lon, lat, lon + 3,
                                                          lat + 3))):
                    names.append('{0}{1:02d}{2}{3:03d}'.format(
                        'N' if lat >= 0 else 'S', abs(lat),
                        'E' if lon >= 0 else 'W', abs(lon)))
                lon += 3
            lat += 3
        return names

    def extra_rows(self, roi, year):
        count = len(self.tile_names(roi))
        return [('Download', 'streamed from {0} tile{1} - only the part '
                             'inside the area is read'.format(
                                 count, '' if count == 1 else 's'))]

    def _existing(self):
        """The tiles WorldCover publishes, or None if unknown."""
        path = cached_path(self.id, 'grid', '.geojson')
        if not is_fresh(path, METADATA_TTL):
            try:
                download_to('{0}/esa_worldcover_grid.geojson'.format(
                    self.BUCKET), path)
            except NetError:
                if not os.path.exists(path):
                    return None
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
            return {f['properties']['ll_tile']
                    for f in payload.get('features', [])}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def rasters(self, item, on_progress=None):
        version = self.VERSIONS[item.year]
        names = self.tile_names(item.roi)
        existing = self._existing()
        if existing is not None:
            names = [name for name in names if name in existing]
        if not names:
            raise DemError('ESA WorldCover has no tiles over that area - it is '
                           'open sea as far as the map is concerned.')
        return ['/vsicurl/{0}/{1}/{2}/map/ESA_WorldCover_10m_{2}_{1}_{3}_Map'
                '.tif'.format(self.BUCKET, version, item.year, name)
                for name in names]


# -------------------------------------------------------------- writing it out

def write_landcover(rasters, item, out_path, dst_crs=None, progress=None):
    """Mosaic and clip `rasters`, then give the result its classes.

    Nearest-neighbour throughout: a class code averaged with its neighbour is
    a different class, not a smoother one. Returns True when written, False
    when cancelled; raises :class:`DemError` otherwise.
    """
    from osgeo import gdal

    written = warp_to_file(rasters, item.roi, out_path, dst_crs, progress,
                           nodata=NODATA, resample='near',
                           output_type=gdal.GDT_Byte, snap=True)
    if written:
        apply_classes(out_path, item.classes)
    return written


def apply_classes(path, classes):
    """Write the colour table, class names and attribute table into `path`.

    Returns {code: pixel count} for the classes present.
    """
    from osgeo import gdal

    with _gdal_exceptions(gdal):
        handle = gdal.Open(path, gdal.GA_Update)
        band = handle.GetRasterBand(1)
        band.SetNoDataValue(NODATA)

        table = gdal.ColorTable()
        table.SetColorEntry(NODATA, (0, 0, 0, 0))
        for lc in classes:
            table.SetColorEntry(lc.code, lc.rgb() + (255,))
        band.SetRasterColorTable(table)
        band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)

        names = [''] * (max(lc.code for lc in classes) + 1)
        names[NODATA] = 'No data'
        for lc in classes:
            names[lc.code] = lc.name
        band.SetCategoryNames(names)

        histogram = band.GetHistogram(-0.5, 255.5, 256, 0, 0)
        counts = {lc.code: int(histogram[lc.code]) for lc in classes}

        rat = gdal.RasterAttributeTable()
        columns = (('Value', gdal.GFT_Integer, gdal.GFU_MinMax),
                   ('Count', gdal.GFT_Real, gdal.GFU_PixelCount),
                   ('lc_code', gdal.GFT_Integer, gdal.GFU_Generic),
                   ('lc_class', gdal.GFT_String, gdal.GFU_Name),
                   ('Red', gdal.GFT_Integer, gdal.GFU_Red),
                   ('Green', gdal.GFT_Integer, gdal.GFU_Green),
                   ('Blue', gdal.GFT_Integer, gdal.GFU_Blue))
        for name, kind, usage in columns:
            rat.CreateColumn(name, kind, usage)
        rat.SetRowCount(len(classes))
        for row, lc in enumerate(classes):
            red, green, blue = lc.rgb()
            rat.SetValueAsInt(row, 0, lc.code)
            rat.SetValueAsDouble(row, 1, float(counts[lc.code]))
            rat.SetValueAsInt(row, 2, lc.code)
            rat.SetValueAsString(row, 3, lc.name)
            rat.SetValueAsInt(row, 4, red)
            rat.SetValueAsInt(row, 5, green)
            rat.SetValueAsInt(row, 6, blue)
        band.SetDefaultRAT(rat)
        band.FlushCache()
        band = None
        handle = None
    return {code: count for code, count in counts.items() if count}


def polygonize(raster_path, gpkg_path, classes, progress=None):
    """Turn the classified raster into polygons with lc_code and lc_class.

    Writes one GeoPackage layer, :data:`POLYGON_LAYER`. NoData becomes no
    feature at all. Returns True when written, False when cancelled.
    """
    from osgeo import gdal, ogr

    state = {'cancelled': False}

    def callback(complete, _message, _data):
        if progress is not None and progress(complete) is False:
            state['cancelled'] = True
            return 0
        return 1

    with _gdal_exceptions(gdal):
        raster = gdal.Open(raster_path)
        band = raster.GetRasterBand(1)
        if band.XSize * band.YSize > MAX_POLYGON_PIXELS:
            raise DemError(
                'The area is too large to turn into polygons: {0:,} million '
                'pixels, where the limit is {1:,} million. The raster was '
                'added; choose a smaller area for polygons.'.format(
                    band.XSize * band.YSize // 1000000,
                    MAX_POLYGON_PIXELS // 1000000))
        driver = ogr.GetDriverByName('GPKG')
        if os.path.exists(gpkg_path):
            driver.DeleteDataSource(gpkg_path)
        target = driver.CreateDataSource(gpkg_path)
        layer = target.CreateLayer(POLYGON_LAYER, raster.GetSpatialRef(),
                                   ogr.wkbPolygon)
        layer.CreateField(ogr.FieldDefn('lc_code', ogr.OFTInteger))
        name_field = ogr.FieldDefn('lc_class', ogr.OFTString)
        name_field.SetWidth(max(len(lc.name) for lc in classes))
        layer.CreateField(name_field)

        target.StartTransaction()
        try:
            gdal.Polygonize(band, band.GetMaskBand(), layer, 0, [],
                            callback=callback)
        except RuntimeError:
            if not state['cancelled']:
                target.RollbackTransaction()
                layer = None
                target = None
                # Not a half-written file left beside the raster.
                driver.DeleteDataSource(gpkg_path)
                raise
        if state['cancelled']:
            target.RollbackTransaction()
            layer = None
            target = None
            driver.DeleteDataSource(gpkg_path)
            return False
        target.CommitTransaction()

        lookup = target.CreateLayer(LOOKUP_LAYER, None, ogr.wkbNone)
        lookup.CreateField(ogr.FieldDefn('lc_code', ogr.OFTInteger))
        lookup.CreateField(ogr.FieldDefn('lc_name', ogr.OFTString))
        for lc in classes:
            row = ogr.Feature(lookup.GetLayerDefn())
            row.SetField('lc_code', int(lc.code))
            row.SetField('lc_name', lc.name)
            lookup.CreateFeature(row)
        lookup = None
        target.ExecuteSQL(FILL_CLASS_NAMES)
        for index in range(target.GetLayerCount()):
            if target.GetLayerByIndex(index).GetName() == LOOKUP_LAYER:
                target.DeleteLayer(index)
                break
        layer = None
        target = None
    return True


# ------------------------------------------------------------------- helpers

def _box_hash(roi):
    west, south, east, north = roi.bounds()
    box = '{0:.6f},{1:.6f},{2:.6f},{3:.6f},{4}'.format(
        west, south, east, north, roi.clip)
    return hashlib.sha256(box.encode('ascii')).hexdigest()[:16]


def _read_years(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            years = [int(y) for y in json.load(handle)]
        return sorted(years)
    except (OSError, ValueError, TypeError):
        return []


def _write_years(path, years):
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(list(years), handle)
    except OSError:                         # pragma: no cover - disk full etc
        pass


def _has_data(path):
    """True when the raster holds at least one classified pixel."""
    from osgeo import gdal

    with _gdal_exceptions(gdal):
        try:
            # The dataset is held in its own name: a band outlives nothing,
            # and GDAL frees it with the dataset.
            handle = gdal.Open(path)
            histogram = handle.GetRasterBand(1).GetHistogram(
                -0.5, 255.5, 256, 0, 0)
            handle = None
        except RuntimeError:
            return False
    return any(histogram[1:])


def _esri_reason(path):
    """The sentence out of an ArcGIS error reply saved where a TIFF should be."""
    try:
        with open(path, 'rb') as handle:
            payload = json.loads(handle.read(8192).decode('utf-8', 'replace'))
        error = payload.get('error') or {}
        message = error.get('message') or ''
        details = ' '.join(error.get('details') or [])
        text = ' '.join(part for part in (message, details) if part).strip()
    except (OSError, ValueError, AttributeError):
        return None
    return ('The Esri image service refused the request: {0}'.format(text)
            if text else None)


def _remove(path):
    try:
        os.remove(path)
    except OSError:                         # pragma: no cover - locked file
        pass


# ------------------------------------------------------------------- registry

LANDCOVER_SOURCES = (
    EsriLandcoverSource(),
    WorldCoverSource(),
)

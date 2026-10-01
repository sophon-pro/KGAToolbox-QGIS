# -*- coding: utf-8 -*-
"""Open digital elevation models for the Add Open Data tool.

A DEM does not fit the country-and-level shape of the boundary sources. It is
asked for by *area*: the user names a region of interest (ROI) and gets back
one GeoTIFF covering it. Everything here is organised around that:

* :class:`Roi` is the region, always held in EPSG:4326 because every open DEM
  published here is served in geographic coordinates. It remembers whether it
  is only a bounding box (the map extent, a layer's extent) or a real shape to
  cut the raster to (a drawn polygon, a layer's boundary).
* The sources are ordinary :class:`~.data_sources.DataSource` subclasses, so
  the dialog lists them, fills their option rows and calls ``describe`` the way
  it does for geoBoundaries. ``describe`` never touches the network: it works
  out the tiles, the pixel count and the size from the ROI alone, so the
  details panel says what a request will cost before it is made.
* :func:`warp_to_file` is the one place a raster is written. Reprojection,
  clipping to the ROI and compression all happen in a single ``gdal.Warp``.

Two sources ship:

**Copernicus DEM on AWS** needs no account. The GLO-30 and GLO-90 tiles are
Cloud Optimised GeoTIFFs in a public bucket, so they are read through GDAL's
``/vsicurl/`` - only the blocks inside the ROI cross the network. A 1 degree
GLO-30 tile is 48 MB; a district cut out of it is a few MB of traffic.

**OpenTopography** offers the wider catalogue (SRTM, NASADEM, ALOS, GEBCO and
more) through one REST call that returns the bounding box as a GeoTIFF. It
needs a free API key, which is kept in the QGIS authentication database rather
than in plain settings - see :func:`load_api_key`.

No Qt widgets here. The dialog imports this; this must never import the dialog.
"""

import hashlib
import math
import os
import re
import uuid
from contextlib import suppress
from html import unescape
from urllib.parse import quote

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsDistanceArea, QgsGeometry,
                       QgsProject, QgsRectangle, QgsSettings)

from .data_sources import KIND_DEM, DataSource, Option, SourceItem
from .net import (METADATA_TTL, NetError, cached_path, download_to, fetch_body,
                  is_fresh, user_agent)

#: The region-of-interest modes, in the order the dialog lists them.
ROI_CANVAS = 'canvas'
ROI_DRAW = 'draw'
ROI_LAYER_CLIP = 'layer_clip'
ROI_LAYER_EXTENT = 'layer_extent'

#: Pixels a single request may produce before it is refused. 700 million is
#: about 2.8 GB of float32 before compression - a 50 x 50 km district at
#: GLO-30 is 3.5 million, a whole Cambodia at GLO-90 is 30 million. Beyond this
#: the warp runs for many minutes on the GUI thread, and the answer is a
#: coarser dataset or a smaller area, which the refusal says.
MAX_PIXELS = 700 * 1000 * 1000

#: Written where the ROI does not reach, when the raster is cut to a shape.
#: Fits Int16 (SRTM) as well as Float32 (Copernicus) and is below any real
#: elevation or bathymetry.
NODATA = -32767

WGS84 = 'EPSG:4326'


class DemError(NetError):
    """A request that cannot be made as it stands. Shown to the user as-is."""


# ------------------------------------------------------------------------ ROI

class Roi(object):
    """A region of interest in EPSG:4326.

    `clip` is False for a plain extent: the output is the bounding box. When it
    is True the raster is cut to `geometry`, and pixels outside it are NoData.
    """

    def __init__(self, geometry, clip, label):
        self.geometry = geometry
        self.clip = clip
        self.label = label

    def bounds(self):
        """(west, south, east, north), clamped to the globe."""
        box = self.geometry.boundingBox()
        return (max(-180.0, box.xMinimum()), max(-90.0, box.yMinimum()),
                min(180.0, box.xMaximum()), min(90.0, box.yMaximum()))

    def is_empty(self):
        west, south, east, north = self.bounds()
        return not (east > west and north > south)

    def area_km2(self):
        """The ROI's own area on the ellipsoid - the shape, not its box."""
        return _ellipsoid_area_km2(self.geometry)

    def bbox_area_km2(self):
        """The bounding box's area, which is what a bbox API bills against."""
        west, south, east, north = self.bounds()
        return _ellipsoid_area_km2(QgsGeometry.fromRect(
            QgsRectangle(west, south, east, north)))

    def geojson(self):
        return ('{"type":"FeatureCollection","features":[{"type":"Feature",'
                '"properties":{},"geometry":' + self.geometry.asJson(7) + '}]}')

    def describe_bounds(self):
        west, south, east, north = self.bounds()
        return '{0} to {1}, {2} to {3}'.format(
            _lon(west), _lon(east), _lat(south), _lat(north))


def _wgs84():
    return QgsCoordinateReferenceSystem(WGS84)


def _to_wgs84(crs):
    return QgsCoordinateTransform(crs, _wgs84(), QgsProject.instance())


def roi_from_extent(rectangle, crs, label):
    """A bounding-box ROI from an extent in `crs` (the canvas, a layer)."""
    if rectangle is None or rectangle.isEmpty():
        raise DemError('That extent is empty.')
    try:
        box = _to_wgs84(crs).transformBoundingBox(rectangle)
    except Exception:
        raise DemError('The extent could not be converted to latitude and '
                       'longitude. Check the layer or project CRS.')
    roi = Roi(QgsGeometry.fromRect(box), False, label)
    if roi.is_empty():
        raise DemError('That extent lies outside the globe once converted to '
                       'latitude and longitude.')
    return roi


def roi_from_geometry(geometry, crs, label):
    """A cut-to-shape ROI from a drawn or selected geometry in `crs`."""
    if geometry is None or geometry.isEmpty():
        raise DemError('The area is empty.')
    geometry = QgsGeometry(geometry)
    # A rectangle drawn in a projected CRS has four vertices; its sides bend
    # once converted to degrees, so they get vertices of their own first.
    geometry = geometry.densifyByCount(16) if _vertex_count(geometry) < 64 \
        else geometry
    try:
        geometry.transform(_to_wgs84(crs))
    except Exception:
        raise DemError('The area could not be converted to latitude and '
                       'longitude. Check the layer or project CRS.')
    if not geometry.isGeosValid():
        geometry = geometry.makeValid()
    roi = Roi(geometry, True, label)
    if roi.is_empty():
        raise DemError('The area lies outside the globe once converted to '
                       'latitude and longitude.')
    return roi


def roi_from_layer(layer, selected_only):
    """A cut-to-shape ROI from a polygon layer's features, dissolved."""
    if layer is None:
        raise DemError('Choose a polygon layer to clip to.')
    if selected_only:
        features = layer.getSelectedFeatures()
        if layer.selectedFeatureCount() == 0:
            raise DemError('"{0}" has no selected features. Select the ones '
                           'to clip to, or untick "Selected features only".'
                           .format(layer.name()))
    else:
        features = layer.getFeatures()
    parts = [f.geometry() for f in features
             if f.hasGeometry() and not f.geometry().isEmpty()]
    if not parts:
        raise DemError('"{0}" has no polygons to clip to.'.format(layer.name()))
    merged = QgsGeometry.unaryUnion(parts)
    if merged is None or merged.isEmpty():
        raise DemError('The polygons of "{0}" could not be merged into one '
                       'boundary. Run Fix Geometries on the layer first.'
                       .format(layer.name()))
    label = 'Boundary of {0}{1}'.format(
        layer.name(), ' (selected)' if selected_only else '')
    return roi_from_geometry(merged, layer.crs(), label)


def _vertex_count(geometry):
    try:
        return sum(1 for _ in geometry.vertices())
    except Exception:                       # pragma: no cover - defensive
        return 0


def _ellipsoid_area_km2(geometry):
    area = QgsDistanceArea()
    area.setSourceCrs(_wgs84(), QgsProject.instance().transformContext())
    area.setEllipsoid('WGS84')
    try:
        return area.measureArea(geometry) / 1e6
    except Exception:                       # pragma: no cover - defensive
        return 0.0


def _lon(value):
    return '{0:.4f}{1}'.format(abs(value), 'E' if value >= 0 else 'W')


def _lat(value):
    return '{0:.4f}{1}'.format(abs(value), 'N' if value >= 0 else 'S')


# ------------------------------------------------------------------- datasets

class Dataset(object):
    """One elevation product: what it is, how fine, and who to credit."""

    def __init__(self, id, label, arcsec, dtype_bytes, surface, licence,
                 attribution, max_km2=None, coverage='Global', note=''):
        self.id = id
        self.label = label
        #: Pixel size in arc seconds, for the size estimate.
        self.arcsec = arcsec
        self.dtype_bytes = dtype_bytes
        #: 'DSM' (top of trees and roofs), 'DTM' (bare earth), or 'Bathymetry'.
        self.surface = surface
        self.licence = licence
        self.attribution = attribution
        #: The server's own bounding-box limit, so a request it would refuse
        #: is refused here first, with a better sentence.
        self.max_km2 = max_km2
        self.coverage = coverage
        self.note = note

    def resolution_text(self):
        metres = self.arcsec * 30.87
        if metres >= 1000:
            return '{0:g} arc-second (~{1:.0f} km)'.format(
                self.arcsec, metres / 1000)
        return '{0:g} arc-second (~{1:.0f} m)'.format(self.arcsec, metres)


COPERNICUS_LICENCE = 'Copernicus DEM licence - free, attribution required'
COPERNICUS_ATTRIBUTION = (
    'Copernicus DEM (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space '
    'GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA; '
    'all rights reserved')
COPERNICUS_NOTE = ('A surface model: it follows the tops of trees and '
                   'buildings, not the ground beneath them.')
SRTM_ATTRIBUTION = ('NASA Shuttle Radar Topography Mission (SRTM), '
                    'NASA JPL / USGS')


class DemSource(DataSource):
    """Base for the DEM sources: an ROI in, a list of rasters out."""

    kind = KIND_DEM
    needs_country = False
    #: Whether the dialog should show the API key row.
    needs_api_key = False
    DATASETS = ()

    def options(self):
        return [Option('dataset', 'Dataset', [
            (d.id, d.label) for d in self.DATASETS],
            tooltip='Which elevation model to read. Coarser models cover '
                    'larger areas in one request.')]

    def dataset(self, options):
        wanted = (options or {}).get('dataset')
        for dataset in self.DATASETS:
            if dataset.id == wanted:
                return dataset
        return self.DATASETS[0]

    def levels(self, iso3, options=None):
        return []

    def describe(self, iso3, level, options=None):
        options = options or {}
        roi = options.get('roi')
        if roi is None:
            raise DemError(options.get('roi_error') or
                           'Choose the area to download.')
        dataset = self.dataset(options)
        self.check(roi, dataset, options)

        west, south, east, north = roi.bounds()
        per_degree = 3600.0 / dataset.arcsec
        pixels = int((east - west) * per_degree * (north - south) * per_degree)
        if pixels > MAX_PIXELS:
            raise DemError(
                'That area is too large for {0}: about {1:,} million pixels, '
                'where one request is limited to {2:,} million. Choose a '
                'smaller area or a coarser dataset.'.format(
                    dataset.label.split(' - ')[0], pixels // 1000000,
                    MAX_PIXELS // 1000000))

        rows = [
            ('Area', '{0} - {1:,.0f} km2'.format(roi.label, roi.area_km2())),
            ('Bounds', roi.describe_bounds()),
            ('Resolution', dataset.resolution_text()),
            ('Surface', dataset.surface),
            ('Output', '{0:,} pixels, about {1} before compression'.format(
                pixels, _human(pixels * dataset.dtype_bytes))),
        ]
        rows.extend(self.extra_rows(roi, dataset))
        notes = [dataset.note] if dataset.note else []
        notes.append('Clipped to the shape; pixels outside it are NoData.'
                     if roi.clip else 'Covers the bounding box of the area.')

        item = SourceItem(
            title=self.title(dataset, roi),
            cache_key=self.cache_key(dataset, roi),
            extension='.tif',
            licence=dataset.licence,
            attribution=dataset.attribution,
            publisher=self.publisher,
            source_url=self.homepage,
            notes=' '.join(notes),
        )
        item.extra_rows = rows
        item.roi = roi
        item.dataset = dataset
        item.api_key = (options.get('api_key') or '').strip()
        return item

    def title(self, dataset, roi):
        return '{0} DEM'.format(dataset.label.split(' - ')[0])

    def cache_key(self, dataset, roi):
        west, south, east, north = roi.bounds()
        box = '{0:.6f},{1:.6f},{2:.6f},{3:.6f}'.format(west, south, east, north)
        return '{0}_{1}'.format(dataset.id, hashlib.sha256(
            box.encode('ascii')).hexdigest()[:16])

    def check(self, roi, dataset, options):
        """Raise :class:`DemError` for a request the server would refuse."""

    def extra_rows(self, roi, dataset):
        return []

    def rasters(self, item, on_progress=None):
        """The rasters `warp_to_file` reads, or None when the user cancelled."""
        raise NotImplementedError


# ------------------------------------------------------------ Copernicus / AWS

class CopernicusAwsSource(DemSource):
    """Copernicus GLO-30 / GLO-90, read straight out of the AWS open bucket.

    The tiles are 1 x 1 degree Cloud Optimised GeoTIFFs named after their
    south-west corner. Rather than download them whole - 48 MB each at GLO-30 -
    a VRT over their ``/vsicurl/`` addresses is handed to ``gdal.Warp``, and
    GDAL fetches only the internal blocks inside the ROI with HTTP range
    requests.

    Over open sea there is no tile at all. The bucket publishes the list of
    tiles that exist, and that list (about 1 MB) is cached for a week so the
    sea is skipped without a round trip per missing tile.
    """

    id = 'copernicus_aws'
    label = 'Copernicus DEM (AWS open data, no account needed)'
    publisher = 'European Space Agency / Airbus, via AWS Open Data'
    homepage = 'https://registry.opendata.aws/copernicus-dem/'
    attribution = COPERNICUS_ATTRIBUTION

    DATASETS = (
        Dataset('glo30', 'Copernicus GLO-30 - 1 arc-second (~30 m)', 1, 4,
                'DSM', COPERNICUS_LICENCE, COPERNICUS_ATTRIBUTION,
                note=COPERNICUS_NOTE),
        Dataset('glo90', 'Copernicus GLO-90 - 3 arc-second (~90 m)', 3, 4,
                'DSM', COPERNICUS_LICENCE, COPERNICUS_ATTRIBUTION,
                note=COPERNICUS_NOTE),
    )

    BUCKETS = {
        'glo30': ('https://copernicus-dem-30m.s3.amazonaws.com', '10'),
        'glo90': ('https://copernicus-dem-90m.s3.amazonaws.com', '30'),
    }

    def tile_names(self, roi, dataset):
        """Every tile name the ROI's bounding box touches, sea included."""
        _base, code = self.BUCKETS[dataset.id]
        west, south, east, north = roi.bounds()
        names = []
        for lat in range(int(math.floor(south)), int(math.ceil(north))):
            for lon in range(int(math.floor(west)), int(math.ceil(east))):
                names.append('Copernicus_DSM_COG_{0}_{1}{2:02d}_00_{3}{4:03d}'
                             '_00_DEM'.format(code, 'N' if lat >= 0 else 'S',
                                              abs(lat), 'E' if lon >= 0 else 'W',
                                              abs(lon)))
        return names

    def extra_rows(self, roi, dataset):
        count = len(self.tile_names(roi, dataset))
        return [('Download', 'streamed from {0} tile{1} - only the part '
                             'inside the area is read'.format(
                                 count, '' if count == 1 else 's'))]

    def cached_file(self, item):
        # Nothing is cached: the tiles are read in place.
        return None

    def _existing(self, dataset):
        """The set of tiles the bucket actually holds, or None if unknown."""
        base, _code = self.BUCKETS[dataset.id]
        path = cached_path(self.id, '{0}_tileList'.format(dataset.id), '.txt')
        if not is_fresh(path, METADATA_TTL):
            try:
                download_to('{0}/tileList.txt'.format(base), path)
            except NetError:
                if not os.path.exists(path):
                    # Without the list every tile is tried, and GDAL skips the
                    # ones that are not there. Slower over sea, never wrong.
                    return None
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return {line.strip() for line in handle if line.strip()}
        except OSError:                     # pragma: no cover - locked file
            return None

    def rasters(self, item, on_progress=None):
        dataset = item.dataset
        base, _code = self.BUCKETS[dataset.id]
        names = self.tile_names(item.roi, dataset)
        existing = self._existing(dataset)
        if existing is not None:
            names = [name for name in names if name in existing]
        if not names:
            raise DemError('{0} has no tiles over that area - it is open sea '
                           'as far as the DEM is concerned.'.format(
                               dataset.label.split(' - ')[0]))
        return ['/vsicurl/{0}/{1}/{1}.tif'.format(base, name) for name in names]


# -------------------------------------------------------------- OpenTopography

OPENTOPO_SIGNUP = 'https://portal.opentopography.org/requestService?service=api'


class OpenTopographySource(DemSource):
    """OpenTopography's Global DEM API.

    One GET returns the ROI's bounding box as a GeoTIFF, from any of a dozen
    global models. The price is a free API key, and a per-request area limit
    that depends on the dataset; both are checked here before anything is sent,
    because the server's own refusal is an XML fragment, not a sentence.
    """

    id = 'opentopography'
    label = 'OpenTopography Global DEM API (free API key)'
    publisher = 'OpenTopography (NSF)'
    homepage = 'https://opentopography.org'
    attribution = 'Data provided by OpenTopography (opentopography.org)'
    needs_api_key = True

    API = ('https://portal.opentopography.org/API/globaldem?demtype={demtype}'
           '&south={south:.6f}&north={north:.6f}&west={west:.6f}&east={east:.6f}'
           '&outputFormat=GTiff&API_Key={key}')

    #: The service's documented limits: 4,050,000 km2 for SRTM GL3 and COP90,
    #: 450,000 km2 for the 30 m products. The 15 arc-second and 1 km products
    #: are left to the server, which answers in plain words.
    DATASETS = (
        Dataset('COP30', 'Copernicus GLO-30 - 30 m', 1, 4, 'DSM',
                COPERNICUS_LICENCE, COPERNICUS_ATTRIBUTION, 450000,
                note=COPERNICUS_NOTE),
        Dataset('COP90', 'Copernicus GLO-90 - 90 m', 3, 4, 'DSM',
                COPERNICUS_LICENCE, COPERNICUS_ATTRIBUTION, 4050000,
                note=COPERNICUS_NOTE),
        Dataset('SRTMGL1', 'SRTM GL1 - 30 m', 1, 2, 'DSM', 'Public domain',
                SRTM_ATTRIBUTION, 450000, '60N to 56S'),
        Dataset('SRTMGL1_E', 'SRTM GL1 Ellipsoidal - 30 m', 1, 2, 'DSM',
                'Public domain', SRTM_ATTRIBUTION, 450000, '60N to 56S',
                'Heights above the WGS84 ellipsoid, not above sea level.'),
        Dataset('SRTMGL3', 'SRTM GL3 - 90 m', 3, 2, 'DSM', 'Public domain',
                SRTM_ATTRIBUTION, 4050000, '60N to 56S'),
        Dataset('NASADEM', 'NASADEM - 30 m', 1, 2, 'DSM', 'Public domain',
                'NASADEM, NASA JPL', 450000, '60N to 56S',
                'SRTM reprocessed by NASA with fewer voids.'),
        Dataset('AW3D30', 'ALOS World 3D - 30 m', 1, 2, 'DSM',
                'JAXA - free, attribution required', '(c) JAXA ALOS AW3D30',
                450000),
        Dataset('AW3D30_E', 'ALOS World 3D Ellipsoidal - 30 m', 1, 2, 'DSM',
                'JAXA - free, attribution required', '(c) JAXA ALOS AW3D30',
                450000, note='Heights above the WGS84 ellipsoid, not above '
                             'sea level.'),
        Dataset('EU_DTM', 'EU DTM - 30 m (Europe only)', 1, 4, 'DTM',
                'CC BY 4.0', 'Ensemble Digital Terrain Model of Europe, '
                'OpenGeoHub', 450000, 'Continental Europe'),
        Dataset('SRTM15Plus', 'SRTM15+ - 500 m, land and sea floor', 15, 2,
                'Bathymetry', 'Public domain',
                'SRTM15+ V2 (Tozer et al. 2019)'),
        Dataset('GEBCOIceTopo', 'GEBCO Ice Surface - 500 m, land and sea '
                'floor', 15, 2, 'Bathymetry', 'Public domain',
                'GEBCO Compilation Group, GEBCO Grid'),
        Dataset('GEBCOSubIceTopo', 'GEBCO Sub-Ice - 500 m, land and sea '
                'floor', 15, 2, 'Bathymetry', 'Public domain',
                'GEBCO Compilation Group, GEBCO Grid'),
        Dataset('GEDI_L3', 'GEDI L3 - 1 km, ground elevation', 30, 4, 'DTM',
                'Public domain', 'NASA GEDI Level 3', None, '52N to 52S'),
    )

    def check(self, roi, dataset, options):
        if not (options or {}).get('api_key'):
            raise DemError(
                'OpenTopography needs an API key. Registration is free: '
                '<a href="{0}">request a key</a>, then paste it into the '
                '<b>API key</b> box. Or choose the Copernicus DEM on AWS, '
                'which needs no account.'.format(OPENTOPO_SIGNUP))
        if dataset.max_km2 and roi.bbox_area_km2() > dataset.max_km2:
            raise DemError(
                'OpenTopography limits {0} to {1:,} km2 per request, and the '
                'bounding box of that area is {2:,.0f} km2. Choose a smaller '
                'area, or a 90 m dataset.'.format(
                    dataset.label.split(' - ')[0], dataset.max_km2,
                    roi.bbox_area_km2()))

    def extra_rows(self, roi, dataset):
        rows = [('Coverage', dataset.coverage)]
        if dataset.max_km2:
            rows.append(('Request', '{0:,.0f} of {1:,} km2 allowed'.format(
                roi.bbox_area_km2(), dataset.max_km2)))
        return rows

    def rasters(self, item, on_progress=None):
        path = cached_path(self.id, item.cache_key, '.tif')
        if is_fresh(path):
            return [path]
        west, south, east, north = item.roi.bounds()
        url = self.API.format(demtype=item.dataset.id, south=south,
                              north=north, west=west, east=east,
                              key=quote(item.api_key, safe=''))
        try:
            if not download_to(url, path, on_progress):
                return None
        except NetError:
            # The download error is generic; the service says why in XML.
            raise DemError(_opentopo_reason(url) or
                           'OpenTopography could not supply that area.')

        if not _opens_as_raster(path):
            reason = _opentopo_reason_from_file(path)
            try:
                os.remove(path)
            except OSError:                 # pragma: no cover - locked file
                pass
            raise DemError(reason or 'OpenTopography returned something that '
                                     'is not a raster.')
        return [path]


def _opentopo_reason(url):
    try:
        _status, body = fetch_body(url)
    except NetError:
        return None
    return _xml_error(body)


def _opentopo_reason_from_file(path):
    try:
        with open(path, 'rb') as handle:
            return _xml_error(handle.read(4096))
    except OSError:                         # pragma: no cover - vanished file
        return None


def _xml_error(body):
    """'Error: API Key required...' out of OpenTopography's XML answer.

    The reply is a one-line ``<error>`` element, so it is read with a pattern
    rather than an XML parser: the body comes from a remote server, and the
    standard-library parser is open to entity-expansion attacks that a
    sentence-sized answer has no need to risk.
    """
    if not body:
        return None
    text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else body
    text = text[:4096].strip()
    found = re.search(r'<error[^>]*>(.*?)</error>', text, re.S | re.I)
    if found:
        message = unescape(found.group(1)).strip()
    elif '<' not in text and len(text) < 400:
        message = text
    else:
        message = ''
    if not message:
        return None
    message = message.replace('Error:', '').strip()
    if 'api key' in message.lower() or 'unauthori' in message.lower():
        return ('OpenTopography did not accept the API key: {0}'.format(
            message))
    return 'OpenTopography refused the request: {0}'.format(message)


def _opens_as_raster(path):
    from osgeo import gdal

    with _gdal_exceptions(gdal):
        try:
            handle = gdal.Open(path)
        except RuntimeError:
            return False
        return handle is not None and handle.RasterCount > 0


# --------------------------------------------------------------- the API key

#: Where the OpenTopography key lives in the QGIS authentication database.
API_KEY_SETTING = 'kga_tools/opentopography_api_key'


def load_api_key():
    """The remembered OpenTopography key, or ''.

    Stored encrypted in the QGIS authentication database, the same place QGIS
    keeps every other credential, so the plugin never holds a secret of its
    own. Reading it may prompt once for the QGIS master password.
    """
    try:
        manager = QgsApplication.authManager()
        value = manager.authSetting(API_KEY_SETTING, '', True)
        return str(value or '')
    except Exception:                       # pragma: no cover - auth unavailable
        return ''


def has_saved_api_key():
    """True when a key is stored - checked without decrypting it."""
    try:
        return QgsApplication.authManager().existsAuthSetting(API_KEY_SETTING)
    except Exception:                       # pragma: no cover - auth unavailable
        return False


def save_api_key(key):
    """Store `key` encrypted. Returns False when the store refused it."""
    try:
        manager = QgsApplication.authManager()
        if not key:
            manager.removeAuthSetting(API_KEY_SETTING)
            return True
        return bool(manager.storeAuthSetting(API_KEY_SETTING, key, True))
    except Exception:                       # pragma: no cover - auth unavailable
        return False


# -------------------------------------------------------------- writing it out

def warp_to_file(sources, roi, out_path, dst_crs=None, progress=None,
                 nodata=NODATA, resample='bilinear', output_type=None,
                 snap=False):
    """Mosaic, clip and (optionally) reproject `sources` into one GeoTIFF.

    `sources` are paths or ``/vsicurl/`` addresses, all in one CRS. The ROI
    decides the footprint: its bounding box, or - when ``roi.clip`` - its shape,
    with NoData outside. `dst_crs` is a ``QgsCoordinateReferenceSystem`` to
    reproject into, or None to keep the source's own grid, which is the
    lossless choice. `progress(fraction)` may return False to cancel.

    The defaults suit elevation. Categorical rasters - land cover - pass
    ``resample='near'`` so a class is never averaged into a neighbour that
    means something else, a ``nodata`` that fits their Byte type, and
    ``snap=True``, which keeps the output on the source's own pixel grid when
    it is not reprojected instead of shifting it to the ROI's corner.

    Returns True when the file was written, False when cancelled. Raises
    :class:`DemError` otherwise.
    """
    from osgeo import gdal

    token = uuid.uuid4().hex
    vrt_path = '/vsimem/kga_dem_{0}.vrt'.format(token)
    cut_path = '/vsimem/kga_dem_{0}.geojson'.format(token)
    state = {'cancelled': False}

    def callback(complete, _message, _data):
        if progress is not None and progress(complete) is False:
            state['cancelled'] = True
            return 0
        return 1

    reproject = dst_crs is not None and dst_crs.isValid() \
        and dst_crs.authid() != WGS84
    west, south, east, north = roi.bounds()

    options = dict(
        format='GTiff',
        dstNodata=nodata,
        # Threads for the arithmetic only. `multithread=True` would also move
        # the I/O - and with it the progress callback - onto a worker thread,
        # and the callback drives a Qt progress dialog: that crashes QGIS.
        warpOptions=['NUM_THREADS=ALL_CPUS'],
        creationOptions=['COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER'],
        callback=callback,
    )
    if output_type is not None:
        options['outputType'] = output_type
    if reproject:
        options['dstSRS'] = dst_crs.toWkt()
        options['resampleAlg'] = resample
    elif resample != 'bilinear':
        options['resampleAlg'] = resample
    if roi.clip:
        options['cutlineDSName'] = cut_path
        options['cropToCutline'] = True
    else:
        options['outputBounds'] = (west, south, east, north)
        options['outputBoundsSRS'] = WGS84

    config = _http_config()
    previous = {key: gdal.GetConfigOption(key) for key in config}
    with _gdal_exceptions(gdal):
        try:
            for key, value in config.items():
                gdal.SetConfigOption(key, value)
            if roi.clip:
                gdal.FileFromMemBuffer(cut_path, roi.geojson().encode('utf-8'))
            try:
                vrt = gdal.BuildVRT(vrt_path, list(sources),
                                    resolution='highest')
            except RuntimeError as exc:
                raise DemError(_gdal_message(exc))
            if vrt is None:
                raise DemError('None of the source tiles could be opened.')
            if snap and not reproject:
                # The mosaic's own pixel size, and output corners on
                # multiples of it - which is where the published tiles have
                # theirs - so no pixel is resampled at all.
                transform = vrt.GetGeoTransform()
                x_res, y_res = abs(transform[1]), abs(transform[5])
                options['xRes'] = x_res
                options['yRes'] = y_res
                if roi.clip:
                    options['targetAlignedPixels'] = True
                else:
                    # gdalwarp will not align an explicit extent, so it is
                    # taken into the source CRS and widened to the grid here.
                    options['outputBounds'] = _snapped_bounds(
                        roi, vrt.GetProjection(), x_res, y_res)
                    options.pop('outputBoundsSRS', None)
            try:
                result = gdal.Warp(out_path, vrt, **options)
            except RuntimeError as exc:
                if state['cancelled']:
                    result = None
                else:
                    raise DemError(_gdal_message(exc))
            vrt = None
            if result is None:
                _remove_output(gdal, out_path)
                if state['cancelled']:
                    return False
                raise DemError('GDAL could not write the raster.')
            result.FlushCache()
            result = None
            return True
        finally:
            for key, value in previous.items():
                gdal.SetConfigOption(key, value)
            for path in (vrt_path, cut_path):
                try:
                    gdal.Unlink(path)
                except RuntimeError:
                    pass


def _snapped_bounds(roi, wkt, x_res, y_res):
    """The ROI's box in the CRS `wkt`, widened outward onto the pixel grid."""
    west, south, east, north = roi.bounds()
    box = QgsRectangle(west, south, east, north)
    crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
    if crs.isValid() and crs.authid() != WGS84:
        try:
            box = QgsCoordinateTransform(_wgs84(), crs, QgsProject.instance()) \
                .transformBoundingBox(box)
        except Exception:
            raise DemError('The area could not be converted into the '
                           'coordinate system of the data.')
    return (math.floor(box.xMinimum() / x_res) * x_res,
            math.floor(box.yMinimum() / y_res) * y_res,
            math.ceil(box.xMaximum() / x_res) * x_res,
            math.ceil(box.yMaximum() / y_res) * y_res)


def _remove_output(gdal, path):
    with suppress(Exception):
        gdal.GetDriverByName('GTiff').Delete(path)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:                     # pragma: no cover - locked file
            pass


class _gdal_exceptions(object):
    """``gdal.UseExceptions`` for one block, restoring whatever was there.

    Switching exceptions on for the whole interpreter would change behaviour
    for every other plugin that shares the ``osgeo`` module.
    """

    def __init__(self, gdal):
        self.gdal = gdal
        self.manager = None
        self.previous = None

    def __enter__(self):
        if hasattr(self.gdal, 'ExceptionMgr'):
            self.manager = self.gdal.ExceptionMgr(useExceptions=True)
            self.manager.__enter__()
        else:                               # pragma: no cover - GDAL < 3.7
            self.previous = self.gdal.GetUseExceptions()
            self.gdal.UseExceptions()
        return self

    def __exit__(self, *exc):
        if self.manager is not None:
            return self.manager.__exit__(*exc)
        if not self.previous:               # pragma: no cover - GDAL < 3.7
            self.gdal.DontUseExceptions()
        return False


def _http_config():
    """GDAL's HTTP settings, taken from what the user set in QGIS.

    ``/vsicurl/`` is GDAL's own client, not QGIS's, so it does not see the
    proxy in QGIS Options unless it is handed over. Without this the
    Copernicus source would work at home and fail behind an office proxy.
    """
    config = {
        'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
        'CPL_VSIL_CURL_ALLOWED_EXTENSIONS': '.tif',
        'GDAL_HTTP_MAX_RETRY': '3',
        'GDAL_HTTP_RETRY_DELAY': '2',
        'GDAL_HTTP_USERAGENT': user_agent(),
        'VSI_CACHE': 'TRUE',
    }
    settings = QgsSettings()
    enabled = str(settings.value('proxy/proxyEnabled', 'false')).lower()
    host = settings.value('proxy/proxyHost', '')
    if enabled in ('true', '1') and host:
        port = settings.value('proxy/proxyPort', '')
        config['GDAL_HTTP_PROXY'] = '{0}:{1}'.format(host, port) if port \
            else host
        user = settings.value('proxy/proxyUser', '')
        if user:
            config['GDAL_HTTP_PROXYUSERPWD'] = '{0}:{1}'.format(
                user, settings.value('proxy/proxyPassword', ''))
    return config


def _gdal_message(exc):
    text = str(exc).strip()
    lowered = text.lower()
    if 'curl' in lowered or 'http' in lowered or 'resolve' in lowered:
        return ('Could not read the raster tiles over the network. Check '
                'the internet connection, or the proxy settings in QGIS '
                'Options. ({0})'.format(text[:200]))
    if 'permission denied' in lowered or 'being used' in lowered:
        return ('The output file could not be written - it may be open in '
                'QGIS or another program. Choose another name.')
    return 'GDAL could not build the raster: {0}'.format(text[:300])


def _human(num_bytes):
    from .net import human_size

    return human_size(num_bytes)


# ------------------------------------------------------------------- registry

DEM_SOURCES = (
    CopernicusAwsSource(),
    OpenTopographySource(),
)

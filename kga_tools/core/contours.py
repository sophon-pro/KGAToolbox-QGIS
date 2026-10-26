# -*- coding: utf-8 -*-
"""Contour lines for a region of interest, from an open DEM.

The engine behind *Generate Contour for ROI*. One :class:`ContourTask` runs the
whole job on a worker thread so QGIS stays responsive, and reports a single
progress figure that spans every step:

1. read the DEM tiles for the ROI and cut them out (``dem_sources.warp_to_file``)
2. read the elevation range, to size the job and refuse an absurd interval
3. trace the contours (``gdal.ContourGenerateEx``) straight into a GeoPackage
4. mark each line ``main`` or ``sub``, and optionally smooth it

The task never touches a widget and never makes a network call that needs the
event loop: the tile list is resolved by the dialog before the task starts,
because ``net.download_to`` spins a ``QEventLoop`` and must stay on the GUI
thread.

Nothing here imports the dialog.
"""

import math
import os
import shutil
import tempfile
import traceback
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsGeometry, QgsMessageLog,
                       QgsProcessingUtils, QgsProject, QgsRectangle, QgsTask,
                       QgsUnitTypes)
from qgis.PyQt.QtCore import pyqtSignal

from . import dem_sources as DEM

LOG_TAG = 'KGA Toolbox'

#: Layer name inside the GeoPackage, and the attribute names.
CONTOUR_LAYER = 'contours'
FIELD_ID = 'ID'
FIELD_ELEV = 'ELEV'
FIELD_TYPE = 'TYPE'
#: Random spot heights live beside the contours, in the same GeoPackage.
SPOT_LAYER = 'spot_heights'
TYPE_MAIN = 'main'
TYPE_SUB = 'sub'

#: The surfaces that can be derived from the DEM, in the order they are made.
TERRAIN = ('hillshade', 'slope', 'aspect')
#: Written where there is no slope or aspect (the edge of the data, or flat
#: ground for aspect). A Byte hillshade uses 0, as gdaldem does.
TERRAIN_NODATA = -9999
#: Metres in one degree of latitude, which gdaldem wants as the scale when the
#: DEM is in degrees but its heights are in metres.
METRES_PER_DEGREE = 111120.0

#: DEM pixels above which the user is asked before the job starts. 25 million is
#: about 5,000 x 5,000 cells - 150 km square at GLO-30 - which is 100 MB of
#: float32 to hold and cut, and a contour set that can run to millions of
#: vertices at a fine interval.
WARN_PIXELS = 25 * 1000 * 1000
#: Above this the warning changes tone: this is where a desktop PC starts
#: swapping, and QGIS can stop answering while it draws the result.
HUGE_PIXELS = 150 * 1000 * 1000

#: More levels than this is a typo (an interval of 0.1 m across a mountain
#: range), not a request. Each level is a pass over the whole raster.
MAX_LEVELS = 20000

#: Chaikin corner cutting. One round takes the pixel-step look off a 30 m DEM
#: without the line wandering more than a fraction of a pixel, and doubles the
#: vertex count; each further round doubles it again for little visible gain.
SMOOTH_ITERATIONS = 1
SMOOTH_OFFSET = 0.25


class ContourError(Exception):
    """A job that cannot run as asked. Shown to the user as-is."""


# ----------------------------------------------------------------- estimates

def pixel_count(roi, dataset):
    """Pixels in the DEM raster the ROI will produce (its bounding box)."""
    west, south, east, north = roi.bounds()
    per_degree = 3600.0 / dataset.arcsec
    return int((east - west) * per_degree * (north - south) * per_degree)


def dem_bytes(pixels, dtype_bytes):
    """Uncompressed size of the DEM raster, which is what memory must hold."""
    return pixels * dtype_bytes


def size_warning(pixels, dtype_bytes, temporary, free_bytes=None):
    """A sentence to put in front of the user for a big job, or None.

    Returns ``(severe, text)``. `temporary` matters because a temporary result
    lives in the profile's temp folder, which is usually on the system drive.
    """
    from .net import human_size

    size = dem_bytes(pixels, dtype_bytes)
    lines = []
    severe = pixels >= HUGE_PIXELS
    if pixels >= WARN_PIXELS:
        lines.append(
            'This area is {0:,} million DEM pixels - about {1} before '
            'compression.'.format(pixels // 1000000, human_size(size)))
        lines.append(
            'It will be read, cut out and contoured in memory, and a large '
            'area with a fine interval can produce millions of vertices. On '
            'a modest PC that can make QGIS slow to a crawl or freeze.'
            if not severe else
            'That is very large. Reading it and tracing the contours will '
            'use a great deal of memory, and QGIS or the whole PC may freeze '
            'before it finishes.')
        lines.append('A smaller area, or a larger contour interval, keeps it '
                     'light.')
        if temporary:
            lines.append(
                'You have not chosen an output folder, so everything is '
                'written to a temporary folder (usually on the system '
                'drive) and deleted when QGIS closes.')
    if free_bytes is not None and free_bytes < size * 3:
        lines.append(
            'Only {0} is free on the drive the files go to, and the DEM '
            'and the contours need about {1}.'.format(
                human_size(free_bytes), human_size(size * 3)))
        severe = severe or free_bytes < size
    if not lines:
        return None
    return severe, '\n\n'.join(lines)


# --------------------------------------------------------------- a local DEM

class LocalDem(object):
    """A DEM the user already has: a raster layer's file or any GDAL raster."""

    def __init__(self, path):
        from osgeo import gdal

        self.path = path
        if not os.path.isfile(path):
            raise ContourError('The DEM file was not found: {0}'.format(path))
        try:
            dataset = gdal.Open(path)
        except RuntimeError:
            dataset = None
        if dataset is None or dataset.RasterCount < 1:
            raise ContourError('{0} could not be opened as a raster.'.format(
                os.path.basename(path)))
        try:
            self.width = dataset.RasterXSize
            self.height = dataset.RasterYSize
            self.transform = dataset.GetGeoTransform()
            wkt = dataset.GetProjection()
            band = dataset.GetRasterBand(1)
            self.nodata = band.GetNoDataValue()
            self.dtype_bytes = max(1, gdal.GetDataTypeSize(band.DataType) // 8)
            self.unsigned = gdal.GetDataTypeName(band.DataType) in (
                'Byte', 'UInt16', 'UInt32')
        finally:
            band = None
            dataset = None
        self.crs = QgsCoordinateReferenceSystem.fromWkt(wkt) if wkt \
            else QgsCoordinateReferenceSystem()
        if not self.crs.isValid():
            raise ContourError(
                '{0} has no coordinate system. Give it one first (Assign '
                'Projection), then try again.'.format(os.path.basename(path)))
        if self.transform[2] or self.transform[4]:
            raise ContourError(
                '{0} is rotated. Warp it to a north-up raster first.'.format(
                    os.path.basename(path)))

    def rect(self):
        t = self.transform
        x2 = t[0] + t[1] * self.width
        y2 = t[3] + t[5] * self.height
        return QgsRectangle(min(t[0], x2), min(t[3], y2),
                            max(t[0], x2), max(t[3], y2))

    def footprint_roi(self):
        """The whole raster as an ROI - for display; it is never clipped to."""
        return DEM.roi_from_extent(self.rect(), self.crs, 'Whole DEM')

    def pixels(self, roi):
        """Pixels of this DEM the ROI covers (all of them for no ROI)."""
        if roi is None:
            return self.width * self.height
        west, south, east, north = roi.bounds()
        try:
            box = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(DEM.WGS84), self.crs,
                QgsProject.instance()).transformBoundingBox(
                    QgsRectangle(west, south, east, north))
        except Exception:
            raise ContourError('The area could not be converted into the '
                               'coordinate system of the DEM.')
        overlap = box.intersect(self.rect())
        if overlap.isEmpty() or overlap.width() <= 0 or overlap.height() <= 0:
            raise ContourError('That area does not overlap the DEM.')
        return int(overlap.width() / abs(self.transform[1])
                   * overlap.height() / abs(self.transform[5]))

    def resolution_text(self):
        units = QgsUnitTypes.toString(self.crs.mapUnits())
        return '{0:g} x {1:g} {2}'.format(abs(self.transform[1]),
                                          abs(self.transform[5]), units)

    def warp_options(self):
        """Keyword arguments for ``dem_sources.warp_to_file``."""
        from osgeo import gdal

        options = {'source_is_wgs84': False}
        if self.nodata is not None:
            options['nodata'] = self.nodata
        if self.unsigned:
            # The default NoData is negative, which an unsigned type cannot hold.
            options['output_type'] = gdal.GDT_Float32
        return options


class DemJob(object):
    """What the dialog needs to describe and size one request, either source."""

    def __init__(self, roi, pixels, dtype_bytes, resolution, shown_roi,
                 item=None, local=None):
        #: None means "the whole local DEM": no cutting at all.
        self.roi = roi
        self.pixels = pixels
        self.dtype_bytes = dtype_bytes
        self.resolution = resolution
        #: Always set; what the details panel reports as the area.
        self.shown_roi = shown_roi
        self.item = item
        self.local = local

    @property
    def clip(self):
        return self.roi is not None and self.roi.clip

    @classmethod
    def from_item(cls, item):
        return cls(item.roi, pixel_count(item.roi, item.dataset),
                   item.dataset.dtype_bytes, item.dataset.resolution_text(),
                   item.roi, item=item)

    @classmethod
    def from_local(cls, local, roi):
        pixels = local.pixels(roi)
        if pixels > DEM.MAX_PIXELS:
            raise ContourError(
                'That is {0:,} million pixels, where one run is limited to '
                '{1:,} million. Choose a smaller area.'.format(
                    pixels // 1000000, DEM.MAX_PIXELS // 1000000))
        return cls(roi, pixels, local.dtype_bytes, local.resolution_text(),
                   roi or local.footprint_roi(), local=local)


def temp_folder():
    """A fresh scratch folder for a run saved nowhere in particular."""
    base = os.path.join(QgsProcessingUtils.tempFolder(), 'kga_contours')
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix='run_', dir=base)


def level_count(low, high, interval):
    """How many contour levels fall between `low` and `high`."""
    if interval <= 0 or high < low:
        return 0
    return int(math.floor(high / interval) - math.ceil(low / interval)) + 1


# ------------------------------------------------------------------- the job

class ContourSpec(object):
    """Everything one run needs, resolved on the GUI thread."""

    def __init__(self, rasters, roi, dem_path, contour_path, interval,
                 main_every, smooth, title, crs=None, local=None,
                 terrain=(), spot_count=0):
        self.rasters = rasters
        self.roi = roi
        self.dem_path = dem_path
        self.contour_path = contour_path
        self.interval = interval
        self.main_every = main_every
        self.smooth = smooth
        self.title = title
        #: QgsCoordinateReferenceSystem to warp the DEM into; None keeps
        #: the published EPSG:4326 grid.
        self.crs = crs
        #: A :class:`LocalDem` when the DEM is the user's own file.
        self.local = local
        #: Which of :data:`TERRAIN` to derive from the DEM.
        self.terrain = tuple(t for t in TERRAIN if t in (terrain or ()))
        #: How many random spot heights to place; 0 for none.
        self.spot_count = max(0, int(spot_count or 0))

    def terrain_path(self, kind):
        base, _ext = os.path.splitext(self.dem_path)
        return '{0}_{1}.tif'.format(base[:-4] if base.endswith('_DEM')
                                    else base, kind)

    @property
    def main_interval(self):
        return self.interval * self.main_every

    def steps(self):
        """(label, weight) per phase. The weights make one bar of them."""
        steps = [('Reading and cutting out the DEM' if self.local else
                  'Downloading and cutting out the DEM', 0.50),
                 ('Reading the elevation range', 0.02),
                 ('Tracing contour lines', 0.28)]
        if self.smooth:
            steps.append(('Smoothing the lines and marking main contours',
                          0.20))
        else:
            steps.append(('Marking main and sub contours', 0.04))
        if self.terrain:
            steps.append(('Computing {0}'.format(', '.join(self.terrain)),
                          0.05 * len(self.terrain)))
        if self.spot_count:
            steps.append(('Placing random spot heights', 0.03))
        total = sum(weight for _label, weight in steps)
        return [(label, weight / total) for label, weight in steps]


class ContourTask(QgsTask):
    """Download, cut, contour. One job at a time, on a worker thread."""

    #: Step text and overall progress 0..1, across every step of the job.
    phase = pyqtSignal(str, float)
    jobFinished = pyqtSignal(dict)

    def __init__(self, spec):
        flag = getattr(QgsTask, 'Flag', QgsTask)     # scoped enum, QGIS 3.36+
        super().__init__('Contours: {0}'.format(spec.title), flag.CanCancel)
        self.spec = spec
        self.result = {}
        self._stop = False
        self._steps = spec.steps()
        self._step = 0
        self._base = 0.0

    def request_stop(self):
        self._stop = True

    def should_stop(self):
        return self._stop or self.isCanceled()

    # ------------------------------------------------------------- progress

    def _begin(self, index):
        self._step = index
        self._base = sum(w for _l, w in self._steps[:index])
        self._report(0.0)

    def _report(self, fraction):
        label, weight = self._steps[self._step]
        overall = min(1.0, self._base + weight * max(0.0, min(1.0, fraction)))
        self.setProgress(100.0 * overall)
        self.phase.emit(label, overall)

    def _callback(self, fraction):
        """A gdal progress callback: False stops it, anything else carries on."""
        self._report(fraction)
        return False if self.should_stop() else True

    # ------------------------------------------------------------------ run

    def run(self):
        spec = self.spec
        result = {'state': 'error', 'message': '', 'dem': spec.dem_path,
                  'contours': spec.contour_path, 'levels': 0, 'features': 0,
                  'low': None, 'high': None}
        try:
            self._run(spec, result)
        except (ContourError, DEM.DemError) as exc:
            result.update(state='error', message=str(exc))
        except Exception as exc:                # never let a task die silently
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            result.update(state='error', message='The contours could not be '
                                                 'made: {0}'.format(exc))
        if result['state'] != 'done':
            _discard(spec.contour_path)
            if result['state'] == 'stopped':
                _discard(spec.dem_path)
                for kind in spec.terrain:
                    _discard(spec.terrain_path(kind))
        self.result = result
        return result['state'] == 'done'

    def _run(self, spec, result):
        from osgeo import gdal

        self._begin(0)
        if spec.roi is None:
            written = _warp_whole(spec, self._callback)
        else:
            extra = spec.local.warp_options() if spec.local else {}
            written = DEM.warp_to_file(spec.rasters, spec.roi, spec.dem_path,
                                       spec.crs, self._callback, **extra)
        if not written or self.should_stop():
            result['state'] = 'stopped'
            return

        self._begin(1)
        low, high = _elevation_range(spec.dem_path)
        result.update(low=low, high=high)
        levels = level_count(low, high, spec.interval)
        if levels > MAX_LEVELS:
            raise ContourError(
                'An interval of {0:g} m across {1:,.0f} to {2:,.0f} m would '
                'make {3:,} contour levels. Choose a larger interval. The '
                'DEM was kept at {4}.'.format(
                    spec.interval, low, high, levels, spec.dem_path))
        if levels == 0:
            raise ContourError(
                'The land in that area runs only from {0:,.1f} to {1:,.1f} m, '
                'so no contour at a {2:g} m interval passes through it. '
                'Choose a smaller interval. The DEM was kept at {3}.'.format(
                    low, high, spec.interval, spec.dem_path))
        result['levels'] = levels
        self._report(1.0)

        self._begin(2)
        _trace(gdal, spec, self._callback, self.should_stop)
        if self.should_stop():
            result['state'] = 'stopped'
            return

        self._begin(3)
        finished = _finish(gdal, spec, self._callback)
        if finished is None or self.should_stop():
            result['state'] = 'stopped'
            return
        result['features'] = finished
        if finished == 0:
            raise ContourError(
                'No contour lines came out. The DEM was kept at {0}.'.format(
                    spec.dem_path))
        if spec.terrain:
            self._begin(4)
            try:
                made = _terrain(gdal, spec, self._callback)
            except (ContourError, DEM.DemError) as exc:
                # The contours are fine; say what went wrong with the rest.
                result['terrain_error'] = str(exc)
                made = {}
            if self.should_stop():
                result['state'] = 'stopped'
                return
            result['terrain'] = made
        if spec.spot_count:
            self._begin(len(self._steps) - 1)
            try:
                result['spots'] = _spot_heights(gdal, spec, self._callback)
            except (ContourError, DEM.DemError) as exc:
                result['spot_error'] = str(exc)
            if self.should_stop():
                result['state'] = 'stopped'
                return
        result['state'] = 'done'

    def finished(self, success):
        self.jobFinished.emit(self.result)


def start(task):
    QgsApplication.taskManager().addTask(task)
    return task


# ------------------------------------------------------------------ the steps

def _warp_whole(spec, callback):
    """Copy the whole local DEM, reprojected if asked. False when stopped."""
    from osgeo import gdal

    state = {'stopped': False}

    def progress(complete, _message, _data):
        if callback(complete) is False:
            state['stopped'] = True
            return 0
        return 1

    options = dict(
        format='GTiff', resampleAlg='bilinear', callback=progress,
        warpOptions=['NUM_THREADS=ALL_CPUS'],
        creationOptions=['COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER'])
    if spec.local.nodata is not None:
        options['dstNodata'] = spec.local.nodata
    if spec.local.unsigned:
        options['outputType'] = gdal.GDT_Float32
    if spec.crs is not None:
        options['dstSRS'] = DEM.gdal_srs(spec.crs)
    with DEM._gdal_exceptions(gdal):
        try:
            result = gdal.Warp(spec.dem_path, spec.local.path, **options)
        except RuntimeError as exc:
            if not state['stopped']:
                raise DEM.DemError(DEM._gdal_message(exc))
            result = None
        if result is None:
            DEM._remove_output(gdal, spec.dem_path)
            if state['stopped']:
                return False
            raise DEM.DemError('GDAL could not write the raster.')
        result.FlushCache()
        result = None
    return True


def _elevation_range(path):
    from osgeo import gdal

    dataset = gdal.Open(path)
    if dataset is None:
        raise ContourError('The DEM was written but could not be read back.')
    try:
        band = dataset.GetRasterBand(1)
        # Exact, not approximate: it is a clipped raster, read once.
        low, high = band.ComputeRasterMinMax(False)
    finally:
        band = None
        dataset = None
    if low is None or high is None or math.isnan(low) or math.isnan(high):
        raise ContourError('The DEM has no elevation values inside that area.')
    return float(low), float(high)


def _trace(gdal, spec, callback, stopped):
    """Contour the DEM straight into a fresh GeoPackage layer."""
    from osgeo import ogr, osr

    _discard(spec.contour_path)
    source = gdal.Open(spec.dem_path)
    if source is None:
        raise ContourError('The DEM could not be opened to contour it.')
    band = source.GetRasterBand(1)
    nodata = band.GetNoDataValue()

    driver = ogr.GetDriverByName('GPKG')
    target = driver.CreateDataSource(spec.contour_path)
    if target is None:
        raise ContourError('The contour file could not be created at {0}. '
                           'Is that folder writable?'.format(
                               spec.contour_path))
    try:
        # The contours take the DEM's own CRS, whatever it was warped to.
        srs = osr.SpatialReference()
        if source.GetProjection():
            srs.ImportFromWkt(source.GetProjection())
        else:
            srs.SetFromUserInput(DEM.WGS84)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        layer = target.CreateLayer(CONTOUR_LAYER, srs, ogr.wkbLineString)
        layer.CreateField(ogr.FieldDefn(FIELD_ID, ogr.OFTInteger))
        layer.CreateField(ogr.FieldDefn(FIELD_ELEV, ogr.OFTReal))
        type_field = ogr.FieldDefn(FIELD_TYPE, ogr.OFTString)
        type_field.SetWidth(8)
        layer.CreateField(type_field)

        options = ['LEVEL_INTERVAL={0!r}'.format(float(spec.interval)),
                   'LEVEL_BASE=0',
                   'ID_FIELD=0', 'ELEV_FIELD=1']
        if nodata is not None:
            options.append('NODATA={0!r}'.format(float(nodata)))
        # One transaction: a GeoPackage commits every feature otherwise, and a
        # contour set is tens of thousands of them.
        layer.StartTransaction()
        try:
            status = gdal.ContourGenerateEx(
                band, layer, options=options,
                callback=lambda fraction, _msg, _data:
                0 if callback(fraction) is False else 1)
        except RuntimeError as exc:
            # A callback that returns 0 is how a stop is delivered, and with
            # exceptions on GDAL reports it as one.
            if not stopped():
                layer.RollbackTransaction()
                raise ContourError('GDAL could not trace the contours: '
                                   '{0}'.format(str(exc)[:300]))
            status = 1
        if status != 0:
            layer.RollbackTransaction()
        else:
            layer.CommitTransaction()
        layer = None
    finally:
        target = None
        band = None
        source = None


def _finish(gdal, spec, callback):
    """Set TYPE on every line and, if asked, smooth it - in one pass.

    Returns how many lines there are, or None when stopped.
    """
    from osgeo import ogr

    target = ogr.Open(spec.contour_path, 1)
    if target is None:
        raise ContourError('The contour file could not be reopened.')
    try:
        layer = target.GetLayerByName(CONTOUR_LAYER)
        total = layer.GetFeatureCount()
        main = float(spec.main_interval)
        type_index = layer.GetLayerDefn().GetFieldIndex(FIELD_TYPE)
        done = 0
        layer.StartTransaction()
        try:
            for feature in layer:
                # Contour levels are exact multiples of the interval, so a
                # level is a main one when it is also a multiple of interval x
                # N. The tolerance absorbs float noise from an interval like
                # 0.1.
                ratio = feature.GetField(FIELD_ELEV) / main
                is_main = abs(ratio - round(ratio)) < 1e-6
                feature.SetField(type_index, TYPE_MAIN if is_main else TYPE_SUB)
                if spec.smooth:
                    _smooth_feature(ogr, feature)
                layer.SetFeature(feature)
                done += 1
                if done % 200 == 0 and callback(done / max(1, total)) is False:
                    layer.RollbackTransaction()
                    return None
        except BaseException:
            layer.RollbackTransaction()
            raise
        layer.CommitTransaction()
        callback(1.0)
        layer = None
    finally:
        target = None
    return total


def _smooth_feature(ogr, feature):
    """Round off the pixel steps of one line, in place."""
    geometry = feature.GetGeometryRef()
    if geometry is None:
        return
    smoothed = QgsGeometry()
    smoothed.fromWkb(bytes(geometry.ExportToWkb()))
    smoothed = smoothed.smooth(SMOOTH_ITERATIONS, SMOOTH_OFFSET)
    if smoothed is not None and not smoothed.isEmpty():
        feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(smoothed.asWkb())))


def _terrain(gdal, spec, callback):
    """Hillshade, slope and aspect from the DEM. Returns {kind: path}.

    A DEM in degrees needs the horizontal scale spelled out, or gdaldem would
    treat a degree as a metre and report every slope as near vertical. One
    degree is about 111 km north-south and shrinks east-west with the cosine of
    the latitude, so the two are given separately.
    """
    source = gdal.Open(spec.dem_path)
    if source is None:
        raise ContourError('The DEM could not be opened to derive from it.')
    scales = {}
    try:
        from qgis.core import QgsCoordinateReferenceSystem

        wkt = source.GetProjection()
        crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
        if crs.isValid() and crs.isGeographic():
            t = source.GetGeoTransform()
            middle = t[3] + t[5] * source.RasterYSize / 2.0
            scales = {'xscale': METRES_PER_DEGREE * math.cos(
                          math.radians(middle)),
                      'yscale': METRES_PER_DEGREE}
    finally:
        source = None

    made = {}
    count = len(spec.terrain)
    for index, kind in enumerate(spec.terrain):
        path = spec.terrain_path(kind)
        _discard(path)

        def progress(fraction, _message, _data, index=index):
            return 0 if callback((index + fraction) / count) is False else 1

        options = dict(format='GTiff', computeEdges=True, callback=progress,
                       creationOptions=['COMPRESS=DEFLATE', 'TILED=YES',
                                        'BIGTIFF=IF_SAFER'], **scales)
        if kind == 'hillshade':
            options.update(azimuth=315, altitude=45)
        else:
            options.update(alg='Horn')
        if kind == 'slope':
            options['slopeFormat'] = 'degree'
        with DEM._gdal_exceptions(gdal):
            try:
                result = gdal.DEMProcessing(path, spec.dem_path, kind,
                                            **options)
            except RuntimeError as exc:
                _discard(path)
                if callback(index / count) is False:
                    return made
                raise ContourError('The {0} could not be made: {1}'.format(
                    kind, str(exc)[:200]))
            if result is None:
                _discard(path)
                raise ContourError('The {0} could not be made.'.format(kind))
            result.FlushCache()
            result = None
        made[kind] = path
    callback(1.0)
    return made


def _spot_heights(gdal, spec, callback):
    """Scatter `spec.spot_count` random spot heights over the DEM.

    The raster is cut into a grid of about 1.5 x the wanted number of blocks and
    one valid cell is drawn at random from each, so the points are random but
    spread over the map like a surveyed sheet, never in a clump. Each is written
    to the ``spot_heights`` layer of the contour GeoPackage with the DEM's own
    height at that cell. Returns how many were placed.
    """
    import random

    import numpy as np
    from osgeo import ogr, osr

    source = gdal.Open(spec.dem_path)
    if source is None:
        raise ContourError('The DEM could not be opened to place spot heights.')
    try:
        band = source.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        data = band.ReadAsArray().astype('float64')
        transform = source.GetGeoTransform()
        wkt = source.GetProjection()
    finally:
        band = None
        source = None
    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata
    rows, cols = data.shape
    blocks = max(1, int(math.ceil(math.sqrt(spec.spot_count * 1.5))))
    row_edges = np.linspace(0, rows, blocks + 1).astype(int)
    col_edges = np.linspace(0, cols, blocks + 1).astype(int)
    rng = random.SystemRandom()
    picks = []
    for i in range(blocks):
        for j in range(blocks):
            window = valid[row_edges[i]:row_edges[i + 1],
                           col_edges[j]:col_edges[j + 1]]
            found = np.argwhere(window)
            if len(found):
                r, c = found[rng.randrange(len(found))]
                picks.append((int(r) + int(row_edges[i]),
                              int(c) + int(col_edges[j])))
    rng.shuffle(picks)
    picks = picks[:spec.spot_count]
    if not picks:
        raise ContourError('There was no land to place spot heights on.')

    target = ogr.Open(spec.contour_path, 1)
    if target is None:
        raise ContourError('The contour file could not be reopened.')
    try:
        srs = osr.SpatialReference()
        if wkt:
            srs.ImportFromWkt(wkt)
        else:
            srs.SetFromUserInput(DEM.WGS84)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        layer = target.CreateLayer(SPOT_LAYER, srs, ogr.wkbPoint)
        layer.CreateField(ogr.FieldDefn(FIELD_ID, ogr.OFTInteger))
        layer.CreateField(ogr.FieldDefn(FIELD_ELEV, ogr.OFTReal))
        layer.StartTransaction()
        for number, (r, c) in enumerate(picks, 1):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField(FIELD_ID, number)
            feature.SetField(FIELD_ELEV, round(float(data[r, c]), 1))
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint_2D(transform[0] + transform[1] * (c + 0.5),
                              transform[3] + transform[5] * (r + 0.5))
            feature.SetGeometry(point)
            layer.CreateFeature(feature)
        layer.CommitTransaction()
        layer = None
    finally:
        target = None
    callback(1.0)
    return len(picks)


def _discard(path):
    """Delete a GeoPackage and its sidecars, quietly."""
    for suffix in ('', '-wal', '-shm', '-journal'):
        with suppress(OSError):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def remove_folder(path):
    """Delete a scratch folder made by :func:`temp_folder`."""
    shutil.rmtree(path, ignore_errors=True)

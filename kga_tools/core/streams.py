# -*- coding: utf-8 -*-
"""Rivers and streams for a region of interest, from an open DEM.

The engine behind *Generate River or Stream for ROI*. One :class:`StreamTask`
runs the whole job on a worker thread so QGIS stays responsive, and reports a
single progress figure that spans every step:

1. read the DEM tiles for the ROI and cut them out (``dem_sources.warp_to_file``)
2. load the DEM into memory
3. fill its depressions and route every cell downhill (``hydrology.route``)
4. accumulate the contributing area down those routes
5. threshold it into a stream network and trace it into numbered segments
6. check the network - internally, and against a reference if one was given
7. write the streams (and the checks) to a GeoPackage, and the report

The task never touches a widget and never makes a network call that needs the
event loop: the tile list and any reference are fetched by the dialog before
the task starts, because ``net.download_to`` spins a ``QEventLoop`` and must
stay on the GUI thread.

Nothing here imports the dialog.
"""

import math
import os
import tempfile
import traceback
from contextlib import suppress

import numpy as np

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsCoordinateTransformContext,
                       QgsGeometry, QgsMessageLog, QgsPointXY,
                       QgsProcessingUtils, QgsRectangle, QgsTask,
                       QgsUnitTypes)
from qgis.PyQt.QtCore import pyqtSignal

from . import dem_sources as DEM
from . import hydrology as H
from . import net
from . import stream_check as CHECK
from .contours import remove_folder  # noqa: F401  (re-exported for the dialog)

LOG_TAG = 'KGA Toolbox'

#: Layer names inside the GeoPackage, and the attribute names.
STREAM_LAYER = 'streams'
CHECK_LAYER = 'stream_check'
FIELD_ID = 'STREAM_ID'
FIELD_NEXT = 'NEXT_ID'
FIELD_ORDER = 'STRAHLER'
FIELD_LENGTH = 'LENGTH_M'
FIELD_UP = 'UP_KM2'
FIELD_ELEV_UP = 'ELEV_UP'
FIELD_ELEV_DN = 'ELEV_DN'
FIELD_SLOPE = 'SLOPE_PCT'
FIELD_FILL = 'FILL_M'
FIELD_RELIEF = 'RELIEF_M'
FIELD_CONF = 'CONF'
FIELD_FLAGS = 'FLAGS'
FIELD_KIND = 'KIND'
KIND_EXTRA = 'extra'
KIND_MISSED = 'missed'

#: How much of the landscape the user wants as a stream. Each entry is
#: (id, label, contributing area in km2, what it is good for). A cell is stream
#: when at least that much ground drains through it. At 30 m a km2 is about
#: 1,100 cells.
DETAIL_PRESETS = (
    ('major', 'Major rivers only', 25.0,
     'Only the big channels - the skeleton of the drainage. Needs a large '
     'area: below about 100 km2 there may be nothing this big.'),
    ('main', 'Main streams', 5.0,
     'The streams a map would normally show. A good start for most areas.'),
    ('detailed', 'Detailed network', 1.0,
     'Adds the smaller tributaries. Good for a catchment of tens of km2.'),
    ('fine', 'Very detailed (headwater gullies)', 0.2,
     'Every gully that gathers any water. Includes channels that run only '
     'after rain, and more false streams on flat ground.'),
)
DETAIL_CUSTOM = 'custom'
DETAIL_DENSITY = 'density'
DEFAULT_DETAIL = 'main'

#: Pixels above which the user is asked before the job starts. Routing a DEM
#: needs far more memory than contouring it: about 110 bytes a pixel, so 6
#: million pixels (a 75 km square at GLO-30) is already some 650 MB.
WARN_PIXELS = 6 * 1000 * 1000
HUGE_PIXELS = 30 * 1000 * 1000
#: Refused outright: past this it is minutes of pure-Python routing and several
#: GB, and the answer is a coarser DEM or a smaller area.
MAX_PIXELS = 100 * 1000 * 1000
BYTES_PER_PIXEL = 110

#: Fewer than this many cells of contributing area is not a stream, it is noise.
MIN_THRESHOLD_CELLS = 3
#: A threshold so low that this share of the area is "stream" is a typo.
MAX_STREAM_SHARE = 0.25

SMOOTH_ITERATIONS = 1
SMOOTH_OFFSET = 0.25

#: A reference stream is matched when a DEM stream is within this many cells.
MATCH_CELLS = 3


class StreamError(Exception):
    """A job that cannot run as asked. Shown to the user as-is."""


# ----------------------------------------------------------------- estimates

def pixel_count(roi, dataset):
    """Pixels in the DEM raster the ROI will produce (its bounding box)."""
    west, south, east, north = roi.bounds()
    per_degree = 3600.0 / dataset.arcsec
    return int((east - west) * per_degree * (north - south) * per_degree)


def memory_bytes(pixels):
    """What routing a DEM of this size will hold in memory at its peak."""
    return pixels * BYTES_PER_PIXEL


def work_roi(roi, margin_km):
    """The area to download: the ROI's bounding box plus a margin.

    A margin lets streams that rise outside the ROI arrive with their full
    catchment. The streams are cut back to the ROI afterwards.
    """
    if margin_km <= 0:
        return roi
    west, south, east, north = roi.bounds()
    lat = max(abs(south), abs(north))
    dlat = margin_km / 110.574
    dlon = margin_km / (111.320 * max(0.05, math.cos(math.radians(lat))))
    box = QgsRectangle(max(-180.0, west - dlon), max(-90.0, south - dlat),
                       min(180.0, east + dlon), min(90.0, north + dlat))
    return DEM.Roi(QgsGeometry.fromRect(box), False, roi.label)


def size_warning(pixels, temporary, free_bytes=None):
    """``(severe, text)`` for a big job, or None."""
    from .net import human_size

    size = memory_bytes(pixels)
    lines = []
    severe = pixels >= HUGE_PIXELS
    if pixels >= WARN_PIXELS:
        lines.append(
            'This area is {0:,} million DEM pixels. Finding the streams needs '
            'about {1} of memory.'.format(max(1, pixels // 1000000),
                                          human_size(size)))
        lines.append(
            'That is a lot for a modest PC: QGIS can slow to a crawl, and if '
            'the PC runs out of memory it can freeze.' if not severe else
            'That is very large. If the PC runs short of memory it will start '
            'swapping to disk and may freeze until the job ends.')
        lines.append('A smaller area, or the 90 m DEM, keeps it light.')
        if temporary:
            lines.append(
                'You have not chosen an output folder, so the files are '
                'written to a temporary folder (usually on the system drive) '
                'and deleted when QGIS closes.')
    dem_size = pixels * 4
    if free_bytes is not None and free_bytes < dem_size * 3:
        lines.append(
            'Only {0} is free on the drive the files go to, and the DEM and '
            'results need about {1}.'.format(human_size(free_bytes),
                                             human_size(dem_size * 3)))
        severe = severe or free_bytes < dem_size
    if not lines:
        return None
    return severe, '\n\n'.join(lines)


def local_dataset(raster):
    """A ``Dataset`` describing a user's own DEM, from a QgsRasterLayer.

    Only its pixel size matters to the estimates, expressed in arc seconds so
    the Copernicus arithmetic works unchanged. Heights are taken as metres.
    """
    crs = raster.crs()
    size = abs(raster.rasterUnitsPerPixelX())
    if crs.isGeographic():
        arcsec = size * 3600.0
    else:
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            crs.mapUnits(), Qgis.DistanceUnit.Meters)
        arcsec = size * factor / 30.87
    return DEM.Dataset(
        'local', 'Local DEM - {0}'.format(raster.name()), max(arcsec, 1e-6),
        4, 'DSM', 'The licence of your own data', '',
        note='Heights are taken as metres.')


def temp_folder():
    """A fresh scratch folder for a run saved nowhere in particular."""
    base = os.path.join(QgsProcessingUtils.tempFolder(), 'kga_streams')
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix='run_', dir=base)


def utm_crs(roi):
    """The UTM zone at the middle of the ROI, as a QgsCoordinateReferenceSystem."""
    west, south, east, north = roi.bounds()
    lon = (west + east) / 2.0
    lat = (south + north) / 2.0
    zone = int(min(60, max(1, math.floor((lon + 180.0) / 6.0) + 1)))
    return QgsCoordinateReferenceSystem(
        'EPSG:{0}'.format((32600 if lat >= 0 else 32700) + zone))


def cell_sizes(transform, wkt, rows):
    """Cell width and height in metres for each row of a raster.

    A geographic grid is narrower towards the poles, a Web Mercator one is
    stretched; area and slope are only right if that is taken out.
    """
    from osgeo import osr

    srs = osr.SpatialReference()
    if wkt:
        srs.ImportFromWkt(wkt)
    else:
        srs.SetFromUserInput(DEM.WGS84)
    px, py = abs(transform[1]), abs(transform[5])
    ys = transform[3] + (np.arange(rows) + 0.5) * transform[5]
    if srs.IsGeographic():
        lat = np.radians(ys)
        dx = px * (111412.84 * np.cos(lat) - 93.5 * np.cos(3 * lat))
        dy = py * (111132.92 - 559.82 * np.cos(2 * lat)
                   + 1.175 * np.cos(4 * lat))
        return dx, dy
    unit = srs.GetLinearUnits() or 1.0
    scale = np.ones(rows)
    projection = srs.GetAttrValue('PROJECTION') or ''
    if 'Mercator' in projection and 'Transverse' not in projection:
        lat = 2.0 * np.arctan(np.exp(ys * unit / 6378137.0)) - math.pi / 2.0
        scale = np.cos(lat)
    return px * unit * scale, py * unit * scale


def metric_crs(stream_crs, roi):
    """A CRS in metres to measure and compare in."""
    if stream_crs is not None and stream_crs.isValid() \
            and not stream_crs.isGeographic() \
            and stream_crs.mapUnits() == Qgis.DistanceUnit.Meters:
        return stream_crs
    return utm_crs(roi)


# ------------------------------------------------------------------- the job

class StreamSpec(object):
    """Everything one run needs, resolved on the GUI thread."""

    def __init__(self, rasters, roi, analysis_roi, dem_path, stream_path,
                 detail, smooth, title, crs=None, accum_path=None,
                 report_path=None, reference=None, osm_bounds=None,
                 tolerance=None, dataset=None, local=False):
        self.rasters = rasters
        #: The ROI the user drew or chose: streams are cut back to it.
        self.roi = roi
        #: The area downloaded: the ROI, or the ROI plus a margin.
        self.analysis_roi = analysis_roi
        self.dem_path = dem_path
        self.stream_path = stream_path
        #: (mode, contributing area km2 or None, drainage density or None)
        self.detail = detail
        self.smooth = smooth
        self.title = title
        #: QgsCoordinateReferenceSystem to warp the DEM into; None keeps the
        #: published EPSG:4326 grid.
        self.crs = crs
        self.accum_path = accum_path
        self.report_path = report_path
        #: A stream_check.Reference, or None.
        self.reference = reference
        #: (west, south, east, north) to fetch OpenStreetMap streams for, in
        #: the task, when the reference is OpenStreetMap; None otherwise.
        self.osm_bounds = osm_bounds
        #: Why the reference could not be used, once the task has tried.
        self.reference_problem = ''
        self.tolerance = tolerance
        self.dataset = dataset
        #: True when the DEM is the user's own rather than Copernicus.
        self.local = local

    @property
    def has_reference(self):
        return self.reference is not None or self.osm_bounds is not None

    @property
    def margin(self):
        return self.analysis_roi is not self.roi

    def steps(self):
        """(label, weight) per phase. The weights make one bar of them."""
        steps = [('Downloading and cutting out the DEM', 0.34),
                 ('Loading the DEM', 0.03),
                 ('Filling pits and routing the water', 0.27),
                 ('Accumulating flow', 0.08),
                 ('Tracing the stream network', 0.07),
                 ('Checking the streams' if not self.has_reference else
                  'Checking the streams against the reference',
                  0.06 if not self.has_reference else 0.12),
                 ('Writing the results', 0.12)]
        total = sum(weight for _label, weight in steps)
        return [(label, weight / total) for label, weight in steps]


class StreamTask(QgsTask):
    """Download, cut, route, trace, check. One job at a time, on a worker."""

    #: Step text and overall progress 0..1, across every step of the job.
    phase = pyqtSignal(str, float)
    jobFinished = pyqtSignal(dict)

    def __init__(self, spec):
        flag = getattr(QgsTask, 'Flag', QgsTask)     # scoped enum, QGIS 3.36+
        super().__init__('Streams: {0}'.format(spec.title), flag.CanCancel)
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

    def _report(self, fraction, label=None):
        step_label, weight = self._steps[self._step]
        overall = min(1.0, self._base + weight * max(0.0, min(1.0, fraction)))
        self.setProgress(100.0 * overall)
        self.phase.emit(label or step_label, overall)

    def _callback(self, fraction):
        """A progress callback: False stops it, anything else carries on."""
        self._report(fraction)
        return False if self.should_stop() else True

    # ------------------------------------------------------------------ run

    def run(self):
        spec = self.spec
        result = {'state': 'error', 'message': '', 'dem': spec.dem_path,
                  'streams': spec.stream_path, 'accumulation': None,
                  'report_path': None, 'report_html': '', 'report_text': '',
                  'segments': 0, 'length_km': 0.0, 'max_order': 0,
                  'threshold_km2': None, 'check_layer': False,
                  'low': None, 'high': None, 'confidence': {}}
        try:
            self._run(spec, result)
        except H.Stopped:
            result['state'] = 'stopped'
        except (StreamError, DEM.DemError) as exc:
            result.update(state='error', message=str(exc))
        except MemoryError:
            result.update(
                state='error',
                message='The PC ran out of memory finding the streams. Use a '
                        'smaller area or the 90 m DEM. The DEM was kept at '
                        '{0}.'.format(spec.dem_path))
        except Exception as exc:                # never let a task die silently
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            result.update(state='error', message='The streams could not be '
                                                 'made: {0}'.format(exc))
        if result['state'] != 'done':
            _discard(spec.stream_path)
            if spec.accum_path:
                _remove(spec.accum_path)
            if result['state'] == 'stopped':
                _remove(spec.dem_path)
        self.result = result
        return result['state'] == 'done'

    def _run(self, spec, result):
        self._begin(0)
        written = DEM.warp_to_file(spec.rasters, spec.analysis_roi,
                                   spec.dem_path, spec.crs, self._callback)
        if not written or self.should_stop():
            result['state'] = 'stopped'
            return

        self._begin(1)
        dem = _read_dem(spec.dem_path)
        result.update(low=dem.low, high=dem.high)
        if dem.pixels > MAX_PIXELS:
            raise StreamError(
                'That DEM is {0:,} million pixels, more than this tool will '
                'route in one go. Use a smaller area or the 90 m DEM. The '
                'DEM was kept at {1}.'.format(dem.pixels // 1000000,
                                              spec.dem_path))
        self._report(1.0)

        self._begin(2)
        grid = H.Grid(dem.rows, dem.cols, *cell_sizes(
            dem.transform, dem.wkt, dem.rows))
        routing = H.route(grid, dem.elevation, dem.valid,
                          progress=self._report, stop=self.should_stop)
        dem.elevation = None                # the padded copy is in `routing`

        self._begin(3)
        area = H.accumulate(routing, progress=self._report,
                            stop=self.should_stop)

        self._begin(4)
        threshold = self._threshold(spec, grid, routing, area, result, dem)
        segments, stream_mask, _strahler = H.trace(
            routing, area, threshold, progress=self._report,
            stop=self.should_stop)
        if not segments:
            raise StreamError('No stream came out. The DEM was kept at '
                              '{0}.'.format(spec.dem_path))
        self._report(1.0)

        self._begin(5)
        records, stats = self._records(spec, grid, routing, area, dem,
                                       segments, threshold)
        comparison = None
        if spec.osm_bounds is not None:
            self._fetch_osm(spec)
        if self.should_stop():
            result['state'] = 'stopped'
            return
        if spec.reference is not None:
            comparison = self._compare(spec, records, dem)
            if comparison is None:
                result['state'] = 'stopped'
                return
        self._report(1.0)

        self._begin(6)
        self._write(spec, dem, grid, area, records, comparison, result)
        if self.should_stop():
            result['state'] = 'stopped'
            return
        stats['sensitivity'] = _sensitivity(grid, area, routing.valid)
        report = build_report(spec, dem, stats, records, comparison, threshold)
        result['report_html'] = report.html()
        result['report_text'] = report.text()
        if spec.report_path:
            with suppress(OSError):
                with open(spec.report_path, 'w', encoding='utf-8') as handle:
                    handle.write(result['report_text'])
                result['report_path'] = spec.report_path
        result.update(segments=len(records), length_km=stats['length_km'],
                      max_order=stats['max_order'],
                      threshold_km2=threshold / 1e6,
                      confidence=stats['confidence_km'],
                      check_layer=bool(comparison is not None and (
                          comparison.extra or comparison.missed)),
                      reference_problem=spec.reference_problem)
        self._report(1.0)
        result['state'] = 'done'

    def finished(self, success):
        self.jobFinished.emit(self.result)

    # ----------------------------------------------------------- the stages

    def _threshold(self, spec, grid, routing, area, result, dem):
        """Contributing area, in m2, above which a cell is a stream."""
        mode, km2, density = spec.detail
        valid_km2 = float(np.sum(grid.row_values(
            grid.dx * grid.dy)[routing.valid])) / 1e6
        cell_m2 = float(np.min(grid.dx * grid.dy))
        if mode == DETAIL_DENSITY:
            threshold = H.threshold_for_density(grid, area, routing, density,
                                                valid_km2)
        else:
            threshold = float(km2) * 1e6
        threshold = max(threshold, MIN_THRESHOLD_CELLS * cell_m2)
        biggest = float(area[routing.valid].max()) if routing.valid.any() \
            else 0.0
        if threshold > biggest:
            raise StreamError(
                'The biggest catchment in this area drains only {0:,.2f} km2, '
                'so no stream gathers the {1:,.2f} km2 you asked for. Choose '
                'a more detailed setting or a larger area. The DEM was kept '
                'at {2}.'.format(biggest / 1e6, threshold / 1e6,
                                 spec.dem_path))
        cells = H.stream_cell_count(area, threshold)
        if cells > MAX_STREAM_SHARE * int(routing.valid.sum()):
            raise StreamError(
                '{0:.0f}% of the area would be stream at {1:,.3f} km2. That '
                'is not a stream network - choose a larger contributing '
                'area. The DEM was kept at {2}.'.format(
                    100.0 * cells / int(routing.valid.sum()),
                    threshold / 1e6, spec.dem_path))
        return threshold

    def _records(self, spec, grid, routing, area, dem, segments, threshold):
        """One dict per segment: geometry, attributes, and the checks."""
        windows = H.Windows(grid, routing.raw, routing.valid)
        stream_cells = np.unique(np.concatenate(
            [np.asarray(s.cells) for s in segments]))
        relief_all = np.zeros(grid.size, dtype=np.float32)
        relief_all[stream_cells] = windows.relief(stream_cells)
        heads = np.array([s.cells[0] for s in segments])
        near_void = windows.near_void(heads)
        clip = self._clip_geometry(spec)

        records = []
        for count, (segment, edge) in enumerate(zip(segments, near_void)):
            cells = np.asarray(segment.cells)
            length = H.segment_length_m(grid, segment)
            x, y = H.coordinates(grid, dem.transform, cells)
            points = [QgsPointXY(a, b) for a, b in zip(x.tolist(), y.tolist())]
            end = -2 if segment.junction and len(cells) >= 2 else -1
            up_m2 = float(area[cells[end]])
            elev_up = float(routing.filled[cells[0]])
            elev_dn = float(routing.filled[cells[-1]])
            slope = 100.0 * (elev_up - elev_dn) / length if length > 0 else 0.0
            fill = float(np.max(routing.filled[cells] - routing.raw[cells]))
            relief = float(np.mean(relief_all[cells]))
            flags, conf = CHECK.classify(slope, fill, relief,
                                         bool(edge) and segment.head)
            geometry = QgsGeometry.fromPolylineXY(points)
            if spec.smooth and len(points) >= 3:
                smoothed = geometry.smooth(SMOOTH_ITERATIONS, SMOOTH_OFFSET)
                if smoothed is not None and not smoothed.isEmpty():
                    geometry = smoothed
            share = 1.0
            if clip is not None:
                before = geometry.length()
                geometry = geometry.intersection(clip)
                if geometry is None or geometry.isEmpty():
                    continue
                share = geometry.length() / before if before > 0 else 0.0
                if share <= 0:
                    continue
            records.append({
                'ident': segment.ident, 'next': segment.to_ident,
                'order': segment.order, 'length': length * share,
                'up_km2': up_m2 / 1e6, 'elev_up': elev_up,
                'elev_dn': elev_dn, 'slope': slope, 'fill': fill,
                'relief': relief, 'flags': flags, 'conf': conf,
                'parts': _parts(geometry), 'head': segment.head})
            if count % 500 == 0:
                if self.should_stop():
                    raise H.Stopped()
                self._report(0.5 * count / max(1, len(segments)))

        if not records:
            raise StreamError(
                'The streams all lie outside the area you chose. The DEM was '
                'kept at {0}.'.format(spec.dem_path))
        return records, _stats(records, grid, routing, area, threshold)

    @staticmethod
    def _clip_geometry(spec):
        """The ROI in the DEM's CRS when streams must be cut back to it."""
        if not spec.margin:
            return None
        geometry = QgsGeometry(spec.roi.geometry)
        if spec.crs is not None and spec.crs.isValid() \
                and spec.crs.authid() != DEM.WGS84:
            geometry.transform(QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(DEM.WGS84), spec.crs,
                QgsCoordinateTransformContext()))
        return geometry

    def _fetch_osm(self, spec):
        """Rivers and streams from OpenStreetMap, on this worker thread.

        ``QgsBlockingNetworkRequest`` is made for worker threads, so a slow
        Overpass server holds up this job and nothing else. A failure does not
        fail the run: the streams are still good, only the comparison is lost.
        """
        self._report(0.0, 'Fetching rivers and streams from OpenStreetMap')
        try:
            spec.reference = CHECK.fetch_osm(spec.osm_bounds, net.get,
                                             self.should_stop)
        except CHECK.ReferenceError as exc:
            spec.reference_problem = str(exc)

    def _compare(self, spec, records, dem):
        """Streams against the reference, in a metric CRS."""
        stream_crs = spec.crs if spec.crs is not None \
            else QgsCoordinateReferenceSystem(DEM.WGS84)
        metric = metric_crs(stream_crs, spec.roi)
        context = QgsCoordinateTransformContext()
        forward = QgsCoordinateTransform(stream_crs, metric, context)
        reference = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem(DEM.WGS84), metric, context)

        extracted = []
        for record in records:
            for part in record['parts']:
                geometry = QgsGeometry.fromPolylineXY(
                    [QgsPointXY(a, b) for a, b in part])
                geometry.transform(forward)
                extracted.append((record['ident'], record['conf'],
                                  record['up_km2'], record['order'],
                                  _vertices(geometry)))
        lines = []
        for line in spec.reference.lines:
            geometry = QgsGeometry.fromPolylineXY(
                [QgsPointXY(a, b) for a, b in line])
            geometry.transform(reference)
            lines.append(_vertices(geometry))

        def stop():
            self._report(0.5)
            return self.should_stop()

        found = CHECK.compare(extracted, lines, spec.tolerance, stop=stop)
        if found is None:
            return None
        found.metric_to_stream = QgsCoordinateTransform(
            metric, stream_crs, context)
        return found

    # --------------------------------------------------------------- writing

    def _write(self, spec, dem, grid, area, records, comparison, result):
        _write_streams(spec, dem, records, comparison)
        if spec.accum_path:
            _write_accumulation(spec.accum_path, dem, grid, area)
            result['accumulation'] = spec.accum_path


def start(task):
    QgsApplication.taskManager().addTask(task)
    return task


# ------------------------------------------------------------------ the steps

class _Dem(object):
    """A DEM held in memory: the heights, which cells hold data, georeferencing."""

    def __init__(self, elevation, valid, transform, wkt, low, high):
        self.elevation = elevation
        self.valid = valid
        self.transform = transform
        self.wkt = wkt
        self.low = low
        self.high = high
        self.rows, self.cols = elevation.shape
        self.pixels = self.rows * self.cols


def _read_dem(path):
    from osgeo import gdal

    dataset = gdal.Open(path)
    if dataset is None:
        raise StreamError('The DEM was written but could not be read back.')
    try:
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        elevation = band.ReadAsArray().astype(np.float32)
        transform = dataset.GetGeoTransform()
        wkt = dataset.GetProjection()
    finally:
        band = None
        dataset = None
    valid = np.isfinite(elevation)
    if nodata is not None:
        valid &= elevation != np.float32(nodata)
    if not valid.any():
        _remove(path)                       # an all-NoData raster is no use
        raise StreamError('The DEM has no elevation values inside that area. '
                          'Over open sea there is nothing to trace.')
    low = float(elevation[valid].min())
    high = float(elevation[valid].max())
    return _Dem(elevation, valid, transform, wkt, low, high)


def _parts(geometry):
    """The lines of a geometry as a list of (n, 2) float arrays."""
    if geometry.isMultipart():
        lines = geometry.asMultiPolyline()
    else:
        lines = [geometry.asPolyline()]
    return [np.array([(p.x(), p.y()) for p in line]) for line in lines
            if len(line) >= 2]


def _vertices(geometry):
    parts = _parts(geometry)
    return parts[0] if parts else np.zeros((0, 2))


def _stats(records, grid, routing, area, threshold):
    """Totals for the report."""
    length_km = sum(r['length'] for r in records) / 1000.0
    by_order = {}
    for record in records:
        count, km = by_order.get(record['order'], (0, 0.0))
        by_order[record['order']] = (count + 1, km + record['length'] / 1000.0)
    confidence_km = {CHECK.CONF_HIGH: 0.0, CHECK.CONF_MEDIUM: 0.0,
                     CHECK.CONF_LOW: 0.0}
    flag_km = {CHECK.FLAG_FLAT: 0.0, CHECK.FLAG_FILLED: 0.0,
               CHECK.FLAG_NOVALLEY: 0.0}
    edge_heads = 0
    for record in records:
        confidence_km[record['conf']] += record['length'] / 1000.0
        for flag in record['flags'].split():
            if flag in flag_km:
                flag_km[flag] += record['length'] / 1000.0
            elif flag == CHECK.FLAG_EDGE:
                edge_heads += 1
    raised = routing.filled - routing.raw
    inside = routing.valid
    filled_cells = int(np.count_nonzero((raised > 0.01) & inside))
    return {
        'length_km': length_km, 'by_order': by_order,
        'max_order': max(by_order) if by_order else 0,
        'confidence_km': confidence_km, 'flag_km': flag_km,
        'edge_heads': edge_heads,
        'valid_km2': float(np.sum(grid.row_values(
            grid.dx * grid.dy)[inside])) / 1e6,
        'filled_share': filled_cells / max(1, int(inside.sum())),
        'deepest_fill': float(raised[inside].max()) if inside.any() else 0.0,
        'cell_m': float(np.mean(np.sqrt(grid.dx * grid.dy))),
    }


def _sensitivity(grid, area, valid):
    """Approximate network length at each preset detail level, in km."""
    cell = float(np.mean(np.sqrt(grid.dx * grid.dy)))
    rows = []
    for _id, label, km2, _tip in DETAIL_PRESETS:
        cells = int(np.count_nonzero((area >= km2 * 1e6) & valid))
        rows.append((label, km2, cells * cell * 1.1 / 1000.0))
    return rows


# --------------------------------------------------------------- the outputs

def _create_geometry(ogr, parts):
    geometry = QgsGeometry.fromPolylineXY(
        [QgsPointXY(a, b) for a, b in parts])
    return ogr.CreateGeometryFromWkb(bytes(geometry.asWkb()))


def _srs_for(spec, dem):
    from osgeo import osr

    srs = osr.SpatialReference()
    if dem.wkt:
        srs.ImportFromWkt(dem.wkt)
    else:
        srs.SetFromUserInput(DEM.WGS84)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _write_streams(spec, dem, records, comparison):
    from osgeo import ogr

    _discard(spec.stream_path)
    driver = ogr.GetDriverByName('GPKG')
    target = driver.CreateDataSource(spec.stream_path)
    if target is None:
        raise StreamError('The stream file could not be created at {0}. Is '
                          'that folder writable?'.format(spec.stream_path))
    try:
        srs = _srs_for(spec, dem)
        layer = target.CreateLayer(STREAM_LAYER, srs, ogr.wkbLineString)
        for name, kind, width in (
                (FIELD_ID, ogr.OFTInteger, 0), (FIELD_NEXT, ogr.OFTInteger, 0),
                (FIELD_ORDER, ogr.OFTInteger, 0),
                (FIELD_LENGTH, ogr.OFTReal, 0), (FIELD_UP, ogr.OFTReal, 0),
                (FIELD_ELEV_UP, ogr.OFTReal, 0),
                (FIELD_ELEV_DN, ogr.OFTReal, 0),
                (FIELD_SLOPE, ogr.OFTReal, 0), (FIELD_FILL, ogr.OFTReal, 0),
                (FIELD_RELIEF, ogr.OFTReal, 0),
                (FIELD_CONF, ogr.OFTString, 8),
                (FIELD_FLAGS, ogr.OFTString, 32)):
            definition = ogr.FieldDefn(name, kind)
            if width:
                definition.SetWidth(width)
            layer.CreateField(definition)
        layer.StartTransaction()
        try:
            for record in records:
                for part in record['parts']:
                    feature = ogr.Feature(layer.GetLayerDefn())
                    feature.SetField(FIELD_ID, record['ident'])
                    feature.SetField(FIELD_NEXT, record['next'])
                    feature.SetField(FIELD_ORDER, record['order'])
                    feature.SetField(FIELD_LENGTH, round(record['length'], 1))
                    feature.SetField(FIELD_UP, round(record['up_km2'], 4))
                    feature.SetField(FIELD_ELEV_UP, round(record['elev_up'], 1))
                    feature.SetField(FIELD_ELEV_DN, round(record['elev_dn'], 1))
                    feature.SetField(FIELD_SLOPE, round(record['slope'], 3))
                    feature.SetField(FIELD_FILL, round(record['fill'], 1))
                    feature.SetField(FIELD_RELIEF, round(record['relief'], 1))
                    feature.SetField(FIELD_CONF, record['conf'])
                    feature.SetField(FIELD_FLAGS, record['flags'])
                    feature.SetGeometry(_create_geometry(ogr, part))
                    layer.CreateFeature(feature)
        except BaseException:
            layer.RollbackTransaction()
            raise
        layer.CommitTransaction()

        if comparison is not None and (comparison.extra
                                       or comparison.missed):
            _write_check_layer(ogr, target, srs, comparison)
        layer = None
    finally:
        target = None


def _write_check_layer(ogr, target, srs, comparison):
    layer = target.CreateLayer(CHECK_LAYER, srs, ogr.wkbLineString)
    for name, kind, width in ((FIELD_KIND, ogr.OFTString, 8),
                              (FIELD_ID, ogr.OFTInteger, 0),
                              (FIELD_LENGTH, ogr.OFTReal, 0),
                              (FIELD_UP, ogr.OFTReal, 0),
                              (FIELD_CONF, ogr.OFTString, 8)):
        definition = ogr.FieldDefn(name, kind)
        if width:
            definition.SetWidth(width)
        layer.CreateField(definition)
    back = comparison.metric_to_stream
    layer.StartTransaction()
    try:
        runs = [(KIND_EXTRA, ident, run, up, conf, points)
                for ident, conf, up, run, points in comparison.extra]
        runs += [(KIND_MISSED, 0, run, None, None, points)
                 for run, points in comparison.missed]
        for kind, ident, run, up, conf, points in runs:
            geometry = QgsGeometry.fromPolylineXY(
                [QgsPointXY(a, b) for a, b in points.tolist()])
            geometry.transform(back)
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField(FIELD_KIND, kind)
            if ident:
                feature.SetField(FIELD_ID, ident)
            feature.SetField(FIELD_LENGTH, round(run, 1))
            if up is not None:
                feature.SetField(FIELD_UP, round(up, 4))
                feature.SetField(FIELD_CONF, conf)
            feature.SetGeometry(ogr.CreateGeometryFromWkb(
                bytes(geometry.asWkb())))
            layer.CreateFeature(feature)
    except BaseException:
        layer.RollbackTransaction()
        raise
    layer.CommitTransaction()


def _write_accumulation(path, dem, grid, area):
    """Contributing area in km2, on the DEM's grid."""
    from osgeo import gdal

    _remove(path)
    driver = gdal.GetDriverByName('GTiff')
    target = driver.Create(path, dem.cols, dem.rows, 1, gdal.GDT_Float32,
                           ['COMPRESS=DEFLATE', 'TILED=YES',
                            'BIGTIFF=IF_SAFER'])
    if target is None:
        raise StreamError('The flow accumulation file could not be created.')
    try:
        target.SetGeoTransform(dem.transform)
        target.SetProjection(dem.wkt)
        band = target.GetRasterBand(1)
        band.SetNoDataValue(-1.0)
        values = grid.unpad(area) / np.float32(1e6)
        values = np.where(dem.valid, values, np.float32(-1.0)).astype(
            np.float32)
        band.WriteArray(values)
        band.FlushCache()
        band = None
    finally:
        target = None


def _discard(path):
    """Delete a GeoPackage and its sidecars, quietly."""
    for suffix in ('', '-wal', '-shm', '-journal'):
        _remove(path + suffix)


def _remove(path):
    with suppress(OSError):
        if os.path.exists(path):
            os.remove(path)


# -------------------------------------------------------------------- report

def build_report(spec, dem, stats, records, comparison, threshold):
    """The run, and how far to trust it, as a :class:`stream_check.Report`."""
    report = CHECK.Report('Rivers and streams - {0}'.format(spec.title))
    cell = stats['cell_m']
    area_km2 = stats['valid_km2']
    length = stats['length_km']
    report.add('The network', [
        ('Source', ('Local DEM, about {0:.0f} m cells' if spec.local else
                    'Copernicus DEM, about {0:.0f} m cells').format(cell)),
        ('Area analysed', '{0:,.1f} km2'.format(area_km2)),
        ('Stream threshold', '{0:,.3f} km2 of ground draining through a cell '
                             '(about {1:,.0f} cells)'.format(
                                 threshold / 1e6, threshold / (cell * cell))),
        ('Stream segments', '{0:,}'.format(len(records))),
        ('Total length', '{0:,.1f} km'.format(length)),
        ('Drainage density', '{0:,.2f} km per km2'.format(
            length / area_km2 if area_km2 else 0.0)),
        ('Highest Strahler order', str(stats['max_order'])),
    ])
    report.add('By stream order', [
        ('Order {0}'.format(order), '{0:,} segments, {1:,.1f} km'.format(
            count, km)) for order, (count, km) in sorted(
                stats['by_order'].items())])

    conf = stats['confidence_km']
    total = max(length, 1e-9)
    rows = [('{0} confidence'.format(grade), '{0:,.1f} km ({1:.0f}%)'.format(
        conf[grade], 100.0 * conf[grade] / total))
        for grade in (CHECK.CONF_HIGH, CHECK.CONF_MEDIUM, CHECK.CONF_LOW)]
    flag_km = stats['flag_km']
    rows += [('On flat ground (slope under {0:g}%)'.format(
                  CHECK.FLAT_SLOPE_PCT),
              '{0:,.1f} km'.format(flag_km[CHECK.FLAG_FLAT])),
             ('Across a depression filled by {0:g} m or more'.format(
                 CHECK.FILL_FLAG_M),
              '{0:,.1f} km'.format(flag_km[CHECK.FLAG_FILLED])),
             ('Along no clear valley', '{0:,.1f} km'.format(
                 flag_km[CHECK.FLAG_NOVALLEY])),
             ('Sources cut off at the edge of the data',
              '{0:,}'.format(stats['edge_heads'])),
             ('Cells raised to fill depressions', '{0:.1f}% of the area, '
              'deepest {1:,.1f} m'.format(100.0 * stats['filled_share'],
                                          stats['deepest_fill']))]
    notes = []
    low_share = conf[CHECK.CONF_LOW] / total
    if low_share >= 0.4:
        notes.append(
            '{0:.0f}% of the stream length is low confidence. That is typical '
            'of flat country: where the ground falls less than the DEM\'s own '
            'error, the trace is a best guess and may run straight or in '
            'parallel lines. Treat those reaches as indicative and check '
            'them against imagery.'.format(100 * low_share))
    elif low_share <= 0.1:
        notes.append('Most of the network lies in clear valleys on sloping '
                     'ground, where a DEM trace is at its most reliable.')
    if stats['edge_heads'] and not spec.margin:
        notes.append(
            '{0} stream{1} start right at the edge of the data, so '
            'the real source is probably outside the area and the upstream '
            'part is missing. Set "Include upstream area" to catch it.'
            .format(stats['edge_heads'],
                    's' if stats['edge_heads'] != 1 else ''))
    report.add('Internal checks (from the DEM alone)', rows, notes)

    if comparison is not None:
        report.add('Comparison with the reference',
                   *_comparison_rows(spec, comparison, conf, total))
    elif spec.reference_problem:
        report.add('Comparison with the reference', paragraphs=[
            'The comparison could not be made: {0}'.format(
                spec.reference_problem)])

    report.add('If you chose another detail level (approximate length)', [
        (label, '{0:,.0f} km'.format(km)) for label, _a, km
        in stats['sensitivity']])
    report.add('Reading this report', paragraphs=[
        'A DEM-derived network shows where water would run, not where '
        'channels are surveyed to be. It includes gullies that carry water '
        'only after rain, and it can miss or misplace streams across flat '
        'ground, through culverts, under bridges and along canals.',
        'A surface model such as the Copernicus DEM follows tree tops and '
        'buildings, so a stream under dense canopy can be displaced. Its '
        'vertical error is a few metres, which is why gentle slopes are '
        'flagged.' if not spec.local else
        'Heights in a local DEM are taken as metres. A surface model '
        'follows tree tops and buildings, so a stream under dense canopy '
        'can be displaced.',
    ] + ([] if spec.local else [
        'Copernicus DEM (c) DLR e.V. 2010-2014 and (c) Airbus Defence and '
        'Space GmbH 2014-2018, provided under COPERNICUS by the European '
        'Union and ESA.']))
    return report


def _comparison_rows(spec, comparison, conf, total):
    reference = spec.reference

    def km(metres):
        return '{0:,.1f} km'.format(metres / 1000.0)

    low_extra = sum(item[3] for item in comparison.extra
                    if item[1] == CHECK.CONF_LOW)
    extra = comparison.extra_m()
    rows = [('Reference', '{0}{1}'.format(
                reference.label, ' ({0})'.format(reference.note)
                if reference.note else '')),
            ('Match distance', '{0:,.0f} m'.format(spec.tolerance)),
            ('DEM streams', km(comparison.extracted_m)),
            ('Reference streams', km(comparison.reference_m)),
            ('DEM streams with a reference stream nearby',
             '{0:.0f}%'.format(100.0 * comparison.precision)),
            ('Reference streams with a DEM stream nearby',
             '{0:.0f}%'.format(100.0 * comparison.recall)),
            ('In the DEM, not in the reference', '{0} in {1:,} stretches'
             .format(km(extra), len(comparison.extra))),
            ('In the reference, not in the DEM', '{0} in {1:,} stretches'
             .format(km(comparison.missed_m()), len(comparison.missed)))]
    notes = []
    if comparison.reference_m <= 0:
        notes.append('The reference has no stream in this area, so there is '
                     'nothing to compare with.')
        return rows, notes
    notes.append('Result: {0}.'.format(CHECK.agreement_words(
        comparison.precision, comparison.recall)).capitalize())
    if extra > 0:
        notes.append(
            '{0:.0f}% of the stream length in the DEM but not the reference '
            'is low confidence by the internal checks, which fits '
            'false streams on flat ground.'.format(
                100.0 * low_extra / extra))
    notes.append(
        'Not matching does not prove a stream wrong: a reference can be '
        'incomplete (OpenStreetMap often is in the countryside), and a DEM '
        'stream can be a real gully that carries water only after rain.')
    if comparison.extra or comparison.missed:
        notes.append(
            'The "{0}" layer on the map draws every stretch that only one '
            'network has: judge each against imagery.'.format(CHECK_LAYER))
    orders = sorted(comparison.by_order)
    if len(orders) > 1 and comparison.by_order[orders[0]][0] > 0:
        first = comparison.by_order[orders[0]]
        last = comparison.by_order[orders[-1]]
        if last[0] > 0 and last[1] / last[0] - first[1] / first[0] > 0.2:
            notes.append(
                'Agreement rises with stream size: the bigger streams match '
                'far better than the smallest ones. That is the usual '
                'pattern - small headwater streams are the ones maps leave '
                'out, and the ones a DEM is least sure of.')
    if comparison.missed_m() > 0.3 * comparison.reference_m:
        notes.append(
            'A good share of the reference streams has no DEM stream beside '
            'it. If they are small streams, a more detailed setting will pick '
            'them up.')
    if comparison.reference_m / max(1.0, comparison.extracted_m) < 0.25:
        notes.append(
            'The reference holds far less stream than the DEM, so it is '
            'probably incomplete here and the "in the DEM, not in the '
            'reference" figure is mostly its gaps.')
    return rows, notes

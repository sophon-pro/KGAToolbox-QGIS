# -*- coding: utf-8 -*-
"""Detect Roads from Imagery: the background job and what it writes.

One ``RoadTask`` per run, on a QgsTask thread. Its steps, on one progress bar:

1. read the imagery block by block on the area's UTM grid and run the model
   over each block (the bulk of the time);
2. clean the probability into a road mask;
3. thin and trace the mask into centrelines, prune spurs, join gaps;
4. turn the mask into surface polygons and their edges;
5. optionally compare the centrelines with OpenStreetMap or a line layer;
6. write ``<name>_roads.gpkg`` (and the probability / imagery GeoTIFFs).

Everything the GUI resolves (the layer clone, the area, the paths) arrives in a
``RoadSpec`` so the worker touches no project object.
"""

import math
import os
import tempfile
import time
import traceback
from contextlib import suppress

import numpy as np
from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsCoordinateTransformContext,
                       QgsGeometry, QgsMessageLog, QgsPointXY,
                       QgsProcessingUtils, QgsTask)
from qgis.PyQt.QtCore import pyqtSignal

from .. import stream_check as CHECK
from . import infer, runtime, sampler
from . import skeleton as SK
from . import vectorize as V

LOG_TAG = 'KGA Toolbox'

LAYER_CENTRE = 'road_centerlines'
LAYER_EDGE = 'road_edges'
LAYER_SURFACE = 'road_surface'
LAYER_CHECK = 'road_check'

KIND_EXTRA = 'extra'
KIND_MISSED = 'missed'

#: Post-processing runs this many times coarser than the model: the model's
#: mask is smooth and about 20 model pixels wide, so 0.6 m loses nothing and
#: needs a quarter of the memory.
POST_FACTOR = 2

#: Context read around the area so roads at its edge are seen whole, metres.
MARGIN_M = 20.0

#: Model pixels: warn, warn hard, refuse. At 0.3 m, 40 M is 3.6 km2.
WARN_PIXELS = 40 * 1000 * 1000
HUGE_PIXELS = 160 * 1000 * 1000
MAX_PIXELS = 600 * 1000 * 1000

#: Sensitivity presets: probability a pixel needs to count as road.
SENSITIVITY = (
    ('strict', 'Strict - fewer false roads, misses more', 0.65),
    ('normal', 'Normal', 0.5),
    ('sensitive', 'Sensitive - more narrow and earth roads', 0.35),
    ('very', 'Very sensitive - most roads, more false ones', 0.2),
)
DEFAULT_SENSITIVITY = 'normal'

#: Simplify presets: Douglas-Peucker tolerance in metres (None = custom).
#: Light is about one post-processing pixel: it only takes the stairs off.
SIMPLIFY = (
    ('off', 'Off - keep every traced step', 0.0),
    ('light', 'Light (0.5 m) - follows every bend', 0.5),
    ('medium', 'Medium (1.5 m) - straight streets, kept curves', 1.5),
    ('strong', 'Strong (3 m) - mapping scale, fewest vertices', 3.0),
    ('custom', 'Custom', None),
)
DEFAULT_SIMPLIFY = 'light'

#: OpenStreetMap highway values that are roads one can see from above.
OSM_HIGHWAYS = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary',
                'unclassified', 'residential', 'service', 'living_street',
                'track', 'road', 'motorway_link', 'trunk_link', 'primary_link',
                'secondary_link', 'tertiary_link')


class RoadError(Exception):
    """A run that cannot go on. Shown to the user as-is."""


class Stopped(Exception):
    pass


def threshold_for(key):
    for ident, _label, value in SENSITIVITY:
        if ident == key:
            return value
    return 0.5


def temp_folder():
    base = os.path.join(QgsProcessingUtils.tempFolder(), 'kga_roads')
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix='run_', dir=base)


def size_warning(pixels):
    """(severe, text) for a large job, or None."""
    if pixels < WARN_PIXELS:
        return None
    # About 0.9 s per million pixels was measured for reading Esri tiles and
    # running the model on a laptop CPU; a slow tile service takes longer.
    minutes = int(round(pixels / 1e6 * 0.9 / 60.0))
    took = 'about a minute' if minutes <= 1 else \
        'about {0} minutes'.format(minutes)
    text = ('That area is {0:,} million pixels at the model\'s 0.3 m. Expect '
            '{1} or more, mostly reading the imagery, and {2:.1f} GB of '
            'memory.').format(pixels // 1000000, took,
                              pixels / POST_FACTOR ** 2 * 24 / 1e9 + 0.3)
    return pixels >= HUGE_PIXELS, text


def osm_urls(bounds):
    from urllib.parse import quote

    west, south, east, north = bounds
    query = ('[out:json][timeout:60];way["highway"~"^({0})$"]'
             '({1:.6f},{2:.6f},{3:.6f},{4:.6f});out geom;').format(
                 '|'.join(OSM_HIGHWAYS), south, west, north, east)
    return ['{0}?data={1}'.format(base, quote(query))
            for base in CHECK.OVERPASS_URLS]


def fetch_osm_roads(bounds, get, stop=None):
    """OpenStreetMap roads in `bounds`, as a stream_check.Reference.

    Each Overpass server is tried in turn; when all fail, every server's
    complaint is given, since the last one alone is often just "empty".
    """
    from urllib.parse import urlparse

    problems = []
    for url in osm_urls(bounds):
        if stop is not None and stop():
            raise CHECK.ReferenceError('Stopped.')
        host = urlparse(url).hostname
        try:
            body = get(url)
            if not body:
                raise CHECK.ReferenceError('it sent an empty answer')
            reference = CHECK.parse_overpass(body)
            reference.label = 'OpenStreetMap roads'
            return reference
        except Exception as exc:                # NetError, ReferenceError
            problems.append('{0}: {1}'.format(host, exc))
    raise CHECK.ReferenceError(
        'OpenStreetMap (Overpass) did not answer - {0}. The servers are often '
        'busy; try again in a few minutes.'.format('; '.join(problems)))


# ------------------------------------------------------------------- the job

class RoadSpec(object):
    """Everything one run needs, resolved on the GUI thread."""

    def __init__(self, **values):
        #: QgsRasterLayer clone to render.
        self.layer = None
        self.layer_name = ''
        self.local = False
        #: The area, as QgsGeometry in `area_crs`; `clip` cuts results to it.
        self.area = None
        self.area_crs = None
        self.clip = False
        self.area_label = ''
        #: Bounds of the area in EPSG:4326 (west, south, east, north).
        self.bounds = None
        self.work_crs = None
        self.out_crs = None
        self.model_path = ''
        self.threshold = 0.5
        self.min_area_m2 = 60.0
        self.hole_m2 = 20.0
        self.spur_m = 8.0
        self.gap_m = 30.0
        self.min_length_m = 25.0
        self.smooth = True
        #: Douglas-Peucker tolerance for every output, metres; 0 is off.
        self.simplify_m = 0.5
        self.want_centre = True
        self.want_edge = True
        self.want_surface = True
        self.gpkg_path = ''
        self.prob_path = None
        self.image_path = None
        self.report_path = None
        self.title = 'Roads'
        #: stream_check.Reference to compare with, or None.
        self.reference = None
        #: Fetch OpenStreetMap roads for `bounds` in the task.
        self.compare_osm = False
        self.tolerance = 6.0
        self.reference_problem = ''
        self.__dict__.update(values)

    @property
    def has_reference(self):
        return self.reference is not None or self.compare_osm

    def steps(self):
        steps = [('Reading the imagery and detecting roads', 0.78),
                 ('Cleaning the road mask', 0.04),
                 ('Tracing the centerlines', 0.06),
                 ('Building the road surface and edges', 0.05)]
        if self.has_reference:
            steps.append(('Comparing with the reference', 0.04))
        steps.append(('Writing the results', 0.03))
        total = sum(weight for _l, weight in steps)
        return [(label, weight / total) for label, weight in steps]


class RoadTask(QgsTask):
    """Read, detect, trace, write. One job at a time, on a worker."""

    phase = pyqtSignal(str, float)
    jobFinished = pyqtSignal(dict)

    def __init__(self, spec):
        flag = getattr(QgsTask, 'Flag', QgsTask)     # scoped enum, QGIS 3.36+
        super().__init__('Roads: {0}'.format(spec.title), flag.CanCancel)
        self.spec = spec
        self.result = {}
        self._stop = False
        self._steps = spec.steps()
        self._step = 0
        self._base = 0.0
        self._labels = [label for label, _w in self._steps]

    def request_stop(self):
        self._stop = True

    def should_stop(self):
        return self._stop or self.isCanceled()

    # ------------------------------------------------------------- progress

    def _begin(self, label):
        self._step = self._labels.index(label)
        self._base = sum(w for _l, w in self._steps[:self._step])
        self._report(0.0)

    def _report(self, fraction, label=None):
        step_label, weight = self._steps[self._step]
        overall = min(1.0, self._base + weight * max(0.0, min(1.0, fraction)))
        self.setProgress(100.0 * overall)
        self.phase.emit(label or step_label, overall)

    def _check(self):
        if self.should_stop():
            raise Stopped()

    # ------------------------------------------------------------------ run

    def run(self):
        spec = self.spec
        result = {'state': 'error', 'message': '', 'gpkg': spec.gpkg_path,
                  'report_html': '', 'report_text': '', 'report_path': None,
                  'centrelines': 0, 'length_km': 0.0, 'surfaces': 0,
                  'edges': 0, 'bridges': 0, 'check_layer': False,
                  'prob_path': None, 'image_path': None,
                  'reference_problem': '', 'scale': 1}
        started = time.time()
        try:
            self._run(spec, result)
            result['seconds'] = time.time() - started
        except Stopped:
            result['state'] = 'stopped'
        except (RoadError, runtime.RuntimeProblem, sampler.SamplerError) as exc:
            result.update(state='error', message=str(exc))
        except MemoryError:
            result.update(state='error', message=(
                'The PC ran out of memory. Use a smaller area.'))
        except Exception as exc:                # never let a task die silently
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            result.update(state='error', message='The roads could not be '
                                                 'detected: {0}'.format(exc))
        if result['state'] != 'done':
            _discard(spec.gpkg_path)
            for path in (spec.prob_path, spec.image_path):
                if path:
                    _remove(path)
        self.result = result
        return result['state'] == 'done'

    def finished(self, success):
        self.jobFinished.emit(self.result)

    def _run(self, spec, result):
        self._t0 = time.time()
        ort = runtime.load_onnxruntime()
        runtime.check_model_file(spec.model_path)
        session = runtime.open_session(ort, spec.model_path)
        model = runtime.MODEL
        predictor = infer.Predictor(session, model)

        grid = sampler.Grid.around(spec.area, spec.area_crs, spec.work_crs,
                                   model['gsd'], MARGIN_M)
        if grid.pixels > MAX_PIXELS:
            raise RoadError('That area is {0:,} million pixels at 0.3 m, more '
                            'than this tool reads in one go. Choose a smaller '
                            'area.'.format(grid.pixels // 1000000))
        post = grid.coarser(POST_FACTOR)

        self._begin('Reading the imagery and detecting roads')
        prob, rgb, stats = self._detect(spec, grid, post, predictor)
        result['scale'] = stats['scale']
        if stats['valid'] == 0:
            raise RoadError(
                'The imagery layer drew nothing over this area. Check that it '
                'covers the area and shows on the map here.')
        self._check()

        timing = result.setdefault('timing', {})
        timing['detect'] = time.time() - self._t0
        self._begin('Cleaning the road mask')
        area_post = self._area_in(spec, post.crs)
        inside = V.roi_mask(post, area_post) if spec.clip else \
            np.ones((post.rows, post.cols), dtype=bool)
        valid_area = inside & stats['valid_mask']
        pixel_m2 = post.gsd * post.gsd
        mask = V.clean_mask(prob, spec.threshold, valid_area,
                            max(1, int(spec.min_area_m2 / pixel_m2)),
                            max(1, int(spec.hole_m2 / pixel_m2)))
        self._report(1.0)
        if not mask.any():
            raise RoadError(
                'No road was found. Try a more sensitive setting, or imagery '
                'with a finer resolution (the model expects about 0.3 m).')
        self._check()

        from scipy import ndimage
        distance = ndimage.distance_transform_edt(mask).astype(np.float32)

        timing['clean'] = time.time() - self._t0 - timing['detect']
        self._begin('Tracing the centerlines')
        graph = SK.trace(SK.thin(V.mask_to_thin(mask, 1.5),
                                 stop=self.should_stop),
                         stop=self.should_stop)
        if graph is None:
            raise Stopped()
        self._report(0.5)
        SK.prune(graph, spec.spur_m / post.gsd)
        bridges = SK.bridge(graph, spec.gap_m / post.gsd,
                            stop=self.should_stop) if spec.gap_m > 0 else 0
        SK.merge_through(graph)
        SK.drop_small_networks(graph, spec.min_length_m / post.gsd)
        self._check()
        clip = self._clip_geometry(spec, area_post)
        centres = self._centre_records(spec, graph, post, prob, distance, rgb,
                                       clip)
        result['bridges'] = sum(1 for c in centres if c['bridge'])
        self._report(1.0)

        timing['trace'] = time.time() - self._t0
        self._begin('Building the road surface and edges')
        surfaces, edges = [], []
        if spec.want_surface or spec.want_edge:
            surfaces, edges = self._surfaces(spec, mask, post, prob,
                                             area_post)
        self._report(1.0)

        comparison = None
        if spec.has_reference:
            self._begin('Comparing with the reference')
            comparison = self._compare(spec, centres, post.crs,
                                       area_post if spec.clip else
                                       QgsGeometry.fromRect(_grid_rect(post)))
            self._report(1.0)

        timing['surface_compare'] = time.time() - self._t0
        self._begin('Writing the results')
        self._write(spec, post.crs, centres, edges, surfaces, comparison)
        if spec.prob_path:
            _write_probability(spec.prob_path, post, prob, stats['valid_mask'])
            result['prob_path'] = spec.prob_path
        if spec.image_path and os.path.exists(spec.image_path):
            result['image_path'] = spec.image_path
        length_km = sum(c['length'] for c in centres) / 1000.0
        report = build_report(spec, grid, post, stats, centres, surfaces,
                              edges, bridges, comparison, length_km)
        result['report_html'] = report.html()
        result['report_text'] = report.text()
        if spec.report_path:
            with suppress(OSError):
                with open(spec.report_path, 'w', encoding='utf-8') as handle:
                    handle.write(result['report_text'])
                result['report_path'] = spec.report_path
        result.update(centrelines=len(centres), length_km=length_km,
                      surfaces=len(surfaces), edges=len(edges),
                      check_layer=bool(comparison is not None and (
                          comparison.extra or comparison.missed)),
                      reference_problem=spec.reference_problem,
                      paved_km=sum(c['length'] for c in centres
                                   if c['surface'] == 'paved') / 1000.0)
        self._report(1.0)
        result['state'] = 'done'

    # ----------------------------------------------------------- the stages

    def _detect(self, spec, grid, post, predictor):
        """Every block: render, predict, keep the middle at post resolution."""
        prob = np.zeros((post.rows, post.cols), dtype=np.float32)
        rgb = np.zeros((post.rows, post.cols, 3), dtype=np.uint8)
        valid_mask = np.zeros((post.rows, post.cols), dtype=bool)
        image = _ImageWriter(spec.image_path, grid) if spec.image_path \
            else None
        pad = sampler.PAD
        total = grid.block_count()
        scale = 1
        placeholder_blocks = 0
        valid_pixels = 0
        try:
            for index, (row, col, rows, cols) in enumerate(grid.blocks()):
                self._check()
                block, valid = sampler.render(spec.layer, grid, row, col,
                                              rows, cols, pad, scale)
                # A tile service with no imagery at this zoom: read the block
                # again a zoom level up, then two, and keep that for the rest.
                while not spec.local and scale < 8 and \
                        sampler.looks_like_placeholder(block, valid):
                    scale *= 2
                    block, valid = sampler.render(spec.layer, grid, row, col,
                                                  rows, cols, pad, scale)
                if sampler.looks_like_placeholder(block, valid):
                    placeholder_blocks += 1
                    valid[:] = False
                p = predictor.predict(block, valid, stop=self.should_stop)
                if p is None:
                    raise Stopped()
                core = (slice(pad, pad + rows), slice(pad, pad + cols))
                core_rgb = block[core]
                core_valid = valid[core]
                valid_pixels += int(core_valid.sum())
                if image is not None:
                    image.write(row, col, core_rgb, core_valid)
                r0, c0 = row // POST_FACTOR, col // POST_FACTOR
                small_p = sampler.downsample(p[core], POST_FACTOR)
                small_rgb = sampler.downsample(core_rgb, POST_FACTOR)
                small_valid = sampler.downsample(
                    core_valid.astype(np.float32), POST_FACTOR) >= 0.5
                h = min(small_p.shape[0], post.rows - r0)
                w = min(small_p.shape[1], post.cols - c0)
                prob[r0:r0 + h, c0:c0 + w] = small_p[:h, :w]
                rgb[r0:r0 + h, c0:c0 + w] = small_rgb[:h, :w].astype(np.uint8)
                valid_mask[r0:r0 + h, c0:c0 + w] = small_valid[:h, :w]
                self._report((index + 1) / float(total), '{0} ({1}/{2})'.format(
                    self._labels[0], index + 1, total))
        finally:
            if image is not None:
                image.close()
        return prob, rgb, {'scale': scale, 'valid': valid_pixels,
                           'valid_mask': valid_mask,
                           'placeholder_blocks': placeholder_blocks,
                           'blocks': total}

    @staticmethod
    def _area_in(spec, crs):
        geometry = QgsGeometry(spec.area)
        if spec.area_crs != crs:
            geometry.transform(QgsCoordinateTransform(
                spec.area_crs, crs, QgsCoordinateTransformContext()))
        return geometry

    @staticmethod
    def _clip_geometry(spec, area_post):
        return area_post if spec.clip else None

    def _centre_records(self, spec, graph, post, prob, distance, rgb, clip):
        networks = SK.network_ids(graph)
        records = []
        alive = graph.alive()
        for count, edge in enumerate(alive):
            pixels = edge.pixels
            geometry = V.centreline(post, pixels, spec.simplify_m,
                                    spec.smooth)
            if clip is not None:
                geometry = geometry.intersection(clip)
                if geometry is None or geometry.isEmpty():
                    continue
            length = geometry.length()
            if length <= 0:
                continue
            on_road = V.sample(distance, pixels)
            body = on_road[on_road > 0]
            width = 2.0 * float(np.median(body)) * post.gsd if len(body) \
                else 0.0
            conf = float(np.mean(V.sample(prob, pixels)))
            kind = 'unknown' if edge.bridge else V.surface_kind(
                V.sample(rgb, pixels))
            records.append({
                'geometry': geometry, 'length': length,
                'width': width if not edge.bridge else 0.0,
                'conf': conf, 'surface': kind, 'bridge': edge.bridge,
                'network': networks.get(edge.ident, 0)})
            if count % 500 == 0:
                self._check()
                self._report(0.5 + 0.5 * count / max(1, len(alive)))
        records.sort(key=lambda r: (r['network'], -r['length']))
        for ident, record in enumerate(records, 1):
            record['ident'] = ident
        return records

    def _surfaces(self, spec, mask, post, prob, area_post):
        polygons = V.polygonize(mask, post)
        cut = None
        if spec.clip:
            boundary = QgsGeometry(area_post.convertToType(
                Qgis.GeometryType.Line, True))
            cut = boundary.buffer(post.gsd * 1.5, 4)
        else:
            outline = QgsGeometry.fromRect(_grid_rect(post))
            cut = QgsGeometry(outline.convertToType(
                Qgis.GeometryType.Line, True)).buffer(post.gsd * 1.5, 4)
        surfaces, edges = [], []
        for count, polygon in enumerate(polygons):
            if count % 200 == 0:
                self._check()
            polygon = V.tidy_polygon(polygon, spec.simplify_m, spec.smooth)
            if spec.clip:
                polygon = polygon.intersection(area_post)
            if polygon is None or polygon.isEmpty():
                continue
            box = polygon.boundingBox()
            r0 = max(0, int((post.north - box.yMaximum()) / post.gsd))
            r1 = min(post.rows, int(math.ceil((post.north - box.yMinimum())
                                              / post.gsd)) + 1)
            c0 = max(0, int((box.xMinimum() - post.west) / post.gsd))
            c1 = min(post.cols, int(math.ceil((box.xMaximum() - post.west)
                                              / post.gsd)) + 1)
            window = mask[r0:r1, c0:c1]
            conf = float(prob[r0:r1, c0:c1][window].mean()) if window.any() \
                else 0.0
            ident = len(surfaces) + 1
            surfaces.append({'ident': ident, 'geometry': polygon,
                             'area': polygon.area(), 'conf': conf})
            if spec.want_edge:
                lines = V.edge_lines(polygon, cut)
                if lines is not None:
                    parts = lines.asGeometryCollection() \
                        if lines.isMultipart() else [lines]
                    for part in parts:
                        if part.length() >= post.gsd * 4:
                            edges.append({'ident': len(edges) + 1,
                                          'surface': ident, 'geometry': part,
                                          'length': part.length()})
        return surfaces, edges

    def _compare(self, spec, centres, crs, area):
        if spec.compare_osm:
            from .. import net
            self._report(0.0, 'Fetching roads from OpenStreetMap')
            try:
                spec.reference = fetch_osm_roads(spec.bounds, net.get,
                                                 self.should_stop)
            except CHECK.ReferenceError as exc:
                spec.reference_problem = str(exc)
                return None
        if spec.reference is None:
            return None
        context = QgsCoordinateTransformContext()
        to_metric = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem('EPSG:4326'), crs, context)
        extracted = []
        for record in centres:
            for part in _line_parts(record['geometry']):
                extracted.append((record['ident'], '{0:.2f}'.format(
                    record['conf']), record['width'], 0, np.asarray(part)))
        lines = []
        for line in spec.reference.lines:
            geometry = QgsGeometry.fromPolylineXY(
                [QgsPointXY(a, b) for a, b in line])
            geometry.transform(to_metric)
            # Only the reference inside the area counts; the request asked for
            # its bounding box.
            geometry = geometry.intersection(area)
            if geometry is None or geometry.isEmpty():
                continue
            for part in _line_parts(_multi(geometry)):
                lines.append(np.asarray(part))
        found = CHECK.compare(extracted, lines, spec.tolerance,
                              step=max(2.0, spec.tolerance / 3.0),
                              stop=self.should_stop)
        if found is None:
            raise Stopped()
        return found

    # --------------------------------------------------------------- writing

    def _write(self, spec, crs, centres, edges, surfaces, comparison):
        from osgeo import ogr, osr

        _discard(spec.gpkg_path)
        out_crs = spec.out_crs if spec.out_crs is not None and \
            spec.out_crs.isValid() else crs
        transform = None
        if out_crs != crs:
            transform = QgsCoordinateTransform(crs, out_crs,
                                               QgsCoordinateTransformContext())
        srs = osr.SpatialReference()
        srs.ImportFromWkt(out_crs.toWkt())
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target = ogr.GetDriverByName('GPKG').CreateDataSource(spec.gpkg_path)
        if target is None:
            raise RoadError('The road file could not be created at {0}. Is '
                            'that folder writable?'.format(spec.gpkg_path))

        def geom(geometry):
            geometry = QgsGeometry(geometry)
            if transform is not None:
                geometry.transform(transform)
            return ogr.CreateGeometryFromWkb(bytes(geometry.asWkb()))

        try:
            if spec.want_centre:
                _layer(ogr, target, LAYER_CENTRE, srs, ogr.wkbMultiLineString,
                       (('ROAD_ID', ogr.OFTInteger), ('NETWORK', ogr.OFTInteger),
                        ('LENGTH_M', ogr.OFTReal), ('WIDTH_M', ogr.OFTReal),
                        ('CONF', ogr.OFTReal), ('SURFACE', ogr.OFTString),
                        ('GAP_JOIN', ogr.OFTInteger)),
                       [({'ROAD_ID': c['ident'], 'NETWORK': c['network'],
                          'LENGTH_M': round(c['length'], 1),
                          'WIDTH_M': round(c['width'], 1),
                          'CONF': round(c['conf'], 3),
                          'SURFACE': c['surface'],
                          'GAP_JOIN': 1 if c['bridge'] else 0},
                         geom(_multi(c['geometry']))) for c in centres])
            if spec.want_edge:
                _layer(ogr, target, LAYER_EDGE, srs, ogr.wkbMultiLineString,
                       (('EDGE_ID', ogr.OFTInteger),
                        ('SURF_ID', ogr.OFTInteger),
                        ('LENGTH_M', ogr.OFTReal)),
                       [({'EDGE_ID': e['ident'], 'SURF_ID': e['surface'],
                          'LENGTH_M': round(e['length'], 1)},
                         geom(_multi(e['geometry']))) for e in edges])
            if spec.want_surface:
                _layer(ogr, target, LAYER_SURFACE, srs, ogr.wkbMultiPolygon,
                       (('SURF_ID', ogr.OFTInteger), ('AREA_M2', ogr.OFTReal),
                        ('CONF', ogr.OFTReal)),
                       [({'SURF_ID': s['ident'],
                          'AREA_M2': round(s['area'], 1),
                          'CONF': round(s['conf'], 3)},
                         geom(_multi(s['geometry']))) for s in surfaces])
            if comparison is not None and (comparison.extra
                                           or comparison.missed):
                rows = []
                for ident, _conf, _w, run, points in comparison.extra:
                    rows.append(({'KIND': KIND_EXTRA, 'ROAD_ID': ident,
                                  'LENGTH_M': round(run, 1)},
                                 geom(_multi(_line_from(points)))))
                for run, points in comparison.missed:
                    rows.append(({'KIND': KIND_MISSED, 'ROAD_ID': None,
                                  'LENGTH_M': round(run, 1)},
                                 geom(_multi(_line_from(points)))))
                _layer(ogr, target, LAYER_CHECK, srs, ogr.wkbMultiLineString,
                       (('KIND', ogr.OFTString), ('ROAD_ID', ogr.OFTInteger),
                        ('LENGTH_M', ogr.OFTReal)),
                       rows)
        finally:
            target = None


def start(task):
    QgsApplication.taskManager().addTask(task)
    return task


# ------------------------------------------------------------------ helpers

def _grid_rect(grid):
    from qgis.core import QgsRectangle

    return QgsRectangle(grid.west, grid.north - grid.rows * grid.gsd,
                        grid.west + grid.cols * grid.gsd, grid.north)


def _line_parts(geometry):
    if geometry.type() != Qgis.GeometryType.Line:
        return []
    if geometry.isMultipart():
        return [[(p.x(), p.y()) for p in part]
                for part in geometry.asMultiPolyline() if len(part) >= 2]
    line = geometry.asPolyline()
    return [[(p.x(), p.y()) for p in line]] if len(line) >= 2 else []


def _line_from(points):
    return QgsGeometry.fromPolylineXY(
        [QgsPointXY(a, b) for a, b in np.asarray(points).tolist()])


def _multi(geometry):
    """`geometry` as a multi-part of its own kind.

    An intersection can hand back a collection with stray points or lines in
    it; only the parts of the main kind are kept.
    """
    from qgis.core import QgsWkbTypes

    geometry = QgsGeometry(geometry)
    if QgsWkbTypes.flatType(geometry.wkbType()) == \
            Qgis.WkbType.GeometryCollection:
        parts = geometry.asGeometryCollection()
        for wanted in (Qgis.GeometryType.Polygon, Qgis.GeometryType.Line):
            keep = [g for g in parts if g.type() == wanted]
            if keep:
                geometry = QgsGeometry.collectGeometry(keep)
                break
    geometry.convertToMultiType()
    return geometry


def _layer(ogr, target, name, srs, kind, fields, rows):
    layer = target.CreateLayer(name, srs, kind)
    for field, field_type in fields:
        layer.CreateField(ogr.FieldDefn(field, field_type))
    layer.StartTransaction()
    try:
        for values, geometry in rows:
            if geometry is None:
                continue
            feature = ogr.Feature(layer.GetLayerDefn())
            for key, value in values.items():
                if value is not None:
                    feature.SetField(key, value)
            feature.SetGeometry(geometry)
            layer.CreateFeature(feature)
    except BaseException:
        layer.RollbackTransaction()
        raise
    layer.CommitTransaction()


class _ImageWriter(object):
    """The imagery the model saw, as a JPEG-compressed GeoTIFF with a mask."""

    def __init__(self, path, grid):
        from osgeo import gdal, osr

        _remove(path)
        driver = gdal.GetDriverByName('GTiff')
        self.dataset = driver.Create(
            path, grid.cols, grid.rows, 3, gdal.GDT_Byte,
            ['COMPRESS=JPEG', 'JPEG_QUALITY=90', 'PHOTOMETRIC=YCBCR',
             'TILED=YES', 'BIGTIFF=IF_SAFER'])
        if self.dataset is None:
            raise RoadError('The imagery file could not be created at '
                            '{0}.'.format(path))
        self.dataset.SetGeoTransform(grid.geotransform())
        srs = osr.SpatialReference()
        srs.ImportFromWkt(grid.crs.toWkt())
        self.dataset.SetProjection(srs.ExportToWkt())
        self.dataset.CreateMaskBand(gdal.GMF_PER_DATASET)
        self.mask = self.dataset.GetRasterBand(1).GetMaskBand()

    def write(self, row, col, rgb, valid):
        for band in range(3):
            self.dataset.GetRasterBand(band + 1).WriteArray(
                rgb[:, :, band], col, row)
        self.mask.WriteArray(valid.astype(np.uint8) * 255, col, row)

    def close(self):
        if self.dataset is not None:
            self.dataset.FlushCache()
            self.dataset = None


def _write_probability(path, grid, prob, valid):
    """Road probability 0-100 as a byte GeoTIFF (255 = no imagery)."""
    from osgeo import gdal, osr

    _remove(path)
    driver = gdal.GetDriverByName('GTiff')
    target = driver.Create(path, grid.cols, grid.rows, 1, gdal.GDT_Byte,
                           ['COMPRESS=DEFLATE', 'TILED=YES',
                            'BIGTIFF=IF_SAFER'])
    if target is None:
        raise RoadError('The probability file could not be created.')
    try:
        target.SetGeoTransform(grid.geotransform())
        srs = osr.SpatialReference()
        srs.ImportFromWkt(grid.crs.toWkt())
        target.SetProjection(srs.ExportToWkt())
        band = target.GetRasterBand(1)
        band.SetNoDataValue(255)
        values = np.round(prob * 100.0).astype(np.uint8)
        values[~valid] = 255
        band.WriteArray(values)
        band.FlushCache()
        band = None
    finally:
        target = None


def _discard(path):
    if not path:
        return
    for suffix in ('', '-wal', '-shm', '-journal'):
        _remove(path + suffix)


def _remove(path):
    with suppress(OSError):
        if path and os.path.exists(path):
            os.remove(path)


# -------------------------------------------------------------------- report

def build_report(spec, grid, post, stats, centres, surfaces, edges, bridges,
                 comparison, length_km):
    report = CHECK.Report('Roads - {0}'.format(spec.title))
    work_area = QgsGeometry(spec.area)
    if spec.area_crs != post.crs:
        work_area.transform(QgsCoordinateTransform(
            spec.area_crs, post.crs, QgsCoordinateTransformContext()))
    area_km2 = work_area.area() / 1e6 if spec.clip else \
        work_area.boundingBox().area() / 1e6
    by_kind = {}
    for record in centres:
        by_kind[record['surface']] = by_kind.get(record['surface'], 0.0) + \
            record['length']
    widths = [r['width'] for r in centres if r['width'] > 0]
    rows = [('Imagery', spec.layer_name),
            ('Area', '{0} ({1:,.2f} km²)'.format(
                spec.area_label, area_km2)),
            ('Model resolution', '{0:.2f} m per pixel; post-processing at '
             '{1:.2f} m'.format(grid.gsd, post.gsd)),
            ('Sensitivity', 'probability ≥ {0:.2f}'.format(
                spec.threshold)),
            ('Generalization', '{0}{1}; dead ends under {2:.0f} m trimmed'
             .format('simplified to {0:.1f} m'.format(spec.simplify_m)
                     if spec.simplify_m > 0 else 'not simplified',
                     ', smoothed' if spec.smooth else '', spec.spur_m)),
            ('Centerlines', '{0:,} segments, {1:,.2f} km'.format(
                len(centres), length_km)),
            ('Road density', '{0:,.1f} km per km²'.format(
                length_km / area_km2 if area_km2 > 0 else 0.0)),
            ('Gaps joined', '{0:,} (GAP_JOIN = 1)'.format(bridges)),
            ('Surface guess', ', '.join('{0} {1:,.2f} km'.format(
                kind, value / 1000.0) for kind, value in sorted(
                    by_kind.items())) or '-'),
            ('Median width', '{0:.1f} m'.format(float(np.median(widths)))
             if widths else '-'),
            ('Surface polygons', '{0:,}'.format(len(surfaces))),
            ('Edge lines', '{0:,}'.format(len(edges)))]
    report.add('Result', rows)

    notes = []
    if stats['scale'] > 1:
        notes.append(
            'The tile service has no imagery at the model\'s resolution here, '
            'so it was read {0} zoom level{1} coarser (about {2:.1f} m) and '
            'enlarged. Narrow roads are harder to see at that resolution.'
            .format(int(math.log(stats['scale'], 2)),
                    '' if stats['scale'] == 2 else 's',
                    grid.gsd * stats['scale']))
    if stats['placeholder_blocks']:
        notes.append(
            '{0} of {1} blocks showed a "no imagery" placeholder at every '
            'zoom tried and were skipped.'.format(stats['placeholder_blocks'],
                                                  stats['blocks']))
    notes += [
        'WIDTH_M is twice the distance from the centerline to the mask\'s '
        'edge. The model was trained on centerlines buffered to 6 m, so it '
        'tends to draw roads about that wide: treat widths under 4 m or over '
        '10 m, and the edge lines, as approximate.',
        'SURFACE is a guess from colour: grey reads as paved, red-brown or tan '
        'as unpaved (laterite, earth). Shade and dust fool it.',
        'CONF is the model\'s mean probability along the line (0-1). Low '
        'values and GAP_JOIN = 1 are the first places to check.',
        'Roads under trees, in deep shadow, or narrower than about 3 m are '
        'often missed; bare plots, field bunds, parking lots and flat roofs '
        'are sometimes taken for roads. Treat the result as a draft to edit.']
    report.add('Reading the result', paragraphs=notes)

    if comparison is not None:
        rows = [('Reference', spec.reference.label),
                ('Match distance', '{0:.0f} m'.format(spec.tolerance)),
                ('Detected roads found in the reference', '{0:.0f}%'.format(
                    100.0 * comparison.precision)),
                ('Reference roads detected', '{0:.0f}%'.format(
                    100.0 * comparison.recall)),
                ('Agreement', CHECK.agreement_words(comparison.precision,
                                                    comparison.recall))]
        report.add('Comparison', rows, [
            'Stretches only one network has are on the road_check layer: '
            '"extra" were detected but are not in the reference, "missed" are '
            'in the reference but were not detected. OpenStreetMap is often '
            'incomplete for small lanes, so some "extra" roads are real.'])
    elif spec.reference_problem:
        report.add('Comparison', paragraphs=[
            'The comparison could not be made: {0}'.format(
                spec.reference_problem)])

    report.add('Credits', paragraphs=[
        runtime.MODEL['credit'],
        'Imagery: {0}. The detected roads are derived from it; check the '
        'imagery provider\'s terms before you publish or share them.'.format(
            spec.layer_name)])
    return report

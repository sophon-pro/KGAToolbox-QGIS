# -*- coding: utf-8 -*-
"""Detect Paddy Field Plots from Imagery: the background job.

One ``PaddyTask`` per run, on a QgsTask thread. Its steps, on one progress
bar:

1. read the imagery block by block on the area's UTM grid and run the field
   model over overlapping 512 x 512 chips (the bulk of the time);
2. put the fields together into one plot map: every pixel goes to the most
   confident field that claims it, and bunds narrower than the "close gaps"
   distance are shared out between the plots either side;
3. optionally keep only plots on farmland (ESA WorldCover, read from the
   internet);
4. turn the plot map into polygons and boundary lines;
5. write ``<name>_paddy.gpkg`` (and the imagery GeoTIFF).

**How chips are stitched.** Chips are 512 px and placed every 256 px, so each
pixel is in the middle half (the *core*) of exactly one chip. A field is
taken from the chip whose core holds its centre: every field narrower than
256 px is then seen whole, with context on all sides. Where fields from two
chips overlap, the pixel goes to the more confident one.

Everything the GUI resolves (the layer clone, the area, the paths) arrives in
a ``PaddySpec``, so the worker touches no project object.
"""

import math
import os
import tempfile
import time
import traceback
from contextlib import suppress

import numpy as np
from qgis.core import (Qgis, QgsApplication, QgsCoordinateTransform,
                       QgsCoordinateTransformContext,
                       QgsGeometry, QgsMessageLog, QgsProcessingUtils,
                       QgsRectangle, QgsTask)
from qgis.PyQt.QtCore import pyqtSignal

from .. import stream_check as CHECK
from ..roads import runtime as RT
from ..roads import sampler
from ..roads import vectorize as V
from . import detect, models

LOG_TAG = 'KGA Toolbox'

LAYER_PLOTS = 'paddy_plots'
LAYER_BOUNDS = 'plot_boundaries'

#: Chip side, and the step between chips (half a chip).
CHIP = 512
STEP = CHIP // 2
#: Context rendered around every block, so its outer chips are whole.
PAD = (CHIP - STEP) // 2
#: Block of chip cores rendered at once, in pixels (a multiple of STEP).
BLOCK = 8 * STEP

#: Context read around the area so plots at its edge are seen whole, metres.
MARGIN_M = 30.0

#: Pixels at the working resolution: warn, warn hard, refuse.
WARN_PIXELS = 30 * 1000 * 1000
HUGE_PIXELS = 100 * 1000 * 1000
MAX_PIXELS = 250 * 1000 * 1000

#: Working resolution presets, metres per pixel.
RESOLUTIONS = (
    (0.5, '0.5 m - small plots (under about 0.1 ha)'),
    (1.0, '1 m - most paddy plots'),
    (2.0, '2 m - large fields, big areas'),
)
DEFAULT_RESOLUTION = 1.0

#: Confidence presets: the score a field needs to be kept.
SENSITIVITY = (
    ('strict', 'Strict - only clear plots', 0.35),
    ('normal', 'Normal', 0.2),
    ('sensitive', 'Sensitive - more plots, more false ones', 0.1),
)
DEFAULT_SENSITIVITY = 'normal'

#: Simplify presets: Douglas-Peucker tolerance in metres (None = custom).
SIMPLIFY = (
    ('off', 'Off - keep every pixel step', 0.0),
    ('light', 'Light (0.5 m) - follows every bend', 0.5),
    ('medium', 'Medium (1 m) - straight bunds, kept corners', 1.0),
    ('strong', 'Strong (2 m) - fewest vertices', 2.0),
    ('custom', 'Custom', None),
)
DEFAULT_SIMPLIFY = 'medium'

#: A "giant" is a plot at least this many times the run's median plot ...
GIANT_RATIO = 5.0
#: ... that the model was unsure of, that more confident plots mostly
#: overlapped, or that ran off the chip (the model saw only part of it) with a
#: middling score: the model boxing a whole block of plots (or a pond and the
#: plots around it) as one. Measured on Takeo and Battambang (v2, 1 m): the
#: bogus ones scored 0.25-0.39, kept under 70% of their pixels and all ran
#: off the chip; the real large fields scored 0.8 and fitted in it.
GIANT_CONF = 0.3
GIANT_KEPT = 0.6
GIANT_CUT_CONF = 0.5
#: Plots needed before a median means anything.
GIANT_MIN_PLOTS = 10

#: ESA WorldCover classes a paddy can be in. Cropland is the main one; a
#: flooded paddy is sometimes herbaceous wetland and a fallow one grassland.
FARM_CLASSES = (40, 90, 30)
WORLDCOVER = ('https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/'
              'map/ESA_WorldCover_10m_2021_v200_{0}_Map.tif')
WORLDCOVER_CREDIT = ('Farmland filter: ESA WorldCover 2021 v200 (c) ESA '
                     'WorldCover project / Contains modified Copernicus '
                     'Sentinel data (2021) processed by ESA WorldCover '
                     'consortium, CC BY 4.0.')


class PaddyError(Exception):
    """A run that cannot go on. Shown to the user as-is."""


class Stopped(Exception):
    pass


def preset(table, key, default):
    for ident, _label, value in table:
        if ident == key:
            return value
    return default


def temp_folder():
    base = os.path.join(QgsProcessingUtils.tempFolder(), 'kga_paddy')
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix='run_', dir=base)


def size_warning(pixels, seconds_per_mpx):
    """(severe, text) for a large job, or None."""
    if pixels < WARN_PIXELS:
        return None
    minutes = int(round(pixels / 1e6 * seconds_per_mpx / 60.0))
    took = 'about a minute' if minutes <= 1 else \
        'about {0} minutes'.format(minutes)
    text = ('That area is {0:,} million pixels at the chosen resolution. '
            'Expect {1} or more, and {2:.1f} GB of memory. A coarser '
            'resolution or a smaller area is quicker.').format(
                pixels // 1000000, took, pixels * 16 / 1e9 + 0.3)
    return pixels >= HUGE_PIXELS, text


def worldcover_tiles(bounds):
    """WorldCover 3-degree tile names covering (west, south, east, north)."""
    west, south, east, north = bounds
    names = []
    lat = int(math.floor(south / 3.0)) * 3
    while lat < north:
        lon = int(math.floor(west / 3.0)) * 3
        while lon < east:
            names.append('{0}{1:02d}{2}{3:03d}'.format(
                'N' if lat >= 0 else 'S', abs(lat),
                'E' if lon >= 0 else 'W', abs(lon)))
            lon += 3
        lat += 3
    return names


# ------------------------------------------------------------------- the job

class PaddySpec(object):
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
        self.model = models.model(models.DEFAULT_MODEL)
        self.model_path = ''
        self.gsd = DEFAULT_RESOLUTION
        self.confidence = 0.2
        self.min_area_m2 = 100.0
        self.hole_m2 = 50.0
        #: Bunds and gaps narrower than this are shared between the plots
        #: either side, so neighbours meet; 0 keeps the model's own outlines.
        self.close_m = 2.0
        self.farmland_only = True
        self.drop_giants = True
        self.smooth = True
        self.simplify_m = 1.0
        self.want_plots = True
        self.want_bounds = True
        self.gpkg_path = ''
        self.image_path = None
        self.report_path = None
        self.title = 'Paddy plots'
        self.__dict__.update(values)

    def steps(self):
        steps = [('Reading the imagery and finding fields', 0.86),
                 ('Putting the plots together', 0.04)]
        if self.farmland_only:
            steps.append(('Checking the plots are on farmland', 0.03))
        steps += [('Drawing the plot outlines', 0.05),
                  ('Writing the results', 0.02)]
        total = sum(weight for _l, weight in steps)
        return [(label, weight / total) for label, weight in steps]


class PaddyTask(QgsTask):
    """Read, detect, assemble, write. One job at a time, on a worker."""

    phase = pyqtSignal(str, float)
    jobFinished = pyqtSignal(dict)

    def __init__(self, spec):
        flag = getattr(QgsTask, 'Flag', QgsTask)     # scoped enum, QGIS 3.36+
        super().__init__('Paddy plots: {0}'.format(spec.title),
                         flag.CanCancel)
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
                  'plots': 0, 'hectares': 0.0, 'bounds_km': 0.0,
                  'image_path': None, 'scale': 1, 'farmland_problem': ''}
        started = time.time()
        try:
            self._run(spec, result)
            result['seconds'] = time.time() - started
        except Stopped:
            result['state'] = 'stopped'
        except (PaddyError, RT.RuntimeProblem, sampler.SamplerError) as exc:
            result.update(state='error', message=str(exc))
        except MemoryError:
            result.update(state='error', message=(
                'The PC ran out of memory. Use a smaller area or a coarser '
                'resolution.'))
        except Exception as exc:                # never let a task die silently
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            result.update(state='error', message='The plots could not be '
                                                 'detected: {0}'.format(exc))
        if result['state'] != 'done':
            _discard(spec.gpkg_path)
            if spec.image_path:
                _remove(spec.image_path)
        self.result = result
        return result['state'] == 'done'

    def finished(self, success):
        self.jobFinished.emit(self.result)

    def _run(self, spec, result):
        timing = result.setdefault('timing', {})
        t0 = time.time()
        ort = RT.load_onnxruntime()
        models.check_file(spec.model_path, spec.model)
        session = RT.open_session(ort, spec.model_path)
        detector = detect.FieldDetector(session, CHIP, spec.confidence)

        grid = sampler.Grid.around(spec.area, spec.area_crs, spec.work_crs,
                                   spec.gsd, MARGIN_M)
        if grid.pixels > MAX_PIXELS:
            raise PaddyError(
                'That area is {0:,} million pixels at {1:g} m, more than this '
                'tool reads in one go. Choose a smaller area or a coarser '
                'resolution.'.format(grid.pixels // 1000000, spec.gsd))

        self._begin('Reading the imagery and finding fields')
        labels, scores, stats = self._detect(spec, grid, detector)
        result['scale'] = stats['scale']
        timing['detect'] = time.time() - t0
        if stats['valid'] == 0:
            raise PaddyError(
                'The imagery layer drew nothing over this area. Check that it '
                'covers the area and shows on the map here.')
        self._check()

        self._begin('Putting the plots together')
        area_grid = self._area_in(spec, grid.crs)
        inside = V.roi_mask(grid, area_grid) if spec.clip else \
            np.ones((grid.rows, grid.cols), dtype=bool)
        usable = inside & stats['valid_mask']
        labels[~usable] = 0
        labels = self._close_gaps(labels, usable, spec.close_m / grid.gsd)
        self._report(1.0)
        if not labels.any():
            raise PaddyError(
                'No field was found. Try a more sensitive setting, or check '
                'the imagery shows fields clearly at this place.')
        self._check()

        farmland = None
        if spec.farmland_only:
            self._begin('Checking the plots are on farmland')
            try:
                farmland = self._farmland_share(spec, grid, labels)
            except PaddyError as exc:
                result['farmland_problem'] = str(exc)
            self._report(1.0)
        timing['assemble'] = time.time() - t0 - timing['detect']

        self._begin('Drawing the plot outlines')
        plots, dropped = self._plots(spec, grid, labels, stats['instances'],
                                     farmland, area_grid)
        bounds = self._boundaries(spec, grid, plots, area_grid) \
            if spec.want_bounds else []
        self._report(1.0)
        if not plots:
            raise PaddyError(
                'Fields were found, but none was left after the filters '
                '({0}). Lower "Drop plots smaller than", or untick "Keep only '
                'plots on farmland".'.format(_dropped_words(dropped)))

        self._begin('Writing the results')
        self._write(spec, grid.crs, plots, bounds)
        if spec.image_path and os.path.exists(spec.image_path):
            result['image_path'] = spec.image_path
        hectares = sum(p['area'] for p in plots) / 1e4
        bounds_km = sum(b['length'] for b in bounds) / 1000.0
        report = build_report(spec, grid, stats, plots, bounds, dropped,
                              farmland is not None,
                              result['farmland_problem'])
        result['report_html'] = report.html()
        result['report_text'] = report.text()
        if spec.report_path:
            with suppress(OSError):
                with open(spec.report_path, 'w', encoding='utf-8') as handle:
                    handle.write(result['report_text'])
                result['report_path'] = spec.report_path
        result.update(plots=len(plots), hectares=hectares,
                      bounds_km=bounds_km, dropped=dropped)
        timing['total'] = time.time() - t0
        self._report(1.0)
        result['state'] = 'done'

    # ----------------------------------------------------------- the stages

    def _detect(self, spec, grid, detector):
        """Every block: render it, run every chip, keep fields by centre."""
        labels = np.zeros((grid.rows, grid.cols), dtype=np.int32)
        scores = np.zeros((grid.rows, grid.cols), dtype=np.float32)
        valid_mask = np.zeros((grid.rows, grid.cols), dtype=bool)
        image = _ImageWriter(spec.image_path, grid) if spec.image_path \
            else None
        instances = [None]                  # index = label; 0 is no field
        total = grid.block_count(BLOCK)
        scale = 1
        placeholder_blocks = 0
        valid_pixels = 0
        chips = 0
        try:
            for index, (row, col, rows, cols) in enumerate(
                    grid.blocks(BLOCK)):
                self._check()
                block, valid = sampler.render(spec.layer, grid, row, col,
                                              rows, cols, PAD, scale)
                while not spec.local and scale < 8 and \
                        sampler.looks_like_placeholder(block, valid):
                    scale *= 2
                    block, valid = sampler.render(spec.layer, grid, row, col,
                                                  rows, cols, PAD, scale)
                if sampler.looks_like_placeholder(block, valid):
                    placeholder_blocks += 1
                    valid[:] = False
                core = (slice(PAD, PAD + rows), slice(PAD, PAD + cols))
                valid_pixels += int(valid[core].sum())
                valid_mask[row:row + rows, col:col + cols] = valid[core]
                if image is not None:
                    image.write(row, col, block[core], valid[core])
                # Chip cores tile the block's core; chip k starts PAD before
                # its core, which is k * STEP into the padded block.
                starts_r = range(0, rows, STEP)
                starts_c = range(0, cols, STEP)
                count = len(starts_r) * len(starts_c)
                done = 0
                for top in starts_r:
                    for left in starts_c:
                        self._check()
                        chips += self._chip(detector, block, valid, top,
                                            left, row, col, grid, labels,
                                            scores, instances)
                        done += 1
                        self._report((index + done / float(count))
                                     / float(total),
                                     '{0} ({1}/{2})'.format(
                                         self._labels[0], index + 1, total))
        finally:
            if image is not None:
                image.close()
        return labels, scores, {'scale': scale, 'valid': valid_pixels,
                                'valid_mask': valid_mask,
                                'placeholder_blocks': placeholder_blocks,
                                'blocks': total, 'chips': chips,
                                'instances': instances}

    def _chip(self, detector, block, valid, top, left, row, col, grid,
              labels, scores, instances):
        """One chip: detect, keep the fields centred in its core, paint them.

        Returns 1 when the chip was run, 0 when it had no imagery.
        """
        rgb = block[top:top + CHIP, left:left + CHIP]
        ok = valid[top:top + CHIP, left:left + CHIP]
        if not ok.any():
            return 0
        if rgb.shape[0] < CHIP or rgb.shape[1] < CHIP:
            pad_r, pad_c = CHIP - rgb.shape[0], CHIP - rgb.shape[1]
            rgb = np.pad(rgb, ((0, pad_r), (0, pad_c), (0, 0)))
            ok = np.pad(ok, ((0, pad_r), (0, pad_c)))
        # Grid position of the chip's top-left pixel.
        origin_r = row + top - PAD
        origin_c = col + left - PAD
        for field in detector.run(rgb, ok):
            centre_r, centre_c = field.centre()
            if not (PAD <= centre_r < PAD + STEP and
                    PAD <= centre_c < PAD + STEP):
                continue
            height, width = field.mask.shape
            r0 = origin_r + field.top
            c0 = origin_c + field.left
            # Clip the field's window to the grid.
            a0, b0 = max(0, -r0), max(0, -c0)
            a1 = min(height, grid.rows - r0)
            b1 = min(width, grid.cols - c0)
            if a1 <= a0 or b1 <= b0:
                continue
            mask = field.mask[a0:a1, b0:b1]
            window = (slice(r0 + a0, r0 + a1), slice(c0 + b0, c0 + b1))
            better = mask & (scores[window] < field.score)
            if not better.any():
                continue
            label = len(instances)
            cut = (field.top == 0 or field.left == 0
                   or field.top + height >= CHIP
                   or field.left + width >= CHIP)
            instances.append({'score': field.score, 'cut': cut,
                              'claimed': int(mask.sum())})
            labels[window][better] = label
            scores[window][better] = field.score
        return 1

    @staticmethod
    def _close_gaps(labels, usable, distance_px):
        """Share narrow gaps (bunds, ditches) out to the nearest plot.

        A background pixel within `distance_px` of a plot joins the nearest
        one, so two plots either side of a bund meet in its middle.
        """
        if distance_px < 0.5:
            return labels
        from scipy import ndimage

        background = labels == 0
        if not background.any():
            return labels
        distance, (near_r, near_c) = ndimage.distance_transform_edt(
            background, return_indices=True)
        grow = background & usable & (distance <= distance_px)
        labels = labels.copy()
        labels[grow] = labels[near_r[grow], near_c[grow]]
        return labels

    def _farmland_share(self, spec, grid, labels):
        """Share of each plot on WorldCover farmland, by label (float array).

        Raises PaddyError when WorldCover cannot be read.
        """
        from osgeo import gdal

        sources = []
        for name in worldcover_tiles(spec.bounds):
            self._check()
            path = '/vsicurl/' + WORLDCOVER.format(name)
            with suppress(RuntimeError):
                if gdal.Open(path) is not None:
                    sources.append(path)
        if not sources:
            raise PaddyError('ESA WorldCover could not be read (no internet, '
                             'or no tile here), so every plot was kept.')
        srs = grid.crs.toWkt()
        rect = _grid_rect(grid)
        try:
            warped = gdal.Warp(
                '', sources, format='MEM', dstSRS=srs,
                outputBounds=(rect.xMinimum(), rect.yMinimum(),
                              rect.xMaximum(), rect.yMaximum()),
                width=grid.cols, height=grid.rows, resampleAlg='near',
                outputType=gdal.GDT_Byte)
        except RuntimeError as exc:
            raise PaddyError('ESA WorldCover could not be read ({0}), so '
                             'every plot was kept.'.format(exc))
        if warped is None:
            raise PaddyError('ESA WorldCover could not be read, so every plot '
                             'was kept.')
        classes = warped.GetRasterBand(1).ReadAsArray()
        warped = None
        farm = np.isin(classes, FARM_CLASSES)
        known = classes > 0
        count = int(labels.max()) + 1
        total = np.bincount(labels[known].ravel(), minlength=count)
        on_farm = np.bincount(labels[known & farm].ravel(), minlength=count)
        share = np.where(total > 0, on_farm / np.maximum(total, 1), 1.0)
        return share

    @staticmethod
    def _area_in(spec, crs):
        geometry = QgsGeometry(spec.area)
        if spec.area_crs != crs:
            geometry.transform(QgsCoordinateTransform(
                spec.area_crs, crs, QgsCoordinateTransformContext()))
        return geometry

    def _plots(self, spec, grid, labels, instances, farmland, area_grid):
        """Plot polygons from the plot map, filtered and generalized."""
        pieces = _polygonize_labels(labels, grid)
        dropped = {'small': 0, 'farmland': 0, 'giant': 0}
        giant_m2 = self._giant_area(spec, pieces)
        final = np.bincount(labels.ravel(), minlength=len(instances))
        boundary = None
        if spec.clip:
            boundary = QgsGeometry(area_grid.convertToType(
                Qgis.GeometryType.Line, True)).buffer(grid.gsd * 1.5, 4)
        else:
            boundary = QgsGeometry(QgsGeometry.fromRect(
                _grid_rect(grid)).convertToType(
                    Qgis.GeometryType.Line, True)).buffer(grid.gsd * 1.5, 4)
        plots = []
        items = sorted(pieces.items())
        for count, (label, polygon) in enumerate(items):
            if count % 200 == 0:
                self._check()
                self._report(count / float(max(1, len(items))))
            if farmland is not None and label < len(farmland) and \
                    farmland[label] < 0.5:
                dropped['farmland'] += 1
                continue
            info = instances[label] if label < len(instances) else None
            raw = polygon
            if spec.hole_m2 > 0:
                raw = raw.removeInteriorRings(spec.hole_m2)
            if raw.area() < spec.min_area_m2:
                dropped['small'] += 1
                continue
            if giant_m2 and info and raw.area() > giant_m2 and (
                    info['score'] < GIANT_CONF
                    or final[label] < GIANT_KEPT * max(1, info['claimed'])
                    or info['cut'] and info['score'] < GIANT_CUT_CONF):
                dropped['giant'] += 1
                continue
            shape = V.tidy_polygon(raw, spec.simplify_m, spec.smooth)
            if spec.clip:
                shape = shape.intersection(area_grid)
            if shape is None or shape.isEmpty():
                continue
            area = shape.area()
            if area < spec.min_area_m2:
                dropped['small'] += 1
                continue
            perimeter = shape.length()
            edge = bool(info and info['cut']) or (
                boundary is not None and shape.intersects(boundary))
            plots.append({
                'label': label, 'geometry': shape, 'raw': raw,
                'area': area, 'perimeter': perimeter,
                'conf': info['score'] if info else 0.0,
                'compact': 4.0 * math.pi * area / (perimeter ** 2)
                if perimeter > 0 else 0.0,
                'farm': float(farmland[label]) if farmland is not None
                and label < len(farmland) else None,
                'edge': edge})
        # Biggest first reads naturally in the attribute table.
        plots.sort(key=lambda p: -p['area'])
        for ident, plot in enumerate(plots, 1):
            plot['ident'] = ident
        return plots, dropped

    @staticmethod
    def _giant_area(spec, pieces):
        """Area above which an unsure plot counts as a giant, or None."""
        if not spec.drop_giants:
            return None
        areas = [g.area() for g in pieces.values()]
        areas = [a for a in areas if a >= spec.min_area_m2]
        if len(areas) < GIANT_MIN_PLOTS:
            return None
        return GIANT_RATIO * float(np.median(areas))

    def _boundaries(self, spec, grid, plots, area_grid):
        """Every plot's outline once: a shared bund is one line, not two.

        The outlines come from the unsimplified polygons, whose shared edges
        coincide exactly (they are the same pixel edges), so the union
        dissolves them; the result is simplified afterwards.
        """
        rings = []
        for plot in plots:
            raw = plot['raw']
            parts = raw.asMultiPolygon() if raw.isMultipart() \
                else [raw.asPolygon()]
            for part in parts:
                for ring in part:
                    if len(ring) >= 3:
                        rings.append(QgsGeometry.fromPolylineXY(ring))
        if not rings:
            return []
        self._check()
        lines = QgsGeometry.unaryUnion(rings)
        if lines is None or lines.isEmpty():
            return []
        merged = lines.mergeLines()
        if merged is not None and not merged.isEmpty():
            lines = merged
        out = []
        for part in (lines.asGeometryCollection() if lines.isMultipart()
                     else [lines]):
            shape = V.generalize(part, spec.simplify_m, spec.smooth)
            if spec.clip:
                shape = shape.intersection(area_grid)
            if shape is None or shape.isEmpty():
                continue
            length = shape.length()
            if length < grid.gsd * 3:
                continue
            out.append({'ident': len(out) + 1, 'geometry': shape,
                        'length': length})
        return out

    # --------------------------------------------------------------- writing

    def _write(self, spec, crs, plots, bounds):
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
            raise PaddyError('The plot file could not be created at {0}. Is '
                             'that folder writable?'.format(spec.gpkg_path))

        def geom(geometry):
            geometry = QgsGeometry(geometry)
            if transform is not None:
                geometry.transform(transform)
            return ogr.CreateGeometryFromWkb(bytes(geometry.asWkb()))

        try:
            if spec.want_plots:
                fields = [('PLOT_ID', ogr.OFTInteger),
                          ('AREA_M2', ogr.OFTReal), ('AREA_HA', ogr.OFTReal),
                          ('PERIM_M', ogr.OFTReal), ('CONF', ogr.OFTReal),
                          ('COMPACT', ogr.OFTReal), ('EDGE', ogr.OFTInteger)]
                if spec.farmland_only:
                    fields.append(('FARM_SHARE', ogr.OFTReal))
                rows = []
                for p in plots:
                    values = {'PLOT_ID': p['ident'],
                              'AREA_M2': round(p['area'], 1),
                              'AREA_HA': round(p['area'] / 1e4, 4),
                              'PERIM_M': round(p['perimeter'], 1),
                              'CONF': round(p['conf'], 3),
                              'COMPACT': round(p['compact'], 3),
                              'EDGE': 1 if p['edge'] else 0}
                    if spec.farmland_only and p['farm'] is not None:
                        values['FARM_SHARE'] = round(p['farm'], 2)
                    rows.append((values, geom(_multi(p['geometry']))))
                _layer(ogr, target, LAYER_PLOTS, srs, ogr.wkbMultiPolygon,
                       fields, rows)
            if spec.want_bounds:
                _layer(ogr, target, LAYER_BOUNDS, srs, ogr.wkbMultiLineString,
                       (('BOUND_ID', ogr.OFTInteger),
                        ('LENGTH_M', ogr.OFTReal)),
                       [({'BOUND_ID': b['ident'],
                          'LENGTH_M': round(b['length'], 1)},
                         geom(_multi(b['geometry']))) for b in bounds])
        finally:
            target = None


def start(task):
    QgsApplication.taskManager().addTask(task)
    return task


# ------------------------------------------------------------------ helpers

def _polygonize_labels(labels, grid):
    """{label: QgsGeometry} - each plot's largest connected piece.

    A plot whose pixels fell apart (a corner taken by a more confident
    neighbour) keeps its main piece; crumbs are dropped.
    """
    from osgeo import gdal, ogr, osr

    driver = gdal.GetDriverByName('MEM')
    source = driver.Create('', grid.cols, grid.rows, 1, gdal.GDT_Int32)
    source.SetGeoTransform(grid.geotransform())
    srs = osr.SpatialReference()
    srs.ImportFromWkt(grid.crs.toWkt())
    source.SetProjection(srs.ExportToWkt())
    band = source.GetRasterBand(1)
    band.WriteArray(labels)
    mask_ds = driver.Create('', grid.cols, grid.rows, 1, gdal.GDT_Byte)
    mask_band = mask_ds.GetRasterBand(1)
    mask_band.WriteArray((labels > 0).astype(np.uint8))
    target = V.memory_vector()
    layer = target.CreateLayer('plots', srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('label', ogr.OFTInteger))
    # 4-connected: two plots touching only at a corner stay apart.
    gdal.Polygonize(band, mask_band, layer, 0, [])
    best = {}
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None:
            continue
        label = feature.GetField(0)
        area = geometry.GetArea()
        if label not in best or area > best[label][0]:
            best[label] = (area, geometry.ExportToWkt())
    return {label: QgsGeometry.fromWkt(wkt)
            for label, (_area, wkt) in best.items()}


def _dropped_words(dropped):
    parts = []
    if dropped.get('small'):
        parts.append('{0:,} too small'.format(dropped['small']))
    if dropped.get('farmland'):
        parts.append('{0:,} not on farmland'.format(dropped['farmland']))
    if dropped.get('giant'):
        parts.append('{0:,} giant'.format(dropped['giant']))
    return ', '.join(parts) or 'none'


def _grid_rect(grid):
    return QgsRectangle(grid.west, grid.north - grid.rows * grid.gsd,
                        grid.west + grid.cols * grid.gsd, grid.north)


def _multi(geometry):
    """`geometry` as a multi-part of its own kind (see roads.task)."""
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
            raise PaddyError('The imagery file could not be created at '
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
                np.ascontiguousarray(rgb[:, :, band]), col, row)
        self.mask.WriteArray(valid.astype(np.uint8) * 255, col, row)

    def close(self):
        if self.dataset is not None:
            self.dataset.FlushCache()
            self.dataset = None


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

def build_report(spec, grid, stats, plots, bounds, dropped, farm_checked,
                 farm_problem):
    report = CHECK.Report('Paddy plots - {0}'.format(spec.title))
    work_area = QgsGeometry(spec.area)
    if spec.area_crs != grid.crs:
        work_area.transform(QgsCoordinateTransform(
            spec.area_crs, grid.crs, QgsCoordinateTransformContext()))
    area_ha = (work_area.area() if spec.clip else
               work_area.boundingBox().area()) / 1e4
    sizes = np.asarray([p['area'] / 1e4 for p in plots], dtype=np.float64)
    total_ha = float(sizes.sum()) if len(sizes) else 0.0
    rows = [('Imagery', spec.layer_name),
            ('Area', '{0} ({1:,.1f} ha)'.format(spec.area_label, area_ha)),
            ('Model', spec.model['short']),
            ('Resolution', '{0:g} m per pixel; {1:,} chips of {2} px'.format(
                grid.gsd, stats['chips'], CHIP)),
            ('Confidence', 'fields scored ≥ {0:.2f}'.format(spec.confidence)),
            ('Plots', '{0:,}, {1:,.2f} ha ({2:.0f}% of the area)'.format(
                len(plots), total_ha,
                100.0 * total_ha / area_ha if area_ha > 0 else 0.0))]
    if len(sizes):
        rows += [('Plot size', 'median {0:,.3f} ha; middle half {1:,.3f} - '
                  '{2:,.3f} ha; largest {3:,.2f} ha'.format(
                      float(np.median(sizes)),
                      float(np.percentile(sizes, 25)),
                      float(np.percentile(sizes, 75)), float(sizes.max()))),
                 ('On the area\'s edge or cut', '{0:,} (EDGE = 1)'.format(
                     sum(1 for p in plots if p['edge'])))]
    rows += [('Dropped', _dropped_words(dropped)),
             ('Boundary lines', '{0:,}, {1:,.2f} km'.format(
                 len(bounds), sum(b['length'] for b in bounds) / 1000.0)),
             ('Generalization', '{0}{1}; gaps up to {2:g} m closed'.format(
                 'simplified to {0:g} m'.format(spec.simplify_m)
                 if spec.simplify_m > 0 else 'not simplified',
                 ', smoothed' if spec.smooth else '', spec.close_m))]
    report.add('Result', rows)

    notes = []
    if stats['scale'] > 1:
        notes.append(
            'The tile service has no imagery at the chosen resolution here, '
            'so '
            'it was read {0} zoom level{1} coarser (about {2:.1f} m) and '
            'enlarged. Small plots are harder to separate at that '
            'resolution.'.format(int(math.log(stats['scale'], 2)),
                                 '' if stats['scale'] == 2 else 's',
                                 grid.gsd * stats['scale']))
    if stats['placeholder_blocks']:
        notes.append(
            '{0} of {1} blocks showed a "no imagery" placeholder at every '
            'zoom tried and were skipped.'.format(stats['placeholder_blocks'],
                                                  stats['blocks']))
    if farm_checked:
        notes.append(
            'Plots with less than half their area on cropland, grassland or '
            'herbaceous wetland in ESA WorldCover 2021 (10 m) were dropped: '
            'that removes most villages, orchards and woodland the model '
            'outlines. FARM_SHARE is that share for the plots kept.')
    elif farm_problem:
        notes.append(farm_problem)
    if dropped.get('giant'):
        notes.append(
            'Giants were dropped: plots over {0:g} times the median size that '
            'the model was unsure of (score under {1:g}), that more '
            'confident plots mostly overlapped, or that were larger than the '
            'model could see at once with a score under {2:g}. The model '
            'sometimes boxes a '
            'whole block of plots as one; the plots inside such a block may '
            'be missing.'.format(GIANT_RATIO, GIANT_CONF, GIANT_CUT_CONF))
    notes += [
        'The model outlines every kind of field, not only rice: in the dry '
        'season, or near villages, check that the plots are paddies.',
        'CONF is the model\'s score for the plot (0-1). EDGE = 1 marks plots '
        'that touch the area\'s edge or were larger than the model could see '
        'at once; they may be cut short.',
        'Where neighbouring paddies look alike (the same crop, the same '
        'water), the bund between them may be missed and two plots come out '
        'as one. A finer resolution, or the v2 model, separates more of them.',
        'Treat the plots as a draft to check and edit, not as a survey.']
    report.add('Reading the result', paragraphs=notes)
    credits = [spec.model['credit'],
               'Imagery: {0}. The plots are derived from it; check the '
               'imagery '
               'provider\'s terms before you publish or share them.'.format(
                   spec.layer_name)]
    if farm_checked:
        credits.append(WORLDCOVER_CREDIT)
    report.add('Credits', paragraphs=credits)
    return report

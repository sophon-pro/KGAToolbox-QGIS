# -*- coding: utf-8 -*-
"""Is the stream network any good? Checks, a reference comparison, a report.

A drainage network traced from a DEM is a *model* of where water runs, not a
survey of where streams are. Two kinds of check say how far to trust it:

* **Internal checks**, always on, need nothing but the DEM. Each stream segment
  is tested for the tell-tale signs of a bad trace - it runs across flat ground,
  through a depression the DEM had to fill, along no valley at all, or starts
  right at the edge of the data where its upstream is cut off - and is graded
  High, Medium or Low confidence.
* **A reference comparison**, optional, sets the network against an independent
  one: OpenStreetMap rivers and streams, or a line layer of the user's own. Wherever
  one network has a stream the other does not, that is reported and drawn - "in
  the DEM but not on the ground" and the other way round.

Neither check is proof. A reference can be incomplete (OpenStreetMap often is
in the countryside), and a DEM stream can be a real gully that carries water
only after rain. The report says so, and does not turn a score into a verdict
it cannot back.

Nothing here touches a widget. The reference is fetched by the dialog, on the
GUI thread, and handed over as plain coordinate lists.
"""

import json
import math
from contextlib import suppress
from urllib.parse import quote

import numpy as np

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeatureRequest, QgsGeometry, QgsProject,
                       QgsRectangle, QgsWkbTypes)

# ------------------------------------------------------------ internal checks

FLAG_FLAT = 'FLAT'
FLAG_FILLED = 'FILLED'
FLAG_NOVALLEY = 'NOVALLEY'
FLAG_EDGE = 'EDGE'

CONF_HIGH = 'High'
CONF_MEDIUM = 'Medium'
CONF_LOW = 'Low'

#: Below this slope a DEM cannot say which way water goes: the vertical error
#: of a 30 m model (a few metres) swamps the fall over any ordinary reach, and
#: the trace there is a straight line or a set of parallel ones.
FLAT_SLOPE_PCT = 0.10
#: Water the DEM had to pour into a depression to get a stream across it, in
#: metres: twice the Copernicus DEM's stated vertical accuracy (4 m). A surface
#: model is noisy along a channel, so a few metres of filling is normal; more
#: is a reservoir, an embankment, a bridge or a real sink the trace cannot
#: follow.
FILL_FLAG_M = 8.0
#: Mean ground height around the stream minus its own, in metres, over the
#: 7 x 7 cells centred on it. Under this the "stream" lies on open ground.
RELIEF_FLAG_M = 1.0


def classify(slope_pct, fill_m, relief_m, at_edge):
    """Flags and a confidence grade for one segment."""
    flags = []
    if slope_pct < FLAT_SLOPE_PCT:
        flags.append(FLAG_FLAT)
    if fill_m >= FILL_FLAG_M:
        flags.append(FLAG_FILLED)
    if relief_m < RELIEF_FLAG_M:
        flags.append(FLAG_NOVALLEY)
    if at_edge:
        flags.append(FLAG_EDGE)
    # Edge is a caution about the start of the line, not about the channel, so
    # it does not lower the grade by itself.
    serious = [f for f in flags if f != FLAG_EDGE]
    if len(serious) >= 2:
        grade = CONF_LOW
    elif serious:
        grade = CONF_MEDIUM
    else:
        grade = CONF_HIGH
    return ' '.join(flags), grade


# --------------------------------------------------------- reference sources

#: Public Overpass servers, tried in turn: the first is often busy.
OVERPASS_URLS = ('https://overpass-api.de/api/interpreter',
                 'https://overpass.kumi.systems/api/interpreter')
#: Natural watercourses only. Canals, drains and ditches are dug where they are
#: wanted, not where the ground runs downhill, so they are not a test of a
#: DEM-derived network - and an irrigation area is full of them.
OSM_WATERWAYS = ('river', 'stream')

#: A layer with more features than this is almost certainly not a stream layer.
MAX_REFERENCE_FEATURES = 50000


class ReferenceError(Exception):
    """A reference that cannot be used. Shown to the user as-is."""


class Reference(object):
    """Lines to compare against, in EPSG:4326, with a label for the report."""

    def __init__(self, label, lines, note=''):
        self.label = label
        #: A list of lines, each a list of (lon, lat).
        self.lines = lines
        self.note = note


def overpass_urls(bounds):
    """One request URL per Overpass server, for the rivers and streams in `bounds`."""
    west, south, east, north = bounds
    query = ('[out:json][timeout:30];way["waterway"~"^({0})$"]'
             '({1:.6f},{2:.6f},{3:.6f},{4:.6f});out geom;').format(
                 '|'.join(OSM_WATERWAYS), south, west, north, east)
    return ['{0}?data={1}'.format(base, quote(query)) for base in OVERPASS_URLS]


def fetch_osm(bounds, get, stop=None):
    """Rivers and streams from OpenStreetMap, as a :class:`Reference`.

    `get` fetches a URL and returns bytes (``net.get``); it is passed in so
    this module stays free of the network layer, and `stop()` is asked between
    servers. Raises ReferenceError with the
    last server's complaint when every server fails.
    """
    problem = None
    for url in overpass_urls(bounds):
        if stop is not None and stop():
            raise ReferenceError('Stopped.')
        try:
            return parse_overpass(get(url))
        except ReferenceError as exc:
            problem = str(exc)
        except Exception as exc:                # NetError, in practice
            problem = str(exc)
    raise ReferenceError(problem or 'OpenStreetMap could not be reached.')


def parse_overpass(body):
    """Overpass JSON -> :class:`Reference`."""
    try:
        data = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ReferenceError('OpenStreetMap answered with something that is '
                             'not valid JSON. The service may be busy; try '
                             'again in a minute.')
    remark = data.get('remark') or ''
    elements = data.get('elements') or []
    if not elements and ('timed out' in remark or 'runtime error' in remark):
        raise ReferenceError('OpenStreetMap could not answer for an area this '
                             'big in time. Use a smaller area, or compare with '
                             'a layer of your own.')
    lines = []
    for element in elements:
        geometry = element.get('geometry') or []
        points = [(p['lon'], p['lat']) for p in geometry
                  if 'lon' in p and 'lat' in p]
        if len(points) >= 2:
            lines.append(points)
    return Reference('OpenStreetMap rivers and streams', lines)


def reference_from_layer(layer, bounds, selected_only):
    """Lines of a vector layer that fall in `bounds` (EPSG:4326), as a Reference.

    Runs on the GUI thread: it reads the layer. Only lines are accepted; a
    polygon's outline is not a stream.
    """
    if layer is None:
        raise ReferenceError('Choose a line layer to compare with.')
    if QgsWkbTypes.geometryType(layer.wkbType()) != \
            QgsWkbTypes.GeometryType.LineGeometry:
        raise ReferenceError('"{0}" is not a line layer. Choose a layer of '
                             'streams or rivers.'.format(layer.name()))
    wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
    to_layer = QgsCoordinateTransform(wgs84, layer.crs(), QgsProject.instance())
    to_wgs = QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance())
    west, south, east, north = bounds
    try:
        window = to_layer.transformBoundingBox(
            QgsRectangle(west, south, east, north))
    except Exception:
        raise ReferenceError('The area could not be converted into the CRS of '
                             '"{0}".'.format(layer.name()))
    request = QgsFeatureRequest().setFilterRect(window)
    if selected_only:
        if layer.selectedFeatureCount() == 0:
            raise ReferenceError('"{0}" has no selected features.'.format(
                layer.name()))
        request.setFilterFids(layer.selectedFeatureIds())
        request.setFilterRect(window)
    lines = []
    for count, feature in enumerate(layer.getFeatures(request)):
        if count >= MAX_REFERENCE_FEATURES:
            raise ReferenceError(
                '"{0}" has more than {1:,} lines in that area, which is not '
                'a stream layer. Select the streams only, or choose another '
                'layer.'.format(layer.name(), MAX_REFERENCE_FEATURES))
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        geometry = QgsGeometry(geometry)
        with suppress(Exception):
            geometry.transform(to_wgs)
        parts = geometry.asMultiPolyline() if geometry.isMultipart() \
            else [geometry.asPolyline()]
        for part in parts:
            if len(part) >= 2:
                lines.append([(p.x(), p.y()) for p in part])
    note = 'selected features only' if selected_only else ''
    return Reference(layer.name(), lines, note)


# ---------------------------------------------------------------- comparison

def sample_line(xy, step):
    """Points along a polyline, at most `step` apart, ends included.

    `xy` is an (n, 2) array in metres. Returns ``(points, length)``.
    """
    xy = np.asarray(xy, dtype=np.float64)
    if len(xy) < 2:
        return xy.reshape(-1, 2), 0.0
    seg = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    along = np.concatenate(([0.0], np.cumsum(seg)))
    length = float(along[-1])
    if length <= 0:
        return xy[:1], 0.0
    count = max(2, int(math.ceil(length / step)) + 1)
    at = np.linspace(0.0, length, count)
    return np.column_stack((np.interp(at, along, xy[:, 0]),
                            np.interp(at, along, xy[:, 1]))), length


class _PointIndex(object):
    """Is anything within `radius` of a point? A hash grid, no scipy needed."""

    def __init__(self, points, radius):
        self.radius = radius
        self.cells = {}
        keys = np.floor(points / radius).astype(np.int64)
        for (i, j), (x, y) in zip(keys.tolist(), points.tolist()):
            self.cells.setdefault((i, j), []).append((x, y))

    def near(self, x, y):
        r = self.radius
        i = int(math.floor(x / r))
        j = int(math.floor(y / r))
        limit = r * r
        cells = self.cells
        for a in (i - 1, i, i + 1):
            for b in (j - 1, j, j + 1):
                for px, py in cells.get((a, b), ()):
                    if (px - x) ** 2 + (py - y) ** 2 <= limit:
                        return True
        return False


def _runs(flags, minimum):
    """(start, stop) index pairs where `flags` is False for `minimum`+ samples."""
    runs = []
    start = None
    for index, value in enumerate(flags):
        if not value:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= minimum:
                runs.append((start, index))
            start = None
    if start is not None and len(flags) - start >= minimum:
        runs.append((start, len(flags)))
    return runs


class Comparison(object):
    """What :func:`compare` found. Lengths are in metres."""

    def __init__(self):
        #: Strahler order -> [length, matched length] of the DEM streams.
        self.by_order = {}
        self.extracted_m = 0.0
        self.extracted_matched_m = 0.0
        self.reference_m = 0.0
        self.reference_matched_m = 0.0
        #: [(segment ident, confidence, up_km2, length_m, points)] - in the
        #: DEM but not in the reference.
        self.extra = []
        #: [(length_m, points)] - in the reference but not in the DEM.
        self.missed = []

    @property
    def precision(self):
        return self.extracted_matched_m / self.extracted_m \
            if self.extracted_m else 0.0

    @property
    def recall(self):
        return self.reference_matched_m / self.reference_m \
            if self.reference_m else 0.0

    def extra_m(self):
        return sum(item[3] for item in self.extra)

    def missed_m(self):
        return sum(item[0] for item in self.missed)


def compare(extracted, reference, tolerance, step=None, stop=None):
    """Set two networks against each other.

    `extracted` is ``[(ident, confidence, up_km2, order, xy)]`` and `reference` a list
    of ``xy`` arrays, all in one metric CRS. A stretch of one network is
    *matched* when the other has a line within `tolerance` metres of it. Only
    stretches unmatched for at least twice the tolerance are reported, so the
    ragged ends where two lines meet at an angle are not counted as faults.
    """
    step = step or max(10.0, tolerance / 4.0)
    minimum = max(2, int(math.ceil(2.0 * tolerance / step)))

    sampled_ext = []
    for ident, conf, up, order, xy in extracted:
        points, length = sample_line(xy, step)
        sampled_ext.append((ident, conf, up, order, points, length))
    sampled_ref = []
    for xy in reference:
        points, length = sample_line(xy, step)
        sampled_ref.append((points, length))

    result = Comparison()
    ref_points = [p for p, _l in sampled_ref if len(p)]
    ext_points = [item[4] for item in sampled_ext if len(item[4])]
    if not ref_points or not ext_points:
        result.extracted_m = sum(item[5] for item in sampled_ext)
        result.reference_m = sum(item[1] for item in sampled_ref)
        return result
    ref_index = _PointIndex(np.vstack(ref_points), tolerance)
    ext_index = _PointIndex(np.vstack(ext_points), tolerance)

    for count, (ident, conf, up, order, points, length) in enumerate(
            sampled_ext):
        if stop is not None and count % 200 == 0 and stop():
            return None
        matched = [ref_index.near(x, y) for x, y in points.tolist()]
        result.extracted_m += length
        tally = result.by_order.setdefault(order, [0.0, 0.0])
        tally[0] += length
        if len(points):
            share = length * sum(matched) / len(matched)
            result.extracted_matched_m += share
            tally[1] += share
        for a, b in _runs(matched, minimum):
            chunk = points[max(0, a - 1):min(len(points), b + 1)]
            run_length = float(np.sum(np.hypot(np.diff(chunk[:, 0]),
                                               np.diff(chunk[:, 1]))))
            result.extra.append((ident, conf, up, run_length, chunk))

    for count, (points, length) in enumerate(sampled_ref):
        if stop is not None and count % 200 == 0 and stop():
            return None
        if not len(points):
            continue
        matched = [ext_index.near(x, y) for x, y in points.tolist()]
        result.reference_m += length
        result.reference_matched_m += length * sum(matched) / len(matched)
        for a, b in _runs(matched, minimum):
            chunk = points[max(0, a - 1):min(len(points), b + 1)]
            run_length = float(np.sum(np.hypot(np.diff(chunk[:, 0]),
                                               np.diff(chunk[:, 1]))))
            result.missed.append((run_length, chunk))
    return result


def agreement_words(precision, recall):
    """One honest phrase for how well two networks agree."""
    if precision >= 0.80 and recall >= 0.70:
        return 'good agreement'
    if precision >= 0.60 and recall >= 0.50:
        return 'moderate agreement'
    return 'weak agreement'


# -------------------------------------------------------------------- report

class Report(object):
    """Titled sections of (label, value) rows and plain paragraphs."""

    def __init__(self, title):
        self.title = title
        self.sections = []

    def add(self, heading, rows=None, paragraphs=None):
        self.sections.append((heading, rows or [], paragraphs or []))

    def text(self):
        lines = [self.title, '=' * len(self.title), '']
        for heading, rows, paragraphs in self.sections:
            lines.append(heading)
            lines.append('-' * len(heading))
            width = max([len(str(label)) for label, _v in rows] or [0])
            for label, value in rows:
                lines.append('  {0}  {1}'.format(str(label).ljust(width),
                                                 value))
            for paragraph in paragraphs:
                lines.append(paragraph)
            lines.append('')
        return '\n'.join(lines)

    def html(self):
        from html import escape

        out = ['<h3>{0}</h3>'.format(escape(self.title))]
        for heading, rows, paragraphs in self.sections:
            out.append('<h4>{0}</h4>'.format(escape(heading)))
            if rows:
                out.append('<table cellspacing="0" cellpadding="2">')
                for label, value in rows:
                    out.append('<tr><td><b>{0}</b>&nbsp;&nbsp;</td><td>{1}'
                               '</td></tr>'.format(escape(str(label)),
                                                   escape(str(value))))
                out.append('</table>')
            for paragraph in paragraphs:
                out.append('<p>{0}</p>'.format(escape(paragraph)))
        return ''.join(out)

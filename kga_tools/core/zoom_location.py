# -*- coding: utf-8 -*-
"""Zoom to Location: the logic behind the window.

A place is picked down a ladder - continent, country, province, district,
commune, village - or typed as a latitude and longitude. Nothing here touches
a widget.

Where the places come from, all open:

* **Continents and countries** are built in. The country table and a
  continent for each code ship with the plugin, and a country is framed by the
  extents of the QGIS world map (`country_extents`), so the first two rungs
  work offline and open instantly.
* **Provinces, districts and communes** are geoBoundaries levels ADM1, ADM2 and
  ADM3, through the same source Add Open Data uses, so the files land in the
  same cache and are shared with it. geoBoundaries does not say which district
  sits in which province, so the tree is rebuilt from the shapes: a unit
  belongs to the parent that holds its inner point (see :func:`_assign_parents`).
* **Villages** come from OpenStreetMap through the Overpass API, because no
  global open source publishes village boundaries. A village is a point: the
  place node OSM carries for it.

A country that does not publish a level simply has no such rung; the window
greys that row out.
"""

import hashlib
import re
import urllib.parse

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsFeature,
                       QgsFillSymbol, QgsGeometry, QgsPointXY, QgsRectangle,
                       QgsSingleSymbolRenderer, QgsSpatialIndex,
                       QgsVectorLayer)

from . import compat
from . import country_extents
from . import countries as C
from . import data_sources as DS
from . import layer_loader as LL
from . import net

WGS84 = 'EPSG:4326'

#: The admin rungs between a country and a village: (geoBoundaries level, label).
ADMIN_ROWS = ((1, 'Province'), (2, 'District'), (3, 'Commune'))
LEVEL_VILLAGE = 4

#: Half the side, in degrees, of the box a typed coordinate or a village is
#: framed with: about a kilometre each way.
POINT_HALF_SIDE = 0.01

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
#: Tried in order; the public servers refuse or time out now and then.
OVERPASS_URLS = (OVERPASS_URL,
                 'https://overpass.kumi.systems/api/interpreter')
OVERPASS_ATTRIBUTION = ('Villages: OpenStreetMap contributors, '
                        'https://www.openstreetmap.org/copyright')
OVERPASS_LICENCE = 'ODbL 1.0'

_GB_OPTIONS = {'release': 'gbOpen', 'geometry': 'simplified'}


class ZoomError(Exception):
    """Something the user can act on, worded as a sentence."""


# ---------------------------------------------------------------- continents

#: (id, label, (west, south, east, north)). The boxes are rough on purpose:
#: they frame the continent on screen, they are not a boundary.
CONTINENTS = (
    ('AF', 'Africa', (-26.0, -35.5, 64.0, 38.0)),
    ('AN', 'Antarctica', (-180.0, -85.0, 180.0, -60.0)),
    ('AS', 'Asia', (25.0, -11.0, 150.0, 78.0)),
    ('EU', 'Europe', (-25.0, 34.0, 45.0, 72.0)),
    ('NA', 'North America', (-170.0, 7.0, -50.0, 84.0)),
    ('OC', 'Oceania', (110.0, -48.0, 180.0, 0.0)),
    ('SA', 'South America', (-82.0, -56.0, -34.0, 13.0)),
)

_CONTINENT_MEMBERS = {
    'AF': ('DZA AGO BEN BWA BFA BDI CPV CMR CAF TCD COM COG COD CIV DJI EGY '
           'GNQ ERI SWZ ETH GAB GMB GHA GIN GNB KEN LSO LBR LBY MDG MWI MLI '
           'MRT MUS MYT MAR MOZ NAM NER NGA REU RWA SHN STP SEN SYC SLE SOM '
           'ZAF SSD SDN TZA TGO TUN UGA ESH ZMB ZWE'),
    'AN': 'ATA ATF BVT HMD SGS',
    'AS': ('AFG ARM AZE BHR BGD BTN IOT BRN KHM CHN CXR CCK CYP GEO HKG IND '
           'IDN IRN IRQ ISR JPN JOR KAZ KWT KGZ LAO LBN MAC MYS MDV MNG MMR '
           'NPL PRK OMN PAK PSE PHL QAT SAU SGP KOR LKA SYR TWN TJK THA TLS '
           'TUR TKM ARE UZB VNM YEM'),
    'EU': ('ALA ALB AND AUT BLR BEL BIH BGR HRV CZE DNK EST FRO FIN FRA DEU '
           'GIB GRC GGY VAT HUN ISL IRL IMN ITA JEY LVA LIE LTU LUX MLT MDA '
           'MCO MNE NLD MKD NOR POL PRT ROU RUS SMR SRB SVK SVN ESP SJM SWE '
           'CHE UKR GBR'),
    'NA': ('AIA ATG ABW BHS BRB BLZ BMU BES CAN CYM CRI CUB CUW DMA DOM SLV '
           'GRL GRD GLP GTM HTI HND JAM MTQ MEX MSR NIC PAN PRI BLM KNA LCA '
           'MAF SPM VCT SXM TTO TCA USA VGB VIR UMI'),
    'OC': ('ASM AUS COK FJI PYF GUM KIR MHL FSM NRU NCL NZL NIU NFK MNP PLW '
           'PNG PCN WSM SLB TKL TON TUV VUT WLF'),
    'SA': 'ARG BOL BRA CHL COL ECU FLK GUF GUY PRY PER SUR URY VEN',
}

CONTINENT_OF = {iso3: cid for cid, codes in _CONTINENT_MEMBERS.items()
                for iso3 in codes.split()}


def continent_extent(continent_id):
    """The framing box (EPSG:4326) of a continent, or None."""
    for cid, _label, box in CONTINENTS:
        if cid == continent_id:
            return QgsRectangle(*box)
    return None


def countries_in(continent_id):
    """[(alpha-3, name)] of the countries on a continent, by name."""
    return [(iso3, name) for iso3, name in C.COUNTRIES
            if CONTINENT_OF.get(iso3) == continent_id]


def country_extent(iso3):
    """The framing box (EPSG:4326) of a country, or None."""
    return country_extents.extent_for(iso3)


# --------------------------------------------------------------- coordinates

_NUMBER = r'([+-]?\d+(?:\.\d+)?)\s*°?\s*([NSEWnsew])?'
_PAIR = re.compile(r'\s*{0}\s*[,;\s]\s*{0}\s*$'.format(_NUMBER))
_HINT = 'Type a latitude and a longitude, for example 11.5972, 104.8887.'


def parse_coordinate(text):
    """'11.5972, 104.8887' -> (lat, lon). Raises :class:`ZoomError`.

    Latitude comes first. A hemisphere letter after a number (``12.5N
    103.55E``) is honoured, in either order. Without letters, a first number
    that cannot be a latitude while the second can is read as longitude first,
    which is how a pasted GeoJSON or GPX pair arrives.
    """
    match = _PAIR.match(text or '')
    if not match:
        raise ZoomError(_HINT)
    first, first_hemi, second, second_hemi = match.groups()
    a, b = float(first), float(second)
    fh = (first_hemi or '').upper()
    sh = (second_hemi or '').upper()

    if fh in ('E', 'W') or sh in ('N', 'S'):
        a, b, fh, sh = b, a, sh, fh
    if fh in ('E', 'W') or sh in ('N', 'S'):
        raise ZoomError('That has two latitudes or two longitudes. ' + _HINT)
    lat = -abs(a) if fh == 'S' else (abs(a) if fh == 'N' else a)
    lon = -abs(b) if sh == 'W' else (abs(b) if sh == 'E' else b)

    if not fh and not sh and abs(lat) > 90 and abs(lon) <= 90:
        lat, lon = lon, lat
    if abs(lat) > 90:
        raise ZoomError('Latitude {0:g} is outside -90 to 90. {1}'.format(
            lat, _HINT))
    if abs(lon) > 180:
        raise ZoomError('Longitude {0:g} is outside -180 to 180. {1}'.format(
            lon, _HINT))
    return lat, lon


def point_extent(lat, lon, half=POINT_HALF_SIDE):
    return QgsRectangle(lon - half, lat - half, lon + half, lat + half)


# ------------------------------------------------------------- the admin tree

class Unit(object):
    """One boundary (or village point) the user can pick."""

    def __init__(self, level, name, geometry, code='', label=''):
        self.level = level
        self.name = name
        self.geometry = geometry
        self.code = code
        #: What the combo shows; the name unless a second spelling is known.
        self.label = label or name
        self.parent = None

    def extent(self):
        box = self.geometry.boundingBox()
        if box.width() == 0 and box.height() == 0:
            return point_extent(box.yMinimum(), box.xMinimum())
        return box

    def path(self):
        """Names from the top of the tree down to this unit."""
        names, unit = [], self
        while unit is not None:
            names.append(unit.name)
            unit = unit.parent
        return list(reversed(names))


class GeoBoundariesLoader(object):
    """Reads one country's admin levels from geoBoundaries (cached download)."""

    def __init__(self, iso3):
        self.iso3 = iso3
        self.source = DS.GeoBoundariesSource()
        self.item = {}

    def levels(self):
        try:
            return set(self.source.levels(self.iso3, _GB_OPTIONS))
        except net.NetError as exc:
            raise ZoomError(str(exc))

    def load(self, level):
        """[Unit] for one level. Raises :class:`ZoomError`."""
        try:
            item = self.source.describe(self.iso3, level, _GB_OPTIONS)
            if item is None:
                return []
            self.item[level] = item
            path = self.source.fetch(item)
        except net.NetError as exc:
            raise ZoomError(str(exc))
        if not path:
            return []
        layer = QgsVectorLayer(path, 'zoom_to_location', 'ogr')
        if not layer.isValid():
            raise ZoomError('The downloaded boundary file could not be '
                            'opened. Try again; if it keeps failing, clear '
                            'the data cache in KGA Settings.')
        units = []
        fields = [f.name() for f in layer.fields()]
        name_field = 'shapeName' if 'shapeName' in fields else None
        code_field = 'shapeID' if 'shapeID' in fields else None
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            if not geometry.isGeosValid():
                fixed = geometry.makeValid()
                if fixed is not None and not fixed.isEmpty():
                    geometry = fixed
            name = (str(feature[name_field]).strip()
                    if name_field and feature[name_field] else '')
            units.append(Unit(
                level, name or '(unnamed)', QgsGeometry(geometry),
                str(feature[code_field]) if code_field else ''))
        units.sort(key=lambda u: u.name.casefold())
        return units


def _assign_parents(children, parents):
    """Give every child the parent that holds it.

    The child's inner point decides; a point on a shared edge, or in a gap
    between two simplified parents, falls back to the parent the child
    overlaps most, and to the nearest parent when it overlaps none.
    """
    if not parents:
        return
    index = QgsSpatialIndex()
    for number, parent in enumerate(parents):
        feature = QgsFeature(number)
        feature.setGeometry(parent.geometry)
        index.addFeature(feature)

    for child in children:
        point = child.geometry.pointOnSurface()
        pxy = point.asPoint() if point and not point.isEmpty() else None
        holders = []
        if pxy is not None:
            spot = QgsRectangle(pxy, pxy)
            holders = [parents[i] for i in index.intersects(spot)
                       if parents[i].geometry.contains(point)]
        if len(holders) == 1:
            child.parent = holders[0]
            continue
        candidates = holders or [
            parents[i] for i in index.intersects(child.geometry.boundingBox())]
        best, best_area = None, 0.0
        for parent in candidates:
            area = parent.geometry.intersection(child.geometry).area()
            if area > best_area:
                best, best_area = parent, area
        if best is None and pxy is not None:
            near = index.nearestNeighbor(pxy, 1)
            best = parents[near[0]] if near else None
        child.parent = best


class AdminTree(object):
    """The provinces, districts and communes of one country, on demand.

    `loader` has ``levels() -> set`` and ``load(level) -> [Unit]``; the default
    reads geoBoundaries, a test passes its own.
    """

    def __init__(self, iso3, loader=None):
        self.iso3 = iso3
        self.loader = loader or GeoBoundariesLoader(iso3)
        self._levels = None
        self._units = {}

    def levels(self):
        """The admin levels (of 1, 2, 3) this country publishes."""
        if self._levels is None:
            found = self.loader.levels()
            self._levels = {n for n, _label in ADMIN_ROWS if n in found}
        return self._levels

    def parent_level(self, level):
        """The published level a unit of `level` hangs under, or 0."""
        below = [n for n in self.levels() if n < level]
        return max(below) if below else 0

    def units(self, level):
        """Every unit of one level, parents assigned. [] if not published."""
        if level not in self.levels():
            return []
        if level not in self._units:
            units = sorted(self.loader.load(level),
                           key=lambda u: u.name.casefold())
            parent_level = self.parent_level(level)
            if parent_level:
                _assign_parents(units, self.units(parent_level))
            self._units[level] = units
        return self._units[level]

    def children(self, level, parent=None):
        """The units of `level` inside `parent` (all of them when None)."""
        units = self.units(level)
        if parent is None:
            return list(units)
        return [u for u in units if u.parent is parent]

    def provenance(self, level):
        item = getattr(self.loader, 'item', {}).get(level)
        return item


# ------------------------------------------------------------------- villages

def overpass_query(box):
    return ('[out:json][timeout:25];'
            'node["place"~"^(village|hamlet)$"]["name"]'
            '({0:.6f},{1:.6f},{2:.6f},{3:.6f});out;').format(
        box.yMinimum(), box.xMinimum(), box.yMaximum(), box.xMaximum())


def parse_villages(payload, inside=None):
    """[Unit] out of an Overpass answer, optionally only those in `inside`."""
    units = []
    for element in (payload or {}).get('elements', []):
        tags = element.get('tags') or {}
        name = ' '.join((tags.get('name') or '').split())
        if not name or 'lat' not in element or 'lon' not in element:
            continue
        geometry = QgsGeometry.fromPointXY(
            QgsPointXY(element['lon'], element['lat']))
        if inside is not None and not inside.contains(geometry):
            continue
        english = ' '.join(
            (tags.get('name:en') or tags.get('int_name') or '').split())
        label = name
        if english and english.casefold() != name.casefold():
            label = '{0} ({1})'.format(name, english)
        units.append(Unit(LEVEL_VILLAGE, name, geometry,
                          'osm:{0}'.format(element.get('id', '')), label))
    units.sort(key=lambda u: u.label.casefold())
    return units


def villages_in(parent, fetch=None):
    """The villages OpenStreetMap places inside `parent`. Cached a week.

    `fetch(url) -> parsed JSON` is for tests; the default is the shared
    network helper. Raises :class:`ZoomError`.
    """
    box = parent.geometry.boundingBox()
    query = overpass_query(box)
    key = hashlib.sha256(query.encode('utf-8')).hexdigest()[:20]
    path = net.cached_path('zoom_location', 'villages_' + key, '.json')
    payload = None
    if net.is_fresh(path, net.METADATA_TTL):
        payload = net.read_cached_json(path)
    if payload is None:
        data = urllib.parse.quote(query)
        error = None
        for server in OVERPASS_URLS:
            try:
                payload = (fetch or net.get_json)(
                    '{0}?data={1}'.format(server, data))
                break
            except net.NetError as exc:
                error = exc
        if payload is None:
            raise ZoomError(
                'Could not get the villages from OpenStreetMap: {0}'.format(
                    error))
        net.write_cached_json(path, payload)
    units = parse_villages(payload, parent.geometry)
    for unit in units:
        unit.parent = parent
    return units


# --------------------------------------------------------------- the result

class Target(object):
    """Where to go: a label, a box and (when there is one) the shape."""

    def __init__(self, label, kind, extent, geometry=None, path=None,
                 attribution='', licence='', parts=None):
        self.label = label
        self.kind = kind
        self.extent = extent
        self.geometry = geometry
        self.path = path or [label]
        self.attribution = attribution
        self.licence = licence
        #: [(name, geometry, path text)] when several places were picked.
        self.parts = parts or []

    def geometries(self):
        """Every shape to draw or add: one per picked place."""
        if self.parts:
            return [geometry for _name, geometry, _path in self.parts]
        return [self.geometry] if self.geometry is not None else []

    def layer_name(self):
        return '{0} ({1})'.format(self.label, self.kind)


def coordinate_target(lat, lon):
    return Target('{0:.5f}, {1:.5f}'.format(lat, lon), 'Coordinate',
                  point_extent(lat, lon),
                  QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))


def unit_target(unit, kind, tree=None):
    attribution, licence = OVERPASS_ATTRIBUTION, OVERPASS_LICENCE
    if unit.level != LEVEL_VILLAGE:
        item = tree.provenance(unit.level) if tree is not None else None
        attribution = item.attribution if item else DS.GeoBoundariesSource.attribution
        licence = item.licence if item else ''
    return Target(unit.name, kind, unit.extent(), unit.geometry,
                  unit.path(), attribution, licence)


def units_target(units, kind, tree=None):
    """A target for one or more picked units of the same level."""
    if len(units) == 1:
        return unit_target(units[0], kind, tree)
    names = [u.name for u in units]
    label = (', '.join(names) if len(names) <= 3
             else '{0}, {1} +{2} more'.format(names[0], names[1],
                                              len(names) - 2))
    first = unit_target(units[0], kind, tree)
    box = QgsRectangle(units[0].extent())
    for unit in units[1:]:
        box.combineExtentWith(unit.extent())
    parts = [(u.name, u.geometry, ' > '.join(u.path())) for u in units]
    return Target(label, kind, box, QgsGeometry.collectGeometry(
        [u.geometry for u in units]), [label], first.attribution,
        first.licence, parts)


# ---------------------------------------------------------------- the layer

def target_layer(target):
    """A scratch layer (EPSG:4326) holding the place(s), outlined not filled."""
    shapes = target.geometries()
    if not shapes:
        raise ZoomError('There is no shape for that place to add.')
    is_point = shapes[0].type() == Qgis.GeometryType.Point
    wkb = 'Point' if is_point else 'MultiPolygon'
    layer = QgsVectorLayer('{0}?crs={1}'.format(wkb, WGS84),
                           target.layer_name(), 'memory')
    layer.dataProvider().addAttributes([
        compat.make_field(name, compat.T_STRING)
        for name in ('name', 'kind', 'path')])
    layer.updateFields()
    rows = target.parts or [(target.label, target.geometry,
                             ' > '.join(target.path))]
    features = []
    for name, shape, path in rows:
        geometry = QgsGeometry(shape)
        if not is_point:
            geometry.convertToMultiType()
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        feature.setAttributes([name, target.kind, path])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    if not is_point:
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
            'style': 'no', 'outline_color': '#e8590c',
            'outline_width': '0.8'})))
    item = DS.SourceItem(target.layer_name(), 'zoom_location',
                         attribution=target.attribution,
                         licence=target.licence)
    LL.tag_provenance(layer, item)
    return layer


def wgs84():
    return QgsCoordinateReferenceSystem(WGS84)

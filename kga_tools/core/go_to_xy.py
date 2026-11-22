# -*- coding: utf-8 -*-
"""Go To XY: the logic behind the bar.

Turns what the user typed - X and Y in metres, feet, decimal degrees, degrees
minutes seconds, degrees decimal minutes, a UTM position, or an MGRS / US
National Grid reference - into one point in the map's coordinate system. Nothing
here touches a widget.

Metres, feet and US feet are values in the map's own coordinate system, the way
ArcGIS Pro reads them; the other units say where on the Earth they are, so they
work whatever the map's coordinate system is.

Not handled: the UTM zone exceptions around Norway and Svalbard when a latitude
and longitude are turned into a grid reference (a reference typed in is always
read in the zone it names), and the polar UPS grid.
"""

import math
import re
import urllib.parse
from urllib.parse import unquote, unquote_plus

from qgis.core import (Qgis, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsPointXY, QgsProject,
                       QgsRectangle, QgsUnitTypes)

from . import net

WGS84 = 'EPSG:4326'

#: (id, label) in the order the drop-down shows them.
UNITS = (
    ('search', 'Search Place'),
    ('m', 'Meters'),
    ('ft', 'Feet'),
    ('usft', 'US Feet'),
    ('dd', 'Decimal Degrees'),
    ('dms', 'Degrees Minutes Seconds'),
    ('ddm', 'Degrees Decimal Minutes'),
    ('mgrs', 'MGRS'),
    ('usng', 'US National Grid'),
    ('utm', 'UTM'),
)
DEFAULT_UNIT = 'dd'

SEARCH = 'search'
PROJECTED = ('m', 'ft', 'usft')
ANGLES = ('dd', 'dms', 'ddm')
GRIDS = ('mgrs', 'usng')
#: Units that can be typed as one combined pair.
PAIRABLE = ANGLES + PROJECTED + ('utm',)

#: Metres in one of each linear unit.
METRES_PER = {'m': 1.0, 'ft': 0.3048, 'usft': 1200.0 / 3937.0}

#: What the X, Y (and zone) boxes show as a hint for each unit.
EXAMPLES = {
    'search': ('Phnom Penh, Cambodia', ''),
    'm': ('500000', '1283000'),
    'ft': ('1640420', '4209000'),
    'usft': ('1640417', '4209000'),
    'dd': ('104.8887', '11.5972'),
    'dms': ('104 53 19.3 E', '11 35 50 N'),
    'ddm': ('104 53.32 E', '11 35.83 N'),
    'mgrs': ('48PXS 12345 67890', ''),
    'usng': ('48P XS 12345 67890', ''),
    'utm': ('500000', '1283000'),
}
ZONE_EXAMPLE = '48N'
#: What the one combined box shows as a hint (angles read latitude first).
COMBINED_EXAMPLES = {
    'dd': '11.5601, 104.9151',
    'dms': '11 33 36.4 N, 104 54 54.5 E',
    'ddm': '11 33.607 N, 104 54.908 E',
    'm': '500000, 1283000',
    'ft': '1640420, 4209000',
    'usft': '1640417, 4209000',
    'utm': '500000, 1283000',
}


class GoToError(Exception):
    """Something the user can act on, worded as a sentence."""


class Place(object):
    """Where the typed coordinate is: WGS 84 and in the map's system."""

    def __init__(self, lat, lon, point, extent=None):
        self.lat = lat
        self.lon = lon
        self.point = point                      # QgsPointXY in the map CRS
        self.extent = extent                    # QgsRectangle, WGS 84, or None

    def __repr__(self):
        return 'Place({:.6f}, {:.6f})'.format(self.lat, self.lon)


def wgs84():
    return QgsCoordinateReferenceSystem(WGS84)


def label_of(unit):
    return dict(UNITS).get(unit, unit)


# ------------------------------------------------------------------ numbers

def parse_number(text, name):
    """A plain decimal number, or :class:`GoToError` naming the box."""
    s = (text or '').strip().replace(' ', '')
    if not s:
        raise GoToError('Type a value for {}.'.format(name))
    try:
        value = float(s)
    except ValueError:
        raise GoToError('{} is not a number: "{}".'.format(name, text.strip()))
    if not math.isfinite(value):
        raise GoToError('{} is not a number: "{}".'.format(name, text.strip()))
    return value


_SYMBOLS = re.compile(r'[°º′″ʹʺ\'"\s,;:]+')
_NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)')


def parse_angle(text, axis):
    """Degrees, from ``104.8887``, ``104 53 19.3 E``, ``104°53'19.3"E`` or
    ``104 53.32 E``. ``axis`` is ``'x'`` (longitude, E/W) or ``'y'`` (latitude,
    N/S). A minus sign or S / W makes the value negative."""
    name = 'X (longitude)' if axis == 'x' else 'Y (latitude)'
    raw = (text or '').strip()
    if not raw:
        raise GoToError('Type a value for {}.'.format(name))
    letters = re.findall(r'[A-Za-z]', raw)
    if len(letters) > 1:
        raise GoToError('{} has more than one hemisphere letter: "{}".'
                        .format(name, raw))
    hemi = letters[0].upper() if letters else ''
    allowed = 'EW' if axis == 'x' else 'NS'
    if hemi and hemi not in allowed:
        raise GoToError('{} takes {} or {}, not {}.'.format(
            name, allowed[0], allowed[1], hemi))

    body = re.sub(r'[A-Za-z]', ' ', raw)
    numbers = _NUMBER.findall(body)
    leftover = _NUMBER.sub(' ', body)
    leftover = _SYMBOLS.sub('', leftover)
    if not numbers or leftover or len(numbers) > 3:
        raise GoToError('{} is not an angle: "{}".'.format(name, raw))
    negative = numbers[0].startswith('-')
    parts = [abs(float(n)) for n in numbers]
    deg = parts[0]
    minutes = parts[1] if len(parts) > 1 else 0.0
    seconds = parts[2] if len(parts) > 2 else 0.0
    if len(parts) > 1 and minutes >= 60:
        raise GoToError('{}: minutes must be under 60.'.format(name))
    if len(parts) > 2:
        if seconds >= 60:
            raise GoToError('{}: seconds must be under 60.'.format(name))
        if minutes != int(minutes):
            raise GoToError('{}: minutes must be whole when seconds are '
                            'given.'.format(name))
    value = deg + minutes / 60.0 + seconds / 3600.0
    if hemi in ('S', 'W') or negative:
        value = -value
    limit = 180.0 if axis == 'x' else 90.0
    if abs(value) > limit:
        raise GoToError('{0} is outside -{1:g} to {1:g}.'.format(name, limit))
    return value


# ------------------------------------------------------------ transforms

def _transform(source, target):
    return QgsCoordinateTransform(source, target, QgsProject.instance())


def _convert(point, source, target):
    try:
        out = _transform(source, target).transform(point)
    except Exception:                   # noqa: BLE001 - QgsCsException
        raise GoToError('That position cannot be converted to the map\'s '
                        'coordinate system ({}).'.format(target.authid()))
    if not (math.isfinite(out.x()) and math.isfinite(out.y())):
        raise GoToError('That position cannot be converted to the map\'s '
                        'coordinate system ({}).'.format(target.authid()))
    return out


def utm_crs(zone, north):
    return QgsCoordinateReferenceSystem(
        'EPSG:{}'.format((32600 if north else 32700) + zone))


def _make_place(lat, lon, map_crs, point=None):
    """A Place from a WGS 84 position (or from a map-CRS point)."""
    if point is None:
        point = _convert(QgsPointXY(lon, lat), wgs84(), map_crs)
    return Place(lat, lon, point)


# ----------------------------------------------------------------- UTM

_ZONE = re.compile(r'^\s*(\d{1,2})\s*([A-Za-z])?\s*$')


def parse_zone(text):
    """``48N`` / ``48S`` / ``48`` -> (zone, north). With no letter, north.

    N and S are read as the hemisphere. Any other letter is a latitude band
    (C to X), which says the hemisphere too: C-M south, P-X north."""
    m = _ZONE.match(text or '')
    if not m:
        raise GoToError('Type the UTM zone as a number and N or S, for '
                        'example {}.'.format(ZONE_EXAMPLE))
    zone = int(m.group(1))
    if not 1 <= zone <= 60:
        raise GoToError('The UTM zone must be from 1 to 60.')
    letter = (m.group(2) or 'N').upper()
    if letter == 'N':
        return zone, True
    if letter == 'S':
        return zone, False
    if letter in BAND_LETTERS and letter not in 'AB':
        return zone, letter > 'N'
    raise GoToError('"{}" is not a UTM hemisphere or band letter.'.format(
        m.group(2)))


def utm_to_place(easting, northing, zone, north, map_crs):
    if not 100000.0 <= easting <= 900000.0:
        raise GoToError('A UTM easting is between 100000 and 900000 metres.')
    if not 0.0 <= northing <= 10000000.0:
        raise GoToError('A UTM northing is between 0 and 10000000 metres.')
    utm = utm_crs(zone, north)
    ll = _convert(QgsPointXY(easting, northing), utm, wgs84())
    if abs(ll.y()) > 84.5:
        raise GoToError('That UTM position is beyond 84 degrees north or '
                        'south, where UTM is not used.')
    return _make_place(ll.y(), ll.x(), map_crs)


# ---------------------------------------------------------------- MGRS

BAND_LETTERS = 'CDEFGHJKLMNPQRSTUVWX'
_COLUMN_SETS = ('ABCDEFGH', 'JKLMNPQR', 'STUVWXYZ')
_ROW_LETTERS = 'ABCDEFGHJKLMNPQRSTUV'

_MGRS = re.compile(r'^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)$')


def band_letter(lat):
    index = int((lat + 80.0) // 8)
    return BAND_LETTERS[max(0, min(index, len(BAND_LETTERS) - 1))]


def _min_northing(band, zone):
    """The lowest UTM northing the latitude band reaches in this zone."""
    south = BAND_LETTERS.index(band) * 8 - 80
    north = band >= 'N'
    crs = utm_crs(zone, north)
    centre = (zone - 1) * 6 - 180 + 3
    to_utm = _transform(wgs84(), crs)
    return min(to_utm.transform(QgsPointXY(centre + d, south)).y()
               for d in (-3.0, 0.0, 3.0))


def parse_mgrs(text):
    """``48PXS1234567890`` -> (zone, band, column, row, easting, northing),
    the position being the middle of the square the digits name."""
    s = re.sub(r'\s+', '', text or '').upper()
    m = _MGRS.match(s)
    if not m:
        raise GoToError('Type an MGRS reference such as 48PXS 12345 67890 '
                        '(zone, band, 100 km square, then an even number of '
                        'digits).')
    zone = int(m.group(1))
    band, col, row, digits = m.group(2), m.group(3), m.group(4), m.group(5)
    if not 1 <= zone <= 60:
        raise GoToError('The grid zone must be from 1 to 60.')
    if len(digits) % 2 or len(digits) > 10:
        raise GoToError('The digits of an MGRS reference come in pairs, up '
                        'to five each for easting and northing.')
    columns = _COLUMN_SETS[(zone - 1) % 3]
    if col not in columns:
        raise GoToError('{} is not a 100 km column letter in zone {}.'.format(
            col, zone))
    half = len(digits) // 2
    scale = 10 ** (5 - half)
    east = (columns.index(col) + 1) * 100000.0
    north_in_cycle = ((_ROW_LETTERS.index(row) - (5 if zone % 2 == 0 else 0))
                      % 20) * 100000.0
    e_digits = int(digits[:half]) if half else 0
    n_digits = int(digits[half:]) if half else 0
    easting = east + e_digits * scale + (scale / 2.0 if half < 5 else 0.0)
    northing_low = north_in_cycle + n_digits * scale + (
        scale / 2.0 if half < 5 else 0.0)

    floor = math.floor(_min_northing(band, zone) / 100000.0) * 100000.0
    cycles = math.ceil((floor - north_in_cycle) / 2000000.0)
    northing = northing_low + max(0, cycles) * 2000000.0
    return zone, band, easting, northing


def mgrs_to_place(text, map_crs):
    zone, band, easting, northing = parse_mgrs(text)
    north = band >= 'N'
    ll = _convert(QgsPointXY(easting, northing), utm_crs(zone, north),
                  wgs84())
    south_lat = BAND_LETTERS.index(band) * 8 - 80
    top = 84.0 if band == 'X' else south_lat + 8
    if not south_lat - 0.05 <= ll.y() <= top + 0.05:
        raise GoToError('That reference is not inside latitude band {} of '
                        'zone {}.'.format(band, zone))
    return _make_place(ll.y(), ll.x(), map_crs)


def latlon_to_mgrs(lat, lon, digits=5):
    """The MGRS reference of a position, ``digits`` per axis (0 to 5)."""
    if not -80.0 <= lat <= 84.0:
        raise GoToError('MGRS covers 80 degrees south to 84 degrees north.')
    zone = int((lon + 180.0) // 6) % 60 + 1
    north = lat >= 0
    utm = _convert(QgsPointXY(lon, lat), wgs84(), utm_crs(zone, north))
    columns = _COLUMN_SETS[(zone - 1) % 3]
    col = columns[int(utm.x() // 100000.0) - 1]
    row = _ROW_LETTERS[(int(utm.y() // 100000.0)
                        + (5 if zone % 2 == 0 else 0)) % 20]
    scale = 10 ** (5 - digits)
    e = int((utm.x() % 100000.0) // scale)
    n = int((utm.y() % 100000.0) // scale)
    tail = '{:0{w}d}{:0{w}d}'.format(e, n, w=digits) if digits else ''
    return '{}{}{}{}{}'.format(zone, band_letter(lat), col, row, tail)


# ----------------------------------------------------------- combined pair

def split_pair(text, unit):
    """One box holding both values -> (x_text, y_text).

    Angles are written latitude first, the way web maps copy them
    (``11.5601, 104.9151``); a hemisphere letter on either value overrides the
    order, and a first value that cannot be a latitude while the second can is
    taken as longitude first. Metres, feet and UTM are ``X, Y``."""
    raw = (text or '').strip().strip('()[]{}').strip()
    if not raw:
        raise GoToError('Type both values, for example {}.'.format(
            COMBINED_EXAMPLES.get(unit, '')))
    parts = re.split(r'\s*[,;	]\s*', raw)
    parts = [p for p in parts if p]
    if len(parts) == 1:
        if unit == 'dd' or unit not in ANGLES:
            parts = raw.split()
        else:                       # split after the first hemisphere letter
            parts = re.split(r'(?<=[NSEWnsew])\s+', raw, maxsplit=1)
    if len(parts) != 2:
        raise GoToError('Type exactly two values, for example {}.'.format(
            COMBINED_EXAMPLES.get(unit, '')))
    a, b = (p.strip() for p in parts)
    if unit not in ANGLES:
        return a, b                 # X, Y
    ha = re.findall(r'[NSEWnsew]', a)
    hb = re.findall(r'[NSEWnsew]', b)
    first_is_lon = bool((ha and ha[0].upper() in 'EW')
                        or (hb and hb[0].upper() in 'NS'))
    if not ha and not hb:
        try:
            first_is_lon = (abs(parse_angle(a, 'x')) > 90
                            and abs(parse_angle(b, 'x')) <= 90)
        except GoToError:
            first_is_lon = False
    return (a, b) if first_is_lon else (b, a)     # (x = lon, y = lat)


# ------------------------------------------------------------- the entry

def linear_to_map(value_x, value_y, unit, map_crs):
    """X and Y in metres / feet / US feet -> the map CRS's own units."""
    if map_crs.isGeographic():
        raise GoToError(
            'The map uses degrees, so {} do not apply. Choose a degree, UTM '
            'or MGRS unit, or change the map\'s coordinate system.'.format(
                label_of(unit)))
    crs_metres = QgsUnitTypes.fromUnitToUnitFactor(
        map_crs.mapUnits(), Qgis.DistanceUnit.Meters)
    factor = METRES_PER[unit] / crs_metres
    return QgsPointXY(value_x * factor, value_y * factor)


def locate(unit, x_text, y_text='', zone_text='', map_crs=None,
           combined=False):
    """The :class:`Place` the boxes name. Raises :class:`GoToError`.

    With ``combined`` the X box holds both values (see :func:`split_pair`)."""
    if combined and unit in PAIRABLE:
        x_text, y_text = split_pair(x_text, unit)
    if map_crs is None:
        map_crs = QgsProject.instance().crs()
    if unit in GRIDS:
        return mgrs_to_place(x_text, map_crs)
    if unit in ANGLES:
        lon = parse_angle(x_text, 'x')
        lat = parse_angle(y_text, 'y')
        return _make_place(lat, lon, map_crs)
    if unit == 'utm':
        zone, north = parse_zone(zone_text)
        return utm_to_place(parse_number(x_text, 'X (easting)'),
                            parse_number(y_text, 'Y (northing)'),
                            zone, north, map_crs)
    if unit in PROJECTED:
        x = parse_number(x_text, 'X')
        y = parse_number(y_text, 'Y')
        point = linear_to_map(x, y, unit, map_crs)
        ll = _convert(point, map_crs, wgs84())
        if abs(ll.y()) > 90.0 or abs(ll.x()) > 180.0:
            raise GoToError('That X, Y is outside the area the map\'s '
                            'coordinate system covers ({}).'.format(
                                map_crs.authid()))
        return _make_place(ll.y(), ll.x(), map_crs, point)
    raise GoToError('Unknown unit "{}".'.format(unit))


# ----------------------------------------------------------------- search

#: OpenStreetMap's Nominatim. Its usage policy asks for one request at a time,
#: a real User-Agent (net.user_agent) and no search-as-you-type, so a search
#: runs only when the user asks for it.
SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
SEARCH_LIMIT = 8
#: A found area wider than this (degrees) is framed by its box; anything
#: smaller is shown as a point at the usual scale.
AREA_MIN_SIDE = 0.005


class Found(object):
    """One place the search returned."""

    def __init__(self, label, lat, lon, box=None, title='', detail=''):
        self.label = label
        self.lat = lat
        self.lon = lon
        self.box = box                          # QgsRectangle, WGS 84, or None
        #: The two halves of a suggestion row: the name, then where it is.
        self.title = title or label
        self.detail = detail

    def to_place(self, map_crs):
        place = _make_place(self.lat, self.lon, map_crs)
        box = self.box
        if box is not None and (box.width() >= AREA_MIN_SIDE
                                or box.height() >= AREA_MIN_SIDE):
            place.extent = box
        return place


def parse_search(payload):
    """The places in a Nominatim ``jsonv2`` answer."""
    found = []
    for item in payload if isinstance(payload, list) else []:
        try:
            lat, lon = float(item['lat']), float(item['lon'])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(lat) > 90 or abs(lon) > 180:
            continue
        box = None
        try:
            south, north, west, east = (float(v)
                                        for v in item['boundingbox'])
            box = QgsRectangle(west, south, east, north)
        except (KeyError, TypeError, ValueError):
            pass
        label = str(item.get('display_name') or item.get('name') or
                    '{:.5f}, {:.5f}'.format(lat, lon))
        found.append(Found(label, lat, lon, box))
    return found


def search_places(text, fetch=None, limit=SEARCH_LIMIT):
    """Places matching ``text``, best first. Raises :class:`GoToError`.

    ``fetch(url) -> parsed JSON`` is for tests."""
    query = (text or '').strip()
    if not query:
        raise GoToError('Type a place to search for, for example '
                        '{}.'.format(EXAMPLES['search'][0]))
    url = '{}?{}'.format(SEARCH_URL, urllib.parse.urlencode({
        'q': query, 'format': 'jsonv2', 'limit': limit}))
    try:
        payload = (fetch or net.get_json)(url)
    except net.NetError as exc:
        raise GoToError('The place search is not reachable: {}'.format(exc))
    return parse_search(payload)


# ------------------------------------------------------------- suggestions

#: Photon (photon.komoot.io), an OpenStreetMap geocoder built for search as
#: you type. Nominatim, used for a full search, forbids that.
SUGGEST_URL = 'https://photon.komoot.io/api/'
SUGGEST_LIMIT = 6
SUGGEST_MIN_CHARS = 3


def parse_suggestions(payload):
    """The places in a Photon GeoJSON answer, as :class:`Found`."""
    found = []
    features = payload.get('features') if isinstance(payload, dict) else None
    for feature in features or []:
        try:
            lon, lat = (float(v) for v in feature['geometry']['coordinates'])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(lat) > 90 or abs(lon) > 180:
            continue
        props = feature.get('properties') or {}
        street = ' '.join(str(props[k]) for k in ('housenumber', 'street')
                          if props.get(k))
        title = str(props.get('name') or street or '').strip()
        if not title:
            continue
        where = []
        for value in (street if title != street else '',
                      props.get('district'), props.get('city'),
                      props.get('county'), props.get('state'),
                      props.get('country')):
            value = str(value or '').strip()
            if value and value != title and value not in where:
                where.append(value)
        detail = ', '.join(where)
        box = None
        try:
            west, north, east, south = (float(v) for v in props['extent'])
            box = QgsRectangle(west, south, east, north)
        except (KeyError, TypeError, ValueError):
            pass
        found.append(Found(', '.join(filter(None, (title, detail))), lat,
                           lon, box, title, detail))
    return found


def suggest_places(text, bias=None, fetch=None, limit=SUGGEST_LIMIT):
    """Places that match what is typed so far, best first.

    ``bias`` is (lat, lon): places near it rank higher. ``fetch(url) -> parsed
    JSON`` is for tests. Raises :class:`GoToError`."""
    query = (text or '').strip()
    if len(query) < SUGGEST_MIN_CHARS:
        return []
    params = {'q': query, 'limit': limit, 'lang': 'en'}
    if bias is not None:
        params['lat'] = '{:.4f}'.format(bias[0])
        params['lon'] = '{:.4f}'.format(bias[1])
    url = '{}?{}'.format(SUGGEST_URL, urllib.parse.urlencode(params))
    try:
        payload = (fetch or net.get_json)(url)
    except net.NetError as exc:
        raise GoToError('The place search is not reachable: {}'.format(exc))
    return parse_suggestions(payload)


# ------------------------------------------------------ Google Maps links

_MAP_LINK = re.compile(
    r'^\s*(?:https?://)?(?:maps\.app\.goo\.gl|goo\.gl/maps|'
    r'(?:www\.|maps\.)?google\.[a-z.]+/maps|maps\.google\.[a-z.]+)',
    re.IGNORECASE)
_NUM = r'(-?\d{1,3}(?:\.\d+)?)'
#: In the order they are trusted: the place's own spot, then the pin of a
#: ``?q=`` search, then a ``/search/lat,lon`` path, then the map's centre.
_COORD_PATTERNS = (
    re.compile(r'!3d' + _NUM + r'!4d' + _NUM),
    re.compile(r'[?&](?:q|query|ll|destination|daddr)=\+?' + _NUM
               + r'\s*,\s*\+?' + _NUM),
    re.compile(r'/(?:search|dir)/(?:[^/@]*/)?\+?' + _NUM + r',\s*\+?' + _NUM
               + r'(?:[/?]|$)'),
    re.compile(r'@' + _NUM + r',' + _NUM),
)
_PLACE_NAME = re.compile(r'/maps/place/([^/@?]+)')


def is_map_link(text):
    """Does this look like a Google Maps address?"""
    return bool(_MAP_LINK.match(text or ''))


def parse_map_url(url):
    """The place a full Google Maps address names, as :class:`Found`, or None.

    Reads the coordinates written in the address; no network."""
    raw = url or ''
    # The EU consent page wraps the real address in ``continue=``.
    wrapped = re.search(r'[?&]continue=(https?[^&\s]+)', raw)
    if wrapped and 'consent.' in raw.split('?')[0]:
        return parse_map_url(unquote(wrapped.group(1)))
    text = unquote_plus(raw)
    for pattern in _COORD_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        lat, lon = float(match.group(1)), float(match.group(2))
        if abs(lat) > 90 or abs(lon) > 180:
            continue
        name = ''
        named = _PLACE_NAME.search(text)
        if named:
            name = named.group(1).strip()
            if re.fullmatch(r'-?[\d.]+,\s*-?[\d.]+', name):
                name = ''
        label = name or '{:.6f}, {:.6f}'.format(lat, lon)
        return Found(label, lat, lon, None, label, '')
    return None


def resolve_map_link(text, final_url=None):
    """The :class:`Found` a pasted Google Maps link points at.

    A full address is read as it stands; a short link (maps.app.goo.gl) is
    followed to the address it redirects to first. ``final_url(url)`` is for
    tests. Raises :class:`GoToError`."""
    url = (text or '').strip()
    if not is_map_link(url):
        raise GoToError('That is not a Google Maps link.')
    if not re.match(r'https?://', url, re.IGNORECASE):
        url = 'https://' + url
    found = parse_map_url(url)
    if found is None:
        try:
            end = (final_url or net.final_url)(url)
        except net.NetError as exc:
            raise GoToError('The Google Maps link could not be opened: '
                            '{}'.format(exc))
        found = parse_map_url(end)
    if found is None:
        raise GoToError('No location was found in that Google Maps link. '
                        'Share a link to a place or a dropped pin.')
    return found

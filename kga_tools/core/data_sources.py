# -*- coding: utf-8 -*-
"""The catalogue of places the Add Open Data tool can pull data from.

Every source is a :class:`DataSource` subclass answering the same four
questions: which extra combo boxes do you need, which admin levels do you have
for this country, what exactly would I be adding, and where is the file. The
dialog knows nothing about geoBoundaries or HDX - it walks this registry. Adding
a sixth portal later is a class in this file, not a change to the UI.

No Qt widgets here. The dialog imports this; this must never import the dialog.

**Attribution is not optional.** Every source in the registry requires it. The
licence travels from here into the details panel, into the layer's metadata and
into the tool's documentation, so a boundary that ends up in a report can be
traced back to who published it.
"""

import json
import os
import re

from urllib.parse import urlsplit, urlunsplit

from .net import (NetError, METADATA_TTL, cached_path, download_to, get_json,
                  is_fresh, read_cached_json, trim_cache, write_cached_json,
                  write_json_atomic)

#: What a source produces. Drives which rows the dialog shows.
KIND_ADMIN = 'admin'
KIND_VECTOR = 'vector'
KIND_BASEMAP = 'basemap'
#: Elevation rasters, asked for by area rather than by country. The sources
#: live in `dem_sources`, which builds on the classes here.
KIND_DEM = 'dem'
#: Classified land-cover rasters for one year, asked for by area like a DEM.
#: The sources live in `landcover_sources`.
KIND_LANDCOVER = 'landcover'

#: How the result is reached: a file to download, or a URI a provider opens live.
ACCESS_DOWNLOAD = 'download'
ACCESS_URI = 'uri'


class SourceItem(object):
    """One concrete thing that can be added: a country, a level, a file.

    Built by :meth:`DataSource.describe` out of whatever the source's metadata
    endpoint said, so the dialog can show the user what they are about to pull
    down - name, year, publisher, licence, feature count, size - before they
    commit to the download.
    """

    def __init__(self, title, cache_key, extension='.geojson',
                 download_url=None, uri=None, provider='ogr',
                 access=ACCESS_DOWNLOAD, licence='', attribution='',
                 publisher='', year='', unit_count=None, size_bytes=None,
                 sublayer=None, notes='', source_url='',
                 filter_fields=None, filter_value=None):
        self.title = title
        self.cache_key = cache_key
        self.extension = extension
        self.download_url = download_url
        self.uri = uri
        self.provider = provider
        self.access = access
        self.licence = licence
        self.attribution = attribution
        self.publisher = publisher
        self.year = year
        self.unit_count = unit_count
        self.size_bytes = size_bytes
        #: For a container (a zip of shapefiles): which layer inside it to take.
        self.sublayer = sublayer
        self.notes = notes
        self.source_url = source_url
        #: For a global file that is cut down to one country on the way in:
        #: the candidate field names holding a country code, and the code to
        #: match. The loader uses whichever of the fields the layer actually
        #: has, so one set of candidates covers files that disagree about
        #: naming. None means take every feature.
        self.filter_fields = filter_fields
        self.filter_value = filter_value
        #: Further (label, value) rows for the details panel, for sources whose
        #: facts do not fit the fields above - a DEM's area and resolution.
        self.extra_rows = []


class Option(object):
    """One source-specific combo box, e.g. geoBoundaries' release type."""

    def __init__(self, key, label, choices, default=None, tooltip=''):
        self.key = key
        self.label = label
        #: [(value, label)] - value is what comes back in the options dict.
        self.choices = choices
        self.default = default if default is not None else choices[0][0]
        self.tooltip = tooltip


class DataSource(object):
    """Base class. Subclasses fill in the four hooks below."""

    id = ''
    label = ''
    kind = KIND_ADMIN
    #: A disabled source is still listed, greyed, with `disabled_note` as its
    #: tooltip - so a planned source is visible rather than secretly missing.
    enabled = True
    disabled_note = ''
    needs_country = True
    attribution = ''
    homepage = ''

    def options(self):
        """Extra combo boxes this source wants. See :class:`Option`."""
        return []

    def wants_country(self, options=None):
        """Whether the country row is meaningful for the current options.

        Static for most sources, but Natural Earth can be asked either for one
        country or for the whole world, and showing a country picker that is
        about to be ignored would be a lie.
        """
        return self.needs_country

    def levels(self, iso3, options=None):
        """Admin levels published for `iso3`, as a sorted list of ints.

        Raises :class:`NetError` when the source cannot be reached. Returns an
        empty list when the source is reachable but publishes nothing for that
        country - the dialog says so rather than showing a dead level combo.
        """
        return []

    def describe(self, iso3, level, options=None):
        """The :class:`SourceItem` for one country and level, or None."""
        raise NotImplementedError

    def fetch(self, item, on_progress=None):
        """Bring `item` to a local file and return the path.

        The default is a cached download: a file already on disk is returned
        untouched, so adding the same boundary twice costs one download and the
        second one works offline. Returns None when the user cancels.
        """
        path = cached_path(self.id, item.cache_key, item.extension)
        if is_fresh(path):
            return path
        if not item.download_url:
            raise NetError('This source did not supply a download address.')
        if not download_to(item.download_url, path, on_progress):
            return None
        trim_cache()
        return path

    def cached_file(self, item):
        """The path `fetch` would return without downloading, if it is there."""
        path = cached_path(self.id, item.cache_key, item.extension)
        return path if is_fresh(path) else None


# --------------------------------------------------------------- geoBoundaries

class GeoBoundariesSource(DataSource):
    """geoBoundaries, from William & Mary's geoLab.

    The one source here that covers essentially every country at every level it
    can find, with the licence of each national source carried through. Its
    ``/ISO3/ALL/`` endpoint answers the level question in a single request and
    hands back the metadata for every level at once, so picking a country costs
    one small GET and changing the level afterwards costs nothing.
    """

    id = 'geoboundaries'
    label = 'geoBoundaries (global, open)'
    kind = KIND_ADMIN
    needs_country = True
    homepage = 'https://www.geoboundaries.org'
    attribution = ('geoBoundaries (Runfola et al. 2020), '
                   'https://www.geoboundaries.org')

    API = 'https://www.geoboundaries.org/api/current/{release}/{iso3}/{level}/'

    def options(self):
        return [
            Option('release', 'Release', [
                ('gbOpen', 'gbOpen - open licence, widest coverage'),
                ('gbHumanitarian', 'gbHumanitarian - UN OCHA sourced'),
                ('gbAuthoritative', 'gbAuthoritative - UN SALB, official'),
            ], tooltip='Which geoBoundaries release series to read. gbOpen '
                       'covers the most countries; the other two are narrower '
                       'but carry official provenance.'),
            Option('geometry', 'Geometry', [
                ('simplified', 'Simplified - smaller, faster'),
                ('full', 'Full detail - every vertex'),
            ], tooltip='Simplified is the right choice for mapping and for '
                       'context layers. Take full detail only when the exact '
                       'boundary line matters, it can be many times larger.'),
        ]

    def _all_levels(self, iso3, release):
        """Every level geoBoundaries publishes for one country, cached.

        One request for the whole country. The payload is small and the
        boundaries behind it are a published release, so a week-old copy is
        still correct - but not older, because the download URLs carry a git
        commit hash that moves between releases.
        """
        key = '{0}_{1}_index'.format(release, iso3)
        path = cached_path(self.id, key, '.json')
        if is_fresh(path, METADATA_TTL):
            cached = read_cached_json(path)
            if cached is not None:
                return cached

        url = self.API.format(release=release, iso3=iso3, level='ALL')
        try:
            payload = get_json(url)
        except NetError as exc:
            if 'HTTP 404' in str(exc):
                write_cached_json(path, [])
                return []
            raise

        if isinstance(payload, dict):
            payload = [payload]
        payload = [entry for entry in (payload or []) if isinstance(entry, dict)]
        write_cached_json(path, payload)
        return payload

    def levels(self, iso3, options=None):
        release = (options or {}).get('release', 'gbOpen')
        found = set()
        for entry in self._all_levels(iso3, release):
            match = re.match(r'ADM(\d)', str(entry.get('boundaryType', '')))
            if match:
                found.add(int(match.group(1)))
        return sorted(found)

    def describe(self, iso3, level, options=None):
        options = options or {}
        release = options.get('release', 'gbOpen')
        simplified = options.get('geometry', 'simplified') == 'simplified'

        wanted = 'ADM{0}'.format(level)
        entry = None
        for candidate in self._all_levels(iso3, release):
            if str(candidate.get('boundaryType', '')) == wanted:
                entry = candidate
                break
        if entry is None:
            return None

        url = (entry.get('simplifiedGeometryGeoJSON') if simplified
               else entry.get('gjDownloadURL'))
        if not url:
            url = entry.get('gjDownloadURL') or entry.get(
                'simplifiedGeometryGeoJSON')

        licence = entry.get('boundaryLicense') or ''
        return SourceItem(
            title='{0} {1}'.format(entry.get('boundaryName') or iso3, wanted),
            cache_key='{0}_{1}_{2}_{3}'.format(
                release, iso3, wanted, 'simp' if simplified else 'full'),
            extension='.geojson',
            download_url=url,
            licence=licence,
            attribution='{0}. Source: {1}. Licence: {2}'.format(
                self.attribution, entry.get('boundarySource') or 'unknown',
                licence or 'see geoBoundaries'),
            publisher=entry.get('boundarySource') or '',
            year=str(entry.get('boundaryYearRepresented') or ''),
            unit_count=_as_int(entry.get('admUnitCount')),
            source_url=entry.get('boundarySourceURL') or self.homepage,
            notes=('Simplified geometry.' if simplified
                   else 'Full-detail geometry - this file can be large.'),
        )


# ------------------------------------------------------------------- HDX / COD

class HdxCodSource(DataSource):
    """OCHA Common Operational Datasets, through the HDX CKAN API.

    These are the boundaries the humanitarian community agrees to use, complete
    with p-codes, which is what makes them worth the extra work: unlike the
    other sources, HDX publishes one zipped shapefile holding every level at
    once, so the level choice happens *inside* the downloaded container rather
    than in the URL. One download serves ADM0 through ADM3.
    """

    id = 'hdx_cod'
    label = 'HDX / OCHA COD-AB (humanitarian, p-coded)'
    kind = KIND_ADMIN
    needs_country = True
    homepage = 'https://data.humdata.org'
    attribution = 'OCHA Common Operational Datasets, via the Humanitarian Data Exchange'

    PACKAGE = 'https://data.humdata.org/api/3/action/package_show?id=cod-ab-{iso3}'
    SEARCH = ('https://data.humdata.org/api/3/action/package_search'
              '?fq=name:cod-ab-{iso3}&rows=1')

    def _package(self, iso3):
        """The CKAN package for one country, cached.

        ``cod-ab-<iso3>`` is a naming convention rather than a guarantee, so a
        404 falls back to a search before giving up.
        """
        slug = iso3.lower()
        path = cached_path(self.id, '{0}_package'.format(slug), '.json')
        if is_fresh(path, METADATA_TTL):
            cached = read_cached_json(path)
            if cached is not None:
                return cached or None

        payload = None
        try:
            payload = get_json(self.PACKAGE.format(iso3=slug))
        except NetError as exc:
            if 'HTTP 404' not in str(exc):
                raise

        if not (payload or {}).get('success'):
            try:
                found = get_json(self.SEARCH.format(iso3=slug))
                results = ((found or {}).get('result') or {}).get('results') or []
                payload = {'success': True, 'result': results[0]} if results else None
            except NetError:
                payload = None

        result = (payload or {}).get('result') if payload else None
        write_cached_json(path, result or {})
        return result

    def _resource(self, package):
        """The zipped shapefile, or the geodatabase if there is no shapefile."""
        resources = (package or {}).get('resources') or []
        for wanted in ('shp', 'geodatabase'):
            for resource in resources:
                fmt = str(resource.get('format') or '').lower()
                url = resource.get('url') or resource.get('download_url')
                if wanted in fmt and url and url.lower().endswith('.zip'):
                    return resource
        return None

    def levels(self, iso3, options=None):
        package = self._package(iso3)
        if not package:
            return []
        return _levels_from_notes(package.get('notes') or '',
                                  package.get('title') or '')

    def describe(self, iso3, level, options=None):
        package = self._package(iso3)
        if not package:
            return None
        resource = self._resource(package)
        if not resource:
            return None

        counts = _counts_from_notes(package.get('notes') or '')
        url = resource.get('url') or resource.get('download_url')
        return SourceItem(
            title='{0} ADM{1}'.format(
                package.get('title') or iso3, level),
            # Keyed by country, not by level: the one zip holds every level, so
            # switching ADM2 to ADM3 must not trigger a second download.
            cache_key='{0}_admin'.format(iso3.lower()),
            extension='.zip',
            download_url=url,
            licence=package.get('license_title') or '',
            attribution='{0}. Licence: {1}'.format(
                self.attribution, package.get('license_title') or 'see HDX'),
            publisher=package.get('dataset_source') or '',
            year=_year_from_dates(package.get('dataset_date') or ''),
            unit_count=counts.get(level),
            size_bytes=_as_int(resource.get('size')),
            sublayer='adm{0}'.format(level),
            source_url='https://data.humdata.org/dataset/{0}'.format(
                package.get('name') or ''),
            notes=('One download carries every admin level; switching level '
                   'afterwards costs nothing.'),
        )


# --------------------------------------------------------------- Natural Earth

class NaturalEarthSource(DataSource):
    """Natural Earth: country and state outlines, worldwide, public domain.

    The files are global, so a country is reached by cutting the global file
    down on the way in rather than by asking for a different download. That is
    a good trade rather than a compromise: one download serves every country,
    and a second country afterwards costs nothing at all.

    The catch is admin 1 coverage, which is not global at every scale and is
    worth knowing before choosing:

    ==========  ========  ==================================================
    ADM1 scale  features  countries
    ==========  ========  ==================================================
    1:110m      51        the United States, and nothing else
    1:50m       294       nine large countries (see ``ADM1_COVERAGE``)
    1:10m       4596      251 - effectively everywhere
    ==========  ========  ==================================================

    So ADM1 is reported unavailable at 1:110m and 1:50m for a country the file
    does not hold, which greys the level out exactly as a missing geoBoundaries
    level does. Admin 0 is complete at all three scales.
    """

    id = 'naturalearth'
    label = 'Natural Earth (worldwide, public domain)'
    kind = KIND_ADMIN
    needs_country = True
    homepage = 'https://www.naturalearthdata.com'
    attribution = 'Made with Natural Earth. Free vector and raster map data @ naturalearthdata.com'

    BASE = ('https://raw.githubusercontent.com/nvkelso/natural-earth-vector/'
            'master/geojson/{name}.geojson')

    #: (level, scale) -> (file name, size in bytes or None when not measured)
    FILES = {
        (0, '10m'): ('ne_10m_admin_0_countries', 13287234),
        (0, '50m'): ('ne_50m_admin_0_countries', 3041210),
        (0, '110m'): ('ne_110m_admin_0_countries', 838758),
        (1, '10m'): ('ne_10m_admin_1_states_provinces', 40726851),
        (1, '50m'): ('ne_50m_admin_1_states_provinces', 2307358),
        (1, '110m'): ('ne_110m_admin_1_states_provinces', 183603),
    }

    #: Which countries the coarser admin 1 files actually contain, measured
    #: from the published files. 1:10m is not listed because it holds 251
    #: countries - everything a user is going to ask for.
    #:
    #: A hint, not a contract: if Natural Earth adds a country to the 1:50m
    #: file this list goes stale and would grey out something that is in fact
    #: there. The empty-result message in the loader is the backstop, and it
    #: names the scale to switch to.
    ADM1_COVERAGE = {
        '110m': ('USA',),
        '50m': ('AUS', 'BRA', 'CAN', 'CHN', 'IDN', 'IND', 'RUS', 'USA', 'ZAF'),
    }

    #: Where a country code hides. Admin 0 spells them upper case and admin 1
    #: lower case, but QGIS matches field names without regard to case, so one
    #: list covers both. ISO_A3 is '-99' for a handful of disputed territories,
    #: which is why ADM0_A3 leads.
    COUNTRY_FIELDS = ('ADM0_A3', 'ISO_A3_EH', 'ISO_A3', 'SOV_A3', 'GU_A3')

    def options(self):
        return [
            Option('extent', 'Extent', [
                ('country', 'Selected country only (default)'),
                ('world', 'Whole world - every country'),
            ], tooltip='The files are worldwide. "Selected country only" cuts '
                       'the download down to the country chosen above as it '
                       'is loaded; the download itself is the same either '
                       'way, and is cached.'),
            Option('scale', 'Scale', [
                ('10m', '1:10m - large scale, full admin 1 coverage'),
                ('50m', '1:50m - medium detail'),
                ('110m', '1:110m - small scale, smallest file'),
            ], tooltip='Natural Earth publishes the same layer at three '
                       'generalisations. Admin 1 is only worldwide at 1:10m; '
                       'the coarser two hold a handful of large countries.'),
        ]

    def wants_country(self, options=None):
        return (options or {}).get('extent', 'country') == 'country'

    def levels(self, iso3, options=None):
        options = options or {}
        scale = options.get('scale', '10m')
        # Admin 0 is complete at every scale, and no request is needed to know
        # any of this - the answer is a property of the published files.
        if options.get('extent', 'country') == 'world':
            return [0, 1]
        covered = self.ADM1_COVERAGE.get(scale)
        if covered is None or (iso3 or '').upper() in covered:
            return [0, 1]
        return [0]

    def describe(self, iso3, level, options=None):
        options = options or {}
        scale = options.get('scale', '10m')
        whole_world = options.get('extent', 'country') == 'world'

        entry = self.FILES.get((level, scale))
        if entry is None:
            return None
        name, size = entry
        iso3 = (iso3 or '').upper()

        if whole_world:
            title = 'Natural Earth {0} admin {1}'.format(scale, level)
            notes = ('Every country, not just the one above. Natural Earth '
                     'admin 1 is only worldwide at 1:10m.')
            fields, value = None, None
        else:
            title = '{0} ADM{1} (Natural Earth {2})'.format(
                _country_name(iso3), level, scale)
            notes = ('Cut from the worldwide file as it loads. The download is '
                     'the same for every country and is cached, so the next '
                     'country costs nothing.')
            fields, value = self.COUNTRY_FIELDS, iso3

        return SourceItem(
            title=title,
            # Keyed by the file, not the country: one download, every country.
            cache_key=name,
            extension='.geojson',
            download_url=self.BASE.format(name=name),
            licence='Public domain',
            attribution=self.attribution,
            publisher='Natural Earth',
            unit_count=None,
            size_bytes=size,
            source_url=self.homepage,
            notes=notes,
            filter_fields=fields,
            filter_value=value,
        )


# ------------------------------------------------------------------ custom URL

class CustomUrlSource(DataSource):
    """Anything else with a public address.

    Three shapes, told apart by the address itself: an ArcGIS REST layer, an
    OGC WFS endpoint, or a plain file. This is the escape hatch that keeps the
    tool useful for the national portal that nobody has written a class for.
    """

    id = 'custom_url'
    label = 'Custom URL or API (GeoJSON, ArcGIS REST, WFS)'
    kind = KIND_VECTOR
    needs_country = False
    attribution = 'Supplied by the user'

    #: An ArcGIS REST *layer*, i.e. one ending in a layer index.
    ARCGIS = re.compile(r'/(?:Feature|Map)Server/(\d+)/?$', re.IGNORECASE)
    #: How many pages of an ArcGIS query to walk before giving up. At the usual
    #: 1000-2000 records a page this is a very large dataset already.
    MAX_PAGES = 50

    def levels(self, iso3, options=None):
        return []

    def describe_url(self, url):
        """Work out what a pasted address is, without fetching it."""
        url = (url or '').strip()
        if not url:
            return None
        if not url.lower().startswith(('http://', 'https://')):
            raise NetError('Enter a web address starting with https://. Local '
                           'files are opened with Layer Export / Import '
                           'instead.')

        name = _name_from_url(url)

        if self.ARCGIS.search(url):
            return SourceItem(
                title=name,
                cache_key=_hash_key(url),
                extension='.geojson',
                download_url=url,
                licence='Set by the publisher of the service',
                attribution='Downloaded from {0}'.format(_public_url(url)),
                source_url=_public_url(url),
                notes=('ArcGIS REST layer. The features are requested as '
                       'GeoJSON and paged through, so a service that caps each '
                       'reply at 1000 records still arrives complete.'),
            )

        if _looks_like_wfs(url):
            return SourceItem(
                title=name,
                cache_key=_hash_key(url),
                uri=url if 'url=' in url else "url='{0}'".format(url),
                provider='WFS',
                access=ACCESS_URI,
                licence='Set by the publisher of the service',
                attribution='Served from {0}'.format(_public_url(url)),
                source_url=_public_url(url),
                notes=('WFS endpoint. QGIS reads it live rather than '
                       'downloading it, so this one is not a scratch layer.'),
            )

        return SourceItem(
            title=name,
            cache_key=_hash_key(url),
            extension=_extension_from_url(url),
            download_url=url,
            licence='Set by the publisher of the file',
            attribution='Downloaded from {0}'.format(_public_url(url)),
            source_url=_public_url(url),
            notes='Downloaded and opened with GDAL/OGR.',
        )

    def describe(self, iso3, level, options=None):
        return self.describe_url((options or {}).get('url'))

    def fetch(self, item, on_progress=None):
        if not self.ARCGIS.search(item.download_url or ''):
            return DataSource.fetch(self, item, on_progress)
        return self._fetch_arcgis(item, on_progress)

    def _fetch_arcgis(self, item, on_progress=None):
        """Page an ArcGIS REST layer into one GeoJSON file.

        Services cap a single reply at 1000 or 2000 records and flag it with
        ``exceededTransferLimit``. Taking the first page only would silently
        hand back a truncated layer, which is worse than failing, so this walks
        the offsets until the service says there is no more.
        """
        path = cached_path(self.id, item.cache_key, '.geojson')
        if is_fresh(path):
            return path

        base = item.download_url.rstrip('/')
        features = []
        offset = 0
        previous_first = None
        complete = False
        for _page in range(self.MAX_PAGES):
            url = ('{0}/query?where=1%3D1&outFields=*&outSR=4326&f=geojson'
                   '&returnGeometry=true&resultOffset={1}'.format(base, offset))
            payload = get_json(url)

            if not isinstance(payload, dict):
                raise NetError('The service did not answer with a feature '
                               'collection.')
            if payload.get('error'):
                message = (payload['error'].get('message')
                           if isinstance(payload['error'], dict) else '')
                raise NetError('The service rejected the query: {0}'.format(
                    message or 'no reason given'))

            page = payload.get('features') or []
            if page:
                first = json.dumps(page[0], sort_keys=True)
                if first == previous_first:
                    # A server that ignores resultOffset hands back the same
                    # page for ever; stacking fifty copies of it would be a
                    # worse layer than an honest refusal.
                    raise NetError('The service keeps returning the same '
                                   'records, so it cannot be read in pages. '
                                   'Try a different layer address.')
                previous_first = first
            features.extend(page)
            if on_progress is not None:
                if on_progress(len(features), 0) is False:
                    return None
            if not page or not _exceeded_transfer_limit(payload):
                complete = True
                break
            offset += len(page)

        if not complete:
            raise NetError('The service has more than {0:,} records, which is '
                           'more than this tool will page through in one '
                           'go.'.format(len(features)))
        if not features:
            raise NetError('The service returned no features.')

        write_json_atomic(path, {'type': 'FeatureCollection',
                                 'features': features})
        trim_cache()
        return path


# ------------------------------------------------------------ KGA database

class KgaDatabaseSource(DataSource):
    """The KGA PostgreSQL store. The seat, not yet the connection.

    Listed and disabled on purpose: the source list is where a user goes
    looking for it, and an entry that says "not yet" is better than an absence
    that reads as "never". When the server exists, this class implements the
    same four hooks as the others and nothing in the dialog changes.

    The intended shape, so the next pass does not have to rediscover it::

        md = QgsProviderRegistry.instance().providerMetadata('postgres')
        conn = md.findConnection(<name from QgsSettings>)   # QgsAbstractDatabaseProviderConnection
        conn.tables(schema, QgsAbstractDatabaseProviderConnection.TableFlag.Vector)
        uri = QgsDataSourceUri(conn.uri()); uri.setDataSource(schema, table, 'geom')

    Credentials stay in the QGIS authentication database - this plugin must
    never hold a password of its own.
    """

    id = 'kga_database'
    label = 'KGA Database (PostgreSQL)'
    kind = KIND_ADMIN
    enabled = False
    disabled_note = ('Not connected yet. A future release will read the KGA '
                     'PostgreSQL store through the QGIS connection you already '
                     'have configured.')
    needs_country = True

    def levels(self, iso3, options=None):
        return []

    def describe(self, iso3, level, options=None):
        raise NotImplementedError(self.disabled_note)


# ------------------------------------------------------------------- basemaps

class Basemap(object):
    """One XYZ tile service."""

    def __init__(self, id, label, url, zmin=0, zmax=19, attribution=''):
        self.id = id
        self.label = label
        self.url = url
        self.zmin = zmin
        self.zmax = zmax
        self.attribution = attribution


#: The tile services the tool offers, in the order the combo lists them.
#:
#: This is the list KGA asked for, matching what people here actually use. Two
#: things to know about it:
#:
#: * **The Google entries are the user's call, made explicitly.** Google's tile
#:   endpoints are not licensed for consumption outside Google's own APIs and
#:   SDKs, so whether a particular use is permitted is a question for whoever
#:   makes the map, not for this file. They are included because they were
#:   asked for; the documentation says the same thing in the open rather than
#:   leaving it implied.
#: * **A Google URL carries query parameters**, which is why
#:   `layer_loader.xyz_uri` percent-encodes the whole address. The provider URI
#:   is itself ampersand separated, so an unencoded `lyrs=s&x={x}` would lose
#:   everything after the first `&` and the layer would come up empty.
#:
#: `zmax` is the deepest level the service actually serves. Setting it honestly
#: lets QGIS scale the last real level up when the user zooms past it, which
#: looks soft but works; setting it too high makes the canvas go blank on 404s.
BASEMAPS = (
    Basemap('osm', 'OpenStreetMap',
            'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
            0, 19, 'OpenStreetMap contributors, ODbL'),
    Basemap('google_satellite', 'Google Satellite',
            'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
            0, 21, 'Imagery (c) Google'),
    Basemap('google_hybrid', 'Google Satellite Hybrid',
            'https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
            0, 21, 'Imagery and map data (c) Google'),
    Basemap('google_terrain', 'Google Terrain',
            'https://mt1.google.com/vt/lyrs=t&x={x}&y={y}&z={z}',
            0, 18, 'Terrain data (c) Google'),
    Basemap('google_terrain_hybrid', 'Google Terrain Hybrid',
            'https://mt1.google.com/vt/lyrs=p&x={x}&y={y}&z={z}',
            0, 18, 'Terrain data and map data (c) Google'),
    Basemap('google_map', 'Google Map',
            'https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',
            0, 20, 'Map data (c) Google'),
    Basemap('esri_topo', 'ESRI Topography',
            'https://services.arcgisonline.com/ArcGIS/rest/services/'
            'World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
            0, 19, 'Esri and the GIS community'),
    Basemap('esri_imagery', 'ESRI Imagery',
            'https://server.arcgisonline.com/ArcGIS/rest/services/'
            'World_Imagery/MapServer/tile/{z}/{y}/{x}',
            0, 19, 'Esri, Maxar, Earthstar Geographics and the GIS community'),
    Basemap('esri_natgeo', 'ESRI National Geographic',
            'https://services.arcgisonline.com/ArcGIS/rest/services/'
            'NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}',
            0, 16, 'National Geographic, Esri, DeLorme, HERE, UNEP-WCMC, '
                   'USGS, NASA, ESA, METI, NRCAN, GEBCO, NOAA, iPC'),
    Basemap('esri_dark_grey', 'ESRI Grey (Dark)',
            'https://services.arcgisonline.com/ArcGIS/rest/services/'
            'Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
            0, 16, 'Esri, HERE, Garmin, (c) OpenStreetMap contributors'),
    Basemap('esri_light_grey', 'ESRI Grey (Light)',
            'https://services.arcgisonline.com/ArcGIS/rest/services/'
            'Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
            0, 16, 'Esri, HERE, Garmin, (c) OpenStreetMap contributors'),
)

#: The id the dialog uses for "let me paste my own tile URL".
CUSTOM_BASEMAP = 'custom_xyz'


def basemap_by_id(basemap_id):
    for basemap in BASEMAPS:
        if basemap.id == basemap_id:
            return basemap
    return None


# ------------------------------------------------------------------- registry

#: Order is the order the source combo lists them: the two that cover a whole
#: country properly first, then the global fallback, then the escape hatches.
SOURCES = (
    GeoBoundariesSource(),
    HdxCodSource(),
    NaturalEarthSource(),
    CustomUrlSource(),
    KgaDatabaseSource(),
)


def sources_for(kind):
    """Every source that produces `kind`, in registry order."""
    if kind == KIND_ADMIN:
        return [s for s in SOURCES if s.kind == KIND_ADMIN]
    if kind == KIND_VECTOR:
        # Deliberately not the admin sources as well: offering geoBoundaries
        # under "other vector data" would put the same country-and-level form
        # behind two different data types, which reads as a bug.
        return [s for s in SOURCES if s.kind == KIND_VECTOR]
    if kind == KIND_DEM:
        return list(_dem_sources())
    if kind == KIND_LANDCOVER:
        return list(_landcover_sources())
    return []


def source_by_id(source_id):
    for source in (SOURCES + tuple(_dem_sources())
                   + tuple(_landcover_sources())):
        if source.id == source_id:
            return source
    return None


def _dem_sources():
    """The DEM registry, imported late: `dem_sources` subclasses the classes
    in this module, so importing it at the top would be circular."""
    from .dem_sources import DEM_SOURCES

    return DEM_SOURCES


def _landcover_sources():
    """The land-cover registry, imported late for the same reason."""
    from .landcover_sources import LANDCOVER_SOURCES

    return LANDCOVER_SOURCES


# ------------------------------------------------------------------- helpers

def _country_name(iso3):
    """'KHM' -> 'Cambodia', for a layer title. Imported late to keep the
    country table out of the import path of anything that does not need it."""
    from .countries import name_for

    return name_for(iso3)


def _exceeded_transfer_limit(payload):
    """True when an ArcGIS reply says there are more records than it holds.

    The flag is a top-level member of an ``f=json`` reply but sits under
    ``properties`` in ``f=geojson``, which is the one this tool asks for. Reading
    only the first would take the first page of every large layer for all of it.
    """
    if payload.get('exceededTransferLimit'):
        return True
    properties = payload.get('properties')
    return isinstance(properties, dict) and bool(
        properties.get('exceededTransferLimit'))


def _public_url(url):
    """`url` without credentials or a query string, for what gets written down.

    A pasted address often carries a token (``?token=...``) or a user name and
    password. The layer's attribution and source URL travel with the project
    and with every export of it, so they get the address, not the secret.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        if parts.port:
            host = '{0}:{1}'.format(host, parts.port)
        return urlunsplit((parts.scheme, host, parts.path, '', ''))
    except ValueError:
        return ''


def _as_int(value):
    try:
        return int(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _levels_from_notes(notes, title):
    """Which admin levels a COD dataset covers.

    HDX states this twice and in prose: once as a range in the description
    ("administrative level 0-3 boundaries") and once as a list of per-level
    counts. Both are read, because neither is guaranteed to be there.
    """
    levels = set()

    span = re.search(r'level[s]?\s*(\d)\s*[-–]\s*(\d)',
                     notes + ' ' + title, re.IGNORECASE)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        levels.update(range(min(low, high), max(low, high) + 1))

    levels.update(_counts_from_notes(notes).keys())
    if levels:
        # ADM0 is the country outline; COD always carries it even when the
        # per-level counts start at 1.
        levels.add(0)
    return sorted(levels)


def _counts_from_notes(notes):
    """{1: 25, 2: 197, 3: 1633} out of the '- Admin 1: 25' lines."""
    counts = {}
    for match in re.finditer(r'Admin\s*(\d)\s*:\s*([\d,]+)', notes or '',
                             re.IGNORECASE):
        value = _as_int(match.group(2))
        if value is not None:
            counts[int(match.group(1))] = value
    return counts


def _year_from_dates(dataset_date):
    match = re.search(r'(\d{4})-\d{2}-\d{2}', dataset_date or '')
    return match.group(1) if match else ''


def _looks_like_wfs(url):
    lowered = url.lower()
    return ('service=wfs' in lowered or lowered.rstrip('/').endswith('/wfs')
            or 'wfs?' in lowered)


def _name_from_url(url):
    """A first guess at a layer name, from the tail of the address."""
    tail = url.split('?')[0].rstrip('/').split('/')[-1]
    tail = re.sub(r'\.(geo)?json$|\.zip$|\.gpkg$|\.kml$', '', tail,
                  flags=re.IGNORECASE)
    tail = tail.replace('_', ' ').replace('%20', ' ').strip()
    return tail or 'Downloaded layer'


def _extension_from_url(url):
    tail = os.path.splitext(url.split('?')[0])[1].lower()
    known = ('.geojson', '.json', '.zip', '.gpkg', '.kml', '.kmz', '.gml',
             '.csv')
    return tail if tail in known else '.geojson'


def _hash_key(url):
    """A stable, short cache key for an arbitrary address."""
    import hashlib
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
    return '{0}_{1}'.format(_name_from_url(url)[:40].replace(' ', '_'), digest)

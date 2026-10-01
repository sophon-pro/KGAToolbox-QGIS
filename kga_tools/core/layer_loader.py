# -*- coding: utf-8 -*-
"""Turning what a data source handed back into a layer on the map.

Two jobs, and the difference between them is the whole design of the tool:

**Vector data becomes a temporary scratch layer.** The features are copied into
memory and the file on disk is left in the cache, untouched and unreferenced.
That is deliberate. This tool reaches out to somebody else's server for
reference data; it has no business deciding where a file lands in the user's
project folder, and a layer whose source is a cache entry would break the day
the cache is trimmed. A scratch layer is honest about what it is, QGIS marks it
in the Layers panel, and the tool says plainly that it must be exported to keep.

**A basemap becomes an XYZ raster layer.** That one is a URL, not data, so it
saves into the project file normally and needs no warning.
"""

from contextlib import suppress

from qgis.core import (QgsCoordinateReferenceSystem, QgsDataSourceUri,
                       QgsFeatureRequest, QgsProviderRegistry, QgsRasterLayer,
                       QgsVectorLayer)

#: QGIS stores Browser XYZ entries under this settings prefix.
XYZ_SETTINGS = 'qgis/connections-xyz'


class LoadError(Exception):
    """A file arrived but could not be turned into a layer."""


def sublayers(path):
    """Every layer inside a container, as (name, uri) pairs.

    Given the plain path to the container - a COD-AB download is a zip of
    shapefiles - the registry finds the layers inside and hands back URIs
    already decorated with ``/vsizip/`` and ``|layername=``. Prefixing the path
    by hand first also works, but makes GDAL log an error on the way, so the
    registry is left to do it.
    """
    found = []
    for detail in QgsProviderRegistry.instance().querySublayers(path):
        try:
            found.append((detail.name(), detail.uri()))
        except AttributeError:              # pragma: no cover - old QGIS
            continue
    return found


def pick_sublayer(path, hint):
    """The URI of the layer inside `path` holding admin level `hint`.

    `hint` is a token like ``adm2``; only its trailing digit really matters.
    COD-AB publishers do not agree on a filename convention - Cambodia ships
    ``khm_admin2.shp`` while other countries ship
    ``xxx_admbnda_adm2_gov_20181004`` - so both spellings are matched, along
    with ``admin_2`` and ``ADM2``.

    The trailing ``(?![0-9])`` is what keeps ADM1 from matching an ADM10 layer,
    and requiring a boundary in front is what keeps ``adm2`` from matching
    inside a longer word.
    """
    import re

    entries = sublayers(path)
    if not entries:
        raise LoadError('The downloaded file does not contain any layer this '
                        'version of QGIS can read.')

    if not hint:
        return entries[0][1]

    digits = re.sub(r'\D', '', str(hint))
    if not digits:
        return entries[0][1]

    pattern = re.compile(
        r'(?:^|[^a-z0-9])adm(?:in)?[_-]?{0}(?![0-9])'.format(digits),
        re.IGNORECASE)
    for name, uri in entries:
        if pattern.search(name):
            return uri

    available = ', '.join(name for name, _uri in entries[:8])
    raise LoadError('The download does not contain an ADM{0} layer. It holds: '
                    '{1}.'.format(digits, available))


def country_filter(layer, fields, value):
    """An expression selecting one country, or None when it cannot be built.

    `fields` is a list of candidate column names; only those the layer really
    has are used, because the files disagree - Natural Earth's admin 0 carries
    ISO_A3 and its admin 1 does not. They are OR-ed rather than ranked because
    ISO_A3 is '-99' for a few disputed territories that ADM0_A3 still names
    correctly.

    Field names are compared without regard to case, which is also how QGIS
    resolves them in an expression: admin 0 spells them upper case, admin 1
    lower.
    """
    if not fields or not value:
        return None

    present = {field.name().lower(): field.name() for field in layer.fields()}
    terms = []
    for candidate in fields:
        actual = present.get(candidate.lower())
        if actual:
            terms.append('"{0}" = \'{1}\''.format(
                actual.replace('"', '""'), str(value).replace("'", "''")))
    return ' OR '.join(terms) if terms else None


def to_temp_layer(path, name, sublayer_hint=None, filter_fields=None,
                  filter_value=None):
    """Open a downloaded file and copy it into a temporary scratch layer.

    ``materialize`` is what does the copying: it runs the request against the
    source and hands back an in-memory layer holding the result. After this
    returns, the layer owes the file on disk nothing.

    When `filter_fields` and `filter_value` are given, the request carries a
    country filter, so a worldwide file arrives as one country. The cut happens
    on the way into memory rather than afterwards, so the scratch layer only
    ever holds what was asked for.
    """
    if sublayer_hint:
        uri = pick_sublayer(path, sublayer_hint)
    else:
        # Even without a hint, go through the registry: a plain .geojson comes
        # back as itself, and a container comes back already addressed.
        entries = sublayers(path)
        uri = entries[0][1] if entries else path

    source = QgsVectorLayer(uri, name, 'ogr')
    if not source.isValid():
        # A single-layer file sometimes needs no decoration at all.
        source = QgsVectorLayer(path, name, 'ogr')
    if not source.isValid():
        raise LoadError('The downloaded file could not be opened as a vector '
                        'layer. It may be in a format GDAL does not recognise, '
                        'or the download may be incomplete - try Refresh.')

    if source.featureCount() == 0:
        raise LoadError('The downloaded layer is empty.')

    request = QgsFeatureRequest()
    expression = country_filter(source, filter_fields, filter_value)
    if filter_value and expression is None:
        raise LoadError(
            'The downloaded layer has no country column to filter on, so it '
            'cannot be cut down to one country. Ask for the whole world '
            'instead.')
    if expression:
        request.setFilterExpression(expression)

    layer = source.materialize(request)
    if layer is None or not layer.isValid():
        raise LoadError('The layer could not be copied into memory.')

    if expression and layer.featureCount() == 0:
        # The backstop for a stale coverage table: the file was fine, it just
        # does not hold this country. Saying which is far more use than an
        # empty layer in the panel.
        raise LoadError(
            'The source has no features for {0} in this layer. Natural '
            'Earth\'s admin 1 boundaries are only worldwide at 1:10m - the '
            'coarser scales hold only a few large countries. Try 1:10m, or '
            'another source.'.format(filter_value))

    layer.setName(name)
    return layer


def open_uri_layer(uri, name, provider):
    """A live layer served by a provider, e.g. WFS. Not a scratch layer."""
    layer = QgsVectorLayer(uri, name, provider)
    if not layer.isValid():
        raise LoadError('QGIS could not connect to that service. Check the '
                        'address, and that the service is reachable.')
    layer.setName(name)
    return layer


def tag_provenance(layer, item):
    """Write the source's licence and address into the layer's metadata.

    So that attribution survives the trip: a layer exported to GeoPackage and
    mailed to somebody else still carries who published it and under what terms.
    """
    if item is None:
        return
    # Provenance is worth attempting and never worth failing an add over.
    with suppress(Exception):               # pragma: no cover - API drift
        metadata = layer.metadata()
        if item.attribution:
            metadata.setRights([item.attribution])
        if item.licence:
            metadata.setLicenses([item.licence])
        if item.source_url:
            metadata.setIdentifier(item.source_url)
        if item.title:
            metadata.setTitle(item.title)
        layer.setMetadata(metadata)


def xyz_uri(url, zmin=0, zmax=19):
    """The provider URI for an XYZ tile service.

    Built with ``QgsDataSourceUri`` rather than by string formatting, because
    the escaping here is narrower than it looks and getting it wrong fails
    quietly. The URI is itself ``&``-separated ``key=value`` pairs, so a tile
    URL carrying a query string - ``lyrs=s&x={x}&y={y}&z={z}`` - must have its
    ``=``, ``&``, ``{`` and ``}`` escaped or the url parameter ends at the
    first ``&``.

    What must *not* be escaped is the rest of the address. Percent-encoding the
    whole URL turns ``https://`` into ``https%3A%2F%2F``, which the provider
    passes through literally: the layer still reports ``isValid()`` and still
    draws nothing, which is the worst way for this to be wrong. Handing the raw
    URL to ``QgsDataSourceUri`` produces exactly the string QGIS's own XYZ
    connection produces, escaping the delimiters and leaving the scheme alone.
    """
    uri = QgsDataSourceUri()
    uri.setParam('type', 'xyz')
    uri.setParam('url', url)
    uri.setParam('zmin', str(zmin))
    uri.setParam('zmax', str(zmax))
    return bytes(uri.encodedUri()).decode('utf-8')


def build_basemap(url, name, zmin=0, zmax=19):
    """An XYZ raster layer, ready to add."""
    layer = QgsRasterLayer(xyz_uri(url, zmin, zmax), name, 'wms')
    if not layer.isValid():
        raise LoadError('That tile service could not be opened. Check that the '
                        'address contains the {z}/{x}/{y} placeholders.')
    layer.setCrs(QgsCoordinateReferenceSystem('EPSG:3857'))
    return layer


def remember_xyz(name, url, zmin=0, zmax=19):
    """Add a tile service to the Browser panel's XYZ Tiles list.

    Writes the same settings keys the Browser writes itself, so the entry is
    indistinguishable from one added by hand and outlives the project.
    """
    from qgis.core import QgsSettings

    settings = QgsSettings()
    key = '{0}/{1}'.format(XYZ_SETTINGS, name)
    settings.setValue('{0}/url'.format(key), url)
    settings.setValue('{0}/zmin'.format(key), zmin)
    settings.setValue('{0}/zmax'.format(key), zmax)
    settings.setValue('{0}/authcfg'.format(key), '')
    settings.setValue('{0}/username'.format(key), '')
    settings.setValue('{0}/password'.format(key), '')
    settings.setValue('{0}/referer'.format(key), '')


def xyz_exists(name):
    """True when the Browser already lists a tile service under `name`."""
    from qgis.core import QgsSettings

    return QgsSettings().contains('{0}/{1}/url'.format(XYZ_SETTINGS, name))


def unique_name(project, name):
    """`name`, or 'name (2)' when the project already holds that name.

    Two adds of the same boundary should be distinguishable in the Layers
    panel rather than being two identically named rows.
    """
    existing = {layer.name() for layer in project.mapLayers().values()}
    if name not in existing:
        return name
    index = 2
    while '{0} ({1})'.format(name, index) in existing:
        index += 1
    return '{0} ({1})'.format(name, index)

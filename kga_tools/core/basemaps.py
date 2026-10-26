"""Ready-made XYZ Tiles connections (OSM, Google, ESRI) for the Browser panel.

QGIS keeps XYZ connections in QgsSettings under ``qgis/connections-xyz``, so
registering one is a settings write plus a Browser refresh. Each connection is
added once: a note of what has been offered lives under ``kga_tools/basemaps``,
so a connection the user later edits or deletes is not put back, while basemaps
added in a newer plugin release still appear.

The ESRI vector-tile entries in the source list are left out. They all share
one VectorTileServer URL and differ only by style, which needs an ArcGIS
online account, so they cannot be offered as plain connections.
"""

from urllib.parse import quote

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings
from qgis.PyQt.QtCore import QTimer

XYZ_GROUP = 'qgis/connections-xyz'
OFFERED_KEY = 'kga_tools/basemaps_offered'

_GOOGLE = 'https://mt1.google.com/vt/lyrs={}&x={{x}}&y={{y}}&z={{z}}'
_ESRI = ('https://{host}.arcgisonline.com/ArcGIS/rest/services/{path}'
         '/MapServer/tile/{{z}}/{{y}}/{{x}}')


def _esri(path, host='server'):
    return _ESRI.format(host=host, path=path)


# (name, url, max zoom)
BASEMAPS = (
    ('Osm tile', 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', 19),
    ('Google Satellite', _GOOGLE.format('s'), 20),
    ('Google Satellite Hybrid', _GOOGLE.format('y'), 20),
    ('Google Terrain', _GOOGLE.format('t'), 20),
    ('Google Terrain Hybrid', _GOOGLE.format('p'), 20),
    ('Google Map', _GOOGLE.format('m'), 20),
    ('Google Label', _GOOGLE.format('h'), 20),
    ('Google Traffic',
     'https://mt1.google.com/vt?lyrs=h@159000000,traffic|seconds_into_week:-1'
     '&style=3&x={x}&y={y}&z={z}', 20),
    ('ESRI Topography', _esri('World_Topo_Map', 'services'), 19),
    ('ESRI Imagery', _esri('World_Imagery'), 19),
    ('ESRI National Geographic', _esri('NatGeo_World_Map', 'services'), 16),
    ('ESRI Grey (Dark)', _esri('Canvas/World_Dark_Gray_Base', 'services'), 16),
    ('ESRI Grey (Light)', _esri('Canvas/World_Light_Gray_Base', 'services'), 16),
    ('ESRI Ocean', _esri('Ocean/World_Ocean_Base', 'services'), 13),
    ('ESRI Physical', _esri('World_Physical_Map'), 8),
    ('ESRI Reference overlay', _esri('Reference/World_Reference_Overlay'), 13),
    ('ESRI Hillshade', _esri('World_Shaded_Relief'), 13),
    ('ESRI Terrain', _esri('World_Terrain_Base'), 13),
    ('ESRI Standard', _esri('World_Street_Map'), 19),
    ('ESRI Transportation', _esri('Reference/World_Transportation'), 19),
)


def register_basemaps(settings=None):
    """Add the connections not yet offered; return the names added."""
    s = settings or QgsSettings()
    offered = set(s.value(OFFERED_KEY, [], type=list) or [])
    existing = set(_existing_names(s))
    added = []
    for name, url, zmax in BASEMAPS:
        if name in offered:
            continue
        offered.add(name)
        if name in existing:
            continue
        base = f'{XYZ_GROUP}/{name}'
        s.setValue(f'{base}/url', url)
        s.setValue(f'{base}/zmin', 0)
        s.setValue(f'{base}/zmax', zmax)
        s.setValue(f'{base}/authcfg', '')
        s.setValue(f'{base}/username', '')
        s.setValue(f'{base}/password', '')
        s.setValue(f'{base}/referer', '')
        s.setValue(f'{base}/tilePixelRatio', 0)
        s.setValue(f'{base}/interpretation', '')
        added.append(name)
    s.setValue(OFFERED_KEY, sorted(offered))
    return added


def _existing_names(s):
    s.beginGroup(XYZ_GROUP)
    try:
        return s.childGroups()
    finally:
        s.endGroup()


# Label / reference layers sit above the data; everything else is a background.
OVERLAYS = frozenset({'Google Label', 'Google Traffic',
                      'ESRI Reference overlay', 'ESRI Transportation'})


def provider_of(name):
    return {'Osm': 'OpenStreetMap', 'Goo': 'Google', 'ESR': 'ESRI'}[name[:3]]


def add_basemap_layer(name, project=None, group=None):
    """Add the named basemap to the project; return the layer.

    With a layer tree `group` it goes into that group (a map), at the bottom
    for a background and at the top for an overlay, instead of the root.
    """
    for n, url, zmax in BASEMAPS:
        if n == name:
            break
    else:
        raise KeyError(name)
    project = project or QgsProject.instance()
    uri = 'type=xyz&url={}&zmin=0&zmax={}'.format(
        quote(url, safe=':/{}'), zmax)
    layer = QgsRasterLayer(uri, name, 'wms')
    if not layer.isValid():
        raise RuntimeError(f'{name} could not be loaded.')
    root = project.layerTreeRoot()
    keep = project.crs()
    project.addMapLayer(layer, False)
    target = root if group is None else group
    if name in OVERLAYS:
        target.insertLayer(0, layer)
    else:
        target.insertLayer(len(target.children()), layer)
    # The project keeps its own CRS: the tiles are reprojected on the fly. QGIS
    # may switch an empty project to the first layer's CRS (Web Mercator here)
    # as the layer arrives, so the CRS is put back now and again once QGIS has
    # finished.
    if keep.isValid():
        _restore_crs(project, keep)
        QTimer.singleShot(0, lambda: _restore_crs(project, keep))
        QTimer.singleShot(500, lambda: _restore_crs(project, keep))
    return layer


def _restore_crs(project, crs):
    try:
        if project.crs() != crs:
            project.setCrs(crs)
    except RuntimeError:                # the project went away meanwhile
        pass

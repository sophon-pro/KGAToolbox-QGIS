# -*- coding: utf-8 -*-
"""Where each country is on the map, for the start extent of a new project.

The extents come from the world map that ships with QGIS
(`resources/data/world_map.gpkg`), so nothing is downloaded and the table is
the same on every machine. A country made of several parts (France with its
overseas departments, the USA with Alaska and Hawaii) is framed by its largest
part, because the box around all of them would be most of the globe.
"""

import os

from qgis.core import QgsApplication, QgsRectangle, QgsVectorLayer

from . import countries

#: Cambodia, longitude / latitude, used if the QGIS world map is missing.
FALLBACK = {'KHM': QgsRectangle(102.2, 9.8, 107.8, 14.8)}

_MARGIN = 0.06
_cache = None


def _world_map():
    path = os.path.join(QgsApplication.pkgDataPath(), 'resources', 'data',
                        'world_map.gpkg')
    return path if os.path.isfile(path) else ''


def _largest_part(geometry):
    """The bounding box of the biggest polygon of `geometry`."""
    if not geometry.isMultipart():
        return geometry.boundingBox()
    best, best_area = None, -1.0
    for part in geometry.asGeometryCollection():
        area = part.area()
        if area > best_area:
            best, best_area = part, area
    return best.boundingBox() if best is not None else geometry.boundingBox()


def _padded(rect):
    dx = max(rect.width() * _MARGIN, 0.05)
    dy = max(rect.height() * _MARGIN, 0.05)
    return QgsRectangle(max(rect.xMinimum() - dx, -180.0),
                        max(rect.yMinimum() - dy, -90.0),
                        min(rect.xMaximum() + dx, 180.0),
                        min(rect.yMaximum() + dy, 90.0))


def _load():
    found = {}
    path = _world_map()
    if path:
        layer = QgsVectorLayer(path + '|layername=countries', 'countries', 'ogr')
        if layer.isValid():
            for feature in layer.getFeatures():
                iso3 = str(feature['ISO_A3'] or '').upper()
                geometry = feature.geometry()
                if len(iso3) != 3 or geometry is None or geometry.isEmpty():
                    continue
                found[iso3] = _padded(_largest_part(geometry))
    for iso3, rect in FALLBACK.items():
        found.setdefault(iso3, rect)
    return found


def _extents():
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def extent_for(iso3):
    """The extent (EPSG:4326) that frames the country, or None if unknown."""
    return _extents().get((iso3 or '').upper())


def available():
    """[(alpha-3, name)] for the countries that can be framed, by name."""
    have = _extents()
    return [(iso3, name) for iso3, name in countries.COUNTRIES if iso3 in have]

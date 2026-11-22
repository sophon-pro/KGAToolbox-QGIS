# -*- coding: utf-8 -*-
"""Grid maths: no widgets, no canvas."""

import math

import numpy as np

#: Lines closer than this many pixels are not drawn: the spacing is multiplied by 10 until they are
#: farther apart ("Grid is dynamically scaled to ...").
MIN_PX = 14.0
#: Every this-many-th line is drawn stronger.
MAJOR_EVERY = 10
METRES_PER_FOOT = 0.3048
METRES_PER_DEGREE = 111320.0
UNIT_LABELS = {'m': 'm', 'ft': 'ft', 'map': 'map units'}


def metres_per_map_unit(crs):
    """How many metres one unit of the map CRS is (degrees count as 111 320 m, which is only right on the
    equator for x; fine for a grid)."""
    try:
        if crs.isGeographic():
            return METRES_PER_DEGREE
        from qgis.core import Qgis, QgsUnitTypes
        return float(QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters))
    except Exception:
        return 1.0


def spacing_in_map_units(spacing, unit, crs):
    """The spacing the user typed, in units of the map CRS."""
    if unit == 'map' or crs is None:
        return float(spacing)
    metres = float(spacing) * (METRES_PER_FOOT if unit == 'ft' else 1.0)
    return metres / metres_per_map_unit(crs)


def effective_scale(spacing_map, mupp):
    """(multiplier, spacing in map units actually drawn): the multiplier is 1, 10, 100 ... so that the
    lines are at least MIN_PX pixels apart."""
    if spacing_map <= 0 or mupp <= 0:
        return 1, spacing_map
    factor = 1
    while spacing_map * factor / mupp < MIN_PX and factor < 10 ** 12:
        factor *= 10
    return factor, spacing_map * factor


def format_value(value):
    text = '{:.6f}'.format(value).rstrip('0').rstrip('.')
    return text or '0'


def scaled_text(spacing, unit, factor):
    """'100 m' for a spacing of 10 m drawn at x10."""
    return '{} {}'.format(format_value(spacing * factor), UNIT_LABELS.get(unit, unit))


def grid_lines(corners, spacing, rotation_deg):
    """Segments of the grid across the area whose corners are *corners* (4x2, map coordinates).

    Returns [(p0, p1, k)] for the two directions: p0 / p1 are Nx2 start and end points and k the line
    number (the line at k * spacing; every MAJOR_EVERY-th is a major line). Lines run along the grid's
    own axes, turned *rotation_deg* counter-clockwise about the map origin."""
    theta = math.radians(rotation_deg)
    u = np.array([math.cos(theta), math.sin(theta)])
    v = np.array([-math.sin(theta), math.cos(theta)])
    corners = np.asarray(corners, dtype='f8')
    cu, cv = corners @ u, corners @ v
    out = []
    for lo_a, hi_a, lo_b, hi_b, axis_a, axis_b in ((cu.min(), cu.max(), cv.min(), cv.max(), u, v),
                                                   (cv.min(), cv.max(), cu.min(), cu.max(), v, u)):
        first = int(math.ceil(lo_a / spacing))
        last = int(math.floor(hi_a / spacing))
        if last < first:
            out.append((np.empty((0, 2)), np.empty((0, 2)), np.empty(0, dtype=np.int64)))
            continue
        k = np.arange(first, last + 1, dtype=np.int64)
        base = (k * spacing)[:, None] * axis_a[None, :]
        out.append((base + lo_b * axis_b[None, :], base + hi_b * axis_b[None, :], k))
    return out

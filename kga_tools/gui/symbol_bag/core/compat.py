# -*- coding: utf-8 -*-
"""Compatibility helpers for QGIS 3.28 – 3.x (Qt5) and QGIS 4 (Qt6)."""

from qgis.core import Qgis, QgsColorRampShader, QgsRasterBandStats, QgsRasterMinMaxOrigin

from ....core.compat import scoped


def event_pos(event):
    """Drag/drop event position as QPoint (Qt6 removed QDropEvent.pos())."""
    if hasattr(event, "position"):
        return event.position().toPoint()
    return event.pos()


def _first(*getters):
    for g in getters:
        try:
            return g()
        except AttributeError:
            continue
    raise AttributeError("No compatible enum found")


def stats_min_max():
    return _first(lambda: Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max,
                  lambda: QgsRasterBandStats.Stats.Min | QgsRasterBandStats.Stats.Max,
                  lambda: scoped(QgsRasterBandStats, 'Stats', 'Min') | scoped(QgsRasterBandStats, 'Stats', 'Max'))


def limits_cumulative_cut():
    return _first(lambda: Qgis.RasterRangeLimit.CumulativeCut,
                  lambda: QgsRasterMinMaxOrigin.Limits.CumulativeCut,
                  lambda: scoped(QgsRasterMinMaxOrigin, 'Limits', 'CumulativeCut'))


def shader_type(name):
    """name: 'Interpolated', 'Discrete' or 'Exact'."""
    return _first(lambda: getattr(Qgis.ShaderInterpolationMethod, {"Interpolated": "Linear"}.get(name, name)),
                  lambda: getattr(QgsColorRampShader.Type, name),
                  lambda: getattr(QgsColorRampShader, name))

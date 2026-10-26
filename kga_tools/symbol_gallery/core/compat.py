# -*- coding: utf-8 -*-
"""Enum shims so the plugin runs on QGIS 3.22 LTR through current 3.x."""
from qgis.core import Qgis, QgsSymbol, QgsWkbTypes

try:  # QGIS >= 3.20
    SYMBOL_MARKER = Qgis.SymbolType.Marker
    SYMBOL_LINE = Qgis.SymbolType.Line
    SYMBOL_FILL = Qgis.SymbolType.Fill
except AttributeError:  # pragma: no cover
    SYMBOL_MARKER, SYMBOL_LINE, SYMBOL_FILL = QgsSymbol.SymbolType.Marker, QgsSymbol.SymbolType.Line, QgsSymbol.SymbolType.Fill

try:  # QGIS >= 3.30
    GEOM_POINT = Qgis.GeometryType.Point
    GEOM_LINE = Qgis.GeometryType.Line
    GEOM_POLYGON = Qgis.GeometryType.Polygon
except AttributeError:  # pragma: no cover
    GEOM_POINT = QgsWkbTypes.GeometryType.PointGeometry
    GEOM_LINE = QgsWkbTypes.GeometryType.LineGeometry
    GEOM_POLYGON = QgsWkbTypes.GeometryType.PolygonGeometry

GEOM_TO_SYMBOL = {
    GEOM_POINT: SYMBOL_MARKER,
    GEOM_LINE: SYMBOL_LINE,
    GEOM_POLYGON: SYMBOL_FILL,
}


def _first(*candidates):
    """First enum member that exists in this QGIS / Qt version.

    Each candidate is `(owner, "Enum.Member")`, looked up by name so the scoped spelling is tried before the
    unscoped one that older builds need.
    """
    for owner, path in candidates:
        obj = owner
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    raise AttributeError("none of the enum spellings exist")


from qgis.core import (  # noqa: E402
    QgsLayerTreeModelLegendNode, QgsStyle, QgsRasterBandStats, QgsColorRampShader,
    QgsRenderContext, QgsMarkerSymbolLayer,
)

# Legend node rule key role: scoped enum since QGIS 3.36, plain before
LEGEND_RULE_KEY_ROLE = _first(
    (QgsLayerTreeModelLegendNode, "CustomRole.RuleKey"),
    (QgsLayerTreeModelLegendNode, "CustomRole.RuleKeyRole"),
    (QgsLayerTreeModelLegendNode, "RuleKeyRole"))

STYLE_SYMBOL = _first((QgsStyle, "StyleEntity.SymbolEntity"), (QgsStyle, "SymbolEntity"))
STYLE_RAMP = _first((QgsStyle, "StyleEntity.ColorrampEntity"), (QgsStyle, "ColorrampEntity"))

STATS_MIN_MAX = (
    _first((Qgis, "RasterBandStatistic.Min"), (QgsRasterBandStats, "Stats.Min"), (QgsRasterBandStats, "Min"))
    | _first((Qgis, "RasterBandStatistic.Max"), (QgsRasterBandStats, "Stats.Max"), (QgsRasterBandStats, "Max")))

SHADER_INTERPOLATED = _first((Qgis, "ShaderInterpolationMethod.Linear"),
                             (QgsColorRampShader, "Type.Interpolated"),
                             (QgsColorRampShader, "Interpolated"))
SHADER_CONTINUOUS = _first((Qgis, "ShaderClassificationMethod.Continuous"),
                           (QgsColorRampShader, "ClassificationMode.Continuous"),
                           (QgsColorRampShader, "Continuous"))

RC_ANTIALIASING = _first((Qgis, "RenderContextFlag.Antialiasing"),
                         (QgsRenderContext, "Flag.Antialiasing"),
                         (QgsRenderContext, "Antialiasing"))
try:
    RC_HQ_IMAGES = _first((Qgis, "RenderContextFlag.HighQualityImageTransforms"),
                          (QgsRenderContext, "Flag.HighQualityImageTransforms"),
                          (QgsRenderContext, "HighQualityImageTransforms"))
except AttributeError:  # pragma: no cover - very old QGIS
    RC_HQ_IMAGES = None

VANCHOR_BOTTOM = _first((Qgis, "VerticalAnchorPoint.Bottom"),
                        (QgsMarkerSymbolLayer, "VerticalAnchorPoint.Bottom"),
                        (QgsMarkerSymbolLayer, "Bottom"))
VANCHOR_VCENTER = _first((Qgis, "VerticalAnchorPoint.VCenter"),
                         (QgsMarkerSymbolLayer, "VerticalAnchorPoint.VCenter"),
                         (QgsMarkerSymbolLayer, "VCenter"))

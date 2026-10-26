# -*- coding: utf-8 -*-
"""Symbology and labels for the contour layer.

Main contours (every Nth level) are drawn as a dark, thicker line and the sub
contours between them as a thin grey one. Labels carry the elevation: on the
main lines only, on every line, or not at all. The look is keyed on the TYPE
attribute written by ``contours.py``, so it survives the layer being saved to
another format.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

from contextlib import suppress

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsColorRampShader,
                       QgsLineSymbol, QgsMarkerSymbol, QgsSingleSymbolRenderer,
                       QgsPalLayerSettings, QgsRasterShader,
                       QgsRendererCategory, QgsRuleBasedLabeling,
                       QgsSingleBandPseudoColorRenderer, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor, QFont

from .contours import FIELD_ELEV, FIELD_TYPE, TYPE_MAIN, TYPE_SUB

LABELS_MAIN = 'main'
LABELS_ALL = 'all'
LABELS_NONE = 'none'

MAIN_COLOUR = '#262626'
SUB_COLOUR = '#9e9e9e'
MAIN_WIDTH_MM = 0.5
SUB_WIDTH_MM = 0.15

#: Shows 100, not 100.0, for a whole level and 2.5 for a fractional one.
LABEL_EXPRESSION = 'to_string(round("{0}", 2))'.format(FIELD_ELEV)


#: Pale on purpose: the main contours are dark, and a default black-to-white
#: stretch would swallow them wherever the ground is low.
DEM_RAMP = ((0.0, '#e4eed2'), (0.5, '#efe6c8'), (1.0, '#f7f7f4'))


def style_dem(layer, low, high):
    """A pale hypsometric tint for the DEM, so the contours stay readable."""
    if high <= low:
        return
    shader = QgsColorRampShader(low, high)
    shader.setColorRampType(QgsColorRampShader.Type.Interpolated)
    shader.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(low + (high - low) * at,
                                         QColor(colour))
        for at, colour in DEM_RAMP])
    raster_shader = QgsRasterShader()
    raster_shader.setRasterShaderFunction(shader)
    layer.setRenderer(QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, raster_shader))
    layer.triggerRepaint()


#: Slope: flat is pale, steep is dark red. Aspect: a cycle, because 359 degrees
#: and 1 degree face the same way.
SLOPE_RAMP = ((0.0, '#ffffcc'), (0.25, '#fed976'), (0.5, '#fd8d3c'),
              (0.75, '#e31a1c'), (1.0, '#800026'))
ASPECT_RAMP = ((0, '#d73027'), (90, '#fee08b'), (180, '#1a9850'),
               (270, '#4575b4'), (360, '#d73027'))


def _ramp(layer, stops, low, high, scale):
    """A pseudocolour renderer whose stops sit at `scale`-relative positions."""
    shader = QgsColorRampShader(low, high)
    shader.setColorRampType(QgsColorRampShader.Type.Interpolated)
    shader.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(low + (high - low) * at / scale,
                                         QColor(colour))
        for at, colour in stops])
    raster_shader = QgsRasterShader()
    raster_shader.setRasterShaderFunction(shader)
    layer.setRenderer(QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, raster_shader))


def style_terrain(layer, kind):
    """Colour a derived surface. The hillshade keeps QGIS's grey default."""
    if kind == 'hillshade':
        layer.setOpacity(0.6)
    elif kind == 'slope':
        stats = layer.dataProvider().bandStatistics(1)
        high = max(5.0, min(90.0, stats.maximumValue))
        _ramp(layer, SLOPE_RAMP, 0.0, high, 1.0)
    elif kind == 'aspect':
        _ramp(layer, ASPECT_RAMP, 0.0, 360.0, 360.0)
    layer.triggerRepaint()


def _line(colour, width):
    return QgsLineSymbol.createSimple({
        'line_color': colour, 'line_width': str(width),
        'line_width_unit': 'MM', 'capstyle': 'round', 'joinstyle': 'round'})


def apply_style(layer, symbolize=True, labels=LABELS_MAIN):
    """Style `layer` as asked. Returns nothing; the layer is changed in place."""
    if symbolize:
        # Sub first: categories draw in list order, so main ends up on top.
        categories = [
            QgsRendererCategory(TYPE_SUB, _line(SUB_COLOUR, SUB_WIDTH_MM),
                                'Sub contour'),
            QgsRendererCategory(TYPE_MAIN, _line(MAIN_COLOUR, MAIN_WIDTH_MM),
                                'Main contour'),
        ]
        layer.setRenderer(QgsCategorizedSymbolRenderer(FIELD_TYPE, categories))
    _apply_labels(layer, labels)
    layer.triggerRepaint()


def _settings(colour, size, bold):
    settings = QgsPalLayerSettings()
    settings.fieldName = LABEL_EXPRESSION
    settings.isExpression = True

    text = QgsTextFormat()
    font = QFont()
    font.setBold(bold)
    text.setFont(font)
    text.setSize(size)
    text.setColor(QColor(colour))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(0.8)
    buffer.setColor(QColor('white'))
    text.setBuffer(buffer)
    settings.setFormat(text)

    settings.placement = Qgis.LabelPlacement.Curved
    # On the line, not beside it: the line is broken by its own label, the
    # way a printed contour map does it. Only this flag - the default also
    # carries "above line", which would put the label back beside the line.
    settings.lineSettings().setPlacementFlags(
        Qgis.LabelLinePlacementFlags(Qgis.LabelLinePlacementFlag.OnLine))
    with suppress(Exception):               # a label every ~12 cm of line
        settings.repeatDistance = 120
        settings.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
    return settings


def _apply_labels(layer, mode):
    if mode == LABELS_NONE:
        layer.setLabelsEnabled(False)
        layer.setLabeling(None)
        return
    root = QgsRuleBasedLabeling.Rule(QgsPalLayerSettings())
    rules = [(TYPE_MAIN, 'Main contour labels', MAIN_COLOUR, 8.0, True)]
    if mode == LABELS_ALL:
        rules.append((TYPE_SUB, 'Sub contour labels', '#6e6e6e', 6.5, False))
    for kind, label, colour, size, bold in rules:
        rule = QgsRuleBasedLabeling.Rule(_settings(colour, size, bold))
        rule.setFilterExpression('"{0}" = \'{1}\''.format(FIELD_TYPE, kind))
        rule.setDescription(label)
        root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(root))
    layer.setLabelsEnabled(True)


SPOT_COLOUR = '#262626'


def style_spot_heights(layer):
    """Topographic-map spot heights: a small black dot, its height beside it.

    The figure is upright (not bold) black text with a white halo, set to the
    right of the dot, as on a printed topo sheet.
    """
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({
        'name': 'circle', 'color': SPOT_COLOUR, 'size': '1.2',
        'size_unit': 'MM', 'outline_style': 'no'})))
    settings = QgsPalLayerSettings()
    settings.fieldName = LABEL_EXPRESSION
    settings.isExpression = True
    text = QgsTextFormat()
    font = QFont()
    font.setItalic(False)
    text.setFont(font)
    text.setSize(7.5)
    text.setColor(QColor(SPOT_COLOUR))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(0.7)
    buffer.setColor(QColor('white'))
    text.setBuffer(buffer)
    settings.setFormat(text)
    settings.placement = Qgis.LabelPlacement.OrderedPositionsAroundPoint
    settings.dist = 0.8
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()

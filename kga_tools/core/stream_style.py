# -*- coding: utf-8 -*-
"""Symbology for the stream layers.

Streams are drawn blue with the line width growing with Strahler order, so the
trunk of the network reads at a glance and the headwaters stay fine. The other
way to look at them is by confidence: the same widths, coloured by the grade the
internal checks gave each segment. The comparison layer shows what only one
network has: orange for DEM streams the reference lacks, purple for reference
streams the DEM lacks.

The look is keyed on attributes written by ``streams.py``, so it survives the
layer being saved to another format.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

from qgis.core import (QgsCategorizedSymbolRenderer, QgsLineSymbol,
                       QgsProperty, QgsRendererCategory,
                       QgsSingleSymbolRenderer, QgsSymbolLayer)

from .compat import enum_value
from .stream_check import CONF_HIGH, CONF_LOW, CONF_MEDIUM
from .streams import FIELD_CONF, FIELD_KIND, FIELD_ORDER, KIND_EXTRA, KIND_MISSED

STYLE_ORDER = 'order'
STYLE_CONFIDENCE = 'confidence'

STREAM_COLOUR = '#1f78b4'
CONFIDENCE_COLOURS = ((CONF_HIGH, '#2e7d32'), (CONF_MEDIUM, '#f9a825'),
                      (CONF_LOW, '#c62828'))
EXTRA_COLOUR = '#ef6c00'
MISSED_COLOUR = '#8e24aa'

#: Width in mm: a first-order stream is fine, each order up adds a little.
WIDTH_EXPRESSION = '0.25 + "{0}" * 0.3'.format(FIELD_ORDER)
SINGLE_WIDTH_MM = 0.5


def _stroke_width_property():
    # QGIS 3.44 spells it QgsSymbolLayer.Property.StrokeWidth; older builds
    # expose PropertyStrokeWidth on the class itself.
    return enum_value((QgsSymbolLayer, 'Property.StrokeWidth'),
                      (QgsSymbolLayer, 'Property.PropertyStrokeWidth'),
                      (QgsSymbolLayer, 'PropertyStrokeWidth'))


def _line(colour, width, dashed=False, by_order=False):
    props = {'line_color': colour, 'line_width': str(width),
             'line_width_unit': 'MM', 'capstyle': 'round',
             'joinstyle': 'round'}
    if dashed:
        props['line_style'] = 'dash'
    symbol = QgsLineSymbol.createSimple(props)
    if by_order:
        symbol.symbolLayer(0).setDataDefinedProperty(
            _stroke_width_property(),
            QgsProperty.fromExpression(WIDTH_EXPRESSION))
    return symbol


def apply_streams(layer, style=STYLE_ORDER):
    """Style the stream layer: by order (blue) or by check confidence."""
    if style == STYLE_CONFIDENCE:
        categories = [
            QgsRendererCategory(grade, _line(colour, SINGLE_WIDTH_MM,
                                             by_order=True),
                                '{0} confidence'.format(grade))
            for grade, colour in CONFIDENCE_COLOURS]
        layer.setRenderer(QgsCategorizedSymbolRenderer(FIELD_CONF, categories))
    else:
        layer.setRenderer(QgsSingleSymbolRenderer(
            _line(STREAM_COLOUR, SINGLE_WIDTH_MM, by_order=True)))
    layer.triggerRepaint()


def apply_check(layer):
    """Style the comparison layer: DEM-only orange, reference-only purple."""
    categories = [
        QgsRendererCategory(KIND_EXTRA, _line(EXTRA_COLOUR, 0.7, dashed=True),
                            'In the DEM, not in the reference'),
        QgsRendererCategory(KIND_MISSED, _line(MISSED_COLOUR, 0.7,
                                               dashed=True),
                            'In the reference, not in the DEM'),
    ]
    layer.setRenderer(QgsCategorizedSymbolRenderer(FIELD_KIND, categories))
    layer.triggerRepaint()

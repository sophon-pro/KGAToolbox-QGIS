# -*- coding: utf-8 -*-
"""Symbology for the plot layers: readable over imagery."""

from qgis.core import (QgsFillSymbol, QgsLineSymbol, QgsSingleSymbolRenderer)


def apply_plots(layer):
    """A light green wash with a yellow outline; doubtful plots dashed."""
    from qgis.core import QgsProperty, QgsSymbolLayer

    symbol = QgsFillSymbol.createSimple({
        'color': '76,217,100,45', 'outline_color': '#ffd60a',
        'outline_width': '0.5', 'joinstyle': 'round'})
    symbol.symbolLayer(0).setDataDefinedProperty(
        QgsSymbolLayer.Property.StrokeStyle,
        QgsProperty.fromExpression(
            "CASE WHEN \"CONF\" < 0.3 OR \"EDGE\" = 1 THEN 'dash' "
            "ELSE 'solid' END"))
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def apply_boundaries(layer):
    symbol = QgsLineSymbol.createSimple({
        'color': '#ff9f0a', 'width': '0.45', 'capstyle': 'round',
        'joinstyle': 'round'})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()

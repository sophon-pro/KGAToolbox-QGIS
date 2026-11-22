# -*- coding: utf-8 -*-
"""Symbology for the road layers: readable over imagery, gaps called out."""

from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol,
                       QgsLineSymbol, QgsRendererCategory,
                       QgsSingleSymbolRenderer)

CENTRE_COLOURS = (('paved', '#ff3b30', 'Paved (guess)'),
                  ('unpaved', '#ff9f0a', 'Unpaved (guess)'),
                  ('unknown', '#ffd60a', 'Unknown surface'))


def apply_centrelines(layer):
    categories = []
    for value, colour, label in CENTRE_COLOURS:
        symbol = QgsLineSymbol.createSimple({
            'color': colour, 'width': '0.7', 'capstyle': 'round',
            'joinstyle': 'round'})
        categories.append(QgsRendererCategory(value, symbol, label))
    renderer = QgsCategorizedSymbolRenderer('SURFACE', categories)
    layer.setRenderer(renderer)
    _gap_rule(layer)
    layer.triggerRepaint()


def _gap_rule(layer):
    """Joined gaps drawn dashed, whatever their category."""
    from qgis.core import QgsProperty, QgsSymbolLayer

    renderer = layer.renderer()
    for index, category in enumerate(renderer.categories()):
        symbol = category.symbol().clone()
        symbol.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.Property.StrokeStyle,
            QgsProperty.fromExpression(
                "CASE WHEN \"GAP_JOIN\" = 1 THEN 'dash' ELSE 'solid' END"))
        renderer.updateCategorySymbol(index, symbol)


def apply_edges(layer):
    symbol = QgsLineSymbol.createSimple({'color': '#00e5ff', 'width': '0.35'})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def apply_surface(layer):
    symbol = QgsFillSymbol.createSimple({
        'color': '255,59,48,70', 'outline_style': 'no'})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def apply_check(layer):
    extra = QgsLineSymbol.createSimple({'color': '#bf5af2', 'width': '1.0'})
    missed = QgsLineSymbol.createSimple({'color': '#ff2d55', 'width': '0.8',
                                         'line_style': 'dash'})
    layer.setRenderer(QgsCategorizedSymbolRenderer('KIND', [
        QgsRendererCategory('extra', extra,
                            'Detected, not in the reference'),
        QgsRendererCategory('missed', missed,
                            'In the reference, not detected')]))
    layer.triggerRepaint()


def apply_probability(layer):
    """0-100 road probability: transparent below 20, then yellow to red."""
    from qgis.core import (QgsColorRampShader, QgsRasterShader,
                           QgsSingleBandPseudoColorRenderer)
    from qgis.PyQt.QtGui import QColor

    ramp = QgsColorRampShader(0, 100)
    ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
    ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(0, QColor(255, 255, 0, 0), '0'),
        QgsColorRampShader.ColorRampItem(20, QColor(255, 255, 0, 0), '20'),
        QgsColorRampShader.ColorRampItem(50, QColor(255, 159, 10, 200), '50'),
        QgsColorRampShader.ColorRampItem(100, QColor(255, 45, 85, 230),
                                         '100')])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    layer.setRenderer(QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, shader))
    layer.triggerRepaint()

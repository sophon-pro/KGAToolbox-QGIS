# -*- coding: utf-8 -*-
"""Put the finished image on the map."""

import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsProject, QgsRasterLayer,
                       QgsRectangle)
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QPushButton

from ..layer_loader import unique_name
from .estimator import human_size


class LoadError(Exception):
    pass


def add_result(iface, result, spec):
    """Add the raster below the vector layers and announce it."""
    path = result['path']
    name = spec.layer_name or spec.name
    layer = QgsRasterLayer(path, unique_name(QgsProject.instance(), name))
    if not layer.isValid():
        raise LoadError('QGIS could not open the result: %s' % path)
    if not layer.crs().isValid():
        raise LoadError('The result has no coordinate system: %s' % path)
    if spec.source.attribution:
        meta = layer.metadata()
        meta.setRights([spec.source.attribution])
        layer.setMetadata(meta)
        layer.setAbstract('Source: %s. %s' % (spec.source.name,
                                              spec.source.attribution))

    project = QgsProject.instance()
    project.addMapLayer(layer, False)
    project.layerTreeRoot().insertLayer(-1, layer)     # bottom: under vectors

    canvas = iface.mapCanvas() if iface else None
    if canvas is not None and not _visible(canvas, spec.bbox):
        canvas.setExtent(_layer_extent_in_canvas(canvas, layer))
        canvas.refresh()
    if iface is not None:
        _announce(iface, result, layer, path)
    return layer


def _visible(canvas, bbox):
    """True when any of the download area is already on screen."""
    try:
        tr = QgsCoordinateTransform(
            canvas.mapSettings().destinationCrs(),
            QgsCoordinateReferenceSystem('EPSG:4326'),
            QgsProject.instance().transformContext())
        view = tr.transformBoundingBox(canvas.extent())
        return view.intersects(QgsRectangle(*bbox))
    except Exception:
        return True


def _layer_extent_in_canvas(canvas, layer):
    tr = QgsCoordinateTransform(layer.crs(),
                                canvas.mapSettings().destinationCrs(),
                                QgsProject.instance().transformContext())
    return tr.transformBoundingBox(layer.extent())


def _announce(iface, result, layer, path):
    size = 0
    for f in result.get('files') or [path]:
        try:
            size += os.path.getsize(f)
        except OSError:
            pass
    res = result.get('pixel_size') or 0
    unit = 'deg' if layer.crs().isGeographic() else 'm'
    text = '%s: %d x %d px, %s, %.4g %s/px.' % (
        layer.name(), result.get('width', 0), result.get('height', 0),
        human_size(size), res, unit)
    bar = iface.messageBar()
    item = bar.createMessage('Imagery downloaded', text)
    button = QPushButton('Show in folder')
    folder = os.path.dirname(path)
    button.clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
    item.layout().addWidget(button)
    bar.pushWidget(item, Qgis.MessageLevel.Success, 15)

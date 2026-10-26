# -*- coding: utf-8 -*-
"""Draw the selection the way ArcGIS Pro does: cyan, on top of the symbol.

QGIS paints a selected feature in one solid colour (yellow by default) that
replaces its symbol. Pro keeps the symbol and outlines the feature in cyan, so
the selection can be seen on any layer and any basemap. Two things give the
same look here:

* the project / canvas selection colour is made practically invisible, so
  QGIS's own flat selection paint no longer hides the symbol;
* a canvas item redraws every selected feature with its layer's own renderer
  (the original symbol, fill included) and then a crisp opaque cyan outline
  (a ring on a point, the line itself, the polygon boundary) above the layers.

A selection too big to redraw falls back to a translucent cyan from QGIS.

The switch lives in Settings > Appearance (`core/kga_settings.py`).
"""

import traceback
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsGeometry,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsMessageLog,
    QgsNullSymbolRenderer,
    QgsProject,
    QgsRenderContext,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QObject, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF

from ..branding import LOG_TAG
from ..core import kga_settings as settings

#: Pro's default selection colour is cyan (0, 255, 255).
OUTLINE = QColor(0, 255, 255)
#: What QGIS paints under the outline: cyan, see-through so the symbol shows.
FILL = QColor(0, 255, 255, 60)
#: QGIS's own selection paint, made all but invisible (alpha 0 would not draw).
HIDDEN = QColor(0, 255, 255, 1)
#: Above the map image (z -10) but below every rubber band, marker and snap indicator (z 0) - the sketches and previews
#: of the editing tools must stay in front of the selected feature's redrawn symbol.
OVERLAY_Z = -5
POINT_RADIUS = 8.0
LINE_WIDTH = 2.6
#: (pen width, alpha) of the glow strokes under the core line, widest first.
GLOW = ((6.5, 22), (4.8, 45))
#: A huge selection is left to QGIS's own colour rather than redrawn on every pan.
MAX_FEATURES = 5000
#: More selected features than this keep QGIS's own labels (hidden) rather than a slow re-render of them.
MAX_LABELLED = 400


def selected_total(canvas):
    """How many features are selected on the layers the canvas shows."""
    return sum(layer.selectedFeatureCount() for layer in canvas.layers()
               if isinstance(layer, QgsVectorLayer))


def _ring(points):
    return QPolygonF([QPointF(p[0], p[1]) for p in points])


class _Overlay(QgsMapCanvasItem):
    """Paints the outline of every selected feature on the map canvas."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setZValue(OVERLAY_Z)
        self._label_cache = {}                      # layer id -> (view and selection key, picture of the labels)

    def boundingRect(self):
        return QRectF(0, 0, self.canvas.width(), self.canvas.height())

    def updatePosition(self):
        self.setPos(0, 0)

    def _selected(self):
        """[(layer, [features])] for the visible layers, or None if too many."""
        found, total = [], 0
        for layer in self.canvas.layers():
            if not isinstance(layer, QgsVectorLayer):
                continue
            count = layer.selectedFeatureCount()
            if not count:
                continue
            total += count
            if total > MAX_FEATURES:
                return None
            found.append((layer, layer.selectedFeatures()))
        return found

    def paint(self, painter, option=None, widget=None):
        try:
            self._paint(painter)
        except Exception:               # never let a paint error reach Qt
            QgsMessageLog.logMessage(
                'Selection outline failed:{}{}'.format(chr(10), traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)

    def _paint(self, painter):
        selected = self._selected()
        if not selected:
            return
        settings_ = self.canvas.mapSettings()
        to_pixel = settings_.mapToPixel()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Wide and faint outside, narrow and bright inside: reads as a soft glow.
        pens = []
        for width, alpha in GLOW:
            pen = QPen(QColor(0, 255, 255, alpha), width)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pens.append(pen)
        core = QPen(OUTLINE, LINE_WIDTH)
        core.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        core.setCapStyle(Qt.PenCapStyle.RoundCap)
        pens.append(core)
        for layer, features in selected:
            transform = None
            if layer.crs() != settings_.destinationCrs():
                transform = QgsCoordinateTransform(
                    layer.crs(), settings_.destinationCrs(), QgsProject.instance())
            self._symbols(painter, settings_, layer, features, transform)
            self._labels(painter, settings_, layer, features)
            for feature in features:
                geometry = QgsGeometry(feature.geometry())
                if geometry.isNull() or geometry.isEmpty():
                    continue
                if transform is not None:
                    moved = False
                    with suppress(Exception):
                        geometry.transform(transform)
                        moved = True
                    if not moved:
                        continue
                if QgsWkbTypes.isCurvedType(geometry.wkbType()):
                    geometry = QgsGeometry(geometry.constGet().segmentize())
                self._draw(painter, geometry, to_pixel, pens)

    @staticmethod
    def _symbols(painter, map_settings, layer, features, transform):
        """Paint the layer's own symbol over QGIS's selection paint."""
        renderer = layer.renderer()
        if renderer is None:
            return
        renderer = renderer.clone()
        context = QgsRenderContext.fromMapSettings(map_settings)
        context.setPainter(painter)
        context.setExpressionContext(QgsExpressionContext(
            QgsExpressionContextUtils.globalProjectLayerScopes(layer)))
        if transform is not None:
            context.setCoordinateTransform(transform)
        painter.save()
        painter.setOpacity(layer.opacity())
        try:
            renderer.startRender(context, layer.fields())
            try:
                for feature in features:
                    context.expressionContext().setFeature(feature)
                    renderer.renderFeature(feature, context, -1, False, False)
            finally:
                renderer.stopRender(context)
        finally:
            painter.restore()

    def _labels(self, painter, map_settings, layer, features):
        """
        Paint the labels of the selected features again. The symbol redrawn above covers what the map render labelled
        (labels are drawn into the map image), so a selected feature would lose its label. QGIS cannot label one
        feature of a layer on demand, so the selected features are copied to a scratch layer with the layer's own
        labeling and no symbol, and that is rendered (once per view and selection: the picture is kept).
        """
        if not layer.labelsEnabled() or layer.labeling() is None or len(features) > MAX_LABELLED:
            return
        key = (layer.id(), tuple(sorted(f.id() for f in features)), map_settings.visibleExtent().toString(),
               map_settings.deviceOutputSize().width(), map_settings.deviceOutputSize().height(),
               round(map_settings.rotation(), 6))
        cached = self._label_cache.get(layer.id())
        if cached is None or cached[0] != key:
            cached = (key, self._label_image(map_settings, layer, features))
            self._label_cache[layer.id()] = cached
        if cached[1] is not None:
            painter.drawImage(0, 0, cached[1])

    @staticmethod
    def _label_image(map_settings, layer, features):
        scratch = QgsVectorLayer(
            "{}?crs={}".format(QgsWkbTypes.displayString(layer.wkbType()), layer.crs().authid() or "EPSG:4326"),
            "kga-selection-labels", "memory")
        if not scratch.isValid():
            return None
        scratch.setCrs(layer.crs())
        scratch.dataProvider().addAttributes(layer.fields().toList())
        scratch.updateFields()
        scratch.dataProvider().addFeatures([QgsFeature(f) for f in features])
        scratch.setRenderer(QgsNullSymbolRenderer())
        scratch.setLabeling(layer.labeling().clone())
        scratch.setLabelsEnabled(True)
        settings_ = QgsMapSettings(map_settings)
        settings_.setLayers([scratch])
        settings_.setBackgroundColor(QColor(0, 0, 0, 0))
        image = QImage(settings_.deviceOutputSize(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            job = QgsMapRendererCustomPainterJob(settings_, painter)
            job.renderSynchronously()
        finally:
            painter.end()
        return image

    def _pixels(self, to_pixel, points):
        out = []
        for point in points:
            p = to_pixel.transform(point.x(), point.y())
            out.append((p.x(), p.y()))
        return out

    def _draw(self, painter, geometry, to_pixel, pens):
        kind = QgsWkbTypes.geometryType(geometry.wkbType())
        multi = geometry.isMultipart()
        if kind == QgsWkbTypes.GeometryType.PointGeometry:
            points = geometry.asMultiPoint() if multi else [geometry.asPoint()]
            painter.setBrush(QBrush(QColor(0, 255, 255, 90)))
            for x, y in self._pixels(to_pixel, points):
                for pen in pens:
                    painter.setPen(pen)
                    painter.drawEllipse(QPointF(x, y), POINT_RADIUS, POINT_RADIUS)
            return
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        if kind == QgsWkbTypes.GeometryType.LineGeometry:
            lines = geometry.asMultiPolyline() if multi else [geometry.asPolyline()]
            for line in lines:
                pixels = self._pixels(to_pixel, line)
                if len(pixels) > 1:
                    path.addPolygon(_ring(pixels))
        elif kind == QgsWkbTypes.GeometryType.PolygonGeometry:
            polygons = geometry.asMultiPolygon() if multi else [geometry.asPolygon()]
            for polygon in polygons:
                for ring in polygon:
                    pixels = self._pixels(to_pixel, ring)
                    if len(pixels) > 2:
                        path.addPolygon(_ring(pixels))
                        path.closeSubpath()
        if path.isEmpty():
            return
        for pen in pens:
            painter.setPen(pen)
            painter.drawPath(path)


class ProSelection(QObject):
    """Switches the canvas to the Pro-style selection and keeps it on."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.overlay = None
        self._installed = False

    # ------------------------------------------------------------- lifecycle

    def install(self):
        if self._installed:
            return
        self._installed = True
        for signal in (self.iface.projectRead, self.iface.newProjectCreated):
            signal.connect(self.refresh)
        self.canvas.selectionChanged.connect(self._update)
        self.canvas.extentsChanged.connect(self._update)
        self.canvas.layersChanged.connect(self._update)
        self.refresh()

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        for signal, slot in ((self.iface.projectRead, self.refresh),
                             (self.iface.newProjectCreated, self.refresh),
                             (self.canvas.selectionChanged, self._update),
                             (self.canvas.extentsChanged, self._update),
                             (self.canvas.layersChanged, self._update)):
            with suppress(Exception):
                signal.disconnect(slot)
        self._remove_overlay()
        self._colour(QColor(255, 255, 0, 255))

    # ------------------------------------------------------------- behaviour

    def refresh(self, *_args):
        """Follow the Settings switch: Pro look on, or QGIS's yellow back."""
        try:
            if settings.pro_selection():
                if self.overlay is None:
                    self.overlay = _Overlay(self.canvas)
                self._update()
            else:
                self._remove_overlay()
                self._colour(QColor(255, 255, 0, 255))
        except RuntimeError:            # the plugin was unloaded meanwhile
            pass

    def _colour(self, colour):
        self.canvas.setSelectionColor(colour)
        project = QgsProject.instance()
        if project.selectionColor() != colour:
            project.setSelectionColor(colour)

    def _remove_overlay(self):
        if self.overlay is not None:
            with suppress(Exception):
                self.canvas.scene().removeItem(self.overlay)
            self.overlay = None

    def _update(self, *_args):
        if self.overlay is not None:
            with suppress(RuntimeError):
                big = selected_total(self.canvas) > MAX_FEATURES
                colour = FILL if big else HIDDEN
                if self.canvas.selectionColor() != colour:
                    self._colour(colour)
                self.overlay.update()

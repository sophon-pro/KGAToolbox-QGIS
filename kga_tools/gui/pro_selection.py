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

import time
import traceback
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsLineSymbol,
    QgsMapRendererCustomPainterJob,
    QgsMapRendererParallelJob,
    QgsMarkerSymbol,
    QgsMapSettings,
    QgsMessageLog,
    QgsNullSymbolRenderer,
    QgsProject,
    QgsRectangle,
    QgsRenderContext,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QPointF, QRectF, Qt, QTimer
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
#: Outlines are simplified to this many pixels: invisible, and a border of 50 000 vertices draws as fast as a box.
SIMPLIFY_PX = 0.4
#: The outline is cut this far outside the view, so the cut edges and wide pens never show.
CLIP_MARGIN_PX = 48
#: A picture that took longer than SLOW_BUILD_S to make is not made again while the view or the selection
#: keeps changing less than QUIET_S apart (a panel sliding open resizes the map on every frame): the outline
#: waits, then is drawn once things are still.
SLOW_BUILD_S = 0.03
QUIET_S = 0.2
#: From this many selected features on, the outline is one crisp line without the two glow strokes under
#: it (at that density the glow is lost), and the picture is drawn by a QGIS render job in background
#: threads instead of in the GUI thread: with 1 400 features selected, drawing it in place froze the
#: window for 0.4-0.9 s after the selection and again after every pan or zoom. The outline then appears
#: a moment after the map, the way the map's own layers do.
MANY_FEATURES = 300


#: The map-settings flag that makes QGIS paint selected features in the selection colour.
DRAW_SELECTION = Qgis.MapSettingsFlag.DrawSelection
_STYLE_SCAN = {}                # layer id -> its style mentions the selection (see style_uses_selection)


def map_ignores_selection(canvas):
    """True while the map image does not depend on what is selected.

    QGIS redraws the map after every selection change, because it paints the selected features in
    the selection colour. In a project with some sixty layers that redraw costs 200-300 ms of the
    GUI thread per click with the select tool (preparing every layer, labelling again). The Pro-style
    selection never shows QGIS's colour - the outline is drawn on top by the overlay - so while it is
    on, the canvas is told not to draw selections at all (`ProSelection._update`). The map then looks
    the same whatever is selected, and a selection change needs no redraw."""
    try:
        return not canvas.mapSettings().testFlag(DRAW_SELECTION)
    except Exception:
        return False


def style_uses_selection(layer):
    """True if the layer's symbology or labels ask what is selected (`is_selected()`, as the Layer
    Bag's "labels of the selected features" does): such a layer must be redrawn when the selection
    changes. The style is read once and again only after it changes."""
    key = layer.id()
    known = _STYLE_SCAN.get(key)
    if known is None:
        known = True                                # when in doubt: redraw
        with suppress(Exception):
            from qgis.core import QgsMapLayerStyle
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            known = 'is_selected' in style.xmlData()
            layer.styleChanged.connect(lambda k=key: _STYLE_SCAN.pop(k, None))
        _STYLE_SCAN[key] = known
    return known


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
        self._image = None                          # the last full paint, reused while nothing changed
        self._image_key = None
        self._layers = []                           # the visible layers that hold a selection (see invalidate)
        self._features = {}                         # layer id -> (selection key, the selected features)
        self._build_cost = 0.0                      # how long the last picture took to make
        self._changed_at = 0.0                      # when the view or the selection last changed ...
        self._rapid = False                         # ... and whether that came right after another change
        self._later = QTimer()                      # paints the picture once a fast run of view changes is over
        self._later.setSingleShot(True)
        self._later.setInterval(int(QUIET_S * 1000) + 30)
        self._later.timeout.connect(self._paint_later)
        self._job = None                            # the background render of a big selection ...
        self._job_key = None                        # ... and the view / selection it is for
        self._dying = []                            # cancelled jobs, kept until they have stopped
        self._scratch = {}                          # layer id -> (selection key, [outline layer, symbol layer])
        self.setVisible(False)                      # nothing selected yet: nothing to paint
        # The item follows the map by itself, so what it shows can never be out of date whoever owns it.
        canvas.selectionChanged.connect(self._map_changed)
        canvas.layersChanged.connect(self._map_changed)
        canvas.extentsChanged.connect(self._map_changed)

    def _map_changed(self, *_args):
        if not sip.isdeleted(self):
            self.invalidate()

    def _paint_later(self):
        if not sip.isdeleted(self):
            self.update()

    def detach(self):
        """The item is being taken off the map."""
        self._later.stop()
        self._cancel_job()
        self._scratch = {}
        for signal in (self.canvas.selectionChanged, self.canvas.layersChanged, self.canvas.extentsChanged):
            with suppress(Exception):
                signal.disconnect(self._map_changed)

    def boundingRect(self):
        return QRectF(0, 0, self.canvas.width(), self.canvas.height())

    def invalidate(self):
        """The selection or the layers changed: paint again from scratch.

        The layers that hold a selection are looked up here, once per change, and not in every paint:
        the map repaints this item on each frame of a zoom or pan, and walking every layer of a big
        project there made navigation slower with each layer added. With nothing selected the item is
        hidden, so the map does not blend an empty full-size picture into every frame."""
        now = time.perf_counter()
        self._rapid = now - self._changed_at < QUIET_S
        self._changed_at = now
        self._image = None
        self._layers = [layer for layer in self.canvas.layers()
                        if isinstance(layer, QgsVectorLayer) and layer.selectedFeatureCount()]
        keep = {layer.id() for layer in self._layers}
        for layer_id in [k for k in self._label_cache if k not in keep]:
            del self._label_cache[layer_id]         # a full-size picture per layer: do not keep stale ones
        if not self._layers:                        # nothing selected any more: no job, no copies
            self._cancel_job()
            self._scratch = {}
        self.setVisible(bool(self._layers))
        self.update()

    def _selected_layers(self):
        self._layers = [layer for layer in self._layers if not sip.isdeleted(layer)]
        return self._layers

    def _signature(self, settings_):
        """What the picture depends on. While Smooth Navigation moves this item
        every frame nothing here changes, so the picture is just drawn again
        instead of every selected feature being repainted in Python per frame."""
        layers = []
        for layer in self._selected_layers():
            if layer.selectedFeatureCount():
                edits = 0
                with suppress(Exception):
                    if layer.isEditable():
                        edits = layer.undoStack().index()       # moves with every edit
                layers.append((layer.id(), layer.selectedFeatureCount(), edits))
        size = settings_.deviceOutputSize()
        return (settings_.visibleExtent().toString(), size.width(), size.height(),
                round(settings_.rotation(), 6), self.canvas.devicePixelRatioF(), tuple(layers),
                settings_.testFlag(DRAW_SELECTION))

    def updatePosition(self):
        self.setPos(0, 0)

    def _selected(self):
        """[(layer, [features])] for the visible layers, or None if too many."""
        found, total, kept = [], 0, {}
        for layer in self._selected_layers():
            count = layer.selectedFeatureCount()
            if not count:
                continue
            total += count
            if total > MAX_FEATURES:
                return None
            # A pan or zoom repaints the same features: read them from the data source once per
            # selection (and per edit), not once per view - that read alone took ~100 ms on a big layer.
            key = self._feature_key(layer)
            entry = self._features.get(layer.id())
            if entry is None or entry[0] != key:
                entry = (key, layer.selectedFeatures())
            kept[layer.id()] = entry
            found.append((layer, entry[1]))
        self._features = kept
        return found

    @staticmethod
    def _feature_key(layer):
        edits = -1
        with suppress(Exception):
            if layer.isEditable():
                edits = layer.undoStack().index()       # moves with every edit
        return (tuple(sorted(layer.selectedFeatureIds())), edits)

    def paint(self, painter, option=None, widget=None):
        try:
            self._paint(painter)
        except Exception:               # never let a paint error reach Qt
            QgsMessageLog.logMessage(
                'Selection outline failed:{}{}'.format(chr(10), traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)

    def _paint(self, painter):
        settings_ = self.canvas.mapSettings()
        key = self._signature(settings_)
        if self._image is None or key != self._image_key:
            now = time.perf_counter()
            many = sum(layer.selectedFeatureCount() for layer in self._selected_layers()) > MANY_FEATURES
            if (many or self._build_cost > SLOW_BUILD_S) and self._rapid and now - self._changed_at < QUIET_S:
                self._later.start()                 # still changing: nothing is drawn (never a stale outline)
                return
            if many:
                if self._job is None or self._job_key != key:
                    self._start_job(settings_, key)
                return                              # the job's picture is drawn when it is ready
            self._cancel_job()
            dpr = self.canvas.devicePixelRatioF()
            image = QImage(round(self.canvas.width() * dpr), round(self.canvas.height() * dpr),
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(0)
            inner = QPainter(image)
            try:
                self._paint_features(inner)
            finally:
                inner.end()
            self._image, self._image_key = image, key
            self._build_cost = time.perf_counter() - now
        painter.drawImage(0, 0, self._image)

    # ------------------------------------------------------------- a big selection: background render
    def _cancel_job(self):
        job, self._job, self._job_key = self._job, None, None
        if job is not None:
            self._dying.append(job)
            with suppress(Exception):
                job.cancelWithoutBlocking()

    @staticmethod
    def _outline_symbol(layer):
        """The cyan outline as a QGIS symbol: a ring on a point, the line itself, the polygon boundary."""
        kind = layer.geometryType()
        width = str(LINE_WIDTH)
        if kind == QgsWkbTypes.GeometryType.PointGeometry:
            return QgsMarkerSymbol.createSimple({
                'name': 'circle', 'size': str(POINT_RADIUS * 2), 'size_unit': 'Pixel',
                'color': '0,255,255,90', 'outline_color': '0,255,255,255',
                'outline_width': width, 'outline_width_unit': 'Pixel'})
        if kind == QgsWkbTypes.GeometryType.LineGeometry:
            return QgsLineSymbol.createSimple({
                'color': '0,255,255,255', 'width': width, 'width_unit': 'Pixel',
                'joinstyle': 'round', 'capstyle': 'round'})
        return QgsFillSymbol.createSimple({
            'style': 'no', 'outline_color': '0,255,255,255', 'outline_width': width,
            'outline_width_unit': 'Pixel', 'joinstyle': 'round'})

    def _scratch_layers(self, layer, features, with_symbols=True):
        """[outline layer, symbol layer] holding copies of the selected features of *layer*: the first
        draws the cyan outline, the second the layer's own symbols (and labels) over QGIS's hidden
        selection paint. Made once per selection, not per view."""
        key = (self._feature_key(layer), with_symbols)
        entry = self._scratch.get(layer.id())
        if entry is not None and entry[0] == key:
            return entry[1]
        uri = "{}?crs={}".format(QgsWkbTypes.displayString(layer.wkbType()), layer.crs().authid() or "EPSG:4326")
        made = []
        for role in (("outline", "symbols") if with_symbols else ("outline",)):
            scratch = QgsVectorLayer(uri, "kga-selection-" + role, "memory")
            if not scratch.isValid():
                return []
            scratch.setCrs(layer.crs())
            provider = scratch.dataProvider()
            provider.addAttributes(layer.fields().toList())
            scratch.updateFields()
            provider.addFeatures([QgsFeature(f) for f in features])    # copies: the provider renumbers them
            if role == "outline":
                scratch.setRenderer(QgsSingleSymbolRenderer(self._outline_symbol(layer)))
            else:
                renderer = layer.renderer()
                if renderer is None:
                    continue
                scratch.setRenderer(renderer.clone())
                scratch.setOpacity(layer.opacity())
                if layer.labelsEnabled() and layer.labeling() is not None and len(features) <= MAX_LABELLED:
                    scratch.setLabeling(layer.labeling().clone())
                    scratch.setLabelsEnabled(True)
            made.append(scratch)
        self._scratch[layer.id()] = (key, made)
        return made

    def _start_job(self, map_settings, key):
        self._cancel_job()
        selected = self._selected()
        if not selected:
            self._scratch = {}
            return
        outlines, symbols, keep = [], [], {}
        for layer, features in selected:
            # the symbols (and labels) only have to be drawn again where QGIS painted its selection
            # colour over them; a map that ignores the selection still shows them
            made = self._scratch_layers(layer, features, map_settings.testFlag(DRAW_SELECTION))
            if layer.id() in self._scratch:
                keep[layer.id()] = self._scratch[layer.id()]
            for scratch in made:
                (outlines if scratch.name().endswith("outline") else symbols).append(scratch)
        self._scratch = keep                        # layers that lost their selection give their copies back
        if not outlines:
            return
        settings_ = QgsMapSettings(map_settings)
        settings_.setLayers(outlines + symbols)     # the first is drawn on top
        settings_.setBackgroundColor(QColor(0, 0, 0, 0))
        job = QgsMapRendererParallelJob(settings_)
        job.finished.connect(lambda j=job, k=key: self._job_done(j, k))
        self._job, self._job_key = job, key
        job.start()

    def _job_done(self, job, key):
        with suppress(RuntimeError, TypeError):
            job.finished.disconnect()               # break the job <-> slot cycle so it is freed
        if job in self._dying:
            self._dying.remove(job)
            return
        if job is not self._job or sip.isdeleted(self):
            return
        self._job, self._job_key = None, None
        image = job.renderedImage()
        if image is None or image.isNull():
            return
        self._image, self._image_key = image, key
        self._build_cost = 0.0
        self.update()

    def _paint_features(self, painter):
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
        if sum(len(features) for _layer, features in selected) > MANY_FEATURES:
            pens = [core]
        extent = settings_.visibleExtent()
        size = settings_.outputSize()
        view = QgsRectangle(-CLIP_MARGIN_PX, -CLIP_MARGIN_PX,
                            size.width() + CLIP_MARGIN_PX, size.height() + CLIP_MARGIN_PX)
        for layer, features in selected:
            transform = None
            if layer.crs() != settings_.destinationCrs():
                transform = QgsCoordinateTransform(
                    layer.crs(), settings_.destinationCrs(), QgsProject.instance())
            # Only what is in view is drawn: zoomed in on a few of a thousand selected features, the
            # rest used to have their symbol, label and outline worked out all the same.
            shown, shapes = [], []
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
                if not geometry.boundingBox().intersects(extent):
                    continue
                if QgsWkbTypes.isCurvedType(geometry.wkbType()):
                    geometry = QgsGeometry(geometry.constGet().segmentize())
                shown.append(feature)
                shapes.append(geometry)
            if not shown:
                continue
            if settings_.testFlag(DRAW_SELECTION):  # QGIS painted over them: put symbol and label back
                self._symbols(painter, settings_, layer, shown, transform)
                self._labels(painter, settings_, layer, shown)
            if len(shapes) > 1 and QgsWkbTypes.geometryType(shapes[0].wkbType()) != QgsWkbTypes.GeometryType.PointGeometry:
                # one outline for the whole layer: cut, simplified and stroked once, not once per feature
                merged = None
                with suppress(Exception):
                    merged = QgsGeometry.collectGeometry(shapes)
                if merged is not None and not merged.isNull() and not merged.isEmpty():
                    shapes = [merged]
            vertex_shapes = list(shapes) if self._marks_vertices(layer, settings_) else []
            for geometry in shapes:
                self._draw(painter, geometry, to_pixel, pens, view)
            if vertex_shapes:
                self._vertex_markers(painter, settings_, vertex_shapes, to_pixel, view)

    @staticmethod
    def _marks_vertices(layer, map_settings):
        """True if this overlay has to mark the vertices of the layer's selected features itself.

        QGIS marks them only while it paints the selection, and that paint also replaces the symbols of
        the selected features, which the overlay then redraws above every layer (a selected polygon came
        out in front of the points over it). So while a layer is edited the map ignores the selection and
        the markers are drawn here, the way QGIS would: only for the selected features, unless the user
        asked for all of them (then QGIS draws them with the layer, selected or not)."""
        if map_settings.testFlag(DRAW_SELECTION) or not layer.isEditable():
            return False
        with suppress(Exception):
            from qgis.core import QgsSettings
            return QgsSettings().value('qgis/digitizing/marker_only_for_selected', True, type=bool)
        return True

    @staticmethod
    def _vertex_markers(painter, map_settings, shapes, to_pixel, view):
        """QGIS's editing vertex markers (red cross or translucent circle) on the vertices of *shapes*."""
        style, mm = 'Cross', 2.0
        with suppress(Exception):
            from qgis.core import QgsSettings
            options = QgsSettings()
            style = str(options.value('qgis/digitizing/marker_style', 'Cross'))
            mm = float(options.value('qgis/digitizing/marker_size_mm', 2.0))
        if style == 'None':
            return
        half = mm * map_settings.outputDpi() / 25.4 / 2.0
        with suppress(Exception):
            context = QgsRenderContext.fromMapSettings(map_settings)
            half = context.convertToPainterUnits(mm, Qgis.RenderUnit.Millimeters) / 2.0
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            if style == 'Cross':
                painter.setPen(QPen(QColor(255, 0, 0), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            else:
                painter.setPen(QPen(QColor(255, 0, 0, 65), 1))
                painter.setBrush(QBrush(QColor(255, 0, 0, 40)))
            budget = 20000                          # a vertex or two thousand per feature is plenty to show
            for geometry in shapes:
                for vertex in geometry.vertices():
                    budget -= 1
                    if budget < 0:
                        return
                    p = to_pixel.transform(vertex.x(), vertex.y())
                    x, y = p.x(), p.y()
                    if x < view.xMinimum() or x > view.xMaximum() or y < view.yMinimum() or y > view.yMaximum():
                        continue
                    if style == 'Cross':
                        painter.drawLine(QPointF(x - half, y - half), QPointF(x + half, y + half))
                        painter.drawLine(QPointF(x - half, y + half), QPointF(x + half, y - half))
                    else:
                        painter.drawEllipse(QPointF(x, y), half, half)
        finally:
            painter.restore()

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

    @staticmethod
    def _outline_path(geometry, to_pixel, view):
        """The outline of a line or polygon as a painter path in pixels, or None to fall back.

        The whole way from map coordinates to the path stays inside QGIS: converting every vertex in
        Python and stroking the full-detail outline took over 100 ms for one selected district, on
        every view change. The outline is cut to the view (plus a margin) and simplified to less than
        half a pixel first, so what is stroked is small whatever the feature."""
        try:
            shape = QgsGeometry(geometry)
            shape.mapToPixel(to_pixel)
            if not view.contains(shape.boundingBox()):
                shape = shape.clipped(view)
                if shape.isNull() or shape.isEmpty():
                    return QPainterPath()           # nothing of it is in view
            simple = shape.simplify(SIMPLIFY_PX)
            if not simple.isNull() and not simple.isEmpty():
                shape = simple
            return shape.constGet().asQPainterPath()
        except Exception:
            return None

    def _draw(self, painter, geometry, to_pixel, pens, view=None):
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
        path = self._outline_path(geometry, to_pixel, view) if view is not None else None
        if path is not None:
            if not path.isEmpty():
                for pen in pens:
                    painter.setPen(pen)
                    painter.drawPath(path)
            return
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
        self._edit_watch = {}           # layer id -> layer whose editing start / stop is followed

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
        self._unwatch_editing()
        self._draw_selection(True)
        self._colour(QColor(255, 255, 0, 255))

    # ------------------------------------------------------------- behaviour

    def refresh(self, *_args):
        """Follow the Settings switch: Pro look on, or QGIS's yellow back."""
        try:
            if settings.pro_selection():
                if self.overlay is None:
                    self.overlay = _Overlay(self.canvas)
                    self.overlay.invalidate()       # what is selected right now
                self._update()
            else:
                self._remove_overlay()
                self._unwatch_editing()
                self._draw_selection(True)
                self._colour(QColor(255, 255, 0, 255))
        except RuntimeError:            # the plugin was unloaded meanwhile
            pass

    def _colour(self, colour):
        self.canvas.setSelectionColor(colour)
        project = QgsProject.instance()
        if project.selectionColor() != colour:
            project.setSelectionColor(colour)

    def _editing(self):
        """True while a layer on the map is in edit mode; also follows the layers starting / stopping it."""
        editing, current = False, {}
        for layer in self.canvas.layers():
            if not isinstance(layer, QgsVectorLayer):
                continue
            current[layer.id()] = layer
            editing = editing or layer.isEditable()
            if layer.id() not in self._edit_watch:
                layer.editingStarted.connect(self._editing_changed)
                layer.editingStopped.connect(self._editing_changed)
        self._edit_watch = current
        return editing

    def _unwatch_editing(self):
        for layer in self._edit_watch.values():
            if not sip.isdeleted(layer):
                for signal in (layer.editingStarted, layer.editingStopped):
                    with suppress(Exception):
                        signal.disconnect(self._editing_changed)
        self._edit_watch = {}

    def _editing_changed(self, *_args):
        """A layer started or stopped editing: its vertex markers come or go, so the overlay paints again."""
        if self.overlay is not None:
            with suppress(RuntimeError):
                self.overlay.invalidate()
        self._update()

    def _draw_selection(self, on):
        """Switch QGIS's own selection paint on the canvas on or off (changing it redraws the map once)."""
        with suppress(Exception):
            flags = self.canvas.mapSettings().flags()
            if self.canvas.mapSettings().testFlag(DRAW_SELECTION) != bool(on):
                self.canvas.setMapSettingsFlags(flags | DRAW_SELECTION if on else flags & ~DRAW_SELECTION)

    def _remove_overlay(self):
        if self.overlay is not None:
            with suppress(Exception):
                self.overlay.detach()
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
                # QGIS only has to draw selections itself for a selection too big for the overlay (its
                # translucent fill) and for a big selection in an edited layer (it marks the vertices of
                # the selected features; a smaller one gets its markers from the overlay, because QGIS's
                # selection paint would take the symbols out of the layer order). Otherwise the map
                # ignores the selection: see map_ignores_selection.
                self._draw_selection(big or (self._editing() and selected_total(self.canvas) > MANY_FEATURES))
                # the overlay follows the selection, the layers and the view by itself

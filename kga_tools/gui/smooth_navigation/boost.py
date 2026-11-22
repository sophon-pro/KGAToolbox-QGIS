# -*- coding: utf-8 -*-
"""Rendering speed-ups for one map canvas (all runtime, all reversible).

1. Context underlay
   After every completed canvas render, a LOW-resolution image of a larger
   area (default 5x the view, labels off) is rendered in background threads
   and placed *behind* the normal map image. When you zoom out or pan, the
   area outside the cached view is filled instantly from this underlay
   instead of showing a blank background while QGIS re-renders. Online
   basemaps (XYZ/WMS) use lower zoom-level tiles for it, so it is cheap.

2. Progressive display
   While navigating, the canvas "map update interval" is raised for this canvas
   only (QGIS default 250 ms) so partial renders do not replace the stretched
   map; it goes back to the stock value once that render completes.

3. Render cache + parallel rendering
   Forced ON for this canvas only while the plugin is ON.

Every value changed here is a per-canvas runtime property (not a QGIS
setting) and is restored when the plugin is switched off or unloaded.
"""
from contextlib import suppress

from qgis.core import (
    QgsMapRendererParallelJob, QgsMapSettings, QgsMessageLog, QgsPointXY,
    QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QElapsedTimer, QPoint, QRectF, QTimer
from qgis.PyQt.QtWidgets import QGraphicsItem

_UNDERLAY_Z = -1.0e6
_UNDERLAY_DELAY_MS = 700       # wait after a render before starting underlay
_LAYERS_SETTLE_MS = 2500       # ...and after the layer set changed
# The underlay is a second render of the map over a larger area. Starting it prepares every layer
# on the GUI thread and finishing it composes every layer's image there too, so on a map with
# many layers each view change was followed by two pauses of 60-90 ms (and seconds of background
# rendering that competes with the next real render). A map that is heavy - it took this long to
# draw, or has this many layers - gets a light underlay: the basemap and rasters only, which is
# what fills the edges best anyway; its vector layers appear with the normal render.
_HEAVY_RENDER_MS = 1000
_HEAVY_LAYERS = 30


def _map_flag(name):
    try:
        from qgis.core import Qgis
        return getattr(Qgis.MapSettingsFlag, name)
    except AttributeError:
        return getattr(QgsMapSettings, name, None)


class _UnderlayItem(QGraphicsItem):
    """Draws the context image scaled into its rect (no own transform use)."""

    def __init__(self):
        super().__init__()
        self.image = None
        self._w = 0.0
        self._h = 0.0
        self.setZValue(_UNDERLAY_Z)
        self.setAcceptedMouseButtons(self._no_buttons())
        self.setVisible(False)

    @staticmethod
    def _no_buttons():
        from qgis.PyQt.QtCore import Qt
        return Qt.MouseButton.NoButton

    def set_size(self, w, h):
        if w != self._w or h != self._h:
            self.prepareGeometryChange()
            self._w, self._h = w, h

    def boundingRect(self):  # noqa: N802 (Qt API)
        return QRectF(0.0, 0.0, self._w, self._h)

    def paint(self, painter, option, widget=None):
        if self.image is not None and self._w > 0 and self._h > 0:
            painter.drawImage(QRectF(0.0, 0.0, self._w, self._h), self.image)


class CanvasBooster:
    """Owns the underlay and the per-canvas render tuning for one canvas."""

    def __init__(self, canvas, config, is_idle):
        self.canvas = canvas
        self.cfg = config
        self.is_idle = is_idle          # callable -> True when no motion
        self._item = None
        self._job = None
        self._dying = []                # cancelled jobs kept alive until finished
        self._job_meta = None
        self._meta = None               # (center QgsPointXY, mupp, rotation, w, h)
        self._placed = False            # the item has a valid place for the committed view
        self._last_key = None
        self._saved = {}
        self._installed = False
        # The underlay only shows where the map image does not reach: after the extent changed and
        # until that view is rendered (_stale), and while a pan / zoom-out exposes the area around
        # the image (_exposed). The rest of the time it is hidden behind an opaque map image, and a
        # hidden item costs nothing; a visible one is resampled into every repaint of the canvas.
        self._stale = False
        self._exposed = False
        self._render_clock = QElapsedTimer()
        self._render_ms = 0             # how long the last map render took
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_underlay_job)

    # ------------------------------------------------------------ lifecycle
    def install(self):
        if self._installed:
            return
        self._installed = True
        self.canvas.mapCanvasRefreshed.connect(self._on_canvas_rendered)
        self.canvas.renderStarting.connect(self._on_render_starting)
        self.canvas.extentsChanged.connect(self._on_extent_changed)
        self.canvas.layersChanged.connect(self._on_layers_changed)
        self.apply_config()

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        for sig, slot in ((self.canvas.mapCanvasRefreshed, self._on_canvas_rendered),
                          (self.canvas.renderStarting, self._on_render_starting),
                          (self.canvas.extentsChanged, self._on_extent_changed),
                          (self.canvas.layersChanged, self._on_layers_changed)):
            with suppress(RuntimeError, TypeError):
                sig.disconnect(slot)
        self._timer.stop()
        self.cancel()
        self._remove_item()
        self._restore_tuning()

    def apply_config(self):
        if not self._installed:
            return
        self._restore_tuning()
        if self.cfg.boost_canvas:
            self._tune("isCachingEnabled", "setCachingEnabled", True)
            self._tune("isParallelRenderingEnabled", "setParallelRenderingEnabled", True)
        if self.cfg.context_underlay:
            self._last_key = None
            self._timer.start(_UNDERLAY_DELAY_MS)
        else:
            self._timer.stop()
            self.cancel()
            self._remove_item()

    # ----------------------------------------------------- per-canvas tuning
    def _tune(self, getter, setter, value):
        c = self.canvas
        if not (hasattr(c, getter) and hasattr(c, setter)):
            return
        try:
            self._saved[setter] = getattr(c, getter)()
            getattr(c, setter)(value)
        except Exception as exc:
            QgsMessageLog.logMessage("tuning %s failed: %r" % (setter, exc), "Smooth Navigation")

    def begin_motion(self):
        """Slow the progressive display only while the map is being navigated
        (partial renders would replace the stretched image); every other render,
        e.g. adding layers, keeps QGIS's own 250 ms updates so data appears at once."""
        if not (self._installed and self.cfg.boost_canvas):
            return
        if "setMapUpdateInterval" not in self._saved:
            self._tune("mapUpdateInterval", "setMapUpdateInterval", int(self.cfg.progressive_ms))

    def _restore_interval(self):
        value = self._saved.pop("setMapUpdateInterval", None)
        if value is not None:
            with suppress(Exception):
                self.canvas.setMapUpdateInterval(value)

    def _restore_tuning(self):
        for setter, value in self._saved.items():
            with suppress(Exception):
                getattr(self.canvas, setter)(value)
        self._saved = {}

    # ------------------------------------------------------------- underlay
    def cancel(self):
        """Stop a running underlay render (called when motion starts)."""
        self._timer.stop()
        if self._job is not None:
            job = self._job
            self._job = None
            self._dying.append(job)
            with suppress(Exception):
                job.cancelWithoutBlocking()

    def _on_render_starting(self):
        self._render_clock.start()

    def _on_canvas_rendered(self):
        if not getattr(self, "_installed", False):
            return
        if self._render_clock.isValid():
            self._render_ms = self._render_clock.elapsed()
            self._render_clock.invalidate()
        self._restore_interval()
        self._stale = False                 # the map image covers the view again
        self._show_item()
        if self.cfg.context_underlay:
            self._timer.start(_UNDERLAY_DELAY_MS)

    def _on_layers_changed(self):
        """Layers are being added/removed: hold the underlay back (its start-up
        prepares every layer on the GUI thread) until the layer set settles."""
        if not getattr(self, "_installed", False):
            return
        self.cancel()
        if self.cfg.context_underlay:
            self._timer.start(_LAYERS_SETTLE_MS)

    def _on_extent_changed(self):
        if not getattr(self, "_installed", False):
            return
        self.cancel()
        self._stale = True                  # the old image is stretched to the new view until it is rendered
        self.place()

    def set_exposed(self, exposed):
        """The navigator says whether its uncommitted pan / zoom shows anything beside the map image."""
        exposed = bool(exposed)
        if exposed != self._exposed:
            self._exposed = exposed
            self._show_item()

    def _show_item(self):
        item = self._item
        if item is None:
            return
        with suppress(RuntimeError):
            wanted = bool(self._placed and (self._stale or self._exposed))
            if item.isVisible() != wanted:
                item.setVisible(wanted)

    def _key(self, ms):
        e = ms.extent()
        return (round(e.xMinimum(), 9), round(e.yMinimum(), 9), round(e.xMaximum(), 9),
                round(e.yMaximum(), 9), ms.rotation(), ms.outputSize().width(),
                ms.outputSize().height(), tuple(layer.id() for layer in ms.layers()))

    def _start_underlay_job(self):
        if not getattr(self, "_installed", False):
            return
        if not (self._installed and self.cfg.context_underlay):
            return
        if not self.is_idle() or self.canvas.isDrawing():
            self._timer.start(_UNDERLAY_DELAY_MS)      # try again later
            return
        base = self.canvas.mapSettings()
        layers = base.layers()
        if not base.hasValidSettings() or not layers:
            return
        heavy = self._render_ms >= _HEAVY_RENDER_MS or len(layers) > _HEAVY_LAYERS
        if heavy:
            layers = [layer for layer in layers if not isinstance(layer, QgsVectorLayer)]
            if not layers:                             # nothing cheap to draw: no underlay for this map
                self._remove_item()
                return
        key = self._key(base) + (heavy,)
        if key == self._last_key and self._meta is not None:
            return                                     # underlay already current
        ms = QgsMapSettings(base)
        if heavy:
            ms.setLayers(layers)
        ext = QgsRectangle(ms.extent())
        ext.scale(max(1.5, float(self.cfg.underlay_factor)))
        ms.setExtent(ext)
        # A context image only has to look roughly right: no labels, no selection
        # highlight, no antialiasing, half resolution (a quarter of the pixels).
        for name, on in (("DrawLabeling", False), ("DrawSelection", False),
                         ("Antialiasing", False), ("UseRenderingOptimization", True)):
            flag = _map_flag(name)
            if flag is not None:
                ms.setFlag(flag, on)
        ms.setOutputSize(ms.outputSize() / 2)
        ms.setOutputDpi(ms.outputDpi() / 2.0)
        if not ms.hasValidSettings():
            return
        size = ms.outputSize()
        meta = (QgsPointXY(ms.visibleExtent().center()), ms.mapUnitsPerPixel(),
                ms.rotation(), float(size.width()), float(size.height()))
        job = QgsMapRendererParallelJob(ms)
        job.finished.connect(lambda j=job, m=meta, k=key: self._job_done(j, m, k))
        self._job = job
        job.start()

    def _job_done(self, job, meta, key):
        with suppress(RuntimeError, TypeError):
            job.finished.disconnect()       # break job<->slot cycle so it is freed
        if job in self._dying:
            self._dying.remove(job)
            return
        if job is not self._job:
            return
        self._job = None
        if not self._installed:
            return
        img = job.renderedImage()
        if img is None or img.isNull():
            return
        item = self._ensure_item()
        if item is None:
            return
        item.image = img
        self._meta = meta
        self._last_key = key
        self.place()

    def _ensure_item(self):
        scene = self.canvas.scene()
        if scene is None:
            return None
        if self._item is None:
            self._item = _UnderlayItem()
            scene.addItem(self._item)
        return self._item

    def _remove_item(self):
        if self._item is not None:
            with suppress(RuntimeError):
                scene = self._item.scene()
                if scene is not None:
                    scene.removeItem(self._item)
            self._item = None
        self._meta = None
        self._placed = False
        self._last_key = None

    def place(self):
        """Position the underlay for the canvas's current (committed) view."""
        item, meta = self._item, self._meta
        if item is None or meta is None:
            return
        center, u_mupp, rot, iw, ih = meta
        with suppress(RuntimeError):
            self._placed = False
            mupp = self.canvas.mapUnitsPerPixel()
            if abs(self.canvas.rotation() - rot) > 1e-9 or mupp <= 0:
                item.setVisible(False)              # rotation changed: stale
                return
            f = u_mupp / mupp                      # screen px per underlay px
            if f > 16.0:                           # far zoomed in: never visible
                item.setVisible(False)
                return
            p = self.canvas.getCoordinateTransform().transform(center)
            off = self.canvas.mapToScene(QPoint(0, 0))
            w, h = iw * f, ih * f
            item.set_size(w, h)
            item.setPos(off.x() + p.x() - w / 2.0, off.y() + p.y() - h / 2.0)
            self._placed = True
            self._show_item()

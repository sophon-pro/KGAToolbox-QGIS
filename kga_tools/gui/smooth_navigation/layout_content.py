# -*- coding: utf-8 -*-
"""Smooth zoom and glide for the layout designer's Move Item Content tool.

QGIS zooms the content of a map frame one wheel notch at a time and stops the
moment the mouse is released. This controller sits on the layout view's
viewport and, only while the Move Item Content tool is active:

* turns a wheel notch into an eased zoom of the item's content (the same
  critically damped spring as the map canvas), applied as many small
  ``zoomContent`` calls inside one undo step;
* lets a quick drag glide on after release (``moveContent`` with decaying
  velocity), inside one undo step.

Everything else (other tools, Ctrl+wheel, the stock drag itself) is left to
QGIS. Removing the filter restores stock behaviour.
"""
import math
from collections import deque
from contextlib import suppress

from qgis.core import QgsSettings
from qgis.gui import QgsLayoutViewToolMoveItemContent
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QElapsedTimer, QEvent, QObject, QPointF, Qt, QTimer

from .compat import event_pos, has_modifiers

_FRAME_MS = 16
_ZOOM_EPS = 2e-3
_MIN_SPEED = 6.0               # item units/s below which a glide stops
_MAX_PENDING_STEPS = 6
_SAMPLE_WINDOW_MS = 90
_FLICK_MAX_IDLE_MS = 50


class LayoutContentNavigator(QObject):
    """Smooth Move Item Content for one ``QgsLayoutView``."""

    def __init__(self, view, config):
        super().__init__(view)
        self.view = view
        self.viewport = view.viewport()
        self.cfg = config
        self._installed = False

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(_FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._finish)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ms = 0.0

        self._item = None
        self._command = False          # an undo command is open on _item
        self._zoom_remaining = 0.0     # log(zoom factor) still to apply (+ = in)
        self._zoom_vel = 0.0
        self._anchor = None            # scene point the zoom is about
        self._vx = 0.0                 # glide velocity, item units/s
        self._vy = 0.0
        self._drag_item = None
        self._samples = deque(maxlen=32)
        self._zf_cache = (0, 2.0)

    # ------------------------------------------------------------------ setup
    def install(self):
        if self._installed:
            return
        self.viewport.installEventFilter(self)
        self._installed = True

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        with suppress(RuntimeError):
            self.viewport.removeEventFilter(self)
        self._timer.stop()
        self._settle.stop()
        self._finish()

    # ------------------------------------------------------------------ utils
    def _active(self):
        tool = self.view.tool()
        return tool is not None and isinstance(tool, QgsLayoutViewToolMoveItemContent)

    def _alive(self):
        return self._item is not None and not sip.isdeleted(self._item)

    def _zoom_factor(self):
        now = self._clock.elapsed()
        stamp, cached = self._zf_cache
        if stamp and now - stamp < 1000:
            return cached
        zf = QgsSettings().value('qgis/zoom_factor', 2.0, type=float)
        zf = zf if zf and zf > 1.0001 else 2.0
        self._zf_cache = (now or 1, zf)
        return zf

    def _scene_point(self, e):
        x, y = event_pos(e)
        return self.view.mapToScene(int(round(x)), int(round(y)))

    def _item_at(self, scene_pt):
        layout = self.view.currentLayout()
        if layout is None:
            return None
        item = layout.layoutItemAt(scene_pt, True)      # ignore locked items
        return item

    def _begin(self, item):
        if self._command and self._item is item:
            return
        self._finish()
        self._item = item
        item.beginCommand('Move item content')
        self._command = True

    def _finish(self):
        """Close the open undo step and forget the item."""
        self._zoom_remaining = self._zoom_vel = 0.0
        self._vx = self._vy = 0.0
        if self._command and self._alive():
            with suppress(RuntimeError):
                self._item.endCommand()
        self._command = False
        self._item = None

    def _stop_motion(self):
        self._timer.stop()
        self._settle.stop()
        self._zoom_remaining = self._zoom_vel = 0.0
        self._vx = self._vy = 0.0

    # ----------------------------------------------------------- event filter
    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if not getattr(self, '_installed', False) or obj is not self.viewport:
            return False
        try:
            et = event.type()
            if et == QEvent.Type.Wheel:
                return self._on_wheel(event)
            if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                self._on_press(event)
            elif et == QEvent.Type.MouseMove:
                self._on_move(event)
            elif et == QEvent.Type.MouseButtonRelease:
                self._on_release(event)
        except Exception:
            self._stop_motion()
            self._finish()
        return False                    # the stock tool still gets every mouse event

    # ------------------------------------------------------------------ wheel
    def _on_wheel(self, e):
        if not self.cfg.smooth_zoom or has_modifiers(e) or not self._active():
            return False
        dy = e.angleDelta().y()
        if dy == 0:
            return False
        scene_pt = self._scene_point(e)
        item = self._item_at(scene_pt)
        if item is None:
            return False
        if self._item is not item:
            self._stop_motion()
            self._begin(item)
        self._vx = self._vy = 0.0
        log_delta = (dy / 120.0) * math.log(self._zoom_factor())    # + = zoom in
        if self._zoom_remaining * log_delta < 0:
            self._zoom_remaining = self._zoom_vel = 0.0
        limit = _MAX_PENDING_STEPS * math.log(self._zoom_factor())
        self._zoom_remaining = max(-limit, min(limit, self._zoom_remaining + log_delta))
        self._anchor = scene_pt
        self._start()
        e.accept()
        return True

    # ------------------------------------------------------------------ mouse
    def _on_press(self, e):
        self._stop_motion()
        self._finish()
        self._samples.clear()
        self._drag_item = None
        if e.button() == Qt.MouseButton.LeftButton and self._active():
            self._drag_item = self._item_at(self._scene_point(e))

    def _on_move(self, e):
        item = self._drag_item
        if item is None or sip.isdeleted(item):
            return
        local = item.mapFromScene(self._scene_point(e))
        self._samples.append((self._clock.nsecsElapsed() / 1e6, local.x(), local.y(), item))

    def _on_release(self, e):
        item, self._drag_item = self._drag_item, None
        samples, self._samples = list(self._samples), deque(maxlen=32)
        if (item is None or sip.isdeleted(item) or not self.cfg.inertia
                or e.button() != Qt.MouseButton.LeftButton or len(samples) < 2):
            return
        now = self._clock.nsecsElapsed() / 1e6
        if now - samples[-1][0] > _FLICK_MAX_IDLE_MS:
            return                              # the hand stopped before letting go
        recent = [s for s in samples if now - s[0] <= _SAMPLE_WINDOW_MS]
        if len(recent) < 2:
            return
        t0, x0, y0, _ = recent[0]
        t1, x1, y1, _ = recent[-1]
        dt = (t1 - t0) / 1000.0
        if dt <= 0:
            return
        # moveContent(dx, dy) shifts the content the opposite way to a mouse delta;
        # the content followed the hand, so keep going in the same direction.
        vx, vy = -(x1 - x0) / dt, -(y1 - y0) / dt
        # item units/s, scaled to item size so the glide feels the same on any frame
        if math.hypot(vx, vy) < _MIN_SPEED * 4:
            return
        # our filter sees the release before the stock tool closes its own undo
        # step, so start the glide once that has happened
        QTimer.singleShot(0, lambda: self._begin_glide(item, vx, vy))

    def _begin_glide(self, item, vx, vy):
        if (not getattr(self, '_installed', False) or sip.isdeleted(item)
                or self._timer.isActive() or self._drag_item is not None):
            return
        self._begin(item)
        self._vx, self._vy = vx, vy
        self._start()

    # -------------------------------------------------------------- animation
    def _start(self):
        self._settle.stop()
        if not self._timer.isActive():
            self._last_ms = self._clock.nsecsElapsed() / 1e6 - _FRAME_MS
            self._timer.start()

    def _tick(self):
        if not getattr(self, '_installed', False):
            return
        try:
            if not self._alive():
                self._stop_motion()
                self._finish()
                return
            now = self._clock.nsecsElapsed() / 1e6
            dt = min(max(now - self._last_ms, 0.5), 50.0) / 1000.0
            self._last_ms = now
            moved = self._step_glide(dt)
            zoomed = self._step_zoom(dt)
            if not (moved or zoomed):
                self._timer.stop()
                self._settle.start(60)
        except Exception:
            self._stop_motion()
            self._finish()

    def _step_glide(self, dt):
        if not (self._vx or self._vy):
            return False
        decay = math.exp(-dt / max(self.cfg.inertia_tau_ms / 1000.0, 0.01))
        self._vx *= decay
        self._vy *= decay
        if math.hypot(self._vx, self._vy) < _MIN_SPEED:
            self._vx = self._vy = 0.0
            return False
        self._item.moveContent(self._vx * dt, self._vy * dt)
        return True

    def _step_zoom(self, dt):
        r = self._zoom_remaining
        if abs(r) < _ZOOM_EPS:
            self._zoom_remaining = self._zoom_vel = 0.0
            return False
        w = 2.0 / (max(float(self.cfg.zoom_tau_ms), 1.0) / 1000.0)
        v = self._zoom_vel
        decay = math.exp(-w * dt)
        b = v + w * r
        left = (r + b * dt) * decay
        self._zoom_vel = (v - w * b * dt) * decay
        step = r - left
        if abs(left) < _ZOOM_EPS:
            step, left, self._zoom_vel = r, 0.0, 0.0
        self._zoom_remaining = left
        point = self._item.mapFromScene(self._anchor or QPointF())
        self._item.zoomContent(math.exp(step), point)
        return True

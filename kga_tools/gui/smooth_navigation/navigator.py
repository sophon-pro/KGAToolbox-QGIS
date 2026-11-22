# -*- coding: utf-8 -*-
"""Per-canvas smooth navigation controller.

How it stays fast
-----------------
QGIS already keeps the last rendered map as a cached image inside the canvas
scene (``QgsMapCanvasMap`` plus rubber bands, markers, annotations...). While
the user is zooming or gliding, this controller does NOT change the map
extent; it only applies a cheap 2D similarity transform (scale + translate)
to the scene items, so the cached image moves/zooms at display rate with no
layer rendering at all.

When motion has been idle for ``settle_delay_ms`` the accumulated transform is
converted into exactly ONE extent change (``zoomByFactor`` about the computed
centre) and QGIS's normal asynchronous, cached, multi-threaded renderer draws
the new view once. Benefits:

* zero vector/raster rendering cost while moving (no stutter on heavy layers);
* wheel bursts and flicks are coalesced into a single render;
* Zoom Last / Zoom Next history gets one entry per gesture;
* other plugins see one ``extentsChanged`` per gesture, not 60 per second.

Everything is driven by event filters installed on the canvas and its
viewport. Removing the filters (toggle OFF) restores stock QGIS behaviour.
"""
import math
import os
from collections import deque
from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog, QgsPointXY
from qgis.gui import QgsMapTool, QgsMapToolPan, QgsRubberBand
from qgis.PyQt.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QPointF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QCursor, QMouseEvent, QPainter, QPixmap, QTransform
from qgis.PyQt.QtWidgets import QApplication, QGraphicsItem

from ...branding import ICON_DIR
from ...core.kga_settings import shared as _settings
from .boost import CanvasBooster
from .compat import event_pos, flag_value, has_modifiers

_SMOOTH = QPainter.RenderHint.SmoothPixmapTransform
_FRAME_MS = 16                # slowest animation clock (60 fps)
_MIN_FRAME_MS = 8             # fastest (120 fps): faster only starves the event loop
# Only static items may be cached: a digitizing tool resizes its QgsRubberBand on
# every mouse move, and Qt then stretches the stale cache to the new bounds. A
# QgsHighlight resizes on every extent change too, so with a fixed cache size the
# Attributes pane's parcel highlight was drawn ~20x too big (huge blue band) while
# zooming (seen 2026-11-21 in the live QGIS; not reproducible headless). Nothing is
# cached now: an outline is cheap, and a stale cache is a visible bug.
_VECTOR_ITEMS = ()
_CACHE_SPAN = 1.5             # item cache size, in viewports (caps memory for huge outlines)
_MIN_SPEED = 12.0              # px/s below which gliding stops
_MAX_SPEED = 9000.0            # px/s flick cap
_ZOOM_EPS = 2e-3               # remaining log-zoom considered "arrived" (0.2 %, invisible)
_MAX_PENDING_ZOOM_STEPS = 6    # wheel notches that may be queued
_SAMPLE_WINDOW_MS = 90         # drag history used for flick velocity
_FLICK_MAX_IDLE_MS = 50        # pause before release => no flick
_KEY_ACCEL_TAU = 0.10          # s, arrow-key ramp-up
_KEY_STOP_TAU = 0.12           # s, arrow-key stop when inertia is off
_DRAG_ZOOM_PER_PX = 0.006      # right-drag: ln(zoom factor) per pixel (about 2x per 115 px)
_CLICK_SLOP_PX = 3.0           # Pan-tool press/release within this = click

# pixel shift of the *map content* per arrow key (screen y grows downwards)
_ARROWS = {
    flag_value(Qt.Key.Key_Left): (1.0, 0.0),
    flag_value(Qt.Key.Key_Right): (-1.0, 0.0),
    flag_value(Qt.Key.Key_Up): (0.0, 1.0),
    flag_value(Qt.Key.Key_Down): (0.0, -1.0),
}
# +1 = zoom out, -1 = zoom in (QGIS convention: PgUp zooms in)
_ZOOM_KEYS = {
    flag_value(Qt.Key.Key_PageUp): -1.0,
    flag_value(Qt.Key.Key_PageDown): 1.0,
}


_CURSOR_PX = 32
_zoom_cursors = {}


def _zoom_cursor(zoom_in):
    """The magnifier (zoomIn.png / zoomOut.png) with its hotspot on the lens."""
    cursor = _zoom_cursors.get(zoom_in)
    if cursor is None:
        pixmap = QPixmap(os.path.join(ICON_DIR, 'navigation/zoomIn.png' if zoom_in else 'navigation/zoomOut.png'))
        if pixmap.isNull():
            return QCursor(Qt.CursorShape.SizeVerCursor)
        pixmap = pixmap.scaled(_CURSOR_PX, _CURSOR_PX, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        cursor = _zoom_cursors[zoom_in] = QCursor(
            pixmap, int(_CURSOR_PX * 0.57), int(_CURSOR_PX * 0.5))
    return cursor


def _edit_tool_flag():
    for path in (("Flag", "EditTool"), ("EditTool",)):
        obj = QgsMapTool
        try:
            for part in path:
                obj = getattr(obj, part)
            return flag_value(obj)
        except AttributeError:
            continue
    return 0


_EDIT_TOOL = _edit_tool_flag()


def _log(msg):
    level = getattr(getattr(Qgis, "MessageLevel", Qgis), "Warning", None)
    if level is None:
        QgsMessageLog.logMessage(msg, "Smooth Navigation")
    else:
        QgsMessageLog.logMessage(msg, "Smooth Navigation", level)


class SmoothNavigator(QObject):
    """Smooth zoom + kinetic pan for one ``QgsMapCanvas``."""

    def __init__(self, canvas, config):
        super().__init__(canvas)          # parented: dies with the canvas
        self.canvas = canvas
        self.viewport = canvas.viewport()
        self.cfg = config
        self._installed = False
        self._committing = False
        self._saved_preview = None
        self._saved_smooth = False

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(_FRAME_MS)      # set again per motion from the screen
        self._timer.timeout.connect(self._tick)

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._commit)

        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ms = 0

        # visual (uncommitted) transform: screen' = s * screen + (tx, ty)
        self._s = 1.0
        self._tx = 0.0
        self._ty = 0.0
        self._items = {}                  # id(item) -> (item, original QTransform)
        self._cached_items = {}           # id(item) -> item whose cache mode we set
        self._strokes = {}                # id(item) -> (item, width, icon size) before counter-scaling
        self._smooth_on = True            # install() switches the hint on
        self._zf_cache = (0, 2.0)         # (clock ms, value) of qgis/zoom_factor
        self._frame_ms = _FRAME_MS

        self.booster = CanvasBooster(canvas, config, self.is_idle)
        self._saved_cursor = None
        self._cursor_was_set = False
        self._swallow_button = None   # button whose release must not leak
        self._drag_moved_zoom = False
        self._replaying = False
        self._reset_motion()

    # ------------------------------------------------------------------ setup
    def install(self):
        if self._installed:
            return
        self.viewport.installEventFilter(self)
        self.canvas.installEventFilter(self)
        self.canvas.extentsChanged.connect(self._on_foreign_extent_change)
        self.canvas.mapToolSet.connect(self._on_map_tool_set)
        self.canvas.destroyed.connect(self._on_canvas_destroyed)
        self._installed = True
        # QGIS draws the cached map without smoothing, so a stretched image
        # looks blocky and shimmers while zooming; smooth it for this canvas.
        self._saved_smooth = bool(self.canvas.renderHints() & _SMOOTH)
        self.canvas.setRenderHint(_SMOOTH, True)
        self.apply_config()
        self.booster.install()

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        with suppress(RuntimeError, TypeError):
            self.viewport.removeEventFilter(self)
            self.canvas.removeEventFilter(self)
            self.canvas.extentsChanged.disconnect(self._on_foreign_extent_change)
            self.canvas.mapToolSet.disconnect(self._on_map_tool_set)
        self.commit_now()
        self.booster.uninstall()
        self._restore_preview()
        with suppress(RuntimeError):
            self.canvas.setRenderHint(_SMOOTH, self._saved_smooth)

    def is_idle(self):
        return (not self._timer.isActive() and not self._dragging
                and not self._settle.isActive() and not self._visual_active())

    def apply_config(self):
        """(Re)apply runtime options. Preview jobs are a per-canvas runtime
        flag (not a global setting) and are restored on uninstall."""
        if self._installed and hasattr(self, "booster"):
            self.booster.apply_config()
        if not hasattr(self.canvas, "setPreviewJobsEnabled"):
            return
        if self.cfg.preview_jobs:
            if self._saved_preview is None:
                self._saved_preview = self.canvas.previewJobsEnabled()
            self.canvas.setPreviewJobsEnabled(True)
        else:
            self._restore_preview()

    def _restore_preview(self):
        if self._saved_preview is not None:
            with suppress(RuntimeError):
                self.canvas.setPreviewJobsEnabled(self._saved_preview)
            self._saved_preview = None

    # ------------------------------------------------------------ state utils
    def _reset_motion(self):
        self._zoom_remaining = 0.0        # log(extent factor) still to apply
        self._zoom_vel = 0.0              # its rate of change per second
        self._zoom_anchor = None          # (x, y) viewport px
        self._dragging = False
        self._drag_button = None
        self._drag_last = None
        self._drag_start = None
        self._drag_is_pan_tool = False
        self._drag_is_zoom = False
        self._pending_zoom = 0.0
        self._recenter_x = 0.0           # eased click-to-centre shift (px)
        self._recenter_y = 0.0
        self._pending_dx = 0.0
        self._pending_dy = 0.0
        self._samples = deque(maxlen=48)
        self._vx = 0.0
        self._vy = 0.0
        self._keys = set()

    def _visual_active(self):
        return self._s != 1.0 or self._tx != 0.0 or self._ty != 0.0

    def _zoom_factor(self):
        now = self._clock.elapsed()
        stamp, cached = self._zf_cache
        if stamp and now - stamp < 1000:    # QgsSettings hits the registry; once a second is enough
            return cached
        zf = _settings().value("qgis/zoom_factor", 2.0, type=float)
        zf = zf if zf and zf > 1.0001 else 2.0
        self._zf_cache = (now or 1, zf)
        return zf

    def _is_edit_tool(self):
        tool = self.canvas.mapTool()
        if tool is None or not _EDIT_TOOL:
            return False
        try:
            return bool(flag_value(tool.flags()) & _EDIT_TOOL)
        except Exception:
            return False

    # --------------------------------------------------------- event filter
    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        # getattr: Qt may deliver late events while the canvas is being
        # destroyed, after Python has already torn this object down.
        if not getattr(self, "_installed", False):
            return False
        try:
            et = event.type()
            if obj is self.viewport:
                if et == QEvent.Type.Wheel:
                    return self._on_wheel(event)
                if et == QEvent.Type.MouseButtonPress:
                    return self._on_press(event)
                if et == QEvent.Type.MouseMove:
                    return self._on_move(event)
                if et == QEvent.Type.MouseButtonRelease:
                    return self._on_release(event)
                if et == QEvent.Type.ContextMenu and self._drag_moved_zoom:
                    self._drag_moved_zoom = False       # a zoom drag ended: no menu after it
                    event.accept()
                    return True
                if et == QEvent.Type.MouseButtonDblClick:
                    # Qt replaces the 2nd press of a double-click with this
                    # event: treat it exactly like a press.
                    return self._on_press(e=event)
            elif obj is self.canvas:
                if et == QEvent.Type.KeyPress:
                    return self._on_key_press(event)
                if et == QEvent.Type.KeyRelease:
                    return self._on_key_release(event)
                if et == QEvent.Type.FocusOut:
                    self._keys.clear()
        except Exception as exc:  # never leave the canvas in a broken state
            _log("Event handling error, reverting to default: %r" % (exc,))
            self._abort()
        return False

    # ---------------------------------------------------------------- wheel
    def _on_wheel(self, e):
        if not self.cfg.smooth_zoom or has_modifiers(e):
            return False            # Ctrl/Shift/Alt+wheel -> stock QGIS
        dy = e.angleDelta().y()
        if dy == 0 or self.canvas.scaleLocked():
            return False
        tool = self.canvas.mapTool()
        if tool is not None:        # QGIS gives the map tool the wheel first
            e.ignore()
            with suppress(Exception):
                tool.wheelEvent(e)
            if e.isAccepted():
                return True         # tool handled it, same as stock QGIS
        steps = dy / 120.0          # works for mice and hi-res touchpads
        if _settings().value("qgis/reverse_wheel_zoom", False, type=bool):
            steps = -steps
        self._vx = self._vy = 0.0   # wheel cancels a running glide
        self._add_zoom(-steps * math.log(self._zoom_factor()), event_pos(e))
        e.accept()
        return True

    def _add_zoom(self, log_delta, anchor_px):
        if self._zoom_remaining * log_delta < 0:
            self._zoom_remaining = 0.0          # direction reversed: react now
            self._zoom_vel = 0.0
        limit = _MAX_PENDING_ZOOM_STEPS * math.log(self._zoom_factor())
        self._zoom_remaining = max(-limit, min(limit, self._zoom_remaining + log_delta))
        if anchor_px is None:
            anchor_px = (self.viewport.width() / 2.0, self.viewport.height() / 2.0)
        self._zoom_anchor = anchor_px
        self._start_motion()

    # ---------------------------------------------------------------- mouse
    def _takes_press(self, e):
        btn = e.button()
        if btn == Qt.MouseButton.MiddleButton and self.cfg.middle_pan:
            return True
        if (btn == Qt.MouseButton.LeftButton and self.cfg.pan_tool_drag
                and not has_modifiers(e)
                and isinstance(self.canvas.mapTool(), QgsMapToolPan)):
            return True
        # Pro's Explore tool: hold the right button, drag up to zoom in and
        # down to zoom out. QgsMapToolPan ignores the right button, so nothing
        # is taken from QGIS.
        if (btn == Qt.MouseButton.RightButton and self.cfg.right_drag_zoom
                and not has_modifiers(e) and not self.canvas.scaleLocked()
                and self._right_zoom_tool(self.canvas.mapTool())):
            return True
        return False

    @staticmethod
    def _right_zoom_tool(tool):
        """Pan tools, plus any tool that opts in (the KGA Select tool) with kga_right_drag_zoom."""
        return isinstance(tool, QgsMapToolPan) or bool(getattr(tool, "kga_right_drag_zoom", False))

    def _replay_right_click(self, e):
        """A right button press+release with no drag: re-send it past this filter so the tool's
        context menu (QGIS shows it on the press) still appears."""
        self._replaying = True
        try:
            for kind, btns in ((QEvent.Type.MouseButtonPress, e.button()),
                               (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
                where = e.globalPosition() if hasattr(e, "globalPosition") else QPointF(e.globalPos())  # Qt 6 / 5
                ev = QMouseEvent(kind, QPointF(*event_pos(e)), QPointF(where), e.button(), btns,
                                 e.modifiers())
                QApplication.sendEvent(self.viewport, ev)
        finally:
            self._replaying = False

    def _held(self, e, button):
        return button is not None and flag_value(e.buttons() & button) != 0

    def _on_press(self, e):
        if self._swallow_button is not None:
            # A new press of the same button, or the button no longer held,
            # means its release was lost (e.g. Alt-Tab): stop swallowing.
            if e.button() == self._swallow_button or not self._held(e, self._swallow_button):
                self._swallow_button = None
            else:
                return True
        if self._dragging:
            if e.button() != self._drag_button and self._held(e, self._drag_button):
                return True              # extra button during a drag: ignore
            # otherwise the previous drag's release was lost: start fresh
            self._dragging = False
            self._restore_cursor()
        if self._replaying:
            return False                # our own replayed right-click: the tool and its menu get it
        if not self._takes_press(e):
            # Any other click (identify, select, digitize...) must hit the map
            # exactly where the user sees it: stop and commit synchronously
            # before QGIS/map tool processes the press.
            if self._visual_active() or self._timer.isActive():
                self.commit_now()
            return False
        return self._begin_drag(e, e.button(), *event_pos(e))

    def _begin_drag(self, e, button, x, y):
        self._vx = self._vy = 0.0
        self._zoom_remaining = 0.0
        self._zoom_vel = 0.0
        self._dragging = True
        self._drag_button = button
        self._drag_last = (x, y)
        self._drag_start = (x, y)
        self._drag_is_pan_tool = button == Qt.MouseButton.LeftButton
        self._drag_is_zoom = button == Qt.MouseButton.RightButton
        self._drag_moved_zoom = False
        self._zoom_anchor = (x, y)
        self._pending_zoom = 0.0
        self._recenter_x = self._recenter_y = 0.0
        self._samples.clear()
        self._samples.append((self._clock.elapsed(), x, y))
        self._settle.stop()
        if self._saved_cursor is None:
            self._cursor_was_set = self.viewport.testAttribute(
                Qt.WidgetAttribute.WA_SetCursor)
            self._saved_cursor = QCursor(self.viewport.cursor())
        self.viewport.setCursor(
            _zoom_cursor(True) if self._drag_is_zoom
            else QCursor(Qt.CursorShape.ClosedHandCursor))
        e.accept()
        return True

    def _on_move(self, e):
        if not self._dragging:
            if self._swallow_button is not None and not self._held(e, self._swallow_button):
                self._swallow_button = None
            return self._swallow_button is not None
        x, y = event_pos(e)
        lx, ly = self._drag_last
        if self._drag_is_zoom:
            # sign +1: down = zoom out (extent grows), up = zoom in
            delta = (y - ly) * _DRAG_ZOOM_PER_PX * self.cfg.right_drag_sign
            self._pending_zoom += delta
            if delta:   # the magnifier shows which way the drag is going
                self.viewport.setCursor(_zoom_cursor(delta < 0))
        else:
            self._pending_dx += x - lx
            self._pending_dy += y - ly
        self._drag_last = (x, y)
        self._samples.append((self._clock.elapsed(), x, y))
        self._start_motion()            # coalesced into the next frame
        e.accept()
        return True

    def _on_release(self, e):
        if self._replaying:
            return False
        if self._swallow_button is not None:
            if e.button() == self._swallow_button:
                self._swallow_button = None
            return True
        if not self._dragging or e.button() != self._drag_button:
            return self._dragging       # swallow other buttons mid-drag
        self._dragging = False
        self._drag_button = None
        self._restore_cursor()

        if self._drag_is_zoom:
            self._samples.clear()
            self._start_motion()        # settle, then render once
            rx, ry = event_pos(e)
            sx, sy = self._drag_start
            if math.hypot(rx - sx, ry - sy) <= _CLICK_SLOP_PX:
                if not isinstance(self.canvas.mapTool(), QgsMapToolPan):
                    self._replay_right_click(e)     # QGIS opens the menu on press, so give it one now
                    return True
            else:
                self._drag_moved_zoom = True    # the OS may still send a context-menu event: drop it
            e.accept()
            return True

        rx, ry = event_pos(e)
        sx, sy = self._drag_start
        if self._drag_is_pan_tool and math.hypot(rx - sx, ry - sy) <= _CLICK_SLOP_PX:
            # Stock QgsMapToolPan recentres the map on a plain click:
            # keep that behaviour, but animate it.
            self._samples.clear()
            self._recenter_x = self.viewport.width() / 2.0 - rx
            self._recenter_y = self.viewport.height() / 2.0 - ry
            self._start_motion()
            e.accept()
            return True

        now = self._clock.elapsed()
        last_move_ms = self._samples[-1][0] if self._samples else now
        # the release itself is the final sample: velocity = distance / time
        # up to the moment of release (slowing down before letting go = less glide)
        self._samples.append((now, rx, ry))
        pts = [p for p in self._samples if now - p[0] <= _SAMPLE_WINDOW_MS]
        if self.cfg.inertia and len(pts) >= 2 and now - last_move_ms <= _FLICK_MAX_IDLE_MS:
            t0, x0, y0 = pts[0]
            t1, x1, y1 = pts[-1]
            span = max(t1 - t0, 16) / 1000.0
            vx = (x1 - x0) / span * self.cfg.flick_gain
            vy = (y1 - y0) / span * self.cfg.flick_gain
            speed = math.hypot(vx, vy)
            if speed > _MAX_SPEED:
                vx, vy = vx * _MAX_SPEED / speed, vy * _MAX_SPEED / speed
            self._vx, self._vy = vx, vy
        self._samples.clear()
        self._start_motion()
        e.accept()
        return True

    def _restore_cursor(self):
        if self._saved_cursor is not None:
            with suppress(RuntimeError):
                if self._cursor_was_set:
                    self.viewport.setCursor(self._saved_cursor)
                else:               # was inheriting the map tool's cursor
                    self.viewport.unsetCursor()
            self._saved_cursor = None

    # ------------------------------------------------------------- keyboard
    def _on_key_press(self, e):
        handled = self._handle_key_press(e)
        if handled:
            self._emit_key_signal("keyPressed", e)
        elif (self._visual_active() or self._timer.isActive()) and not e.isAutoRepeat():
            # e.g. Space-bar pan or a tool shortcut: give QGIS the real view first
            self.commit_now()
        return handled

    def _emit_key_signal(self, name, e):
        # QgsMapCanvas normally emits keyPressed/keyReleased for every key it
        # receives; keep that contract for other plugins.
        with suppress(Exception):
            getattr(self.canvas, name).emit(e)

    def _handle_key_press(self, e):
        if not self.cfg.keyboard or has_modifiers(e) or self._is_edit_tool():
            return False
        k = int(e.key())
        if k in _ARROWS:
            if not e.isAutoRepeat():
                self._keys.add(k)
            self._start_motion()
            e.accept()
            return True
        if k in _ZOOM_KEYS:
            if not self.cfg.smooth_zoom or self.canvas.scaleLocked():
                return False
            step = _ZOOM_KEYS[k] * math.log(self._zoom_factor())
            # a tap = one smooth step; holding keeps a steady stream going
            if not e.isAutoRepeat() or abs(self._zoom_remaining) < abs(step) * 0.6:
                self._add_zoom(step, None)
            e.accept()
            return True
        return False

    def _on_key_release(self, e):
        k = int(e.key())
        if k in _ARROWS and (k in self._keys or e.isAutoRepeat() and self._keys):
            if not e.isAutoRepeat():
                self._keys.discard(k)
                self._start_motion()      # let it ease to a stop
            self._emit_key_signal("keyReleased", e)
            return True
        return False

    def _key_vector(self):
        kx = ky = 0.0
        for k in self._keys:
            dx, dy = _ARROWS[k]
            kx += dx
            ky += dy
        return kx, ky

    # ------------------------------------------------------------ animation
    def _start_motion(self):
        self._settle.stop()
        self.booster.cancel()           # free CPU for the motion
        self.booster.begin_motion()
        if not self._timer.isActive():
            interval = self._frame_ms = self._frame_interval()
            self._timer.setInterval(interval)
            self._last_ms = self._clock.nsecsElapsed() / 1e6 - interval
            self._timer.start()

    def _frame_interval(self):
        """Milliseconds per frame: the refresh rate of the screen the map is on
        (a 240 Hz display gets 4 ms frames), never slower than 60 fps."""
        rate = 0.0
        with suppress(Exception):
            rate = float(self.canvas.screen().refreshRate())
        if rate < 30.0:
            rate = 60.0
        return max(_MIN_FRAME_MS, min(_FRAME_MS, int(round(1000.0 / rate))))

    def _tick(self):
        if not getattr(self, "_installed", False):
            return
        try:
            now = self._clock.nsecsElapsed() / 1e6      # ms, fractional
            dt = min(max(now - self._last_ms, 0.5), 50.0) / 1000.0
            self._last_ms = now

            panned = self._step_pan(dt)
            zoomed = self._step_zoom(dt)
            if panned or zoomed:
                self._apply_visual()
                self._adapt_interval((self._clock.nsecsElapsed() / 1e6) - now)

            if not (panned or zoomed or self._keys or self._recenter_x or self._recenter_y):
                self._timer.stop()
                if not self._dragging:   # idle -> render once after delay
                    self._settle.start(max(0, int(self.cfg.settle_delay_ms)))
        except Exception as exc:
            _log("Animation error, reverting to default: %r" % (exc,))
            self._abort()

    def _adapt_interval(self, cost_ms):
        """Back off when frames cost more than the clock allows, so the event
        loop keeps getting time (input, repaint) instead of freezing."""
        cur = self._timer.interval()
        if cost_ms > cur * 0.5 and cur < _FRAME_MS * 2:
            self._timer.setInterval(min(_FRAME_MS * 2, cur + 4))
        elif cost_ms < cur * 0.2 and cur > self._frame_ms:
            self._timer.setInterval(max(self._frame_ms, cur - 2))

    def _step_pan(self, dt):
        if self._dragging:
            dx, dy = self._pending_dx, self._pending_dy
            self._pending_dx = self._pending_dy = 0.0
            if dx or dy:
                self._tx += dx
                self._ty += dy
                return True
            return False

        kx, ky = self._key_vector()
        if kx or ky:
            n = math.hypot(kx, ky)
            sp = float(self.cfg.key_pan_speed)
            tx, ty = kx / n * sp, ky / n * sp
            a = 1.0 - math.exp(-dt / _KEY_ACCEL_TAU)
            self._vx += (tx - self._vx) * a
            self._vy += (ty - self._vy) * a
        else:
            tau = (self.cfg.inertia_tau_ms / 1000.0) if self.cfg.inertia else _KEY_STOP_TAU
            decay = math.exp(-dt / max(tau, 0.01))
            self._vx *= decay
            self._vy *= decay

        moved = False
        if math.hypot(self._vx, self._vy) < _MIN_SPEED:
            self._vx = self._vy = 0.0
        else:
            self._tx += self._vx * dt
            self._ty += self._vy * dt
            moved = True

        rx, ry = self._recenter_x, self._recenter_y
        if rx or ry:
            if math.hypot(rx, ry) < 0.5:
                sx, sy = rx, ry
            else:
                a = 1.0 - math.exp(-dt / (max(float(self.cfg.zoom_tau_ms), 1.0) / 1000.0))
                sx, sy = rx * a, ry * a
            self._recenter_x, self._recenter_y = rx - sx, ry - sy
            if abs(self._recenter_x) < 0.5 and abs(self._recenter_y) < 0.5:
                sx += self._recenter_x
                sy += self._recenter_y
                self._recenter_x = self._recenter_y = 0.0
            self._tx += sx
            self._ty += sy
            moved = True
        return moved

    def _step_zoom(self, dt):
        if self._pending_zoom:
            # a right-button drag follows the mouse directly, no easing
            step, self._pending_zoom = self._pending_zoom, 0.0
            k = math.exp(-step)
            ax, ay = self._zoom_anchor
            self._s *= k
            self._tx = ax + k * (self._tx - ax)
            self._ty = ay + k * (self._ty - ay)
            return True
        r = self._zoom_remaining
        if abs(r) < _ZOOM_EPS:
            self._zoom_remaining = 0.0
            self._zoom_vel = 0.0
            return False
        # Critically damped spring towards zero: the zoom speed is continuous,
        # so a new wheel notch pushes the motion on instead of jerking it.
        w = 2.0 / (max(float(self.cfg.zoom_tau_ms), 1.0) / 1000.0)
        v = self._zoom_vel
        decay = math.exp(-w * dt)
        b = v + w * r
        left = (r + b * dt) * decay
        self._zoom_vel = (v - w * b * dt) * decay
        step = r - left
        if abs(left) < _ZOOM_EPS:
            step = r
            left = 0.0
            self._zoom_vel = 0.0
        self._zoom_remaining = left
        # extent grows by e^step  ->  screen content scales by e^-step about anchor
        k = math.exp(-step)
        ax, ay = self._zoom_anchor
        self._s *= k
        self._tx = ax + k * (self._tx - ax)
        self._ty = ay + k * (self._ty - ay)
        return True

    # ----------------------------------------------------- visual transform
    def _apply_visual(self):
        """Scale/translate every top-level scene item (cached map image, rubber
        bands, markers, annotations) - GPU/CPU-cheap, no layer rendering."""
        scene = self.canvas.scene()
        if scene is None:
            return
        s, tx, ty = self._s, self._tx, self._ty
        # Resampling a full-screen image with smoothing is the costly part of a
        # frame; a pure pan only needs it for the sub-pixel shift, so skip it there.
        smooth = s != 1.0
        if smooth != self._smooth_on:
            self._smooth_on = smooth
            self.canvas.setRenderHint(_SMOOTH, smooth)
        # The low-resolution underlay is only worth its per-frame resampling while the moved map
        # image leaves part of the view uncovered (zooming out, panning); zooming in never does.
        w, h = self.viewport.width(), self.viewport.height()
        self.booster.set_exposed(not (tx <= 0.0 and ty <= 0.0 and tx + s * w >= w and ty + s * h >= h))
        # viewport -> scene offset (QGIS shifts the scene rect while panning)
        o = self.canvas.mapToScene(QPoint(0, 0))
        ox, oy = o.x(), o.y()
        # Rescan every frame: tools delete their rubber bands and markers from C++,
        # and touching such a wrapper (even pos()) is an access violation, not a
        # RuntimeError. Only items the scene still reports are ever touched.
        live = set()
        for item in scene.items():
            if item.parentItem() is not None:
                continue
            key = id(item)
            live.add(key)
            entry = self._items.get(key)
            if entry is None or entry[0] is not item:
                self._items[key] = (item, QTransform(item.transform()))
                self._cache_vector_item(item, key)
        for key in [k for k in self._items if k not in live]:
            del self._items[key]
            self._cached_items.pop(key, None)
            self._strokes.pop(key, None)
        for key, (item, base) in self._items.items():
            p = item.pos()
            g = QTransform(s, 0.0, 0.0, s,
                           (s - 1.0) * (p.x() - ox) + tx,
                           (s - 1.0) * (p.y() - oy) + ty)
            item.setTransform(base * g)
            self._keep_stroke(item, key, s)

    def _keep_stroke(self, item, key, s):
        """The transform scales an item's pen and vertex markers too, so a
        sketch would fatten while zooming in. Counter-scale them so the line
        and vertex-marker sizes stay constant on screen, as in ArcGIS Pro."""
        if isinstance(item, QgsRubberBand):
            saved = self._strokes.get(key)
            if saved is None:
                saved = self._strokes[key] = (item, int(item.width()), int(item.iconSize()))
            item.setWidth(max(0, round(saved[1] / s)))   # 0 = hairline, never scaled
            item.setIconSize(max(1, round(saved[2] / s)))

    def _restore_strokes(self):
        scene = self.canvas.scene()
        live = {id(it) for it in scene.items()} if scene is not None else set()
        for key, (item, width, icon) in self._strokes.items():
            if key not in live:             # a deleted item must not be touched
                continue
            item.setWidth(width)
            item.setIconSize(icon)
        self._strokes.clear()

    def _cache_vector_item(self, item, key):
        """Selection highlights and rubber bands are vector paths (often tens of
        thousands of vertices) that Qt would re-draw, antialiased, on every frame
        of the animation. Cache them as an image for the duration of the motion,
        so each frame only moves/scales that image."""
        if not isinstance(item, _VECTOR_ITEMS):
            return
        with suppress(RuntimeError):
            if item.cacheMode() != QGraphicsItem.CacheMode.NoCache:
                return
            w = self.viewport.width() * _CACHE_SPAN
            h = self.viewport.height() * _CACHE_SPAN
            item.setCacheMode(QGraphicsItem.CacheMode.ItemCoordinateCache,
                              QSize(int(w), int(h)))
            self._cached_items[key] = item

    def _uncache_vector_items(self):
        scene = self.canvas.scene()
        live = {id(it) for it in scene.items()} if scene is not None else set()
        for key, item in self._cached_items.items():
            if key in live:                  # a deleted item must not be touched
                with suppress(RuntimeError):
                    item.setCacheMode(QGraphicsItem.CacheMode.NoCache)
        self._cached_items.clear()

    def _restore_items(self):
        self.booster.set_exposed(False)
        self._uncache_vector_items()
        self._restore_strokes()
        if not self._smooth_on:
            self._smooth_on = True
            with suppress(RuntimeError):
                self.canvas.setRenderHint(_SMOOTH, True)
        if not self._items:
            return
        scene = self.canvas.scene()
        live = {id(it): it for it in scene.items()} if scene is not None else {}
        for key, (_item, base) in self._items.items():
            current = live.get(key)          # only touch items still alive
            if current is not None:
                current.setTransform(base)
        self._items.clear()

    # --------------------------------------------------------------- commit
    def _affine(self):
        """Exact pixel->map affine of the committed view (handles rotation)."""
        m2p = self.canvas.getCoordinateTransform()
        o = m2p.toMapCoordinates(0, 0)
        a = m2p.toMapCoordinates(1000, 0)
        b = m2p.toMapCoordinates(0, 1000)
        return (o.x(), o.y(),
                (a.x() - o.x()) / 1000.0, (a.y() - o.y()) / 1000.0,
                (b.x() - o.x()) / 1000.0, (b.y() - o.y()) / 1000.0)

    def _commit(self):
        """Turn the accumulated visual transform into ONE extent change."""
        if self._dragging or self._timer.isActive():
            return
        if not self._visual_active():
            self._restore_items()
            return
        s, tx, ty = self._s, self._tx, self._ty
        self._restore_items()
        self._s, self._tx, self._ty = 1.0, 0.0, 0.0

        ox, oy, ux, uy, vx, vy = self._affine()
        cx = (self.viewport.width() / 2.0 - tx) / s
        cy = (self.viewport.height() / 2.0 - ty) / s
        center = QgsPointXY(ox + ux * cx + vx * cy, oy + uy * cx + vy * cy)

        self._committing = True
        try:
            # Same code path as QGIS's own zoom tools (rotation-safe). Scale
            # lock is respected upstream: zoom never starts while locked, so
            # here 1/s == 1 whenever the scale is locked (pure recentre).
            self.canvas.zoomByFactor(1.0 / s, center, True)  # refreshes itself
        finally:
            self._committing = False

    def commit_now(self):
        """Stop all motion and commit immediately (used before clicks,
        tool changes and when the plugin is switched off)."""
        self._timer.stop()
        self._settle.stop()
        self._restore_cursor()
        self._reset_motion()
        try:
            self._commit()
        except Exception as exc:
            _log("Commit error: %r" % (exc,))
            self._abort()

    def _abort(self):
        """Drop any uncommitted motion and return to the committed view."""
        if self._dragging:      # button still down: don't leak its release
            self._swallow_button = self._drag_button
        self._timer.stop()
        self._settle.stop()
        self._restore_cursor()
        self._reset_motion()
        try:
            self._restore_items()
        except Exception:
            self._items.clear()
        self._s, self._tx, self._ty = 1.0, 0.0, 0.0

    # ------------------------------------------------------- canvas signals
    def _on_canvas_destroyed(self, *args):
        self._installed = False
        with suppress(RuntimeError):
            self._timer.stop()
            self._settle.stop()

    def _on_foreign_extent_change(self):
        if not getattr(self, "_installed", False):
            return
        # Someone else (zoom-to-layer, resize, another plugin) moved the map:
        # their extent wins, our pending visual motion is discarded.
        if not self._committing and (self._visual_active() or self._items):
            self._abort()

    def _on_map_tool_set(self, *args):
        if not getattr(self, "_installed", False):
            return
        if self._visual_active() or self._timer.isActive():
            self.commit_now()

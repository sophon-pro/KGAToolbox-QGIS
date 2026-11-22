# -*- coding: utf-8 -*-
"""
Temporary pan - the event-filter controller.

Ported from the standalone Temporary Pan While Digitizing plugin.

How it works (and why it preserves the digitizing session)
----------------------------------------------------------
QGIS's map canvas already has a pan engine that runs *without* involving the
active map tool: the middle mouse button.  A middle-button press makes the
canvas pan directly; the active tool (Add Line Feature, Add Polygon...) never
receives the event, so its vertices, rubber band, snapping and Advanced
Digitizing state are untouched.

While the configured key (default ``C``) is held on a focused map canvas, this
controller:

* swallows the key events (so the key does not reach the digitizing tool,
  the Advanced Digitizing panel or a QGIS shortcut);
* turns a LEFT-button press/release on the canvas into a synthetic MIDDLE-button
  press/release, and swallows the original left-button events - so the
  digitizing tool never sees a click and no vertex is added;
* lets mouse-move events through - the canvas pans them itself;
* shows a hand cursor.

The map tool is never switched, deactivated or replaced.  Wheel zoom and the
real middle button are never touched.

One instance serves every map canvas: an event filter is installed on each
canvas (keys, focus) and its viewport (mouse) the first time it gets focus.
"""

import logging
from typing import Optional

from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QEvent, QObject, QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor, QMouseEvent
from qgis.PyQt.QtWidgets import QApplication

log = logging.getLogger("KGA.TemporaryPan")

# modifiers that mean "this is a shortcut like Ctrl+C, not our pan key"
_BLOCKING_MODIFIERS = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                       | Qt.KeyboardModifier.MetaModifier)


def _mods(event) -> int:
    m = event.modifiers()
    return int(m.value) if hasattr(m, "value") else int(m)


def _pos(event) -> QPointF:
    """Widget position of a mouse event (Qt5 pos(), Qt6 position())."""
    return QPointF(event.position()) if hasattr(event, "position") else QPointF(event.pos())


def _flag(v) -> int:
    return int(v.value) if hasattr(v, "value") else int(v)


class TemporaryPanController(QObject):
    """Installs/removes event filters and runs the temporary pan."""

    panStarted = pyqtSignal()     # key went down (pan mode on)
    panFinished = pyqtSignal()    # key released (pan mode off)

    def __init__(self, key: int = Qt.Key.Key_C, parent=None):
        super().__init__(parent)
        self.key = _flag(key)
        self.enabled = False
        self._canvases = []                 # canvases we filter
        self._active_canvas: Optional[QgsMapCanvas] = None
        self._dragging = False
        self._last_pos = QPointF()
        self._sending = False               # True while we deliver a synthetic event
        self._cursor_depth = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def set_key(self, key: int):
        self.end_pan()
        self.key = _flag(key)

    def enable(self, canvases=()):
        if self.enabled:
            return
        self.enabled = True
        for c in canvases:
            self.attach(c)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_changed)

    def disable(self):
        if not self.enabled:
            return
        self.end_pan()
        self.enabled = False
        app = QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._focus_changed)
            except (TypeError, RuntimeError):
                pass
        for c in list(self._canvases):
            self.detach(c)

    def attach(self, canvas: QgsMapCanvas):
        """Install filters on a canvas (idempotent - never twice)."""
        if canvas is None or canvas in self._canvases:
            return
        try:
            canvas.installEventFilter(self)
            canvas.viewport().installEventFilter(self)
            canvas.destroyed.connect(self._canvas_destroyed)
        except RuntimeError:
            return
        self._canvases.append(canvas)

    def detach(self, canvas: QgsMapCanvas):
        if canvas in self._canvases:
            self._canvases.remove(canvas)
        try:
            canvas.removeEventFilter(self)
            canvas.viewport().removeEventFilter(self)
            canvas.destroyed.disconnect(self._canvas_destroyed)
        except (RuntimeError, TypeError):
            pass

    def _canvas_destroyed(self, obj=None):
        self._canvases = [c for c in self._canvases if not _is_deleted(c)]
        if self._active_canvas is not None and _is_deleted(self._active_canvas):
            self._active_canvas = None
            self._dragging = False
            self._restore_cursor(all_levels=True)

    def _focus_changed(self, _old, new):
        """Pick up map canvases created later (extra map views)."""
        w = new
        while w is not None:
            if isinstance(w, QgsMapCanvas):
                self.attach(w)
                break
            w = w.parentWidget()
        # focus went away from the canvas while the key was held -> stop
        if self._active_canvas is not None and not _inside(new, self._active_canvas):
            self.end_pan()

    @property
    def active(self) -> bool:
        return self._active_canvas is not None

    # ------------------------------------------------------------------
    # event filter
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if self._sending or not self.enabled:
            return False
        et = event.type()
        T = QEvent.Type
        try:
            if isinstance(obj, QgsMapCanvas):
                if et in (T.KeyPress, T.KeyRelease, T.ShortcutOverride):
                    return self._key_event(obj, event)
                if et in (T.FocusOut, T.Hide) and self._active_canvas is obj:
                    self.end_pan()
                return False
            if self._active_canvas is None:
                return False
            if obj is not self._active_canvas.viewport():
                return False
            if et == T.MouseButtonPress:
                return self._mouse_press(event)
            if et == T.MouseButtonDblClick:
                # a double-click must not finish/alter the feature while panning
                return event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
            if et == T.MouseMove:
                self._last_pos = _pos(event)
                return False                      # the canvas pans by itself
            if et == T.MouseButtonRelease:
                return self._mouse_release(event)
        except RuntimeError:  # C++ object gone during shutdown
            return False
        return False

    def _is_our_key(self, event) -> bool:
        return event.key() == self.key and not (_mods(event) & _flag(_BLOCKING_MODIFIERS))

    def _key_event(self, canvas, event) -> bool:
        if not self._is_our_key(event):
            return False
        et = event.type()
        if et == QEvent.Type.ShortcutOverride:
            # claim the key so a QGIS shortcut bound to it doesn't fire on the canvas
            event.accept()
            return True
        if event.isAutoRepeat():
            return True                           # holding the key: swallow repeats
        if et == QEvent.Type.KeyPress:
            if self._active_canvas is None:
                self._active_canvas = canvas
                self._set_cursor(Qt.CursorShape.OpenHandCursor)
                self.panStarted.emit()
            return True
        # KeyRelease
        if self._active_canvas is canvas:
            self.end_pan()
        return True

    def _mouse_press(self, event) -> bool:
        b = event.button()
        if b == Qt.MouseButton.LeftButton:
            if not self._dragging:
                self._dragging = True
                self._last_pos = _pos(event)
                self._send_middle(QEvent.Type.MouseButtonPress, self._last_pos)
                self._set_cursor(Qt.CursorShape.ClosedHandCursor, replace=True)
            return True                           # the digitizing tool never sees it
        if b == Qt.MouseButton.RightButton:
            return True                           # don't finish the feature by accident
        return False                              # real middle button etc.: untouched

    def _mouse_release(self, event) -> bool:
        b = event.button()
        if b == Qt.MouseButton.LeftButton:
            if self._dragging:
                self._finish_drag(_pos(event))
            return True
        if b == Qt.MouseButton.RightButton:
            return True
        return False

    # ------------------------------------------------------------------
    # pan helpers
    # ------------------------------------------------------------------
    def _send_middle(self, etype, pos: QPointF):
        canvas = self._active_canvas
        if canvas is None:
            return
        vp = canvas.viewport()
        buttons = Qt.MouseButton.MiddleButton if etype == QEvent.Type.MouseButtonPress \
            else Qt.MouseButton.NoButton
        ev = QMouseEvent(etype, pos, QPointF(vp.mapToGlobal(pos.toPoint())),
                         Qt.MouseButton.MiddleButton, buttons, Qt.KeyboardModifier.NoModifier)
        self._sending = True
        try:
            QApplication.sendEvent(vp, ev)
        finally:
            self._sending = False

    def _finish_drag(self, pos: QPointF):
        self._dragging = False
        self._send_middle(QEvent.Type.MouseButtonRelease, pos)
        if self._active_canvas is not None:
            self._set_cursor(Qt.CursorShape.OpenHandCursor, replace=True)

    def end_pan(self):
        """Leave pan mode cleanly (key released, focus lost, plugin disabled...)."""
        if self._active_canvas is None:
            return
        if self._dragging and not _is_deleted(self._active_canvas):
            self._finish_drag(self._last_pos)
        self._dragging = False
        self._active_canvas = None
        self._restore_cursor(all_levels=True)
        self.panFinished.emit()

    # ------------------------------------------------------------------
    # cursor (application override, always balanced)
    # ------------------------------------------------------------------
    def _set_cursor(self, shape, replace=False):
        if replace and self._cursor_depth:
            QApplication.changeOverrideCursor(QCursor(shape))
        else:
            QApplication.setOverrideCursor(QCursor(shape))
            self._cursor_depth += 1

    def _restore_cursor(self, all_levels=False):
        while self._cursor_depth > 0:
            QApplication.restoreOverrideCursor()
            self._cursor_depth -= 1
            if not all_levels:
                break


def _is_deleted(obj) -> bool:
    try:
        from qgis.PyQt import sip
        return sip.isdeleted(obj)
    except Exception:
        try:
            obj.objectName()
            return False
        except RuntimeError:
            return True


def _inside(widget, canvas) -> bool:
    w = widget
    while w is not None:
        if w is canvas:
            return True
        w = w.parentWidget()
    return False

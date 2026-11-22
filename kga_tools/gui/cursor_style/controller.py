"""Applies the selected cursor to QGIS map canvases.

How QGIS manages map-canvas cursors (QGIS 3.34 source, src/gui/qgsmaptool.cpp)
------------------------------------------------------------------------------
* Every QgsMapTool owns a base cursor (``mCursor``).  ``QgsMapTool::activate()``
  calls ``canvas->setCursor(mCursor)`` and ``QgsMapTool::setCursor()`` updates
  ``mCursor`` (and the canvas, if the tool is active).
* Short-lived feedback is layered on top and then put back from ``mCursor``:
  the pan tool shows a closed hand while dragging, the annotation tool shows
  resize arrows over items, and the canvas uses application *override*
  cursors (QgsTemporaryCursorOverride) for wheel-zoom, space-bar / middle-
  button panning and busy states.

Strategy
--------
We never touch QApplication's global cursor and never poll.  When a tool
becomes active (``QgsMapCanvas.mapToolSet``) we remember the tool's native
base cursor, and - if the user enabled the tool's category - replace only the
*base* cursor through the public ``QgsMapTool.setCursor()``.  All native
transient feedback therefore keeps working and returns to our cursor
afterwards.  When the tool is deactivated, or the plugin is disabled /
unloaded, the tool's native base cursor is written back, so tools are never
left modified.
"""

import weakref
from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import QApplication
from qgis.gui import (QgsMapCanvas, QgsMapTool, QgsMapToolPan, QgsMapToolZoom,
                      QgsMapToolIdentify, QgsMapToolCapture, QgsMapToolEdit)

from .renderer import CursorCache

CAT_DIGITIZING = "digitizing"
CAT_EDITING = "editing"
CAT_SELECTION = "selection"
CAT_INSPECT = "inspect"
CAT_NAVIGATION = "navigation"
CAT_OTHER = "other"

CATEGORY_LABELS = [
    (CAT_DIGITIZING, "Digitizing", "Add feature / part / ring, split, reshape, shape tools, KGA Create Features"),
    (CAT_EDITING, "Editing", "Vertex tool, move, rotate, simplify, delete ring/part, KGA Modify Features tools"),
    (CAT_SELECTION, "Selection", "Select by click, rectangle, polygon, freehand, radius"),
    (CAT_INSPECT, "Identify & measure", "Identify features, measure tools, run feature action"),
    (CAT_NAVIGATION, "Pan & zoom", "QGIS cursor by default: keeps the hand and magnifier feedback"),
    (CAT_OTHER, "Other tools", "Map tools from processing and other plugins"),
]

# What each kind of tool shows: the tool's own QGIS cursor, the main style
# (the one edited in the panel), or the name of a preset / saved style.
# The sentinels cannot be style names (see manager.check_new_name).
ASSIGN_QGIS = "<qgis>"
ASSIGN_MAIN = "<main>"
DEFAULT_ASSIGNMENTS = {
    CAT_DIGITIZING: ASSIGN_MAIN, CAT_EDITING: ASSIGN_MAIN, CAT_SELECTION: ASSIGN_MAIN,
    CAT_INSPECT: ASSIGN_MAIN, CAT_NAVIGATION: ASSIGN_QGIS, CAT_OTHER: ASSIGN_QGIS,
}


def _flag_int(value):
    """An enum / flags value as an int on PyQt5 (int-like) and PyQt6 (enum.Flag)."""
    try:
        return int(value)
    except TypeError:
        return int(getattr(value, "value", 0))


# Scoped enum access differs between PyQt/sip versions; resolve once.
_EDIT_TOOL_FLAG = _flag_int(getattr(getattr(QgsMapTool, "Flag", QgsMapTool), "EditTool"))

# KGA Toolbox's own map tools are plain QgsMapTool subclasses, so the generic
# checks below would put them all in "Other tools". Matched on the class MRO by
# (module suffix, class name), so nothing heavy is imported to classify a tool.
_KGA_TOOL_CATEGORIES = (
    (("editor.sketch.construction_tool", "ConstructionSketchTool"), CAT_DIGITIZING),
    (("editor.selection", "SelectTool"), CAT_SELECTION),
    (("editor.modify.reshape", "PickTool"), CAT_EDITING),
    (("editor.modify.transform", "TransformTool"), CAT_EDITING),
    (("editor.edit_tools", "SplitTool"), CAT_EDITING),
    (("gui.modify_dialog", "InteractiveMapTool"), CAT_EDITING),
    (("gui.modify_dialog", "AngleMapTool"), CAT_EDITING),
)
_KGA_PACKAGE = __name__.rsplit(".gui.", 1)[0]  # "kga_tools" (or whatever it is installed as)


def _kga_category(tool):
    for cls in type(tool).__mro__:
        module = getattr(cls, "__module__", "") or ""
        if not module.startswith(_KGA_PACKAGE + "."):
            continue
        suffix = module[len(_KGA_PACKAGE) + 1:]
        for (mod, name), cat in _KGA_TOOL_CATEGORIES:
            if suffix == mod and cls.__name__ == name:
                return cat
    return None


_ACTION_CATEGORIES = {
    CAT_NAVIGATION: ("actionPan", "actionZoomIn", "actionZoomOut"),
    CAT_SELECTION: ("actionSelect", "actionSelectRectangle", "actionSelectPolygon",
                    "actionSelectFreehand", "actionSelectRadius"),
    CAT_INSPECT: ("actionIdentify", "actionMeasure", "actionMeasureArea",
                  "actionMeasureAngle", "actionFeatureAction"),
}


def _alive(obj):
    return obj is not None and not sip.isdeleted(obj)


def _addr(obj):
    try:
        return sip.unwrapinstance(obj)
    except Exception:
        return id(obj)


class _ToolRef:
    """Reference to a map tool that never extends the life of tools owned by
    Python code (plugin tools) but survives wrapper re-creation for tools
    owned by QGIS itself."""

    def __init__(self, tool):
        self.addr = _addr(tool)
        if sip.ispyowned(tool):
            self._strong, self._weak = None, weakref.ref(tool)
        else:
            self._strong, self._weak = tool, None

    def get(self):
        t = self._strong if self._weak is None else self._weak()
        return t if _alive(t) else None


class _CanvasWatcher(QObject):
    """Per-canvas receiver for QGIS signals.

    Real slots on a QObject (instead of lambdas) so that deleting the watcher
    makes Qt drop every connection, and only a *weak* reference back to the
    controller, so a lingering connection can never keep an old plugin
    instance alive across reloads."""

    def __init__(self, controller, canvas):
        super().__init__()
        self._ctl = weakref.ref(controller)
        self.canvas = canvas
        self.key = _addr(canvas)
        self.native_ref = None       # tool whose native cursor is recorded
        self.native_cursor = None    # that tool's own base cursor
        self.override_ref = None     # tool currently carrying our cursor
        self.override_key = None
        self.pending_ref = None      # tool switched to, awaiting activated()
        self.window_handle = None
        self.dead = False

    def _c(self):
        c = self._ctl()
        return None if (c is None or self.dead) else c

    def on_tool_set(self, new, old):
        c = self._c()
        if c is not None:
            c._on_tool_set(self, new, old)

    def on_activated(self):
        c = self._c()
        if c is not None:
            c._complete_activation(self)

    def on_screen_changed(self, *_a):
        c = self._c()
        if c is not None:
            c._apply(self)
            c._emit_status()

    def on_canvas_destroyed(self, *_a):
        c = self._c()
        if c is not None:
            c._forget(self)


class CursorController(QObject):
    statusChanged = pyqtSignal(str)

    def __init__(self, iface, cache=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.cache = cache or CursorCache()
        # category -> (display name, CursorStyle), or None for the QGIS cursor
        self.styles = {}
        self.enabled = False
        self._states = {}
        self._running = False
        self._action_map = None
        self.apply_count = 0   # number of QgsMapTool.setCursor calls we made
        self.status = ""
        self.blocked_category = None

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self._running:
            return
        self._running = True
        self._attach_known()
        QApplication.instance().focusChanged.connect(self._on_focus_changed)
        self.refresh()

    def stop(self):
        """Restore every tool's native cursor and disconnect everything."""
        if not self._running:
            return
        self._running = False
        try:
            QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
        except (TypeError, RuntimeError):
            pass
        for w in list(self._states.values()):
            self._cancel_pending(w)
            self._restore(w)
            self._detach(w)
        self._states.clear()
        self.cache.clear()

    # -------------------------------------------------------------- config
    def set_config(self, styles, enabled=None):
        """styles: {category: (display name, CursorStyle) or None}; see
        CursorManager.resolved_styles()."""
        self.styles = dict(styles)
        if enabled is not None:
            self.enabled = bool(enabled)
        if self._running:
            self._attach_known()
            self.refresh()

    def refresh(self):
        for w in list(self._states.values()):
            self._apply(w)
        self._emit_status()

    # ----------------------------------------------------------- canvases
    def _known_canvases(self):
        canvases = []
        main = self.iface.mapCanvas()
        if main is not None:
            canvases.append(main)
        with suppress(Exception):
            for c in self.iface.mapCanvases():
                if c is not None and all(_addr(c) != _addr(x) for x in canvases):
                    canvases.append(c)
        return canvases

    def _attach_known(self):
        for c in self._known_canvases():
            if _addr(c) not in self._states:
                self._attach(c)
                self._apply(self._states.get(_addr(c)))

    def _attach(self, canvas):
        if not _alive(canvas):
            return
        key = _addr(canvas)
        if key in self._states:
            return
        w = _CanvasWatcher(self, canvas)
        canvas.mapToolSet.connect(w.on_tool_set)
        canvas.destroyed.connect(w.on_canvas_destroyed)
        self._states[key] = w
        self._record_native(w, canvas.mapTool())

    def _detach(self, w):
        w.dead = True
        pairs = []
        if _alive(w.canvas):
            pairs += [(w.canvas.mapToolSet, w.on_tool_set),
                      (w.canvas.destroyed, w.on_canvas_destroyed)]
        if _alive(w.window_handle):
            pairs.append((w.window_handle.screenChanged, w.on_screen_changed))
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        w.canvas = w.window_handle = None
        w.native_ref = w.override_ref = w.pending_ref = None
        if not sip.isdeleted(w):
            w.deleteLater()  # Qt drops any connection we could not remove

    def _forget(self, w):
        self._states.pop(w.key, None)
        w.dead = True

    def _watch_screen(self, w):
        """Regenerate the cursor when the canvas window moves to a screen with
        a different scale factor (multi-monitor / changed Windows scaling)."""
        try:
            wh = w.canvas.window().windowHandle()
        except RuntimeError:
            return
        if wh is None or (_alive(w.window_handle) and _addr(wh) == _addr(w.window_handle)):
            return
        if _alive(w.window_handle):
            try:
                w.window_handle.screenChanged.disconnect(w.on_screen_changed)
            except (TypeError, RuntimeError):
                pass
        w.window_handle = wh
        wh.screenChanged.connect(w.on_screen_changed)

    def _on_focus_changed(self, _old, new):
        wid = new
        while wid is not None:
            if isinstance(wid, QgsMapCanvas):
                if _addr(wid) not in self._states:
                    self._attach(wid)
                    self._apply(self._states.get(_addr(wid)))
                    self._emit_status()
                return
            wid = wid.parentWidget()

    # --------------------------------------------------------------- tools
    def _record_native(self, w, tool):
        if _alive(tool):
            w.native_ref = _ToolRef(tool)
            w.native_cursor = QCursor(w.canvas.cursor())
        else:
            w.native_ref = None
            w.native_cursor = None

    def _on_tool_set(self, w, new, old):
        # NOTE: QgsMapCanvas::setMapTool() emits mapToolSet *before*
        # new->activate(); the canvas still shows the previous tool's cursor
        # here. The new tool's own base cursor is only on the canvas once
        # QgsMapTool::activate() emits `activated`, so we finish there
        # (synchronously, same event-loop turn: no visible flicker).
        self._restore(w, old)
        self._cancel_pending(w)
        w.native_ref = None
        w.native_cursor = None
        if self._running and not _alive(new):
            self._emit_status()
        if not _alive(new):
            return
        # a tool switch is also the moment new map views appear
        self._attach_known()
        w.pending_ref = _ToolRef(new)
        new.activated.connect(w.on_activated)
        # Safety net for a tool whose activate() never emits `activated`.
        QTimer.singleShot(0, w.on_activated)

    def _cancel_pending(self, w):
        if w.pending_ref is not None:
            tool = w.pending_ref.get()
            if tool is not None:
                try:
                    tool.activated.disconnect(w.on_activated)
                except (TypeError, RuntimeError):
                    pass
        w.pending_ref = None

    def _complete_activation(self, w):
        if w.pending_ref is None:
            return  # already completed (activated fired, timer is a no-op)
        addr = w.pending_ref.addr
        self._cancel_pending(w)
        if not _alive(w.canvas):
            return
        tool = w.canvas.mapTool()
        if not _alive(tool) or _addr(tool) != addr:
            return  # superseded by a newer tool switch
        self._record_native(w, tool)
        self._apply(w)
        self._emit_status()

    def _restore(self, state, hint=None):
        ref = state.override_ref
        if ref is None:
            return
        tool = ref.get()
        if tool is None and _alive(hint) and _addr(hint) == ref.addr:
            tool = hint
        if tool is not None and state.native_cursor is not None:
            tool.setCursor(state.native_cursor)
        state.override_ref = None
        state.override_key = None

    def _apply(self, state):
        if state is None or state.dead or not _alive(state.canvas) or state.pending_ref is not None:
            return
        canvas = state.canvas
        tool = canvas.mapTool()
        if not _alive(tool):
            return
        if state.native_ref is None or state.native_ref.addr != _addr(tool):
            # tool changed without us seeing it (e.g. plugin just started)
            self._restore(state)
            self._record_native(state, tool)
        style = self.style_for_tool(tool)
        if style is None:
            self._restore(state)
            return
        self._watch_screen(state)
        dpr = canvas.devicePixelRatioF()
        key = (style.key(), round(dpr, 4))
        if state.override_ref is not None and state.override_ref.addr == _addr(tool) \
                and state.override_key == key:
            return  # already showing this exact cursor: no flicker, no work
        res = self.cache.get(style, dpr)
        if res.cursor is None:
            self._restore(state)
            return
        state.override_ref = _ToolRef(tool)
        state.override_key = key
        tool.setCursor(res.cursor)
        self.apply_count += 1

    # ------------------------------------------------------- classification
    def _actions(self):
        if self._action_map is None:
            m = {}
            for cat, names in _ACTION_CATEGORIES.items():
                for name in names:
                    getter = getattr(self.iface, name, None)
                    try:
                        act = getter() if callable(getter) else None
                    except Exception:
                        act = None
                    if act is not None:
                        m[_addr(act)] = cat
            self._action_map = m
        return self._action_map

    def style_for_tool(self, tool):
        """The custom style this tool should carry, or None for its own cursor."""
        if not self.enabled:
            return None
        entry = self.styles.get(self.classify(tool))
        if entry is None or entry[1] is None or not entry[1].overrides_native:
            return None
        return entry[1]

    def classify(self, tool):
        try:
            act = tool.action()
        except RuntimeError:
            act = None
        if act is not None:
            cat = self._actions().get(_addr(act))
            if cat:
                return cat
        cat = _kga_category(tool)
        if cat:
            return cat
        if isinstance(tool, QgsMapToolCapture):
            return CAT_DIGITIZING
        if isinstance(tool, QgsMapToolEdit) or (_flag_int(tool.flags()) & _EDIT_TOOL_FLAG):
            return CAT_EDITING
        if isinstance(tool, (QgsMapToolPan, QgsMapToolZoom)):
            return CAT_NAVIGATION
        if isinstance(tool, QgsMapToolIdentify):
            return CAT_INSPECT
        return CAT_OTHER

    # -------------------------------------------------------------- status
    def describe_tool(self, tool):
        act = tool.action() if _alive(tool) else None
        if act is not None and act.text():
            return act.text().replace("&", "").rstrip(".… ")
        return tool.toolName() if hasattr(tool, "toolName") and tool.toolName() else type(tool).__name__

    def _emit_status(self):
        """Publish a one-line status plus, when the custom cursor is held back
        only because the active tool's family is switched off, that family in
        `blocked_category` so the UI can offer a one-click fix."""
        canvas = self.iface.mapCanvas()
        tool = canvas.mapTool() if _alive(canvas) else None
        blocked = None
        if not self.enabled:
            text = "Custom cursor is switched off - QGIS cursors are shown"
        elif not _alive(tool):
            text = "No active map tool"
        else:
            cat = self.classify(tool)
            label = dict((k, l) for k, l, _ in CATEGORY_LABELS)[cat]
            entry = self.styles.get(cat)
            state = self._states.get(_addr(canvas))
            on = state is not None and state.override_ref is not None
            name = self.describe_tool(tool)
            if entry is None or entry[1] is None:
                text = f"{name}: QGIS cursor - “{label}” is set to QGIS cursor"
                blocked = cat
            elif not entry[1].overrides_native:
                text = f"{name}: QGIS cursor - “{entry[0]}” keeps QGIS cursors; pick a preset"
            elif on:
                text = f"{name}: {entry[0]}"
            else:
                text = f"{name}: QGIS cursor"
        if text != self.status or blocked != self.blocked_category:
            self.status = text
            self.blocked_category = blocked
            self.statusChanged.emit(text)

    # ------------------------------------------------------------- testing
    def overridden_tool(self, canvas=None):
        canvas = canvas or self.iface.mapCanvas()
        st = self._states.get(_addr(canvas))
        return st.override_ref.get() if st and st.override_ref else None

    def attached_count(self):
        return len(self._states)

"""Discovery of QGIS actions and stable tool identifiers.

Identifier scheme (stored in workspace configs):

* ``action:<objectName>``           QAction with an object name (all core QGIS
                                    tools, e.g. ``action:mActionAddRing``)
* ``menu:<Top/Sub/...>::<text>``    unnamed action found in the menu bar
                                    (typical for third-party plugin entries)
* ``toolbar:<toolbar name>::<text>`` unnamed action only present on a toolbar
* ``processing:<algorithm id>``     Processing algorithm, e.g.
                                    ``processing:native:buffer``

Native actions are never copied or modified: the workspace toolbar adds the
*same* QAction object, so clicking it is identical to clicking the original
(checked state, enabled state, map-tool exclusivity and requirements such as
"layer must be in edit mode" are all preserved by QGIS itself).
"""

import time
from contextlib import suppress
from functools import partial

from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QDockWidget, QToolBar, QToolButton

from .compat import (OWN_PREFIX, QAction, QWidgetAction, alive, is_own_object,
                     message_level, ptr)

PROCESSING_PREFIX = "processing:"
SEP = " › "  # " › "


def clean_text(text):
    text = (text or "").replace("&&", "\x00").replace("&", "").replace("\x00", "&")
    text = text.split("\t")[0].strip()
    for suffix in ("…", "..."):
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip()
    return text


class ToolInfo:
    __slots__ = ("id", "text", "category", "source", "locations", "loc_sources", "action",
                 "alg_id", "description")

    def __init__(self, tid, text, category, source, action=None, alg_id=None, description=""):
        self.id = tid
        self.text = text
        self.category = category
        self.source = source            # "menu" | "toolbar" | "other" | "processing"
        self.locations = []
        self.loc_sources = []           # parallel to locations: where each one came from
        self.action = action
        self.alg_id = alg_id
        self.description = description

    @property
    def where(self):
        return "; ".join(self.locations)


class ActionRegistry(QObject):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setObjectName(OWN_PREFIX + "_registry")
        self.iface = iface
        self._tools = {}          # id -> ToolInfo (native actions)
        self._by_ptr = {}         # C++ address -> ToolInfo
        self._algs = None         # list[ToolInfo] (lazy)
        self._launchers = {}      # alg id -> QAction
        self._native_toolbars = []
        self._last_scan = 0.0
        self._launcher_parent = QObject(self)
        self._launcher_parent.setObjectName(OWN_PREFIX + "_launchers")

    # ------------------------------------------------------------ scanning
    @staticmethod
    def _acceptable(action):
        if not alive(action) or action.isSeparator() or isinstance(action, QWidgetAction):
            return False
        if (action.objectName() or "").startswith(OWN_PREFIX):
            return False
        if action.menu() is not None:
            return False
        return bool(clean_text(action.text() or action.iconText()))

    def scan(self, throttle=0.0):
        """Rebuild the action index. With *throttle* > 0, skip if scanned recently."""
        now = time.monotonic()
        if throttle and now - self._last_scan < throttle:
            return False
        self._last_scan = now
        mw = self.iface.mainWindow()
        tools, by_ptr = {}, {}

        def register(action, location, source, path_id):
            key = ptr(action)
            info = by_ptr.get(key)
            if info is not None:
                if location and (location, source) not in zip(info.locations, info.loc_sources):
                    info.locations.append(location)
                    info.loc_sources.append(source)
                return
            name = action.objectName() or ""
            tid = ("action:" + name) if name else path_id
            if not tid:
                return
            if tid in tools and name and path_id:
                tid = path_id
            base, n = tid, 2
            while tid in tools:
                tid = f"{base}#{n}"
                n += 1
            text = clean_text(action.text() or action.iconText())
            category = location.split(SEP)[0] if location else "Other"
            desc = action.statusTip() or ""
            if clean_text(desc) == text:
                desc = ""
            info = ToolInfo(tid, text, category, source, action=action, description=desc)
            if location:
                info.locations.append(location)
                info.loc_sources.append(source)
            tools[tid] = info
            by_ptr[key] = info

        def walk_menu(menu, path, source, location=None, depth=0):
            if not alive(menu) or depth > 8 or is_own_object(menu):
                return
            for a in menu.actions():
                if not alive(a) or a.isSeparator():
                    continue
                sub = a.menu()
                if sub is not None:
                    walk_menu(sub, path + [clean_text(a.text()) or "…"], source, location, depth + 1)
                elif self._acceptable(a):
                    loc = location or SEP.join(path)
                    register(a, loc, source, f"{source}:{'/'.join(path)}::{clean_text(a.text())}")

        # 1) menu bar (gives the most readable categories: Vector, Raster, Plugins ...)
        menubar = mw.menuBar()
        if menubar is not None:
            for a in menubar.actions():
                if alive(a) and a.menu() is not None:
                    walk_menu(a.menu(), [clean_text(a.text())], "menu")

        # 2) every toolbar, including toolbars inside panels and floating toolbars
        self._native_toolbars = []
        for tb in mw.findChildren(QToolBar):
            if not alive(tb) or is_own_object(tb):
                continue
            self._native_toolbars.append(tb)
            title = tb.windowTitle()
            if not title:
                p = tb.parent()
                while p is not None and not isinstance(p, QDockWidget):
                    p = p.parent()
                title = (p.windowTitle() + " panel") if p is not None else (tb.objectName() or "Toolbar")
            tb_key = tb.objectName() or title
            for a in tb.actions():
                if not alive(a) or a.isSeparator():
                    continue
                if isinstance(a, QWidgetAction):
                    w = tb.widgetForAction(a)
                    if isinstance(w, QToolButton):
                        d = w.defaultAction()
                        if self._acceptable(d):
                            register(d, title, "toolbar", f"toolbar:{tb_key}::{clean_text(d.text())}")
                        if w.menu() is not None:
                            walk_menu(w.menu(), [tb_key], "toolbar", title)
                    continue
                if a.menu() is not None:
                    walk_menu(a.menu(), [tb_key], "toolbar", title)
                    continue
                w = tb.widgetForAction(a)
                if isinstance(w, QToolButton) and w.menu() is not None:
                    walk_menu(w.menu(), [tb_key], "toolbar", title)
                if self._acceptable(a):
                    register(a, title, "toolbar", f"toolbar:{tb_key}::{clean_text(a.text())}")

        # 3) remaining named actions owned by the main window (e.g. digitizing
        #    actions not placed on any visible toolbar)
        for a in mw.findChildren(QAction):
            if self._acceptable(a) and a.objectName() and ptr(a) not in by_ptr:
                register(a, "Other", "other", "")

        self._tools, self._by_ptr = tools, by_ptr
        self._algs = None
        return True

    # ------------------------------------------------------------ processing
    @staticmethod
    def _algorithm_info(alg):
        """One Processing algorithm as a ToolInfo (None if QGIS cannot describe it)."""
        try:
            provider = alg.provider().name() if alg.provider() else "Processing"
            group = alg.group() or ""
            cat = "Processing" + SEP + provider + ((SEP + group) if group else "")
            info = ToolInfo(PROCESSING_PREFIX + alg.id(), alg.displayName(),
                            "Processing", "processing", alg_id=alg.id(),
                            description=(alg.shortDescription() or "") if hasattr(alg, "shortDescription") else "")
        except Exception:
            return None
        info.locations.append(cat)
        info.loc_sources.append("processing")
        return info

    def processing_tools(self):
        if self._algs is None:
            algs = []
            with suppress(Exception):
                from qgis.core import QgsApplication
                for alg in QgsApplication.processingRegistry().algorithms():
                    info = self._algorithm_info(alg)
                    if info is not None:
                        algs.append(info)
            self._algs = algs
        return self._algs

    def _alg(self, alg_id):
        try:
            from qgis.core import QgsApplication
            return QgsApplication.processingRegistry().algorithmById(alg_id)
        except Exception:
            return None

    def _launcher(self, alg_id):
        a = self._launchers.get(alg_id)
        if alive(a):
            return a
        alg = self._alg(alg_id)
        if alg is None:
            return None
        a = QAction(alg.icon(), alg.displayName(), self._launcher_parent)
        a.setObjectName(f"{OWN_PREFIX}_alg_{alg_id}")
        a.setToolTip(alg.displayName())
        a.setStatusTip(f"Processing algorithm: {alg_id}")
        a.triggered.connect(partial(self._run_algorithm, alg_id))
        self._launchers[alg_id] = a
        return a

    def _run_algorithm(self, alg_id, *_):
        try:
            import processing  # QGIS' Processing plugin
            processing.execAlgorithmDialog(alg_id)
        except Exception as exc:  # pragma: no cover - depends on QGIS runtime
            self.iface.messageBar().pushMessage(
                "Tool Bag",
                f"Could not open Processing algorithm '{alg_id}': {exc}",
                level=message_level("Warning"), duration=6)

    # ------------------------------------------------------------ public lookups
    def native_toolbars(self):
        return [tb for tb in self._native_toolbars if alive(tb)]

    def tools(self, include_processing=True):
        items = list(self._tools.values())
        if include_processing:
            items.extend(self.processing_tools())
        return items

    def info(self, tid):
        if tid.startswith(PROCESSING_PREFIX):
            alg_id = tid[len(PROCESSING_PREFIX):]
            for i in self.processing_tools():
                if i.alg_id == alg_id:
                    return i
            return None
        info = self._tools.get(tid)
        if info is not None and not alive(info.action):
            return None
        return info

    def action_for(self, tid):
        """Return the live QAction for a tool id, or None if it is not available now."""
        if not tid:
            return None
        if tid.startswith(PROCESSING_PREFIX):
            return self._launcher(tid[len(PROCESSING_PREFIX):])
        info = self._tools.get(tid)
        if info is not None and alive(info.action):
            return info.action
        if tid.startswith("action:") and "#" not in tid:
            a = self.iface.mainWindow().findChild(QAction, tid[len("action:"):])
            if self._acceptable(a):
                return a
        return None

    def id_for_action(self, action):
        """Stable id of an arbitrary QAction (used by pick mode)."""
        if not alive(action):
            return None
        name = action.objectName() or ""
        if name.startswith(OWN_PREFIX + "_alg_"):
            return PROCESSING_PREFIX + name[len(OWN_PREFIX + "_alg_"):]
        info = self._by_ptr.get(ptr(action))
        if info is None:
            self.scan()
            info = self._by_ptr.get(ptr(action))
        if info is not None:
            return info.id
        if name and self._acceptable(action):
            return "action:" + name
        return None

    def display_text(self, tid, fallback=""):
        info = self.info(tid)
        if info is not None:
            return info.text
        a = self.action_for(tid)
        if a is not None:
            return clean_text(a.text())
        return fallback or tid.split("::")[-1].split(":")[-1]

    def unload(self):
        for a in self._launchers.values():
            if alive(a):
                a.deleteLater()
        self._launchers.clear()
        self._tools.clear()
        self._by_ptr.clear()

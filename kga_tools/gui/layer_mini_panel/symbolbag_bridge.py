# -*- coding: utf-8 -*-
"""Bridge to Symbol Bag.

KGA Toolbox has Symbol Bag built in (``gui/symbol_bag``); the panel gets it from the plugin through a
provider callable. When the built-in bag is off (the stand-alone Symbol Bag plugin is installed, so KGA
skipped its own), the running stand-alone instance is found in qgis.utils.plugins instead and its own
modules are imported from wherever QGIS installed it. The library, previews, colour tools and the
renderer-safe apply logic all stay Symbol Bag's.

Used Symbol Bag interfaces (checked at runtime; anything missing is reported instead of guessed):

  plugin.store / plugin._ensure_store()   SQLite library (opened without showing the bag)
  store.symbol_records(), record(), symbol_xml(), collections(), preview(), mark_used(),
  store.add_listener() / remove_listener()
  core.applier.apply_to_layers(), classified_type(), is_compatible(), incompatibility_reason(),
               MODE_REPLACE / MODE_KEEP_COLORS / MODE_SAME, LayerStyleUndo, ApplyError
  core.symbols.layer_kind(), symbol_from_xml(), vector_preview()
  plugin._with_panel(), panel.set_category(), panel.edit_symbol(), panel.open_layer_colors(),
  panel.undo (shared undo history), panel._update_footer()
  plugin.create_layer_colors_widget(parent)   Layer Colors as a child widget (built-in bag)
"""

from contextlib import suppress

import importlib

from qgis.core import QgsProject
from qgis.PyQt.QtGui import QPixmap

REQUIRED = {
    "core.applier": ("apply_to_layers", "classified_type", "is_compatible", "incompatibility_reason",
                     "MODE_KEEP_COLORS", "MODE_SAME", "MODE_REPLACE", "ApplyError", "LayerStyleUndo"),
    "core.symbols": ("layer_kind", "symbol_from_xml", "vector_preview"),
    "core.storage": ("KIND_MARKER", "KIND_LINE", "KIND_FILL"),
}
PLUGIN_KEYS = ("symbol_bag", "SymbolBag", "symbolbag")


def _package_of(inst):
    """'kga_tools.gui.symbol_bag' for the built-in bag, 'symbol_bag' for the stand-alone plugin."""
    return type(inst).__module__.rsplit(".", 1)[0]


def _importable(module):
    with suppress(Exception):
        importlib.import_module(module)
        return True
    return False


class SymbolBagUnavailable(Exception):
    pass


class SymbolBagBridge(object):
    def __init__(self, iface, provider=None):
        self.iface = iface
        self.provider = provider     # callable -> KGA's built-in SymbolBagPlugin, or None
        self.instance = None
        self.package = None
        self.mods = {}
        self.error = None
        self._own_undo = None
        self._listeners = []
        self._store = None

    # ------------------------------------------------------------------ discovery
    def _find_instance(self):
        if self.provider is not None:
            inst = None
            with suppress(Exception):
                inst = self.provider()
            if inst is not None:
                return inst, _package_of(inst)
        try:
            from qgis import utils
            plugins = utils.plugins
        except Exception:
            return None, None
        for key in PLUGIN_KEYS:
            inst = plugins.get(key)
            if inst is not None:
                return inst, _package_of(inst)
        for _key, inst in plugins.items():  # renamed folder: recognise it by its modules
            pkg = _package_of(inst)
            if (hasattr(inst, "_ensure_store") and hasattr(inst, "toggle_action")
                    and _importable(pkg + ".core.applier")):
                return inst, pkg
        return None, None

    def resolve(self):
        """(Re)discover Symbol Bag. Returns True when it is usable."""
        inst, pkg = self._find_instance()
        if inst is None:
            self._reset("Symbol Bag is not available.")
            return False
        if inst is self.instance and self.mods and self.error is None:
            return True
        mods = {}
        for mod, names in REQUIRED.items():
            try:
                m = importlib.import_module("{}.{}".format(pkg, mod))
            except Exception as e:
                self._reset("Symbol Bag is installed but its '{}' module could not be loaded ({}).".format(mod, e))
                return False
            missing = [n for n in names if not hasattr(m, n)]
            if missing:
                self._reset("This Symbol Bag version is not compatible (missing {}). "
                            "Please update Symbol Bag.".format(", ".join(missing)))
                return False
            mods[mod] = m
        self.instance, self.package, self.mods, self.error = inst, pkg, mods, None
        return True

    def _reset(self, error):
        self.detach_listeners()
        self.instance = self.package = None
        self.mods = {}
        self._store = None
        self.error = error

    @property
    def available(self):
        return self.instance is not None and not self.error

    def version(self):
        try:
            mod = importlib.import_module(self.package + ".plugin")
            return mod.plugin_version()
        except Exception:
            return "?"

    # ------------------------------------------------------------------ library
    def store(self):
        if not self.available:
            raise SymbolBagUnavailable(self.error or "Symbol Bag is not available.")
        st = getattr(self.instance, "store", None)
        if st is None and hasattr(self.instance, "_ensure_store"):
            st = self.instance._ensure_store()   # opens the library; the bag stays closed
        if st is None:
            raise SymbolBagUnavailable("Symbol Bag's library could not be opened.")
        if st is not self._store:
            # library object replaced (plugin reloaded): move our listeners over
            old = self._listeners[:]
            self.detach_listeners()
            self._store = st
            for cb in old:
                self.add_listener(cb)
        return st

    def add_listener(self, cb):
        st = self._store if self._store is not None else None
        if cb not in self._listeners:
            self._listeners.append(cb)
        if st is not None and hasattr(st, "add_listener"):
            st.add_listener(cb)

    def detach_listeners(self):
        st = self._store
        if st is not None and hasattr(st, "remove_listener"):
            for cb in self._listeners:
                with suppress(Exception):
                    st.remove_listener(cb)
        self._listeners_backup = self._listeners
        self._listeners = []

    def layer_kind(self, layer):
        return self.mods["core.symbols"].layer_kind(layer)

    def records_for_kind(self, kind):
        return [r for r in self.store().symbol_records() if r.kind == kind]

    def collections(self):
        st = self.store()
        try:
            return {c["id"]: c for c in st.collections()}
        except Exception:
            return {}

    def collection_label(self, cid, cols):
        parts = []
        seen = 0
        while cid is not None and cid in cols and seen < 10:
            parts.append(cols[cid]["name"])
            cid = cols[cid].get("parent_id")
            seen += 1
        return " / ".join(reversed(parts))

    def preview_pixmap(self, rec, size):
        """Symbol Bag's cached card image when present, else its own renderer."""
        st = self.store()
        with suppress(Exception):
            data = st.preview(rec.uid)
            if data:
                pm = QPixmap()
                if pm.loadFromData(data):
                    return pm
        sym = self.mods["core.symbols"].symbol_from_xml(st.symbol_xml(rec.uid))
        return self.mods["core.symbols"].vector_preview(sym, size)

    # ------------------------------------------------------------------ renderer info
    def renderer_info(self, layer):
        """(type key, human label, class count, is_classified)."""
        ap = self.mods.get("core.applier")
        r = layer.renderer() if layer is not None else None
        if r is None:
            return None, "No renderer", 0, False
        inner = r
        wrappers = []
        for _ in range(5):
            if hasattr(inner, "embeddedRenderer") and inner.embeddedRenderer() is not None:
                wrappers.append(inner.type())
                inner = inner.embeddedRenderer()
            else:
                break
        t = inner.type()
        labels = {"singleSymbol": "Single symbol", "categorizedSymbol": "Categorized",
                  "graduatedSymbol": "Graduated", "RuleRenderer": "Rule-based",
                  "heatmapRenderer": "Heatmap", "pointCluster": "Point cluster",
                  "pointDisplacement": "Point displacement", "invertedPolygonRenderer": "Inverted polygons",
                  "25dRenderer": "2.5 D", "nullSymbol": "No symbols", "embeddedSymbol": "Embedded symbols"}
        label = labels.get(t, t)
        if wrappers:
            label = "{} inside {}".format(label, " / ".join(labels.get(w, w) for w in wrappers))
        try:
            n = len(inner.legendSymbolItems())
        except Exception:
            n = 0
        classified = bool(ap and ap.classified_type(layer))
        return t, label, n, classified

    def can_restyle(self, layer):
        """Renderer types Symbol Bag can restyle without replacing them."""
        t, _l, _n, classified = self.renderer_info(layer)
        return classified or t == "singleSymbol"

    # ------------------------------------------------------------------ apply
    def undo_stack(self):
        panel = getattr(self.instance, "panel", None)
        if panel is not None and getattr(panel, "undo", None) is not None:
            return panel.undo  # shared with the bag: its Ctrl+Z also undoes our applies
        if self._own_undo is None:
            self._own_undo = self.mods["core.applier"].LayerStyleUndo()
        return self._own_undo

    def mode(self, name):
        return getattr(self.mods["core.applier"], name)

    def apply(self, rec, layer, mode):
        """Apply via Symbol Bag. Returns (ok, message)."""
        ap = self.mods["core.applier"]
        st = self.store()
        if not ap.is_compatible(layer, rec.kind):
            return False, ap.incompatibility_reason(layer, rec.kind)
        try:
            applied, skipped, warnings = ap.apply_to_layers([layer], rec.kind, st.symbol_xml(rec.uid),
                                                            mode=mode, undo=self.undo_stack())
        except ap.ApplyError as e:
            return False, str(e)
        if not applied:
            return False, "; ".join(r for _n, r in skipped) or "The symbol could not be applied."
        with suppress(Exception):
            st.mark_used(rec.uid, notify=False)
        with suppress(Exception):
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        QgsProject.instance().setDirty(True)
        panel = getattr(self.instance, "panel", None)
        if panel is not None and hasattr(panel, "_update_footer"):
            with suppress(Exception):
                panel._update_footer()  # enable the bag's undo button
        msg = "Applied “{}” to {}.".format(rec.name, layer.name())
        if warnings:
            msg += " " + " ".join(warnings[:2])
        return True, msg

    def can_undo(self):
        try:
            return self.undo_stack().can_undo()
        except Exception:
            return False

    def undo(self):
        n = self.undo_stack().undo()
        if n:
            with suppress(Exception):
                for lyr in QgsProject.instance().mapLayers().values():
                    self.iface.layerTreeView().refreshLayerSymbology(lyr.id())
        return n

    # ------------------------------------------------------------------ open Symbol Bag UI
    def _with_panel(self, fn):
        inst = self.instance
        if hasattr(inst, "_with_panel"):
            inst._with_panel(fn)
            return True
        if getattr(inst, "toggle_action", None) is not None:
            inst.toggle_action.setChecked(True)
            panel = getattr(inst, "panel", None)
            if panel is not None:
                fn(panel)
                return True
        return False

    def open_bag(self, kind=None):
        def fn(panel):
            if kind and hasattr(panel, "set_category"):
                try:
                    panel.set_category(kind, user=True)
                except TypeError:
                    panel.set_category(kind)
        return self._with_panel(fn)

    def edit_symbol(self, uid):
        return self._with_panel(lambda p: p.edit_symbol(uid) if hasattr(p, "edit_symbol") else None)

    # ------------------------------------------------------------------ embedded Layer Colors
    RECOMMENDED = (1, 16, 0)     # Size / Outline chips for points, wrapping chip row

    def version_tuple(self):
        try:
            return tuple(int(x) for x in self.version().split(".")[:3])
        except (ValueError, AttributeError):
            return (0, 0, 0)

    def supports_point_size(self):
        """Symbol Bag 1.16.0+ (point Size / Outline in Layer Colors)."""
        try:
            has = hasattr(importlib.import_module(self.package + ".core.recolor"), "size_info")
        except Exception:
            has = False
        return has and self.version_tuple() >= self.RECOMMENDED

    def can_embed_colors(self):
        return self.available and hasattr(self.instance, "create_layer_colors_widget")

    def create_layer_colors_widget(self, parent):
        """Symbol Bag's Layer Colors as a child widget (Symbol Bag 1.13.1+)."""
        if not self.can_embed_colors():
            return None
        return self.instance.create_layer_colors_widget(parent)

    def theme(self):
        """Symbol Bag's current style object, if the bag window exists."""
        panel = getattr(self.instance, "panel", None) if self.instance is not None else None
        return getattr(panel, "theme", None)

    def make_theme(self, style):
        """A Symbol Bag Theme object for a bag style name (for the embedded wheel)."""
        try:
            mod = importlib.import_module(self.package + ".ui.theme")
            return mod.Theme(style, 1.0, False)
        except Exception:
            return None

    def palette(self):
        """Colours of the current Symbol Bag style (dict), or None.
        Read from the bag window when it exists, else from its saved settings
        (so the bag window never has to be opened just for colours)."""
        if not (self.available or self.resolve()):
            return None
        t = self.theme()
        if t is not None and hasattr(t, "p"):
            return dict(t.p)
        try:
            styles = importlib.import_module(self.package + ".ui.theme").BAG_STYLES
            name = (self.store().get_setting("ui", {}) or {}).get("style", "mint")
            return dict(styles.get(name) or styles["mint"])
        except Exception:
            return None

    def open_layer_colors(self):
        inst = self.instance
        if hasattr(inst, "_ensure_panel"):
            panel = inst._ensure_panel()
            if hasattr(panel, "open_layer_colors"):
                from qgis.PyQt.QtGui import QCursor
                panel.open_layer_colors(QCursor.pos())
                return True
        return False

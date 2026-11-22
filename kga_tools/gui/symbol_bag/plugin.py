# -*- coding: utf-8 -*-
"""Symbol Bag and Layer Colors, built into KGA Toolbox.

Runs embedded: the two toolbar buttons (``toggle_action``, ``colors_action``) are owned by KgaToolsPlugin,
which puts them right after the Tool Bag button, and nothing is added to the Plugins menu or toolbar.
The library stays in ``<profile>/symbol_bag/`` so a bag made with the stand-alone plugin carries over.
Both bubbles sit in the same column as the Tool Bag's bubble (``gui/bag_group.py``).
"""

import os
from contextlib import suppress


from qgis.PyQt.QtGui import QIcon
from qgis.core import Qgis, QgsApplication

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QAction

from .core.log import log_error, log_info
from .core.storage import SymbolStore

PLUGIN_DIR = os.path.dirname(__file__)
VERSION = "1.16.0"

try:
    _VECTOR = Qgis.LayerType.Vector
    _RASTER = Qgis.LayerType.Raster
except AttributeError:  # QGIS < 3.30
    from qgis.core import QgsMapLayerType
    _VECTOR = QgsMapLayerType.VectorLayer
    _RASTER = QgsMapLayerType.RasterLayer


def plugin_version():
    return VERSION


class SymbolBagPlugin(object):
    def __init__(self, iface, group=None, toggle_icon=None):
        self.iface = iface
        self._toggle_icon = toggle_icon
        self.group = group       # BubbleGroup shared with the Tool Bag
        self.store = None
        self.panel = None
        self.toggle_action = None
        self.layer_action = None
        self.colors_action = None
        self.layer_colors_action = None
        self.extra_actions = []
        self._pick_callback = None
        self._pick_connected = False

    # ------------------------------------------------------------------
    def initGui(self):
        icon = self._toggle_icon or QIcon(os.path.join(PLUGIN_DIR, "icons", "symbol_bag.svg"))
        self.toggle_action = QAction(icon, "Symbol Bag", self.iface.mainWindow())
        self.toggle_action.setObjectName("SymbolBagToggle")
        self.toggle_action.setCheckable(True)
        self.toggle_action.setToolTip("Symbol Bag: your floating symbol library")
        self.toggle_action.toggled.connect(self._toggle_panel)
        with suppress(Exception):
            self.iface.registerMainWindowAction(self.toggle_action, "")

        # standalone colour picker: works on map layers without opening the bag
        colors_icon = QIcon(os.path.join(PLUGIN_DIR, "icons", "symbol_bag_colors.svg"))
        self.colors_action = QAction(colors_icon, "Layer Colors", self.iface.mainWindow())
        self.colors_action.setObjectName("SymbolBagColors")
        self.colors_action.setToolTip("Layer Colors – recolor the selected layer or categories live on the map\n"
                                      "(works on its own; minimize it to a bubble like the bag)")
        self.colors_action.triggered.connect(self._toggle_colors)
        self.layer_colors_action = QAction(colors_icon, "Recolor Layer Colors…", self.iface.mainWindow())
        self.layer_colors_action.triggered.connect(self._recolor_layer_from_context)
        self.iface.addCustomActionForLayerType(self.layer_colors_action, "", _VECTOR, True)
        self.iface.addCustomActionForLayerType(self.layer_colors_action, "", _RASTER, True)

        # Layers panel context menu: "Add Layer Symbol to Symbol Bag"
        self.layer_action = QAction(icon, "Add Layer Symbol to Symbol Bag", self.iface.mainWindow())
        self.layer_action.triggered.connect(self._add_layer_from_context)
        self.iface.addCustomActionForLayerType(self.layer_action, "", _VECTOR, True)
        self.iface.addCustomActionForLayerType(self.layer_action, "", _RASTER, True)

    def unload(self):
        if self.panel is not None:
            try:
                self.panel.save_window_state()
                self.panel.cleanup()
                self.panel.hide()
                self.panel.setParent(None)
                self.panel.deleteLater()
            except RuntimeError:
                pass
            self.panel = None
            self._pick_connected = False
        if getattr(self, "colors_action", None) is not None:
            self.colors_action = None
        if getattr(self, "layer_colors_action", None) is not None:
            self.iface.removeCustomActionForLayerType(self.layer_colors_action)
            self.layer_colors_action = None
        if self.layer_action is not None:
            self.iface.removeCustomActionForLayerType(self.layer_action)
            self.layer_action = None
        if self.toggle_action is not None:
            with suppress(Exception):
                self.iface.unregisterMainWindowAction(self.toggle_action)
            self.toggle_action = None
        if self.store is not None:
            self.store.close()
            self.store = None

    # ------------------------------------------------------------------
    def _ensure_store(self):
        if self.store is not None:
            return self.store
        folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "symbol_bag")
        os.makedirs(folder, exist_ok=True)
        self.store = SymbolStore(folder)
        if not self.store.is_seeded():
            from .core.defaults import seed_library
            n = seed_library(self.store)
            log_info("Starter library created with {} symbols.".format(n))
        else:
            from .ui.dialogs import DEFAULT_SETTINGS
            keep = (self.store.get_setting("ui", {}) or {}).get("auto_backups", DEFAULT_SETTINGS["auto_backups"])
            try:
                self.store.auto_backup(keep)
            except Exception as e:
                log_error("Automatic backup failed: {}".format(e))
        return self.store

    def _ensure_panel(self):
        if self.panel is not None:
            return self.panel
        from .ui.panel import SymbolBagPanel
        store = self._ensure_store()
        self.panel = SymbolBagPanel(self.iface, store, PLUGIN_DIR, plugin_version(), self.iface.mainWindow(),
                                    group=self.group)
        if not self.panel.restore_window_state():
            # first run: float near the top-right corner of the map canvas
            canvas = self.iface.mapCanvas()
            tr = canvas.mapToGlobal(canvas.rect().topRight())
            self.panel.resize(320, 560)
            self.panel.move(tr.x() - 340, tr.y() + 30)
        self.panel.visibilityChanged.connect(self._panel_visibility)
        self.panel.hide()
        return self.panel

    # ------------------------------------------------------------------
    # integration API for other plugins (e.g. Layer Bag)
    # ------------------------------------------------------------------
    def create_layer_colors_widget(self, parent):
        """Return Layer Colors as a child widget of `parent`.

        It follows the Layers panel selection (layer or selected classes),
        recolours live on the map and shares the bag's theme and Ctrl+Z.
        When Symbol Bag is unloaded the widget is deleted (watch destroyed())."""
        return self._ensure_panel().create_embedded_layer_colors(parent)

    def _with_panel(self, func):
        panel = self._ensure_panel()
        if panel.in_bubble:
            panel.restore_from_bubble()
        elif not panel.isVisible():
            self.toggle_action.setChecked(True)
        func(panel)

    # ------------------------------------------------------------------ used by Quick Symbology
    def start_picking(self, target, on_applied=None):
        """Open the bag so that what is applied in it goes to `target()` = (layer, class key or None).

        `on_applied(layers)` runs after each apply. Quick Symbology calls `stop_picking` when its window closes.
        """
        panel = self._ensure_panel()
        panel.target_override = target
        self._pick_callback = on_applied
        if not self._pick_connected:
            panel.symbolApplied.connect(self._pick_applied)
            self._pick_connected = True
        with suppress(Exception):
            panel._update_footer()
        if panel.in_bubble:
            panel.restore_from_bubble()
        elif not panel.is_active():
            self.toggle_action.setChecked(True)
        else:
            panel.raise_()

    def stop_picking(self):
        self._pick_callback = None
        if self.panel is not None:
            self.panel.target_override = None
            with suppress(Exception):
                self.panel._update_footer()

    def _pick_applied(self, layers):
        if self._pick_callback is not None:
            self._pick_callback(layers)

    def _toggle_panel(self, checked):
        panel = self._ensure_panel()
        if checked and not panel.is_active():
            panel.show_animated()
        elif not checked and panel.is_active():
            panel.close_animated()

    def _panel_visibility(self, visible):
        if self.toggle_action is not None and self.toggle_action.isChecked() != visible:
            self.toggle_action.blockSignals(True)
            self.toggle_action.setChecked(visible)
            self.toggle_action.blockSignals(False)

    def _toggle_colors(self):
        """Toolbar: the standalone Layer Colors tool (the bag can stay closed)."""
        from qgis.PyQt.QtGui import QCursor
        self._ensure_panel().toggle_layer_colors(QCursor.pos())

    def _recolor_layer_from_context(self):
        from qgis.PyQt.QtGui import QCursor
        self._ensure_panel().open_layer_colors(QCursor.pos())

    def _add_layer_from_context(self):
        layer = self.iface.activeLayer()
        self._with_panel(lambda p: p.add_current_layer(layer))

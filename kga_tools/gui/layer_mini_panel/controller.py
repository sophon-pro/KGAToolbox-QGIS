# -*- coding: utf-8 -*-
"""Layer Bag, built into KGA Toolbox.

KgaToolsPlugin puts ``action`` first on the KGA Layer toolbar and in the KGA Toolbox menu; nothing is
added to the Plugins menu. The panel (a floating dock over the map) is created the first time it is
shown. With the plugin's BubbleGroup it minimizes into a bubble on the map, in the same column as the
Tool Bag, Symbol Bag and Catalog bubbles (`gui/panel_bubble.PanelBubbleManager`). Settings stay under ``LayerMiniPanel/`` so a panel set up with the stand-alone plugin carries over,
but the built-in panel starts closed.
"""

from contextlib import suppress

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QTimer

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QAction

from . import compat as C

TOOLTIP = ("Layer Bag - the attribute table, statistics, labels and colours of the layer selected "
           "in the Layers panel, in one small floating panel")


class LayerMiniPanelController(object):
    # what a sibling bag (the Label Bag) overrides
    NAME = C.PLUGIN_NAME
    ACTION_NAME = "mActionKgaLayerMiniPanel"
    TIP = TOOLTIP
    BUBBLE_TIP = "Layer Bag – click to open"
    GLYPH = "bubble/layer_mini_panel.png"
    VISIBLE_KEY = C.SETTINGS_KEY + "visible"

    def __init__(self, iface, bag_provider=None, icon=None, group=None):
        self.iface = iface
        self.bag_provider = bag_provider      # callable -> KGA's Symbol Bag plugin, or None
        self._icon = icon
        self.group = group                    # the bags' BubbleGroup (minimize to a bubble), or None
        self.action = None
        self.dock = None
        self.watcher = None
        self.bubble = None                    # PanelBubbleManager

    def initGui(self):  # noqa: N802
        from .ui_common import plugin_icon
        self.action = QAction(self._icon if self._icon is not None and not self._icon.isNull() else plugin_icon(),
                              self.NAME, self.iface.mainWindow())
        self.action.setObjectName(self.ACTION_NAME)
        self.action.setCheckable(True)
        self.action.setToolTip(self.TIP)
        self.action.toggled.connect(self._toggle)
        if QgsSettings().value(self.VISIBLE_KEY, False, type=bool):
            # wait until QGIS has restored its window layout
            QTimer.singleShot(0, self._restore_open)

    def _restore_open(self):
        if self.action is not None:
            self.action.setChecked(True)

    def _ensure_dock(self):
        if self.dock is not None:
            return self.dock
        from .watcher import LayerWatcher
        self.watcher = LayerWatcher(self.iface)
        self.dock = self._new_dock()
        self.dock.resize(380, self.dock.height())
        # The bag is never shown docked, not even for one frame. QGIS's iface.addDockWidget docks AND shows a
        # dock at once: on Windows that painted an empty dock area under the Catalog for a frame before the
        # bag floated (a flash), and the bag was already visible, so its first open could not animate.
        # Qt's own addDockWidget keeps an explicitly hidden dock hidden; it floats while still hidden and is
        # first shown by the open animation. (The only thing skipped is an entry in View > Panels.)
        # Always floating, not even with Allow docking on (a setting the stand-alone plugin may have left
        # behind): docking is only ever the user's drag. All this happens before the visibility watchers
        # below are connected, so the hide is not taken for a close.
        self.dock.hide()
        self.iface.mainWindow().addDockWidget(C.RightDockWidgetArea, self.dock)
        self.dock.make_floating()
        self.dock.closedStateChanged.connect(self._dock_closed)
        # a close that folds away (the shared bag animation) ends with a plain hide: watch that too
        self.dock.visibilityChanged.connect(self._visibility_changed)
        self._install_bubble()
        self.watcher.start()
        return self.dock

    def _new_dock(self):
        from .dock import MiniPanelDock
        return MiniPanelDock(self.iface, self.watcher, self.iface.mainWindow(), bag_provider=self.bag_provider)

    def _install_bubble(self):
        """Minimize-to-bubble, shared with the bags' bubbles. Never blocks the panel."""
        if self.group is None:
            return
        try:
            from ..panel_bubble import PanelBubbleManager, file_glyph
            self.bubble = PanelBubbleManager(
                self.dock, self.group, self.iface.mapCanvas(), self._restore, self.dock.BUBBLE_KEY,
                self.BUBBLE_TIP, file_glyph(self.GLYPH))
            self.bubble.toggle_action = self.action
            self.dock.bag_group = self.group
            self.dock.minimizeRequested.connect(self.bubble.minimize)
            self.dock.show_minimize(self.bubble.bubbles_enabled())
            self.group.settingsChanged.connect(self._bubble_setting)
            self.bubble.bubble.hideRequested.connect(self._bubble_dismissed)
        except Exception:
            from qgis.core import Qgis, QgsMessageLog
            import traceback
            QgsMessageLog.logMessage(self.NAME + ": no bubble: " + traceback.format_exc(), "KGA Tools",
                                     Qgis.MessageLevel.Warning)
            self.bubble = None

    def _bubble_setting(self, key):
        if self.bubble is not None and self.dock is not None and key in ("bubble", "disabled"):
            with suppress(RuntimeError):
                self.dock.show_minimize(self.bubble.bubbles_enabled())

    def _bubble_dismissed(self):
        """The bubble's own Hide: the panel is closed, so the button is released."""
        if self.action is not None and self.action.isChecked():
            self.action.blockSignals(True)
            self.action.setChecked(False)
            self.action.blockSignals(False)
        QgsSettings().setValue(self.VISIBLE_KEY, False)

    def _restore(self):
        if self.dock is not None:
            self.dock.show()
            self.dock.raise_()

    def _minimized(self):
        b = self.bubble
        with suppress(RuntimeError, AttributeError):
            return b is not None and b.bubble is not None and b.bubble.isVisible()
        return False

    def _toggle(self, on):
        if not on and self._minimized():
            # like the bags: the button brings a minimized panel back out of its bubble
            self.action.blockSignals(True)
            self.action.setChecked(True)
            self.action.blockSignals(False)
            self.bubble.restore()
            return
        if on:
            self._ensure_dock().show()
            self.dock.raise_()
        elif self.dock is not None:
            self.dock.hide()
        QgsSettings().setValue(self.VISIBLE_KEY, bool(on))

    def _visibility_changed(self, visible):
        if not visible:
            QTimer.singleShot(0, self._check_closed)

    def _check_closed(self):
        dock = self.dock
        if dock is None or self.action is None or not self.action.isChecked():
            return
        with suppress(RuntimeError):
            minimizing = self.bubble is not None and getattr(self.bubble, "_minimizing", False)
            # isHidden(): really hidden, not just tabbed behind another dock
            if dock.isHidden() and not minimizing and not self._minimized():
                self._dock_closed(True)

    def _dock_closed(self, closed):
        """The dock's x was used (a dock merely tabbed behind another stays 'open')."""
        if self.action is not None and self.action.isChecked() == closed:
            self.action.blockSignals(True)
            self.action.setChecked(not closed)
            self.action.blockSignals(False)
            QgsSettings().setValue(self.VISIBLE_KEY, not closed)

    def unload(self):
        if self.group is not None:
            with suppress(TypeError, RuntimeError):
                self.group.settingsChanged.disconnect(self._bubble_setting)
        if self.bubble is not None:
            with suppress(Exception):
                self.bubble.unload()
            self.bubble = None
        if self.dock is not None:
            with suppress(RuntimeError):
                self.dock.save_geometry()
                self.dock.cleanup()
                self.iface.mainWindow().removeDockWidget(self.dock)
                self.dock.setParent(None)
                self.dock.deleteLater()
            self.dock = None
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher.deleteLater()
            self.watcher = None
        if self.action is not None:
            with suppress(RuntimeError, TypeError):
                self.action.toggled.disconnect(self._toggle)
            self.action.deleteLater()
            self.action = None

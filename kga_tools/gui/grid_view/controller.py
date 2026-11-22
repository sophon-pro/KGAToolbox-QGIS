# -*- coding: utf-8 -*-
"""The grid: switch, settings and the buttons (status bar button and the map's right-click menu).

The status-bar button toggles the grid; its drop-down arrow opens the panel with Spacing, Rotation and Grid Settings
(which opens Settings > Editor). The state and the values are the ``KGA/settings/grid*`` settings, so they
survive a restart and Settings > Editor edits the same numbers.
"""

from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtWidgets import QMenu, QWidgetAction

from ...branding import icon
from ...core import kga_settings as ks
from .. import status_buttons
from . import engine
from .overlay import GridOverlay
from .popup import GridPanel

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

ICON = 'interface/grid.png'
TEXT_ON = 'Enable Grid'
TEXT_OFF = 'Disable Grid'
MENU_NAME = 'kgaGridMenuAction'
NODE_MENU_NAME = 'kgaNodeViewMenuAction'     # the Vertices and Nodes entry it follows
TOOLTIP = ('Show a grid of lines over the map, like ArcGIS Pro.\n'
           'Use the arrow in the status bar to set the spacing and rotation.')


class GridController(QObject):

    def __init__(self, iface, open_settings=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self._open_settings = open_settings
        self.action = None
        self.menu = None
        self.panel = None
        self.overlay = None
        self.status_button = None
        self._installed = False

    # ------------------------------------------------------------------ lifecycle
    def install(self):
        if self._installed:
            return
        self._installed = True
        self.action = QAction(icon(ICON), 'Grid', self)
        self.action.setCheckable(True)
        self.action.setObjectName('mActionKgaGrid')
        self.action.setToolTip(TOOLTIP)
        self.action.setStatusTip('Show or hide the map grid; the arrow sets spacing and rotation')
        self.action.triggered.connect(self._action_triggered)
        self.menu = QMenu()
        self.panel = GridPanel(self)
        holder = QWidgetAction(self.menu)
        holder.setDefaultWidget(self.panel)
        self.menu.addAction(holder)
        self.menu.aboutToShow.connect(self.panel.load)
        self.action.setMenu(self.menu)
        self.canvas.extentsChanged.connect(self._view_changed)
        self.canvas.contextMenuAboutToShow.connect(self._canvas_menu)
        self.status_button = status_buttons.add_button(self.iface, self.action, 'kgaGridStatusButton', self.menu)
        with suppress(Exception):
            self.iface.initializationCompleted.connect(self._late_status_button)
        self.refresh()

    def _late_status_button(self, *_args):
        if self.status_button is None and self.action is not None:
            self.status_button = status_buttons.add_button(self.iface, self.action, 'kgaGridStatusButton',
                                                           self.menu)

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        for signal, slot in ((self.canvas.extentsChanged, self._view_changed),
                             (self.canvas.contextMenuAboutToShow, self._canvas_menu),
                             (self.iface.initializationCompleted, self._late_status_button)):
            with suppress(Exception):
                signal.disconnect(slot)
        self._remove_overlay()
        button, self.status_button = self.status_button, None
        status_buttons.remove_button(self.iface, button)
        if self.action is not None:
            with suppress(RuntimeError):
                self.action.setMenu(None)
                self.action.deleteLater()
        self.action = None
        if self.menu is not None:
            with suppress(RuntimeError):
                self.menu.deleteLater()
        self.menu = self.panel = None

    # ------------------------------------------------------------------ the switch
    @property
    def enabled(self):
        return ks.grid_enabled()

    def set_enabled(self, on):
        ks.set_value(ks.KEY_GRID, bool(on))
        self.refresh()

    def _action_triggered(self, checked=False):
        self.set_enabled(checked)

    def refresh(self, *_args):
        """Follow the settings: grid on / off, new spacing or rotation."""
        if not self._installed:
            return
        try:
            on = self.enabled
            if self.action is not None:
                self.action.blockSignals(True)
                self.action.setChecked(on)
                self.action.blockSignals(False)
            if self.panel is not None and self.panel.on.isChecked() != on:
                self.panel.load()
            if on:
                if self.overlay is None:
                    self.overlay = GridOverlay(self.canvas, self)
                self.overlay.update()
            else:
                self._remove_overlay()
            if self.panel is not None:
                self.panel.update_info()
        except RuntimeError:                # the plugin was unloaded meanwhile
            pass

    def _remove_overlay(self):
        if self.overlay is not None:
            with suppress(Exception):
                self.canvas.scene().removeItem(self.overlay)
            self.overlay = None

    def _view_changed(self, *_args):
        if self.overlay is not None and not sip.isdeleted(self.overlay):
            self.overlay.update()
            if self.menu is not None and self.menu.isVisible() and self.panel is not None:
                self.panel.update_info()

    # ------------------------------------------------------------------ right-click menu
    def _canvas_menu(self, menu, _event):
        """Enable / Disable Grid at the end of the map's right-click menu, under Vertices and Nodes."""
        with suppress(Exception):               # never break the map's menu over this
            on = self.enabled
            actions = menu.actions()
            if not actions or actions[-1].objectName() != NODE_MENU_NAME:
                menu.addSeparator()             # the node entry brings its own separator
            action = QAction(icon(ICON), TEXT_OFF if on else TEXT_ON, menu)
            action.setObjectName(MENU_NAME)
            action.setToolTip(TOOLTIP)
            action.triggered.connect(lambda _c=False: self.set_enabled(not on))
            menu.addAction(action)

    # ------------------------------------------------------------------ values
    @staticmethod
    def rotation():
        return ks.grid_rotation()

    def spacing_map_units(self):
        return engine.spacing_in_map_units(ks.grid_spacing(), ks.grid_unit(), self.canvas.mapSettings().destinationCrs())

    def scaled_text(self):
        """'100 m' while the grid is drawn at a bigger spacing than the one typed, else ''."""
        factor, _spacing = engine.effective_scale(self.spacing_map_units(), self.canvas.mapSettings().mapUnitsPerPixel())
        return engine.scaled_text(ks.grid_spacing(), ks.grid_unit(), factor) if factor > 1 else ''

    def open_settings(self):
        """Grid Settings...: Settings > Editor."""
        if self._open_settings is not None:
            QTimer.singleShot(0, self._open_settings)

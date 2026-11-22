# -*- coding: utf-8 -*-
"""Cursor Style Manager: choose, customise, preview and save the map-canvas cursor.

Ported from the stand-alone Cursor Style Manager 1.0.1 plugin (GPL-2.0-or-later,
see LICENSE). The engine (`controller.py`) replaces only the *base* cursor of
the active map tool through `QgsMapTool.setCursor()`, so QGIS's own short-lived
feedback (closed hand while panning, wheel zoom, busy cursor) keeps working and
every tool gets its own cursor back when the engine stops.

`CursorStyleManager` is what `KgaToolsPlugin` talks to: it starts the engine,
owns the two KGA Toolbox menu actions (no toolbar button) and builds the dock on
first use. A fresh profile starts on the passive "QGIS Default" preset, so
nothing changes until the user picks a style.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt

from ...branding import LOG_TAG, icon
from ...data_ingest.compat import QAction

TITLE = 'Cursor Style Manager'
ICON = 'navigation/cursorStyleManager.png'


class CursorStyleManager:

    def __init__(self, iface):
        self.iface = iface
        self.manager = None
        self.dock = None
        self.panel_action = None
        self.toggle_action = None

    # ------------------------------------------------------------- lifecycle
    def install(self):
        parent = self.iface.mainWindow()
        self.panel_action = QAction(icon(ICON), TITLE, parent)
        self.panel_action.setObjectName('kgaCursorStyleManagerAction')
        self.panel_action.setCheckable(True)
        self.panel_action.setToolTip(
            'Cursor Style Manager: choose, customise, preview and save the map '
            'cursor used while digitizing, editing, selecting and measuring')
        self.panel_action.toggled.connect(self._toggle_panel)

        self.toggle_action = QAction('Use Custom Map Cursor', parent)
        self.toggle_action.setObjectName('kgaCursorStyleToggleAction')
        self.toggle_action.setCheckable(True)
        self.toggle_action.setToolTip(
            'Use the custom map cursor. Off: every QGIS tool shows its own cursor again.')
        self.toggle_action.toggled.connect(self._toggle_enabled)
        # Lets users give it their own shortcut (Settings > Keyboard Shortcuts).
        with suppress(Exception):
            self.iface.registerMainWindowAction(self.toggle_action, '')

        try:
            from .manager import CursorManager
            self.manager = CursorManager(self.iface)
            self.manager.stateChanged.connect(self._sync_toggle)
            self.manager.start()
        except Exception as exc:  # never leave QGIS half-modified
            QgsMessageLog.logMessage(
                'Cursor Style Manager could not start: {!r}'.format(exc),
                LOG_TAG, Qgis.MessageLevel.Critical)
            if self.manager is not None:
                with suppress(Exception):
                    self.manager.stop()
            self.manager = None
            self.toggle_action.setEnabled(False)
            return
        self._sync_toggle()

    def actions(self):
        """The KGA Toolbox menu entries, in order."""
        return [a for a in (self.panel_action, self.toggle_action) if a is not None]

    def uninstall(self):
        if self.manager is not None:
            with suppress(TypeError, RuntimeError):
                self.manager.stateChanged.disconnect(self._sync_toggle)
            with suppress(Exception):
                self.manager.stop()  # every tool gets its own cursor back
        if self.dock is not None:
            with suppress(Exception):
                self.dock.cleanup()
                self.iface.removeDockWidget(self.dock)
            # Delete now, not deleteLater(): a quick plugin reload would otherwise
            # keep the old panel (and the manager it references) alive.
            with suppress(Exception):
                if not sip.isdeleted(self.dock):
                    sip.delete(self.dock)
            self.dock = None
        if self.toggle_action is not None:
            with suppress(Exception):
                self.iface.unregisterMainWindowAction(self.toggle_action)
        for action in (self.panel_action, self.toggle_action):
            if action is not None:
                with suppress(RuntimeError):
                    action.deleteLater()
        if self.manager is not None:
            with suppress(RuntimeError):
                self.manager.deleteLater()
        self.manager = self.panel_action = self.toggle_action = None

    # ----------------------------------------------------------------- slots
    def show_panel(self):
        if self.panel_action is not None:
            self.panel_action.setChecked(True)
        if self.dock is not None:
            self.dock.raise_()

    def _toggle_panel(self, show):
        if show and self.dock is None:
            if self.manager is None:
                self.iface.messageBar().pushWarning(
                    TITLE, 'The custom cursor could not be started; see the Log Messages panel.')
                self.panel_action.setChecked(False)
                return
            try:
                from .dock import CursorStyleDock
                self.dock = CursorStyleDock(self.manager, self.iface.mainWindow())
            except Exception as exc:
                QgsMessageLog.logMessage(
                    'Could not open Cursor Style Manager: {!r}'.format(exc),
                    LOG_TAG, Qgis.MessageLevel.Critical)
                self.iface.messageBar().pushCritical(
                    TITLE, 'Could not open the panel: {}'.format(exc))
                self.panel_action.setChecked(False)
                return
            self.dock.visibilityChanged.connect(self._dock_visibility)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        if self.dock is not None:
            self.dock.setUserVisible(show)

    def _dock_visibility(self, _visible):
        if self.dock is not None and self.panel_action is not None:
            self.panel_action.blockSignals(True)
            self.panel_action.setChecked(self.dock.isUserVisible())
            self.panel_action.blockSignals(False)

    def _toggle_enabled(self, on):
        if self.manager is not None:
            self.manager.set_enabled(on)

    def _sync_toggle(self):
        if self.toggle_action is not None and self.manager is not None:
            self.toggle_action.blockSignals(True)
            self.toggle_action.setChecked(self.manager.enabled)
            self.toggle_action.blockSignals(False)

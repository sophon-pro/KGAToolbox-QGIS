# -*- coding: utf-8 -*-
"""Owns the Panels and Toolbars Manager dock and its toolbar button.

`KgaToolsPlugin` creates one `UiManagerController`, puts `toggle_action` on the
toolbar right after the Data Explorer button, and calls `unload()` on the way
out.  The dock is built on first use, so QGIS starts as fast as before.
"""

import os
from contextlib import suppress

from qgis.PyQt.QtCore import QObject, Qt
from qgis.PyQt.QtGui import QIcon

from ...data_ingest.compat import QAction
from .. import panel_bubble

TITLE = 'Panels and Toolbars Manager'
SHORTCUT = 'Ctrl+Alt+P'
_ICON = os.path.join(os.path.dirname(__file__), 'icons', 'icon.svg')


class UiManagerController(QObject):

    def __init__(self, iface, parent=None, catalog_getter=None):
        super().__init__(parent)
        self.iface = iface
        self.catalog_getter = catalog_getter  # the KGA Catalog, to dock with
        self.dock = None
        self.toggle_action = None

    def initGui(self):  # noqa: N802 - QGIS naming
        self.toggle_action = QAction(QIcon(_ICON), TITLE,
                                     self.iface.mainWindow())
        self.toggle_action.setToolTip(
            'Panels and Toolbars Manager: show or hide any panel or toolbar visually, search '
            'by tool name, save layouts ({})'.format(SHORTCUT))
        self.toggle_action.setCheckable(True)
        self.toggle_action.triggered.connect(self.toggle_dock)

    def toggle_dock(self, checked=True):
        if self.dock is None:
            self._create_dock()
        self.dock.setVisible(bool(checked))
        if self.dock.isVisible():
            self.dock.raise_()
            self.dock.focus_search()

    def _create_dock(self):
        """Build and place the dock while it is hidden.

        addDockWidget() shows a new dock in its own spot before the tabify
        moves it next to the Catalog, which showed as a flashing window the
        first time it was opened in a session.  Everything is done hidden and
        with window updates off; the caller shows it once it is in place.
        """
        from .dock import UiManagerDock
        window = self.iface.mainWindow()
        window.setUpdatesEnabled(False)
        try:
            self.dock = UiManagerDock(self.iface, window)
            self.dock.hide()
            self.dock.visibilityChanged.connect(self.toggle_action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                     self.dock)
            self.dock.hide()
            self._dock_with_catalog()
            panel_bubble.attach_reveal(self.dock, self.toggle_action, 'ui_manager',
                                       'Panels and Toolbars Manager – click to open',
                                       panel_bubble.file_glyph('bubble/ui_manager.png'))
        finally:
            window.setUpdatesEnabled(True)

    def _dock_with_catalog(self):
        """Tab the manager with the Catalog panel when there is one."""
        catalog = self.catalog_getter() if self.catalog_getter else None
        panel = getattr(catalog, 'panel', None)
        if panel is None or panel.isFloating():
            return
        with suppress(Exception):
            self.iface.mainWindow().tabifyDockWidget(panel, self.dock)

    def unload(self):
        dock = self.dock
        if dock is not None:
            with suppress(Exception):
                dock.cleanup()
            with suppress(Exception):
                self.iface.removeDockWidget(dock)
            dock.deleteLater()
            self.dock = None
        self.toggle_action = None

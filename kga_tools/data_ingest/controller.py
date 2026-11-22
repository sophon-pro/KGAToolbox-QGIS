# -*- coding: utf-8 -*-
"""Owns the Data Explorer dock and its toolbar button.

`KgaToolsPlugin` creates one `DataIngestController`, puts `toggle_action` on the
toolbar next to the Catalog button, and calls `unload()` on the way out.  The
dock itself is built on first use, so QGIS starts as fast as before.
"""

import os
from contextlib import suppress

from qgis.PyQt.QtCore import QObject, Qt
from ..branding import icon as branding_icon

from .compat import QAction

TITLE = 'Data Explorer'
SHORTCUT = 'Ctrl+Alt+I'
_ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons')


class DataIngestController(QObject):

    def __init__(self, iface, parent=None, catalog_getter=None, group=None):
        super().__init__(parent)
        self.iface = iface
        self.group = group            # BubbleGroup shared with the bags (None: no bubble)
        self.bubble = None
        self.catalog_getter = catalog_getter  # the KGA Catalog, for Add to Project
        self.dock = None
        self.toggle_action = None

    def initGui(self):  # noqa: N802 - QGIS naming
        icon = branding_icon('data_management/dataIngest.png')
        self.toggle_action = QAction(icon, TITLE, self.iface.mainWindow())
        self.toggle_action.setToolTip(
            'Data Explorer: browse, search, preview and ingest geodata '
            '({})'.format(SHORTCUT))
        self.toggle_action.setCheckable(True)
        self.toggle_action.triggered.connect(self.toggle_dock)
        with suppress(Exception):
            self.iface.registerMainWindowAction(self.toggle_action, SHORTCUT)

    def toggle_dock(self, checked=True):
        if self.dock is None:
            from .media_browser import MediaBrowserDock
            self.dock = MediaBrowserDock(self.iface, self.iface.mainWindow())
            self.dock.catalog_getter = self.catalog_getter
            self.dock.visibilityChanged.connect(self.toggle_action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,
                                     self.dock)
            self.dock.hide()       # built hidden, so the first open from the toolbar animates too
            self._install_bubble()
        self.dock.setVisible(bool(checked) or not self.dock.isVisible())
        if self.dock.isVisible():
            self.dock.raise_()

    def _install_bubble(self):
        """Minimize-to-bubble, shared with the bags' bubbles. Never blocks the Data Explorer."""
        if self.group is None:
            return
        try:
            from ..gui.panel_bubble import PanelBubbleManager, file_glyph, install_title_bar
            self.bubble = PanelBubbleManager(
                self.dock, self.group, self.iface.mapCanvas(), self._restore,
                'explorer', 'Data Explorer – click to open',
                file_glyph('bubble/data_explorer.png'))
            self.dock.minimizeRequested.connect(self.bubble.minimize)
            install_title_bar(self.dock, self.bubble)       # Minimize beside Float and Close
            self.bubble.toggle_action = self.toggle_action
        except Exception:
            self.bubble = None

    def _restore(self):
        if self.dock is not None:
            self.dock.show()
            self.dock.raise_()

    def unload(self):
        if self.bubble is not None:
            with suppress(Exception):
                self.bubble.unload()
            self.bubble = None
        if self.toggle_action is not None:
            with suppress(Exception):
                self.iface.unregisterMainWindowAction(self.toggle_action)
        dock = self.dock
        if dock is not None:
            with suppress(RuntimeError):
                if dock._index_task is not None:
                    dock._index_task.cancel()
            for job, _layer in list(dock._jobs_alive):
                with suppress(RuntimeError):
                    job.cancel()  # blocking: safe before the dock is deleted
            for win in list(dock._mini_windows):
                win.close()
            dock._retire(dock._preview_layer)
            dock._preview_layer = None
            with suppress(Exception):
                self.iface.removeDockWidget(dock)
            dock.deleteLater()
            self.dock = None
            from . import tasks
            tasks.DISPOSER.shutdown()
        self.toggle_action = None

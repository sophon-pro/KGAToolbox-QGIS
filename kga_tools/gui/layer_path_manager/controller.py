# -*- coding: utf-8 -*-
"""Owns the Layer Path Manager window and its toolbar button.

`KgaToolsPlugin` puts `action` on the toolbar right after the Panels and Toolbars
Manager button and calls `unload()` on the way out. The window is built on first
use, so QGIS starts as fast as before.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog

from ...branding import icon
from ...data_ingest.compat import QAction

TITLE = 'Layer Path Manager'
_ICON = 'data_management/layerPathManager.png'


class LayerPathManagerController:

    def __init__(self, iface, catalog_getter=None):
        self.iface = iface
        self.catalog_getter = catalog_getter
        self.action = None
        self.dialog = None
        self.source_list = None

    def initGui(self):  # noqa: N802 - QGIS naming
        self.action = QAction(icon(_ICON), TITLE, self.iface.mainWindow())
        self.action.setObjectName('kgaLayerPathManagerAction')
        self.action.setToolTip(
            'Layer Path Manager: inspect, repair, relocate and standardize the '
            'data source paths of the project layers')
        self.action.triggered.connect(lambda *_: self.run())
        try:  # the Layers panel buttons: List by Data Source, Show Full Path
            from .source_list import SourceListController
            self.source_list = SourceListController(self.iface, self.catalog_getter)
            self.source_list.initGui()
        except Exception as exc:
            QgsMessageLog.logMessage('Could not add List by Data Source: {}'.format(exc),
                                     'KGA Toolbox', Qgis.MessageLevel.Warning)
            self.source_list = None

    def run(self):
        """Show (or raise) the non-modal main window."""
        try:
            if self.dialog is None:
                from .main_dialog import LayerPathManagerDialog
                self.dialog = LayerPathManagerDialog(
                    self.iface, self.iface.mainWindow())
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open Layer Path Manager: {}'.format(exc),
                'KGA Toolbox', Qgis.MessageLevel.Critical)
            self.iface.messageBar().pushCritical(
                TITLE, 'Could not open the window: {}'.format(exc))

    def unload(self):
        if self.source_list is not None:
            with suppress(Exception):
                self.source_list.unload()
            self.source_list = None
        if self.dialog is not None:
            with suppress(RuntimeError, Exception):
                self.dialog.cleanup()
                self.dialog.close()
                self.dialog.deleteLater()
            self.dialog = None
        if self.action is not None:
            with suppress(RuntimeError):
                self.action.deleteLater()
            self.action = None

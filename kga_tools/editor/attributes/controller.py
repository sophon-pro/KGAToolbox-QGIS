"""
Open Feature Attribute: the toolbar button, the Attributes pane and its select tool.

The pane docks on the right and joins the Processing Toolbox's tab group, like KGA Geoprocessing.
It works with Edit Session On or Off. A value confirmed while the session is Off first turns Edit Session
On (the editor controller's before_edit hook), so the edit is an ordinary unsaved edit.
"""

import os
from contextlib import suppress

from qgis.core import QgsFeatureRequest
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import QDockWidget, QMenu

from .layer_browser import feature_label
from .panel import TITLE, AttributesPanel
from .select_tool import AttributesSelectTool

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
MENU_MAX_FEATURES = 12          # overlapping features offered in the right-click submenu


class AttributesController:

    def __init__(self, iface, manager, before_edit=None):
        self.iface = iface
        self.manager = manager
        self.before_edit = before_edit     # callable(layer): runs before the pane writes an edit
        self.panel = None
        self.tool = None
        self.action = None
        self.toolbar = None
        self.menu = None

    def build(self, toolbar, before=None):
        win = self.iface.mainWindow()
        self.panel = AttributesPanel(self.iface, win)
        if self.before_edit is not None:
            self.panel.set_before_edit(self.before_edit)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel.hide()
        # Same reasoning as the KGA Geoprocessing pane: the Processing plugin may not have built its
        # dock yet, and QGIS restores the saved window layout after plugins load, so tabify later.
        QTimer.singleShot(0, self.tabify_with_toolbox)

        # toggleViewAction is a checkable action wired to the dock, correct even when the pane is
        # tabbed behind another one; raise it to the front tab as well when it is switched on.
        self.action = self.panel.toggleViewAction()
        self.action.setObjectName("mActionKgaEditorAttributes")
        self.action.setIcon(QIcon(os.path.join(ICON_DIR, "attributes.svg")))
        self.action.setText("Open Feature Attribute")
        self.action.setToolTip("Open Feature Attribute - select a feature on the map and type into its "
                               "attributes, no attribute table needed")
        self.action.triggered.connect(self._toggled)

        self.tool = AttributesSelectTool(self.iface.mapCanvas(), self.manager, self.iface)
        self.tool.setAction(self.panel.select_action)       # QGIS unchecks the banner when another tool is chosen
        self.panel.select_action.triggered.connect(self._select_toggled)
        self.panel.shapeChanged.connect(self._shape_changed)

        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)

        self.toolbar = toolbar
        if before is not None:
            toolbar.insertAction(before, self.action)
        else:
            toolbar.addAction(self.action)

    def tabify_with_toolbox(self):
        """Put the pane in the Processing Toolbox's tab group (skipped if either was pulled out)."""
        if self.panel is None:
            return
        window = self.iface.mainWindow()
        toolbox = window.findChild(QDockWidget, "ProcessingToolbox")
        if toolbox is None or toolbox.isFloating() or self.panel.isFloating():
            return
        window.tabifyDockWidget(toolbox, self.panel)

    def remove(self):
        canvas = self.iface.mapCanvas()
        with suppress(TypeError, RuntimeError):
            canvas.contextMenuAboutToShow.disconnect(self._canvas_menu)
        if self.tool is not None:
            if canvas.mapTool() is self.tool:
                canvas.unsetMapTool(self.tool)
                self.iface.actionPan().trigger()
            self.tool.dispose()
            self.tool.deleteLater()
            self.tool = None
        if self.panel is not None:
            self.panel.disconnect_all()
            self.iface.removeDockWidget(self.panel)
            if self.toolbar is not None and self.action is not None:
                self.toolbar.removeAction(self.action)
            self.panel.deleteLater()
            self.panel = None
        self.action = None

    # ------------------------------------------------------------------ actions
    def _toggled(self, checked):
        """Toolbar button: opening the pane also arms the selection tool, closing it puts the tool away."""
        if self.panel is None:
            return
        if checked:
            self.panel.raise_()
            if not self.panel.select_action.isChecked():
                self.panel.select_action.trigger()
        else:
            self._release_tool()

    def _select_toggled(self, checked):
        canvas = self.iface.mapCanvas()
        if checked:
            canvas.setMapTool(self.tool)
        elif canvas.mapTool() is self.tool:
            self.iface.actionPan().trigger()

    def _release_tool(self):
        canvas = self.iface.mapCanvas()
        if self.tool is not None and canvas.mapTool() is self.tool:
            canvas.unsetMapTool(self.tool)
            self.iface.actionPan().trigger()

    def _shape_changed(self, mode):
        self.tool.set_mode(mode)
        if not self.panel.select_action.isChecked():
            self.panel.select_action.trigger()
        self.iface.mainWindow().statusBar().showMessage(
            "{}: {} selection.".format(TITLE, mode.capitalize()), 4000)

    # ------------------------------------------------------------------ canvas right-click menu
    def _canvas_menu(self, menu, event):
        """Right-click on the map (QGIS's own menu: Copy Coordinate, Select Feature ...): add Attribute on top."""
        if self.panel is None or self.tool is None:
            return
        try:
            hits = self.tool.hits_at(event.mapPoint())
        except Exception:
            return
        hits = [(lyr, fid) for lyr, fid in hits if not sip.isdeleted(lyr)][:MENU_MAX_FEATURES]
        if not hits:
            return
        first = menu.actions()[0] if menu.actions() else None
        icon = self.action.icon()
        if len(hits) == 1:
            layer, fid = hits[0]
            action = QAction(icon, "Attribute", menu)
            action.triggered.connect(lambda _c=False: self.panel.show_feature(layer, fid))
            menu.insertAction(first, action)
            return
        sub = QMenu("Attribute", menu)
        sub.setIcon(icon)
        for layer, fid in hits:
            feature = next(layer.getFeatures(QgsFeatureRequest(fid)), None)
            text = "{}: {}".format(layer.name(), feature_label(layer, feature) if feature is not None else fid)
            sub.addAction(text).triggered.connect(lambda _c=False, lyr=layer, f=fid: self.panel.show_feature(lyr, f))
        menu.insertMenu(first, sub)

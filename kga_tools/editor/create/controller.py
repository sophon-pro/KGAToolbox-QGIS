"""
Create Features: the toolbar button, the pane and the map tool behind it.

The button takes the place Construction Sketch had on the KGA Editor toolbar. It shows or hides the
pane; picking a template (and a tool) in the pane arms the Construction Sketch map tool on that
template's layer. The pane docks on the right and joins the Processing Toolbox's tab group, like the
Catalog and Attributes panes.
"""

import os
from contextlib import suppress

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDockWidget

from ..sketch.construction_tool import ConstructionSketchTool
from .panel import TITLE, CreateFeaturesPanel
from .templates import effective_attributes

ICON = os.path.join(os.path.dirname(__file__), "..", "..", "icons", "createFeature.png")


class CreateFeaturesController:

    def __init__(self, iface):
        self.iface = iface
        self.panel = None
        self.tool = None
        self.action = None
        self.toolbar = None

    def build(self, toolbar, before=None):
        win = self.iface.mainWindow()
        self.panel = CreateFeaturesPanel(self.iface, win)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel.hide()
        # QGIS restores the saved window layout after plugins load, which would undo a tabify done now.
        QTimer.singleShot(0, self.tabify_with_toolbox)

        self.action = self.panel.toggleViewAction()
        self.action.setObjectName("mActionKgaCreateFeatures")
        self.action.setIcon(QIcon(os.path.normpath(ICON)))
        self.action.setText(TITLE)
        self.action.setToolTip("Create Features (needs Edit Session: On) - pick a template, then draw it with a "
                               "construction tool (point, line, polygon, rectangle, circle ...)")
        self.action.triggered.connect(self._toggled)

        self.tool = ConstructionSketchTool(self.iface.mapCanvas())
        self.tool.featureAdded.connect(self._feature_added)
        self.tool.activated.connect(lambda: self._tool_state(True))
        self.tool.deactivated.connect(lambda: self._tool_state(False))
        self.panel.templateActivated.connect(self._activate)
        self.panel.templateReleased.connect(self._release_tool)
        self.panel.closed.connect(self._pane_closed)

        self.toolbar = toolbar
        if before is not None:
            toolbar.insertAction(before, self.action)
        else:
            toolbar.addAction(self.action)

    def set_enabled(self, on):
        """The button follows the Edit Session switch; switching it Off closes the pane and puts the tool away."""
        if self.action is None:
            return
        self.action.setEnabled(on)
        if not on and self.panel is not None and not self.panel.isHidden():
            self.panel.close()

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

    # ------------------------------------------------------------------ pane -> tool
    def _toggled(self, checked):
        if checked and self.panel is not None:
            self.panel.raise_()

    def _activate(self, layer, template, tool_id):
        if self.tool is None:
            return
        attrs = effective_attributes(layer, template)
        self.tool.set_template(layer, attrs, tool_id, template.title)
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is not self.tool:
            canvas.setMapTool(self.tool)

    def _release_tool(self):
        canvas = self.iface.mapCanvas()
        if self.tool is None:
            return
        self.tool.clear_template()
        if canvas.mapTool() is self.tool:
            canvas.unsetMapTool(self.tool)
            self.iface.actionPan().trigger()

    def _pane_closed(self):
        """Closing the pane puts the tool away, as in ArcGIS Pro (switching to another tab does not)."""
        canvas = self.iface.mapCanvas()
        if self.tool is not None and canvas.mapTool() is self.tool:
            canvas.unsetMapTool(self.tool)
            with suppress(Exception):
                self.iface.actionPan().trigger()

    # ------------------------------------------------------------------ tool -> pane
    def _tool_state(self, active):
        if self.panel is not None:
            self.panel.set_active_tool(active)

    def _feature_added(self, layer, _feature):
        if self.panel is not None:
            self.panel.show_completed(layer.name())

# -*- coding: utf-8 -*-
"""KGA Layer Symbol as a docked panel, opened from a button on the Layers panel's
toolbar right beside "Open Layer Styling Panel" and tabbed with that panel."""
import os

from qgis.PyQt.QtCore import Qt
from ...branding import themed_icon
from qgis.PyQt.QtWidgets import QDockWidget, QToolBar
try:  # Qt6 / QGIS 4: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction
from qgis.gui import QgsDockWidget

from ...gui.panel_bubble import ToolbarReveal, attach_reveal, file_glyph
from .gallery_panel import GalleryPanel

ICON = os.path.join(os.path.dirname(__file__), "..", "..", "icons", "mapping/kgaLayerSymbol.png")
TITLE = "KGA Layer Symbol"


class SymbolDock(ToolbarReveal, QgsDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__(TITLE, parent)
        self.setObjectName("KGALayerSymbolDock")
        self.panel = GalleryPanel(iface, self)
        self.setWidget(self.panel)

    def cleanup(self):
        self.panel.cleanup()


class SymbolDockController:
    """Owns the Layers-panel button and the dock behind it."""

    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self.toolbar = None

    # ------------------------------------------------------------- install
    def install(self):
        self.action = QAction(themed_icon(ICON), "Open KGA Layer Symbol Panel",
                              self.iface.mainWindow())
        self.action.setObjectName("KGALayerSymbolDockAction")
        self.action.setToolTip("Open KGA Layer Symbol Panel")
        self.action.setCheckable(True)
        self.action.toggled.connect(self._toggle)
        self._place_button()

    def _styling_dock(self):
        for d in self.iface.mainWindow().findChildren(QDockWidget):
            if d is not self.dock and d.windowTitle() == "Layer Styling":
                return d
        return None

    def _place_button(self):
        """Right after the Layers panel's styling button; failing that, at the start
        of the Layers panel toolbar."""
        win = self.iface.mainWindow()
        layers = next((d for d in win.findChildren(QDockWidget)
                       if d.objectName() == "Layers" or d.windowTitle() == "Layers"), None)
        bars = layers.findChildren(QToolBar) if layers is not None else []
        for bar in bars:
            acts = bar.actions()
            for i, a in enumerate(acts):
                text = (a.text() or "") + " " + (a.toolTip() or "")
                if a.objectName() == "mActionStyleDock" or "Layer Styling" in text:
                    nxt = acts[i + 1] if i + 1 < len(acts) else None
                    bar.insertAction(nxt, self.action)
                    self.toolbar = bar
                    return
        if bars:
            acts = bars[0].actions()
            bars[0].insertAction(acts[1] if len(acts) > 1 else None, self.action)
            self.toolbar = bars[0]
        else:  # no Layers toolbar found: keep it reachable from the main toolbar area
            self.iface.addToolBarIcon(self.action)

    # ---------------------------------------------------------------- dock
    def _create_dock(self):
        self.dock = SymbolDock(self.iface, self.iface.mainWindow())
        self.dock.visibilityChanged.connect(self._sync)
        win = self.iface.mainWindow()
        win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        styling = self._styling_dock()
        if styling is not None:
            win.tabifyDockWidget(styling, self.dock)
        self.dock.hide()               # placed hidden, so the first open animates too
        attach_reveal(self.dock, self.action, 'layer_symbol', 'KGA Layer Symbol – click to open',
                      file_glyph('bubble/layer_symbol.png'))

    def _toggle(self, on):
        if on and self.dock is None:
            self._create_dock()
        if self.dock is not None:
            self.dock.setVisible(on)
            if on:
                self.dock.raise_()

    def _sync(self, visible):
        if self.action is not None and self.action.isChecked() != visible:
            self.action.blockSignals(True)
            self.action.setChecked(visible)
            self.action.blockSignals(False)

    # ----------------------------------------------------------- uninstall
    def uninstall(self):
        if self.dock is not None:
            try:
                self.dock.cleanup()
                self.iface.mainWindow().removeDockWidget(self.dock)
                self.dock.deleteLater()
            except RuntimeError:
                pass
            self.dock = None
        if self.action is not None:
            try:
                if self.toolbar is not None:
                    self.toolbar.removeAction(self.action)
                else:
                    self.iface.removeToolBarIcon(self.action)
                self.action.deleteLater()
            except RuntimeError:
                pass
            self.action = None

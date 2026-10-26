# -*- coding: utf-8 -*-
"""The KGA Layer Symbol tab of Layer Properties, listed right under Symbology."""
from contextlib import suppress
import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QVBoxLayout
from qgis.core import QgsRasterLayer, QgsVectorLayer
from qgis.gui import (
    QgsMapLayerConfigWidget, QgsMapLayerConfigWidgetFactory, QgsRendererPropertiesDialog,
)

from .gallery_panel import GalleryPanel
from .styling_dock_order import StylingDockOrder

TITLE = "KGA Layer Symbol"
ICON = os.path.join(os.path.dirname(__file__), "..", "..", "icons", "kgaLayerSymbol.png")


class LayerSymbolConfigWidget(QgsMapLayerConfigWidget):
    """The gallery pinned to the layer whose properties are open.

    The Apply / OK buttons of Layer Properties apply the picked symbol (this page
    is applied after Symbology, so it wins); the Symbology page is re-read
    afterwards so a later OK there does not put the old style back."""

    def __init__(self, iface, layer, canvas, parent=None, live=False):
        super().__init__(layer, canvas, parent)
        self._layer = layer
        self.setPanelTitle(TITLE)
        self.setObjectName("KGALayerSymbolTab")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.panel = GalleryPanel(iface, self, layer=layer, live=live)
        lay.addWidget(self.panel)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(150)
        self._sync_timer.timeout.connect(self._sync_symbology)
        self._connect(True)

    def _connect(self, on):
        sig = getattr(self._layer, "rendererChanged", None)
        if sig is None:
            return
        try:
            (sig.connect if on else sig.disconnect)(self._sync_timer.start)
        except (TypeError, RuntimeError):
            pass

    def _sync_symbology(self):
        win = self.window()
        if win is None or win is self:
            return
        pages = win.findChildren(QgsRendererPropertiesDialog)
        with suppress(Exception):  # pragma: no cover - never break the properties dialog
            if pages:
                for page in pages:
                    page.syncToLayer()
            elif hasattr(win, "syncToLayer"):
                win.syncToLayer()

    def apply(self):
        """Layer Properties' Apply / OK: style the layer with the symbol picked here."""
        self.panel.apply_picked()

    def cleanup(self):
        self._connect(False)
        self.panel.cleanup()


class LayerSymbolTabFactory(QgsMapLayerConfigWidgetFactory):
    #: the page to sit in front of, so the tab lands right under Symbology
    after_symbology = "mOptsPage_Labels"

    def __init__(self, iface, order=None):
        super().__init__(TITLE, QIcon(ICON))
        self.iface = iface
        # keeps our button right after Symbology in the Layer Styling panel
        self.order = order if order is not None else StylingDockOrder(iface, TITLE)
        self.order.attach()
        try:
            iface.initializationCompleted.connect(self.order.attach)
        except (AttributeError, TypeError):
            pass

    def shutdown(self):
        try:
            self.iface.initializationCompleted.disconnect(self.order.attach)
        except (AttributeError, TypeError, RuntimeError):
            pass
        self.order.detach()

    def supportsLayer(self, layer):
        # QGIS asks this each time it builds the Layer Styling panel's button list
        QTimer.singleShot(0, self.order.attach)
        return isinstance(layer, QgsVectorLayer) and layer.isSpatial()

    def supportLayerPropertiesDialog(self):
        return True

    def supportsStyleDock(self):
        return True

    def title(self):
        return TITLE

    def icon(self):
        return QIcon(ICON)

    def layerPropertiesPagePositionHint(self):
        return self.after_symbology

    def createWidget(self, layer, canvas, dockWidget=True, parent=None):
        return LayerSymbolConfigWidget(self.iface, layer, canvas, parent, live=bool(dockWidget))


class RasterLayerSymbolTabFactory(LayerSymbolTabFactory):
    after_symbology = "mOptsPage_Transparency"

    def supportsLayer(self, layer):
        QTimer.singleShot(0, self.order.attach)
        return isinstance(layer, QgsRasterLayer) and layer.isValid()

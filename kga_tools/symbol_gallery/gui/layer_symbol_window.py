# -*- coding: utf-8 -*-
"""The stand-alone KGA Layer Symbol window: the gallery, following the Layers panel."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout
from qgis.core import QgsSettings

from .gallery_panel import GalleryPanel, SETTINGS


class LayerSymbolWindow(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.setWindowTitle("KGA Layer Symbol")
        self.setObjectName("KGALayerSymbolWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.iface = iface
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.panel = GalleryPanel(iface, self)
        lay.addWidget(self.panel)
        geo = QgsSettings().value(SETTINGS + "window_geometry")
        if geo is None or not self.restoreGeometry(geo):
            self.resize(460, 760)

    def done(self, result):
        self._shutdown()
        super().done(result)

    def closeEvent(self, event):
        self._shutdown()
        super().closeEvent(event)

    def _shutdown(self):
        if getattr(self, "_down", False):
            return
        self._down = True
        QgsSettings().setValue(SETTINGS + "window_geometry", self.saveGeometry())
        self.panel.cleanup()

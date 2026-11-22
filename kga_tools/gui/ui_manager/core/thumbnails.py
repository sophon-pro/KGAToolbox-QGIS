# -*- coding: utf-8 -*-
"""Snapshot cache for panels and widget-based toolbars.

A widget can only be photographed while it is on screen, so every time a panel or toolbar
becomes visible we grab a picture of it and keep it on disk. The manager can then show what
it looks like even after it has been closed.
"""
import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.core import QgsApplication

from .registry import TOOLBAR

PANEL_MAX_WIDTH = 520   # stored resolution for panels (sharp on HiDPI screens)
PANEL_ASPECT = 0.62     # keep the top part of tall panels
TOOLBAR_MAX_HEIGHT = 64


class ThumbnailCache:
    def __init__(self):
        self.folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "kga_tools", "ui_manager", "thumbnails")
        os.makedirs(self.folder, exist_ok=True)
        self._memory = {}

    def path(self, key):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
        return os.path.join(self.folder, safe + ".png")

    def get(self, key):
        if key not in self._memory:
            path = self.path(key)
            if os.path.exists(path):
                pix = QPixmap(path)
                if not pix.isNull():
                    self._memory[key] = pix
        return self._memory.get(key)

    def capture(self, item):
        """Grab the widget if it is on screen. Returns the pixmap or ``None``."""
        widget = item.widget
        if not widget.isVisible() or widget.width() < 16 or widget.height() < 16:
            return None
        pix = widget.grab()
        if pix.isNull():
            return None
        pix.setDevicePixelRatio(1.0)
        if item.kind == TOOLBAR:
            if pix.height() > TOOLBAR_MAX_HEIGHT:
                pix = pix.scaledToHeight(TOOLBAR_MAX_HEIGHT, Qt.TransformationMode.SmoothTransformation)
        else:
            width = min(pix.width(), PANEL_MAX_WIDTH)
            pix = pix.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
            pix = pix.copy(0, 0, pix.width(), min(pix.height(), int(width * PANEL_ASPECT)))
        pix.save(self.path(item.key), "PNG")
        self._memory[item.key] = pix
        return pix

    def clear(self):
        self._memory.clear()
        for name in os.listdir(self.folder):
            if name.endswith(".png"):
                try:
                    os.remove(os.path.join(self.folder, name))
                except OSError:
                    pass

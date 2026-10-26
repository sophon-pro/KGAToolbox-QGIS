# -*- coding: utf-8 -*-
"""Duplicate to New Layer on the Layers panel's right-click menu.

`iface.addCustomActionForLayerType` can only append at the end of the menu, and
the request is for the entry to sit directly below QGIS's own Duplicate Layer.
QGIS builds that menu fresh for every right-click, so this watches menus being
polished (just before they first show) and, in one that holds Duplicate Layer,
inserts the KGA action after it.
"""

from contextlib import suppress

from qgis.core import QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QEvent, QObject
from qgis.PyQt.QtWidgets import QApplication, QMenu, QMenuBar

from ..branding import icon as _icon

TEXT = 'Duplicate to New Layer…'


def _plain(text):
    return text.replace('&', '').replace('…', '').replace('...', '').strip().lower()


class LayerMenuInjector(QObject):

    def __init__(self, iface, callback, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.action = None
        self._callback = callback
        self._native = None

    def install(self):
        from qgis.PyQt.QtWidgets import QAction
        self.action = QAction(_icon('duplicateLayer.png'), TEXT,
                              self.iface.mainWindow())
        self.action.triggered.connect(self._callback)
        try:
            self._native = self.iface.actionDuplicateLayer()
        except Exception:
            self._native = None
        QApplication.instance().installEventFilter(self)

    def uninstall(self):
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self.action is not None:
            self.action.deleteLater()
            self.action = None

    def _is_duplicate(self, action):
        if action is self._native and self._native is not None:
            return True
        return _plain(action.text()) in ('duplicate layer',
                                         _plain(self._native.text())
                                         if self._native is not None else '')

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Polish and isinstance(obj, QMenu):
            with suppress(Exception):       # never break a menu over this
                self._inject(obj)
        return False

    def _inject(self, menu):
        if self.action is None or isinstance(menu.parent(), QMenuBar):
            return
        layer = self.iface.activeLayer()
        if not isinstance(layer, (QgsVectorLayer, QgsRasterLayer)):
            return
        actions = menu.actions()
        if self.action in actions:
            return
        for index, action in enumerate(actions):
            if not action.isSeparator() and self._is_duplicate(action):
                before = actions[index + 1] if index + 1 < len(actions) else None
                if before is None:
                    menu.addAction(self.action)
                else:
                    menu.insertAction(before, self.action)
                return

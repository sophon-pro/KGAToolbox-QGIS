# -*- coding: utf-8 -*-
"""Duplicate to New Layer and Attribute Table Bubble on the Layers panel's right-click menu.

`iface.addCustomActionForLayerType` can only append at the end of the menu, and
the request is for the entry to sit directly below QGIS's own Duplicate Layer.
QGIS builds that menu fresh for every right-click, so this watches menus being
polished (just before they first show) and, in one that holds Duplicate Layer,
inserts the KGA action after it. The same goes for Attribute Table Bubble, which sits directly below
Open Attribute Table of a vector layer.
"""

from contextlib import suppress

from qgis.core import QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QEvent, QObject
from qgis.PyQt.QtWidgets import QMenu, QMenuBar

from ..branding import icon as _icon
from . import app_events

TEXT = 'Duplicate to New Layer…'
BUBBLE_TEXT = 'Attribute Table Bubble'


def _plain(text):
    return text.replace('&', '').replace('…', '').replace('...', '').strip().lower()


class LayerMenuInjector(QObject):

    def __init__(self, iface, callback, parent=None, bubble_callback=None):
        super().__init__(parent)
        self.iface = iface
        self.action = None
        self.bubble_action = None
        self._bubble_callback = bubble_callback      # callable(layer, screen point), or None
        self._callback = callback
        self._native = None

    def install(self):
        from qgis.PyQt.QtWidgets import QAction
        self.action = QAction(_icon('data_management/duplicateLayer.png'), TEXT,
                              self.iface.mainWindow())
        self.action.triggered.connect(self._callback)
        if self._bubble_callback is not None:
            from qgis.core import QgsApplication
            self.bubble_action = QAction(QgsApplication.getThemeIcon('/mActionOpenTable.svg'), BUBBLE_TEXT,
                                         self.iface.mainWindow())
            self.bubble_action.setToolTip('Open the attribute table as a bubble on the map')
            self.bubble_action.triggered.connect(self._bubble_triggered)
        try:
            self._native = self.iface.actionDuplicateLayer()
        except Exception:
            self._native = None
        # through the plugin's one application-wide filter, and only when a widget is polished
        app_events.add((QEvent.Type.Polish,), self.eventFilter)

    def uninstall(self):
        app_events.remove(self.eventFilter)
        for name in ('action', 'bubble_action'):
            action = getattr(self, name)
            if action is not None:
                action.deleteLater()
                setattr(self, name, None)

    def _bubble_triggered(self, _checked=False):
        from qgis.PyQt.QtGui import QCursor
        layer = self.iface.activeLayer()
        if isinstance(layer, QgsVectorLayer) and self._bubble_callback is not None:
            self._bubble_callback(layer, QCursor.pos())     # the menu entry the user just clicked

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

    def _inject_bubble(self, menu, actions, layer):
        if self.bubble_action is None or self.bubble_action in actions or not isinstance(layer, QgsVectorLayer):
            return
        for index, action in enumerate(actions):
            if not action.isSeparator() and _plain(action.text()) == 'open attribute table':
                before = actions[index + 1] if index + 1 < len(actions) else None
                if before is None:
                    menu.addAction(self.bubble_action)
                else:
                    menu.insertAction(before, self.bubble_action)
                return

    def _inject(self, menu):
        if self.action is None or isinstance(menu.parent(), QMenuBar):
            return
        layer = self.iface.activeLayer()
        if not isinstance(layer, (QgsVectorLayer, QgsRasterLayer)):
            return
        actions = menu.actions()
        self._inject_bubble(menu, actions, layer)
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

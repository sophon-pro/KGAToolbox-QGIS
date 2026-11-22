# -*- coding: utf-8 -*-
"""Copy Coordinate to Excel on the map's and the Layers panel's right-click menus.

Map right-click: for every layer that has selected features (select first, then
right-click anywhere on the map). Layers panel right-click: the selected features
if the layer has a selection, otherwise every feature. The submenu lists the
coordinate types the layer's geometry offers; clicking one opens the table in Excel
(or in Notepad, comma delimited, when Excel is not installed).
"""

from contextlib import suppress

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QMenu

from ..core import copy_coordinates as engine

TEXT = 'Copy Coordinate to Excel'


def _icon():
    return QgsApplication.getThemeIcon('/mActionEditCopy.svg')


def _plain(text):
    return text.replace('&', '').replace('…', '').replace('...', '').strip().lower()


def _noun(layer, count):
    kind = engine.geometry_kind(layer)
    return {'point': 'point', 'line': 'line', 'polygon': 'polygon'}.get(kind, 'feature') + (
        '' if count == 1 else 's')


def run(layer, system, mode, use_selection, iface=None):
    """Build the table and send it. Returns (ok, message)."""
    fids = layer.selectedFeatureIds() if use_selection else None
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        header, rows = engine.build_table(layer, system, mode, fids)
        app, path = engine.send_table(layer.name(), header, rows)
    except engine.CoordinateError as error:
        return False, str(error)
    except Exception as error:      # noqa: BLE001 - tell the user instead of a traceback
        return False, 'Could not copy the coordinates: {}'.format(error)
    finally:
        QApplication.restoreOverrideCursor()
    note = {'Excel': 'Opened in Excel.',
            'Notepad': 'Excel was not found, so it opened in Notepad as comma-delimited text.',
            'default': 'Opened with the default program.'}[app]
    if app == 'Excel' and path.lower().endswith('.csv'):
        note = 'Opened in Excel (as a .csv, because the .xlsx writer is not available).'
    plural = '' if len(rows) == 1 else 's'
    return True, '{} coordinate{} from "{}". {}'.format(len(rows), plural, layer.name(), note)


class CopyCoordinatesController:

    def __init__(self, iface):
        self.iface = iface
        self._connected = False

    def install(self):
        self.iface.layerTreeView().contextMenuAboutToShow.connect(self._layer_menu)
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self._connected = True

    def uninstall(self):
        if not self._connected:
            return
        with suppress(TypeError, RuntimeError):
            self.iface.layerTreeView().contextMenuAboutToShow.disconnect(self._layer_menu)
        with suppress(TypeError, RuntimeError):
            self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._canvas_menu)
        self._connected = False

    def _go(self, layer, system, mode, use_selection):
        ok, message = run(layer, system, mode, use_selection, self.iface)
        bar = self.iface.messageBar()
        (bar.pushSuccess if ok else bar.pushWarning)(TEXT, message)

    def _submenu(self, parent, layer, use_selection, title=TEXT):
        sub = QMenu(title, parent)
        sub.setIcon(_icon())
        for label, system, mode in engine.options(layer):
            action = sub.addAction(label)
            action.triggered.connect(
                lambda _c=False, lyr=layer, s=system, m=mode, sel=use_selection:
                self._go(lyr, s, m, sel))
        return sub

    # ------------------------------------------------------------------ layer menu
    def _layer_menu(self, menu):
        with suppress(Exception):       # never break a menu over this
            layer = self.iface.activeLayer()
            if engine.geometry_kind(layer) is None:
                return
            if any(a.text() == TEXT for a in menu.actions()):
                return
            selected = layer.selectedFeatureCount()
            use_selection = selected > 0
            sub = self._submenu(menu, layer, use_selection)
            sub.setToolTip('{} {}'.format(
                selected if use_selection else layer.featureCount(),
                'selected ' + _noun(layer, selected) if use_selection else
                _noun(layer, layer.featureCount()) + ' (no selection: all features)'))
            before = None
            for existing in menu.actions():
                if not existing.isSeparator() and _plain(existing.text()) == 'export':
                    before = existing
                    break
            if before is None:
                menu.addMenu(sub)
            else:
                menu.insertMenu(before, sub)

    # ------------------------------------------------------------------ map menu
    def _canvas_menu(self, menu, _event):
        with suppress(Exception):
            if any(a.text() == TEXT for a in menu.actions()):
                return
            active = self.iface.activeLayer()
            layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
                      if engine.geometry_kind(lyr) is not None and lyr.selectedFeatureCount() > 0]
            if not layers:
                return
            layers.sort(key=lambda lyr: (lyr is not active, lyr.name().lower()))
            first = menu.actions()[0] if menu.actions() else None
            if len(layers) == 1:
                menu.insertMenu(first, self._submenu(menu, layers[0], True))
                return
            top = QMenu(TEXT, menu)
            top.setIcon(_icon())
            for layer in layers:
                count = layer.selectedFeatureCount()
                top.addMenu(self._submenu(
                    top, layer, True, '{} ({} {})'.format(layer.name(), count, _noun(layer, count))))
            menu.insertMenu(first, top)

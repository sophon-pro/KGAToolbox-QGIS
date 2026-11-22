# -*- coding: utf-8 -*-
"""Buttons in QGIS's status bar, placed right to the left of the coordinate box.

QGIS's coordinate widget class is not exposed to Python, so it is found by class name. A button goes at the start of
that widget's layout (always next to it, first added = leftmost); without a layout it takes a
permanent status-bar slot just before it, and without the coordinate box at all the end of the bar.
"""

from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtWidgets import QToolButton, QWidget

COORDS_CLASS = 'QgsStatusBarCoordinatesWidget'
COORDS_NAME = 'mCoordsEdit'


def status_bar(iface):
    """The main window's status bar, or None (tests, stubs)."""
    try:
        return iface.mainWindow().statusBar()
    except Exception:
        return None


def find_coordinate_box(bar):
    for widget in bar.findChildren(QWidget):
        if widget.metaObject().className() == COORDS_CLASS or widget.objectName() == COORDS_NAME:
            return widget
    return None


def _is_ours(widget):
    name = widget.objectName() if widget is not None else ''
    return name.startswith('kga') and name.endswith(('StatusButton', 'StatusLabel'))


def add_button(iface, action, name, menu=None):
    """A tool button for *action* beside the coordinate box; None if there is no status bar.

    The button shares the action's icon, tip and checked state. With a *menu* it gets a drop-down arrow."""
    bar = status_bar(iface)
    if bar is None:
        return None
    coords = find_coordinate_box(bar)
    button = QToolButton()
    button.setObjectName(name)
    if menu is not None:                    # before the action: setMenu() would forget the default action
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    button.setDefaultAction(action)
    button.setAutoRaise(True)
    button.setIconSize(QSize(18, 18))
    layout = coords.layout() if coords is not None else None
    if layout is not None:
        # left of the coordinates, after the KGA buttons that are already there (first added = leftmost)
        mine = 0
        while mine < layout.count() and _is_ours(layout.itemAt(mine).widget()):
            mine += 1
        layout.insertWidget(mine, button)
    elif coords is not None:
        index = 0
        for child in bar.children():
            if child is coords:
                break
            index += isinstance(child, QWidget)
        bar.insertPermanentWidget(index, button)        # just before the coordinate box
    else:
        bar.addPermanentWidget(button)
    return button


def add_label(iface, widget):
    """Put *widget* (objectName ending in 'StatusLabel') at the far left of the KGA group beside the coordinate box."""
    bar = status_bar(iface)
    if bar is None:
        return None
    coords = find_coordinate_box(bar)
    layout = coords.layout() if coords is not None else None
    if layout is not None:
        layout.insertWidget(0, widget)
    elif coords is not None:
        index = 0
        for child in bar.children():
            if child is coords:
                break
            index += isinstance(child, QWidget)
        bar.insertPermanentWidget(max(index - 2, 0), widget)
    else:
        bar.addPermanentWidget(widget)
    return widget


def remove_button(iface, button):
    if button is None or sip.isdeleted(button):
        return
    bar = status_bar(iface)
    if bar is not None:
        with suppress(Exception):
            bar.removeWidget(button)
    button.setParent(None)
    button.deleteLater()

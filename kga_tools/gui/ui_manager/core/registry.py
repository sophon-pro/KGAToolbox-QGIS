# -*- coding: utf-8 -*-
"""Discovery of the panels (QDockWidget) and toolbars (QToolBar) owned by the QGIS main window.

This module contains no UI code. It wraps each Qt widget in a small ``UiItem`` so the rest of
the plugin does not need to know whether it is dealing with a panel or a toolbar.
"""
import os

from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDockWidget, QToolBar

PANEL = "panel"
TOOLBAR = "toolbar"

_ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")

_DOCK_AREAS = {
    Qt.DockWidgetArea.LeftDockWidgetArea: "Left",
    Qt.DockWidgetArea.RightDockWidgetArea: "Right",
    Qt.DockWidgetArea.TopDockWidgetArea: "Top",
    Qt.DockWidgetArea.BottomDockWidgetArea: "Bottom",
}
_TOOLBAR_AREAS = {
    Qt.ToolBarArea.LeftToolBarArea: "Left",
    Qt.ToolBarArea.RightToolBarArea: "Right",
    Qt.ToolBarArea.TopToolBarArea: "Top",
    Qt.ToolBarArea.BottomToolBarArea: "Bottom",
}


def clean_text(text):
    """Remove mnemonic ampersands and trailing ellipses from Qt texts."""
    return (text or "").replace("&&", "\x00").replace("&", "").replace("\x00", "&").rstrip(".… ").strip()


class UiItem:
    """A uniform wrapper around a QDockWidget or a QToolBar."""

    def __init__(self, widget, kind, location=""):
        self.widget = widget
        self.kind = kind
        self.location = location

    # Identity ------------------------------------------------------------
    @property
    def title(self):
        return clean_text(self.widget.windowTitle()) or self.widget.objectName() or "(untitled)"

    @property
    def key(self):
        """Stable identifier, used by profiles and the thumbnail cache."""
        return self.widget.objectName() or "title:" + self.title

    # State ---------------------------------------------------------------
    def is_visible(self):
        # The toggle-view action reflects the "open" state. A panel that is open but sits
        # behind another tab still counts as open, which matches the QGIS menu.
        return self.widget.toggleViewAction().isChecked()

    def set_visible(self, visible):
        self.widget.setVisible(visible)
        if visible and self.kind == PANEL:
            self.widget.raise_()  # bring a tabified panel to the front

    # Visuals -------------------------------------------------------------
    def tool_actions(self):
        """Toolbar actions that carry an icon, excluding separators and embedded widgets."""
        if self.kind != TOOLBAR:
            return []
        return [a for a in self.widget.actions() if not a.isSeparator() and not a.icon().isNull()]

    def own_icon(self):
        """The icon the widget itself declares (may be null)."""
        return self.widget.toggleViewAction().icon()

    def icon(self):
        icon = self.own_icon()
        if not icon.isNull():
            return icon
        if self.kind == TOOLBAR:
            tools = self.tool_actions()
            if tools:
                return tools[0].icon()  # the first tool is the best visual cue
            return QIcon(os.path.join(_ICON_DIR, "toolbar.svg"))
        return QIcon(os.path.join(_ICON_DIR, "panel.svg"))


def discover(main_window, exclude=()):
    """Return ``(panels, toolbars)`` for the main window, each sorted by title.

    Only widgets that are actually docked into the main window are returned. Toolbars that
    live inside panels (for example the small toolbar of the Layers panel) are skipped,
    which reproduces the list QGIS shows on right-click.
    """
    panels, toolbars = [], []

    for dock in main_window.findChildren(QDockWidget):
        if dock in exclude:
            continue
        area = main_window.dockWidgetArea(dock)
        if area == Qt.DockWidgetArea.NoDockWidgetArea and not dock.isFloating():
            continue
        where = "Floating" if dock.isFloating() else _DOCK_AREAS.get(area, "")
        panels.append(UiItem(dock, PANEL, where))

    for bar in main_window.findChildren(QToolBar):
        if bar in exclude:
            continue
        area = main_window.toolBarArea(bar)
        if area == Qt.ToolBarArea.NoToolBarArea:
            continue
        where = "Floating" if bar.isFloating() else _TOOLBAR_AREAS.get(area, "")
        toolbars.append(UiItem(bar, TOOLBAR, where))

    panels.sort(key=lambda i: i.title.lower())
    toolbars.sort(key=lambda i: i.title.lower())
    return panels, toolbars


ICON_SIZE = QSize(18, 18)

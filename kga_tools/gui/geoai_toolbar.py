# -*- coding: utf-8 -*-
"""The KGA GeoAI toolbar: tools that run AI models on imagery.

One button per tool of the "KGA GeoAI" Processing group, in the group's menu
order: Detect Roads from Imagery, then Detect Paddy Field Plots from
Imagery. The tools are kept off the KGA
Toolbox toolbar so that toolbar stays for everyday GIS work; they are still in
the KGA Toolbox menu, the Processing Toolbox and the KGA Geoprocessing panel.

The KGA Toolbox menu gets a "KGA GeoAI Toolbar" show/hide checkbox, beside the
other toolbar checkboxes.
"""

from contextlib import suppress

TITLE = 'KGA GeoAI'
OBJECT_NAME = 'KGAGeoAIToolbar'


class GeoAIToolbarController(object):
    """Builds and removes the KGA GeoAI toolbar.

    ``actions`` are the tool actions, made and owned by the plugin's toolbar
    builder; this only places them.
    """

    def __init__(self, iface, actions=(), parent_menu=None, menu_before=None):
        self.iface = iface
        self.actions = list(actions)
        self.parent_menu = parent_menu
        self.menu_before = menu_before
        self.toolbar = None
        self.menu_action = None
        self.magnify = None

    def initGui(self):  # noqa: N802
        if not self.actions:
            return
        self.toolbar = self.iface.addToolBar(TITLE)
        self.toolbar.setObjectName(OBJECT_NAME)
        self.toolbar.setToolTip(TITLE)
        try:
            from .toolbar_magnify import ToolbarMagnify
            self.magnify = ToolbarMagnify(self.toolbar)
        except Exception:
            self.magnify = None
        for action in self.actions:
            self.toolbar.addAction(action)
        if self.parent_menu is not None:
            self.menu_action = self.toolbar.toggleViewAction()
            self.menu_action.setText('KGA GeoAI Toolbar')
            if self.menu_before is not None:
                self.parent_menu.insertAction(self.menu_before,
                                              self.menu_action)
            else:
                self.parent_menu.addAction(self.menu_action)

    def unload(self):
        if self.parent_menu is not None and self.menu_action is not None:
            with suppress(RuntimeError):
                self.parent_menu.removeAction(self.menu_action)
        self.menu_action = None
        if self.magnify is not None:
            with suppress(RuntimeError):
                self.magnify.remove()
        self.magnify = None
        if self.toolbar is not None:
            with suppress(RuntimeError):
                self.toolbar.clear()        # the actions belong to the plugin
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar.deleteLater()
        self.toolbar = None

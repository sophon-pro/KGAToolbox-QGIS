# -*- coding: utf-8 -*-
"""The KGA Layer toolbar: tools that work on the layer selected in the Layers panel.

Left to right, a separator between the groups:

* Layer Bag - the attribute table, statistics, labels and colours of the selected layer;
* Quick Labeling and Quick Symbology (moved here from the KGA Editor toolbar; QGIS 3.44 or newer, as
  before);
* the bags, one button each - Tool Bag, Symbol Bag, Layer Colors and Label Bag (moved here from the KGA Toolbox toolbar);
* Layer Path Manager - audit and repair layer data sources (moved here from the KGA Toolbox toolbar).

The KGA Toolbox menu gets a "KGA Layer Toolbar" show/hide checkbox, next to "KGA Editor Toolbar".
"""

from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog

TITLE = "KGA Layer"
OBJECT_NAME = "KGALayerToolbar"
QUICK_TOOLS_MIN_QGIS = 34400        # Quick Labeling / Quick Symbology were only tested on 3.44+
LOG_TAG = "KGA Tools"


class LayerToolbarController(object):
    """Builds and removes the KGA Layer toolbar.

    ``mini_panel`` is the LayerMiniPanelController (or None), ``bag_actions`` the Tool Bag / Symbol Bag /
    Layer Colors / Label Bag actions (each gets its own button; ``bag_icon`` and ``set_default_action`` are
    kept for callers but no longer used), ``bag_provider`` a
    callable returning the Symbol Bag plugin (Quick Symbology's Symbol Bag button),
    ``path_manager_action`` the Layer Path Manager action (or None).
    """

    def __init__(self, iface, mini_panel=None, bag_actions=(), bag_icon=None, set_default_action=None,
                 bag_provider=None, path_manager_action=None, parent_menu=None, menu_before=None):
        self.iface = iface
        self.mini_panel = mini_panel
        self.bag_actions = list(bag_actions)
        self.bag_icon = bag_icon
        self.set_default_action = set_default_action
        self.bag_provider = bag_provider
        self.path_manager_action = path_manager_action
        self.parent_menu = parent_menu
        self.menu_before = menu_before
        self.toolbar = None
        self.menu_action = None
        self.quick_label = None
        self.quick_symbol = None
        self.magnify = None

    # ------------------------------------------------------------------ build
    def initGui(self):  # noqa: N802
        self.toolbar = self.iface.addToolBar(TITLE)
        self.toolbar.setObjectName(OBJECT_NAME)
        self.toolbar.setToolTip(TITLE)
        groups = [self._add_mini_panel, self._add_quick_tools, self._add_bags,
                  self._add_path_manager]
        first = True
        for add in groups:
            before = len(self.toolbar.actions())
            sep = None if first else self.toolbar.addSeparator()
            add()
            if len(self.toolbar.actions()) == before + (0 if first else 1):
                if sep is not None:             # the group added nothing: no separator for it
                    self.toolbar.removeAction(sep)
                continue
            first = False
        self._build_menu()
        try:
            from .toolbar_magnify import ToolbarMagnify
            self.magnify = ToolbarMagnify(self.toolbar)
        except Exception:
            self._log("Magnify")
            self.magnify = None

    def _add_mini_panel(self):
        if self.mini_panel is not None and self.mini_panel.action is not None:
            self.toolbar.addAction(self.mini_panel.action)

    def _add_quick_tools(self):
        if Qgis.QGIS_VERSION_INT < QUICK_TOOLS_MIN_QGIS:
            return
        try:
            from ..editor.quicklabel.controller import QuickLabelController
            self.quick_label = QuickLabelController(self.iface)
            self.quick_label.build(self.toolbar)
        except Exception:
            self._log("Quick Labeling")
            self.quick_label = None
        try:
            from ..editor.quicksymbol.controller import QuickSymbolController
            self.quick_symbol = QuickSymbolController(
                self.iface, bag_provider=lambda: self.bag_provider() if self.bag_provider else None)
            self.quick_symbol.build(self.toolbar)
        except Exception:
            self._log("Quick Symbology")
            self.quick_symbol = None

    def _add_bags(self):
        """The bags - Tool Bag, Symbol Bag, Layer Colors and Label Bag - each get a button of their own."""
        for action in self.bag_actions:
            self.toolbar.addAction(action)

    def _add_path_manager(self):
        if self.path_manager_action is not None:
            self.toolbar.addAction(self.path_manager_action)

    def _build_menu(self):
        """A 'KGA Layer Toolbar' show/hide checkbox in the KGA Toolbox menu."""
        if self.parent_menu is None:
            return
        self.menu_action = self.toolbar.toggleViewAction()
        self.menu_action.setText("KGA Layer Toolbar")
        if self.menu_before is not None:
            self.parent_menu.insertAction(self.menu_before, self.menu_action)
        else:
            self.parent_menu.addAction(self.menu_action)

    @staticmethod
    def _log(what):
        import traceback
        QgsMessageLog.logMessage("Could not add {} to the KGA Layer toolbar:\n{}".format(
            what, traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)

    # ------------------------------------------------------------------ remove
    def unload(self):
        if self.magnify is not None:
            with suppress(Exception):
                self.magnify.remove()
        self.magnify = None
        for tool in (self.quick_symbol, self.quick_label):
            if tool is not None:
                with suppress(Exception):
                    tool.remove()
        self.quick_symbol = self.quick_label = None
        if self.parent_menu is not None and self.menu_action is not None:
            with suppress(RuntimeError):
                self.parent_menu.removeAction(self.menu_action)
        self.menu_action = None
        if self.toolbar is not None:
            with suppress(RuntimeError):
                self.toolbar.clear()        # the shared actions (mini panel, bags, path manager) belong to their owners
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar.deleteLater()
        self.toolbar = None

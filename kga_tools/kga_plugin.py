"""KGA Toolbox plugin.

Registers the KGA processing provider, builds a toolbar whose buttons mirror the
algorithm groups, and hosts the KGA Geoprocessing dock panel.

Icon and group-ordering configuration now lives in branding.py so the toolbar
and the panel stay in sync.
"""

import traceback
from contextlib import suppress
from functools import partial

import qgis.utils
from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAction, QDockWidget, QMenu, QToolButton, QWidget, QWidgetAction)

from .branding import (
    GROUP_ICONS,
    KGA_WEBSITE,
    LOG_TAG,
    MENU_ONLY_GROUPS,
    STANDALONE_ALGS,
    STANDALONE_COMPANIONS,
    algorithm_icon,
    button_label,
    icon as _icon,
    ordered_algorithms,
    ordered_groups,
)
from .core import gc_relief
from .core import geometry_fields
from .core import kga_settings as ks
from .core import ui_layout
from .core import system_theme
from .core.basemaps import BASEMAPS, add_basemap_layer, provider_of, register_basemaps
from .gui import geoprocessing_panel as gp
from .gui.about_dialog import AboutDialog
from .gui.browser_package import BrowserPackageInstaller
from .gui.browser_paste import BrowserPasteInstaller
from .data_ingest.controller import DataIngestController
from .gui.catalog_controller import CatalogController
from .gui.ui_manager.controller import UiManagerController
from .gui.layer_path_manager.controller import LayerPathManagerController
from .gui import panel_bubble
from .gui.bag_group import BubbleGroup
from .gui.label_bag.controller import LabelBagController
from .gui.layer_mini_panel.controller import LayerMiniPanelController
from .gui.layer_toolbar import LayerToolbarController
from .gui.geometry_info import GeometryInfoController
from .gui.grid_view import GridController
from .gui.node_view import NodeViewController
from .gui.geoai_toolbar import GeoAIToolbarController
from .gui.symbol_bag.plugin import SymbolBagPlugin
from .gui.tool_bag.plugin import ToolBagPlugin
from .gui.geoprocessing_panel import KgaGeoprocessingPanel
from .gui.duplicate_layer_dialog import show_duplicate_layer
from .gui.layout_guide_dock import LayoutGuideManager
from .gui.layer_menu import LayerMenuInjector
from .gui.layer_sort_controller import LayerSortController
from .gui.clip_basemap_dialog import close_clip_basemap, show_clip_basemap
from .gui.go_to_xy_bar import close_go_to_xy
from .gui.go_to_xy_bar import current_bar as current_go_to_xy
from .gui.go_to_xy_bar import get_bar as get_go_to_xy_bar
from .gui.go_to_xy_bar import set_go_to_xy_visible
from .gui.zoom_location_dialog import close_zoom_location, show_zoom_location
from .gui.quick_template_dialog import (
    close_quick_template,
    show_quick_template,
)
from .gui.new_project_basemap import NewProjectBasemap
from .gui import layer_scope
from .gui.map_tabs import MapTabs
from .gui.pro_layers import ProLayers
from .gui.drag_chip import LayerDragChip
from .gui.pro_selection import ProSelection
from .gui.settings_dialog import SettingsDialog
from .gui.smooth_navigation import SmoothNavigation
from .gui import window_reveal
from .gui.temporary_pan import TemporaryPan
from .gui.cursor_style import CursorStyleManager
from .gui.start_page import StartPageInstaller, settle_blank_project
from .gui.update_dialog import UpdateDialog
from .provider import KgaProvider

MENU_TITLE = '&KGA Toolbox'

# The KGA Editor toolbar is built on QGIS APIs that only exist from 3.44.
EDITOR_MIN_QGIS = 34400
# Every Editing Tools tool (Planarize Lines included) lives on the KGA Editor toolbar
# as buttons, right after its Vertex button.
EDITOR_TOOLBAR_GROUP = 'KGA Editing Tools'
# The GeoAI tools (AI models run on imagery) get a toolbar of their own, the
# KGA GeoAI toolbar, rather than a drop-down on the KGA Toolbox toolbar.
GEOAI_TOOLBAR_GROUP = 'KGA GeoAI'
# The Catalog panel uses the scoped Browser enums (Qgis.BrowserItemState and
# friends), which arrived in 3.36.
CATALOG_MIN_QGIS = 33600
# Data Explorer uses QgsProviderRegistry.querySublayers() and the 3.44 Qt6 shims.
DATA_INGEST_MIN_QGIS = 34400
# Stand-alone plugins the editor replaces. Two copies would give duplicate
# buttons and two canvas event filters, so the KGA editor stays out of the way.
# Layer Auto-Sort uses Qgis.LayerType / Qgis.GeometryType (3.30+). The stand-alone
# plugin it replaces would sort every added layer twice.
LAYER_SORT_MIN_QGIS = 33000
LAYER_SORT_CLASH_PLUGINS = ('layer_autosort',)
# Tool Bag is built in. The stand-alone plugin (or its old name) would give two bags and two
# toolbar hooks, so the built-in one stays out of the way.
TOOL_BAG_CLASH_PLUGINS = ('tool_bag', 'custom_tool_workspace')
SYMBOL_BAG_CLASH_PLUGINS = ('symbol_bag',)
LAYER_MINI_PANEL_CLASH_PLUGINS = ('layer_mini_panel',)
EDITOR_CLASH_PLUGINS = ('edit_session', 'construction_sketch',
                        'arcgis_construction_sketch')

BASEMAP_ICONS = {'OpenStreetMap': 'imagery_basemaps/osm.png', 'Google': 'imagery_basemaps/GoogleMap.png',
                 'ESRI': 'imagery_basemaps/esri.png'}


# Data Explorer already owns Ctrl+Alt+I.
SHORTCUT_GEOPROCESSING = 'Ctrl+Alt+G'
SHORTCUT_CATALOG = 'Ctrl+Alt+C'
SHORTCUT_EDITOR = 'Ctrl+Alt+E'
SHORTCUT_UI_MANAGER = 'Ctrl+Alt+P'
# The stand-alone plugin used Ctrl+Shift+M, which QGIS already gives a tool of its own.
SHORTCUT_MINI_PANEL = 'Ctrl+Alt+L'


class _DividerLine(QWidget):
    """A one-pixel rule that paints the same at any display scaling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(7)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return QSize(20, 7)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(self.palette().mid().color())
        pen.setWidth(0)         # cosmetic: always exactly one device pixel
        painter.setPen(pen)
        painter.drawLine(4, 3, self.width() - 4, 3)


class KgaToolsPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.toolbar = None
        self.menu = None
        self.panel = None
        self.panel_action = None
        self._owned_widgets = []
        self._reveal = None
        self._layer_menu = None
        self._filter_selected = None
        self._copy_coordinates = None
        self._quick_layer_actions = []
        self._basemap_menus = []
        self._browser_paste = None
        self._browser_package = None
        self._layer_symbol_tabs = []
        self._callout_label_hook = None
        self._layer_symbol_dock = None
        self._editor = None
        self._editor_tools = []
        self._tools_anchor = None
        self._catalog = None
        self._data_ingest = None
        self._ui_manager = None
        self._path_manager = None
        self._tool_bag = None
        self._symbol_bag = None
        self._bag_group = None
        self._bag_actions = []
        self._mini_panel = None
        self._label_bag = None
        self._layer_toolbar = None
        self._geoai_toolbar = None
        self._geoai_tools = []
        self._attr_bubble = None
        self._bags_menu = None
        self._layer_sort = None
        self._new_basemap = None
        self._pro_selection = None
        self._pro_layers = None
        self._layer_modes = None
        self._drag_chip = None
        self._map_tabs = None
        self._smooth_nav = None
        self._temp_pan = None
        self._node_view = None
        self._grid = None
        self._geometry_info = None
        self._cursor_style = None
        self._start_page = None
        self._layout_guides = None
        self._geometry_fields_action = None

    # ------------------------------------------------------------------ setup

    def _default_docked_attribute_table(self):
        """Options > Data Sources > "Open attribute table as docked window" starts ticked.

        Applied once per profile, on first install, so a user who later unticks it keeps their choice.
        """
        with suppress(Exception):
            s = QgsSettings()
            flag = 'KGA/defaults/dock_attribute_table_done'
            if not s.value(flag, False, type=bool):
                s.setValue('qgis/dockAttributeTable', True)
                s.setValue(flag, True)

    def initGui(self):
        self._default_docked_attribute_table()
        self.init_system_theme()
        self.init_processing()

        self.toolbar = self.iface.addToolBar('KGA Toolbox')
        self.toolbar.setObjectName('KGAToolsToolbar')
        self.toolbar.setToolTip('KGA Toolbox')
        self._toolbar_magnify = self._magnify(self.toolbar)

        self.menu = QMenu(MENU_TITLE, self.iface.mainWindow())
        self.iface.mainWindow().menuBar().insertMenu(
            self.iface.firstRightStandardMenu().menuAction(), self.menu)

        panel_bubble.set_reveal_group(self.bag_group())
        self.bag_group().open_settings = lambda: self.show_settings('Bubbles')
        self.init_panel()
        self.init_catalog()
        self.init_new_project_basemap()
        self.init_pro_selection()
        self.init_pro_layers()
        with suppress(Exception):                    # Layers panel drags show a compact badge
            self._drag_chip = LayerDragChip(self.iface)
            self._drag_chip.install()
        self.init_map_tabs()
        with suppress(Exception):
            layer_scope.install()                    # every layer drop-down lists only the current map's layers
        self.init_smooth_navigation()
        self.init_temporary_pan()
        self.init_cursor_style()
        self.init_start_page()
        self.init_data_ingest()
        self.init_ui_manager()
        self.init_path_manager()
        self.init_layer_modes()                      # after the path manager: it places its buttons by theirs
        self.init_tool_bag()
        self.init_symbol_bag()
        self.init_layer_mini_panel()
        self.init_label_bag()
        self.init_layer_sort()
        self.init_attribute_bubble()
        self.init_layers_bubble()
        self.build_toolbar()
        self.init_layer_menu()
        self.init_quick_template_menu()
        self.init_geometry_fields()
        self.init_browser_paste()
        self.init_browser_package()
        self.init_editor()
        self.init_node_view()
        self.init_grid()
        self.init_geometry_info()
        self.init_layer_toolbar()
        self.init_geoai_toolbar()
        self.init_layer_symbol_tab()
        self.init_callout_label_mode()
        self.init_layout_guides()
        self.init_basemaps()
        # The bag lists the tools of every toolbar, including the ones built just above.
        QTimer.singleShot(0, self.refresh_tool_bag)
        # Once start-up is over: take the long-lived Python objects out of the garbage collector's
        # way, so its full passes stop freezing the window for ~100 ms (see core/gc_relief.py).
        gc_relief.settle_later()

    def init_layout_guides(self):
        """A Guides panel and toolbar button in every Layout Designer."""
        try:
            self._layout_guides = LayoutGuideManager(self.iface, self.run_algorithm)
            self._layout_guides.install()
        except Exception:
            self._layout_guides = None
            QgsMessageLog.logMessage(
                'Could not add the layout guides panel: {}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)

    def remove_layout_guides(self):
        if self._layout_guides is not None:
            with suppress(Exception):
                self._layout_guides.uninstall()
            self._layout_guides = None

    def init_basemaps(self):
        """Offer the OSM / Google / ESRI XYZ Tiles connections in the Browser."""
        try:
            if register_basemaps():
                self.iface.reloadConnections()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add the basemap connections:\n'
                + traceback.format_exc(), 'KGA Tools', Qgis.MessageLevel.Warning)

    def init_processing(self):
        self.provider = KgaProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def init_panel(self):
        """The Geoprocessing pane. Hidden by default; toggled from the toolbar."""
        self.panel = KgaGeoprocessingPanel(
            self.provider.algorithms, self.iface.mainWindow())
        self.panel.algorithmTriggered.connect(self._run_from_panel)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel.hide()
        # It lists the same algorithms the Processing Toolbox does, so it
        # belongs in the Toolbox's tab group rather than as a second panel
        # stacked under it. Deferred to the event loop for two reasons: the
        # Processing plugin may not have built its dock yet when this one is
        # loaded at start-up, and QGIS restores the saved window layout after
        # plugins load, which would undo a tabify done here.
        QTimer.singleShot(0, self.tabify_panel_with_toolbox)

        # toggleViewAction gives a checkable action wired to the dock for free,
        # so the toolbar button reflects the panel's visibility.
        self.panel_action = self.panel.toggleViewAction()
        self.panel_action.setIcon(_icon('branding/kga.png'))
        self.panel_action.setText('KGA Geoprocessing')
        self.panel_action.setToolTip('Show the KGA Geoprocessing panel')
        # toggleViewAction only shows the dock. Tabbed onto the Processing
        # Toolbox that leaves it behind whichever tab is in front, so the
        # button reads as doing nothing; raise it to the front tab as well.
        self.panel_action.triggered.connect(self.raise_panel)
        panel_bubble.attach_reveal(self.panel, self.panel_action, 'geoprocessing',
                                   'KGA Geoprocessing – click to open',
                                   panel_bubble.file_glyph('bubble/geoprocessing.png'))

    def init_catalog(self):
        """The Catalog panel (Project / Connections / Computer / Favorites)."""
        if Qgis.QGIS_VERSION_INT < CATALOG_MIN_QGIS:
            QgsMessageLog.logMessage(
                'The Catalog panel needs QGIS 3.36 or newer; this is QGIS '
                '{}.'.format(Qgis.QGIS_VERSION), LOG_TAG,
                Qgis.MessageLevel.Info)
            return
        try:
            self._catalog = CatalogController(self.iface, group=self.bag_group())
            self._catalog.initGui()
        except Exception:
            # Never let the Catalog stop the rest of the plugin loading.
            QgsMessageLog.logMessage(
                'Could not add the Catalog panel:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.remove_catalog()

    def remove_catalog(self):
        if self._catalog is not None:
            with suppress(Exception):
                self._catalog.unload()
            self._catalog = None

    def init_data_ingest(self):
        """Data Explorer: the Premiere-style media browser dock."""
        if Qgis.QGIS_VERSION_INT < DATA_INGEST_MIN_QGIS:
            QgsMessageLog.logMessage(
                'Data Explorer needs QGIS 3.44 or newer; this is QGIS '
                '{}.'.format(Qgis.QGIS_VERSION), LOG_TAG,
                Qgis.MessageLevel.Info)
            return
        try:
            self._data_ingest = DataIngestController(
                self.iface, catalog_getter=lambda: self._catalog,
                group=self.bag_group())
            self._data_ingest.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Data Explorer:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.remove_data_ingest()

    def remove_data_ingest(self):
        if self._data_ingest is not None:
            with suppress(Exception):
                self._data_ingest.unload()
            self._data_ingest = None

    def init_ui_manager(self):
        """Panels and Toolbars Manager: visual switcher with layouts."""
        try:
            self._ui_manager = UiManagerController(
                self.iface, catalog_getter=lambda: self._catalog)
            self._ui_manager.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Panels and Toolbars Manager:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.remove_ui_manager()

    def remove_ui_manager(self):
        if self._ui_manager is not None:
            with suppress(Exception):
                self._ui_manager.unload()
            self._ui_manager = None

    def init_path_manager(self):
        """Layer Path Manager: audit and repair layer data sources."""
        try:
            self._path_manager = LayerPathManagerController(
                self.iface, catalog_getter=lambda: self._catalog)
            self._path_manager.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Layer Path Manager:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.remove_path_manager()

    def remove_path_manager(self):
        if self._path_manager is not None:
            with suppress(Exception):
                self._path_manager.unload()
            self._path_manager = None

    def init_tool_bag(self):
        """Tool Bag: a floating bag of favourite tools that shrinks into a bubble on the map."""
        clash = [name for name in TOOL_BAG_CLASH_PLUGINS if name in qgis.utils.plugins]
        if clash:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox',
                'Tool Bag was not added because the stand-alone plugin "{}" is '
                'installed and does the same job.'.format(clash[0]))
            return
        try:
            self._tool_bag = ToolBagPlugin(
                self.iface, embedded=True, toggle_icon=_icon('interface/toolBag.png'),
                group=self.bag_group())
            self._tool_bag.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Tool Bag:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.remove_tool_bag()

    def open_attribute_bubble(self, layer, point):
        """Layer menu > Attribute Table Bubble: the table opens as a bubble, flying out of the click."""
        manager = getattr(self, '_attr_bubble', None)
        if manager is None:
            self.iface.showAttributeTable(layer)
            return
        manager.open_in_bubble(layer, point)

    def init_layers_bubble(self):
        """The Layers panel gets a Minimize button and folds into a bubble like the KGA panels."""
        try:
            dock = self.iface.mainWindow().findChild(QDockWidget, 'Layers')
            if dock is not None:
                panel_bubble.attach_reveal(
                    dock, None, 'layers_panel', 'Layers – click to open',
                    panel_bubble.file_glyph('bubble/layers_panel.png'))
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add the Layers panel bubble:' + chr(10) + traceback.format_exc(),
                'KGA Tools', Qgis.MessageLevel.Warning)

    def init_attribute_bubble(self):
        """Attribute tables get a Minimize button and fold into a bubble on the map."""
        try:
            from .gui.panel_bubble import AttributeBubbleManager
            self._attr_bubble = AttributeBubbleManager(self.iface, self.bag_group())
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add the attribute table bubble:' + chr(10) + '{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)
            self._attr_bubble = None

    def remove_attribute_bubble(self):
        if self._attr_bubble is not None:
            with suppress(Exception):
                self._attr_bubble.unload()
            self._attr_bubble = None

    def bag_group(self):
        """The column the bubbles of the Tool Bag, Symbol Bag and Layer Colors share."""
        if self._bag_group is None:
            self._bag_group = BubbleGroup(self.iface)
        return self._bag_group

    def init_symbol_bag(self):
        """Symbol Bag and Layer Colors: toolbar buttons right after the Tool Bag."""
        clash = [name for name in SYMBOL_BAG_CLASH_PLUGINS
                 if name in qgis.utils.plugins]
        if clash:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox',
                'Symbol Bag was not added because the stand-alone plugin "{}" is '
                'installed and does the same job.'.format(clash[0]))
            return
        try:
            self._symbol_bag = SymbolBagPlugin(
                self.iface, group=self.bag_group(),
                toggle_icon=_icon('mapping/symbolBag.png'))
            self._symbol_bag.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Symbol Bag:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.remove_symbol_bag()

    def remove_symbol_bag(self):
        if self._symbol_bag is not None:
            with suppress(Exception):
                self._symbol_bag.unload()
            self._symbol_bag = None

    def init_layer_mini_panel(self):
        """Layer Bag: attributes, labels and colours of the selected layer (KGA Layer toolbar)."""
        clash = [name for name in LAYER_MINI_PANEL_CLASH_PLUGINS
                 if name in qgis.utils.plugins]
        if clash:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox',
                'Layer Bag was not added because the stand-alone plugin "{}" is '
                'installed and does the same job.'.format(clash[0]))
            return
        try:
            self._mini_panel = LayerMiniPanelController(
                self.iface, bag_provider=lambda: self._symbol_bag,
                icon=_icon('interface/layerMiniPanel.png'), group=self.bag_group())
            self._mini_panel.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Layer Bag:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.remove_layer_mini_panel()

    def remove_layer_mini_panel(self):
        if self._mini_panel is not None:
            self._unregister_shortcut(self._mini_panel.action)
            with suppress(Exception):
                self._mini_panel.unload()
            self._mini_panel = None

    def init_label_bag(self):
        """Label Bag: the labels of the selected layer in a floating bag (Bags drop-down)."""
        try:
            self._label_bag = LabelBagController(
                self.iface, bag_provider=lambda: self._symbol_bag,
                icon=_icon('interface/labelBag.png'), group=self.bag_group())
            self._label_bag.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Label Bag:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.remove_label_bag()

    def remove_label_bag(self):
        if self._label_bag is not None:
            with suppress(Exception):
                self._label_bag.unload()
            self._label_bag = None

    @staticmethod
    def _magnify(toolbar):
        """The Dock-style magnify effect for a KGA toolbar (Settings > Appearance > Toolbar buttons)."""
        try:
            from .gui.toolbar_magnify import ToolbarMagnify
            return ToolbarMagnify(toolbar)
        except Exception:
            QgsMessageLog.logMessage('Could not add Magnify to a toolbar:\n{}'.format(traceback.format_exc()),
                                     LOG_TAG, Qgis.MessageLevel.Warning)
            return None

    def init_layer_toolbar(self):
        """The KGA Layer toolbar: Layer Bag, Quick Labeling / Symbology, the Bags drop-down and
        Layer Path Manager."""
        if self._layer_toolbar is not None:
            return
        try:
            self._layer_toolbar = LayerToolbarController(
                self.iface, mini_panel=self._mini_panel, bag_actions=self._bag_actions,
                bag_icon=_icon('interface/toolBag.png'),
                set_default_action=self.set_default_action,
                bag_provider=lambda: self._symbol_bag,
                path_manager_action=(self._path_manager.action
                                     if self._path_manager is not None else None),
                parent_menu=self.menu,
                menu_before=getattr(self, '_update_separator', None))
            self._layer_toolbar.initGui()
            if self._reveal is not None:
                self._reveal.watch_toolbar(self._layer_toolbar.toolbar)
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add the KGA Layer toolbar:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.remove_layer_toolbar()

    def remove_layer_toolbar(self):
        if self._layer_toolbar is not None:
            with suppress(Exception):
                self._layer_toolbar.unload()
            self._layer_toolbar = None

    def init_geoai_toolbar(self):
        """The KGA GeoAI toolbar: one button per tool of the KGA GeoAI group."""
        if self._geoai_toolbar is not None or not self._geoai_tools:
            return
        try:
            self._geoai_toolbar = GeoAIToolbarController(
                self.iface, self._geoai_tools, parent_menu=self.menu,
                menu_before=getattr(self, '_update_separator', None))
            self._geoai_toolbar.initGui()
            if self._reveal is not None and self._geoai_toolbar.toolbar is not None:
                self._reveal.watch_toolbar(self._geoai_toolbar.toolbar)
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add the KGA GeoAI toolbar:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.remove_geoai_toolbar()

    def remove_geoai_toolbar(self):
        if self._geoai_toolbar is not None:
            with suppress(Exception):
                self._geoai_toolbar.unload()
            self._geoai_toolbar = None

    def refresh_tool_bag(self):
        if self._tool_bag is not None:
            with suppress(Exception):
                self._tool_bag.refresh()

    def remove_tool_bag(self):
        if self._tool_bag is not None:
            with suppress(Exception):
                self._tool_bag.unload()
            self._tool_bag = None

    def init_layer_sort(self):
        """Layer Auto-Sort: ArcGIS Pro style ordering of the Layers panel."""
        if Qgis.QGIS_VERSION_INT < LAYER_SORT_MIN_QGIS:
            QgsMessageLog.logMessage(
                'Layer Auto-Sort needs QGIS 3.30 or newer; this is QGIS '
                '{}.'.format(Qgis.QGIS_VERSION), LOG_TAG,
                Qgis.MessageLevel.Info)
            return
        clash = [name for name in LAYER_SORT_CLASH_PLUGINS
                 if name in qgis.utils.plugins]
        if clash:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox',
                'Layer Auto-Sort was not added because the stand-alone plugin '
                '"{}" is installed and does the same job.'.format(clash[0]))
            return
        try:
            self._layer_sort = LayerSortController(self.iface)
            self._layer_sort.initGui()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add Layer Auto-Sort:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.remove_layer_sort()

    def remove_layer_sort(self):
        if self._layer_sort is not None:
            with suppress(Exception):
                self._layer_sort.unload()
            self._layer_sort = None

    def raise_panel(self, checked):
        """Make the Geoprocessing panel the current tab when it is switched on."""
        if checked and self.panel is not None:
            self.panel.raise_()

    def tabify_panel_with_toolbox(self):
        """Put the Geoprocessing panel in the Processing Toolbox's tab group."""
        if self.panel is None:
            return
        window = self.iface.mainWindow()
        toolbox = window.findChild(QDockWidget, 'ProcessingToolbox')
        # No Toolbox (Processing switched off) or the user has pulled it out
        # into its own window: leave the panel where QGIS put it.
        if toolbox is None or toolbox.isFloating() or self.panel.isFloating():
            return
        window.tabifyDockWidget(toolbox, self.panel)

    def init_layer_menu(self):
        """Duplicate to New Layer, below Duplicate Layer on the layer menu."""
        self._layer_menu = LayerMenuInjector(
            self.iface, self.show_duplicate_layer, self.iface.mainWindow(),
            bubble_callback=self.open_attribute_bubble)
        self._layer_menu.install()
        from .gui.filter_selected import FilterSelectedController
        self._filter_selected = FilterSelectedController(self.iface)
        self._filter_selected.install()
        from .gui.copy_coordinates import CopyCoordinatesController
        self._copy_coordinates = CopyCoordinatesController(self.iface)
        self._copy_coordinates.install()

    def init_quick_template_menu(self):
        """Add Quick Template Layer, first entry of the QGIS Layer menu."""
        try:
            menu = self.iface.layerMenu()
            first = menu.actions()[0] if menu.actions() else None
            action = QAction(_icon('editing/quickTemplate.png'),
                             'Add Quick Template Layer',
                             self.iface.mainWindow())
            action.setToolTip('Make an empty point, line or polygon layer')
            action.triggered.connect(self.show_quick_template)
            separator = QAction(self.iface.mainWindow())
            separator.setSeparator(True)
            basemap = QAction(_icon('data_management/addToMap.png'), 'Add Quick Basemap',
                              self.iface.mainWindow())
            basemap.setToolTip('Add an OSM, Google or ESRI basemap')
            basemap.setMenu(self.build_basemap_menu(self.iface.mainWindow()))
            # All go in front of whatever QGIS put first, in this order.
            menu.insertAction(first, action)
            menu.insertAction(first, basemap)
            menu.insertAction(first, separator)
            self._quick_layer_actions = [action, basemap, separator]
        except Exception:
            self._quick_layer_actions = []
            QgsMessageLog.logMessage(
                'Could not add Add Quick Template Layer to the Layer menu:\n{}'
                .format(traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def init_geometry_fields(self):
        """Shape_Length / Shape_Area on project database layers, plus a Layer menu entry."""
        try:
            geometry_fields.install()
            action = QAction('Add Shape_Length / Shape_Area Fields',
                             self.iface.mainWindow())
            action.setToolTip('Add and keep up to date the geometry fields of '
                              'the selected line or polygon layer')
            action.triggered.connect(self.add_geometry_fields)
            self.iface.layerMenu().addAction(action)
            self._geometry_fields_action = action
        except Exception:
            QgsMessageLog.logMessage(
                'Could not set up the geometry fields:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)

    def remove_geometry_fields(self):
        with suppress(Exception):
            geometry_fields.uninstall()
        if self._geometry_fields_action is not None:
            with suppress(Exception):
                self.iface.layerMenu().removeAction(self._geometry_fields_action)
            self._geometry_fields_action.deleteLater()
            self._geometry_fields_action = None

    def add_geometry_fields(self):
        layer = self.iface.activeLayer()
        bar = self.iface.messageBar()
        try:
            added, filled = geometry_fields.install().prepare(layer)
        except ValueError as exc:
            bar.pushWarning('KGA Toolbox', str(exc))
            return
        except Exception as exc:
            bar.pushWarning('KGA Toolbox',
                            'Could not add the geometry fields: {}'.format(exc))
            return
        bar.pushSuccess('KGA Toolbox', '{}: {} Updated {} feature(s).'.format(
            layer.name(),
            'Added {}.'.format(', '.join(added)) if added
            else 'The fields were already there.', filled))

    def build_basemap_menu(self, parent):
        """One submenu per provider; picking an entry adds that basemap."""
        menu = QMenu(parent)
        subs = {}
        for name, _url, _zmax in BASEMAPS:
            provider = provider_of(name)
            sub = subs.get(provider)
            if sub is None:
                sub = subs[provider] = menu.addMenu(
                    _icon(BASEMAP_ICONS[provider]), provider)
            sub.addAction(name).triggered.connect(
                partial(self.add_quick_basemap, name))
        self._basemap_menus.append(menu)
        return menu

    def add_quick_basemap(self, name, _checked=False):
        try:
            add_basemap_layer(name)
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not add basemap {}: {}'.format(
                    name, traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not add {}: {}'.format(name, exc))

    def remove_quick_template_menu(self):
        with suppress(Exception):
            menu = self.iface.layerMenu()
            for action in self._quick_layer_actions:
                menu.removeAction(action)
                action.deleteLater()
        self._quick_layer_actions = []
        for menu in self._basemap_menus:
            menu.deleteLater()
        self._basemap_menus = []

    def init_browser_paste(self):
        """Copy and Paste on the Browser panel's right-click menu."""
        try:
            self._browser_paste = BrowserPasteInstaller(self.iface)
            self._browser_paste.install()
        except Exception:
            # Never let this stop the rest of the plugin loading.
            self._browser_paste = None
            QgsMessageLog.logMessage(
                'Could not add Copy / Paste to the Browser:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def init_browser_package(self):
        """`.kgalp` layer packages, browsable like a database."""
        try:
            self._browser_package = BrowserPackageInstaller()
            self._browser_package.install()
        except Exception:
            self._browser_package = None
            QgsMessageLog.logMessage(
                'Could not add layer packages to the Browser:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def park_editor_tools(self):
        """Put the Editing Tools drop-down and Planarize Lines back on the
        KGA Toolbox toolbar when there is no KGA Editor toolbar to hold them."""
        tools = getattr(self, '_editor_tools', [])
        anchor = getattr(self, '_tools_anchor', None)
        if self.toolbar is None or anchor is None:
            return
        for tool in tools:
            if isinstance(tool, QAction):
                self.toolbar.insertAction(anchor, tool)
            else:
                self.toolbar.insertWidget(anchor, tool)

    def init_editor(self):
        """The KGA Editor toolbar: Edit Session and Create Features."""
        if self._editor is not None:
            return
        self._editor_tools_placed = False
        try:
            self._init_editor()
        finally:
            if not self._editor_tools_placed:
                self.park_editor_tools()

    def _init_editor(self):
        if Qgis.QGIS_VERSION_INT < EDITOR_MIN_QGIS:
            QgsMessageLog.logMessage(
                'The KGA Editor toolbar needs QGIS 3.44 or newer; this is '
                'QGIS {}.'.format(Qgis.QGIS_VERSION), LOG_TAG,
                Qgis.MessageLevel.Info)
            return
        clash = [name for name in EDITOR_CLASH_PLUGINS
                 if name in qgis.utils.plugins]
        if clash:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox',
                'The KGA Editor toolbar was not added because the stand-alone '
                'plugin "{}" is installed and does the same job. Uninstall it '
                'in Plugins > Manage and Install Plugins to use the KGA '
                'Editor.'.format(clash[0]))
            return
        try:
            from .editor.controller import EditorController
            # The editor checkbox goes in front of the separator that sets
            # Check for Update / About Us apart.
            before = getattr(self, '_update_separator', None)
            self._editor = EditorController(self.iface, self.menu, before, self.run_algorithm)
            self._editor.initGui()
            # The ten KGA editing tools are no longer fixed buttons: the editor shows the user's favorite tools
            # from the Modify Features pane (ten by default) instead. The actions built for them in
            # build_toolbar are only used when there is no editor to hold them (park_editor_tools).
            self._editor_tools_placed = True
            self._register_shortcut(self._editor.menu_action, SHORTCUT_EDITOR)
        except Exception:
            # Never let the editor stop the rest of the plugin loading.
            QgsMessageLog.logMessage(
                'Could not add the KGA Editor toolbar:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.remove_editor()

    def remove_editor(self):
        if self._editor is not None:
            self._unregister_shortcut(self._editor.menu_action)
        if self._editor is not None:
            with suppress(Exception):
                self._editor.unload()
            self._editor = None

    def init_layer_symbol_tab(self):
        """The KGA Layer Symbol tab of Layer Properties, under Symbology."""
        try:
            from .symbol_gallery.gui.layer_symbol_tab import (
                LayerSymbolTabFactory, RasterLayerSymbolTabFactory)
            vector_tab = LayerSymbolTabFactory(self.iface)
            self._layer_symbol_tabs = [vector_tab,
                                       RasterLayerSymbolTabFactory(self.iface, vector_tab.order)]
            for factory in self._layer_symbol_tabs:
                self.iface.registerMapLayerConfigWidgetFactory(factory)
        except Exception:
            self._layer_symbol_tabs = []
            QgsMessageLog.logMessage(
                'Could not add the KGA Layer Symbol tab:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
        try:
            from .symbol_gallery.gui.symbol_dock import SymbolDockController
            self._layer_symbol_dock = SymbolDockController(self.iface)
            self._layer_symbol_dock.install()
        except Exception:
            self._layer_symbol_dock = None
            QgsMessageLog.logMessage(
                'Could not add the KGA Layer Symbol panel button:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def init_callout_label_mode(self):
        """Callout Label Studio as a mode of Layer Properties > Labels."""
        try:
            from .gui.callout_label_tab import CalloutLabelHookFactory
            self._callout_label_hook = CalloutLabelHookFactory(self.iface)
            self.iface.registerMapLayerConfigWidgetFactory(self._callout_label_hook)
        except Exception:
            self._callout_label_hook = None
            QgsMessageLog.logMessage(
                'Could not add Callout Label Studio to the Labels tab:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_callout_label_mode(self):
        if self._callout_label_hook is not None:
            with suppress(Exception):
                self._callout_label_hook.uninstall()
            with suppress(Exception):
                self.iface.unregisterMapLayerConfigWidgetFactory(self._callout_label_hook)
        self._callout_label_hook = None

    def remove_layer_symbol_tab(self):
        dock = getattr(self, '_layer_symbol_dock', None)
        if dock is not None:
            with suppress(Exception):
                dock.uninstall()
        self._layer_symbol_dock = None
        for factory in getattr(self, '_layer_symbol_tabs', []):
            with suppress(Exception):
                factory.shutdown()
                self.iface.unregisterMapLayerConfigWidgetFactory(factory)
        self._layer_symbol_tabs = []

    def remove_browser_package(self):
        if self._browser_package is not None:
            with suppress(Exception):
                self._browser_package.uninstall()
            self._browser_package = None

    def remove_browser_paste(self):
        if self._browser_paste is not None:
            with suppress(Exception):
                self._browser_paste.uninstall()
            self._browser_paste = None

    def remove_layer_menu(self):
        if getattr(self, '_copy_coordinates', None) is not None:
            self._copy_coordinates.uninstall()
            self._copy_coordinates = None
        if getattr(self, '_filter_selected', None) is not None:
            self._filter_selected.uninstall()
            self._filter_selected = None
        if self._layer_menu is not None:
            self._layer_menu.uninstall()
            self._layer_menu.deleteLater()
            self._layer_menu = None

    def show_duplicate_layer(self):
        try:
            show_duplicate_layer(self.iface.activeLayer())
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open Duplicate to New Layer:\n{}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox',
                'Could not open Duplicate to New Layer: {}'.format(exc))

    # ---------------------------------------------------------------- toolbar

    def _register_shortcut(self, action, keys):
        """Give a menu action a keyboard shortcut QGIS lets the user rebind."""
        if action is None:
            return
        shortcuts = self.__dict__.setdefault('_shortcut_actions', [])
        if action in shortcuts:
            return
        with suppress(Exception):
            self.iface.registerMainWindowAction(action, keys)
            shortcuts.append(action)
            action.setToolTip('{} ({})'.format(action.toolTip(), keys))

    def _unregister_shortcut(self, action):
        shortcuts = self.__dict__.get('_shortcut_actions', [])
        if action is not None and action in shortcuts:
            shortcuts.remove(action)
            with suppress(Exception):
                self.iface.unregisterMainWindowAction(action)

    def _unregister_shortcuts(self):
        for action in self.__dict__.pop('_shortcut_actions', []):
            with suppress(Exception):
                self.iface.unregisterMainWindowAction(action)

    def _menu_divider(self):
        """Add a divider line to the KGA Toolbox menu and return its action.

        A plain QMenu separator is not painted at some display scalings on
        Windows, so the line is drawn by a small widget instead.
        """
        action = QWidgetAction(self.menu)
        action.setDefaultWidget(_DividerLine())
        action.setEnabled(False)
        self.menu.addAction(action)
        return action

    def build_toolbar(self):
        """Group the registered algorithms and make one dropdown per group."""
        standalone_icons = dict(STANDALONE_ALGS)
        if self._reveal is not None:
            self._reveal.deleteLater()
        self._reveal = window_reveal.OriginTracker()
        self.menu.aboutToHide.connect(self._reveal.menu_hidden)
        window_reveal.set_tracker(self._reveal)

        # Add Quick Template leads the toolbar. It is a window of its own, not
        # a Processing algorithm, so it is added here rather than promoted.
        quick = QAction(_icon('editing/quickTemplate.png'), 'Add Quick Template',
                        self.iface.mainWindow())
        quick.setToolTip('Add Quick Template - make an empty point, line or '
                         'polygon layer')
        quick.triggered.connect(self.show_quick_template)
        self.toolbar.addAction(quick)
        self.menu.addAction(quick)
        self._owned_widgets.append(quick)

        basemap = QToolButton(self.toolbar)
        basemap.setIcon(_icon('data_management/addToMap.png'))
        basemap.setText('Add Quick Basemap')
        basemap.setToolTip('Add Quick Basemap - add an OSM, Google or ESRI '
                           'basemap to the map')
        basemap.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        basemap.setMenu(self.build_basemap_menu(basemap))
        self.toolbar.addWidget(basemap)
        self._owned_widgets.append(basemap)

        basemap_item = self.menu.addMenu(self.build_basemap_menu(self.menu))
        basemap_item.setIcon(_icon('data_management/addToMap.png'))
        basemap_item.setText('Add Quick Basemap')

        clip_basemap = QAction(_icon('imagery_basemaps/clipBasemap.png'),
                               'Clip Basemap by Boundary',
                               self.iface.mainWindow())
        clip_basemap.setToolTip('Clip Basemap by Boundary - show a basemap '
                                'only inside a boundary')
        clip_basemap.triggered.connect(self.show_clip_basemap)
        self.toolbar.addAction(clip_basemap)
        self.menu.addAction(clip_basemap)
        self._owned_widgets.append(clip_basemap)

        zoom_location = QAction(_icon('navigation/zoomLocation.png'), 'Zoom to Location',
                                self.iface.mainWindow())
        zoom_location.setToolTip('Zoom to Location - go to a continent, '
                                 'country, province, district, commune, '
                                 'village or coordinate')
        zoom_location.triggered.connect(self.show_zoom_location)
        self.toolbar.addAction(zoom_location)
        self.menu.addAction(zoom_location)
        self._owned_widgets.append(zoom_location)

        go_to_xy = QAction(_icon('navigation/goToXY.png'), 'Go To XY',
                           self.iface.mainWindow())
        go_to_xy.setCheckable(True)
        go_to_xy.setToolTip('Go To XY - type a coordinate (meters, feet, '
                            'degrees, UTM or MGRS), zoom there and mark it '
                            'with a temporary point')
        go_to_xy.toggled.connect(self.toggle_go_to_xy)
        self.toolbar.addAction(go_to_xy)
        self.menu.addAction(go_to_xy)
        self._owned_widgets.append(go_to_xy)
        self._go_to_xy_action = go_to_xy

        # Layer Auto-Sort works in the background: no toolbar button, only a
        # menu to switch it off, change the order or sort on demand.
        if self._layer_sort is not None and self._layer_sort.sort_action is not None:
            ls = self._layer_sort
            sort_menu = self.menu.addMenu(ls.sort_action.icon(), 'Layer Auto-Sort')
            for act in (ls.sort_action, ls.auto_action, ls.settings_action):
                sort_menu.addAction(act)

        self.toolbar.addSeparator()
        self._menu_divider()

        self._editor_tools = []
        self._geoai_tools = []
        groups = {}
        promoted = {}
        companions = {name for names in STANDALONE_COMPANIONS.values()
                      for name in names}
        for alg in self.provider.algorithms():
            if alg.name() in standalone_icons or alg.name() in companions:
                promoted[alg.name()] = alg
            else:
                groups.setdefault(alg.group() or 'General', []).append(alg)

        # Promoted algorithms lead the toolbar as plain single-click buttons.
        for alg_name, icon_file in STANDALONE_ALGS:
            alg = promoted.get(alg_name)
            if alg is None:
                continue
            action = QAction(_icon(icon_file), alg.displayName(),
                             self.iface.mainWindow())
            action.setToolTip(alg.displayName())
            action.triggered.connect(partial(self.run_algorithm, alg.id()))
            # Divider between the export/import tool and the data-source tools.
            if alg_name == 'add_data':
                self.toolbar.addSeparator()
                self._menu_divider()
            extra = [promoted[n] for n in STANDALONE_COMPANIONS.get(alg_name, [])
                     if n in promoted]
            if extra:
                self._add_promoted_dropdown(alg_name, action, extra)
                continue
            self.toolbar.addAction(action)
            self.menu.addAction(action)
            self._owned_widgets.append(action)
        if promoted:
            self.toolbar.addSeparator()
            self._menu_divider()

        for group_name in ordered_groups(groups.keys()):
            group_icon = _icon(GROUP_ICONS.get(group_name))

            if group_name in MENU_ONLY_GROUPS:
                # Menu entry only: no toolbar button.
                submenu = self.menu.addMenu(group_icon, button_label(group_name))
                for alg in ordered_algorithms(group_name, groups[group_name]):
                    action = QAction(algorithm_icon(alg, group_icon),
                                     alg.displayName(), submenu)
                    action.setToolTip(alg.displayName())
                    action.triggered.connect(partial(self.run_algorithm, alg.id()))
                    submenu.addAction(action)
                continue

            to_editor = group_name == EDITOR_TOOLBAR_GROUP
            to_geoai = group_name == GEOAI_TOOLBAR_GROUP
            # Both put their tools on a toolbar of their own, as buttons.
            elsewhere = to_editor or to_geoai

            button = QToolButton(self.toolbar)
            button.setText(button_label(group_name))
            button.setToolTip(group_name)
            button.setIcon(group_icon)
            # Split button: clicking the icon runs the selected algorithm,
            # clicking the arrow beside it opens the group menu.
            button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            # Icon-only keeps the toolbar compact; the tooltip carries the name.
            # Without an icon the button would be blank, so keep text in that case.
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly if not group_icon.isNull()
                else Qt.ToolButtonStyle.ToolButtonTextOnly)

            menu = QMenu(button)
            submenu = self.menu.addMenu(group_icon, button_label(group_name))
            first_action = None
            for alg in ordered_algorithms(group_name, groups[group_name]):
                action = QAction(algorithm_icon(alg, group_icon),
                                 alg.displayName(),
                                 submenu if elsewhere else menu)
                action.setToolTip(alg.displayName())
                # Bind the id, not the instance: instances are replaced on refresh.
                action.triggered.connect(partial(self.run_algorithm, alg.id()))
                if elsewhere:
                    # These sit on the KGA Editor or KGA GeoAI toolbar as
                    # buttons of their own, not in this group's drop-down;
                    # they stay in the menu.
                    submenu.addAction(action)
                    own_action = QAction(action.icon(), alg.displayName(),
                                         self.iface.mainWindow())
                    own_action.setToolTip(alg.displayName())
                    own_action.triggered.connect(
                        partial(self.run_algorithm, alg.id()))
                    (self._editor_tools if to_editor
                     else self._geoai_tools).append(own_action)
                    self._owned_widgets.append(own_action)
                    continue
                menu.addAction(action)
                submenu.addAction(action)
                first_action = first_action or action

            button.setMenu(menu)
            # Picking from the menu makes that entry the button's default, so the
            # icon half re-runs it. Keep the group icon so the toolbar stays
            # readable whichever algorithm was used last.
            menu.triggered.connect(partial(self.set_default_action, button,
                                           group_icon))
            if first_action is not None:
                self.set_default_action(button, group_icon, first_action)

            if elsewhere:
                button.deleteLater()        # its tools went to their own toolbar
            else:
                self.toolbar.addWidget(button)
                self._owned_widgets.append(button)

        # --- Non-algorithm tools go here, as plain actions --------------------
        self._tools_anchor = self.toolbar.addSeparator()
        self._menu_divider()

        # Only the Geoprocessing panel's toggle goes here.
        if self.panel_action is not None:
            self._register_shortcut(self.panel_action, SHORTCUT_GEOPROCESSING)
            self.toolbar.addAction(self.panel_action)
            self.menu.addAction(self.panel_action)

        # The Catalog button sits right after KGA Geoprocessing.
        if self._catalog is not None and self._catalog.toggle_action is not None:
            self._register_shortcut(self._catalog.toggle_action, SHORTCUT_CATALOG)
            self.toolbar.addAction(self._catalog.toggle_action)
            self.menu.addAction(self._catalog.toggle_action)

        # Data Explorer sits right after the Catalog.
        if (self._data_ingest is not None
                and self._data_ingest.toggle_action is not None):
            self.toolbar.addAction(self._data_ingest.toggle_action)
            self.menu.addAction(self._data_ingest.toggle_action)

        # Panels and Toolbars Manager sits right after Data Explorer.
        if (self._ui_manager is not None
                and self._ui_manager.toggle_action is not None):
            self._register_shortcut(self._ui_manager.toggle_action,
                                    SHORTCUT_UI_MANAGER)
            self.toolbar.addAction(self._ui_manager.toggle_action)
            self.menu.addAction(self._ui_manager.toggle_action)

        # Layer Path Manager: its button is on the KGA Layer toolbar
        # (init_layer_toolbar); in the KGA menu it sits right after the Panels
        # and Toolbars Manager.
        if (self._path_manager is not None
                and self._path_manager.action is not None):
            self.menu.addAction(self._path_manager.action)

        # Layer Bag leads the KGA Layer toolbar (init_layer_toolbar); in the
        # KGA menu it sits right after the Layer Path Manager.
        if self._mini_panel is not None and self._mini_panel.action is not None:
            self._register_shortcut(self._mini_panel.action, SHORTCUT_MINI_PANEL)
            self.menu.addAction(self._mini_panel.action)

        # Tool Bag, Symbol Bag and Layer Colors share one "Bags" button (a drop-down)
        # on the KGA Layer toolbar (init_layer_toolbar), and one submenu in the KGA menu.
        bag_actions = []
        if self._tool_bag is not None and self._tool_bag.action is not None:
            bag_actions.append(self._tool_bag.action)
        if self._symbol_bag is not None:
            bag_actions += [a for a in (self._symbol_bag.toggle_action,
                                        self._symbol_bag.colors_action)
                            if a is not None]
        if self._label_bag is not None and self._label_bag.action is not None:
            bag_actions.append(self._label_bag.action)
        self._bag_actions = bag_actions
        if bag_actions:
            submenu = self.menu.addMenu(_icon('interface/toolBag.png'), 'Bags')
            for action in bag_actions:
                submenu.addAction(action)

        # Menu only (no toolbar button): Cursor Style Manager and its on/off switch.
        if self._cursor_style is not None and self._cursor_style.actions():
            cursor_menu = self.menu.addMenu(
                self._cursor_style.panel_action.icon(), 'Map Cursor')
            for action in self._cursor_style.actions():
                cursor_menu.addAction(action)

        # The KGA Editor checkbox is inserted in front of this separator.
        self._update_separator = self._menu_divider()

        # Menu only: settings are changed now and then, so no toolbar button.
        settings = QAction(QgsApplication.getThemeIcon('/mActionOptions.svg'),
                           'Settings', self.iface.mainWindow())
        settings.setToolTip('KGA Toolbox settings: default project location, '
                            'new project basemap, tool output')
        settings.triggered.connect(self.show_settings)
        self.menu.addAction(settings)
        self._owned_widgets.append(settings)

        # Menu only: an update check is occasional, so it gets no toolbar button.
        update = QAction(_icon('interface/checkForUpdate.png'), 'Check for Update',
                         self.iface.mainWindow())
        update.setToolTip('Check for a newer version of KGA Toolbox')
        update.triggered.connect(self.show_update_check)
        self.menu.addAction(update)
        self._owned_widgets.append(update)

        about = QAction(_icon('branding/kga_logo.png'), 'About Us',
                        self.iface.mainWindow())
        about.setToolTip('About Us - KGA Toolbox')
        about.triggered.connect(self.show_about)
        # Menu only: About Us is rarely needed, so it gets no toolbar button.
        self.menu.addAction(about)
        self._owned_widgets.append(about)

        # Windows that open out of the toolbar button the user just used.
        self._reveal.watch_toolbar(self.toolbar)

    def _add_promoted_dropdown(self, leader_name, leader_action, companions):
        """A promoted tool that shares its button with related tools.

        Same split button as the group drop-downs: the icon half runs the tool
        picked last (the leader to begin with), the arrow lists all of them.
        The KGA Toolbox menu gets the same entries as a submenu.
        """
        group_icon = leader_action.icon()
        button = QToolButton(self.toolbar)
        button.setText(leader_action.text())
        button.setToolTip(leader_action.text())
        button.setIcon(group_icon)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        menu = QMenu(button)
        submenu = self.menu.addMenu(group_icon, leader_action.text())

        menu.addAction(leader_action)
        submenu.addAction(leader_action)
        for alg in companions:
            action = QAction(algorithm_icon(alg, group_icon), alg.displayName(),
                             menu)
            action.setToolTip(alg.displayName())
            action.triggered.connect(partial(self.run_algorithm, alg.id()))
            menu.addAction(action)
            submenu.addAction(action)
        button.setMenu(menu)
        menu.triggered.connect(partial(self.set_default_action, button,
                                       group_icon))
        self.set_default_action(button, group_icon, leader_action)
        self.toolbar.addWidget(button)
        self._owned_widgets.extend((button, leader_action))

    def rebuild_toolbar(self):
        """Call after adding/removing algorithms during development."""
        if self.toolbar is None:
            return
        self.toolbar.clear()
        self.menu.clear()
        self._owned_widgets = []
        self.provider.refreshAlgorithms()
        self.build_toolbar()
        self.remove_editor()
        self.remove_layer_toolbar()
        self.remove_geoai_toolbar()
        self.init_editor()
        self.init_layer_toolbar()
        self.init_geoai_toolbar()
        if self.panel is not None:
            self.panel.refresh()

    # ----------------------------------------------------------------- actions

    def show_about(self):
        try:
            AboutDialog(self.iface.mainWindow()).exec()
        except Exception as exc:
            # Never let the About box be the thing that breaks the toolbar:
            # fall back to the behaviour it replaced.
            QgsMessageLog.logMessage(
                'Could not open the About dialog: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.open_website()

    def show_quick_template(self):
        try:
            show_quick_template()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open Add Quick Template: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox',
                'Could not open Add Quick Template: {}'.format(exc))

    def show_clip_basemap(self):
        try:
            show_clip_basemap()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open Clip Basemap by Boundary: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox',
                'Could not open Clip Basemap by Boundary: {}'.format(exc))

    def show_zoom_location(self):
        try:
            show_zoom_location()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open Zoom to Location: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox',
                'Could not open Zoom to Location: {}'.format(exc))

    def toggle_go_to_xy(self, on):
        """The Go To XY button: show or hide the bar over the map."""
        try:
            bar = get_go_to_xy_bar()
            if bar is not None:
                bar.button = self.toolbar.widgetForAction(self._go_to_xy_action)
            showing = set_go_to_xy_visible(on)
            bar = current_go_to_xy()
            if bar is not None and on:
                with suppress(TypeError):
                    bar.closed.disconnect(self._go_to_xy_closed)
                bar.closed.connect(self._go_to_xy_closed)
            if on and not showing:
                self._go_to_xy_closed()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open Go To XY: {}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open Go To XY: {}'.format(exc))
            self._go_to_xy_closed()

    def _go_to_xy_closed(self):
        action = getattr(self, '_go_to_xy_action', None)
        if action is not None and action.isChecked():
            action.blockSignals(True)
            action.setChecked(False)
            action.blockSignals(False)

    def remember_layout(self):
        """Snapshot the toolbars and panels, seeing through the start page."""
        window = self.iface.mainWindow()
        if self._start_page is not None and self._start_page.installed:
            with self._start_page.full_chrome():
                return ui_layout.remember(window)
        return ui_layout.remember(window)

    def reset_layout(self):
        """Switch every remembered toolbar and panel back on."""
        switched = ui_layout.reset(self.iface.mainWindow())
        if self._start_page is not None and self._start_page.installed:
            self._start_page.reapply_chrome()
        return switched

    def show_settings(self, page=None):
        try:
            SettingsDialog(
                self.iface.mainWindow(),
                layout_hooks=(self.remember_layout, self.reset_layout), page=page).exec()
            self.sync_start_page()
            if self._pro_selection is not None:
                self._pro_selection.refresh()
            if self._pro_layers is not None:
                self._pro_layers.refresh()
            if self._map_tabs is not None:
                self._map_tabs.refresh()
            layer_scope.refresh_all()
            if self._smooth_nav is not None:
                self._smooth_nav.refresh()
            if self._temp_pan is not None:
                self._temp_pan.refresh()
            if self._node_view is not None:
                self._node_view.refresh()
            if self._grid is not None:
                self._grid.refresh()
            if self._layer_toolbar is not None and self._layer_toolbar.magnify is not None:
                self._layer_toolbar.magnify.refresh()
            for magnify in (getattr(self, '_toolbar_magnify', None),
                            getattr(self._editor, 'magnify', None),
                            getattr(self._geoai_toolbar, 'magnify', None)):
                if magnify is not None:
                    magnify.refresh()
            create = getattr(self._editor, 'create', None)
            if create is not None and create.sketch_bar is not None:
                create.sketch_bar.refresh_settings()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open the Settings window: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open the Settings window: {}'.format(exc))

    def init_system_theme(self):
        """Open QGIS in the light or dark theme the computer is using."""
        try:
            if ks.follow_system_theme():
                system_theme.follow_system()
        except Exception:
            QgsMessageLog.logMessage(
                'Could not match the QGIS theme to the system: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def init_new_project_basemap(self):
        """Add the chosen basemap to every new project."""
        try:
            self._new_basemap = NewProjectBasemap(self.iface)
            self._new_basemap.install()
        except Exception:
            self._new_basemap = None
            QgsMessageLog.logMessage(
                'Could not set up the new project basemap: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def init_start_page(self):
        """Put the KGA start page where QGIS's welcome page is."""
        self.remove_start_page()
        if not ks.start_page_enabled():
            return
        try:
            self._start_page = StartPageInstaller(self.iface, {
                'settings': self.show_settings,
                'about': self.show_about,
                'new_workspace': self.start_new_workspace,
                'open_workspace': self._catalog_call('open_workspace_dialog'),
                'import_package': self._catalog_call('import_project_package'),
            })
            self._start_page.install()
        except Exception:
            self._start_page = None
            QgsMessageLog.logMessage(
                'Could not set up the start page: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def sync_start_page(self):
        """Follow the Settings switch without rebuilding a page that is right."""
        installed = self._start_page is not None and self._start_page.installed
        if ks.start_page_enabled() != installed:
            self.init_start_page()

    def remove_start_page(self):
        if self._start_page is not None:
            with suppress(Exception):
                self._start_page.uninstall()
            self._start_page.deleteLater()
            self._start_page = None

    def _catalog_call(self, name):
        """A start-page handler that runs a Catalog action, or None without one."""
        if self._catalog is None:
            return None
        return lambda: getattr(self._catalog, name)()

    def start_new_workspace(self):
        """A new empty project, then the Set Workspace window for it."""
        if self._catalog is None:
            return
        settle_blank_project()
        if self.iface.newProject():
            self._catalog.set_workspace_dialog()

    def init_pro_selection(self):
        """Cyan, Pro-style selection on the map canvas."""
        try:
            self._pro_selection = ProSelection(self.iface)
            self._pro_selection.install()
        except Exception:
            self._pro_selection = None
            QgsMessageLog.logMessage(
                'Could not set up the Pro-style selection: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_pro_selection(self):
        if self._pro_selection is not None:
            with suppress(Exception):
                self._pro_selection.uninstall()
            self._pro_selection.deleteLater()
            self._pro_selection = None

    def init_pro_layers(self):
        """Pro-style Contents look for the Layers panel."""
        try:
            self._pro_layers = ProLayers(self.iface)
            self._pro_layers.refresh()
        except Exception:
            self._pro_layers = None
            QgsMessageLog.logMessage(
                'Could not set up the Pro-style Layers panel: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def init_layer_modes(self):
        """List By Selection / List By Editing buttons above the layer tree (like Pro's Contents tabs)."""
        try:
            from .gui.layer_modes import LayerModes
            self._layer_modes = LayerModes(self.iface, editor=lambda: self._editor)
            if not self._layer_modes.install():
                self._layer_modes = None
        except Exception:
            self._layer_modes = None
            QgsMessageLog.logMessage(
                'Could not add the Layers panel selection / editing lists: {}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)

    def remove_layer_modes(self):
        if self._layer_modes is not None:
            with suppress(Exception):
                self._layer_modes.uninstall()
            self._layer_modes.deleteLater()
            self._layer_modes = None

    def remove_pro_layers(self):
        if self._pro_layers is not None:
            with suppress(Exception):
                self._pro_layers.uninstall()
            self._pro_layers = None

    def init_smooth_navigation(self):
        """Animated zoom and kinetic panning (Settings > Appearance)."""
        try:
            self._smooth_nav = SmoothNavigation(self.iface)
            self._smooth_nav.refresh()
        except Exception:
            self._smooth_nav = None
            QgsMessageLog.logMessage(
                'Could not set up smooth navigation: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_smooth_navigation(self):
        if self._smooth_nav is not None:
            with suppress(Exception):
                self._smooth_nav.uninstall()
            self._smooth_nav = None

    def init_temporary_pan(self):
        """Hold a key and left-drag to pan mid-digitizing (Settings > Appearance)."""
        try:
            self._temp_pan = TemporaryPan(self.iface)
            self._temp_pan.refresh()
        except Exception:
            self._temp_pan = None
            QgsMessageLog.logMessage(
                'Could not set up the temporary pan: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_temporary_pan(self):
        if self._temp_pan is not None:
            with suppress(Exception):
                self._temp_pan.uninstall()
            self._temp_pan = None

    def init_node_view(self):
        """Vertices and nodes on the map, like ArcGIS Pro (KGA Layer toolbar, right-click menu, Settings > Editor)."""
        try:
            self._node_view = NodeViewController(self.iface)
            self._node_view.install()
        except Exception:
            self._node_view = None
            QgsMessageLog.logMessage(
                'Could not set up the vertices and nodes view: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_node_view(self):
        if self._node_view is not None:
            with suppress(Exception):
                self._node_view.uninstall()
            self._node_view = None

    def init_grid(self):
        """Map grid like ArcGIS Pro (KGA Layer toolbar and status bar buttons; spacing and rotation)."""
        try:
            self._grid = GridController(self.iface, open_settings=lambda: self.show_settings('Editor'))
            self._grid.install()
        except Exception:
            self._grid = None
            QgsMessageLog.logMessage(
                'Could not set up the grid: {}'.format(traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_grid(self):
        if self._grid is not None:
            with suppress(Exception):
                self._grid.uninstall()
            self._grid = None

    def init_geometry_info(self):
        """Length / perimeter / area of the selected feature, in the status bar left of the node button."""
        try:
            self._geometry_info = GeometryInfoController(self.iface)
            self._geometry_info.install()
        except Exception:
            self._geometry_info = None
            QgsMessageLog.logMessage(
                'Could not set up the geometry info: {}'.format(traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_geometry_info(self):
        if self._geometry_info is not None:
            with suppress(Exception):
                self._geometry_info.uninstall()
            self._geometry_info = None

    def init_cursor_style(self):
        """Cursor Style Manager: custom map-canvas cursors (KGA Toolbox menu only)."""
        try:
            self._cursor_style = CursorStyleManager(self.iface)
            self._cursor_style.install()
        except Exception:
            self._cursor_style = None
            QgsMessageLog.logMessage(
                'Could not set up the Cursor Style Manager: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_cursor_style(self):
        if self._cursor_style is not None:
            with suppress(Exception):
                self._cursor_style.uninstall()
            self._cursor_style = None

    def init_map_tabs(self):
        """Map and layout tabs above the canvas (Settings > Appearance)."""
        try:
            self._map_tabs = MapTabs(self.iface)
            self._map_tabs.refresh()
        except Exception:
            self._map_tabs = None
            QgsMessageLog.logMessage(
                'Could not set up the map tabs: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

    def remove_map_tabs(self):
        if self._map_tabs is not None:
            with suppress(Exception):
                self._map_tabs.uninstall()
            self._map_tabs = None

    def remove_new_project_basemap(self):
        if self._new_basemap is not None:
            with suppress(Exception):
                self._new_basemap.uninstall()
            self._new_basemap.deleteLater()
            self._new_basemap = None

    def show_update_check(self):
        try:
            UpdateDialog(self.iface.mainWindow()).exec()
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open the update window: {}'.format(
                    traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open the update window: {}'.format(exc))

    def open_website(self):
        if not QDesktopServices.openUrl(QUrl(KGA_WEBSITE)):
            QgsMessageLog.logMessage(
                'Could not open {}'.format(KGA_WEBSITE), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open {}'.format(KGA_WEBSITE))

    def set_default_action(self, button, group_icon, action):
        """Make `action` what the icon half of `button` runs.

        Like QGIS's own grouped buttons (Select, etc.), the button takes on the
        icon and tooltip of the tool picked last, so the face always shows what
        a click will run. The group icon is only the fallback.
        """
        button.setDefaultAction(action)
        if action.icon().isNull() and not group_icon.isNull():
            button.setIcon(group_icon)

    def _run_from_panel(self, alg_id):
        """A tool picked in the Geoprocessing panel: its window just appears."""
        if self._reveal is not None:
            self._reveal.clear()
        self.run_algorithm(alg_id)

    def run_algorithm(self, alg_id, _retry=True):
        window_reveal.patch_processing()      # the parameters dialog opens out of its button too
        self._run_algorithm(alg_id, _retry)

    def _run_algorithm(self, alg_id, _retry=True):
        try:
            import processing
            alg = QgsApplication.processingRegistry().algorithmById(alg_id)

            # Record before running: "recent" should mean recently opened, not
            # recently completed, and the dialog may sit open for a long time.
            # Bookkeeping only, so a failure here must not stop the tool.
            try:
                gp.push_recent(alg_id)
                if self.panel is not None:
                    self.panel.refresh()
            except Exception:
                QgsMessageLog.logMessage(
                    'Could not update the recent list:\n{}'.format(
                        traceback.format_exc()),
                    LOG_TAG, Qgis.MessageLevel.Warning)

            # Algorithms that declare no parameters build their own dialog in
            # processAlgorithm(). Showing the Processing parameter dialog for
            # those puts an empty window with a Run button in front of it, so
            # execute them straight away and let their dialog come up.
            if alg is not None and not alg.parameterDefinitions():
                processing.run(alg_id, {})
            else:
                processing.execAlgorithmDialog(alg_id, {})
        except Exception as exc:
            detail = traceback.format_exc()
            if _retry:
                # The first launch after QGIS starts, or after the panel
                # rebuilt itself, has been seen failing where the very next
                # one works: Processing had not settled its algorithm objects
                # yet. Try once more on the next turn of the event loop before
                # bothering the user, and keep the traceback either way.
                QgsMessageLog.logMessage(
                    'First attempt to open {} failed, retrying:\n{}'.format(
                        alg_id, detail),
                    LOG_TAG, Qgis.MessageLevel.Warning)
                QTimer.singleShot(
                    0, partial(self.run_algorithm, alg_id, False))
                return
            QgsMessageLog.logMessage(
                'Could not open {}:\n{}'.format(alg_id, detail), LOG_TAG,
                Qgis.MessageLevel.Critical)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox',
                'Could not open {}: {}'.format(alg_id, exc))

    # ----------------------------------------------------------------- cleanup

    def unload(self):
        gc_relief.release()
        self._unregister_shortcuts()
        window_reveal.unload()
        panel_bubble.unload_reveals()
        if self._reveal is not None:
            self._reveal.deleteLater()
            self._reveal = None
        self.remove_attribute_bubble()
        self.remove_layer_toolbar()
        self.remove_geoai_toolbar()
        self.remove_label_bag()             # before the bag: its Labels bag holds a Symbols section too
        self.remove_layer_mini_panel()      # before the bag: its Colors section holds a bag widget
        self.remove_symbol_bag()
        self.remove_tool_bag()
        if self._bag_group is not None:
            self._bag_group.unload()
            self._bag_group = None
        self.remove_layer_sort()
        self.remove_path_manager()
        self.remove_ui_manager()
        self.remove_data_ingest()
        self.remove_start_page()
        self.remove_pro_selection()
        self.remove_smooth_navigation()
        self.remove_temporary_pan()
        self.remove_geometry_info()
        self.remove_grid()
        self.remove_node_view()
        self.remove_cursor_style()
        with suppress(Exception):
            layer_scope.uninstall()
        self.remove_map_tabs()
        if self._drag_chip is not None:
            with suppress(Exception):
                self._drag_chip.uninstall()
                self._drag_chip.deleteLater()
            self._drag_chip = None
        self.remove_layer_modes()
        self.remove_pro_layers()
        self.remove_new_project_basemap()
        self.remove_catalog()
        self.remove_editor()
        self.remove_layer_symbol_tab()
        self.remove_callout_label_mode()
        self.remove_layout_guides()
        self.remove_layer_menu()
        self.remove_quick_template_menu()
        self.remove_geometry_fields()
        self.remove_browser_paste()
        self.remove_browser_package()
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

        # Some algorithms put a window on the QGIS main window instead of
        # returning a layer: the Add Open Data window, the Imagery Downloader window, the Generate Contour for ROI and Generate River or Stream for ROI windows, the Multipart Line Repair window, the Sliver and Spike Cleaner window, the Strip Lines and Point Grid window, the Point to Geometry Converter window, the Reservoir Storage Analyzer window, the Add Grid to Layout
        # window, the Error Inspector window, the Duplicate Checker dialog, the Duplicate to New Layer dialog, the
        # Sequential Numbering dialog, the Copy-Paste Feature dialog and the
        # Modify Features dialogs. None is parented to
        # anything the plugin owns, so without this they survive the unload as
        # dead widgets — and the interactive ones would leave a map tool or a
        # rubber band on the canvas.
        from .algorithms import (
            add_data_alg,
            add_to_layer_package,
            add_grid_alg,
            buffer_features,
            callout_label_studio_alg,
            clip_features,
            construct_polygon,
            copy_parallel,
            copy_paste_feature,
            CreatePointsFromTable as create_points_from_table_alg,
            csv_to_dem_contour_qgis,
            dem_dsm_inspector_alg,
            detect_paddy_plots_imagery_alg,
            detect_roads_imagery_alg,
            divide_features,
            duplicate_checker,
            duplicate_to_new_layer,
            error_inspector,
            generate_contour_roi_alg,
            ExportToKML_With_Label as export_to_kml_alg,
            attributes_xlsx,
            gdb_gpkg,
            generate_stream_roi_alg,
            gpkg_to_shapefiles,
            highest_point_per_block_alg,
            satellite_imagery_archive_alg,
            imagery_downloader_alg,
            kga_layer_symbol_alg,
            layer_package,
            merge_features,
            multipart_line_repair_alg,
            planarize_line,
            point_to_geometry_alg,
            raster_format_converter,
            reservoir_storage_analyzer_alg,
            sequential_numbering_dialog,
            shapefiles_to_gpkg,
            sliver_spike_cleaner_alg,
            split_into_cogo_lines,
            strip_point_grid_alg,
            update_xy_fields,
        )
        for close in (add_data_alg.close_add_data,
                      add_to_layer_package.close_add_to_layer_package,
                      layer_package.close_layer_package_windows,
                      add_grid_alg.close_add_grid,
                      callout_label_studio_alg.close_callout_label_studio,
                      kga_layer_symbol_alg.close_kga_layer_symbol,
                      imagery_downloader_alg.close_imagery_downloader,
                      satellite_imagery_archive_alg.close_satellite_imagery_archive,
                      detect_roads_imagery_alg.close_detect_roads_imagery,
                      detect_paddy_plots_imagery_alg.close_detect_paddy_plots_imagery,
                      dem_dsm_inspector_alg.close_dem_dsm_inspector,
                      generate_contour_roi_alg.close_generate_contour_roi,
                      generate_stream_roi_alg.close_generate_stream_roi,
                      multipart_line_repair_alg.close_multipart_line_repair,
                      sliver_spike_cleaner_alg.close_sliver_spike_cleaner,
                      strip_point_grid_alg.close_strip_point_grid,
                      point_to_geometry_alg.close_point_to_geometry,
                      highest_point_per_block_alg.close_highest_point_per_block,
                      reservoir_storage_analyzer_alg.close_reservoir_storage,
                      error_inspector.close_error_inspector,
                      duplicate_checker.close_duplicate_checker,
                      duplicate_to_new_layer.close_duplicate_to_new_layer,
                      sequential_numbering_dialog.close_sequential_numbering,
                      copy_paste_feature.close_copy_paste_feature,
                      copy_parallel.close_copy_parallel,
                      csv_to_dem_contour_qgis.close_csv_dem,
                      create_points_from_table_alg.close_create_points,
                      gpkg_to_shapefiles.close_gpkg_to_shp,
                      shapefiles_to_gpkg.close_shp_to_gpkg,
                      gdb_gpkg.close_gdb_to_gpkg,
                      gdb_gpkg.close_gpkg_to_gdb,
                      attributes_xlsx.close_export_attributes,
                      attributes_xlsx.close_import_attributes,
                      export_to_kml_alg.close_kml_export,
                      raster_format_converter.close_raster_converter,
                      update_xy_fields.close_update_xy,
                      buffer_features.close_buffer_features,
                      split_into_cogo_lines.close_split_into_cogo_lines,
                      merge_features.close_merge_features,
                      planarize_line.close_planarize_lines,
                      divide_features.close_divide_features,
                      clip_features.close_clip_features,
                      construct_polygon.close_construct_polygon):
            try:
                close()
            except Exception as exc:
                QgsMessageLog.logMessage(
                    'Could not close a KGA window: {}'.format(exc), LOG_TAG,
                    Qgis.MessageLevel.Warning)

        with suppress(Exception):
            close_quick_template()
        with suppress(Exception):
            close_clip_basemap()
        with suppress(Exception):
            close_zoom_location()
        with suppress(Exception):
            close_go_to_xy()
        self._go_to_xy_action = None

        if self.panel is not None:
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
            self.panel_action = None

        if getattr(self, '_toolbar_magnify', None) is not None:
            with suppress(Exception):
                self._toolbar_magnify.remove()
            self._toolbar_magnify = None
        if self.toolbar is not None:
            self.toolbar.clear()
            # Removes the toolbar itself from the QGIS main window.
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

        if self.menu is not None:
            self.menu.clear()
            self.iface.mainWindow().menuBar().removeAction(self.menu.menuAction())
            self.menu.deleteLater()
            self.menu = None

        self._owned_widgets = []

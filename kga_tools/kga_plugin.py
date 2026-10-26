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
from qgis.core import Qgis, QgsApplication, QgsMessageLog
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
    algorithm_icon,
    button_label,
    icon as _icon,
    ordered_algorithms,
    ordered_groups,
)
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
from .gui.geoprocessing_panel import KgaGeoprocessingPanel
from .gui.duplicate_layer_dialog import show_duplicate_layer
from .gui.layout_guide_dock import LayoutGuideManager
from .gui.layer_menu import LayerMenuInjector
from .gui.layer_sort_controller import LayerSortController
from .gui.clip_basemap_dialog import close_clip_basemap, show_clip_basemap
from .gui.quick_template_dialog import (
    close_quick_template,
    show_quick_template,
)
from .gui.new_project_basemap import NewProjectBasemap
from .gui.map_tabs import MapTabs
from .gui.pro_layers import ProLayers
from .gui.pro_selection import ProSelection
from .gui.settings_dialog import SettingsDialog
from .gui.start_page import StartPageInstaller, settle_blank_project
from .gui.update_dialog import UpdateDialog
from .provider import KgaProvider

MENU_TITLE = '&KGA Toolbox'

# The KGA Editor toolbar is built on QGIS APIs that only exist from 3.44.
EDITOR_MIN_QGIS = 34400
# Every Editing Tools tool (Planarize Lines included) lives on the KGA Editor toolbar
# as buttons, right after its Vertex button.
EDITOR_TOOLBAR_GROUP = 'KGA Editing Tools'
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
EDITOR_CLASH_PLUGINS = ('edit_session', 'construction_sketch',
                        'arcgis_construction_sketch')

BASEMAP_ICONS = {'OpenStreetMap': 'osm.png', 'Google': 'GoogleMap.png',
                 'ESRI': 'esri.png'}


# Data Explorer already owns Ctrl+Alt+I.
SHORTCUT_GEOPROCESSING = 'Ctrl+Alt+G'
SHORTCUT_CATALOG = 'Ctrl+Alt+C'
SHORTCUT_EDITOR = 'Ctrl+Alt+E'


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
        self._layer_menu = None
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
        self._layer_sort = None
        self._new_basemap = None
        self._pro_selection = None
        self._pro_layers = None
        self._map_tabs = None
        self._start_page = None
        self._layout_guides = None
        self._geometry_fields_action = None

    # ------------------------------------------------------------------ setup

    def initGui(self):
        self.init_system_theme()
        self.init_processing()

        self.toolbar = self.iface.addToolBar('KGA Toolbox')
        self.toolbar.setObjectName('KGAToolsToolbar')
        self.toolbar.setToolTip('KGA Toolbox')

        self.menu = QMenu(MENU_TITLE, self.iface.mainWindow())
        self.iface.mainWindow().menuBar().insertMenu(
            self.iface.firstRightStandardMenu().menuAction(), self.menu)

        self.init_panel()
        self.init_catalog()
        self.init_new_project_basemap()
        self.init_pro_selection()
        self.init_pro_layers()
        self.init_map_tabs()
        self.init_start_page()
        self.init_data_ingest()
        self.init_layer_sort()
        self.build_toolbar()
        self.init_layer_menu()
        self.init_quick_template_menu()
        self.init_geometry_fields()
        self.init_browser_paste()
        self.init_browser_package()
        self.init_editor()
        self.init_layer_symbol_tab()
        self.init_callout_label_mode()
        self.init_layout_guides()
        self.init_basemaps()

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
        self.panel.algorithmTriggered.connect(self.run_algorithm)
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
        self.panel_action.setIcon(_icon('kga.png'))
        self.panel_action.setText('KGA Geoprocessing')
        self.panel_action.setToolTip('Show the KGA Geoprocessing panel')
        # toggleViewAction only shows the dock. Tabbed onto the Processing
        # Toolbox that leaves it behind whichever tab is in front, so the
        # button reads as doing nothing; raise it to the front tab as well.
        self.panel_action.triggered.connect(self.raise_panel)

    def init_catalog(self):
        """The Catalog panel (Project / Connections / Computer / Favorites)."""
        if Qgis.QGIS_VERSION_INT < CATALOG_MIN_QGIS:
            QgsMessageLog.logMessage(
                'The Catalog panel needs QGIS 3.36 or newer; this is QGIS '
                '{}.'.format(Qgis.QGIS_VERSION), LOG_TAG,
                Qgis.MessageLevel.Info)
            return
        try:
            self._catalog = CatalogController(self.iface)
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
                self.iface, catalog_getter=lambda: self._catalog)
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
            self.iface, self.show_duplicate_layer, self.iface.mainWindow())
        self._layer_menu.install()

    def init_quick_template_menu(self):
        """Add Quick Template Layer, first entry of the QGIS Layer menu."""
        try:
            menu = self.iface.layerMenu()
            first = menu.actions()[0] if menu.actions() else None
            action = QAction(_icon('quickTemplate.png'),
                             'Add Quick Template Layer',
                             self.iface.mainWindow())
            action.setToolTip('Make an empty point, line or polygon layer')
            action.triggered.connect(self.show_quick_template)
            separator = QAction(self.iface.mainWindow())
            separator.setSeparator(True)
            basemap = QAction(_icon('addToMap.png'), 'Add Quick Basemap',
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

        # Add Quick Template leads the toolbar. It is a window of its own, not
        # a Processing algorithm, so it is added here rather than promoted.
        quick = QAction(_icon('quickTemplate.png'), 'Add Quick Template',
                        self.iface.mainWindow())
        quick.setToolTip('Add Quick Template - make an empty point, line or '
                         'polygon layer')
        quick.triggered.connect(self.show_quick_template)
        self.toolbar.addAction(quick)
        self.menu.addAction(quick)
        self._owned_widgets.append(quick)

        basemap = QToolButton(self.toolbar)
        basemap.setIcon(_icon('addToMap.png'))
        basemap.setText('Add Quick Basemap')
        basemap.setToolTip('Add Quick Basemap - add an OSM, Google or ESRI '
                           'basemap to the map')
        basemap.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        basemap.setMenu(self.build_basemap_menu(basemap))
        self.toolbar.addWidget(basemap)
        self._owned_widgets.append(basemap)

        basemap_item = self.menu.addMenu(self.build_basemap_menu(self.menu))
        basemap_item.setIcon(_icon('addToMap.png'))
        basemap_item.setText('Add Quick Basemap')

        clip_basemap = QAction(_icon('clipBasemap.png'),
                               'Clip Basemap by Boundary',
                               self.iface.mainWindow())
        clip_basemap.setToolTip('Clip Basemap by Boundary - show a basemap '
                                'only inside a boundary')
        clip_basemap.triggered.connect(self.show_clip_basemap)
        self.toolbar.addAction(clip_basemap)
        self.menu.addAction(clip_basemap)
        self._owned_widgets.append(clip_basemap)

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
        groups = {}
        promoted = {}
        for alg in self.provider.algorithms():
            if alg.name() in standalone_icons:
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
                                 submenu if to_editor else menu)
                action.setToolTip(alg.displayName())
                # Bind the id, not the instance: instances are replaced on refresh.
                action.triggered.connect(partial(self.run_algorithm, alg.id()))
                if to_editor:
                    # These sit on the KGA Editor toolbar as buttons of their
                    # own, not in this group's drop-down; they stay in the menu.
                    submenu.addAction(action)
                    editor_action = QAction(action.icon(), alg.displayName(),
                                            self.iface.mainWindow())
                    editor_action.setToolTip(alg.displayName())
                    editor_action.triggered.connect(
                        partial(self.run_algorithm, alg.id()))
                    self._editor_tools.append(editor_action)
                    self._owned_widgets.append(editor_action)
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

            if to_editor:
                button.deleteLater()        # its tools went to the editor toolbar
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
        update = QAction(_icon('checkForUpdate.png'), 'Check for Update',
                         self.iface.mainWindow())
        update.setToolTip('Check for a newer version of KGA Toolbox')
        update.triggered.connect(self.show_update_check)
        self.menu.addAction(update)
        self._owned_widgets.append(update)

        about = QAction(_icon('kga_logo.png'), 'About Us',
                        self.iface.mainWindow())
        about.setToolTip('About Us - KGA Toolbox')
        about.triggered.connect(self.show_about)
        # Menu only: About Us is rarely needed, so it gets no toolbar button.
        self.menu.addAction(about)
        self._owned_widgets.append(about)

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
        self.init_editor()
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

    def show_settings(self):
        try:
            SettingsDialog(
                self.iface.mainWindow(),
                layout_hooks=(self.remember_layout, self.reset_layout)).exec()
            self.sync_start_page()
            if self._pro_selection is not None:
                self._pro_selection.refresh()
            if self._pro_layers is not None:
                self._pro_layers.refresh()
            if self._map_tabs is not None:
                self._map_tabs.refresh()
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

    def remove_pro_layers(self):
        if self._pro_layers is not None:
            with suppress(Exception):
                self._pro_layers.uninstall()
            self._pro_layers = None

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

    def run_algorithm(self, alg_id, _retry=True):
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
        self._unregister_shortcuts()
        self.remove_layer_sort()
        self.remove_data_ingest()
        self.remove_start_page()
        self.remove_pro_selection()
        self.remove_map_tabs()
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
        # returning a layer: the Add Open Data window, the Imagery Downloader window, the Generate Contour for ROI and Generate River or Stream for ROI windows, the Reservoir Storage Analyzer window, the Add Grid to Layout
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
            csv_to_dem_contour_qgis,
            divide_features,
            duplicate_checker,
            duplicate_to_new_layer,
            error_inspector,
            generate_contour_roi_alg,
            generate_stream_roi_alg,
            highest_point_per_block_alg,
            imagery_downloader_alg,
            kga_layer_symbol_alg,
            layer_package,
            merge_features,
            planarize_line,
            reservoir_storage_analyzer_alg,
            sequential_numbering_dialog,
            split_into_cogo_lines,
        )
        for close in (add_data_alg.close_add_data,
                      add_to_layer_package.close_add_to_layer_package,
                      layer_package.close_layer_package_windows,
                      add_grid_alg.close_add_grid,
                      callout_label_studio_alg.close_callout_label_studio,
                      kga_layer_symbol_alg.close_kga_layer_symbol,
                      imagery_downloader_alg.close_imagery_downloader,
                      generate_contour_roi_alg.close_generate_contour_roi,
                      generate_stream_roi_alg.close_generate_stream_roi,
                      highest_point_per_block_alg.close_highest_point_per_block,
                      reservoir_storage_analyzer_alg.close_reservoir_storage,
                      error_inspector.close_error_inspector,
                      duplicate_checker.close_duplicate_checker,
                      duplicate_to_new_layer.close_duplicate_to_new_layer,
                      sequential_numbering_dialog.close_sequential_numbering,
                      copy_paste_feature.close_copy_paste_feature,
                      copy_parallel.close_copy_parallel,
                      csv_to_dem_contour_qgis.close_csv_dem,
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

        if self.panel is not None:
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
            self.panel_action = None

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

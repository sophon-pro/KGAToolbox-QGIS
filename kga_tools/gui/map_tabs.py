# -*- coding: utf-8 -*-
"""Map Tabs: ArcGIS Pro style map and layout tabs in the QGIS main window.

Off by default; the switch is in Settings > Appearance
(`core/kga_settings.py`). Integrated from the standalone Map Tabs plugin.

How it works
------------
Maps
* Every "map" is a top-level group in the project layer tree, tagged with the
  custom property ``maptabs/is_map``. All layers of that map live inside it.
* Switching map tabs saves the old map's extent / CRS / rotation, turns the old
  group off and hides it in the Layers panel, then shows the new group and
  restores its view. New layers always land in the active map.
* Each map is mirrored as a map theme ("Map: <name>") so print layout map
  items can show any map.

Layouts
* QGIS normally opens a print layout in a separate Layout Designer window.
  The plugin moves that window into the main window's central area (the same
  stacked widget that holds the map canvas) and gives it a tab, so maps and
  layouts sit side by side in one tab bar, as in ArcGIS Pro.
* Because the designer now lives inside the main window, keyboard shortcuts
  that exist in both (Ctrl+S, Ctrl+Z, Delete...) would clash. The plugin
  temporarily switches off the clashing shortcuts of whatever is not on screen.

Everything is stored in the .qgz project itself, so a project opened without
the plugin still works: you just see one group per map.
"""
import json
import os
import uuid
from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QFont, QFontMetrics, QPalette
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMenuBar,
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QListWidget,
    QMessageBox,
    QVBoxLayout,
    QProxyStyle,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTabBar,
    QToolBar,
    QToolButton,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsLayerTreeRegistryBridge,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsMapThemeCollection,
    QgsPrintLayout,
    QgsProject,
    QgsRectangle,
    QgsSettings,
)

from ..branding import ICON_DIR
from ..branding import icon as branding_icon
from ..core import basemaps
from ..core import kga_settings as settings
from ..core import layout_import, map_import
from .layout_contents import LayoutContents
from .map_picker_dialog import MapPickerDialog
from .new_layout_dialog import ask as ask_new_layout

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

# Custom properties stored on each map group (saved in the project file)
PROP_MAP = "maptabs/is_map"
PROP_ID = "maptabs/id"
PROP_EXTENT = "maptabs/extent"
PROP_CRS = "maptabs/crs"
PROP_ROTATION = "maptabs/rotation"
PROP_THEME = "maptabs/theme"
PROP_OPEN = "maptabs/open"      # "0" = tab closed; the map stays in the Catalog

# Project entries
ENTRY_SCOPE = "MapTabs"
ENTRY_ACTIVE = "active"
ENTRY_THEMES = "themes"

THEME_PREFIX = "Map: "

# Tab keys
MAP_KEY = "m:"
LAYOUT_KEY = "l:"

# Panels (object names) the user keeps on layout tabs
SETTING_LAYOUT_PANELS = settings.PREFIX + "map_tabs_layout_panels"
SETTING_LAYOUT_BARS = settings.PREFIX + "map_tabs_layout_toolbars"

# version tag for QMainWindow.saveState()/restoreState() of the map workspace
STATE_VERSION = 0x4D54

# Layout Designer panels shown by default on a layout tab (object name, title)
# (the Items panel is replaced by our own Contents panel)
DESIGNER_PANELS = (("ItemDock", "Item Properties"),)


ACCENT = "#005a9e"      # ArcGIS Pro's blue


def _url(name):
    return os.path.join(ICON_DIR, name).replace("\\", "/")


def build_style(dark):
    """Style sheet for the tab strip: a solid blue active tab, as in Pro."""
    if dark:
        idle, hover, text, strip = "#2b2f33", "#383d42", "#e6e6e6", "#1f2225"
    else:
        idle, hover, text, strip = "#eef0f2", "#dde3e8", "#1e1e1e", "#f5f6f7"
    return """
QWidget#MapTabsStrip {{ background: {strip}; border-bottom: 2px solid {accent}; }}
QTabBar {{ background: transparent; border: 0px; }}
QTabBar::tab {{ background: {idle}; color: {text}; padding: 3px 10px;
    margin: 0px 1px 0px 0px; border: 0px; min-height: 20px; }}
QTabBar::tab:hover {{ background: {hover}; }}
QTabBar::tab:selected {{ background: {accent}; color: #ffffff; }}
QTabBar::close-button {{ image: url({close}); subcontrol-position: right;
    width: 12px; height: 12px; }}
QTabBar::close-button:selected {{ image: url({close_white}); }}
""".format(strip=strip, idle=idle, hover=hover, text=text, accent=ACCENT,
           close=_url("pro_tab_close.svg"), close_white=_url("pro_tab_close_white.svg"))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def is_map(node):
    """True if the layer tree node is a map group created by this plugin."""
    if node is None or not QgsLayerTree.isGroup(node):
        return False
    return str(node.customProperty(PROP_MAP, "")).lower() in ("1", "true")


def node_prop(node, key):
    value = node.customProperty(key)
    return "" if value is None else str(value)


def set_node_prop(node, key, value):
    """Set a custom property only if it changed (avoids needless signals)."""
    if node_prop(node, key) != value:
        node.setCustomProperty(key, value)


def map_id(group):
    return node_prop(group, PROP_ID)


def map_layer_names(project=None):
    """{map id: [layer names]} in tree order, for every map of the project."""
    root = (project or QgsProject.instance()).layerTreeRoot()
    return {map_id(g): [n.name() for n in g.findLayers()]
            for g in root.children() if is_map(g)}


def layout_panels():
    """Object names of the QGIS panels kept visible on layout tabs."""
    value = QgsSettings().value(SETTING_LAYOUT_PANELS, [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value]


def is_open(group):
    """True unless the map's tab was closed (the map itself is kept)."""
    return node_prop(group, PROP_OPEN) != "0"


def list_maps(project=None):
    """[(id, name, open)] for every map of the project, in tab order."""
    root = (project or QgsProject.instance()).layerTreeRoot()
    return [(map_id(g), g.name(), is_open(g))
            for g in root.children() if is_map(g)]


#: The running MapTabs (None while the feature is off) and who wants to hear
#: about changes to the list of maps (the Catalog).
_ACTIVE = []
_LISTENERS = []


def active():
    return _ACTIVE[0] if _ACTIVE else None


def active_map_layer_ids():
    """The ids of the layers inside the active map, or None while Map Tabs is off (every layer counts then)."""
    tabs = active()
    group = tabs._active_group() if tabs is not None else None
    if group is None:
        return None
    return {node.layer().id() for node in group.findLayers() if node.layer() is not None}


def add_listener(callback):
    if callback not in _LISTENERS:
        _LISTENERS.append(callback)


def remove_listener(callback):
    if callback in _LISTENERS:
        _LISTENERS.remove(callback)


_SIGNATURE = []


def _notify_if_changed():
    """Tell the listeners, but only when the maps or their layers changed."""
    signature = (list_maps(), sorted(map_layer_names().items()))
    if _SIGNATURE and _SIGNATURE[0] == signature:
        return
    _SIGNATURE[:] = [signature]
    _notify()


def _notify():
    for callback in list(_LISTENERS):
        with suppress(Exception):   # a listener must not break the tabs
            callback()


def make_timer(msec, slot):
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setInterval(msec)
    timer.timeout.connect(slot)
    return timer


def ptr(obj):
    """C++ address of a Qt object, to compare objects reliably."""
    return sip.unwrapinstance(obj)


def alive(obj):
    return obj is not None and not sip.isdeleted(obj)


class _CloseFilter(QObject):
    """Calls back just before the main window closes (QGIS then saves its state)."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        if event.type() == QEvent.Type.Close:
            self._callback()
        return False


class _WindowCloseFilter(QObject):
    """Calls back with the window, just before it closes."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        if event.type() == QEvent.Type.Close:
            self._callback(obj)
        return False


class _ReleaseFilter(QObject):
    """Calls back when the mouse is released on the tab bar (end of a drag)."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        if event.type() == QEvent.Type.MouseButtonRelease:
            QTimer.singleShot(0, self._callback)
        return False


class _ContextFilter(QObject):
    """Hands right-clicks on the main window's toolbar area to a callback."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        if event.type() == QEvent.Type.ContextMenu:
            return bool(self._callback(event))
        return False


# --------------------------------------------------------------------------
# plugin
# --------------------------------------------------------------------------
class _NoUnderlineStyle(QProxyStyle):
    """The main menu bar never underlines its mnemonics; neither should this one."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_UnderlineShortcut:
            return 0
        return super().styleHint(hint, option, widget, returnData)


class MapTabs:
    """Map and layout tabs above the map canvas."""

    def __init__(self, iface):
        self.iface = iface
        self.project = QgsProject.instance()

        self.dock = None
        self.tabbar = None
        self.add_btn = None
        self.layouts_btn = None
        self.convert_btn = None
        self._layouts_menu = None
        self._release_filter = None
        self._close_filter = None
        self._context_filter = None
        self._installed = False
        self.last_notes = []

        # workspace switching (map tabs <-> layout tabs)
        self._mode = "map"
        self._map_state = None
        self._map_menubar = True
        self._map_statusbar = True
        self._prepared = set()      # designer keys whose panels were set up
        self._menu_style = None
        self._contents = None       # LayoutContents dock (layout tabs)
        self._dirty_maps = set()    # maps whose layers were switched since the last theme sync
        self._layers_focus = ""      # map whose layers the Layers panel shows on a layout tab
        self._pending_map = None    # map chosen in the Add Map dialog, for the next frame
        self._hooked = set()        # layouts whose new map frames we adopt
        self._chrome = None         # designer menu/toolbars lent to the main window

        self._active_id = ""        # id of the active map
        self._shown = ""            # key of the tab on screen ("m:<id>" / "l:<key>")
        self._designers = {}        # key -> QgsLayoutDesignerInterface (embedded)
        self._suspended = []        # [(QAction, [QKeySequence])] silenced shortcuts
        self._stack = None          # QGIS central QStackedWidget
        self._canvas_page = None    # page of that stack holding the map canvas
        self._busy = False          # True while the plugin itself edits things
        self._legacy = False        # opened project that has content but no maps
        self._order_dirty = False   # tabs were dragged, tree order must follow
        self._connections = []

        self._structure_timer = make_timer(0, self._on_structure_changed)
        self._tabs_timer = make_timer(0, self._sync_tabs)
        self._rows_timer = make_timer(0, self._refresh_rows)
        self._theme_timer = make_timer(400, self._sync_themes)
        self._view_timer = make_timer(300, self._save_active_view)

    # ------------------------------------------------------------------ GUI
    @property
    def installed(self):
        return self._installed

    def refresh(self):
        """Follow the Settings: the switch and the layout options."""
        if settings.map_tabs():
            self.install()
            self.apply_settings()
        else:
            self.uninstall()

    def apply_settings(self):
        """Put the Settings > Map tabs options into effect in this session."""
        if not self._installed:
            return
        if not settings.map_tabs_dock_layouts() and self._designers:
            for key in list(self._designers):
                self._release_designer(key, show=True)
            self._show(MAP_KEY + self._active_id)
            self._sync_tabs()
        if settings.map_tabs_sync_themes():
            self._sync_themes()

    def install(self):
        if self._installed:
            return
        self._installed = True
        _ACTIVE[:] = [self]
        mw = self.iface.mainWindow()
        self._build_dock()

        root = self.project.layerTreeRoot()
        self._connect(self.iface.projectRead, self.reload)
        self._connect(self.iface.newProjectCreated, self.reload)
        self._connect(root.addedChildren, self._on_tree_added)
        self._connect(root.removedChildren, self._on_tree_removed)
        self._connect(root.nameChanged, self._on_tree_renamed)
        self._connect(root.visibilityChanged, self._on_visibility_changed)
        for signal in (root.visibilityChanged, root.addedChildren, root.removedChildren,
                       root.nameChanged):
            self._connect(signal, self._refresh_contents)
        self._connect(self.iface.mapCanvas().extentsChanged, self._on_extent_changed)

        model = self.iface.layerTreeView().model()
        self._connect(model.modelReset, self._schedule_rows)
        self._connect(model.layoutChanged, self._schedule_rows)
        self._connect(model.rowsInserted, self._schedule_rows)

        self._connect(self.iface.layoutDesignerOpened, self._on_designer_opened)
        self._connect(self.iface.layoutDesignerClosed, self._on_designer_closed)
        self._connect(self.project.layoutManager().layoutRenamed, self._schedule_tabs)

        stack = self._find_stack()
        if stack is not None:
            self._connect(stack.currentChanged, self._on_stack_changed)

        # QGIS saves its window layout on exit: make sure that is the map one
        self._close_filter = _CloseFilter(self._before_main_close, mw)
        mw.installEventFilter(self._close_filter)
        self._context_filter = _ContextFilter(self._layout_popup, mw)
        mw.installEventFilter(self._context_filter)

        QTimer.singleShot(0, self.reload)

    def _build_dock(self):
        mw = self.iface.mainWindow()
        self.dock = QDockWidget("Map Tabs", mw)
        self.dock.setObjectName("KgaMapTabsDock")
        self.dock.setTitleBarWidget(QWidget())  # no title bar: looks like a tab strip
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        bar = QWidget()
        bar.setObjectName("MapTabsStrip")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(4, 2, 4, 2)   # the 2px blue line sits below
        layout.setSpacing(2)

        self.tabbar = QTabBar()
        self.tabbar.setMovable(True)
        self.tabbar.setTabsClosable(True)
        self.tabbar.setExpanding(False)
        self.tabbar.setDocumentMode(True)
        self.tabbar.setUsesScrollButtons(True)
        # only as wide as its tabs, so the + button sits right after the last one
        self.tabbar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.tabbar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabbar.currentChanged.connect(self._on_tab_changed)
        self.tabbar.tabCloseRequested.connect(self._on_tab_close)
        self.tabbar.tabMoved.connect(self._on_tab_moved)
        self.tabbar.tabBarDoubleClicked.connect(self._on_tab_double_clicked)
        self.tabbar.customContextMenuRequested.connect(self._show_tab_menu)
        self._release_filter = _ReleaseFilter(self._apply_tab_order, self.tabbar)
        self.tabbar.installEventFilter(self._release_filter)

        self.add_btn = QToolButton()
        self.add_btn.setText("+")
        self.add_btn.setAutoRaise(True)
        self.add_btn.setToolTip("New map")
        self.add_btn.clicked.connect(self.new_map)

        self.layouts_btn = QToolButton()
        self.layouts_btn.setText("Layouts")
        self.layouts_btn.setIcon(QgsApplication.getThemeIcon("/mIconLayout.svg"))
        self.layouts_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.layouts_btn.setAutoRaise(True)
        self.layouts_btn.setToolTip("Open or create a print layout")
        self.layouts_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._layouts_menu = QMenu(self.layouts_btn)
        self._layouts_menu.aboutToShow.connect(self._fill_layouts_menu)
        self.layouts_btn.setMenu(self._layouts_menu)

        self.convert_btn = QPushButton("Organize this project into Map tabs")
        self.convert_btn.setToolTip(
            "Put the current layers into a map called 'Map' and start using tabs")
        self.convert_btn.clicked.connect(self.convert_project)

        layout.addWidget(self.tabbar)
        layout.addWidget(self.add_btn)
        layout.addWidget(self.convert_btn)
        layout.addStretch(1)
        layout.addWidget(self.layouts_btn)

        # keep the strip as thin as one row of tabs
        fm = QFontMetrics(self.tabbar.font())
        height = max(fm.height() + 14,
                     self.convert_btn.sizeHint().height(),
                     self.layouts_btn.sizeHint().height()) + 6
        bar.setFixedHeight(height)
        bar.setStyleSheet(build_style(bar.palette().window().color().lightness() < 128))
        self.dock.setWidget(bar)
        self.dock.setFixedHeight(height)
        mw.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock)

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        _ACTIVE[:] = []
        for timer in (self._structure_timer, self._tabs_timer, self._rows_timer,
                      self._theme_timer, self._view_timer):
            timer.stop()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []

        if self._close_filter is not None:
            self.iface.mainWindow().removeEventFilter(self._close_filter)
            self._close_filter = None
        if self._context_filter is not None:
            self.iface.mainWindow().removeEventFilter(self._context_filter)
            self._context_filter = None
        self._return_chrome()
        self._leave_layout_workspace()
        self._restore_shortcuts()
        for key in list(self._designers):
            self._release_designer(key, show=False)
        if alive(self._stack) and alive(self._canvas_page):
            self._stack.setCurrentWidget(self._canvas_page)

        # make every map group visible in the Layers panel again
        view = self.iface.layerTreeView()
        for node in self.project.layerTreeRoot().children():
            index = view.node2index(node)
            if index.isValid():
                view.setRowHidden(index.row(), index.parent(), False)

        if self._contents is not None and alive(self._contents):
            self._contents.set_layout(None)
            self.iface.mainWindow().removeDockWidget(self._contents)
            self._contents.deleteLater()
        self._contents = None

        if self.dock is not None:
            self.iface.mainWindow().removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _update_ui_state(self):
        has_maps = bool(self._map_groups())
        self.tabbar.setVisible(has_maps or bool(self._designers))
        self.add_btn.setVisible(has_maps)
        self.convert_btn.setVisible(not has_maps)

    # ------------------------------------------------------------- lookups
    def _root(self):
        return self.project.layerTreeRoot()

    def _map_groups(self):
        return [n for n in self._root().children() if is_map(n)]

    def _group_by_id(self, mid):
        if not mid:
            return None
        for group in self._map_groups():
            if map_id(group) == mid:
                return group
        return None

    def _active_group(self):
        return self._group_by_id(self._active_id)

    def _key_at(self, index):
        if index < 0:
            return ""
        return self.tabbar.tabData(index) or ""

    def _group_at(self, index):
        key = self._key_at(index)
        if not key.startswith(MAP_KEY):
            return None
        return self._group_by_id(key[len(MAP_KEY):])

    def _index_of(self, key):
        for i in range(self.tabbar.count()):
            if self.tabbar.tabData(i) == key:
                return i
        return -1

    def _unique_name(self, base, sep=""):
        names = {g.name() for g in self._map_groups()}
        if base not in names:
            return base
        i = 1
        while f"{base}{sep}{i}" in names:
            i += 1
        return f"{base}{sep}{i}"

    def _ensure_unique_ids(self):
        """Copy/paste of a map group in the Layers panel duplicates its id."""
        seen = set()
        for group in self._map_groups():
            mid = map_id(group)
            if not mid or mid in seen:
                mid = uuid.uuid4().hex
                group.setCustomProperty(PROP_ID, mid)
                group.removeCustomProperty(PROP_THEME)
            seen.add(mid)

    @staticmethod
    def _new_group_node(name):
        group = QgsLayerTreeGroup(name)
        group.setCustomProperty(PROP_MAP, "1")
        group.setCustomProperty(PROP_ID, uuid.uuid4().hex)
        return group

    def _ask_text(self, title, label, default):
        text, ok = QInputDialog.getText(
            self.iface.mainWindow(), title, label, QLineEdit.EchoMode.Normal, default)
        text = text.strip() if ok else ""
        return text or None

    def _ask_name(self, title, default):
        return self._ask_text(title, "Map name:", default)

    # --------------------------------------------------------- sync / load
    def reload(self, *args):
        """Rebuild everything from the project (on load / new project)."""
        self._active_id = self.project.readEntry(ENTRY_SCOPE, ENTRY_ACTIVE, "")[0]
        self._shown = MAP_KEY + self._active_id
        root = self._root()
        self._legacy = bool(root.children()) and not self._map_groups()
        self._sync_tabs()
        group = self._active_group()
        if group is not None:
            # QGIS already restored the canvas view from the project file
            self._activate(group, restore_view=False)

    def _schedule_tabs(self, *args):
        self._tabs_timer.start()

    def _sync_tabs(self):
        """Make the tab bar match the map groups and the embedded layouts."""
        self._ensure_unique_ids()
        self._prune_designers()
        all_maps = self._map_groups()
        if all_maps and not any(is_open(g) for g in all_maps):
            all_maps[0].removeCustomProperty(PROP_OPEN)     # never leave no tab
        maps = [g for g in all_maps if is_open(g)]
        active_found = any(map_id(g) == self._active_id for g in maps)
        map_icon = branding_icon("mapTab.png")
        layout_icon = QgsApplication.getThemeIcon("/mIconLayout.svg")

        self._busy = True
        self.tabbar.blockSignals(True)
        try:
            while self.tabbar.count():
                self.tabbar.removeTab(0)
            for group in maps:
                i = self.tabbar.addTab(map_icon, group.name())
                self.tabbar.setTabData(i, MAP_KEY + map_id(group))
                self.tabbar.setTabToolTip(
                    i, f"Map: {group.name()} — {len(group.findLayers())} layer(s)\n"
                       "✕ closes the tab; the map stays in Catalog > Maps\n"
                       "Double-click to rename, right-click for more")
            for key, designer in self._designers.items():
                layout = self._layout_of(designer)
                name = layout.name() if layout is not None else "Layout"
                i = self.tabbar.addTab(layout_icon, name)
                self.tabbar.setTabData(i, LAYOUT_KEY + key)
                self.tabbar.setTabToolTip(
                    i, f"Layout: {name}\n✕ closes the view only — the layout is kept "
                       "(reopen it from Layouts)")

            index = self._index_of(self._shown)
            if index < 0 and active_found:
                index = self._index_of(MAP_KEY + self._active_id)
            if index < 0 and self.tabbar.count():
                index = 0
            if index >= 0:
                self.tabbar.setCurrentIndex(index)
                self._shown = self.tabbar.tabData(index)
        finally:
            self.tabbar.blockSignals(False)
            self._busy = False

        if maps and not active_found:
            self._activate(maps[0], restore_view=True)
        else:
            self._refresh_rows()
        self._update_ui_state()
        self._theme_timer.start()
        QTimer.singleShot(0, self._fit_tabbar)
        _notify_if_changed()

    def _fit_tabbar(self):
        """Make the tab bar exactly as wide as its tabs (its size hint is wider)."""
        bar = self.tabbar
        if not alive(bar) or not bar.count():
            return
        # summed from the tabs' own hints: tabRect() would follow the current
        # (possibly too narrow, scrolled) width and never let the bar grow
        width = sum(bar.tabSizeHint(i).width() for i in range(bar.count()))
        bar.setMaximumWidth(width + 4)
        bar.updateGeometry()

    def _on_structure_changed(self):
        if self._busy:
            self._structure_timer.start()
            return
        root = self._root()
        maps = self._map_groups()
        strays = [n for n in root.children() if not is_map(n)]
        if strays:
            if maps:
                self._adopt(strays, self._active_group() or maps[0])
            elif not self._legacy and settings.map_tabs_auto_organize():
                self.convert_project()
                return
        self._sync_tabs()

    def _adopt(self, nodes, group):
        """Move nodes that sit at the tree root into the given map group."""
        first_layer = None
        self._busy = True
        try:
            for i, node in enumerate(nodes):
                group.insertChildNode(i, node.clone())
                if first_layer is None and QgsLayerTree.isLayer(node):
                    first_layer = node.layer()
            root = self._root()
            for node in nodes:  # clone first, then remove: layers stay in project
                root.removeChildNode(node)
        finally:
            self._busy = False
        if first_layer is not None:
            self.iface.layerTreeView().setCurrentLayer(first_layer)

    # ------------------------------------------------------------ activate
    def _activate(self, group, restore_view=True):
        self._busy = True
        try:
            self._active_id = map_id(group)
            for g in self._map_groups():
                g.setItemVisibilityChecked(map_id(g) == self._active_id)
            group.setExpanded(True)
            if restore_view:
                self._restore_view(group)
            self._refresh_rows()

            view = self.iface.layerTreeView()
            index = view.node2index(group)
            if index.isValid():
                view.setCurrentIndex(index)  # QGIS then inserts new layers here
            try:
                bridge = self.project.layerTreeRegistryBridge()
                bridge.setLayerInsertionPoint(
                    QgsLayerTreeRegistryBridge.InsertionPoint(group, 0))
            except (AttributeError, TypeError):
                pass
            self.project.writeEntry(ENTRY_SCOPE, ENTRY_ACTIVE, self._active_id)
        finally:
            self._busy = False

    def _refresh_rows(self, *args):
        """Hide every map group except the active one in the Layers panel."""
        view = self.iface.layerTreeView()
        if view is None:
            return
        shown = self._layers_focus if self._mode == "layout" and self._layers_focus \
            else self._active_id
        for group in self._map_groups():
            index = view.node2index(group)
            if index.isValid():
                view.setRowHidden(index.row(), index.parent(), map_id(group) != shown)

    def _schedule_rows(self, *args):
        self._rows_timer.start()

    # ----------------------------------------------------- extent / CRS
    def _save_view(self, group):
        if group is None:
            return
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        if extent.isEmpty():
            return
        set_node_prop(group, PROP_EXTENT, ",".join(
            repr(v) for v in (extent.xMinimum(), extent.yMinimum(),
                              extent.xMaximum(), extent.yMaximum())))
        crs = canvas.mapSettings().destinationCrs()
        if crs.isValid():
            set_node_prop(group, PROP_CRS, crs.authid() or "WKT:" + crs.toWkt())
        set_node_prop(group, PROP_ROTATION, repr(float(canvas.rotation())))

    def _save_active_view(self):
        if not self._busy and self._shown.startswith(MAP_KEY):
            self._save_view(self._active_group())

    @staticmethod
    def _stored_extent(group):
        parts = node_prop(group, PROP_EXTENT).split(",")
        if len(parts) != 4:
            return None
        try:
            rect = QgsRectangle(*[float(p) for p in parts])
        except ValueError:
            return None
        return None if rect.isEmpty() else rect

    @staticmethod
    def _stored_crs(group):
        text = node_prop(group, PROP_CRS)
        if not text:
            return None
        if text.startswith("WKT:"):
            crs = QgsCoordinateReferenceSystem.fromWkt(text[4:])
        else:
            crs = QgsCoordinateReferenceSystem(text)
        return crs if crs.isValid() else None

    def _restore_view(self, group):
        canvas = self.iface.mapCanvas()
        crs = self._stored_crs(group)
        if crs is not None and crs != self.project.crs():
            self.project.setCrs(crs)  # QGIS pushes the project CRS to the canvas
        extent = self._stored_extent(group)
        if extent is not None:
            canvas.setExtent(extent)
        try:
            rotation = float(node_prop(group, PROP_ROTATION) or 0)
        except ValueError:
            rotation = 0.0
        if rotation != canvas.rotation():
            canvas.setRotation(rotation)
        canvas.refresh()

    def _on_extent_changed(self):
        if not self._busy:
            self._view_timer.start()

    # ------------------------------------------------------ tree signals
    def _on_tree_added(self, node, index_from, index_to):
        if node.parent() is None:  # something was added directly to the root
            self._structure_timer.start()
        self._theme_timer.start()

    def _on_tree_removed(self, node, index_from, index_to):
        if node.parent() is None:
            self._structure_timer.start()
        self._theme_timer.start()

    def _on_tree_renamed(self, node, name):
        if is_map(node):
            self._structure_timer.start()
            self._theme_timer.start()

    def _on_visibility_changed(self, node):
        group = node
        while group is not None and not is_map(group):
            group = group.parent()
        if group is not None:
            self._dirty_maps.add(map_id(group))
        self._theme_timer.start()

    def _refresh_frames(self, group):
        """Redraw the layout map frames that show this map (its theme just changed)."""
        theme = node_prop(group, PROP_THEME)
        for layout in self.project.layoutManager().printLayouts():
            for item in layout.items():
                if isinstance(item, QgsLayoutItemMap) and item.followVisibilityPreset() \
                        and item.followVisibilityPresetName() == theme:
                    item.invalidateCache()
                    item.refresh()

    # -------------------------------------------------------- tab events
    def _on_tab_changed(self, index):
        if self._busy or index < 0:
            return
        key = self._key_at(index)
        if key.startswith(LAYOUT_KEY):
            self._show(key)
            return
        group = self._group_at(index)
        if group is None:
            return
        self._show(key)
        if map_id(group) != self._active_id:
            self._save_view(self._active_group())
            self._activate(group, restore_view=True)

    def _on_tab_close(self, index):
        key = self._key_at(index)
        if key.startswith(LAYOUT_KEY):
            self.close_layout(key)
        elif key.startswith(MAP_KEY):
            self.close_map(self._group_at(index))

    def _on_tab_double_clicked(self, index):
        key = self._key_at(index)
        if key.startswith(MAP_KEY):
            self.rename_map(index)
        elif key.startswith(LAYOUT_KEY):
            self.rename_layout(key)
        else:
            self.new_map()

    def _on_tab_moved(self, index_from, index_to):
        self._order_dirty = True  # applied when the mouse button is released

    def _apply_tab_order(self):
        if not self._order_dirty:
            return
        self._order_dirty = False
        order = [k[len(MAP_KEY):] for k in
                 (self._key_at(i) for i in range(self.tabbar.count()))
                 if k.startswith(MAP_KEY)]
        maps = self._map_groups()
        if [map_id(g) for g in maps] == order + [
                map_id(g) for g in maps if map_id(g) not in order]:
            return
        by_id = {map_id(g): g for g in maps}
        order += [map_id(g) for g in maps if map_id(g) not in order]  # closed maps
        root = self._root()
        self._busy = True
        try:
            clones = [by_id[mid].clone() for mid in order if mid in by_id]
            for clone in clones:
                root.addChildNode(clone)
            for group in maps:
                root.removeChildNode(group)
        finally:
            self._busy = False
        self._schedule_rows()

    def _show_tab_menu(self, pos):
        index = self.tabbar.tabAt(pos)
        key = self._key_at(index)
        menu = QMenu(self.tabbar)
        if key.startswith(LAYOUT_KEY):
            menu.addAction("Rename layout…", lambda: self.rename_layout(key))
            menu.addAction("Open in a separate window", lambda: self.float_layout(key))
            menu.addSeparator()
            menu.addAction("Close layout view", lambda: self.close_layout(key))
        else:
            menu.addAction("New map…", self.new_map)
            if index >= 0:
                menu.addAction("Rename…", lambda: self.rename_map(index))
                menu.addAction("Duplicate", lambda: self.duplicate_map(index))
                menu.addAction("New layout from this map…",
                               lambda: self.new_layout_from_map(self._group_at(index)))
                menu.addAction("Open in a new 2D map view",
                               lambda: self.open_in_view(index))
                menu.addSeparator()
                menu.addAction("Delete map…", lambda: self.delete_map(index))
        menu.exec(self.tabbar.mapToGlobal(pos))

    # ----------------------------------------------------- map commands
    def convert_project(self):
        """Wrap everything currently in the Layers panel into a map called 'Map'."""
        if self._map_groups():
            return
        root = self._root()
        nodes = list(root.children())
        group = self._new_group_node("Map")
        self._busy = True
        try:
            for node in nodes:
                group.addChildNode(node.clone())
            root.insertChildNode(0, group)
            for node in nodes:
                root.removeChildNode(node)
        finally:
            self._busy = False
        self._legacy = False
        self._save_view(group)
        self._active_id = map_id(group)
        self._shown = MAP_KEY + self._active_id
        self._sync_tabs()
        self._activate(group, restore_view=False)

    def _ask_new_map(self, default):
        """The New Map dialog: a name and a basemap (None to start empty).

        Returns (name, basemap name or "") or (None, "") when cancelled.
        """
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("New Map")
        form = QFormLayout(dialog)
        name = QLineEdit(default)
        name.selectAll()
        form.addRow("Map name:", name)
        combo = QComboBox()
        combo.addItem("None", "")
        for title, _url, _zmax in basemaps.BASEMAPS:
            if title not in basemaps.OVERLAYS:
                combo.addItem(title, title)
        form.addRow("Basemap:", combo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if not dialog.exec() or not name.text().strip():
            return None, ""
        return name.text().strip(), combo.currentData() or ""

    def new_map(self, ask=None):
        """Add a map; `ask` (default: the dialog) returns (name, basemap)."""
        if not self._map_groups():
            self.convert_project()
            return
        name, basemap = (ask or self._ask_new_map)(self._unique_name("Map"))
        if not name:
            return
        if self._shown.startswith(MAP_KEY):
            self._save_view(self._active_group())
        group = self._new_group_node(name)
        group.setItemVisibilityChecked(False)
        self._busy = True
        try:
            self._root().addChildNode(group)
        finally:
            self._busy = False
        self._save_view(group)  # a new map starts from the current view
        if basemap:
            try:
                basemaps.add_basemap_layer(basemap, self.project, group)
            except (KeyError, RuntimeError) as error:
                QMessageBox.warning(self.iface.mainWindow(), "New Map",
                                    f"The basemap could not be added: {error}")
        self._active_id = map_id(group)
        self._show(MAP_KEY + self._active_id)
        self._sync_tabs()
        self._activate(group, restore_view=False)

    def import_map(self, path=None, pick=None, database=""):
        """Import maps into this project as new, separate maps.

        `path` is a `.kgamap` shortcut, or a QGIS project: a project with map
        tabs offers its maps, and a traditional project comes in whole as one
        map named after the file. Vector data is copied into `database` (a
        GeoPackage) with its styles; `last_notes` says what happened to each
        layer. `path` and `pick` (a callable taking the map names and returning
        the chosen ones) stand in for the file and selection dialogs.
        """
        self.last_notes = []
        window = self.iface.mainWindow()
        if path is None:
            path, _filter = QFileDialog.getOpenFileName(
                window, "Import Map", os.path.dirname(self.project.fileName()),
                "Maps and projects (*.kgamap *.qgz *.qgs);;"
                "Map shortcuts (*.kgamap);;QGIS projects (*.qgz *.qgs)")
        if not path:
            return []
        wanted_id = ""
        if path.lower().endswith(".kgamap"):
            path, wanted_id, wanted_name = map_import.read_shortcut(path)
            same = os.path.normcase(os.path.normpath(path)) == os.path.normcase(
                os.path.normpath(self.project.fileName()))
            if same:                    # that map is already here: just show it
                group = self._group_by_id(wanted_id) or next(
                    (g for g in self._map_groups() if g.name() == wanted_name), None)
                if group is not None:
                    self.open_map(group)
                return []
        source = map_import.read_project(path)
        if source is None:
            QMessageBox.warning(window, "Import Map",
                                "The project could not be read:\n" + path)
            return []
        found = map_import.list_maps(source, path)
        if wanted_id:
            found = [(n, g) for n, g in found
                     if str(g.customProperty(map_import.PROP_ID, "")) == wanted_id] or found
        if not found:
            QMessageBox.information(window, "Import Map",
                                    "That project has no layers to import.")
            return []
        names = [name for name, _group in found]
        chosen = names if wanted_id else (pick or self._pick_maps)(names)
        if not chosen:
            return []
        if self._shown.startswith(MAP_KEY):
            self._save_view(self._active_group())
        if not self._map_groups():
            self.convert_project()
        added, cache = [], {}
        self._busy = True
        try:
            for name, group in found:
                if name not in chosen:
                    continue
                new, notes = map_import.import_group(
                    source, group, self.project, self._unique_name(name, " "),
                    database, cache)
                self.last_notes.extend(notes)
                if new is not None:
                    self._store_extent(new, replace=False)
                    self._root().addChildNode(new)
                    added.append(new)
        finally:
            self._busy = False
        if not added:
            QMessageBox.information(window, "Import Map",
                                    "None of the layers could be copied.")
            return []
        self._active_id = map_id(added[-1])
        self._show(MAP_KEY + self._active_id)
        self._sync_tabs()
        self._activate(added[-1], restore_view=True)
        return [g.name() for g in added]

    def new_import_group(self, name):
        """A map group, not yet in the project, to receive imported layers."""
        group = self._new_group_node(self._unique_name(name, " "))
        group.setItemVisibilityChecked(False)
        return group

    def _store_extent(self, group, replace=True):
        """Remember a view that frames the group's layers (unless it has one)."""
        if not replace and self._stored_extent(group) is not None:
            return
        layers = [n.layer() for n in group.findLayers() if n.layer() is not None]
        if not layers:
            return
        crs = self.project.crs()
        box = QgsRectangle()
        for layer in layers:
            extent = layer.extent()
            if layer.crs() != crs:
                with suppress(Exception):
                    extent = QgsCoordinateTransform(
                        layer.crs(), crs, self.project).transformBoundingBox(extent)
            if not extent.isEmpty():
                box.combineExtentWith(extent)
        if not box.isEmpty():
            box.scale(1.1)
            group.setCustomProperty(PROP_EXTENT, ",".join(repr(v) for v in (
                box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())))
            group.setCustomProperty(PROP_CRS, crs.authid() or "WKT:" + crs.toWkt())

    def add_import_group(self, group):
        """Add a filled map group as a new tab, without leaving the current map."""
        layers = [n.layer() for n in group.findLayers() if n.layer() is not None]
        if not layers:
            return False
        self._store_extent(group)
        if self._shown.startswith(MAP_KEY):
            self._save_view(self._active_group())
        if not self._map_groups():
            self.convert_project()
        self._busy = True
        try:
            self._root().addChildNode(group)
        finally:
            self._busy = False
        self._sync_tabs()
        return True

    def _pick_maps(self, names):
        """Ask which of the maps to import; [] when cancelled."""
        if len(names) == 1:
            return names
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("Import Map")
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        listing.addItems(names)
        listing.selectAll()
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if not dialog.exec():
            return []
        return [item.text() for item in listing.selectedItems()]

    def rename_map(self, index):
        self.rename_group(self._group_at(index))

    def rename_group(self, group):
        if group is None:
            return
        name = self._ask_name("Rename Map", group.name())
        if name and name != group.name():
            group.setName(name)  # nameChanged -> tabs and theme follow

    def duplicate_map(self, index):
        self.duplicate_group(self._group_at(index))

    def duplicate_group(self, group):
        if group is None:
            return
        if map_id(group) == self._active_id and self._shown.startswith(MAP_KEY):
            self._save_view(group)
        clone = group.clone()
        clone.setName(self._unique_name(group.name() + " copy", " "))
        clone.setCustomProperty(PROP_ID, uuid.uuid4().hex)
        clone.removeCustomProperty(PROP_THEME)
        clone.removeCustomProperty(PROP_OPEN)       # the copy opens its own tab
        clone.setItemVisibilityChecked(False)

        root = self._root()
        position = len(root.children())
        for i, node in enumerate(root.children()):
            if is_map(node) and map_id(node) == map_id(group):
                position = i + 1
                break
        self._busy = True
        try:
            root.insertChildNode(position, clone)
        finally:
            self._busy = False
        self._active_id = map_id(clone)
        self._show(MAP_KEY + self._active_id)
        self._sync_tabs()
        self._activate(clone, restore_view=True)

    def delete_map(self, index):
        self.delete_group(self._group_at(index))

    def group_by_id(self, mid):
        return self._group_by_id(mid)

    def close_map(self, group):
        """Close a map's tab. The map and its layers stay in the project."""
        if group is None or not is_open(group):
            return
        open_maps = [g for g in self._map_groups() if is_open(g)]
        if len(open_maps) < 2:
            return                      # one map tab always stays open
        position = [map_id(g) for g in open_maps].index(map_id(group))
        neighbour = (open_maps[position + 1] if position + 1 < len(open_maps)
                     else open_maps[position - 1])
        if map_id(group) == self._active_id and self._shown.startswith(MAP_KEY):
            self._save_view(group)
        group.setCustomProperty(PROP_OPEN, "0")
        if map_id(group) == self._active_id:
            self._active_id = map_id(neighbour)
            self._show(MAP_KEY + self._active_id)
            self._sync_tabs()
            self._activate(neighbour, restore_view=True)
        else:
            self._sync_tabs()

    def open_map(self, group):
        """Open (or just show) a map's tab; used by the Catalog's Maps folder."""
        if group is None:
            return
        if map_id(group) == self._active_id and self._shown.startswith(MAP_KEY):
            self._save_view(group)
        elif self._shown.startswith(MAP_KEY):
            self._save_view(self._active_group())
        group.removeCustomProperty(PROP_OPEN)
        self._active_id = map_id(group)
        self._show(MAP_KEY + self._active_id)
        self._sync_tabs()
        self._activate(group, restore_view=True)

    def delete_group(self, group):
        if group is not None:
            self.delete_groups([group])

    def delete_groups(self, groups):
        """Delete maps (and their layers) after a single question."""
        groups = [g for g in groups if g is not None]
        if not groups:
            return
        n_layers = len({n.layerId() for g in groups for n in g.findLayers()})
        if len(groups) == 1:
            title, what = "Delete Map", f"the map \u201c{groups[0].name()}\u201d"
        else:
            title = "Delete Maps"
            what = f"these {len(groups)} maps:\n" + "\n".join(g.name() for g in groups)
        answer = QMessageBox.question(
            self.iface.mainWindow(), title,
            f"Delete {what}?\n\n"
            f"Their {n_layers} layer(s) will be removed from the project "
            "unless they are also used in another map.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._busy = True
        try:
            for group in groups:
                if map_id(group) == self._active_id:
                    self._active_id = ""
                self._root().removeChildNode(group)
        finally:
            self._busy = False
        self._sync_tabs()

    def open_in_view(self, index):
        """Show a map in its own dockable 2D map view (side-by-side work)."""
        group = self._group_at(index)
        if group is None:
            return
        if map_id(group) == self._active_id and self._shown.startswith(MAP_KEY):
            self._save_view(group)
        theme = self._ensure_theme(group)

        canvas = None
        base = group.name()
        for i in range(1, 20):
            name = base if i == 1 else f"{base} ({i})"
            canvas = self.iface.createNewMapCanvas(name)
            if canvas is not None:
                break
        if canvas is None:
            QMessageBox.information(self.iface.mainWindow(), "Map Tabs",
                                    "Could not create a new map view.")
            return
        canvas.setTheme(theme)
        crs = self._stored_crs(group)
        if crs is not None:
            canvas.setDestinationCrs(crs)
        extent = self._stored_extent(group)
        if extent is not None:
            canvas.setExtent(extent)
        canvas.refresh()

    # -------------------------------------------- central area switching
    def _find_stack(self):
        """The QStackedWidget in QGIS's central area that holds the map canvas."""
        if alive(self._stack) and alive(self._canvas_page):
            return self._stack
        child = self.iface.mapCanvas()
        parent = child.parentWidget()
        while parent is not None:
            if isinstance(parent, QStackedWidget):
                self._stack, self._canvas_page = parent, child
                return parent
            child, parent = parent, parent.parentWidget()
        return None

    def _show(self, key):
        """Put the page of a tab (map canvas or a layout) on screen."""
        self._shown = key
        stack = self._find_stack()
        if stack is not None:
            self._busy = True
            try:
                designer = None
                if key.startswith(LAYOUT_KEY):
                    designer = self._designers.get(key[len(LAYOUT_KEY):])
                if alive(designer) and alive(designer.window()):
                    self._enter_layout_workspace()
                    self._lend_chrome(designer.window())
                    stack.setCurrentWidget(designer.window())
                    designer.window().show()
                    if self._contents is not None:
                        self._focus_layers(None)
                        self._contents.set_layout(self._layout_of(designer))
                    self._prepare_designer_panels(key[len(LAYOUT_KEY):], designer.window())
                else:
                    self._return_chrome()
                    stack.setCurrentWidget(self._canvas_page)
                    self._leave_layout_workspace()
            finally:
                self._busy = False
        self._update_shortcuts()
        index = self._index_of(key)
        if index >= 0 and index != self.tabbar.currentIndex():
            self.tabbar.blockSignals(True)
            self.tabbar.setCurrentIndex(index)
            self.tabbar.blockSignals(False)

    def _on_stack_changed(self, index):
        """QGIS itself switched the central page (e.g. back to the canvas)."""
        if self._busy or not alive(self._stack):
            return
        widget = self._stack.widget(index)
        if widget is None:
            return
        if alive(self._canvas_page) and ptr(widget) == ptr(self._canvas_page):
            if not self._shown.startswith(MAP_KEY):
                self._show(MAP_KEY + self._active_id)
            return
        key = self._key_for_window(widget)
        if key is not None:
            self._show(LAYOUT_KEY + key)

    # --------------------------------------------------------- layouts
    def _dock_layouts(self):
        return settings.map_tabs_dock_layouts()

    def _fill_layouts_menu(self):
        menu = self._layouts_menu
        menu.clear()
        icon = QgsApplication.getThemeIcon("/mIconLayout.svg")
        layouts = sorted(self.project.layoutManager().printLayouts(),
                         key=lambda lay: lay.name().lower())
        for layout in layouts:
            menu.addAction(icon, layout.name(),
                           lambda checked=False, lay=layout: self.open_layout(lay))
        if not layouts:
            empty = menu.addAction("(no layouts in this project yet)")
            empty.setEnabled(False)
        menu.addSeparator()
        new_action = menu.addAction(
            QgsApplication.getThemeIcon("/mActionNewLayout.svg"),
            "New layout from the current map…",
            lambda: self.new_layout_from_map(self._active_group()))
        new_action.setEnabled(self._active_group() is not None)
        menu.addAction("Layout Manager…", self.iface.showLayoutManager)

    def open_layout(self, layout):
        designer = self.iface.openLayoutDesigner(layout)
        if designer is not None and self._dock_layouts():
            QTimer.singleShot(0, lambda d=designer: self._embed(d))

    def new_layout_from_map(self, group):
        """Create a print layout whose map frame follows this map's theme."""
        if group is None:
            return
        manager = self.project.layoutManager()
        default = f"{group.name()} Layout"
        if manager.layoutByName(default) is not None:
            default = manager.generateUniqueTitle()
        answer = ask_new_layout(self.iface.mainWindow(), default)
        if answer is None:
            return
        name, options = answer
        if manager.layoutByName(name) is not None:
            QMessageBox.warning(self.iface.mainWindow(), "New Layout",
                                f"A layout called “{name}” already exists.")
            return
        if map_id(group) == self._active_id and self._shown.startswith(MAP_KEY):
            self._save_view(group)
        theme = self._ensure_theme(group)

        layout = QgsPrintLayout(self.project)
        layout.initializeDefaults()  # one A4 page
        layout.setName(name)
        layout_import.apply_page_options(layout, **options)
        page_size = layout.pageCollection().page(0).pageSize()
        margin = 10.0
        item = QgsLayoutItemMap(layout)
        item.attemptSetSceneRect(QRectF(margin, margin,
                                        page_size.width() - 2 * margin,
                                        page_size.height() - 2 * margin))
        crs = self._stored_crs(group)
        if crs is not None:
            item.setCrs(crs)
        item.setFollowVisibilityPreset(True)
        item.setFollowVisibilityPresetName(theme)
        item.setId(group.name())
        extent = self._stored_extent(group)
        if extent is not None:
            item.zoomToExtent(extent)
        layout.addLayoutItem(item)

        if not manager.addLayout(layout):
            QMessageBox.warning(self.iface.mainWindow(), "New Layout",
                                "QGIS could not add the layout.")
            return
        self.open_layout(layout)

    def rename_layout(self, key):
        designer = self._designers.get(key[len(LAYOUT_KEY):])
        if not alive(designer):
            return
        layout = self._layout_of(designer)
        if layout is None:
            return
        name = self._ask_text("Rename Layout", "Layout name:", layout.name())
        if not name or name == layout.name():
            return
        if self.project.layoutManager().layoutByName(name) is not None:
            QMessageBox.warning(self.iface.mainWindow(), "Rename Layout",
                                f"A layout called “{name}” already exists.")
            return
        layout.setName(name)  # layoutRenamed -> tabs follow

    def close_layout(self, key):
        """Close the designer (the layout itself stays in the project)."""
        designer = self._designers.get(key[len(LAYOUT_KEY):])
        if not alive(designer):
            self._sync_tabs()
            return
        try:
            designer.close()
        except AttributeError:
            designer.window().close()

    def float_layout(self, key):
        """Give a layout back its own window."""
        self._release_designer(key[len(LAYOUT_KEY):], show=True)
        self._show(MAP_KEY + self._active_id)
        self._sync_tabs()

    def _release_designer(self, key, show):
        designer = self._designers.pop(key, None)
        if self._chrome is not None and designer is not None and alive(designer) \
                and ptr(self._chrome["window"]) == ptr(designer.window()):
            self._return_chrome()
        self._restore_shortcuts()
        if not alive(designer) or not alive(designer.window()):
            return
        window = designer.window()
        # parent stays the main window, so QGIS keeps owning it
        window.setParent(self.iface.mainWindow(), Qt.WindowType.Window)
        if show:
            window.show()
            window.raise_()
            window.activateWindow()

    def _layout_of(self, designer):
        """The print layout (with a name) shown in a designer.

        designer.layout() is typed as the base QgsLayout, which has no name(),
        so look the same object up in the project's layout manager.
        """
        if not alive(designer):
            return None
        try:
            master = designer.masterLayout()
            if master is not None and hasattr(master, "name"):
                return master
        except AttributeError:
            pass
        base = designer.layout()
        if base is None:
            return None
        target = ptr(base)
        for layout in self.project.layoutManager().printLayouts():
            if ptr(layout) == target:
                return layout
        return None

    def _key_for_window(self, window):
        for key, designer in self._designers.items():
            if alive(designer) and alive(designer.window()) \
                    and ptr(designer.window()) == ptr(window):
                return key
        return None

    def _prune_designers(self):
        """Forget designers that were closed or moved out of the main window."""
        stack = self._find_stack()
        for key in list(self._designers):
            designer = self._designers[key]
            window = designer.window() if alive(designer) else None
            if not alive(window) or stack is None or window.parentWidget() is None \
                    or ptr(window.parentWidget()) != ptr(stack):
                del self._designers[key]

    def _on_designer_opened(self, designer):
        if self._dock_layouts():
            QTimer.singleShot(0, lambda d=designer: self._embed(d))

    def _embed(self, designer):
        """Move a Layout Designer window into the main window as a tab."""
        if not alive(designer):
            return
        window = designer.window()
        stack = self._find_stack()
        if not alive(window) or stack is None:
            return
        key = self._key_for_window(window)
        if key is None:
            key = uuid.uuid4().hex
            self._designers[key] = designer
            window.setParent(stack, Qt.WindowType.Widget)
            stack.addWidget(window)
        self._hook_add_map(window)
        window.installEventFilter(_WindowCloseFilter(self._designer_closing, window))
        layout = self._layout_of(designer)
        if layout is not None and ptr(layout) not in self._hooked:
            self._hooked.add(ptr(layout))
            layout.itemAdded.connect(self._on_item_added)
        self._shown = LAYOUT_KEY + key
        self._sync_tabs()
        self._show(LAYOUT_KEY + key)

    def _designer_closing(self, window):
        """A layout is closing while on screen: give its menu and toolbars back
        and go to the map first, so the main window is never left on an empty page."""
        key = self._key_for_window(window)
        if key is None:
            return
        if self._shown == LAYOUT_KEY + key:
            self._show(MAP_KEY + self._active_id)
        # the window lingers until Qt deletes it: forget it now so its tab goes away
        self._designers.pop(key, None)
        self._restore_shortcuts()
        self._update_shortcuts()
        self._schedule_tabs()

    def _on_designer_closed(self, *args):
        QTimer.singleShot(0, self._after_designer_closed)

    def _after_designer_closed(self):
        self._prune_designers()
        if self._shown.startswith(LAYOUT_KEY) \
                and self._shown[len(LAYOUT_KEY):] not in self._designers:
            self._show(MAP_KEY + self._active_id)
        self._sync_tabs()
        self._update_shortcuts()

    # ------------------------------------------------------- workspaces
    def _main_bars(self):
        """Toolbars and panels that belong to the QGIS main window itself."""
        mw = self.iface.mainWindow()
        main = ptr(mw)
        own = ptr(self.dock) if alive(self.dock) else None
        lent = set()
        if self._contents is not None and alive(self._contents):
            lent.add(ptr(self._contents))
        if self._chrome:
            lent |= {ptr(bar) for bar in self._chrome["bars"] + [self._chrome["holder"]]
                     if bar is not None}
        bars = []
        for cls in (QToolBar, QDockWidget):
            for widget in mw.findChildren(cls):
                parent = widget.parentWidget()
                if parent is not None and ptr(parent) == main and ptr(widget) != own \
                        and ptr(widget) not in lent:
                    bars.append(widget)
        return bars

    def _enter_layout_workspace(self):
        """Hide the map tools: a layout tab shows only the layout's own tools."""
        if self._mode == "layout":
            return
        mw = self.iface.mainWindow()
        self._map_state = mw.saveState(STATE_VERSION)
        self._map_menubar = not mw.menuBar().isHidden()
        self._map_statusbar = not mw.statusBar().isHidden()
        keep = set(layout_panels()) - {"Layers"}   # the Contents panel replaces it
        for bar in self._main_bars():
            bar.setVisible(bar.objectName() in keep)
        self._show_contents(True)
        mw.menuBar().hide()      # the designer has its own menu bar
        mw.statusBar().hide()    # ...and its own status bar (x / y in mm, page)
        if alive(self.dock):
            self.dock.show()
        self._mode = "layout"

    def _leave_layout_workspace(self):
        """Put the map workspace back exactly as it was."""
        if self._mode != "layout":
            return
        mw = self.iface.mainWindow()
        # remember which QGIS panels the user chose to keep on layout tabs
        keep = [bar.objectName() for bar in self._main_bars()
                if not bar.isHidden() and bar.objectName() and bar.objectName() != "Layers"]
        QgsSettings().setValue(SETTING_LAYOUT_PANELS, keep)
        self._show_contents(False)
        self._layers_focus = ""
        self._refresh_rows()
        if self._map_state is not None:
            mw.restoreState(self._map_state, STATE_VERSION)
        mw.menuBar().setVisible(self._map_menubar)
        mw.statusBar().setVisible(self._map_statusbar)
        if alive(self.dock):
            self.dock.show()
        self._mode = "map"

    def _lend_chrome(self, window):
        """Show the layout's menu bar and toolbars at the very top of the main window.

        Rows from the top: layout menu, layout toolbars, then the tab strip (with
        the Layers panel beside it). They are given back when the tab is left.
        """
        if self._chrome is not None:
            if ptr(self._chrome["window"]) == ptr(window):
                return
            self._return_chrome()
        mw = self.iface.mainWindow()
        win = ptr(window)
        top = Qt.ToolBarArea.TopToolBarArea
        bars = [(bar, window.toolBarBreak(bar)) for bar in window.findChildren(QToolBar)
                if bar.parentWidget() is not None and ptr(bar.parentWidget()) == win
                and window.toolBarArea(bar) == top]
        menubar = next((m for m in window.findChildren(QMenuBar)
                        if m.parentWidget() is not None and ptr(m.parentWidget()) == win), None)

        holder = None
        box = None
        if menubar is not None:
            holder = QToolBar("Layout menu", mw)
            holder.setObjectName("KgaLayoutMenuBar")
            holder.setMovable(False)
            holder.setFloatable(False)
            # no padding or bottom line: same slim row as the main menu bar
            holder.setStyleSheet(
                "QToolBar { border: 0px; padding: 0px; margin: 0px; spacing: 0px;"
                f" background: {mw.menuBar().palette().window().color().name()}; }}")
            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            menubar.setParent(box)
            lay.addWidget(menubar)
            menubar.show()
            self._match_main_menubar(menubar, mw)
            holder.addWidget(box)
            mw.addToolBar(top, holder)
        state = self._bar_state()
        visible = {ptr(bar): not bar.isHidden() for bar, _ in bars}

        def saved(bar):
            return state.get(bar.objectName() or bar.windowTitle())

        if any(saved(bar) for bar, _ in bars):
            # same rows, order and visibility as the user left them last time
            order = {ptr(bar): i for i, (bar, _) in enumerate(bars)}
            bars.sort(key=lambda item: (saved(item[0]) or [99, 0, 0])[:2]
                      + [order[ptr(item[0])]])
            rows = [(saved(bar) or [99])[0] for bar, _ in bars]
            bars = [(bar, i > 0 and rows[i] != rows[i - 1]) for i, (bar, _) in enumerate(bars)]
            for bar, _ in bars:
                if saved(bar):
                    visible[ptr(bar)] = bool(saved(bar)[2])
        first = True
        for bar, broke in bars:
            window.removeToolBar(bar)
            if broke or first:
                mw.addToolBarBreak(top)
            mw.addToolBar(top, bar)
            bar.setVisible(visible[ptr(bar)])
            first = False
        self._chrome = {"window": window, "bars": [b for b, _ in bars],
                        "breaks": [k for _, k in bars], "holder": holder,
                        "box": box, "menubar": menubar}

    @staticmethod
    def _bar_state():
        """{toolbar name: [row, x, visible]} remembered from earlier layout tabs."""
        try:
            data = json.loads(QgsSettings().value(SETTING_LAYOUT_BARS, "") or "{}")
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _save_bar_state(self, chrome):
        """Remember where the user put the layout toolbars and which are shown."""
        self.iface.mainWindow().layout().activate()   # geometry must be current
        state = self._bar_state()
        alive_bars = [bar for bar in chrome["bars"] if alive(bar)]
        ys = sorted({bar.geometry().y() for bar in alive_bars if not bar.isHidden()})
        for bar in alive_bars:
            name = bar.objectName() or bar.windowTitle()
            old = state.get(name, [99, 0, 1])
            if bar.isHidden():
                state[name] = [old[0], old[1], 0]
            else:
                state[name] = [ys.index(bar.geometry().y()), bar.geometry().x(), 1]
        QgsSettings().setValue(SETTING_LAYOUT_BARS, json.dumps(state))

    # ------------------------------------------------------ contents panel
    def _map_choices(self):
        return [(g, node_prop(g, PROP_THEME) or THEME_PREFIX + g.name())
                for g in self._map_groups()]

    def _show_contents(self, on):
        """The Contents panel stands in for the Layers panel on layout tabs."""
        mw = self.iface.mainWindow()
        if on:
            if self._contents is None or not alive(self._contents):
                self._contents = LayoutContents(
                    mw, self._map_choices, self._link_frame, self._frame_map_changed,
                    self._show_item_properties, self._focus_layers)
            # place both panels afresh every time: after the map workspace was restored
            # Qt may keep an empty column for docks it no longer knows where to put
            layers = next((d for d in mw.findChildren(QDockWidget)
                           if d.objectName() == "Layers" and d.parentWidget() is not None
                           and ptr(d.parentWidget()) == ptr(mw)), None)
            left = Qt.DockWidgetArea.LeftDockWidgetArea
            mw.removeDockWidget(self._contents)
            mw.addDockWidget(left, self._contents)
            self._contents.show()
            # the Layers panel sits right under Contents: map layers beside layout items
            if layers is not None:
                if layers.isFloating():
                    layers.setFloating(False)
                mw.removeDockWidget(layers)
                mw.addDockWidget(left, layers)
                mw.splitDockWidget(self._contents, layers, Qt.Orientation.Vertical)
                layers.show()
                self._contents.show()
                width = max(self._contents.width(), layers.width(), 240)
                mw.resizeDocks([self._contents, layers], [width, width],
                               Qt.Orientation.Horizontal)
                half = max(mw.height() // 2 - 40, 100)
                mw.resizeDocks([self._contents, layers], [half, half], Qt.Orientation.Vertical)
        elif self._contents is not None and alive(self._contents):
            self._contents.set_layout(None)
            self._contents.hide()

    def _unique_frame_name(self, layout, base, frame):
        taken = {i.id() for i in layout.items()
                 if isinstance(i, QgsLayoutItemMap) and ptr(i) != ptr(frame)}
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base} ({n})"
        return name

    def _link_frame(self, frame, group):
        """Show a map in a map frame and name the frame after the map."""
        names = {g.name() for g in self._map_groups()}
        theme = self._ensure_theme(group)
        frame.setFollowVisibilityPreset(True)
        frame.setFollowVisibilityPresetName(theme)
        if not frame.id() or frame.id() in names \
                or any(frame.id().startswith(n + " (") for n in names):
            layout = frame.layout()
            if layout is not None:
                frame.setId(self._unique_frame_name(layout, group.name(), frame))
        frame.invalidateCache()
        frame.refresh()

    def _focus_layers(self, group):
        """The Layers panel follows the map frame selected on the layout."""
        focus = map_id(group) if group is not None else ""
        if focus != self._layers_focus:
            self._layers_focus = focus
            self._refresh_rows()

    def _show_item_properties(self):
        """Bring the Layout Designer's Item Properties panel to the front."""
        chrome = self._chrome
        window = chrome["window"] if chrome else None
        if window is None or not alive(window):
            return
        win = ptr(window)
        for dock in window.findChildren(QDockWidget):
            if dock.objectName() == "ItemDock" and dock.parentWidget() is not None \
                    and ptr(dock.parentWidget()) == win:
                dock.show()
                dock.raise_()
                return

    def _frame_map_changed(self, group, frame):
        self._ensure_theme(group)
        frame.invalidateCache()
        frame.refresh()

    def _on_item_added(self, item):
        if isinstance(item, QgsLayoutItemLegend):
            self._adopt_legend(item)
        else:
            self._on_frame_added(item)

    def _frames_of(self, layout):
        """[(map frame, group it shows)] for the frames linked to one of our maps."""
        found = []
        choices = self._map_choices()
        for item in layout.items():
            if isinstance(item, QgsLayoutItemMap) and item.followVisibilityPreset():
                for group, theme in choices:
                    if theme == item.followVisibilityPresetName():
                        found.append((item, group))
        return found

    def _adopt_legend(self, legend, group=None):
        """A new legend lists the layers of one map, not of every map in the project."""
        layout = legend.layout()
        if layout is None or not self._map_groups():
            return
        frames = self._frames_of(layout)
        if not frames:
            return
        if group is None:
            selected = {ptr(i) for i in layout.selectedLayoutItems()}
            picked = [(f, g) for f, g in frames if ptr(f) in selected]
            groups = {map_id(g): g for _f, g in frames}
            if picked:
                group = picked[0][1]
            elif len(groups) == 1:
                group = next(iter(groups.values()))
            else:
                group = self.choose_map(None, self.iface.mainWindow())
                if group is None:
                    return
        frame = next((f for f, g in frames if g is group), None)
        legend.setAutoUpdateModel(False)
        root = legend.model().rootGroup()
        root.removeAllChildren()
        for child in group.children():
            root.addChildNode(child.clone())
        if frame is not None:
            legend.setLinkedMap(frame)
            legend.setLegendFilterByMapEnabled(True)
        legend.updateLegend()

    def _on_frame_added(self, item):
        """A map frame drawn in a layout shows the active map, under that map's name."""
        if not isinstance(item, QgsLayoutItemMap) or item.followVisibilityPreset():
            return
        group, self._pending_map = self._pending_map or self._active_group(), None
        if group is not None:
            self._link_frame(item, group)

    def _hook_add_map(self, window):
        """Make the designer's Add Map button ask which map to add."""
        action = self._find_action(window, ("mActionAddNewMap", "mActionAddMap"),
                                   ("add map", "add map..."))
        if action is not None and not action.property("kga_hooked"):
            action.setProperty("kga_hooked", True)
            if action.isCheckable():
                action.toggled.connect(lambda on, w=window: self._on_add_map(on, w))
            else:
                action.triggered.connect(lambda _c=False, w=window: self._on_add_map(True, w))

    @staticmethod
    def _find_action(window, names, texts):
        """A designer action by object name, else by its menu text."""
        for name in names:
            action = window.findChild(QAction, name)
            if action is not None:
                return action
        for action in window.findChildren(QAction):
            text = action.text().replace("&", "").replace("…", "...").strip().lower()
            if text in texts:
                return action
        return None

    def _on_add_map(self, on, window):
        if not on or not self._map_groups() or not alive(window):
            return
        group = self.choose_map(self._active_group(), window)
        if group is None:    # cancelled: back to the select tool
            self._pending_map = None
            select = self._find_action(window, ("mActionSelectMoveItem",),
                                       ("select/move item",))
            if select is not None:
                select.trigger()
        else:
            self._pending_map = group

    def choose_map(self, current, parent=None):
        """Show the map cards; returns the chosen map group or None."""
        entries = []
        collection = self.project.mapThemeCollection()
        for group in self._map_groups():
            theme = self._ensure_theme(group)
            layers = collection.mapThemeVisibleLayers(theme)
            crs = self._stored_crs(group) or self.project.crs()
            entries.append((group, layers, self._stored_extent(group), crs))
        dialog = MapPickerDialog(parent or self.iface.mainWindow(), entries, current)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def _refresh_contents(self, *args):
        if self._contents is not None and alive(self._contents) \
                and self._mode == "layout":
            self._contents.schedule()

    def _layout_popup(self, event):
        """Right-click on a layout tab's toolbars: list only the layout's own."""
        chrome = self._chrome
        if self._mode != "layout" or chrome is None or not alive(chrome["window"]):
            return False
        mw = self.iface.mainWindow()
        window = chrome["window"]
        menu = QMenu(mw)
        for bar in chrome["bars"]:
            if alive(bar):
                menu.addAction(bar.toggleViewAction())
        docks = [d for d in window.findChildren(QDockWidget)
                 if d.parentWidget() is not None and ptr(d.parentWidget()) == ptr(window)]
        if docks:
            menu.addSeparator()
            for dock in docks:
                menu.addAction(dock.toggleViewAction())
        if self._contents is not None and alive(self._contents):
            menu.addSeparator()
            menu.addAction(self._contents.toggleViewAction())
        menu.exec(event.globalPos())
        menu.deleteLater()
        return True

    def _match_main_menubar(self, menubar, mw):
        """Make the layout's menu bar look like the main window's one."""
        main = mw.menuBar()
        menubar.setEnabled(True)
        menubar.setFont(main.font())
        menubar.setPalette(main.palette())
        menubar.setAutoFillBackground(False)
        text = main.palette().color(QPalette.ColorRole.ButtonText).name()
        menubar.setStyleSheet(main.styleSheet() + f"QMenuBar {{ color: {text}; }}")
        height = main.sizeHint().height()
        if height > 0:
            menubar.setFixedHeight(height)
        menubar.setNativeMenuBar(False)
        self._menu_style = _NoUnderlineStyle(main.style().objectName())
        menubar.setStyle(self._menu_style)

    def _return_chrome(self):
        """Put the lent menu bar and toolbars back into their Layout Designer."""
        chrome, self._chrome = self._chrome, None
        if chrome is None:
            return
        mw = self.iface.mainWindow()
        window = chrome["window"]
        holder, box, menubar = chrome["holder"], chrome["box"], chrome["menubar"]
        window_ok = alive(window)
        self._save_bar_state(chrome)
        shown = {ptr(bar): not bar.isHidden() for bar in chrome["bars"] if alive(bar)}
        for bar, broke in zip(chrome["bars"], chrome["breaks"]):
            if not alive(bar):
                continue
            mw.removeToolBar(bar)
            if not window_ok:
                bar.deleteLater()
            else:
                if broke:
                    window.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
                window.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
                bar.setVisible(shown[ptr(bar)])
        if menubar is not None and alive(menubar):
            menubar.setStyle(None)
            menubar.setStyleSheet("")
            menubar.setPalette(QPalette())
            menubar.setFont(QFont())
            if window_ok:
                window.setMenuBar(menubar)
                menubar.show()
            else:
                menubar.setParent(None)
        if alive(holder):
            mw.removeToolBar(holder)
            holder.deleteLater()
        if alive(box):
            box.deleteLater()

    def _stack_with_properties(self, window, docks):
        """Extra panels (Khmer North Arrows, Layout Guides, ...) open as tabs of Item Properties."""
        for dock in docks:
            if dock.objectName() in ("ItemDock", "ItemsDock") or dock.property("kga_tabbed"):
                continue
            dock.setProperty("kga_tabbed", True)
            dock.visibilityChanged.connect(
                lambda visible, d=dock, w=window: visible and self._tab_with_properties(w, d))
            if not dock.isHidden():
                self._tab_with_properties(window, dock)

    def _tab_with_properties(self, window, dock):
        if not alive(window) or not alive(dock) or dock.isFloating():
            return
        win = ptr(window)
        props = next((d for d in window.findChildren(QDockWidget)
                      if d.objectName() == "ItemDock" and d.parentWidget() is not None
                      and ptr(d.parentWidget()) == win), None)
        if props is None or ptr(props) == ptr(dock):
            return
        if any(ptr(t) == ptr(dock) for t in window.tabifiedDockWidgets(props)):
            return
        window.tabifyDockWidget(props, dock)
        dock.raise_()

    def _prepare_designer_panels(self, key, window):
        """Make sure Item Properties is docked and visible on the right, with the
        other panels (north arrows, guides) tabbed beside it."""
        if key in self._prepared:
            return
        self._prepared.add(key)
        self._arrange_panels(window)
        win = ptr(window)
        self._stack_with_properties(window, [
            d for d in window.findChildren(QDockWidget)
            if d.parentWidget() is not None and ptr(d.parentWidget()) == win])

    def _arrange_panels(self, window):
        win = ptr(window)
        docks = [d for d in window.findChildren(QDockWidget)
                 if d.parentWidget() is not None and ptr(d.parentWidget()) == win]
        for dock in docks:
            if dock.isFloating():
                dock.setFloating(False)
            if dock.objectName() == "ItemsDock":
                dock.hide()    # our Contents panel lists the items
        if any(not dock.isHidden() for dock in docks):
            return  # the user already has panels arranged
        wanted = []
        for name, title in DESIGNER_PANELS:
            match = next((d for d in docks if d.objectName() == name), None) \
                or next((d for d in docks if d.windowTitle() == title), None)
            if match is not None:
                wanted.append(match)
        if not wanted:
            wanted = [d for d in docks if d.objectName() != "ItemsDock"][:1]
        if not wanted:
            return
        for dock in wanted:
            if window.dockWidgetArea(dock) == Qt.DockWidgetArea.NoDockWidgetArea:
                window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            dock.show()
        for other in wanted[1:]:
            window.tabifyDockWidget(wanted[0], other)
        wanted[0].raise_()

    def _before_main_close(self):
        if self._mode == "layout":
            self._show(MAP_KEY + self._active_id)

    # ------------------------------------------------------- shortcuts
    def _restore_shortcuts(self):
        for action, shortcuts in self._suspended:
            if alive(action):
                action.setShortcuts(shortcuts)
        self._suspended = []

    def _suspend(self, action):
        shortcuts = action.shortcuts()
        if shortcuts:
            self._suspended.append((action, shortcuts))
            action.setShortcuts([])

    def _in_other_window(self, action):
        """True if the action belongs to a separate top-level window."""
        main = ptr(self.iface.mainWindow())
        obj = action.parent()
        while obj is not None:
            if isinstance(obj, QWidget) and obj.isWindow():
                return ptr(obj) != main
            obj = obj.parent()
        return False

    def _update_shortcuts(self):
        """Only the page on screen keeps shortcuts that would otherwise clash."""
        self._restore_shortcuts()
        self._prune_designers()
        if not self._designers:
            return
        designer_actions = {}
        designer_ptrs = set()
        for key, designer in self._designers.items():
            actions = designer.window().findChildren(QAction)
            designer_actions[key] = actions
            designer_ptrs.update(ptr(a) for a in actions)

        shown = self._shown[len(LAYOUT_KEY):] if self._shown.startswith(LAYOUT_KEY) else None
        for key, actions in designer_actions.items():
            if key != shown:
                for action in actions:
                    self._suspend(action)
        if shown not in designer_actions:
            return

        taken = {seq.toString() for a in designer_actions[shown] for seq in a.shortcuts()}
        taken.discard("")
        if not taken:
            return
        for action in self.iface.mainWindow().findChildren(QAction):
            if ptr(action) in designer_ptrs or self._in_other_window(action):
                continue
            if any(seq.toString() in taken for seq in action.shortcuts()):
                self._suspend(action)

    # ------------------------------------------------------- map themes
    @staticmethod
    def _theme_record(group):
        """A theme showing the layers that are switched on inside this map."""
        record = QgsMapThemeCollection.MapThemeRecord()
        layer_records = []
        for node in group.findLayers():
            layer = node.layer()
            if layer is None:
                continue
            visible = node.itemVisibilityChecked()
            parent = node.parent()
            while visible and parent is not None and not is_map(parent):
                visible = parent.itemVisibilityChecked()
                parent = parent.parent()
            if not visible:
                continue
            layer_record = QgsMapThemeCollection.MapThemeLayerRecord(layer)
            layer_record.usingCurrentStyle = True
            layer_record.currentStyle = layer.styleManager().currentStyle()
            layer_records.append(layer_record)
        record.setLayerRecords(layer_records)
        return record

    def _ensure_theme(self, group):
        collection = self.project.mapThemeCollection()
        name = THEME_PREFIX + group.name()
        old = node_prop(group, PROP_THEME)
        if old and old != name and collection.hasMapTheme(old) \
                and not collection.hasMapTheme(name):
            collection.renameMapTheme(old, name)  # keeps print layouts linked
        record = self._theme_record(group)
        if collection.hasMapTheme(name):
            try:
                unchanged = collection.mapThemeState(name) == record
            except TypeError:
                unchanged = False
            if not unchanged:
                collection.update(name, record)
        else:
            collection.insert(name, record)
        set_node_prop(group, PROP_THEME, name)
        return name

    def _sync_themes(self):
        _notify_if_changed()
        if not settings.map_tabs_sync_themes():
            return
        collection = self.project.mapThemeCollection()
        wanted = [self._ensure_theme(g) for g in self._map_groups()]
        dirty, self._dirty_maps = self._dirty_maps, set()
        for group in self._map_groups():
            if map_id(group) in dirty:
                self._refresh_frames(group)
        owned = self.project.readListEntry(ENTRY_SCOPE, ENTRY_THEMES, [])[0]
        for name in owned:  # only remove themes this plugin created
            if name not in wanted and collection.hasMapTheme(name):
                collection.removeMapTheme(name)
        if sorted(owned) != sorted(wanted):
            self.project.writeEntry(ENTRY_SCOPE, ENTRY_THEMES, wanted)

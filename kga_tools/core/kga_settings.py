# -*- coding: utf-8 -*-
"""The KGA Toolbox settings: one place, one set of QgsSettings keys.

Everything here reads and writes `QgsSettings` under `KGA/settings/`, so the
values are per QGIS profile. Each getter returns the factory default until the
user changes it, and no function touches a widget, so the Settings window, the
catalog and the tools all ask the same place.
"""

from qgis.core import QgsSettings

PREFIX = 'KGA/settings/'

#: Folder new workspaces are offered in; '' means Documents\KGA Toolbox Project.
KEY_PROJECTS_FOLDER = PREFIX + 'projects_folder'
#: Add a basemap to every new project.
KEY_BASEMAP_ENABLED = PREFIX + 'new_project_basemap_enabled'
KEY_BASEMAP_NAME = PREFIX + 'new_project_basemap'
#: Tool output starts in the project's default database / folder.
KEY_OUTPUT_TO_PROJECT = PREFIX + 'output_to_project'
#: Shape_Length / Shape_Area on line and polygon layers of the project databases.
KEY_GEOMETRY_FIELDS = PREFIX + 'geometry_fields'
#: Ask to convert a traditional project to a workspace when it is opened
#: (kept under its original key, which the open guard already uses).
KEY_ASK_CONVERT = 'KGA/catalog/ask_convert'
#: What Save / Save As do with a project that is not in a workspace.
KEY_SAVE_MODE = PREFIX + 'save_mode'
SAVE_ASK = 'ask'
SAVE_WORKSPACE = 'workspace'
SAVE_TRADITIONAL = 'traditional'
#: What happens to scratch (memory) layers when the project is closed.
KEY_SCRATCH_ON_CLOSE = PREFIX + 'scratch_on_close'
SCRATCH_ASK = 'ask'
SCRATCH_PROJECT = 'project'     # add them to the project GeoPackage without asking
SCRATCH_OFF = 'off'             # leave them to QGIS (they are lost)
#: Match the QGIS light/dark theme to the operating system when QGIS starts.
KEY_FOLLOW_SYSTEM_THEME = PREFIX + 'follow_system_theme'
#: Selected features in ArcGIS Pro's cyan outline instead of QGIS's yellow.
KEY_BAG_ANIMATION = PREFIX + 'bag_animation'
BAG_GENIE = 'genie'
BAG_CLASSIC = 'classic'
#: Tool windows (Create Layer Package first) open out of their toolbar button.
KEY_WINDOW_REVEAL = PREFIX + 'window_reveal'
KEY_PRO_SELECTION = PREFIX + 'pro_selection'
#: Layers panel dressed like ArcGIS Pro's Contents pane.
KEY_PRO_LAYERS = PREFIX + 'pro_layers'
#: Map and layout tabs above the canvas (off: it reorganises a project's layers).
KEY_MAP_TABS = PREFIX + 'map_tabs'
KEY_MAP_TABS_AUTO_ORGANIZE = PREFIX + 'map_tabs_auto_organize'
KEY_LAYER_LISTS_CURRENT_MAP = PREFIX + 'layer_lists_current_map'
KEY_MAP_TABS_DOCK_LAYOUTS = PREFIX + 'map_tabs_dock_layouts'
KEY_MAP_TABS_SYNC_THEMES = PREFIX + 'map_tabs_sync_themes'
#: ArcGIS Pro-style navigation: animated wheel zoom, kinetic pan, arrow keys.
KEY_SMOOTH_NAV = PREFIX + 'smooth_navigation'
KEY_SMOOTH_NAV_ZOOM = PREFIX + 'smooth_navigation_zoom'
KEY_SMOOTH_NAV_ZOOM_MS = PREFIX + 'smooth_navigation_zoom_ms'
KEY_SMOOTH_NAV_INERTIA = PREFIX + 'smooth_navigation_inertia'
KEY_SMOOTH_NAV_GLIDE_MS = PREFIX + 'smooth_navigation_glide_ms'
KEY_SMOOTH_NAV_KEYBOARD = PREFIX + 'smooth_navigation_keyboard'
KEY_SMOOTH_NAV_RIGHT_ZOOM = PREFIX + 'smooth_navigation_right_zoom'
#: Which way the right-button drag zooms: 'up_in' (drag up zooms in) or 'up_out'.
KEY_SMOOTH_NAV_RIGHT_DIR = PREFIX + 'smooth_navigation_right_dir'
RIGHT_UP_IN = 'up_in'
RIGHT_UP_OUT = 'up_out'
KEY_SMOOTH_NAV_UNDERLAY = PREFIX + 'smooth_navigation_underlay'
#: Hold a key and left-drag to pan while a digitizing tool is active.
KEY_TEMP_PAN = PREFIX + 'temporary_pan'
KEY_TEMP_PAN_KEY = PREFIX + 'temporary_pan_key'     # a Qt.Key code
DEFAULT_TEMP_PAN_KEY = 0x43                          # Qt.Key_C
#: Vertices and nodes view (ArcGIS Pro style): markers on the vertices, nodes and midpoints of lines and polygons.
KEY_NODE_VIEW = PREFIX + 'node_view'
KEY_NODE_VERTICES = PREFIX + 'node_view_vertices'
KEY_NODE_NODES = PREFIX + 'node_view_nodes'
KEY_NODE_MIDPOINTS = PREFIX + 'node_view_midpoints'
#: Which layers: all visible line / polygon layers, only the layer selected in the Layers panel, or only layers in edit mode.
KEY_NODE_SCOPE = PREFIX + 'node_view_scope'
NODE_SCOPE_VISIBLE = 'visible'
NODE_SCOPE_ACTIVE = 'active'
NODE_SCOPE_EDITABLE = 'editable'
KEY_NODE_VERTEX_SIZE = PREFIX + 'node_view_vertex_size'
KEY_NODE_NODE_SIZE = PREFIX + 'node_view_node_size'
KEY_NODE_VERTEX_COLOR = PREFIX + 'node_view_vertex_color'
KEY_NODE_NODE_COLOR = PREFIX + 'node_view_node_color'
KEY_NODE_MID_COLOR = PREFIX + 'node_view_midpoint_color'
#: More vertices than this in the view and nothing is drawn (zoom in), so a zoomed-out view never stalls.
KEY_NODE_MAX = PREFIX + 'node_view_max_vertices'
#: Map grid (ArcGIS Pro's Grid): lines at a fixed spacing, optionally rotated.
KEY_GRID = PREFIX + 'grid'
KEY_GRID_SPACING = PREFIX + 'grid_spacing'
KEY_GRID_UNIT = PREFIX + 'grid_unit'
KEY_GRID_ROTATION = PREFIX + 'grid_rotation'
GRID_UNIT_M = 'm'
GRID_UNIT_FT = 'ft'
GRID_UNIT_MAP = 'map'
#: The floating editing toolbar over the map (Create Features): show it, where, how big, magnify on hover.
KEY_SKETCH_BAR = PREFIX + 'sketch_toolbar'
KEY_SKETCH_BAR_POSITION = 'kga_editor/create_toolbar_position'      # key kept from before it had a settings page
KEY_SKETCH_BAR_SIZE = PREFIX + 'sketch_toolbar_size'
KEY_SKETCH_BAR_MAGNIFY = PREFIX + 'sketch_toolbar_magnify'
#: The buttons of the KGA toolbars swell under the cursor (the same Magnify effect as the sketch toolbar).
KEY_MAGNIFY_TOOLBARS = PREFIX + 'magnify_toolbars'
SKETCH_POSITIONS = ('left', 'top', 'bottom', 'right')
SKETCH_SIZES = ('small', 'medium', 'large')
#: Show the KGA start page instead of QGIS's welcome page.
KEY_START_PAGE = PREFIX + 'custom_start_page'
#: Hide the menu bar too while the start page is up.
KEY_START_HIDE_MENU = PREFIX + 'start_page_hide_menu'
#: Remember which toolbars and panels are on, so Reset can put them back.
KEY_REMEMBER_LAYOUT = PREFIX + 'remember_layout'
KEY_LAYOUT_STATE = PREFIX + 'layout_state'
KEY_LAYOUT_NAMES = PREFIX + 'layout_names'
KEY_LAYOUT_STAMP = PREFIX + 'layout_stamp'

#: CRS of every new project, and the country it opens zoomed to.
KEY_PROJECT_CRS = PREFIX + 'new_project_crs'
KEY_ZOOM_ENABLED = PREFIX + 'new_project_zoom_cambodia'     # key kept from the first release
KEY_ZOOM_COUNTRY = PREFIX + 'new_project_country'

DEFAULT_BASEMAP = 'Google Terrain Hybrid'
DEFAULT_COUNTRY = 'KHM'         # Cambodia
DEFAULT_CRS = 'EPSG:32648'      # WGS 84 / UTM zone 48N, which covers Cambodia

DEFAULTS = {
    KEY_PROJECTS_FOLDER: '',
    KEY_BASEMAP_ENABLED: True,
    KEY_BASEMAP_NAME: DEFAULT_BASEMAP,
    KEY_OUTPUT_TO_PROJECT: True,
    KEY_GEOMETRY_FIELDS: True,
    KEY_ASK_CONVERT: True,
    KEY_SAVE_MODE: SAVE_ASK,
    KEY_SCRATCH_ON_CLOSE: SCRATCH_ASK,
    KEY_FOLLOW_SYSTEM_THEME: False,
    KEY_BAG_ANIMATION: BAG_GENIE,
    KEY_WINDOW_REVEAL: True,
    KEY_PRO_SELECTION: True,
    KEY_PRO_LAYERS: True,
    KEY_MAP_TABS: True,
    KEY_MAP_TABS_AUTO_ORGANIZE: True,
    KEY_LAYER_LISTS_CURRENT_MAP: True,
    KEY_MAP_TABS_DOCK_LAYOUTS: True,
    KEY_MAP_TABS_SYNC_THEMES: True,
    KEY_SMOOTH_NAV: True,
    KEY_SMOOTH_NAV_ZOOM: True,
    KEY_SMOOTH_NAV_ZOOM_MS: 110,
    KEY_SMOOTH_NAV_INERTIA: True,
    KEY_SMOOTH_NAV_GLIDE_MS: 325,
    KEY_SMOOTH_NAV_KEYBOARD: True,
    KEY_SMOOTH_NAV_RIGHT_ZOOM: True,
    KEY_SMOOTH_NAV_RIGHT_DIR: RIGHT_UP_IN,
    KEY_SMOOTH_NAV_UNDERLAY: True,
    KEY_TEMP_PAN: True,
    KEY_TEMP_PAN_KEY: DEFAULT_TEMP_PAN_KEY,
    KEY_NODE_VIEW: False,
    KEY_NODE_VERTICES: True,
    KEY_NODE_NODES: True,
    KEY_NODE_MIDPOINTS: False,
    KEY_NODE_SCOPE: NODE_SCOPE_VISIBLE,
    KEY_NODE_VERTEX_SIZE: 9,
    KEY_NODE_NODE_SIZE: 12,
    KEY_NODE_VERTEX_COLOR: '#505050',
    KEY_NODE_NODE_COLOR: '#ff00ff',
    KEY_NODE_MID_COLOR: '#2f7fd8',
    KEY_NODE_MAX: 80000,
    KEY_GRID: False,
    KEY_GRID_SPACING: 10.0,
    KEY_GRID_UNIT: GRID_UNIT_M,
    KEY_GRID_ROTATION: 0.0,
    KEY_MAGNIFY_TOOLBARS: False,
    KEY_SKETCH_BAR: True,
    KEY_SKETCH_BAR_POSITION: 'bottom',
    KEY_SKETCH_BAR_SIZE: 'medium',
    KEY_SKETCH_BAR_MAGNIFY: True,
    KEY_START_PAGE: True,
    KEY_START_HIDE_MENU: True,
    KEY_REMEMBER_LAYOUT: False,
    KEY_LAYOUT_STATE: '',
    KEY_LAYOUT_NAMES: '',
    KEY_LAYOUT_STAMP: '',
    KEY_PROJECT_CRS: DEFAULT_CRS,
    KEY_ZOOM_ENABLED: True,
    KEY_ZOOM_COUNTRY: DEFAULT_COUNTRY,
}


_SHARED = []


def shared():
    """One QgsSettings for the code that reads settings again and again (bubbles, navigation, panels).

    Building a QgsSettings costs about 200 times as much as reading a value from one (0.6 ms against
    0.003 ms), and some settings are read on every wheel notch or once a second. Every QgsSettings
    of the process shares the same stored values, so a change made anywhere else (the Options window,
    another plugin) is seen here at once."""
    if not _SHARED:
        _SHARED.append(QgsSettings())
    return _SHARED[0]


def _get(key, kind):
    return shared().value(key, DEFAULTS[key], type=kind)


def projects_folder():
    """The folder the user chose for new projects, or '' for the standard one."""
    return (_get(KEY_PROJECTS_FOLDER, str) or '').strip()


def basemap_enabled():
    return _get(KEY_BASEMAP_ENABLED, bool)


def basemap_name():
    return (_get(KEY_BASEMAP_NAME, str) or '').strip() or DEFAULT_BASEMAP


def output_to_project():
    return _get(KEY_OUTPUT_TO_PROJECT, bool)


def geometry_fields():
    return _get(KEY_GEOMETRY_FIELDS, bool)


def ask_convert():
    return _get(KEY_ASK_CONVERT, bool)


def save_mode():
    """'ask', 'workspace' or 'traditional'; anything else counts as 'ask'."""
    value = (_get(KEY_SAVE_MODE, str) or '').strip().lower()
    return value if value in (SAVE_WORKSPACE, SAVE_TRADITIONAL) else SAVE_ASK


def scratch_on_close():
    """'ask', 'project' or 'off'; anything else counts as 'ask'."""
    value = (_get(KEY_SCRATCH_ON_CLOSE, str) or '').strip().lower()
    return value if value in (SCRATCH_PROJECT, SCRATCH_OFF) else SCRATCH_ASK


def project_crs():
    """Authority id (such as EPSG:32648) of the CRS new projects start with."""
    return (_get(KEY_PROJECT_CRS, str) or '').strip() or DEFAULT_CRS


def zoom_enabled():
    return _get(KEY_ZOOM_ENABLED, bool)


def zoom_country():
    """Alpha-3 code of the country new projects open zoomed to."""
    return (_get(KEY_ZOOM_COUNTRY, str) or '').strip().upper() or DEFAULT_COUNTRY


def follow_system_theme():
    return _get(KEY_FOLLOW_SYSTEM_THEME, bool)


def bag_animation():
    """How the Tool Bag, Symbol Bag and Layer Colors open and close: 'genie' or 'classic'."""
    value = (_get(KEY_BAG_ANIMATION, str) or '').strip()
    return value if value in (BAG_GENIE, BAG_CLASSIC) else BAG_GENIE


def window_reveal():
    """Whether tool windows open out of their toolbar button."""
    return _get(KEY_WINDOW_REVEAL, bool)


def pro_selection():
    return _get(KEY_PRO_SELECTION, bool)


def pro_layers():
    return _get(KEY_PRO_LAYERS, bool)


def map_tabs():
    return _get(KEY_MAP_TABS, bool)


def layer_lists_current_map():
    """Whether every layer drop-down lists only the layers of the map being shown (needs Map Tabs)."""
    return _get(KEY_LAYER_LISTS_CURRENT_MAP, bool)


def map_tabs_auto_organize():
    return _get(KEY_MAP_TABS_AUTO_ORGANIZE, bool)


def map_tabs_dock_layouts():
    return _get(KEY_MAP_TABS_DOCK_LAYOUTS, bool)


def map_tabs_sync_themes():
    return _get(KEY_MAP_TABS_SYNC_THEMES, bool)


def smooth_nav_enabled():
    return _get(KEY_SMOOTH_NAV, bool)


def smooth_nav_zoom():
    return _get(KEY_SMOOTH_NAV_ZOOM, bool)


def smooth_nav_zoom_ms():
    return min(max(_get(KEY_SMOOTH_NAV_ZOOM_MS, int), 30), 500)


def smooth_nav_inertia():
    return _get(KEY_SMOOTH_NAV_INERTIA, bool)


def smooth_nav_glide_ms():
    return min(max(_get(KEY_SMOOTH_NAV_GLIDE_MS, int), 80), 2000)


def smooth_nav_keyboard():
    return _get(KEY_SMOOTH_NAV_KEYBOARD, bool)


def smooth_nav_right_zoom():
    return _get(KEY_SMOOTH_NAV_RIGHT_ZOOM, bool)


def smooth_nav_right_dir():
    """'up_in' or 'up_out'; anything else counts as 'up_in'."""
    value = (_get(KEY_SMOOTH_NAV_RIGHT_DIR, str) or '').strip().lower()
    return RIGHT_UP_OUT if value == RIGHT_UP_OUT else RIGHT_UP_IN


def smooth_nav_underlay():
    return _get(KEY_SMOOTH_NAV_UNDERLAY, bool)


def temp_pan_enabled():
    return _get(KEY_TEMP_PAN, bool)


def temp_pan_key():
    return _get(KEY_TEMP_PAN_KEY, int) or DEFAULT_TEMP_PAN_KEY


def node_view_enabled():
    return _get(KEY_NODE_VIEW, bool)


def node_view_vertices():
    return _get(KEY_NODE_VERTICES, bool)


def node_view_nodes():
    return _get(KEY_NODE_NODES, bool)


def node_view_midpoints():
    return _get(KEY_NODE_MIDPOINTS, bool)


def node_view_scope():
    """'visible', 'active' or 'editable'; anything else counts as 'visible'."""
    value = (_get(KEY_NODE_SCOPE, str) or '').strip().lower()
    return value if value in (NODE_SCOPE_ACTIVE, NODE_SCOPE_EDITABLE) else NODE_SCOPE_VISIBLE


def node_view_vertex_size():
    return min(max(_get(KEY_NODE_VERTEX_SIZE, int), 4), 20)


def node_view_node_size():
    return min(max(_get(KEY_NODE_NODE_SIZE, int), 5), 28)


def _colour(key):
    value = (_get(key, str) or '').strip()
    from qgis.PyQt.QtGui import QColor
    return value if value and QColor(value).isValid() else DEFAULTS[key]


def node_view_vertex_color():
    return _colour(KEY_NODE_VERTEX_COLOR)


def node_view_node_color():
    return _colour(KEY_NODE_NODE_COLOR)


def node_view_midpoint_color():
    return _colour(KEY_NODE_MID_COLOR)


def node_view_max_vertices():
    return min(max(_get(KEY_NODE_MAX, int), 2000), 1000000)


def grid_enabled():
    return _get(KEY_GRID, bool)


def grid_spacing():
    return min(max(_get(KEY_GRID_SPACING, float), 0.0001), 10000000.0)


def grid_unit():
    """'m', 'ft' or 'map' (the map's own units); anything else counts as 'm'."""
    value = (_get(KEY_GRID_UNIT, str) or '').strip().lower()
    return value if value in (GRID_UNIT_FT, GRID_UNIT_MAP) else GRID_UNIT_M


def grid_rotation():
    """Degrees, counter-clockwise, folded into -180..180."""
    value = _get(KEY_GRID_ROTATION, float) % 360.0
    return value - 360.0 if value > 180.0 else value


def magnify_toolbars():
    return _get(KEY_MAGNIFY_TOOLBARS, bool)


def sketch_bar_enabled():
    return _get(KEY_SKETCH_BAR, bool)


def sketch_bar_position():
    """'left', 'top', 'bottom' or 'right'; anything else counts as 'bottom'."""
    value = (_get(KEY_SKETCH_BAR_POSITION, str) or '').strip().lower()
    return value if value in SKETCH_POSITIONS else 'bottom'


def sketch_bar_size():
    """'small', 'medium' or 'large'; anything else counts as 'medium'."""
    value = (_get(KEY_SKETCH_BAR_SIZE, str) or '').strip().lower()
    return value if value in SKETCH_SIZES else 'medium'


def sketch_bar_magnify():
    return _get(KEY_SKETCH_BAR_MAGNIFY, bool)


def start_page_enabled():
    return _get(KEY_START_PAGE, bool)


def remember_layout():
    return _get(KEY_REMEMBER_LAYOUT, bool)


def get_str(key):
    return str(_get(key, str) or '')


def start_page_hide_menu():
    return _get(KEY_START_HIDE_MENU, bool)


def set_value(key, value):
    if key not in DEFAULTS:
        raise KeyError(key)
    shared().setValue(key, value)


def reset_all():
    """Put every KGA setting back to its factory default."""
    settings = shared()
    for key in DEFAULTS:
        settings.remove(key)

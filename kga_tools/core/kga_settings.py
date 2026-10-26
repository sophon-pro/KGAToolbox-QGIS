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
#: Match the QGIS light/dark theme to the operating system when QGIS starts.
KEY_FOLLOW_SYSTEM_THEME = PREFIX + 'follow_system_theme'
#: Selected features in ArcGIS Pro's cyan outline instead of QGIS's yellow.
KEY_PRO_SELECTION = PREFIX + 'pro_selection'
#: Layers panel dressed like ArcGIS Pro's Contents pane.
KEY_PRO_LAYERS = PREFIX + 'pro_layers'
#: Map and layout tabs above the canvas (off: it reorganises a project's layers).
KEY_MAP_TABS = PREFIX + 'map_tabs'
KEY_MAP_TABS_AUTO_ORGANIZE = PREFIX + 'map_tabs_auto_organize'
KEY_MAP_TABS_DOCK_LAYOUTS = PREFIX + 'map_tabs_dock_layouts'
KEY_MAP_TABS_SYNC_THEMES = PREFIX + 'map_tabs_sync_themes'
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
    KEY_FOLLOW_SYSTEM_THEME: False,
    KEY_PRO_SELECTION: True,
    KEY_PRO_LAYERS: True,
    KEY_MAP_TABS: False,
    KEY_MAP_TABS_AUTO_ORGANIZE: False,
    KEY_MAP_TABS_DOCK_LAYOUTS: True,
    KEY_MAP_TABS_SYNC_THEMES: True,
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


def _get(key, kind):
    return QgsSettings().value(key, DEFAULTS[key], type=kind)


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


def pro_selection():
    return _get(KEY_PRO_SELECTION, bool)


def pro_layers():
    return _get(KEY_PRO_LAYERS, bool)


def map_tabs():
    return _get(KEY_MAP_TABS, bool)


def map_tabs_auto_organize():
    return _get(KEY_MAP_TABS_AUTO_ORGANIZE, bool)


def map_tabs_dock_layouts():
    return _get(KEY_MAP_TABS_DOCK_LAYOUTS, bool)


def map_tabs_sync_themes():
    return _get(KEY_MAP_TABS_SYNC_THEMES, bool)


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
    QgsSettings().setValue(key, value)


def reset_all():
    """Put every KGA setting back to its factory default."""
    settings = QgsSettings()
    for key in DEFAULTS:
        settings.remove(key)

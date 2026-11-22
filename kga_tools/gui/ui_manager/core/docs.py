# -*- coding: utf-8 -*-
"""Links from a panel or toolbar to its page in the QGIS user manual.

Only the built-in QGIS panels and toolbars are documented there, so
third-party items (including KGA's own) have no link and get no info button.
Matching uses the lower-cased title plus object name, so it works on both the
English titles and QGIS's internal object names.
"""
from .registry import PANEL, TOOLBAR

BASE = "https://docs.qgis.org/latest/en/docs/user_manual/"

# (keywords, url) - the first rule whose keyword appears in the item text wins.
_PANELS = (
    (("advanced digitizing",), "working_with_vector/editing_geometry_attributes.html#advanced-digitizing-panel"),
    (("browser",), "managing_data_source/opening_data.html#browser-panel"),
    (("debugging", "developer tools", "devtools"), "introduction/general_tools.html#debug-dev-tools"),
    (("elevation profile",), "map_views/elevation_profile.html#label-elevation-profile-view"),
    (("gps information", "gpsinformation"), "working_with_gps/live_GPS_tracking.html#sec-gpstracking"),
    (("layer order", "layerorder"), "introduction/general_tools.html#layer-order"),
    (("layer styling", "layerstyling"), "introduction/general_tools.html#layer-styling-panel"),
    (("log messages", "logmessage", "messagelog"), "introduction/general_tools.html#log-message-panel"),
    (("overview",), "introduction/general_tools.html#overview-panels"),
    (("processing toolbox", "processingtoolbox"), "processing/toolbox.html#processing-toolbox"),
    (("snapping",), "working_with_vector/editing_geometry_attributes.html#snapping-tolerance"),
    (("spatial bookmark", "bookmark"), "map_views/map_view.html#sec-bookmarks"),
    (("statistic",), "introduction/general_tools.html#statistical-summary"),
    (("temporal controller", "temporalcontroller"), "map_views/map_view.html#temporal-controller"),
    (("undo", ), "introduction/general_tools.html#undo-redo-panel"),
    (("vertex editor", "vertexeditor"), "working_with_vector/editing_geometry_attributes.html#vertex-editor-panel"),
    (("layers", "layertree"), "introduction/general_tools.html#label-legend"),
)

_TOOLBARS = (
    (("advanced digitizing",), "working_with_vector/editing_geometry_attributes.html#sec-advanced-edit"),
    (("annotation",), "map_views/map_view.html#sec-annotations"),
    (("attributes",), "working_with_vector/attribute_table.html#sec-attribute-table"),
    (("data source manager",), "managing_data_source/index.html#manage-data-source"),
    (("database",), "plugins/core_plugins/plugins_db_manager.html#dbmanager"),
    (("digitizing",), "working_with_vector/editing_geometry_attributes.html#sec-edit-existing-layer"),
    (("gps",), "working_with_gps/live_GPS_tracking.html#sec-gpstracking"),
    (("grass",), "grass_integration/grass_integration.html#sec-grass"),
    (("label",), "working_with_vector/vector_properties.html#label-toolbar"),
    (("map navigation",), "map_views/map_view.html#zoom-pan"),
    (("mesh",), "working_with_mesh/mesh.html#editing-mesh"),
    (("selection",), "introduction/general_tools.html#sec-selection"),
    (("shape digitizing",), "working_with_vector/editing_geometry_attributes.html#shape-edit"),
    (("snapping",), "working_with_vector/editing_geometry_attributes.html#snapping-tolerance"),
    (("project",), "introduction/project_files.html#project-files"),
    (("file",), "introduction/project_files.html#project-files"),
    (("layer",), "managing_data_source/opening_data.html#opening-data"),
    (("plugins",), "plugins/index.html#plugins-index"),
    (("help",), "introduction/qgis_gui.html#toolbars"),
    (("raster", "vector", "web"), "introduction/qgis_gui.html#toolbars"),
)


def doc_url(item):
    """Return the QGIS manual URL for a ``UiItem``, or ``None`` if it has none."""
    name = item.widget.objectName().lower()
    if "kga" in name:
        return None  # plugin-made, not in the QGIS manual
    text = (item.title + " " + item.widget.objectName()).lower()
    rules = _PANELS if item.kind == PANEL else _TOOLBARS if item.kind == TOOLBAR else ()
    for keywords, path in rules:
        if any(k in text for k in keywords):
            return BASE + path
    return None

"""Shared branding: icon lookup, group ordering, toolbar/panel labels.

Both kga_plugin.py (toolbar) and gui/geoprocessing_panel.py (dock) read their
icon and ordering configuration from here, so a new algorithm needs its icon
registered in exactly one place.
"""

import os

from qgis.PyQt.QtGui import QIcon

PLUGIN_DIR = os.path.dirname(__file__)
ICON_DIR = os.path.join(PLUGIN_DIR, 'icons')

LOG_TAG = 'KGA Toolbox'
KGA_WEBSITE = 'https://khmergrs.com'

# Where to find us. Order is the order the About dialog lists them in.
KGA_LINKS = [
    ('Website', KGA_WEBSITE),
    ('Facebook Page', 'https://www.facebook.com/khmergisacademy'),
    ('YouTube Channel', 'https://www.youtube.com/@Khmergisacademy'),
    ('Telegram Channel', 'https://t.me/khmergisacademychannel'),
    ('TikTok', 'https://www.tiktok.com/@khmergrsacademy'),
]

# Algorithms promoted out of their group menu into their own toolbar button,
# in the order they should appear at the head of the toolbar.
STANDALONE_ALGS = [
    ('spatialdatamanager', 'spatialDataManager.png'),
    ('layerexportimport', 'import_export.png'),
    ('domain_manager', 'domain.png'),
]

# Order the group sections. Groups in neither tuple are sorted alphabetically
# between the two.
GROUP_ORDER = [
    'KGA Data Management',
    'KGA Schema Tools',
    'KGA Editing Tools',
    'KGA Geometry Utilities',
    'KGA Topology',
]

# Pinned to the end.
GROUP_LAST = [
    'KGA Data Conversion',
    'KGA Irrigation Tools',
]

# Optional per-group icon files in icons/. Missing files fall back to text.
GROUP_ICONS = {
    'KGA Data Conversion': 'conversion.png',
    'KGA Data Management': 'management.png',
    'KGA Geometry Utilities': 'geometry.png',
    'KGA Irrigation Tools': 'irrigation.png',
    'KGA Topology': 'topology.png',
    'KGA Schema Tools': 'schema.png',
    'KGA Editing Tools': 'editing.png',
}

# Optional per-algorithm icon files in icons/, keyed by alg.name(). Missing
# files fall back to the group icon, so this can stay sparse.
ALG_ICONS = {
    'create_points_from_table': 'PointFromTable.png',
    'export_layers_to_kml': 'alg_kml.png',
    'geometry_conversion_dynamic': 'alg_geometry.png',
    'gpkgtoshapefiles': 'shapefile.png',
    'shapefilestogpkg': 'gpkg.png',
    'layerexportimport': 'import_export.png',
    'checkoverlapsandgaps': 'alg_topology.png',
    'domain_manager': 'domain.png',
    'apply_domain_library': 'alg_domain_apply.png',
    'validate_against_domains': 'alg_validate.png',
    'append_with_mapping': 'alg_append.png',
    'generate_field_mapping': 'alg_mapping.png',
    'sequential_numbering': 'seqNumbering.png',
    # The ArcGIS Modify Features replicas, in the same order as their
    # ALG_ORDER block below.
    'construct_polygon': 'constructPolygon.png',
    'copy_parallel': 'copyParallel.png',
    'buffer_features': 'bufferFeature.png',
    'split_into_cogo_lines': 'splitCogoLines.png',
    'divide_features': 'divideFeatures.png',
    'merge_features': 'mergeFeatures.png',
    'clip_features': 'clipFeatures.png',
    'attributes_to_xlsx': 'toExcel.png',
    'xlsx_to_attributes': 'fromExcel.png',
    'filegdb_to_geopackage': 'alg_gdb_to_gpkg.png',
    'geopackage_to_filegdb': 'alg_gpkg_to_gdb.png',
    'create_layer_package': 'alg_package_out.png',
    'open_layer_package': 'alg_package_in.png',
    'planarizelines': 'planarizeTool.png',
    'checkpointsonboundary': 'pointOnBoundary.png',
    'errorinspector': 'errorInspector.png',
    'duplicate_checker': 'checkDuplicate.png', 
    'updatexyfields': 'updateXYFields.png',
    'updategeometryfields': 'updateGeomFields.png',
    'copy_paste_feature': 'copyPaste.png',
}           


def icon(filename):
    """QIcon for a file in icons/, or a null QIcon if it is absent."""
    if not filename:
        return QIcon()
    path = os.path.join(ICON_DIR, filename)
    return QIcon(path) if os.path.exists(path) else QIcon()


def group_icon(group_name):
    return icon(GROUP_ICONS.get(group_name))


def algorithm_icon(alg, fallback=None):
    """Menu/tree icon for one algorithm: own file -> group icon -> QGIS default.

    alg.icon() is last because none of the algorithms override it, so it is the
    same generic processing gear for every entry.
    """
    own = icon(ALG_ICONS.get(alg.name()))
    if not own.isNull():
        return own
    if fallback is None:
        fallback = group_icon(alg.group() or '')
    if fallback is not None and not fallback.isNull():
        return fallback
    return alg.icon()


def button_label(group_name):
    """'KGA Data Conversion' -> 'Data Conversion' (the toolbar is already KGA)."""
    prefix = 'KGA '
    return group_name[len(prefix):] if group_name.startswith(prefix) else group_name


# Algorithms pinned to the top of their group's menu, by alg.name(). Anything
# not listed follows, sorted by display name as before.
#
# Alphabetical order is right almost everywhere, but it separates pairs that
# only make sense read together: "Open Layer Package" belongs under "Create
# Layer Package", not four entries below it.
ALG_ORDER = {
    'KGA Data Management': [
        'create_layer_package',
        'open_layer_package',
    ],
    'KGA Data Conversion': [
        # Two round trips, each of which makes no sense read apart. The
        # geodatabase pair leads because it is the one that decides where a
        # whole job lives, not just how one table travels.
        'filegdb_to_geopackage',
        'geopackage_to_filegdb',
        'attributes_to_xlsx',
        'xlsx_to_attributes',
    ],
    'KGA Topology': [
        # Run a check, then inspect what it found. The inspector reads the error
        # layers the two checks write, so alphabetical order would put it first.
        'checkoverlapsandgaps',
        'checkpointsonboundary',
        'errorinspector',
    ],
    'KGA Editing Tools': [
        # Copy-Paste Feature leads: it is the one that puts a feature on the
        # map in the first place, and the rest of the group works on features
        # that are already there.
        'copy_paste_feature',
        # Then the ArcGIS Modify Features replicas, kept together and in the
        # order the work runs in rather than alphabetically: build a feature
        # out of lines, make new features beside the selection, break features
        # up, put them back together, then trim. Alphabetical order would
        # interleave them with Sequential Numbering, which is a different job.
        'construct_polygon',
        'copy_parallel',
        'buffer_features',
        'split_into_cogo_lines',
        'divide_features',
        'merge_features',
        'clip_features',
    ],
}


def ordered_algorithms(group_name, algorithms):
    """One group's algorithms in menu order.

    Read by both the toolbar and the Geoprocessing panel so the two never drift
    apart.
    """
    pinned = ALG_ORDER.get(group_name, [])
    by_name = {alg.name(): alg for alg in algorithms}

    ordered = [by_name[name] for name in pinned if name in by_name]
    claimed = {alg.name() for alg in ordered}
    ordered += sorted((alg for alg in algorithms if alg.name() not in claimed),
                      key=lambda a: a.displayName())
    return ordered


def ordered_groups(present):
    """Apply GROUP_ORDER / alphabetical / GROUP_LAST to the groups we actually have."""
    present = set(present)
    pinned = set(GROUP_ORDER) | set(GROUP_LAST)
    ordered = [g for g in GROUP_ORDER if g in present]
    ordered += sorted(g for g in present if g not in pinned)
    ordered += [g for g in GROUP_LAST if g in present]
    return ordered

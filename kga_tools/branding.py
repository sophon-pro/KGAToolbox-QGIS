"""Shared branding: icon lookup, group ordering, toolbar/panel labels.

Both kga_plugin.py (toolbar) and gui/geoprocessing_panel.py (dock) read their
icon and ordering configuration from here, so a new algorithm needs its icon
registered in exactly one place.
"""

import os

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QPoint, QRect, QSize, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon, QIconEngine, QPainter, QPixmap
from qgis.PyQt.QtWidgets import QApplication, QPushButton

PLUGIN_DIR = os.path.dirname(__file__)
ICON_DIR = os.path.join(PLUGIN_DIR, 'icons')
DARK_ICON_DIR = os.path.join(ICON_DIR, 'dark')   # made by dev_tools/make_dark_icons.py

LOG_TAG = 'KGA Toolbox'
KGA_WEBSITE = 'https://khmergrs.com'
DOCS_URL = 'https://khmergrs.com/docs/qgis/{name}'

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
    ('spatialdatamanager', 'data_management/spatialDataManager.png'),
    ('layerexportimport', 'conversion/import_export.png'),
    ('add_data', 'data_management/addData.png'),
    ('imagery_downloader', 'imagery_basemaps/imagery_download.png'),
]

# Algorithms that share their toolbar button with a promoted one: the button
# becomes a drop-down listing the leader first, then these. They are still
# ordinary algorithms in the Processing panel and the group menus.
STANDALONE_COMPANIONS = {
    'imagery_downloader': ['satellite_imagery_archive'],
}

# Groups kept in the KGA Toolbox menu (and the Processing/KGA panel) but given
# no toolbar button, to leave room for the most-used tools.
MENU_ONLY_GROUPS = {
    'KGA Schema Tools',
}

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
    'KGA Mapping',
]

# Optional per-group icon files in icons/. Missing files fall back to text.
GROUP_ICONS = {
    'KGA Data Conversion': 'toolbox_groups/conversion.png',
    'KGA Data Management': 'toolbox_groups/management.png',
    'KGA Geometry Utilities': 'toolbox_groups/geometry.png',
    'KGA Irrigation Tools': 'toolbox_groups/irrigation.png',
    'KGA Mapping': 'toolbox_groups/Mapping.png',
    'KGA Topology': 'toolbox_groups/topology.png',
    'KGA Schema Tools': 'toolbox_groups/schema.png',
    'KGA Editing Tools': 'toolbox_groups/editing.png',
    'KGA GeoAI': 'toolbox_groups/geoai.png',
}

# Optional per-algorithm icon files in icons/, keyed by alg.name(). Missing
# files fall back to the group icon, so this can stay sparse.
ALG_ICONS = {
    'create_points_from_table': 'conversion/PointFromTable.png',
    'export_layers_to_kml': 'conversion/alg_kml.png',
    'geometry_conversion_dynamic': 'conversion/alg_geometry.png',
    'gpkgtoshapefiles': 'conversion/shapefile.png',
    'shapefilestogpkg': 'conversion/gpkg.png',
    'layerexportimport': 'conversion/import_export.png',
    'add_data': 'data_management/addData.png',
    'imagery_downloader': 'imagery_basemaps/imagery_download.png',
    'satellite_imagery_archive': 'imagery_basemaps/historical_imagery.png',
    'detect_roads_imagery': 'geoai/detectRoads.png',
    'detect_paddy_plots_imagery': 'geoai/detectPaddy.png',
    'duplicate_to_new_layer': 'data_management/duplicateLayer.png',
    'generate_contour_roi': 'terrain_water/generateContour.png',
    'highest_point_per_block': 'terrain_water/highestPoint.png',
    'dem_dsm_inspector': 'terrain_water/demDsmInspector.png',
    'generate_stream_roi': 'terrain_water/generateStream.png',
    'multipart_line_repair': 'quality_control/multipartLineRepair.png',
    'sliver_spike_cleaner': 'quality_control/sliverSpikeCleaner.png',
    'point_to_geometry': 'conversion/pointToGeometry.png',
    'strip_point_grid': 'conversion/stripPointGrid.png',
    'csvtodemcontour': 'terrain_water/csvToDemContour.png',
    'demelevationcorrectionandpointsamplereport': 'terrain_water/demCorrectionReport.png',
    'reservoir_rating_curve': 'terrain_water/reservoirRatingCurve.png',
    'reservoir_storage_analyzer': 'terrain_water/storageCapacityCurve.png',
    'add_grid': 'mapping/addGrid.png',
    'callout_label_studio': 'mapping/calloutLabelStudio.png',
    'kga_layer_symbol': 'mapping/kgaLayerSymbol.png',
    'dem_legend_bar': 'mapping/demLegendBar.png',
    'checkoverlapsandgaps': 'quality_control/alg_topology.png',
    'domain_manager': 'schema/domain.png',
    'apply_domain_library': 'schema/alg_domain_apply.png',
    'validate_against_domains': 'schema/alg_validate.png',
    'append_with_mapping': 'schema/alg_append.png',
    'generate_field_mapping': 'schema/alg_mapping.png',
    'sequential_numbering': 'editing/seqNumbering.png',
    # The ArcGIS Modify Features replicas, in the same order as their
    # ALG_ORDER block below.
    'construct_polygon': 'editing/constructPolygon.png',
    'copy_parallel': 'editing/copyParallel.png',
    'buffer_features': 'editing/bufferFeature.png',
    'split_into_cogo_lines': 'editing/splitCogoLines.png',
    'divide_features': 'editing/divideFeatures.png',
    'merge_features': 'editing/mergeFeatures.png',
    'clip_features': 'editing/clipFeatures.png',
    'attributes_to_xlsx': 'conversion/toExcel.png',
    'xlsx_to_attributes': 'conversion/fromExcel.png',
    'raster_format_converter': 'conversion/rasterFormatConverter.png',
    'filegdb_to_geopackage': 'conversion/alg_gdb_to_gpkg.png',
    'geopackage_to_filegdb': 'conversion/alg_gpkg_to_gdb.png',
    'create_layer_package': 'data_management/alg_package_out.png',
    'open_layer_package': 'data_management/alg_package_in.png',
    'add_to_layer_package': 'data_management/addToLayerPackage.png',
    'planarizelines': 'editing/planarizeTool.png',
    'checkpointsonboundary': 'quality_control/pointOnBoundary.png',
    'errorinspector': 'quality_control/errorInspector.png',
    'duplicate_checker': 'quality_control/checkDuplicate.png', 
    'updatexyfields': 'data_management/updateXYFields.png',
    'updategeometryfields': 'data_management/updateGeomFields.png',
    'copy_paste_feature': 'editing/copyPaste.png',
}           


def ui_is_dark():
    """True while QGIS runs a dark user-interface theme (Night Mapping and the like)."""
    app = QApplication.instance()
    return app is not None and app.palette().window().color().lightness() < 128


def dark_variant(path):
    """The dark-theme copy of an icon under icons/ (kept in icons/dark/), or None if it has none."""
    try:
        rel = os.path.relpath(os.path.normpath(path), ICON_DIR)
    except ValueError:                      # another drive on Windows
        return None
    if rel.startswith('..'):
        return None
    dark = os.path.join(DARK_ICON_DIR, rel)
    return dark if os.path.isfile(dark) else None


def icon_path(filename):
    """Path of an icon in icons/ for the current theme, for code that needs a file (QPixmap, style sheets)."""
    path = os.path.join(ICON_DIR, filename)
    return (ui_is_dark() and dark_variant(path)) or path


class _ThemedIconEngine(QIconEngine):
    """Draws the light or the dark copy of an icon, whichever matches the theme at paint time,
    so toolbars and menus follow a theme switch without being rebuilt."""

    def __init__(self, light, dark):
        super().__init__()
        self._paths = (light, dark)
        self._icons = {}

    def _current(self):
        path = self._paths[1] if ui_is_dark() else self._paths[0]
        found = self._icons.get(path)
        if found is None:
            found = self._icons[path] = QIcon(path)
        return found

    def scaledPixmap(self, size, mode, state, scale):
        # Render at the physical pixel size ourselves. Handing the request to the nested QIcon
        # made it apply the screen's pixel ratio a second time, and the pixmap Qt then had to
        # shrink back was drawn without smoothing - stair-stepped toolbar icons on scaled displays.
        scale = scale if scale and scale > 0 else 1.0
        phys = QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        pix = QPixmap(phys)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._current().paint(painter, QRect(QPoint(0, 0), phys),
                              Qt.AlignmentFlag.AlignCenter, mode, state)
        painter.end()
        pix.setDevicePixelRatio(scale)
        return pix

    def paint(self, painter, rect, mode, state):
        pix = self.scaledPixmap(rect.size(), mode, state, painter.device().devicePixelRatioF())
        painter.drawPixmap(rect.topLeft(), pix)

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def actualSize(self, size, mode, state):
        return self._current().actualSize(size, mode, state)

    def availableSizes(self, mode=QIcon.Mode.Normal, state=QIcon.State.Off):
        return self._current().availableSizes(mode, state)

    def clone(self):
        return _ThemedIconEngine(*self._paths)


def themed_icon(path):
    """QIcon for an icon file that switches to its icons/dark/ copy while the theme is dark.
    Files with no dark copy (and files outside icons/) give a plain QIcon."""
    dark = dark_variant(path)
    return QIcon(_ThemedIconEngine(os.path.normpath(path), dark)) if dark else QIcon(path)


def icon(filename):
    """QIcon for a file in icons/ that follows the light / dark theme, or a null QIcon if it is absent."""
    if not filename:
        return QIcon()
    path = os.path.join(ICON_DIR, filename)
    return themed_icon(path) if os.path.exists(path) else QIcon()


def docs_url(alg_name):
    """The Help button target for one algorithm, keyed by its `name()`.

    The page it lands on is transcribed from the same `toolMetadata/<name>.md`
    that ships in the plugin, so the button and the bundled documentation never
    disagree.
    """
    return DOCS_URL.format(name=alg_name)


def open_docs(alg_name):
    """Open one algorithm's documentation page in the browser."""
    url = docs_url(alg_name)
    if not QDesktopServices.openUrl(QUrl(url)):
        QgsMessageLog.logMessage(
            'Could not open {}'.format(url), LOG_TAG,
            Qgis.MessageLevel.Warning)


def help_button(alg_name, parent=None):
    """A Help button for a tool that opens its own window.

    The Processing parameters dialog carries a Help button of its own, wired to
    `helpUrl()`. A tool with no parameters never shows that dialog - the plugin
    runs it straight away so an empty parameter window does not land in front of
    the tool's real one - so it has nowhere to put its help. This is that
    button, landing on the same page the Processing one would.
    """
    button = QPushButton('Help', parent)
    button.setToolTip('Open the documentation for this tool')
    button.clicked.connect(lambda: open_docs(alg_name))
    return button


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
    'KGA GeoAI': [
        # In the order they were added: roads first, then paddy plots.
        'detect_roads_imagery',
        'detect_paddy_plots_imagery',
    ],
    'KGA Data Management': [
        # Getting data in comes before anything that can be done to it.
        'add_data',
        'imagery_downloader',
        'satellite_imagery_archive',
        'create_layer_package',
        'open_layer_package',
        'add_to_layer_package',
        'duplicate_to_new_layer',
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
    'KGA Irrigation Tools': [
        # The existing tools keep the alphabetical order they always had; the
        # two generators go last, below Reservoir Storage Analyzer, because they
        # are the ones that start from nothing but an area on the map.
        'csvtodemcontour',
        'demelevationcorrectionandpointsamplereport',
        'reservoir_rating_curve',
        'reservoir_storage_analyzer',
        'highest_point_per_block',
        'generate_contour_roi',
        'generate_stream_roi',
        'multipart_line_repair',
        'dem_dsm_inspector',
    ],
    'KGA Mapping': [
        # Callout Label Studio leads: labelling is the first thing a map
        # needs. Adding a grid to a sheet then comes before making the legend
        # that goes beside it, and alphabetical order would put the legend
        # first.
        'callout_label_studio',
        # Styling the layers comes right after labelling them.
        'kga_layer_symbol',
        'add_grid',
        'dem_legend_bar',
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
        # Sequential Numbering is not pinned, so it would sort after
        # Planarize Lines; pin both to keep Planarize Lines last.
        'sequential_numbering',
        'planarizelines',
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

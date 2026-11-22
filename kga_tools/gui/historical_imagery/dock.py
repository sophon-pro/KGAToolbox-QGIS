# -*- coding: utf-8 -*-
"""
Main dock panel – tabbed layout:

    Search   → source, area of interest, dates, cloud cover
    Results  → thumbnails, year filter, sort, preview, footprints
    Download → bands, resolution, format, folder, pre-download check
    Settings → API keys, custom STAC, limits, help

A progress bar, status line and a single Cancel button stay visible under the tabs.
"""

import contextlib
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from qgis.core import (Qgis, QgsApplication, QgsContrastEnhancement, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsFeature, QgsField, QgsFields, QgsGeometry, QgsMultiBandColorRenderer, QgsProject, QgsRasterLayer,
                       QgsRasterMinMaxOrigin, QgsRectangle, QgsTask, QgsVectorLayer)
from qgis.gui import QgsDockWidget, QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QDate, QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDateEdit,
                                 QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                                 QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                                 QSizePolicy, QSlider, QSpinBox, QSplitter, QTabWidget, QToolButton,
                                 QVBoxLayout, QWidget)

from ...core.compat import scoped
from ...core.historical_imagery.compat import MSG_INFO, MSG_SUCCESS, MSG_WARNING, enum, log, polygon_geometry_type
from ...core.historical_imagery import providers as registry
from ...core.historical_imagery import quality, raster, settings
from ...core.historical_imagery.models import OUTPUT_FORMATS, RESOLUTION_CHOICES
from ...core.historical_imagery.tasks import PLUGIN_VERSION, DownloadTask, SearchTask, ThumbnailTask
from ..panel_bubble import ToolbarReveal
from .map_tools import (RectangleMapTool, aoi_bbox_from_layer, aoi_from_layer, aoi_from_rectangle, aoi_to_canvas_geometry,
                        footprint_to_canvas_geometry, geojson_to_wkt, make_rubber_band)
from .report_dialog import DownloadReportDialog
from .settings_panel import SettingsPanel

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))

USER_ROLE = enum(Qt, "ItemDataRole", "UserRole")
KEEP_ASPECT = enum(Qt, "AspectRatioMode", "KeepAspectRatio")
SMOOTH = enum(Qt, "TransformationMode", "SmoothTransformation")
HORIZONTAL = enum(Qt, "Orientation", "Horizontal")
VERTICAL = enum(Qt, "Orientation", "Vertical")
ALIGN_CENTER = enum(Qt, "AlignmentFlag", "AlignCenter")
ALIGN_TOP = enum(Qt, "AlignmentFlag", "AlignTop")
TEXT_UNDER = enum(Qt, "ToolButtonStyle", "ToolButtonTextUnderIcon")
EXTENDED_SEL = enum(QAbstractItemView, "SelectionMode", "ExtendedSelection")
NO_FRAME = enum(QFrame, "Shape", "NoFrame")
TICKS_BELOW = enum(QSlider, "TickPosition", "TicksBelow")
EXPANDING = enum(QSizePolicy, "Policy", "Expanding")
PREFERRED = enum(QSizePolicy, "Policy", "Preferred")
MB_YES = enum(QMessageBox, "StandardButton", "Yes")
MB_NO = enum(QMessageBox, "StandardButton", "No")
BYTE = enum(Qgis, "DataType", "Byte")
PREVIEW_PROP = "historical_imagery/preview"
PREVIEW_GROUP = "Imagery Preview"
PREVIEW_STRIPS = 8         # parallel reads of one preview
PREVIEW_PIXELS = 1400    # longest side of the local preview copy
PREVIEW_MAX = 4        # scenes drawn at once; every one is a remote read
SCROLL_OFF = enum(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff")
SHRINK_COMBO = enum(QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon")
TOOLTIP_ROLE = enum(Qt, "ItemDataRole", "ToolTipRole")
WRAP_LONG = enum(QFormLayout, "RowWrapPolicy", "WrapLongRows")
ALL_GROW = enum(QFormLayout, "FieldGrowthPolicy", "AllNonFixedFieldsGrow")

AOI_CANVAS, AOI_RECTANGLE, AOI_POLYGON = "canvas", "rectangle", "polygon"
SORT_MODES = [("Newest first", "newest"), ("Oldest first", "oldest"),
              ("Least cloudy first", "cloud"), ("Best AOI coverage first", "coverage")]
TAB_SEARCH, TAB_RESULTS, TAB_DOWNLOAD, TAB_SETTINGS = range(4)
THUMB_SIZE = 72
PREVIEW_SIZE = 128
LEVEL_COLOR = {"PASS": "#2b8a3e", "WARN": "#e67700", "FAIL": "#c92a2a"}  # nosec B105
LEVEL_SYMBOL = {"PASS": "✔", "WARN": "⚠", "FAIL": "✖"}  # nosec B105

STYLE = """
QPushButton#primary {
    background-color: palette(highlight); color: palette(highlighted-text);
    font-weight: 600; padding: 7px 14px; border: none; border-radius: 4px;
}
QPushButton#primary:hover { background-color: palette(dark); }
QPushButton#primary:disabled { background-color: palette(midlight); color: palette(mid); }
QLabel#hint { color: gray; }
QLabel#chip {
    background-color: palette(alternate-base); border: 1px solid palette(midlight);
    border-radius: 4px; padding: 5px 7px;
}
QToolButton#segment { padding: 4px 6px; border: 1px solid palette(mid); }
QToolButton#segment:checked { background-color: palette(highlight); color: palette(highlighted-text); }
QListWidget#results::item { padding: 3px; border-bottom: 1px solid palette(midlight); }
QListWidget#results::item:selected { background-color: palette(highlight); color: palette(highlighted-text); }
QFrame#preview { border: 1px solid palette(midlight); border-radius: 4px; }
"""


def _field_types():
    """(string, double) field types for QgsField on Qt5 (QVariant) and Qt6 (QMetaType)."""
    try:
        from qgis.PyQt.QtCore import QVariant
        if hasattr(QVariant, "String"):
            return getattr(QVariant, "String"), getattr(QVariant, "Double")
    except ImportError:
        pass
    from qgis.PyQt.QtCore import QMetaType
    return QMetaType.Type.QString, QMetaType.Type.Double


STR, DBL = _field_types()


def _polygon_filter():
    from qgis.core import QgsMapLayerProxyModel
    if hasattr(Qgis, "LayerFilter") and hasattr(Qgis.LayerFilter, "PolygonLayer"):
        return Qgis.LayerFilter.PolygonLayer
    return scoped(QgsMapLayerProxyModel, 'Filter', 'PolygonLayer')


def _range_limit_cumulative():
    if hasattr(Qgis, "RasterRangeLimit"):
        return Qgis.RasterRangeLimit.CumulativeCut
    return enum(QgsRasterMinMaxOrigin, "Limits", "CumulativeCut")


def _icon(name):
    return QgsApplication.getThemeIcon(name)


def _scrollable(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(NO_FRAME)
    area.setHorizontalScrollBarPolicy(SCROLL_OFF)  # content reflows instead of scrolling sideways
    area.setWidget(widget)
    return area


def _make_narrow_friendly(root):
    """Let combos/forms shrink so the dock works at ~320 px width."""
    for combo in root.findChildren(QComboBox):
        combo.setSizeAdjustPolicy(SHRINK_COMBO)
        combo.setMinimumContentsLength(6)
        combo.setSizePolicy(EXPANDING, PREFERRED)
        for i in range(combo.count()):
            if not combo.itemData(i, TOOLTIP_ROLE):
                combo.setItemData(i, combo.itemText(i), TOOLTIP_ROLE)
    for form in root.findChildren(QFormLayout):
        form.setRowWrapPolicy(WRAP_LONG)
        form.setFieldGrowthPolicy(ALL_GROW)


class _MapLayerCombo(QgsMapLayerComboBox):
    """Layer box that re-reads the active map's layers every time it is opened."""

    def __init__(self, refresh, parent=None):
        super().__init__(parent)
        self._refresh = refresh

    def showPopup(self):  # noqa: N802 (Qt API name)
        self._refresh()
        super().showPopup()


class HistoricalImageryDock(ToolbarReveal, QgsDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Satellite Imagery Archive", parent)
        self.setObjectName("HistoricalImageryDownloaderDock")
        self.iface = iface
        self.canvas = iface.mapCanvas()

        self.scenes = {}           # uid -> Scene (all results)
        self.order = []            # uids in search order
        self.thumbs = {}           # uid -> QPixmap
        self.search_task = None
        self.thumb_task = None
        self.download_task = None
        self._live_tasks = set()   # keep Python wrappers alive until QGIS is done with them
        self.search_aoi = None
        self.search_provider = None
        self.search_variant = None
        self.drawn_aoi = None
        self.prev_map_tool = None

        self.rect_tool = RectangleMapTool(self.canvas)
        self.rect_tool.rectangleCreated.connect(self._on_rectangle_drawn)
        self.rect_tool.toolDeactivated.connect(self._on_draw_tool_off)
        self.aoi_band = make_rubber_band(self.canvas, "#e8590c", (232, 89, 12, 25), 2)
        self.fp_band = make_rubber_band(self.canvas, "#1c7ed6", (28, 126, 214, 45), 2)

        self._precheck_timer = QTimer(self)
        self._precheck_timer.setSingleShot(True)
        self._precheck_timer.setInterval(250)
        self._precheck_timer.timeout.connect(self._update_precheck)

        self._preview_dir = None
        self._preview_task = None
        self._preview_gen = 0
        self._preview_cover = None
        self._poly_cache = None
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(900)   # let a pan or zoom settle before re-reading
        self._view_timer.timeout.connect(self._refresh_preview_for_view)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(350)   # arrow-keying down the list must not fire a read per row
        self._preview_timer.timeout.connect(self._update_map_preview)

        self._build_ui()
        self._load_state()
        self._watch_active_map()
        self.canvas.extentsChanged.connect(self._on_canvas_extent_changed)
        self.canvas.extentsChanged.connect(self._on_view_changed_for_preview)

    # =====================================================================
    # UI construction
    # =====================================================================
    def _build_ui(self):
        root = QWidget()
        root.setStyleSheet(STYLE)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.addTab(_scrollable(self._build_search_tab()), _icon("/search.svg"), "Search")
        self.tabs.addTab(self._build_results_tab(), _icon("/mIconRaster.svg"), "Results")
        self.tabs.addTab(_scrollable(self._build_download_tab()), _icon("/mActionFileSave.svg"), "Download")
        self.tabs.addTab(_scrollable(self._build_settings_tab()), _icon("/mActionOptions.svg"), "")
        self.tabs.setTabToolTip(TAB_SEARCH, "1 · Choose source, area and time period, then search")
        self.tabs.setTabToolTip(TAB_RESULTS, "2 · Browse, preview and select scenes")
        self.tabs.setTabToolTip(TAB_DOWNLOAD, "3 · Choose bands, resolution and format, then download")
        self.tabs.setTabToolTip(TAB_SETTINGS, "API keys, custom STAC catalog, limits and help")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.tabs, 1)
        _make_narrow_friendly(root)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setMaximumHeight(18)
        self.progress.setToolTip("Progress of the current search or download")
        self.cancel_btn = QToolButton()
        self.cancel_btn.setIcon(_icon("/mTaskCancel.svg"))
        self.cancel_btn.setToolTip("Cancel the running search or download")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_running)
        footer.addWidget(self.progress, 1)
        footer.addWidget(self.cancel_btn)
        outer.addLayout(footer)
        self.status = QLabel("Ready – choose a source and area, then press Search.")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.setWidget(root)

    # ---- tab 1: search ----------------------------------------------------
    def _build_search_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(8)

        box = QGroupBox("Imagery source")
        form = QFormLayout(box)
        self.provider_combo = QComboBox()
        self.provider_combo.setToolTip("Archive to search. Sentinel-2, Landsat and OpenAerialMap "
                                       "need no account.")
        for pid, prov in registry.providers().items():
            self.provider_combo.addItem(prov.name, pid)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("Source", self.provider_combo)
        self.variant_combo = QComboBox()
        self.variant_combo.setToolTip("Collection, mission or platform within the source.")
        self.variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        form.addRow("Collection", self.variant_combo)
        self.provider_info = QLabel()
        self.provider_info.setObjectName("hint")
        self.provider_info.setWordWrap(True)
        self.provider_info.setOpenExternalLinks(True)
        form.addRow(self.provider_info)
        lay.addWidget(box)

        box = QGroupBox("Area of interest")
        v = QVBoxLayout(box)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.aoi_group = QButtonGroup(self)
        self.aoi_group.setExclusive(True)
        self.aoi_buttons = {}
        for mode, text, icon, tip in (
                (AOI_CANVAS, "Map view", "/mActionPan.svg", "Use the current map canvas extent."),
                (AOI_RECTANGLE, "Draw box", "/mActionAddBasicRectangle.svg",
                 "Click and drag on the map to draw a rectangle."),
                (AOI_POLYGON, "Polygon", "/mIconPolygonLayer.svg",
                 "Use the features of a polygon layer (search + optional clipping).")):
            btn = QToolButton()
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setText(text)
            btn.setIcon(_icon(icon))
            btn.setToolButtonStyle(TEXT_UNDER)
            btn.setToolTip(tip)
            btn.setSizePolicy(EXPANDING, PREFERRED)
            self.aoi_group.addButton(btn)
            self.aoi_buttons[mode] = btn
            seg.addWidget(btn, 1)
            btn.toggled.connect(lambda checked: checked and self._on_aoi_mode_changed())
        v.addLayout(seg)

        self.draw_row = QWidget()
        dr = QHBoxLayout(self.draw_row)
        dr.setContentsMargins(0, 0, 0, 0)
        self.draw_btn = QPushButton("Draw on map")
        self.draw_btn.setCheckable(True)
        self.draw_btn.setIcon(_icon("/mActionAddBasicRectangle.svg"))
        self.draw_btn.setToolTip("Activate the drawing tool, then click and drag on the map.")
        self.draw_btn.toggled.connect(self._toggle_draw)
        dr.addWidget(self.draw_btn, 1)
        v.addWidget(self.draw_row)

        self.poly_row = QWidget()
        pr = QFormLayout(self.poly_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.layer_combo = _MapLayerCombo(self._scope_layers_to_active_map)
        self.layer_combo.setFilters(_polygon_filter())
        self.layer_combo.setToolTip("Polygon layer defining the AOI. Lists the layers of the active map.")
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        pr.addRow("Layer", self.layer_combo)
        self.selected_only = QCheckBox("Selected features only")
        self.selected_only.setToolTip("Use only the selected polygons.")
        self.selected_only.toggled.connect(lambda _c: self._update_aoi_label())
        pr.addRow(self.selected_only)
        v.addWidget(self.poly_row)

        chip_row = QHBoxLayout()
        self.aoi_label = QLabel("AOI: –")
        self.aoi_label.setObjectName("chip")
        self.aoi_label.setWordWrap(True)
        self.aoi_label.setToolTip("Area of interest in WGS 84 (lon/lat) and its approximate size.")
        self.show_aoi_btn = QToolButton()
        self.show_aoi_btn.setIcon(_icon("/mActionZoomToSelected.svg"))
        self.show_aoi_btn.setToolTip("Highlight (and zoom to) the AOI on the map.")
        self.show_aoi_btn.clicked.connect(self._show_aoi)
        chip_row.addWidget(self.aoi_label, 1)
        chip_row.addWidget(self.show_aoi_btn)
        v.addLayout(chip_row)
        lay.addWidget(box)

        box = QGroupBox("Time period && quality")
        form = QFormLayout(box)
        self.year_combo = QComboBox()
        self.year_combo.setToolTip("Pick a year (1 Jan – 31 Dec) or keep a custom date range.")
        self.year_combo.currentIndexChanged.connect(self._on_year_changed)
        form.addRow("Year", self.year_combo)
        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        for w, tip in ((self.start_date, "First acquisition date (inclusive)."),
                       (self.end_date, "Last acquisition date (inclusive).")):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.setToolTip(tip)
            w.dateChanged.connect(self._on_dates_edited)
        form.addRow("From", self.start_date)
        form.addRow("To", self.end_date)

        cloud_row = QHBoxLayout()
        self.cloud_slider = QSlider(HORIZONTAL)
        self.cloud_slider.setRange(0, 100)
        self.cloud_slider.setPageStep(10)
        self.cloud_slider.setTickInterval(10)
        self.cloud_slider.setTickPosition(TICKS_BELOW)
        self.cloud_slider.setToolTip("Maximum scene cloud cover accepted.")
        self.cloud_value = QLabel("30 %")
        self.cloud_value.setMinimumWidth(40)
        self.cloud_slider.valueChanged.connect(lambda val: self.cloud_value.setText("{} %".format(val)))
        cloud_row.addWidget(self.cloud_slider, 1)
        cloud_row.addWidget(self.cloud_value)
        form.addRow("Max cloud", cloud_row)

        self.coverage_spin = QSpinBox()
        self.coverage_spin.setRange(0, 100)
        self.coverage_spin.setSuffix(" % of AOI")
        self.coverage_spin.setToolTip("Hide scenes covering less than this share of the AOI.")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 500)
        self.limit_spin.setValue(50)
        self.limit_spin.setPrefix("up to ")
        self.limit_spin.setSuffix(" scenes")
        self.limit_spin.setToolTip("Maximum number of scenes returned.")
        form.addRow("Min coverage", self.coverage_spin)
        form.addRow("Results", self.limit_spin)
        lay.addWidget(box)

        self.search_btn = QPushButton("  Search imagery")
        self.search_btn.setObjectName("primary")
        self.search_btn.setIcon(_icon("/search.svg"))
        self.search_btn.setToolTip("Search the selected archive in the background.")
        self.search_btn.clicked.connect(self._search)
        lay.addWidget(self.search_btn)
        lay.addStretch(1)
        return page

    # ---- tab 2: results ---------------------------------------------------
    def _build_results_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(6)

        self.result_count = QLabel("No search yet.")
        self.result_count.setObjectName("hint")
        self.result_count.setWordWrap(True)
        top = QHBoxLayout()
        self.year_filter = QComboBox()
        self.year_filter.setToolTip("Show only scenes from one year.")
        self.year_filter.currentIndexChanged.connect(self._refresh_list)
        self.sort_combo = QComboBox()
        for label, key in SORT_MODES:
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip("Order of the result list.")
        self.sort_combo.currentIndexChanged.connect(self._refresh_list)
        top.addWidget(self.year_filter, 1)
        top.addWidget(self.sort_combo, 1)
        lay.addWidget(self.result_count)
        lay.addLayout(top)

        split = QSplitter(VERTICAL)
        self.result_list = QListWidget()
        self.result_list.setObjectName("results")
        self.result_list.setSelectionMode(EXTENDED_SEL)
        self.result_list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.result_list.setWordWrap(True)
        self.result_list.setUniformItemSizes(True)
        self.result_list.setToolTip("Ctrl/Shift-click to select several scenes. Double-click to zoom.")
        self.result_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.result_list.itemDoubleClicked.connect(lambda _i: self._zoom_to_selection())
        split.addWidget(self.result_list)

        prev = QFrame()
        prev.setObjectName("preview")
        ph = QHBoxLayout(prev)
        ph.setContentsMargins(6, 6, 6, 6)
        self.preview = QLabel("Select a scene\nto preview it.")
        self.preview.setAlignment(ALIGN_CENTER)
        self.preview.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.preview.setObjectName("hint")
        self.preview_meta = QLabel()
        self.preview_meta.setWordWrap(True)
        self.preview_meta.setAlignment(ALIGN_TOP)
        self.preview_meta.setOpenExternalLinks(True)
        ph.addWidget(self.preview)
        ph.addWidget(self.preview_meta, 1)
        split.addWidget(prev)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)

        row = QHBoxLayout()
        self.select_all_btn = QToolButton()
        self.select_all_btn.setIcon(_icon("/mActionSelectAll.svg"))
        self.select_all_btn.setToolTip("Select all listed scenes")
        self.select_all_btn.clicked.connect(self.result_list.selectAll)
        self.zoom_btn = QToolButton()
        self.zoom_btn.setIcon(_icon("/mActionZoomToSelected.svg"))
        self.zoom_btn.setToolTip("Zoom to the selected footprints")
        self.zoom_btn.clicked.connect(self._zoom_to_selection)
        self.footprints_btn = QToolButton()
        self.footprints_btn.setIcon(_icon("/mActionAddLayer.svg"))
        self.footprints_btn.setToolTip("Add all footprints as a temporary layer (date, cloud, coverage)")
        self.footprints_btn.clicked.connect(self._add_footprints_layer)
        self.map_preview_btn = QToolButton()
        self.map_preview_btn.setIcon(_icon("/mActionShowAllLayers.svg"))
        self.map_preview_btn.setCheckable(True)
        self.map_preview_btn.setToolTip(
            "Preview the selected scene on the map (up to {} at once). Only the part you are "
            "looking at is read. The preview layer is temporary and is removed when you turn "
            "this off.".format(PREVIEW_MAX))
        self.map_preview_btn.toggled.connect(self._on_map_preview_toggled)
        self.next_btn = QPushButton("Continue to download  ›")
        self.next_btn.setObjectName("primary")
        self.next_btn.setEnabled(False)
        self.next_btn.setToolTip("Go to the Download tab with the selected scenes.")
        self.next_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_DOWNLOAD))
        row.addWidget(self.select_all_btn)
        row.addWidget(self.zoom_btn)
        row.addWidget(self.footprints_btn)
        row.addWidget(self.map_preview_btn)
        row.addStretch(1)
        row.addWidget(self.next_btn)
        lay.addLayout(row)
        return page

    # ---- tab 3: download --------------------------------------------------
    def _build_download_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(8)

        self.selection_chip = QLabel("No scenes selected – pick scenes in the Results tab.")
        self.selection_chip.setObjectName("chip")
        self.selection_chip.setWordWrap(True)
        lay.addWidget(self.selection_chip)

        box = QGroupBox("Output")
        form = QFormLayout(box)
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Bands to stack; the first three are displayed as R, G, B.")
        self.preset_combo.currentIndexChanged.connect(self._schedule_precheck)
        form.addRow("Bands", self.preset_combo)
        self.res_combo = QComboBox()
        for label, kind, value in RESOLUTION_CHOICES:
            self.res_combo.addItem(label, (kind, value))
        self.res_combo.setToolTip("Native keeps the original pixels (verified pixel-by-pixel); "
                                  "other options resample (average when coarsening).")
        self.res_combo.currentIndexChanged.connect(self._schedule_precheck)
        form.addRow("Resolution", self.res_combo)
        self.format_combo = QComboBox()
        for label, key in OUTPUT_FORMATS:
            self.format_combo.addItem(label, key)
        self.format_combo.setToolTip("GeoTIFF is universal; COG is web-ready with internal overviews.")
        form.addRow("Format", self.format_combo)
        self.clip_check = QCheckBox("Clip to polygon shape")
        self.clip_check.setToolTip("Pixels outside the AOI polygon become NoData "
                                   "(only with a polygon-layer AOI).")
        form.addRow("", self.clip_check)
        self.out_dir = QgsFileWidget()
        self.out_dir.setStorageMode(enum(QgsFileWidget, "StorageMode", "GetDirectory"))
        self.out_dir.setDialogTitle("Choose output folder")
        self.out_dir.setToolTip("Folder for the rasters and their .json sidecars.")
        form.addRow("Folder", self.out_dir)
        lay.addWidget(box)

        box = QGroupBox("After download")
        v = QVBoxLayout(box)
        self.autoload_check = QCheckBox("Add to map (auto contrast)")
        self.autoload_check.setToolTip("Load outputs into a 'Satellite Imagery Archive' layer group, shown as "
                                       "true colour with a 2–98 % cumulative-cut stretch.")
        self.sidecar_check = QCheckBox("Write .json metadata")
        self.sidecar_check.setToolTip("Date, satellite, cloud %, source URLs, processing and "
                                      "quality report next to each raster.")
        self.report_check = QCheckBox("Show quality report")
        self.report_check.setToolTip("Open a report listing the quality checks of every file.")
        for w in (self.autoload_check, self.sidecar_check, self.report_check):
            v.addWidget(w)
        lay.addWidget(box)

        box = QGroupBox("Pre-download check")
        v = QVBoxLayout(box)
        self.precheck_summary = QLabel("–")
        self.precheck_summary.setWordWrap(True)
        self.precheck_msgs = QLabel()
        self.precheck_msgs.setWordWrap(True)
        self.precheck_msgs.setTextFormat(enum(Qt, "TextFormat", "RichText"))
        v.addWidget(self.precheck_summary)
        v.addWidget(self.precheck_msgs)
        lay.addWidget(box)

        row = QHBoxLayout()
        self.download_btn = QPushButton("  Download")
        self.download_btn.setObjectName("primary")
        self.download_btn.setIcon(_icon("/mActionFileSave.svg"))
        self.download_btn.setToolTip("Download, clip, resample and quality-check the selected scenes.")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._download)
        self.open_dir_btn = QToolButton()
        self.open_dir_btn.setIcon(_icon("/mActionFileOpen.svg"))
        self.open_dir_btn.setToolTip("Open the output folder")
        self.open_dir_btn.clicked.connect(self._open_output_dir)
        row.addWidget(self.download_btn, 1)
        row.addWidget(self.open_dir_btn)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    # ---- tab 4: settings --------------------------------------------------
    def _build_settings_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        self.settings_panel = SettingsPanel()
        self.settings_panel.saved.connect(self._on_settings_saved)
        lay.addWidget(self.settings_panel)
        about = QGroupBox("About")
        v = QVBoxLayout(about)
        lbl = QLabel("<b>Satellite Imagery Archive {}</b><br>Sentinel-2 © Copernicus · "
                     "Landsat courtesy USGS · OpenAerialMap CC-BY 4.0 · Earth Engine per-dataset "
                     "terms. Please cite the data providers.".format(PLUGIN_VERSION))
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        help_btn = QPushButton("Help")
        help_btn.setIcon(_icon("/mActionHelpContents.svg"))
        help_btn.clicked.connect(self._open_help)
        log_btn = QPushButton("Log")
        log_btn.setToolTip("Detailed errors are written to the Log Messages panel.")
        log_btn.clicked.connect(self._open_log)
        h = QHBoxLayout()
        h.addWidget(help_btn)
        h.addWidget(log_btn)
        v.addLayout(h)
        lay.addWidget(about)
        lay.addStretch(1)
        return page

    # =====================================================================
    # State
    # =====================================================================
    def _load_state(self):
        idx = self.provider_combo.findData(settings.get("last_provider"))
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(max(idx, 0))
        self.provider_combo.blockSignals(False)
        today = date.today()
        self.end_date.setDate(QDate(today.year, today.month, today.day))
        self.start_date.setDate(QDate(today.year - 1, today.month, today.day))
        self._on_provider_changed()
        self.cloud_slider.setValue(settings.get("last_cloud"))
        fidx = self.format_combo.findData(settings.get("last_format"))
        self.format_combo.setCurrentIndex(max(fidx, 0))
        self.autoload_check.setChecked(settings.get("auto_load"))
        self.sidecar_check.setChecked(settings.get("write_sidecar"))
        self.report_check.setChecked(settings.get("show_report"))
        out = settings.get("last_output_dir") or os.path.join(os.path.expanduser("~"), "SatelliteImageryArchive")
        self.out_dir.setFilePath(out)
        self.aoi_buttons[AOI_CANVAS].setChecked(True)
        self._on_aoi_mode_changed()
        self._refresh_year_filter()

    def _save_state(self):
        settings.set("last_provider", self.provider_combo.currentData())
        settings.set("last_cloud", self.cloud_slider.value())
        settings.set("last_format", self.format_combo.currentData())
        settings.set("auto_load", self.autoload_check.isChecked())
        settings.set("write_sidecar", self.sidecar_check.isChecked())
        settings.set("show_report", self.report_check.isChecked())
        settings.set("last_output_dir", self.out_dir.filePath())

    def provider(self):
        return registry.get(self.provider_combo.currentData())

    def variant(self):
        return self.variant_combo.currentData() or ""

    def aoi_mode(self):
        for mode, btn in self.aoi_buttons.items():
            if btn.isChecked():
                return mode
        return AOI_CANVAS

    def set_aoi_mode(self, mode):
        self.aoi_buttons[mode].setChecked(True)

    def _busy(self):
        return self.search_task is not None or self.download_task is not None

    def _track(self, task, show_progress=True):
        """Keep a reference to a running task until QGIS reports it finished, then start it."""
        self._live_tasks.add(task)
        task.taskCompleted.connect(lambda t=task: self._live_tasks.discard(t))
        task.taskTerminated.connect(lambda t=task: self._live_tasks.discard(t))
        if show_progress:
            task.progressChanged.connect(lambda p, t=task: self._on_task_progress(t, p))
        QgsApplication.taskManager().addTask(task)

    def _on_task_progress(self, task, value):
        if task is self.search_task or task is self.download_task:
            self.progress.setValue(int(value))

    def _set_running(self, running):
        self.cancel_btn.setEnabled(running)
        self.search_btn.setEnabled(not running)
        self.provider_combo.setEnabled(not running)
        self.variant_combo.setEnabled(not running and self.variant_combo.count() > 1)
        self._update_download_enabled()
        if running:
            self.progress.setValue(0)

    # =====================================================================
    # Search tab
    # =====================================================================
    def _on_provider_changed(self, *_):
        prov = self.provider()
        if prov is None:
            return
        self.variant_combo.blockSignals(True)
        self.variant_combo.clear()
        for label, key in prov.variants():
            self.variant_combo.addItem(label, key)
        self.variant_combo.blockSignals(False)
        self.variant_combo.setEnabled(self.variant_combo.count() > 1 and not self._busy())
        link = ' <a href="{0}">More info</a>'.format(prov.homepage) if prov.homepage else ""
        self.provider_info.setText(prov.description + link)
        self.cloud_slider.setEnabled(prov.supports_cloud_filter)
        self.cloud_value.setEnabled(prov.supports_cloud_filter)
        self.cloud_slider.setToolTip("Maximum scene cloud cover accepted." if prov.supports_cloud_filter
                                     else "This source has no cloud-cover metadata.")
        self._on_variant_changed()

    def _on_variant_changed(self, *_):
        prov = self.provider()
        if prov is None:
            return
        variant = self.variant()
        self.variant_combo.setToolTip(prov.variant_tooltip(variant) or
                                      "Collection, mission or platform within the source.")
        self._populate_years(prov.min_date(variant))
        # Results and band presets belong to one source: a new source needs a new search.
        if self.scenes and (prov is not self.search_provider or variant != self.search_variant):
            self._clear_results("Source changed – run a new search.")
        self._fill_presets(prov, variant)

    def _fill_presets(self, prov, variant):
        current = self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for name in prov.band_presets(variant).keys():
            self.preset_combo.addItem(name)
        i = self.preset_combo.findText(current)
        self.preset_combo.setCurrentIndex(i if i >= 0 else 0)
        self.preset_combo.blockSignals(False)

    def _populate_years(self, min_date):
        current = self.year_combo.currentData()
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        self.year_combo.addItem("Custom date range", None)
        for y in range(date.today().year, min_date.year - 1, -1):
            self.year_combo.addItem(str(y), y)
        i = self.year_combo.findData(current)
        self.year_combo.setCurrentIndex(i if i > 0 else 0)
        self.year_combo.blockSignals(False)
        today = date.today()
        qmin = QDate(min_date.year, min_date.month, min_date.day)
        qmax = QDate(today.year, today.month, today.day)
        for w in (self.start_date, self.end_date):
            w.blockSignals(True)
            w.setDateRange(qmin, qmax)
            w.blockSignals(False)

    def _on_year_changed(self, *_):
        year = self.year_combo.currentData()
        if year is None:
            return
        today = date.today()
        end = date(year, 12, 31) if year < today.year else today
        for w, d in ((self.start_date, date(year, 1, 1)), (self.end_date, end)):
            w.blockSignals(True)
            w.setDate(QDate(d.year, d.month, d.day))
            w.blockSignals(False)

    def _on_dates_edited(self, *_):
        s, e = self.start_date.date().toPyDate(), self.end_date.date().toPyDate()
        year = self.year_combo.currentData()
        if year is not None and not (s == date(year, 1, 1) and e.year == year):
            self.year_combo.blockSignals(True)
            self.year_combo.setCurrentIndex(0)
            self.year_combo.blockSignals(False)

    def _on_aoi_mode_changed(self):
        mode = self.aoi_mode()
        self.draw_row.setVisible(mode == AOI_RECTANGLE)
        self.poly_row.setVisible(mode == AOI_POLYGON)
        if mode != AOI_RECTANGLE and self.draw_btn.isChecked():
            self.draw_btn.setChecked(False)
        if mode == AOI_RECTANGLE and self.drawn_aoi is None and self.isVisible():
            self.draw_btn.setChecked(True)  # start drawing straight away
        self._update_clip_enabled()
        self._update_aoi_label()

    # ---- layers of the active map -------------------------------------------
    def _watch_active_map(self):
        """Keep the layer list in step with the active map (Map Tabs), if that feature is on."""
        self._scope_timer = QTimer(self)
        self._scope_timer.setSingleShot(True)
        self._scope_timer.setInterval(150)
        self._scope_timer.timeout.connect(self._scope_layers_to_active_map)
        root = QgsProject.instance().layerTreeRoot()
        # Switching map tabs turns the maps' groups on and off; layers move in and out of them.
        for signal in (root.visibilityChanged, root.addedChildren, root.removedChildren):
            signal.connect(self._schedule_layer_scope)
        with contextlib.suppress(ImportError):
            from .. import map_tabs
            map_tabs.add_listener(self._schedule_layer_scope)
        self._scope_layers_to_active_map()

    def _unwatch_active_map(self):
        self._scope_timer.stop()
        root = QgsProject.instance().layerTreeRoot()
        for signal in (root.visibilityChanged, root.addedChildren, root.removedChildren):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(self._schedule_layer_scope)
        with contextlib.suppress(ImportError):
            from .. import map_tabs
            map_tabs.remove_listener(self._schedule_layer_scope)

    def _schedule_layer_scope(self, *_args):
        self._scope_timer.start()

    def _scope_layers_to_active_map(self):
        """List only the layers inside the active map; every layer while Map Tabs is off."""
        ids = None
        with contextlib.suppress(ImportError):
            from .. import map_tabs
            ids = map_tabs.active_map_layer_ids()
        hidden = []
        if ids is not None:
            hidden = [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.id() not in ids]
        current = self.layer_combo.currentLayer()
        self.layer_combo.setExceptedLayerList(hidden)
        if self.layer_combo.currentLayer() is not current:
            self._poly_cache = None       # the box now points at another layer

    def _on_layer_changed(self, _layer):
        self._update_clip_enabled()
        self._update_aoi_label()

    def _on_canvas_extent_changed(self):
        if self.aoi_mode() == AOI_CANVAS and self.isVisible():
            self._update_aoi_label()

    def _update_clip_enabled(self):
        ok = self.aoi_mode() == AOI_POLYGON and self.layer_combo.currentLayer() is not None
        self.clip_check.setEnabled(ok)
        if not ok:
            self.clip_check.setChecked(False)

    def _toggle_draw(self, checked):
        if checked:
            current = self.canvas.mapTool()
            if current is not self.rect_tool:
                self.prev_map_tool = current
            self.canvas.setMapTool(self.rect_tool)
            self.status.setText("Click and drag on the map to draw the area of interest.")
        elif self.canvas.mapTool() is self.rect_tool:
            self.canvas.unsetMapTool(self.rect_tool)
            if self.prev_map_tool is not None:
                try:
                    self.canvas.setMapTool(self.prev_map_tool)
                except RuntimeError:  # previous tool was deleted
                    pass
            self.prev_map_tool = None

    def _on_draw_tool_off(self):
        if self.draw_btn.isChecked():
            self.draw_btn.blockSignals(True)
            self.draw_btn.setChecked(False)
            self.draw_btn.blockSignals(False)

    def _on_rectangle_drawn(self, rect):
        try:
            self.drawn_aoi = aoi_from_rectangle(rect, self.canvas.mapSettings().destinationCrs(),
                                                source="rectangle")
        except ValueError as exc:
            self._warn("Area of interest", str(exc))
            return
        self.draw_btn.setChecked(False)
        self.draw_btn.setText("Redraw on map")
        self._draw_aoi(self.drawn_aoi)
        self._update_aoi_label()
        self.status.setText("AOI rectangle set.")

    def current_aoi(self, quick=False):
        """Build the AOI for the selected mode. Raises ValueError with a friendly message.

        `quick` skips building the polygon's shape (the box is enough for the area label and
        the size estimate); the shape is built once and reused until the layer changes.
        """
        mode = self.aoi_mode()
        if mode == AOI_CANVAS:
            return aoi_from_rectangle(self.canvas.extent(), self.canvas.mapSettings().destinationCrs(),
                                      source="extent")
        if mode == AOI_RECTANGLE:
            if self.drawn_aoi is None:
                raise ValueError("Draw a rectangle on the map first.")
            return self.drawn_aoi
        layer = self.layer_combo.currentLayer()
        selected = self.selected_only.isChecked()
        if quick:
            return aoi_bbox_from_layer(layer, selected)
        key = self._polygon_key(layer, selected)
        if key is not None and self._poly_cache is not None and self._poly_cache[0] == key:
            return self._poly_cache[1]
        aoi = aoi_from_layer(layer, selected)
        self._poly_cache = (key, aoi) if key is not None else None
        return aoi

    @staticmethod
    def _polygon_key(layer, selected):
        """What the polygon's shape depends on; a changed key means rebuild it."""
        if layer is None:
            return None
        try:
            return (layer.id(), selected, layer.featureCount(), layer.subsetString(), layer.crs().authid(),
                    tuple(sorted(layer.selectedFeatureIds())) if selected else (),
                    layer.extent().toString(6), layer.isEditable() and layer.undoStack().index())
        except (RuntimeError, AttributeError):
            return None

    def _update_aoi_label(self):
        try:
            aoi = self.current_aoi(quick=True)
            x0, y0, x1, y1 = aoi.bbox
            self.aoi_label.setText("<b>≈ {:,.1f} km²</b><br>W {:.4f} · S {:.4f}<br>E {:.4f} · N {:.4f}".format(
                aoi.approx_area_km2(), x0, y0, x1, y1))
        except ValueError as exc:
            self.aoi_label.setText(str(exc))
        except Exception:  # noqa: BLE001 - informative only
            self.aoi_label.setText("AOI: –")
        self._schedule_precheck()

    def _draw_aoi(self, aoi):
        self.aoi_band.setToGeometry(aoi_to_canvas_geometry(aoi, self.canvas), None)
        self.aoi_band.show()

    def _show_aoi(self):
        try:
            aoi = self.current_aoi()
        except ValueError as exc:
            self._warn("Area of interest", str(exc))
            return
        self._draw_aoi(aoi)
        if self.aoi_mode() != AOI_CANVAS:
            ext = aoi_to_canvas_geometry(aoi, self.canvas).boundingBox()
            ext.scale(1.2)
            self.canvas.setExtent(ext)
            self.canvas.refresh()

    def _search(self):
        prov = self.provider()
        problem = prov.check_available()
        if problem:
            self._warn("{} is not ready".format(prov.name), problem)
            if prov.id in ("gee", "custom_stac"):
                self.tabs.setCurrentIndex(TAB_SETTINGS)
            return
        try:
            aoi = self.current_aoi()
        except ValueError as exc:
            self._warn("Area of interest", str(exc))
            return
        start = self.start_date.date().toPyDate()
        end = self.end_date.date().toPyDate()
        if start > end:
            self._warn("Dates", "The start date must be before the end date.")
            return
        if aoi.approx_area_km2() > 2_000_000:
            self._warn("Area of interest too large",
                       "The AOI is about {:,.0f} km². Zoom in or use an area under 2 million km² "
                       "so the catalog can answer quickly.".format(aoi.approx_area_km2()))
            return

        self._cancel_search_tasks()
        self._save_state()
        self._draw_aoi(aoi)
        self._clear_results("Searching…")
        self.search_aoi = aoi
        self.search_provider = prov
        self.search_variant = self.variant()

        task = SearchTask(prov, aoi, start, end, float(self.cloud_slider.value()),
                          self.limit_spin.value(), self.variant(), float(self.coverage_spin.value()),
                          "newest")
        task.searchFinished.connect(lambda scenes, t=task: self._on_search_finished(t, scenes))
        task.searchFailed.connect(lambda title, msg, t=task: self._on_search_failed(t, title, msg))
        task.searchCanceled.connect(lambda t=task: self._on_search_canceled(t))
        self.search_task = task
        self._set_running(True)
        self.status.setText("Searching {} …".format(prov.name))
        self._track(task)

    def _cancel_search_tasks(self):
        for task in (self.search_task, self.thumb_task):
            if task is not None:
                try:
                    task.cancel()
                except RuntimeError:  # C++ object already deleted
                    pass
        self.search_task = None
        self.thumb_task = None

    def _cancel_running(self):
        if self.download_task is not None:
            try:
                self.download_task.cancel()
            except RuntimeError:
                pass
            self.status.setText("Canceling download – files already finished are kept…")
        elif self.search_task is not None:
            self._cancel_search_tasks()
            self._set_running(False)
            self.progress.setValue(0)
            self.result_count.setText("Search canceled.")
            self.status.setText("Search canceled.")

    def _on_search_finished(self, task, scenes):
        if task is not self.search_task:
            return  # result of an older, superseded search
        self.search_task = None
        self._set_running(False)
        self.progress.setValue(100)
        for scene in scenes:
            self.scenes[scene.uid] = scene
            self.order.append(scene.uid)
        self._refresh_year_filter()
        self._refresh_list()
        n = len(scenes)
        self.tabs.setTabText(TAB_RESULTS, "Results ({})".format(n))
        if n == 0:
            self.result_count.setText("No scenes found.")
            self.status.setText("No scenes found.")
            QMessageBox.information(
                self, "No imagery found",
                "No scenes matched your filters.\n\nTry:\n• a wider date range or another year\n"
                "• a higher maximum cloud cover\n• a lower minimum AOI coverage\n"
                "• another collection or data source\n• a different area of interest")
            return
        self.tabs.setCurrentIndex(TAB_RESULTS)
        self.status.setText("{} scene(s) found – loading thumbnails…".format(n))
        thumb = ThumbnailTask(self.search_provider, scenes)
        thumb.thumbnailReady.connect(self._on_thumbnail)
        thumb.taskCompleted.connect(lambda t=thumb, n=n: self._on_thumbs_done(t, n))
        self.thumb_task = thumb
        self._track(thumb, show_progress=False)
        self.result_list.setCurrentRow(0)

    def _on_thumbs_done(self, task, n):
        if task is self.thumb_task:
            self.thumb_task = None
            if not self._busy():
                self.status.setText("{} scene(s) found. Select scenes, then Continue.".format(n))

    def _on_search_failed(self, task, title, message):
        if task is not self.search_task:
            return
        self.search_task = None
        self._set_running(False)
        self.progress.setValue(0)
        self.result_count.setText("Search failed – see the message.")
        self.status.setText(title)
        self._error(title, message)

    def _on_search_canceled(self, task):
        if task is not self.search_task:
            return
        self.search_task = None
        self._set_running(False)
        self.progress.setValue(0)
        self.status.setText("Search canceled.")

    # =====================================================================
    # Results tab
    # =====================================================================
    def _clear_results(self, message=""):
        self.scenes.clear()
        self.order = []
        self.thumbs.clear()
        self.result_list.clear()
        self.fp_band.reset(polygon_geometry_type())
        self._remove_preview_layers()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Select a scene\nto preview it.")
        self.preview_meta.clear()
        self.result_count.setText(message or "No search yet.")
        self.tabs.setTabText(TAB_RESULTS, "Results")
        self._refresh_year_filter()
        self._on_selection_changed()

    def _refresh_year_filter(self):
        current = self.year_filter.currentData()
        years = {}
        for s in self.scenes.values():
            if s.year:
                years[s.year] = years.get(s.year, 0) + 1
        self.year_filter.blockSignals(True)
        self.year_filter.clear()
        self.year_filter.addItem("All years ({})".format(len(self.scenes)), None)
        for y in sorted(years, reverse=True):
            self.year_filter.addItem("{} ({})".format(y, years[y]), y)
        i = self.year_filter.findData(current)
        self.year_filter.setCurrentIndex(i if i >= 0 else 0)
        self.year_filter.blockSignals(False)
        self.year_filter.setEnabled(len(years) > 1)

    def _visible_scenes(self):
        year = self.year_filter.currentData()
        scenes = [self.scenes[u] for u in self.order if u in self.scenes]
        if year is not None:
            scenes = [s for s in scenes if s.year == year]
        mode = self.sort_combo.currentData()
        if mode == "oldest":
            scenes.sort(key=lambda s: s.datetime)
        elif mode == "cloud":
            scenes.sort(key=lambda s: (s.cloud_cover if s.cloud_cover is not None else 999, s.datetime))
        elif mode == "coverage":
            scenes.sort(key=lambda s: -(s.aoi_coverage or 0))
        else:
            scenes.sort(key=lambda s: s.datetime, reverse=True)
        return scenes

    def _refresh_list(self, *_):
        selected = {i.data(USER_ROLE) for i in self.result_list.selectedItems()}
        self.result_list.blockSignals(True)
        self.result_list.clear()
        placeholder = _icon("/mIconRaster.svg")
        visible = self._visible_scenes()
        row_h = max(THUMB_SIZE + 10, self.result_list.fontMetrics().lineSpacing() * 3 + 12)
        for scene in visible:
            pix = self.thumbs.get(scene.uid)
            icon = QIcon(pix.scaled(THUMB_SIZE, THUMB_SIZE, KEEP_ASPECT, SMOOTH)) if pix else placeholder
            item = QListWidgetItem(icon, scene.label(short=True))
            item.setData(USER_ROLE, scene.uid)
            item.setToolTip("{}\nCollection: {}\nAcquired: {}".format(
                scene.scene_id, scene.collection, scene.datetime))
            item.setSizeHint(QSize(0, row_h))
            self.result_list.addItem(item)
            if scene.uid in selected:
                item.setSelected(True)
        self.result_list.blockSignals(False)
        if self.scenes:
            self.result_count.setText("{} of {} scene{} · Ctrl/Shift-click to select several".format(
                len(visible), len(self.scenes), "" if len(self.scenes) == 1 else "s"))
        self._on_selection_changed()

    def _on_thumbnail(self, uid, data):
        if uid not in self.scenes:
            return  # thumbnail of an older search
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        self.thumbs[uid] = pix
        for i in range(self.result_list.count()):
            item = self.result_list.item(i)
            if item.data(USER_ROLE) == uid:
                item.setIcon(QIcon(pix.scaled(THUMB_SIZE, THUMB_SIZE, KEEP_ASPECT, SMOOTH)))
                if item.isSelected() and len(self.result_list.selectedItems()) == 1:
                    self._show_preview(self.scenes[uid])
                break

    def selected_scenes(self):
        return [self.scenes[i.data(USER_ROLE)] for i in self.result_list.selectedItems()
                if i.data(USER_ROLE) in self.scenes]

    def _on_selection_changed(self):
        scenes = self.selected_scenes()
        self.fp_band.reset(polygon_geometry_type())
        for scene in scenes:
            geom = footprint_to_canvas_geometry(scene.footprint, self.canvas)
            if geom is not None:
                self.fp_band.addGeometry(geom, None)
        self.fp_band.show()
        if self.map_preview_btn.isChecked():
            self._preview_timer.start()
        if len(scenes) == 1:
            self._show_preview(scenes[0])
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("{} scenes\nselected".format(len(scenes)) if scenes
                                 else "Select a scene\nto preview it.")
            self.preview_meta.clear()
        n = len(scenes)
        self.next_btn.setEnabled(bool(scenes))
        self.next_btn.setText("Continue with {} scene{}  ›".format(n, "" if n == 1 else "s")
                              if scenes else "Continue to download  ›")
        if scenes:
            dates = sorted(s.date for s in scenes)
            span = dates[0] if dates[0] == dates[-1] else "{} … {}".format(dates[0], dates[-1])
            self.selection_chip.setText("<b>{} scene{}</b> selected · {}<br>{}".format(
                n, "" if n == 1 else "s", span,
                self.search_provider.name if self.search_provider else ""))
        else:
            self.selection_chip.setText("No scenes selected – pick scenes in the Results tab.")
        self._update_download_enabled()
        self._schedule_precheck()

    def _show_preview(self, scene):
        pix = self.thumbs.get(scene.uid)
        if pix is not None:
            self.preview.setPixmap(pix.scaled(PREVIEW_SIZE, PREVIEW_SIZE, KEEP_ASPECT, SMOOTH))
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Thumbnail\nloading or\nunavailable")
        rows = [("Date", scene.datetime.replace("T", " ").replace("Z", " UTC")[:23]),
                ("Satellite", scene.platform or "–"), ("Tile", scene.tile or "–"),
                ("Cloud", "{:.1f} %".format(scene.cloud_cover) if scene.cloud_cover is not None else "n/a"),
                ("AOI cover", "{:.0f} %".format(scene.aoi_coverage) if scene.aoi_coverage is not None else "n/a"),
                ("GSD", "{:g} m".format(scene.gsd) if scene.gsd else "n/a")]
        html = "".join("<tr><td style='color:gray'>{}&nbsp;</td><td>{}</td></tr>".format(k, v)
                       for k, v in rows)
        link = ""
        if scene.self_url and scene.self_url.startswith("http"):
            link = '<a href="{}">Source metadata</a>'.format(scene.self_url)
        self.preview_meta.setText("<b>{}</b><table>{}</table>{}".format(
            scene.scene_id[:48], html, link))

    # ---- preview on the map ----------------------------------------------
    def _on_map_preview_toggled(self, on):
        if on:
            self._update_map_preview()
        else:
            self._preview_timer.stop()
            self._preview_gen += 1          # a task still running will be ignored when it ends
            self._preview_cover = None
            self._remove_preview_layers()

    def _remove_preview_layers(self):
        project = QgsProject.instance()
        ids = [lid for lid, lyr in project.mapLayers().items()
               if lyr.customProperty(PREVIEW_PROP)]
        if ids:
            project.removeMapLayers(ids)
        root = project.layerTreeRoot()
        group = root.findGroup(PREVIEW_GROUP)
        if group is not None and not group.findLayers():
            root.removeChildNode(group)

    def _preview_job(self, task, jobs, workdir):
        """Worker thread: sign the URLs and wrap each scene in a small local VRT.

        Opening a remote COG straight from QGIS makes GDAL probe ~40 sidecar files one by one
        (.aux.xml, _rpc.txt ...), which took ~9 s. A VRT built here with directory probing off
        opens instantly and QGIS never touches the remote file's neighbours.
        """
        from osgeo import gdal
        # Only the directory probing is switched off: the HTTP/2 and multiplexing options the
        # downloader uses are not needed to open a file and can fail behind some proxies.
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        gdal.SetThreadLocalConfigOption("GDAL_HTTP_USERAGENT", raster.GDAL_HTTP_CONFIG["GDAL_HTTP_USERAGENT"])
        out = []
        try:
            return self._preview_job_inner(task, jobs, workdir, gdal, out)
        finally:  # pool threads are reused by other QGIS tasks
            gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
            gdal.SetThreadLocalConfigOption("GDAL_HTTP_USERAGENT", None)

    def _preview_job_inner(self, task, jobs, workdir, gdal, out):
        for scene, preset, keys, provider, variant, aoi, view_ll in jobs:
            if task.isCanceled():
                return out
            try:
                sources, needs_download = provider.prepare_sources(
                    scene, preset, variant, aoi, "pct", 1.0, workdir, task.isCanceled)
                if needs_download or not sources:
                    raise RuntimeError("this source cannot be streamed")
                stem = os.path.join(workdir, uuid.uuid4().hex)
                # No nodata on the VRT: with one, GDAL stops using the file's reduced-size
                # copies and reads the full-resolution image (minutes instead of seconds).
                vrt = gdal.BuildVRT(stem + ".vrt", [src.path for src in sources],
                                    options=gdal.BuildVRTOptions(separate=len(sources) > 1))
                if vrt is None:
                    raise RuntimeError("could not read the image header: {}".format(
                        gdal.GetLastErrorMsg() or "no detail from GDAL"))
                path = self._read_window(gdal, vrt, stem + ".vrt", stem + "_view.vrt", view_ll, provider.nodata)
                vrt = None
                out.append((scene, path, self._display_range(scene, keys), None))
            except Exception as exc:  # noqa: BLE001 - one bad scene must not stop the others
                out.append((scene, None, None, str(exc)))
        return out

    @staticmethod
    def _read_window(gdal, vrt, vrt_path, out_path, view_ll, nodata=None):
        """Copy the part of `vrt` inside `view_ll` (lon/lat) to a local GeoTIFF of at most
        PREVIEW_PIXELS across. GDAL picks the matching overview, so this reads megabytes, not
        the whole scene, and drawing the result later needs no network at all."""
        gt = vrt.GetGeoTransform()
        xsize, ysize = vrt.RasterXSize, vrt.RasterYSize
        x_min, x_max = gt[0], gt[0] + gt[1] * xsize
        y_max, y_min = gt[3], gt[3] + gt[5] * ysize
        want = raster.transform_bbox(view_ll, raster._epsg_srs(4326), raster._srs(vrt.GetProjection()))
        x0, x1 = max(want[0], x_min), min(want[2], x_max)
        y0, y1 = max(want[1], y_min), min(want[3], y_max)
        if x1 <= x0 or y1 <= y0:
            raise RuntimeError("the scene is outside the current map view")
        px = max(1, int(round((x1 - x0) / abs(gt[1]))))
        py = max(1, int(round((y1 - y0) / abs(gt[5]))))
        shrink = min(1.0, PREVIEW_PIXELS / float(max(px, py)))
        width, height = max(1, int(px * shrink)), max(1, int(py * shrink))
        extra = {"noData": nodata} if nodata is not None else {}
        vrt.FlushCache()    # the strip readers open the VRT file itself
        # One download stream is slow over a long-distance link; eight strips at once are
        # several times faster. The strips are left as separate files under one VRT.
        n = max(1, min(PREVIEW_STRIPS, height // 64))
        edges = [round(height * i / n) for i in range(n + 1)]
        strips = []
        for i in range(n):
            r0, r1 = edges[i], edges[i + 1]
            top = y1 - (y1 - y0) * r0 / height
            bottom = y1 - (y1 - y0) * r1 / height
            strips.append((r1 - r0, [x0, top, x1, bottom], "{}_{}.tif".format(out_path[:-4], i)))

        def read_strip(strip):
            rows, win, path = strip
            gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
            try:
                res = gdal.Translate(path, vrt_path, projWin=win, width=width, height=rows,
                                     resampleAlg="nearest", creationOptions=["TILED=YES"], **extra)
            finally:
                gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
            if res is None:
                raise RuntimeError("could not read the image: {}".format(
                    gdal.GetLastErrorMsg() or "no detail from GDAL"))
            return path

        with ThreadPoolExecutor(max_workers=n) as pool:
            files = list(pool.map(read_strip, strips))
        mosaic = gdal.BuildVRT(out_path, files)
        if mosaic is None:
            raise RuntimeError("could not assemble the preview")
        mosaic = None  # flush to disk
        return out_path

    @staticmethod
    def _display_range(scene, keys):
        """Raw value range matching reflectance 0 - 0.3, or None if the scene has no scaling."""
        if not keys:
            return None
        scale, offset = scene.scaling.get(keys[0], (None, None))
        if not scale:
            return None
        offset = offset or 0.0
        return (-offset / scale, (0.3 - offset) / scale)

    def _view_lonlat(self, factor=2.0):
        """Current map view, grown by `factor`, as a lon/lat box plus the same box in map units."""
        extent = QgsRectangle(self.canvas.extent())
        extent.scale(factor)
        crs = self.canvas.mapSettings().destinationCrs()
        to_ll = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                       QgsProject.instance())
        box = to_ll.transformBoundingBox(extent)
        ll = (max(box.xMinimum(), -180.0), max(box.yMinimum(), -85.0),
              min(box.xMaximum(), 180.0), min(box.yMaximum(), 85.0))
        return ll, extent

    def _update_map_preview(self):
        self._preview_gen += 1
        if self._preview_task is not None:
            with contextlib.suppress(RuntimeError):
                self._preview_task.cancel()
            self._preview_task = None
        scenes = self.selected_scenes()
        prov = self.search_provider
        if not scenes or prov is None:
            self._remove_preview_layers()
            return
        if prov.id == "gee":
            self._remove_preview_layers()
            self.status.setText("Earth Engine scenes cannot be previewed on the map – use the "
                                "thumbnail, or download the scene.")
            return
        presets = prov.band_presets(self.search_variant)
        if not presets:
            return
        names = list(presets)
        preset = next((n for n in names if list(presets[n]) == ["visual"]), names[0])
        if not self._any_footprint_in_view(scenes[:PREVIEW_MAX]):
            self._zoom_to_selection()       # nothing to read where the map is looking
        try:
            view_ll, self._preview_cover = self._view_lonlat()
        except Exception as exc:  # noqa: BLE001 - a view that cannot be transformed
            self.status.setText("The map view cannot be used for a preview: {}".format(exc))
            return
        jobs = [(scene, preset, list(presets[preset]), prov, self.search_variant, self.search_aoi,
                 view_ll) for scene in scenes[:PREVIEW_MAX]]
        if self._preview_dir is None:
            self._preview_dir = tempfile.mkdtemp(prefix="hid_preview_")
        gen = self._preview_gen
        task = QgsTask.fromFunction(
            "Satellite imagery archive preview", self._preview_job, jobs, self._preview_dir,
            on_finished=lambda exc, result: self._on_preview_ready(gen, exc, result, len(scenes)))
        self._preview_task = task
        self.status.setText("Loading preview…")
        QgsApplication.taskManager().addTask(task)

    def _raise_preview_layers(self, root):
        """Keep the preview above every other layer, whatever the Layer Order panel says.

        With "Control rendering order" on, the draw order is separate from the tree, and Layer
        Auto-Sort files rasters below the point, line and polygon layers - a preview there is
        hidden behind them. Returns how many layers still draw above the preview.
        """
        previews = [lyr for lyr in QgsProject.instance().mapLayers().values()
                    if lyr.customProperty(PREVIEW_PROP)]
        if not previews:
            return 0
        if root.hasCustomLayerOrder():
            order = [lyr for lyr in root.customLayerOrder() if lyr not in previews]
            root.setCustomLayerOrder(previews + order)
        drawn = root.layerOrder()
        above = [lyr for lyr in drawn[:min(drawn.index(p) for p in previews if p in drawn)]
                 ] if any(p in drawn for p in previews) else []
        for lyr in above:
            log("Layer drawn above the preview: {}".format(lyr.name()), MSG_WARNING)
        return len(above)

    def _any_footprint_in_view(self, scenes):
        view = self.canvas.extent()
        for scene in scenes:
            geom = footprint_to_canvas_geometry(scene.footprint, self.canvas)
            if geom is not None and geom.boundingBox().intersects(view):
                return True
        return not any(scene.footprint for scene in scenes)   # no footprint known: try anyway

    def _on_view_changed_for_preview(self):
        self._view_timer.start()

    def _refresh_preview_for_view(self):
        """Re-read the preview when the view has left the copy, or zoomed in far past its detail."""
        if not self.map_preview_btn.isChecked() or self._preview_task is not None:
            return
        cover = self._preview_cover
        if cover is None or not self._previews_present():
            return
        view = self.canvas.extent()
        if not cover.contains(view) or cover.width() > 6.0 * view.width():
            self._update_map_preview()

    def _previews_present(self):
        return any(lyr.customProperty(PREVIEW_PROP) for lyr in QgsProject.instance().mapLayers().values())

    def _on_preview_ready(self, gen, exception, result, n_selected):
        if gen != self._preview_gen or not self.map_preview_btn.isChecked():
            return                          # superseded by a newer selection, or switched off
        self._preview_task = None
        self._remove_preview_layers()
        if exception is not None or not result:
            if exception is not None:
                log("Map preview failed: {}".format(exception), MSG_WARNING)
            self.status.setText("The preview could not be loaded: {}".format(exception or "no result"))
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(PREVIEW_GROUP) or root.insertGroup(0, PREVIEW_GROUP)
        failed = 0
        for scene, path, rng, error in result:
            title = "Preview – {} {}".format(scene.date, scene.platform or "").strip()
            layer = QgsRasterLayer(path, title) if path else None
            if layer is None or not layer.isValid():
                failed += 1
                log("Map preview failed for {}: {}".format(scene.scene_id, error or "invalid layer"),
                    MSG_WARNING)
                continue
            layer.setCustomProperty(PREVIEW_PROP, True)
            self._apply_color_style(layer, rng)
            project.addMapLayer(layer, False)
            group.insertLayer(0, layer)
        if not group.findLayers():
            root.removeChildNode(group)
        placed = self._raise_preview_layers(root)
        if failed:
            reason = next((err for _s, path, _r, err in result if not path and err), "")
            self.status.setText("{} scene(s) could not be previewed. {}".format(failed, reason[:200]))
        elif n_selected > PREVIEW_MAX:
            self.status.setText("Previewing the first {} of {} selected scenes.".format(
                PREVIEW_MAX, n_selected))
        else:
            self.status.setText("Preview on the map." if not placed else
                                "Preview on the map, but {} layer(s) are drawn above it "
                                "(Layer Order panel).".format(placed))
        self.canvas.refresh()

    def _zoom_to_selection(self):
        scenes = self.selected_scenes() or list(self.scenes.values())
        extent = None
        for scene in scenes:
            geom = footprint_to_canvas_geometry(scene.footprint, self.canvas)
            if geom is None:
                continue
            bb = geom.boundingBox()
            if extent is None:
                extent = bb
            else:
                extent.combineExtentWith(bb)
        if extent is not None:
            extent.scale(1.1)
            self.canvas.setExtent(extent)
            self.canvas.refresh()

    def _add_footprints_layer(self):
        if not self.scenes:
            self._warn("Footprints", "Run a search first.")
            return
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Imagery footprints", "memory")
        fields = QgsFields()
        for name, vtype in (("scene_id", STR), ("date", STR), ("platform", STR), ("tile", STR),
                            ("cloud", DBL), ("aoi_cov", DBL), ("collection", STR)):
            fields.append(QgsField(name, vtype))
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        feats = []
        for scene in self.scenes.values():
            geom = QgsGeometry.fromWkt(geojson_to_wkt(scene.footprint)) if scene.footprint else None
            if geom is None or geom.isNull():
                continue
            f = QgsFeature(layer.fields())
            f.setGeometry(geom)
            f.setAttributes([scene.scene_id, scene.date, scene.platform, scene.tile,
                             scene.cloud_cover, scene.aoi_coverage, scene.collection])
            feats.append(f)
        layer.dataProvider().addFeatures(feats)
        layer.updateExtents()
        layer.renderer().symbol().setOpacity(0.35)
        QgsProject.instance().addMapLayer(layer)
        self.iface.messageBar().pushMessage("Satellite Imagery Archive", "Added {} footprints.".format(len(feats)),
                                            MSG_INFO, 4)

    # =====================================================================
    # Download tab
    # =====================================================================
    def _update_download_enabled(self):
        n = len(self.selected_scenes())
        self.download_btn.setEnabled(n > 0 and self.download_task is None)
        self.download_btn.setText("  Download {} scene{}".format(n, "" if n == 1 else "s") if n else "  Download")

    def _on_tab_changed(self, index):
        if index == TAB_DOWNLOAD:
            self._update_precheck()
        elif index == TAB_SETTINGS:
            self.settings_panel.reload()

    def _schedule_precheck(self, *_):
        self._precheck_timer.start()

    def _download_aoi(self, quick=False):
        try:
            return self.current_aoi(quick=quick)
        except ValueError:
            return self.search_aoi

    def _preset_shape(self, prov, preset):
        keys = prov.band_presets(self.search_variant or "").get(preset, [])
        n_bands = 3 if keys in (["visual"], ["image"]) else max(len(keys), 1)
        bps = 1 if prov.id == "oam" or keys == ["visual"] else 2
        return n_bands, bps

    def _update_precheck(self):
        scenes = self.selected_scenes()
        prov = self.search_provider
        if not scenes or prov is None:
            self.precheck_summary.setText("Select scenes in the Results tab to see the size estimate "
                                          "and checks.")
            self.precheck_msgs.clear()
            return
        aoi = self._download_aoi(quick=True)
        if aoi is None:
            self.precheck_summary.setText("No valid area of interest.")
            self.precheck_msgs.clear()
            return
        kind, value = self.res_combo.currentData()
        n_bands, bps = self._preset_shape(prov, self.preset_combo.currentText())
        summary, msgs = quality.preflight(scenes, aoi, kind, value, n_bands, prov.native_resolution,
                                          settings.get("max_output_megapixels"), bps)
        if self.search_aoi is not None and aoi.bbox != self.search_aoi.bbox:
            msgs.insert(0, (quality.WARN, "The AOI changed since the search – the download uses the "
                                          "current AOI."))
        self.precheck_summary.setText(summary)
        self.precheck_msgs.setText("<br>".join(
            "<span style='color:{}'>{}</span> {}".format(LEVEL_COLOR[lvl], LEVEL_SYMBOL[lvl], msg)
            for lvl, msg in msgs))

    def _download(self):
        scenes = self.selected_scenes()
        if not scenes:
            self._warn("Nothing selected", "Select one or more scenes in the Results tab first.")
            return
        prov = self.search_provider
        out_dir = self.out_dir.filePath().strip()
        if not out_dir:
            self._warn("Output folder", "Choose an output folder.")
            return
        aoi = self._download_aoi()
        if aoi is None:
            self._warn("Area of interest", "No valid area of interest.")
            return
        preset = self.preset_combo.currentText()
        if not preset or preset not in prov.band_presets(self.search_variant or ""):
            self._warn("Bands", "Choose a band combination.")
            return
        kind, value = self.res_combo.currentData()
        n_bands, bps = self._preset_shape(prov, preset)
        _summary, msgs = quality.preflight(scenes, aoi, kind, value, n_bands, prov.native_resolution,
                                           settings.get("max_output_megapixels"), bps)
        if any(lvl == quality.FAIL for lvl, _ in msgs):
            self._warn("Pre-download check failed", "\n".join(m for lvl, m in msgs if lvl == quality.FAIL))
            return
        if len(scenes) > 10:
            ans = QMessageBox.question(self, "Download many scenes?",
                                       "You are about to download {} scenes. Continue?".format(len(scenes)),
                                       MB_YES | MB_NO)
            if ans != MB_YES:
                return
        self._save_state()
        task = DownloadTask(prov, scenes, preset, self.search_variant or "", aoi, kind, value,
                            self.res_combo.currentText(), self.format_combo.currentData(),
                            self.clip_check.isChecked() and aoi.is_polygon, out_dir,
                            self.sidecar_check.isChecked(), settings.get("max_output_megapixels"))
        task.statusChanged.connect(self.status.setText)
        task.downloadFinished.connect(lambda o, e, c, t=task: self._on_download_finished(t, o, e, c))
        self.download_task = task
        self._set_running(True)
        self.status.setText("Starting download of {} scene(s)…".format(len(scenes)))
        self._track(task)

    def _on_download_finished(self, task, outputs, errors, canceled):
        if task is not self.download_task:
            return
        self.download_task = None
        self._set_running(False)
        self.progress.setValue(100 if outputs and not canceled else 0)
        if self.autoload_check.isChecked():
            for out in outputs:
                self._load_raster(out)
        for out in outputs:
            log("Saved {} – quality {}".format(out["path"], out["qa"]["status"]))
        n_warn = sum(1 for o in outputs if o["qa"]["status"] != quality.PASS)
        if canceled:
            text = "Download canceled – {} file(s) completed.".format(len(outputs))
            level = MSG_WARNING
        elif outputs:
            text = "Saved {} raster(s){}{}.".format(
                len(outputs), " – {} with quality warnings".format(n_warn) if n_warn else
                " – all quality checks passed", ", {} failed".format(len(errors)) if errors else "")
            level = MSG_WARNING if (n_warn or errors) else MSG_SUCCESS
        else:
            text = "Download failed."
            level = MSG_WARNING
        self.status.setText(text)
        self.iface.messageBar().pushMessage("Satellite Imagery Archive", text, level, 8)
        if errors or (outputs and self.report_check.isChecked()):
            DownloadReportDialog(outputs, errors, self.out_dir.filePath(), canceled, self).exec()

    def _load_raster(self, out):
        layer = QgsRasterLayer(out["path"], out["name"])
        if not layer.isValid():
            log("Could not load {}".format(out["path"]), MSG_WARNING)
            return
        scene = out["scene"]
        layer.setCustomProperty("historical_imagery/scene_id", scene.scene_id)
        with contextlib.suppress(Exception):
            meta = layer.metadata()
            meta.setTitle("{} – {}".format(scene.platform or scene.collection, scene.date))
            meta.setAbstract("Scene {} ({}). Cloud cover: {}. Bands: {}. Quality: {}.".format(
                scene.scene_id, self.search_provider.name if self.search_provider else "",
                scene.cloud_cover, ", ".join(out["info"]["band_names"]), out["qa"]["status"]))
            layer.setMetadata(meta)
        self._apply_color_style(layer)
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        root = project.layerTreeRoot()
        group = root.findGroup("Satellite Imagery Archive") or root.insertGroup(0, "Satellite Imagery Archive")
        group.insertLayer(0, layer)

    def _apply_color_style(self, layer, value_range=None):
        """Draw bands 1-3 as RGB, stretching anything that is not already 8-bit.

        `value_range` = (min, max) raw values to stretch between; without it the stretch
        comes from the layer's statistics, which means reading the image first.
        """
        provider = layer.dataProvider()
        if layer.bandCount() < 3:
            return
        renderer = QgsMultiBandColorRenderer(provider, 1, 2, 3)
        layer.setRenderer(renderer)
        if provider.dataType(1) == BYTE:  # 8-bit imagery is already display-ready
            return
        algorithm = enum(QgsContrastEnhancement, "ContrastEnhancementAlgorithm",
                         "StretchToMinimumMaximum")
        try:
            if value_range:
                for band, setter in ((1, renderer.setRedContrastEnhancement),
                                     (2, renderer.setGreenContrastEnhancement),
                                     (3, renderer.setBlueContrastEnhancement)):
                    ce = QgsContrastEnhancement(provider.dataType(band))
                    ce.setContrastEnhancementAlgorithm(algorithm)
                    ce.setMinimumValue(value_range[0])
                    ce.setMaximumValue(value_range[1])
                    setter(ce)
            else:
                layer.setContrastEnhancement(algorithm, _range_limit_cumulative())
        except Exception as exc:  # noqa: BLE001
            log("Contrast stretch failed: {}".format(exc), MSG_WARNING)

    def _open_output_dir(self):
        path = self.out_dir.filePath()
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self._warn("Output folder", "The folder does not exist yet – it is created on the first download.")

    # =====================================================================
    # misc
    # =====================================================================
    def _on_settings_saved(self):
        if self.provider_combo.currentData() == "custom_stac":
            self._on_provider_changed()
        self.status.setText("Settings saved.")

    def _open_help(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.join(PLUGIN_DIR, "README.md")))

    def _open_log(self):
        try:
            self.iface.openMessageLog()
        except AttributeError:
            pass

    def _warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def _error(self, title, text):
        QMessageBox.critical(self, title, text)

    def cleanup(self):
        """Called by the plugin on unload."""
        self._save_state()
        self._precheck_timer.stop()
        try:
            self.canvas.extentsChanged.disconnect(self._on_canvas_extent_changed)
        except (TypeError, RuntimeError):
            pass
        self._preview_timer.stop()
        self._view_timer.stop()
        self._unwatch_active_map()
        with contextlib.suppress(TypeError, RuntimeError):
            self.canvas.extentsChanged.disconnect(self._on_view_changed_for_preview)
        self._preview_gen += 1
        if self._preview_task is not None:
            with contextlib.suppress(RuntimeError):
                self._preview_task.cancel()
            self._preview_task = None
        self._remove_preview_layers()
        if self._preview_dir:
            shutil.rmtree(self._preview_dir, ignore_errors=True)
            self._preview_dir = None
        self._cancel_search_tasks()
        if self.download_task is not None:
            try:
                self.download_task.cancel()
            except RuntimeError:
                pass
        for task in list(self._live_tasks):
            for sig in ("progressChanged", "searchFinished", "searchFailed", "searchCanceled",
                        "thumbnailReady", "statusChanged", "downloadFinished"):
                try:
                    getattr(task, sig).disconnect()
                except (AttributeError, TypeError, RuntimeError):
                    pass
        if self.canvas.mapTool() is self.rect_tool:
            self.canvas.unsetMapTool(self.rect_tool)
        for band in (self.aoi_band, self.fp_band, self.rect_tool.rubber):
            self.canvas.scene().removeItem(band)

    def closeEvent(self, event):  # noqa: N802
        self._save_state()
        self.aoi_band.hide()
        self.fp_band.hide()
        if self.draw_btn.isChecked():
            self.draw_btn.setChecked(False)
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802
        self._update_aoi_label()
        super().showEvent(event)

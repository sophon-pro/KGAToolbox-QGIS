# -*- coding: utf-8 -*-
"""Add Open Data & Basemaps dialog.

One window over the whole of `core.data_sources`. The user picks what kind of
data they want, which source, which country and which admin level, checks the
details panel to see exactly what is about to be downloaded, names the layer and
clicks Add.

Three things shape the design:

* **The form rearranges itself.** A basemap needs zoom levels and no country; a
  custom URL needs an address and no level. Rather than five dialogs or one
  crowded one, the rows are shown and hidden per source, so what is on screen is
  only ever what the current choice actually needs.
* **Nothing is downloaded before it is described.** Picking a country costs one
  small metadata request, and its answer fills the details panel - publisher,
  year, licence, feature count, size - so the click that starts a 40 MB download
  is an informed one.
* **The window opens with no network at all.** The country list is static, so a
  slow or absent connection delays nothing until the user asks for something.

Enum members are written scoped (`Qt.ItemDataRole.UserRole`, not `Qt.UserRole`)
because the unscoped spelling is gone in PyQt6, which QGIS 4 moves to.
"""

import os
import re
from contextlib import suppress

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsFillSymbol,
                       QgsMapLayer, QgsMessageLog, QgsPalettedRasterRenderer,
                       QgsProcessingUtils, QgsProject, QgsRasterLayer,
                       QgsRendererCategory, QgsSettings, QgsVectorLayer)
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox, QgsPasswordLineEdit
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..branding import LOG_TAG, open_docs
from ..core import data_sources as DS
from ..core import dem_sources as DEM
from ..core import landcover_sources as LC
from ..core import layer_loader as LL
from ..core import net
from ..core.countries import COUNTRIES, DEFAULT_ISO3, label_for, name_for
from ..core.modify_features import layer_filter
from .busy import BusyOverlay
from .roi_tools import (SHAPE_POLYGON, SHAPE_RECTANGLE, RoiMapTool,
                        drop_band, make_band)

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'add_data'

#: Remembers the "Don't show this again" tick on the scratch-layer notice.
SETTINGS_TEMP_WARNING = 'KGA/add_data/temp_warning'
#: Remembers the last data type, source and country between sessions.
SETTINGS_LAST = 'KGA/add_data/last'

#: How long to wait after the last combo change before asking the source
#: anything. Long enough to swallow a burst of completer matches while someone
#: types a country name, short enough not to feel like lag.
DEBOUNCE_MS = 350

#: How long an operation has to run before the busy panel appears. A cached
#: answer comes back in under a millisecond, and flashing the panel for that
#: would be worse than showing nothing.
BUSY_DELAY_MS = 150

DATA_TYPES = (
    (DS.KIND_ADMIN, 'Administrative boundaries'),
    (DS.KIND_VECTOR, 'Other vector data'),
    (DS.KIND_BASEMAP, 'Basemap tile layer'),
    (DS.KIND_DEM, 'Open digital elevation (DEM)'),
    (DS.KIND_LANDCOVER, 'Open land cover (LULC)'),
)

#: The data types asked for by area rather than by country. They share the
#: area rows, the reprojection tick and the output file.
AREA_KINDS = (DS.KIND_DEM, DS.KIND_LANDCOVER)

#: How a DEM's or land cover's area is chosen, in the order the ROI combo
#: lists them.
ROI_MODES = (
    (DEM.ROI_CANVAS, 'Current map extent'),
    (DEM.ROI_DRAW, 'Draw a rectangle or polygon on the map'),
    (DEM.ROI_LAYER_CLIP, 'Clip to a polygon layer\'s boundary'),
    (DEM.ROI_LAYER_EXTENT, 'Extent of a layer'),
)

#: Remembers the ROI mode and the reprojection tick between sessions.
SETTINGS_DEM = 'KGA/add_data/dem'
#: Remembers the land-cover polygon tick.
SETTINGS_LANDCOVER = 'KGA/add_data/landcover'

LEVEL_LABELS = {
    0: 'ADM0 - country',
    1: 'ADM1 - province / state',
    2: 'ADM2 - district',
    3: 'ADM3 - commune / sub-district',
    4: 'ADM4 - village',
    5: 'ADM5',
}


def tr(text):
    return text


class AddDataDialog(QDialog):
    """The whole tool."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('KGA Add Open Data & Basemaps'))
        self.setObjectName('KgaAddDataDialog')
        self.resize(660, 560)

        #: The SourceItem currently described, or None. What Add acts on.
        self._item = None
        #: True once the user edits the name box, after which we stop
        #: overwriting what they typed.
        self._name_touched = False
        #: Guards the signal handlers while a combo is being repopulated.
        self._loading = False
        self._option_widgets = {}

        #: The canvas the DEM area is drawn on and read from. None head-less.
        self.canvas = iface.mapCanvas() if iface is not None else None
        #: A drawn DEM area, as (geometry, crs) in the canvas CRS it was drawn
        #: in, and the band that keeps it visible on the map.
        self._drawn = None
        self._roi_band = None
        self._roi_tool = None
        #: The canvas tool the draw buttons replaced, given back afterwards.
        self._previous_tool = None
        #: The OpenTopography key has been looked up in the auth database.
        self._api_key_loaded = False
        #: Set once the window has let go of the canvas and scheduled its own
        #: deletion, so closing twice does not do it twice.
        self._torn_down = False
        #: False until the first describe has run, after the window is shown.
        self._loaded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # The form and the details share a container so the busy panel can
        # cover both without covering the buttons: work that cannot be
        # interrupted should not look like a window that can be closed.
        self.body = QWidget(self)
        body_column = QVBoxLayout(self.body)
        body_column.setContentsMargins(0, 0, 0, 0)
        body_column.setSpacing(8)
        body_column.addWidget(self._build_form())
        body_column.addWidget(self._build_details())
        layout.addWidget(self.body)

        self.status = QLabel('', self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color: palette(mid);')
        layout.addWidget(self.status)

        layout.addWidget(self._build_buttons())

        self.busy = BusyOverlay(self.body)

        # Asking a source what it publishes is a network round trip, and the
        # country box is editable: typing "Cam" would otherwise fire one
        # request per keystroke the completer matches. Collapse a burst of
        # changes into the one request the user actually meant.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._reload_now)

        # Build the form from what was remembered, but ask no source anything
        # yet: that is network, or dissolving a boundary layer, on the GUI
        # thread, and doing it here held the window off screen - QGIS looked
        # hung - until it finished. The first describe waits for the window.
        self._restore_last()
        self._fill_sources()
        self._apply_visibility()
        self.add_button.setEnabled(False)
        self._set_details(tr('Loading...'))
        self._first_load = QTimer(self)
        self._first_load.setSingleShot(True)
        self._first_load.setInterval(50)
        self._first_load.timeout.connect(self._on_source_changed)

        if self.canvas is not None:
            # A "current map extent" area follows the map as it is panned.
            self.canvas.extentsChanged.connect(self._on_canvas_extent)
            self.canvas.mapToolSet.connect(self._on_map_tool_set)

    # ------------------------------------------------------------------- form

    def _build_form(self):
        box = QGroupBox(tr('What to add'), self)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.type_combo = QComboBox(box)
        for kind, label in DATA_TYPES:
            self.type_combo.addItem(label, kind)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(tr('Data type'), self.type_combo)

        self.source_combo = QComboBox(box)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        form.addRow(tr('Data source'), self.source_combo)
        self.source_row = self.source_combo

        self.country_combo = QComboBox(box)
        self.country_combo.setEditable(True)
        self.country_combo.setInsertPolicy(
            QComboBox.InsertPolicy.NoInsert)
        for iso3, _name in COUNTRIES:
            self.country_combo.addItem(label_for(iso3), iso3)
        completer = self.country_combo.completer()
        if completer is not None:
            completer.setCompletionMode(
                completer.CompletionMode.PopupCompletion)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.country_combo.currentIndexChanged.connect(self._on_country_changed)
        form.addRow(tr('Country'), self.country_combo)

        self.level_combo = QComboBox(box)
        self.level_combo.currentIndexChanged.connect(self._refresh_item)
        form.addRow(tr('Admin level'), self.level_combo)

        # One row per source-specific option, built once and reused: the
        # widgets are created empty here and filled in _on_source_changed.
        # Three is one more than any source currently declares - a source whose
        # options outnumber the rows would have the extras silently dropped.
        self.option_rows = []
        for index in range(3):
            combo = QComboBox(box)
            combo.currentIndexChanged.connect(self._on_option_changed)
            label = QLabel('', box)
            form.addRow(label, combo)
            # Hidden until a source claims the row, so a window shown before
            # the first describe has no empty option rows in it.
            label.setVisible(False)
            combo.setVisible(False)
            self.option_rows.append((label, combo))

        self.url_edit = QLineEdit(box)
        self.url_edit.setPlaceholderText(
            tr('https://example.org/data.geojson, or an ArcGIS '
               'FeatureServer/0 or WFS address'))
        self.url_edit.editingFinished.connect(self._refresh_item)
        form.addRow(tr('Address'), self.url_edit)

        self.zoom_widget = QWidget(box)
        zoom_row = QHBoxLayout(self.zoom_widget)
        zoom_row.setContentsMargins(0, 0, 0, 0)
        self.zmin_spin = QSpinBox(self.zoom_widget)
        self.zmin_spin.setRange(0, 24)
        self.zmax_spin = QSpinBox(self.zoom_widget)
        self.zmax_spin.setRange(0, 24)
        zoom_row.addWidget(QLabel(tr('min'), self.zoom_widget))
        zoom_row.addWidget(self.zmin_spin)
        zoom_row.addWidget(QLabel(tr('max'), self.zoom_widget))
        zoom_row.addWidget(self.zmax_spin)
        zoom_row.addStretch(1)
        form.addRow(tr('Zoom levels'), self.zoom_widget)

        self.remember_check = QCheckBox(
            tr('Also add it to the Browser panel, permanently'), box)
        self.remember_check.setToolTip(tr(
            'Adds the tile service to XYZ Tiles in the Browser panel, so it is '
            'available in every project without coming back here.'))
        form.addRow('', self.remember_check)

        self._build_dem_rows(box, form)

        self.name_edit = QLineEdit(box)
        self.name_edit.textEdited.connect(self._on_name_edited)
        form.addRow(tr('Layer name'), self.name_edit)

        self.form = form
        return box

    def _build_dem_rows(self, box, form):
        """The rows only a DEM needs: the API key, the area, the output."""
        self.api_key_widget = QWidget(box)
        key_row = QHBoxLayout(self.api_key_widget)
        key_row.setContentsMargins(0, 0, 0, 0)
        self.api_key_edit = QgsPasswordLineEdit(self.api_key_widget)
        self.api_key_edit.setPlaceholderText(tr('Your OpenTopography API key'))
        self.api_key_edit.editingFinished.connect(self._refresh_item)
        key_row.addWidget(self.api_key_edit, 1)
        self.api_key_remember = QCheckBox(tr('Remember'), self.api_key_widget)
        self.api_key_remember.setToolTip(tr(
            'Keep the key, encrypted, in the QGIS password store. QGIS may '
            'ask for its master password.'))
        key_row.addWidget(self.api_key_remember)
        signup = QLabel('<a href="{0}">{1}</a>'.format(
            DEM.OPENTOPO_SIGNUP, tr('Get a free key')), self.api_key_widget)
        signup.setOpenExternalLinks(True)
        key_row.addWidget(signup)
        form.addRow(tr('API key'), self.api_key_widget)

        self.roi_combo = QComboBox(box)
        for mode, label in ROI_MODES:
            self.roi_combo.addItem(label, mode)
        self.roi_combo.setToolTip(tr(
            'The area to download. A map extent or a layer extent gives its '
            'bounding box; a drawn shape or a layer boundary cuts the raster '
            'to the shape, with NoData outside it.'))
        self.roi_combo.currentIndexChanged.connect(self._on_roi_changed)
        form.addRow(tr('Area'), self.roi_combo)

        self.draw_widget = QWidget(box)
        draw_row = QHBoxLayout(self.draw_widget)
        draw_row.setContentsMargins(0, 0, 0, 0)
        self.draw_rect_button = QPushButton(tr('Draw rectangle'),
                                            self.draw_widget)
        self.draw_rect_button.setCheckable(True)
        self.draw_rect_button.setToolTip(tr(
            'Press and drag on the map to draw a rectangle.'))
        self.draw_rect_button.clicked.connect(
            lambda checked: self._arm_draw(SHAPE_RECTANGLE, checked))
        draw_row.addWidget(self.draw_rect_button)
        self.draw_poly_button = QPushButton(tr('Draw polygon'),
                                            self.draw_widget)
        self.draw_poly_button.setCheckable(True)
        self.draw_poly_button.setToolTip(tr(
            'Click each corner on the map, then double-click, right-click or '
            'press Enter to close the polygon. Backspace removes the last '
            'corner, Escape starts again.'))
        self.draw_poly_button.clicked.connect(
            lambda checked: self._arm_draw(SHAPE_POLYGON, checked))
        draw_row.addWidget(self.draw_poly_button)
        self.draw_clear_button = QPushButton(tr('Clear'), self.draw_widget)
        self.draw_clear_button.clicked.connect(self._clear_drawn)
        draw_row.addWidget(self.draw_clear_button)
        draw_row.addStretch(1)
        form.addRow('', self.draw_widget)

        self.roi_layer_combo = QgsMapLayerComboBox(box)
        self.roi_layer_combo.setAllowEmptyLayer(True)
        self.roi_layer_combo.layerChanged.connect(self._on_roi_input_changed)
        form.addRow(tr('Layer'), self.roi_layer_combo)

        self.roi_selected_check = QCheckBox(tr('Selected features only'), box)
        self.roi_selected_check.toggled.connect(self._on_roi_input_changed)
        form.addRow('', self.roi_selected_check)

        self.reproject_check = QCheckBox(
            tr('Reproject to the project CRS'), box)
        self.reproject_check.setToolTip(tr(
            'Open DEMs are published in latitude and longitude (EPSG:4326). '
            'Tick this to warp the result into the project CRS with bilinear '
            'resampling - handy for tools that want metres. Left unticked, the '
            'published grid is kept exactly.'))
        self.reproject_check.toggled.connect(self._refresh_item)
        form.addRow('', self.reproject_check)

        self.polygon_check = QCheckBox(
            tr('Also add as polygons (lc_code, lc_class)'), box)
        self.polygon_check.setToolTip(tr(
            'Besides the raster, turn the classes into polygons in a '
            'GeoPackage next to it, each carrying its land-cover code and '
            'class name. Slower, and limited to {0:,} million pixels.')
            .format(LC.MAX_POLYGON_PIXELS // 1000000))
        form.addRow('', self.polygon_check)

        self.output_widget = QgsFileWidget(box)
        self.output_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.output_widget.setFilter(tr('GeoTIFF (*.tif *.tiff)'))
        self.output_widget.setConfirmOverwrite(True)
        line = self.output_widget.lineEdit()
        if line is not None:
            line.setPlaceholderText(tr(
                '[Save to a temporary file]'))
        self.output_widget.setToolTip(tr(
            'Where to write the GeoTIFF. Leave it empty for a temporary file, '
            'which QGIS deletes when it closes.'))
        form.addRow(tr('Save to'), self.output_widget)

    def _build_details(self):
        box = QGroupBox(tr('Details'), self)
        column = QVBoxLayout(box)
        self.details = QLabel('', box)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setOpenExternalLinks(True)
        self.details.setMinimumHeight(110)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop
                                  | Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self.details)
        return box

    def _build_buttons(self):
        widget = QWidget(self)
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        self.add_button = QPushButton(tr('Add to Map'), widget)
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self.add_to_map)
        row.addWidget(self.add_button)

        self.refresh_button = QPushButton(tr('Refresh'), widget)
        self.refresh_button.setToolTip(tr(
            'Re-read the source, ignoring anything already downloaded.'))
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)

        self.clear_button = QPushButton(tr('Clear cache'), widget)
        self.clear_button.clicked.connect(self.clear_cache)
        row.addWidget(self.clear_button)

        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
            | QDialogButtonBox.StandardButton.Help, widget)
        buttons.rejected.connect(self.close)
        buttons.helpRequested.connect(lambda: open_docs(ALG_NAME))
        row.addWidget(buttons)
        return widget

    # --------------------------------------------------------- row visibility

    def _set_row_visible(self, widget, visible):
        """Show or hide a form row, label included."""
        label = self.form.labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    def _current_kind(self):
        return self.type_combo.currentData()

    def _current_source(self):
        if self._current_kind() == DS.KIND_BASEMAP:
            return None
        return DS.source_by_id(self.source_combo.currentData())

    def _apply_visibility(self):
        kind = self._current_kind()
        source = self._current_source()
        basemap = kind == DS.KIND_BASEMAP
        custom_url = source is not None and source.id == DS.CustomUrlSource.id
        # The address row serves two different "paste your own" cases: a custom
        # data source, and the custom XYZ entry at the end of the basemap list.
        custom_tiles = (basemap
                        and self.source_combo.currentData() == DS.CUSTOM_BASEMAP)

        self._set_row_visible(self.country_combo, bool(
            source is not None and not custom_url
            and source.wants_country(self._options())))
        self._set_row_visible(self.level_combo, bool(
            source is not None and source.kind == DS.KIND_ADMIN
            and not custom_url))
        self._set_row_visible(self.url_edit, custom_url or custom_tiles)
        self._set_row_visible(self.zoom_widget, basemap)
        self._set_row_visible(self.remember_check, basemap)
        self.remember_check.setVisible(basemap)

        # A DEM and land cover are both asked for by area; `area` covers both.
        area = kind in AREA_KINDS
        mode = self.roi_combo.currentData()
        layer_mode = mode in (DEM.ROI_LAYER_CLIP, DEM.ROI_LAYER_EXTENT)
        self._set_row_visible(self.api_key_widget, bool(
            area and getattr(source, 'needs_api_key', False)))
        self._set_row_visible(self.roi_combo, area)
        self._set_row_visible(self.draw_widget, area and mode == DEM.ROI_DRAW)
        self._set_row_visible(self.roi_layer_combo, area and layer_mode)
        self._set_row_visible(self.roi_selected_check,
                              area and mode == DEM.ROI_LAYER_CLIP)
        self._set_row_visible(self.reproject_check, area)
        self._set_row_visible(self.polygon_check,
                              kind == DS.KIND_LANDCOVER)
        self._set_row_visible(self.output_widget, area)
        self._set_row_visible(self.name_edit, True)
        drawing_area = area and mode == DEM.ROI_DRAW
        self._show_drawn_band(drawing_area)
        if not drawing_area:
            self._disarm_draw()

    # ------------------------------------------------------------- populating

    def _on_type_changed(self):
        if self._loading:
            return
        self._fill_sources()
        self._on_source_changed()

    def _fill_sources(self):
        """List the current data type's sources. Asks none of them anything."""
        self._loading = True
        kind = self._current_kind()
        self.source_combo.clear()

        if kind == DS.KIND_BASEMAP:
            for basemap in DS.BASEMAPS:
                self.source_combo.addItem(basemap.label, basemap.id)
            self.source_combo.addItem(tr('Custom XYZ URL...'),
                                      DS.CUSTOM_BASEMAP)
        else:
            model = self.source_combo.model()
            for source in DS.sources_for(kind):
                self.source_combo.addItem(source.label, source.id)
                if not source.enabled:
                    index = self.source_combo.count() - 1
                    item = model.item(index)
                    if item is not None:
                        item.setEnabled(False)
                    self.source_combo.setItemData(
                        index, source.disabled_note,
                        Qt.ItemDataRole.ToolTipRole)

        self._loading = False

    def _on_source_changed(self):
        if self._loading:
            return
        self._first_load.stop()
        self._loaded = True
        self._name_touched = False
        # Picking a source is a deliberate click rather than a burst of
        # keystrokes, so this one does not wait for the debounce. It is still
        # one busy panel across both halves: begin/end nest.
        self._debounce.stop()
        self.busy.begin(self._busy_text(), BUSY_DELAY_MS)
        try:
            self._loading = True
            try:
                if self._current_kind() == DS.KIND_BASEMAP:
                    self._setup_basemap_source()
                else:
                    self._setup_data_source()
            finally:
                self._loading = False
            self._apply_visibility()
            self._refresh_item()
        finally:
            self.busy.end()

    def _setup_basemap_source(self):
        for label, combo in self.option_rows:
            label.setVisible(False)
            combo.setVisible(False)
        basemap = DS.basemap_by_id(self.source_combo.currentData())
        custom = basemap is None
        # The row itself is shown or hidden by _apply_visibility, which runs
        # after this; here we only set what goes in it.
        if custom:
            self.url_edit.setPlaceholderText(
                tr('https://tiles.example.org/{z}/{x}/{y}.png'))
            self.zmin_spin.setValue(0)
            self.zmax_spin.setValue(19)
        else:
            self.url_edit.setPlaceholderText('')
            self.zmin_spin.setValue(basemap.zmin)
            self.zmax_spin.setValue(basemap.zmax)

    def _setup_data_source(self):
        source = self._current_source()
        self._option_widgets = {}

        options = source.options() if source is not None else []
        for index, (label, combo) in enumerate(self.option_rows):
            if index < len(options):
                option = options[index]
                combo.clear()
                for value, text in option.choices:
                    combo.addItem(text, value)
                combo.setCurrentIndex(max(0, combo.findData(option.default)))
                combo.setToolTip(option.tooltip)
                label.setText(option.label)
                label.setToolTip(option.tooltip)
                label.setVisible(True)
                combo.setVisible(True)
                self._option_widgets[option.key] = combo
            else:
                label.setVisible(False)
                combo.setVisible(False)

        if source is not None and source.id == DS.CustomUrlSource.id:
            self.url_edit.setPlaceholderText(
                tr('https://example.org/data.geojson, or an ArcGIS '
                   'FeatureServer/0 or WFS address'))

        if getattr(source, 'needs_api_key', False):
            self._load_api_key()
        if source is not None and source.kind in AREA_KINDS:
            self._apply_roi_filter()
            self._update_reproject_label()

        self._reload_levels()

    def _on_option_changed(self):
        if self._loading:
            return
        self._name_touched = False
        # An option can change which rows make sense straight away - asking
        # Natural Earth for the whole world retires the country picker - so
        # that happens now, and only the asking is deferred.
        self._apply_visibility()
        self._schedule_reload()

    def _on_country_changed(self):
        if self._loading:
            return
        self._name_touched = False
        self._schedule_reload()

    def _schedule_reload(self):
        """Ask the source in a moment, unless another change arrives first."""
        self._debounce.start()

    def _reload_now(self):
        """What the debounce timer fires: one pass over levels and details.

        Both halves can go to the network, so they run under one busy panel -
        the overlay nests, so it stays up across the pair instead of
        flickering between them.
        """
        self._debounce.stop()
        self.busy.begin(self._busy_text(), BUSY_DELAY_MS)
        try:
            self._loading = True
            try:
                self._reload_levels()
            finally:
                self._loading = False
            self._apply_visibility()
            self._refresh_item()
        finally:
            self.busy.end()

    def _busy_text(self):
        source = self._current_source()
        label = source.label.split(' (')[0] if source is not None else tr(
            'the data source')
        if source is not None and source.wants_country(self._options()):
            return tr('Asking {0} about {1}...').format(
                label, name_for(self._iso3()))
        return tr('Asking {0}...').format(label)

    def _options(self, with_roi=False):
        """The option values a source reads.

        `with_roi` builds the DEM area as well. Only `describe` needs it, and
        dissolving a large boundary layer is not free, so the callers that only
        ask "is the country row wanted?" leave it out.
        """
        values = {key: combo.currentData()
                  for key, combo in self._option_widgets.items()}
        # Always passed, never gated on the row being visible: isVisible() is
        # False for every widget of a window that has not been shown yet, which
        # would leave the address behind on the first describe.
        values['url'] = self.url_edit.text().strip()
        if with_roi and self._current_kind() in AREA_KINDS:
            values['api_key'] = self.api_key_edit.text().strip()
            try:
                values['roi'] = self._build_roi()
            except DEM.DemError as exc:
                values['roi'] = None
                values['roi_error'] = str(exc)
        return values

    def _iso3(self):
        data = self.country_combo.currentData()
        if data:
            return data
        # The combo is editable: fall back to whatever was typed.
        text = self.country_combo.currentText().strip().upper()
        return text[-4:-1] if text.endswith(')') else text[:3]

    def _reload_levels(self):
        """Ask the current source which levels it has, and rebuild the combo."""
        source = self._current_source()
        self.level_combo.clear()
        if source is None or source.kind != DS.KIND_ADMIN or not source.enabled:
            return

        wanted = self.level_combo.property('lastLevel')
        self.busy.begin(tr('Asking {0} which levels it publishes...').format(
            source.label.split(' (')[0]), BUSY_DELAY_MS)
        try:
            available = source.levels(self._iso3(), self._options())
        except net.NetError as exc:
            self._say(str(exc), warn=True)
            return
        except Exception as exc:            # pragma: no cover - defensive
            self._log(exc)
            self._say(tr('Could not read the source: {0}').format(exc),
                      warn=True)
            return
        finally:
            self.busy.end()

        if not available:
            self._say(tr('{0} does not publish boundaries for {1}.').format(
                source.label, label_for(self._iso3())), warn=True)
            return

        model = self.level_combo.model()
        for level in range(0, 6):
            self.level_combo.addItem(
                LEVEL_LABELS.get(level, 'ADM{0}'.format(level)), level)
            if level not in available:
                # Shown but disabled, so the gap is visible: "this country has
                # no ADM4" is information, and hiding the row would leave the
                # user wondering whether the tool supports it at all.
                index = self.level_combo.count() - 1
                item = model.item(index)
                if item is not None:
                    item.setEnabled(False)
                self.level_combo.setItemData(
                    index, tr('Not published for this country'),
                    Qt.ItemDataRole.ToolTipRole)

        target = wanted if wanted in available else available[
            min(1, len(available) - 1)]
        self.level_combo.setCurrentIndex(self.level_combo.findData(target))
        self._say('')

    # ----------------------------------------------------------- the details

    def _refresh_item(self):
        """Describe what is currently selected, and fill the details panel."""
        if self._loading:
            return
        self._item = None
        kind = self._current_kind()

        if kind == DS.KIND_BASEMAP:
            self._describe_basemap()
            return

        source = self._current_source()
        if source is None:
            self._set_details(tr('Choose a data source.'))
            return
        if not source.enabled:
            self._set_details(source.disabled_note)
            self.add_button.setEnabled(False)
            return

        if source.id == DS.CustomUrlSource.id \
                and not self.url_edit.text().strip():
            self._set_details(tr(
                'Paste the address of a GeoJSON file, an ArcGIS REST layer '
                '(one ending in <b>/FeatureServer/0</b>) or a WFS endpoint.'))
            self.add_button.setEnabled(False)
            return

        level = self.level_combo.currentData()
        if source.kind == DS.KIND_ADMIN and source.id != DS.CustomUrlSource.id:
            if level is None:
                self._set_details(tr('Choose an admin level.'))
                self.add_button.setEnabled(False)
                return
            self.level_combo.setProperty('lastLevel', level)

        self.busy.begin(tr('Reading the details...'), BUSY_DELAY_MS)
        try:
            item = source.describe(self._iso3(), level,
                                   self._options(with_roi=True))
        except net.NetError as exc:
            self._set_details(str(exc))
            self.add_button.setEnabled(False)
            return
        except Exception as exc:            # pragma: no cover - defensive
            self._log(exc)
            self._set_details(tr('Could not read the source: {0}').format(exc))
            self.add_button.setEnabled(False)
            return
        finally:
            self.busy.end()

        if item is None:
            self._set_details(tr('Nothing is published for that combination.'))
            self.add_button.setEnabled(False)
            return

        self._item = item
        self.add_button.setEnabled(True)
        if not self._name_touched:
            self.name_edit.setText(self._default_name(item))
        self._set_details(self._details_html(source, item))

    def _describe_basemap(self):
        basemap = DS.basemap_by_id(self.source_combo.currentData())
        if basemap is None:
            url = self.url_edit.text().strip()
            self.add_button.setEnabled(bool(url))
            if not self._name_touched:
                self.name_edit.setText(tr('Custom basemap'))
            self._set_details(tr(
                'Paste a tile address containing the <b>{z}/{x}/{y}</b> '
                'placeholders. Check the terms of whatever service it points '
                'at - not every tile server allows direct use.'))
            return

        self.add_button.setEnabled(True)
        if not self._name_touched:
            self.name_edit.setText(basemap.label)
        self._set_details(
            '<b>{0}</b><br/>Tile service, added as an XYZ raster layer.'
            '<br/><br/><i>{1}</i>'.format(
                _escape(basemap.label), _escape(basemap.attribution)))

    def _default_name(self, item):
        return item.title

    def _details_html(self, source, item):
        rows = []
        if item.publisher:
            rows.append((tr('Published by'), item.publisher))
        if item.year:
            rows.append((tr('Represents'), item.year))
        if item.unit_count:
            rows.append((tr('Features'), '{0:,}'.format(item.unit_count)))
        rows.extend(getattr(item, 'extra_rows', None) or [])
        if item.licence:
            rows.append((tr('Licence'), item.licence))

        cached = source.cached_file(item) if item.access == DS.ACCESS_DOWNLOAD \
            else None
        if cached:
            rows.append((tr('Download'),
                         tr('already cached - nothing to download')))
        elif item.size_bytes:
            rows.append((tr('Download'), net.human_size(item.size_bytes)))
        elif item.access == DS.ACCESS_URI:
            rows.append((tr('Access'), tr('read live from the service')))

        html = ['<b>{0}</b>'.format(_escape(item.title))]
        html.append('<table cellspacing="0" cellpadding="2">')
        for label, value in rows:
            html.append(
                '<tr><td><i>{0}</i></td><td>&nbsp;{1}</td></tr>'.format(
                    _escape(label), _escape(str(value))))
        html.append('</table>')
        if item.notes:
            html.append('<div>{0}</div>'.format(_escape(item.notes)))
        return ''.join(html)

    def _set_details(self, html):
        self.details.setText(html)

    # ------------------------------------------------------------------- add

    def add_to_map(self):
        if self._current_kind() == DS.KIND_BASEMAP:
            self._add_basemap()
            return
        if self._current_kind() == DS.KIND_DEM:
            self._add_dem()
            return
        if self._current_kind() == DS.KIND_LANDCOVER:
            self._add_landcover()
            return
        self._add_vector()

    def _add_basemap(self):
        basemap = DS.basemap_by_id(self.source_combo.currentData())
        if basemap is not None:
            url = basemap.url
            attribution = basemap.attribution
        else:
            url = self.url_edit.text().strip()
            zmin, zmax = self.zmin_spin.value(), self.zmax_spin.value()
            attribution = 'Custom tile service'
            if '{z}' not in url or '{x}' not in url or '{y}' not in url:
                self._warn(tr(
                    'That address has no {z}/{x}/{y} placeholders, so QGIS '
                    'cannot work out which tile to ask for.'))
                return
        # The spin boxes start at the service's own range but the user may have
        # narrowed it, so they win.
        zmin, zmax = self.zmin_spin.value(), self.zmax_spin.value()

        project = QgsProject.instance()
        name = self._chosen_name(basemap.label if basemap else 'Custom basemap')
        try:
            layer = LL.build_basemap(url, LL.unique_name(project, name),
                                     zmin, zmax)
        except LL.LoadError as exc:
            self._warn(str(exc))
            return

        project.addMapLayer(layer)
        # A basemap belongs underneath everything else, which is where it is
        # useful and where nobody has to drag it.
        _send_to_bottom(project, layer)

        if self.remember_check.isChecked():
            LL.remember_xyz(name, url, zmin, zmax)

        self._say(tr('Added "{0}".').format(layer.name()))
        self._push_message(tr('Basemap added: {0}. {1}').format(
            layer.name(), attribution), warn=False)

    def _add_vector(self):
        source = self._current_source()
        if source is None or self._item is None:
            return
        item = self._item

        if item.access == DS.ACCESS_URI:
            self._add_live_layer(item)
            return

        path = self._fetch_with_progress(source, item)
        if path is None:
            return

        project = QgsProject.instance()
        name = LL.unique_name(project, self._chosen_name(item.title))
        try:
            layer = LL.to_temp_layer(path, name, item.sublayer,
                                     item.filter_fields, item.filter_value)
        except LL.LoadError as exc:
            self._warn(str(exc))
            return
        except Exception as exc:            # pragma: no cover - defensive
            self._log(exc)
            self._warn(tr('The download could not be opened: {0}').format(exc))
            return

        LL.tag_provenance(layer, item)
        project.addMapLayer(layer)

        self._say(tr('Added "{0}" - {1:,} features.').format(
            layer.name(), layer.featureCount()))
        self._temp_layer_notice(layer, item)

    def _add_live_layer(self, item):
        project = QgsProject.instance()
        name = LL.unique_name(project, self._chosen_name(item.title))
        try:
            layer = LL.open_uri_layer(item.uri, name, item.provider)
        except LL.LoadError as exc:
            self._warn(str(exc))
            return
        LL.tag_provenance(layer, item)
        project.addMapLayer(layer)
        self._say(tr('Added "{0}", read live from the service.').format(name))

    def _fetch_with_progress(self, source, item):
        """Download `item`, showing a cancelable progress dialog.

        Returns the local path, or None when the user cancelled or it failed.
        """
        cached = source.cached_file(item)
        if cached:
            return cached

        progress = QProgressDialog(
            tr('Downloading {0}...').format(item.title), tr('Cancel'), 0, 100,
            self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setValue(0)

        def on_progress(received, total):
            if progress.wasCanceled():
                return False
            if total > 0:
                progress.setMaximum(100)
                progress.setValue(int(received * 100 / total))
                progress.setLabelText(tr('Downloading {0}... {1} of {2}').format(
                    item.title, net.human_size(received),
                    net.human_size(total)))
            else:
                # An unknown length, or the feature count of a paged API.
                progress.setMaximum(0)
                progress.setLabelText(tr('Downloading {0}... {1}').format(
                    item.title, net.human_size(received) if received > 4096
                    else tr('{0} features').format(received)))
            QApplication.processEvents()
            return True

        try:
            path = source.fetch(item, on_progress)
        except net.NetError as exc:
            progress.close()
            self._warn(str(exc))
            return None
        except Exception as exc:            # pragma: no cover - defensive
            progress.close()
            self._log(exc)
            self._warn(tr('The download failed: {0}').format(exc))
            return None
        progress.close()

        if path is None:
            self._say(tr('Download cancelled.'))
        return path

    def _chosen_name(self, fallback):
        return self.name_edit.text().strip() or fallback

    def _on_name_edited(self, _text):
        self._name_touched = True

    # --------------------------------------------------------------- the DEM

    def _add_dem(self):
        source = self._current_source()
        if source is None or source.kind != DS.KIND_DEM:
            return
        # Described afresh rather than taken from the panel: a map-extent area
        # follows the map, and the map may have moved since.
        try:
            item = source.describe(None, None, self._options(with_roi=True))
        except net.NetError as exc:
            self._warn(str(exc))
            return

        out_path, temporary = self._dem_output_path(item)
        if out_path is None:
            return

        progress = QProgressDialog(
            tr('Reading {0}...').format(item.title), tr('Cancel'), 0, 0, self)
        progress.setWindowTitle(tr('KGA Add Open Data'))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setValue(0)
        QApplication.processEvents()

        def on_download(received, total):
            if progress.wasCanceled():
                return False
            if total > 0:
                progress.setMaximum(100)
                progress.setValue(int(received * 100 / total))
            else:
                progress.setMaximum(0)
            progress.setLabelText(tr('Downloading {0}... {1}').format(
                item.title, net.human_size(received)))
            QApplication.processEvents()
            return True

        def on_warp(fraction):
            if progress.wasCanceled():
                return False
            progress.setMaximum(100)
            progress.setValue(int(fraction * 100))
            QApplication.processEvents()
            return True

        try:
            rasters = source.rasters(item, on_download)
            if rasters is None:
                self._say(tr('Download cancelled.'))
                return
            progress.setLabelText(tr('Cutting out the area and writing {0}...')
                                  .format(os.path.basename(out_path)))
            written = DEM.warp_to_file(rasters, item.roi, out_path,
                                       self._reproject_crs(), on_warp)
        except net.NetError as exc:
            self._warn(str(exc))
            return
        except Exception as exc:            # pragma: no cover - defensive
            self._log(exc)
            self._warn(tr('The elevation raster could not be built: {0}')
                       .format(exc))
            return
        finally:
            progress.close()
            # Only now: trimming mid-request could evict a file still in use.
            net.trim_cache()

        if not written:
            self._say(tr('Cancelled.'))
            return

        project = QgsProject.instance()
        name = LL.unique_name(project, self._chosen_name(item.title))
        layer = QgsRasterLayer(out_path, name, 'gdal')
        if not layer.isValid():
            self._warn(tr('The raster was written to {0} but QGIS could not '
                          'open it.').format(out_path))
            return
        LL.tag_provenance(layer, item)
        project.addMapLayer(layer)
        self._keep_api_key()

        self._say(tr('Added "{0}" - {1} x {2} pixels.').format(
            layer.name(), layer.width(), layer.height()))
        if temporary:
            self._push_save_message(layer, tr(
                '"{0}" was written to a temporary file, which QGIS deletes '
                'when it closes - save it to keep it.').format(layer.name()))
        else:
            self._push_message(tr('DEM saved to {0}. {1}').format(
                out_path, item.attribution), warn=False)

    # ---------------------------------------------------------- land cover

    def _add_landcover(self):
        source = self._current_source()
        if source is None or source.kind != DS.KIND_LANDCOVER:
            return
        # Described afresh, as for a DEM: the map may have moved.
        try:
            item = source.describe(None, None, self._options(with_roi=True))
        except net.NetError as exc:
            self._warn(str(exc))
            return

        out_path, temporary = self._dem_output_path(item)
        if out_path is None:
            return
        polygons = self.polygon_check.isChecked()
        gpkg_path = self._sibling_path(out_path, '.gpkg') if polygons else None

        progress = QProgressDialog(
            tr('Reading {0}...').format(item.title), tr('Cancel'), 0, 100,
            self)
        progress.setWindowTitle(tr('KGA Add Open Data'))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setValue(0)
        QApplication.processEvents()

        def on_download(fraction, text):
            if progress.wasCanceled():
                return False
            progress.setValue(int(fraction * 100))
            progress.setLabelText(text)
            QApplication.processEvents()
            return True

        def on_step(fraction):
            if progress.wasCanceled():
                return False
            progress.setValue(int(fraction * 100))
            QApplication.processEvents()
            return True

        polygon_error = None
        try:
            rasters = source.rasters(item, on_download)
            if rasters is None:
                self._say(tr('Download cancelled.'))
                return
            progress.setValue(0)
            progress.setLabelText(tr('Cutting out the area and writing {0}...')
                                  .format(os.path.basename(out_path)))
            written = LC.write_landcover(rasters, item, out_path,
                                         self._reproject_crs(), on_step)
            if written and polygons:
                progress.setValue(0)
                progress.setLabelText(tr('Turning the classes into polygons...'))
                try:
                    if not LC.polygonize(out_path, gpkg_path, item.classes,
                                         on_step):
                        gpkg_path = None
                except DEM.DemError as exc:
                    # The raster is fine; say why the polygons are missing
                    # rather than throwing the raster away with them.
                    polygon_error = str(exc)
                    gpkg_path = None
                except Exception as exc:    # pragma: no cover - GDAL failure
                    self._log(exc)
                    polygon_error = tr('The raster was added, but it could '
                                       'not be turned into polygons: {0}'
                                       ).format(exc)
                    gpkg_path = None
        except net.NetError as exc:
            self._warn(str(exc))
            return
        except Exception as exc:            # pragma: no cover - defensive
            self._log(exc)
            self._warn(tr('The land-cover raster could not be built: {0}')
                       .format(exc))
            return
        finally:
            progress.close()
            net.trim_cache()

        if not written:
            self._say(tr('Cancelled.'))
            return

        project = QgsProject.instance()
        name = LL.unique_name(project, self._chosen_name(item.title))
        layer = QgsRasterLayer(out_path, name, 'gdal')
        if not layer.isValid():
            self._warn(tr('The raster was written to {0} but QGIS could not '
                          'open it.').format(out_path))
            return
        _style_landcover_raster(layer, item.classes)
        LL.tag_provenance(layer, item)
        project.addMapLayer(layer)

        added = [layer.name()]
        if gpkg_path:
            vector = QgsVectorLayer('{0}|layername={1}'.format(
                gpkg_path, LC.POLYGON_LAYER), LL.unique_name(
                    project, tr('{0} polygons').format(name)), 'ogr')
            if vector.isValid():
                _style_landcover_polygons(vector, item.classes)
                LL.tag_provenance(vector, item)
                project.addMapLayer(vector)
                added.append(vector.name())
            else:
                polygon_error = tr('The polygons were written to {0} but QGIS '
                                   'could not open them.').format(gpkg_path)

        self._say(tr('Added "{0}" - {1} x {2} pixels.').format(
            '", "'.join(added), layer.width(), layer.height()))
        if polygon_error:
            self._warn(polygon_error)
        if temporary:
            self._push_save_message(layer, tr(
                '"{0}" was written to a temporary file, which QGIS deletes '
                'when it closes - save it to keep it.').format(layer.name()))
        else:
            self._push_message(tr('Land cover saved to {0}. {1}').format(
                out_path, item.attribution), warn=False)

    def _sibling_path(self, path, extension):
        """`path` with `extension`, numbered past any file already there.

        Never an existing file, so an earlier run's polygons - possibly open
        in the project - are left alone rather than overwritten.
        """
        stem = os.path.splitext(path)[0]
        candidate = stem + extension
        index = 2
        while os.path.exists(candidate):
            candidate = '{0}_{1}{2}'.format(stem, index, extension)
            index += 1
        return candidate

    def _dem_output_path(self, item):
        """(path, is_temporary), or (None, False) when the path is unusable."""
        path = (self.output_widget.filePath() or '').strip()
        if not path:
            stem = re.sub(r'[^A-Za-z0-9_-]+', '_',
                          self._chosen_name(item.title)).strip('_') or 'dem'
            folder = QgsProcessingUtils.tempFolder()
            path = os.path.join(folder, stem + '.tif')
            index = 2
            while os.path.exists(path):
                path = os.path.join(folder, '{0}_{1}.tif'.format(stem, index))
                index += 1
            return path, True

        if not path.lower().endswith(('.tif', '.tiff')):
            path += '.tif'
        folder = os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            self._warn(tr('The folder for "{0}" does not exist.').format(path))
            return None, False
        target = os.path.normcase(os.path.abspath(path))
        for layer in QgsProject.instance().mapLayers().values():
            source = layer.source().split('|')[0]
            if os.path.normcase(os.path.abspath(source)) == target:
                self._warn(tr('"{0}" is open in the project as "{1}". Remove '
                              'that layer first, or choose another file name.')
                           .format(path, layer.name()))
                return None, False
        if os.path.exists(path):
            # A path typed into the box never went through a save dialog, so
            # nothing has asked yet whether the file may be replaced.
            answer = QMessageBox.question(
                self, tr('Replace the file?'),
                tr('"{0}" already exists. Replace it?').format(
                    os.path.basename(path)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return None, False
        return path, False

    def _reproject_crs(self):
        """The CRS to warp into, or None to keep the published grid."""
        if not self.reproject_check.isChecked():
            return None
        crs = QgsProject.instance().crs()
        return crs if crs.isValid() else None

    def _update_reproject_label(self):
        crs = QgsProject.instance().crs()
        self.reproject_check.setText(tr('Reproject to the project CRS ({0})')
                                     .format(crs.authid() if crs.isValid()
                                             else tr('none set')))
        if self._current_kind() == DS.KIND_LANDCOVER:
            self.reproject_check.setToolTip(tr(
                'Land cover is kept on the grid it is published on - UTM for '
                'Esri, latitude and longitude for WorldCover. Tick this to '
                'warp it into the project CRS with nearest-neighbour '
                'resampling, which never invents a class that was not there.'))
        else:
            self.reproject_check.setToolTip(tr(
                'Open DEMs are published in latitude and longitude '
                '(EPSG:4326). Tick this to warp the result into the project '
                'CRS with bilinear resampling - handy for tools that want '
                'metres. Left unticked, the published grid is kept exactly.'))

    def _build_roi(self):
        """The DEM area for the current ROI mode. Raises DemError if unset."""
        mode = self.roi_combo.currentData()
        if mode == DEM.ROI_CANVAS:
            if self.canvas is None:
                raise DEM.DemError(tr('The map canvas is not available.'))
            return DEM.roi_from_extent(
                self.canvas.extent(),
                self.canvas.mapSettings().destinationCrs(),
                tr('Current map extent'))
        if mode == DEM.ROI_DRAW:
            if self._drawn is None:
                raise DEM.DemError(tr(
                    'Draw the area on the map: click <b>Draw rectangle</b> or '
                    '<b>Draw polygon</b> above, then draw it.'))
            geometry, crs, shape = self._drawn
            return DEM.roi_from_geometry(
                geometry, crs, tr('Drawn rectangle') if shape ==
                SHAPE_RECTANGLE else tr('Drawn polygon'))

        layer = self.roi_layer_combo.currentLayer()
        if layer is None:
            raise DEM.DemError(tr('Choose a layer.') if mode ==
                               DEM.ROI_LAYER_EXTENT else
                               tr('Choose a polygon layer to clip to.'))
        if mode == DEM.ROI_LAYER_CLIP:
            return DEM.roi_from_layer(layer,
                                      self.roi_selected_check.isChecked())
        return DEM.roi_from_extent(layer.extent(), layer.crs(),
                                   tr('Extent of {0}').format(layer.name()))

    def _on_roi_changed(self):
        if self._loading:
            return
        self._apply_roi_filter()
        self._apply_visibility()
        self._refresh_item()

    def _on_roi_input_changed(self, *_args):
        if self._loading:
            return
        self._refresh_item()

    def _apply_roi_filter(self):
        """Polygon layers for a boundary to clip to; anything with an extent
        otherwise."""
        if self.roi_combo.currentData() == DEM.ROI_LAYER_CLIP:
            wanted = layer_filter('PolygonLayer')
        else:
            wanted = layer_filter('HasGeometry', 'RasterLayer')
        self.roi_layer_combo.setFilters(wanted)
        if self.roi_layer_combo.currentLayer() is None:
            # Not the empty row a filter change leaves behind: the active
            # layer, or the first one that fits, is the likely answer.
            active = iface.activeLayer() if iface is not None else None
            index = self.roi_layer_combo.model().indexFromLayer(active) \
                if active is not None else None
            if index is not None and index.isValid():
                self.roi_layer_combo.setLayer(active)
            elif self.roi_layer_combo.count() > 1:
                self.roi_layer_combo.setCurrentIndex(1)

    def _on_canvas_extent(self):
        if self._current_kind() in AREA_KINDS and self.isVisible() \
                and self.roi_combo.currentData() == DEM.ROI_CANVAS:
            self._schedule_reload()

    # Drawing. The tool is ours - see roi_tools - and the two buttons are its
    # checkable switches, kept in step with the canvas through mapToolSet.

    def _arm_draw(self, shape, checked):
        if self.canvas is None:
            self._sync_draw_buttons()
            return
        if not checked:
            self._disarm_draw()
            return
        if self._roi_tool is None:
            self._roi_tool = RoiMapTool(self.canvas, shape)
            self._roi_tool.drawn.connect(self._on_drawn)
            self._roi_tool.message.connect(
                lambda text: self._say(text, warn=True))
        else:
            self._roi_tool.set_shape(shape)
        current = self.canvas.mapTool()
        if current is not None and current is not self._roi_tool:
            self._previous_tool = current
        self.canvas.setMapTool(self._roi_tool)
        self._sync_draw_buttons()
        self._say(tr('Press and drag on the map to draw the rectangle.')
                  if shape == SHAPE_RECTANGLE else
                  tr('Click each corner on the map; double-click, right-click '
                     'or press Enter to finish.'))

    def _disarm_draw(self):
        if self.canvas is not None and self._drawing():
            previous = getattr(self, '_previous_tool', None)
            self._previous_tool = None
            if previous is not None:
                try:
                    self.canvas.setMapTool(previous)
                except RuntimeError:        # the old tool was deleted
                    self.canvas.unsetMapTool(self._roi_tool)
            else:
                self.canvas.unsetMapTool(self._roi_tool)
        self._sync_draw_buttons()

    def _drawing(self):
        return (self._roi_tool is not None and self.canvas is not None
                and self.canvas.mapTool() is self._roi_tool)

    def _on_map_tool_set(self, *_args):
        self._sync_draw_buttons()

    def _sync_draw_buttons(self):
        active = self._drawing()
        shape = self._roi_tool.shape if self._roi_tool is not None else None
        for button, own in ((self.draw_rect_button, SHAPE_RECTANGLE),
                            (self.draw_poly_button, SHAPE_POLYGON)):
            button.blockSignals(True)
            button.setChecked(active and shape == own)
            button.blockSignals(False)

    def _on_drawn(self, geometry):
        crs = self.canvas.mapSettings().destinationCrs()
        self._drawn = (geometry, crs, self._roi_tool.shape)
        if self._roi_band is None:
            self._roi_band = make_band(self.canvas, fill_alpha=25)
        self._roi_band.setToGeometry(geometry, crs)
        self._roi_band.setVisible(True)
        # One shape is what was asked for; hand the map back for panning.
        self._disarm_draw()
        self._say(tr('Area drawn. Draw again to replace it.'))
        self._refresh_item()

    def _clear_drawn(self):
        self._drawn = None
        if self._roi_band is not None:
            drop_band(self.canvas, self._roi_band)
            self._roi_band = None
        self._refresh_item()

    def _show_drawn_band(self, visible):
        if self._roi_band is not None:
            self._roi_band.setVisible(visible)

    # The OpenTopography key, kept in the QGIS password store when asked to.

    def _load_api_key(self):
        if self._api_key_loaded:
            return
        self._api_key_loaded = True
        if DEM.has_saved_api_key():
            key = DEM.load_api_key()
            if key:
                self.api_key_edit.setText(key)
                self.api_key_remember.setChecked(True)

    def _keep_api_key(self):
        source = self._current_source()
        if not getattr(source, 'needs_api_key', False):
            return
        key = self.api_key_edit.text().strip()
        if self.api_key_remember.isChecked() and key:
            if not DEM.save_api_key(key):
                self._push_message(tr(
                    'The API key could not be saved in the QGIS password '
                    'store. Set a master password in Settings > Options > '
                    'Authentication to keep it.'), warn=True)
        elif not self.api_key_remember.isChecked() \
                and DEM.has_saved_api_key():
            DEM.save_api_key('')

    # ------------------------------------------------- the scratch-layer talk

    def _temp_layer_notice(self, layer, item):
        """Say, once loudly and then quietly, that this layer is temporary."""
        message = tr(
            '"{0}" was added as a temporary scratch layer. It lives in memory '
            'only and is lost when the project closes - export it to a file to '
            'keep it.').format(layer.name())

        self._push_save_message(layer, message)

        if QgsSettings().value(SETTINGS_TEMP_WARNING, 'yes') != 'yes':
            return

        box = QMessageBox(self)
        box.setWindowTitle(tr('Added as a temporary layer'))
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(message)
        box.setInformativeText(tr(
            'Right-click the layer and choose Export > Save Features As, or '
            'use the KGA Layer Export / Import tool, to write it to a '
            'GeoPackage or shapefile.'))
        if item is not None and item.attribution:
            box.setDetailedText(tr('Attribution: {0}').format(item.attribution))
        check = QCheckBox(tr("Don't show this again"), box)
        box.setCheckBox(check)
        box.exec()
        if check.isChecked():
            QgsSettings().setValue(SETTINGS_TEMP_WARNING, 'no')

    def _push_save_message(self, layer, message):
        """A message-bar warning carrying a Save button for that layer."""
        if iface is None:
            return
        bar = iface.messageBar()
        widget = bar.createMessage(tr('KGA Toolbox'), message)
        button = QPushButton(tr('Save layer...'), widget)

        def save():
            iface.setActiveLayer(layer)
            action = iface.actionLayerSaveAs()
            if action is not None:
                action.trigger()

        button.clicked.connect(save)
        widget.layout().addWidget(button)
        bar.pushWidget(widget, Qgis.MessageLevel.Warning, 0)

    # ---------------------------------------------------------------- buttons

    def refresh(self):
        """Drop what is cached for the current selection and describe it again."""
        source = self._current_source()
        if source is not None and self._item is not None:
            cached = source.cached_file(self._item)
            if cached:
                try:
                    os.remove(cached)
                except OSError:             # pragma: no cover - locked file
                    pass
        self._reload_now()
        self._say(tr('Re-read from the source.'))

    def clear_cache(self):
        size = net.cache_size()
        if not size:
            self._say(tr('Nothing is cached.'))
            return
        answer = QMessageBox.question(
            self, tr('Clear the download cache'),
            tr('Delete the {0} of data this tool has downloaded?\n\n'
               'Layers already on the map are not affected - they hold their '
               'own copy in memory.').format(net.human_size(size)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        freed = net.clear_cache()
        self._say(tr('Cleared {0}.').format(net.human_size(freed)))
        self._refresh_item()

    # ------------------------------------------------------------- housekeeping

    def _say(self, text, warn=False):
        self.status.setText(text)
        self.status.setStyleSheet(
            'color: palette(highlight);' if warn else 'color: palette(mid);')

    def _warn(self, text):
        self._say(text, warn=True)
        QMessageBox.warning(self, tr('KGA Add Open Data'), text)

    def _push_message(self, text, warn=False):
        if iface is None:
            return
        level = Qgis.MessageLevel.Warning if warn else Qgis.MessageLevel.Info
        iface.messageBar().pushMessage(tr('KGA Toolbox'), text, level, 8)

    def _log(self, exc):
        QgsMessageLog.logMessage(
            'Add Open Data: {0}'.format(exc), LOG_TAG,
            Qgis.MessageLevel.Warning)

    def _restore_last(self):
        """Come back where the user left off, which is usually the same place."""
        settings = QgsSettings()
        # Quietly throughout: the first describe runs once the window is up,
        # not once per restored combo.
        self._loading = True
        try:
            iso3 = settings.value('{0}/country'.format(SETTINGS_LAST),
                                  DEFAULT_ISO3)
            index = self.country_combo.findData(iso3)
            self.country_combo.setCurrentIndex(
                index if index >= 0
                else self.country_combo.findData(DEFAULT_ISO3))

            # Clipping to a layer's boundary is not brought back: rebuilding
            # it means dissolving every polygon of that layer, which is a poor
            # thing for a window to do the moment it opens. The user picks it
            # again when they want it; the map extent costs nothing.
            roi = settings.value('{0}/roi'.format(SETTINGS_DEM),
                                 DEM.ROI_CANVAS)
            if roi == DEM.ROI_LAYER_CLIP:
                roi = DEM.ROI_CANVAS
            index = self.roi_combo.findData(roi)
            self.roi_combo.setCurrentIndex(max(0, index))
            self.reproject_check.setChecked(str(settings.value(
                '{0}/reproject'.format(SETTINGS_DEM), 'false')).lower()
                == 'true')
            self.polygon_check.setChecked(str(settings.value(
                '{0}/polygons'.format(SETTINGS_LANDCOVER), 'false')).lower()
                == 'true')
            self._apply_roi_filter()

            kind = settings.value('{0}/kind'.format(SETTINGS_LAST),
                                  DS.KIND_ADMIN)
            index = self.type_combo.findData(kind)
            if index >= 0:
                self.type_combo.setCurrentIndex(index)
        finally:
            self._loading = False

    def _remember_last(self):
        settings = QgsSettings()
        settings.setValue('{0}/country'.format(SETTINGS_LAST), self._iso3())
        settings.setValue('{0}/kind'.format(SETTINGS_LAST), self._current_kind())
        settings.setValue('{0}/roi'.format(SETTINGS_DEM),
                          self.roi_combo.currentData())
        settings.setValue('{0}/reproject'.format(SETTINGS_DEM),
                          'true' if self.reproject_check.isChecked() else 'false')
        settings.setValue('{0}/polygons'.format(SETTINGS_LANDCOVER),
                          'true' if self.polygon_check.isChecked() else 'false')

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded and not self._torn_down:
            self._first_load.start()

    def closeEvent(self, event):
        self._teardown()
        super().closeEvent(event)

    def done(self, result):
        # Escape and reject() arrive here without a closeEvent, and used to
        # leave the window hidden but alive: still wired to the canvas, its
        # drawn area still painted on the map, reloading on every pan.
        self._teardown()
        super().done(result)

    def _teardown(self):
        """End the session: remember the choices, forget everything else.

        The window is deleted rather than hidden, so a reopen starts from a
        clean form instead of replaying the last one's state.
        """
        if self._torn_down:
            return
        self._torn_down = True
        # A pending request must not fire into a window that is going away.
        self._debounce.stop()
        self._first_load.stop()
        self._remember_last()
        self._release_canvas()
        self._item = None
        self.deleteLater()

    def _release_canvas(self):
        """Give the map back: the draw tool, its bands and the signals.

        The bands belong to the canvas scene, not to this window, so without
        this a drawn area would stay painted on the map after the window shut.
        """
        if self.canvas is None:
            return
        self._disarm_draw()
        if self._roi_tool is not None:
            self._roi_tool.remove()
            self._roi_tool = None
        self._drawn = None
        drop_band(self.canvas, self._roi_band)
        self._roi_band = None
        for signal, slot in ((self.canvas.extentsChanged,
                              self._on_canvas_extent),
                             (self.canvas.mapToolSet, self._on_map_tool_set)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass


def _send_to_bottom(project, layer):
    """Move a freshly added layer to the bottom of the layer tree."""
    with suppress(Exception):               # pragma: no cover - tree API drift
        root = project.layerTreeRoot()
        node = root.findLayer(layer.id())
        if node is None:
            return
        clone = node.clone()
        root.insertChildNode(-1, clone)
        node.parent().removeChildNode(node)


def _style_landcover_raster(layer, classes):
    """The publisher's legend as a paletted renderer, labelled by class name.

    The file's own colour table would give the colours; this adds the names
    to the Layers panel legend. Saved as the file's default style (a .qml
    beside it), so it opens the same way in the next project too.
    """
    entries = [QgsPalettedRasterRenderer.Class(
        lc.code, QColor(lc.color), '{0} - {1}'.format(lc.code, lc.name))
        for lc in classes]
    renderer = QgsPalettedRasterRenderer(layer.dataProvider(), 1, entries)
    layer.setRenderer(renderer)
    _save_default_style(layer)


def _style_landcover_polygons(layer, classes):
    """Polygons coloured by lc_code with the same legend as the raster."""
    categories = []
    for lc in classes:
        symbol = QgsFillSymbol.createSimple({
            'color': lc.color, 'outline_style': 'no'})
        categories.append(QgsRendererCategory(
            lc.code, symbol, '{0} - {1}'.format(lc.code, lc.name)))
    layer.setRenderer(QgsCategorizedSymbolRenderer('lc_code', categories))
    _save_default_style(layer)


def _save_default_style(layer):
    # A style that only lives in the project is still a styled layer.
    with suppress(Exception):               # pragma: no cover - API drift
        try:
            layer.saveDefaultStyle(
                QgsMapLayer.StyleCategory.AllStyleCategories)
        except TypeError:                   # pragma: no cover - QGIS < 3.26
            layer.saveDefaultStyle()


def _escape(text):
    """Minimal HTML escaping for the details panel.

    The strings here come off somebody else's API, so they are escaped before
    they reach a rich-text label rather than trusted to be tag-free.
    """
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))

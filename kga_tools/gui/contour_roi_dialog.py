# -*- coding: utf-8 -*-
"""Generate Contour for ROI: the window.

UI and wiring only. The region of interest comes from the same code the Add
Open Data tool uses (``core.dem_sources``), the job itself is a
``contours.ContourTask`` on a worker thread, and one progress bar runs across
all of it - reading the DEM, cutting it out, tracing the lines, smoothing.

The window is modeless and lives until closed. Drawing an area uses
``RoiMapTool``, a map tool of its own rather than an iface action.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
from contextlib import suppress

from qgis.core import (Qgis, QgsMessageLog,
                       QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.gui import (QgsDoubleSpinBox, QgsFileWidget, QgsMapLayerComboBox,
                      QgsProjectionSelectionWidget)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget)

from .window_reveal import revealed
from ..branding import LOG_TAG, open_docs
from ..core import contour_style as style
from ..core import contours as C
from ..core import dem_sources as DEM
from ..core import layer_loader as LL
from ..core import net
from ..core.imagery import paths
from .output_fields import FollowingField
from ..core.modify_features import layer_filter
from .roi_tools import SHAPE_POLYGON, SHAPE_RECTANGLE, RoiMapTool, drop_band, make_band

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'generate_contour_roi'

# Colours sampled from icons/branding/kga.png, as in the Imagery Downloader.
BLUE = '#174592'
ORANGE = '#FC663A'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE}

#: The dataset entry that means "my own file", and its ROI choice.
LOCAL = 'local'
ROI_WHOLE = 'whole'

ROI_MODES = (
    (ROI_WHOLE, 'Whole DEM (no cutting)'),
    (DEM.ROI_CANVAS, 'Current map extent'),
    (DEM.ROI_DRAW, 'Draw a rectangle or polygon on the map'),
    (DEM.ROI_LAYER_CLIP, 'Boundary of a polygon layer'),
    (DEM.ROI_LAYER_EXTENT, 'Extent of a layer'),
)

LABEL_MODES = (
    (style.LABELS_MAIN, 'Main contours only'),
    (style.LABELS_ALL, 'All contours'),
    (style.LABELS_NONE, 'No labels'),
)

RASTER_FILTER = ('Rasters (*.tif *.tiff *.img *.vrt *.asc *.dem *.hgt);;'
                 'All files (*.*)')

#: The DEM source. Copernicus on AWS is the one that needs no account or key;
#: OpenTopography is left to the Add Open Data tool.
SOURCE = DEM.DEM_SOURCES[0]


def tr(text):
    return text


@revealed
class ContourRoiDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Generate Contour for ROI'))
        self.setObjectName('KgaContourRoiDialog')
        self.resize(560, 640)
        self.setStyleSheet(STYLE)

        self.canvas = iface.mapCanvas() if iface is not None else None
        self._task = None
        self._spec = None
        self._temp_dir = None
        self._closing = False
        self._torn_down = False
        self._loading = True
        self._drawn = None                  # (geometry, canvas crs, shape)
        self._roi_tool = None
        self._roi_band = None
        self._previous_tool = None
        self._item = None
        self._layer_cache = {}
        self._lockable = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_source())
        root.addWidget(self._build_tabs())
        root.addWidget(self._build_details(), 1)
        root.addLayout(self._build_progress())
        root.addLayout(self._build_buttons())

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._refresh_details)

        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)

        if self.canvas is not None:
            self.canvas.extentsChanged.connect(self._on_canvas_extent)
            self.canvas.mapToolSet.connect(self._on_map_tool_set)
        self._loading = False
        self._on_source_changed()
        self._on_main_every()
        QTimer.singleShot(0, self._refresh_details)

    # ------------------------------------------------------------- building

    def _build_tabs(self):
        """One tab per section; the tab area is as tall as the page shown."""
        self.tabs = QTabWidget(self)
        # `_build_area` first: `_on_source_changed` reads the ROI combo.
        sections = (
            (self._build_area(), tr('ROI'),
             tr('The area of the DEM to contour')),
            (self._build_contours(), tr('Contour'),
             tr('Interval, main contours, smoothing and labels')),
            (self._build_terrain(), tr('Terrain Surface'),
             tr('Hillshade, slope, aspect and random spot heights')),
            (self._build_crs(), tr('Output CRS'),
             tr('The coordinate system of the results')),
            (self._build_output(), tr('Output'),
             tr('Where the results go')))
        self._pages = []
        for box, name, tip in sections:
            box.setFlat(True)
            box.setTitle('')
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(2, 6, 2, 2)
            lay.addWidget(box)
            lay.addStretch(1)
            self._pages.append(page)
            self.tabs.setTabToolTip(self.tabs.addTab(page, name), tip)
        self.tabs.currentChanged.connect(self._fit_tab)
        self._fit_tab()
        return self.tabs

    def _fit_tab(self, *_args):
        """Size the tab area to the page shown, not the tallest page."""
        current = self.tabs.currentWidget()
        for page in self._pages:
            page.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred if page is current
                               else QSizePolicy.Policy.Ignored)
        self.tabs.updateGeometry()

    def _lock(self, *widgets):
        self._lockable.extend(widgets)

    def _build_source(self):
        box = QGroupBox(tr('Elevation source'), self)
        self.source_form = form = QFormLayout(box)
        self.dataset_combo = QComboBox(box)
        for dataset in SOURCE.DATASETS:
            self.dataset_combo.addItem(dataset.label, dataset.id)
        self.dataset_combo.addItem(tr('Local DEM - a raster layer or file'),
                                   LOCAL)
        self.dataset_combo.setToolTip(tr(
            'Copernicus DEM, read from the AWS open-data bucket: free, no '
            'account and no API key. Or use a DEM you already have.'))
        self.dataset_combo.currentIndexChanged.connect(self._on_source_changed)
        form.addRow(tr('DEM'), self.dataset_combo)

        self.local_layer = QgsMapLayerComboBox(box)
        self.local_layer.setFilters(layer_filter('RasterLayer'))
        self.local_layer.setAllowEmptyLayer(True)
        self.local_layer.layerChanged.connect(self._schedule)
        form.addRow(tr('Raster layer'), self.local_layer)

        self.local_file = QgsFileWidget(box)
        self.local_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.local_file.setFilter(tr(RASTER_FILTER))
        self.local_file.setToolTip(tr(
            'A DEM file to use instead of the layer above. Any raster GDAL '
            'can read, in any coordinate system.'))
        self.local_file.fileChanged.connect(self._schedule)
        form.addRow(tr('or DEM file'), self.local_file)

        self._lock(self.dataset_combo, self.local_layer, self.local_file)
        return box

    def _is_local(self):
        return self.dataset_combo.currentData() == LOCAL

    def _on_source_changed(self, *_args):
        local = self._is_local()
        self._set_row_visible(self.local_layer, local, self.source_form)
        self._set_row_visible(self.local_file, local, self.source_form)
        # "Whole DEM" only means something for a DEM of one's own.
        item = self.roi_combo.model().item(
            self.roi_combo.findData(ROI_WHOLE))
        if item is not None:
            item.setEnabled(local)
        if local:
            if self.local_layer.currentLayer() is None \
                    and self.local_layer.count() > 1:
                self.local_layer.setCurrentIndex(1)
            if self.roi_combo.currentData() == DEM.ROI_CANVAS:
                self.roi_combo.setCurrentIndex(
                    self.roi_combo.findData(ROI_WHOLE))
        elif self.roi_combo.currentData() == ROI_WHOLE:
            self.roi_combo.setCurrentIndex(
                self.roi_combo.findData(DEM.ROI_CANVAS))
        self._on_mode_changed()

    def _build_area(self):
        box = QGroupBox(tr('Region of interest'), self)
        self.area_form = form = QFormLayout(box)

        self.roi_combo = QComboBox(box)
        for mode, label in ROI_MODES:
            self.roi_combo.addItem(label, mode)
        self.roi_combo.setToolTip(tr(
            'A map or layer extent gives its bounding box. A drawn shape or a '
            'layer boundary cuts the DEM to the shape.'))
        self.roi_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow(tr('Area'), self.roi_combo)

        self.draw_widget = QWidget(box)
        row = QHBoxLayout(self.draw_widget)
        row.setContentsMargins(0, 0, 0, 0)
        self.draw_rect_button = QPushButton(tr('Draw rectangle'),
                                            self.draw_widget)
        self.draw_rect_button.setCheckable(True)
        self.draw_rect_button.setToolTip(tr(
            'Press and drag on the map to draw a rectangle.'))
        self.draw_rect_button.clicked.connect(
            lambda checked: self._arm_draw(SHAPE_RECTANGLE, checked))
        row.addWidget(self.draw_rect_button)
        self.draw_poly_button = QPushButton(tr('Draw polygon'),
                                            self.draw_widget)
        self.draw_poly_button.setCheckable(True)
        self.draw_poly_button.setToolTip(tr(
            'Click each corner on the map, then double-click, right-click or '
            'press Enter to close the polygon. Backspace removes the last '
            'corner, Escape starts again.'))
        self.draw_poly_button.clicked.connect(
            lambda checked: self._arm_draw(SHAPE_POLYGON, checked))
        row.addWidget(self.draw_poly_button)
        self.draw_clear_button = QPushButton(tr('Clear'), self.draw_widget)
        self.draw_clear_button.clicked.connect(self._clear_drawn)
        row.addWidget(self.draw_clear_button)
        row.addStretch(1)
        form.addRow('', self.draw_widget)

        self.layer_combo = QgsMapLayerComboBox(box)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.layerChanged.connect(self._schedule)
        form.addRow(tr('Layer'), self.layer_combo)

        self.selected_check = QCheckBox(tr('Selected features only'), box)
        self.selected_check.toggled.connect(self._schedule)
        form.addRow('', self.selected_check)

        self._lock(self.roi_combo, self.draw_widget, self.layer_combo,
                   self.selected_check)
        return box

    def _build_contours(self):
        box = QGroupBox(tr('Contours'), self)
        form = QFormLayout(box)

        self.interval_spin = QgsDoubleSpinBox(box)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setRange(0.1, 5000.0)
        self.interval_spin.setValue(10.0)
        self.interval_spin.setSuffix(' m')
        self.interval_spin.setToolTip(tr(
            'Vertical distance between contour lines. Lines fall on whole '
            'multiples of it (0, 10, 20 ... for 10 m).'))
        self.interval_spin.valueChanged.connect(self._on_main_every)
        form.addRow(tr('Interval'), self.interval_spin)

        self.main_spin = QSpinBox(box)
        self.main_spin.setRange(2, 20)
        self.main_spin.setValue(5)
        self.main_spin.setPrefix(tr('every '))
        self.main_spin.setSuffix(tr('th contour'))
        self.main_spin.setToolTip(tr(
            'Every Nth contour is a main (index) contour: drawn thick and dark '
            'and, with "Main contours only", the only one labelled.'))
        self.main_spin.valueChanged.connect(self._on_main_every)
        self.main_hint = QLabel('', box)
        self.main_hint.setStyleSheet('font-style: italic;')
        main_row = QHBoxLayout()
        main_row.addWidget(self.main_spin)
        main_row.addWidget(self.main_hint, 1)
        form.addRow(tr('Main contour'), main_row)

        self.smooth_check = QCheckBox(tr('Smooth the lines'), box)
        self.smooth_check.setToolTip(tr(
            'Round off the pixel steps in the lines. Makes a nicer map and '
            'roughly doubles the number of vertices; the lines move by less '
            'than a pixel, so do not smooth if you will measure from them.'))
        form.addRow('', self.smooth_check)

        self.symbolize_check = QCheckBox(
            tr('Symbolize: main dark and thick, sub grey and thin'), box)
        self.symbolize_check.setChecked(True)
        form.addRow('', self.symbolize_check)

        self.label_combo = QComboBox(box)
        for mode, label in LABEL_MODES:
            self.label_combo.addItem(label, mode)
        self.label_combo.setToolTip(tr('Which contours carry an elevation '
                                       'label.'))
        form.addRow(tr('Labels'), self.label_combo)

        self._lock(self.interval_spin, self.main_spin, self.smooth_check,
                   self.symbolize_check, self.label_combo)
        return box

    def _build_terrain(self):
        box = QGroupBox(tr('Terrain surfaces'), self)
        column = QVBoxLayout(box)
        row = QHBoxLayout()
        column.addLayout(row)
        self.terrain_checks = {}
        tips = {
            'hillshade': tr('Shaded relief, lit from the north-west.'),
            'slope': tr('Steepness in degrees.'),
            'aspect': tr('The compass direction the ground faces, 0-360 '
                         'degrees clockwise from north.'),
        }
        for kind in C.TERRAIN:
            check = QCheckBox(tr(kind.capitalize()), box)
            check.setToolTip(tips[kind])
            self.terrain_checks[kind] = check
            row.addWidget(check)
        row.addStretch(1)

        self.spot_check = QCheckBox(tr('Add random spot height'), box)
        self.spot_check.setToolTip(tr(
            'Scatter spot heights at random over the area, spread evenly '
            'rather than clumped. Each is a small black dot with its height '
            'beside it, in the style of a topographic map.'))
        self.spot_check.toggled.connect(self._on_spot_toggled)
        self.spot_spin = QSpinBox(box)
        self.spot_spin.setRange(1, 5000)
        self.spot_spin.setValue(50)
        self.spot_spin.setPrefix(tr('about '))
        self.spot_spin.setSuffix(tr(' spots'))
        self.spot_spin.setToolTip(tr('How many spot heights to place.'))
        spot_row = QHBoxLayout()
        spot_row.addWidget(self.spot_check)
        spot_row.addWidget(self.spot_spin)
        spot_row.addStretch(1)
        column.addLayout(spot_row)
        column.addStretch(1)
        self._on_spot_toggled()
        self._lock(*self.terrain_checks.values(), self.spot_check)
        return box

    def _on_spot_toggled(self, *_args):
        self.spot_spin.setEnabled(self.spot_check.isChecked()
                                  and not self._running())

    def _spot_count(self):
        return self.spot_spin.value() if self.spot_check.isChecked() else 0

    def _terrain(self):
        return tuple(kind for kind, check in self.terrain_checks.items()
                     if check.isChecked())

    def _build_crs(self):
        box = QGroupBox(tr('Output CRS'), self)
        column = QVBoxLayout(box)
        self.crs_project_radio = QRadioButton(box)
        self.crs_project_radio.setChecked(True)
        self.crs_project_radio.setToolTip(tr(
            'The DEM is published in WGS 84 (EPSG:4326). It is warped into '
            'this CRS (bilinear), and the contours are made in it.'))
        self.crs_keep_radio = QRadioButton(tr('Same as the DEM'), box)
        self.crs_keep_radio.setToolTip(tr(
            'No reprojection: keep the grid of the DEM as it is (WGS 84 for '
            'the Copernicus DEM).'))
        self.crs_custom_radio = QRadioButton(tr('Specified CRS'), box)
        self.crs_widget = QgsProjectionSelectionWidget(box)
        self.crs_widget.setCrs(QgsProject.instance().crs())
        self.crs_widget.crsChanged.connect(self._schedule)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.crs_custom_radio)
        custom_row.addWidget(self.crs_widget, 1)
        column.addWidget(self.crs_project_radio)
        column.addWidget(self.crs_keep_radio)
        column.addLayout(custom_row)
        for radio in (self.crs_project_radio, self.crs_keep_radio,
                      self.crs_custom_radio):
            radio.toggled.connect(self._on_crs_mode)
        QgsProject.instance().crsChanged.connect(self._on_project_crs)
        self._on_project_crs()
        self._on_crs_mode()
        self._lock(self.crs_project_radio, self.crs_keep_radio,
                   self.crs_custom_radio, self.crs_widget)
        return box

    def _on_project_crs(self, *_args):
        crs = QgsProject.instance().crs()
        self.crs_project_radio.setText(tr('Current project CRS - {0}').format(
            crs.authid() or crs.description() or tr('not set')))
        self._schedule()

    def _on_crs_mode(self, *_args):
        self.crs_widget.setEnabled(self.crs_custom_radio.isChecked()
                                   and not self._running())
        self._schedule()

    def _output_crs(self):
        """The CRS to warp into, or None to keep the DEM's own grid."""
        if self.crs_keep_radio.isChecked():
            return None
        if self.crs_custom_radio.isChecked():
            crs = self.crs_widget.crs()
        else:
            crs = QgsProject.instance().crs()
        if not crs.isValid():
            raise DEM.DemError(tr('Choose a valid output CRS.'))
        return crs

    def _build_output(self):
        box = QGroupBox(tr('Output'), self)
        form = QFormLayout(box)

        self.folder_widget = QgsFileWidget(box)
        self.folder_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        line = self.folder_widget.lineEdit()
        if line is not None:
            line.setPlaceholderText(tr('[Save to a temporary folder]'))
        self.folder_widget.setToolTip(tr(
            'Where the DEM (GeoTIFF) and the contours (GeoPackage) are '
            'written. Leave it empty for a temporary folder, which QGIS '
            'deletes when it closes.'))
        self.folder_widget.fileChanged.connect(self._schedule)
        form.addRow(tr('Output folder'), self.folder_widget)
        FollowingField(self.folder_widget).suggest_folder()   # the project folder

        self.name_edit = QLineEdit('Contours', box)
        self.name_edit.setToolTip(tr(
            'Base name: <name>_DEM.tif and <name>_contours.gpkg.'))
        form.addRow(tr('Name'), self.name_edit)

        self.add_dem_check = QCheckBox(tr('Also add the DEM to the map'), box)
        self.add_dem_check.setChecked(True)
        form.addRow('', self.add_dem_check)

        self._lock(self.folder_widget, self.name_edit, self.add_dem_check)
        return box

    def _build_details(self):
        box = QGroupBox(tr('Details'), self)
        column = QVBoxLayout(box)
        self.details = QLabel('', box)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop
                                  | Qt.AlignmentFlag.AlignLeft)
        self.details.setMinimumHeight(90)
        column.addWidget(self.details)
        return box

    def _build_progress(self):
        box = QVBoxLayout()
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat('%p%')
        self.status = QLabel(tr('Ready.'), self)
        self.status.setWordWrap(True)
        box.addWidget(self.bar)
        box.addWidget(self.status)
        return box

    def _build_buttons(self):
        row = QHBoxLayout()
        self.help_btn = QPushButton(tr('Help'), self)
        self.help_btn.clicked.connect(lambda: open_docs(ALG_NAME))
        self.go_btn = QPushButton(tr('Generate contours'), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setProperty('running', False)
        self.go_btn.clicked.connect(self._go_or_stop)
        self.close_btn = QPushButton(tr('Close'), self)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.help_btn)
        row.addStretch(1)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        return row

    # ---------------------------------------------------------- small state

    def _running(self):
        return self._task is not None

    def _dataset(self):
        wanted = self.dataset_combo.currentData()
        for dataset in SOURCE.DATASETS:
            if dataset.id == wanted:
                return dataset
        return SOURCE.DATASETS[0]

    def _schedule(self, *_args):
        if self._loading or self._torn_down:
            return
        self._debounce.start()

    def _set_row_visible(self, widget, visible, form=None):
        label = (form or self.area_form).labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    def _on_main_every(self, *_args):
        self.main_hint.setText(tr('= every {0:g} m').format(
            self.interval_spin.value() * self.main_spin.value()))

    def _on_mode_changed(self, *_args):
        if self._loading:
            return
        mode = self.roi_combo.currentData()
        layer_mode = mode in (DEM.ROI_LAYER_CLIP, DEM.ROI_LAYER_EXTENT)
        self._set_row_visible(self.draw_widget, mode == DEM.ROI_DRAW)
        self._set_row_visible(self.layer_combo, layer_mode)
        self._set_row_visible(self.selected_check, mode == DEM.ROI_LAYER_CLIP)
        if layer_mode:
            self._apply_layer_filter()
        if self._roi_band is not None:
            self._roi_band.setVisible(mode == DEM.ROI_DRAW)
        if mode != DEM.ROI_DRAW:
            self._disarm_draw()
        self._schedule()

    def _apply_layer_filter(self):
        """Polygon layers to take a boundary from; any layer for an extent."""
        if self.roi_combo.currentData() == DEM.ROI_LAYER_CLIP:
            self.layer_combo.setFilters(layer_filter('PolygonLayer'))
        else:
            self.layer_combo.setFilters(layer_filter('HasGeometry',
                                                     'RasterLayer'))
        if self.layer_combo.currentLayer() is None:
            active = iface.activeLayer() if iface is not None else None
            index = self.layer_combo.model().indexFromLayer(active) \
                if active is not None else None
            if index is not None and index.isValid():
                self.layer_combo.setLayer(active)
            elif self.layer_combo.count() > 1:
                self.layer_combo.setCurrentIndex(1)

    def _on_canvas_extent(self, *_args):
        if self.isVisible() and not self._running() \
                and self.roi_combo.currentData() == DEM.ROI_CANVAS:
            self._schedule()

    # -------------------------------------------------------------- drawing

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
            self._roi_tool.message.connect(self._say)
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
            previous, self._previous_tool = self._previous_tool, None
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
        self._disarm_draw()
        self._say(tr('Area drawn. Draw again to replace it.'))
        self._schedule()

    def _clear_drawn(self):
        self._drawn = None
        if self._roi_band is not None:
            drop_band(self.canvas, self._roi_band)
            self._roi_band = None
        self._schedule()

    # ----------------------------------------------------- area and details

    def _build_roi(self):
        """The ROI for the current mode. Raises DemError when it is unset."""
        mode = self.roi_combo.currentData()
        if mode == DEM.ROI_CANVAS:
            if self.canvas is None:
                raise DEM.DemError(tr('The map canvas is not available.'))
            return DEM.roi_from_extent(
                self.canvas.extent(), self.canvas.mapSettings().destinationCrs(),
                tr('Current map extent'))
        if mode == DEM.ROI_DRAW:
            if self._drawn is None:
                raise DEM.DemError(tr(
                    'Click <b>Draw rectangle</b> or <b>Draw polygon</b>, then '
                    'draw the area on the map.'))
            geometry, crs, shape = self._drawn
            return DEM.roi_from_geometry(
                geometry, crs, tr('Drawn rectangle') if shape ==
                SHAPE_RECTANGLE else tr('Drawn polygon'))
        layer = self.layer_combo.currentLayer()
        if layer is None:
            raise DEM.DemError(tr('Choose a layer.') if mode ==
                               DEM.ROI_LAYER_EXTENT else
                               tr('Choose a polygon layer.'))
        if mode == DEM.ROI_LAYER_CLIP:
            selected = self.selected_check.isChecked()
            key = (layer.id(), selected,
                   tuple(sorted(layer.selectedFeatureIds())) if selected
                   else ())
            if key not in self._layer_cache:
                # A boundary is a union of every feature; once is enough.
                self._layer_cache = {key: DEM.roi_from_layer(layer, selected)}
            return self._layer_cache[key]
        return DEM.roi_from_extent(layer.extent(), layer.crs(),
                                   tr('Extent of {0}').format(layer.name()))

    def _local_dem(self):
        """The user's own DEM. Raises ContourError when it cannot be used."""
        path = self.local_file.filePath().strip()
        if not path:
            layer = self.local_layer.currentLayer()
            if layer is None:
                raise C.ContourError(tr(
                    'Choose a DEM raster layer, or browse for a DEM file.'))
            if layer.providerType() != 'gdal':
                raise C.ContourError(tr(
                    '"{0}" is not a file-based raster. Choose another layer '
                    'or browse for the DEM file.').format(layer.name()))
            path = layer.source().split('|')[0]
        return C.LocalDem(path)

    def _describe(self):
        """(job, error text). Never raises."""
        try:
            if self._is_local():
                local = self._local_dem()
                whole = self.roi_combo.currentData() == ROI_WHOLE
                job = C.DemJob.from_local(
                    local, None if whole else self._build_roi())
            else:
                roi = self._build_roi()
                job = C.DemJob.from_item(SOURCE.describe(None, None, {
                    'roi': roi, 'dataset': self._dataset().id}))
        except (net.NetError, C.ContourError) as exc:
            return None, str(exc)
        return job, ''

    def _refresh_details(self):
        if self._torn_down or self._running():
            return
        job, error = self._describe()
        if job is not None:
            try:
                self._output_crs()
            except DEM.DemError as exc:
                job, error = None, str(exc)
        self._item = job
        if job is None:
            self.details.setText('<span style="color:%s">%s</span>'
                                 % (ORANGE, error))
            self._update_go_enabled()
            return
        shown = job.shown_roi
        rows = [('Area', '{0} - {1:,.0f} km2'.format(shown.label,
                                                     shown.area_km2())),
                ('Bounds', shown.describe_bounds()),
                ('Resolution', job.resolution),
                ('Output CRS', self._crs_text(job)),
                ('DEM size', '{0:,} pixels, about {1}'.format(
                    job.pixels, net.human_size(
                        C.dem_bytes(job.pixels, job.dtype_bytes))))]
        html = ''.join('<tr><td style="padding-right:10px"><b>{0}</b></td>'
                       '<td>{1}</td></tr>'.format(k, v) for k, v in rows)
        html = '<table>{0}</table>'.format(html)
        warning = C.size_warning(job.pixels, job.dtype_bytes,
                                 self._is_temporary())
        if warning is not None:
            html += ('<p style="color:{0}"><b>Large area.</b> It is a big job; '
                     'you will be asked to confirm before it starts.</p>'
                     .format(ORANGE))
        elif job.clip:
            html += '<p>The DEM is cut to the shape; outside it is NoData.</p>'
        self.details.setText(html)
        self._update_go_enabled()

    def _crs_text(self, job):
        crs = self._output_crs()
        if crs is None:
            if job.local is not None:
                return '{0} - as the DEM'.format(job.local.crs.authid()
                                                 or job.local.crs.description())
            return 'WGS 84 (EPSG:4326), as published'
        return '{0} - {1}'.format(crs.authid(), crs.description())

    def _update_go_enabled(self):
        if not self._running():
            self.go_btn.setEnabled(self._item is not None)

    def _is_temporary(self):
        return not self.folder_widget.filePath().strip()

    def _say(self, text, warn=False):
        self.status.setText(text)
        if warn:
            self.status.setStyleSheet('color: %s;' % ORANGE)
        else:
            self.status.setStyleSheet('')

    def _warn(self, text):
        QMessageBox.warning(self, tr('Generate Contour for ROI'), text)

    # ------------------------------------------------------------------ run

    def _go_or_stop(self):
        if self._running():
            self._say(tr('Stopping...'))
            self.go_btn.setEnabled(False)
            self._task.request_stop()
            return
        self._start()

    def _start(self):
        # Described afresh rather than taken from the panel: a map-extent area
        # follows the map, and the map may have moved since.
        self._layer_cache.clear()
        job, error = self._describe()
        if job is None:
            self._warn(error)
            return
        name = self.name_edit.text().strip()
        problem = paths.name_problem(name)
        if problem:
            self._warn(problem)
            return

        folder = self.folder_widget.filePath().strip()
        temporary = not folder
        if not temporary:
            folder = os.path.normpath(folder)
            problem = paths.folder_problem(folder)
            if problem:
                self._warn(problem)
                return
        interval = self.interval_spin.value()
        try:
            crs = self._output_crs()
        except DEM.DemError as exc:
            self._warn(str(exc))
            return

        target = folder if not temporary else None
        free = self._free_bytes(target)
        warning = C.size_warning(job.pixels, job.dtype_bytes, temporary, free)
        if warning is not None:
            severe, text = warning
            box = QMessageBox(
                QMessageBox.Icon.Warning if severe else
                QMessageBox.Icon.Question, tr('Large area'),
                text + '\n\n' + tr('Continue anyway?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                self)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return

        try:
            if temporary:
                folder = C.temp_folder()
                self._temp_dir = folder
            else:
                os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._warn(tr('The output folder could not be created: {0}')
                       .format(exc))
            return
        dem_path = os.path.join(folder, '{0}_DEM.tif'.format(name))
        contour_path = os.path.join(folder, '{0}_contours.gpkg'.format(name))

        if not temporary:
            wanted = [dem_path, contour_path] + [
                os.path.join(folder, '{0}_{1}.tif'.format(name, kind))
                for kind in self._terrain()]
            existing = [os.path.basename(p) for p in wanted
                        if os.path.exists(p)]
            if existing:
                answer = QMessageBox.question(
                    self, tr('Overwrite?'),
                    tr('{0} already exists. Replace it?').format(
                        ' and '.join(existing)))
                if answer != QMessageBox.StandardButton.Yes:
                    return

        if job.local is not None:
            rasters = [job.local.path]
        else:
            # The tile list is fetched here, on the GUI thread, because the
            # download helper spins an event loop that a worker thread must
            # not.
            self._say(tr('Locating the DEM tiles...'))
            self.bar.setValue(0)
            self.bar.setFormat('%p%')
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                rasters = SOURCE.rasters(job.item)
            except net.NetError as exc:
                self._cleanup_temp()
                self._warn(str(exc))
                self._say(tr('Ready.'))
                return
            finally:
                QApplication.restoreOverrideCursor()
            if not rasters:
                self._cleanup_temp()
                return

        spec = C.ContourSpec(
            rasters, job.roi, dem_path, contour_path, interval,
            self.main_spin.value(), self.smooth_check.isChecked(), name,
            crs, job.local, self._terrain(), self._spot_count())
        self._spec = spec
        self._item = job
        task = C.ContourTask(spec)
        task.phase.connect(self._on_phase)
        task.jobFinished.connect(self._on_finished)
        self._task = task
        self._set_running(True)
        self._say(tr('Starting...'))
        C.start(task)

    @staticmethod
    def _free_bytes(folder):
        from ..core.imagery import downloader

        if folder is None:
            from qgis.core import QgsProcessingUtils
            folder = QgsProcessingUtils.tempFolder()
        return downloader.free_bytes(folder)

    def _set_running(self, running):
        for widget in self._lockable:
            widget.setEnabled(not running)
        self.go_btn.setText(tr('Stop') if running else tr('Generate contours'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True if running else self._item is not None)
        if not running:
            self._on_mode_changed()
            self._on_crs_mode()
            self._on_spot_toggled()
        else:
            self.spot_spin.setEnabled(False)

    def _on_phase(self, text, fraction):
        pct = int(round(100.0 * fraction))
        self.bar.setValue(pct)
        self.bar.setFormat('{0}   %p%'.format(text))
        self._say(text + '...')

    def _on_finished(self, result):
        spec = self._spec
        self._task = None
        if self._torn_down:
            self._cleanup_temp()
            return
        self._set_running(False)
        if self._closing:
            self._cleanup_temp()
            self._teardown()
            return
        state = result.get('state')
        if state == 'done':
            self._on_done(result, spec)
        elif state == 'stopped':
            self.bar.setValue(0)
            self.bar.setFormat('%p%')
            self._cleanup_temp()
            self._say(tr('Stopped. Nothing was added to the map.'))
        else:
            message = result.get('message') or tr('The contours failed.')
            self.bar.setValue(0)
            self.bar.setFormat('%p%')
            self._say(message, warn=True)
            QgsMessageLog.logMessage(message, LOG_TAG,
                                     Qgis.MessageLevel.Warning)
            QMessageBox.critical(self, tr('Generate Contour for ROI'),
                                 message)
            # The DEM may have survived (the message says so); a temporary
            # folder has no one to tell, so it goes.
            if self._temp_dir and not os.path.exists(spec.dem_path):
                self._cleanup_temp()

    def _on_done(self, result, spec):
        self.bar.setValue(100)
        self.bar.setFormat('100%')
        project = QgsProject.instance()
        contours = QgsVectorLayer('{0}|layername={1}'.format(
            spec.contour_path, C.CONTOUR_LAYER), spec.title, 'ogr')
        if not contours.isValid():
            self._warn(tr('The contours were written to {0} but QGIS could '
                          'not open them.').format(spec.contour_path))
            return
        contours.setName(LL.unique_name(project, '{0} ({1:g} m)'.format(
            spec.title, spec.interval)))
        style.apply_style(contours, self.symbolize_check.isChecked(),
                          self.label_combo.currentData())
        self._tag(contours, spec)

        dem = None
        if self.add_dem_check.isChecked():
            dem = QgsRasterLayer(spec.dem_path, '{0} DEM'.format(spec.title),
                                 'gdal')
            if dem.isValid():
                dem.setName(LL.unique_name(project, dem.name()))
                style.style_dem(dem, result['low'], result['high'])
                self._tag(dem, spec)
            else:
                dem = None

        spots = None
        if result.get('spots'):
            spots = QgsVectorLayer('{0}|layername={1}'.format(
                spec.contour_path, C.SPOT_LAYER), spec.title, 'ogr')
            if spots.isValid():
                spots.setName(LL.unique_name(
                    project, '{0} spot heights'.format(spec.title)))
                style.style_spot_heights(spots)
                self._tag(spots, spec)
            else:
                spots = None
        if result.get('spot_error'):
            self._warn(result['spot_error'])

        terrain_layers = []
        for kind, path in (result.get('terrain') or {}).items():
            layer = QgsRasterLayer(path, '{0} {1}'.format(spec.title, kind),
                                   'gdal')
            if layer.isValid():
                layer.setName(LL.unique_name(project, layer.name()))
                style.style_terrain(layer, kind)
                self._tag(layer, spec)
                terrain_layers.append((kind, layer))
        if result.get('terrain_error'):
            self._warn(result['terrain_error'])

        # The DEM goes in below the vector layers (but above any basemap
        # raster) so it never hides what the user already has; the contours
        # go on top.
        root = project.layerTreeRoot()
        if dem is not None:
            project.addMapLayer(dem, False)
            root.insertLayer(self._first_raster_index(root), dem)
        # Each goes in at the same spot, so the last one added ends up on top:
        # slope and aspect first (and switched off, so they do not hide the
        # DEM), then the hillshade over them.
        for kind, layer in sorted(terrain_layers, key=lambda k: k[0] ==
                                  'hillshade'):
            project.addMapLayer(layer, False)
            node = root.insertLayer(self._first_raster_index(root), layer)
            if kind != 'hillshade' and node is not None:
                node.setItemVisibilityChecked(False)
        project.addMapLayer(contours, False)
        root.insertLayer(0, contours)
        if spots is not None:
            project.addMapLayer(spots, False)
            root.insertLayer(0, spots)

        text = tr('Done. {0:,} contour lines from {1:,.0f} to {2:,.0f} m '
                  'added to the map.').format(
                      result['features'], result['low'], result['high'])
        self._say(text)
        if self._temp_dir:
            self._push(tr('"{0}" was written to a temporary folder, which '
                          'QGIS deletes when it closes - export it to keep '
                          'it.').format(contours.name()), warn=True)
        else:
            self._push(tr('Contours saved to {0}. {1}').format(
                spec.contour_path,
                '' if spec.local else DEM.COPERNICUS_ATTRIBUTION), warn=False)
        self._temp_dir = None

    @staticmethod
    def _first_raster_index(root):
        """Where a DEM goes: above the first raster, else at the bottom."""
        from qgis.core import QgsLayerTreeLayer

        children = root.children()
        for index, node in enumerate(children):
            if isinstance(node, QgsLayerTreeLayer) \
                    and isinstance(node.layer(), QgsRasterLayer):
                return index
        return len(children)

    @staticmethod
    def _tag(layer, spec):
        with suppress(Exception):           # attribution is best effort
            metadata = layer.metadata()
            if spec.local is None:
                metadata.setRights([DEM.COPERNICUS_ATTRIBUTION])
                metadata.setLicenses([DEM.COPERNICUS_LICENCE])
            metadata.setTitle(spec.title)
            layer.setMetadata(metadata)

    def _push(self, text, warn):
        if iface is None:
            return
        bar = iface.messageBar()
        (bar.pushWarning if warn else bar.pushSuccess)(
            tr('Generate Contour for ROI'), text)

    def _cleanup_temp(self):
        if self._temp_dir:
            C.remove_folder(self._temp_dir)
            self._temp_dir = None

    # -------------------------------------------------------------- closing

    def closeEvent(self, event):
        if self._running():
            self._confirm_stop()
            event.ignore()                  # torn down when the task ends
            return
        self._teardown()
        super().closeEvent(event)

    def done(self, result):
        if self._running():
            self._confirm_stop()
            return
        self._teardown()
        super().done(result)

    def _confirm_stop(self):
        answer = QMessageBox.question(
            self, tr('Generate Contour for ROI'),
            tr('Stop generating contours and close?'))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._closing = True
        self.hide()
        if self._task is not None:
            self._task.request_stop()
        return True

    def shutdown(self):
        """Plugin unload: stop any job cleanly, then go away."""
        self._closing = True
        task = self._task
        if task is not None:
            task.request_stop()
            with suppress(Exception):
                task.waitForFinished(15000)
        self._task = None
        self._teardown()

    def _teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self._debounce.stop()
        with suppress(Exception):
            QgsProject.instance().crsChanged.disconnect(self._on_project_crs)
        if self.canvas is not None:
            for signal, slot in ((self.canvas.extentsChanged,
                                  self._on_canvas_extent),
                                 (self.canvas.mapToolSet,
                                  self._on_map_tool_set)):
                with suppress(Exception):
                    signal.disconnect(slot)
            if self._roi_tool is not None and \
                    self.canvas.mapTool() is self._roi_tool:
                try:
                    if self._previous_tool is not None:
                        self.canvas.setMapTool(self._previous_tool)
                    else:
                        self.canvas.unsetMapTool(self._roi_tool)
                except RuntimeError:
                    pass
            if self._roi_tool is not None:
                self._roi_tool.remove()
                self._roi_tool = None
            drop_band(self.canvas, self._roi_band)
            self._roi_band = None
        self.deleteLater()

# -*- coding: utf-8 -*-
"""Generate River or Stream for ROI: the window.

UI and wiring only. The region of interest comes from ``RoiPicker`` (the same
code the Add Open Data tool's drawing uses), the DEM from ``core.dem_sources``,
and the job itself is a ``streams.StreamTask`` on a worker thread. One progress
bar runs across all of it - reading the DEM, cutting it out, routing the water,
tracing the streams, checking them.

The window is modeless and lives until closed.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
from contextlib import suppress
from types import SimpleNamespace

from qgis.core import (Qgis, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsMessageLog,
                       QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.gui import (QgsDoubleSpinBox, QgsFileWidget, QgsMapLayerComboBox,
                      QgsProjectionSelectionWidget)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSpinBox, QTextBrowser,
    QVBoxLayout, QWidget)

from ..branding import LOG_TAG, open_docs
from ..core import contour_style
from ..core import dem_sources as DEM
from ..core import layer_loader as LL
from ..core import net
from ..core import stream_check as CHECK
from ..core import stream_style as style
from ..core import streams as S
from ..core.imagery import paths
from .output_fields import FollowingField
from ..core.modify_features import layer_filter
from .roi_picker import RoiPicker

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'generate_stream_roi'
TITLE = 'Generate River or Stream for ROI'

# Colours sampled from icons/kga.png, as in the Imagery Downloader.
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

#: The DEM source. Copernicus on AWS is the one that needs no account or key.
SOURCE = DEM.DEM_SOURCES[0]
#: The DEM combo's id for a DEM the user already has.
LOCAL = 'local'

REFERENCE_NONE = 'none'
REFERENCE_OSM = 'osm'
REFERENCE_LAYER = 'layer'
REFERENCES = (
    (REFERENCE_NONE, 'None - internal checks only'),
    (REFERENCE_OSM, 'OpenStreetMap rivers and streams (needs internet)'),
    (REFERENCE_LAYER, 'A line layer of my own'),
)

STYLES = (
    (style.STYLE_ORDER, 'By stream order (blue, wider = bigger)'),
    (style.STYLE_CONFIDENCE, 'By check confidence (green / amber / red)'),
)

#: Kilometres of extra DEM around the area, so streams that rise outside it
#: arrive with their whole catchment.
MARGINS = ((0, 'Off - use the area exactly'), (2, '2 km'), (5, '5 km'),
           (10, '10 km'))

CRS_AUTO, CRS_PROJECT, CRS_CUSTOM, CRS_PUBLISHED = (
    'auto', 'project', 'custom', 'published')


def tr(text):
    return text


class StreamRoiDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaStreamRoiDialog')
        self.resize(600, 820)
        self.setStyleSheet(STYLE)

        self.canvas = iface.mapCanvas() if iface is not None else None
        self._task = None
        self._spec = None
        self._temp_dir = None
        self._closing = False
        self._torn_down = False
        self._loading = True
        self._item = None
        self._roi = None
        self._tolerance_touched = False
        self._report_dialog = None
        self._last_report = ''
        self._lockable = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # The options scroll; the details, the progress bar and the buttons
        # stay put underneath.
        body = QWidget(self)
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 4, 0)
        column.setSpacing(8)
        column.addWidget(self._build_source())
        self.picker = RoiPicker(self.canvas, body)
        self.picker.changed.connect(self._schedule)
        self.picker.message.connect(self._say)
        column.addWidget(self.picker)
        column.addWidget(self._build_streams())
        column.addWidget(self._build_check())
        column.addWidget(self._build_crs())
        column.addWidget(self._build_output())
        column.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        root.addWidget(self._build_details())
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

        self._loading = False
        self._on_dataset()
        self._on_detail_changed()
        self._on_reference_changed()
        QTimer.singleShot(0, self._refresh_details)

    # ------------------------------------------------------------- building

    def _lock(self, *widgets):
        self._lockable.extend(widgets)

    def _build_source(self):
        box = QGroupBox(tr('Elevation source'), self)
        form = QFormLayout(box)
        self.dataset_combo = QComboBox(box)
        for dataset in SOURCE.DATASETS:
            self.dataset_combo.addItem(dataset.label, dataset.id)
        self.dataset_combo.addItem(tr('Local DEM - a raster layer or file'),
                                   LOCAL)
        self.dataset_combo.setToolTip(tr(
            'Copernicus DEM, read from the AWS open-data bucket. Free: no '
            'account and no API key. 30 m is the one to use; 90 m is for a '
            'very large area. Or use a DEM you already have.'))
        self.dataset_combo.currentIndexChanged.connect(self._on_dataset)
        form.addRow(tr('DEM'), self.dataset_combo)

        self.local_layer_combo = QgsMapLayerComboBox(box)
        self.local_layer_combo.setAllowEmptyLayer(True)
        self.local_layer_combo.setFilters(layer_filter('RasterLayer'))
        self.local_layer_combo.setToolTip(tr(
            'A raster layer in the project. Band 1 is read as elevation in '
            'metres.'))
        self.local_layer_combo.layerChanged.connect(self._on_dataset)
        form.addRow(tr('DEM layer'), self.local_layer_combo)
        self.local_file_widget = QgsFileWidget(box)
        self.local_file_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.local_file_widget.setFilter(
            'Rasters (*.tif *.tiff *.img *.asc *.vrt *.hgt *.dem);;'
            'All files (*)')
        self.local_file_widget.setToolTip(tr(
            'Or a DEM file. Used only when no layer is chosen above.'))
        self.local_file_widget.fileChanged.connect(self._on_dataset)
        form.addRow(tr('or DEM file'), self.local_file_widget)
        self._form_source = form
        self._lock(self.dataset_combo, self.local_layer_combo,
                   self.local_file_widget)
        return box

    def _build_streams(self):
        box = QGroupBox(tr('Stream network'), self)
        form = QFormLayout(box)

        self.detail_combo = QComboBox(box)
        for ident, label, km2, _tip in S.DETAIL_PRESETS:
            self.detail_combo.addItem(
                '{0} - from {1:g} km²'.format(label, km2), ident)
        self.detail_combo.addItem(tr('Custom - set the contributing area'),
                                  S.DETAIL_CUSTOM)
        self.detail_combo.addItem(tr('Match a drainage density'),
                                  S.DETAIL_DENSITY)
        self.detail_combo.setCurrentIndex(self.detail_combo.findData(
            S.DEFAULT_DETAIL))
        self.detail_combo.currentIndexChanged.connect(self._on_detail_changed)
        form.addRow(tr('Detail'), self.detail_combo)

        self.area_spin = QgsDoubleSpinBox(box)
        self.area_spin.setDecimals(3)
        self.area_spin.setRange(0.01, 5000.0)
        self.area_spin.setValue(2.0)
        self.area_spin.setSuffix(' km²')
        self.area_spin.setToolTip(tr(
            'A cell is a stream when at least this much ground drains '
            'through it. Smaller gives more, finer streams.'))
        self.area_spin.valueChanged.connect(self._on_detail_changed)
        form.addRow(tr('Contributing area'), self.area_spin)

        self.density_spin = QgsDoubleSpinBox(box)
        self.density_spin.setDecimals(2)
        self.density_spin.setRange(0.1, 5.0)
        self.density_spin.setValue(1.0)
        self.density_spin.setSuffix(' km per km²')
        self.density_spin.setToolTip(tr(
            'Stream length per square kilometre. The tool picks the '
            'contributing area that gives about this much stream. Natural '
            'catchments run roughly 0.5 (sparse) to 2 (dense).'))
        self.density_spin.valueChanged.connect(self._on_detail_changed)
        form.addRow(tr('Drainage density'), self.density_spin)

        self.detail_hint = QLabel('', box)
        self.detail_hint.setWordWrap(True)
        self.detail_hint.setStyleSheet('font-style: italic;')
        form.addRow('', self.detail_hint)

        self.margin_combo = QComboBox(box)
        for km, label in MARGINS:
            self.margin_combo.addItem(label, km)
        self.margin_combo.setToolTip(tr(
            'Streams that rise outside the area are cut short at its edge, '
            'and the ones just inside it look smaller than they are. Adding a '
            'margin downloads some extra DEM, so each stream is traced from its '
            'real source; the streams are cut back to the area afterwards. The '
            'DEM saved then includes the margin.'))
        self.margin_combo.currentIndexChanged.connect(self._schedule)
        form.addRow(tr('Include upstream area'), self.margin_combo)

        self.smooth_check = QCheckBox(tr('Smooth the lines'), box)
        self.smooth_check.setChecked(True)
        self.smooth_check.setToolTip(tr(
            'Round off the cell-by-cell steps in the lines. Makes a nicer '
            'map; the lines move by less than a cell. LENGTH_M is measured '
            'before smoothing.'))
        form.addRow('', self.smooth_check)

        self.style_combo = QComboBox(box)
        for ident, label in STYLES:
            self.style_combo.addItem(label, ident)
        self.style_combo.setToolTip(tr(
            'How the streams are drawn on the map. The attributes are the '
            'same either way.'))
        form.addRow(tr('Symbology'), self.style_combo)

        self._lock(self.detail_combo, self.area_spin, self.density_spin,
                   self.margin_combo, self.smooth_check, self.style_combo)
        self._detail_rows = (self.area_spin, self.density_spin)
        self._form_streams = form
        return box

    def _build_check(self):
        box = QGroupBox(tr('Check the streams'), self)
        form = QFormLayout(box)
        self._form_check = form

        note = QLabel(tr(
            'Every segment is always graded for confidence from the DEM alone '
            '(flat ground, filled depressions, no valley). Add a reference to '
            'also find streams that only one network has.'), box)
        note.setWordWrap(True)
        note.setStyleSheet('font-style: italic;')
        form.addRow(note)

        self.reference_combo = QComboBox(box)
        for ident, label in REFERENCES:
            self.reference_combo.addItem(label, ident)
        self.reference_combo.setToolTip(tr(
            'OpenStreetMap: natural rivers and streams only (canals and '
            'drains are dug, not drained, so they are left out). Your own '
            'layer: any line layer of streams, e.g. a national hydrography '
            'or a survey.'))
        self.reference_combo.currentIndexChanged.connect(
            self._on_reference_changed)
        form.addRow(tr('Compare with'), self.reference_combo)

        self.ref_layer_combo = QgsMapLayerComboBox(box)
        self.ref_layer_combo.setAllowEmptyLayer(True)
        self.ref_layer_combo.setFilters(layer_filter('LineLayer'))
        form.addRow(tr('Line layer'), self.ref_layer_combo)

        self.ref_selected_check = QCheckBox(tr('Selected features only'), box)
        form.addRow('', self.ref_selected_check)

        self.tolerance_spin = QSpinBox(box)
        self.tolerance_spin.setRange(10, 2000)
        self.tolerance_spin.setSingleStep(10)
        self.tolerance_spin.setSuffix(' m')
        self.tolerance_spin.setValue(90)
        self.tolerance_spin.setToolTip(tr(
            'A stretch of one network counts as matched when the other has a '
            'line within this distance. About three DEM cells is fair: it '
            'allows for the DEM\'s own position error and for how a '
            'reference was digitised.'))
        self.tolerance_spin.valueChanged.connect(self._touch_tolerance)
        form.addRow(tr('Match distance'), self.tolerance_spin)

        self._lock(self.reference_combo, self.ref_layer_combo,
                   self.ref_selected_check, self.tolerance_spin)
        return box

    def _touch_tolerance(self, *_args):
        if not self._loading:
            self._tolerance_touched = True

    def _build_crs(self):
        box = QGroupBox(tr('Output CRS'), self)
        column = QVBoxLayout(box)
        self.crs_auto_radio = QRadioButton(tr(
            'UTM zone of the area (recommended)'), box)
        self.crs_auto_radio.setChecked(True)
        self.crs_auto_radio.setToolTip(tr(
            'Streams, slopes and catchment areas are measured in metres, so a '
            'metric CRS gives the truest numbers.'))
        self.crs_project_radio = QRadioButton(box)
        self.crs_published_radio = QRadioButton(tr(
            'WGS 84 (EPSG:4326), as published'), box)
        self.crs_custom_radio = QRadioButton(tr('Specified CRS'), box)
        self.crs_widget = QgsProjectionSelectionWidget(box)
        self.crs_widget.setCrs(QgsProject.instance().crs())
        self.crs_widget.crsChanged.connect(self._schedule)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.crs_custom_radio)
        custom_row.addWidget(self.crs_widget, 1)
        column.addWidget(self.crs_auto_radio)
        column.addWidget(self.crs_project_radio)
        column.addWidget(self.crs_published_radio)
        column.addLayout(custom_row)
        self._crs_radios = (self.crs_auto_radio, self.crs_project_radio,
                            self.crs_published_radio, self.crs_custom_radio)
        for radio in self._crs_radios:
            radio.toggled.connect(self._on_crs_mode)
        QgsProject.instance().crsChanged.connect(self._on_project_crs)
        self._on_project_crs()
        self._on_crs_mode()
        self._lock(*self._crs_radios)
        self._lock(self.crs_widget)
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

    def _crs_mode(self):
        if self.crs_custom_radio.isChecked():
            return CRS_CUSTOM
        if self.crs_project_radio.isChecked():
            return CRS_PROJECT
        if self.crs_published_radio.isChecked():
            return CRS_PUBLISHED
        return CRS_AUTO

    def _output_crs(self, roi):
        """The CRS to warp into, or None when the grid is kept as published."""
        mode = self._crs_mode()
        if mode == CRS_PUBLISHED:
            return None
        if mode == CRS_AUTO:
            if self._is_local():
                # Keep a metric DEM's own grid; anything else goes to UTM.
                with suppress(DEM.DemError):
                    own = self._local_raster().crs()
                    if own.isValid() and not own.isGeographic() and \
                            own.mapUnits() == Qgis.DistanceUnit.Meters:
                        return own
            return S.utm_crs(roi)
        crs = self.crs_widget.crs() if mode == CRS_CUSTOM \
            else QgsProject.instance().crs()
        if not crs.isValid():
            raise DEM.DemError(tr('Choose a valid output CRS.'))
        if crs == QgsCoordinateReferenceSystem(DEM.WGS84):
            return None
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
            'Where the DEM (GeoTIFF), the streams (GeoPackage) and the report '
            'are written. Leave it empty for a temporary folder, which QGIS '
            'deletes when it closes.'))
        self.folder_widget.fileChanged.connect(self._schedule)
        form.addRow(tr('Output folder'), self.folder_widget)
        FollowingField(self.folder_widget).suggest_folder()   # the project folder

        self.name_edit = QLineEdit('Streams', box)
        self.name_edit.setToolTip(tr(
            'Base name: <name>_DEM.tif, <name>_streams.gpkg and '
            '<name>_report.txt.'))
        form.addRow(tr('Name'), self.name_edit)

        self.add_dem_check = QCheckBox(tr('Also add the DEM to the map'), box)
        self.add_dem_check.setChecked(True)
        form.addRow('', self.add_dem_check)

        self.accum_check = QCheckBox(
            tr('Also save the flow accumulation raster (km² draining '
               'through each cell)'), box)
        self.accum_check.setToolTip(tr(
            'Useful to judge the stream threshold, or to pick catchment '
            'outlets. Saved as <name>_flow_accumulation.tif and added to the '
            'map switched off.'))
        form.addRow('', self.accum_check)

        self._lock(self.folder_widget, self.name_edit, self.add_dem_check,
                   self.accum_check)
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
        self.report_btn = QPushButton(tr('Last report'), self)
        self.report_btn.setEnabled(False)
        self.report_btn.clicked.connect(self._show_report)
        self.go_btn = QPushButton(tr('Generate streams'), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setProperty('running', False)
        self.go_btn.clicked.connect(self._go_or_stop)
        self.close_btn = QPushButton(tr('Close'), self)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.help_btn)
        row.addWidget(self.report_btn)
        row.addStretch(1)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        return row

    # ---------------------------------------------------------- small state

    def _running(self):
        return self._task is not None

    def _is_local(self):
        return self.dataset_combo.currentData() == LOCAL

    def _local_raster(self):
        """The user's own DEM as a QgsRasterLayer. Raises DemError."""
        layer = self.local_layer_combo.currentLayer()
        if layer is None:
            path = self.local_file_widget.filePath().strip()
            if not path:
                raise DEM.DemError(tr('Choose a DEM layer or a DEM file.'))
            if not os.path.isfile(path):
                raise DEM.DemError(tr('That DEM file does not exist.'))
            layer = QgsRasterLayer(path, os.path.splitext(
                os.path.basename(path))[0], 'gdal')
        if not layer.isValid() or layer.providerType() != 'gdal':
            raise DEM.DemError(tr('That is not a raster file QGIS can read '
                                  'as a DEM.'))
        return layer

    def _dataset(self):
        if self._is_local():
            try:
                return S.local_dataset(self._local_raster())
            except DEM.DemError:
                return SOURCE.DATASETS[0]
        wanted = self.dataset_combo.currentData()
        for dataset in SOURCE.DATASETS:
            if dataset.id == wanted:
                return dataset
        return SOURCE.DATASETS[0]

    def _schedule(self, *_args):
        if self._loading or self._torn_down:
            return
        self._debounce.start()

    def _on_dataset(self, *_args):
        local = self._is_local()
        for widget in (self.local_layer_combo, self.local_file_widget):
            self._row_visible(self._form_source, widget, local)
        if not self._tolerance_touched:
            self._loading = True
            self.tolerance_spin.setValue(
                int(round(S.MATCH_CELLS * self._dataset().arcsec * 30.87
                          / 10.0)) * 10)
            self._loading = False
        self._on_detail_changed()
        self._schedule()

    def _row_visible(self, form, widget, visible):
        label = form.labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    def _detail(self):
        """(mode, contributing area km2, density) for the chosen detail level."""
        mode = self.detail_combo.currentData()
        if mode == S.DETAIL_CUSTOM:
            return mode, self.area_spin.value(), None
        if mode == S.DETAIL_DENSITY:
            return mode, None, self.density_spin.value()
        for ident, _label, km2, _tip in S.DETAIL_PRESETS:
            if ident == mode:
                return mode, km2, None
        return S.DEFAULT_DETAIL, 5.0, None

    def _cells(self, km2):
        cell = self._dataset().arcsec * 30.87
        return km2 * 1e6 / (cell * cell)

    def _on_detail_changed(self, *_args):
        if self._loading:
            return
        mode, km2, _density = self._detail()
        self._row_visible(self._form_streams, self.area_spin,
                          mode == S.DETAIL_CUSTOM)
        self._row_visible(self._form_streams, self.density_spin,
                          mode == S.DETAIL_DENSITY)
        if mode == S.DETAIL_DENSITY:
            hint = tr('The contributing area is chosen after the water is '
                      'routed, so the network comes out about this dense.')
        else:
            tip = next((t for i, _l, _k, t in S.DETAIL_PRESETS if i == mode),
                       tr('Streams begin where at least this much ground '
                          'drains through a cell.'))
            hint = tr('{0} A cell is a stream when {1:g} km² (about '
                      '{2:,.0f} cells) drains through it.').format(
                          tip, km2, self._cells(km2))
        self.detail_hint.setText(hint)
        self._schedule()

    def _on_reference_changed(self, *_args):
        if self._loading:
            return
        mode = self.reference_combo.currentData()
        self._row_visible(self._form_check, self.ref_layer_combo,
                          mode == REFERENCE_LAYER)
        self._row_visible(self._form_check, self.ref_selected_check,
                          mode == REFERENCE_LAYER)
        self._row_visible(self._form_check, self.tolerance_spin,
                          mode != REFERENCE_NONE)
        if mode == REFERENCE_LAYER and self.ref_layer_combo.currentLayer() \
                is None and self.ref_layer_combo.count() > 1:
            self.ref_layer_combo.setCurrentIndex(1)
        self._schedule()

    # ----------------------------------------------------- area and details

    def _describe(self):
        """(item, work roi, error text). Never raises."""
        try:
            roi = self.picker.roi()
            margin = self.margin_combo.currentData()
            work = S.work_roi(roi, margin)
            if self._is_local():
                item = self._describe_local(work)
            else:
                item = SOURCE.describe(None, None, {
                    'roi': work, 'dataset': self._dataset().id})
        except net.NetError as exc:
            return None, None, str(exc)
        return item, roi, ''

    def _describe_local(self, work):
        """A stand-in for a described source item, for the user's own DEM."""
        raster = self._local_raster()
        try:
            box = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(DEM.WGS84), raster.crs(),
                QgsProject.instance()).transformBoundingBox(
                    work.geometry.boundingBox())
        except Exception:
            raise DEM.DemError(tr('The area could not be converted into the '
                                  'CRS of that DEM.'))
        if not box.intersects(raster.extent()):
            raise DEM.DemError(tr('That DEM does not cover the area.'))
        path = raster.source().split('|')[0]
        return SimpleNamespace(roi=work, dataset=S.local_dataset(raster),
                               rasters=[path], crs=raster.crs())

    def _refresh_details(self):
        if self._torn_down or self._running():
            return
        item, roi, error = self._describe()
        crs_text = ''
        if item is not None:
            try:
                crs_text = self._crs_text(roi)
            except DEM.DemError as exc:
                item, error = None, str(exc)
        self._item = item
        self._roi = roi
        if item is None:
            self.details.setText('<span style="color:%s">%s</span>'
                                 % (ORANGE, error))
            self._update_go_enabled()
            return
        pixels = S.pixel_count(item.roi, item.dataset)
        rows = [('Area', '{0} - {1:,.0f} km2'.format(roi.label,
                                                     roi.area_km2())),
                ('Bounds', roi.describe_bounds()),
                ('Resolution', item.dataset.resolution_text()),
                ('Output CRS', crs_text),
                ('DEM size', '{0:,} pixels, about {1} in memory'.format(
                    pixels, net.human_size(S.memory_bytes(pixels))))]
        html = ''.join('<tr><td style="padding-right:10px"><b>{0}</b></td>'
                       '<td>{1}</td></tr>'.format(k, v) for k, v in rows)
        html = '<table>{0}</table>'.format(html)
        warning = S.size_warning(pixels, self._is_temporary())
        _mode, km2, _density = self._detail()
        if pixels > S.MAX_PIXELS:
            html += ('<p style="color:{0}"><b>Too large.</b> This is more '
                     'than the tool will route in one go; choose a smaller '
                     'area or the 90 m DEM.</p>'.format(ORANGE))
        elif warning is not None:
            html += ('<p style="color:{0}"><b>Large area.</b> Finding streams '
                     'in it takes a lot of memory; you will be asked to '
                     'confirm before it starts.</p>'.format(ORANGE))
        elif km2 is not None and roi.area_km2() < 4 * km2:
            html += ('<p style="color:{0}">This area is small for that '
                     'detail level and may hold few or no streams. Try '
                     '<b>Detailed</b> or <b>Very detailed</b>.</p>'
                     .format(ORANGE))
        self.details.setText(html)
        self._update_go_enabled(pixels <= S.MAX_PIXELS)

    def _crs_text(self, roi):
        crs = self._output_crs(roi)
        if crs is None:
            return 'WGS 84 (EPSG:4326), as published'
        return '{0} - {1}'.format(crs.authid(), crs.description())

    def _update_go_enabled(self, allowed=True):
        if not self._running():
            self.go_btn.setEnabled(self._item is not None and allowed)

    def _is_temporary(self):
        return not self.folder_widget.filePath().strip()

    def _say(self, text, warn=False):
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % ORANGE if warn else '')

    def _warn(self, text):
        QMessageBox.warning(self, tr(TITLE), text)

    # ------------------------------------------------------------------ run

    def _go_or_stop(self):
        if self._running():
            self._say(tr('Stopping...'))
            self.go_btn.setEnabled(False)
            self._task.request_stop()
            return
        self._start()

    def _reference(self, roi):
        """``(reference, osm_bounds)`` for the chosen comparison.

        A layer is read here, on the GUI thread. OpenStreetMap is fetched by
        the task, on its own thread, so a slow server freezes nothing.
        """
        mode = self.reference_combo.currentData()
        if mode == REFERENCE_LAYER:
            reference = CHECK.reference_from_layer(
                self.ref_layer_combo.currentLayer(), roi.bounds(),
                self.ref_selected_check.isChecked())
            return reference, None
        if mode == REFERENCE_OSM:
            return None, roi.bounds()
        return None, None

    def _start(self):
        # Described afresh rather than taken from the panel: a map-extent area
        # follows the map, and the map may have moved since.
        self.picker.forget_cached_boundary()
        item, roi, error = self._describe()
        if item is None:
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
        try:
            crs = self._output_crs(roi)
            reference, osm_bounds = self._reference(roi)
        except (DEM.DemError, CHECK.ReferenceError) as exc:
            self._warn(str(exc))
            return

        pixels = S.pixel_count(item.roi, item.dataset)
        if pixels > S.MAX_PIXELS:
            self._warn(tr('That area is {0:,} million DEM pixels, more than '
                          'this tool will route in one go. Choose a smaller '
                          'area or the 90 m DEM.').format(pixels // 1000000))
            return
        free = self._free_bytes(folder if not temporary else None)
        warning = S.size_warning(pixels, temporary, free)
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
                folder = S.temp_folder()
                self._temp_dir = folder
            else:
                os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._warn(tr('The output folder could not be created: {0}')
                       .format(exc))
            return
        dem_path = os.path.join(folder, '{0}_DEM.tif'.format(name))
        stream_path = os.path.join(folder, '{0}_streams.gpkg'.format(name))
        accum_path = os.path.join(
            folder, '{0}_flow_accumulation.tif'.format(name)) \
            if self.accum_check.isChecked() else None
        report_path = os.path.join(folder, '{0}_report.txt'.format(name))

        if not temporary:
            existing = [os.path.basename(p) for p in (
                dem_path, stream_path, accum_path) if p and os.path.exists(p)]
            if existing:
                answer = QMessageBox.question(
                    self, tr('Overwrite?'),
                    tr('{0} already exists. Replace it?').format(
                        ' and '.join(existing)))
                if answer != QMessageBox.StandardButton.Yes:
                    return

        # The tile list is fetched here, on the GUI thread, because the
        # download helper spins an event loop that a worker thread must not.
        self._say(tr('Locating the DEM tiles...'))
        self.bar.setValue(0)
        self.bar.setFormat('%p%')
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rasters = item.rasters if self._is_local() \
                else SOURCE.rasters(item)
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

        spec = S.StreamSpec(
            rasters, roi, item.roi, dem_path, stream_path, self._detail(),
            self.smooth_check.isChecked(), name, crs, accum_path,
            report_path, reference, osm_bounds,
            float(self.tolerance_spin.value()), item.dataset,
            self._is_local())
        self._spec = spec
        self._item = item
        task = S.StreamTask(spec)
        task.phase.connect(self._on_phase)
        task.jobFinished.connect(self._on_finished)
        self._task = task
        self._set_running(True)
        self._say(tr('Starting...'))
        S.start(task)

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
        self.picker.set_locked(running)
        self.go_btn.setText(tr('Stop') if running else tr('Generate streams'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True if running else self._item is not None)
        if not running:
            self._on_reference_changed()
            self._on_crs_mode()
            self._on_detail_changed()

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
            message = result.get('message') or tr('The streams failed.')
            self.bar.setValue(0)
            self.bar.setFormat('%p%')
            self._say(message, warn=True)
            QgsMessageLog.logMessage(message, LOG_TAG,
                                     Qgis.MessageLevel.Warning)
            QMessageBox.critical(self, tr(TITLE), message)
            # The DEM may have survived (the message says so); a temporary
            # folder has no one to tell, so it goes.
            if self._temp_dir and not os.path.exists(spec.dem_path):
                self._cleanup_temp()

    def _on_done(self, result, spec):
        self.bar.setValue(100)
        self.bar.setFormat('100%')
        project = QgsProject.instance()
        streams = QgsVectorLayer('{0}|layername={1}'.format(
            spec.stream_path, S.STREAM_LAYER), spec.title, 'ogr')
        if not streams.isValid():
            self._warn(tr('The streams were written to {0} but QGIS could '
                          'not open them.').format(spec.stream_path))
            return
        streams.setName(LL.unique_name(project, '{0} streams'.format(
            spec.title)))
        style.apply_streams(streams, self.style_combo.currentData())
        self._tag(streams, spec)

        check = None
        if result.get('check_layer'):
            check = QgsVectorLayer('{0}|layername={1}'.format(
                spec.stream_path, S.CHECK_LAYER), spec.title, 'ogr')
            if check.isValid():
                check.setName(LL.unique_name(project, '{0} stream check'
                                             .format(spec.title)))
                style.apply_check(check)
            else:
                check = None

        dem = None
        if self.add_dem_check.isChecked():
            dem = QgsRasterLayer(spec.dem_path, '{0} DEM'.format(spec.title),
                                 'gdal')
            if dem.isValid():
                dem.setName(LL.unique_name(project, dem.name()))
                contour_style.style_dem(dem, result['low'], result['high'])
                self._tag(dem, spec)
            else:
                dem = None
        accumulation = None
        if result.get('accumulation'):
            accumulation = QgsRasterLayer(
                result['accumulation'], '{0} flow accumulation'.format(
                    spec.title), 'gdal')
            if accumulation.isValid():
                accumulation.setName(LL.unique_name(project,
                                                    accumulation.name()))
            else:
                accumulation = None

        # Rasters go in below the vector layers (but above any basemap raster)
        # so they never hide what the user already has; the streams go on top
        # and the comparison above them.
        root = project.layerTreeRoot()
        index = self._first_raster_index(root)
        for raster, visible in ((accumulation, False), (dem, True)):
            if raster is not None:
                project.addMapLayer(raster, False)
                root.insertLayer(index, raster)
                if not visible:
                    node = root.findLayer(raster.id())
                    if node is not None:
                        node.setItemVisibilityChecked(False)
        project.addMapLayer(streams, False)
        root.insertLayer(0, streams)
        if check is not None:
            project.addMapLayer(check, False)
            root.insertLayer(0, check)

        confidence = result.get('confidence') or {}
        total = max(1e-9, sum(confidence.values()))
        text = tr('Done. {0:,} stream segments, {1:,.1f} km, added to the '
                  'map. Check: {2:.0f}% high confidence, {3:.0f}% low.'
                  ).format(result['segments'], result['length_km'],
                           100.0 * confidence.get(CHECK.CONF_HIGH, 0) / total,
                           100.0 * confidence.get(CHECK.CONF_LOW, 0) / total)
        self._say(text)
        if self._temp_dir:
            self._push(tr('"{0}" was written to a temporary folder, which '
                          'QGIS deletes when it closes - export it to keep '
                          'it.').format(streams.name()), warn=True)
        else:
            self._push(tr('Streams saved to {0}. {1}').format(
                spec.stream_path, '' if spec.local
                else DEM.COPERNICUS_ATTRIBUTION), warn=False)
        if result.get('reference_problem'):
            self._push(tr('The comparison could not be made: {0}').format(
                result['reference_problem']), warn=True)
        self._temp_dir = None
        self._last_report = result.get('report_html') or ''
        self.report_btn.setEnabled(bool(self._last_report))
        self._show_report()

    @staticmethod
    def _first_raster_index(root):
        """Where a DEM goes: above the first raster, else at the bottom."""
        from qgis.core import QgsLayerTreeLayer

        children = root.children()
        for index, node in enumerate(children):
            if isinstance(node, QgsLayerTreeLayer) and node.layer() is not None \
                    and node.layer().type() == Qgis.LayerType.Raster:
                return index
        return len(children)

    @staticmethod
    def _tag(layer, spec):
        if spec.local:
            return
        with suppress(Exception):           # attribution is best effort
            metadata = layer.metadata()
            metadata.setRights([DEM.COPERNICUS_ATTRIBUTION])
            metadata.setLicenses([DEM.COPERNICUS_LICENCE])
            metadata.setTitle(spec.title)
            layer.setMetadata(metadata)

    def _push(self, text, warn):
        if iface is None:
            return
        bar = iface.messageBar()
        (bar.pushWarning if warn else bar.pushSuccess)(tr(TITLE), text)

    def _cleanup_temp(self):
        if self._temp_dir:
            S.remove_folder(self._temp_dir)
            self._temp_dir = None

    # --------------------------------------------------------------- report

    def _show_report(self):
        """Show the last run's report in a window of its own."""
        if not self._last_report:
            return
        if self._report_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle(tr('Stream report'))
            dialog.setModal(False)
            dialog.resize(640, 700)
            layout = QVBoxLayout(dialog)
            self._report_view = QTextBrowser(dialog)
            self._report_view.setOpenExternalLinks(False)
            layout.addWidget(self._report_view)
            close = QPushButton(tr('Close'), dialog)
            close.clicked.connect(dialog.close)
            layout.addWidget(close)
            self._report_dialog = dialog
        self._report_view.setHtml(self._last_report)
        self._report_dialog.show()
        self._report_dialog.raise_()

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
            self, tr(TITLE), tr('Stop generating streams and close?'))
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
        self.picker.teardown()
        if self._report_dialog is not None:
            with suppress(RuntimeError):
                self._report_dialog.close()
        self.deleteLater()

# -*- coding: utf-8 -*-
"""Detect Paddy Field Plots from Imagery: the window.

UI and wiring only. The area comes from ``RoiPicker``, the imagery is any
raster layer in the project, and the job is a ``paddy.task.PaddyTask`` on a
worker thread. The AI runtime (onnxruntime) is the road tool's and installed
the same way; the field model is chosen, downloaded or loaded from a file in
the "Field model" group, which also says who made it.

The window is modeless and lives until closed.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import importlib
import os
from contextlib import suppress

from qgis.core import (Qgis, QgsCoordinateTransform, QgsGeometry,
                       QgsMessageLog, QgsProject, QgsRasterLayer,
                       QgsVectorLayer)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QProcess, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QTextBrowser,
    QVBoxLayout, QWidget)

from .road_detect_dialog import (_drop_layers_from, _FitScroll, _FitTabs,
                                 _wgs84_bounds)
from .window_reveal import revealed_preview
from ..branding import LOG_TAG, open_docs
from ..core import dem_sources as DEM
from ..core import layer_loader as LL
from ..core import net
from ..core.imagery import paths
from ..core.modify_features import layer_filter
from ..core.paddy import models, style
from ..core.paddy import task as T
from ..core.roads import runtime as RT
from ..core.roads import sampler
from .output_fields import FollowingField
from .roi_picker import RoiPicker

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'detect_paddy_plots_imagery'
TITLE = 'Detect Paddy Field Plots from Imagery'

BLUE = '#174592'
ORANGE = '#FC663A'
GREEN = '#2E7D32'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid);
    color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE}

CRS_UTM, CRS_PROJECT = 'utm', 'project'

#: Seconds per million pixels (reading Esri tiles + the model on a laptop
#: CPU), for the "large area" estimate. Measured; see the help page.
SECONDS_PER_MPX = {'da-small': 2.5, 'da-v2': 6.5}


def tr(text):
    return text


# Opens out of the GeoAI toolbar button and shrinks back into it on close.
@revealed_preview
class PaddyDetectDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaPaddyDetectDialog')
        self.resize(600, 600)
        self.setStyleSheet(STYLE)

        self.canvas = iface.mapCanvas() if iface is not None else None
        self._task = None
        self._spec = None
        self._temp_dir = None
        self._closing = False
        self._torn_down = False
        self._report_dialog = None
        self._last_report = ''
        self._lockable = []
        self._process = None
        self._process_kind = None
        self._process_output = []
        self._built = False

        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_height)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._refresh_details)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        for box in (self._build_model(), self._build_imagery()):
            box.setSizePolicy(QSizePolicy.Policy.Preferred,
                              QSizePolicy.Policy.Maximum)
            root.addWidget(box)
        self._runtime = models.status(self._model())

        self.tabs = _FitTabs(self)
        self.picker = RoiPicker(self.canvas, self)
        self.picker.changed.connect(self._schedule)
        self.picker.message.connect(self._say)
        self.area_tab = self._add_tab(tr('Area'), self.picker,
                                      self._build_details())
        self._add_tab(tr('Detection'), self._build_detection())
        self._add_tab(tr('Generalize'), self._build_generalize())
        self._add_tab(tr('Output'), self._build_output())
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)
        root.addLayout(self._build_progress())
        root.addLayout(self._build_buttons())

        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy
                                      .AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)

        self._refresh_runtime()
        self._built = True
        self._on_tab_changed(self.tabs.currentIndex())
        self._refresh_details()
        self._fit_height()
        if self._runtime['runtime'] == 'unprobed':
            QTimer.singleShot(200, self._probe_runtime)

    # ------------------------------------------------------------- building

    def _lock(self, *widgets):
        self._lockable.extend(widgets)

    def _add_tab(self, title, *boxes):
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(6, 6, 6, 6)
        column.setSpacing(8)
        for box in boxes:
            if isinstance(box, QGroupBox) and len(boxes) == 1 \
                    or box is self.picker:
                box.setTitle('')
                box.setFlat(True)
            column.addWidget(box)
        column.addStretch(1)
        scroll = _FitScroll(self.tabs)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        return scroll

    def _on_tab_changed(self, _index):
        self.tabs.updateGeometry()
        self._schedule_fit()

    def _schedule_fit(self, *_args):
        if self._built and not self._torn_down:
            self._fit_timer.start()

    def _fit_height(self):
        """Grow or shrink the window to its content, within the screen."""
        if self._torn_down:
            return
        layout = self.layout()
        layout.invalidate()
        layout.activate()
        wanted = layout.totalSizeHint().height()
        if layout.hasHeightForWidth():
            wanted = max(wanted, layout.totalHeightForWidth(self.width()))
        wanted = max(wanted, layout.totalMinimumSize().height())
        screen = self.screen() if hasattr(self, 'screen') else None
        if screen is not None:
            wanted = min(wanted, int(screen.availableGeometry().height()
                                     * 0.92))
        if wanted != self.height():
            self.resize(self.width(), wanted)

    def _build_model(self):
        box = QGroupBox(tr('Field model'), self)
        column = QVBoxLayout(box)
        pick = QHBoxLayout()
        pick.addWidget(QLabel(tr('Model'), box))
        self.model_combo = QComboBox(box)
        for entry in models.MODELS:
            self.model_combo.addItem('{0} ({1} MB)'.format(
                entry['title'], models.megabytes(entry)), entry['id'])
            self.model_combo.setItemData(
                self.model_combo.count() - 1, entry['about'],
                Qt.ItemDataRole.ToolTipRole)
        self.model_combo.setCurrentIndex(
            self.model_combo.findData(models.DEFAULT_MODEL))
        self.model_combo.setToolTip(tr(
            'Both are Delineate Anything models. The small one runs about 4 '
            'times faster on a normal PC. Neither is best everywhere: in '
            'tests S did better on large paddy blocks, v2 on small plots '
            'among villages - try both on a small area first. Each is '
            'downloaded once, when first chosen.'))
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        pick.addWidget(self.model_combo, 1)
        column.addLayout(pick)
        self.model_status = QLabel('', box)
        self.model_status.setWordWrap(True)
        self.model_status.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self.model_status)
        row = QHBoxLayout()
        self.install_btn = QPushButton(tr('Install AI runtime'), box)
        self.install_btn.setToolTip(tr(
            'Installs onnxruntime (about 40 MB, from pypi.org) into a folder '
            'of its own in your QGIS profile. Nothing QGIS already has is '
            'changed. The road tool uses the same one.'))
        self.install_btn.clicked.connect(self._install_runtime)
        self.model_btn = QPushButton(tr('Download model'), box)
        self.model_btn.setToolTip(tr(
            'Downloads the chosen model from the KGA Toolbox releases on '
            'github.com into your QGIS profile. Checked byte for byte after '
            'download.'))
        self.model_btn.clicked.connect(self._download_model)
        self.file_btn = QPushButton(tr('Use a model file...'), box)
        self.file_btn.setToolTip(tr(
            'Already have the .onnx file (a shared drive, a USB stick, a PC '
            'without internet)? Pick it; it is checked and copied into your '
            'QGIS profile.'))
        self.file_btn.clicked.connect(self._model_from_file)
        self.copy_btn = QPushButton(tr('Copy install command'), box)
        self.copy_btn.setToolTip(tr(
            'If the automatic install fails (a proxy, a locked-down PC), run '
            'this command yourself in the OSGeo4W Shell or a terminal, then '
            'reopen this window.'))
        self.copy_btn.clicked.connect(self._copy_command)
        for button in (self.install_btn, self.model_btn, self.file_btn,
                       self.copy_btn):
            row.addWidget(button)
        row.addStretch(1)
        column.addLayout(row)
        credit = QLabel(
            '<small>Model: Delineate Anything by Lavreniuk et al., AGPL-3.0 - '
            '<a href="{0}">model page</a></small>'.format(
                models.MODELS[0]['page']), box)
        credit.setToolTip(models.MODELS[0]['credit'])
        credit.setWordWrap(True)
        credit.setOpenExternalLinks(True)
        credit.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(credit)
        self._lock(self.model_combo)
        return box

    def _build_imagery(self):
        box = QGroupBox(tr('Imagery'), self)
        form = QFormLayout(box)
        self.layer_combo = QgsMapLayerComboBox(box)
        self.layer_combo.setFilters(layer_filter('RasterLayer'))
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setToolTip(tr(
            'Any raster layer: an XYZ, WMS, WMTS or ArcGIS tile service, a '
            'satellite GeoTIFF, or a drone orthomosaic. It is read as it '
            'shows on the map (its colours and band choice), at the '
            'resolution set on the Detection tab.'))
        self.layer_combo.layerChanged.connect(self._schedule)
        form.addRow(tr('Imagery layer'), self.layer_combo)
        self.imagery_note = QLabel('', box)
        self.imagery_note.setWordWrap(True)
        self.imagery_note.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(self.imagery_note)
        self._pick_default_layer()
        self._lock(self.layer_combo)
        return box

    def _pick_default_layer(self):
        if self.canvas is None:
            return
        for layer in self.canvas.layers():
            if layer.type() == Qgis.LayerType.Raster \
                    and not sampler.looks_like_overlay(layer):
                self.layer_combo.setLayer(layer)
                return

    def _build_detection(self):
        box = QGroupBox(tr('Detection'), self)
        form = QFormLayout(box)
        self.gsd_combo = QComboBox(box)
        for value, label in T.RESOLUTIONS:
            self.gsd_combo.addItem(tr(label), value)
        self.gsd_combo.setCurrentIndex(
            self.gsd_combo.findData(T.DEFAULT_RESOLUTION))
        self.gsd_combo.setToolTip(tr(
            'The imagery is read at this many metres per pixel. The model '
            'works from 0.25 to 10 m; finer finds smaller plots and takes '
            'four times as long per step. 1 m suits most paddies.'))
        self.gsd_combo.currentIndexChanged.connect(self._schedule)
        form.addRow(tr('Resolution'), self.gsd_combo)
        self.sensitivity_combo = QComboBox(box)
        for ident, label, _value in T.SENSITIVITY:
            self.sensitivity_combo.addItem(tr(label), ident)
        self.sensitivity_combo.setCurrentIndex(
            self.sensitivity_combo.findData(T.DEFAULT_SENSITIVITY))
        self.sensitivity_combo.setToolTip(tr(
            'How sure the model must be of a field to keep it. More sensitive '
            'keeps plots with faint bunds, and more false ones.'))
        form.addRow(tr('Sensitivity'), self.sensitivity_combo)
        self.farm_check = QCheckBox(tr('Keep only plots on farmland (ESA '
                                       'WorldCover, needs internet)'), box)
        self.farm_check.setChecked(True)
        self.farm_check.setToolTip(tr(
            'Drops plots with less than half their area on cropland, '
            'grassland or herbaceous wetland in ESA WorldCover 2021 (10 m): '
            'mostly ponds, village yards, orchards and woodland. Without '
            'internet every plot is kept and the report says so.'))
        form.addRow('', self.farm_check)
        self.giant_check = QCheckBox(tr('Drop giant plots the model is '
                                        'unsure of'), box)
        self.giant_check.setChecked(True)
        self.giant_check.setToolTip(tr(
            'The model sometimes boxes a whole block of plots, or a pond and '
            'the plots around it, as one. A plot over 5 times the typical '
            'size is dropped when the model scored it low or more confident '
            'plots overlap most of it. Real large fields are kept.'))
        form.addRow('', self.giant_check)
        self.min_area_spin = self._spin(box, 0, 100000, 100, ' m²', tr(
            'Plots smaller than this are dropped: mostly gardens, ponds and '
            'pieces of larger plots.'))
        form.addRow(tr('Drop plots smaller than'), self.min_area_spin)
        self.close_spin = QDoubleSpinBox(box)
        self.close_spin.setRange(0.0, 10.0)
        self.close_spin.setDecimals(1)
        self.close_spin.setSingleStep(0.5)
        self.close_spin.setValue(2.0)
        self.close_spin.setSuffix(' m')
        self.close_spin.setToolTip(tr(
            'The model leaves the bunds between plots empty. Gaps up to this '
            'wide are shared out to the plots either side, so neighbours meet '
            'along the middle of the bund and share one boundary line. 0 '
            'keeps the model\'s own outlines.'))
        form.addRow(tr('Close gaps between plots up to'), self.close_spin)
        self._lock(self.gsd_combo, self.sensitivity_combo, self.farm_check,
                   self.giant_check, self.min_area_spin, self.close_spin)
        return box

    def _build_generalize(self):
        box = QGroupBox(tr('Generalize'), self)
        form = QFormLayout(box)
        self.simplify_combo = QComboBox(box)
        for ident, label, _value in T.SIMPLIFY:
            self.simplify_combo.addItem(tr(label), ident)
        self.simplify_combo.setCurrentIndex(
            self.simplify_combo.findData(T.DEFAULT_SIMPLIFY))
        self.simplify_combo.setToolTip(tr(
            'Douglas-Peucker simplification of the plots and boundaries: a '
            'vertex is dropped when the outline stays within this distance '
            'of where it was. Larger values give straighter bunds and fewer '
            'vertices.'))
        self.simplify_combo.currentIndexChanged.connect(
            self._on_simplify_changed)
        form.addRow(tr('Simplify'), self.simplify_combo)
        self.simplify_spin = QDoubleSpinBox(box)
        self.simplify_spin.setRange(0.1, 20.0)
        self.simplify_spin.setDecimals(1)
        self.simplify_spin.setSingleStep(0.5)
        self.simplify_spin.setValue(1.0)
        self.simplify_spin.setSuffix(' m')
        self.simplify_spin.setToolTip(tr('Simplification tolerance in '
                                         'metres.'))
        form.addRow(tr('Tolerance'), self.simplify_spin)
        self.hole_spin = self._spin(box, 0, 10000, 50, ' m²', tr(
            'Holes inside a plot smaller than this are filled: a tree, a hut '
            'or a puddle the model left out.'))
        form.addRow(tr('Fill holes smaller than'), self.hole_spin)
        self.smooth_check = QCheckBox(tr('Smooth the outlines'), box)
        self.smooth_check.setChecked(True)
        self.smooth_check.setToolTip(tr(
            'Rounds off the pixel steps before simplifying, so corners stay '
            'sharp and straight bunds come out straight.'))
        form.addRow('', self.smooth_check)
        self._form_generalize = form
        self._lock(self.simplify_combo, self.simplify_spin, self.hole_spin,
                   self.smooth_check)
        self._on_simplify_changed()
        return box

    def _on_simplify_changed(self, *_args):
        self._row_visible(self._form_generalize, self.simplify_spin,
                          self.simplify_combo.currentData() == 'custom')
        self._schedule_fit()

    def _simplify_m(self):
        value = T.preset(T.SIMPLIFY, self.simplify_combo.currentData(), None)
        return value if value is not None else float(
            self.simplify_spin.value())

    @staticmethod
    def _spin(parent, low, high, value, suffix, tip):
        spin = QDoubleSpinBox(parent)
        spin.setRange(low, high)
        spin.setDecimals(0)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setToolTip(tip)
        return spin

    def _build_output(self):
        box = QGroupBox(tr('Output'), self)
        form = QFormLayout(box)
        self.plots_check = QCheckBox(tr('Paddy plots (polygons)'), box)
        self.bounds_check = QCheckBox(tr('Plot boundaries (bund lines)'),
                                      box)
        for check in (self.plots_check, self.bounds_check):
            check.setChecked(True)
            form.addRow('', check)
        self.image_check = QCheckBox(tr('Also save the imagery that was read '
                                        '(GeoTIFF)'), box)
        self.image_check.setToolTip(tr(
            'The exact image the model saw, so the plots can be checked '
            'against it later.'))
        form.addRow('', self.image_check)
        self.crs_combo = QComboBox(box)
        self.crs_combo.addItem(tr('UTM zone of the area (metres)'), CRS_UTM)
        self.crs_combo.addItem(tr('Current project CRS'), CRS_PROJECT)
        form.addRow(tr('Output CRS'), self.crs_combo)
        self.folder_widget = QgsFileWidget(box)
        self.folder_widget.setStorageMode(
            QgsFileWidget.StorageMode.GetDirectory)
        line = self.folder_widget.lineEdit()
        if line is not None:
            line.setPlaceholderText(tr('[Save to a temporary folder]'))
        self.folder_widget.setToolTip(tr(
            'Where <name>_paddy.gpkg and the report are written. Leave it '
            'empty for a temporary folder, which QGIS deletes when it '
            'closes.'))
        self.folder_widget.fileChanged.connect(self._schedule)
        form.addRow(tr('Output folder'), self.folder_widget)
        FollowingField(self.folder_widget).suggest_folder()
        self.name_edit = QLineEdit('Paddy plots', box)
        self.name_edit.setToolTip(tr(
            'Base name: <name>_paddy.gpkg and <name>_paddy_report.txt; also '
            'the name of the layer group.'))
        form.addRow(tr('Name'), self.name_edit)
        self._lock(self.plots_check, self.bounds_check, self.image_check,
                   self.crs_combo, self.folder_widget, self.name_edit)
        return box

    def _build_details(self):
        box = QGroupBox(tr('Details'), self)
        column = QVBoxLayout(box)
        self.details = QLabel('', box)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop
                                  | Qt.AlignmentFlag.AlignLeft)
        self.details.setMinimumHeight(70)
        column.addWidget(self.details)
        return box

    def _build_progress(self):
        box = QVBoxLayout()
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
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
        self.go_btn = QPushButton(tr('Detect plots'), self)
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

    # --------------------------------------------------------------- runtime

    def _model(self):
        return models.model(self.model_combo.currentData())

    def _on_model_changed(self, *_args):
        self._runtime = models.status(self._model())
        self._refresh_runtime()
        self._schedule()

    def _refresh_runtime(self):
        state = self._runtime
        entry = self._model()
        ready = state['runtime'] == 'ready'
        lines = []
        if ready:
            lines.append('<span style="color:{0}">✔ AI runtime '
                         '(onnxruntime {1}) ready.</span>'.format(
                             GREEN, state['version'] or ''))
        elif state['runtime'] == 'unprobed':
            lines.append(tr('Checking the AI runtime (onnxruntime {0})...')
                         .format(state['version'] or ''))
        elif state['runtime'] == 'broken':
            lines.append('<span style="color:{0}">✖ onnxruntime {1} is '
                         'installed but does not load in this QGIS. Click '
                         '<b>Install AI runtime</b> to put a version that '
                         'does into the plugin\'s own folder.</span>'.format(
                             ORANGE, state['version'] or ''))
        else:
            lines.append(tr('The AI runtime (onnxruntime) is not installed. '
                            'Click <b>Install AI runtime</b> - once, about '
                            '40 MB.'))
        if state['model']:
            lines.append('<span style="color:{0}">✔ {1} '
                         'downloaded.</span>'.format(GREEN, entry['short']))
        else:
            lines.append(tr('{0} is not downloaded yet. Click <b>Download '
                            'model</b> - once, about {1} MB.').format(
                entry['short'], models.megabytes(entry)))
        self.model_status.setText('<br>'.join(lines))
        busy = self._process is not None or self._running()
        self.install_btn.setVisible(not ready)
        self.copy_btn.setVisible(not ready)
        self.install_btn.setEnabled(not busy)
        self.model_btn.setVisible(not state['model'])
        self.file_btn.setVisible(not state['model'])
        self.model_btn.setEnabled(not busy)
        self.file_btn.setEnabled(not busy)
        self._update_go_enabled()
        self._schedule_fit()

    def _model_ready(self):
        return self._runtime['runtime'] == 'ready' and self._runtime['model']

    def _install_runtime(self):
        exe = RT.python_exe()
        if exe is None:
            self._warn(tr('The Python that QGIS runs on could not be found, '
                          'so onnxruntime cannot be installed from here. '
                          'Click <b>Copy install command</b> and run it '
                          'yourself.'))
            return
        os.makedirs(RT.libs_dir(), exist_ok=True)
        self._start_process('install', exe, RT.pip_arguments())
        self._say(tr('Installing onnxruntime from pypi.org...'))

    def _probe_runtime(self):
        state = models.status(self._model())
        self._runtime = state
        if state['runtime'] != 'unprobed':
            self._refresh_runtime()
            return
        exe = RT.python_exe()
        if exe is None:
            ok = os.path.dirname(state['folder']) == RT.libs_dir()
            RT.remember_probe(state['folder'], state['version'], ok)
            self._runtime = models.status(self._model())
            self._refresh_runtime()
            return
        self._start_process('probe', exe, RT.probe_arguments(
            state['folder']))

    def _start_process(self, kind, program, arguments):
        process = QProcess(self)
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._on_process_output)
        process.finished.connect(self._on_process_finished)
        self._process = process
        self._process_kind = kind
        self._process_output = []
        self._refresh_runtime()
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        process.start(program, arguments)

    def _on_process_output(self):
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode(
            'utf-8', 'replace')
        self._process_output.append(text)
        last = [line for line in text.splitlines() if line.strip()]
        if last and self._process_kind == 'install':
            self._say(last[-1][:160])

    def _on_process_finished(self, code, _status=None):
        QApplication.restoreOverrideCursor()
        kind, process = self._process_kind, self._process
        self._process, self._process_kind = None, None
        output = ''.join(self._process_output)
        if process is not None:
            process.deleteLater()
        if self._torn_down:
            return
        if kind == 'install':
            if code != 0:
                QgsMessageLog.logMessage(output, LOG_TAG,
                                         Qgis.MessageLevel.Warning)
                self._say(tr('The install failed.'), warn=True)
                self._warn(tr('onnxruntime could not be installed (pip exit '
                              'code {0}). The output is in the KGA Toolbox '
                              'log. You can click <b>Copy install command</b> '
                              'and run it in the OSGeo4W Shell instead.')
                           .format(code))
                self._refresh_runtime()
                return
            self._say(tr('Installed. Checking it loads...'))
            importlib.invalidate_caches()
            self._probe_runtime()
            return
        state = RT.status()
        ok = code == 0 and 'KGA-ORT-OK' in output
        if state['folder']:
            RT.remember_probe(state['folder'], state['version'], ok)
        if not ok:
            QgsMessageLog.logMessage(
                'onnxruntime probe failed (exit {0}): {1}'.format(
                    code, output), LOG_TAG, Qgis.MessageLevel.Warning)
        self._runtime = models.status(self._model())
        self._refresh_runtime()
        self._say(tr('Ready.') if ok else tr(
            'onnxruntime does not load in this QGIS.'), warn=not ok)

    def _download_model(self):
        entry = self._model()
        path = models.model_path(entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.model_btn.setEnabled(False)
        self.file_btn.setEnabled(False)
        self.go_btn.setEnabled(False)
        self.bar.setValue(0)

        def progress(received, total):
            total = total if total and total > 0 else entry['bytes']
            self.bar.setValue(int(100.0 * received / max(1, total)))
            self.bar.setFormat(tr('Downloading the model   %p%'))
            return not self._torn_down

        self._say(tr('Downloading {0} from github.com...').format(
            entry['short']))
        try:
            arrived = net.download_to(entry['url'], path, progress, self)
            if arrived:
                self._say(tr('Checking the model file...'))
                QApplication.processEvents()
                models.check_file(path, entry)
        except (net.NetError, RT.RuntimeProblem) as exc:
            with suppress(OSError):
                if os.path.exists(path):
                    os.remove(path)
            # All HTML: Qt picks rich text only from a tag in the first line,
            # and the reason (plain text) comes first.
            from html import escape
            self._warn(tr('<p>{0} could not be downloaded: {1}</p><p>If this '
                          'PC cannot reach github.com, get the file <b>{2}'
                          '</b> another way and click <b>Use a model '
                          'file...</b></p>').format(
                entry['short'], escape(str(exc)), entry['file']))
            arrived = False
        self.bar.setValue(0)
        self.bar.setFormat('%p%')
        if self._torn_down:
            return
        self._runtime = models.status(entry)
        self._refresh_runtime()
        self._say(tr('Model ready.') if arrived else tr('Ready.'))

    def _model_from_file(self):
        entry = self._model()
        source, _filter = QFileDialog.getOpenFileName(
            self, tr('Choose {0}').format(entry['file']), '',
            tr('ONNX model (*.onnx)'))
        if not source:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            models.import_file(source, entry)
            ok = True
        except (RT.RuntimeProblem, OSError) as exc:
            ok = False
            problem = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            self._warn(tr('That file is not {0}: {1}').format(
                entry['short'], problem))
            return
        self._runtime = models.status(entry)
        self._refresh_runtime()
        self._say(tr('Model ready.'))

    def _copy_command(self):
        QApplication.clipboard().setText(RT.install_command())
        self._say(tr('Install command copied. Paste it into the OSGeo4W Shell '
                     '(or a terminal), run it, then reopen this window.'))

    # ---------------------------------------------------------- small state

    def _running(self):
        return self._task is not None

    def _schedule(self, *_args):
        if not self._torn_down:
            self._debounce.start()

    def _row_visible(self, form, widget, visible):
        label = form.labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    def _area(self):
        """(geometry, crs, clip, label). Raises DemError when unset."""
        region = self.picker.region()
        if region is None:
            raise DEM.DemError(tr('Choose an area.'))
        return region.geometry, region.crs, region.clip, region.label

    def _gsd(self):
        return float(self.gsd_combo.currentData() or T.DEFAULT_RESOLUTION)

    def _seconds_per_mpx(self):
        return SECONDS_PER_MPX.get(self._model()['id'], 5.0)

    def _refresh_details(self):
        if self._torn_down:
            return
        gsd = self._gsd()
        layer = self.layer_combo.currentLayer()
        notes = []
        if layer is None:
            self.imagery_note.setText(tr('Choose the imagery to read.'))
        else:
            text = []
            if sampler.is_local(layer):
                size = sampler.native_resolution_m(layer, None)
                if size is not None:
                    text.append(tr('A local raster at about {0:.2f} m per '
                                   'pixel.').format(size))
                    if size > gsd * 1.5:
                        text.append(tr('<span style="color:{0}">That is '
                                       'coarser than the {1:g} m chosen: it '
                                       'is enlarged, and small plots may '
                                       'merge.</span>').format(ORANGE, gsd))
            else:
                text.append(tr('A tile service; a coarser zoom is used where '
                               'the full one has no imagery.'))
            if sampler.looks_like_overlay(layer):
                text.append(tr('<span style="color:{0}">Its name suggests '
                               'labels or roads drawn on it (hybrid, map, '
                               'OSM...). Choose plain imagery.</span>').format(
                                   ORANGE))
            self.imagery_note.setText(' '.join(text))
        try:
            geometry, crs, clip, label = self._area()
            utm = sampler.utm_for(geometry, crs)
            grid = sampler.Grid.around(geometry, crs, utm, gsd, T.MARGIN_M)
            transform = QgsCoordinateTransform(crs, utm,
                                               QgsProject.instance())
            shape = QgsGeometry(geometry)
            shape.transform(transform)
            hectares = (shape.area() if clip else
                        shape.boundingBox().area()) / 1e4
            notes.append(tr('<b>Area:</b> {0}, {1:,.1f} ha ({2:,.1f} '
                            'million pixels at {3:g} m), in {4}.').format(
                label, hectares, grid.pixels / 1e6, gsd, utm.authid()))
            warning = T.size_warning(grid.pixels, self._seconds_per_mpx())
            if warning is not None:
                notes.append('<span style="color:{0}">{1}</span>'.format(
                    ORANGE, warning[1]))
        except DEM.DemError as exc:
            notes.append(str(exc))
        except Exception as exc:                # a bad geometry, a bad CRS
            QgsMessageLog.logMessage(
                'Detect Paddy Plots: the area could not be read: {0}'.format(
                    exc), LOG_TAG, Qgis.MessageLevel.Info)
            notes.append(tr(
                'The area could not be read. If the map is empty, add the '
                'imagery layer and zoom to the area first; otherwise choose '
                'another area or layer.'))
        if not self._model_ready():
            notes.append(tr('Install the AI runtime and download the model '
                            'first (above).'))
        notes.append(tr('Detecting takes about {0:g} s per million pixels '
                        'with this model, plus a few seconds for the '
                        'farmland check.').format(self._seconds_per_mpx()))
        notes.append(tr('Results are a draft from imagery: check them before '
                        'use, and check the imagery provider\'s terms before '
                        'you share plots traced from it.'))
        self.details.setText('<br>'.join(notes))
        self._update_go_enabled()
        self._schedule_fit()

    def _update_go_enabled(self):
        if not self._running():
            self.go_btn.setEnabled(self._model_ready()
                                   and self._process is None)

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

    def _start(self):
        self.picker.forget_cached_boundary()
        if not self._model_ready():
            self._warn(tr('Install the AI runtime and download the model '
                          'first.'))
            return
        layer = self.layer_combo.currentLayer()
        try:
            clone = sampler.prepare_layer(layer)
            geometry, crs, clip, label = self._area()
        except sampler.SamplerError as exc:
            self._warn(str(exc))
            return
        except DEM.DemError as exc:
            self.tabs.setCurrentWidget(self.area_tab)
            self._warn(str(exc))
            return
        if not (self.plots_check.isChecked()
                or self.bounds_check.isChecked()):
            self.tabs.setCurrentIndex(self.tabs.count() - 1)
            self._warn(tr('Tick at least one of plots or boundaries.'))
            return
        if sampler.looks_like_overlay(layer):
            answer = QMessageBox.question(
                self, tr(TITLE), tr(
                    '"{0}" looks like a map or a hybrid layer with roads or '
                    'labels drawn on it. Use it anyway?').format(layer.name()))
            if answer != QMessageBox.StandardButton.Yes:
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

        gsd = self._gsd()
        try:
            work_crs = sampler.utm_for(geometry, crs)
            grid = sampler.Grid.around(geometry, crs, work_crs, gsd,
                                       T.MARGIN_M)
        except Exception as exc:                # an empty map, a bad CRS
            QgsMessageLog.logMessage(
                'Detect Paddy Plots: the area could not be read: {0}'.format(
                    exc), LOG_TAG, Qgis.MessageLevel.Info)
            self.tabs.setCurrentWidget(self.area_tab)
            self._warn(tr('The area could not be read. If the map is empty, '
                          'add the imagery layer and zoom to the area first; '
                          'otherwise choose another area or layer.'))
            return
        if grid.pixels > T.MAX_PIXELS:
            self._warn(tr('That area is {0:,} million pixels at {1:g} m, more '
                          'than this tool reads in one go. Choose a smaller '
                          'area or a coarser resolution.').format(
                grid.pixels // 1000000, gsd))
            return
        warning = T.size_warning(grid.pixels, self._seconds_per_mpx())
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
                folder = T.temp_folder()
                self._temp_dir = folder
            else:
                os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._warn(tr('The output folder could not be created: {0}')
                       .format(exc))
            return
        gpkg = os.path.join(folder, '{0}_paddy.gpkg'.format(name))
        image = os.path.join(folder, '{0}_imagery.tif'.format(name)) \
            if self.image_check.isChecked() else None
        report = os.path.join(folder, '{0}_paddy_report.txt'.format(name))
        if not temporary:
            existing = [os.path.basename(p) for p in (gpkg, image)
                        if p and os.path.exists(p)]
            if existing:
                answer = QMessageBox.question(
                    self, tr('Overwrite?'),
                    tr('{0} already exists. Replace it?').format(
                        ' and '.join(existing)))
                if answer != QMessageBox.StandardButton.Yes:
                    return
                _drop_layers_from(gpkg)

        entry = self._model()
        out_crs = work_crs if self.crs_combo.currentData() == CRS_UTM else \
            QgsProject.instance().crs()
        spec = T.PaddySpec(
            layer=clone, layer_name=layer.name(),
            local=sampler.is_local(layer), area=QgsGeometry(geometry),
            area_crs=crs, clip=clip, area_label=label,
            bounds=_wgs84_bounds(geometry, crs),
            work_crs=work_crs, out_crs=out_crs,
            model=entry, model_path=models.model_path(entry), gsd=gsd,
            confidence=T.preset(T.SENSITIVITY,
                                self.sensitivity_combo.currentData(), 0.2),
            min_area_m2=self.min_area_spin.value(),
            hole_m2=self.hole_spin.value(),
            close_m=self.close_spin.value(),
            farmland_only=self.farm_check.isChecked(),
            drop_giants=self.giant_check.isChecked(),
            smooth=self.smooth_check.isChecked(),
            simplify_m=self._simplify_m(),
            want_plots=self.plots_check.isChecked(),
            want_bounds=self.bounds_check.isChecked(),
            gpkg_path=gpkg, image_path=image, report_path=report, title=name)
        self._spec = spec
        task = T.PaddyTask(spec)
        task.phase.connect(self._on_phase)
        task.jobFinished.connect(self._on_finished)
        self._task = task
        self._set_running(True)
        self._say(tr('Starting...'))
        T.start(task)

    def _set_running(self, running):
        for widget in self._lockable:
            widget.setEnabled(not running)
        self.picker.set_locked(running)
        self.go_btn.setText(tr('Stop') if running else tr('Detect plots'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True if running else self._model_ready())
        self.install_btn.setEnabled(not running)
        self.model_btn.setEnabled(not running)
        self.file_btn.setEnabled(not running)

    def _on_phase(self, text, fraction):
        self.bar.setValue(int(round(100.0 * fraction)))
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
        self.bar.setFormat('%p%')
        if state == 'done':
            self._on_done(result, spec)
        elif state == 'stopped':
            self.bar.setValue(0)
            self._cleanup_temp()
            self._say(tr('Stopped. Nothing was added to the map.'))
        else:
            message = result.get('message') or tr('The detection failed.')
            self.bar.setValue(0)
            self._say(message, warn=True)
            QgsMessageLog.logMessage(message, LOG_TAG,
                                     Qgis.MessageLevel.Warning)
            QMessageBox.critical(self, tr(TITLE), message)
            self._cleanup_temp()

    def _on_done(self, result, spec):
        self.bar.setValue(100)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.insertGroup(0, LL.unique_name(project, spec.title))
        added = []
        for name, wanted, painter in (
                (T.LAYER_BOUNDS, spec.want_bounds, style.apply_boundaries),
                (T.LAYER_PLOTS, spec.want_plots, style.apply_plots)):
            if not wanted:
                continue
            layer = QgsVectorLayer('{0}|layername={1}'.format(
                spec.gpkg_path, name), name, 'ogr')
            if not layer.isValid():
                continue
            with suppress(Exception):           # styling is best effort
                painter(layer)
            _tag(layer, spec)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            added.append(layer)
        if result.get('image_path'):
            raster = QgsRasterLayer(result['image_path'], 'imagery read',
                                    'gdal')
            if raster.isValid():
                project.addMapLayer(raster, False)
                node = group.addLayer(raster)
                node.setItemVisibilityChecked(False)
        if not added:
            self._warn(tr('The plots were written to {0} but QGIS could not '
                          'open them.').format(spec.gpkg_path))
            return

        text = tr('Done. {0:,} plots, {1:,.2f} ha.').format(
            result['plots'], result['hectares'])
        if result.get('scale', 1) > 1:
            text += tr(' The tile service had no imagery at full zoom here; a '
                       'coarser zoom was used (see the report).')
        self._say(text)
        if self._temp_dir:
            self._push(tr('The plots were written to a temporary folder, '
                          'which QGIS deletes when it closes - export them to '
                          'keep them.'), warn=True)
        else:
            self._push(tr('Plots saved to {0}.').format(spec.gpkg_path),
                       warn=False)
        if result.get('farmland_problem'):
            self._push(result['farmland_problem'], warn=True)
        self._temp_dir = None
        self._last_report = result.get('report_html') or ''
        self.report_btn.setEnabled(bool(self._last_report))
        self._show_report()

    def _push(self, text, warn):
        if iface is None:
            return
        bar = iface.messageBar()
        (bar.pushWarning if warn else bar.pushSuccess)(tr(TITLE), text)

    def _cleanup_temp(self):
        if self._temp_dir:
            import shutil
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    # --------------------------------------------------------------- report

    def _show_report(self):
        if not self._last_report:
            return
        if self._report_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle(tr('Paddy plot report'))
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
            event.ignore()
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
            self, tr(TITLE), tr('Stop detecting plots and close?'))
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
        if self._process is not None:
            with suppress(Exception):
                self._process.kill()
            QApplication.restoreOverrideCursor()
            self._process = None
        self.picker.teardown()
        if self._report_dialog is not None:
            with suppress(RuntimeError):
                self._report_dialog.close()


def _tag(layer, spec):
    with suppress(Exception):
        metadata = layer.metadata()
        metadata.setTitle('{0} - {1}'.format(spec.title, layer.name()))
        rights = [spec.model['credit'], 'Imagery: {0}'.format(
            spec.layer_name)]
        if spec.farmland_only:
            rights.append(T.WORLDCOVER_CREDIT)
        metadata.setRights(rights)
        metadata.setLicenses([
            'Model: {0}. Check the imagery provider\'s terms for derived '
            'data.'.format(spec.model['licence'])])
        layer.setMetadata(metadata)

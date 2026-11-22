# -*- coding: utf-8 -*-
"""Detect Roads from Imagery: the window.

UI and wiring only. The area comes from ``RoiPicker``, the imagery is any
raster layer in the project, and the job is a ``roads.task.RoadTask`` on a
worker thread. The model runtime (onnxruntime) and the model file are fetched
on request from the "Road model" group, which also says who made the model.

The window is modeless and lives until closed.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import importlib
import os
from contextlib import suppress

from qgis.core import (Qgis, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsGeometry, QgsMessageLog,
                       QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QProcess, QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget)

from .window_reveal import revealed_preview
from ..branding import LOG_TAG, open_docs
from ..core import dem_sources as DEM
from ..core import layer_loader as LL
from ..core import net
from ..core import stream_check as CHECK
from ..core.imagery import paths
from ..core.modify_features import layer_filter
from ..core.roads import runtime, sampler, style
from ..core.roads import task as T
from .output_fields import FollowingField
from .roi_picker import RoiPicker

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'detect_roads_imagery'
TITLE = 'Detect Roads from Imagery'

BLUE = '#174592'
ORANGE = '#FC663A'
GREEN = '#2E7D32'

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

REFERENCE_NONE, REFERENCE_OSM, REFERENCE_LAYER = 'none', 'osm', 'layer'
REFERENCES = (
    (REFERENCE_NONE, 'None'),
    (REFERENCE_OSM, 'OpenStreetMap roads (needs internet)'),
    (REFERENCE_LAYER, 'A line layer of my own'),
)

CRS_UTM, CRS_PROJECT = 'utm', 'project'


def tr(text):
    return text


# Opens out of the GeoAI toolbar button and shrinks back into it on close.
# Preview mode: the height fits itself to the open tab, so the window is laid
# out (shown invisibly) before the animation photographs it.
@revealed_preview
class RoadDetectDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaRoadDetectDialog')
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
        self._runtime = runtime.status()
        self._built = False

        # The window's height follows the open tab and whatever rows it shows;
        # one queued fit per burst of changes.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_height)

        # Made before any widget: choosing the default imagery layer and the
        # suggested output folder both fire change signals while the window
        # is still being built, and those land in _schedule.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._refresh_details)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        # The model and the imagery stay in view above the tabs: they are
        # what every run starts from. The settings live in tabs below.
        for box in (self._build_model(), self._build_imagery()):
            # As tall as their text and no taller: spare height goes to the
            # tabs.
            box.setSizePolicy(QSizePolicy.Policy.Preferred,
                              QSizePolicy.Policy.Maximum)
            root.addWidget(box)

        self.tabs = _FitTabs(self)
        self.picker = RoiPicker(self.canvas, self)
        self.picker.changed.connect(self._schedule)
        self.picker.message.connect(self._say)
        self.area_tab = self._add_tab(tr('Area'), self.picker,
                                      self._build_details())
        self._add_tab(tr('Detection'), self._build_detection())
        self._add_tab(tr('Generalize'), self._build_generalize())
        self._add_tab(tr('Check'), self._build_check())
        self._add_tab(tr('Output'), self._build_output())
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)
        root.addLayout(self._build_progress())
        root.addLayout(self._build_buttons())

        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)

        self._on_reference_changed()
        self._refresh_runtime()
        self._built = True
        self._on_tab_changed(self.tabs.currentIndex())
        # Now rather than on the next event-loop turn: the opening animation
        # photographs the window as soon as it is shown, and should show it
        # with its details and its final height.
        self._refresh_details()
        self._fit_height()
        if self._runtime['runtime'] == 'unprobed':
            QTimer.singleShot(200, self._probe_runtime)

    # ------------------------------------------------------------- building

    def _lock(self, *widgets):
        self._lockable.extend(widgets)

    def _add_tab(self, title, *boxes):
        """A tab holding `boxes`, scrolling when the window is short.

        The group boxes lose their own titles - the tab says it - and their
        frames, so a tab does not look like a box in a box.
        """
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

    # ------------------------------------------------------- dynamic height

    def _on_tab_changed(self, _index):
        """The tab area takes the open tab's height (see _FitTabs)."""
        self.tabs.updateGeometry()
        self._schedule_fit()

    def _schedule_fit(self, *_args):
        if self._built and not self._torn_down:
            self._fit_timer.start()

    def _fit_height(self):
        """Grow or shrink the window to its content, within the screen.

        The width is the user's; only the height follows. A page taller than
        the screen allows scrolls inside its tab instead.
        """
        if self._torn_down:
            return
        layout = self.layout()
        layout.invalidate()
        layout.activate()
        wanted = layout.totalSizeHint().height()
        if layout.hasHeightForWidth():
            # Wrapped notes in the groups above the tabs need more height
            # when the window is narrow.
            wanted = max(wanted, layout.totalHeightForWidth(self.width()))
        wanted = max(wanted, layout.totalMinimumSize().height())
        screen = self.screen() if hasattr(self, 'screen') else None
        if screen is not None:
            wanted = min(wanted, int(screen.availableGeometry().height()
                                     * 0.92))
        if wanted != self.height():
            self.resize(self.width(), wanted)

    def _build_model(self):
        box = QGroupBox(tr('Road model'), self)
        column = QVBoxLayout(box)
        self.model_status = QLabel('', box)
        self.model_status.setWordWrap(True)
        self.model_status.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self.model_status)
        row = QHBoxLayout()
        self.install_btn = QPushButton(tr('Install AI runtime'), box)
        self.install_btn.setToolTip(tr(
            'Installs onnxruntime (about 40 MB, from pypi.org) into a folder '
            'of its own in your QGIS profile. Nothing QGIS already has is '
            'changed.'))
        self.install_btn.clicked.connect(self._install_runtime)
        self.model_btn = QPushButton(tr('Download model'), box)
        self.model_btn.setToolTip(tr(
            'Downloads the road model (about 98 MB, from huggingface.co) into '
            'your QGIS profile. Checked byte for byte after download.'))
        self.model_btn.clicked.connect(self._download_model)
        self.copy_btn = QPushButton(tr('Copy install command'), box)
        self.copy_btn.setToolTip(tr(
            'If the automatic install fails (a proxy, a locked-down PC), run '
            'this command yourself in the OSGeo4W Shell or a terminal, then '
            'reopen this window.'))
        self.copy_btn.clicked.connect(self._copy_command)
        row.addWidget(self.install_btn)
        row.addWidget(self.model_btn)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        column.addLayout(row)
        # One line here; the whole credit is in the tooltip, the help page,
        # the report and every layer's metadata.
        credit = QLabel(
            '<small>Model: "unet-roads" by Rafael990 (fAIr), CC BY-SA 4.0 - '
            '<a href="{0}">model page</a></small>'.format(
                runtime.MODEL['page']), box)
        credit.setToolTip(runtime.MODEL['credit'])
        credit.setWordWrap(True)
        credit.setOpenExternalLinks(True)
        credit.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(credit)
        return box

    def _build_imagery(self):
        box = QGroupBox(tr('Imagery'), self)
        form = QFormLayout(box)
        self.layer_combo = QgsMapLayerComboBox(box)
        self.layer_combo.setFilters(layer_filter('RasterLayer'))
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setToolTip(tr(
            'Any raster layer: an XYZ, WMS, WMTS or ArcGIS tile service, a '
            'satellite GeoTIFF, or a drone orthomosaic. It is read as it shows '
            'on the map (its colours and band choice), at 0.3 m per pixel.'))
        self.layer_combo.layerChanged.connect(self._schedule)
        form.addRow(tr('Imagery layer'), self.layer_combo)
        self.imagery_note = QLabel('', box)
        self.imagery_note.setWordWrap(True)
        self.imagery_note.setTextFormat(Qt.TextFormat.RichText)
        # Full width: in the field column a wrapped note reserves height for
        # a narrower column than it gets, and leaves a gap under it.
        form.addRow(self.imagery_note)
        self._pick_default_layer()
        self._lock(self.layer_combo)
        return box

    def _pick_default_layer(self):
        """The topmost visible raster that is not a map overlay."""
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
        self.sensitivity_combo = QComboBox(box)
        for ident, label, _value in T.SENSITIVITY:
            self.sensitivity_combo.addItem(tr(label), ident)
        self.sensitivity_combo.setCurrentIndex(
            self.sensitivity_combo.findData(T.DEFAULT_SENSITIVITY))
        self.sensitivity_combo.setToolTip(tr(
            'How sure the model must be before a pixel counts as road. More '
            'sensitive finds more narrow lanes and earth tracks, and more '
            'false roads.'))
        form.addRow(tr('Sensitivity'), self.sensitivity_combo)

        self.gap_spin = self._spin(box, 0, 200, 30, ' m', tr(
            'A road end that points at another road this close is joined to '
            'it, across trees, shadow or a bridge deck. Joins are marked '
            'GAP_JOIN = 1 and drawn dashed. 0 turns it off.'))
        form.addRow(tr('Join gaps up to'), self.gap_spin)
        self.min_length_spin = self._spin(box, 0, 1000, 25, ' m', tr(
            'Road pieces whose whole network is shorter than this are '
            'dropped: they are mostly roofs, yards and paths.'))
        form.addRow(tr('Drop pieces shorter than'), self.min_length_spin)
        self._lock(self.sensitivity_combo, self.gap_spin,
                   self.min_length_spin)
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
            'Douglas-Peucker simplification of the centerlines, edges and '
            'surface: a vertex is dropped when the line stays within this '
            'distance of where it was. Larger values give straighter streets '
            'and far fewer vertices; curves flatten and small bends go.'))
        self.simplify_combo.currentIndexChanged.connect(
            self._on_simplify_changed)
        form.addRow(tr('Simplify'), self.simplify_combo)
        self.simplify_spin = QDoubleSpinBox(box)
        self.simplify_spin.setRange(0.1, 20.0)
        self.simplify_spin.setDecimals(1)
        self.simplify_spin.setSingleStep(0.5)
        self.simplify_spin.setValue(1.0)
        self.simplify_spin.setSuffix(' m')
        self.simplify_spin.setToolTip(tr(
            'Simplification tolerance in metres.'))
        form.addRow(tr('Tolerance'), self.simplify_spin)
        self.spur_spin = self._spin(box, 0, 100, 8, ' m', tr(
            'Dead-end stubs shorter than this, where a road meets another, '
            'are removed - mostly driveways, yard entrances and the whiskers '
            'a wide junction leaves. Raise it for a cleaner network; 0 keeps '
            'them all.'))
        form.addRow(tr('Trim dead ends shorter than'), self.spur_spin)
        self.smooth_check = QCheckBox(tr('Smooth the lines and outlines'), box)
        self.smooth_check.setChecked(True)
        self.smooth_check.setToolTip(tr(
            'Rounds off the pixel steps before simplifying, so curves stay '
            'round and junction corners stay sharp.'))
        form.addRow('', self.smooth_check)
        self._form_generalize = form
        self._lock(self.simplify_combo, self.simplify_spin, self.spur_spin,
                   self.smooth_check)
        self._on_simplify_changed()
        return box

    def _on_simplify_changed(self, *_args):
        self._row_visible(self._form_generalize, self.simplify_spin,
                          self.simplify_combo.currentData() == 'custom')
        self._schedule_fit()

    def _simplify_m(self):
        key = self.simplify_combo.currentData()
        for ident, _label, value in T.SIMPLIFY:
            if ident == key and value is not None:
                return value
        return float(self.simplify_spin.value())

    @staticmethod
    def _spin(parent, low, high, value, suffix, tip):
        spin = QDoubleSpinBox(parent)
        spin.setRange(low, high)
        spin.setDecimals(0)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setToolTip(tip)
        return spin

    def _build_check(self):
        box = QGroupBox(tr('Check'), self)
        form = QFormLayout(box)
        self.reference_combo = QComboBox(box)
        for ident, label in REFERENCES:
            self.reference_combo.addItem(tr(label), ident)
        self.reference_combo.setToolTip(tr(
            'Compare the centerlines with another road network. Stretches '
            'only one of them has go on a road_check layer, and the report '
            'gives the share each way.'))
        self.reference_combo.currentIndexChanged.connect(
            self._on_reference_changed)
        form.addRow(tr('Compare with'), self.reference_combo)
        self.ref_layer_combo = QgsMapLayerComboBox(box)
        self.ref_layer_combo.setFilters(layer_filter('LineLayer'))
        form.addRow(tr('Line layer'), self.ref_layer_combo)
        self.tolerance_spin = self._spin(box, 1, 50, 8, ' m', tr(
            'How close the other network must be for a stretch to count as '
            'matched. Road centerlines in OpenStreetMap are often a few '
            'metres off the imagery.'))
        form.addRow(tr('Match distance'), self.tolerance_spin)
        self._form_check = form
        self._lock(self.reference_combo, self.ref_layer_combo,
                   self.tolerance_spin)
        return box

    def _build_output(self):
        box = QGroupBox(tr('Output'), self)
        form = QFormLayout(box)
        self.centre_check = QCheckBox(tr('Road centerlines'), box)
        self.edge_check = QCheckBox(tr('Road edges'), box)
        self.surface_check = QCheckBox(tr('Road surface (polygons)'), box)
        for check in (self.centre_check, self.edge_check, self.surface_check):
            check.setChecked(True)
            form.addRow('', check)
        self.prob_check = QCheckBox(tr('Also save the road probability '
                                       'raster'), box)
        self.prob_check.setToolTip(tr(
            '0-100 per pixel: how sure the model is. Useful to see what a '
            'more or less sensitive setting would catch.'))
        form.addRow('', self.prob_check)
        self.image_check = QCheckBox(tr('Also save the imagery that was read '
                                        '(GeoTIFF)'), box)
        self.image_check.setToolTip(tr(
            'The exact image the model saw, at 0.3 m, so the result can be '
            'checked against it later.'))
        form.addRow('', self.image_check)

        self.crs_combo = QComboBox(box)
        self.crs_combo.addItem(tr('UTM zone of the area (metres)'), CRS_UTM)
        self.crs_combo.addItem(tr('Current project CRS'), CRS_PROJECT)
        form.addRow(tr('Output CRS'), self.crs_combo)

        self.folder_widget = QgsFileWidget(box)
        self.folder_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        line = self.folder_widget.lineEdit()
        if line is not None:
            line.setPlaceholderText(tr('[Save to a temporary folder]'))
        self.folder_widget.setToolTip(tr(
            'Where <name>_roads.gpkg and the report are written. Leave it '
            'empty for a temporary folder, which QGIS deletes when it '
            'closes.'))
        self.folder_widget.fileChanged.connect(self._schedule)
        form.addRow(tr('Output folder'), self.folder_widget)
        FollowingField(self.folder_widget).suggest_folder()
        self.name_edit = QLineEdit('Detected roads', box)
        self.name_edit.setToolTip(tr(
            'Base name: <name>_roads.gpkg and <name>_roads_report.txt; also '
            'the name of the layer group.'))
        form.addRow(tr('Name'), self.name_edit)
        self._lock(self.centre_check, self.edge_check, self.surface_check,
                   self.prob_check, self.image_check, self.crs_combo,
                   self.folder_widget, self.name_edit)
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
        self.go_btn = QPushButton(tr('Detect roads'), self)
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

    def _refresh_runtime(self):
        state = self._runtime
        ready = state['runtime'] == 'ready'
        lines = []
        if state['runtime'] == 'ready':
            lines.append('<span style="color:{0}">✔ AI runtime '
                         '(onnxruntime {1}) ready.</span>'.format(
                             GREEN, state['version'] or ''))
        elif state['runtime'] == 'unprobed':
            lines.append(tr('Checking the AI runtime (onnxruntime {0})...')
                         .format(state['version'] or ''))
        elif state['runtime'] == 'broken':
            lines.append('<span style="color:{0}">✖ onnxruntime {1} is '
                         'installed but does not load in this QGIS. Click '
                         '<b>Install AI runtime</b> to put a version that does '
                         'into the plugin\'s own folder.</span>'.format(
                             ORANGE, state['version'] or ''))
        else:
            lines.append(tr('The AI runtime (onnxruntime) is not installed. '
                            'Click <b>Install AI runtime</b> - once, about '
                            '40 MB.'))
        if state['model']:
            lines.append('<span style="color:{0}">✔ Road model '
                         'downloaded.</span>'.format(GREEN))
        else:
            lines.append(tr('The road model is not downloaded yet. Click '
                            '<b>Download model</b> - once, about 98 MB.'))
        self.model_status.setText('<br>'.join(lines))
        busy = self._process is not None or self._running()
        self.install_btn.setVisible(not ready)
        self.copy_btn.setVisible(not ready)
        self.install_btn.setEnabled(not busy)
        self.model_btn.setVisible(not state['model'])
        self.model_btn.setEnabled(not busy)
        self._update_go_enabled()
        self._schedule_fit()

    def _model_ready(self):
        return self._runtime['runtime'] == 'ready' and self._runtime['model']

    def _install_runtime(self):
        exe = runtime.python_exe()
        if exe is None:
            self._warn(tr('The Python that QGIS runs on could not be found, '
                          'so onnxruntime cannot be installed from here. Click '
                          '<b>Copy install command</b> and run it yourself.'))
            return
        os.makedirs(runtime.libs_dir(), exist_ok=True)
        self._start_process('install', exe, runtime.pip_arguments())
        self._say(tr('Installing onnxruntime from pypi.org...'))

    def _probe_runtime(self):
        state = runtime.status()
        self._runtime = state
        if state['runtime'] != 'unprobed':
            self._refresh_runtime()
            return
        exe = runtime.python_exe()
        if exe is None:
            # Nothing to test with: trust a copy the plugin installed itself.
            ok = os.path.dirname(state['folder']) == runtime.libs_dir()
            runtime.remember_probe(state['folder'], state['version'], ok)
            self._runtime = runtime.status()
            self._refresh_runtime()
            return
        self._start_process('probe', exe, runtime.probe_arguments(
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
        # The probe: a crash or an error there means "do not import".
        state = runtime.status()
        ok = code == 0 and 'KGA-ORT-OK' in output
        if state['folder']:
            runtime.remember_probe(state['folder'], state['version'], ok)
        if not ok:
            QgsMessageLog.logMessage(
                'onnxruntime probe failed (exit {0}): {1}'.format(code, output),
                LOG_TAG, Qgis.MessageLevel.Warning)
        self._runtime = runtime.status()
        self._refresh_runtime()
        self._say(tr('Ready.') if ok else tr(
            'onnxruntime does not load in this QGIS.'), warn=not ok)

    def _download_model(self):
        path = runtime.model_path()
        self.model_btn.setEnabled(False)
        self.go_btn.setEnabled(False)
        self.bar.setValue(0)

        def progress(received, total):
            total = total if total and total > 0 else runtime.MODEL['bytes']
            self.bar.setValue(int(100.0 * received / total))
            self.bar.setFormat(tr('Downloading the model   %p%'))
            return not self._torn_down

        self._say(tr('Downloading the road model from huggingface.co...'))
        try:
            arrived = net.download_to(runtime.MODEL['url'], path, progress,
                                      self)
            if arrived:
                self._say(tr('Checking the model file...'))
                QApplication.processEvents()
                runtime.check_model_file(path)
        except (net.NetError, runtime.RuntimeProblem) as exc:
            with suppress(OSError):
                if os.path.exists(path):
                    os.remove(path)
            self._warn(str(exc))
            arrived = False
        self.bar.setValue(0)
        self.bar.setFormat('%p%')
        if self._torn_down:
            return
        self._runtime = runtime.status()
        self._refresh_runtime()
        self._say(tr('Model ready.') if arrived else tr('Ready.'))

    def _copy_command(self):
        QApplication.clipboard().setText(runtime.install_command())
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

    def _on_reference_changed(self, *_args):
        mode = self.reference_combo.currentData()
        self._row_visible(self._form_check, self.ref_layer_combo,
                          mode == REFERENCE_LAYER)
        self._row_visible(self._form_check, self.tolerance_spin,
                          mode != REFERENCE_NONE)
        self._schedule_fit()

    def _area(self):
        """(geometry, crs, clip, label). Raises DemError when unset."""
        region = self.picker.region()
        if region is None:
            raise DEM.DemError(tr('Choose an area.'))
        return region.geometry, region.crs, region.clip, region.label

    def _refresh_details(self):
        if self._torn_down:
            return
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
                    if size > 0.8:
                        text.append(tr('<span style="color:{0}">That is '
                                       'coarser than the 0.3 m the model '
                                       'expects: expect narrow roads to be '
                                       'missed.</span>').format(ORANGE))
                    elif size < 0.2:
                        text.append(tr('It is averaged down to 0.3 m, which '
                                       'suits the model.'))
            else:
                text.append(tr('A tile service; a coarser zoom is used where '
                               'the full one has no imagery.'))
            if sampler.looks_like_overlay(layer):
                text.append(tr('<span style="color:{0}">Its name suggests '
                               'labels or roads drawn on it (hybrid, '
                               'map, OSM...). Drawn roads would be "detected": '
                               'choose plain imagery.</span>').format(ORANGE))
            self.imagery_note.setText(' '.join(text))
        try:
            geometry, crs, clip, label = self._area()
            utm = sampler.utm_for(geometry, crs)
            grid = sampler.Grid.around(geometry, crs, utm, 0.3, T.MARGIN_M)
            transform = QgsCoordinateTransform(crs, utm,
                                               QgsProject.instance())
            shape = QgsGeometry(geometry)
            shape.transform(transform)
            km2 = (shape.area() if clip else shape.boundingBox().area()) / 1e6
            notes.append(tr('<b>Area:</b> {0}, {1:,.2f} km² '
                            '({2:,} million pixels at 0.3 m), in {3}.').format(
                label, km2, max(1, grid.pixels // 1000000), utm.authid()))
            warning = T.size_warning(grid.pixels)
            if warning is not None:
                notes.append('<span style="color:{0}">{1}</span>'.format(
                    ORANGE, warning[1]))
        except DEM.DemError as exc:
            notes.append(str(exc))
        except Exception as exc:                # a bad geometry, a bad CRS
            # Typically a new, empty project: the map has no real extent yet
            # and the transform error is a wall of 1e308s. The words help; the
            # error goes to the log for whoever needs it.
            QgsMessageLog.logMessage(
                'Detect Roads: the area could not be read: {0}'.format(exc),
                LOG_TAG, Qgis.MessageLevel.Info)
            notes.append(tr(
                'The area could not be read. If the map is empty, add the '
                'imagery layer and zoom to the area first; otherwise choose '
                'another area or layer.'))
        if not self._model_ready():
            notes.append(tr('Install the AI runtime and download the model '
                            'first (above).'))
        notes.append(tr('Results are a draft from imagery: check them before '
                        'use, and check the imagery provider\'s terms before '
                        'you share roads traced from it.'))
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
        if not (self.centre_check.isChecked() or self.edge_check.isChecked()
                or self.surface_check.isChecked()):
            self.tabs.setCurrentIndex(self.tabs.count() - 1)
            self._warn(tr('Tick at least one of centerlines, edges or '
                          'surface.'))
            return
        if sampler.looks_like_overlay(layer):
            answer = QMessageBox.question(
                self, tr(TITLE), tr(
                    '"{0}" looks like a map or a hybrid layer with roads or '
                    'labels drawn on it. Those would be detected as roads. '
                    'Use it anyway?').format(layer.name()))
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

        try:
            work_crs = sampler.utm_for(geometry, crs)
            grid = sampler.Grid.around(geometry, crs, work_crs, 0.3,
                                       T.MARGIN_M)
        except Exception as exc:                # an empty map, a bad CRS
            QgsMessageLog.logMessage(
                'Detect Roads: the area could not be read: {0}'.format(exc),
                LOG_TAG, Qgis.MessageLevel.Info)
            self.tabs.setCurrentWidget(self.area_tab)
            self._warn(tr('The area could not be read. If the map is empty, '
                          'add the imagery layer and zoom to the area first; '
                          'otherwise choose another area or layer.'))
            return
        if grid.pixels > T.MAX_PIXELS:
            self._warn(tr('That area is {0:,} million pixels at 0.3 m, more '
                          'than this tool reads in one go. Choose a smaller '
                          'area.').format(grid.pixels // 1000000))
            return
        warning = T.size_warning(grid.pixels)
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

        reference = None
        mode = self.reference_combo.currentData()
        bounds = _wgs84_bounds(geometry, crs)
        if mode == REFERENCE_LAYER:
            try:
                reference = CHECK.reference_from_layer(
                    self.ref_layer_combo.currentLayer(), bounds, False)
            except CHECK.ReferenceError as exc:
                self._warn(str(exc).replace('streams or rivers', 'roads')
                           .replace('stream layer', 'road layer'))
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
        gpkg = os.path.join(folder, '{0}_roads.gpkg'.format(name))
        prob = os.path.join(folder, '{0}_road_probability.tif'.format(name)) \
            if self.prob_check.isChecked() else None
        image = os.path.join(folder, '{0}_imagery.tif'.format(name)) \
            if self.image_check.isChecked() else None
        report = os.path.join(folder, '{0}_roads_report.txt'.format(name))
        if not temporary:
            existing = [os.path.basename(p) for p in (gpkg, prob, image)
                        if p and os.path.exists(p)]
            if existing:
                answer = QMessageBox.question(
                    self, tr('Overwrite?'),
                    tr('{0} already exists. Replace it?').format(
                        ' and '.join(existing)))
                if answer != QMessageBox.StandardButton.Yes:
                    return
                # A GeoPackage still open in the project cannot be replaced.
                _drop_layers_from(gpkg)

        out_crs = work_crs if self.crs_combo.currentData() == CRS_UTM else \
            QgsProject.instance().crs()
        spec = T.RoadSpec(
            layer=clone, layer_name=layer.name(),
            local=sampler.is_local(layer), area=QgsGeometry(geometry),
            area_crs=crs, clip=clip, area_label=label, bounds=bounds,
            work_crs=work_crs, out_crs=out_crs,
            model_path=runtime.model_path(),
            threshold=T.threshold_for(self.sensitivity_combo.currentData()),
            gap_m=self.gap_spin.value(),
            min_length_m=self.min_length_spin.value(),
            smooth=self.smooth_check.isChecked(),
            simplify_m=self._simplify_m(),
            spur_m=self.spur_spin.value(),
            want_centre=self.centre_check.isChecked(),
            want_edge=self.edge_check.isChecked(),
            want_surface=self.surface_check.isChecked(),
            gpkg_path=gpkg, prob_path=prob, image_path=image,
            report_path=report, title=name, reference=reference,
            compare_osm=mode == REFERENCE_OSM,
            tolerance=self.tolerance_spin.value())
        self._spec = spec
        task = T.RoadTask(spec)
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
        self.go_btn.setText(tr('Stop') if running else tr('Detect roads'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True if running else self._model_ready())
        self.install_btn.setEnabled(not running)
        self.model_btn.setEnabled(not running)
        if not running:
            self._on_reference_changed()

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
                (T.LAYER_CHECK, result.get('check_layer'), style.apply_check),
                (T.LAYER_CENTRE, spec.want_centre, style.apply_centrelines),
                (T.LAYER_EDGE, spec.want_edge, style.apply_edges),
                (T.LAYER_SURFACE, spec.want_surface, style.apply_surface)):
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
        if result.get('prob_path'):
            raster = QgsRasterLayer(result['prob_path'],
                                    'road probability', 'gdal')
            if raster.isValid():
                with suppress(Exception):
                    style.apply_probability(raster)
                project.addMapLayer(raster, False)
                node = group.addLayer(raster)
                node.setItemVisibilityChecked(False)
        if result.get('image_path'):
            raster = QgsRasterLayer(result['image_path'], 'imagery read',
                                    'gdal')
            if raster.isValid():
                project.addMapLayer(raster, False)
                node = group.addLayer(raster)
                node.setItemVisibilityChecked(False)
        if not added:
            self._warn(tr('The roads were written to {0} but QGIS could not '
                          'open them.').format(spec.gpkg_path))
            return

        text = tr('Done. {0:,} centerline segments, {1:,.2f} km; {2:,} '
                  'surface polygons.').format(
            result['centrelines'], result['length_km'], result['surfaces'])
        if result.get('scale', 1) > 1:
            text += tr(' The tile service had no imagery at full zoom here; a '
                       'coarser zoom was used (see the report).')
        self._say(text)
        if self._temp_dir:
            self._push(tr('The roads were written to a temporary folder, '
                          'which QGIS deletes when it closes - export them to '
                          'keep them.'), warn=True)
        else:
            self._push(tr('Roads saved to {0}.').format(spec.gpkg_path),
                       warn=False)
        if result.get('reference_problem'):
            self._push(tr('The comparison could not be made: {0}').format(
                result['reference_problem']), warn=True)
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
            dialog.setWindowTitle(tr('Road report'))
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
            self, tr(TITLE), tr('Stop detecting roads and close?'))
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


# ------------------------------------------------------------------ helpers

def _wgs84_bounds(geometry, crs):
    transform = QgsCoordinateTransform(
        crs, QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
    box = transform.transformBoundingBox(geometry.boundingBox())
    return (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())


def _drop_layers_from(path):
    """Remove project layers reading `path`, so the file can be replaced."""
    project = QgsProject.instance()
    norm = os.path.normcase(os.path.normpath(path))
    doomed = []
    for layer in project.mapLayers().values():
        source = layer.source().split('|')[0]
        if os.path.normcase(os.path.normpath(source)) == norm:
            doomed.append(layer.id())
    if doomed:
        project.removeMapLayers(doomed)


def _tag(layer, spec):
    with suppress(Exception):
        metadata = layer.metadata()
        metadata.setTitle('{0} - {1}'.format(spec.title, layer.name()))
        metadata.setRights([runtime.MODEL['credit'],
                            'Imagery: {0}'.format(spec.layer_name)])
        metadata.setLicenses([
            'Model: {0}. Check the imagery provider\'s terms for derived '
            'data.'.format(runtime.MODEL['licence'])])
        layer.setMetadata(metadata)


class _FitScroll(QScrollArea):
    """A scroll area that asks for the height of what it holds.

    A plain QScrollArea's size hint ignores its content, so a window sized to
    hints would not know how tall a tab wants to be. This one reports the
    content's height; the window caps it at the screen, and only then does the
    tab scroll.
    """

    def sizeHint(self):
        hint = super().sizeHint()
        inner = self.widget()
        if inner is None:
            return hint
        content = inner.sizeHint()
        return QSize(hint.width(), content.height() + 2 * self.frameWidth())

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        inner = self.widget()
        if inner is None:
            return hint
        return QSize(hint.width(), min(inner.minimumSizeHint().height(), 60))


class _FitTabs(QTabWidget):
    """A tab widget as tall as its open page, not its tallest one.

    QTabWidget's own size hint takes the largest page whatever is shown, so a
    window fitted to it would keep the Output tab's height on the Check tab.
    """

    def _chrome(self, base, pages):
        """Height of the tab bar and frame: the base hint less its page."""
        return max(0, base - max(pages or [0]))

    def _page_heights(self, minimum=False):
        heights = []
        for i in range(self.count()):
            page = self.widget(i)
            if page is not None:
                hint = page.minimumSizeHint() if minimum else page.sizeHint()
                heights.append(hint.height())
        return heights

    def sizeHint(self):
        hint = super().sizeHint()
        page = self.currentWidget()
        if page is None:
            return hint
        chrome = self._chrome(hint.height(), self._page_heights())
        return QSize(hint.width(), chrome + page.sizeHint().height())

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        page = self.currentWidget()
        if page is None:
            return hint
        chrome = self._chrome(hint.height(), self._page_heights(True))
        return QSize(hint.width(), chrome + page.minimumSizeHint().height())

    def hasHeightForWidth(self):
        return False

# -*- coding: utf-8 -*-
"""CSV to DEM and Contour: the window.

UI and wiring only; the numerics are ``core.csv_dem``, shared with the
Processing algorithm. The window opens in place of the Toolbox form
(``createCustomParametersWidget``) and is modeless, so the map stays usable
while a job runs on a worker thread.

The settings panel follows the interpolation method: each method shows only
the options it reads (``csv_dem.METHOD_OPTIONS``) under a short note on when to
use it. Once a CSV is chosen the window reads it, so it can say how many points
it found and how big the DEM will be before anything is run.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
from contextlib import suppress

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsProject, QgsRasterLayer, QgsTask, QgsVectorLayer)
from qgis.gui import QgsDoubleSpinBox, QgsFileWidget, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTabWidget,
    QStackedWidget, QVBoxLayout, QWidget)

from ..branding import open_docs
from ..core import csv_dem as C
from ..core.imagery import paths
from .csv_column_widget import guess_column, read_csv_headers
from .output_fields import FollowingField

ALG_NAME = 'csvtodemcontour'
TITLE = 'CSV to DEM and Contour'

# Colours sampled from icons/kga.png, as in the Imagery Downloader.
BLUE = '#174592'
ORANGE = '#FC663A'
RED = '#B3261E'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QLabel#kgaNote { font-weight: normal; font-style: italic; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE}

#: Label and a one-or-two-line "when to use it" note for each method.
METHOD_INFO = {
    C.IDW: ('IDW - Inverse Distance Weighting',
            'Weighted average of the nearest points; nearer ones count more. '
            'The safe default for scattered survey points. Never goes above '
            'the highest or below the lowest value nearby.'),
    C.LINEAR: ('Linear - Delaunay triangles',
               'Tilts a flat plane across each triangle of points. Honest to '
               'the data, best for dense, even points such as a grid survey. '
               'Contours look angular.'),
    C.CUBIC: ('Cubic - smooth surface',
              'Like Linear but blended into a smooth curve. Good for gently '
              'rolling ground; may overshoot where points are sparse or uneven.'),
    C.NEAREST: ('Nearest Neighbour',
                'Each cell copies the height of the closest point. Blocky: a '
                'quick look only, not suited to contours.'),
    C.RBF: ('RBF - Radial basis functions',
            'Fits a smooth surface through the nearest points around each '
            'cell. Natural-looking terrain from scattered points; slower than '
            'the others and can overshoot.'),
}

#: How each method option is shown: label, widget kind, settings, tooltip.
OPTION_SPECS = {
    'power': ('Power', 'double', {'min': 0.1, 'max': 10.0, 'decimals': 1},
              'How fast influence falls with distance. 2 is standard; higher '
              'follows the nearest points more tightly.'),
    'neighbours': ('Neighbours', 'int', {'min': 1, 'max': 500},
                   'Nearest survey points averaged for each cell.'),
    'radius': ('Search radius (m)', 'double',
               {'min': 0.0, 'max': 1e7, 'decimals': 1, 'special': 'No limit'},
               'Ignore points further away than this. A cell with none in '
               'range takes the nearest point\'s height.'),
    'fill_edges': ('Fill outside the points', 'bool', {},
                   'Linear and Cubic only know the ground inside the outline '
                   'of the points. Ticked: the rest takes the nearest '
                   'point\'s height. Unticked: left as NoData.'),
    'clamp': ('Clamp to surveyed range', 'bool', {},
              'Stops the surface rising above the highest or falling below '
              'the lowest surveyed point.'),
    'rbf_kernel': ('Kernel', 'choice', {'items': C.RBF_KERNELS},
                   'Shape of the fitted surface. thin_plate_spline is the '
                   'usual choice for terrain.'),
    'rbf_neighbours': ('Neighbours', 'int', {'min': 5, 'max': 2000},
                       'Nearest points used for each local fit. More is '
                       'smoother and slower.'),
    'rbf_smoothing': ('Smoothing', 'double',
                      {'min': 0.0, 'max': 1e6, 'decimals': 3},
                      '0 passes through every point. Raise it to relax the '
                      'surface when points disagree (noisy survey, duplicates).'),
}


def tr(text):
    return QgsApplication.translate('KgaCsvDem', text)


class _Quiet(object):
    """A feedback that says nothing, for reading the CSV while the form is filled."""

    def pushInfo(self, _text):
        pass

    def setProgress(self, _value):
        pass

    def isCanceled(self):
        return False


class _TaskFeedback(object):
    """The feedback `csv_dem.run` gets on the worker thread."""

    def __init__(self, task):
        self._task = task

    def pushInfo(self, text):
        self._task.logged.emit(str(text))

    def pushWarning(self, text):
        self._task.logged.emit('WARNING: ' + str(text))

    def setProgress(self, value):
        self._task.setProgress(float(value))

    def isCanceled(self):
        return self._task.isCanceled()


class CsvDemTask(QgsTask):
    """The whole job on a worker thread."""

    logged = pyqtSignal(str)
    jobFinished = pyqtSignal(dict)

    def __init__(self, args):
        flag = getattr(QgsTask, 'Flag', QgsTask)        # scoped enum, QGIS 3.36+
        super().__init__(tr(TITLE), flag.CanCancel)
        self.args = args
        self.result = {}
        self.error = None
        self.stopped = False

    def run(self):
        try:
            self.result = C.run(feedback=_TaskFeedback(self), **self.args)
            return True
        except C.Canceled:
            self.stopped = True
        except (ValueError, OSError, MemoryError, RuntimeError) as exc:
            self.error = str(exc) or exc.__class__.__name__
        except Exception as exc:                        # noqa: BLE001
            self.error = '{0}: {1}'.format(exc.__class__.__name__, exc)
        return False

    def finished(self, ok):
        self.jobFinished.emit({'ok': ok, 'result': self.result,
                               'error': self.error, 'stopped': self.stopped})


class CsvDemDialog(QDialog):
    """Survey CSV in; DEM GeoTIFF and contours out."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)

        self._torn_down = False
        self._ignore_close = False
        self._closing = False
        self._task = None
        self._points = None             # DataFrame of the chosen CSV, or None
        self._points_key = None
        self._opts = {}                 # (method, key) -> widget

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._read_points)

        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.addWidget(self._build_points())
        root.addWidget(self._build_tabs())
        root.addStretch(1)
        root.addLayout(self._build_run())

        self._on_method(0)
        self._on_contour_toggled()
        self._read_points()

    # --------------------------------------------------------------- building

    def _note(self, text='', parent=None):
        label = QLabel(text, parent)
        label.setObjectName('kgaNote')
        label.setWordWrap(True)
        return label

    def _build_points(self):
        box = QGroupBox(tr('Survey points'), self)
        form = QFormLayout(box)

        self.csv_widget = QgsFileWidget(box)
        self.csv_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.csv_widget.setFilter('CSV files (*.csv *.txt);;All files (*.*)')
        self.csv_widget.setToolTip(tr(
            'Surveyed points, one per row, with easting, northing and '
            'elevation columns.'))
        self.csv_widget.fileChanged.connect(self._on_csv)
        form.addRow(tr('Point CSV'), self.csv_widget)

        row = QHBoxLayout()
        self.field_boxes = {}
        for axis, text in (('X', tr('X (Easting)')), ('Y', tr('Y (Northing)')),
                           ('Z', tr('Z (Elevation)'))):
            col = QVBoxLayout()
            col.addWidget(QLabel(text, box))
            combo = QComboBox(box)
            combo.setEditable(True)
            combo.lineEdit().setPlaceholderText(tr('auto-detect'))
            combo.setToolTip(tr(
                'Pick the {0} column, or type its name or number (1 = first '
                'column). Leave all three blank to auto-detect.').format(axis))
            combo.currentTextChanged.connect(self._schedule)
            col.addWidget(combo)
            row.addLayout(col)
            self.field_boxes[axis] = combo
        form.addRow(row)

        self.points_label = QLabel('', box)
        self.points_label.setWordWrap(True)
        form.addRow(self.points_label)

        self.crs_widget = QgsProjectionSelectionWidget(box)
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem('EPSG:32648'))
        self.crs_widget.setToolTip(tr(
            'The CRS the Easting/Northing values are already in. Nothing is '
            'reprojected: this is stamped on the outputs, so a wrong choice '
            'puts the DEM in the wrong place.'))
        self.crs_widget.crsChanged.connect(self._on_crs)
        form.addRow(tr('Coordinate system'), self.crs_widget)
        self.crs_note = self._note('', box)
        form.addRow(self.crs_note)
        self._on_crs()
        return box

    def _make_option(self, key, parent):
        label, kind, cfg, tip = OPTION_SPECS[key]
        default = C.DEFAULT_OPTIONS[key]
        if kind == 'double':
            w = QgsDoubleSpinBox(parent)
            w.setRange(cfg['min'], cfg['max'])
            w.setDecimals(cfg.get('decimals', 2))
            w.setClearValueMode(QgsDoubleSpinBox.ClearValueMode.CustomValue,
                                str(default))
            w.setValue(default)
            if 'special' in cfg:
                w.setSpecialValueText(tr(cfg['special']))
        elif kind == 'int':
            w = QSpinBox(parent)
            w.setRange(cfg['min'], cfg['max'])
            w.setValue(default)
        elif kind == 'bool':
            w = QCheckBox(parent)
            w.setChecked(default)
        else:
            w = QComboBox(parent)
            w.addItems(list(cfg['items']))
            w.setCurrentText(default)
        w.setToolTip(tr(tip))
        return label, w

    def _build_method(self):
        box = QGroupBox(self)
        lay = QVBoxLayout(box)

        form = QFormLayout()
        self.method_combo = QComboBox(box)
        for method in C.METHODS:
            self.method_combo.addItem(METHOD_INFO[method][0], method)
        self.method_combo.setToolTip(tr(
            'How the height of each DEM cell is estimated from the points '
            'around it. The settings below change with the method.'))
        form.addRow(tr('Method'), self.method_combo)
        lay.addLayout(form)

        self.method_note = self._note('', box)
        lay.addWidget(self.method_note)

        # One page of settings per method, so the panel shows only what the
        # chosen method reads.
        self.stack = QStackedWidget(box)
        for method in C.METHODS:
            page = QWidget(self.stack)
            pform = QFormLayout(page)
            pform.setContentsMargins(0, 0, 0, 0)
            keys = C.METHOD_OPTIONS[method]
            if not keys:
                pform.addRow(self._note(tr('This method has no settings.'), page))
            for key in keys:
                label, widget = self._make_option(key, page)
                self._opts[(method, key)] = widget
                pform.addRow(tr(label), widget)
            self.stack.addWidget(page)
        lay.addWidget(self.stack)

        cell = QHBoxLayout()
        self.auto_cell = QCheckBox(tr('Auto'), box)
        self.auto_cell.setChecked(True)
        self.auto_cell.setToolTip(tr(
            'Pick the cell size from the point density (about 1/500 of the '
            'site width, never below 0.5 m).'))
        self.cell_spin = QgsDoubleSpinBox(box)
        self.cell_spin.setRange(0.001, 100000.0)
        self.cell_spin.setDecimals(3)
        self.cell_spin.setValue(1.0)
        self.cell_spin.setSuffix(' m')
        self.cell_spin.setToolTip(tr(
            'Size of one DEM cell. Halving it makes four times as many cells.'))
        cell.addWidget(self.auto_cell)
        cell.addWidget(self.cell_spin, 1)
        cform = QFormLayout()
        cform.addRow(tr('Cell size'), cell)
        lay.addLayout(cform)
        self.grid_label = self._note('', box)
        lay.addWidget(self.grid_label)

        self.method_combo.currentIndexChanged.connect(self._on_method)
        self.auto_cell.toggled.connect(self._on_cell_mode)
        self.cell_spin.valueChanged.connect(self._update_grid)
        self._on_cell_mode()
        return box

    def _build_contours(self):
        box = QGroupBox(tr('Generate contour lines'), self)
        box.setCheckable(True)
        box.setChecked(True)
        box.setToolTip(tr('Untick to produce only the DEM.'))
        self.contour_box = box
        form = QFormLayout(box)

        self.interval_spin = QgsDoubleSpinBox(box)
        self.interval_spin.setRange(0.01, 10000.0)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setValue(1.0)
        self.interval_spin.setSuffix(' m')
        self.interval_spin.setToolTip(tr(
            'Vertical distance between lines. Lines fall on multiples of it. '
            'A small interval on a large, steep site makes a very large file.'))
        form.addRow(tr('Interval'), self.interval_spin)

        self.smooth_spin = QgsDoubleSpinBox(box)
        self.smooth_spin.setRange(0.0, 20.0)
        self.smooth_spin.setDecimals(1)
        self.smooth_spin.setValue(1.0)
        self.smooth_spin.setSpecialValueText(tr('Off'))
        self.smooth_spin.setToolTip(tr(
            'Gaussian blur in cells applied before the lines are traced: '
            '1 = light, 2-3 = strong. Strong values round off real features '
            'such as ditches and bunds. The DEM itself is not smoothed.'))
        form.addRow(tr('Smoothing (cells)'), self.smooth_spin)

        self.format_combo = QComboBox(box)
        self.format_combo.addItem('Shapefile (.shp)', 'shp')
        self.format_combo.addItem('GeoPackage (.gpkg)', 'gpkg')
        form.addRow(tr('Format'), self.format_combo)

        box.toggled.connect(self._on_contour_toggled)
        return box

    def _build_output(self):
        box = QGroupBox(self)
        form = QFormLayout(box)

        self.folder_widget = QgsFileWidget(box)
        self.folder_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.folder_widget.setToolTip(tr(
            'The DEM and contours are written here, named after the CSV.'))
        form.addRow(tr('Folder'), self.folder_widget)
        self._follow = FollowingField(self.folder_widget)
        self._follow.suggest_folder()                   # the project folder

        self.load_check = QCheckBox(tr('Add the results to the map'), box)
        self.load_check.setChecked(True)
        form.addRow('', self.load_check)
        return box

    def _build_tabs(self):
        """One tab per section; the tab area is as tall as the page shown."""
        self.tabs = QTabWidget(self)
        for box, name, tip in (
                (self._build_method(), tr('Surface'),
                 tr('Interpolation method, its settings and the cell size')),
                (self._build_contours(), tr('Contours'),
                 tr('Contour interval, smoothing and format')),
                (self._build_output(), tr('Output'),
                 tr('Where the results go'))):
            box.setFlat(True)
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(2, 2, 2, 2)
            lay.addWidget(box)
            lay.addStretch(1)
            self.tabs.setTabToolTip(self.tabs.addTab(page, name), tip)
        self.tabs.currentChanged.connect(self._fit_tab)
        self._fit_tab()
        return self.tabs

    def _fit_tab(self, *_, adjust=True):
        """Make the tab area as tall as the page shown, at the width it has.

        QTabWidget sizes to its tallest page, and a wrapped note is taller at
        a narrow width, so the height is asked of the page's own layout.
        """
        page = self.tabs.currentWidget() if hasattr(self, 'tabs') else None
        if page is None:
            return
        lay = page.layout()
        lay.activate()
        width = max(self.tabs.width() - 6, 200)
        height = lay.totalHeightForWidth(width)
        if height <= 0:
            height = lay.totalSizeHint().height()
        height += self.tabs.tabBar().sizeHint().height() + 10
        if self.tabs.height() != height or self.tabs.minimumHeight() != height:
            self.tabs.setFixedHeight(height)
        if adjust and self.isVisible():
            self._later_resize()

    def _build_run(self):
        lay = QVBoxLayout()
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(16)

        # Collapsed until a run starts, as in Paddy Irrigation Tools.
        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(120)
        self.log_group = QGroupBox(tr('Log'), self)
        self.log_group.setCheckable(True)
        glay = QVBoxLayout(self.log_group)
        glay.setContentsMargins(6, 2, 6, 4)
        glay.addWidget(self.bar)
        glay.addWidget(self.log)
        self.log_group.toggled.connect(self._on_log_toggled)
        self.log_group.setChecked(False)
        self.bar.setVisible(False)
        self.log.setVisible(False)
        lay.addWidget(self.log_group)

        row = QHBoxLayout()
        self.help_btn = QPushButton(tr('Help'), self)
        self.help_btn.clicked.connect(lambda: open_docs(ALG_NAME))
        self.go_btn = QPushButton(tr('Run'), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._go_or_stop)
        self.close_btn = QPushButton(tr('Close'), self)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.help_btn)
        row.addStretch(1)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        return lay

    # Processing drives a custom parameters dialog like its own: setParameters(),
    # show(), exec(), results(), then close(). This one is modeless, so exec()
    # just shows it, and the close() that follows straight away is ignored.
    def exec(self):
        self._ignore_close = True
        QTimer.singleShot(0, self._allow_close)
        self.show()
        self.raise_()
        self.activateWindow()
        return 0

    exec_ = exec

    def _allow_close(self):
        self._ignore_close = False

    def setParameters(self, parameters):
        """Pre-fill from a Processing parameter dict (only what is given)."""
        csv = parameters.get('INPUT_CSV')
        if isinstance(csv, str) and csv:
            self.csv_widget.setFilePath(csv)
            self._on_csv()
        folder = parameters.get('OUTPUT_FOLDER')
        if isinstance(folder, str) and folder:
            self.folder_widget.setFilePath(folder)
        method = parameters.get('METHOD')
        if isinstance(method, int) and 0 <= method < len(C.METHODS):
            self.method_combo.setCurrentIndex(method)

    def results(self):
        return {}

    # ---------------------------------------------------------------- reacting

    def _method(self):
        return self.method_combo.currentData()

    def _on_method(self, _index=None):
        method = self._method()
        self.stack.setCurrentIndex(C.METHODS.index(method))
        # A stack is as tall as its tallest page; size it to the shown one.
        page = self.stack.currentWidget()
        page.layout().activate()
        self.stack.setFixedHeight(page.layout().totalSizeHint().height())
        if hasattr(self, 'tabs'):
            self._fit_tab()
        self.method_note.setText(METHOD_INFO[method][1])

    def _on_cell_mode(self, *_):
        self.cell_spin.setEnabled(not self.auto_cell.isChecked())
        self._update_grid()

    def showEvent(self, event):
        super().showEvent(event)
        # Page heights are only right once the widgets have real fonts and
        # widths, so size the settings and the tab again now.
        self._on_method()
        self._later_resize()

    def _on_log_toggled(self, shown):
        self.bar.setVisible(shown)
        self.log.setVisible(shown)
        self._later_resize()

    def _later_resize(self):
        # After the layout has settled, so the new size hint is the real one.
        QTimer.singleShot(0, self._refit)

    def _refit(self):
        if self._torn_down:
            return
        self._fit_tab(adjust=False)
        self.layout().invalidate()
        self.layout().activate()
        # Keep the width the user chose; take the height the content needs.
        self.resize(self.width(), self.layout().totalMinimumSize().height()
                    if self.isMaximized() else self.layout().totalSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_tab(adjust=False)         # wrapped notes change height with width

    def _on_contour_toggled(self, *_):
        # A checkable group box leaves its children enabled; say so by greying.
        for w in (self.interval_spin, self.smooth_spin, self.format_combo):
            w.setEnabled(self.contour_box.isChecked())

    def _on_crs(self, *_):
        crs = self.crs_widget.crs()
        if not crs.isValid():
            text = tr('Choose a coordinate system.')
        elif crs.isGeographic():
            text = tr('This CRS is in degrees. The cell size and contour '
                      'interval are in the CRS\'s units, so use a projected '
                      'CRS in metres.')
        else:
            text = ''
        self.crs_note.setText(text)
        self.crs_note.setVisible(bool(text))

    def _on_csv(self, *_):
        self._points = None
        self._points_key = None
        self._fill_fields()
        self._schedule()

    def _fill_fields(self):
        """List the CSV's headers in the X/Y/Z boxes, pre-picking the obvious."""
        headers = read_csv_headers(self.csv_widget.filePath().strip())
        for axis, combo in self.field_boxes.items():
            current = combo.currentText().strip()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('')
            combo.addItems(headers)
            if current in headers or current.isdigit():
                combo.setCurrentText(current)
            elif len(headers) < 10:         # 10+ = side-by-side tables, auto
                combo.setCurrentText(guess_column(headers, axis))
            combo.blockSignals(False)

    def _schedule(self, *_):
        self._debounce.start()

    def _fields(self):
        return tuple(self.field_boxes[a].currentText().strip() for a in 'XYZ')

    def _read_points(self):
        """Read the chosen CSV so the window can say what it found."""
        path = self.csv_widget.filePath().strip()
        fields = self._fields()
        key = (path, fields)
        if key == self._points_key:
            return
        self._points_key = key
        self._points = None
        if not path:
            self._say_points(tr('Choose the survey CSV.'))
        elif not os.path.isfile(path):
            self._say_points(tr('That file does not exist.'), bad=True)
        else:
            try:
                df = C.load_points(path, _Quiet(), *fields)
            except Exception as exc:                    # noqa: BLE001 - shown to the user
                self._say_points(str(exc), bad=True)
            else:
                self._points = df
                z = df['Elevation']
                self._say_points(tr('{0} points  -  elevation {1:.2f} to {2:.2f}')
                                 .format(format(len(df), ','), z.min(), z.max()))
        self._update_grid()

    def _say_points(self, text, bad=False):
        self.points_label.setText(text)
        self.points_label.setStyleSheet('color: %s;' % RED if bad else '')

    def _cell(self):
        """The cell size the run will use, or None until points are read."""
        if not self.auto_cell.isChecked():
            return self.cell_spin.value()
        return None if self._points is None else C.auto_resolution(self._points)

    def _update_grid(self, *_):
        cell = self._cell()
        if self._points is None or not cell:
            self.grid_label.setText('')
            return
        cols, rows = C.grid_size(self._points, cell)
        cells = cols * rows
        text = tr('Cell size {0:g} m  -  DEM of {1} x {2} cells ({3} total)').format(
            cell, format(cols, ','), format(rows, ','), format(cells, ','))
        if cells > C.BIG_GRID:
            text += tr('  -  very large: expect a long run and a big file.')
        self.grid_label.setText(text)
        self.grid_label.setStyleSheet('color: %s;' % RED
                                      if cells > C.BIG_GRID else '')
        if self.auto_cell.isChecked():
            self.cell_spin.blockSignals(True)
            self.cell_spin.setValue(cell)
            self.cell_spin.blockSignals(False)

    # -------------------------------------------------------------------- run

    def _options(self):
        """The settings of the chosen method only."""
        method = self._method()
        out = {}
        for key in C.METHOD_OPTIONS[method]:
            w = self._opts[(method, key)]
            if isinstance(w, QCheckBox):
                out[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            else:
                out[key] = w.value()
        return out

    def _epsg(self):
        """The EPSG number of the chosen CRS, or an error message."""
        crs = self.crs_widget.crs()
        if not crs.isValid():
            return None, tr('Choose the coordinate system of the CSV.')
        auth, _sep, code = crs.authid().partition(':')
        if auth != 'EPSG' or not code.isdigit():
            return None, tr('Choose a coordinate system that has an EPSG '
                            'code (this one is {0}).').format(
                                crs.authid() or crs.description())
        return int(code), ''

    def _warn(self, text):
        QMessageBox.warning(self, tr(TITLE), text)

    def _running(self):
        return self._task is not None

    def _go_or_stop(self):
        if self._running():
            self.go_btn.setEnabled(False)
            self.status_text(tr('Stopping...'))
            self._task.cancel()
            return
        self._start()

    def status_text(self, text):
        self.bar.setFormat(text + '  %p%' if text else '%p%')

    def _start(self):
        self._read_points()                 # settle any edit still waiting
        self._debounce.stop()
        path = self.csv_widget.filePath().strip()
        if not path or not os.path.isfile(path):
            self._warn(tr('Choose the survey CSV first.'))
            return
        if self._points is None:
            self._warn(self.points_label.text())
            return
        epsg, problem = self._epsg()
        if epsg is None:
            self._warn(problem)
            return
        folder = os.path.normpath(self.folder_widget.filePath().strip() or '.')
        problem = paths.folder_problem(self.folder_widget.filePath().strip())
        if problem:
            self._warn(problem)
            return

        cell = self._cell()
        cols, rows = C.grid_size(self._points, cell)
        if cols * rows > C.BIG_GRID:
            answer = QMessageBox.question(
                self, tr('Large DEM'),
                tr('This makes a DEM of {0} cells and may take a long time '
                   'and a lot of memory.\n\nContinue anyway?').format(
                       format(cols * rows, ',')))
            if answer != QMessageBox.StandardButton.Yes:
                return

        contour = self.contour_box.isChecked()
        interval = self.interval_spin.value()
        fmt = self.format_combo.currentData()
        base = os.path.splitext(os.path.basename(path))[0]
        wanted = [base + '_DEM.tif']
        if contour:
            wanted.append('{0}_Contour_{1}m.{2}'.format(base, interval, fmt))
        existing = [n for n in wanted if os.path.exists(os.path.join(folder, n))]
        if existing:
            answer = QMessageBox.question(
                self, tr('Overwrite?'),
                tr('{0} already exists. Replace it?').format(' and '.join(existing)))
            if answer != QMessageBox.StandardButton.Yes:
                return

        x, y, z = self._fields()
        args = dict(
            csv_path=path, out_dir=folder, x_field=x, y_field=y, z_field=z,
            resolution=0.0 if self.auto_cell.isChecked() else self.cell_spin.value(),
            method=self._method(), options=self._options(), epsg=epsg,
            contour=contour, interval=interval,
            smoothing=self.smooth_spin.value(), fmt=fmt)

        self.log.clear()
        self.log_group.setChecked(True)     # show progress while it runs
        self.bar.setValue(0)
        self._task = CsvDemTask(args)
        self._task.logged.connect(self.log.appendPlainText)
        self._task.progressChanged.connect(
            lambda value: self.bar.setValue(int(value)))
        self._task.jobFinished.connect(self._on_finished)
        self._set_running(True)
        QgsApplication.taskManager().addTask(self._task)

    def _set_running(self, running):
        self.go_btn.setText(tr('Stop') if running else tr('Run'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True)
        for w in (self.csv_widget, self.crs_widget, self.method_combo,
                  self.stack, self.auto_cell, self.cell_spin, self.contour_box,
                  self.folder_widget, self.load_check,
                  *self.field_boxes.values()):
            w.setEnabled(not running)
        if not running:
            self._on_cell_mode()
            self._on_contour_toggled()

    def _on_finished(self, info):
        task, self._task = self._task, None
        if task is not None:
            task.deleteLater()
        if self._closing or self._torn_down:
            self._teardown()
            return
        self._set_running(False)
        if info['stopped']:
            self.log.appendPlainText(tr('Stopped.'))
            self.bar.setValue(0)
            return
        if not info['ok']:
            self.log.appendPlainText(tr('FAILED: {0}').format(info['error']))
            self._warn(info['error'] or tr('The run failed.'))
            return

        result = info['result']
        base = result['base']
        self.bar.setValue(100)
        added = []
        if self.load_check.isChecked():
            rl = QgsRasterLayer(result['dem'], '{0} DEM'.format(base))
            if rl.isValid():
                QgsProject.instance().addMapLayer(rl)
                added.append(rl.name())
            else:
                self.log.appendPlainText(
                    tr('Could not load the DEM: {0}').format(result['dem']))
            if result['contour']:
                name = '{0} Contour {1}m'.format(base, self.interval_spin.value())
                vl = QgsVectorLayer(result['contour'], name, 'ogr')
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
                    added.append(name)
                else:
                    self.log.appendPlainText(tr('Could not load the contours: {0}')
                                             .format(result['contour']))
        self.log.appendPlainText('\n' + tr('Done.'))
        if added:
            self.log.appendPlainText(tr('Added to the map: {0}').format(', '.join(added)))

    # ---------------------------------------------------------------- closing

    def closeEvent(self, event):
        if self._ignore_close:
            event.ignore()
            return
        if self._running():
            self._confirm_stop()
            event.ignore()                  # torn down when the task ends
            return
        self._teardown()
        super().closeEvent(event)

    def done(self, result):
        if self._ignore_close:
            return
        if self._running():
            self._confirm_stop()
            return
        self._teardown()
        super().done(result)

    def _confirm_stop(self):
        answer = QMessageBox.question(
            self, tr(TITLE), tr('Stop the run and close?'))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._closing = True
        self.hide()
        if self._task is not None:
            self._task.cancel()
        return True

    def shutdown(self):
        """Plugin unload: stop any job cleanly, then go away."""
        self._closing = True
        task = self._task
        if task is not None:
            task.cancel()
            with suppress(Exception):
                task.waitForFinished(15000)
        self._task = None
        self._teardown()

    def _teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self._debounce.stop()
        self.deleteLater()

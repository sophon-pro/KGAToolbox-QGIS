# -*- coding: utf-8 -*-
"""Create Points From Table: the window.

UI and wiring only; the reading, typing and point building stay in the
``CreatePointsFromTable`` algorithm, so scripts, the modeller and this window
all go through the same code. The window opens in place of the Toolbox form
(``createCustomParametersWidget``) and is modeless: the algorithm runs on a
worker thread (``QgsProcessingAlgRunnerTask``) and the map stays usable.

Once a table is chosen the window reads it, fills the X / Y / Z / label
drop-downs with its real headers, picks the obvious ones, shows the first rows
with the chosen coordinate columns tinted, and says how many points it will
make, so a wrong column or header row is visible before anything is run.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
from contextlib import suppress

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsProcessingAlgRunnerTask, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject)
from qgis.gui import QgsFileWidget, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .window_reveal import revealed_preview
from ..algorithms import CreatePointsFromTable as T
from ..branding import open_docs

ALG_NAME = 'create_points_from_table'
TITLE = 'Create Points From Table'
TEMP = 'TEMPORARY_OUTPUT'

# Colours sampled from icons/branding/kga.png, as in the other KGA windows.
BLUE = '#174592'
ORANGE = '#FC663A'
RED = '#B3261E'
GREEN = '#1E7B34'

#: Tints for the coordinate columns in the preview.
TINTS = {'X': QColor(210, 226, 255), 'Y': QColor(255, 226, 206),
         'Z': QColor(214, 240, 216)}

PREVIEW_ROWS = 6

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QLabel#kgaNote { font-weight: normal; font-style: italic; }
QLabel#kgaStatus { font-weight: normal; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE}


def tr(text):
    return QgsApplication.translate('KgaCreatePoints', text)


class _Quiet(object):
    """A feedback that says nothing, for reading the table while the form is filled."""

    def pushInfo(self, _text):
        pass

    def pushWarning(self, _text):
        pass

    def setProgress(self, _value):
        pass

    def isCanceled(self):
        return False


class _Feedback(QgsProcessingFeedback):
    """The run's feedback: everything goes to the window's log."""

    logged = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.error = None

    def pushInfo(self, info):
        self.logged.emit(str(info))

    def pushWarning(self, warning):
        self.logged.emit('WARNING: ' + str(warning))

    def pushDebugInfo(self, info):
        pass

    def pushCommandInfo(self, info):
        pass

    def pushConsoleInfo(self, info):
        pass

    def reportError(self, error, fatalError=False):
        if fatalError:
            self.error = str(error)
        self.logged.emit('ERROR: ' + str(error))


def _note(text='', parent=None):
    label = QLabel(text, parent)
    label.setObjectName('kgaNote')
    label.setWordWrap(True)
    return label


@revealed_preview
class CreatePointsDialog(QDialog):
    """A CSV / Excel / ODS table in; a point layer out."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(600)

        self._torn_down = False
        self._ignore_close = False
        self._closing = False
        self._task = None
        self._alg = None
        self._feedback = None
        self._context = None
        self._reader = T.CreatePointsFromTable()
        self._headers = []
        self._rows = []
        self._key = None

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._read_table)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_table_card())
        root.addWidget(self._build_tabs())
        root.addStretch(1)
        root.addLayout(self._build_buttons())

        self._on_crs()
        self._on_file()

    # --------------------------------------------------------------- building

    def _build_tabs(self):
        """One tab per section; the tab area is as tall as the page shown."""
        self.tabs = QTabWidget(self)
        sections = (
            (self._build_coordinate_card(), tr('Coordinates'),
             tr('Which columns are X, Y and Z, and their coordinate system')),
            (self._build_options_card(), tr('Options'),
             tr('Labels, column types and how the file is read')),
            (self._build_output_card(), tr('Output'),
             tr('Where the points go')),
            (self._build_log_card(), tr('Log'),
             tr('Progress and messages from the run')))
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
        if self.isVisible():
            self._later_resize()

    def _card(self, title):
        return QGroupBox(title, self)

    def _build_table_card(self):
        box = self._card(tr('Table'))
        lay = QVBoxLayout(box)

        self.file_widget = QgsFileWidget(box)
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.file_widget.setFilter(
            'All supported (*.csv *.txt *.tsv *.xlsx *.xlsm *.xls *.ods);;'
            'Text and CSV (*.csv *.txt *.tsv);;Excel (*.xlsx *.xlsm *.xls);;'
            'OpenDocument (*.ods);;All files (*.*)')
        self.file_widget.lineEdit().setPlaceholderText(
            tr('Choose a .csv, .txt, .tsv, .xlsx, .xlsm, .xls or .ods file'))
        self.file_widget.setToolTip(tr(
            'The table is read straight from disk; it does not have to be '
            'loaded in QGIS first and is never changed.'))
        self.file_widget.fileChanged.connect(self._on_file)
        lay.addWidget(self.file_widget)

        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.preview = QTableWidget(0, 0, box)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview.verticalHeader().setDefaultSectionSize(22)
        self.preview.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.preview.setFixedHeight(22 * (PREVIEW_ROWS + 1) + 22)
        self.preview.setToolTip(tr(
            'The first rows of the table. The columns chosen for X, Y and Z '
            'are tinted.'))
        lay.addWidget(self.preview)
        return box

    def _combo(self, parent, tip, optional=False):
        combo = QComboBox(parent)
        combo.setToolTip(tip)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
        combo.currentIndexChanged.connect(self._on_columns)
        combo._optional = optional
        return combo

    def _build_coordinate_card(self):
        box = self._card(tr('Coordinates'))
        lay = QVBoxLayout(box)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.field_boxes = {}
        specs = (
            ('X', tr('X  (Easting / Longitude)'), False, tr(
                'Column holding the X coordinate. Picked for you when a '
                'header such as x, easting or lon is found.')),
            ('Y', tr('Y  (Northing / Latitude)'), False, tr(
                'Column holding the Y coordinate. Picked for you when a '
                'header such as y, northing or lat is found.')),
            ('Z', tr('Z  (Elevation, optional)'), True, tr(
                'Choose a column to build a 3D point layer. Leave on (none) '
                'for a flat layer. Rows whose Z will not parse get 0.')))
        for col, (axis, text, optional, tip) in enumerate(specs):
            grid.addWidget(QLabel(text, box), 0, col)
            combo = self._combo(box, tip, optional)
            grid.addWidget(combo, 1, col)
            self.field_boxes[axis] = combo
        lay.addLayout(grid)

        form = QFormLayout()
        self.crs_widget = QgsProjectionSelectionWidget(box)
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem('EPSG:32648'))
        self.crs_widget.setToolTip(tr(
            'The CRS the numbers in the table are already in, not the one '
            'you want to end up in. Nothing is reprojected. EPSG:32648 (WGS 84 '
            '/ UTM 48N) covers most of Cambodia; use EPSG:4326 for degrees.'))
        self.crs_widget.crsChanged.connect(self._on_crs)
        form.addRow(tr('Coordinate system'), self.crs_widget)
        lay.addLayout(form)
        self.crs_note = _note('', box)
        lay.addWidget(self.crs_note)
        return box

    def _build_options_card(self):
        box = self._card(tr('Options'))
        lay = QVBoxLayout(box)

        form = QFormLayout()
        self.label_box = self._combo(box, tr(
            'Label the points with this column. The layer is added with '
            'labelling already on (9 pt, white buffer); restyle it as usual. '
            'Only applies when the result is added to the project.'), True)
        form.addRow(tr('Label points with'), self.label_box)
        lay.addLayout(form)

        self.detect_check = QCheckBox(tr('Detect numeric columns'), box)
        self.detect_check.setChecked(True)
        self.detect_check.setToolTip(tr(
            'Type each column as Integer, Real or Text instead of all Text, '
            'so symbology and the field calculator work at once. Codes with '
            'a leading zero stay Text.'))
        self.skip_check = QCheckBox(tr('Skip rows with unusable coordinates'), box)
        self.skip_check.setChecked(True)
        self.skip_check.setToolTip(tr(
            'Ticked: bad rows are counted and left out. Unticked: the first '
            'bad row stops the run and names the value.'))
        lay.addWidget(self.detect_check)
        lay.addWidget(self.skip_check)

        # Folded away: only needed for files that do not read cleanly.
        self.adv_box = QGroupBox(tr('How the file is read'), box)
        self.adv_box.setCheckable(True)
        self.adv_inner = QWidget(self.adv_box)
        outer = QVBoxLayout(self.adv_box)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.addWidget(self.adv_inner)
        aform = QFormLayout(self.adv_inner)
        aform.setContentsMargins(0, 0, 0, 0)

        self.sheet_box = QComboBox(self.adv_inner)
        self.sheet_box.setEditable(True)
        self.sheet_box.lineEdit().setPlaceholderText(tr('first sheet'))
        self.sheet_box.setToolTip(tr(
            'Worksheet of an Excel / ODS workbook, by name or by position '
            'starting at 1. Ignored for text files.'))
        self.sheet_box.currentTextChanged.connect(self._schedule)
        aform.addRow(tr('Worksheet'), self.sheet_box)

        self.header_spin = QSpinBox(self.adv_inner)
        self.header_spin.setRange(1, 1000)
        self.header_spin.setToolTip(tr(
            'Row the column headers are on. Raise it when the sheet starts '
            'with a title block; everything above is discarded.'))
        self.header_spin.valueChanged.connect(self._schedule)
        aform.addRow(tr('Header row'), self.header_spin)

        self.delim_box = QComboBox(self.adv_inner)
        self.delim_box.addItems(T.DELIMITERS)
        self.delim_box.setToolTip(tr(
            'Auto-detect samples the file. Set it when that guess is wrong. '
            'Ignored for Excel and ODS.'))
        self.delim_box.currentIndexChanged.connect(self._schedule)
        aform.addRow(tr('Column separator'), self.delim_box)

        self.enc_box = QComboBox(self.adv_inner)
        self.enc_box.addItems(T.ENCODINGS)
        self.enc_box.setToolTip(tr(
            'Set it when Khmer or accented text comes out garbled. CSVs saved '
            'from Excel on Windows are usually Windows-1252.'))
        self.enc_box.currentIndexChanged.connect(self._schedule)
        aform.addRow(tr('Character encoding'), self.enc_box)

        self.adv_box.toggled.connect(self._on_adv_toggled)
        self.adv_box.setChecked(False)
        self.adv_inner.setVisible(False)
        lay.addWidget(self.adv_box)
        return box

    def _build_output_card(self):
        box = self._card(tr('Output'))
        form = QFormLayout(box)

        self.out_widget = QgsFileWidget(box)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.out_widget.setFilter('GeoPackage (*.gpkg);;Shapefile (*.shp)')
        self.out_widget.lineEdit().setPlaceholderText(
            tr('Temporary layer (leave empty), or choose a .gpkg / .shp'))
        self.out_widget.setToolTip(tr(
            'Leave empty to check the result as a temporary layer first. '
            'Shapefile truncates field names to 10 characters.'))
        form.addRow(tr('Save to'), self.out_widget)

        self.name_edit = QLineEdit(box)
        self.name_edit.setPlaceholderText(tr('named after the table'))
        self.name_edit.setToolTip(tr('Name of the layer in the Layers panel.'))
        form.addRow(tr('Layer name'), self.name_edit)

        self.load_check = QCheckBox(tr('Add the result to the map'), box)
        self.load_check.setChecked(True)
        form.addRow('', self.load_check)
        return box

    def _build_log_card(self):
        box = self._card(tr('Log'))
        lay = QVBoxLayout(box)
        self.bar = QProgressBar(box)
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(16)
        self.log = QPlainTextEdit(box)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(170)
        lay.addWidget(self.bar)
        lay.addWidget(self.log)
        return box

    def _build_buttons(self):
        lay = QVBoxLayout()
        row = QHBoxLayout()
        self.help_btn = QPushButton(tr('Help'), self)
        self.help_btn.clicked.connect(lambda: open_docs(ALG_NAME))
        self.go_btn = QPushButton(tr('Create Points'), self)
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
        path = parameters.get('INPUT_FILE')
        if isinstance(path, str) and path:
            self.file_widget.setFilePath(path)
            self._on_file()

    def results(self):
        return {}

    # ---------------------------------------------------------------- reacting

    def showEvent(self, event):
        super().showEvent(event)
        self._later_resize()

    def _on_adv_toggled(self, shown):
        self.adv_inner.setVisible(shown)
        self._later_resize()

    def _later_resize(self):
        # After the layout has settled, so the new size hint is the real one.
        QTimer.singleShot(0, self._refit)

    def _refit(self):
        if self._torn_down:
            return
        self.layout().invalidate()
        self.layout().activate()
        # Keep the width the user chose; take the height the content needs.
        self.resize(self.width(), self.layout().totalMinimumSize().height()
                    if self.isMaximized() else self.layout().totalSizeHint().height())

    def _ext(self):
        return os.path.splitext(self.file_widget.filePath().strip())[1].lower()

    def _on_file(self, *_):
        path = self.file_widget.filePath().strip()
        ext = self._ext()
        self.sheet_box.setEnabled(ext in T.EXCEL_EXT + T.ODS_EXT)
        self.delim_box.setEnabled(ext in T.TEXT_EXT)
        self.enc_box.setEnabled(ext in T.TEXT_EXT)
        self._fill_sheets(path, ext)
        self._key = None
        self._schedule()

    def _fill_sheets(self, path, ext):
        current = self.sheet_box.currentText()
        names = []
        if ext in (".xlsx", ".xlsm") and os.path.isfile(path):
            with suppress(Exception):
                import openpyxl
                book = openpyxl.load_workbook(path, read_only=True)
                names = list(book.sheetnames)
                book.close()
        self.sheet_box.blockSignals(True)
        self.sheet_box.clear()
        self.sheet_box.addItems(names)
        self.sheet_box.setCurrentText(current if current in names or current.isdigit() else '')
        self.sheet_box.blockSignals(False)

    def _on_crs(self, *_):
        crs = self.crs_widget.crs()
        if not crs.isValid():
            text = tr('Choose the coordinate system of the table.')
        elif crs.isGeographic():
            text = tr('This CRS is in degrees: X must be longitude and Y latitude.')
        else:
            text = ''
        self.crs_note.setText(text)
        self.crs_note.setVisible(bool(text))
        self._later_resize()

    def _schedule(self, *_):
        self._debounce.start()

    def _opts(self):
        return {
            'sheet': self.sheet_box.currentText().strip(),
            'delimiter': T.DELIMITER_CHARS[self.delim_box.currentIndex()],
            'encoding': T.ENCODING_CODECS[self.enc_box.currentIndex()],
            'header_row': self.header_spin.value(),
        }

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def _read_table(self):
        """Read the chosen table so the window can say what it found."""
        path = self.file_widget.filePath().strip()
        opts = self._opts()
        key = (path, tuple(sorted((k, str(v)) for k, v in opts.items())))
        if key == self._key:
            return
        self._key = key
        self._headers, self._rows = [], []

        if not path:
            self._say(tr('Choose the table to read.'))
        elif not os.path.isfile(path):
            self._say(tr('That file does not exist.'), 'bad')
        elif self._ext() not in T.SUPPORTED_EXT:
            self._say(tr("'{0}' is not a table format this tool reads. "
                         "Supported: {1}.").format(
                             self._ext() or tr('(no extension)'),
                             ' '.join(T.SUPPORTED_EXT)), 'bad')
        else:
            try:
                grid, delimiter = self._reader._read_grid(path, opts, _Quiet())
                if len(grid) < opts['header_row']:
                    raise ValueError(tr('The file has {0} row(s), so there is '
                                        'nothing on header row {1}.').format(
                                            len(grid), opts['header_row']))
                self._headers = T._unique_headers(grid[opts['header_row'] - 1])
                self._rows = [
                    row for row in grid[opts['header_row']:]
                    if any(c is not None and str(c).strip() != '' for c in row)]
            except Exception as exc:                    # noqa: BLE001 - shown to the user
                self._headers, self._rows = [], []
                self._say(str(exc) or exc.__class__.__name__, 'bad')
            else:
                how = ''
                if self._ext() in T.TEXT_EXT:
                    how = tr('  -  separated by {0}').format(
                        {'\t': 'tab', ' ': 'space'}.get(delimiter, "'%s'" % delimiter))
                self._say(tr('{0} rows  -  {1} columns{2}').format(
                    format(len(self._rows), ','), len(self._headers), how), 'good')

        self._fill_columns()
        self._fill_preview()
        self._on_columns()

    def _fill_columns(self):
        """List the headers in every drop-down, keeping a choice that still exists."""
        guesses = {'X': T.X_ALIASES, 'Y': T.Y_ALIASES, 'Z': T.Z_ALIASES}
        boxes = dict(self.field_boxes, L=self.label_box)
        for axis, combo in boxes.items():
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            if combo._optional:
                combo.addItem(tr('(none)'), '')
            for name in self._headers:
                combo.addItem(name, name)
            index = combo.findText(current) if current else -1
            if index < 0 and axis in ('X', 'Y') and self._headers:
                hit = T._match_header('', self._headers, guesses[axis])
                index = combo.findText(hit) if hit else 0
            combo.setCurrentIndex(max(index, 0))
            combo.setEnabled(bool(self._headers))
            combo.blockSignals(False)

    def _fill_preview(self):
        table = self.preview
        table.clear()
        table.setColumnCount(len(self._headers))
        table.setRowCount(min(PREVIEW_ROWS, len(self._rows)))
        table.setHorizontalHeaderLabels(self._headers)
        for r, row in enumerate(self._rows[:PREVIEW_ROWS]):
            for c in range(len(self._headers)):
                value = row[c] if c < len(row) else None
                table.setItem(r, c, QTableWidgetItem('' if value is None else str(value)))
        self._tint()

    def _tint(self):
        """Colour the columns picked for X, Y and Z in the preview."""
        picked = {self.field_boxes[a].currentData(): a for a in 'XYZ'}
        picked.pop('', None)
        picked.pop(None, None)
        for c, name in enumerate(self._headers):
            axis = picked.get(name)
            brush = QBrush(TINTS[axis]) if axis else QBrush()
            head = self.preview.horizontalHeaderItem(c)
            if head is not None:
                head.setText(name if not axis else '{0}  [{1}]'.format(name, axis))
                head.setBackground(brush)
            for r in range(self.preview.rowCount()):
                item = self.preview.item(r, c)
                if item is not None:
                    item.setBackground(brush)

    def _on_columns(self, *_):
        if self._headers:
            self._tint()
            x, y = (self.field_boxes[a].currentData() for a in 'XY')
            if x and x == y:
                self._say(tr('X and Y are the same column; pick two different ones.'), 'bad')
            elif self._rows:
                self._say(tr('{0} rows  -  {1} columns  -  ready to make up to {0} points')
                          .format(format(len(self._rows), ','), len(self._headers)), 'good')

    # -------------------------------------------------------------------- run

    def _warn(self, text):
        QMessageBox.warning(self, tr(TITLE), text)

    def _running(self):
        return self._task is not None

    def _go_or_stop(self):
        if self._running():
            self.go_btn.setEnabled(False)
            self.bar.setFormat(tr('Stopping...') + '  %p%')
            if self._feedback is not None:
                self._feedback.cancel()
            self._task.cancel()
            return
        self._start()

    def _parameters(self):
        out = self.out_widget.filePath().strip()
        return {
            T.CreatePointsFromTable.INPUT_FILE: self.file_widget.filePath().strip(),
            T.CreatePointsFromTable.X_FIELD: self.field_boxes['X'].currentData() or '',
            T.CreatePointsFromTable.Y_FIELD: self.field_boxes['Y'].currentData() or '',
            T.CreatePointsFromTable.Z_FIELD: self.field_boxes['Z'].currentData() or '',
            T.CreatePointsFromTable.CRS: self.crs_widget.crs(),
            T.CreatePointsFromTable.LABEL_FIELD: self.label_box.currentData() or '',
            T.CreatePointsFromTable.DETECT_TYPES: self.detect_check.isChecked(),
            T.CreatePointsFromTable.SKIP_ERRORS: self.skip_check.isChecked(),
            T.CreatePointsFromTable.SHEET: self.sheet_box.currentText().strip(),
            T.CreatePointsFromTable.HEADER_ROW: self.header_spin.value(),
            T.CreatePointsFromTable.DELIMITER: self.delim_box.currentIndex(),
            T.CreatePointsFromTable.ENCODING: self.enc_box.currentIndex(),
            T.CreatePointsFromTable.OUTPUT: out or TEMP,
        }

    def _start(self):
        self._debounce.stop()
        self._read_table()                  # settle any edit still waiting
        path = self.file_widget.filePath().strip()
        if not path or not os.path.isfile(path):
            self._warn(tr('Choose the table first.'))
            return
        if not self._headers or not self._rows:
            self._warn(self.status.text() or tr('The table has no data rows.'))
            return
        if not self.crs_widget.crs().isValid():
            self._warn(tr('Choose the coordinate system of the table.'))
            return
        out = self.out_widget.filePath().strip()
        if out and os.path.exists(out):
            answer = QMessageBox.question(
                self, tr('Overwrite?'),
                tr('{0} already exists. Replace it?').format(os.path.basename(out)))
            if answer != QMessageBox.StandardButton.Yes:
                return

        alg = T.CreatePointsFromTable().create()
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        params = self._parameters()
        ok, problem = alg.checkParameterValues(params, context)
        if not ok:
            self._warn(problem)
            return

        self._alg, self._context = alg, context
        self._feedback = _Feedback()
        self._feedback.logged.connect(self.log.appendPlainText)
        self._feedback.progressChanged.connect(lambda v: self.bar.setValue(int(v)))

        self.log.clear()
        self.tabs.setCurrentIndex(self.tabs.count() - 1)    # the Log tab shows progress
        self.bar.setFormat('%p%')
        self.bar.setValue(0)
        self._task = QgsProcessingAlgRunnerTask(alg, params, context, self._feedback)
        self._task.executed.connect(self._on_finished)
        self._set_running(True)
        QgsApplication.taskManager().addTask(self._task)

    def _set_running(self, running):
        self.go_btn.setText(tr('Stop') if running else tr('Create Points'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True)
        for w in (self.file_widget, self.crs_widget, self.out_widget,
                  self.name_edit, self.load_check, self.detect_check,
                  self.skip_check, self.adv_box, self.label_box,
                  *self.field_boxes.values()):
            w.setEnabled(not running)

    def _on_finished(self, ok, results):
        task, self._task = self._task, None
        if task is not None:
            task.deleteLater()
        if self._closing or self._torn_down:
            self._teardown()
            return
        feedback, self._feedback = self._feedback, None
        self._set_running(False)
        if feedback is not None and feedback.isCanceled():
            self.log.appendPlainText(tr('Stopped.'))
            self.bar.setValue(0)
            return
        if not ok or not results:
            message = (feedback.error if feedback is not None else None) \
                or tr('The run failed. See the log.')
            self._warn(message)
            return

        self.bar.setValue(100)
        if self.load_check.isChecked():
            self._load_results(feedback)
        else:
            self.log.appendPlainText('\n' + tr('Done. The result was not added to the map.'))

    def _load_results(self, feedback):
        """Name the output layer and hand it to Processing to load (labels included)."""
        context = self._context
        name = self.name_edit.text().strip() or os.path.splitext(
            os.path.basename(self.file_widget.filePath().strip()))[0]
        for layer_id in list(context.layersToLoadOnCompletion()):
            context.layerToLoadOnCompletionDetails(layer_id).name = name
        try:
            from processing.gui.Postprocessing import handleAlgorithmResults
            handleAlgorithmResults(self._alg, context, feedback, False)
        except Exception as exc:                        # noqa: BLE001 - shown to the user
            self.log.appendPlainText(tr('Could not add the layer: {0}').format(exc))
            return
        self.log.appendPlainText('\n' + tr("Added to the map: {0}").format(name))

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
        if self._feedback is not None:
            self._feedback.cancel()
        if self._task is not None:
            self._task.cancel()
        return True

    def shutdown(self):
        """Plugin unload: stop any job cleanly, then go away."""
        self._closing = True
        task = self._task
        if task is not None:
            if self._feedback is not None:
                self._feedback.cancel()
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

# -*- coding: utf-8 -*-
"""Export Attributes to Excel, and Import Attributes from Excel: the windows.

UI and wiring only; the work is ``attributes_xlsx`` / ``core.xlsx_io``.

Export lists the layer's fields with a tick box each. Import opens already
filled in from the last export (workbook and layer), reads the workbook so it
can say how many rows and which columns it holds and whether it came from the
layer chosen, and defaults to a dry run: the button says Compare until the dry
run is switched off, and then asks before it writes.
"""

import os

from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel,
                                 QMessageBox, QVBoxLayout)

from .layer_checklist import LayerChecklist
from .tabbed_run_dialog import GREEN, RED, TabbedRunDialog, note, tr
from ..algorithms import attributes_xlsx as A
from ..core import xlsx_io
from ..core.modify_features import layer_filter


def _starting_folder(layer):
    """A folder to suggest next to the layer's data, else the project, else Documents."""
    if layer is not None:
        source = (layer.source() or '').split('|', 1)[0]
        if source and os.path.isfile(source):
            return os.path.dirname(source)
    home = QgsProject.instance().homePath()
    return home if home and os.path.isdir(home) else os.path.expanduser('~/Documents')


def _safe(name):
    return ''.join(c if c.isalnum() or c in ' _-' else '_' for c in name).strip() or 'layer'


class _Base(TabbedRunDialog):

    def status_style(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')


class ExportAttributesDialog(_Base):
    """A layer in; a workbook a colleague can edit out."""

    TITLE = 'Export Attributes to Excel'
    ALG_NAME = 'attributes_to_xlsx'
    RUN_TEXT = 'Export'

    def algorithm_class(self):
        return A.AttributesToXlsxAlgorithm

    def init_state(self):
        self._built = False             # signals fired while the widgets are made are ignored
        self._suggested = ''

    def build_top(self):
        box = self.card(tr('Layer'))
        lay = QVBoxLayout(box)
        self.layer_box = QgsMapLayerComboBox(box)
        self.layer_box.setFilters(layer_filter('VectorLayer'))
        self.layer_box.setToolTip(tr('The layer whose attributes are written to the workbook.'))
        self.layer_box.layerChanged.connect(self._on_layer)
        lay.addWidget(self.layer_box)
        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        return box

    def build_sections(self):
        fields = self.card()
        flay = QVBoxLayout(fields)
        form = QFormLayout()
        self.key_box = QgsFieldComboBox(fields)
        self.key_box.setAllowEmptyFieldName(True)
        self.key_box.setToolTip(tr(
            'Identifies each row, so the import can match it back. Leave empty '
            'to use the layer\'s primary key (fid).'))
        form.addRow(tr('Key field'), self.key_box)
        flay.addLayout(form)
        self.checklist = LayerChecklist(fields, height=130)
        self.checklist.setToolTip(tr('Tick the fields to export. All ticked = every field.'))
        flay.addWidget(self.checklist)

        opts = self.card()
        oform = QFormLayout(opts)
        self.selected_check = QCheckBox(tr('Selected features only'), opts)
        self.domain_check = QCheckBox(tr('Turn value maps into Excel dropdowns'), opts)
        self.domain_check.setChecked(True)
        self.domain_check.setToolTip(tr(
            'Fields with a value map (a field domain, or set by hand) become '
            'drop-down lists in Excel, so the data comes back valid.'))
        self.open_check = QCheckBox(tr('Open the workbook when it is written'), opts)
        self.open_check.setChecked(True)
        for w in (self.selected_check, self.domain_check, self.open_check):
            oform.addRow(w)
        oform.addRow(note(tr(
            'The key column is shaded and locked, because an edited key is a '
            'row that matches nothing on the way back. Import Attributes from '
            'Excel offers this workbook and layer automatically.'), opts))

        out = self.card()
        uform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.out_widget.setFilter('Excel workbook (*.xlsx)')
        self.out_widget.lineEdit().setPlaceholderText(tr('The workbook to write'))
        uform.addRow(tr('Workbook'), self.out_widget)
        return [(fields, tr('Fields'), tr('The key column and the fields to export')),
                (opts, tr('Options'), tr('Selection, dropdowns and opening')),
                (out, tr('Output'), tr('Where the workbook goes'))]

    def after_build(self):
        self._built = True
        self._on_layer(self.layer_box.currentLayer())

    def locked_widgets(self):
        return [self.layer_box, self.key_box, self.checklist, self.selected_check,
                self.domain_check, self.open_check, self.out_widget]

    def _layer(self):
        layer = self.layer_box.currentLayer()
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _on_layer(self, layer=None):
        if not self._built:
            return
        layer = self._layer()
        self.key_box.setLayer(layer)
        self.checklist.clear()
        if layer is None:
            self.status_style(tr('Choose a layer.'))
        else:
            self.checklist.set_items(
                [(f.name(), '{0}   ({1})'.format(f.name(), f.typeName()))
                 for f in layer.fields()])
            selected = layer.selectedFeatureCount()
            self.status_style(tr('{0} features  -  {1} fields  -  {2} selected').format(
                format(layer.featureCount(), ','), layer.fields().count(),
                selected), 'good')
            current = self.out_widget.filePath().strip()
            if not current or current == self._suggested:
                self._suggested = os.path.join(_starting_folder(layer),
                                               _safe(layer.name()) + '.xlsx')
                self.out_widget.setFilePath(self._suggested)
        self.later_resize()

    def problem(self):
        layer = self._layer()
        if layer is None:
            return tr('Choose a layer first.')
        if not self.checklist.checked():
            return tr('Tick at least one field.')
        if self.selected_check.isChecked() and not layer.selectedFeatureCount():
            return tr('Nothing is selected in "{0}".').format(layer.name())
        out = self.out_widget.filePath().strip()
        if not out:
            return tr('Choose the workbook to write.')
        if not out.lower().endswith('.xlsx'):
            out += '.xlsx'
            self.out_widget.setFilePath(out)
        if os.path.exists(out):
            answer = QMessageBox.question(
                self, tr(self.TITLE),
                tr('{0} already exists. Replace it?').format(os.path.basename(out)))
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return None

    def parameters(self):
        K = A.AttributesToXlsxAlgorithm
        return {
            K.INPUT: self._layer().id(),
            K.KEY_FIELD: self.key_box.currentField() or '',
            K.FIELDS: [] if not self.checklist.only() else self.checklist.checked(),
            K.SELECTED_ONLY: self.selected_check.isChecked(),
            K.INCLUDE_DOMAINS: self.domain_check.isChecked(),
            K.OPEN_AFTER: self.open_check.isChecked(),
            K.OUTPUT: self.out_widget.filePath().strip(),
        }

    def done_ok(self, results):
        K = A.AttributesToXlsxAlgorithm
        self.log.appendPlainText('\n' + tr('Done. {0} row(s) in {1}').format(
            results.get(K.ROW_COUNT, '?'), results.get(K.OUTPUT, '')))


class ImportAttributesDialog(_Base):
    """A workbook in; its edited values applied back onto the layer."""

    TITLE = 'Import Attributes from Excel'
    ALG_NAME = 'xlsx_to_attributes'
    RUN_TEXT = 'Compare'

    def algorithm_class(self):
        return A.XlsxToAttributesAlgorithm

    def init_state(self):
        self._built = False
        self._rows = []
        self._meta = {}
        self._columns = []
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._read_workbook)

    def build_top(self):
        box = self.card(tr('Workbook and layer'))
        lay = QVBoxLayout(box)
        form = QFormLayout()

        self.book_widget = QgsFileWidget(box)
        self.book_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.book_widget.setFilter('Excel workbook (*.xlsx);;All files (*.*)')
        self.book_widget.lineEdit().setPlaceholderText(tr('Choose the edited workbook'))
        self.book_widget.setToolTip(tr(
            'Filled in from your last export, so the usual round trip needs no browsing.'))
        self.book_widget.fileChanged.connect(lambda *_: self._debounce.start())
        form.addRow(tr('Workbook'), self.book_widget)

        self.layer_box = QgsMapLayerComboBox(box)
        self.layer_box.setFilters(layer_filter('VectorLayer'))
        self.layer_box.setToolTip(tr('The layer the values are written onto.'))
        self.layer_box.layerChanged.connect(self._on_layer)
        form.addRow(tr('Target layer'), self.layer_box)
        lay.addLayout(form)

        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.warning = QLabel('', box)
        self.warning.setObjectName('kgaNote')
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet('color: %s;' % RED)
        lay.addWidget(self.warning)

        remembered = xlsx_io.last_export()
        if remembered.get('path'):
            self.book_widget.setFilePath(remembered['path'])
        layer = QgsProject.instance().mapLayer(remembered.get('layer_id') or '')
        if layer is not None:
            self.layer_box.setLayer(layer)
        return box

    def build_sections(self):
        match = self.card()
        mlay = QVBoxLayout(match)
        form = QFormLayout()
        self.key_box = QgsFieldComboBox(match)
        self.key_box.setAllowEmptyFieldName(True)
        self.key_box.setToolTip(tr(
            'Matches workbook rows to features. Left empty, the key recorded '
            'in the workbook is used.'))
        form.addRow(tr('Key field'), self.key_box)
        self.missing_box = QComboBox(match)
        self.missing_box.addItems([tr(o) for o in A.XlsxToAttributesAlgorithm.MISSING_OPTIONS])
        self.missing_box.setCurrentIndex(A.MISSING_REPORT)
        self.missing_box.setToolTip(tr('What to do with a workbook row whose key is not in the layer.'))
        form.addRow(tr('Rows with no matching feature'), self.missing_box)
        mlay.addLayout(form)
        self.checklist = LayerChecklist(match, height=110)
        self.checklist.setToolTip(tr('Tick the columns to apply. All ticked = every column the layer has.'))
        mlay.addWidget(self.checklist)

        opts = self.card()
        oform = QFormLayout(opts)
        self.dates_check = QCheckBox(tr('Read bare numbers in date fields as Excel day serials'), opts)
        self.dates_check.setChecked(True)
        self.dates_check.setToolTip(tr('A date turned into the serial 45231 is decoded.'))
        self.dry_check = QCheckBox(tr('Dry run (report only, write nothing)'), opts)
        self.dry_check.setChecked(True)
        self.dry_check.setToolTip(tr(
            'Reports per-field counts and old / new values without changing '
            'the layer. Turn off deliberately, after reading the report.'))
        self.dry_check.toggled.connect(lambda *_: self.refresh_run_text())
        oform.addRow(self.dates_check)
        oform.addRow(self.dry_check)
        oform.addRow(note(tr('The target must have no unsaved edits.'), opts))

        out = self.card()
        uform = QFormLayout(out)
        self.report_widget = QgsFileWidget(out)
        self.report_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.report_widget.setFilter('HTML files (*.html)')
        self.report_widget.lineEdit().setPlaceholderText(tr('Optional: save the change report as HTML'))
        uform.addRow(tr('Change report'), self.report_widget)
        return [(match, tr('Matching'), tr('Key field, missing rows and columns to apply')),
                (opts, tr('Options'), tr('Dates and dry run')),
                (out, tr('Output'), tr('An optional saved report'))]

    def after_build(self):
        self._built = True
        self._on_layer()
        self._read_workbook()

    def run_text(self):
        return 'Compare' if self.dry_check.isChecked() else 'Import'

    def locked_widgets(self):
        return [self.book_widget, self.layer_box, self.key_box, self.missing_box,
                self.checklist, self.dates_check, self.dry_check, self.report_widget]

    def _layer(self):
        layer = self.layer_box.currentLayer()
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _on_layer(self, *_):
        if not self._built:
            return
        self.key_box.setLayer(self._layer())
        self._fill_columns()
        self._check_match()

    def _read_workbook(self):
        if not self._built:
            return
        path = self.book_widget.filePath().strip()
        self._rows, self._meta = [], {}
        if not path:
            self.status_style(tr('Choose the workbook to import.'))
        elif not os.path.isfile(path):
            self.status_style(tr('That file does not exist.'), 'bad')
        else:
            try:
                self._rows, self._meta = xlsx_io.read_workbook(path)
            except Exception as exc:                    # noqa: BLE001 - shown to the user
                self.status_style(str(exc), 'bad')
            else:
                if not self._rows:
                    self.status_style(tr('The workbook has no data rows.'), 'bad')
                else:
                    came = self._meta.get('layer_name')
                    self.status_style(tr('{0} rows  -  {1} columns{2}').format(
                        format(len(self._rows), ','), len(self._rows[0]),
                        tr('  -  exported from "{0}"').format(came) if came else ''), 'good')
        self._fill_columns()
        self._check_match()
        self.later_resize()

    def _fill_columns(self):
        layer = self._layer()
        self._columns = []
        if self._rows and layer is not None:
            key = self.key_box.currentField() or self._meta.get('key_field')
            self._columns = [c for c in self._rows[0]
                             if c != key and layer.fields().indexOf(c) >= 0]
        self.checklist.set_items([(c, c) for c in self._columns]) if self._columns \
            else self.checklist.clear()

    def _check_match(self):
        layer = self._layer()
        text = ''
        if layer is not None and self._rows:
            text = '\n'.join(xlsx_io.describe_mismatch(self._meta, layer))
        self.warning.setText(text)
        self.warning.setVisible(bool(text))
        self.later_resize()

    def problem(self):
        self._debounce.stop()
        self._read_workbook()
        path = self.book_widget.filePath().strip()
        if not path or not os.path.isfile(path):
            return tr('Choose the workbook first.')
        if not self._rows:
            return self.status.text()
        layer = self._layer()
        if layer is None:
            return tr('Choose the target layer.')
        if not self._columns:
            return tr("None of the workbook's columns matches a field of the layer.")
        if not self.checklist.checked():
            return tr('Tick at least one column to apply.')
        if layer.isEditable() and layer.isModified():
            return tr('"{0}" has unsaved edits. Save or discard them first.').format(layer.name())
        if not self.dry_check.isChecked():
            answer = QMessageBox.question(
                self, tr(self.TITLE),
                tr('This writes the workbook\'s values into "{0}". Run a dry run '
                   'first if you have not read the report.\n\nWrite the changes now?')
                .format(layer.name()))
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return None

    def parameters(self):
        X = A.XlsxToAttributesAlgorithm
        params = {
            X.INPUT_XLSX: self.book_widget.filePath().strip(),
            X.TARGET: self._layer().id(),
            X.KEY_FIELD: self.key_box.currentField() or '',
            X.FIELDS: [] if not self.checklist.only() else self.checklist.checked(),
            X.ON_MISSING: self.missing_box.currentIndex(),
            X.EXCEL_DATES: self.dates_check.isChecked(),
            X.DRY_RUN: self.dry_check.isChecked(),
        }
        report = self.report_widget.filePath().strip()
        if report:
            params[X.OUTPUT_HTML] = report
        return params

    def setParameters(self, parameters):
        path = parameters.get(A.XlsxToAttributesAlgorithm.INPUT_XLSX)
        if isinstance(path, str) and path:
            self.book_widget.setFilePath(path)
            self._read_workbook()

    def done_ok(self, results):
        changed = results.get(A.XlsxToAttributesAlgorithm.CHANGED, 0)
        if self.dry_check.isChecked():
            self.log.appendPlainText('\n' + tr(
                'Dry run: {0} value(s) would change. Nothing was written. '
                'Untick "Dry run" to apply them.').format(changed))
        else:
            self.log.appendPlainText('\n' + tr('Done. {0} value(s) written.').format(changed))

    def _teardown(self):
        self._debounce.stop()
        super()._teardown()

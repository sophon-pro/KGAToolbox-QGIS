# -*- coding: utf-8 -*-
"""Shapefiles to GeoPackage: the window.

UI and wiring only; the packing is the ``ShapefilesToGpkg`` algorithm. Choosing
a folder lists the files that will be packed (honouring the extensions and the
subfolder option), and suggests a GeoPackage named after the folder.
"""

import os

from qgis.core import QgsCoordinateReferenceSystem
from qgis.gui import QgsFileWidget, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QVBoxLayout)

from .tabbed_run_dialog import GREEN, RED, TabbedRunDialog, tr
from ..algorithms.shapefiles_to_gpkg import ShapefilesToGpkg as Alg

ENCODINGS = ['UTF-8', 'Windows-1252', 'Windows-874', 'UTF-16', 'ISO-8859-1']
MAX_LISTED = 500


class ShpToGpkgDialog(TabbedRunDialog):
    """A folder of shapefiles in; one GeoPackage out."""

    TITLE = 'Shapefiles to GeoPackage'
    ALG_NAME = 'shapefilestogpkg'
    RUN_TEXT = 'Pack'

    def algorithm_class(self):
        return Alg

    def init_state(self):
        self._suggested = ''
        self._files = []
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._scan)

    # --------------------------------------------------------------- building

    def build_top(self):
        box = self.card(tr('Shapefiles'))
        lay = QVBoxLayout(box)

        self.in_widget = QgsFileWidget(box)
        self.in_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.in_widget.lineEdit().setPlaceholderText(
            tr('Choose the folder that holds the shapefiles'))
        self.in_widget.setToolTip(tr('Every matching file in this folder becomes a layer.'))
        self.in_widget.fileChanged.connect(self._on_input)
        lay.addWidget(self.in_widget)

        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.file_list = QListWidget(box)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.file_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.file_list.setFixedHeight(110)
        self.file_list.setToolTip(tr('The files that will be packed.'))
        lay.addWidget(self.file_list)
        return box

    def build_sections(self):
        # Options
        opts = self.card()
        form = QFormLayout(opts)

        self.ext_edit = QLineEdit('shp', opts)
        self.ext_edit.setToolTip(tr(
            'File extensions to pack, separated by commas (for example '
            'shp, geojson). Matching ignores case.'))
        self.ext_edit.textChanged.connect(self._on_input)
        form.addRow(tr('File extensions'), self.ext_edit)

        self.enc_box = QComboBox(opts)
        self.enc_box.setEditable(True)
        self.enc_box.addItems(ENCODINGS)
        self.enc_box.setToolTip(tr(
            'Encoding of the source attributes. QGIS falls back to the system '
            'encoding when a .cpg file is missing, the usual cause of '
            'unreadable Khmer text. The GeoPackage itself is always UTF-8.'))
        form.addRow(tr('Source encoding'), self.enc_box)

        self.crs_widget = QgsProjectionSelectionWidget(opts)
        options = getattr(QgsProjectionSelectionWidget, 'CrsOption', QgsProjectionSelectionWidget)
        self.crs_widget.setOptionVisible(options.CrsNotSet, True)
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem())
        self.crs_widget.setToolTip(tr(
            'Leave as Not set to keep each layer in its own CRS. Pick a CRS '
            'to reproject every layer to it on the way in.'))
        form.addRow(tr('Reproject all layers to'), self.crs_widget)

        self.recursive_check = QCheckBox(tr('Search subfolders'), opts)
        self.recursive_check.toggled.connect(self._on_input)
        form.addRow('', self.recursive_check)
        self.skip_check = QCheckBox(tr('Skip layers with no features'), opts)
        form.addRow('', self.skip_check)

        # Output
        out = self.card()
        oform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.out_widget.setFilter('GeoPackage (*.gpkg)')
        self.out_widget.lineEdit().setPlaceholderText(tr('The GeoPackage to write'))
        self.out_widget.setToolTip(tr(
            'Each file becomes a layer named after the file. Names are made '
            'safe for a GeoPackage and duplicates get a number.'))
        oform.addRow(tr('GeoPackage'), self.out_widget)
        self.append_check = QCheckBox(
            tr('Add to the GeoPackage if it exists (keep current layers)'), out)
        self.append_check.setToolTip(tr(
            'Unticked: an existing file is replaced. Ticked: the layers are '
            'added to it, and a layer with the same name is overwritten.'))
        oform.addRow('', self.append_check)
        return [(opts, tr('Options'), tr('Which files, encoding, CRS and what to skip')),
                (out, tr('Output'), tr('Where the GeoPackage goes'))]

    def after_build(self):
        self._scan()

    def locked_widgets(self):
        return [self.in_widget, self.out_widget, self.ext_edit, self.enc_box,
                self.crs_widget, self.recursive_check, self.skip_check,
                self.append_check]

    # --------------------------------------------------------------- reacting

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def _on_input(self, *_):
        self._debounce.start()

    def _extensions(self):
        exts = {e.strip().lower().lstrip('.')
                for e in self.ext_edit.text().split(',') if e.strip()}
        return exts or {'shp'}

    def _scan(self):
        folder = self.in_widget.filePath().strip()
        self.file_list.clear()
        self._files = []
        if not folder:
            self._say(tr('Choose the folder to pack.'))
        elif not os.path.isdir(folder):
            self._say(tr('That folder does not exist.'), 'bad')
        else:
            try:
                self._files = Alg().collect_files(
                    folder, self._extensions(), self.recursive_check.isChecked())
            except OSError as exc:
                self._say(str(exc), 'bad')
            else:
                for path in self._files[:MAX_LISTED]:
                    self.file_list.addItem(os.path.relpath(path, folder))
                if len(self._files) > MAX_LISTED:
                    self.file_list.addItem(tr('... and {0} more').format(
                        len(self._files) - MAX_LISTED))
                if self._files:
                    self._say(tr('{0} file(s) will be packed').format(
                        len(self._files)), 'good')
                else:
                    self._say(tr('No file with extension {0} in this folder.').format(
                        ', '.join(sorted(self._extensions()))), 'bad')
            self._suggest_output(folder)
        self.later_resize()

    def _suggest_output(self, folder):
        """Offer <folder>/<folder name>.gpkg, unless the user chose one."""
        current = self.out_widget.filePath().strip()
        if current and current != self._suggested:
            return
        name = os.path.basename(os.path.normpath(folder)) or 'shapefiles'
        self._suggested = os.path.join(folder, name + '.gpkg')
        self.out_widget.setFilePath(self._suggested)

    # -------------------------------------------------------------------- run

    def problem(self):
        self._debounce.stop()
        self._scan()
        folder = self.in_widget.filePath().strip()
        if not folder or not os.path.isdir(folder):
            return tr('Choose the folder of shapefiles first.')
        if not self._files:
            return self.status.text()
        out = self.out_widget.filePath().strip()
        if not out:
            return tr('Choose the GeoPackage to write.')
        if not out.lower().endswith('.gpkg'):
            out += '.gpkg'
            self.out_widget.setFilePath(out)
        if os.path.exists(out) and not self.append_check.isChecked():
            answer = QMessageBox.question(
                self, tr(self.TITLE),
                tr('{0} already exists and will be replaced. Continue?').format(
                    os.path.basename(out)))
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return None

    def parameters(self):
        return {
            Alg.INPUT: self.in_widget.filePath().strip(),
            Alg.OUTPUT: self.out_widget.filePath().strip(),
            Alg.EXTENSIONS: ','.join(sorted(self._extensions())),
            Alg.ENCODING: self.enc_box.currentText().strip() or 'UTF-8',
            Alg.TARGET_CRS: self.crs_widget.crs(),
            Alg.RECURSIVE: self.recursive_check.isChecked(),
            Alg.APPEND: self.append_check.isChecked(),
            Alg.SKIP_EMPTY: self.skip_check.isChecked(),
        }

    def setParameters(self, parameters):
        folder = parameters.get(Alg.INPUT)
        if isinstance(folder, str) and folder:
            self.in_widget.setFilePath(folder)
            self._scan()
        out = parameters.get(Alg.OUTPUT)
        if isinstance(out, str) and out and out != 'TEMPORARY_OUTPUT':
            self.out_widget.setFilePath(out)

    def done_ok(self, results):
        self.log.appendPlainText('\n' + tr('Done. GeoPackage: {0}').format(
            results.get(Alg.OUTPUT, '')))

    def _teardown(self):
        self._debounce.stop()
        super()._teardown()

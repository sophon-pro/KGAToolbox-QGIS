# -*- coding: utf-8 -*-
"""GeoPackage to Shapefiles: the window.

UI and wiring only; the export is the ``GpkgToShapefiles`` algorithm. Choosing
a GeoPackage lists the layers inside it (name, geometry, feature count), marks
the tables that have no geometry (they cannot become shapefiles), and suggests
an output folder next to the file.
"""

import os

from qgis.core import QgsProviderRegistry, QgsWkbTypes
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QVBoxLayout)

from .tabbed_run_dialog import GREEN, RED, TabbedRunDialog, geometry_kind, note, tr
from ..algorithms.gpkg_to_shapefiles import GpkgToShapefiles as Alg

ENCODINGS = ['UTF-8', 'Windows-1252', 'Windows-874', 'UTF-16', 'ISO-8859-1']


class GpkgToShpDialog(TabbedRunDialog):
    """A GeoPackage in; one shapefile per spatial layer out."""

    TITLE = 'GeoPackage to Shapefiles'
    ALG_NAME = 'gpkgtoshapefiles'
    RUN_TEXT = 'Export'

    def algorithm_class(self):
        return Alg

    def init_state(self):
        self._suggested = ''
        self._layers = []
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._read_layers)

    # --------------------------------------------------------------- building

    def build_top(self):
        box = self.card(tr('GeoPackage'))
        lay = QVBoxLayout(box)

        self.in_widget = QgsFileWidget(box)
        self.in_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.in_widget.setFilter('GeoPackage (*.gpkg *.GPKG);;All files (*.*)')
        self.in_widget.lineEdit().setPlaceholderText(tr('Choose a .gpkg file'))
        self.in_widget.setToolTip(tr('Every spatial layer in this file becomes a shapefile.'))
        self.in_widget.fileChanged.connect(self._on_file)
        lay.addWidget(self.in_widget)

        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.layer_list = QListWidget(box)
        self.layer_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.layer_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.layer_list.setFixedHeight(110)
        self.layer_list.setToolTip(tr('The layers found in the GeoPackage.'))
        lay.addWidget(self.layer_list)
        return box

    def build_sections(self):
        # Options
        opts = self.card()
        form = QFormLayout(opts)
        self.enc_box = QComboBox(opts)
        self.enc_box.setEditable(True)
        self.enc_box.addItems(ENCODINGS)
        self.enc_box.setToolTip(tr(
            'Character encoding written into the shapefiles (.cpg). UTF-8 '
            'keeps Khmer and accented text readable.'))
        form.addRow(tr('Field encoding'), self.enc_box)

        self.prefix_edit = QLineEdit(opts)
        self.prefix_edit.setPlaceholderText(tr('none'))
        self.prefix_edit.setToolTip(tr('Text put in front of every file name.'))
        form.addRow(tr('File name prefix'), self.prefix_edit)

        self.skip_check = QCheckBox(tr('Skip layers with no features'), opts)
        self.skip_check.setToolTip(tr('Empty layers are not exported.'))
        form.addRow('', self.skip_check)
        self.over_check = QCheckBox(tr('Overwrite existing shapefiles'), opts)
        self.over_check.setChecked(True)
        self.over_check.setToolTip(tr(
            'Unticked: a layer whose shapefile already exists is skipped.'))
        form.addRow('', self.over_check)
        form.addRow(note(tr(
            'Shapefiles cut field names to 10 characters and cannot hold mixed '
            'geometry types. Keep the GeoPackage as the master copy.'), opts))

        # Output
        out = self.card()
        oform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.out_widget.lineEdit().setPlaceholderText(tr('Folder for the shapefiles'))
        self.out_widget.setToolTip(tr(
            'One .shp (with its sidecar files) per layer is written here. '
            'The folder is created if it does not exist.'))
        oform.addRow(tr('Folder'), self.out_widget)
        return [(opts, tr('Options'), tr('Encoding, file names and what to skip')),
                (out, tr('Output'), tr('Where the shapefiles go'))]

    def after_build(self):
        self._read_layers()

    def locked_widgets(self):
        return [self.in_widget, self.out_widget, self.enc_box, self.prefix_edit,
                self.skip_check, self.over_check]

    # --------------------------------------------------------------- reacting

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def _on_file(self, *_):
        self._debounce.start()

    def _read_layers(self):
        path = self.in_widget.filePath().strip()
        self.layer_list.clear()
        self._layers = []
        if not path:
            self._say(tr('Choose the GeoPackage to export.'))
        elif not os.path.isfile(path):
            self._say(tr('That file does not exist.'), 'bad')
        else:
            try:
                meta = QgsProviderRegistry.instance().providerMetadata('ogr')
                subs = meta.querySublayers(path) if meta else []
                if not subs:
                    self._say(tr('No layers were found in this file.'), 'bad')
            except Exception as exc:                    # noqa: BLE001 - shown to the user
                subs = []
                self._say(str(exc), 'bad')
            spatial = 0
            for sub in subs:
                has_geom = geometry_kind(sub.wkbType()) != 'none'
                spatial += has_geom
                count = sub.featureCount()
                text = '{0}   -   {1}, {2}'.format(
                    sub.name(),
                    QgsWkbTypes.displayString(sub.wkbType()) if has_geom
                    else tr('table, will be skipped'),
                    tr('{0} features').format(format(count, ',')) if count >= 0 else '?')
                item = QListWidgetItem(text)
                if not has_geom:
                    item.setForeground(self.palette().mid())
                self.layer_list.addItem(item)
                self._layers.append(sub.name())
            if subs:
                self._say(tr('{0} layers found  -  {1} will be exported').format(
                    len(subs), spatial), 'good')
            self._suggest_folder(path)
        self.later_resize()

    def _suggest_folder(self, gpkg):
        """Offer <folder of the file>/<file name>_shp, unless the user chose one."""
        current = self.out_widget.filePath().strip()
        if current and current != self._suggested:
            return
        stem = os.path.splitext(os.path.basename(gpkg))[0]
        self._suggested = os.path.join(os.path.dirname(gpkg), stem + '_shp')
        self.out_widget.setFilePath(self._suggested)

    # -------------------------------------------------------------------- run

    def problem(self):
        self._debounce.stop()
        path = self.in_widget.filePath().strip()
        if not path or not os.path.isfile(path):
            return tr('Choose the GeoPackage first.')
        if not self._layers:
            self._read_layers()
        if not self._layers:
            return tr('No layers were found in this GeoPackage.')
        out = self.out_widget.filePath().strip()
        if not out:
            return tr('Choose the output folder.')
        if os.path.exists(out) and not os.path.isdir(out):
            return tr('The output path is a file, not a folder.')
        return None

    def parameters(self):
        return {
            Alg.INPUT: self.in_widget.filePath().strip(),
            Alg.OUTPUT: self.out_widget.filePath().strip(),
            Alg.ENCODING: self.enc_box.currentText().strip() or 'UTF-8',
            Alg.PREFIX: self.prefix_edit.text().strip(),
            Alg.SKIP_EMPTY: self.skip_check.isChecked(),
            Alg.OVERWRITE: self.over_check.isChecked(),
        }

    def setParameters(self, parameters):
        path = parameters.get(Alg.INPUT)
        if isinstance(path, str) and path:
            self.in_widget.setFilePath(path)
            self._read_layers()
        out = parameters.get(Alg.OUTPUT)
        if isinstance(out, str) and out and out != 'TEMPORARY_OUTPUT':
            self.out_widget.setFilePath(out)

    def done_ok(self, results):
        self.log.appendPlainText('\n' + tr('Done. Shapefiles are in {0}').format(
            results.get(Alg.OUTPUT, '')))

    def _teardown(self):
        self._debounce.stop()
        super()._teardown()

# -*- coding: utf-8 -*-
"""File Geodatabase to GeoPackage, and GeoPackage to File Geodatabase: the windows.

UI and wiring only; the conversion is ``gdb_gpkg`` / ``core.gdb_convert``.
Choosing the source lists its layers with a tick box each, so a subset can be
converted without typing names; every layer ticked means "all of them".
"""

import os

from qgis.core import QgsProviderRegistry, QgsWkbTypes
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel,
                                 QLineEdit, QMessageBox, QVBoxLayout)

from .layer_checklist import LayerChecklist
from .tabbed_run_dialog import GREEN, RED, TabbedRunDialog, geometry_kind, note, tr
from ..algorithms import gdb_gpkg as G


def list_layers(path):
    """[(name, text)] for every layer and table in `path`, or raise."""
    meta = QgsProviderRegistry.instance().providerMetadata('ogr')
    out = []
    for sub in (meta.querySublayers(path) if meta else []):
        spatial = geometry_kind(sub.wkbType()) != 'none'
        count = sub.featureCount()
        out.append((sub.name(), '{0}   -   {1}, {2}'.format(
            sub.name(),
            QgsWkbTypes.displayString(sub.wkbType()) if spatial else tr('table'),
            tr('{0} features').format(format(count, ',')) if count >= 0 else '?')))
    return out


class _GdbDialog(TabbedRunDialog):
    """What the two directions share: a source box with a layer checklist."""

    SOURCE_BOX = ''
    SOURCE_HINT = ''
    SOURCE_TIP = ''

    def init_state(self):
        self._suggested = ''
        self._ready = False
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._read_source)

    # --- the source box
    def make_source_widget(self, parent):
        raise NotImplementedError

    def build_top(self):
        box = self.card(tr(self.SOURCE_BOX))
        lay = QVBoxLayout(box)
        self.in_widget = self.make_source_widget(box)
        self.in_widget.lineEdit().setPlaceholderText(tr(self.SOURCE_HINT))
        self.in_widget.setToolTip(tr(self.SOURCE_TIP))
        self.in_widget.fileChanged.connect(lambda *_: self._debounce.start())
        lay.addWidget(self.in_widget)

        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.checklist = LayerChecklist(box)
        self.checklist.setToolTip(tr(
            'Tick the layers to convert. Leave them all ticked to convert everything.'))
        self.checklist.changed.connect(self._on_ticks)
        lay.addWidget(self.checklist)
        return box

    def after_build(self):
        self._read_source()

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def source_path(self):
        return self.in_widget.filePath().strip()

    def source_exists(self, path):
        return os.path.exists(path)

    def _read_source(self):
        path = self.source_path()
        self.checklist.clear()
        self._ready = False
        if not path:
            self._say(tr('Choose the source to convert.'))
        elif not self.source_exists(path):
            self._say(tr('That path does not exist.'), 'bad')
        else:
            try:
                items = list_layers(path)
            except Exception as exc:                    # noqa: BLE001 - shown to the user
                items = []
                self._say(str(exc), 'bad')
            else:
                if not items:
                    self._say(tr('No layers were found here.'), 'bad')
            if items:
                self.checklist.set_items(items)
                self._ready = True
            self.suggest_output(path)
        self.later_resize()

    def _on_ticks(self):
        if self._ready:
            self._say(tr('{0} of {1} layers will be converted').format(
                len(self.checklist.checked()), len(self.checklist.names())), 'good')

    def common_problem(self):
        self._debounce.stop()
        path = self.source_path()
        if not path or not self.source_exists(path):
            return tr('Choose the source first.')
        if not self._ready:
            self._read_source()
        if not self._ready:
            return tr('No layers were found in the source.')
        if not self.checklist.checked():
            return tr('Tick at least one layer.')
        return None

    def suggest_output(self, source):
        """Offer a sensible output next to the source, unless the user chose one."""
        current = self.out_widget.filePath().strip()
        if current and current != self._suggested:
            return
        self._suggested = self.default_output(source)
        self.out_widget.setFilePath(self._suggested)

    def default_output(self, source):
        raise NotImplementedError

    def _teardown(self):
        self._debounce.stop()
        super()._teardown()


class GdbToGpkgDialog(_GdbDialog):
    """An ArcGIS File Geodatabase in; one GeoPackage out."""

    TITLE = 'File Geodatabase to GeoPackage'
    ALG_NAME = 'filegdb_to_geopackage'
    RUN_TEXT = 'Convert'
    SOURCE_BOX = 'File Geodatabase'
    SOURCE_HINT = 'Choose the .gdb folder'
    SOURCE_TIP = ('The .gdb folder. Close it in ArcGIS first: a .lock file in '
                  'the folder means it is still open there.')

    def algorithm_class(self):
        return G.FileGdbToGeoPackage

    def make_source_widget(self, parent):
        w = QgsFileWidget(parent)
        w.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        return w

    def build_sections(self):
        opts = self.card()
        form = QFormLayout(opts)
        self.alias_check = QCheckBox(tr('Use layer aliases as layer names'), opts)
        self.alias_check.setChecked(True)
        self.alias_check.setToolTip(tr(
            'A class called Canal_Alignment with the alias "Canal Alignment" '
            'arrives under the readable name. Untick for the raw table names.'))
        self.prefix_check = QCheckBox(
            tr('Prefix layer names with their feature dataset'), opts)
        self.prefix_check.setToolTip(tr(
            'A GeoPackage is flat, so feature datasets disappear. This keeps '
            'the grouping visible, and keeps same-named classes apart.'))
        self.styles_check = QCheckBox(
            tr('Restore QGIS layer styles the geodatabase carries'), opts)
        self.styles_check.setChecked(True)
        self.styles_check.setToolTip(tr(
            'Only present when the geodatabase came out of the GeoPackage to '
            'File Geodatabase tool.'))
        self.fid_check = QCheckBox(tr('Keep ObjectID values as GeoPackage feature ids'), opts)
        self.fid_check.setChecked(True)
        self.skip_check = QCheckBox(tr('Skip layers with no features'), opts)
        self.linear_check = QCheckBox(
            tr('Convert curved geometries to straight segments'), opts)
        for w in (self.alias_check, self.prefix_check, self.styles_check,
                  self.fid_check, self.skip_check, self.linear_check):
            form.addRow(w)
        form.addRow(note(tr(
            'Field domains, tables, Z / M values and true curves travel with '
            'the data.'), opts))

        out = self.card()
        oform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.out_widget.setFilter('GeoPackage (*.gpkg)')
        self.out_widget.lineEdit().setPlaceholderText(tr('The GeoPackage to write'))
        oform.addRow(tr('GeoPackage'), self.out_widget)
        self.append_check = QCheckBox(
            tr('Add to the GeoPackage if it exists (instead of replacing it)'), out)
        oform.addRow('', self.append_check)
        self.load_check = QCheckBox(tr('Add the converted layers to the project'), out)
        oform.addRow('', self.load_check)
        return [(opts, tr('Options'), tr('Names, styles and what to keep')),
                (out, tr('Output'), tr('Where the GeoPackage goes'))]

    def default_output(self, source):
        stem = os.path.splitext(os.path.basename(os.path.normpath(source)))[0]
        return os.path.join(os.path.dirname(os.path.normpath(source)), stem + '.gpkg')

    def locked_widgets(self):
        return [self.in_widget, self.checklist, self.out_widget, self.alias_check,
                self.prefix_check, self.styles_check, self.fid_check,
                self.skip_check, self.linear_check, self.append_check,
                self.load_check]

    def problem(self):
        issue = self.common_problem()
        if issue:
            return issue
        path = self.source_path()
        if not (os.path.isdir(path) and path.lower().endswith('.gdb')):
            return tr('The source must be a .gdb folder.')
        out = self.out_widget.filePath().strip()
        if not out:
            return tr('Choose the GeoPackage to write.')
        if not out.lower().endswith('.gpkg'):
            self.out_widget.setFilePath(out + '.gpkg')
        if os.path.exists(self.out_widget.filePath()) and not self.append_check.isChecked():
            answer = QMessageBox.question(
                self, tr(self.TITLE),
                tr('{0} already exists and will be replaced. Continue?').format(
                    os.path.basename(self.out_widget.filePath())))
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return None

    def parameters(self):
        A = G.FileGdbToGeoPackage
        return {
            A.INPUT: self.source_path(),
            A.OUTPUT: self.out_widget.filePath().strip(),
            A.USE_ALIASES: self.alias_check.isChecked(),
            A.DATASET_PREFIX: self.prefix_check.isChecked(),
            A.LOAD: self.load_check.isChecked(),
            A.LAYERS: self.checklist.only(),
            A.STYLES: self.styles_check.isChecked(),
            A.SKIP_EMPTY: self.skip_check.isChecked(),
            A.LINEARIZE: self.linear_check.isChecked(),
            A.APPEND: self.append_check.isChecked(),
            A.PRESERVE_FID: self.fid_check.isChecked(),
        }

    def setParameters(self, parameters):
        path = parameters.get(G.FileGdbToGeoPackage.INPUT)
        if isinstance(path, str) and path:
            self.in_widget.setFilePath(path)
            self._read_source()

    def done_ok(self, results):
        self.log.appendPlainText('\n' + tr('Done. {0} layer(s) in {1}').format(
            results.get(G.FileGdbToGeoPackage.LAYER_COUNT, '?'),
            results.get(G.FileGdbToGeoPackage.OUTPUT, '')))


class GpkgToGdbDialog(_GdbDialog):
    """A GeoPackage in; an ArcGIS File Geodatabase out."""

    TITLE = 'GeoPackage to File Geodatabase'
    ALG_NAME = 'geopackage_to_filegdb'
    RUN_TEXT = 'Convert'
    SOURCE_BOX = 'GeoPackage'
    SOURCE_HINT = 'Choose a .gpkg file'
    SOURCE_TIP = 'Every ticked layer and table is written to the geodatabase.'

    def algorithm_class(self):
        return G.GeoPackageToFileGdb

    def make_source_widget(self, parent):
        w = QgsFileWidget(parent)
        w.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        w.setFilter('GeoPackage (*.gpkg *.GPKG);;All files (*.*)')
        return w

    def build_sections(self):
        opts = self.card()
        form = QFormLayout(opts)

        self.version_box = QComboBox(opts)
        self.version_box.addItems([
            tr('Every ArcGIS version (64-bit integers become decimals)'),
            tr('ArcGIS Pro 3.2 and later (keeps 64-bit integers)')])
        self.version_box.setToolTip(tr(
            'Leave on every version unless the recipient is on Pro 3.2 or '
            'newer. The Pro setting only matters for very large ID numbers.'))
        form.addRow(tr('Readable by'), self.version_box)

        self.dataset_edit = QLineEdit(opts)
        self.dataset_edit.setPlaceholderText(tr('none'))
        self.dataset_edit.setToolTip(tr('Put the layers in this feature dataset.'))
        form.addRow(tr('Feature dataset'), self.dataset_edit)

        self.shape_check = QCheckBox(tr('Create Shape_Length and Shape_Area fields'), opts)
        self.shape_check.setChecked(True)
        self.alias_check = QCheckBox(
            tr('Keep the original layer name as the ArcGIS alias'), opts)
        self.alias_check.setChecked(True)
        self.alias_check.setToolTip(tr(
            'Table names are cleaned to letters, digits and underscores; the '
            'original name stays as the alias, and the reverse tool restores it.'))
        self.styles_check = QCheckBox(
            tr('Carry QGIS layer styles across (adds a KGA_layer_styles table)'), opts)
        self.styles_check.setChecked(True)
        self.fid_check = QCheckBox(tr('Keep GeoPackage feature ids as ObjectID values'), opts)
        self.fid_check.setChecked(True)
        self.skip_check = QCheckBox(tr('Skip layers with no features'), opts)
        self.linear_check = QCheckBox(
            tr('Convert curved geometries to straight segments'), opts)
        for w in (self.shape_check, self.alias_check, self.styles_check,
                  self.fid_check, self.skip_check, self.linear_check):
            form.addRow(w)

        out = self.card()
        oform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.out_widget.lineEdit().setPlaceholderText(tr('The .gdb folder to write'))
        self.out_widget.setToolTip(tr('Created if it does not exist; the .gdb suffix is added if missing.'))
        oform.addRow(tr('File geodatabase'), self.out_widget)
        self.append_check = QCheckBox(
            tr('Add to the geodatabase if it exists (instead of replacing it)'), out)
        oform.addRow('', self.append_check)
        oform.addRow(note(tr(
            'Open the result in ArcGIS Catalog to check it before sending it on.'), out))
        return [(opts, tr('Options'), tr('ArcGIS version, names and what to keep')),
                (out, tr('Output'), tr('Where the geodatabase goes'))]

    def default_output(self, source):
        stem = os.path.splitext(os.path.basename(source))[0]
        return os.path.join(os.path.dirname(source), stem + '.gdb')

    def locked_widgets(self):
        return [self.in_widget, self.checklist, self.out_widget, self.version_box,
                self.dataset_edit, self.shape_check, self.alias_check,
                self.styles_check, self.fid_check, self.skip_check,
                self.linear_check, self.append_check]

    def problem(self):
        issue = self.common_problem()
        if issue:
            return issue
        if not os.path.isfile(self.source_path()):
            return tr('The source must be a .gpkg file.')
        out = self.out_widget.filePath().strip()
        if not out:
            return tr('Choose the geodatabase to write.')
        if not out.lower().endswith('.gdb'):
            out += '.gdb'
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
        A = G.GeoPackageToFileGdb
        return {
            A.INPUT: self.source_path(),
            A.OUTPUT: self.out_widget.filePath().strip(),
            A.ARCGIS_VERSION: self.version_box.currentIndex(),
            A.FEATURE_DATASET: self.dataset_edit.text().strip(),
            A.SHAPE_FIELDS: self.shape_check.isChecked(),
            A.LAYERS: self.checklist.only(),
            A.STYLES: self.styles_check.isChecked(),
            A.SKIP_EMPTY: self.skip_check.isChecked(),
            A.LINEARIZE: self.linear_check.isChecked(),
            A.APPEND: self.append_check.isChecked(),
            A.SET_ALIASES: self.alias_check.isChecked(),
            A.PRESERVE_FID: self.fid_check.isChecked(),
        }

    def setParameters(self, parameters):
        path = parameters.get(G.GeoPackageToFileGdb.INPUT)
        if isinstance(path, str) and path:
            self.in_widget.setFilePath(path)
            self._read_source()

    def done_ok(self, results):
        self.log.appendPlainText('\n' + tr('Done. {0} layer(s) in {1}').format(
            results.get(G.GeoPackageToFileGdb.LAYER_COUNT, '?'),
            results.get(G.GeoPackageToFileGdb.OUTPUT, '')))

# -*- coding: utf-8 -*-
"""Add Quick Template: the window.

A small modeless window that makes an empty point, line or polygon layer in a
few clicks. It is not a Processing dialog: nothing here needs a parameter form,
a log or a run button, and the window stays open so several layers can be made
one after another.

Fields come from one of two places. By default the layer gets ``name``, ``id``
and ``note`` plus one measurement for its geometry. Alternatively the field
format is copied from any vector layer - one in the project, one dragged in
from the Layers or Browser panel, or one browsed to on disk - and the user
ticks which of its fields to keep.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
from contextlib import suppress

from qgis.core import (Qgis, QgsMessageLog, QgsProject, QgsProviderRegistry,
                       QgsVectorLayer)
from qgis.gui import QgsMapLayerComboBox, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QRadioButton, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

try:
    from qgis.core import QgsMimeDataUtils
except ImportError:                         # pragma: no cover
    from qgis.gui import QgsMimeDataUtils

from ..branding import LOG_TAG, open_docs
from ..core import layer_loader as LL
from ..core import output_defaults as od
from ..core import quick_template as QT
from ..core.modify_features import layer_filter
from .output_fields import FollowingField

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

TITLE = 'Add Quick Template'
ALG_NAME = 'add_quick_template'

BLUE = '#174592'
ORANGE = '#FC663A'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 22px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaReminder { background: #FFF4E5; border: 1px solid %(orange)s;
    border-radius: 3px; padding: 5px; font-weight: normal; }
QLabel#kgaHint { color: palette(mid); font-weight: normal; }
""" % {'blue': BLUE, 'orange': ORANGE}

_INSTANCE = None
INVALID_FILE_CHARS = '\\/:*?"<>|'


def tr(text):
    return text


class QuickTemplateDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaQuickTemplateDialog')
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)
        # Closing discards the window, so the next open starts from the
        # defaults instead of showing the last path and message.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._name_touched = False
        self._external = []         # layers browsed or dropped in; kept alive
        self._source = []           # FieldSpec rows shown in the preview
        self._source_layer = None

        root = QVBoxLayout(self)

        # --- Layer ---------------------------------------------------------
        box = QGroupBox(tr('Layer'), self)
        form = QFormLayout(box)

        self.name_edit = QLineEdit(self)
        self.name_edit.textEdited.connect(self._on_name_edited)
        form.addRow(tr('Layer name'), self.name_edit)

        row = QHBoxLayout()
        self.geom_buttons = {}
        for key, label, _geom, _name in QT.GEOMETRIES:
            button = QRadioButton(tr(label), self)
            button.toggled.connect(self._on_geometry_changed)
            row.addWidget(button)
            self.geom_buttons[key] = button
        row.addStretch(1)
        form.addRow(tr('Geometry'), row)

        self.crs_widget = QgsProjectionSelectionWidget(self)
        self.crs_widget.setCrs(QT.crs_from_authid())
        self.crs_widget.setToolTip(tr(
            'Defaults to WGS 84 / UTM zone 48N. Press the button on the right '
            'to choose another coordinate reference system.'))
        form.addRow(tr('CRS'), self.crs_widget)
        root.addWidget(box)

        # --- Fields --------------------------------------------------------
        box = QGroupBox(tr('Fields'), self)
        lay = QVBoxLayout(box)

        self.default_check = QCheckBox(tr('Use the default fields'), self)
        self.default_check.setChecked(True)
        self.default_check.toggled.connect(self._on_default_toggled)
        lay.addWidget(self.default_check)

        self.reminder = QLabel(self)
        self.reminder.setObjectName('kgaReminder')
        self.reminder.setWordWrap(True)
        self.reminder.setTextFormat(Qt.TextFormat.RichText)
        self.reminder.setSizePolicy(QSizePolicy.Policy.Preferred,
                                    QSizePolicy.Policy.Maximum)
        lay.addWidget(self.reminder)

        self.copy_check = QCheckBox(
            tr('Copy the field format from a layer'), self)
        self.copy_check.toggled.connect(self._on_copy_toggled)
        lay.addWidget(self.copy_check)

        self.copy_panel = QWidget(self)
        cp = QVBoxLayout(self.copy_panel)
        cp.setContentsMargins(18, 0, 0, 0)

        row = QHBoxLayout()
        self.layer_combo = QgsMapLayerComboBox(self.copy_panel)
        self.layer_combo.setFilters(layer_filter('VectorLayer'))
        self.layer_combo.setAllowEmptyLayer(True)
        # Drops are taken by the window, which also understands the Browser
        # panel and files; the combo only knows layers already in the project.
        self.layer_combo.setAcceptDrops(False)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        row.addWidget(self.layer_combo, 1)
        self.browse_button = QPushButton(tr('Browse...'), self.copy_panel)
        self.browse_button.setToolTip(tr(
            'Pick a vector file: Shapefile, GeoPackage, GeoJSON, CSV ...'))
        self.browse_button.clicked.connect(self._browse_file)
        row.addWidget(self.browse_button)
        self.gdb_button = QPushButton(tr('Browse GDB...'), self.copy_panel)
        self.gdb_button.setToolTip(tr('Pick a File Geodatabase (.gdb folder).'))
        self.gdb_button.clicked.connect(self._browse_gdb)
        row.addWidget(self.gdb_button)
        cp.addLayout(row)

        hint = QLabel(tr(
            'Choose a layer, or drag one in from the Layers panel, the '
            'Browser panel or a file manager.'), self.copy_panel)
        hint.setObjectName('kgaHint')
        hint.setWordWrap(True)
        cp.addWidget(hint)

        self.tree = QTreeWidget(self.copy_panel)
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels([tr('Field'), tr('Type'), tr('Length')])
        self.tree.setMinimumHeight(150)
        self.tree.itemChanged.connect(self._update_count)
        cp.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.all_button = QPushButton(tr('Select all'), self.copy_panel)
        self.all_button.clicked.connect(lambda: self._set_all(True))
        row.addWidget(self.all_button)
        self.none_button = QPushButton(tr('Select none'), self.copy_panel)
        self.none_button.clicked.connect(lambda: self._set_all(False))
        row.addWidget(self.none_button)
        self.count_label = QLabel(self.copy_panel)
        row.addWidget(self.count_label, 1)
        cp.addLayout(row)
        lay.addWidget(self.copy_panel, 1)
        self.root = root
        self.fields_box = box
        root.addWidget(box)

        # --- Output --------------------------------------------------------
        box = QGroupBox(tr('Output'), self)
        self.output_box = box
        form = QFormLayout(box)
        self.output_combo = QComboBox(self)
        for key, label, _suffix, _filter in QT.OUTPUTS:
            self.output_combo.addItem(tr(label), key)
        index = self.output_combo.findData(QT.OUT_GDB)
        if not QT.has_gdb_writer() and index >= 0:
            self.output_combo.model().item(index).setEnabled(False)
            self.output_combo.setItemData(
                index, tr('This QGIS has no File Geodatabase write driver '
                          '(GDAL 3.6 or newer).'), Qt.ItemDataRole.ToolTipRole)
        self.output_combo.currentIndexChanged.connect(self._on_output_changed)
        form.addRow(tr('Save as'), self.output_combo)

        self.path_widget = QWidget(self)
        row = QHBoxLayout(self.path_widget)
        row.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit(self.path_widget)
        self.path_edit.setPlaceholderText(tr(
            'Where to save the file - or drop a folder, .gpkg or .gdb here'))
        row.addWidget(self.path_edit, 1)
        self.path_button = QPushButton(tr('...'), self.path_widget)
        self.path_button.setMaximumWidth(36)
        self.path_button.clicked.connect(self._browse_output)
        row.addWidget(self.path_button)
        form.addRow(tr('File'), self.path_widget)
        self.path_form = form
        self._path_follow = FollowingField(self.path_edit)
        if od.project_database():
            # Like Pro: a new layer goes into the project's default database.
            self.output_combo.setCurrentIndex(
                self.output_combo.findData(QT.OUT_GPKG))
        root.addWidget(box)

        # --- Buttons -------------------------------------------------------
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        row = QHBoxLayout()
        self.help_button = QPushButton(tr('Help'), self)
        self.help_button.setAutoDefault(False)
        self.help_button.clicked.connect(lambda: open_docs(ALG_NAME))
        row.addWidget(self.help_button)
        row.addStretch(1)
        self.create_button = QPushButton(tr('Create Layer'), self)
        self.create_button.setObjectName('kgaPrimary')
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self.create_layer)
        row.addWidget(self.create_button)
        self.close_button = QPushButton(tr('Close'), self)
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        root.addLayout(row)

        self.geom_buttons[QT.GEOM_POINT].setChecked(True)
        self._on_default_toggled()
        self._on_copy_toggled()
        self.resize(540, self.minimumSizeHint().height())
        self._on_output_changed()

    # ------------------------------------------------------------- helpers

    def geometry(self):
        for key, button in self.geom_buttons.items():
            if button.isChecked():
                return key
        return QT.GEOM_POINT

    def output(self):
        return self.output_combo.currentData()

    def _say(self, text, error=False):
        self.status.setStyleSheet('color: #B00020;' if error else
                                  'color: #1B6E2D;')
        self.status.setText(text)

    # ------------------------------------------------------------- layer box

    def _on_name_edited(self, _text):
        self._name_touched = True

    def _on_geometry_changed(self, checked=True):
        if not checked:
            return
        if not self._name_touched or not self.name_edit.text().strip():
            self.name_edit.setText(QT.default_layer_name(self.geometry()))
            self._name_touched = False
        self._update_reminder()

    # ------------------------------------------------------------- fields box

    def _update_reminder(self):
        names = ', '.join('<b>{}</b>'.format(f.name)
                          for f in QT.default_fields(self.geometry()))
        self.reminder.setText(tr(
            'These fields will be added: {}.').format(names))

    def _on_default_toggled(self, checked=None):
        on = self.default_check.isChecked()
        if on and self.copy_check.isChecked():
            self.copy_check.setChecked(False)
        self.reminder.setVisible(on)
        self._update_reminder()

    def _on_copy_toggled(self, checked=None):
        on = self.copy_check.isChecked()
        if on and self.default_check.isChecked():
            self.default_check.setChecked(False)
        self.copy_panel.setVisible(on)
        self._fit_height(on)
        if on:
            self._on_layer_changed(self.layer_combo.currentLayer())
        if not on and not self.default_check.isChecked():
            self._say(tr('No attribute fields will be added.'))
        else:
            self.status.clear()

    def _fit_height(self, roomy):
        """Grow for the field list, shrink to the bare options otherwise."""
        # A box that may not grow cannot soak up the height the list left.
        self.fields_box.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding if roomy
            else QSizePolicy.Policy.Maximum)
        self.root.setStretchFactor(self.fields_box, 1 if roomy else 0)
        if self.isVisible():
            # Next turn of the event loop, once the hidden panel has left the
            # layout; shrinking now measures the old, taller layout.
            QTimer.singleShot(0, lambda: self._apply_height(roomy))

    def _apply_height(self, roomy):
        if roomy:
            self.resize(self.width(), max(self.height(), 700))
        else:
            self.layout().invalidate()
            self.layout().activate()
            self.resize(self.width(), self.minimumSizeHint().height())

    def _on_layer_changed(self, layer):
        self._source_layer = layer
        self._source = []
        self.tree.blockSignals(True)
        self.tree.clear()
        if layer is not None:
            self._source = QT.fields_of_layer(layer)
            for f in self._source:
                item = QTreeWidgetItem([f.name, f.note, str(f.length or '')])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    0, Qt.CheckState.Unchecked if QT.is_system(layer, f.name)
                    else Qt.CheckState.Checked)
                if QT.is_system(layer, f.name):
                    item.setToolTip(0, tr(
                        'Maintained by the data format itself; left off by '
                        'default.'))
                self.tree.addTopLevelItem(item)
            for column in range(3):
                self.tree.resizeColumnToContents(column)
        self.tree.blockSignals(False)
        self._update_count()

    def _checked_rows(self):
        rows = []
        for i in range(self.tree.topLevelItemCount()):
            if self.tree.topLevelItem(i).checkState(0) == \
                    Qt.CheckState.Checked:
                rows.append(i)
        return rows

    def _set_all(self, on):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(
                0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_count()

    def _update_count(self, *_args):
        self.count_label.setText(tr('{0} of {1} fields selected').format(
            len(self._checked_rows()), len(self._source)))

    def selected_fields(self):
        """The specs to create, per the options currently chosen."""
        if self.copy_check.isChecked():
            if self._source_layer is None:
                raise QT.TemplateError(tr(
                    'Choose a layer to copy the fields from, or switch back '
                    'to the default fields.'))
            return [self._source[i] for i in self._checked_rows()]
        if self.default_check.isChecked():
            return QT.default_fields(self.geometry())
        return []

    # ----------------------------------------------- layers in: drop / browse

    def _use_layer(self, layer):
        """Make `layer` the source of the field format."""
        if layer is None or not layer.isValid():
            self._say(tr('That layer could not be opened.'), True)
            return
        if QgsProject.instance().mapLayer(layer.id()) is None:
            self._external.append(layer)
            self.layer_combo.setAdditionalLayers(self._external)
        if not self.copy_check.isChecked():
            self.copy_check.setChecked(True)
        self.layer_combo.setLayer(layer)
        self.status.clear()

    def _layers_from_mime(self, mime):
        layers = []
        if QgsMimeDataUtils.isUriList(mime):
            for uri in QgsMimeDataUtils.decodeUriList(mime):
                if uri.layerType != 'vector':
                    continue
                layer = None
                layer_id = getattr(uri, 'layerId', '')
                if layer_id:
                    layer = QgsProject.instance().mapLayer(layer_id)
                if layer is None:
                    layer = QgsVectorLayer(uri.uri, uri.name, uri.providerKey)
                layers.append(layer)
        elif mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path:
                    layer = self._layer_from_path(path)
                    if layer is not None:
                        layers.append(layer)
        return layers

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if QgsMimeDataUtils.isUriList(mime) or mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        point = event.position().toPoint() if hasattr(event, 'position') \
            else event.pos()
        if self.output_box.geometry().contains(point) \
                and self._drop_output(mime):
            event.acceptProposedAction()
            return
        layers = [layer for layer in self._layers_from_mime(mime)
                  if layer is not None]
        if not layers:
            self._say(tr('Drop a vector layer: nothing usable was found.'),
                      True)
            event.ignore()
            return
        event.acceptProposedAction()
        self._use_layer(layers[0])
        if len(layers) > 1:
            self._say(tr('Several layers were dropped; using "{}".').format(
                layers[0].name()))

    # ------------------------------------------------- drop onto the output

    @staticmethod
    def _paths_from_mime(mime):
        """Existing local paths named by a drop, folders and files alike."""
        found = []
        if QgsMimeDataUtils.isUriList(mime):
            for uri in QgsMimeDataUtils.decodeUriList(mime):
                # A layer inside a container reads "file.gpkg|layername=x".
                found.append(uri.uri.split('|')[0])
        if mime.hasUrls():
            found.extend(url.toLocalFile() for url in mime.urls())
        return [os.path.normpath(p) for p in found
                if p and os.path.exists(p)]

    def _drop_output(self, mime):
        """Fill in the output from a dropped folder, .gpkg, .gdb or .shp.

        Returns False when nothing in the drop is a place to save to.
        """
        for path in self._paths_from_mime(mime):
            low = path.lower()
            output = self.output()
            if low.endswith('.gdb') and os.path.isdir(path):
                output, target = QT.OUT_GDB, path
            elif low.endswith('.gpkg') and os.path.isfile(path):
                output, target = QT.OUT_GPKG, path
            elif os.path.isdir(path):
                if output == QT.OUT_TEMP:
                    output = QT.OUT_SHP
                target = os.path.join(path, self._file_stem()
                                      + QT.suffix_of(output))
            elif low.endswith('.shp'):
                # A new file beside it rather than overwriting it.
                output = QT.OUT_SHP
                target = os.path.join(os.path.dirname(path),
                                      self._file_stem() + '.shp')
            else:
                continue
            if output == QT.OUT_GDB and not QT.has_gdb_writer():
                self._say(tr('This QGIS cannot create a File Geodatabase.'),
                          True)
                return True
            self.output_combo.setCurrentIndex(
                self.output_combo.findData(output))
            self.path_edit.setText(os.path.normpath(target))
            self.status.clear()
            return True
        return False

    def _file_stem(self):
        """The layer name made safe to use as a file name."""
        name = (self.name_edit.text().strip()
                or QT.default_layer_name(self.geometry()))
        return ''.join('_' if c in INVALID_FILE_CHARS else c for c in name)

    def _layer_from_path(self, path):
        """A vector layer for a file or folder; asks when it holds several."""
        entries = LL.sublayers(path)
        if len(entries) > 1:
            names = [name for name, _uri in entries]
            choice, ok = QInputDialog.getItem(
                self, tr(TITLE), tr('Layer to copy the fields from:'),
                names, 0, False)
            if not ok:
                return None
            entries = [e for e in entries if e[0] == choice]
        if entries:
            name, uri = entries[0]
        else:
            name, uri = os.path.splitext(os.path.basename(path))[0], path
        layer = QgsVectorLayer(uri, name, 'ogr')
        if not layer.isValid():
            self._say(tr('QGIS cannot read "{}" as vector data.').format(
                os.path.basename(path)), True)
            return None
        return layer

    def _browse_file(self):
        # Open on Shapefiles only, so the .dbf/.shx/.prj companions are not
        # listed; every other format and "All files" stay in the dropdown.
        shp = 'ESRI Shapefile (*.shp *.SHP)'
        path, _filter = QFileDialog.getOpenFileName(
            self, tr('Choose a vector file'), '',
            shp + ';;' + QgsProviderRegistry.instance().fileVectorFilters(),
            shp)
        if path:
            self._use_layer(self._layer_from_path(path))

    def _browse_gdb(self):
        path = QFileDialog.getExistingDirectory(
            self, tr('Choose a File Geodatabase (.gdb folder)'))
        if path:
            self._use_layer(self._layer_from_path(path))

    # ---------------------------------------------------------------- output

    def _on_output_changed(self, *_args):
        file_output = self.output() != QT.OUT_TEMP
        self._follow_project()
        self.path_widget.setVisible(file_output)
        label = self.path_form.labelForField(self.path_widget)
        if label is not None:
            label.setVisible(file_output)

    def _follow_project(self):
        """Point the file at the project's database / folder, unless chosen."""
        follow = getattr(self, '_path_follow', None)
        if follow is None or not follow.is_ours():
            return
        self.path_edit.clear()
        follow.last = ''
        output = self.output()
        if output == QT.OUT_GPKG:
            follow.suggest_path(od.project_database())
        elif output == QT.OUT_SHP:
            name = self.name_edit.text().strip()                 if hasattr(self, 'name_edit') else ''
            follow.suggest(name or 'Template', '.shp')

    def _browse_output(self):
        output = self.output()
        flt = next(o[3] for o in QT.OUTPUTS if o[0] == output)
        start = self.path_edit.text() or os.path.join(
            od.project_folder(), self.name_edit.text().strip())
        path, _filter = QFileDialog.getSaveFileName(
            self, tr('Save layer as'), start, flt,
            options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.path_edit.setText(QT.with_suffix(path, output))

    # ---------------------------------------------------------------- create

    def create_layer(self):
        try:
            layer = self._make_layer()
        except QT.TemplateError as exc:
            self._say(str(exc), True)
            return
        except Exception as exc:                # noqa: BLE001
            import traceback
            QgsMessageLog.logMessage(
                'Add Quick Template failed:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Critical)
            self._say(tr('Could not create the layer: {}').format(exc), True)
            return
        if layer is None:                       # the user backed out
            return
        QgsProject.instance().addMapLayer(layer)
        with suppress(Exception):
            if iface is not None:
                iface.setActiveLayer(layer)
        where = tr('temporary layer') if self.output() == QT.OUT_TEMP \
            else os.path.basename(self.path_edit.text())
        self._say(tr('Created "{0}" ({1}).').format(layer.name(), where))
        if iface is not None:
            iface.messageBar().pushSuccess(
                tr(TITLE), tr('Created "{}".').format(layer.name()))

    def _make_layer(self):
        key = self.geometry()
        output = self.output()
        name = self.name_edit.text().strip() or QT.default_layer_name(key)
        crs = self.crs_widget.crs()
        fields = self.selected_fields()

        errors = QT.problems(fields)
        if errors:
            raise QT.TemplateError(' '.join(errors))
        notes = QT.warnings(fields, output)
        if notes and QMessageBox.question(
                self, tr(TITLE), '\n\n'.join(notes) + '\n\n' + tr('Continue?')
        ) != QMessageBox.StandardButton.Yes:
            return None

        path = ''
        if output != QT.OUT_TEMP:
            path = QT.with_suffix(self.path_edit.text().strip(), output)
            QT.check_target(path, output)
            self.path_edit.setText(path)
            if QT.writes_into_existing(path, output):
                # Adding to a container is the normal case and needs no
                # question; only a layer of the same name would be lost.
                if QT.layer_exists(path, QT.container_name(name, output)) and QMessageBox.question(
                        self, tr(TITLE),
                        tr('"{0}" already has a layer named "{1}". Replace '
                           'it?').format(os.path.basename(path), name)
                ) != QMessageBox.StandardButton.Yes:
                    return None
            elif os.path.exists(path) and QMessageBox.question(
                    self, tr(TITLE),
                    tr('"{}" already exists and will be replaced. '
                       'Continue?').format(os.path.basename(path))
            ) != QMessageBox.StandardButton.Yes:
                return None

        memory = QT.build_layer(key, name, crs, fields)
        if output == QT.OUT_TEMP:
            memory.setName(LL.unique_name(QgsProject.instance(), name))
            layer = memory
        else:
            layer = QT.save_layer(memory, path, output,
                                  QT.container_name(name, output))
            layer.setName(LL.unique_name(QgsProject.instance(), name))
        QT.apply_defaults(layer, fields)
        return layer

    # ------------------------------------------------------------- teardown

    def shutdown(self):
        self._external = []
        self.close()
        self.deleteLater()


def _alive(dialog):
    if dialog is None:
        return False
    try:
        dialog.objectName()
    except RuntimeError:                        # deleted by Qt
        return False
    return True


def show_quick_template():
    """Open the window, or bring the one that is already open forward."""
    global _INSTANCE
    if not _alive(_INSTANCE):
        _INSTANCE = QuickTemplateDialog(
            iface.mainWindow() if iface is not None else None)
    _INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
    _INSTANCE.show()
    _INSTANCE.raise_()
    _INSTANCE.activateWindow()
    return _INSTANCE


def close_quick_template():
    """Close the window on plugin unload."""
    global _INSTANCE
    dialog, _INSTANCE = _INSTANCE, None
    if _alive(dialog):
        dialog.shutdown()

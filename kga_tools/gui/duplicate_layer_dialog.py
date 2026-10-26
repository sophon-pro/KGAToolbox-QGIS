# -*- coding: utf-8 -*-
"""Duplicate to New Layer: the window.

A small modeless window with two inputs - the original layer and where to save
the copy. It is not a Processing dialog: there is no log or parameter form to
show, and the window stays open so several layers can be copied in a row.
"""

import traceback

from qgis.core import (Qgis, QgsMessageLog, QgsProject,
                       QgsRasterLayer, QgsVectorLayer)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout)

from ..branding import LOG_TAG, help_button
from ..core import duplicate_layer as dup
from ..core import output_defaults as od
from ..core import dem_sources as DEM
from ..core.modify_features import layer_filter
from .roi_picker import RoiPicker

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

TITLE = 'Duplicate to New Layer'
ALG_NAME = 'duplicate_to_new_layer'

BLUE = '#174592'

STYLE = """
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 22px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaHint { color: palette(mid); }
""" % {'blue': BLUE}

_INSTANCE = None


def tr(text):
    return text


class DuplicateLayerDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(480)
        self.setStyleSheet(STYLE)
        self.setObjectName('KgaDuplicateLayerDialog')

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.layer_combo = QgsMapLayerComboBox(self)
        self.layer_combo.setFilters(layer_filter('VectorLayer', 'RasterLayer'))
        self.layer_combo.setAllowEmptyLayer(False)
        # Without this the combo shrinks to the width of its arrow when the
        # first layer's name is not yet measured, hiding the layer name.
        self.layer_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self.layer_combo.setMinimumWidth(300)
        self.layer_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layer_combo.setMinimumContentsLength(20)
        self.layer_combo.setToolTip(tr(
            'A vector or raster layer. A vector layer with a filter copies '
            'only the filtered features.'))
        form.addRow(tr('Original Layer'), self.layer_combo)

        row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.path_edit.setToolTip(tr(
            'The folder to save the copy in. Empty saves it beside the '
            'original. The file is named <layer>_copy and never replaces an '
            'existing file.'))
        self.path_edit.textChanged.connect(self._refresh_preview)
        self._suggested = ''
        row.addWidget(self.path_edit, 1)
        self.browse_button = QPushButton(tr('...'), self)
        self.browse_button.setMaximumWidth(36)
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)
        form.addRow(tr('Output Location'), row)

        self.type_combo = QComboBox(self)
        self.type_combo.setToolTip(tr(
            'The format of the copy. GeoTIFF is the safe choice for a raster.'))
        self.type_combo.currentIndexChanged.connect(self._refresh_preview)
        form.addRow(tr('Output type'), self.type_combo)

        self.selected_check = QCheckBox(tr('Selected features only'), self)
        self.selected_check.setToolTip(tr(
            'Copy only the features selected on the original layer. Available '
            'when a vector layer has a selection.'))
        form.addRow('', self.selected_check)
        outer.addLayout(form)

        # Raster layers only: which part of the raster to copy.
        self.area_picker = RoiPicker(
            iface.mapCanvas() if iface is not None else None, self,
            allow_whole=True, title='Extent to copy')
        self.area_picker.message.connect(self._say)
        self.area_picker.hide()
        outer.addWidget(self.area_picker)

        self.status = QLabel('', self)
        self.status.setObjectName('kgaHint')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addWidget(help_button(ALG_NAME, self))
        buttons.addStretch(1)
        self.run_button = QPushButton(tr('Duplicate'), self)
        self.run_button.setObjectName('kgaPrimary')
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.duplicate)
        buttons.addWidget(self.run_button)
        close = QPushButton(tr('Close'), self)
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        outer.addLayout(buttons)

        self._watched = None
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        self._on_layer_changed(self.layer_combo.currentLayer())

    # ------------------------------------------------------------------ state

    def set_layer(self, layer):
        if layer is not None:
            self.layer_combo.setLayer(layer)
        self._on_layer_changed(self.layer_combo.currentLayer())

    def layer(self):
        return self.layer_combo.currentLayer()

    def _say(self, text, error=False):
        self.status.setStyleSheet('color: #B3261E;' if error else '')
        self.status.setText(text)

    def _fill_types(self, layer):
        """List the formats this kind of layer can be copied to."""
        raster = isinstance(layer, QgsRasterLayer)
        previous = self.type_combo.currentData()
        self.type_combo.blockSignals(True)
        self.type_combo.clear()
        formats = ([(k, l, dup.raster_format_unavailable(k))
                    for k, l, *_rest in dup.RASTER_FORMATS] if raster else
                   [(k, l, dup.format_unavailable(k))
                    for k, l, *_rest in dup.VECTOR_FORMATS])
        for key, label, why in formats:
            self.type_combo.addItem(tr(label), key)
            if why:
                row = self.type_combo.count() - 1
                self.type_combo.model().item(row).setEnabled(False)
                self.type_combo.setItemData(
                    row, why, Qt.ItemDataRole.ToolTipRole)
        index = self.type_combo.findData(
            previous if previous is not None else
            (dup.TIF if raster else dup.GPKG))
        self.type_combo.setCurrentIndex(max(index, 0))
        self.type_combo.blockSignals(False)

    def _on_layer_changed(self, layer):
        self.status.setText('')
        if layer is None:
            self.run_button.setEnabled(False)
            self.path_edit.setPlaceholderText('')
            return
        self.run_button.setEnabled(True)
        memory = dup.is_memory_layer(layer)
        self._fill_types(layer)
        self._watch_selection(layer)
        self.path_edit.setEnabled(not memory)
        self.browse_button.setEnabled(not memory)
        self.type_combo.setEnabled(not memory)
        raster = isinstance(layer, QgsRasterLayer)
        self.selected_check.setVisible(not raster)
        if self.area_picker.isVisible() != raster:
            self.area_picker.setVisible(raster)
            self.adjustSize()
        self._refresh_preview()

    def _watch_selection(self, layer):
        """Follow the layer's selection so the tick box tracks it."""
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(
                    self._update_selection_box)
            except (TypeError, RuntimeError):
                pass
            self._watched = None
        if isinstance(layer, QgsVectorLayer):
            layer.selectionChanged.connect(self._update_selection_box)
            self._watched = layer
        self._update_selection_box()

    def _update_selection_box(self, *_args):
        layer = self.layer()
        count = (layer.selectedFeatureCount()
                 if isinstance(layer, QgsVectorLayer) else 0)
        self.selected_check.setEnabled(count > 0)
        if count == 0:
            self.selected_check.setChecked(False)
            self.selected_check.setText(tr('Selected features only'))
        else:
            self.selected_check.setText(
                tr('Selected features only ({} selected)').format(count))

    def _selected_only(self):
        return self.selected_check.isEnabled() and self.selected_check.isChecked()

    def _format(self):
        return self.type_combo.currentData() or (
            dup.TIF if isinstance(self.layer(), QgsRasterLayer)
            else dup.GPKG)

    def follow_project(self):
        """Offer the project folder, unless the user has typed another."""
        current = self.path_edit.text().strip()
        if current and current != self._suggested:
            return
        self._suggested = od.project_folder()
        self.path_edit.setText(self._suggested)

    def _refresh_preview(self, *_args):
        """Show, as the folder box's hint, where the copy would go."""
        layer = self.layer()
        if layer is None:
            return
        if dup.is_memory_layer(layer):
            self.path_edit.setPlaceholderText(tr(
                'Not needed - a memory layer is copied in memory'))
            return
        self.path_edit.setPlaceholderText(
            dup.source_folder(layer) + tr('  (beside the original)'))

    def _browse(self):
        layer = self.layer()
        if layer is None:
            return
        start = self.path_edit.text().strip() or dup.source_folder(layer)
        folder = QFileDialog.getExistingDirectory(
            self, tr('Save the copy in'), start)
        if folder:
            self.path_edit.setText(folder)

    # ------------------------------------------------------------------- work

    def duplicate(self):
        layer = self.layer()
        if layer is None:
            return
        project = QgsProject.instance()

        if dup.is_memory_layer(layer):
            new_layer = dup.clone_memory(layer, self._selected_only())
            dup.add_above(layer, new_layer, project)
            self._say(tr('"{}" copied in memory.').format(layer.name()))
            return

        fmt = self._format()
        region = None
        if isinstance(layer, QgsRasterLayer):
            try:
                region = self.area_picker.region()
            except DEM.DemError as exc:
                self._say(str(exc), error=True)
                return
        out_path = dup.output_path(layer, fmt, self.path_edit.text())

        self.run_button.setEnabled(False)
        self._say(tr('Copying...'))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            dup.ensure_folder(out_path)
            if isinstance(layer, QgsVectorLayer):
                new_layer = dup.copy_vector(
                    layer, out_path, fmt, project.transformContext(),
                    self._selected_only())
            else:
                new_layer = dup.copy_raster(layer, out_path, fmt, region)
            dup.copy_style(layer, new_layer)
            dup.add_above(layer, new_layer, project)
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Duplicate to New Layer failed:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)
            self._say(str(exc), error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.run_button.setEnabled(self.layer() is not None)

        self._on_layer_changed(self.layer())
        self._say(tr('Copied to {}').format(out_path))
        if iface is not None:
            iface.messageBar().pushSuccess(
                TITLE, tr('Data copied to {}').format(out_path))

    def closeEvent(self, event):
        self.area_picker.disarm()
        self.area_picker.clear_drawn()
        super().closeEvent(event)

    def shutdown(self):
        self.area_picker.teardown()
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


def show_duplicate_layer(layer=None):
    """Open the window (or raise the open one), optionally on `layer`."""
    global _INSTANCE
    if not _alive(_INSTANCE):
        _INSTANCE = DuplicateLayerDialog(
            iface.mainWindow() if iface is not None else None)
    if layer is None and iface is not None:
        layer = iface.activeLayer()
    _INSTANCE.follow_project()
    if isinstance(layer, (QgsVectorLayer, QgsRasterLayer)):
        _INSTANCE.set_layer(layer)
    _INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
    _INSTANCE.show()
    _INSTANCE.raise_()
    _INSTANCE.activateWindow()
    return _INSTANCE


def close_duplicate_layer():
    """Close the window on plugin unload."""
    global _INSTANCE
    dialog, _INSTANCE = _INSTANCE, None
    if _alive(dialog):
        dialog.shutdown()

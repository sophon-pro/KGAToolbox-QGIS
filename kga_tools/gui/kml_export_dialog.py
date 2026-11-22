# -*- coding: utf-8 -*-
"""Export Layers to KML: the window.

UI and wiring only; the writing is the ``ExportToKML`` algorithm. The layers
of the project are listed with a tick box each (the visible ones start ticked,
and the list follows layers being added or removed). The rotated-label
settings are only enabled when labels are carried across in the rotated style,
so the Labels tab shows what will actually be used.
"""

import os

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsColorButton, QgsFileWidget
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                                 QFormLayout, QLabel, QLineEdit, QSpinBox,
                                 QVBoxLayout)

from .layer_checklist import LayerChecklist
from .tabbed_run_dialog import GREEN, RED, TabbedRunDialog, geometry_kind, note, tr
from ..algorithms.ExportToKML_With_Label import ExportToKML as K


def _geometry_name(layer):
    return {'point': tr('points'), 'line': tr('lines'),
            'polygon': tr('polygons')}.get(geometry_kind(layer.wkbType()), tr('no geometry'))


def _spin(parent, lo, hi, value, decimals=None, step=None, tip=''):
    w = QSpinBox(parent) if decimals is None else QDoubleSpinBox(parent)
    if decimals is not None:
        w.setDecimals(decimals)
    w.setRange(lo, hi)
    w.setValue(value)
    if step:
        w.setSingleStep(step)
    w.setToolTip(tip)
    return w


class KmlExportDialog(TabbedRunDialog):
    """Project layers in; .kml / .kmz files for Google Earth out."""

    TITLE = 'Export Layers to KML'
    ALG_NAME = 'export_layers_to_kml'
    RUN_TEXT = 'Export'
    MIN_WIDTH = 580

    def algorithm_class(self):
        return K

    def init_state(self):
        self._built = False
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(300)
        self._refresh.timeout.connect(self._fill_layers)
        project = QgsProject.instance()
        project.layersAdded.connect(self._schedule)
        project.layersRemoved.connect(self._schedule)

    # --------------------------------------------------------------- building

    def build_top(self):
        box = self.card(tr('Layers'))
        lay = QVBoxLayout(box)
        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.checklist = LayerChecklist(box, height=130)
        self.checklist.setToolTip(tr(
            'Point, line and polygon layers in any CRS. Each is reprojected to '
            'WGS 84 on the way out; the layers themselves are left alone.'))
        self.checklist.changed.connect(self._on_ticks)
        lay.addWidget(self.checklist)
        return box

    def build_sections(self):
        # Export
        exp = self.card()
        form = QFormLayout(exp)
        self.mode_box = QComboBox(exp)
        self.mode_box.addItems([tr(m) for m in K.EXPORT_MODES])
        self.mode_box.setToolTip(tr(
            'One file per layer, or every layer in one file with a folder each '
            '(handy as an attachment).'))
        self.mode_box.currentIndexChanged.connect(self._on_mode)
        form.addRow(tr('Export mode'), self.mode_box)
        self.name_edit = QLineEdit('Combined_Export', exp)
        self.name_edit.setToolTip(tr(
            'File name of the single file, without extension. Characters '
            'Windows rejects are replaced with underscores.'))
        form.addRow(tr('Combined file name'), self.name_edit)
        self.selected_check = QCheckBox(tr('Selected features only'), exp)
        self.selected_check.setToolTip(tr(
            'A layer with nothing selected is exported in full, and the log says which.'))
        form.addRow('', self.selected_check)
        self.attrs_check = QCheckBox(tr('Include the attribute balloon'), exp)
        self.attrs_check.setChecked(True)
        self.attrs_check.setToolTip(tr(
            'Every non-empty field goes into the balloon that opens when a '
            'feature is clicked. Untick for a much smaller file.'))
        form.addRow('', self.attrs_check)
        self.width_spin = _spin(exp, 1, 10, 2, tip=tr(
            'Stroke width for lines and polygon outlines. Colours come from each layer\'s symbology.'))
        form.addRow(tr('Line width (px)'), self.width_spin)

        # Labels
        lab = self.card()
        lform = QFormLayout(lab)
        self.keep_check = QCheckBox(tr('Carry layer labels across'), lab)
        self.keep_check.setChecked(True)
        self.keep_check.setToolTip(tr(
            'Writes what the layer\'s Labels tab shows as the placemark name. '
            'Untick for geometry and attributes only.'))
        self.keep_check.toggled.connect(self._on_labels)
        lform.addRow(self.keep_check)
        self.style_box = QComboBox(lab)
        self.style_box.addItems([tr(m) for m in K.LABEL_MODES])
        self.style_box.setToolTip(tr(
            'Placemark labels stay horizontal at a constant screen size. '
            'Rotated ground overlays run along lines and force a .kmz.'))
        self.style_box.currentIndexChanged.connect(self._on_labels)
        lform.addRow(tr('Label style'), self.style_box)
        self.scale_spin = _spin(lab, 0.0, 10.0, 1.0, 1, 0.1, tr(
            'Text scale of placemark labels: 1.0 normal, 0 hides the text.'))
        lform.addRow(tr('Placemark text scale'), self.scale_spin)

        self.rotated = [
            (tr('Text height on ground (m)'),
             _spin(lab, 0.05, 10000.0, 4.0, 2, 1.0, tr(
                 'Glyph height measured on the ground at the closest zoom tier. '
                 '4 m suits parcels, 50 m suits a canal seen from a few km up.'))),
            (tr('Perpendicular offset (m)'),
             _spin(lab, -10000.0, 10000.0, 0.0, 2, 1.0, tr(
                 'Pushes the text off the line: positive is left of the '
                 'direction it runs, negative right, 0 centres it.'))),
            (tr('Zoom tiers'),
             _spin(lab, 1, 6, 3, tip=tr(
                 'Each extra tier adds a copy 4x larger that switches on as '
                 'you zoom out. 3 tiers covers a 64x zoom range.'))),
        ]
        self.height_spin, self.offset_spin, self.tiers_spin = (w for _l, w in self.rotated)
        for label, widget in self.rotated:
            lform.addRow(label, widget)

        self.text_color = QgsColorButton(lab)
        self.text_color.setAllowOpacity(True)
        self.text_color.setColor(QColor(255, 255, 255))
        self.text_color.setToolTip(tr('Colour of the rendered glyphs.'))
        lform.addRow(tr('Text colour'), self.text_color)
        self.halo_color = QgsColorButton(lab)
        self.halo_color.setAllowOpacity(True)
        self.halo_color.setColor(QColor(0, 0, 0))
        self.halo_color.setToolTip(tr('Outline behind the text. Opacity 0 means no halo.'))
        lform.addRow(tr('Halo colour'), self.halo_color)
        self.flip_check = QCheckBox(tr('Keep text right-way-up'), lab)
        self.flip_check.setChecked(True)
        self.flip_check.setToolTip(tr(
            'Spin labels turned past vertical by 180 degrees so they always '
            'read left to right. Untick for flow labels.'))
        lform.addRow('', self.flip_check)

        self.font_edit = QLineEdit('Arial', lab)
        self.font_edit.setToolTip(tr(
            'Font for the rendered labels. Khmer text needs one that covers it, '
            'such as Khmer OS or Noto Sans Khmer.'))
        lform.addRow(tr('Font family'), self.font_edit)
        self.px_spin = _spin(lab, 8, 256, 48, tip=tr(
            'Pixel height the label image is drawn at: sharpness, not size.'))
        lform.addRow(tr('Render resolution (px)'), self.px_spin)
        self.bold_check = QCheckBox(tr('Bold text'), lab)
        self.bold_check.setChecked(True)
        lform.addRow('', self.bold_check)

        self.rotated_widgets = [self.height_spin, self.offset_spin, self.tiers_spin,
                                self.text_color, self.halo_color, self.flip_check,
                                self.font_edit, self.px_spin, self.bold_check]

        # Output
        out = self.card()
        oform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.out_widget.lineEdit().setPlaceholderText(tr('Folder for the .kml / .kmz files'))
        self.out_widget.setToolTip(tr('Files of the same name are overwritten without asking.'))
        oform.addRow(tr('Folder'), self.out_widget)
        oform.addRow(note(tr(
            'Rotated labels are written as a .kmz, because the images have to '
            'travel inside the file.'), out))
        return [(exp, tr('Export'), tr('Mode, selection and attributes')),
                (lab, tr('Labels'), tr('How layer labels are written')),
                (out, tr('Output'), tr('Where the files go'))]

    def after_build(self):
        self._built = True
        self._fill_layers()
        self._on_mode()
        self._on_labels()
        folder = QgsProject.instance().homePath()
        if folder and os.path.isdir(folder):
            self.out_widget.setFilePath(folder)

    def locked_widgets(self):
        return [self.checklist, self.mode_box, self.name_edit, self.selected_check,
                self.attrs_check, self.width_spin, self.keep_check, self.style_box,
                self.scale_spin, self.out_widget, *self.rotated_widgets]

    # --------------------------------------------------------------- reacting

    def _schedule(self, *_):
        if self._built:
            self._refresh.start()

    def _exportable(self):
        layers = []
        for layer in QgsProject.instance().mapLayers().values():
            if (isinstance(layer, QgsVectorLayer) and layer.isValid()
                    and geometry_kind(layer.wkbType()) != 'none'):
                layers.append(layer)
        return layers

    def _fill_layers(self):
        """List the layers, keeping the ticks; the visible ones start ticked."""
        layers = self._exportable()
        previous = self.checklist.names()
        keep = set(self.checklist.checked())
        root = QgsProject.instance().layerTreeRoot()
        ticked = set()
        for layer in layers:
            if layer.id() in previous:
                if layer.id() in keep:
                    ticked.add(layer.id())
            else:
                node = root.findLayer(layer.id())
                if node is not None and node.isVisible():
                    ticked.add(layer.id())
        self.checklist.set_items(
            [(lyr.id(), '{0}   -   {1}, {2}'.format(
                lyr.name(), _geometry_name(lyr),
                tr('{0} features').format(format(lyr.featureCount(), ',')))) for lyr in layers],
            ticked)
        self.later_resize()

    def _on_ticks(self):
        total = len(self.checklist.names())
        if not total:
            self._say(tr('The project has no point, line or polygon layers.'), 'bad')
        else:
            picked = len(self.checklist.checked())
            self._say(tr('{0} of {1} layers will be exported').format(picked, total),
                      'good' if picked else '')

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def _on_mode(self, *_):
        self.name_edit.setEnabled(self.mode_box.currentIndex() == 1)

    def _on_labels(self, *_):
        keep = self.keep_check.isChecked()
        rotated = keep and self.style_box.currentIndex() == 1
        self.style_box.setEnabled(keep)
        self.scale_spin.setEnabled(keep)
        for w in self.rotated_widgets:
            w.setEnabled(rotated)

    # -------------------------------------------------------------------- run

    def problem(self):
        if not self.checklist.checked():
            return tr('Tick at least one layer to export.')
        out = self.out_widget.filePath().strip()
        if not out:
            return tr('Choose the output folder.')
        if os.path.exists(out) and not os.path.isdir(out):
            return tr('The output path is a file, not a folder.')
        return None

    def parameters(self):
        return {
            K.INPUT_LAYERS: self.checklist.checked(),
            K.SELECTED_ONLY: self.selected_check.isChecked(),
            K.EXPORT_MODE: self.mode_box.currentIndex(),
            K.COMBINED_NAME: self.name_edit.text().strip() or 'Combined_Export',
            K.KEEP_LABELS: self.keep_check.isChecked(),
            K.LABEL_MODE: self.style_box.currentIndex(),
            K.LABEL_SCALE: self.scale_spin.value(),
            K.LABEL_HEIGHT: self.height_spin.value(),
            K.LABEL_OFFSET: self.offset_spin.value(),
            K.LOD_TIERS: self.tiers_spin.value(),
            K.TEXT_COLOR: self.text_color.color(),
            K.HALO_COLOR: self.halo_color.color(),
            K.FLIP_UPRIGHT: self.flip_check.isChecked(),
            K.FONT_SIZE: self.px_spin.value(),
            K.FONT_FAMILY: self.font_edit.text().strip() or 'Arial',
            K.FONT_BOLD: self.bold_check.isChecked(),
            K.INCLUDE_ATTRS: self.attrs_check.isChecked(),
            K.LINE_WIDTH: self.width_spin.value(),
            K.OUTPUT_FOLDER: self.out_widget.filePath().strip(),
        }

    def done_ok(self, results):
        self.log.appendPlainText('\n' + tr('Done. Files are in {0}').format(
            results.get(K.OUTPUT_FOLDER, '')))

    def _teardown(self):
        self._refresh.stop()
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(self._schedule)
            except (TypeError, RuntimeError):
                pass
        super()._teardown()

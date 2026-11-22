# -*- coding: utf-8 -*-
"""Raster Format Converter: the window.

UI and wiring only; the tiled, resumable conversion is the
``RasterFormatConverter`` algorithm. The window describes the source (size,
bands, data type), checks it against the chosen format before anything runs
(a format that cannot hold the bands or data type, or a driver this QGIS
cannot write), enables only the options the format reads, and suggests an
output name next to the source.
"""

import os

from qgis.core import QgsRasterLayer
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel,
                                 QSpinBox, QVBoxLayout)

from .tabbed_run_dialog import GREEN, RED, TabbedRunDialog, note, tr
from ..algorithms import raster_format_converter as R
from ..core.modify_features import layer_filter


class RasterConverterDialog(TabbedRunDialog):
    """A raster in; the same raster in another format out."""

    TITLE = 'Raster Format Converter'
    ALG_NAME = 'raster_format_converter'
    RUN_TEXT = 'Convert'

    def algorithm_class(self):
        return R.RasterFormatConverter

    def init_state(self):
        self._built = False
        self._suggested = ''
        self._alg = R.RasterFormatConverter()
        self._source = None             # the QgsRasterLayer the choice resolves to

    # --------------------------------------------------------------- building

    def build_top(self):
        box = self.card(tr('Source raster'))
        lay = QVBoxLayout(box)
        form = QFormLayout()

        self.layer_box = QgsMapLayerComboBox(box)
        self.layer_box.setFilters(layer_filter('RasterLayer'))
        self.layer_box.setAllowEmptyLayer(True)
        self.layer_box.setToolTip(tr('A raster layer already in the project.'))
        self.layer_box.layerChanged.connect(self._on_source)
        form.addRow(tr('Raster layer'), self.layer_box)

        self.file_widget = QgsFileWidget(box)
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.file_widget.setFilter(
            'Rasters (*.tif *.tiff *.ecw *.jp2 *.img *.sid *.png *.jpg *.jpeg *.vrt);;'
            'All files (*.*)')
        self.file_widget.lineEdit().setPlaceholderText(tr('or browse to a file'))
        self.file_widget.setToolTip(tr(
            'A raster file to use instead of the layer above. Any raster GDAL '
            'can read: ECW, GeoTIFF, JPEG 2000, IMG, SID, PNG, JPEG.'))
        self.file_widget.fileChanged.connect(self._on_source)
        form.addRow(tr('File'), self.file_widget)
        lay.addLayout(form)

        self.status = QLabel('', box)
        self.status.setObjectName('kgaStatus')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        return box

    def build_sections(self):
        # Format
        fmt = self.card()
        form = QFormLayout(fmt)
        self.format_box = QComboBox(fmt)
        self.format_box.addItems(R.FORMAT_LABELS)
        self.format_box.setToolTip(tr(
            'The format to write. The output extension is corrected to match.'))
        self.format_box.currentIndexChanged.connect(self._on_format)
        form.addRow(tr('Format'), self.format_box)

        self.comp_box = QComboBox(fmt)
        self.comp_box.addItems(R.COMPRESSIONS)
        self.comp_box.setToolTip(tr(
            'GeoTIFF: LZW and DEFLATE are lossless and widely read, ZSTD is '
            'lossless and faster where GDAL has it, JPEG is lossy but small for '
            'photographs. Erdas IMAGINE reads anything but NONE as "compressed".'))
        form.addRow(tr('Compression'), self.comp_box)

        self.quality_spin = QSpinBox(fmt)
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(75)
        self.quality_spin.setToolTip(tr(
            'JPEG / JPEG 2000: higher is better quality and a bigger file. ECW '
            'reads it as a target compression ratio, where higher means a '
            'smaller file.'))
        form.addRow(tr('Quality'), self.quality_spin)

        self.over_check = QCheckBox(tr('Build overviews (pyramids)'), fmt)
        self.over_check.setChecked(True)
        self.over_check.setToolTip(tr(
            'Make a large raster draw quickly when zoomed out. Only GeoTIFF and '
            'Erdas IMAGINE carry them internally.'))
        form.addRow('', self.over_check)
        self.format_note = note('', fmt)
        form.addRow(self.format_note)

        # Output
        out = self.card()
        oform = QFormLayout(out)
        self.out_widget = QgsFileWidget(out)
        self.out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.out_widget.setFilter(R.FILE_FILTER)
        self.out_widget.lineEdit().setPlaceholderText(tr('The converted raster to write'))
        self.out_widget.setToolTip(tr(
            'Where to write. Run again with this same path to resume an '
            'interrupted conversion.'))
        oform.addRow(tr('Save to'), self.out_widget)
        self.load_check = QCheckBox(tr('Add the result to the project'), out)
        oform.addRow('', self.load_check)
        oform.addRow(note(tr(
            'A conversion is done in blocks with a checkpoint, so an '
            'interrupted run continues where it stopped.'), out))

        # Advanced
        adv = self.card()
        aform = QFormLayout(adv)
        self.block_spin = QSpinBox(adv)
        self.block_spin.setRange(512, 32768)
        self.block_spin.setSingleStep(512)
        self.block_spin.setValue(4096)
        self.block_spin.setToolTip(tr(
            'How much of the source is converted between checkpoints. Smaller '
            'loses less work when interrupted but costs more overhead. Changing '
            'it invalidates an existing checkpoint.'))
        aform.addRow(tr('Processing block size (px)'), self.block_spin)
        self.threads_box = QComboBox(adv)
        self.threads_box.addItems(R.THREADS)
        self.threads_box.setToolTip(tr(
            'Threads GDAL may use. Drop it below ALL_CPUS to leave the machine '
            'usable during a long conversion.'))
        aform.addRow(tr('GDAL threads'), self.threads_box)
        self.resume_check = QCheckBox(tr('Resume from an existing checkpoint'), adv)
        self.resume_check.setChecked(True)
        self.resume_check.setToolTip(tr(
            'Off: blocks already on disk are thrown away and the conversion '
            'starts over.'))
        aform.addRow('', self.resume_check)

        return [(fmt, tr('Format'), tr('Output format and its options')),
                (out, tr('Output'), tr('Where the converted raster goes')),
                (adv, tr('Advanced'), tr('Block size, threads and resuming'))]

    def after_build(self):
        self._built = True
        self._on_format()
        self._on_source()

    def locked_widgets(self):
        return [self.layer_box, self.file_widget, self.format_box, self.comp_box,
                self.quality_spin, self.over_check, self.out_widget,
                self.load_check, self.block_spin, self.threads_box,
                self.resume_check]

    # --------------------------------------------------------------- reacting

    def _spec(self):
        return R.FORMAT_SPECS[self.format_box.currentIndex()]

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def _resolve_source(self):
        path = self.file_widget.filePath().strip()
        if path:
            layer = QgsRasterLayer(path, os.path.basename(path))
            return layer if layer.isValid() else None
        layer = self.layer_box.currentLayer()
        return layer if isinstance(layer, QgsRasterLayer) else None

    def _source_text(self):
        """What to hand the algorithm: the file typed, else the layer's id."""
        path = self.file_widget.filePath().strip()
        if path:
            return path
        layer = self.layer_box.currentLayer()
        return layer.id() if layer is not None else ''

    def _on_source(self, *_):
        if not self._built:
            return
        path = self.file_widget.filePath().strip()
        self._source = self._resolve_source()
        if path and self._source is None:
            self._say(tr('GDAL cannot read that file as a raster.'), 'bad')
        elif self._source is None:
            self._say(tr('Choose the raster to convert.'))
        else:
            layer = self._source
            self._say(tr('{0} x {1} px  -  {2} band(s)  -  {3}  -  {4}').format(
                format(layer.width(), ','), format(layer.height(), ','),
                layer.bandCount(), layer.crs().authid() or tr('no CRS'),
                os.path.basename(self._alg.source_path(layer)) or layer.name()), 'good')
            self._suggest_output()
        self._check_format()
        self.later_resize()

    def _on_format(self, *_):
        if not self._built:
            return
        spec = self._spec()
        self.comp_box.setEnabled(spec['compress'])
        self.quality_spin.setEnabled(spec['quality'])
        self.over_check.setEnabled(spec['overviews'])
        self._suggest_output()
        self._check_format()

    def _check_format(self):
        """Say now whether this format can take this raster, not after a long run."""
        spec = self._spec()
        label = R.FORMAT_LABELS[R.FORMAT_SPECS.index(spec)]
        driver, why = R.resolve_driver(spec)
        text = ''
        if driver is None:
            text = tr('This QGIS cannot write {0}: {1}. GeoTIFF is the format '
                      'every build can write.').format(label, why)
        elif self._source is not None:
            text = self._alg.format_rejects(self._source, spec, driver, label)
        self.format_note.setText(text)
        self.format_note.setStyleSheet('color: %s;' % RED if text else '')
        self.format_note.setVisible(bool(text))
        self.later_resize()

    def _suggest_output(self):
        if self._source is None:
            return
        current = self.out_widget.filePath().strip()
        if current and current != self._suggested:
            return
        source = self._alg.source_path(self._source)
        folder = os.path.dirname(source) if source else os.path.expanduser('~/Documents')
        stem = os.path.splitext(os.path.basename(source or self._source.name()))[0]
        self._suggested = os.path.join(folder, stem + '_converted' + self._spec()['ext'])
        self.out_widget.setFilePath(self._suggested)

    # -------------------------------------------------------------------- run

    def problem(self):
        self._source = self._resolve_source()
        if self._source is None:
            return tr('Choose a raster to convert.')
        spec = self._spec()
        label = R.FORMAT_LABELS[R.FORMAT_SPECS.index(spec)]
        driver, why = R.resolve_driver(spec)
        if driver is None:
            return tr('This QGIS cannot write {0}: {1}.').format(label, why)
        rejected = self._alg.format_rejects(self._source, spec, driver, label)
        if rejected:
            return rejected
        if not self.out_widget.filePath().strip():
            return tr('Choose where to save the converted raster.')
        return None

    def parameters(self):
        K = R.RasterFormatConverter
        return {
            K.INPUT: self._source_text(),
            K.FORMAT: self.format_box.currentIndex(),
            K.OUTPUT: self.out_widget.filePath().strip(),
            K.COMPRESSION: self.comp_box.currentIndex(),
            K.QUALITY: self.quality_spin.value(),
            K.OVERVIEWS: self.over_check.isChecked(),
            K.LOAD: self.load_check.isChecked(),
            K.BLOCK_SIZE: self.block_spin.value(),
            K.THREADS: self.threads_box.currentIndex(),
            K.RESUME: self.resume_check.isChecked(),
        }

    def done_ok(self, results):
        K = R.RasterFormatConverter
        self.log.appendPlainText('\n' + tr('Done. {0} block(s) converted: {1}').format(
            results.get(K.BLOCKS_DONE, '?'), results.get(K.OUTPUT_PATH, '')))

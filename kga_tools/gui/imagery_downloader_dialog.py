# -*- coding: utf-8 -*-
"""Imagery Downloader dialog: UI and wiring only.

Everything that can be tested without a GUI lives in ``core.imagery``. This
window resolves the extent, shows the live details panel, and runs one
``ImageryTask`` at a time. It is modeless and lives until closed.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import dataclasses
import math
import os
import shutil
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem,
                       QgsMessageLog, QgsProject, QgsSettings, QgsTask)
from qgis.gui import (QgsFileWidget, QgsMapLayerComboBox,
                      QgsProjectionSelectionWidget)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QSlider, QSpinBox, QVBoxLayout)

from ..branding import LOG_TAG, open_docs
from ..core import net
from ..core.imagery import checkpoint as ck
from ..core.imagery import downloader, estimator, exporter, extent, paths
from ..core.imagery import layer_loader as imagery_loader
from ..core.imagery import sampling
from ..core.imagery import tile_math as tm
from ..core.imagery.job import JobSpec
from ..core.imagery.sources import SOURCES, get_source
from ..core.modify_features import layer_filter
from .imagery_extent_tool import DrawBoxMapTool, make_box_band, show_box
from .roi_tools import drop_band

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

SETTINGS = 'kga_tools/imagery_downloader'

MODE_CANVAS, MODE_LAYER, MODE_DRAW, MODE_POLYGON = range(4)
# Colours sampled from icons/kga.png
BLUE = '#174592'
NAVY = '#1B3665'
ORANGE = '#FC663A'
AMBER = '#D06927'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE}


def tr(text):
    return text


def _human_time(seconds):
    if seconds is None or seconds < 0:
        return '--'
    seconds = int(seconds)
    if seconds < 60:
        return '%d s' % seconds
    if seconds < 3600:
        return '%d min' % round(seconds / 60.0)
    return '%d h %02d min' % (seconds // 3600, (seconds % 3600) // 60)


class ImageryDownloaderDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('KGA Imagery Downloader'))
        self.setObjectName('KgaImageryDownloaderDialog')
        self.resize(540, 700)
        self.setStyleSheet(STYLE)

        self.canvas = iface.mapCanvas() if iface is not None else None
        self._task = None
        self._spec = None
        self._closing = False
        self._torn_down = False
        self._loading = True
        self._box = None                    # drawn box, (w, s, e, n) in 4326
        self._band = None
        self._tool = None
        self._previous_tool = None
        self._est = None
        self._area = None
        self._area_error = ''
        self._polygon_cache = {}
        self._sample = None                 # {'avg': bytes, 'blank': bool}
        self._sample_key = None
        self._sample_task = None
        self._last_percent = 0
        self._lockable = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addLayout(self._build_source())
        root.addWidget(self._build_extent())
        root.addLayout(self._build_options())
        root.addWidget(self._build_details(), 1)
        root.addLayout(self._build_output())
        root.addLayout(self._build_progress())
        root.addLayout(self._build_buttons())

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._refresh_details)

        # A combo sizes itself to its longest entry (a CRS name is very long),
        # which would make the whole window wider than the screen allows.
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)

        self._restore()
        self._loading = False
        self._connect_project()
        self._on_source_changed()
        self._on_mode_changed()
        QTimer.singleShot(0, self._refresh_details)

    # ------------------------------------------------------------- building

    def _lock(self, *widgets):
        self._lockable.extend(widgets)

    def _build_source(self):
        box = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel(tr('Imagery source')))
        self.source_combo = QComboBox(self)
        for src in SOURCES:
            self.source_combo.addItem(src.name, src.id)
        row.addWidget(self.source_combo, 1)
        box.addLayout(row)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self._lock(self.source_combo)
        return box

    def _build_extent(self):
        group = QGroupBox(tr('Download extent'), self)
        grid = QVBoxLayout(group)
        self.mode_group = QButtonGroup(self)

        def line(mode, text):
            radio = QRadioButton(text, group)
            self.mode_group.addButton(radio, mode)
            row = QHBoxLayout()
            row.addWidget(radio)
            return radio, row

        self.radio_canvas, row = line(MODE_CANVAS, tr('Map canvas extent'))
        row.addStretch(1)
        self.refresh_btn = QPushButton(tr('Refresh'), group)
        self.refresh_btn.setToolTip(tr('Re-read the current map view'))
        self.refresh_btn.clicked.connect(self._force_refresh)
        row.addWidget(self.refresh_btn)
        grid.addLayout(row)

        self.radio_layer, row = line(MODE_LAYER, tr('Layer extent'))
        self.layer_combo = QgsMapLayerComboBox(group)
        self.layer_combo.setFilters(layer_filter('HasGeometry', 'RasterLayer'))
        row.addWidget(self.layer_combo, 1)
        grid.addLayout(row)

        self.radio_draw, row = line(MODE_DRAW, tr('Draw box on map'))
        self.draw_btn = QPushButton(tr('Draw'), group)
        self.draw_btn.clicked.connect(self._start_draw)
        self.box_edit = QLineEdit(group)
        self.box_edit.setReadOnly(True)
        self.box_edit.setPlaceholderText(tr('W, S, E, N'))
        row.addWidget(self.draw_btn)
        row.addWidget(self.box_edit, 1)
        grid.addLayout(row)

        self.radio_poly, row = line(MODE_POLYGON, tr('Clip to polygon'))
        self.poly_combo = QgsMapLayerComboBox(group)
        self.poly_combo.setFilters(layer_filter('PolygonLayer'))
        row.addWidget(self.poly_combo, 1)
        grid.addLayout(row)
        self.selected_check = QCheckBox(tr('Selected features only'), group)
        indent = QHBoxLayout()
        indent.addSpacing(24)
        indent.addWidget(self.selected_check)
        grid.addLayout(indent)

        self.mode_group.idClicked.connect(self._on_mode_changed)
        self.layer_combo.layerChanged.connect(self._schedule)
        self.poly_combo.layerChanged.connect(self._poly_changed)
        self.selected_check.toggled.connect(self._poly_changed)
        self._lock(group)
        return group

    def _build_options(self):
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        zoom_row = QHBoxLayout()
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_spin = QSpinBox(self)
        self.zoom_slider.valueChanged.connect(self.zoom_spin.setValue)
        self.zoom_spin.valueChanged.connect(self.zoom_slider.setValue)
        self.zoom_spin.valueChanged.connect(self._on_zoom)
        self.res_label = QLabel(self)
        self.auto_btn = QPushButton(tr('Auto'), self)
        self.auto_btn.setToolTip(tr('Pick the zoom that matches the map scale'))
        self.auto_btn.clicked.connect(self._auto_zoom)
        zoom_row.addWidget(self.zoom_slider, 1)
        zoom_row.addWidget(self.zoom_spin)
        zoom_row.addWidget(self.res_label)
        zoom_row.addWidget(self.auto_btn)
        form.addRow(tr('Zoom level'), zoom_row)

        crs_row = QHBoxLayout()
        self.crs_project = QRadioButton(tr('Project CRS'), self)
        self.crs_selected = QRadioButton(tr('Selected'), self)
        self.crs_project.setChecked(True)
        self.crs_widget = QgsProjectionSelectionWidget(self)
        self.crs_widget.setCrs(QgsProject.instance().crs())
        crs_row.addWidget(self.crs_project)
        crs_row.addWidget(self.crs_selected)
        crs_row.addWidget(self.crs_widget, 1)
        form.addRow(tr('Output CRS'), crs_row)
        self.crs_project.toggled.connect(self._on_crs_mode)
        self.crs_widget.crsChanged.connect(self._schedule)

        self.format_combo = QComboBox(self)
        for key in exporter.available_formats():
            self.format_combo.addItem(exporter.FORMATS[key][0], key)
        self.format_combo.currentIndexChanged.connect(self._on_format)
        form.addRow(tr('Format'), self.format_combo)

        opt = QHBoxLayout()
        self.pyramids_check = QCheckBox(tr('Build pyramids'), self)
        self.pyramids_check.setChecked(True)
        self.transparent_check = QCheckBox(
            tr('Transparent outside polygon'), self)
        self.transparent_check.setChecked(True)
        self.transparent_check.setToolTip(
            tr('Where the format supports it; otherwise white'))
        opt.addWidget(self.pyramids_check)
        opt.addWidget(self.transparent_check)
        opt.addStretch(1)
        opt.addWidget(QLabel(tr('Connections')))
        self.threads_spin = QSpinBox(self)
        self.threads_spin.setRange(1, 16)
        self.threads_spin.setValue(8)
        opt.addWidget(self.threads_spin)
        form.addRow(tr('Options'), opt)
        self.pyramids_check.toggled.connect(self._schedule)
        self.transparent_check.toggled.connect(self._schedule)
        self._lock(self.zoom_slider, self.zoom_spin, self.auto_btn,
                   self.crs_project, self.crs_selected, self.crs_widget,
                   self.format_combo, self.pyramids_check,
                   self.transparent_check, self.threads_spin)
        return form

    def _build_details(self):
        group = QGroupBox(tr('Details'), self)
        lay = QVBoxLayout(group)
        self.details = QLabel(group)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop |
                                  Qt.AlignmentFlag.AlignLeft)
        self.details.setMinimumHeight(130)
        lay.addWidget(self.details, 1)
        row = QHBoxLayout()
        self.estimate_btn = QPushButton(tr('Refresh estimate'), group)
        self.estimate_btn.setToolTip(
            tr('Download a few sample tiles to measure their size'))
        self.estimate_btn.clicked.connect(self._sample_now)
        row.addStretch(1)
        row.addWidget(self.estimate_btn)
        lay.addLayout(row)
        return group

    def _build_output(self):
        form = QFormLayout()
        self.folder_widget = QgsFileWidget(self)
        self.folder_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.folder_widget.fileChanged.connect(self._schedule)
        form.addRow(tr('Output folder'), self.folder_widget)
        self.name_edit = QLineEdit(self)
        self.name_edit.textEdited.connect(self._name_edited)
        self.name_edit.textChanged.connect(self._schedule)
        form.addRow(tr('File name'), self.name_edit)
        self.checkpoint_check = QCheckBox(
            tr('Enable checkpoint (resume if interrupted)'), self)
        self.checkpoint_check.setChecked(True)
        form.addRow('', self.checkpoint_check)
        self._name_touched = False
        self._lock(self.folder_widget, self.name_edit, self.checkpoint_check)
        return form

    def _build_progress(self):
        box = QVBoxLayout()
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat('%p%')
        self.status = QLabel(tr('Ready.'), self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color: palette(mid);')
        box.addWidget(self.bar)
        box.addWidget(self.status)
        return box

    def _build_buttons(self):
        row = QHBoxLayout()
        self.help_btn = QPushButton(tr('Help'), self)
        self.help_btn.clicked.connect(self._show_help)
        self.go_btn = QPushButton(tr('Download'), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setProperty('running', False)
        self.go_btn.clicked.connect(self._go_or_stop)
        self.close_btn = QPushButton(tr('Close'), self)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.help_btn)
        row.addStretch(1)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        return row

    # ------------------------------------------------------------- settings

    def _restore(self):
        """Every opening starts from the same defaults; nothing is remembered.

        Earlier versions stored the last choices under SETTINGS, so that
        leftover is removed here once and for all.
        """
        QgsSettings().remove(SETTINGS)
        self._on_source_changed()
        self.radio_canvas.setChecked(True)
        self.zoom_spin.setValue(18)
        self._on_crs_mode()
        self._update_name_default()

    # ---------------------------------------------------------- small state

    def _source(self):
        return get_source(self.source_combo.currentData())

    def _fmt(self):
        return self.format_combo.currentData() or 'tif'

    def _running(self):
        return self._task is not None

    def _schedule(self, *_args):
        if self._loading or self._torn_down:
            return
        self._debounce.start()

    def _force_refresh(self):
        self._polygon_cache.clear()
        self._refresh_details()

    def _connect_project(self):
        project = QgsProject.instance()
        project.crsChanged.connect(self._on_project_crs)
        if self.canvas is not None:
            self.canvas.extentsChanged.connect(self._on_canvas_extent)
            self.canvas.destinationCrsChanged.connect(self._on_canvas_crs)

    def _disconnect_project(self):
        pairs = [(QgsProject.instance().crsChanged, self._on_project_crs)]
        if self.canvas is not None:
            pairs += [(self.canvas.extentsChanged, self._on_canvas_extent),
                      (self.canvas.destinationCrsChanged, self._on_canvas_crs)]
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_project_crs(self, *_a):
        self._schedule()

    def _on_canvas_extent(self, *_a):
        if self.mode_group.checkedId() == MODE_CANVAS and not self._running():
            self._schedule()

    def _on_canvas_crs(self, *_a):
        if self._band is not None:
            show_box(self.canvas, self._band, self._box)
        self._schedule()

    def _on_source_changed(self, *_a):
        src = self._source()
        if src is None:
            return
        lo, hi = src.min_zoom, src.max_zoom
        self.zoom_slider.setRange(lo, hi)
        self.zoom_spin.setRange(lo, hi)
        self._sample = None
        self._update_name_default()
        self._schedule()

    def _on_mode_changed(self, *_a):
        mode = self.mode_group.checkedId()
        self.layer_combo.setEnabled(mode == MODE_LAYER)
        self.draw_btn.setEnabled(mode == MODE_DRAW)
        self.poly_combo.setEnabled(mode == MODE_POLYGON)
        self.selected_check.setEnabled(mode == MODE_POLYGON)
        self.refresh_btn.setEnabled(mode == MODE_CANVAS)
        self.transparent_check.setEnabled(mode == MODE_POLYGON)
        if mode != MODE_DRAW and self._band is not None:
            show_box(self.canvas, self._band, None)
        elif mode == MODE_DRAW and self._band is not None:
            show_box(self.canvas, self._band, self._box)
        self._schedule()

    def _poly_changed(self, *_a):
        self._polygon_cache.clear()
        self._schedule()

    def _on_crs_mode(self, *_a):
        self.crs_widget.setEnabled(self.crs_selected.isChecked() and
                                   not self._running())
        self._schedule()

    def _on_format(self, *_a):
        self.pyramids_check.setEnabled(self._fmt() == 'tif' and
                                       not self._running())
        self._update_name_default()
        self._schedule()

    def _on_zoom(self, *_a):
        self._update_name_default()
        self._update_res_label()
        self._schedule()

    def _name_edited(self, _text):
        self._name_touched = True

    def _update_name_default(self):
        src = self._source()
        if src is None or self._name_touched:
            return
        self.name_edit.blockSignals(True)
        self.name_edit.setText('%s_z%d' % (src.id, self.zoom_spin.value()))
        self.name_edit.blockSignals(False)

    def _update_res_label(self):
        lat = 0.0
        if self._area is not None:
            lat = (self._area.bbox[1] + self._area.bbox[3]) / 2.0
        elif self.canvas is not None:
            try:
                lat = extent.canvas_area(self.canvas).bbox[1]
            except extent.AreaError:
                lat = 0.0
        res = tm.ground_resolution(self.zoom_spin.value(), lat)
        self.res_label.setText('~%.2f m/px' % res)

    def _auto_zoom(self):
        if self.canvas is None:
            return
        try:
            area = extent.canvas_area(self.canvas)
        except extent.AreaError:
            return
        lat = (area.bbox[1] + area.bbox[3]) / 2.0
        dpi = self.canvas.logicalDpiX() or 96
        # ground metres per screen pixel at the current scale
        res = self.canvas.scale() * 0.0254 / dpi
        base = tm.ground_resolution(0, lat)
        if res <= 0 or base <= 0:
            return
        self.zoom_spin.setValue(int(round(math.log(base / res, 2))))

    # -------------------------------------------------------------- drawing

    def _start_draw(self):
        if self.canvas is None:
            return
        if self._tool is None:
            self._tool = DrawBoxMapTool(self.canvas)
            self._tool.drawn.connect(self._on_box_drawn)
            self._tool.cancelled.connect(self._end_draw)
        current = self.canvas.mapTool()
        if current is not None and current is not self._tool:
            self._previous_tool = current
        self.canvas.setMapTool(self._tool)
        self.hide()
        if iface is not None:
            iface.messageBar().pushInfo(
                tr('Imagery Downloader'),
                tr('Drag a box on the map. Press Esc to cancel.'))

    def _end_draw(self):
        if self.canvas is not None and self._tool is not None and \
                self.canvas.mapTool() is self._tool:
            prev, self._previous_tool = self._previous_tool, None
            try:
                if prev is not None:
                    self.canvas.setMapTool(prev)
                else:
                    self.canvas.unsetMapTool(self._tool)
            except RuntimeError:
                self.canvas.unsetMapTool(self._tool)
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_box_drawn(self, geometry):
        try:
            area = extent.rect_area(
                geometry.boundingBox(),
                self.canvas.mapSettings().destinationCrs(), 'Drawn box')
        except extent.AreaError as exc:
            self.status.setText(str(exc))
            self._end_draw()
            return
        self._box = area.bbox
        w, s, e, n = self._box
        self.box_edit.setText('%.5f, %.5f, %.5f, %.5f' % (w, s, e, n))
        if self._band is None:
            self._band = make_box_band(self.canvas)
        show_box(self.canvas, self._band, self._box)
        self.radio_draw.setChecked(True)
        self._end_draw()
        self._schedule()

    # ------------------------------------------------------- area / details

    def _resolve_area(self):
        mode = self.mode_group.checkedId()
        if mode == MODE_CANVAS:
            return extent.canvas_area(self.canvas)
        if mode == MODE_LAYER:
            return extent.layer_area(self.layer_combo.currentLayer())
        if mode == MODE_DRAW:
            if self._box is None:
                raise extent.AreaError(
                    tr('Click Draw, then drag a box on the map.'))
            return extent.Area(self._box, None, 'Drawn box')
        layer = self.poly_combo.currentLayer()
        selected = self.selected_check.isChecked()
        key = (layer.id() if layer else None, selected,
               tuple(sorted(layer.selectedFeatureIds())) if layer and
               selected else ())
        if key not in self._polygon_cache:
            self._polygon_cache = {
                key: extent.polygon_area(layer, selected)}
        return self._polygon_cache[key]

    def _output_crs(self):
        if self.crs_selected.isChecked():
            return self.crs_widget.crs()
        crs = QgsProject.instance().crs()
        return crs if crs.isValid() else QgsCoordinateReferenceSystem(
            'EPSG:4326')

    @staticmethod
    def _crs_text(crs):
        return crs.authid() or crs.toWkt()

    def _refresh_details(self):
        if self._torn_down or self._running():
            return
        src = self._source()
        self._area, self._area_error = None, ''
        try:
            self._area = self._resolve_area()
        except extent.AreaError as exc:
            self._area_error = str(exc)
        except Exception as exc:            # a broken layer must not break the box
            self._area_error = 'Could not read the extent: %s' % exc
        self._update_res_label()
        crs = self._output_crs()
        fmt = self._fmt()
        est = None
        if self._area is not None and src is not None:
            free = downloader.free_bytes(self.folder_widget.filePath() or '.')
            sample = self._sample if self._sample_key == self._key() else None
            est = estimator.estimate(
                self._area.bbox, self.zoom_spin.value(), src, fmt,
                transform_bounds=extent.bounds_transformer(crs),
                output_is_geographic=crs.isGeographic(),
                has_mask=bool(self._area.mask_wkt),
                avg_tile_bytes=sample['avg'] if sample else None,
                free_bytes=free, area_km2=extent.geodesic_area_km2(self._area),
                sample_blank=bool(sample and sample['blank']),
                format_available=exporter.driver_for(fmt) is not None)
        self._est = est
        self._render(est, crs, src)

    def _key(self):
        return (self.source_combo.currentData(), self.zoom_spin.value(),
                self._area.bbox if self._area else None)

    def _render(self, est, crs, src):
        lines, ok = [], False
        if est is None:
            lines.append('<span style="color:%s">%s</span>' % (
                AMBER, self._area_error or tr('No extent.')))
        else:
            approx = ' (approx.)' if not (self._sample and
                                          self._sample_key == self._key()) \
                else ' (from sample)'
            w, s, e, n = est.bbox
            lines.append('Extent (W,S,E,N): %.5f, %.5f, %.5f, %.5f' %
                         (w, s, e, n))
            if est.tiles:
                lines.append('Area: %s km&sup2;&nbsp;&nbsp; Tiles: %s (%s x %s)'
                             % (self._fmt_num(est.area_km2), '{:,}'.format(
                                 est.tiles), est.cols, est.rows))
                lines.append('Image: %s x %s px&nbsp;&nbsp; Res: %.2f m/px' %
                             ('{:,}'.format(est.out_w), '{:,}'.format(est.out_h),
                              est.res_m))
                lines.append('Est. download: ~%s%s&nbsp;&nbsp; Est. output: ~%s'
                             ' (approx.)' % (
                                 estimator.human_size(est.download_bytes),
                                 approx,
                                 estimator.human_size(est.output_bytes)))
                free = downloader.free_bytes(
                    self.folder_widget.filePath() or '.')
                lines.append('CRS: %s&nbsp;&nbsp; Free disk: %s (work file '
                             '~%s)' % (crs.authid() or 'custom',
                                       estimator.human_size(free)
                                       if free is not None else '?',
                                       estimator.human_size(est.work_bytes)))
            for text in est.blockers:
                lines.append('<span style="color:%s"><b>&#9888; %s</b></span>'
                             % (AMBER, text))
            for text in est.warnings:
                lines.append('<span style="color:%s">&#9888; %s</span>' %
                             (AMBER, text))
            ok = est.valid
        self.details.setText('<br>'.join(lines))
        self._update_go_enabled(ok)

    @staticmethod
    def _fmt_num(value):
        return '{:,.1f}'.format(value) if value < 1000 else '{:,.0f}'.format(
            value)

    def _update_go_enabled(self, ok=None):
        if ok is None:
            ok = bool(self._est and self._est.valid)
        if self._running():
            self.go_btn.setEnabled(True)
            return
        self.go_btn.setEnabled(bool(ok))

    # -------------------------------------------------------------- sampling

    def _sample_now(self):
        src, est = self._source(), self._est
        if src is None or est is None or not est.tile_range or \
                self._sample_task is not None:
            return
        key = self._key()
        self.status.setText(tr('Fetching sample tiles...'))
        self.estimate_btn.setEnabled(False)
        rng, zoom = est.tile_range, self.zoom_spin.value()

        def work(task):
            return sampling.sample_tiles(src, rng, zoom, task.isCanceled)

        def done(exception, value=None):
            self._sample_task = None
            self.estimate_btn.setEnabled(True)
            if self._torn_down:
                return
            if exception or not value or not value['sizes']:
                self.status.setText(tr(
                    'Could not fetch sample tiles. Check the connection.'))
                return
            self._sample = {'avg': sum(value['sizes']) / len(value['sizes']),
                            'blank': value['blank'] >= max(
                                1, len(value['sizes']) // 2)}
            self._sample_key = key
            self.status.setText(tr('Sample of %d tiles measured.') %
                                len(value['sizes']))
            self._refresh_details()

        self._sample_task = QgsTask.fromFunction(
            'KGA imagery sample', work, on_finished=done)
        QgsApplication.taskManager().addTask(self._sample_task)

    # -------------------------------------------------------------- download

    def _go_or_stop(self):
        if self._running():
            self.status.setText(tr('Stopping...'))
            self.go_btn.setEnabled(False)
            self._task.request_stop()
            return
        self._start()

    def _warn(self, text):
        QMessageBox.warning(self, tr('Imagery Downloader'), text)

    def _start(self):
        est, src = self._est, self._source()
        if est is None or not est.valid or src is None:
            return
        name = self.name_edit.text().strip()
        problem = paths.name_problem(name)
        if problem:
            self._warn(problem)
            return
        folder = os.path.normpath(self.folder_widget.filePath().strip())
        problem = paths.folder_problem(self.folder_widget.filePath().strip())
        if problem:
            self._warn(problem)
            return
        try:
            self._resolve_fresh_area()
        except extent.AreaError as exc:
            self._warn(str(exc))
            return
        est = self._est

        crs = self._output_crs()
        fmt = self._fmt()
        area = self._area
        spec = JobSpec(
            source=src, zoom=self.zoom_spin.value(), tile_range=est.tile_range,
            bbox=est.bbox, output_crs=self._crs_text(crs), fmt=fmt,
            folder=folder, name=name, mask_wkt=area.mask_wkt,
            checkpoint_enabled=self.checkpoint_check.isChecked(),
            workers=self.threads_spin.value(),
            pyramids=self.pyramids_check.isChecked() and fmt == 'tif',
            plugin_version=net.plugin_version(), layer_name=name,
            transparent=self.transparent_check.isChecked())

        final = exporter.output_path(folder, name, fmt)
        if os.path.exists(final):
            answer = QMessageBox.question(
                self, tr('Overwrite?'),
                tr('%s already exists. Replace it?') % os.path.basename(final))
            if answer != QMessageBox.StandardButton.Yes:
                return

        free = downloader.free_bytes(folder)
        need = est.work_bytes + est.output_bytes
        if free is not None and free < need:
            answer = QMessageBox.question(
                self, tr('Low disk space'),
                tr('About %s is needed for the work file and the result but '
                   'only %s is free. Continue anyway?') % (
                    estimator.human_size(need), estimator.human_size(free)))
            if answer != QMessageBox.StandardButton.Yes:
                return

        if spec.checkpoint_enabled:
            if not self._check_existing(spec):
                return
        else:
            shutil.rmtree(spec.work_dir, ignore_errors=True)
        self._launch(spec)

    def _resolve_fresh_area(self):
        """Re-read the extent at click time (the canvas may have moved)."""
        self._polygon_cache.clear()
        self._area = self._resolve_area()
        crs = self._output_crs()
        src = self._source()
        self._est = estimator.estimate(
            self._area.bbox, self.zoom_spin.value(), src, self._fmt(),
            transform_bounds=extent.bounds_transformer(crs),
            output_is_geographic=crs.isGeographic(),
            has_mask=bool(self._area.mask_wkt),
            area_km2=extent.geodesic_area_km2(self._area),
            avg_tile_bytes=(self._sample['avg'] if self._sample and
                            self._sample_key == self._key() else None),
            format_available=exporter.driver_for(self._fmt()) is not None)
        if not self._est.valid:
            raise extent.AreaError(' '.join(self._est.blockers))

    def _check_existing(self, spec):
        """Handle a checkpoint already in the folder. False = do not start."""
        try:
            data = ck.read(spec.checkpoint_file)
        except ValueError as exc:
            answer = QMessageBox.question(
                self, tr('Unreadable checkpoint'),
                tr('%s\n\nStart over and discard the old files?') % exc)
            if answer != QMessageBox.StandardButton.Yes:
                return False
            ck.remove_job_files(spec.folder, spec.name)
            return True
        if data is None:
            return True
        if data.get('signature') == spec.signature and \
                os.path.exists(spec.work_file):
            try:
                done = ck.load_bitmap(data).count()
            except ValueError:
                done = 0
            pct = int(100.0 * done / max(1, spec.total))
            box = QMessageBox(self)
            box.setWindowTitle(tr('Resume download?'))
            box.setText(tr('A previous download with these settings is %d%% '
                           'complete (%s of %s tiles).') % (
                pct, '{:,}'.format(done), '{:,}'.format(spec.total)))
            resume = box.addButton(tr('Resume'),
                                   QMessageBox.ButtonRole.AcceptRole)
            fresh = box.addButton(tr('Start over'),
                                  QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is resume:
                return True
            if clicked is fresh:
                ck.remove_job_files(spec.folder, spec.name)
                return True
            return False
        diffs = ck.describe_differences(
            data, spec.source.id, spec.zoom, spec.tile_range, spec.output_crs,
            spec.mask_hash, spec.fmt)
        box = QMessageBox(self)
        box.setWindowTitle(tr('Different download in this folder'))
        box.setText(tr('A checkpoint for "%s" exists but its settings differ: '
                       '%s.\n\nStarting over discards the old checkpoint.') % (
            spec.name, ', '.join(diffs) or tr('the work file is missing')))
        fresh = box.addButton(tr('Start over (discard old checkpoint)'),
                              QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is fresh:
            ck.remove_job_files(spec.folder, spec.name)
            return True
        return False

    def _launch(self, spec):
        self._spec = spec
        task = downloader.ImageryTask(spec)
        task.progressInfo.connect(self._on_progress)
        task.statusText.connect(self.status.setText)
        task.phase.connect(self._on_phase)
        task.jobFinished.connect(self._on_finished)
        self._task = task
        self._last_percent = 0
        self._set_running(True)
        self.bar.setValue(0)
        self.bar.setFormat('%p%')
        self.status.setText(tr('Starting...'))
        downloader.start(task)

    def _set_running(self, running):
        for widget in self._lockable:
            widget.setEnabled(not running)
        if not running:
            self._on_mode_changed()
            self._on_crs_mode()
            self._on_format()
        else:
            for w in (self.layer_combo, self.poly_combo, self.draw_btn,
                      self.selected_check, self.refresh_btn,
                      self.estimate_btn):
                w.setEnabled(False)
        self.estimate_btn.setEnabled(not running)
        self.radio_canvas.setEnabled(not running)
        self.radio_layer.setEnabled(not running)
        self.radio_draw.setEnabled(not running)
        self.radio_poly.setEnabled(not running)
        self.go_btn.setText(tr('Stop') if running else tr('Download'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True if running else bool(
            self._est and self._est.valid))

    def _on_progress(self, done, total, failed, rate, eta):
        pct = int(100.0 * done / max(1, total))
        self.bar.setValue(pct)
        self.bar.setFormat('%d%%   %s/%s' % (pct, '{:,}'.format(done),
                                             '{:,}'.format(total)))
        self.status.setText('%.1f tiles/s  |  ETA %s  |  %d failed' % (
            rate, _human_time(eta), failed))

    def _on_phase(self, text, fraction):
        """After the download the bar follows the build step (crop, write
        image, pyramids...), each running 0-100% under its own label."""
        if self._task is not None:
            self.bar.setValue(int(round(100.0 * fraction)))
            self.bar.setFormat('%s   %%p%%' % text.rstrip('.'))
            self.status.setText(tr('Building the image from the downloaded '
                                   'tiles...'))

    def _on_finished(self, result):
        spec = self._spec
        self._task = None
        if self._torn_down:
            return
        self._set_running(False)
        if self._closing:
            self._teardown()
            return
        state = result.get('state')
        message = result.get('message', '')
        if state == 'exported':
            self._on_exported(result, spec)
        elif state == 'stopped':
            self.status.setText(
                tr('Stopped. Progress is saved; press Download to resume.')
                if spec.checkpoint_enabled else tr('Stopped.'))
        elif state == 'incomplete':
            self._ask_failed(spec, result)
        elif state == 'throttled':
            self.status.setText(message)
            self._warn(message)
        else:
            self.status.setText(message or tr('The download failed.'))
            QgsMessageLog.logMessage(message, LOG_TAG, Qgis.MessageLevel.Warning)
            QMessageBox.critical(self, tr('Imagery Downloader'),
                                 message or tr('The download failed.'))

    def _on_exported(self, result, spec):
        self.bar.setValue(100)
        self.bar.setFormat('100%')
        try:
            imagery_loader.add_result(iface, result, spec)
        except imagery_loader.LoadError as exc:
            self.status.setText(str(exc))
            QMessageBox.critical(self, tr('Imagery Downloader'), str(exc))
            return
        show_box(self.canvas, self._band, None) if self._band else None
        self.status.setText(tr('Done. Added "%s" to the map.') % spec.name)

    def _ask_failed(self, spec, result):
        failed = result.get('failed', 0)
        box = QMessageBox(self)
        box.setWindowTitle(tr('Some tiles failed'))
        box.setText(tr('%d tiles could not be downloaded.') % failed)
        retry = box.addButton(tr('Retry failed tiles'),
                              QMessageBox.ButtonRole.AcceptRole)
        finish = box.addButton(tr('Finish anyway'),
                               QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is retry:
            self._launch(spec)
        elif clicked is finish:
            self._launch(dataclasses.replace(spec, export_only=True))
        else:
            self.status.setText(
                tr('%d tiles are missing. Press Download to retry them.') %
                failed)

    # ------------------------------------------------------------------ help

    def _show_help(self):
        open_docs('imagery_downloader')

    # --------------------------------------------------------------- closing

    def closeEvent(self, event):
        if self._running():
            if not self._confirm_stop():
                event.ignore()
                return
            event.ignore()                  # torn down when the task ends
            return
        self._teardown()
        super().closeEvent(event)

    def done(self, result):
        if self._running():
            self._confirm_stop()
            return
        self._teardown()
        super().done(result)

    def _confirm_stop(self):
        answer = QMessageBox.question(
            self, tr('Imagery Downloader'), tr('Stop the download and close?'))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._closing = True
        self.hide()
        if self._task is not None:
            self._task.request_stop()
        return True

    def shutdown(self):
        """Plugin unload: stop any job cleanly, then go away."""
        self._closing = True
        task = self._task
        if task is not None:
            task.request_stop()
            with suppress(Exception):
                task.waitForFinished(15000)
        if self._sample_task is not None:
            self._sample_task.cancel()
        self._task = None
        self._teardown()

    def _teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self._debounce.stop()
        self._disconnect_project()
        if self.canvas is not None:
            if self._tool is not None and self.canvas.mapTool() is self._tool:
                try:
                    if self._previous_tool is not None:
                        self.canvas.setMapTool(self._previous_tool)
                    else:
                        self.canvas.unsetMapTool(self._tool)
                except RuntimeError:
                    pass
            if self._tool is not None:
                self._tool.remove()
                self._tool = None
            drop_band(self.canvas, self._band)
            self._band = None
        self.deleteLater()

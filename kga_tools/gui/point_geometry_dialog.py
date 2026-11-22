# -*- coding: utf-8 -*-
"""Point to Geometry Converter: the window.

Same layout as the other KGA windows (Create Points From Table, Sliver and
Spike Cleaner): the input in a box on top, the settings in tabs whose height
follows the page shown, a Report tab that takes over when a run finishes, and
Help / Run / Close along the bottom. It opens out of the toolbar button with
the plugin's window animation (``window_reveal``).

UI and wiring only. The work is ``core.point_geometry``: a ``QgsTask`` reads
the points once, groups and orders them, builds and validates the lines or
polygons and writes them, so the map stays usable while it runs. The input
layer is never changed.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import html
import os
from contextlib import suppress

from qgis.core import (QgsApplication, QgsExpression, QgsProject,
                       QgsProviderRegistry, QgsSettings, QgsVectorLayer)
from qgis.gui import (QgsFieldComboBox, QgsFieldExpressionWidget, QgsFileWidget,
                      QgsMapLayerComboBox)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QSizePolicy, QStackedWidget, QTabWidget, QTextBrowser,
    QToolButton, QVBoxLayout, QWidget)

from .output_fields import FollowingField
from .window_reveal import revealed_preview
from ..branding import open_docs
from ..core import output_defaults as od
from ..core.modify_features import layer_filter
from ..core.point_geometry import config as C
from ..core.point_geometry import writer
from ..core.point_geometry.compat import is_point_layer, wkb_display_string
from ..core.point_geometry.task import ConversionTask

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'point_to_geometry'
TITLE = 'Point to Geometry Converter'
SETTINGS = 'KGA/point_to_geometry/'

# Colours sampled from icons/branding/kga.png, as in the other KGA windows.
BLUE = '#174592'
ORANGE = '#FC663A'
RED = '#B3261E'
GREEN = '#1E7B34'
AMBER = '#A86A00'
MUTED = '#6B6B6B'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QLabel#kgaNote { font-weight: normal; font-style: italic; }
QLabel#kgaWarn { font-weight: normal; color: %(amber)s; }
QLabel#kgaStatus { font-weight: normal; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE, 'amber': AMBER}

LINE_STRUCTURE_HELP = {
    C.LINE_CONTINUOUS: 'One continuous line per group: P1 - P2 - P3 - P4.',
    C.LINE_SEGMENTS: 'One line per consecutive pair (P1-P2, P2-P3 ...) with '
                     'seg_seq, from_fid and to_fid.',
}
POLYGON_OUTPUT_HELP = {
    C.POLY_ONLY: 'One closed polygon per group.',
    C.POLY_AND_BOUNDARY: 'A polygon layer plus a matching line layer '
                         '(<name>_boundary, joined on out_id).',
    C.POLY_BOUNDARY_ONLY: 'Closed boundary lines only (last point joined to '
                          'the first); no polygon layer is written.',
}


def tr(text):
    return QgsApplication.translate('KgaPointToGeometry', text)


def _note(text='', name='kgaNote', parent=None):
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def _decode(source):
    """(normalised file path, layer name) of an OGR source, or ('', '')."""
    with suppress(Exception):
        parts = QgsProviderRegistry.instance().decodeUri('ogr', source)
        path = parts.get('path', '')
        if path:
            return (os.path.normcase(os.path.abspath(path)),
                    parts.get('layerName') or '')
    return '', ''


def _safe(text):
    return ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in text)


@revealed_preview
class PointToGeometryDialog(QDialog):
    """An ordered point layer in; lines, segments or polygons out."""

    def __init__(self, parent=None):
        super().__init__(parent or (iface.mainWindow() if iface else None))
        self.setWindowTitle(tr(TITLE))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(580)

        self._torn_down = False
        self._closing = False
        self._task = None
        self._watched = None
        self._name_edited = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_input_card())
        root.addWidget(self._build_tabs())
        root.addStretch(1)
        root.addLayout(self._build_buttons())

        self._restore_settings()
        self._on_mode()
        self._on_order_method()
        self._on_format()
        self.refresh_layers()

    # --------------------------------------------------------------- building

    def _card(self, title):
        return QGroupBox(title, self)

    def _build_input_card(self):
        box = self._card(tr('Input'))
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        self.layer_combo = QgsMapLayerComboBox(box)
        self.layer_combo.setFilters(layer_filter('PointLayer'))
        self.layer_combo.setToolTip(tr(
            'Point or multipoint layer from the project. Each point becomes a '
            'vertex; a multipoint gives its parts as consecutive vertices. The '
            'layer itself is never changed.'))
        self.layer_combo.layerChanged.connect(self._on_layer)
        row.addWidget(self.layer_combo, 1)
        self.refresh_btn = QToolButton(box)
        self.refresh_btn.setIcon(QgsApplication.getThemeIcon('/mActionRefresh.svg'))
        self.refresh_btn.setToolTip(tr('Refresh the feature and selection counts'))
        self.refresh_btn.clicked.connect(self.refresh_layers)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)

        info = QHBoxLayout()
        self.layer_info = QLabel('', box)
        self.layer_info.setObjectName('kgaStatus')
        info.addWidget(self.layer_info, 1)
        self.selected_check = QCheckBox(tr('Selected features only'), box)
        self.selected_check.setToolTip(tr(
            'Use only the points selected on the map. The window is modeless: '
            'the count follows the selection while it is open.'))
        info.addWidget(self.selected_check)
        lay.addLayout(info)
        return box

    def _build_tabs(self):
        """One tab per section; the tab area is as tall as the page shown."""
        self.tabs = QTabWidget(self)
        sections = (
            (self._build_build_card(), tr('Build'),
             tr('Line or polygon, the vertex order and the grouping')),
            (self._build_options_card(), tr('Options'),
             tr('Validation, repair, duplicate vertices and attributes')),
            (self._build_output_card(), tr('Output'),
             tr('Where the result goes')),
            (self._build_report_card(), tr('Report'),
             tr('Progress and what happened in the last run')))
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

    def _build_build_card(self):
        box = self._card(tr('Build'))
        form = QFormLayout(box)

        modes = QHBoxLayout()
        self.line_radio = QRadioButton(tr('Line'), box)
        self.line_radio.setIcon(QgsApplication.getThemeIcon('/mIconLineLayer.svg'))
        self.line_radio.setToolTip(tr('Join the points into lines'))
        self.polygon_radio = QRadioButton(tr('Polygon'), box)
        self.polygon_radio.setIcon(QgsApplication.getThemeIcon('/mIconPolygonLayer.svg'))
        self.polygon_radio.setToolTip(tr(
            'Join the points into a ring; it is closed for you (last point '
            'back to the first)'))
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.line_radio)
        self.mode_group.addButton(self.polygon_radio)
        self.line_radio.setChecked(True)
        self.line_radio.toggled.connect(self._on_mode)
        modes.addWidget(self.line_radio)
        modes.addSpacing(16)
        modes.addWidget(self.polygon_radio)
        modes.addStretch(1)
        form.addRow(tr('Create'), modes)

        self.structure_label = QLabel(tr('Lines'), box)
        self.structure_stack = QStackedWidget(box)
        self.line_structure = QComboBox(box)
        self.line_structure.addItem(tr('Continuous polyline'), C.LINE_CONTINUOUS)
        self.line_structure.addItem(tr('Individual segments'), C.LINE_SEGMENTS)
        self.polygon_output = QComboBox(box)
        self.polygon_output.addItem(tr('Polygon'), C.POLY_ONLY)
        self.polygon_output.addItem(tr('Polygon + boundary line'), C.POLY_AND_BOUNDARY)
        self.polygon_output.addItem(tr('Boundary line only (closed line)'),
                                    C.POLY_BOUNDARY_ONLY)
        for combo, helps in ((self.line_structure, LINE_STRUCTURE_HELP),
                             (self.polygon_output, POLYGON_OUTPUT_HELP)):
            for i in range(combo.count()):
                combo.setItemData(i, tr(helps[combo.itemData(i)]),
                                  Qt.ItemDataRole.ToolTipRole)
            combo.currentIndexChanged.connect(self._on_structure)
            self.structure_stack.addWidget(combo)
        self.structure_stack.setSizePolicy(QSizePolicy.Policy.Expanding,
                                           QSizePolicy.Policy.Fixed)
        form.addRow(self.structure_label, self.structure_stack)
        self.structure_note = _note('', parent=box)
        form.addRow('', self.structure_note)

        order = QHBoxLayout()
        self.order_method = QComboBox(box)
        self.order_method.addItem(tr('Feature ID'), C.ORDER_FID)
        self.order_method.addItem(tr('Field'), C.ORDER_FIELD)
        self.order_method.addItem(tr('Expression'), C.ORDER_EXPRESSION)
        self.order_method.setToolTip(tr(
            'The exact vertex sequence. Ties are broken by feature ID; points '
            'with a NULL order value cannot be placed, so they are left out '
            'and listed in the report. The order of the rows in the file is '
            'never used.'))
        self.order_method.currentIndexChanged.connect(self._on_order_method)
        self.order_stack = QStackedWidget(box)
        self.order_stack.addWidget(QWidget())
        self.order_field = QgsFieldComboBox(box)
        self.order_field.setToolTip(tr('Points are joined in this field\'s order'))
        self.order_field.fieldChanged.connect(self._update_order_hint)
        self.order_stack.addWidget(self.order_field)
        self.order_expr = QgsFieldExpressionWidget(box)
        self.order_expr.setToolTip(tr(
            'Any QGIS expression, e.g. "timestamp" or to_int("label")'))
        self.order_stack.addWidget(self.order_expr)
        self.order_stack.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self.reverse_check = QCheckBox(tr('Descending'), box)
        self.reverse_check.setToolTip(tr(
            'Join the points from the highest order value to the lowest'))
        order.addWidget(self.order_method)
        order.addWidget(self.order_stack, 1)
        order.addWidget(self.reverse_check)
        form.addRow(tr('Order by'), order)
        self.order_hint = _note('', 'kgaWarn', box)
        self.order_hint.setVisible(False)
        form.addRow('', self.order_hint)

        self.group_field = QgsFieldComboBox(box)
        self.group_field.setAllowEmptyFieldName(True)
        self.group_field.setToolTip(tr(
            'Optional. Each distinct value becomes its own feature. Leave '
            'empty to build one feature from all the points.'))
        self.group_field.fieldChanged.connect(self._on_group)
        form.addRow(tr('Group by'), self.group_field)

        self.null_policy = QComboBox(box)
        self.null_policy.addItem(tr('Form one group'), C.NULL_GROUP_MERGE)
        self.null_policy.addItem(tr('Skip those points'), C.NULL_GROUP_SKIP)
        self.null_policy.setToolTip(tr(
            'For points whose group value is NULL, empty or only spaces. One '
            'group: they make one more feature (group value NULL, written '
            'last). Skip: they are left out and listed in the report.'))
        form.addRow(tr('Missing group value'), self.null_policy)
        return box

    def _build_options_card(self):
        box = self._card(tr('Options'))
        lay = QVBoxLayout(box)
        self.validate_check = QCheckBox(tr('Validate geometries'), box)
        self.validate_check.setToolTip(tr(
            'Reject empty, zero-length and zero-area results and check '
            'polygons with GEOS (self-intersections, ring problems). Rejected '
            'features are listed in the report, never dropped silently.'))
        self.repair_check = QCheckBox(tr('Repair invalid polygons'), box)
        self.repair_check.setToolTip(tr(
            'Use QGIS "make valid" on a self-intersecting ring (a bow-tie). '
            'Repaired features get repaired = 1; if any becomes multipart the '
            'whole layer is written as MultiPolygon.'))
        self.dups_check = QCheckBox(tr('Remove duplicate consecutive vertices'), box)
        self.dups_check.setToolTip(tr(
            'Consecutive points with exactly the same X and Y become one '
            'vertex (the first is kept). Z and M are not compared.'))
        self.keep_check = QCheckBox(tr('Keep attributes shared by all points of a feature'), box)
        self.keep_check.setToolTip(tr(
            'A value is copied only when every point of the output feature '
            'has it; otherwise it is NULL. Fields that differ in every '
            'feature are left out. The order field and fid are never copied.'))
        for check in (self.validate_check, self.repair_check, self.dups_check,
                      self.keep_check):
            check.setChecked(True)
            lay.addWidget(check)
        self.validate_check.toggled.connect(self._on_validate)
        lay.addWidget(_note(tr(
            'Lines need 2 distinct vertices and polygons 3; a group with fewer '
            'is reported and skipped. Z and M values are kept.'), parent=box))
        return box

    def _build_output_card(self):
        box = self._card(tr('Output'))
        form = QFormLayout(box)

        self.format_combo = QComboBox(box)
        self.format_combo.addItem(tr('GeoPackage (.gpkg)'), C.FORMAT_GPKG)
        self.format_combo.addItem(tr('Shapefile (.shp)'), C.FORMAT_SHP)
        self.format_combo.addItem(tr('Temporary layer'), C.FORMAT_MEMORY)
        self.format_combo.setToolTip(tr(
            'A temporary layer is lost when QGIS closes; use it to check the '
            'result first.'))
        self.format_combo.currentIndexChanged.connect(self._on_format)
        form.addRow(tr('Save as'), self.format_combo)

        self.file_label = QLabel(tr('File'), box)
        self.file_widget = QgsFileWidget(box)
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.file_widget.setConfirmOverwrite(False)     # asked before the run
        self.file_widget.lineEdit().setPlaceholderText(tr('Choose the output file'))
        self.file_widget.setToolTip(tr(
            'A GeoPackage keeps its other layers; only a layer of the same '
            'name is replaced, after you confirm.'))
        self.file_widget.fileChanged.connect(self._on_file)
        self._out = FollowingField(self.file_widget)
        form.addRow(self.file_label, self.file_widget)

        self.name_edit = QLineEdit(box)
        self.name_edit.setToolTip(tr(
            'Layer name in the project, and the table name inside a '
            'GeoPackage. With "Polygon + boundary line" the lines are written '
            'as <name>_boundary.'))
        self.name_edit.textEdited.connect(self._on_name_edited)
        form.addRow(tr('Layer name'), self.name_edit)

        self.load_check = QCheckBox(tr('Add the result to the map'), box)
        self.load_check.setChecked(True)
        form.addRow('', self.load_check)
        self.output_note = _note('', parent=box)
        form.addRow('', self.output_note)
        return box

    def _build_report_card(self):
        box = self._card(tr('Report'))
        lay = QVBoxLayout(box)
        self.bar = QProgressBar(box)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(16)
        lay.addWidget(self.bar)
        self.report = QTextBrowser(box)
        self.report.setOpenExternalLinks(False)
        self.report.setFixedHeight(230)
        self.report.setHtml("<p style='color:%s'>%s</p>" % (
            MUTED, html.escape(tr('Run a conversion to see its report here.'))))
        lay.addWidget(self.report)
        return box

    def _build_buttons(self):
        row = QHBoxLayout()
        self.help_btn = QPushButton(tr('Help'), self)
        self.help_btn.clicked.connect(lambda: open_docs(ALG_NAME))
        self.status = QLabel('', self)
        self.status.setObjectName('kgaStatus')
        self.go_btn = QPushButton(tr('Run'), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._go_or_stop)
        self.close_btn = QPushButton(tr('Close'), self)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.help_btn)
        row.addWidget(self.status, 1)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        return row

    # ---------------------------------------------------------------- sizing

    def showEvent(self, event):
        super().showEvent(event)
        if not self._running():
            self.refresh_layers()
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
        self.resize(self.width(), self.layout().totalSizeHint().height())

    # ---------------------------------------------------------------- reacting

    def layer(self):
        layer = self.layer_combo.currentLayer()
        return layer if isinstance(layer, QgsVectorLayer) and is_point_layer(layer) else None

    def set_layer(self, layer):
        """Pick `layer` if it is a point layer (used when the tool is reopened)."""
        if isinstance(layer, QgsVectorLayer) and is_point_layer(layer):
            self.layer_combo.setLayer(layer)

    def refresh_layers(self):
        """List only the point layers of the active map (every layer while Map Tabs is off)."""
        try:
            from . import map_tabs                      # not at import time: it pulls in a lot of the GUI
            ids = map_tabs.active_map_layer_ids()
        except Exception:                               # pragma: no cover - head-less
            ids = None
        keep = self.layer_combo.currentLayer()
        if ids is None:
            self.layer_combo.setExceptedLayerList([])
        else:
            self.layer_combo.setExceptedLayerList(
                [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.id() not in ids])
        if keep is not None and (ids is None or keep.id() in ids):
            self.layer_combo.setLayer(keep)
        self._on_layer()

    def _mode(self):
        return C.MODE_POLYGON if self.polygon_radio.isChecked() else C.MODE_LINE

    def _watch(self, layer):
        if self._watched is not None:
            with suppress(TypeError, RuntimeError):
                self._watched.selectionChanged.disconnect(self._update_info)
        self._watched = layer
        if layer is not None:
            layer.selectionChanged.connect(self._update_info)

    def _on_layer(self, *_):
        if self._torn_down:
            return
        layer = self.layer()
        self._watch(layer)
        for widget in (self.order_field, self.group_field, self.order_expr):
            widget.setLayer(layer)
        self._update_info()
        self._update_order_hint()
        self._on_group()
        self._propose_output()

    def _update_info(self, *_):
        if self._torn_down:
            return
        layer = self.layer()
        if layer is None:
            self.layer_info.setText(tr('No point layer in the project.'))
            self.selected_check.setText(tr('Selected features only'))
            self.go_btn.setEnabled(self._task is not None)
            return
        crs = layer.crs().authid() or tr('unknown CRS')
        self.layer_info.setText('{0}  -  {1} points  -  {2}'.format(
            wkb_display_string(layer.wkbType()),
            format(layer.featureCount(), ','), crs))
        self.selected_check.setText(tr('Selected features only ({0})').format(
            format(layer.selectedFeatureCount(), ',')))
        self.go_btn.setEnabled(True)

    def _on_mode(self, *_):
        polygon = self._mode() == C.MODE_POLYGON
        self.structure_stack.setCurrentIndex(1 if polygon else 0)
        self.structure_label.setText(tr('Output') if polygon else tr('Lines'))
        self._on_structure()

    def _on_structure(self, *_):
        if self._mode() == C.MODE_POLYGON:
            text = POLYGON_OUTPUT_HELP[self.polygon_output.currentData()]
        else:
            text = LINE_STRUCTURE_HELP[self.line_structure.currentData()]
        self.structure_note.setText(tr(text))
        self._on_validate()
        self._propose_output()
        self._later_resize()

    def _on_validate(self, *_):
        polygon = self._mode() == C.MODE_POLYGON
        self.repair_check.setEnabled(polygon and self.validate_check.isChecked())

    def _on_order_method(self, *_):
        method = self.order_method.currentData()
        self.order_stack.setCurrentIndex(
            {C.ORDER_FID: 0, C.ORDER_FIELD: 1, C.ORDER_EXPRESSION: 2}[method])
        self.reverse_check.setEnabled(True)
        self._update_order_hint()

    def _update_order_hint(self, *_):
        text = ''
        layer = self.layer()
        if (layer is not None and self.order_method.currentData() == C.ORDER_FIELD
                and self.order_field.currentField()):
            idx = layer.fields().lookupField(self.order_field.currentField())
            if idx >= 0:
                field = layer.fields().at(idx)
                if not field.isNumeric() and not field.isDateOrTime():
                    text = tr(
                        "Text field: it sorts alphabetically ('10' before '2'). "
                        'For numbers stored as text use Expression and '
                        'to_real("{0}").').format(field.name())
        if text != self.order_hint.text() or self.order_hint.isVisible() != bool(text):
            self.order_hint.setText(text)
            self.order_hint.setVisible(bool(text))
            self._later_resize()

    def _on_group(self, *_):
        self.null_policy.setEnabled(bool(self.group_field.currentField()))

    def _on_format(self, *_):
        fmt = self.format_combo.currentData()
        file_based = fmt != C.FORMAT_MEMORY
        self.file_label.setVisible(file_based)
        self.file_widget.setVisible(file_based)
        self.load_check.setEnabled(file_based)
        if not file_based:
            self.load_check.setChecked(True)    # a temporary layer is useless otherwise
        else:
            self.file_widget.setFilter('GeoPackage (*.gpkg)' if fmt == C.FORMAT_GPKG
                                       else 'Shapefile (*.shp)')
            path = self.file_widget.filePath().strip()
            if path and not self._out.is_ours():
                fixed = writer.normalize_output_path(os.path.splitext(path)[0], fmt)
                if fixed != path:
                    self.file_widget.setFilePath(fixed)
            elif path:
                # Our own proposal was for the other format: make a fresh one.
                self.file_widget.setFilePath('')
                self._out.last = ''
        self._propose_output()
        self._later_resize()

    def _on_file(self, *_):
        self._update_output_note()

    def _on_name_edited(self, text):
        self._name_edited = bool(text.strip())
        self._update_output_note()

    # ------------------------------------------------------- output proposal

    def _stem(self):
        layer = self.layer()
        base = _safe(layer.name()) if layer is not None else 'points'
        if self._mode() == C.MODE_POLYGON:
            suffix = ('boundary' if self.polygon_output.currentData() == C.POLY_BOUNDARY_ONLY
                      else 'polygon')
        else:
            suffix = ('segments' if self.line_structure.currentData() == C.LINE_SEGMENTS
                      else 'line')
        return '{0}_{1}'.format(base, suffix)

    def _fallback_folder(self):
        """Folder of the input file, or the last folder used, for a project-less session."""
        layer = self.layer()
        if layer is not None and layer.providerType() == 'ogr':
            path, _name = _decode(layer.source())
            if path and os.path.isdir(os.path.dirname(path)):
                return os.path.dirname(path)
        folder = QgsSettings().value(SETTINGS + 'last_dir', '')
        return folder if folder and os.path.isdir(folder) else ''

    def _propose_output(self):
        """Fill the file and layer name the way the other KGA tools do.

        GeoPackage: the project's default database, under an unused layer name
        (as ArcGIS Pro proposes the project geodatabase). Shapefile: a new file
        in the project folder. Without a project, the input file's folder. A
        path or name the user typed is never replaced.
        """
        if self._torn_down:
            return
        stem = self._stem()
        fmt = self.format_combo.currentData()
        name = stem
        if fmt != C.FORMAT_MEMORY and self._out.is_ours():
            path = ''
            if fmt == C.FORMAT_GPKG:
                database = od.project_database()
                if database:
                    path = database
                    name = od.unique_table(database, stem)
                else:
                    path = od.default_file(stem, 'gpkg')
            else:
                path = od.default_file(stem, 'shp')
            if not path:
                folder = self._fallback_folder()
                if folder:
                    path = writer.normalize_output_path(os.path.join(folder, stem), fmt)
            if path:
                self._out.suggest_path(path)
        elif fmt == C.FORMAT_GPKG:
            path = self.file_widget.filePath().strip()
            if path:
                name = od.unique_table(path, stem)
        if not self._name_edited:
            self.name_edit.setText(name)
        self._update_output_note()

    def _update_output_note(self):
        fmt = self.format_combo.currentData()
        name = self.name_edit.text().strip()
        text = ''
        if fmt == C.FORMAT_SHP:
            text = tr('Shapefile field names are cut to 10 characters.')
        elif fmt == C.FORMAT_GPKG and name:
            path = self.file_widget.filePath().strip()
            if path and name.lower() in {n.lower() for n in writer.existing_gpkg_layers(path)}:
                text = tr("'{0}' already has a layer named '{1}'; it will be "
                          'replaced after you confirm.').format(os.path.basename(path), name)
        self.output_note.setText(text)
        self.output_note.setVisible(bool(text))

    # ---------------------------------------------------------------- settings

    def _restore_settings(self):
        s = QgsSettings()
        index = self.format_combo.findData(s.value(SETTINGS + 'format', C.FORMAT_GPKG))
        self.format_combo.setCurrentIndex(max(0, index))
        for key, check in (('validate', self.validate_check), ('repair', self.repair_check),
                           ('dedup', self.dups_check), ('keep', self.keep_check)):
            check.setChecked(s.value(SETTINGS + key, True, type=bool))

    def _save_settings(self, settings):
        s = QgsSettings()
        s.setValue(SETTINGS + 'format', settings.output_format)
        s.setValue(SETTINGS + 'validate', settings.validate)
        s.setValue(SETTINGS + 'repair', self.repair_check.isChecked())
        s.setValue(SETTINGS + 'dedup', settings.remove_duplicates)
        s.setValue(SETTINGS + 'keep', settings.preserve_attributes)
        if settings.output_path:
            s.setValue(SETTINGS + 'last_dir', os.path.dirname(settings.output_path))

    # -------------------------------------------------------------------- run

    def collect_settings(self):
        fmt = self.format_combo.currentData()
        return C.ConversionSettings(
            mode=self._mode(),
            line_structure=self.line_structure.currentData(),
            selected_only=self.selected_check.isChecked(),
            order_method=self.order_method.currentData(),
            order_field=self.order_field.currentField(),
            order_expression=self.order_expr.expression(),
            reverse_order=self.reverse_check.isChecked(),
            group_field=self.group_field.currentField(),
            null_group_policy=self.null_policy.currentData(),
            remove_duplicates=self.dups_check.isChecked(),
            validate=self.validate_check.isChecked(),
            repair=self.validate_check.isChecked() and self.repair_check.isChecked(),
            preserve_attributes=self.keep_check.isChecked(),
            polygon_output=self.polygon_output.currentData(),
            output_format=fmt,
            output_path=writer.normalize_output_path(self.file_widget.filePath(), fmt),
            output_layer_name=self.name_edit.text().strip(),
            add_to_project=self.load_check.isChecked() or fmt == C.FORMAT_MEMORY,
        )

    def _warn(self, text):
        QMessageBox.warning(self, tr(TITLE), text)
        return None

    def validate_inputs(self, s, interactive=True):
        """The layer if `s` can run, else None (after telling the user)."""
        layer = self.layer()
        if layer is None:
            return self._warn(tr('Choose a point layer.'))
        if s.selected_only and layer.selectedFeatureCount() == 0:
            return self._warn(tr("'Selected features only' is ticked but nothing "
                                 "is selected in '{0}'.").format(layer.name()))
        if not s.selected_only and layer.featureCount() == 0:
            return self._warn(tr("'{0}' has no features.").format(layer.name()))
        if s.order_method == C.ORDER_FIELD and not s.order_field:
            return self._warn(tr('Choose the field that sets the point order.'))
        if s.order_method == C.ORDER_EXPRESSION:
            expr = QgsExpression(s.order_expression)
            if not s.order_expression.strip() or expr.hasParserError():
                return self._warn(tr('The order expression is not valid.\n{0}').format(
                    expr.parserErrorString()))
        if not s.output_layer_name:
            return self._warn(tr('Enter a name for the output layer.'))
        if s.output_format == C.FORMAT_MEMORY:
            return layer
        if not s.output_path:
            return self._warn(tr('Choose the output file.'))
        folder = os.path.dirname(os.path.abspath(s.output_path))
        if not os.path.isdir(folder):
            return self._warn(tr('The output folder does not exist:\n{0}').format(folder))
        if not interactive:
            return layer
        return layer if self._confirm_overwrite(s, layer) else None

    def _targets(self, s):
        return {(os.path.normcase(os.path.abspath(p.path)),
                 p.layer_name.lower() if s.output_format == C.FORMAT_GPKG else '')
                for p in writer.planned_outputs(s)}

    def _hits(self, layer, s, targets):
        """True when project layer `layer` reads one of the planned outputs."""
        if layer.providerType() != 'ogr':
            return False
        path, name = _decode(layer.source())
        if not path:
            return False
        if s.output_format == C.FORMAT_GPKG:
            if not name:    # a GeoPackage opened without a layer name is its first layer
                names = writer.existing_gpkg_layers(path)
                name = names[0] if names else ''
            return (path, name.lower()) in targets
        return (path, '') in targets

    def _confirm_overwrite(self, s, input_layer):
        plans = writer.planned_outputs(s)
        if s.output_format == C.FORMAT_GPKG:
            existing = {n.lower() for n in writer.existing_gpkg_layers(s.output_path)}
            conflicts = [p for p in plans if p.layer_name.lower() in existing]

            def describe(p):
                return tr("layer '{0}' in {1}").format(p.layer_name, os.path.basename(p.path))
        else:
            conflicts = [p for p in plans if os.path.exists(p.path)]

            def describe(p):
                return os.path.basename(p.path)

        targets = self._targets(s)
        if self._hits(input_layer, s, targets):
            self._warn(tr('The output would overwrite the input layer. Choose '
                          'another file or layer name.'))
            return False
        if not conflicts:
            return True
        loaded = [lyr for lyr in QgsProject.instance().mapLayers().values()
                  if isinstance(lyr, QgsVectorLayer) and self._hits(lyr, s, targets)]
        message = tr('This output already exists and will be replaced:') + \
            '\n\n  - ' + '\n  - '.join(describe(p) for p in conflicts)
        if loaded:
            message += '\n\n' + tr('These project layers use it and will be removed '
                                   'from the project first:') + \
                '\n  - ' + '\n  - '.join(lyr.name() for lyr in loaded)
        message += '\n\n' + tr('Replace it?')
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        if QMessageBox.question(self, tr('Replace output?'), message, yes | no, no) != yes:
            return False
        if loaded:
            QgsProject.instance().removeMapLayers([lyr.id() for lyr in loaded])
        return True

    def _running(self):
        return self._task is not None

    def _go_or_stop(self):
        if self._running():
            self.go_btn.setEnabled(False)
            self.bar.setFormat(tr('Stopping...') + '  %p%')
            self._say(tr('Stopping...'), AMBER)
            self._task.cancel()
            return
        self._start()

    def _start(self):
        if not self._name_edited:
            self._propose_output()          # a name taken since the window opened
        s = self.collect_settings()
        layer = self.validate_inputs(s)
        if layer is None:
            return
        self._save_settings(s)
        task = ConversionTask(layer, s, on_finished=self._on_finished)
        task.progressChanged.connect(self._on_progress)
        self._task = task
        self.tabs.setCurrentIndex(self.tabs.count() - 1)     # the Report tab
        self.bar.setFormat('%p%')
        self.bar.setValue(0)
        self.report.setHtml("<p style='color:%s'>%s</p>" % (
            MUTED, html.escape(tr("Building from '{0}'...").format(layer.name()))))
        self._say(tr('Running...'))
        self._set_running(True)
        QgsApplication.taskManager().addTask(task)

    def _on_progress(self, value):
        if not self._torn_down:
            self.bar.setValue(int(value))

    def _set_running(self, running):
        self.go_btn.setText(tr('Stop') if running else tr('Run'))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True)
        for page in self._pages[:-1]:
            page.setEnabled(not running)
        for widget in (self.layer_combo, self.refresh_btn, self.selected_check):
            widget.setEnabled(not running)

    def _say(self, text, color=''):
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')

    def _on_finished(self, task):
        if task is not self._task:
            return
        self._task = None
        if self._closing or self._torn_down:
            self._teardown()
            return
        r = task.result
        self._set_running(False)
        self._update_info()
        self.report.setHtml(render_report(r))
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        bar = iface.messageBar() if iface is not None else None
        if r.cancelled:
            self.bar.setValue(0)
            self._say(tr('Stopped - nothing was written.'), AMBER)
            return
        if r.fatal_error:
            self._say(tr('Failed: {0}').format(r.fatal_error), RED)
            if bar is not None:
                bar.pushCritical(tr(TITLE), r.fatal_error)
            return
        self.bar.setValue(100)
        text = tr('{0} feature(s) created').format(format(r.output_features, ','))
        if r.invalid_geometries:
            text += tr(', {0} skipped - see the report').format(
                format(r.invalid_geometries, ','))
        problems = bool(r.invalid_geometries or not r.outputs)
        self._say(text + '  ({0:.1f} s)'.format(r.elapsed), AMBER if problems else GREEN)
        if bar is not None:
            (bar.pushWarning if problems else bar.pushSuccess)(tr(TITLE), text)
        # A second run must not offer the layer the first one just wrote.
        if not self._name_edited:
            self._propose_output()

    # ---------------------------------------------------------------- closing

    def closeEvent(self, event):
        if self._running():
            self._confirm_stop()
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
        answer = QMessageBox.question(self, tr(TITLE), tr('Stop the run and close?'))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._closing = True
        self.hide()
        if self._task is not None:
            self._task.cancel()
        return True

    def shutdown(self):
        """Plugin unload: stop any run cleanly, then go away."""
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.on_finished = None
            with suppress(Exception):
                task.progressChanged.disconnect(self._on_progress)
            task.cancel()
            with suppress(Exception):
                task.waitForFinished(15000)
        self._teardown()

    def _teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self._watch(None)
        self.deleteLater()


def render_report(r):
    """The Report tab: what was read, what was made, and every message."""
    esc = html.escape
    if r.cancelled:
        badge, title, sub = AMBER, tr('Stopped'), tr('The run was stopped; nothing was written.')
    elif r.fatal_error:
        badge, title, sub = RED, tr('Failed'), esc(r.fatal_error)
    elif r.invalid_geometries or not r.outputs:
        badge, title, sub = AMBER, tr('Done, with problems'), \
            tr('Some groups or features need attention - see Messages.')
    else:
        badge, title, sub = GREEN, tr('Done'), tr('Every geometry was created.')
    head = ("<p style='margin:0'><span style='color:{c};font-size:large;"
            "font-weight:600'>{t}</span></p>"
            "<p style='color:{m};margin:2px 0 8px 0'>{s}</p>").format(
                c=badge, t=title, m=MUTED, s=sub)

    def table(rows):
        return ("<table cellspacing='0' cellpadding='2' width='100%'>" + ''.join(
            "<tr><td width='46%' style='color:{0}'>{1}</td><td>{2}</td></tr>".format(
                MUTED, k, v) for k, v in rows) + '</table>')

    def heading(text):
        return ("<p style='font-weight:600;color:{0};margin:10px 0 2px 0'>{1}</p>"
                .format(BLUE, text))

    run_rows = [
        (tr('Input layer'), esc(r.input_layer_name)),
        (tr('Scope'), esc(r.scope)),
        (tr('Operation'), esc(r.mode_label)),
        (tr('Time'), '{0:.2f} s'.format(r.elapsed)),
    ]
    groups = format(r.groups, ',')
    if r.groups_failed:
        groups += tr(' ({0} failed)').format(format(r.groups_failed, ','))
    made = format(r.successful_geometries, ',')
    if r.repaired_geometries:
        made += tr(' (incl. {0} repaired)').format(format(r.repaired_geometries, ','))
    count_rows = [
        (tr('Points read'), format(r.points_read, ',')),
        (tr('Points used'), format(r.points_used, ',')),
        (tr('Points skipped'), format(r.points_skipped, ',')),
        (tr('Groups'), groups),
        (tr('Output features'), format(r.output_features, ',')),
        (tr('Geometries created'), made),
        (tr('Skipped / invalid geometries'), "<span style='color:{0}'>{1}</span>".format(
            AMBER if r.invalid_geometries else 'inherit', format(r.invalid_geometries, ','))),
    ]

    def where(o):
        if not o.path:
            return esc(o.layer_name) + tr(' (temporary)')
        if o.path.lower().endswith('.gpkg'):
            return esc(o.path) + ' &gt; ' + esc(o.layer_name)
        return esc(o.path)

    outputs = ''.join(
        "<p style='margin:2px 0'>{0}<br><span style='color:{1}'>{2}</span></p>".format(
            where(o), MUTED, tr('{0} features').format(format(o.feature_count, ',')))
        for o in r.outputs) or "<p style='color:{0}'>-</p>".format(MUTED)
    body = (head + table(run_rows) + heading(tr('Counts')) + table(count_rows)
            + heading(tr('Output')) + outputs)
    if r.issues:
        colors = {'error': RED, 'warning': AMBER, 'info': MUTED}
        labels = {'error': tr('Error'), 'warning': tr('Warning'), 'info': tr('Info')}
        rank = {'error': 0, 'warning': 1, 'info': 2}
        body += heading(tr('Messages ({0} error, {1} warning, {2} info)').format(
            r.count('error'), r.count('warning'), r.count('info')))
        body += ''.join(
            "<p style='margin:2px 0'><b style='color:{0}'>{1}</b>&nbsp; {2}</p>".format(
                colors[i.level], labels[i.level], esc(i.text()))
            for i in sorted(r.issues, key=lambda i: rank[i.level]))
    return body

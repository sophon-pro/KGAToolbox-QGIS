# -*- coding: utf-8 -*-

from qgis.PyQt.QtCore import QCoreApplication, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsFieldProxyModel,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProject,
    QgsWkbTypes,
)
from qgis.gui import (
    QgsFieldComboBox,
    QgsMapLayerComboBox,
    QgsMapTool,
    QgsRubberBand,
)

from ..core.compat import no_threading
from ..branding import docs_url, help_button
# `qgis.PyQt.sip` is the name that works both where sip is a
# top-level module and where it is only PyQt5.sip; see modify_base.
from ..gui.modify_base import is_deleted as _is_deleted

try:  # QGIS >= 3.30
    from qgis.core import QgsVariantUtils
except ImportError:  # pragma: no cover
    QgsVariantUtils = None

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None

#: The one open dialog, if any. Module-level so the plugin can close it on
#: unload: it is parented to the QGIS main window, not to anything the run
#: that opened it owns.
DIALOG_INSTANCE = None


# --------------------------------------------------------------------------- #
#  small shared helpers (same logic as the batch algorithm)
# --------------------------------------------------------------------------- #

def is_null(value):
    if value is None:
        return True
    if QgsVariantUtils is not None:
        try:
            return QgsVariantUtils.isNull(value)
        except Exception:
            pass
    try:
        return bool(value.isNull())
    except AttributeError:
        return False


def classify_field(field):
    """Return 'int', 'double', 'string' or 'other' for a QgsField."""
    type_name = (field.typeName() or '').lower()
    if field.isNumeric():
        if 'int' in type_name or 'serial' in type_name:
            return 'int'
        return 'double'
    if any(key in type_name for key in ('string', 'text', 'char', 'json', 'uuid')):
        return 'string'
    return 'other'


def make_value(number, field_kind, prefix='', suffix='', pad_width=0):
    if field_kind == 'int':
        return int(round(number))
    if field_kind == 'double':
        return float(number)
    text = str(int(number)) if float(number).is_integer() else ('%g' % number)
    if pad_width > 0:
        text = text.zfill(pad_width)
    return '{}{}{}'.format(prefix, text, suffix)


# --------------------------------------------------------------------------- #
#  map tool
# --------------------------------------------------------------------------- #

class SequentialNumberingMapTool(QgsMapTool):
    """Click a feature, or drag a path across features, to number them."""

    featuresNumbered = pyqtSignal(int)      # how many features got a value
    message = pyqtSignal(str)

    DRAG_THRESHOLD_PX = 4
    PICK_RADIUS_PX = 6

    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller        # the dialog, asked for layer/values
        self.setCursor(Qt.CrossCursor)

        self._band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._band.setColor(QColor(0, 0, 0))
        self._band.setWidth(2)
        try:
            self._band.setLineStyle(Qt.DashLine)
        except AttributeError:
            pass

        self._points = []
        self._press_pos = None
        self._dragging = False

    # ------------------------------------------------------------ events --

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            self._reset()
            return
        self._press_pos = event.pos()
        self._dragging = False
        self._points = [self.toMapCoordinates(event.pos())]

    def canvasMoveEvent(self, event):
        if self._press_pos is None:
            return
        if not self._dragging:
            delta = event.pos() - self._press_pos
            if max(abs(delta.x()), abs(delta.y())) < self.DRAG_THRESHOLD_PX:
                return
            self._dragging = True
        point = self.toMapCoordinates(event.pos())
        self._points.append(point)
        self._band.addPoint(point, True)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._press_pos is None:
            return
        dragging = self._dragging
        points = list(self._points)
        self._reset()

        if dragging and len(points) > 1:
            self._apply(points, single=False)
        else:
            self._apply(points[:1], single=True)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._reset()

    def deactivate(self):
        self._reset()
        super().deactivate()

    def _reset(self):
        self._band.reset(QgsWkbTypes.LineGeometry)
        self._points = []
        self._press_pos = None
        self._dragging = False

    # ------------------------------------------------------------- action --

    def _apply(self, map_points, single):
        layer = self.controller.current_layer()
        if layer is None or not map_points:
            return
        if not layer.isEditable():
            self.message.emit('The layer is not in edit mode.')
            return

        field_index = self.controller.current_field_index()
        if field_index < 0:
            self.message.emit('Choose a field first.')
            return

        tolerance = self.canvas.mapUnitsPerPixel() * self.PICK_RADIUS_PX

        if single:
            path = QgsGeometry.fromPointXY(map_points[0])
            search = path.buffer(tolerance, 8)
        else:
            path = QgsGeometry.fromPolylineXY(map_points)
            search = path
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                search = path.buffer(tolerance, 8)

        # canvas CRS -> layer CRS
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if canvas_crs != layer.crs():
            transform = QgsCoordinateTransform(
                canvas_crs, layer.crs(), QgsProject.instance())
            try:
                path.transform(transform)
                search.transform(transform)
            except Exception as exc:  # pragma: no cover
                self.message.emit('Reprojection failed: {}'.format(exc))
                return

        engine = QgsGeometry.createGeometryEngine(search.constGet())
        engine.prepareGeometry()

        request = QgsFeatureRequest().setFilterRect(search.boundingBox())
        request.setSubsetOfAttributes([field_index])

        skip_filled = self.controller.skip_filled()
        hits = []
        for feature in layer.getFeatures(request):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            if not engine.intersects(geometry.constGet()):
                continue
            if skip_filled and not is_null(feature.attribute(field_index)):
                continue
            hits.append((self._measure(path, geometry, single), feature.id()))

        if not hits:
            return

        hits.sort(key=lambda item: (item[0], item[1]))
        self.controller.assign(layer, field_index, [fid for _, fid in hits])

    @staticmethod
    def _measure(path, geometry, single):
        """Position along the drawn path where the feature is first met."""
        if single:
            return 0.0
        try:
            intersection = path.intersection(geometry)
        except Exception:
            intersection = None

        candidate = intersection
        if candidate is None or candidate.isEmpty():
            candidate = geometry.centroid()

        best = None
        count = 0
        for vertex in candidate.vertices():
            measure = path.lineLocatePoint(
                QgsGeometry.fromPointXY(QgsPointXY(vertex.x(), vertex.y())))
            if measure >= 0 and (best is None or measure < best):
                best = measure
            count += 1
            if count > 500:            # guard against huge geometries
                break
        return best if best is not None else 0.0


# --------------------------------------------------------------------------- #
#  dialog
# --------------------------------------------------------------------------- #

class SequentialNumberingDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setWindowTitle('Sequential Numbering')
        self.setMinimumWidth(360)

        self._tool = None
        self._next_value = 1.0
        self._previous_tool = None

        self._build_ui()
        self._connect()
        self._layer_changed()

    # ----------------------------------------------------------------- ui --

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        form.addRow('Layer', self.layer_combo)

        self.field_combo = QgsFieldComboBox()
        self.field_combo.setFilters(
            QgsFieldProxyModel.String | QgsFieldProxyModel.Numeric)
        form.addRow('Field', self.field_combo)

        self.format_label = QLabel('#')
        self.format_label.setStyleSheet('color: #666;')
        form.addRow('Format', self.format_label)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setDecimals(0)
        self.start_spin.setRange(-1e9, 1e9)
        self.start_spin.setValue(1)
        form.addRow('Start value', self.start_spin)

        self.increment_spin = QDoubleSpinBox()
        self.increment_spin.setDecimals(0)
        self.increment_spin.setRange(-1e6, 1e6)
        self.increment_spin.setValue(1)
        form.addRow('Increment', self.increment_spin)

        self.next_spin = QDoubleSpinBox()
        self.next_spin.setDecimals(0)
        self.next_spin.setRange(-1e9, 1e9)
        self.next_spin.setValue(1)
        form.addRow('Next value', self.next_spin)

        layout.addLayout(form)

        text_box = QGroupBox('Text field options')
        text_form = QFormLayout(text_box)
        self.prefix_edit = QLineEdit()
        text_form.addRow('Prefix', self.prefix_edit)
        self.suffix_edit = QLineEdit()
        text_form.addRow('Suffix', self.suffix_edit)
        self.pad_spin = QSpinBox()
        self.pad_spin.setRange(0, 20)
        text_form.addRow('Pad to digits', self.pad_spin)
        layout.addWidget(text_box)
        self.text_box = text_box

        self.skip_check = QCheckBox('Skip features that already have a value')
        layout.addWidget(self.skip_check)

        buttons = QHBoxLayout()
        self.activate_button = QPushButton('Sequential Numbering')
        self.activate_button.setCheckable(True)
        self.activate_button.setToolTip(
            'Click a feature to number it, or drag a path across several '
            'features to number them in order.')
        buttons.addWidget(self.activate_button)

        self.reset_button = QPushButton('Reset')
        self.reset_button.setToolTip('Set the next value back to the start value.')
        buttons.addWidget(self.reset_button)

        buttons.addWidget(help_button('sequential_numbering', self))

        self.close_button = QPushButton('Close')
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('color: #666;')
        layout.addWidget(self.status_label)

    def _connect(self):
        self.layer_combo.layerChanged.connect(self._layer_changed)
        self.field_combo.fieldChanged.connect(self._update_format)
        self.start_spin.valueChanged.connect(self._start_changed)
        self.next_spin.valueChanged.connect(self._next_changed)
        self.prefix_edit.textChanged.connect(self._update_format)
        self.suffix_edit.textChanged.connect(self._update_format)
        self.pad_spin.valueChanged.connect(self._update_format)
        self.activate_button.toggled.connect(self._toggle_tool)
        self.reset_button.clicked.connect(self._reset_counter)
        self.close_button.clicked.connect(self.close)
        self.canvas.mapToolSet.connect(self._map_tool_set)

    # ------------------------------------------------------------- state --

    def current_layer(self):
        return self.layer_combo.currentLayer()

    def current_field_index(self):
        layer = self.current_layer()
        name = self.field_combo.currentField()
        if layer is None or not name:
            return -1
        return layer.fields().lookupField(name)

    def skip_filled(self):
        return self.skip_check.isChecked()

    def _field_kind(self):
        index = self.current_field_index()
        if index < 0:
            return None
        return classify_field(self.current_layer().fields().at(index))

    def _layer_changed(self):
        layer = self.current_layer()
        self.field_combo.setLayer(layer)
        self._update_format()

    def _update_format(self):
        kind = self._field_kind()
        is_text = kind == 'string'
        self.text_box.setEnabled(is_text)
        if kind is None:
            self.format_label.setText('#')
            return
        sample = make_value(
            self.next_spin.value(), kind,
            self.prefix_edit.text() if is_text else '',
            self.suffix_edit.text() if is_text else '',
            self.pad_spin.value() if is_text else 0)
        self.format_label.setText(str(sample))

    def _start_changed(self, value):
        self.next_spin.blockSignals(True)
        self.next_spin.setValue(value)
        self.next_spin.blockSignals(False)
        self._next_value = value
        self._update_format()

    def _next_changed(self, value):
        self._next_value = value
        self._update_format()

    def _reset_counter(self):
        self.next_spin.setValue(self.start_spin.value())
        self.status_label.setText('Counter reset.')

    # -------------------------------------------------------------- tool --

    def _toggle_tool(self, checked):
        if not checked:
            if self._tool is not None and self.canvas.mapTool() is self._tool:
                self.canvas.unsetMapTool(self._tool)
            return

        layer = self.current_layer()
        if layer is None:
            self._warn('Choose a layer first.')
            return
        if self.current_field_index() < 0:
            self._warn('Choose a field first.')
            return
        if self._field_kind() == 'other':
            self._warn('Only text and numeric fields can be numbered.')
            return

        if not layer.isEditable():
            answer = QMessageBox.question(
                self, 'Sequential Numbering',
                'The layer "{}" is not in edit mode.\n\nStart editing now?'.format(
                    layer.name()),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer != QMessageBox.Yes or not layer.startEditing():
                self._warn('Editing is required to write values.')
                return

        self.iface.setActiveLayer(layer)
        if self._tool is None:
            self._tool = SequentialNumberingMapTool(self.canvas, self)
            self._tool.message.connect(self.status_label.setText)
        self._previous_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self._tool)
        self.status_label.setText(
            'Click a feature, or drag a path across features, to number them.')

    def _map_tool_set(self, new_tool, old_tool=None):
        if self._tool is not None and new_tool is not self._tool:
            self.activate_button.blockSignals(True)
            self.activate_button.setChecked(False)
            self.activate_button.blockSignals(False)

    def _warn(self, text):
        self.status_label.setText(text)
        self.activate_button.blockSignals(True)
        self.activate_button.setChecked(False)
        self.activate_button.blockSignals(False)

    # ------------------------------------------------------------- write --

    def assign(self, layer, field_index, feature_ids):
        """Write the next values to the given features, in the given order."""
        kind = classify_field(layer.fields().at(field_index))
        is_text = kind == 'string'
        prefix = self.prefix_edit.text() if is_text else ''
        suffix = self.suffix_edit.text() if is_text else ''
        pad = self.pad_spin.value() if is_text else 0
        length = layer.fields().at(field_index).length()
        increment = self.increment_spin.value()

        layer.beginEditCommand('Sequential Numbering')
        written = 0
        try:
            for fid in feature_ids:
                value = make_value(self._next_value, kind, prefix, suffix, pad)
                if is_text and length and length > 0 and len(value) > length:
                    value = value[:length]
                if layer.changeAttributeValue(fid, field_index, value):
                    written += 1
                    self._next_value += increment
        except Exception as exc:  # pragma: no cover
            layer.destroyEditCommand()
            self.status_label.setText('Failed: {}'.format(exc))
            return
        layer.endEditCommand()

        self.next_spin.blockSignals(True)
        self.next_spin.setValue(self._next_value)
        self.next_spin.blockSignals(False)
        self._update_format()

        self.canvas.refresh()
        self.status_label.setText(
            '{} feature(s) numbered. Next value: {}.'.format(
                written, make_value(self._next_value, kind, prefix, suffix, pad)))

    # ------------------------------------------------------------- close --

    def closeEvent(self, event):
        if self._tool is not None and self.canvas.mapTool() is self._tool:
            self.canvas.unsetMapTool(self._tool)
        try:
            self.canvas.mapToolSet.disconnect(self._map_tool_set)
        except Exception:
            pass
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  launcher: the entry the toolbox, toolbar and Geoprocessing panel see
# --------------------------------------------------------------------------- #


def close_sequential_numbering():
    """Close the dialog if one is open. Safe to call when none is.

    Called by `KgaToolsPlugin.unload`; without it the dialog — and the map tool
    it may have active on the canvas — survives the plugin being disabled.
    """
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None or _is_deleted(dialog):
        return
    dialog.close()
    dialog.deleteLater()


class SequentialNumberingAlgorithm(QgsProcessingAlgorithm):
    """Show the Sequential Numbering dialog."""

    def tr(self, string):
        return QCoreApplication.translate('KgaSequentialNumbering', string)

    def createInstance(self):
        return SequentialNumberingAlgorithm()

    def name(self):
        return 'sequential_numbering'

    def displayName(self):
        return self.tr('Sequential Numbering')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('sequential_numbering')

    def shortHelpString(self):
        return self.tr(
            'Number features by clicking them on the map, the way ArcGIS Pro\'s '
            '<i>Modify Features > Sequential Numbering</i> pane does.\n\n'
            'Pick a layer, a field, a start value and an increment, then press '
            '<b>Sequential Numbering</b> and work on the canvas:\n\n'
            '• click a feature — it gets the next value\n'
            '• press, drag a path across several features, release — each one '
            'is numbered in the order the path crosses it\n\n'
            'Text fields can take a prefix, a suffix and zero padding, so '
            '<i>MH-007</i> is as easy as <i>7</i>. Turn on <b>Skip features '
            'that already have a value</b> to renumber only the gaps.\n\n'
            'The layer has to be in edit mode; the tool offers to start editing '
            'if it is not. Each click or drag is one undo step, so Ctrl+Z takes '
            'back a whole stroke.\n\n'
            'The dialog stays open while you work — close this Processing '
            'window once it appears.'
        )

    def flags(self):
        # Opens a dialog and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE

        if iface is None:
            raise QgsProcessingException(self.tr(
                'Sequential Numbering is an interactive tool and needs the '
                'QGIS map canvas, so it cannot run from a head-less '
                'Processing session.'))

        if DIALOG_INSTANCE is not None and _is_deleted(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        # closeEvent drops the map tool and disconnects mapToolSet, so a closed
        # dialog is spent: build a fresh one rather than re-showing that.
        if DIALOG_INSTANCE is not None and not DIALOG_INSTANCE.isVisible():
            DIALOG_INSTANCE.deleteLater()
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = SequentialNumberingDialog(iface)

        DIALOG_INSTANCE.setWindowModality(Qt.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo(self.tr(
            'Sequential Numbering opened. You can close this Processing '
            'dialog and keep working on the map.'))
        return {}

import json
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                 QPushButton, QTableWidget, QTableWidgetItem,
                                 QHeaderView, QMessageBox)
from qgis.core import (QgsCoordinateTransform, QgsGeometry,
                       QgsProcessingAlgorithm,
                       QgsProcessingFeatureSourceDefinition, QgsProject,
                       QgsVectorLayer)
from qgis.utils import iface
import processing

#: The one open window, so a second run raises it instead of stacking windows.
DIALOG_INSTANCE = None


def _is_deleted(obj):
    try:
        import sip
        return sip.isdeleted(obj)
    except Exception:
        return False


class ErrorInspectorDialog(QDialog):
    """Modeless window listing the errors on the KGA topology error layers.

    A regular window rather than a dock, like the editing tools: it can be
    moved off the QGIS window, maximised, and left open while the user edits.
    """

    #: layer properties either KGA topology check stamps on its error layers
    ERROR_LAYER_PROPS = ('kga_tools/point_boundary_error_layer',
                         'kga_tools/topology_error_layer')

    #: Per-layer signals that mean the rows for that layer are out of date.
    LAYER_SIGNALS = ('featureAdded', 'featuresDeleted', 'geometryChanged',
                     'attributeValueChanged', 'editingStopped', 'dataChanged',
                     'nameChanged', 'layerModified')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Error Inspector')
        # Qt.Window gives it a real title bar with minimise/maximise, so the
        # user can park it beside QGIS instead of docking it.
        self.setWindowFlags(self.windowFlags() | Qt.Window |
                            Qt.WindowMinMaxButtonsHint)
        self.setMinimumSize(480, 320)
        self.resize(560, 420)

        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(['FID', 'Error Type', 'Layer Name'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.zoom_to_error)
        self._last_zoom_target = None

        self.status = QLabel()
        self.status.setWordWrap(True)

        self.btn_zoom = QPushButton('Zoom to Error')
        self.btn_zoom.clicked.connect(self.zoom_to_error)

        self.btn_refresh = QPushButton('Refresh')
        self.btn_refresh.clicked.connect(self.populate_table)

        self.btn_validate = QPushButton('Validate All')
        self.btn_validate.clicked.connect(self.validate_errors)

        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_zoom)
        buttons.addWidget(self.btn_refresh)
        buttons.addWidget(self.btn_validate)

        layout.addWidget(self.table)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        # A single edit can fire several of LAYER_SIGNALS, and a check run
        # replaces every error layer at once.  Rebuilding the table on each of
        # those made the window fight the user, so the bursts are collapsed
        # into one rebuild.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.populate_table)

        #: Error layers whose edit signals are currently connected.
        self._watched = []

        project = QgsProject.instance()
        project.layersAdded.connect(self.refresh_layers)
        project.layersRemoved.connect(self.refresh_layers)
        self.populate_table()

    # ------------------------------------------------------------------ #
    # Staying in step with the project
    # ------------------------------------------------------------------ #
    def refresh_layers(self, *_):
        """Queue a rebuild after a project or layer change."""
        self._refresh_timer.start()

    def cleanup(self):
        """Drop every connection before the window is destroyed.

        Without this the project and the error layers keep calling back into a
        deleted widget after the plugin is unloaded or the window is closed.
        """
        self._refresh_timer.stop()
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(self.refresh_layers)
            except (TypeError, RuntimeError):
                pass
        self._unwatch_layers()

    def closeEvent(self, event):
        # Closing ends this window: show_error_inspector builds a fresh one,
        # so nothing has to survive here.
        self.cleanup()
        super().closeEvent(event)

    def _unwatch_layers(self):
        for layer in self._watched:
            if _is_deleted(layer):
                continue
            for name in self.LAYER_SIGNALS:
                signal = getattr(layer, name, None)
                if signal is None:
                    continue
                try:
                    signal.disconnect(self.refresh_layers)
                except (TypeError, RuntimeError):
                    pass
        self._watched = []

    def _watch_layers(self, layers):
        """Follow edits to the error layers themselves, not just the project.

        Deleting a fixed error off an error layer, or editing one in place,
        adds and removes no layer, so the project signals alone never told the
        table to rebuild.
        """
        self._unwatch_layers()
        for layer in layers:
            for name in self.LAYER_SIGNALS:
                signal = getattr(layer, name, None)
                if signal is None:
                    # Not every QGIS build exposes every one of these.
                    continue
                try:
                    signal.connect(self.refresh_layers)
                except (TypeError, RuntimeError):
                    continue
            self._watched.append(layer)

    @classmethod
    def _is_error_layer(cls, layer):
        """True for a vector layer written by one of the KGA topology checks."""
        if not isinstance(layer, QgsVectorLayer):
            return False
        return any(layer.customProperty(prop) for prop in cls.ERROR_LAYER_PROPS)

    def _error_layers(self):
        return [layer for layer in QgsProject.instance().mapLayers().values()
                if self._is_error_layer(layer)]

    # ------------------------------------------------------------------ #
    # The table
    # ------------------------------------------------------------------ #
    def populate_table(self):
        self._refresh_timer.stop()
        layers = self._error_layers()
        self._watch_layers(layers)

        # Rebuilding the rows churns the selection, and the selection drives
        # zoom_to_error; without this the canvas jumps on every project change.
        selected = self._selected_target()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            restore_row = -1
            for layer in layers:
                # An older error layer may not carry the field at all, and
                # feat.attribute() raises KeyError rather than returning None.
                error_idx = layer.fields().lookupField('error')
                for feat in layer.getFeatures():
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    fid_item = QTableWidgetItem(str(feat.id()))
                    # Keep the backing error-layer ID out of the UI while
                    # retaining it for Zoom to Error.
                    fid_item.setData(Qt.UserRole, layer.id())
                    self.table.setItem(row, 0, fid_item)
                    value = feat.attribute(error_idx) if error_idx >= 0 else None
                    self.table.setItem(row, 1, QTableWidgetItem(
                        self._error_type(value)
                    ))
                    self.table.setItem(row, 2, QTableWidgetItem(layer.name()))
                    if selected == (layer.id(), feat.id()):
                        restore_row = row
            if restore_row >= 0:
                # The row the user was on survived the rebuild, so keep them
                # on it instead of dropping them back to an empty selection.
                self.table.selectRow(restore_row)
        finally:
            self.table.blockSignals(False)
        # The rows are new, so the next click should frame its error afresh
        # rather than treat it as a repeat click and zoom further in.
        self._last_zoom_target = None
        self._update_status(len(layers))

    def _update_status(self, layer_count):
        rows = self.table.rowCount()
        if not layer_count:
            self.status.setText(
                'No KGA Topology error layers in the project. Run a topology '
                'or point-on-boundary check to fill this list.')
        else:
            self.status.setText('{} error{} across {} layer{}.'.format(
                rows, '' if rows == 1 else 's',
                layer_count, '' if layer_count == 1 else 's'))

    def _selected_target(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        return (item.data(Qt.UserRole), int(item.text()))

    @staticmethod
    def _error_type(value):
        """Present consistent names, including for existing older error layers."""
        normalized = str(value or '').strip().casefold()
        names = {
            'gap': 'Gap',
            'overlap': 'Overlap',
            'point not on vertex': 'Point not on boundary',
            'point not on boundary': 'Point not on boundary',
        }
        return names.get(normalized, str(value or 'Unknown error'))

    def zoom_to_error(self):
        target = self._selected_target()
        if target is None:
            return
        layer_id, fid = target

        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return
        feat = layer.getFeature(fid)
        if not feat.isValid() or feat.geometry().isEmpty():
            return

        canvas = iface.mapCanvas()
        if target != self._last_zoom_target:
            # QGIS transforms the feature extent to the map canvas CRS and
            # gives point features a usable extent.  Directly setting a
            # layer-CRS bounding box was the reason zooming could miss errors.
            try:
                # QgsFeatureIds is exposed to Python as a set, not a list.
                canvas.zoomToFeatureIds(layer, {fid})
            except (AttributeError, TypeError):
                # Fallback for older QGIS builds which do not expose the
                # feature-aware canvas helper.
                geometry = QgsGeometry(feat.geometry())
                canvas_crs = canvas.mapSettings().destinationCrs()
                if layer.crs() != canvas_crs:
                    transform = QgsCoordinateTransform(
                        layer.crs(), canvas_crs,
                        QgsProject.instance().transformContext()
                    )
                    geometry.transform(transform)
                canvas.setCenter(geometry.centroid().asPoint())
            self._last_zoom_target = target
        else:
            # Only a repeat click steps closer.  Applying this on the first
            # click too zoomed 2.5x past the extent QGIS had just framed.
            # A smaller denominator means a larger on-screen feature.
            canvas.zoomScale(canvas.scale() / 2.5)
        canvas.refresh()

    def validate_errors(self):
        checks = []
        seen = set()
        unreplayable = []
        for layer in self._error_layers():
            algo_id = layer.customProperty('kga_tools/algo_id')
            params_str = layer.customProperty('kga_tools/algo_params')
            if not algo_id or not params_str:
                # A check run over files picked straight off disk records no
                # parameters, because the layer IDs it would store die with the
                # Processing run that made them.
                unreplayable.append(layer.name())
                continue
            selected_str = layer.customProperty('kga_tools/algo_params_selected') or '[]'
            # Overlap and gap layers share one topology check, so run it once.
            key = (algo_id, params_str, selected_str)
            if key not in seen:
                seen.add(key)
                checks.append(key)

        if not checks:
            message = 'No error layers with validation metadata were found.'
            if unreplayable:
                message += (
                    '\n\n' + ', '.join(sorted(set(unreplayable))) + ' came from '
                    'a check run on layers that are not in the project. Add the '
                    'inputs to the project and run the check again to make it '
                    'repeatable from here.'
                )
            QMessageBox.warning(self, 'Validation Failed', message)
            return

        # One failing check must not hide the checks after it in the list.
        failures = []
        for algo_id, params_str, selected_str in checks:
            try:
                processing.run(algo_id, self._replay_params(params_str, selected_str))
            except Exception as e:
                failures.append(f'{algo_id}: {e}')

        # The replaced error layers also arrive through the project signals,
        # but rebuild now so the result is on screen before the message box.
        self.populate_table()
        if failures:
            QMessageBox.critical(self, 'Error',
                                 'Failed to validate:\n\n' + '\n\n'.join(failures))
        else:
            iface.messageBar().pushSuccess('Validation', 'All error checks complete.')

    @staticmethod
    def _replay_params(params_str, selected_str):
        """Stored JSON back into Processing parameters.

        "Selected features only" is a QgsProcessingFeatureSourceDefinition,
        which no algorithm can store as JSON, so the check records the plain
        layer ID plus the names of the parameters that carried the flag.
        """
        params = json.loads(params_str)
        for name in json.loads(selected_str or '[]'):
            if name in params:
                params[name] = QgsProcessingFeatureSourceDefinition(
                    params[name], selectedFeaturesOnly=True)
        return params


def close_error_inspector():
    """Close the inspector window if one is open. Safe to call when none is.

    The plugin calls this on unload: the window is parented to the QGIS main
    window rather than owned by the plugin, so nothing else would take it down.
    """
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None or _is_deleted(dialog):
        return
    dialog.cleanup()
    dialog.close()
    dialog.deleteLater()


def show_error_inspector():
    """Open the inspector window, raising the open one instead of stacking."""
    global DIALOG_INSTANCE
    if iface is None:
        return

    if DIALOG_INSTANCE is not None and _is_deleted(DIALOG_INSTANCE):
        DIALOG_INSTANCE = None
    if DIALOG_INSTANCE is not None and not DIALOG_INSTANCE.isVisible():
        # A closed window has dropped its project connections, so it would
        # never refresh again; replace it rather than re-show it.
        DIALOG_INSTANCE.deleteLater()
        DIALOG_INSTANCE = None

    if DIALOG_INSTANCE is None:
        DIALOG_INSTANCE = ErrorInspectorDialog(iface.mainWindow())
    else:
        # Already open: catch it up with anything that changed since.
        DIALOG_INSTANCE.populate_table()

    DIALOG_INSTANCE.setWindowModality(Qt.NonModal)
    DIALOG_INSTANCE.show()
    DIALOG_INSTANCE.raise_()
    DIALOG_INSTANCE.activateWindow()


class ErrorInspectorAlgorithm(QgsProcessingAlgorithm):
    """Processing Toolbox entry point for the Error Inspector window."""

    def tr(self, string):
        return QCoreApplication.translate('ErrorInspector', string)

    def createInstance(self):
        return ErrorInspectorAlgorithm()

    def name(self):
        return 'errorinspector'

    def displayName(self):
        return self.tr('Open Error Inspector')

    def group(self):
        return self.tr('KGA Topology')

    def groupId(self):
        return 'kgatopology'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/errorinspector'

    def shortHelpString(self):
        return self.tr(
            'Opens the Error Inspector window. It lists the features on every '
            'KGA Topology error layer in the project and keeps the list in '
            'step as checks are re-run and errors are fixed. Select a row to '
            'zoom to that error, or run every check again with Validate All.'
        )

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        # Processing algorithms run off the GUI thread.  The window itself is
        # created in postProcessAlgorithm, which QGIS runs on the main thread.
        return {}

    def postProcessAlgorithm(self, context, feedback):
        if iface is not None:
            show_error_inspector()
        return {}

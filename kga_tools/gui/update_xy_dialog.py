# -*- coding: utf-8 -*-
"""Update X/Y or Lat/Lon Fields: the small window.

Two choices, one button: the layer, and whether to write X / Y in the layer's
CRS or Longitude / Latitude in WGS 84. The work is
``UpdateXYFields.update_layer``, shared with the Processing algorithm. The
result is said in the window itself rather than in pop-ups, and the window stays
open so several layers can be done in a row.
"""

from qgis.core import QgsApplication, QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout)

from .window_reveal import revealed
from ..algorithms.update_xy_fields import UpdateXYFields
from ..branding import open_docs
from ..core.modify_features import layer_filter

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'updatexyfields'
TITLE = 'Update X/Y or Lat/Lon Fields'
BLUE = '#174592'
RED = '#B3261E'
GREEN = '#1E7B34'

#: (combo text, field names it writes, note under the combo)
FORMATS = (
    ('X / Y  (layer CRS)', ('POINT_X', 'POINT_Y'),
     'Writes POINT_X and POINT_Y in the layer\'s own CRS.'),
    ('Longitude / Latitude  (WGS 84)', ('LONGITUDE', 'LATITUDE'),
     'Writes LONGITUDE and LATITUDE in degrees (EPSG:4326).'),
)

STYLE = """
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaNote { font-style: italic; }
""" % {'blue': BLUE}


def tr(text):
    return QgsApplication.translate('KgaUpdateXY', text)


@revealed
class UpdateXYDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(380)
        self._torn_down = False
        self._ignore_close = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        form = QFormLayout()
        self.layer_box = QgsMapLayerComboBox(self)
        self.layer_box.setFilters(layer_filter('VectorLayer'))
        self.layer_box.setToolTip(tr(
            'The layer to update. It is edited in place; points use their own '
            'position, lines and polygons their centroid.'))
        active = iface.activeLayer() if iface is not None else None
        if active is not None and active.type() == active.VectorLayer:
            self.layer_box.setLayer(active)
        self.layer_box.layerChanged.connect(self._clear_status)
        form.addRow(tr('Layer'), self.layer_box)

        self.format_box = QComboBox(self)
        for text, _fields, _note in FORMATS:
            self.format_box.addItem(tr(text))
        self.format_box.setToolTip(tr('Which coordinates to write into the layer.'))
        self.format_box.currentIndexChanged.connect(self._on_format)
        form.addRow(tr('Coordinates'), self.format_box)
        root.addLayout(form)

        self.note = QLabel('', self)
        self.note.setObjectName('kgaNote')
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        self.status = QLabel('', self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        row = QHBoxLayout()
        help_btn = QPushButton(tr('Help'), self)
        help_btn.clicked.connect(lambda: open_docs(ALG_NAME))
        self.go_btn = QPushButton(tr('Update'), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._update)
        close_btn = QPushButton(tr('Close'), self)
        close_btn.clicked.connect(self.close)
        row.addWidget(help_btn)
        row.addStretch(1)
        row.addWidget(self.go_btn)
        row.addWidget(close_btn)
        root.addLayout(row)

        self._on_format()

    # Processing drives a custom parameters dialog like its own: setParameters(),
    # show(), exec(), results(), then close(). This one is modeless, so exec()
    # just shows it, and the close() that follows straight away is ignored.
    def exec(self):
        self._ignore_close = True
        QTimer.singleShot(0, self._allow_close)
        self.show()
        self.raise_()
        self.activateWindow()
        return 0

    exec_ = exec

    def _allow_close(self):
        self._ignore_close = False

    def setParameters(self, parameters):
        layer = parameters.get('INPUT')
        if hasattr(layer, 'type'):
            self.layer_box.setLayer(layer)
        elif isinstance(layer, str) and layer:
            found = QgsProject.instance().mapLayer(layer)
            if found is not None:
                self.layer_box.setLayer(found)
        fmt = parameters.get('COORD_FORMAT')
        if fmt in (0, 1):
            self.format_box.setCurrentIndex(fmt)

    def results(self):
        return {}

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._refit)

    def _refit(self):
        if not self._torn_down:
            self.layout().activate()
            self.resize(self.width(), self.layout().totalSizeHint().height())

    def _say(self, text, kind=''):
        color = {'bad': RED, 'good': GREEN}.get(kind, '')
        self.status.setText(text)
        self.status.setStyleSheet('color: %s;' % color if color else '')
        self.status.setVisible(bool(text))
        QTimer.singleShot(0, self._refit)

    def _clear_status(self, *_):
        self._say('')

    def _on_format(self, *_):
        self.note.setText(tr(FORMATS[self.format_box.currentIndex()][2]))
        self._say('')

    def _update(self):
        layer = self.layer_box.currentLayer()
        if layer is None or not layer.isValid():
            self._say(tr('Choose a layer.'), 'bad')
            return
        if layer.isEditable():
            self._say(tr('This layer is being edited. Save its edits and '
                         'switch editing off first.'), 'bad')
            return
        latlon = self.format_box.currentIndex() == 1
        if latlon and not layer.crs().isValid():
            self._say(tr('This layer has no CRS, so it cannot be converted to '
                         'longitude and latitude.'), 'bad')
            return

        self.go_btn.setEnabled(False)
        QgsApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            created, updated = UpdateXYFields.update_layer(layer, latlon)
        except RuntimeError as exc:
            self._say(str(exc), 'bad')
        else:
            text = tr("Updated {0} feature(s) in '{1}'.").format(updated, layer.name())
            if created:
                text += '  ' + tr('New fields: {0}.').format(', '.join(created))
            self._say(text, 'good')
        finally:
            QgsApplication.restoreOverrideCursor()
            self.go_btn.setEnabled(True)

    # ---------------------------------------------------------------- closing

    def closeEvent(self, event):
        if self._ignore_close:
            event.ignore()
            return
        self._torn_down = True
        self.deleteLater()
        super().closeEvent(event)

    def done(self, result):
        if self._ignore_close:
            return
        self._torn_down = True
        self.deleteLater()
        super().done(result)

    def shutdown(self):
        """Plugin unload."""
        self._torn_down = True
        self.deleteLater()

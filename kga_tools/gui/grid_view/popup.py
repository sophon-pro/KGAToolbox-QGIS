# -*- coding: utf-8 -*-
"""The panel under the Grid button's arrow: on/off, spacing with its unit, rotation, Grid Settings.

Changes apply to the map as they are made and are written to the KGA settings, so Settings > Editor shows
the same values. While the grid is drawn at a larger spacing than typed (dynamic scaling) a blue note says so.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import kga_settings as ks
from ...editor.quicksymbol.widgets import ToggleSwitch


class GridPanel(QWidget):

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setMinimumWidth(300)
        title = QLabel('<b>Grid</b>')
        self.on = ToggleSwitch()                  # the green switch of the editor tools
        self.on.setToolTip('Show or hide the grid')
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.on)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet('background:#dcebf9; color:#1f3b57; padding:6px; border-radius:3px;')
        self.spacing = QDoubleSpinBox()
        self.spacing.setDecimals(4)
        self.spacing.setRange(0.0001, 10000000.0)
        self.spacing.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.unit = QComboBox()
        self.unit.addItem('m', ks.GRID_UNIT_M)
        self.unit.addItem('ft', ks.GRID_UNIT_FT)
        self.unit.addItem('map units', ks.GRID_UNIT_MAP)
        row = QHBoxLayout()
        row.addWidget(self.spacing, 1)
        row.addWidget(self.unit)
        self.rotation = QDoubleSpinBox()
        self.rotation.setDecimals(2)
        self.rotation.setRange(-360.0, 360.0)
        self.rotation.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.rotation.setSuffix(' °')
        form = QFormLayout()
        form.addRow('Spacing', row)
        form.addRow('Rotation', self.rotation)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        self.settings_button = QPushButton('Grid Settings...')
        self.settings_button.setFlat(True)
        self.reset_button = QPushButton('Reset')
        self.reset_button.setToolTip('Back to 10 m spacing and no rotation')
        self.reset_button.setFlat(True)
        foot = QHBoxLayout()
        foot.addWidget(self.settings_button)
        foot.addStretch(1)
        foot.addWidget(self.reset_button)
        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.info)
        lay.addLayout(form)
        lay.addWidget(line)
        lay.addLayout(foot)
        self.load()
        self.on.toggled.connect(controller.set_enabled)
        self.spacing.valueChanged.connect(self._changed)
        self.unit.currentIndexChanged.connect(self._changed)
        self.rotation.valueChanged.connect(self._changed)
        self.settings_button.clicked.connect(self._open_settings)
        self.reset_button.clicked.connect(self._reset)

    def load(self):
        """Show the saved values (without writing them back)."""
        for widget in (self.on, self.spacing, self.unit, self.rotation):
            widget.blockSignals(True)
        self.on.setChecked(ks.grid_enabled())
        self.spacing.setValue(ks.grid_spacing())
        self.unit.setCurrentIndex(max(self.unit.findData(ks.grid_unit()), 0))
        self.rotation.setValue(ks.grid_rotation())
        for widget in (self.on, self.spacing, self.unit, self.rotation):
            widget.blockSignals(False)
        self.update_info()

    def update_info(self):
        text = self.controller.scaled_text()
        self.info.setText('Grid is dynamically scaled to: ' + text if text else '')
        self.info.setVisible(bool(text))

    def _changed(self, *_args):
        ks.set_value(ks.KEY_GRID_SPACING, self.spacing.value())
        ks.set_value(ks.KEY_GRID_UNIT, self.unit.currentData() or ks.GRID_UNIT_M)
        ks.set_value(ks.KEY_GRID_ROTATION, self.rotation.value())
        self.controller.refresh()

    def _reset(self):
        for key in (ks.KEY_GRID_SPACING, ks.KEY_GRID_UNIT, ks.KEY_GRID_ROTATION):
            ks.set_value(key, ks.DEFAULTS[key])
        self.load()
        self.controller.refresh()

    def _open_settings(self):
        self.controller.menu.hide()
        self.controller.open_settings()

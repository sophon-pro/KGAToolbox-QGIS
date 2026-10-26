# -*- coding: utf-8 -*-
"""The New Layout dialog: name, page size, orientation, background, neatline."""

from qgis.gui import QgsColorButton
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core import layout_import

CUSTOM = 'Custom'


class NewLayoutDialog(QDialog):
    def __init__(self, parent, default_name):
        super().__init__(parent)
        self.setWindowTitle('New Layout')
        self.setMinimumWidth(340)

        self.name_edit = QLineEdit(default_name)
        self.name_edit.selectAll()

        self.size_combo = QComboBox()
        for name, width, height in layout_import.PAGE_SIZES:
            self.size_combo.addItem(
                '{} ({:g} x {:g} mm)'.format(name, width, height), name)
        self.size_combo.addItem('Custom size', CUSTOM)
        self.size_combo.setCurrentIndex(
            self.size_combo.findData(layout_import.DEFAULT_SIZE))

        self.width_spin = self._spin(210.0)
        self.height_spin = self._spin(297.0)
        custom = QWidget()
        row = QHBoxLayout(custom)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.width_spin)
        row.addWidget(QLabel('x'))
        row.addWidget(self.height_spin)
        row.addWidget(QLabel('mm'))
        self.custom_row = custom

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem('Portrait', False)
        self.orientation_combo.addItem('Landscape', True)

        self.color_button = QgsColorButton()
        self.color_button.setColor(QColor('white'))
        self.color_button.setAllowOpacity(False)
        self.color_button.setColorDialogTitle('Background color')

        self.neatline_check = QCheckBox('Neatline (frame inside the page edge)')
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(0.0, 100.0)
        self.offset_spin.setDecimals(1)
        self.offset_spin.setSuffix(' mm')
        self.offset_spin.setValue(layout_import.NEATLINE_INSET)
        self.offset_label = QLabel('Neatline offset:')

        form = QFormLayout()
        form.addRow('Layout name:', self.name_edit)
        form.addRow('Size:', self.size_combo)
        self.custom_label = QLabel('Custom size:')
        form.addRow(self.custom_label, self.custom_row)
        form.addRow('Orientation:', self.orientation_combo)
        form.addRow('Background color:', self.color_button)
        form.addRow('', self.neatline_check)
        form.addRow(self.offset_label, self.offset_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.neatline_check.toggled.connect(self._neatline_toggled)
        self._neatline_toggled(False)
        self.size_combo.currentIndexChanged.connect(self._size_changed)
        self._size_changed()

    @staticmethod
    def _spin(value):
        spin = QDoubleSpinBox()
        spin.setRange(10.0, 5000.0)
        spin.setDecimals(1)
        spin.setValue(value)
        return spin

    def _size_changed(self, *_args):
        is_custom = self.size_combo.currentData() == CUSTOM
        self.custom_row.setVisible(is_custom)
        self.custom_label.setVisible(is_custom)
        self.adjustSize()

    def _neatline_toggled(self, checked):
        self.offset_spin.setVisible(checked)
        self.offset_label.setVisible(checked)
        self.adjustSize()

    def name(self):
        return self.name_edit.text().strip()

    def dimensions(self):
        """(width, height) in mm with the chosen orientation applied."""
        return layout_import.page_dimensions(
            self.size_combo.currentData(),
            bool(self.orientation_combo.currentData()),
            self.width_spin.value(), self.height_spin.value())

    def options(self):
        width, height = self.dimensions()
        return {
            'width': width,
            'height': height,
            'background': self.color_button.color(),
            'neatline': self.neatline_check.isChecked(),
            'neatline_offset': self.offset_spin.value(),
        }


def ask(parent, default_name):
    """(name, options) from the dialog, or None when cancelled/unnamed."""
    dialog = NewLayoutDialog(parent, default_name)
    if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.name():
        return None
    return dialog.name(), dialog.options()

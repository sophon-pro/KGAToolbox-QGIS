# -*- coding: utf-8 -*-
"""A form layout whose hidden rows take no room.

`QFormLayout` keeps a row's vertical spacing even when every widget in the row
is hidden, so a window that shows and hides rows by mode ends up with random
wide gaps. This is a `QGridLayout` with the small part of the `QFormLayout`
interface those windows use (`addRow`, `labelForField`), and a grid collapses a
row whose widgets are all hidden, spacing included.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QGridLayout, QLabel, QWidget


class TightForm(QGridLayout):
    """Label on the left, field on the right, hidden rows gone."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setHorizontalSpacing(10)
        self.setVerticalSpacing(6)
        self.setColumnStretch(1, 1)
        self._labels = {}
        self._row = 0

    def addRow(self, label, field):
        """Add `label` (text or widget) and `field` (widget or layout)."""
        if isinstance(label, str):
            label = QLabel(label)
        row = self._row
        self._row += 1
        if label is not None:
            self.addWidget(label, row, 0,
                           Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter)
        if isinstance(field, QWidget):
            self.addWidget(field, row, 1)
            self._labels[field] = label
        else:
            self.addLayout(field, row, 1)
        return row

    def labelForField(self, widget):
        return self._labels.get(widget)

    def setFieldGrowthPolicy(self, *_args):
        """Fields always grow; kept so the call sites read the same."""

# -*- coding: utf-8 -*-
"""A list of layers with a tick box each, plus Select all / Clear.

Used by the geodatabase windows to choose which layers of the source are
converted. Ticking everything means "no filter", so the tool is given an empty
layer list rather than a long one.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QHBoxLayout, QListWidget, QListWidgetItem,
                                 QPushButton, QVBoxLayout, QWidget)


class LayerChecklist(QWidget):

    changed = pyqtSignal()

    def __init__(self, parent=None, height=110):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self.list = QListWidget(self)
        self.list.setFixedHeight(height)
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        lay.addWidget(self.list)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.all_btn = QPushButton(self.tr('Select all'), self)
        self.none_btn = QPushButton(self.tr('Clear'), self)
        for button in (self.all_btn, self.none_btn):
            button.setFlat(True)
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn.clicked.connect(lambda: self._set_all(False))
        row.addWidget(self.all_btn)
        row.addWidget(self.none_btn)
        row.addStretch(1)
        lay.addLayout(row)

    def clear(self):
        self.list.blockSignals(True)
        self.list.clear()
        self.list.blockSignals(False)

    def set_items(self, items, ticked=None):
        """items: [(layer name, text shown)].

        `ticked` is the collection of names to tick; None ticks everything.
        """
        self.list.blockSignals(True)
        self.list.clear()
        for name, text in items:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = ticked is None or name in ticked
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.changed.emit()

    def _set_all(self, on):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(
                Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self.changed.emit()

    def names(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.list.count())]

    def checked(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def only(self):
        """Comma-separated names to convert, or '' when every layer is ticked."""
        picked = self.checked()
        return '' if len(picked) == self.list.count() else ','.join(picked)

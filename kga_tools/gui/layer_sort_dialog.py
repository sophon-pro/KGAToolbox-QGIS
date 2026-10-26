"""Layer Auto-Sort settings: drag the type order, switch auto-placement."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QLabel,
    QListWidget, QListWidgetItem, QVBoxLayout,
)

from ..core.layer_sort import (
    CATEGORIES, DEFAULT_ORDER, Ranker, load_settings, save_setting,
)

_LABELS = dict(CATEGORIES)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Layer Auto-Sort Settings")
        self.setMinimumWidth(420)
        cfg = load_settings()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Drag to set the order in the Layers panel (top of the list = top of the panel, drawn on top):"))

        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._fill(Ranker(cfg["order"]).order)
        layout.addWidget(self.list)

        self.auto_cb = QCheckBox("Automatically place new layers when they are added")
        self.auto_cb.setChecked(cfg["auto_sort"])
        layout.addWidget(self.auto_cb)

        self.sync_cb = QCheckBox(
            "Also sort the Layer Order panel when 'Control rendering order' is on")
        self.sync_cb.setChecked(cfg["sync_layer_order"])
        layout.addWidget(self.sync_cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            lambda *_: self._fill(DEFAULT_ORDER))
        layout.addWidget(buttons)

    def _fill(self, order):
        self.list.clear()
        for key in order:
            item = QListWidgetItem(_LABELS[key])
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(item)

    def order(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def accept(self):
        save_setting("order", ",".join(self.order()))
        save_setting("autoSort", self.auto_cb.isChecked())
        save_setting("syncLayerOrderPanel", self.sync_cb.isChecked())
        super().accept()

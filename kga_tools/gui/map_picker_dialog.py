# -*- coding: utf-8 -*-
"""Pick the map a new map frame in a layout will show: one card per map, with a preview."""

from contextlib import suppress
from qgis.core import (
    QgsCoordinateTransform,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

PREVIEW = QSize(240, 160)
CARD = QSize(264, 214)


def render_preview(layers, extent, crs, size=PREVIEW):
    """A pixmap of these layers (top first) over the extent; blank if there is nothing."""
    pixmap = QPixmap(size)
    pixmap.fill(QColor("white"))
    layers = [lyr for lyr in layers if lyr is not None and lyr.isValid()]
    if not layers:
        return pixmap
    settings = QgsMapSettings()
    settings.setLayers(layers)
    settings.setDestinationCrs(crs)
    settings.setOutputSize(size)
    settings.setBackgroundColor(QColor("white"))
    if extent is None or extent.isEmpty():
        extent = QgsRectangle()
        for lyr in layers:
            box = lyr.extent()
            if box.isNull() or box.isEmpty():
                continue
            with suppress(Exception):   # a bad transform just means no preview extent
                box = QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance()
                                             ).transformBoundingBox(box)
                extent.combineExtentWith(box)
        if extent.isNull() or extent.isEmpty():
            return pixmap
        extent.scale(1.1)
    settings.setExtent(extent)
    job = QgsMapRendererSequentialJob(settings)
    job.start()
    job.waitForFinished()
    return QPixmap.fromImage(job.renderedImage())


class MapPickerDialog(QDialog):
    """Cards for the project's maps; OK returns the chosen map group."""

    def __init__(self, parent, entries, current=None):
        """entries: [(group, layers, extent, crs)]; current: group to preselect."""
        super().__init__(parent)
        self.setWindowTitle("Add Map")
        self.resize(620, 460)
        self._groups = []

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Choose the map this frame will show:"))
        self.cards = QListWidget()
        self.cards.setViewMode(QListView.ViewMode.IconMode)
        self.cards.setResizeMode(QListView.ResizeMode.Adjust)
        self.cards.setMovement(QListView.Movement.Static)
        self.cards.setIconSize(PREVIEW)
        self.cards.setGridSize(CARD)
        self.cards.setSpacing(6)
        self.cards.setWordWrap(True)
        self.cards.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.cards.setStyleSheet(
            "QListWidget::item { border: 1px solid #c8ced4; border-radius: 3px; padding: 4px; }"
            "QListWidget::item:selected { border: 2px solid #005a9e; background: #cfe3f5;"
            " color: #000000; }")
        lay.addWidget(self.cards, 1)

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for group, layers, extent, crs in entries:
                count = len(layers)
                item = QListWidgetItem(
                    QIcon(render_preview(layers, extent, crs)),
                    f"{group.name()}\n{count} layer{'' if count == 1 else 's'}")
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                item.setSizeHint(CARD)
                self.cards.addItem(item)
                self._groups.append(group)
        finally:
            QApplication.restoreOverrideCursor()

        row = next((i for i, g in enumerate(self._groups) if g is current), 0)
        if self._groups:
            self.cards.setCurrentRow(row)
        self.cards.itemDoubleClicked.connect(lambda _item: self.accept())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setEnabled(bool(self._groups))
        lay.addWidget(buttons)

    def chosen(self):
        row = self.cards.currentRow()
        return self._groups[row] if 0 <= row < len(self._groups) else None

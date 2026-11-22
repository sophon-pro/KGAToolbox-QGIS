# -*- coding: utf-8 -*-
"""Pick the map a new map frame in a layout will show: one card per map, with a preview.

The dialog opens at once with placeholder cards; previews render in the background
(QgsMapRendererParallelJob, all maps at the same time) and fill in as they finish.
Rendering synchronously before showing the dialog made it take as long as every
map's layers - basemap tiles included - took to draw, one after another.
"""

import time
from contextlib import suppress
from qgis.core import (
    QgsCoordinateTransform,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
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
TIMEOUT_MS = 8000        # after this a slow preview (network tiles) shows what it has
CACHE_TTL = 300          # seconds a finished preview is reused on the next open
CACHE_MAX = 32

_CACHE = {}              # key -> (monotonic time, QPixmap)
_RUNNING = set()         # jobs cancelled on close, kept alive until their threads finish


def _blank(text=""):
    pixmap = QPixmap(PREVIEW)
    pixmap.fill(QColor("white"))
    if text:
        painter = QPainter(pixmap)
        painter.setPen(QColor("#8a8f94"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
    return pixmap


def _preview_settings(layers, extent, crs, size=PREVIEW):
    """Map settings for a preview of these layers (top first), or None if nothing to draw."""
    layers = [lyr for lyr in layers if lyr is not None and lyr.isValid()]
    if not layers:
        return None
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
            return None
        extent.scale(1.1)
    settings = QgsMapSettings()
    settings.setLayers(layers)
    settings.setDestinationCrs(crs)
    settings.setOutputSize(size)
    settings.setBackgroundColor(QColor("white"))
    settings.setExtent(extent)
    return settings


def _cache_key(layers, extent, crs):
    ids = tuple(lyr.id() for lyr in layers if lyr is not None)
    box = extent.toString(6) if extent is not None and not extent.isEmpty() else ""
    return ids, box, crs.authid() or crs.toWkt()


def _cached(key):
    hit = _CACHE.get(key)
    if hit is None:
        return None
    if time.monotonic() - hit[0] > CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return hit[1]


def _store(key, pixmap):
    _CACHE[key] = (time.monotonic(), pixmap)
    if len(_CACHE) > CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)


def _drop(job):
    _RUNNING.discard(job)


class MapPickerDialog(QDialog):
    """Cards for the project's maps; OK returns the chosen map group."""

    def __init__(self, parent, entries, current=None):
        """entries: [(group, layers, extent, crs)]; current: group to preselect."""
        super().__init__(parent)
        self.setWindowTitle("Add Map")
        self.resize(620, 460)
        self._groups = []
        self._jobs = {}          # job -> (item, cache key)

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

        loading = QIcon(_blank("Loading preview…"))
        empty = QIcon(_blank())
        for group, layers, extent, crs in entries:
            count = len(layers)
            item = QListWidgetItem(
                loading, f"{group.name()}\n{count} layer{'' if count == 1 else 's'}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setSizeHint(CARD)
            self.cards.addItem(item)
            self._groups.append(group)
            key = _cache_key(layers, extent, crs)
            pixmap = _cached(key)
            if pixmap is not None:
                item.setIcon(QIcon(pixmap))
                continue
            settings = _preview_settings(layers, extent, crs)
            if settings is None:
                item.setIcon(empty)
                continue
            job = QgsMapRendererParallelJob(settings)
            self._jobs[job] = (item, key)
            job.finished.connect(lambda j=job: self._on_rendered(j))

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

        for job in list(self._jobs):
            job.start()
        if self._jobs:
            self._timeout = QTimer(self)
            self._timeout.setSingleShot(True)
            self._timeout.timeout.connect(self._on_timeout)
            self._timeout.start(TIMEOUT_MS)

    def _on_rendered(self, job):
        entry = self._jobs.pop(job, None)
        if entry is None:
            return
        item, key = entry
        image = job.renderedImage()
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        _store(key, pixmap)
        item.setIcon(QIcon(pixmap))

    def _on_timeout(self):
        """Slow previews show what has drawn so far; not cached, so a later open retries."""
        for job, (item, _key) in list(self._jobs.items()):
            image = job.renderedImage()
            if not image.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(image)))
        self._stop_jobs()

    def _stop_jobs(self):
        for job in list(self._jobs):
            with suppress(TypeError, RuntimeError):
                job.finished.disconnect()
            if job.isActive():
                _RUNNING.add(job)               # keep the C++ job alive until its threads end
                job.finished.connect(lambda j=job: _drop(j))
                job.cancelWithoutBlocking()
        self._jobs.clear()

    def done(self, result):
        self._stop_jobs()
        super().done(result)

    def chosen(self):
        row = self.cards.currentRow()
        return self._groups[row] if 0 <= row < len(self._groups) else None

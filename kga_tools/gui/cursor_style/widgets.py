"""Small reusable widgets."""

from qgis.PyQt.QtCore import Qt, pyqtSignal, QSize, QRectF, QPointF
from qgis.PyQt.QtGui import QPixmap, QPainter, QColor, QPainterPath, QIcon, QFont
from qgis.PyQt.QtWidgets import (QWidget, QHBoxLayout, QLabel, QSlider, QSpinBox,
                                 QListWidget, QListView, QAbstractItemView, QFrame)


class SliderSpin(QWidget):
    """Label + slider + spin box kept in sync; emits valueChanged(int)."""
    valueChanged = pyqtSignal(int)

    def __init__(self, label, lo, hi, suffix=" px", tooltip="", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.label = QLabel(label)
        self.label.setMinimumWidth(104)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setSuffix(suffix)
        self.spin.setMinimumWidth(68)
        self.spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spin.setKeyboardTracking(False)
        for w in (self, self.slider, self.spin, self.label):
            w.setToolTip(tooltip)
        lay.addWidget(self.label)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _from_slider(self, v):
        if self.spin.value() != v:
            self.spin.blockSignals(True)
            self.spin.setValue(v)
            self.spin.blockSignals(False)
        self.valueChanged.emit(v)

    def _from_spin(self, v):
        if self.slider.value() != v:
            self.slider.blockSignals(True)
            self.slider.setValue(v)
            self.slider.blockSignals(False)
        self.valueChanged.emit(v)

    def value(self):
        return self.spin.value()

    def setValue(self, v):
        """Set without emitting."""
        for w in (self.slider, self.spin):
            w.blockSignals(True)
            w.setValue(int(v))
            w.blockSignals(False)


class Gallery(QListWidget):
    """Icon grid that grows to show all items (no inner scrollbar); the dock's
    scroll area handles scrolling."""

    def __init__(self, icon=44, cell=(84, 82), parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(QSize(icon, icon))
        self.setGridSize(QSize(*cell))
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSpacing(0)
        self._cell = cell
        self.setStyleSheet(
            "QListWidget{background:transparent;border:none;}"
            "QListWidget::item{border:1px solid transparent;border-radius:8px;padding:2px;}"
            "QListWidget::item:hover{border-color:palette(mid);}"
            "QListWidget::item:selected{border:1px solid palette(highlight);"
            "background:rgba(127,127,127,0.20);color:palette(window-text);}")

    def fit_height(self):
        n = self.count()
        cols = max(1, (self.viewport().width() or self.width()) // self._cell[0])
        rows = max(1, -(-n // cols)) if n else 0
        self.setFixedHeight(rows * self._cell[1] + 4 if n else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_height()


def tile_icon(pixmap, size, dpr):
    """Rounded tile, light top-left / dark bottom-right, cursor centred: shows
    at a glance whether a style survives both backgrounds."""
    out = QPixmap(int(size * dpr), int(size * dpr))
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    r = QRectF(0.5, 0.5, size - 1, size - 1)
    clip = QPainterPath()
    clip.addRoundedRect(r, 7, 7)
    p.setClipPath(clip)
    p.fillRect(r, QColor("#eeeeea"))
    tri = QPainterPath()
    tri.moveTo(r.right(), r.top() + size * 0.15)
    tri.lineTo(r.right(), r.bottom())
    tri.lineTo(r.left() + size * 0.15, r.bottom())
    tri.closeSubpath()
    p.fillPath(tri, QColor("#22272e"))
    p.setClipping(False)
    if pixmap is not None and not pixmap.isNull():
        lw = pixmap.width() / pixmap.devicePixelRatio()
        lh = pixmap.height() / pixmap.devicePixelRatio()
        maxd = size - 6
        if lw > maxd or lh > maxd:
            k = maxd / max(lw, lh)
            pm = pixmap.scaled(int(pixmap.width() * k), int(pixmap.height() * k),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            pm.setDevicePixelRatio(pixmap.devicePixelRatio())
            lw, lh, pixmap = lw * k, lh * k, pm
        p.drawPixmap(QPointF((size - lw) / 2, (size - lh) / 2), pixmap)
    p.setPen(QColor(0, 0, 0, 50))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(r, 7, 7)
    p.end()
    return QIcon(out)


def section_label(text):
    lab = QLabel(text.upper())
    f = QFont(lab.font())
    f.setBold(True)
    f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
    lab.setFont(f)
    lab.setContentsMargins(0, 6, 0, 0)
    return lab

# -*- coding: utf-8 -*-
"""Small custom widgets: toggle switch, responsive icon strip and segmented control."""
from qgis.PyQt.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QSizePolicy, QToolButton, QToolTip, QWidget,
)

from .style import colors


class ToggleSwitch(QAbstractButton):
    """A compact on/off switch that follows the palette's highlight colour."""

    W, H = 36, 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.W, self.H)

    def sizeHint(self):  # noqa: N802
        return QSize(self.W, self.H)

    def paintEvent(self, _event):  # noqa: N802
        c = colors(self.palette())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        track = QRectF(1, 2, self.W - 2, self.H - 4)
        p.setBrush(c["hl"] if self.isChecked() else c["track_off"])
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        d = self.H - 4
        x = self.W - d - 1 if self.isChecked() else 1
        p.setBrush(Qt.GlobalColor.white)
        p.drawEllipse(QRectF(x + 1.5, 3.5, d - 3, d - 3))
        p.end()


class InfoButton(QAbstractButton):
    """A small outlined circle with a '?', drawn in the palette's highlight colour."""

    SIZE = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.SIZE, self.SIZE)

    def sizeHint(self):  # noqa: N802
        return QSize(self.SIZE, self.SIZE)

    def paintEvent(self, _event):  # noqa: N802
        c = colors(self.palette())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hot = self.underMouse()
        p.setPen(QPen(c["hl"], 1.3))
        p.setBrush(c["tint"] if hot else c["base"])
        p.drawEllipse(QRectF(1, 1, self.SIZE - 2, self.SIZE - 2))
        font = QFont(self.font())
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRectF(0, 0, self.SIZE, self.SIZE), int(Qt.AlignmentFlag.AlignCenter), "?")
        p.end()

    def enterEvent(self, event):  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.update()
        super().leaveEvent(event)


class IconStrip(QWidget):
    """Paints as many tool icons as fit the current width, then '+N'.

    Hovering an icon shows the name of that tool.
    """

    ICON, GAP = 18, 5

    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self._icons = [(a.icon(), a.text().replace("&", "")) for a in actions]
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.ICON + 2)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

    def _layout(self):
        step = self.ICON + self.GAP
        fit = max(1, (self.width() + self.GAP) // step)
        n = len(self._icons)
        if n <= fit:
            return n, 0
        shown = max(0, fit - 1)  # keep one slot for the "+N" badge
        return shown, n - shown

    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        shown, extra = self._layout()
        step = self.ICON + self.GAP
        for i in range(shown):
            icon = self._icons[i][0]
            icon.paint(p, i * step, 1, self.ICON, self.ICON)
        if extra:
            p.setPen(colors(self.palette())["muted"])
            p.drawText(QRectF(shown * step, 0, 40, self.ICON + 2),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "+{}".format(extra))
        p.end()

    def event(self, ev):
        if ev.type() == QEvent.Type.ToolTip:
            shown, extra = self._layout()
            idx = int(ev.pos().x() // (self.ICON + self.GAP))
            if idx < shown:
                QToolTip.showText(ev.globalPos(), self._icons[idx][1], self)
            elif extra:
                QToolTip.showText(ev.globalPos(), "\n".join(t for _, t in self._icons[shown:]), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)


class Segmented(QFrame):
    """A row of mutually exclusive buttons, styled as a segmented control."""

    changed = pyqtSignal(int)

    def __init__(self, labels, expand=False, parent=None):
        super().__init__(parent)
        self.setObjectName("uiSeg")
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = []
        for i, text in enumerate(labels):
            btn = QToolButton()
            btn.setText(text)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if expand:
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self.group.addButton(btn, i)
            row.addWidget(btn)
            self.buttons.append(btn)
        self.buttons[0].setChecked(True)
        self.group.buttonClicked.connect(lambda b: self.changed.emit(self.buttons.index(b)))

    def index(self):
        for i, b in enumerate(self.buttons):
            if b.isChecked():
                return i
        return 0

    def set_index(self, i):
        self.buttons[i].setChecked(True)

    def set_text(self, i, text):
        self.buttons[i].setText(text)
